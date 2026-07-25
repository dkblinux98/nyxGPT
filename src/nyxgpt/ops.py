"""Operational commands for `nyxgpt ops`: install, status, doctor, restart, logs, env-sync.

Wraps the native (Homebrew services + LaunchAgents) and Docker-managed
(Cassandra container, Docker Compose stack) pieces of a local nyxGPT
deployment behind a single CLI surface, so operators never need to run raw
`brew`/`docker`/`launchctl` commands themselves. Also cross-checks for a
Compose deployment running alongside the native one so `status`/`restart`
can warn about -- and refuse to create -- port collisions between the two.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nyxgpt import self_heal
from nyxgpt.config import get_log_aggregation_enabled

logger = logging.getLogger(__name__)

# Repo root: .../nyxGPT/src/nyxgpt/ops.py -> parents[2] is repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# Maps a logical component to its Homebrew service name for native mode.
# Cassandra has no native brew service -- per product_management/PHASE_6_PLAN.md it stays the one
# ops-managed Docker container even under native-first, so it's tracked via
# `_docker_container_state` instead (see `detect_deployment_mode`).
NATIVE_BREW_SERVICES: dict[str, str] = {
    "api": "nyxgpt-api",
    "web": "nyxgpt-web",
    "ollama": "ollama",
}

# Host port each component binds to under Docker Compose (see docker-compose.yml).
# Used only for collision messaging -- detection itself is state-based.
COMPOSE_COMPONENT_PORTS: dict[str, int] = {
    "api": 8000,
    "web": 3000,
    "ollama": 11434,
    "cassandra": 9042,
}

NATIVE_CONFIG_HINT = "~/.nyxGPT/config.ini"
COMPOSE_CONFIG_HINT = "docker/config.docker.ini (mounted into the Compose 'api' container)"

# Referenced by `_ensure_log_symlinks` to detect Compose mode and avoid
# clobbering the file `follow-ollama-logs.sh`/its LaunchAgent is actively
# writing to (see #3276 review).
OLLAMA_CONTAINER_NAME = "nyxgpt-ollama"

# Placeholder substituted with the installing user's home directory when a
# LaunchAgent plist template is copied into ~/Library/LaunchAgents -- the
# templates in ops/launchagents/ must never hard-code a real account's home
# directory (see #3276 acceptance failure: the merged plists hard-coded the
# original author's `/Users/darlabaker`, so the installed LaunchAgent pointed
# at a nonexistent script path for every other user).
LAUNCHAGENT_HOME_PLACEHOLDER = "__NYXGPT_HOME__"

# Container path promtail's docker-compose.yml service binds to native-mode
# host logs (~/.nyxGPT/logs). `_log_aggregation_wiring_issue` greps for this
# marker to catch a regression (see #3277) where that bind mount is dropped
# and native-mode logs silently stop reaching Loki.
PROMTAIL_NATIVE_LOG_MOUNT_MARKER = "/var/log/nyxgpt-native/logs"

# --- Container data layout (see docs/docker-compose.md#volumes, issue #3346) ---
#
# All container data lives in plain host bind mounts under ~/.nyxGPT/volumes/
# instead of opaque named Docker volumes -- visible/attributable on the host
# filesystem and, for the three "shared" directories below, reused as-is by
# every deployment mode that mounts them (rather than each mode keeping its
# own duplicate copy). `VOLUME_DIR_NAMES` maps a logical component name to
# its directory under ~/.nyxGPT/volumes/; `volume_dir()` resolves the full
# path. Kept in this module (rather than config.py) since it's purely a
# Docker/ops concern -- native processes' own state (~/.nyxGPT/config.ini,
# ~/.nyxGPT/logs, ...) is unrelated and untouched by any of this.
VOLUME_DIR_NAMES: dict[str, str] = {
    # Shared across every mode that mounts them: the native `nyxgpt-cassandra`
    # container (`_ensure_cassandra_container` below), docker-compose.yml, and
    # terraform/main.tf all bind to the *same* ~/.nyxGPT/volumes/cassandra and
    # ~/.nyxGPT/volumes/nyxgpt-data directories -- ollama is shared between
    # Compose and Terraform only (native Ollama isn't containerized; it keeps
    # its own Homebrew-managed model store).
    "cassandra": "cassandra",
    "ollama": "ollama",
    "nyxgpt-data": "nyxgpt-data",
    # Compose-only today (no native/Terraform equivalent), so no per-mode
    # duplication to worry about -- see OBSERVABILITY_PROFILES.
    "prometheus": "prometheus",
    "grafana": "grafana",
    "loki": "loki",
    "glitchtip-postgres": "glitchtip-postgres",
    "glitchtip-uploads": "glitchtip-uploads",
}


def volume_dir(component: str) -> Path:
    """Return `~/.nyxGPT/volumes/<component>`'s host bind-mount path, creating it if needed."""
    p = Path.home() / ".nyxGPT" / "volumes" / VOLUME_DIR_NAMES[component]
    _ensure_dir(p)
    return p


@dataclass(frozen=True)
class DeploymentMode:
    """Snapshot of what's actually running, native vs. Docker Compose.

    `native`/`compose` map component name -> a state string ("started"/"running"/
    "none"/"absent"/...); `conflicts` lists components reported live in both.
    """

    native: dict[str, str]
    compose: dict[str, str]
    conflicts: list[str]


@dataclass(frozen=True)
class OpsResult:
    """Outcome of a single ops step: whether it succeeded, plus human-readable detail."""

    ok: bool
    message: str
    details: str = ""


def _emit_results(action: str, results: list[OpsResult]) -> bool:
    """Print and structured-log each OpsResult from an ops step, returning overall success.

    Preserves the `[OK]`/`[FAIL]` stdout lines every CLI entrypoint already
    printed, and additionally logs one INFO/WARNING record per result
    (service/action/result plus any subprocess failure detail in `details`)
    so `nyxgpt ops` activity lands in the log files instead of only stdout.
    """
    ok = True
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        log = logger.info if r.ok else logger.warning
        log(
            "ops: %s %s: %s",
            action,
            "ok" if r.ok else "failed",
            r.message,
            extra={
                "component": "ops",
                "action": action,
                "ok": r.ok,
                "result_message": r.message,
                "details": r.details,
            },
        )
        ok = ok and r.ok
    return ok


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text.

    Raises `subprocess.CalledProcessError` on non-zero exit unless `check=False`.
    """
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _which(prog: str) -> str | None:
    """Return the absolute path to `prog` on PATH, or None if it isn't found."""
    return shutil.which(prog)


def _read_project_version() -> str:
    """Return the project version from pyproject.toml, or a fallback if unreadable."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "1.0.0.md"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", "1.0.0.md"))


def _ensure_dir(p: Path) -> None:
    """Create directory `p` (and any missing parents) if it doesn't already exist."""
    p.mkdir(parents=True, exist_ok=True)


def _copy_file(src: Path, dst: Path, *, mode: int | None = None) -> None:
    """Copy `src` to `dst`, creating `dst`'s parent directory and optionally chmod'ing it.

    Args:
        src: Source file path.
        dst: Destination file path.
        mode: If given, permission bits applied to `dst` after copying (e.g. 0o755).
    """
    _ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    if mode is not None:
        os.chmod(dst, mode)


def _sha256_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of the file at `path`."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _brew_prefix() -> Path:
    """Return Homebrew's install prefix (`brew --prefix`), or `/opt/homebrew` if unavailable."""
    try:
        cp = _run(["brew", "--prefix"])
        return Path((cp.stdout or "").strip())
    except Exception:
        return Path("/opt/homebrew")


def _tap_repo(tap: str) -> Path:
    """Return the local checkout path of Homebrew tap `tap` (`brew --repo <tap>`)."""
    cp = _run(["brew", "--repo", tap])
    return Path((cp.stdout or "").strip())


# --- Deployment mode detection ---


def _brew_services_snapshot() -> dict[str, str]:
    """Return {brew_service_name: state} parsed from `brew services list`."""
    if _which("brew") is None:
        return {}
    cp = _run(["brew", "services", "list"], check=False)
    snapshot: dict[str, str] = {}
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            snapshot[parts[0]] = parts[1]
    return snapshot


def _docker_container_state(name: str) -> str:
    """Return the docker state ('running', 'exited', ...) for a container, or 'absent'."""
    if _which("docker") is None:
        return "absent"
    cp = _run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"],
        check=False,
    )
    out = (cp.stdout or "").strip()
    return out.splitlines()[0].strip() if out else "absent"


def _compose_stack_snapshot() -> dict[str, str]:
    """Return {service: state} for the docker-compose.yml stack, if any is running.

    Reuses self_heal.list_component_status(), which already knows how to resolve
    and query the project's docker-compose.yml via `docker compose ps -a`.
    """
    try:
        statuses = self_heal.list_component_status()
    except Exception:
        return {}
    return {s.service: s.state for s in statuses}


def detect_deployment_mode() -> DeploymentMode:
    """Detect which deployment(s) are actually running: native vs. Docker Compose.

    `ops.py` otherwise assumes native-only (Homebrew services + one ops-managed
    Cassandra container). This cross-checks the Compose stack so `status`/`restart`
    can see -- and avoid colliding with -- a Compose deployment left running.
    """
    brew_snapshot = _brew_services_snapshot()
    native = {
        component: brew_snapshot.get(brew_name, "none")
        for component, brew_name in NATIVE_BREW_SERVICES.items()
    }
    native["cassandra"] = _docker_container_state("nyxgpt-cassandra")

    compose = _compose_stack_snapshot()

    conflicts = [
        component
        for component in COMPOSE_COMPONENT_PORTS
        if native.get(component) in ("started", "running") and compose.get(component) == "running"
    ]

    logger.debug(
        "ops: detected deployment mode (native=%s, compose=%s, conflicts=%s)",
        native,
        compose,
        conflicts,
        extra={
            "component": "ops",
            "action": "detect_deployment_mode",
            "native": native,
            "compose": compose,
            "conflicts": conflicts,
        },
    )
    if conflicts:
        logger.warning(
            "ops: native/Compose deployment conflict on %s -- both report running on the "
            "shared port",
            ", ".join(sorted(conflicts)),
            extra={"component": "ops", "action": "detect_deployment_mode", "conflicts": conflicts},
        )

    return DeploymentMode(native=native, compose=compose, conflicts=conflicts)


# --- Restart helpers ---


def _restart_brew_service(name: str) -> list[OpsResult]:
    """Restart Homebrew service `name` via `brew services restart`.

    Returns a single-element list: an OpsResult reporting brew missing, the
    restart command's success, or its failure with captured stdout/stderr.
    """
    if _which("brew") is None:
        return [OpsResult(False, f"brew not found; cannot restart {name}")]
    try:
        cp = _run(["brew", "services", "restart", name], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Restarted brew service: {name}")]
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return [OpsResult(False, f"Failed to restart brew service: {name}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart brew service: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _restart_docker_container(name: str) -> list[OpsResult]:
    """Restart Docker container `name` via `docker restart`, with a start-back-up recovery attempt.

    If the container was running before the restart and `docker restart`
    leaves it stopped (e.g. a port collision on the start half), this makes
    one `docker start` attempt to bring it back up rather than silently
    leaving a previously healthy container down.

    Returns a single-element list of OpsResult describing success, a
    recovered restart failure, or an unrecovered "DOWN" failure.
    """
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot restart {name}")]

    was_running = _docker_container_state(name) == "running"

    try:
        cp = _run(["docker", "restart", name], check=False)
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart docker container: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]

    if cp.returncode == 0:
        return [OpsResult(True, f"Restarted docker container: {name}")]

    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )

    if was_running and _docker_container_state(name) != "running":
        # `docker restart` stops then starts the container -- if the start half fails
        # (e.g. a port collision) the container is left stopped even though it was
        # healthy before. Try once to bring it back up rather than silently leaving a
        # previously-running container down with only a generic failure message.
        recovery = _run(["docker", "start", name], check=False)
        if recovery.returncode == 0 and _docker_container_state(name) == "running":
            return [
                OpsResult(
                    False,
                    f"Restart of {name} failed but the previously running container was recovered",
                    details.strip(),
                )
            ]
        return [
            OpsResult(
                False,
                f"DOWN: {name} failed to restart and is now STOPPED (recovery attempt also failed)",
                details.strip(),
            )
        ]

    return [OpsResult(False, f"Failed to restart docker container: {name}", details.strip())]


def _restart_launchagent(label: str) -> list[OpsResult]:
    """Restart the LaunchAgent `label` via `launchctl kickstart -k` in the current GUI domain.

    Returns a single-element list of OpsResult reporting success or failure
    (including any exception raised while invoking launchctl).
    """
    # Prefer a kickstart (restart) in the current GUI domain.
    domain = f"gui/{os.getuid()}/{label}"
    try:
        cp = _run(["launchctl", "kickstart", "-k", domain], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Restarted LaunchAgent: {label}")]
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return [OpsResult(False, f"Failed to restart LaunchAgent: {label}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart LaunchAgent: {label}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _find_launchagent_template(
    name: str = "com.nyxgpt.cassandra-logs.plist",
) -> tuple[Path | None, list[Path]]:
    """
    Locate a log-follower LaunchAgent template (by plist filename) inside the repo.
    Returns (path_or_none, candidates_checked).
    """
    candidates = [
        REPO_ROOT / "ops" / "launchagents" / name,
        REPO_ROOT / "ops" / "LaunchAgents" / name,
        REPO_ROOT / name,
        REPO_ROOT / "homebrew" / name,
    ]
    for p in candidates:
        try:
            if p.exists():
                return p, candidates
        except Exception:
            # If something odd happens (permissions, broken symlink), keep searching.
            continue
    return None, candidates


def _install_launchagent_from_template(tpl: Path, dst: Path) -> None:
    """Render a LaunchAgent plist template to `dst`, substituting
    `LAUNCHAGENT_HOME_PLACEHOLDER` with the installing user's actual home
    directory so the installed plist works for whichever account runs
    `nyxgpt ops install`, not just the template's original author.
    """
    _ensure_dir(dst.parent)
    text = tpl.read_text(encoding="utf-8")
    text = text.replace(LAUNCHAGENT_HOME_PLACEHOLDER, str(Path.home()))
    dst.write_text(text, encoding="utf-8")


def _install_scripts() -> list[OpsResult]:
    """Copy the run-web/follow-cassandra-logs/follow-ollama-logs helper scripts into
    ~/.nyxGPT/scripts, executable.

    Scripts not present in the repo's `scripts/` dir are skipped (reported
    as ok, since not every deployment needs them). Returns one OpsResult per
    script considered.
    """
    results: list[OpsResult] = []
    src_dir = REPO_ROOT / "scripts"
    dst_dir = Path.home() / ".nyxGPT" / "scripts"
    _ensure_dir(dst_dir)

    for name in ("run-web.sh", "follow-cassandra-logs.sh", "follow-ollama-logs.sh"):
        src = src_dir / name
        if not src.exists():
            # Not required — some users run the web/API without wrappers.
            results.append(OpsResult(True, f"Script not present (skipped): {name}", str(src)))
            continue
        dst = dst_dir / name
        _copy_file(src, dst, mode=0o755)
        results.append(OpsResult(True, f"Installed script {name}", str(dst)))

    return results


def _install_cassandra_launchagent() -> list[OpsResult]:
    """Install and (re)load the Cassandra log-follower LaunchAgent.

    Locates the plist template in the repo, copies it into
    ~/Library/LaunchAgents, then boots it out and back in via `launchctl
    bootout`/`bootstrap`/`kickstart` so a stale prior load doesn't linger.
    Returns a single-element list of OpsResult; fails if the template can't
    be found among the candidate paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template()
    if tpl is None:
        details = "Tried:\n" + "\n".join(str(p) for p in checked) + f"\nREPO_ROOT={REPO_ROOT}"
        results.append(OpsResult(False, "Missing Cassandra logs LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.cassandra-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Cassandra logs LaunchAgent", str(dst)))
    return results


def _install_ollama_launchagent() -> list[OpsResult]:
    """Install and (re)load the Ollama container-logs LaunchAgent (Compose mode).

    Mirrors `_install_cassandra_launchagent`: locates the plist template,
    copies it into ~/Library/LaunchAgents, then boots it out and back in via
    `launchctl bootout`/`bootstrap`/`kickstart`. Installed unconditionally by
    `nyxgpt ops install` regardless of deployment mode, same as the Cassandra
    LaunchAgent -- in native mode there's no `nyxgpt-ollama` Docker container
    yet, so `follow-ollama-logs.sh` just idles waiting for one (Ollama's
    native-mode logs instead reach ~/.nyxGPT/logs via the Homebrew log
    symlink in `_ensure_log_symlinks`). Returns a single-element list of
    OpsResult; fails if the template can't be found among the candidate
    paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template("com.nyxgpt.ollama-logs.plist")
    if tpl is None:
        details = "Tried:\n" + "\n".join(str(p) for p in checked) + f"\nREPO_ROOT={REPO_ROOT}"
        results.append(OpsResult(False, "Missing Ollama logs LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.ollama-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Ollama logs LaunchAgent", str(dst)))
    return results


def _ensure_log_symlinks() -> list[OpsResult]:
    """Symlink each Homebrew-managed service log into ~/.nyxGPT/logs for convenient access.

    Replaces any existing file/symlink at the destination. Returns one
    OpsResult per (component, extension) log symlink attempted.

    Ollama gets only `.log` (no `.err.log`): its Homebrew formula's service
    block points both StandardOutPath and StandardErrorPath at the same
    file, unlike nyxgpt-api/nyxgpt-web which get separate error logs. In
    Compose mode Ollama isn't a Homebrew service at all -- `nyxgpt-ollama-logs`
    (see `_install_ollama_launchagent`) follows the container's docker logs
    into this same `ollama.log` path instead.

    Skips the `ollama.log` entry entirely when a `nyxgpt-ollama` Docker
    container exists: in that case `follow-ollama-logs.sh` (via its
    LaunchAgent) owns that path and is actively appending to it, so
    replacing it with a (likely dangling, since there's no Homebrew Ollama
    log in Compose mode) symlink here would silently cut off Loki's view of
    Ollama's logs on every `nyxgpt ops install` re-run (see #3276 review).
    """
    results: list[OpsResult] = []
    home_logs = Path.home() / ".nyxGPT" / "logs"
    _ensure_dir(home_logs)

    brew_logs = _brew_prefix() / "var" / "log"
    targets: list[tuple[str, tuple[str, ...]]] = [
        ("nyxgpt-api", (".log", ".err.log")),
        ("nyxgpt-web", (".log", ".err.log")),
        ("ollama", (".log",)),
    ]
    for base, exts in targets:
        for ext in exts:
            dst = home_logs / f"{base}{ext}"
            if base == "ollama" and _docker_container_state(OLLAMA_CONTAINER_NAME) != "absent":
                results.append(
                    OpsResult(
                        True,
                        f"Skipped {dst.name} symlink (Compose-mode {OLLAMA_CONTAINER_NAME} "
                        "container owns this file via follow-ollama-logs.sh)",
                        str(dst),
                    )
                )
                continue
            src = brew_logs / f"{base}{ext}"
            try:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(src)
                results.append(OpsResult(True, f"Symlinked {dst.name}", f"{dst} -> {src}"))
            except Exception as e:
                results.append(OpsResult(False, f"Failed to symlink {dst.name}", str(e)))
    return results


def _create_dist_tarball(tap_dir: Path, name: str, version: str) -> Path:
    """Build a `<name>-<version>.tar.gz` placeholder distribution under `tap_dir/dist`.

    Writes a minimal README into a temp directory and tars it up, replacing
    any existing tarball of the same name/version. Used to give the
    generated Homebrew formula something to point its `url`/`sha256` at.

    Returns the path to the created tarball.
    """
    dist_dir = tap_dir / "dist"
    _ensure_dir(dist_dir)
    tar_path = dist_dir / f"{name}-{version}.tar.gz"

    tmp = dist_dir / f".tmp-{name}-{version}"
    if tmp.exists():
        shutil.rmtree(tmp)
    _ensure_dir(tmp)
    (tmp / "README.txt").write_text(
        f"{name} {version}\nGenerated by nyxgpt ops install\n",
        encoding="utf-8",
    )

    if tar_path.exists():
        tar_path.unlink()

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(tmp, arcname=f"{name}-{version}")

    shutil.rmtree(tmp, ignore_errors=True)
    return tar_path


def _brew_install_or_reinstall(spec: str, name: str) -> None:
    """`brew install` a formula, or `brew reinstall` when already installed.

    The formula version never changes between `ops install` runs, so a plain
    `brew install` skips the already-installed keg -- launcher scripts
    regenerated into the tap on every run would never reach the Cellar. The
    `fetch --force` refreshes brew's download cache first: the tarball URL and
    version are also constant across runs, so without it brew reinstalls from
    a stale cached tarball and fails the formula's sha256 check.
    """
    installed = _run(["brew", "list", "--versions", name], check=False).returncode == 0
    _run(["brew", "fetch", "--force", spec], check=False)
    if installed:
        _run(["brew", "reinstall", spec], check=False)
    else:
        _run(["brew", "install", "--overwrite", spec], check=False)


def _install_homebrew_api(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-api` Homebrew formula into `tap`, then start the service.

    Generates a dist tarball, patches the formula template's `sha256` to
    match it, writes the formula into the tap's Formula/ dir, and runs
    `brew install --overwrite` + `brew services start`. Returns a list of
    OpsResult; fails early if brew isn't installed or the formula template
    is missing.
    """
    results: list[OpsResult] = []
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    template = REPO_ROOT / "homebrew" / "nyxgpt-api.rb"
    if not template.exists():
        return [OpsResult(False, "Missing homebrew/nyxgpt-api.rb", str(template))]

    tap_dir = _tap_repo(tap)
    version = _read_project_version()

    tar = _create_dist_tarball(tap_dir, "nyxgpt-api", version)
    sha = _sha256_file(tar)

    content = template.read_text(encoding="utf-8")
    # Update sha256, version, and tarball filename to match the generated
    # tarball -- the template's hardcoded values go stale when the project
    # version changes, leaving brew fetching an old tarball whose checksum
    # can never match the freshly computed one.
    import re

    content = re.sub(r'sha256 "[a-f0-9]+"', f'sha256 "{sha}"', content)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)
    content = re.sub(r"nyxgpt-api-[^\"/]+\.tar\.gz", f"nyxgpt-api-{version}.tar.gz", content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-api.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-api formula", str(dst)))

    _brew_install_or_reinstall(f"{tap}/nyxgpt-api", "nyxgpt-api")
    _run(["brew", "services", "start", "nyxgpt-api"], check=False)
    results.append(OpsResult(True, "Requested brew install/start nyxgpt-api", ""))

    return results


def _install_homebrew_web(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-web` Homebrew formula into `tap`, then start the service.

    Generates a dist tarball, substitutes its `file://` URL and sha256 into
    the formula template, writes the formula into the tap's Formula/ dir,
    and runs `brew install --overwrite` + `brew services start`. Returns a
    list of OpsResult; fails early if brew isn't installed or the formula
    template is missing.
    """
    results: list[OpsResult] = []
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    template = REPO_ROOT / "homebrew" / "nyxgpt-web.rb"
    if not template.exists():
        return [OpsResult(False, "Missing homebrew/nyxgpt-web.rb", str(template))]

    tap_dir = _tap_repo(tap)
    version = _read_project_version()

    tar = _create_dist_tarball(tap_dir, "nyxgpt-web", version)
    sha = _sha256_file(tar)
    url = f"file://{tar}"

    import re

    content = template.read_text(encoding="utf-8")
    content = content.replace("__NYXGPT_WEB_URL__", url)
    content = content.replace("__NYXGPT_WEB_SHA256__", sha)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-web.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-web formula", str(dst)))

    _brew_install_or_reinstall(f"{tap}/nyxgpt-web", "nyxgpt-web")
    _run(["brew", "services", "start", "nyxgpt-web"], check=False)
    results.append(OpsResult(True, "Requested brew install/start nyxgpt-web", ""))

    return results


def _persist_compose_file_path() -> list[OpsResult]:
    """Record the repo's docker-compose.yml path in config.ini `[paths] compose_file`.

    The brew-installed native API runs from the Homebrew Cellar, where
    self_heal's module-path fallback can't find a docker-compose.yml -- so the
    self-heal watchdog and dashboard silently reported zero components. This
    gives `self_heal._resolve_compose_file` a config-based fallback that
    survives the Cellar layout. No-ops (successfully) when run outside a repo
    checkout or before `nyxgpt wizard` has created config.ini.
    """
    compose_path = REPO_ROOT / "docker-compose.yml"
    if not compose_path.exists():
        return [
            OpsResult(
                True,
                "Skipped compose-file path (not running from a repo checkout)",
                "self-heal keeps its current compose-file resolution.",
            )
        ]

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg_path.exists():
        return [
            OpsResult(
                True,
                "Skipped compose-file path (no config.ini yet)",
                "Run `nyxgpt wizard` to create config.ini, then re-run `nyxgpt ops install`.",
            )
        ]

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)
    if parser.get("paths", "compose_file", fallback="") == str(compose_path):
        return [OpsResult(True, f"Compose-file path already recorded: {compose_path}")]
    if not parser.has_section("paths"):
        parser.add_section("paths")
    parser.set("paths", "compose_file", str(compose_path))
    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)
    return [OpsResult(True, f"Recorded compose-file path in config.ini: {compose_path}")]


def _ensure_ollama_service() -> list[OpsResult]:
    """Ensure the native Ollama Homebrew service is installed and running.

    Reconciles to the intended state like `_ensure_cassandra_container`:
    already started -> no-op; installed but stopped -> `brew services start`;
    formula absent -> `brew install ollama` first, then start. Without this
    step, `ops install` only set up the Ollama *logs* LaunchAgent and never
    started Ollama itself, so chat/embeddings stayed down after an `ops down`
    until a manual `ops restart ollama`.
    """
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found; cannot ensure ollama service", "")]

    state = _brew_services_snapshot().get("ollama")

    if state == "started":
        return [OpsResult(True, "Ollama brew service already running")]

    results: list[OpsResult] = []
    if state is None:
        cp = _run(["brew", "install", "ollama"], check=False)
        if cp.returncode != 0:
            details = (cp.stdout or "").strip() + (
                "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
            )
            return [OpsResult(False, "Failed to brew install ollama", details.strip())]
        results.append(OpsResult(True, "Installed ollama formula"))

    cp = _run(["brew", "services", "start", "ollama"], check=False)
    if cp.returncode == 0:
        results.append(OpsResult(True, "Started brew service: ollama"))
    else:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        results.append(OpsResult(False, "Failed to start brew service: ollama", details.strip()))
    return results


def _ensure_web_deps() -> list[OpsResult]:
    """Ensure web/node_modules is present by running npm ci/install in ./web.

    This is intentionally part of ops install so users don't have to run `npm install` manually.
    """
    results: list[OpsResult] = []
    web_dir = REPO_ROOT / "web"
    if not web_dir.exists():
        return [OpsResult(True, "Web directory not present (skipped)", str(web_dir))]

    if _which("node") is None:
        return [
            OpsResult(
                False,
                "node not found; cannot install web deps",
                "Install Node.js and ensure `node` is on PATH",
            )
        ]
    if _which("npm") is None:
        return [
            OpsResult(
                False,
                "npm not found; cannot install web deps",
                "Install Node.js/npm and ensure `npm` is on PATH",
            )
        ]

    node_modules = web_dir / "node_modules"
    lockfile = web_dir / "package-lock.json"

    def _can_resolve(pkg: str) -> bool:
        try:
            cp = subprocess.run(
                ["node", "-p", f"require.resolve('{pkg}')"],
                cwd=str(web_dir),
                text=True,
                capture_output=True,
            )
            return cp.returncode == 0
        except Exception:
            return False

    # Check if node_modules exists and undici can be resolved
    if node_modules.exists() and _can_resolve("undici"):
        results.append(OpsResult(True, "Web deps already installed (undici OK)", str(node_modules)))
        return results

    def _run_npm(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, cwd=str(web_dir), text=True, capture_output=True)

    # Prefer npm ci when a lockfile exists, but if the lockfile is out of sync
    # (common after editing package.json), fall back to npm install so the lock
    # is updated and dependencies actually get installed.
    try:
        if lockfile.exists():
            cp = _run_npm(["npm", "ci"])
            if cp.returncode != 0:
                stderr = cp.stderr or ""
                # npm uses EUSAGE + a specific message when package-lock is out of sync
                if (
                    "can only install packages when your package.json and package-lock.json"
                    in stderr
                ):
                    cp2 = _run_npm(["npm", "install"])
                    if cp2.returncode != 0:
                        details = (cp2.stdout or "").strip() + (
                            "\n" + (cp2.stderr or "").strip() if (cp2.stderr or "").strip() else ""
                        )
                        results.append(
                            OpsResult(
                                False,
                                "Failed to install web deps via npm install (after npm ci mismatch)",
                                details.strip(),
                            )
                        )
                        return results
                    # npm install succeeded (and likely updated package-lock.json)
                    if not _can_resolve("undici"):
                        details = (cp2.stdout or "").strip() + (
                            "\n" + (cp2.stderr or "").strip() if (cp2.stderr or "").strip() else ""
                        )
                        results.append(
                            OpsResult(
                                False,
                                "Web deps installed but undici still missing",
                                details.strip(),
                            )
                        )
                        return results
                    results.append(
                        OpsResult(
                            True,
                            "Installed web deps via npm install (lockfile was out of sync; package-lock.json updated)",
                            str(node_modules),
                        )
                    )
                    return results

                # Other npm ci failure
                details = (cp.stdout or "").strip() + (
                    "\n" + stderr.strip() if stderr.strip() else ""
                )
                results.append(
                    OpsResult(False, "Failed to install web deps via npm ci", details.strip())
                )
                return results

            # npm ci succeeded
            if not _can_resolve("undici"):
                details = (cp.stdout or "").strip() + (
                    "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
                )
                results.append(
                    OpsResult(
                        False,
                        "Web deps installed but undici still missing",
                        details.strip(),
                    )
                )
                return results
            results.append(OpsResult(True, "Installed web deps via npm ci", str(node_modules)))
            return results

        # No lockfile: use npm install
        cp = _run_npm(["npm", "install"])
        if cp.returncode == 0:
            if not _can_resolve("undici"):
                details = (cp.stdout or "").strip() + (
                    "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
                )
                results.append(
                    OpsResult(
                        False,
                        "Web deps installed but undici still missing",
                        details.strip(),
                    )
                )
                return results
            results.append(OpsResult(True, "Installed web deps via npm install", str(node_modules)))
            return results

        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        results.append(
            OpsResult(False, "Failed to install web deps via npm install", details.strip())
        )
        return results

    except Exception as e:
        results.append(OpsResult(False, "Failed to install web deps", f"{type(e).__name__}: {e}"))
        return results


def _ensure_mcp_deps() -> list[OpsResult]:
    """Ensure root node_modules are present for Claude Code MCP servers."""
    results: list[OpsResult] = []
    root_dir = REPO_ROOT
    pkg_json = root_dir / "package.json"

    if not pkg_json.exists():
        return [OpsResult(True, "No root package.json found (skipped)", str(root_dir))]

    if _which("npm") is None:
        return [
            OpsResult(
                False,
                "npm not found; cannot install MCP deps",
                "Install Node.js/npm and ensure `npm` is on PATH",
            )
        ]

    sentinel = root_dir / "node_modules" / "@modelcontextprotocol" / "server-github"
    if sentinel.exists():
        results.append(
            OpsResult(True, "MCP deps already installed", str(root_dir / "node_modules"))
        )
        return results

    lockfile = root_dir / "package-lock.json"
    use_ci = lockfile.exists()
    npm_cmd = ["npm", "ci"] if use_ci else ["npm", "install"]

    try:
        cp = subprocess.run(npm_cmd, cwd=str(root_dir), text=True, capture_output=True)
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True,
                    f"Installed MCP deps via {' '.join(npm_cmd)}",
                    str(root_dir / "node_modules"),
                )
            )
        else:
            details = (cp.stdout or "").strip() + (
                "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
            )
            results.append(OpsResult(False, "Failed to install MCP deps", details.strip()))
    except Exception as e:
        results.append(OpsResult(False, "Failed to install MCP deps", f"{type(e).__name__}: {e}"))

    return results


# --- Local Cassandra container lifecycle ---

# Canonical definition of the one ops-managed Docker container in a native-mode
# local deployment (api/web/ollama run natively via Homebrew; see docs/ops.md).
# Mirrors the `cassandra` service in docker-compose.yml so the native and
# Compose paths agree on image/port/volume -- but this container is created and
# managed via plain `docker run`/`docker start`, entirely separate from the
# Compose "cloud/server" stack, so its lifecycle never requires (or pulls in)
# the rest of docker-compose.yml.
CASSANDRA_CONTAINER_NAME = "nyxgpt-cassandra"
# Keep this pin identical to the `cassandra` image tag in docker-compose.yml
# and terraform/main.tf (docker_image.cassandra) -- see docs/docker-compose.md
# for the image-pinning policy and how to bump all three together.
CASSANDRA_IMAGE = "cassandra:5.0.8"
# Must match the cluster name stamped into the Cassandra data directory's
# system keyspace: Cassandra refuses to start when the saved cluster name
# differs from the configured one, so a recreated container without this env
# crash-loops with "Saved cluster name nyxgpt != configured name Test Cluster".
CASSANDRA_CLUSTER_NAME = "nyxgpt"


def _ensure_cassandra_container() -> list[OpsResult]:
    """Ensure the local `nyxgpt-cassandra` Docker container exists and is running.

    Reconciles to the intended state rather than only adding:
    - running: nothing to do.
    - present but not running (exited/created/paused/...): `docker start` it.
    - absent: `docker run` a fresh container from `CASSANDRA_IMAGE`, bind-mounted to
      `volume_dir("cassandra")`, bound to `${NYXGPT_BIND_ADDR:-127.0.0.1}:${CASSANDRA_PORT:-9042}`.

    This is what `nyxgpt ops install` was missing entirely: without it, no
    `nyxgpt` command ever created the container in the first place, so
    `nyxgpt ops restart cassandra` (a plain `docker restart`) had nothing to
    restart on a fresh machine or after the container was removed.
    """
    if _which("docker") is None:
        return [
            OpsResult(
                False,
                "docker not found; cannot ensure local Cassandra container",
                "Install Docker Desktop (or the docker CLI) -- Cassandra is the one "
                "Docker-managed piece of a native-mode local install.",
            )
        ]

    # ~/.nyxGPT/volumes/cassandra is the same host directory docker-compose.yml
    # and terraform/main.tf bind-mount their `cassandra` service/container to --
    # two Cassandra processes writing to it concurrently would corrupt the data
    # directory, so refuse rather than create a second writer (see #3346).
    if terraform_stack_state().get("cassandra") == "running":
        return [
            OpsResult(
                False,
                "Refusing to start native Cassandra: the Terraform-managed Cassandra "
                f"container ({TERRAFORM_CONTAINERS['cassandra']}) is already running and "
                "shares the same ~/.nyxGPT/volumes/cassandra data directory",
                "Run `nyxgpt ops down --terraform` first, or skip native Cassandra.",
            )
        ]

    state = _docker_container_state(CASSANDRA_CONTAINER_NAME)

    if state == "running":
        return [OpsResult(True, f"Cassandra container already running: {CASSANDRA_CONTAINER_NAME}")]

    if state != "absent":
        cp = _run(["docker", "start", CASSANDRA_CONTAINER_NAME], check=False)
        if cp.returncode == 0:
            return [
                OpsResult(True, f"Started existing Cassandra container: {CASSANDRA_CONTAINER_NAME}")
            ]
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return [
            OpsResult(
                False,
                f"Failed to start existing Cassandra container: {CASSANDRA_CONTAINER_NAME}",
                details.strip(),
            )
        ]

    bind_addr = os.environ.get("NYXGPT_BIND_ADDR", "127.0.0.1")
    port = os.environ.get("CASSANDRA_PORT", "9042")
    data_dir = volume_dir("cassandra")
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        CASSANDRA_CONTAINER_NAME,
        "--restart",
        "unless-stopped",
        "-p",
        f"{bind_addr}:{port}:9042",
        "-v",
        f"{data_dir}:/var/lib/cassandra",
        "-e",
        f"CASSANDRA_CLUSTER_NAME={CASSANDRA_CLUSTER_NAME}",
        CASSANDRA_IMAGE,
    ]
    cp = _run(cmd, check=False)
    if cp.returncode == 0:
        return [
            OpsResult(
                True,
                f"Created Cassandra container: {CASSANDRA_CONTAINER_NAME} ({CASSANDRA_IMAGE})",
                f"Bound to {bind_addr}:{port}, data persisted in {data_dir}",
            )
        ]
    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )
    return [
        OpsResult(
            False,
            f"Failed to create Cassandra container: {CASSANDRA_CONTAINER_NAME}",
            details.strip(),
        )
    ]


# --- Legacy named-volume migration (issue #3346) ---
#
# Before #3346, container data lived in named Docker volumes rather than
# ~/.nyxGPT/volumes/ bind mounts: `nyxgpt_cassandra_data` (native, and -- by
# coincidence of docker-compose.yml's explicit `name: nyxgpt` project name --
# also Compose's `cassandra_data`), `nyxgpt_ollama_data`/`nyxgpt_data` (Compose),
# and `nyxgpt_tf_ollama_data`/`nyxgpt_tf_cassandra_data`/`nyxgpt_tf_nyxgpt_data`
# (Terraform). This copies that data into the new bind-mount directories on
# upgrade so it isn't silently lost. On macOS/Docker Desktop a named volume's
# files live inside the Docker VM, not directly reachable from the host
# filesystem, so the copy runs through a throwaway container rather than a
# plain filesystem copy.

# Pinned to a specific version (no floating/untagged reference), same policy
# as every other third-party image in this file -- see
# docs/docker-compose.md#image-pinning. This one is just a disposable `cp`
# runner, not a long-lived service, so it isn't part of that doc's
# pinned-images table.
MIGRATION_HELPER_IMAGE = "alpine:3.20.3"

# dest volume_dir() component -> legacy Docker volume names to check, in
# priority order (first one found with data wins; see
# `migrate_legacy_volumes`). Cassandra/Ollama/nyxgpt-data can have both a
# Compose-era and a Terraform-era candidate now that both modes share one
# destination directory; only one can be migrated in since merging two
# independent Cassandra data directories isn't safe.
LEGACY_VOLUME_SOURCES: dict[str, list[str]] = {
    "cassandra": ["nyxgpt_cassandra_data", "nyxgpt_tf_cassandra_data"],
    "ollama": ["nyxgpt_ollama_data", "nyxgpt_tf_ollama_data"],
    # Compose's volume key was literally `nyxgpt_data:`, which Compose prefixes
    # with the project name (`name: nyxgpt` in docker-compose.yml) to get
    # `nyxgpt_nyxgpt_data` -- not `nyxgpt_data`.
    "nyxgpt-data": ["nyxgpt_nyxgpt_data", "nyxgpt_tf_nyxgpt_data"],
    "prometheus": ["nyxgpt_prometheus_data"],
    "grafana": ["nyxgpt_grafana_data"],
    "loki": ["nyxgpt_loki_data"],
    "glitchtip-postgres": ["nyxgpt_glitchtip_postgres_data"],
    "glitchtip-uploads": ["nyxgpt_glitchtip_uploads"],
}


def _docker_volume_exists(name: str) -> bool:
    """Return whether a Docker volume named `name` currently exists."""
    if _which("docker") is None:
        return False
    cp = _run(["docker", "volume", "inspect", name], check=False)
    return cp.returncode == 0


def _migrate_docker_volume_to_bind_dir(
    volume_name: str, dest_dir: Path, *, label: str
) -> OpsResult:
    """Copy `volume_name`'s contents into `dest_dir` via a throwaway container, then remove it.

    `dest_dir` is assumed already created and empty (checked by the caller,
    `migrate_legacy_volumes`, so this can be tested/called standalone without
    re-deriving that check). Removal is best-effort -- a volume still attached
    to some other container is left behind with a note rather than failing
    the whole migration.
    """
    cp = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/from:ro",
            "-v",
            f"{dest_dir}:/to",
            MIGRATION_HELPER_IMAGE,
            "sh",
            "-c",
            "cp -a /from/. /to/",
        ],
        check=False,
    )
    if cp.returncode != 0:
        return OpsResult(
            False,
            f"Failed to migrate {label} data from legacy volume '{volume_name}'",
            _cp_details(cp),
        )

    rm = _run(["docker", "volume", "rm", volume_name], check=False)
    if rm.returncode == 0:
        return OpsResult(
            True,
            f"Migrated {label} data from legacy volume '{volume_name}' into {dest_dir}",
            "Old volume removed.",
        )
    return OpsResult(
        True,
        f"Migrated {label} data from legacy volume '{volume_name}' into {dest_dir}",
        f"Could not remove the old volume (still in use?): {_cp_details(rm)}",
    )


def _migration_marker_path(dest_name: str) -> Path:
    """Path to `migrate_legacy_volumes`'s per-component "already reconciled" marker.

    Deliberately stored outside `~/.nyxGPT/volumes/<component>/` rather than
    inferred from that directory being non-empty: a freshly-started container
    populates its empty bind mount with new files within seconds of `docker
    compose up`, which an emptiness check can't tell apart from "an earlier
    run already migrated the legacy volume in here" -- that ambiguity is what
    let pre-#3346 data get silently stranded. A marker only appears once this
    function has itself made that determination.
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / f"{dest_name}.migrated"


def migrate_legacy_volumes() -> list[OpsResult]:
    """Copy pre-#3346 named-volume data into ~/.nyxGPT/volumes/, then drop the old volumes.

    Idempotent and safe to run on every `nyxgpt ops install` (native and
    `--terraform --local` both do): once a component has been reconciled
    (migrated, or confirmed to have no legacy volume) that's recorded in a
    marker file under `~/.nyxGPT/.migration-state/`, and later runs skip it
    without re-touching `~/.nyxGPT/volumes/<component>/`. Also exposed
    standalone as `nyxgpt ops migrate-volumes` for Compose-only users who
    never run `nyxgpt ops install`.

    If a legacy volume is still found for a component whose destination
    directory is *not yet marked reconciled* but is already non-empty (e.g.
    the new bind-mounted stack was brought up before this ran), migration is
    refused with a loud, actionable failure rather than silently reporting
    success -- auto-merging risks silently overwriting or shadowing whichever
    side holds the data the user actually wants.
    """
    if _which("docker") is None:
        return [OpsResult(True, "Skipped legacy volume migration (docker not found)")]

    results: list[OpsResult] = []
    for dest_name, candidates in LEGACY_VOLUME_SOURCES.items():
        dest_dir = volume_dir(dest_name)
        marker = _migration_marker_path(dest_name)
        if marker.exists():
            results.append(OpsResult(True, f"{dest_name}: already reconciled (nothing to migrate)"))
            continue

        existing = [v for v in candidates if _docker_volume_exists(v)]
        if not existing:
            marker.touch()
            results.append(
                OpsResult(True, f"No legacy volume found for {dest_name} (nothing to migrate)")
            )
            continue

        found, *unmigrated = existing
        if any(dest_dir.iterdir()):
            results.append(
                OpsResult(
                    False,
                    f"{dest_name}: legacy volume '{found}' still holds pre-#3346 data but "
                    f"{dest_dir} is already non-empty -- refusing to auto-migrate. This can "
                    "happen if the new bind-mounted stack was started before running "
                    "`nyxgpt ops migrate-volumes`, which would otherwise silently strand the "
                    "old data.",
                    f"Stop the stack, inspect both {dest_dir} and the legacy volume "
                    f"(docker run --rm -v {found}:/legacy alpine ls -la /legacy), manually merge "
                    f"whichever side holds the data you need, then `docker volume rm {found}` "
                    "once done -- this warning repeats on every run until that volume is gone.",
                )
            )
            continue

        result = _migrate_docker_volume_to_bind_dir(found, dest_dir, label=dest_name)
        if unmigrated:
            note = (
                f"Note: legacy volume(s) {unmigrated} for {dest_name} were also found but not "
                "migrated (only one source can be merged in safely) -- inspect and remove "
                "manually if they hold data you still need."
            )
            result = OpsResult(
                result.ok,
                result.message,
                f"{result.details}\n{note}" if result.details else note,
            )
        if result.ok:
            marker.touch()
        results.append(result)

    return results


def migrate_volumes_cmd(_args) -> int:
    """CLI entrypoint for `nyxgpt ops migrate-volumes`.

    A standalone escape hatch for migrating pre-#3346 named-volume data
    without running the rest of `nyxgpt ops install` -- e.g. for a
    Compose-only user who never runs `install` (that's the native-mode
    reconciler). Safe to re-run; see `migrate_legacy_volumes`.
    """
    results = migrate_legacy_volumes()
    ok = _emit_results("migrate-volumes", results)
    return 0 if ok else 2


# --- Phantom Compose app-tier reconciliation ---


def _detect_phantom_compose_app_containers() -> dict[str, str]:
    """Return {service: state} for app-tier services currently running under Compose.

    In the native mode `nyxgpt ops install` targets, `api`/`web`/`ollama` run
    natively and `cassandra` runs as the one ops-managed Docker container
    (`_ensure_cassandra_container`) -- none of `CORE_APP_SERVICES` should also be
    running as Docker Compose services. A prior raw `docker compose up` (or the
    pre-#3231 observability bring-up, which started every profile-less default
    service too) can leave these running alongside the native ones, colliding on
    the same ports.
    """
    compose = _compose_stack_snapshot()
    return {
        service: state
        for service, state in compose.items()
        if service in CORE_APP_SERVICES and state == "running"
    }


def _reconcile_phantom_compose_app_containers() -> list[OpsResult]:
    """Stop any leaked Compose app-tier containers (api/web/ollama/cassandra).

    Uses `docker compose stop <service>` (not `down`) so containers/volumes are
    preserved -- consistent with `nyxgpt ops` avoiding destructive actions by
    default. This is what makes `nyxgpt ops install` a reconciler instead of an
    additive-only installer: re-running it now cleans up a mixed-mode mess left
    by an earlier run (or a raw `docker compose up`) instead of adding to it.
    """
    if _which("docker") is None:
        return []

    phantoms = _detect_phantom_compose_app_containers()
    if not phantoms:
        return [OpsResult(True, "No phantom Docker Compose app-tier containers detected")]

    results: list[OpsResult] = []
    for service in sorted(phantoms):
        cp = _run(
            ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop", service],
            check=False,
        )
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True,
                    f"Stopped phantom Compose container for {service} (was running "
                    "alongside the native/local deployment)",
                )
            )
        else:
            details = (cp.stdout or "").strip() + (
                "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
            )
            results.append(
                OpsResult(
                    False,
                    f"Failed to stop phantom Compose container for {service}",
                    details.strip(),
                )
            )
    return results


def _cp_details(cp: subprocess.CompletedProcess[str]) -> str:
    """Concatenate stdout+stderr from a CompletedProcess into one details string."""
    stdout = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()
    return (stdout + ("\n" + stderr if stderr else "")).strip()


def _resolve_locality(args) -> str | None:
    """Validate the `--local`/`--cloud` locality flag shared by `--terraform`/`--kubernetes`.

    Only `--local` is implemented today. `--cloud` is accepted by the CLI
    surface (so it doesn't need a redesign later) but always rejected with a
    "not yet implemented" message -- the local deployment is the precursor
    to a future cloud target, not an alternative to it (see issue #3344).

    Returns "local" once implemented, or None (having already printed an
    error) if the flag is missing or unimplemented.
    """
    if getattr(args, "cloud", False):
        print(
            "ERROR: --cloud is not yet implemented. Local Terraform/Kubernetes deployment "
            "(--local) is the precursor to a future cloud target -- see docs/terraform.md "
            "and docs/kubernetes.md.",
            file=sys.stderr,
        )
        return None
    if not getattr(args, "local", False):
        print(
            "ERROR: --local is required with --terraform/--kubernetes "
            "(the only locality implemented today; pass --local explicitly)",
            file=sys.stderr,
        )
        return None
    return "local"


def _resolve_api_key(explicit: str | None) -> str:
    """Resolve the auth API key for a Terraform/Kubernetes bootstrap: explicit, prompted, or random.

    Prefers an explicitly-passed `--api-key`. Otherwise, when stdin is a TTY,
    prompts for one (hidden input, never echoed or logged) so an operator can
    set a memorable key; if left blank -- or stdin isn't interactive at all
    (e.g. driven from the SRE/admin dashboard or a test) -- falls back to a
    random one so the command still completes non-interactively.
    """
    if explicit:
        return explicit
    if sys.stdin.isatty():
        try:
            entered = getpass.getpass(
                "API key for the deployed stack's [auth] section (blank to auto-generate): "
            )
        except Exception:
            entered = ""
        if entered:
            return entered
    return secrets.token_hex(32)


def _refuse_port_collision(components: list[str]) -> OpsResult | None:
    """Refuse to bring up a new deployment on ports already bound by native/Compose.

    Terraform's containers and the native/Compose stack all default to the
    same host ports (8000/3000/11434/9042). Returns a failing `OpsResult`
    naming the conflicting components -- and what to run instead -- if any of
    `components` are already live natively or via Compose, else None.
    """
    mode = detect_deployment_mode()
    bound = [
        c
        for c in components
        if mode.native.get(c) in ("started", "running") or mode.compose.get(c) == "running"
    ]
    if not bound:
        return None
    return OpsResult(
        False,
        "Refusing to start: port collision with a running native/Compose stack",
        f"{', '.join(sorted(bound))} already serving via native/Compose on the same host "
        "port(s) -- run `nyxgpt ops down` (or stop the conflicting components) before "
        "bringing up the Terraform/Kubernetes deployment.",
    )


# --- Terraform local deployment (`nyxgpt ops install/down --terraform --local`) ---

TERRAFORM_DIR = REPO_ROOT / "terraform"

# Container names terraform/main.tf creates for the core stack (see docs/terraform.md).
TERRAFORM_CONTAINERS: dict[str, str] = {
    "ollama": "nyxgpt-tf-ollama",
    "cassandra": "nyxgpt-tf-cassandra",
    "api": "nyxgpt-tf-api",
    "web": "nyxgpt-tf-web",
}

# Terraform was pulled from homebrew-core after HashiCorp's 2023 BUSL relicense,
# so `brew install terraform` fails -- install from the official tap instead.
HASHICORP_TAP = "hashicorp/tap"


def _ensure_terraform_binary() -> list[OpsResult]:
    """Ensure `terraform` is on PATH, installing it via the HashiCorp tap if missing."""
    if _which("terraform") is not None:
        return [OpsResult(True, "terraform already installed")]
    if _which("brew") is None:
        return [
            OpsResult(
                False,
                "terraform not found and Homebrew is unavailable to install it",
                "Install Terraform >= 1.5.0 manually: "
                "https://developer.hashicorp.com/terraform/install",
            )
        ]
    cp = _run(["brew", "tap", HASHICORP_TAP], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"brew tap {HASHICORP_TAP} failed", _cp_details(cp))]
    cp = _run(["brew", "install", f"{HASHICORP_TAP}/terraform"], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"brew install {HASHICORP_TAP}/terraform failed", _cp_details(cp))]
    if _which("terraform") is None:
        return [OpsResult(False, "terraform install reported success but binary still not on PATH")]
    return [OpsResult(True, f"Installed terraform via {HASHICORP_TAP}")]


def _ensure_terraform_tfvars(api_key: str | None) -> list[OpsResult]:
    """Bootstrap terraform/terraform.tfvars from the example if it doesn't exist yet."""
    tfvars = TERRAFORM_DIR / "terraform.tfvars"
    if tfvars.exists():
        return [OpsResult(True, f"{tfvars} already exists")]
    example = TERRAFORM_DIR / "terraform.tfvars.example"
    if not example.exists():
        return [OpsResult(False, f"Missing {example} to bootstrap tfvars from")]
    key = _resolve_api_key(api_key)
    text = example.read_text(encoding="utf-8")
    text = re.sub(r'repo_path\s*=\s*".*"', lambda _m: f'repo_path    = "{REPO_ROOT}"', text)
    text = re.sub(r'auth_api_key\s*=\s*".*"', lambda _m: f'auth_api_key = "{key}"', text)
    tfvars.write_text(text, encoding="utf-8")
    os.chmod(tfvars, 0o600)
    return [OpsResult(True, f"Bootstrapped {tfvars} from terraform.tfvars.example")]


def _terraform_init_plan_apply() -> list[OpsResult]:
    """Run `terraform init` -> `plan` -> `apply` in terraform/, stopping at the first failure."""
    chdir = f"-chdir={TERRAFORM_DIR}"
    cp = _run(["terraform", chdir, "init", "-input=false"], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "terraform init failed", _cp_details(cp))]
    results = [OpsResult(True, "terraform init")]

    cp = _run(["terraform", chdir, "plan", "-input=false", "-out=tfplan"], check=False)
    if cp.returncode != 0:
        results.append(OpsResult(False, "terraform plan failed", _cp_details(cp)))
        return results
    results.append(OpsResult(True, "terraform plan"))

    cp = _run(["terraform", chdir, "apply", "-input=false", "-auto-approve", "tfplan"], check=False)
    if cp.returncode != 0:
        results.append(OpsResult(False, "terraform apply failed", _cp_details(cp)))
        return results
    results.append(OpsResult(True, "terraform apply"))
    return results


def terraform_stack_state() -> dict[str, str]:
    """{component: docker state} for the Terraform-managed containers (used by status/doctor)."""
    return {
        component: _docker_container_state(name) for component, name in TERRAFORM_CONTAINERS.items()
    }


def _terraform_stack_health() -> list[OpsResult]:
    """Report each Terraform-managed container's state, plus the stack's output URLs."""
    results = [
        OpsResult(
            state == "running", f"terraform {component}: {state}", TERRAFORM_CONTAINERS[component]
        )
        for component, state in terraform_stack_state().items()
    ]
    cp = _run(["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"], check=False)
    if cp.returncode == 0 and (cp.stdout or "").strip():
        try:
            outputs = json.loads(cp.stdout)
            urls = ", ".join(f"{k}={v.get('value')}" for k, v in outputs.items())
            results.append(OpsResult(True, f"terraform outputs: {urls}"))
        except (ValueError, AttributeError):
            pass
    return results


def _install_terraform_steps(api_key: str | None) -> list[OpsResult]:
    """Run the Terraform bring-up steps and return structured results (no printing).

    Ensures terraform is present (installing via the hashicorp tap if
    missing), bootstraps terraform.tfvars from the example if absent, runs
    init -> plan -> apply, and reports the resulting stack health. Stops at
    the first failing step since each depends on the last (installing the
    binary before generating tfvars before running init, etc.) -- unlike
    `install()`'s native steps, which are independent and best-effort.

    Shared by the `nyxgpt ops install --terraform --local` CLI entrypoint
    (`_install_terraform`) and `install_terraform_local`, the SRE/admin
    dashboard API's structured equivalent.
    """
    collision = _refuse_port_collision(["api", "web", "ollama", "cassandra"])
    if collision is not None:
        return [collision]

    logger.info(
        "ops: install --terraform --local starting",
        extra={"component": "ops", "action": "install-terraform"},
    )
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        # Must run before terraform apply: main.tf no longer declares the
        # `nyxgpt_tf_*` docker_volume resources (#3346), so apply would
        # otherwise destroy them -- along with any not-yet-migrated data --
        # as part of reconciling state to the new host-bind-mount config.
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("terraform binary", _ensure_terraform_binary),
        ("terraform tfvars", lambda: _ensure_terraform_tfvars(api_key)),
        ("terraform init/plan/apply", _terraform_init_plan_apply),
    ]
    for step_name, fn in steps:
        try:
            step_results = fn()
        except Exception as e:
            results.append(
                OpsResult(False, f"terraform {step_name} raised", f"{type(e).__name__}: {e}")
            )
            step_results = []
        results += step_results
        if step_results and not all(r.ok for r in step_results):
            break
    else:
        results += _terraform_stack_health()

    return results


def install_terraform_local(api_key: str | None = None) -> list[OpsResult]:
    """Structured (non-printing) Terraform local bring-up, for the SRE/admin dashboard API.

    Runs the same steps as `nyxgpt ops install --terraform --local` --
    locality is implicitly "local" here since that's the only target this
    endpoint offers (see `_resolve_locality`) -- and returns the OpsResult
    list directly instead of routing it through `_emit_results`, so a
    FastAPI endpoint can translate it straight to JSON.
    """
    return _install_terraform_steps(api_key)


def _install_terraform(args) -> int:
    """`nyxgpt ops install --terraform --local`: the full Terraform bring-up in one command."""
    if _resolve_locality(args) is None:
        return 2
    results = _install_terraform_steps(getattr(args, "api_key", None))
    ok = _emit_results("install --terraform", results)
    return 0 if ok else 2


def _down_terraform_steps() -> list[OpsResult]:
    """`terraform destroy` the Terraform-managed stack and return structured results."""
    if _which("terraform") is None:
        return [OpsResult(False, "terraform not found on PATH -- nothing to destroy")]

    cp = _run(
        ["terraform", f"-chdir={TERRAFORM_DIR}", "destroy", "-input=false", "-auto-approve"],
        check=False,
    )
    if cp.returncode == 0:
        return [OpsResult(True, "terraform destroy", _cp_details(cp))]
    return [OpsResult(False, "terraform destroy failed", _cp_details(cp))]


def down_terraform() -> list[OpsResult]:
    """Structured (non-printing) `terraform destroy`, for the SRE/admin dashboard API."""
    return _down_terraform_steps()


def _down_terraform(_args) -> int:
    """`nyxgpt ops down --terraform`: `terraform destroy` the Terraform-managed stack."""
    results = _down_terraform_steps()
    ok = _emit_results("down --terraform", results)
    return 0 if ok else 2


# --- Kubernetes local deployment (`nyxgpt ops install/down --kubernetes --local`) ---

K8S_DIR = REPO_ROOT / "k8s"
K8S_NAMESPACE = "nyxgpt"
K8S_IMAGE = "nyxgpt-api:local"


def _ensure_kubectl_and_cluster() -> list[OpsResult]:
    """Check `kubectl` is on PATH and a cluster is reachable."""
    if _which("kubectl") is None:
        return [
            OpsResult(
                False,
                "kubectl not found on PATH",
                "Install kubectl: https://kubernetes.io/docs/tasks/tools/",
            )
        ]
    cp = _run(["kubectl", "cluster-info"], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "No reachable Kubernetes cluster", _cp_details(cp))]
    return [OpsResult(True, "Kubernetes cluster reachable")]


def _kubectl_context() -> str:
    """Return kubectl's current context name (e.g. `kind-nyxgpt`, `docker-desktop`), or "" if unset."""
    cp = _run(["kubectl", "config", "current-context"], check=False)
    return (cp.stdout or "").strip()


def _build_and_load_k8s_image() -> list[OpsResult]:
    """Build the `nyxgpt-api:local` image and load it into the current cluster's image cache.

    Docker Desktop's built-in cluster shares the host's image cache, so a
    build alone is enough there. kind/minikube each need an explicit
    load step; an unrecognized cluster type is treated the same way the
    documented manual flow would be -- skip the load and tell the operator
    to do it themselves if their cluster doesn't share the host cache.
    """
    if _which("docker") is None:
        return [OpsResult(False, "docker not found on PATH -- cannot build the nyxgpt-api image")]
    cp = _run(["docker", "build", "-t", K8S_IMAGE, str(REPO_ROOT)], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "docker build failed", _cp_details(cp))]
    results = [OpsResult(True, f"Built {K8S_IMAGE}")]

    context = _kubectl_context()
    if "docker-desktop" in context:
        results.append(
            OpsResult(True, "Docker Desktop cluster shares the host image cache -- skipped load")
        )
        return results
    if context.startswith("kind-") and _which("kind") is not None:
        cluster_name = context.removeprefix("kind-")
        cp = _run(["kind", "load", "docker-image", K8S_IMAGE, "--name", cluster_name], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "kind load docker-image failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {K8S_IMAGE} into kind cluster {cluster_name}"))
        return results
    if _which("minikube") is not None:
        cp = _run(["minikube", "image", "load", K8S_IMAGE], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "minikube image load failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {K8S_IMAGE} into minikube"))
        return results
    results.append(
        OpsResult(
            True,
            f"Unrecognized cluster context {context!r} -- skipped image load",
            "If this cluster doesn't share the host's image cache, load "
            f"{K8S_IMAGE} into it manually before the Pods can start.",
        )
    )
    return results


def _ensure_k8s_secret(api_key: str | None) -> list[OpsResult]:
    """Bootstrap k8s/secret.yaml from the example if it doesn't exist yet (never committed)."""
    secret_path = K8S_DIR / "secret.yaml"
    if secret_path.exists():
        return [OpsResult(True, f"{secret_path} already exists")]
    example = K8S_DIR / "secret.example.yaml"
    if not example.exists():
        return [OpsResult(False, f"Missing {example} to bootstrap the secret from")]
    key = _resolve_api_key(api_key)
    text = example.read_text(encoding="utf-8")
    text = re.sub(r'api-key:\s*".*"', lambda _m: f'api-key: "{key}"', text)
    secret_path.write_text(text, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    return [OpsResult(True, f"Bootstrapped {secret_path} from secret.example.yaml")]


def _kubectl_apply_kustomization() -> list[OpsResult]:
    """Apply `k8s/`'s kustomization (namespace, RBAC, ConfigMap, Secret, Deployments, HPAs, Service)."""
    cp = _run(["kubectl", "apply", "-k", str(K8S_DIR)], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "kubectl apply -k k8s/ failed", _cp_details(cp))]
    return [OpsResult(True, "kubectl apply -k k8s/", _cp_details(cp))]


def _k8s_stack_health() -> list[OpsResult]:
    """Snapshot of Pod/HPA/Service health in the `nyxgpt` namespace right after apply.

    A one-shot snapshot, not a wait-until-ready loop -- Pods may still be
    starting when this runs; re-check with `nyxgpt ops status`.
    """
    results: list[OpsResult] = []

    cp = _run(
        [
            "kubectl",
            "-n",
            K8S_NAMESPACE,
            "get",
            "pods",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}={.status.phase};{end}",
        ],
        check=False,
    )
    if cp.returncode != 0:
        results.append(OpsResult(False, "Could not read pod status", _cp_details(cp)))
    else:
        entries = [e for e in (cp.stdout or "").split(";") if e]
        if not entries:
            results.append(OpsResult(False, f"No pods found in namespace {K8S_NAMESPACE}"))
        for entry in entries:
            name, _, phase = entry.partition("=")
            results.append(OpsResult(phase == "Running", f"pod {name}: {phase}"))

    cp = _run(["kubectl", "-n", K8S_NAMESPACE, "get", "hpa", "--no-headers"], check=False)
    hpa_lines = [line for line in (cp.stdout or "").splitlines() if line.strip()]
    results.append(
        OpsResult(cp.returncode == 0 and bool(hpa_lines), f"{len(hpa_lines)} HPA(s) found")
    )

    cp = _run(
        ["kubectl", "-n", K8S_NAMESPACE, "get", "svc", "nyxgpt-api", "--no-headers"], check=False
    )
    results.append(
        OpsResult(
            cp.returncode == 0,
            "Service nyxgpt-api" + (" found" if cp.returncode == 0 else " not found"),
        )
    )
    return results


def _install_kubernetes_steps(api_key: str | None) -> list[OpsResult]:
    """Run the Kubernetes bring-up steps and return structured results (no printing).

    Prereq checks (cluster reachable, kubectl present), builds and loads
    `nyxgpt-api:local`, bootstraps k8s/secret.yaml (prompting for the API
    key, never committing it), applies the kustomization, and snapshots
    Pod/HPA/Service health. Stops at the first failing step, same rationale
    as `_install_terraform_steps`.

    Shared by the `nyxgpt ops install --kubernetes --local` CLI entrypoint
    (`_install_kubernetes`) and `install_kubernetes_local`, the SRE/admin
    dashboard API's structured equivalent.
    """
    collision = _refuse_port_collision(["api"])
    if collision is not None:
        return [collision]

    logger.info(
        "ops: install --kubernetes --local starting",
        extra={"component": "ops", "action": "install-kubernetes"},
    )
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("cluster prerequisites", _ensure_kubectl_and_cluster),
        ("build/load image", _build_and_load_k8s_image),
        ("secret bootstrap", lambda: _ensure_k8s_secret(api_key)),
        ("apply kustomization", _kubectl_apply_kustomization),
    ]
    for step_name, fn in steps:
        try:
            step_results = fn()
        except Exception as e:
            results.append(
                OpsResult(False, f"kubernetes {step_name} raised", f"{type(e).__name__}: {e}")
            )
            step_results = []
        results += step_results
        if step_results and not all(r.ok for r in step_results):
            break
    else:
        results += _k8s_stack_health()

    return results


def install_kubernetes_local(api_key: str | None = None) -> list[OpsResult]:
    """Structured (non-printing) Kubernetes local bring-up, for the SRE/admin dashboard API.

    Runs the same steps as `nyxgpt ops install --kubernetes --local` --
    locality is implicitly "local" here since that's the only target this
    endpoint offers (see `_resolve_locality`) -- and returns the OpsResult
    list directly instead of routing it through `_emit_results`, so a
    FastAPI endpoint can translate it straight to JSON.
    """
    return _install_kubernetes_steps(api_key)


def _install_kubernetes(args) -> int:
    """`nyxgpt ops install --kubernetes --local`: the full k8s bring-up in one command."""
    if _resolve_locality(args) is None:
        return 2
    results = _install_kubernetes_steps(getattr(args, "api_key", None))
    ok = _emit_results("install --kubernetes", results)
    return 0 if ok else 2


def _down_kubernetes_steps() -> list[OpsResult]:
    """Remove the `nyxgpt` namespace's Kubernetes resources and return structured results."""
    if _which("kubectl") is None:
        return [OpsResult(False, "kubectl not found on PATH -- nothing to tear down")]

    cp = _run(["kubectl", "delete", "-k", str(K8S_DIR), "--ignore-not-found"], check=False)
    if cp.returncode == 0:
        return [
            OpsResult(True, "kubectl delete -k k8s/ (namespace and all resources)", _cp_details(cp))
        ]
    return [OpsResult(False, "kubectl delete -k k8s/ failed", _cp_details(cp))]


def down_kubernetes() -> list[OpsResult]:
    """Structured (non-printing) `kubectl delete -k k8s/`, for the SRE/admin dashboard API."""
    return _down_kubernetes_steps()


def _down_kubernetes(_args) -> int:
    """`nyxgpt ops down --kubernetes`: remove the `nyxgpt` namespace's Kubernetes resources."""
    results = _down_kubernetes_steps()
    ok = _emit_results("down --kubernetes", results)
    return 0 if ok else 2


def infra_status() -> dict[str, Any]:
    """Structured Terraform/Kubernetes deployment status, for the SRE/admin dashboard API.

    Mirrors the Terraform/Kubernetes sections `nyxgpt ops status` prints
    (see `status`), as JSON instead of stdout lines.
    """
    tf_state = terraform_stack_state()
    terraform = {
        "deployed": any(state != "absent" for state in tf_state.values()),
        "containers": tf_state,
    }

    kubectl_available = _which("kubectl") is not None
    pods: list[str] = []
    if kubectl_available:
        cp = _run(["kubectl", "-n", K8S_NAMESPACE, "get", "pods", "--no-headers"], check=False)
        if cp.returncode == 0:
            pods = [line for line in (cp.stdout or "").splitlines() if line.strip()]
    kubernetes = {
        "available": kubectl_available,
        "deployed": bool(pods),
        "namespace": K8S_NAMESPACE,
        "pods": pods,
    }

    return {"terraform": terraform, "kubernetes": kubernetes}


def install(args) -> int:
    """CLI entrypoint for `nyxgpt ops install`.

    Reconciles the local machine to the intended native-mode topology (see
    docs/ops.md): first migrating any pre-#3346 named-volume data into
    ~/.nyxGPT/volumes/ (see `migrate_legacy_volumes`), then stopping any
    phantom Docker Compose app-tier containers leaked from an earlier run or
    a raw `docker compose up`, then ensuring the
    local Cassandra container plus every other install step (scripts, web deps,
    MCP deps, Cassandra LaunchAgent, Ollama logs LaunchAgent, Homebrew
    formulas, the native Ollama service, log symlinks, env sync from
    config.ini, the observability stack) -- printing an OK/FAIL line per
    result. A failure in one step doesn't stop the rest from
    running.

    The observability step (Grafana/Loki/Jaeger/GlitchTip) runs by default so
    a fresh install comes up with the full SRE view already populated --
    pass `--skip-observability` to opt out (e.g. on a host with no Docker,
    or to keep those Compose profiles stopped for resource reasons).

    Returns 0 if every step succeeded, else 2.
    """
    if getattr(args, "terraform", False) and getattr(args, "kubernetes", False):
        print("ERROR: --terraform and --kubernetes are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "terraform", False):
        return _install_terraform(args)
    if getattr(args, "kubernetes", False):
        return _install_kubernetes(args)

    logger.info("ops: install starting", extra={"component": "ops", "action": "install"})

    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("phantom compose reconciliation", _reconcile_phantom_compose_app_containers),
        ("scripts", _install_scripts),
        ("web deps", _ensure_web_deps),
        ("mcp deps", _ensure_mcp_deps),
        ("cassandra container", _ensure_cassandra_container),
        ("cassandra launchagent", _install_cassandra_launchagent),
        ("ollama logs launchagent", _install_ollama_launchagent),
        ("homebrew api", _install_homebrew_api),
        ("homebrew web", _install_homebrew_web),
        ("ollama service", _ensure_ollama_service),
        ("log symlinks", _ensure_log_symlinks),
        ("env sync", sync_env_from_config),
        ("compose file path", _persist_compose_file_path),
    ]
    if not getattr(args, "skip_observability", False):
        steps.append(("observability stack", _start_observability_stack))
        steps.append(("glitchtip auto-provisioning", _provision_glitchtip))
    for step_name, fn in steps:
        try:
            results += fn()
        except Exception as e:
            logger.error(
                "ops: install step %s raised %s: %s",
                step_name,
                type(e).__name__,
                e,
                extra={"component": "ops", "action": "install", "step": step_name},
                exc_info=True,
            )
            results.append(
                OpsResult(
                    False,
                    f"ops install failed: {step_name}",
                    f"{type(e).__name__}: {e}",
                )
            )

    ok = _emit_results("install", results)
    logger.info(
        "ops: install %s (%d/%d steps ok)",
        "succeeded" if ok else "failed",
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "install", "ok": ok, "steps": len(results)},
    )

    return 0 if ok else 2


def status(_args) -> int:
    """CLI entrypoint for `nyxgpt ops status`.

    Prints the detected deployment mode (native vs. Compose per component),
    a native/Compose port-conflict warning if both are live, Homebrew
    service states, the Cassandra log-follower LaunchAgent's load state, and
    whether the ops-managed Cassandra Docker container is running.

    Always returns 0.
    """
    logger.info("ops: status starting", extra={"component": "ops", "action": "status"})
    print("nyxGPT ops status")

    mode = detect_deployment_mode()

    print("\nDeployment mode:")
    for component in ("api", "web", "ollama"):
        print(f"  native  {component}: {mode.native.get(component, 'none')}")
    # Cassandra is the one Docker-managed piece of a local-first install --
    # labeling it "native" here misstated the topology.
    print(f"  docker  cassandra: {mode.native.get('cassandra', 'absent')}")
    if mode.compose:
        for component, state in sorted(mode.compose.items()):
            print(f"  compose {component}: {state}")
    else:
        print("  compose: not detected (no Docker Compose stack running)")

    if mode.conflicts:
        stop_examples = ", ".join(f"nyxgpt ops stop {c}" for c in sorted(mode.conflicts))
        print(
            "\nWARNING: "
            + ", ".join(sorted(mode.conflicts))
            + " reported running in BOTH native and Docker Compose. Only one is actually "
            "serving traffic on the shared port -- the other is a phantom backend. Config "
            f"edits to {NATIVE_CONFIG_HINT} only reach the native process; if Compose is the "
            f"one answering, edit {COMPOSE_CONFIG_HINT} instead. Run `{stop_examples}` to stop "
            "both and pick one, or `nyxgpt ops down --app-only` to drop the Compose app tier "
            "entirely."
        )
    else:
        config_hint = NATIVE_CONFIG_HINT
        if mode.compose:
            config_hint += f" (native components) / {COMPOSE_CONFIG_HINT} (Compose components)"
        print(f"\nConfig in use: {config_hint}")

    if _which("brew"):
        cp = _run(["brew", "services", "list"], check=False)
        print("\nHomebrew services:\n" + (cp.stdout or "").strip())
    else:
        print("\nHomebrew services: brew not found")

    label = "com.nyxgpt.cassandra-logs"
    try:
        cp = _run(["launchctl", "list"], check=False)
        loaded = label in (cp.stdout or "")
        print(f"\nLaunchAgent {label}: {'LOADED' if loaded else 'NOT LOADED'}")
    except Exception as e:
        print(f"\nLaunchAgent {label}: ERROR ({e})")

    if _which("docker") is None:
        print("\nDocker: docker not found")

    tf_state = terraform_stack_state()
    if any(state != "absent" for state in tf_state.values()):
        print("\nTerraform-managed stack (nyxgpt ops down --terraform to tear down):")
        for component, state in sorted(tf_state.items()):
            print(f"  terraform {component}: {state}")

    if _which("kubectl") is not None:
        cp = _run(["kubectl", "-n", K8S_NAMESPACE, "get", "pods", "--no-headers"], check=False)
        pod_lines = [line for line in (cp.stdout or "").splitlines() if line.strip()]
        if cp.returncode == 0 and pod_lines:
            print(
                f"\nKubernetes ({K8S_NAMESPACE} namespace, nyxgpt ops down --kubernetes to "
                f"tear down): {len(pod_lines)} pod(s)"
            )
            for line in pod_lines:
                print(f"  {line}")

    print(
        "\nCleanup: `nyxgpt ops stop <target>` stops one component (native and/or Compose), "
        "`nyxgpt ops down` tears down the whole stack."
    )

    logger.info(
        "ops: status complete (native=%s, compose=%s, conflicts=%s)",
        mode.native,
        mode.compose,
        mode.conflicts,
        extra={
            "component": "ops",
            "action": "status",
            "native": mode.native,
            "compose": mode.compose,
            "conflicts": mode.conflicts,
        },
    )

    return 0


def _log_aggregation_wiring_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3277 failure mode: native-mode logs never reaching Loki.

    promtail always runs as a Compose container regardless of whether the
    core app is deployed native or Compose (see `OBSERVABILITY_PROFILES`).
    In native mode, api/self-heal/ops write logs to the host `~/.nyxGPT/logs`
    directly -- a plain directory, not the `nyxgpt_data` Docker-managed
    volume promtail otherwise mounts for Compose-mode logs. If
    docker-compose.yml's promtail service ever loses its host bind mount for
    that directory, native logs silently stop reaching Loki -- Grafana just
    shows nothing rather than erroring, so this needs an explicit check
    rather than relying on someone noticing an empty dashboard.

    Only reports an issue when there's something to actually verify: log
    aggregation is enabled, promtail is confirmed running, and native-mode
    log files actually exist to be missed. Returns None otherwise (nothing
    to check yet, or the wiring is intact).
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception:
        return None
    if not get_log_aggregation_enabled(parser):
        return None

    if _compose_stack_snapshot().get("promtail") != "running":
        return None

    native_log_dir = Path.home() / ".nyxGPT" / "logs"
    if not native_log_dir.exists() or not any(native_log_dir.glob("*.log*")):
        return None

    compose_file = REPO_ROOT / "docker-compose.yml"
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if PROMTAIL_NATIVE_LOG_MOUNT_MARKER in compose_text:
        return None

    return (
        f"Log aggregation is enabled and native-mode logs exist under {native_log_dir}, "
        "but promtail's docker-compose.yml service has no host bind mount for them -- "
        "native logs are not reaching Loki. See docs/docker-compose.md#log-aggregation."
    )


def doctor(_args) -> int:
    """CLI entrypoint for `nyxgpt ops doctor`.

    Checks for common misconfigurations: missing ~/.nyxGPT/config.ini,
    non-executable helper scripts, missing brew/docker/node/npm tools on
    PATH, missing/incomplete web dependencies (node_modules, undici), and
    (when log aggregation is enabled and native logs exist) whether
    promtail is actually wired to see native-mode host logs. Prints each
    issue found.

    Returns 0 if no issues were found, else 2.
    """
    logger.info("ops: doctor starting", extra={"component": "ops", "action": "doctor"})
    issues: list[str] = []

    cfg = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg.exists():
        issues.append(f"Missing config {cfg}")

    for name in ("run-web.sh", "follow-cassandra-logs.sh", "follow-ollama-logs.sh"):
        p = Path.home() / ".nyxGPT" / "scripts" / name
        if p.exists() and not os.access(p, os.X_OK):
            issues.append(f"Script not executable {p}")

    for tool in ("brew", "docker"):
        if _which(tool) is None:
            issues.append(f"Missing tool in PATH: {tool}")

    if (
        _which("docker") is not None
        and _docker_container_state(CASSANDRA_CONTAINER_NAME) == "absent"
    ):
        issues.append(
            f"Missing local Cassandra container: {CASSANDRA_CONTAINER_NAME} "
            "(run: nyxgpt ops install)"
        )

    web_dir = REPO_ROOT / "web"
    if web_dir.exists():

        def _can_resolve(pkg: str) -> bool:
            try:
                cp = subprocess.run(
                    ["node", "-p", f"require.resolve('{pkg}')"],
                    cwd=str(web_dir),
                    text=True,
                    capture_output=True,
                )
                return cp.returncode == 0
            except Exception:
                return False

        if _which("node") is None:
            issues.append("Missing tool in PATH: node")
        if _which("npm") is None:
            issues.append("Missing tool in PATH: npm")
        if not (web_dir / "node_modules").exists():
            issues.append(f"Missing web deps: {web_dir / 'node_modules'} (run: nyxgpt ops install)")
        elif not _can_resolve("undici"):
            issues.append("Missing web dependency: undici (run: nyxgpt ops install)")

    log_issue = _log_aggregation_wiring_issue()
    if log_issue:
        issues.append(log_issue)

    if TERRAFORM_DIR.joinpath("terraform.tfstate").exists() and all(
        state == "absent" for state in terraform_stack_state().values()
    ):
        issues.append(
            "Terraform state exists but no nyxgpt-tf-* containers are running "
            "(run: nyxgpt ops install --terraform --local, or nyxgpt ops down --terraform "
            "to clean up the stale state)"
        )

    if issues:
        print("nyxGPT ops doctor: FAIL")
        for i in issues:
            print(f"- {i}")
        logger.warning(
            "ops: doctor found %d issue(s): %s",
            len(issues),
            "; ".join(issues),
            extra={"component": "ops", "action": "doctor", "ok": False, "issues": issues},
        )
        return 2

    print("nyxGPT ops doctor: OK")
    logger.info(
        "ops: doctor found no issues",
        extra={"component": "ops", "action": "doctor", "ok": True, "issues": []},
    )
    return 0


# --- Restart public API ---


def _compose_conflict_result(component: str, compose: dict[str, str]) -> OpsResult | None:
    """Return a refusal OpsResult if a Compose deployment of `component` is live."""
    if compose.get(component) != "running":
        return None
    port = COMPOSE_COMPONENT_PORTS.get(component)
    port_note = f" on port {port}" if port else ""
    return OpsResult(
        False,
        f"Refusing to restart local {component}: a Docker Compose deployment of "
        f"{component} is already running{port_note}",
        "Both deployments would try to bind the same port. Stop the Compose deployment "
        "of this component (or manage it there) before restarting the local one.",
    )


def restart(args) -> int:
    """Restart operational components.

    target: all|api|web|ollama|cassandra|cassandra-logs|observability

    Before touching a native component, checks whether a Docker Compose deployment
    of that same component is already live and, if so, refuses rather than starting
    a second process/container that would collide on the same port.

    Unlike `stop`/`down` (where `observability` is opt-in via its own target and
    excluded from `all`), `restart all` also restarts every currently running
    observability Compose service (monitoring/logging/tracing/errors profiles) --
    so a single wrapped command can bounce the whole local stack, core services
    plus dashboards, after a config change. Observability services that aren't
    enabled/running are skipped cleanly rather than started.
    """
    target = getattr(args, "target", "all") or "all"
    logger.info(
        "ops: restart starting (target=%s)",
        target,
        extra={"component": "ops", "action": "restart", "target": target},
    )

    results: list[OpsResult] = []
    compose = _compose_stack_snapshot()

    if target in ("all", "api"):
        conflict = _compose_conflict_result("api", compose)
        if conflict:
            results.append(conflict)
        else:
            results.extend(_restart_brew_service("nyxgpt-api"))

    if target in ("all", "web"):
        conflict = _compose_conflict_result("web", compose)
        if conflict:
            results.append(conflict)
        else:
            results.extend(_restart_brew_service("nyxgpt-web"))

    if target in ("all", "ollama"):
        conflict = _compose_conflict_result("ollama", compose)
        if conflict:
            results.append(conflict)
        else:
            results.extend(_restart_brew_service("ollama"))

    if target in ("all", "cassandra"):
        conflict = _compose_conflict_result("cassandra", compose)
        if conflict:
            results.append(conflict)
        else:
            results.extend(_restart_docker_container("nyxgpt-cassandra"))

    if target in ("all", "cassandra-logs"):
        results += _restart_launchagent("com.nyxgpt.cassandra-logs")

    if target in ("all", "observability"):
        results.extend(_restart_observability_stack())

    ok = _emit_results("restart", results)
    logger.info(
        "ops: restart %s (target=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        target,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "restart", "target": target, "ok": ok},
    )

    return 0 if ok else 2


# --- Stop/down helpers ---


def _stop_brew_service(name: str) -> list[OpsResult]:
    """Stop Homebrew service `name` via `brew services stop`.

    Returns a single-element list: an OpsResult reporting brew missing, the
    stop command's success, or its failure with captured stdout/stderr.
    """
    if _which("brew") is None:
        return [OpsResult(False, f"brew not found; cannot stop {name}")]
    try:
        cp = _run(["brew", "services", "stop", name], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Stopped brew service: {name}")]
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return [OpsResult(False, f"Failed to stop brew service: {name}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop brew service: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _stop_docker_container(name: str) -> list[OpsResult]:
    """Stop Docker container `name` via `docker stop` (container is preserved, not removed)."""
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot stop {name}")]
    try:
        cp = _run(["docker", "stop", name], check=False)
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop docker container: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]
    if cp.returncode == 0:
        return [OpsResult(True, f"Stopped docker container: {name}")]
    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )
    return [OpsResult(False, f"Failed to stop docker container: {name}", details.strip())]


def _stop_launchagent(label: str) -> list[OpsResult]:
    """Unload the LaunchAgent `label` via `launchctl bootout` in the current GUI domain.

    Unlike `_restart_launchagent`'s kickstart, `bootout` actually unloads the
    agent so it stops for good instead of being immediately relaunched by
    launchd. A "not loaded" failure (already stopped) is reported as success.
    """
    domain = f"gui/{os.getuid()}/{label}"
    try:
        cp = _run(["launchctl", "bootout", domain], check=False)
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        if cp.returncode == 0:
            return [OpsResult(True, f"Stopped LaunchAgent: {label}")]
        if "no such process" in details.lower() or "could not find" in details.lower():
            return [OpsResult(True, f"LaunchAgent already stopped: {label}")]
        return [OpsResult(False, f"Failed to stop LaunchAgent: {label}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop LaunchAgent: {label}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _compose_stop_service(service: str) -> list[OpsResult]:
    """Stop (but don't remove) a single running Compose service: `docker compose stop`."""
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot stop compose service: {service}")]
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop", service], check=False
    )
    if cp.returncode == 0:
        return [OpsResult(True, f"Stopped Compose service: {service}")]
    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )
    return [OpsResult(False, f"Failed to stop Compose service: {service}", details.strip())]


def _resolve_observability_services() -> tuple[list[str] | None, OpsResult | None]:
    """Resolve the Compose service names for the observability profiles.

    Mirrors `_start_observability_stack`'s own resolution (profiles minus
    `CORE_APP_SERVICES`) so `stop`/`down` agree with `install`/`observability`
    on what "observability" means. Returns `(services, None)` on success or
    `(None, failure_result)` if the services list couldn't be resolved.
    """
    base_cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        base_cmd += ["--profile", profile]
    cp = _run(base_cmd + ["config", "--services"], check=False)
    if cp.returncode != 0:
        return None, OpsResult(
            False,
            "Failed to resolve observability services",
            (cp.stderr or cp.stdout or "").strip(),
        )
    services = sorted({s.strip() for s in cp.stdout.splitlines() if s.strip()} - CORE_APP_SERVICES)
    return services, None


def _resolve_app_services() -> tuple[list[str] | None, OpsResult | None]:
    """Resolve the Compose service names for the core app tier (`CORE_APP_SERVICES`).

    Returns `(services, None)` on success or `(None, failure_result)` if the
    services list couldn't be resolved.
    """
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "config", "--services"],
        check=False,
    )
    if cp.returncode != 0:
        return None, OpsResult(
            False,
            "Failed to resolve app services",
            (cp.stderr or cp.stdout or "").strip(),
        )
    services = sorted({s.strip() for s in cp.stdout.splitlines() if s.strip()} & CORE_APP_SERVICES)
    return services, None


def _stop_observability_stack() -> list[OpsResult]:
    """Stop (but don't remove) every running observability Compose service."""
    if not _compose_available():
        return [OpsResult(True, "Skipped observability stack (Docker not found)")]

    services, err = _resolve_observability_services()
    if err is not None:
        return [err]
    if not services:
        return [OpsResult(True, "No observability services resolved to stop")]

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop"] + services
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to stop observability stack",
                (cp.stderr or cp.stdout or "").strip(),
            )
        ]
    return [OpsResult(True, "Observability stack stopped (containers and data preserved)")]


def _restart_observability_stack() -> list[OpsResult]:
    """Restart every currently running observability Compose service.

    Resolves the monitoring/logging/tracing/errors profile services --
    the same set `_start_observability_stack`/`_stop_observability_stack` use --
    and restarts only the ones actually running. A profile service that isn't
    enabled/up is reported as skipped rather than started, since `restart`
    should bounce what's live, not change which services are enabled (that's
    `nyxgpt ops observability`'s job).
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped observability stack (Docker not found)")]

    services, err = _resolve_observability_services()
    if err is not None:
        return [err]
    if not services:
        return [OpsResult(True, "No observability services resolved to restart")]

    running = _compose_stack_snapshot()
    to_restart = [s for s in services if running.get(s) == "running"]
    skipped = [s for s in services if s not in to_restart]

    results: list[OpsResult] = [
        OpsResult(True, f"Observability service not running (skipped): {s}") for s in skipped
    ]

    if not to_restart:
        results.append(OpsResult(True, "No running observability services to restart"))
        return results

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "restart"] + to_restart
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        results.append(
            OpsResult(
                False,
                "Failed to restart observability stack",
                (cp.stderr or cp.stdout or "").strip(),
            )
        )
        return results

    results.append(OpsResult(True, f"Restarted observability services: {', '.join(to_restart)}"))
    return results


# Compose service name -> volume_dir() component(s) it bind-mounts (see
# VOLUME_DIR_NAMES). `_compose_down` uses this to actually delete data when
# `--volumes` is passed: `docker compose down --volumes` only removes
# Docker-managed named volumes, and since #3346 these are plain host bind
# mounts, so that flag alone is a silent no-op against the data it used to
# delete. glitchtip/glitchtip-worker share one uploads directory.
SERVICE_VOLUME_DIRS: dict[str, list[str]] = {
    "ollama": ["ollama"],
    "cassandra": ["cassandra"],
    "api": ["nyxgpt-data"],
    "prometheus": ["prometheus"],
    "grafana": ["grafana"],
    "loki": ["loki"],
    "glitchtip-postgres": ["glitchtip-postgres"],
    "glitchtip": ["glitchtip-uploads"],
    "glitchtip-worker": ["glitchtip-uploads"],
}


def _remove_volume_dirs(services: list[str]) -> list[OpsResult]:
    """Delete the host bind-mount directories owned by `services` (see `SERVICE_VOLUME_DIRS`).

    Called only when `docker compose down --volumes` is requested -- that
    flag no longer touches bind-mounted data itself (see `_compose_down`), so
    this is what actually makes `--volumes` destructive again.
    """
    dirs = sorted({d for s in services for d in SERVICE_VOLUME_DIRS.get(s, [])})
    if not dirs:
        return []
    results: list[OpsResult] = []
    for name in dirs:
        path = volume_dir(name)
        try:
            shutil.rmtree(path)
            results.append(OpsResult(True, f"Removed data directory: {path}"))
        except Exception as e:
            results.append(
                OpsResult(
                    False, f"Failed to remove data directory: {path}", f"{type(e).__name__}: {e}"
                )
            )
    return results


def _compose_down(services: list[str], *, volumes: bool) -> list[OpsResult]:
    """Tear down the given Compose `services` via `docker compose down`.

    Removes containers/networks for exactly the listed services; `volumes`
    additionally deletes their ~/.nyxGPT/volumes/ bind-mount directories
    (destructive -- data loss; see `_remove_volume_dirs`).
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped Compose teardown (Docker not found)")]
    if not services:
        return [OpsResult(True, "No Compose services to tear down for this scope")]

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "down"] + services
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to tear down Compose services",
                (cp.stderr or cp.stdout or "").strip(),
            )
        ]
    suffix = " and their data directories" if volumes else " (data directories preserved)"
    results = [OpsResult(True, f"Removed Compose containers{suffix}: {', '.join(services)}")]
    if volumes:
        results += _remove_volume_dirs(services)
    return results


def stop(args) -> int:
    """Stop operational components.

    target: all|api|web|ollama|cassandra|cassandra-logs|observability

    For components that can run either natively or under Docker Compose
    (api/web/ollama/cassandra), detects which mode is actually running and
    stops the right one -- if both are live (mixed mode), stops both and
    reports it clearly. Does not delete data volumes or remove containers
    (Compose services are stopped, not brought down).

    `observability` has no native equivalent, so -- like `restart` -- it is
    not included in `all`; select it explicitly to stop the Grafana/Loki/
    Jaeger/GlitchTip Compose profiles.
    """
    target = getattr(args, "target", "all") or "all"
    logger.info(
        "ops: stop starting (target=%s)",
        target,
        extra={"component": "ops", "action": "stop", "target": target},
    )

    results: list[OpsResult] = []
    mode = detect_deployment_mode()

    def _stop_dual_mode(
        component: str, native_stop: Callable[[str], list[OpsResult]], native_arg: str
    ) -> None:
        native_running = mode.native.get(component) in ("started", "running")
        compose_running = mode.compose.get(component) == "running"
        if not native_running and not compose_running:
            results.append(OpsResult(True, f"{component}: already stopped"))
            return
        if native_running and compose_running:
            results.append(
                OpsResult(
                    True,
                    f"{component}: running in BOTH native and Compose (mixed mode) -- "
                    "stopping both",
                )
            )
        if native_running:
            results.extend(native_stop(native_arg))
        if compose_running:
            results.extend(_compose_stop_service(component))

    if target in ("all", "api"):
        _stop_dual_mode("api", _stop_brew_service, "nyxgpt-api")

    if target in ("all", "web"):
        _stop_dual_mode("web", _stop_brew_service, "nyxgpt-web")

    if target in ("all", "ollama"):
        _stop_dual_mode("ollama", _stop_brew_service, "ollama")

    if target in ("all", "cassandra"):
        _stop_dual_mode("cassandra", _stop_docker_container, "nyxgpt-cassandra")

    if target in ("all", "cassandra-logs"):
        results += _stop_launchagent("com.nyxgpt.cassandra-logs")

    if target == "observability":
        # Not part of "all" -- like `restart`, "all" only covers the core
        # api/web/ollama/cassandra/cassandra-logs components; observability
        # has no native equivalent and is opt-in via its own target/command.
        results += _stop_observability_stack()

    ok = _emit_results("stop", results)
    logger.info(
        "ops: stop %s (target=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        target,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "stop", "target": target, "ok": ok},
    )

    return 0 if ok else 2


def down(args) -> int:
    """Tear down the local stack: native services plus the Compose app/observability tiers.

    Native services (api/web/ollama/cassandra/cassandra-logs) are stopped;
    Compose containers for the selected scope are removed via `docker
    compose down` (networks too, volumes preserved unless `--volumes` is
    also given). `--app-only`/`--observability-only` scope the teardown to
    one tier so, e.g., a stale Compose app tier can be dropped while
    observability (or vice versa) stays up.

    `--terraform`/`--kubernetes` tear down those deployments instead
    (`terraform destroy` / removing the `nyxgpt` namespace's resources) --
    mutually exclusive with the native/Compose scope flags above.
    """
    if getattr(args, "terraform", False) and getattr(args, "kubernetes", False):
        print("ERROR: --terraform and --kubernetes are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "terraform", False):
        return _down_terraform(args)
    if getattr(args, "kubernetes", False):
        return _down_kubernetes(args)

    app_only = bool(getattr(args, "app_only", False))
    observability_only = bool(getattr(args, "observability_only", False))
    volumes = bool(getattr(args, "volumes", False))
    yes_really = bool(getattr(args, "yes_really", False))

    if volumes and not yes_really:
        print(
            "ERROR: refusing to remove volumes without --yes-really "
            "(this deletes Cassandra/Postgres/Grafana data)",
            file=sys.stderr,
        )
        return 2

    scope = "observability" if observability_only else ("app" if app_only else "all")
    logger.info(
        "ops: down starting (scope=%s, volumes=%s)",
        scope,
        volumes,
        extra={"component": "ops", "action": "down", "scope": scope, "volumes": volumes},
    )

    results: list[OpsResult] = []

    if scope in ("all", "app"):
        results.extend(_stop_brew_service("nyxgpt-api"))
        results.extend(_stop_brew_service("nyxgpt-web"))
        results.extend(_stop_brew_service("ollama"))
        results.extend(_stop_docker_container("nyxgpt-cassandra"))
        results.extend(_stop_launchagent("com.nyxgpt.cassandra-logs"))

    compose_services: list[str] = []

    if not _compose_available():
        results.append(OpsResult(True, "Skipped Compose teardown (Docker not found)"))
    else:
        if scope in ("all", "app"):
            app_services, err = _resolve_app_services()
            if err is not None:
                results.append(err)
            elif app_services:
                compose_services += app_services

        if scope in ("all", "observability"):
            obs_services, err = _resolve_observability_services()
            if err is not None:
                results.append(err)
            elif obs_services:
                compose_services += obs_services

        if compose_services:
            results.extend(_compose_down(compose_services, volumes=volumes))
        else:
            results.append(OpsResult(True, "No Compose services running for this scope"))

    ok = _emit_results("down", results)
    logger.info(
        "ops: down %s (scope=%s, volumes=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        scope,
        volumes,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "down", "scope": scope, "ok": ok},
    )

    return 0 if ok else 2


def logs(args) -> int:
    """Print recent logs for a single Docker Compose service.

    Wraps `docker compose logs` so operators never need to run a raw
    `docker`/`docker compose` command themselves -- e.g. to read the
    `errors` profile's GlitchTip container output for the first-account
    registration confirmation link its console email backend prints there.
    """
    service = args.service
    tail = getattr(args, "tail", None)
    if tail is None:
        tail = 200

    logger.info(
        "ops: logs requested for %s (tail=%d)",
        service,
        tail,
        extra={"component": "ops", "action": "logs", "service": service, "tail": tail},
    )

    result = self_heal.component_logs(service, tail=tail)
    if not result.ok:
        print(f"[FAIL] {result.message}")
        if result.details:
            print(f"  {result.details}")
        logger.warning(
            "ops: logs failed for %s: %s",
            service,
            result.message,
            extra={
                "component": "ops",
                "action": "logs",
                "service": service,
                "ok": False,
                "result_message": result.message,
                "details": result.details,
            },
        )
        return 2

    print(f"--- {result.message} ---")
    print(result.details or "(no output)")
    # Log the outcome, not the log body itself -- the tailed output can be
    # large and would otherwise duplicate the target service's own logs.
    logger.info(
        "ops: logs %s",
        result.message,
        extra={"component": "ops", "action": "logs", "service": service, "ok": True},
    )
    return 0


# --- Observability stack (Grafana/Prometheus/Loki/Jaeger/GlitchTip) ---

# Docker Compose profiles that make up the SRE observability suite. These
# tools have no native/Homebrew path (see docker/config.docker.ini) -- they
# only ever run as Compose services, regardless of whether the core app
# (api/web/cassandra/ollama) is deployed native or Compose.
OBSERVABILITY_PROFILES: list[str] = ["monitoring", "logging", "tracing", "errors"]

# Core app services in docker-compose.yml that have no `profiles:` key, which
# makes Compose treat them as "default" services started on *every* `up`
# regardless of which `--profile` flags are passed -- `--profile` only adds
# services, it never filters the always-on ones. `_start_observability_stack`
# must explicitly exclude these from the service list it starts, or `nyxgpt
# ops install` would silently bring up a full Dockerized copy of the app
# (building api/web images) alongside a native install.
CORE_APP_SERVICES: frozenset[str] = frozenset({"nyxgpt", "ollama", "cassandra", "api", "web"})

# config.ini sections flipped to `enabled = true` once their matching
# Compose profile is confirmed up, so the SRE/admin dashboard (and, for
# tracing, the API's own span-export init) reflect that the stack is
# actually live instead of still showing "opt-in, not running". Deliberately
# excludes `error_tracking`: GlitchTip is only reachable once its container
# passes its health check (well after `up -d` returns), so it's flipped on
# by `_provision_glitchtip` instead, once a DSN actually exists to report to.
OBSERVABILITY_ENABLE_SECTIONS: list[str] = ["monitoring", "log_aggregation", "tracing"]


def _compose_available() -> bool:
    """Return True if both `docker` and `docker compose` are usable on this host."""
    if _which("docker") is None:
        return False
    cp = _run(["docker", "compose", "version"], check=False)
    return cp.returncode == 0


def _enable_observability_config(cfg_path: Path | None = None) -> None:
    """Flip `enabled = true` for `OBSERVABILITY_ENABLE_SECTIONS` in config.ini.

    Only writes the file if a change is actually needed, so re-running
    `nyxgpt ops install`/`nyxgpt ops observability` after the first time is a
    no-op here. Silently does nothing if config.ini doesn't exist yet (run
    `nyxgpt wizard` first) -- this is a follow-on convenience, not something
    that should fail the observability step itself.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)

    changed = False
    for section in OBSERVABILITY_ENABLE_SECTIONS:
        if not parser.has_section(section):
            parser.add_section(section)
        if parser.get(section, "enabled", fallback="false").strip().lower() != "true":
            parser.set(section, "enabled", "true")
            changed = True

    if not changed:
        return

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(cfg_path, 0o600)


def _start_observability_stack() -> list[OpsResult]:
    """Start the Grafana/Loki/Jaeger/GlitchTip Compose profiles (idempotent).

    Wraps `docker compose --profile monitoring --profile logging --profile
    tracing --profile errors up -d <services>` so operators never type that
    raw command themselves (the ops-wrapper principle) -- dashboards are
    already pre-provisioned as code via docker/grafana/provisioning, so
    bringing the stack up is the only step needed to get a populated SRE
    view. Re-running is safe: `docker compose up -d` only (re)creates what's
    missing/changed.

    The service list passed to `up -d` is resolved dynamically via `docker
    compose config --services` and explicitly excludes `CORE_APP_SERVICES`.
    This matters because `ollama`/`cassandra`/`api`/`web` have no `profiles:`
    key in docker-compose.yml, so Compose treats them as always-on default
    services -- `--profile` flags alone would NOT stop `up -d` from also
    building and starting the entire core app stack, which would collide
    with a native install's own processes on the same ports.

    Skips (without failing `ops install`) on hosts without Docker: these
    tools have no native/Homebrew path, so a native-only host simply won't
    have dashboards until Docker is installed.
    """
    if not _compose_available():
        return [
            OpsResult(
                True,
                "Skipped observability stack (Docker not found)",
                "Grafana/Loki/Jaeger/GlitchTip only ship as Docker Compose profiles -- "
                "install Docker, then re-run `nyxgpt ops install` (or `nyxgpt ops "
                "observability`) to get dashboards. See docs/docker-compose.md.",
            )
        ]

    base_cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        base_cmd += ["--profile", profile]

    services_cp = _run(base_cmd + ["config", "--services"], check=False)
    if services_cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to resolve observability services",
                (services_cp.stderr or services_cp.stdout or "").strip(),
            )
        ]

    observability_services = sorted(
        {s.strip() for s in services_cp.stdout.splitlines() if s.strip()} - CORE_APP_SERVICES
    )
    if not observability_services:
        return [
            OpsResult(
                False,
                "No observability services resolved",
                "`docker compose config --services` returned no services outside the "
                "core app stack for the monitoring/logging/tracing/errors profiles -- "
                "check docker-compose.yml.",
            )
        ]

    cmd = base_cmd + ["up", "-d"] + observability_services

    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to start observability stack",
                (cp.stderr or cp.stdout or "").strip(),
            )
        ]

    _enable_observability_config()

    return [
        OpsResult(
            True,
            "Observability stack up: Grafana http://localhost:3001, "
            "Jaeger http://localhost:16686, Loki via Grafana Explore, "
            "GlitchTip http://localhost:8080",
            "Dashboards, tracing, and log search are live with no further steps. "
            "GlitchTip's admin user/org/project/DSN are auto-provisioned next by "
            "`nyxgpt ops glitchtip-init` (run automatically as part of `nyxgpt ops "
            "install`) once its container passes its health check.",
        )
    ]


def observability(_args) -> int:
    """CLI entrypoint for `nyxgpt ops observability`.

    Starts the monitoring/logging/tracing/errors Compose profiles (Grafana,
    Prometheus, Loki, promtail, the OTel collector, Jaeger, GlitchTip) so
    operators never need to run a raw `docker compose --profile X up`
    themselves. Idempotent: re-running just confirms everything is already up.

    Returns 0 on success, else 2.
    """
    logger.info(
        "ops: observability starting", extra={"component": "ops", "action": "observability"}
    )
    results = _start_observability_stack()
    ok = _emit_results("observability", results)
    logger.info(
        "ops: observability %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "observability", "ok": ok},
    )
    return 0 if ok else 2


# --- Env sync public API ---

# Maps a Docker Compose `.env` variable to the config.ini `[section] key` it's
# derived from. config.ini (generated by `nyxgpt wizard`) is the single
# source of truth for these secrets -- Compose's `.env` is a generated
# artifact, not something the user hand-edits.
COMPOSE_ENV_SECRET_MAP: dict[str, tuple[str, str]] = {
    "NYXGPT_AUTH_API_KEY": ("auth", "api_key"),
    "GRAFANA_ADMIN_PASSWORD": ("monitoring", "grafana_admin_password"),
}


def sync_env_from_config(
    cfg_path: Path | None = None, env_path: Path | None = None
) -> list[OpsResult]:
    """Derive Docker Compose's `.env` secrets from `~/.nyxGPT/config.ini`.

    Overwrites (or appends) only the secret lines listed in
    `COMPOSE_ENV_SECRET_MAP`; every other line in `.env` (ports, image tags,
    etc.) is left untouched. If `.env` doesn't exist yet, it's seeded from
    `.env.example`.
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini and its secrets.",
            )
        ]

    cfg = load_config(cfg_path)

    env_path = env_path or (REPO_ROOT / ".env")
    example_path = REPO_ROOT / ".env.example"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif example_path.exists():
        lines = example_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    synced: list[str] = []
    for var_name, (section, key) in COMPOSE_ENV_SECRET_MAP.items():
        value = cfg.get(section, key, fallback="")
        if not value:
            continue
        new_line = f"{var_name}={value}"
        for i, line in enumerate(lines):
            if line.startswith(f"{var_name}="):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        synced.append(var_name)

    if not synced:
        if not cfg.getboolean("auth", "enabled", fallback=False):
            return [
                OpsResult(
                    True,
                    "No secrets to sync (auth disabled)",
                    "[auth] enabled = false with no api_key set is a valid "
                    "localhost-only configuration -- .env left untouched. Run "
                    "`nyxgpt wizard` to generate secrets before any networked "
                    "deploy, then re-run `nyxgpt ops env-sync`.",
                )
            ]
        return [
            OpsResult(
                False,
                "No secrets found in config.ini to sync",
                f"Set [auth] api_key and/or [monitoring] grafana_admin_password in "
                f"{cfg_path} (re-run `nyxgpt wizard` to generate them), then retry.",
            )
        ]

    _ensure_dir(env_path.parent)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)

    return [
        OpsResult(
            True,
            f"Synced {', '.join(synced)} into {env_path} from {cfg_path}",
        )
    ]


def env_sync(args) -> int:
    """CLI entrypoint for `nyxgpt ops env-sync`.

    Reads `--config`/`--env-file` overrides (if given) from `args`, then
    derives Docker Compose's `.env` secrets from config.ini via
    `sync_env_from_config`, printing an OK/FAIL line per result.

    Returns 0 on success, else 2.
    """
    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    env_path = Path(args.env_file).expanduser() if getattr(args, "env_file", None) else None

    logger.info(
        "ops: env-sync starting (config=%s, env_file=%s)",
        cfg_path,
        env_path,
        extra={"component": "ops", "action": "env-sync"},
    )

    results = sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    ok = _emit_results("env-sync", results)
    logger.info(
        "ops: env-sync %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "env-sync", "ok": ok},
    )

    return 0 if ok else 2


# --- GlitchTip auto-provisioning (`nyxgpt ops glitchtip-init`) ---

# Fixed org/project names -- reused (not recreated) on every re-run, which is
# what makes this idempotent.
GLITCHTIP_ORG_SLUG = "nyxgpt"
GLITCHTIP_ORG_NAME = "nyxgpt"
GLITCHTIP_TEAM_SLUG = "nyxgpt"
GLITCHTIP_PROJECT_SLUG = "nyxgpt-backend"
GLITCHTIP_PROJECT_NAME = "nyxgpt-backend"
GLITCHTIP_TOKEN_NAME = "nyxgpt-ops-glitchtip-init"
GLITCHTIP_TOKEN_SCOPES = ["org:read", "org:write", "project:read", "project:write"]
GLITCHTIP_DEFAULT_ADMIN_EMAIL = "admin@nyxgpt.local"


def _glitchtip_container_healthy() -> bool:
    """Whether the `glitchtip` Compose container currently reports healthy."""
    for status in self_heal.list_component_status():
        if status.service == "glitchtip":
            return status.healthy
    return False


def _wait_for_glitchtip_healthy(timeout: float = 120.0, poll_interval: float = 3.0) -> bool:
    """Poll until the `glitchtip` container reports healthy, or `timeout` elapses.

    Returns False immediately (no polling) if the container isn't part of the
    currently running Compose stack at all -- e.g. `--skip-observability` was
    used, or Docker isn't installed -- so a host with no GlitchTip never
    stalls `nyxgpt ops install`. Otherwise waits out its health-check
    `start_period` (see docker-compose.yml), since a container freshly
    started by `_start_observability_stack` is not immediately reachable.
    """
    statuses = [s for s in self_heal.list_component_status() if s.service == "glitchtip"]
    if not statuses:
        return False
    if statuses[0].healthy:
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if _glitchtip_container_healthy():
            return True
    return False


def _resolve_admin_credentials(cfg_path: Path) -> tuple[str, str, bool]:
    """Return `(email, password, generated)` for the GlitchTip admin user.

    Reads `[error_tracking] admin_email`/`admin_password` from `cfg_path` if
    already set; generates a strong password when none is configured. Does
    not write anything -- see `_persist_admin_credentials`.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)

    email = parser.get("error_tracking", "admin_email", fallback="").strip()
    email = email or GLITCHTIP_DEFAULT_ADMIN_EMAIL
    password = parser.get("error_tracking", "admin_password", fallback="").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(24)
    return email, password, generated


def _persist_admin_credentials(cfg_path: Path, email: str, password: str) -> None:
    """Write the (possibly generated) admin email/password back to `cfg_path`.

    Same trust model as `[auth] api_key`: GlitchTip is loopback-only, so this
    is acceptable to store in config.ini chmod 600 -- see docs/self-healing.md.
    Only ever called with the native `~/.nyxGPT/config.ini` path, never the
    git-tracked `docker/config.docker.ini` template.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)
    if not parser.has_section("error_tracking"):
        parser.add_section("error_tracking")
    parser.set("error_tracking", "admin_email", email)
    parser.set("error_tracking", "admin_password", password)

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(cfg_path, 0o600)


def _glitchtip_ensure_superuser(email: str, password: str) -> OpsResult:
    """Idempotently ensure a GlitchTip superuser exists via Django's `createsuperuser --noinput`.

    Non-interactive: credentials are passed as the `DJANGO_SUPERUSER_*`
    environment variables Django's own `createsuperuser` management command
    reads directly, so this never blocks on a TTY prompt. `--noinput` exits
    non-zero if the account already exists -- treated as success here so
    re-running `glitchtip-init` after the first successful run is a no-op.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "exec",
        "-T",
        "-e",
        f"DJANGO_SUPERUSER_EMAIL={email}",
        "-e",
        f"DJANGO_SUPERUSER_PASSWORD={password}",
        "-e",
        f"DJANGO_SUPERUSER_USERNAME={email}",
        "glitchtip",
        "./manage.py",
        "createsuperuser",
        "--noinput",
    ]
    try:
        cp = _run(cmd, check=False)
    except Exception as e:
        return OpsResult(
            False, "Failed to run GlitchTip createsuperuser", f"{type(e).__name__}: {e}"
        )

    if cp.returncode == 0:
        return OpsResult(True, f"Created GlitchTip admin user {email}")

    combined = ((cp.stdout or "") + (cp.stderr or "")).lower()
    if "already" in combined or "unique" in combined:
        return OpsResult(True, f"GlitchTip admin user {email} already exists")

    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )
    return OpsResult(False, "Failed to ensure GlitchTip admin user", details.strip())


def _glitchtip_http_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """Construct an `httpx.Client` against GlitchTip's base URL.

    A thin wrapper purely so tests can monkeypatch it to inject an
    `httpx.MockTransport` instead of hitting a real network socket.
    """
    return httpx.Client(base_url=base_url, timeout=10.0, **kwargs)


def _glitchtip_login(
    base_url: str, email: str, password: str
) -> tuple[httpx.Client | None, OpsResult]:
    """Log into GlitchTip as `email`/`password`, returning an authenticated client.

    Modern GlitchTip (>= 5.x, verified on 6.2.0) serves django-allauth's
    headless browser API: `GET /_allauth/browser/v1/config` primes the
    `csrftoken` cookie, which is echoed back as `X-CSRFToken` on the login
    POST; the resulting session cookie carries auth forward for
    `_glitchtip_ensure_api_token` to mint a bearer token from. Older
    releases used a plain Django/DRF session view at `/api/auth/login/` --
    if the headless route 404s, this falls back to that legacy flow so the
    provisioning works across image pins.
    """

    def _csrf_headers(client: httpx.Client) -> dict[str, str]:
        headers = {"Referer": base_url}
        csrf_token = client.cookies.get("csrftoken", "")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        return headers

    client = _glitchtip_http_client(base_url, follow_redirects=True)
    try:
        client.get("/_allauth/browser/v1/config")
        resp = client.post(
            "/_allauth/browser/v1/auth/login",
            json={"email": email, "password": password},
            headers=_csrf_headers(client),
        )
        if resp.status_code == 404:
            client.get("/api/auth/login/")
            resp = client.post(
                "/api/auth/login/",
                json={"email": email, "password": password},
                headers=_csrf_headers(client),
            )
        if resp.status_code >= 400:
            client.close()
            return None, OpsResult(
                False,
                "Failed to authenticate to GlitchTip",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        return client, OpsResult(True, "Authenticated to GlitchTip")
    except httpx.HTTPError as e:
        client.close()
        return None, OpsResult(False, "Failed to reach GlitchTip API", f"{type(e).__name__}: {e}")


def _glitchtip_ensure_api_token(
    client: httpx.Client, base_url: str
) -> tuple[str | None, OpsResult]:
    """Mint (or reuse) a scoped GlitchTip API token for `nyxgpt ops` automation.

    Uses the authenticated session from `_glitchtip_login` for this one call
    (token creation is CSRF-guarded like any other POST); every call after
    this switches to `Authorization: Bearer <token>`.
    """
    try:
        listing = client.get("/api/0/api-tokens/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for tok in existing:
                    if not isinstance(tok, dict):
                        continue
                    if tok.get("name") == GLITCHTIP_TOKEN_NAME and tok.get("token"):
                        return str(tok["token"]), OpsResult(
                            True, "Reusing existing GlitchTip API token"
                        )

        csrf_token = client.cookies.get("csrftoken", "")
        headers = {"Referer": base_url}
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token

        resp = client.post(
            "/api/0/api-tokens/",
            json={"name": GLITCHTIP_TOKEN_NAME, "scopes": GLITCHTIP_TOKEN_SCOPES},
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip API token",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        token = None
        if isinstance(data, dict):
            token = data.get("token") or data.get("key")
        if not token:
            return None, OpsResult(
                False, "GlitchTip API token response missing a token", str(data)[:500]
            )
        return str(token), OpsResult(True, "Created GlitchTip API token")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to create GlitchTip API token", f"{type(e).__name__}: {e}"
        )


def _glitchtip_ensure_organization(client: httpx.Client) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt` GlitchTip organization exists, returning its slug."""
    try:
        listing = client.get("/api/0/organizations/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for org in existing:
                    if isinstance(org, dict) and org.get("slug") == GLITCHTIP_ORG_SLUG:
                        return GLITCHTIP_ORG_SLUG, OpsResult(
                            True, f"Using existing GlitchTip organization {GLITCHTIP_ORG_SLUG}"
                        )

        resp = client.post("/api/0/organizations/", json={"name": GLITCHTIP_ORG_NAME})
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip organization",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_ORG_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_ORG_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip organization {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip organization", f"{type(e).__name__}: {e}"
        )


def _glitchtip_ensure_team(client: httpx.Client, org_slug: str) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt` GlitchTip team exists under `org_slug`, returning its slug.

    Modern GlitchTip (verified on 6.2.0) only creates projects under a team
    (the org-level projects route is list-only), so provisioning needs a team
    before `_glitchtip_ensure_project` can create anything.
    """
    try:
        listing = client.get(f"/api/0/organizations/{org_slug}/teams/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for team in existing:
                    if isinstance(team, dict) and team.get("slug") == GLITCHTIP_TEAM_SLUG:
                        return GLITCHTIP_TEAM_SLUG, OpsResult(
                            True, f"Using existing GlitchTip team {GLITCHTIP_TEAM_SLUG}"
                        )

        resp = client.post(
            f"/api/0/organizations/{org_slug}/teams/",
            json={"slug": GLITCHTIP_TEAM_SLUG},
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip team",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_TEAM_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_TEAM_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip team {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(False, "Failed to ensure GlitchTip team", f"{type(e).__name__}: {e}")


def _glitchtip_ensure_project(
    client: httpx.Client, org_slug: str, team_slug: str
) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt-backend` GlitchTip project exists under `org_slug`, returning its slug.

    Creates via the team-scoped route (`/api/0/teams/{org}/{team}/projects/`),
    the only creation path modern GlitchTip serves; falls back to the legacy
    org-level POST if the team route is absent on an older image.
    """
    try:
        listing = client.get(f"/api/0/organizations/{org_slug}/projects/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for proj in existing:
                    if isinstance(proj, dict) and proj.get("slug") == GLITCHTIP_PROJECT_SLUG:
                        return GLITCHTIP_PROJECT_SLUG, OpsResult(
                            True, f"Using existing GlitchTip project {GLITCHTIP_PROJECT_SLUG}"
                        )

        resp = client.post(
            f"/api/0/teams/{org_slug}/{team_slug}/projects/",
            json={"name": GLITCHTIP_PROJECT_NAME, "platform": "python"},
        )
        if resp.status_code in (404, 405):
            resp = client.post(
                f"/api/0/organizations/{org_slug}/projects/",
                json={"name": GLITCHTIP_PROJECT_NAME, "platform": "python"},
            )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip project",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_PROJECT_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_PROJECT_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip project {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip project", f"{type(e).__name__}: {e}"
        )


def _extract_dsn(key: Any) -> str:
    """Pull the public DSN out of a GlitchTip ProjectKey response, tolerating shape variants."""
    if not isinstance(key, dict):
        return ""
    dsn_field = key.get("dsn")
    if isinstance(dsn_field, dict):
        return str(dsn_field.get("public") or "")
    if isinstance(dsn_field, str):
        return dsn_field
    return ""


def _glitchtip_ensure_project_key(
    client: httpx.Client, org_slug: str, project_slug: str
) -> tuple[str | None, OpsResult]:
    """Ensure a GlitchTip project key exists for `org_slug`/`project_slug`, returning its DSN."""
    try:
        listing = client.get(f"/api/0/projects/{org_slug}/{project_slug}/keys/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for key in existing:
                    dsn = _extract_dsn(key)
                    if dsn:
                        return dsn, OpsResult(True, "Using existing GlitchTip project key")

        resp = client.post(
            f"/api/0/projects/{org_slug}/{project_slug}/keys/", json={"name": "nyxgpt"}
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip project key",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        dsn = _extract_dsn(data)
        if not dsn:
            return None, OpsResult(
                False, "GlitchTip project key response missing a DSN", str(data)[:500]
            )
        return dsn, OpsResult(True, "Created GlitchTip project key")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip project key", f"{type(e).__name__}: {e}"
        )


def _patch_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Return `text` with `key = value` set inside `[section]`, preserving
    every other line -- including comments -- verbatim.

    Unlike a `ConfigParser` read/write round-trip (which drops comments),
    this only ever rewrites the single matching `key = ...` line, or
    appends one if the key/section isn't present yet. Used for
    `docker/config.docker.ini`, a git-tracked file whose `[error_tracking]`
    section carries hand-written documentation comments that must survive
    `nyxgpt ops glitchtip-init` re-runs.
    """
    lines = text.splitlines(keepends=True)
    section_header_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*).*$")

    section_start: int | None = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = section_header_re.match(line)
        if m:
            if section_start is not None:
                section_end = i
                break
            if m.group(1) == section:
                section_start = i

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"[{section}]\n")
        lines.append(f"{key} = {value}\n")
        return "".join(lines)

    for i in range(section_start + 1, section_end):
        if key_re.match(lines[i]):
            lines[i] = f"{key} = {value}\n"
            return "".join(lines)

    lines.insert(section_end, f"{key} = {value}\n")
    return "".join(lines)


def _write_error_tracking_dsn(cfg_path: Path, dsn: str, *, chmod_600: bool) -> OpsResult:
    """Write the provisioned DSN and `enabled = true` into `[error_tracking]` in `cfg_path`.

    No-ops (reports ok) if `cfg_path` doesn't exist -- not every host has
    both `~/.nyxGPT/config.ini` (native) and `docker/config.docker.ini`
    (Compose) in play. `chmod_600` should only be set for the native path:
    `docker/config.docker.ini` is a git-tracked template, not a secrets
    file -- the DSN is a public key, safe to store there (see
    docs/self-healing.md) -- so its permissions are left untouched.

    Patches only the `dsn`/`enabled` lines in place (via `_patch_ini_value`)
    rather than round-tripping through `ConfigParser`, so any comments in
    `cfg_path` -- notably the documentation comments in the git-tracked
    `docker/config.docker.ini` -- survive untouched.
    """
    if not cfg_path.exists():
        return OpsResult(True, f"Skipped {cfg_path} (not present)")

    text = cfg_path.read_text(encoding="utf-8")
    text = _patch_ini_value(text, "error_tracking", "dsn", dsn)
    text = _patch_ini_value(text, "error_tracking", "enabled", "true")
    cfg_path.write_text(text, encoding="utf-8")

    if chmod_600:
        os.chmod(cfg_path, 0o600)

    return OpsResult(True, f"Wrote GlitchTip DSN into {cfg_path}")


def _provision_glitchtip() -> list[OpsResult]:
    """Auto-provision a GlitchTip admin user, org, project, and DSN -- idempotent, zero-touch.

    Only runs when the `glitchtip` Compose container is up and passes its
    health check; no-ops with a clear message otherwise (e.g.
    `--skip-observability`, no Docker, or a freshly started container still
    inside its health-check `start_period`). Safe to call repeatedly: every
    step first checks for an existing admin user / org / project / key
    before creating one, so re-running never duplicates anything.
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped GlitchTip auto-provisioning (Docker not found)")]

    if not _wait_for_glitchtip_healthy():
        return [
            OpsResult(
                True,
                "Skipped GlitchTip auto-provisioning (glitchtip container not up/healthy)",
                "Run `nyxgpt ops observability` (or `nyxgpt ops install`) to start it, then "
                "retry `nyxgpt ops glitchtip-init`.",
            )
        ]

    native_cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not native_cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {native_cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]

    results: list[OpsResult] = []

    email, password, generated = _resolve_admin_credentials(native_cfg_path)
    if generated:
        _persist_admin_credentials(native_cfg_path, email, password)
        results.append(
            OpsResult(True, f"Generated and saved a GlitchTip admin password to {native_cfg_path}")
        )

    su_result = _glitchtip_ensure_superuser(email, password)
    results.append(su_result)
    if not su_result.ok:
        return results

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(native_cfg_path)
    base_url = parser.get("error_tracking", "glitchtip_ui_url", fallback="http://localhost:8080")

    login_client, login_result = _glitchtip_login(base_url, email, password)
    results.append(login_result)
    if login_client is None:
        return results

    token: str | None = None
    try:
        token, token_result = _glitchtip_ensure_api_token(login_client, base_url)
        results.append(token_result)
    finally:
        login_client.close()
    if token is None:
        return results

    api_client = _glitchtip_http_client(base_url, headers={"Authorization": f"Bearer {token}"})
    dsn: str | None = None
    try:
        org_slug, org_result = _glitchtip_ensure_organization(api_client)
        results.append(org_result)
        if org_slug is None:
            return results

        team_slug, team_result = _glitchtip_ensure_team(api_client, org_slug)
        results.append(team_result)
        if team_slug is None:
            return results

        project_slug, project_result = _glitchtip_ensure_project(api_client, org_slug, team_slug)
        results.append(project_result)
        if project_slug is None:
            return results

        dsn, key_result = _glitchtip_ensure_project_key(api_client, org_slug, project_slug)
        results.append(key_result)
        if dsn is None:
            return results
    finally:
        api_client.close()

    compose_cfg_path = REPO_ROOT / "docker" / "config.docker.ini"
    results.append(_write_error_tracking_dsn(native_cfg_path, dsn, chmod_600=True))
    results.append(_write_error_tracking_dsn(compose_cfg_path, dsn, chmod_600=False))

    return results


def glitchtip_init(_args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops glitchtip-init`.

    Auto-provisions a GlitchTip admin user, organization, project, and DSN
    with no manual sign-in step, writing the DSN into config.ini. Idempotent
    -- safe to re-run any time. No-ops with a clear message if the
    `glitchtip` Compose container isn't up/healthy.

    Returns 0 on success (including a clean no-op), else 2.
    """
    logger.info(
        "ops: glitchtip-init starting", extra={"component": "ops", "action": "glitchtip-init"}
    )
    results = _provision_glitchtip()
    ok = _emit_results("glitchtip-init", results)
    logger.info(
        "ops: glitchtip-init %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "glitchtip-init", "ok": ok},
    )
    return 0 if ok else 2
