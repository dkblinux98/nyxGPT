"""Operational commands for `nyxgpt ops`: install, status, doctor, restart, logs, env-sync.

Wraps the native (Homebrew services + LaunchAgents) and Docker-managed
(Cassandra container, Docker Compose stack) pieces of a local nyxGPT
deployment behind a single CLI surface, so operators never need to run raw
`brew`/`docker`/`launchctl` commands themselves. Also cross-checks for a
Compose deployment running alongside the native one so `status`/`restart`
can warn about -- and refuse to create -- port collisions between the two.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

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

# Container path promtail's docker-compose.yml service binds to native-mode
# host logs (~/.nyxGPT/logs). `_log_aggregation_wiring_issue` greps for this
# marker to catch a regression (see #3277) where that bind mount is dropped
# and native-mode logs silently stop reaching Loki.
PROMTAIL_NATIVE_LOG_MOUNT_MARKER = "/var/log/nyxgpt-native/logs"


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


def _find_launchagent_template() -> tuple[Path | None, list[Path]]:
    """
    Locate the Cassandra log follower LaunchAgent template inside the repo.
    Returns (path_or_none, candidates_checked).
    """
    candidates = [
        REPO_ROOT / "ops" / "launchagents" / "com.nyxgpt.cassandra-logs.plist",
        REPO_ROOT / "ops" / "LaunchAgents" / "com.nyxgpt.cassandra-logs.plist",
        REPO_ROOT / "com.nyxgpt.cassandra-logs.plist",
        REPO_ROOT / "homebrew" / "com.nyxgpt.cassandra-logs.plist",
    ]
    for p in candidates:
        try:
            if p.exists():
                return p, candidates
        except Exception:
            # If something odd happens (permissions, broken symlink), keep searching.
            continue
    return None, candidates


def _install_scripts() -> list[OpsResult]:
    """Copy the run-web/follow-cassandra-logs helper scripts into ~/.nyxGPT/scripts, executable.

    Scripts not present in the repo's `scripts/` dir are skipped (reported
    as ok, since not every deployment needs them). Returns one OpsResult per
    script considered.
    """
    results: list[OpsResult] = []
    src_dir = REPO_ROOT / "scripts"
    dst_dir = Path.home() / ".nyxGPT" / "scripts"
    _ensure_dir(dst_dir)

    for name in ("run-web.sh", "follow-cassandra-logs.sh"):
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
    _copy_file(tpl, dst)

    label = "com.nyxgpt.cassandra-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Cassandra logs LaunchAgent", str(dst)))
    return results


def _ensure_log_symlinks() -> list[OpsResult]:
    """Symlink each Homebrew-managed service log into ~/.nyxGPT/logs for convenient access.

    Replaces any existing file/symlink at the destination. Returns one
    OpsResult per (component, extension) log symlink attempted.
    """
    results: list[OpsResult] = []
    home_logs = Path.home() / ".nyxGPT" / "logs"
    _ensure_dir(home_logs)

    brew_logs = _brew_prefix() / "var" / "log"
    for base in ("nyxgpt-api", "nyxgpt-web"):
        for ext in (".log", ".err.log"):
            src = brew_logs / f"{base}{ext}"
            dst = home_logs / f"{base}{ext}"
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
    # Update the sha256 in the formula to match the generated tarball
    import re

    content = re.sub(r'sha256 "[a-f0-9]+"', f'sha256 "{sha}"', content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-api.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-api formula", str(dst)))

    _run(["brew", "install", "--overwrite", f"{tap}/nyxgpt-api"], check=False)
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

    content = template.read_text(encoding="utf-8")
    content = content.replace("__NYXGPT_WEB_URL__", url)
    content = content.replace("__NYXGPT_WEB_SHA256__", sha)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-web.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-web formula", str(dst)))

    _run(["brew", "install", "--overwrite", f"{tap}/nyxgpt-web"], check=False)
    _run(["brew", "services", "start", "nyxgpt-web"], check=False)
    results.append(OpsResult(True, "Requested brew install/start nyxgpt-web", ""))

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

    try:
        cp = subprocess.run(["npm", "install"], cwd=str(root_dir), text=True, capture_output=True)
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True, "Installed MCP deps via npm install", str(root_dir / "node_modules")
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
CASSANDRA_IMAGE = "cassandra:5.0"
CASSANDRA_VOLUME = "nyxgpt_cassandra_data"


def _ensure_cassandra_container() -> list[OpsResult]:
    """Ensure the local `nyxgpt-cassandra` Docker container exists and is running.

    Reconciles to the intended state rather than only adding:
    - running: nothing to do.
    - present but not running (exited/created/paused/...): `docker start` it.
    - absent: `docker run` a fresh container from `CASSANDRA_IMAGE`/`CASSANDRA_VOLUME`,
      bound to `${NYXGPT_BIND_ADDR:-127.0.0.1}:${CASSANDRA_PORT:-9042}`.

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
        f"{CASSANDRA_VOLUME}:/var/lib/cassandra",
        CASSANDRA_IMAGE,
    ]
    cp = _run(cmd, check=False)
    if cp.returncode == 0:
        return [
            OpsResult(
                True,
                f"Created Cassandra container: {CASSANDRA_CONTAINER_NAME} ({CASSANDRA_IMAGE})",
                f"Bound to {bind_addr}:{port}, data persisted in volume {CASSANDRA_VOLUME}",
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


def install(args) -> int:
    """CLI entrypoint for `nyxgpt ops install`.

    Reconciles the local machine to the intended native-mode topology (see
    docs/ops.md): first stopping any phantom Docker Compose app-tier containers
    leaked from an earlier run or a raw `docker compose up`, then ensuring the
    local Cassandra container plus every other install step (scripts, web deps,
    MCP deps, Cassandra LaunchAgent, Homebrew formulas, log symlinks, the
    observability stack) -- printing an OK/FAIL line per result. A failure in
    one step doesn't stop the rest from running.

    The observability step (Grafana/Loki/Jaeger/GlitchTip) runs by default so
    a fresh install comes up with the full SRE view already populated --
    pass `--skip-observability` to opt out (e.g. on a host with no Docker,
    or to keep those Compose profiles stopped for resource reasons).

    Returns 0 if every step succeeded, else 2.
    """
    logger.info("ops: install starting", extra={"component": "ops", "action": "install"})

    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("phantom compose reconciliation", _reconcile_phantom_compose_app_containers),
        ("scripts", _install_scripts),
        ("web deps", _ensure_web_deps),
        ("mcp deps", _ensure_mcp_deps),
        ("cassandra container", _ensure_cassandra_container),
        ("cassandra launchagent", _install_cassandra_launchagent),
        ("homebrew api", _install_homebrew_api),
        ("homebrew web", _install_homebrew_web),
        ("log symlinks", _ensure_log_symlinks),
    ]
    if not getattr(args, "skip_observability", False):
        steps.append(("observability stack", _start_observability_stack))
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
    for component in ("api", "web", "ollama", "cassandra"):
        print(f"  native  {component}: {mode.native.get(component, 'none')}")
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

    if _which("docker"):
        running = mode.native.get("cassandra") == "running"
        print(f"\nDocker container nyxgpt-cassandra: {'RUNNING' if running else 'NOT RUNNING'}")
    else:
        print("\nDocker: docker not found")

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

    for name in ("run-web.sh", "follow-cassandra-logs.sh"):
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
        f"Refusing to restart native {component}: a Docker Compose deployment of "
        f"{component} is already running{port_note}",
        "Both deployments would try to bind the same port. Stop the Compose deployment "
        "of this component (or manage it there) before restarting the native service.",
    )


def restart(args) -> int:
    """Restart operational components.

    target: all|api|web|ollama|cassandra|cassandra-logs

    Before touching a native component, checks whether a Docker Compose deployment
    of that same component is already live and, if so, refuses rather than starting
    a second process/container that would collide on the same port.
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


def _compose_down(services: list[str], *, volumes: bool) -> list[OpsResult]:
    """Tear down the given Compose `services` via `docker compose down`.

    Removes containers/networks for exactly the listed services; `volumes`
    additionally removes their named volumes (destructive -- data loss).
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped Compose teardown (Docker not found)")]
    if not services:
        return [OpsResult(True, "No Compose services to tear down for this scope")]

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "down"] + services
    if volumes:
        cmd.append("--volumes")
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to tear down Compose services",
                (cp.stderr or cp.stdout or "").strip(),
            )
        ]
    suffix = " and their volumes" if volumes else " (volumes preserved)"
    return [
        OpsResult(True, f"Removed Compose containers{suffix}: {', '.join(services)}"),
    ]


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
    """
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
# excludes `error_tracking`: it needs a GlitchTip project DSN, which nothing
# here can safely provision without an owner to sign in and create it -- see
# `_start_observability_stack`'s returned message.
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
            "Dashboards, tracing, and log search are live with no further steps. GlitchTip "
            "has one remaining manual step: sign in, create a project, and paste its DSN "
            "into [error_tracking] dsn in config.ini -- nothing here can safely create "
            "that DSN without an owner to sign in and claim the account first. See "
            "docs/self-healing.md.",
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
