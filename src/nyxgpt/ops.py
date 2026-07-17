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
import os
import shutil
import subprocess
import tarfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nyxgpt import self_heal

# Repo root: .../nyxGPT/src/nyxgpt/ops.py -> parents[2] is repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# Maps a logical component to its Homebrew service name for native mode.
# Cassandra has no native brew service -- per PHASE_6_PLAN.md it stays the one
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


def install(_args) -> int:
    """CLI entrypoint for `nyxgpt ops install`.

    Runs every install step (scripts, web deps, MCP deps, Cassandra
    LaunchAgent, Homebrew formulas, log symlinks), printing an OK/FAIL line
    per result. A failure in one step doesn't stop the rest from running.

    Returns 0 if every step succeeded, else 2.
    """
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("scripts", _install_scripts),
        ("web deps", _ensure_web_deps),
        ("mcp deps", _ensure_mcp_deps),
        ("cassandra launchagent", _install_cassandra_launchagent),
        ("homebrew api", _install_homebrew_api),
        ("homebrew web", _install_homebrew_web),
        ("log symlinks", _ensure_log_symlinks),
    ]
    for step_name, fn in steps:
        try:
            results += fn()
        except Exception as e:
            results.append(
                OpsResult(
                    False,
                    f"ops install failed: {step_name}",
                    f"{type(e).__name__}: {e}",
                )
            )

    ok = True
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        ok = ok and r.ok

    return 0 if ok else 2


def status(_args) -> int:
    """CLI entrypoint for `nyxgpt ops status`.

    Prints the detected deployment mode (native vs. Compose per component),
    a native/Compose port-conflict warning if both are live, Homebrew
    service states, the Cassandra log-follower LaunchAgent's load state, and
    whether the ops-managed Cassandra Docker container is running.

    Always returns 0.
    """
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
        print(
            "\nWARNING: "
            + ", ".join(sorted(mode.conflicts))
            + " reported running in BOTH native and Docker Compose. Only one is actually "
            "serving traffic on the shared port -- the other is a phantom backend. Config "
            f"edits to {NATIVE_CONFIG_HINT} only reach the native process; if Compose is the "
            f"one answering, edit {COMPOSE_CONFIG_HINT} instead. Stop one deployment before "
            "continuing."
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

    return 0


def doctor(_args) -> int:
    """CLI entrypoint for `nyxgpt ops doctor`.

    Checks for common misconfigurations: missing ~/.nyxGPT/config.ini,
    non-executable helper scripts, missing brew/docker/node/npm tools on
    PATH, and missing/incomplete web dependencies (node_modules, undici).
    Prints each issue found.

    Returns 0 if no issues were found, else 2.
    """
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

    if issues:
        print("nyxGPT ops doctor: FAIL")
        for i in issues:
            print(f"- {i}")
        return 2

    print("nyxGPT ops doctor: OK")
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

    ok = True
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        ok = ok and r.ok

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

    result = self_heal.component_logs(service, tail=tail)
    if not result.ok:
        print(f"[FAIL] {result.message}")
        if result.details:
            print(f"  {result.details}")
        return 2

    print(f"--- {result.message} ---")
    print(result.details or "(no output)")
    return 0


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

    results = sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    ok = True
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        ok = ok and r.ok

    return 0 if ok else 2
