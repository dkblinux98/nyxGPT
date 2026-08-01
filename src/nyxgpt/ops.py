"""Operational commands for `nyxgpt ops`: install, status, doctor, restart, logs, env-sync.

Wraps the native (Homebrew services + LaunchAgents) and Docker-managed
(Cassandra container, Docker Compose stack) pieces of a local nyxGPT
deployment behind a single CLI surface, so operators never need to run raw
`brew`/`docker`/`launchctl` commands themselves. Also cross-checks for a
Compose deployment running alongside the native one so `status`/`restart`
can warn about -- and refuse to create -- port collisions between the two.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import importlib.metadata
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
from collections.abc import Callable, Iterator
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from nyxgpt import metrics as prom_metrics
from nyxgpt import self_heal, tracing
from nyxgpt.config import (
    get_log_aggregation_enabled,
    get_monitoring_config,
    get_monitoring_slack_webhook_url,
    get_tracing_config,
    get_tracing_enabled,
    grafana_admin_password_path,
    resolve_grafana_admin_password,
)
from nyxgpt.logging import get_correlation_id

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

# The container api-config is a derived, per-machine artifact (like .env):
# it's bind-mounted into the containerized api and gets its DSN filled in at
# runtime by `nyxgpt ops glitchtip-init`, so it is git-ignored. `nyxgpt ops
# install`/`env-sync` regenerates it from the native `~/.nyxGPT/config.ini`
# (the single source of truth) via `_generate_compose_config`.
COMPOSE_CONFIG_FILE = REPO_ROOT / "docker" / "config.docker.ini"

# Compose override that attaches the observability profiles to the
# terraform-managed network (`nyxgpt-terraform`) so they interoperate with the
# terraform-managed core containers -- used by `install --terraform --local`.
TERRAFORM_NET_OVERRIDE = REPO_ROOT / "docker" / "docker-compose.terraform-net.yml"

# Placeholder substituted with the installing user's home directory when a
# LaunchAgent plist template is copied into ~/Library/LaunchAgents -- the
# templates in ops/launchagents/ must never hard-code a real account's home
# directory (see #3276 acceptance failure: the merged plists hard-coded the
# original author's `/Users/darlabaker`, so the installed LaunchAgent pointed
# at a nonexistent script path for every other user).
LAUNCHAGENT_HOME_PLACEHOLDER = "__NYXGPT_HOME__"

# Container path promtail's docker-compose.yml service binds to native-mode
# host logs (~/.nyxGPT/logs). `_log_aggregation_wiring_issue` checks the
# running promtail container's actual mounts (via `docker inspect`) for this
# destination to catch a regression (see #3277, #3349) where that bind mount
# is dropped and native-mode logs silently stop reaching Loki.
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
    # ~/.nyxGPT/volumes/nyxgpt-data directories -- ollama is now shared by
    # native mode too (see #3431): native Ollama isn't containerized, but
    # `_ensure_ollama_service` points its `OLLAMA_MODELS` env var at
    # ~/.nyxGPT/volumes/ollama/models, the same directory Compose/Terraform's
    # `ollama` container bind-mounts (as /root/.ollama), instead of native
    # Ollama's own default ~/.ollama/models store.
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


def _ops_action_outcome(results: list[OpsResult]) -> tuple[str, str]:
    """Reduce a block of `OpsResult`s to a single (result, message) pair.

    `result` is "success" if every result ok'd, "refused" if any failing
    result's message starts with "Refusing" (the existing convention used by
    `_compose_conflict_result`/`_ensure_cassandra_container`'s port-collision
    refusals), else "failure".
    """
    if not results:
        return "success", ""
    failed = [r for r in results if not r.ok]
    if not failed:
        return "success", "; ".join(r.message for r in results)
    if any(r.message.startswith("Refusing") for r in failed):
        return "refused", "; ".join(r.message for r in failed)
    return "failure", "; ".join(r.message for r in failed)


def _record_ops_action(command: str, service: str, result: str, message: str = "") -> None:
    """Record one operator-initiated lifecycle action: a `nyxgpt_ops_actions_total`
    increment plus a structured log event.

    Kept entirely separate from self-heal's own `nyxgpt_selfheal_restarts_total`
    counter and heal-event log (see #3390 -- the self-heal counter answers "how
    often did the system recover itself"; folding operator-driven `nyxgpt ops`
    actions into it would make autonomous heals indistinguishable from manual
    restarts). This is the one place both CLI (`nyxgpt ops ...`) and the
    SRE/admin dashboard's equivalent API calls funnel through, so a metrics gap
    can be attributed to a deliberate `ops down` rather than reading as an
    unexplained outage.

    `correlation_id` (#3430) is `get_correlation_id()`'s resolution: the
    active HTTP request's `request_id` for dashboard-triggered actions, or
    `NYXGPT_CORRELATION_ID` from the environment for CLI-triggered ones (see
    `mint_correlation_id`) -- lets an operator join this event to the
    subprocess it drove, or the request that triggered it.
    """
    correlation_id = get_correlation_id()
    prom_metrics.OPS_ACTIONS_TOTAL.labels(command=command, service=service, result=result).inc()
    log = logger.info if result == "success" else logger.warning
    log(
        "ops: lifecycle action command=%s service=%s result=%s correlation_id=%s%s",
        command,
        service,
        result,
        correlation_id,
        f": {message}" if message else "",
        extra={
            "component": "ops",
            "event": "ops_lifecycle_action",
            "command": command,
            "service": service,
            "result": result,
            "result_message": message,
            "correlation_id": correlation_id,
        },
    )


def record_manual_restart(service: str, ok: bool, message: str = "") -> None:
    """Record a dashboard-triggered "Heal Now" restart as an ops lifecycle action.

    Called by the `/api/v1/self-heal/heal` endpoint for each component it
    restarts -- a manual heal-now click is an operator-initiated restart just
    like `nyxgpt ops restart <service>`, so it's recorded the same way (see
    #3390). This does not touch `nyxgpt_selfheal_restarts_total`, which
    self_heal.heal_now() already increments for this same restart -- that
    counter's accounting of "this component was restarted" is unaffected;
    this only adds the separate operator-action signal.
    """
    _record_ops_action("restart", service, "success" if ok else "failure", message)


def record_canary_action(
    action: str, result: str, message: str = "", *, component: str = "api"
) -> None:
    """Record a canary lifecycle action (deploy/start/promote/rollback) per #3390.

    `canary.py` funnels every rollout action through here rather than calling
    `_record_ops_action` directly, keeping the "canary-<action>" command
    naming convention (mirroring "install"/"restart"/"down") in one place.
    `service` is `component` -- "api" by default (unchanged from before
    #3419), or "web" for the web canary pair (see canary.py's `COMPONENTS`).
    """
    _record_ops_action(f"canary-{action}", component, result, message)


def _run(
    cmd: list[str], *, check: bool = True, expected: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text.

    Raises `subprocess.CalledProcessError` on non-zero exit unless `check=False`.
    Non-zero exits are always logged with the command and a stderr tail first,
    so the evidence reaches Loki even when a caller catches the exception (or
    passes `check=False`) without logging it itself (#3415 gap 5).
    Pass `expected=True` for read-only probes where a non-zero exit is a normal
    outcome (e.g. "not found"/"not running") to log at DEBUG instead of WARNING.
    """
    try:
        result = subprocess.run(cmd, check=check, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        level = logging.DEBUG if expected else logging.WARNING
        logger.log(
            level,
            f"Subprocess exited non-zero (rc={e.returncode}): {' '.join(cmd)}",
            extra={
                "component": "ops",
                "cmd": cmd,
                "returncode": e.returncode,
                "stderr_tail": e.stderr[-2000:] if e.stderr else "",
            },
        )
        raise
    if result.returncode != 0:
        level = logging.DEBUG if expected else logging.WARNING
        logger.log(
            level,
            f"Subprocess exited non-zero (rc={result.returncode}): {' '.join(cmd)}",
            extra={
                "component": "ops",
                "cmd": cmd,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-2000:] if result.stderr else "",
            },
        )
    return result


def _which(prog: str) -> str | None:
    """Return the absolute path to `prog` on PATH, or None if it isn't found."""
    return shutil.which(prog)


def _read_project_version() -> str:
    """Return the project version from pyproject.toml, or "0.0.0" if unreadable.

    The "0.0.0" fallback is deliberately implausible: a brew formula stamped
    with it signals "version could not be determined" instead of silently
    masquerading as a real release.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", "0.0.0"))


def project_version() -> str:
    """Public wrapper around `_read_project_version()`, for cross-module version stamping.

    Used by `canary.py` to stamp the versioned image tag `nyxgpt ops deploy
    --kubernetes` builds (see #3409) -- `_read_project_version` stays private
    since every other caller is internal to this module.
    """
    return _read_project_version()


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


def _tap_repo(tap: str) -> Path:
    """Return the local checkout path of Homebrew tap `tap` (`brew --repo <tap>`)."""
    cp = _run(["brew", "--repo", tap])
    return Path((cp.stdout or "").strip())


# --- Deployment mode detection ---


def _brew_services_snapshot() -> dict[str, str]:
    """Return {brew_service_name: state} parsed from `brew services list`."""
    if _which("brew") is None:
        return {}
    cp = _run(["brew", "services", "list"], check=False, expected=True)
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
        expected=True,
    )
    out = (cp.stdout or "").strip()
    return out.splitlines()[0].strip() if out else "absent"


def _compose_stack_snapshot() -> dict[str, str]:
    """Return {service: state} for the docker-compose.yml stack, if any is running.

    Reuses self_heal.list_component_status(), which already knows how to resolve
    and query the project's docker-compose.yml via `docker compose ps -a`. That
    call returns a combined view -- Compose services plus native components plus
    absent-desired entries -- so this filters to `source == "compose"` only;
    otherwise a native component (e.g. the native `nyxgpt-cassandra` container)
    would fold into this "compose" snapshot and collide with the native reading
    of the very same container in `detect_deployment_mode()`, producing a false
    phantom-backend conflict.
    """
    try:
        statuses = self_heal.list_component_status()
    except Exception as e:
        logger.warning(
            "list_component_status() failed, treating compose stack as empty: %s",
            e,
            extra={"component": "ops"},
        )
        return {}
    return {s.service: s.state for s in statuses if s.source == "compose"}


def _terraform_or_kubernetes_managed_components() -> set[str]:
    """Service names `list_component_status()` currently reports as Terraform/Kubernetes-managed.

    `ops down`/`stop` (no `--terraform`/`--kubernetes` flag) mark api/web/
    ollama/cassandra intentionally stopped by service name, but only ever
    stop the native/Compose form of those services. If a Terraform/
    Kubernetes deployment is what's actually running, marking would flag it
    `desired=False` in `list_component_status()` even though neither this
    call nor its `_stop_*` helpers touched it -- silently blinding self-heal
    to a component that never went down. Returns an empty set (never
    raises) if the status lookup fails, so a lookup error never blocks the
    marking it would otherwise guard.
    """
    try:
        statuses = self_heal.list_component_status()
    except Exception as e:
        logger.warning(
            "list_component_status() failed, treating no components as "
            "Terraform/Kubernetes-managed: %s",
            e,
            extra={"component": "ops"},
        )
        return set()
    return {s.service for s in statuses if s.source in ("terraform", "kubernetes")}


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
        except Exception as e:
            # If something odd happens (permissions, broken symlink), keep searching.
            logger.warning(
                "Could not check candidate path %s, skipping: %s",
                p,
                e,
                extra={"component": "ops"},
            )
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
    """Copy the run-web/follow-cassandra-logs/follow-ollama-logs/set-ollama-models-env
    helper scripts into ~/.nyxGPT/scripts, executable.

    Scripts not present in the repo's `scripts/` dir are skipped (reported
    as ok, since not every deployment needs them). Returns one OpsResult per
    script considered.
    """
    results: list[OpsResult] = []
    src_dir = REPO_ROOT / "scripts"
    dst_dir = Path.home() / ".nyxGPT" / "scripts"
    _ensure_dir(dst_dir)

    for name in (
        "run-web.sh",
        "follow-cassandra-logs.sh",
        "follow-ollama-logs.sh",
        "set-ollama-models-env.sh",
    ):
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

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
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
    LaunchAgent -- `follow-ollama-logs.sh` (which this LaunchAgent runs)
    handles both Compose mode (follows the `nyxgpt-ollama` container's
    `docker logs`) and native mode (tails Homebrew's own ollama.log
    directly), switching between them on its own (see #3441). Returns a
    single-element list of OpsResult; fails if the template can't be found
    among the candidate paths.
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

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Ollama logs LaunchAgent", str(dst)))
    return results


def _install_ollama_env_launchagent() -> list[OpsResult]:
    """Install and (re)load the Ollama shared-model-store env LaunchAgent (#3431).

    Mirrors `_install_ollama_launchagent`/`_install_cassandra_launchagent`:
    locates the `com.nyxgpt.ollama-env.plist` template, copies it into
    ~/Library/LaunchAgents, then boots it out and back in. That LaunchAgent
    runs `scripts/set-ollama-models-env.sh` (`launchctl setenv OLLAMA_MODELS
    ...`) at every login, since a bare `launchctl setenv` call (as done
    immediately by `_ensure_ollama_service`) only applies to the current
    session and would otherwise be lost on reboot. Returns a single-element
    list of OpsResult; fails if the template can't be found among the
    candidate paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template("com.nyxgpt.ollama-env.plist")
    if tpl is None:
        details = "Tried:\n" + "\n".join(str(p) for p in checked) + f"\nREPO_ROOT={REPO_ROOT}"
        results.append(OpsResult(False, "Missing Ollama env LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.ollama-env"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Ollama env LaunchAgent", str(dst)))
    return results


_STALE_LOG_SYMLINK_NAMES: tuple[str, ...] = (
    "nyxgpt-api.log",
    "nyxgpt-api.err.log",
    "nyxgpt-web.log",
    "nyxgpt-web.err.log",
)


def _cleanup_stale_log_symlinks() -> list[OpsResult]:
    """Remove leftover ~/.nyxGPT/logs symlinks from the pre-#3441 symlink mechanism.

    A prior nyxgpt version's `_ensure_log_symlinks` pointed these names at
    Homebrew's var/log dir. That target lives outside ~/.nyxGPT/logs, which
    is all promtail's container actually bind-mounts, so those symlinks were
    never reachable from inside it and never shipped a single line to Loki
    (#3441) -- nyxgpt-api/nyxgpt-web's own structured logs already land
    directly in ~/.nyxGPT/logs as real files (`api.log`, see
    `nyxgpt.logging.configure_logging`), and Ollama's log now reaches
    ~/.nyxGPT/logs/ollama.log as a real file too, written by
    `follow-ollama-logs.sh` itself. Reconciles on every `nyxgpt ops install`
    so a leftover symlink from before this fix doesn't linger.

    Returns one OpsResult per name considered.
    """
    results: list[OpsResult] = []
    home_logs = Path.home() / ".nyxGPT" / "logs"
    for name in _STALE_LOG_SYMLINK_NAMES:
        dst = home_logs / name
        try:
            if dst.is_symlink():
                dst.unlink()
                results.append(OpsResult(True, f"Removed stale log symlink {name}", str(dst)))
            else:
                results.append(OpsResult(True, f"No stale log symlink for {name}"))
        except Exception as e:
            results.append(OpsResult(False, f"Failed to remove stale log symlink {name}", str(e)))
    return results


# Directories `_vendor_tree` never copies into a dist tarball -- gitignored
# build artifacts the formula regenerates fresh inside the Cellar keg
# (`web/`'s `node_modules`/`.next`/etc.), or VCS metadata that has no
# business in a release tarball.
_WEB_VENDOR_EXCLUDES = frozenset(
    {
        "node_modules",
        ".next",
        "coverage",
        "out",
        "build",
        ".vercel",
        ".turbo",
        ".git",
    }
)


def _vendor_tree(src: Path, dst: Path, *, excludes: frozenset[str] = frozenset()) -> None:
    """Copy the directory tree `src` to `dst`, skipping any dir named in `excludes`."""

    def _ignore(_dir_path: str, names: list[str]) -> set[str]:
        return {n for n in names if n in excludes}

    shutil.copytree(src, dst, ignore=_ignore)


def _create_dist_tarball(tap_dir: Path, name: str, version: str) -> Path:
    """Build a `<name>-<version>.tar.gz` distribution under `tap_dir/dist`.

    Vendors the actual source the formula needs to build a self-contained
    app inside the Cellar keg -- `pyproject.toml` + `src/nyxgpt/` for
    `nyxgpt-api` (the formula creates a Cellar-local venv and `pip install`s
    this tree), or the `web/` source tree, minus its gitignored
    `node_modules`/`.next` build output (the formula runs `npm ci`/`npm run
    build` fresh inside the keg), for `nyxgpt-web`. Either way, the
    installed app no longer depends on the repo checkout or an editable
    `.venv` at runtime (#3406) -- replacing the old placeholder tarball that
    only ever contained a `README.txt`.

    Replaces any existing tarball of the same name/version. Returns the
    path to the created tarball.
    """
    dist_dir = tap_dir / "dist"
    _ensure_dir(dist_dir)
    tar_path = dist_dir / f"{name}-{version}.tar.gz"

    tmp = dist_dir / f".tmp-{name}-{version}"
    if tmp.exists():
        shutil.rmtree(tmp)
    _ensure_dir(tmp)
    root = tmp / f"{name}-{version}"

    if name == "nyxgpt-web":
        _vendor_tree(REPO_ROOT / "web", root, excludes=_WEB_VENDOR_EXCLUDES)
    else:
        _vendor_tree(REPO_ROOT / "src" / "nyxgpt", root / "src" / "nyxgpt")
        shutil.copy2(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
        # config_wizard builds its schema from example.config.ini at import
        # time (#3388), so `import nyxgpt.app` needs the file present. A venv
        # install has no repo root above the package, so ship the template in
        # the tarball; the formula copies it next to the installed package
        # where _resolve_example_config_path() finds it with no env var (#3406).
        shutil.copy2(REPO_ROOT / "example.config.ini", root / "example.config.ini")

    if tar_path.exists():
        tar_path.unlink()

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(root, arcname=f"{name}-{version}")

    shutil.rmtree(tmp, ignore_errors=True)
    return tar_path


def _brew_install_or_reinstall(spec: str, name: str, *, sha256: str, marker_dir: Path) -> str:
    """`brew install`/`reinstall` formula `name`, skipping the work when unchanged.

    Compares the just-built tarball's `sha256` against the one recorded the
    last time this formula was actually installed (`marker_dir/.<name>.sha256`).
    If the formula is already installed and the checksum matches, the source
    hasn't changed since the last install: skip the `brew fetch`/`install`
    round-trip entirely. Otherwise (not installed yet, or the checksum
    changed -- a new/edited source tree), fetch and install/reinstall, then
    record the new checksum. The `fetch --force` refreshes brew's download
    cache first: the tarball URL and version are constant across runs, so
    without it brew would reinstall from a stale cached tarball and fail the
    formula's sha256 check.

    Returns a short human-readable string describing which of the three
    decisions was made ("installed" / "reinstalled (source changed)" /
    "already up to date (skipped)"), for the caller to report.
    """
    installed = (
        _run(["brew", "list", "--versions", name], check=False, expected=True).returncode == 0
    )
    marker = marker_dir / f".{name}.sha256"
    previous_sha256 = marker.read_text(encoding="utf-8").strip() if marker.exists() else None

    if installed and previous_sha256 == sha256:
        return "already up to date (skipped reinstall)"

    _run(["brew", "fetch", "--force", spec], check=False)
    if installed:
        cp = _run(["brew", "reinstall", spec], check=False)
        action, decision = "reinstall", "reinstalled (source changed since last install)"
    else:
        cp = _run(["brew", "install", "--overwrite", spec], check=False)
        action, decision = "install", "installed"

    if cp.returncode != 0:
        # Surface the failure instead of reporting a false success, and do NOT
        # record the checksum: writing the marker on a failed build would make
        # the next run see a matching checksum and skip, so the broken install
        # would never be retried (the bug that let a failed api keg rebuild
        # report "reinstalled" and then stick as a stale wrapper).
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"brew {action} {name} failed: {detail[-800:]}")

    _ensure_dir(marker_dir)
    marker.write_text(sha256, encoding="utf-8")
    return decision


# Where `_docker_build_if_needed` records the sha256 fingerprint of the source
# an image was last built from (`.<image>.sha256`, image name/tag sanitized) --
# the Docker-image analogue of `_tap_repo(tap) / "dist"` for the Homebrew
# markers `_brew_install_or_reinstall` reads/writes.
DOCKER_IMAGE_MARKER_DIR = Path.home() / ".nyxGPT" / "docker-images"


def _hash_paths(paths: list[Path], *, excludes: frozenset[str] = frozenset()) -> str:
    """sha256 fingerprint over the file contents under `paths` (files or directories).

    Walks each directory deterministically (sorted, skipping any dir named in
    `excludes` -- e.g. `_WEB_VENDOR_EXCLUDES`), hashing every file's path
    relative to its base plus its bytes, so the digest changes iff the
    content or layout under `paths` actually changed. Used by
    `_docker_build_if_needed` to detect source changes for the app the
    image is built from -- the Docker-image equivalent of `_sha256_file`
    over `_create_dist_tarball`'s vendored tarball.
    """
    digest = hashlib.sha256()
    for base in sorted(paths, key=str):
        if base.is_file():
            digest.update(f"{base.name}\0".encode())
            digest.update(base.read_bytes())
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(n for n in dirnames if n not in excludes)
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                rel = fpath.relative_to(base)
                digest.update(f"{base.name}/{rel}\0".encode())
                digest.update(fpath.read_bytes())
    return digest.hexdigest()


def _docker_build_if_needed(
    image: str,
    context: Path,
    *,
    fingerprint_paths: list[Path],
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
    marker_dir: Path,
) -> str:
    """`docker build -t image context`, skipping the work when the source hasn't changed.

    Mirrors `_brew_install_or_reinstall` (#3406) for the Docker/terraform
    image build sites (#3414). Compares a sha256 fingerprint of
    `fingerprint_paths` -- the actual app source the image is built from
    (e.g. `src/nyxgpt/` + `pyproject.toml` for `nyxgpt-api`, `web/` for
    `nyxgpt-web`), not the whole build context -- against the one recorded
    the last time `image` was actually built (`marker_dir/.<image>.sha256`,
    name/tag sanitized). If `image` already exists locally and the
    fingerprint matches, the source hasn't changed since the last build:
    skip `docker build` entirely. Otherwise (image missing, or the
    fingerprint changed -- a new/edited source tree), run the build and
    record the new fingerprint.

    Returns a short human-readable string describing which of the three
    decisions was made ("built" / "rebuilt (source changed since last
    build)" / "already up to date (skipped rebuild)"), for the caller to
    report.
    """
    marker_name = re.sub(r"[^A-Za-z0-9_.-]", "_", image)
    marker = marker_dir / f".{marker_name}.sha256"
    previous_fingerprint = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    fingerprint = _hash_paths(fingerprint_paths, excludes=excludes)

    image_exists = (
        _run(["docker", "image", "inspect", image], check=False, expected=True).returncode == 0
    )
    if image_exists and previous_fingerprint == fingerprint:
        return "already up to date (skipped rebuild)"

    cmd = ["docker", "build", "-t", image]
    for arg_name, arg_value in (build_args or {}).items():
        cmd += ["--build-arg", f"{arg_name}={arg_value}"]
    cmd.append(str(context))
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        # As with `_brew_install_or_reinstall`: surface the failure and do NOT
        # record the fingerprint, so a broken build doesn't get skipped (and
        # so stick as a stale image) on the next run.
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"docker build {image} failed: {detail[-800:]}")

    _ensure_dir(marker_dir)
    marker.write_text(fingerprint, encoding="utf-8")
    return "built" if previous_fingerprint is None else "rebuilt (source changed since last build)"


# The app source `nyxgpt-api`'s image is built from -- COPY'd into the
# Dockerfile (pyproject.toml, src/nyxgpt/, example.config.ini) plus the
# entrypoint script it also COPYs -- not the whole build context, so an
# unrelated repo-root change (docs, terraform/, etc.) doesn't force a rebuild.
_API_IMAGE_FINGERPRINT_PATHS = [
    REPO_ROOT / "src" / "nyxgpt",
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "example.config.ini",
    REPO_ROOT / "docker" / "entrypoint.sh",
]


def _install_homebrew_api(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-api` Homebrew formula into `tap`, then (re)start the service.

    Generates a dist tarball vendoring `pyproject.toml` + `src/nyxgpt/`,
    patches the formula template's `sha256` to match it, writes the formula
    into the tap's Formula/ dir, and installs/reinstalls it only if the
    vendored source actually changed since the last install (see
    `_brew_install_or_reinstall`). The formula itself builds a self-contained
    venv inside the Cellar keg from that vendored source -- the installed app
    never depends on the repo checkout or an editable `.venv` (#3406).

    When a rebuild actually happened (`decision` is "installed" or
    "reinstalled ..."), this restarts the service (`brew services restart`)
    instead of just starting it: `brew services start` on an already-running
    service is a no-op, so a code fix landing while the API is still running
    would otherwise leave the *old* in-memory process serving requests
    against the new keg's on-disk source forever -- silently undoing the fix
    (#3472, mirrors the `nyxgpt-web` fix in #3445). When nothing changed, a
    plain `start` is used so an already-up-to-date install doesn't bounce a
    healthy running service. Returns a list of OpsResult; fails early if brew
    isn't installed or the formula template is missing.
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
    # Stamp the template's __VERSION__/__SHA256__ placeholders with the real
    # values for the tarball just generated. The regex substitutions remain as
    # a safety net for any formula copy that still carries concrete (stale)
    # values instead of placeholders.
    content = content.replace("__VERSION__", version)
    content = content.replace("__SHA256__", sha)
    content = re.sub(r'sha256 "[a-f0-9]+"', f'sha256 "{sha}"', content)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)
    content = re.sub(r"nyxgpt-api-[^\"/]+\.tar\.gz", f"nyxgpt-api-{version}.tar.gz", content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-api.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-api formula", str(dst)))

    decision = _brew_install_or_reinstall(
        f"{tap}/nyxgpt-api", "nyxgpt-api", sha256=sha, marker_dir=tap_dir / "dist"
    )
    if decision == "already up to date (skipped reinstall)":
        _run(["brew", "services", "start", "nyxgpt-api"], check=False)
        results.append(OpsResult(True, f"nyxgpt-api: {decision}; requested service start", ""))
    else:
        # A new keg was just installed -- restart, not start, so the running
        # process actually picks it up instead of continuing to serve the
        # old build's code (#3472).
        results.append(OpsResult(True, f"nyxgpt-api: {decision}", ""))
        results.extend(_restart_brew_service("nyxgpt-api"))

    return results


def _install_homebrew_web(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-web` Homebrew formula into `tap`, then (re)start the service.

    Generates a dist tarball vendoring the `web/` source tree (minus
    gitignored build artifacts -- see `_WEB_VENDOR_EXCLUDES`), substitutes
    its `file://` URL and sha256 into the formula template, writes the
    formula into the tap's Formula/ dir, and installs/reinstalls it only if
    the vendored source actually changed since the last install (see
    `_brew_install_or_reinstall`). The formula itself runs `npm ci`/`npm run
    build` inside the Cellar keg from that vendored source -- the installed
    app never depends on the repo checkout (#3406).

    When a rebuild actually happened (`decision` is "installed" or
    "reinstalled ..."), this restarts the service (`brew services restart`)
    instead of just starting it: `brew services start` on an already-running
    service is a no-op, so a rebuild-while-serving would otherwise leave the
    old `next start` process running against the *old* in-memory build
    manifest while the on-disk `.next` chunks are already the new build's --
    every served page then references chunk hashes that no longer exist,
    404ing (#3445). When nothing changed, a plain `start` is used so an
    already-up-to-date install doesn't bounce a healthy running service.

    Returns a list of OpsResult; fails early if brew isn't installed or the
    formula template is missing.
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
    content = content.replace("__VERSION__", version)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-web.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-web formula", str(dst)))

    decision = _brew_install_or_reinstall(
        f"{tap}/nyxgpt-web", "nyxgpt-web", sha256=sha, marker_dir=tap_dir / "dist"
    )
    if decision == "already up to date (skipped reinstall)":
        _run(["brew", "services", "start", "nyxgpt-web"], check=False)
        results.append(OpsResult(True, f"nyxgpt-web: {decision}; requested service start", ""))
    else:
        # A new keg (and new `.next` build output) was just installed --
        # restart, not start, so the running process actually picks it up
        # instead of continuing to serve the old build's chunk manifest.
        results.append(OpsResult(True, f"nyxgpt-web: {decision}", ""))
        results.extend(_restart_brew_service("nyxgpt-web"))

    return results


# Keys in the derived container api-config that must be rewritten from their
# native (localhost) values to container-network values. Everything else --
# models, RAG tuning, secrets, and the browser-facing *_ui_url values -- is
# copied verbatim from ~/.nyxGPT/config.ini (the single source of truth); only
# service-to-service endpoints change inside the container network. Terraform
# gives its containers matching network aliases (cassandra/ollama/...) so these
# same hostnames resolve under `nyxgpt ops install --terraform --local`.
_COMPOSE_CONFIG_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("nyxgpt", "sessions_dir", "/root/.nyxGPT/sessions"),
    ("nyxgpt", "vectorstore_dir", "/root/.nyxGPT/vectorstore"),
    ("logging", "dir", "/root/.nyxGPT/logs"),
    ("ollama", "base_url", "http://ollama:11434"),
    ("rag", "cassandra_hosts", "cassandra"),
    ("tracing", "otlp_endpoint", "http://otel-collector:4318/v1/traces"),
    ("api", "host", "0.0.0.0"),
    # NOTE: auth is intentionally NOT overridden. The `--local` deploy is
    # loopback-only (NYXGPT_BIND_ADDR defaults to 127.0.0.1) and container
    # traffic stays on the private docker network, so it mirrors the native
    # local-first setup -- auth follows the real config. If the user has
    # `[auth] enabled = true` + an api_key natively, that carries over; if it's
    # disabled (the local default), forcing it on would just reject the web's
    # own requests (it has no matching key), which is the 401/500 we hit.
)


def _generate_compose_config() -> list[OpsResult]:
    """Generate the git-ignored `docker/config.docker.ini` from the native
    `~/.nyxGPT/config.ini` (the single source of truth).

    The containerized deploys -- the observability Compose profiles and the
    terraform/kubernetes `--local` core stack -- bind-mount this file as the
    api container's `config.ini`. Rather than maintain a separate hand-edited
    file, derive it from the real local config so the container runs the
    user's actual settings, rewriting only the service-network endpoints that
    must differ inside the container network (`_COMPOSE_CONFIG_OVERRIDES`). The
    browser-facing `*_ui_url` values stay `localhost` -- they're opened from
    the host, not container-internal.

    Regenerated on every `nyxgpt ops install`/`env-sync` so native edits
    propagate. Comments and unlisted keys survive verbatim (the rewrite is
    line-based via `_patch_ini_value`), and the DSN `nyxgpt ops glitchtip-init`
    writes into the native config carries over so error tracking stays wired.

    No-ops (successfully) before `nyxgpt wizard` has created the native config.
    """
    native = Path.home() / ".nyxGPT" / "config.ini"
    if not native.exists():
        return [
            OpsResult(
                True,
                f"Skipped compose config (no {native})",
                "Run `nyxgpt wizard` to create config.ini, then re-run `nyxgpt ops install`.",
            )
        ]
    try:
        text = native.read_text(encoding="utf-8")
        for section, key, value in _COMPOSE_CONFIG_OVERRIDES:
            text = _patch_ini_value(text, section, key, value)
        COMPOSE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        COMPOSE_CONFIG_FILE.write_text(text, encoding="utf-8")
    except OSError as e:
        return [
            OpsResult(
                False,
                f"Failed to generate {COMPOSE_CONFIG_FILE}",
                f"{type(e).__name__}: {e}",
            )
        ]
    return [OpsResult(True, f"Generated {COMPOSE_CONFIG_FILE} from {native}")]


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


def _shared_ollama_models_dir() -> Path:
    """Path native Ollama's `OLLAMA_MODELS` must point at to read the same
    models as Compose/Terraform (#3431).

    Ollama's `OLLAMA_MODELS` env var names a directory holding `blobs/` and
    `manifests/` directly (its default is `~/.ollama/models`, which has that
    layout). The shared `~/.nyxGPT/volumes/ollama` dir is bind-mounted to
    `/root/.ollama` in the Compose/Terraform `ollama` containers, which -- not
    having `OLLAMA_MODELS` overridden themselves -- store models at the
    container-default `/root/.ollama/models`, i.e. host
    `~/.nyxGPT/volumes/ollama/models`. Pointing native `OLLAMA_MODELS` at that
    same subdirectory (rather than its `~/.nyxGPT/volumes/ollama` parent)
    is what actually unifies the two stores.
    """
    d = volume_dir("ollama") / "models"
    _ensure_dir(d)
    return d


def _ollama_migration_state_path(name: str) -> Path:
    """Path to a `~/.nyxGPT/.migration-state/` marker for a one-time Ollama
    unification step (#3431) -- mirrors `_migration_marker_path`'s pattern so
    later `nyxgpt ops install` runs skip work already done rather than
    re-touching it (merged models, an already-applied env restart, ...).
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / name


def _migrate_native_ollama_models(dest_models_dir: Path) -> list[OpsResult]:
    """One-time merge of native Ollama's own store (`~/.ollama/models`) into
    the shared `dest_models_dir`, so pointing native Ollama at the shared
    store doesn't silently orphan models already pulled natively before this
    unification (#3431).

    Additive merge, not overwrite: blobs are content-addressed by sha256
    digest (so an existing blob at the destination is always identical to
    the source one) and manifests take the destination's copy as
    authoritative on conflict -- the shared store is what Compose/Terraform
    already use, so it wins. Idempotent via a marker file, mirroring
    `migrate_legacy_volumes`. Merges via hardlink (falling back to a real
    copy if source and dest are on different filesystems), since blobs can
    be multi-GB and both paths are normally under the same `$HOME`.
    """
    marker = _ollama_migration_state_path("ollama-native-models.migrated")
    if marker.exists():
        return [OpsResult(True, "Native Ollama models: already reconciled (nothing to migrate)")]

    src_dir = Path.home() / ".ollama" / "models"
    if not src_dir.exists():
        marker.touch()
        return [OpsResult(True, "No native Ollama model store found (nothing to migrate)")]

    copied = 0
    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue
        dst_file = dest_models_dir / src_file.relative_to(src_dir)
        if dst_file.exists():
            continue
        _ensure_dir(dst_file.parent)
        try:
            # Source and dest are normally both under $HOME on the same
            # filesystem, so a hardlink is instant and uses no extra disk
            # for what can be multi-GB blobs -- falls back to a real copy
            # for the (e.g. custom OLLAMA volume elsewhere) cross-device case.
            os.link(src_file, dst_file)
        except OSError:
            _copy_file(src_file, dst_file)
        copied += 1

    marker.touch()
    if copied:
        return [
            OpsResult(
                True,
                f"Merged {copied} native Ollama model file(s) into the shared store",
                f"Source: {src_dir}\nDest: {dest_models_dir}\n"
                f"The old {src_dir} files were left in place -- safe to remove by hand "
                "once you've confirmed the shared store has everything you need.",
            )
        ]
    return [OpsResult(True, "Native Ollama model store already matched the shared store")]


def _set_native_ollama_models_env(models_dir: Path) -> OpsResult:
    """Point native Ollama at `models_dir` via `launchctl setenv OLLAMA_MODELS`
    -- never a symlink (owner constraint, #3431) -- so `brew services
    start`/`restart ollama` picks it up in the user's launchd GUI session.

    `launchctl setenv` only applies to the current login session, which is
    why `nyxgpt ops install` also installs a RunAtLoad LaunchAgent
    (`_install_ollama_env_launchagent`) that reapplies it at every login.
    """
    cp = _run(["launchctl", "setenv", "OLLAMA_MODELS", str(models_dir)], check=False)
    if cp.returncode == 0:
        return OpsResult(True, f"Set OLLAMA_MODELS={models_dir} for native Ollama")
    details = (cp.stdout or "").strip() + (
        "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
    )
    return OpsResult(False, "Failed to set OLLAMA_MODELS via launchctl setenv", details.strip())


def _ensure_ollama_service() -> list[OpsResult]:
    """Ensure the native Ollama Homebrew service is installed, running, and
    pointed at the shared model store.

    Reconciles to the intended state like `_ensure_cassandra_container`:
    already started (and already pointed at the shared store) -> no-op;
    installed but stopped -> `brew services start`; formula absent -> `brew
    install ollama` first, then start. Without this step, `ops install` only
    set up the Ollama *logs* LaunchAgent and never started Ollama itself, so
    chat/embeddings stayed down after an `ops down` until a manual `ops
    restart ollama`.

    Also unifies the native model store with Compose/Terraform's shared one
    (#3431): merges any models already pulled natively into
    `~/.nyxGPT/volumes/ollama/models`, then points native Ollama at it via
    `launchctl setenv OLLAMA_MODELS` (see `_set_native_ollama_models_env`).
    An already-running service doesn't pick up a `launchctl setenv` change on
    its own, so the first time this runs against a live service it issues a
    one-time `brew services restart` -- gated by a marker so later runs stay
    a no-op, same as every other reconciler here.
    """
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found; cannot ensure ollama service", "")]

    results: list[OpsResult] = []
    models_dir = _shared_ollama_models_dir()
    results.extend(_migrate_native_ollama_models(models_dir))

    env_marker = _ollama_migration_state_path("ollama-native-env.configured")
    env_already_applied = env_marker.exists()
    env_result = _set_native_ollama_models_env(models_dir)
    results.append(env_result)
    if env_result.ok:
        env_marker.touch()

    state = _brew_services_snapshot().get("ollama")

    if state == "started":
        if env_already_applied:
            results.append(OpsResult(True, "Ollama brew service already running"))
            return results
        cp = _run(["brew", "services", "restart", "ollama"], check=False)
        if cp.returncode == 0:
            results.append(
                OpsResult(True, "Restarted brew service: ollama (to pick up shared OLLAMA_MODELS)")
            )
        else:
            details = (cp.stdout or "").strip() + (
                "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
            )
            results.append(
                OpsResult(False, "Failed to restart brew service: ollama", details.strip())
            )
        return results

    if state is None:
        cp = _run(["brew", "install", "ollama"], check=False)
        if cp.returncode != 0:
            details = (cp.stdout or "").strip() + (
                "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
            )
            results.append(OpsResult(False, "Failed to brew install ollama", details.strip()))
            return results
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
        except Exception as e:
            logger.warning(
                "Failed to resolve node package %r, assuming missing: %s",
                pkg,
                e,
                extra={"component": "ops"},
            )
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
    cp = _run(["docker", "volume", "inspect", name], check=False, expected=True)
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

    rm = _run(["docker", "volume", "rm", volume_name], check=False, expected=True)
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
        except Exception as e:
            logger.warning(
                "Interactive API key prompt failed, auto-generating one instead: %s",
                e,
                extra={"component": "ops"},
            )
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

# Matches terraform/variables.tf's `api_image_tag`/`web_image_tag` defaults
# ("local") that terraform/main.tf's `docker_image.api`/`.web` resources
# interpolate into their image `name` -- the tag `_build_terraform_docker_images`
# pre-builds below so `terraform apply`'s own `build {}` block hits Docker's
# layer cache instead of doing real work when the source hasn't changed.
TF_API_IMAGE = "nyxgpt-api:local"
TF_WEB_IMAGE = "nyxgpt-web:local"
# Matches terraform/variables.tf's `web_api_base_url` default.
TF_WEB_API_BASE_URL_DEFAULT = "http://localhost:8000"


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


def _build_terraform_docker_images() -> list[OpsResult]:
    """Build the `nyxgpt-api`/`nyxgpt-web` images the Terraform `--local` deploy
    consumes, skipping each build the app source hasn't changed since (#3414).

    Runs before `terraform init/plan/apply` so `docker_image.api`/`.web` in
    terraform/main.tf (tag `local`, matching `TF_API_IMAGE`/`TF_WEB_IMAGE`
    here) already exist locally in the target state: unchanged source means
    `_docker_build_if_needed` skips the rebuild entirely (reported below,
    mirroring the Homebrew `_install_homebrew_api`/`_web` decision output);
    changed source means it rebuilds now, so terraform's own `build {}` block
    then just hits Docker's layer cache instead of doing the real work again.
    """
    if _which("docker") is None:
        return [OpsResult(False, "docker not found on PATH -- cannot build nyxgpt-api/nyxgpt-web")]

    results: list[OpsResult] = []
    try:
        decision = _docker_build_if_needed(
            TF_API_IMAGE,
            REPO_ROOT,
            fingerprint_paths=_API_IMAGE_FINGERPRINT_PATHS,
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
        results.append(OpsResult(True, f"{TF_API_IMAGE}: {decision}"))
    except RuntimeError as e:
        results.append(OpsResult(False, f"docker build {TF_API_IMAGE} failed", str(e)))

    try:
        decision = _docker_build_if_needed(
            TF_WEB_IMAGE,
            REPO_ROOT / "web",
            fingerprint_paths=[REPO_ROOT / "web"],
            excludes=_WEB_VENDOR_EXCLUDES,
            build_args={"NEXT_PUBLIC_API_BASE_URL": TF_WEB_API_BASE_URL_DEFAULT},
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
        results.append(OpsResult(True, f"{TF_WEB_IMAGE}: {decision}"))
    except RuntimeError as e:
        results.append(OpsResult(False, f"docker build {TF_WEB_IMAGE} failed", str(e)))

    return results


def terraform_stack_state() -> dict[str, str]:
    """{component: docker state} for the Terraform-managed containers (used by status/doctor)."""
    return {
        component: _docker_container_state(name) for component, name in TERRAFORM_CONTAINERS.items()
    }


def _terraform_state_has_resources() -> bool:
    """True if terraform.tfstate records any managed resources.

    ``terraform destroy`` (what ``nyxgpt ops down --terraform`` runs) always
    leaves ``terraform.tfstate`` in place with an empty ``resources`` list --
    that's a clean post-destroy state, not stale state. Only a tfstate that
    still records resources (with no matching containers running) is stale.
    """
    tfstate = TERRAFORM_DIR / "terraform.tfstate"
    try:
        data = json.loads(tfstate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(data.get("resources"))


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
        _record_ops_action("install", "terraform", "refused", collision.message)
        return [collision]

    logger.info(
        "ops: install --terraform --local starting",
        extra={"component": "ops", "action": "install-terraform"},
    )
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        (
            "clear intentional-stop markers",
            lambda: _clear_intentional_stops(["api", "web", "ollama", "cassandra"]),
        ),
        # Must run before terraform apply: main.tf no longer declares the
        # `nyxgpt_tf_*` docker_volume resources (#3346), so apply would
        # otherwise destroy them -- along with any not-yet-migrated data --
        # as part of reconciling state to the new host-bind-mount config.
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("terraform binary", _ensure_terraform_binary),
        ("terraform tfvars", lambda: _ensure_terraform_tfvars(api_key)),
        # Must run before apply: terraform/main.tf bind-mounts
        # docker/config.docker.ini into the api container (same pattern as
        # docker-compose.yml), so the derived file has to exist first or
        # Docker creates an empty directory in its place.
        ("compose config (derive from native)", _generate_compose_config),
        # Must run before apply: pre-builds (or skips, if source is unchanged
        # since the last build -- #3414) the images `docker_image.api`/`.web`
        # reference by tag, so terraform's own build hits Docker's cache
        # instead of doing the work again.
        ("docker images (source-change detection)", _build_terraform_docker_images),
        ("terraform init/plan/apply", _terraform_init_plan_apply),
        # Must run before the observability stack starts: Grafana's Compose
        # bind-mount auto-creates a missing ~/.nyxGPT/secrets root-owned on
        # Linux (#3432), which then blocks the token write below.
        ("glitchtip secrets dir", _ensure_glitchtip_secrets_dir),
        # After apply (network + core containers exist): bring the observability
        # stack up on the terraform network and auto-provision GlitchTip, so the
        # terraform deploy has the same full SRE view as the native install.
        ("observability stack (terraform network)", _start_observability_stack_terraform),
        ("glitchtip auto-provisioning", _provision_glitchtip),
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

    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "terraform", result, message)
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
    """Tear down the Terraform-managed stack and return structured results.

    Brings the observability Compose stack down FIRST: `install --terraform
    --local` starts it on the `nyxgpt-terraform` network, and `terraform
    destroy` can't remove that network while those containers are still
    attached (it times out on the network delete). Then runs `terraform
    destroy` for the core stack.
    """
    if _which("terraform") is None:
        results = [OpsResult(False, "terraform not found on PATH -- nothing to destroy")]
    else:
        results = _stop_observability_stack_terraform()
        cp = _run(
            ["terraform", f"-chdir={TERRAFORM_DIR}", "destroy", "-input=false", "-auto-approve"],
            check=False,
        )
        if cp.returncode == 0:
            results.append(OpsResult(True, "terraform destroy", _cp_details(cp)))
        else:
            results.append(OpsResult(False, "terraform destroy failed", _cp_details(cp)))

    # The `--terraform --local` deploy builds the nyxgpt-api/web images via
    # `docker build`, whose BuildKit layer cache balloons across repeated
    # deploys (17GB+ observed). Reclaim it on teardown -- "down" means we're
    # done, so the cache has served its purpose and the next deploy rebuilds it
    # as needed. NOTE: Docker's build cache is host-global, not per-project, so
    # this also clears cache for any other local Docker builds; that's an
    # acceptable trade on a dev/ops box and is what keeps disk from creeping up.
    # Best-effort: a prune failure never fails the teardown.
    if _which("docker") is not None:
        cp = _run(["docker", "builder", "prune", "-f"], check=False)
        reclaimed = next(
            (ln.strip() for ln in (cp.stdout or "").splitlines() if "reclaimed" in ln.lower()),
            "",
        )
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True, "docker build cache pruned" + (f" -- {reclaimed}" if reclaimed else "")
                )
            )
        else:
            results.append(OpsResult(True, f"docker build cache prune skipped ({_cp_details(cp)})"))

    result, message = _ops_action_outcome(results)
    _record_ops_action("down", "terraform", result, message)
    return results


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
    cp = _run(["kubectl", "config", "current-context"], check=False, expected=True)
    return (cp.stdout or "").strip()


def _build_and_load_k8s_image(
    image: str = K8S_IMAGE,
    *,
    context: Path = REPO_ROOT,
    fingerprint_paths: list[Path] | None = None,
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
) -> list[OpsResult]:
    """Build `image` from `context` and load it into the current cluster's image cache.

    Docker Desktop's built-in cluster shares the host's image cache, so a
    build alone is enough there. kind/minikube each need an explicit
    load step; an unrecognized cluster type is treated the same way the
    documented manual flow would be -- skip the load and tell the operator
    to do it themselves if their cluster doesn't share the host cache.

    `image` defaults to the mutable `nyxgpt-api:local` tag `nyxgpt ops
    install --kubernetes` uses; `nyxgpt ops deploy --kubernetes` (via
    `canary.deploy`) passes a versioned tag instead (see
    `build_and_load_k8s_image` / #3409). `context`/`fingerprint_paths`/
    `excludes`/`build_args` default to the `nyxgpt-api` image's build (repo
    root, `_API_IMAGE_FINGERPRINT_PATHS`); `canary.deploy` overrides them for
    the `web` component to build `web/` with `_WEB_VENDOR_EXCLUDES` and the
    `NEXT_PUBLIC_API_BASE_URL` build arg (#3419), mirroring
    `_build_terraform_docker_images`'s web build. Either way the build
    itself is gated by `_docker_build_if_needed` (#3414): a versioned tag is
    always missing locally the first time (so it always builds), while the
    repeated `:local` tag skips the rebuild once the source stops changing
    between installs, mirroring the Homebrew reinstall-if-needed behavior
    from #3406.
    """
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found on PATH -- cannot build the {image} image")]
    try:
        decision = _docker_build_if_needed(
            image,
            context,
            fingerprint_paths=fingerprint_paths or _API_IMAGE_FINGERPRINT_PATHS,
            excludes=excludes,
            build_args=build_args,
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
    except RuntimeError as e:
        return [OpsResult(False, "docker build failed", str(e))]
    results = [OpsResult(True, f"{image}: {decision}")]

    cluster_context = _kubectl_context()
    if "docker-desktop" in cluster_context:
        results.append(
            OpsResult(True, "Docker Desktop cluster shares the host image cache -- skipped load")
        )
        return results
    if cluster_context.startswith("kind-") and _which("kind") is not None:
        cluster_name = cluster_context.removeprefix("kind-")
        cp = _run(["kind", "load", "docker-image", image, "--name", cluster_name], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "kind load docker-image failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {image} into kind cluster {cluster_name}"))
        return results
    if _which("minikube") is not None:
        cp = _run(["minikube", "image", "load", image], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "minikube image load failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {image} into minikube"))
        return results
    results.append(
        OpsResult(
            True,
            f"Unrecognized cluster context {cluster_context!r} -- skipped image load",
            "If this cluster doesn't share the host's image cache, load "
            f"{image} into it manually before the Pods can start.",
        )
    )
    return results


def build_and_load_k8s_image(
    image: str,
    *,
    context: Path | None = None,
    fingerprint_paths: list[Path] | None = None,
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
) -> list[OpsResult]:
    """Build and load a specific, caller-chosen image tag (used by `nyxgpt ops deploy --kubernetes`).

    Public wrapper around `_build_and_load_k8s_image` for cross-module use
    (`canary.deploy`) -- the mutable-`:local`-tag install flow keeps calling
    the private function directly with its default. `canary.deploy` calls
    this with no extra kwargs for the `api` component (forwarding only
    `image`, identical to the original single-argument call) and with
    `web/`'s context/fingerprint/build-args for the `web` component (#3419)
    -- only kwargs the caller actually supplied are forwarded, so the `api`
    call path is unchanged.
    """
    kwargs: dict[str, Any] = {}
    if context is not None:
        kwargs["context"] = context
    if fingerprint_paths is not None:
        kwargs["fingerprint_paths"] = fingerprint_paths
    if excludes:
        kwargs["excludes"] = excludes
    if build_args is not None:
        kwargs["build_args"] = build_args
    return _build_and_load_k8s_image(image, **kwargs)


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
    """Apply `k8s/`'s kustomization (namespace, RBAC, ConfigMap, Secret, Deployments, Service)."""
    cp = _run(["kubectl", "apply", "-k", str(K8S_DIR)], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "kubectl apply -k k8s/ failed", _cp_details(cp))]
    return [OpsResult(True, "kubectl apply -k k8s/", _cp_details(cp))]


def _k8s_stack_health() -> list[OpsResult]:
    """Snapshot of Pod/Service health in the `nyxgpt` namespace right after apply.

    A one-shot snapshot, not a wait-until-ready loop -- Pods may still be
    starting when this runs; re-check with `nyxgpt ops status`. No HPA check
    here -- the stable/canary Deployments deliberately have none (autoscaling
    would fight canary.py's replica-count-based traffic split; see #3409).
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

    for svc in ("nyxgpt-api", "nyxgpt-web"):
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "get", "svc", svc, "--no-headers"],
            check=False,
            expected=True,
        )
        results.append(
            OpsResult(
                cp.returncode == 0,
                f"Service {svc}" + (" found" if cp.returncode == 0 else " not found"),
            )
        )
    return results


def _build_and_load_k8s_web_image() -> list[OpsResult]:
    """Build/load `nyxgpt-web:local`, the web canary pair's image (#3419).

    Mirrors `_build_terraform_docker_images`'s web build: context is `web/`
    (not the repo root), fingerprinted on `web/` itself (excluding
    `_WEB_VENDOR_EXCLUDES` build artifacts) rather than the whole build
    context, with the same `NEXT_PUBLIC_API_BASE_URL` build arg default
    Terraform's local deploy uses -- this is inlined into the browser bundle
    at build time, and like Terraform's containers, a k8s Pod is only
    reachable from the operator's own workstation (via `kubectl
    port-forward`), so the same host-local default applies.
    """
    return _build_and_load_k8s_image(
        TF_WEB_IMAGE,
        context=REPO_ROOT / "web",
        fingerprint_paths=[REPO_ROOT / "web"],
        excludes=_WEB_VENDOR_EXCLUDES,
        build_args={"NEXT_PUBLIC_API_BASE_URL": TF_WEB_API_BASE_URL_DEFAULT},
    )


def _install_kubernetes_steps(api_key: str | None) -> list[OpsResult]:
    """Run the Kubernetes bring-up steps and return structured results (no printing).

    Prereq checks (cluster reachable, kubectl present), builds and loads
    `nyxgpt-api:local` and `nyxgpt-web:local`, bootstraps k8s/secret.yaml
    (prompting for the API key, never committing it), applies the
    kustomization (which now includes the web stable/canary pair -- #3419),
    and snapshots Pod/Service health. Stops at the first failing step, same
    rationale as `_install_terraform_steps`.

    Shared by the `nyxgpt ops install --kubernetes --local` CLI entrypoint
    (`_install_kubernetes`) and `install_kubernetes_local`, the SRE/admin
    dashboard API's structured equivalent.
    """
    collision = _refuse_port_collision(["api", "web"])
    if collision is not None:
        _record_ops_action("install", "kubernetes", "refused", collision.message)
        return [collision]

    logger.info(
        "ops: install --kubernetes --local starting",
        extra={"component": "ops", "action": "install-kubernetes"},
    )
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("clear intentional-stop markers", lambda: _clear_intentional_stops(["api", "web"])),
        ("cluster prerequisites", _ensure_kubectl_and_cluster),
        ("build/load api image", _build_and_load_k8s_image),
        ("build/load web image", _build_and_load_k8s_web_image),
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

    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "kubernetes", result, message)
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
        results = [OpsResult(False, "kubectl not found on PATH -- nothing to tear down")]
    else:
        cp = _run(["kubectl", "delete", "-k", str(K8S_DIR), "--ignore-not-found"], check=False)
        if cp.returncode == 0:
            results = [
                OpsResult(
                    True, "kubectl delete -k k8s/ (namespace and all resources)", _cp_details(cp)
                )
            ]
        else:
            results = [OpsResult(False, "kubectl delete -k k8s/ failed", _cp_details(cp))]

    result, message = _ops_action_outcome(results)
    _record_ops_action("down", "kubernetes", result, message)
    return results


def down_kubernetes() -> list[OpsResult]:
    """Structured (non-printing) `kubectl delete -k k8s/`, for the SRE/admin dashboard API."""
    return _down_kubernetes_steps()


def _down_kubernetes(_args) -> int:
    """`nyxgpt ops down --kubernetes`: remove the `nyxgpt` namespace's Kubernetes resources."""
    results = _down_kubernetes_steps()
    ok = _emit_results("down --kubernetes", results)
    return 0 if ok else 2


def infra_status() -> dict[str, Any]:
    """Honest, status-only deployment status for the Infrastructure admin page (see #3410).

    Reports which mode is actually running (native/compose/terraform/
    kubernetes/none) and each mode's component state, mirroring the
    sections `nyxgpt ops status` prints (see `status`) as JSON. Distinguishes
    a probe that couldn't be run at all from this vantage point --
    `probe_available: False` -- from a probe that ran and found nothing, so
    a caller (the web UI) can render "cannot determine from this deployment
    mode" instead of a false "NOT DEPLOYED" when e.g. the API process has no
    docker socket. This page has no install/destroy actions: those are
    `nyxgpt ops` CLI-only (see docs/terraform.md / docs/kubernetes.md). Also
    reports `serving`, which instance/service is currently handling traffic
    (see `_serving_status`) -- traffic *control* stays on the canary page.

    Kubernetes refines this further (#3468): `configured` reports whether a
    kubeconfig current-context exists at all (kubectl missing counts as not
    configured, since there's then no context to read). No configured
    cluster means there was never anything to be unreachable -- that's a
    confidently-determined NOT DEPLOYED, not CANNOT DETERMINE. The latter is
    reserved for a *configured* context the probe couldn't reach (timeout,
    connection refused to a cluster that's meant to exist, auth failure),
    preserving #3410's original false-NOT-DEPLOYED protection for that case.
    """
    mode_info = detect_deployment_mode()

    docker_available = _which("docker") is not None
    tf_state = terraform_stack_state()
    terraform = {
        "probe_available": docker_available,
        "deployed": docker_available and any(state != "absent" for state in tf_state.values()),
        "containers": tf_state,
    }

    kubectl_available = _which("kubectl") is not None
    kubernetes_configured = kubectl_available and bool(_kubectl_context())
    pods: list[str] = []
    # No kubeconfig/current-context means no cluster was ever configured here --
    # that's a confidently-determined NOT DEPLOYED (#3468), not the CANNOT
    # DETERMINE state reserved for a *configured* cluster the probe couldn't
    # reach (kubectl missing entirely is folded into "not configured" too,
    # since there's no context to read either way).
    kubernetes_probe_available = not kubernetes_configured
    if kubernetes_configured:
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "get", "pods", "--no-headers"],
            check=False,
            expected=True,
        )
        kubernetes_probe_available = cp.returncode == 0
        if kubernetes_probe_available:
            pods = [line for line in (cp.stdout or "").splitlines() if line.strip()]
    kubernetes = {
        "available": kubectl_available,
        "configured": kubernetes_configured,
        "probe_available": kubernetes_probe_available,
        "deployed": bool(pods),
        "namespace": K8S_NAMESPACE,
        "pods": pods,
    }

    native_running = any(state in ("started", "running") for state in mode_info.native.values())
    if terraform["deployed"]:
        running_mode = "terraform"
    elif kubernetes["deployed"]:
        running_mode = "kubernetes"
    elif mode_info.compose:
        running_mode = "compose"
    elif native_running:
        running_mode = "native"
    else:
        running_mode = "none"

    return {
        "mode": running_mode,
        "native": mode_info.native,
        "compose": mode_info.compose,
        "conflicts": sorted(mode_info.conflicts),
        "terraform": terraform,
        "kubernetes": kubernetes,
        "serving": _serving_status(running_mode),
    }


def _serving_status(running_mode: str) -> dict[str, Any]:
    """Which instance/service is currently serving traffic (#3410's third duty for this page).

    Traffic splitting only exists in Kubernetes mode -- native/Compose/
    Terraform each run exactly one instance of every component, so it
    serves 100% of traffic by construction. Only Kubernetes mode delegates
    to `canary.status()` for the stable/canary weight and per-track health;
    everywhere else this reports the single-instance fact directly rather
    than duplicating canary's Kubernetes-only probing. Traffic *control*
    stays on the canary page (#3409) -- this only reports the current split.

    Reports every canary-capable component (api, web -- see #3419) under
    `components`, plus the `api` fields spread at the top level unchanged
    for backward compatibility with existing callers/tests that only knew
    about a single component.
    """
    if running_mode != "kubernetes":
        return {
            "supported": False,
            "message": (
                "Single instance serving 100% of traffic -- traffic splitting is a "
                "Kubernetes-mode feature (see the Canary page)."
            ),
        }

    from nyxgpt import canary as canary_module

    components: dict[str, Any] = {}
    for key in ("api", "web"):
        component_status = canary_module.status(component=key)
        components[key] = {
            "active": component_status["active"],
            "weight_percent": component_status["weight_percent"],
            "stable": component_status["stable"],
            "canary": component_status["canary"],
        }

    return {
        "supported": True,
        "components": components,
        **components["api"],
    }


def _install_config() -> list[OpsResult]:
    """Ensure ``~/.nyxGPT/config.ini`` exists, running the setup wizard if missing.

    First-run experience: on a fresh machine `nyxgpt ops install` has no
    config to install services against, so this step launches the interactive
    wizard (the same one behind `nyxgpt wizard`) to create it before any other
    step runs. When stdin is not a TTY (CI, scripted installs) the wizard
    cannot prompt, so the step fails with instructions instead of hanging.
    """
    dst = Path.home() / ".nyxGPT" / "config.ini"
    if dst.exists():
        return [OpsResult(True, "Config already exists", str(dst))]

    if not sys.stdin.isatty():
        return [
            OpsResult(
                False,
                "No config.ini found and stdin is not a TTY -- run `nyxgpt wizard` first",
                str(dst),
            )
        ]

    from nyxgpt.wizard import run_wizard

    print("\nNo config.ini found. Launching setup wizard...\n")
    if run_wizard(output_path=dst) != 0:
        return [OpsResult(False, "Wizard did not complete", str(dst))]
    return [OpsResult(True, "Created config.ini via wizard", str(dst))]


def install(args) -> int:
    """CLI entrypoint for `nyxgpt ops install`.

    Reconciles the local machine to the intended native-mode topology (see
    docs/ops.md): first ensuring ~/.nyxGPT/config.ini exists (launching the
    interactive setup wizard on a fresh machine -- see `_install_config`),
    then migrating any pre-#3346 named-volume data into
    ~/.nyxGPT/volumes/ (see `migrate_legacy_volumes`), then stopping any
    phantom Docker Compose app-tier containers leaked from an earlier run or
    a raw `docker compose up`, then ensuring the
    local Cassandra container plus every other install step (scripts, web deps,
    MCP deps, Cassandra LaunchAgent, Ollama logs LaunchAgent, Ollama env
    LaunchAgent, Homebrew formulas, the native Ollama service (including
    pointing it at the shared model store -- see `_ensure_ollama_service`,
    #3431), log symlinks, env sync from config.ini, the observability stack)
    -- printing an OK/FAIL line per result. A failure in one step doesn't
    stop the rest from running.

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
        (
            "clear intentional-stop markers",
            lambda: _clear_intentional_stops(["api", "web", "ollama", "cassandra"]),
        ),
        ("config", _install_config),
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("phantom compose reconciliation", _reconcile_phantom_compose_app_containers),
        ("scripts", _install_scripts),
        ("web deps", _ensure_web_deps),
        ("mcp deps", _ensure_mcp_deps),
        ("cassandra container", _ensure_cassandra_container),
        ("cassandra launchagent", _install_cassandra_launchagent),
        ("ollama logs launchagent", _install_ollama_launchagent),
        ("ollama env launchagent", _install_ollama_env_launchagent),
        ("homebrew api", _install_homebrew_api),
        ("homebrew web", _install_homebrew_web),
        ("ollama service", _ensure_ollama_service),
        ("stale log symlink cleanup", _cleanup_stale_log_symlinks),
        ("env sync", sync_env_from_config),
        ("compose config (derive from native)", _generate_compose_config),
        ("compose file path", _persist_compose_file_path),
    ]
    if not getattr(args, "skip_observability", False):
        # Must run before the observability stack starts: Grafana's Compose
        # bind-mount auto-creates a missing ~/.nyxGPT/secrets root-owned on
        # Linux (#3432), which then blocks the token write below.
        steps.append(("glitchtip secrets dir", _ensure_glitchtip_secrets_dir))
        steps.append(("slack webhook secret", _sync_grafana_slack_webhook_secret))
        steps.append(("observability stack", _reconcile_grafana_provisioning))
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
    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "all", result, message)

    return 0 if ok else 2


def status(_args) -> int:
    """CLI entrypoint for `nyxgpt ops status`.

    Prints the detected deployment mode (native vs. Compose per component),
    a native/Compose port-conflict warning if both are live, Homebrew
    service states, the Cassandra log-follower LaunchAgent's load state,
    whether the ops-managed Cassandra Docker container is running, and (in
    Kubernetes mode, when pods are present) each canary-capable component's
    stable/canary rollout state via `_serving_status` (see #3419).

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
        cp = _run(["brew", "services", "list"], check=False, expected=True)
        print("\nHomebrew services:\n" + (cp.stdout or "").strip())
    else:
        print("\nHomebrew services: brew not found")

    label = "com.nyxgpt.cassandra-logs"
    try:
        cp = _run(["launchctl", "list"], check=False, expected=True)
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
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "get", "pods", "--no-headers"],
            check=False,
            expected=True,
        )
        pod_lines = [line for line in (cp.stdout or "").splitlines() if line.strip()]
        if cp.returncode == 0 and pod_lines:
            print(
                f"\nKubernetes ({K8S_NAMESPACE} namespace, nyxgpt ops down --kubernetes to "
                f"tear down): {len(pod_lines)} pod(s)"
            )
            for line in pod_lines:
                print(f"  {line}")

            serving = _serving_status("kubernetes")
            if serving["supported"]:
                print("\nCanary (per component -- see the Canary page in the web admin):")
                for component, c in serving["components"].items():
                    rollout = f"active -- {c['weight_percent']}%" if c["active"] else "idle"
                    print(
                        f"  {component}: rollout {rollout} | "
                        f"stable={c['stable']['state']} ({c['stable']['version'] or 'n/a'}) | "
                        f"canary={c['canary']['state']} ({c['canary']['version'] or 'n/a'})"
                    )

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


def _promtail_container_id() -> str | None:
    """Return the running promtail container's ID, or None if it isn't up."""
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "ps", "-q", "promtail"],
        check=False,
        expected=True,
    )
    container_id = (cp.stdout or "").strip().splitlines()[0] if cp.stdout else ""
    return container_id or None


def _promtail_native_mount_missing(container_id: str) -> bool:
    """Return True if the running promtail container has no native-logs bind mount.

    Inspects the actual container's mounts via `docker inspect` rather than
    grepping docker-compose.yml's text for the mount marker -- a promtail
    container created before a compose-file edit (or otherwise missing the
    bind for any reason) has a stale mount list that still passes a text-only
    check, even though the running container can't see native-mode logs.
    """
    cp = _run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", container_id],
        check=False,
        expected=True,
    )
    if cp.returncode != 0:
        return True
    try:
        mounts = json.loads(cp.stdout or "[]")
    except Exception as e:
        logger.warning(
            "Failed to parse `docker inspect` mounts for %s, assuming misconfigured: %s",
            container_id,
            e,
            extra={"component": "ops"},
        )
        return True
    return not any(m.get("Destination") == PROMTAIL_NATIVE_LOG_MOUNT_MARKER for m in mounts)


def _log_aggregation_wiring_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3277 failure mode: native-mode logs never reaching Loki.

    promtail always runs as a Compose container regardless of whether the
    core app is deployed native or Compose (see `OBSERVABILITY_PROFILES`).
    In native mode, api/self-heal/ops write logs to the host `~/.nyxGPT/logs`
    directly -- a plain directory, not the `nyxgpt_data` Docker-managed
    volume promtail otherwise mounts for Compose-mode logs. If the running
    promtail container ever loses its host bind mount for that directory,
    native logs silently stop reaching Loki -- Grafana just shows nothing
    rather than erroring, so this needs an explicit check rather than
    relying on someone noticing an empty dashboard.

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
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping promtail wiring check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_log_aggregation_enabled(parser):
        return None

    if _compose_stack_snapshot().get("promtail") != "running":
        return None

    native_log_dir = Path.home() / ".nyxGPT" / "logs"
    if not native_log_dir.exists() or not any(native_log_dir.glob("*.log*")):
        return None

    container_id = _promtail_container_id()
    if container_id is None or not _promtail_native_mount_missing(container_id):
        return None

    return (
        f"Log aggregation is enabled and native-mode logs exist under {native_log_dir}, "
        "but the running promtail container has no bind mount for them -- "
        "native logs are not reaching Loki. See docs/docker-compose.md#log-aggregation."
    )


def _tracing_wiring_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3350 failure mode: native-mode spans never reaching the collector.

    The api's OTLPSpanExporter is a fire-and-forget HTTP client: when nothing
    listens on `[tracing] otlp_endpoint`, it silently drops every span after
    retrying, rather than failing the request or raising anywhere visible.
    That leaves the Distributed Tracing panel reporting "active" (it only
    checks that `init_tracing` ran, not that the collector is reachable)
    while Jaeger's store stays permanently empty. This does a real TCP
    connect (via `tracing.otlp_endpoint_reachable`) to the endpoint's
    host/port so `doctor` catches the gap, e.g. the otel-collector Compose
    service missing its host `ports:` mapping in native mode.

    Only reports an issue when tracing is actually enabled. Returns None
    otherwise (nothing to check yet, or the collector is reachable).
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping tracing wiring check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_tracing_enabled(parser):
        return None

    tracing_config = get_tracing_config(parser)
    endpoint = tracing_config["otlp_endpoint"]
    if tracing.otlp_endpoint_reachable(endpoint):
        return None

    return (
        f"Tracing is enabled ([tracing] otlp_endpoint={endpoint}) but nothing is "
        "listening there -- spans are being silently dropped and Jaeger will stay "
        "empty. Confirm the otel-collector Compose service (tracing profile) is "
        "running and publishes that port to the host (nyxgpt ops observability). "
        "See docs/docker-compose.md#distributed-tracing."
    )


def _tracing_packages_doctor_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3484 failure mode: a venv predating the #3430 OTel backbone
    (or missing a package `pip uninstall`'d since) is missing an
    `opentelemetry-instrumentation-*`/exporter package.

    `nyxgpt.tracing` no longer crashes on import when a package is missing
    (that's the #3484 fix), but if tracing is enabled in config the operator
    is silently running degraded -- missing instrumentors are skipped, or
    tracing is disabled outright if the exporter itself is missing -- until
    they reinstall. Only reports an issue when tracing is actually enabled,
    same gating as `_tracing_wiring_issue`.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping tracing packages check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_tracing_enabled(parser):
        return None

    missing = tracing.missing_tracing_packages()
    if not missing:
        return None

    return (
        "Tracing is enabled but these OTel packages are missing from this venv: "
        f"{', '.join(missing)}. Tracing is running degraded (missing instrumentors "
        "are skipped, or tracing is disabled if the exporter itself is missing) "
        "until you run: nyxgpt ops install."
    )


def _ollama_env_drift_issue() -> str | None:
    """Detect the #3431 boot-race failure mode: native Ollama's OLLAMA_MODELS
    env drifting back to Ollama's default `~/.ollama/models` store.

    `launchctl setenv` only applies to the launchd GUI session it's run in,
    and the `com.nyxgpt.ollama-env` LaunchAgent that reapplies it is a
    *separate* RunAtLoad agent from Homebrew's own `ollama` one -- launchd
    doesn't guarantee their relative ordering. If Homebrew's agent wins that
    race on a given login, `ollama serve` starts before OLLAMA_MODELS is set
    and silently falls back to the default store for the rest of the
    session, reproducing the split-library bug this issue exists to fix.
    `set-ollama-models-env.sh` now force-restarts the brew service every
    login to close that race, but this check surfaces drift here too in case
    something else (a manual `launchctl unsetenv`, a non-nyxgpt Ollama
    install) undoes it between doctor runs.

    Only reports an issue once the shared store has actually been configured
    (`nyxgpt ops install` has run at least once) and there's a `launchctl` to
    check (macOS). Returns None otherwise.
    """
    if _which("launchctl") is None:
        return None
    marker = _ollama_migration_state_path("ollama-native-env.configured")
    if not marker.exists():
        return None

    expected = _shared_ollama_models_dir()
    cp = _run(["launchctl", "getenv", "OLLAMA_MODELS"], check=False, expected=True)
    actual = (cp.stdout or "").strip()
    if cp.returncode != 0 or not actual:
        return (
            f"Native Ollama's OLLAMA_MODELS env is not set for this login session "
            f"(expected {expected}) -- models pulled/read natively may split from the "
            "shared store again (run: nyxgpt ops install)"
        )
    if Path(actual) != expected:
        return (
            f"Native Ollama's OLLAMA_MODELS is set to {actual}, not the shared store "
            f"{expected} -- models pulled/read natively may split from the shared store "
            "again (run: nyxgpt ops install)"
        )
    return None


# Curated per-component loggers surfaced in the Log Aggregation panel and
# the "Operational Logs" Grafana dashboard (see app._loki_curated_queries).
# `_loki_recent_volume_by_logger` reports each one's recent line count so
# `doctor` output can distinguish "pipeline healthy, component just idle"
# (e.g. deploy/canary legitimately silent on a native install that's never
# run a k8s operation) from "no logs reaching Loki at all" (every logger at
# zero, including ones that just definitely logged something) -- see #3349.
LOKI_CURATED_LOGGERS: dict[str, str] = {
    "self_heal": "nyxgpt.self_heal",
    "deploy": "nyxgpt.deploy",
    "canary": "nyxgpt.canary",
    "chat": "nyxgpt.chat",
    "rag": "nyxgpt.rag.rag",
}


def _grafana_doctor_token_path() -> Path:
    """Where the Grafana service-account token for doctor's Loki log-volume
    check is written -- see `_provision_grafana_doctor_token`.

    A dedicated Viewer-scoped service account, not the shared `admin`
    credential the rest of this module's Grafana calls use: that password
    lives in config.ini and can drift from whatever a long-running Grafana
    container's own database actually has (a regenerated config.ini, a
    hand-edited `.env`, ...), which is what let the datasource-proxy query
    below 401 on an otherwise-healthy stack (#3438). A token minted once and
    read straight off disk sidesteps that drift entirely -- same pattern as
    `_glitchtip_grafana_token_path`'s token file, just consumed by this
    process instead of by Grafana itself.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "grafana-doctor-token"


def _loki_recent_volume_by_logger(
    grafana_ui_url: str, *, hours: int = 24
) -> tuple[dict[str, int] | None, str | None]:
    """Query Loki (via Grafana's provisioned datasource proxy, authenticated
    with the service-account token `_provision_grafana_doctor_token` mints)
    for each curated logger's line count over the last `hours`.

    Returns `(volumes, issue)`. `volumes` is None when nothing could be
    queried. `issue` is set to an actionable `doctor` finding only for the
    fixable auth failure modes (token file missing, or Grafana rejects the
    token) -- #3438 was specifically about that case degrading into a
    debug-level log line no operator would see. Any other failure (Grafana
    or Loki simply unreachable) still returns `issue=None`: that's not
    itself a failure here, since the rest of `doctor` already covers overall
    stack reachability.
    """
    token_path = _grafana_doctor_token_path()
    token = token_path.read_text().strip() if token_path.exists() else ""
    if not token:
        return None, (
            f"Missing Grafana service-account token at {token_path} -- the Loki "
            "log-volume check can't authenticate (run: nyxgpt ops install)"
        )

    volumes: dict[str, int] = {}
    try:
        with httpx.Client(
            base_url=grafana_ui_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        ) as client:
            for name, logger_name in LOKI_CURATED_LOGGERS.items():
                query = f'sum(count_over_time({{job="nyxgpt", logger="{logger_name}"}}[{hours}h]))'
                resp = client.get(
                    "/api/datasources/proxy/uid/loki/loki/api/v1/query",
                    params={"query": query},
                )
                if resp.status_code == 401:
                    return None, (
                        f"Grafana rejected the doctor service-account token at {token_path} "
                        "(401) -- delete the file and re-run `nyxgpt ops install` to mint a "
                        "fresh one"
                    )
                resp.raise_for_status()
                result = resp.json().get("data", {}).get("result", [])
                volumes[name] = int(float(result[0]["value"][1])) if result else 0
    except Exception as e:
        logger.warning(
            "Failed to query Loki log volumes via %s, skipping: %s",
            grafana_ui_url,
            e,
            extra={"component": "ops"},
        )
        return None, None
    return volumes, None


def doctor(_args) -> int:
    """CLI entrypoint for `nyxgpt ops doctor`.

    Checks for common misconfigurations: missing ~/.nyxGPT/config.ini,
    non-executable helper scripts, missing brew/docker/node/npm tools on
    PATH, missing/incomplete web dependencies (node_modules, undici),
    (when log aggregation is enabled and native logs exist) whether
    promtail is actually wired to see native-mode host logs, (when tracing
    is enabled) whether the configured OTLP endpoint actually has something
    listening on it, (once the shared Ollama store has been configured)
    whether native Ollama's OLLAMA_MODELS env has drifted from it (#3431),
    and whether `~/.nyxGPT/secrets` is writable / holds the GlitchTip
    Grafana token when the observability stack is up (#3432), and whether
    the installed environment actually has every dependency declared in
    `pyproject.toml` -- catches a venv that wasn't refreshed after a `git
    pull` added or bumped one (#3487).
    Also prints a per-logger recent log volume
    (last 24h, via Loki) when log aggregation
    and the monitoring stack are both up, so idle curated components aren't
    mistaken for a broken pipeline -- and, if that check's Grafana
    service-account token is missing or rejected, reports it as an issue
    rather than silently omitting the log volume line (#3438). Prints each
    issue found.

    Returns 0 if no issues were found, else 2.
    """
    logger.info("ops: doctor starting", extra={"component": "ops", "action": "doctor"})
    issues: list[str] = []

    cfg = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg.exists():
        issues.append(f"Missing config {cfg}")

    volume_info: str | None = None
    cfg_parser: ConfigParser | None = None
    if cfg.exists():
        try:
            parsed = ConfigParser()
            parsed.read(cfg)
            cfg_parser = parsed
        except Exception as e:
            logger.warning(
                "Failed to parse %s, skipping config-dependent doctor checks: %s",
                cfg,
                e,
                extra={"component": "ops"},
            )
            cfg_parser = None
    if (
        cfg_parser is not None
        and get_log_aggregation_enabled(cfg_parser)
        and _compose_stack_snapshot().get("promtail") == "running"
    ):
        monitoring_config = get_monitoring_config(cfg_parser)
        volumes, loki_issue = _loki_recent_volume_by_logger(monitoring_config["grafana_ui_url"])
        if volumes is not None:
            volume_info = ", ".join(f"{k}={v}" for k, v in volumes.items())
        if loki_issue is not None:
            issues.append(loki_issue)

    for name in (
        "run-web.sh",
        "follow-cassandra-logs.sh",
        "follow-ollama-logs.sh",
        "set-ollama-models-env.sh",
    ):
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

    if _which("docker") is not None:
        restarting = sorted(
            service for service, state in _compose_stack_snapshot().items() if state == "restarting"
        )
        if restarting:
            issues.append(
                "Compose service(s) stuck in a restart/crash loop: "
                f"{', '.join(restarting)} (run: nyxgpt ops logs <service> to see why it's "
                "failing to start)"
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
            except Exception as e:
                logger.warning(
                    "Failed to resolve node package %r, assuming missing: %s",
                    pkg,
                    e,
                    extra={"component": "ops"},
                )
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

    tracing_issue = _tracing_wiring_issue()
    if tracing_issue:
        issues.append(tracing_issue)

    tracing_packages_issue = _tracing_packages_doctor_issue()
    if tracing_packages_issue:
        issues.append(tracing_packages_issue)

    ollama_env_issue = _ollama_env_drift_issue()
    if ollama_env_issue:
        issues.append(ollama_env_issue)

    issues += _glitchtip_secrets_doctor_issues()
    issues += _stale_venv_doctor_issues()

    if (
        TERRAFORM_DIR.joinpath("terraform.tfstate").exists()
        and _terraform_state_has_resources()
        and all(state == "absent" for state in terraform_stack_state().values())
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
        if volume_info is not None:
            print(f"Log volume (last 24h) by logger: {volume_info}")
        logger.warning(
            "ops: doctor found %d issue(s): %s",
            len(issues),
            "; ".join(issues),
            extra={"component": "ops", "action": "doctor", "ok": False, "issues": issues},
        )
        return 2

    print("nyxGPT ops doctor: OK")
    if volume_info is not None:
        print(f"Log volume (last 24h) by logger: {volume_info}")
    logger.info(
        "ops: doctor found no issues",
        extra={"component": "ops", "action": "doctor", "ok": True, "issues": []},
    )
    return 0


def _clear_intentional_stops(components: list[str]) -> list[OpsResult]:
    """Clear the intentional-stop marker for `components` (they're being brought back up).

    Called from `install`/`restart` for whatever they bring up -- doing so is
    itself the "this is desired again" signal, so self-heal resumes guarding
    the component against future crashes (see self_heal.py's intentional-stop
    registry and its module docstring, #3406). A component never marked
    stopped is a no-op, so this is safe to call unconditionally.
    """
    for component in components:
        self_heal.clear_intentionally_stopped(component)
    return [OpsResult(True, f"Cleared intentional-stop marker(s): {', '.join(components)}")]


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
            self_heal.clear_intentionally_stopped("api")
            results.extend(_restart_brew_service("nyxgpt-api"))

    if target in ("all", "web"):
        conflict = _compose_conflict_result("web", compose)
        if conflict:
            results.append(conflict)
        else:
            self_heal.clear_intentionally_stopped("web")
            results.extend(_restart_brew_service("nyxgpt-web"))

    if target in ("all", "ollama"):
        conflict = _compose_conflict_result("ollama", compose)
        if conflict:
            results.append(conflict)
        else:
            self_heal.clear_intentionally_stopped("ollama")
            results.extend(_restart_brew_service("ollama"))

    if target in ("all", "cassandra"):
        conflict = _compose_conflict_result("cassandra", compose)
        if conflict:
            results.append(conflict)
        else:
            self_heal.clear_intentionally_stopped("cassandra")
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
    result, message = _ops_action_outcome(results)
    _record_ops_action("restart", target, result, message)

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
        cp = _run(["launchctl", "bootout", domain], check=False, expected=True)
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
    tf_or_k8s = _terraform_or_kubernetes_managed_components()

    def _stop_dual_mode(
        component: str, native_stop: Callable[[str], list[OpsResult]], native_arg: str
    ) -> None:
        if component in tf_or_k8s:
            results.append(
                OpsResult(
                    True,
                    f"{component}: running under Terraform/Kubernetes, not native/Compose -- "
                    "left alone (self-heal still guards it; use --terraform/--kubernetes to "
                    "tear it down)",
                )
            )
            return
        try:
            self_heal.mark_intentionally_stopped(component)
        except Exception as e:  # never let self-heal bookkeeping block a stop
            results.append(
                OpsResult(
                    True,
                    f"Could not mark {component} as intentionally stopped "
                    f"({type(e).__name__}: {e})",
                )
            )
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
    result, message = _ops_action_outcome(results)
    _record_ops_action("stop", target, result, message)

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
        # Mark api/web/ollama/cassandra as intentionally stopped BEFORE
        # stopping anything. Otherwise the next heal pass sees them as
        # unhealthy and restarts them (`brew services restart` / `docker
        # restart`), fighting the teardown -- the ports get re-occupied
        # within seconds, which then blocks `nyxgpt ops install --terraform
        # --local` with a spurious port collision. This is per-component
        # (self_heal.py's intentional-stop registry, #3406), not a global
        # watchdog disable: an armed watchdog keeps healing genuine crashes
        # of every other component the whole time. `nyxgpt ops install`/
        # `restart`/`up` of a component clears its marker automatically.
        tf_or_k8s = _terraform_or_kubernetes_managed_components()
        try:
            marked = [
                component
                for component in ("api", "web", "ollama", "cassandra")
                if component not in tf_or_k8s
            ]
            for component in marked:
                self_heal.mark_intentionally_stopped(component)
            if marked:
                results.append(
                    OpsResult(
                        True,
                        f"Marked {', '.join(marked)} as intentionally stopped "
                        "(self-heal will leave them alone until brought back up)",
                    )
                )
            skipped = [c for c in ("api", "web", "ollama", "cassandra") if c in tf_or_k8s]
            if skipped:
                results.append(
                    OpsResult(
                        True,
                        f"Left {', '.join(skipped)} alone -- running under Terraform/"
                        "Kubernetes, not native/Compose (self-heal still guards them; "
                        "use --terraform/--kubernetes to tear those down)",
                    )
                )
        except Exception as e:  # never let self-heal bookkeeping block a teardown
            results.append(
                OpsResult(
                    True,
                    "Could not mark components as intentionally stopped "
                    f"({type(e).__name__}: {e})",
                )
            )

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
    result, message = _ops_action_outcome(results)
    _record_ops_action("down", scope, result, message)

    return 0 if ok else 2


def logs(args) -> int:
    """Print recent logs for a single component, in whichever mode it's actually running.

    Wraps `docker compose logs`/`docker logs`/`kubectl logs`/the component's
    own log files so operators never need to run a raw `docker`/`docker
    compose`/`kubectl` command (or hunt for a native log path) themselves --
    e.g. to read the `errors` profile's GlitchTip container output for the
    first-account registration confirmation link its console email backend
    prints there, or the native API's own log file. See
    `self_heal.component_logs` for the per-mode dispatch (#3442).
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
    cp = _run(["docker", "compose", "version"], check=False, expected=True)
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


def _grafana_datasources_dir() -> Path:
    """`docker/grafana/provisioning/datasources/` -- one YAML file per
    datasource group (GlitchTip lives in its own file, `glitchtip.yml`,
    separate from `datasource.yml`'s Prometheus/Loki/Jaeger -- #3432 -- so a
    `$__file{}` interpolation failure in one file can't take the others
    down; see `docker/grafana/provisioning/datasources/glitchtip.yml`)."""
    return REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources"


def _grafana_provisioning_fingerprint() -> str:
    """sha256 over everything that only takes effect when the `grafana`
    container is recreated: every datasource provisioning file (new/changed
    datasources) and the Compose file itself (image tag, `GF_INSTALL_PLUGINS`,
    any other env). `docker compose up -d` alone does NOT pick up an env or
    image change on an already-running container -- it needs
    `--force-recreate`. This fingerprint lets `_start_observability_stack`
    detect that drift deterministically (#3424) instead of leaving dashboards
    silently pointed at a stale container missing a plugin or datasource.
    """
    digest = hashlib.sha256()
    datasources_dir = _grafana_datasources_dir()
    paths = sorted(datasources_dir.glob("*.yml")) if datasources_dir.exists() else []
    for path in (*paths, self_heal.COMPOSE_FILE):
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _grafana_provisioning_fingerprint_marker() -> Path:
    """Host-side marker recording the provisioning fingerprint last applied.

    Stored outside Grafana's own bind-mounted data dir (like
    `_migration_marker_path`) so it survives independently of the
    container/volume lifecycle and reflects what THIS tool last reconciled,
    not what the container happens to contain.
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / "grafana-provisioning.fingerprint"


def _grafana_provisioning_drifted() -> bool:
    """True if the datasource provisioning file or Compose grafana config
    changed since the last time this tool recreated the container for it."""
    marker = _grafana_provisioning_fingerprint_marker()
    if not marker.exists():
        return True
    try:
        return marker.read_text().strip() != _grafana_provisioning_fingerprint()
    except OSError:
        return True


def _record_grafana_provisioning_fingerprint() -> None:
    """Persist the current provisioning fingerprint as "already applied"."""
    _grafana_provisioning_fingerprint_marker().write_text(_grafana_provisioning_fingerprint())


def _grafana_provisioned_datasource_uids() -> list[str]:
    """Datasource `uid`s declared across every file in
    `docker/grafana/provisioning/datasources/`.

    Deliberately regex-scraped rather than parsed with PyYAML: PyYAML isn't a
    runtime dependency of this package (only a test-suite one), and these
    files are repo-controlled config, not user input, so a targeted regex
    over their known `uid: <value>` shape is sufficient.
    """
    datasources_dir = _grafana_datasources_dir()
    if not datasources_dir.exists():
        return []
    uids: list[str] = []
    for path in sorted(datasources_dir.glob("*.yml")):
        uids += re.findall(r"^\s*uid:\s*(\S+)\s*$", path.read_text(), re.MULTILINE)
    return uids


def _grafana_admin_password_path() -> Path:
    """Where the ops-managed Grafana admin password is stored.

    Thin alias for `config.grafana_admin_password_path` -- kept as a
    module-level name here so existing call sites/tests in this module don't
    need to change, but the actual resolution logic lives in `config.py` so
    `health.py` can share it (#3466) instead of drifting apart (#3458).
    """
    return grafana_admin_password_path()


def _grafana_admin_password(cfg: ConfigParser) -> str:
    """Resolve the Grafana admin password `nyxgpt ops` should reconcile the
    container to.

    Thin alias for `config.resolve_grafana_admin_password` -- see that
    function's docstring for the resolution order.
    """
    return resolve_grafana_admin_password(cfg)


def _grafana_admin_client(grafana_ui_url: str, grafana_admin_password: str) -> httpx.Client:
    """Single source of truth for how ops's install-time checks authenticate
    against Grafana's API -- one place to change the URL/credential wiring
    instead of three separate `auth=("admin", ...)` call sites (#3458)."""
    return httpx.Client(
        base_url=grafana_ui_url,
        auth=("admin", grafana_admin_password),
        timeout=5.0,
    )


@contextlib.contextmanager
def _quiet_httpx_retries() -> Iterator[None]:
    """Suppress httpx's per-request INFO log line for the duration of a
    bounded retry loop against Grafana.

    Each attempt otherwise logs an `HTTP Request: GET ... "200 OK"` line at
    INFO -- pure noise for a handful of retries polled every couple seconds,
    the same "expected outcome shouldn't spam the console" philosophy #3436
    applied to subprocess probe failures.
    """
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous_level)


def _grafana_admin_auth_status(grafana_ui_url: str, grafana_admin_password: str) -> str:
    """Probe `grafana_admin_password` against the running Grafana instance.

    Returns one of `"ok"` (200), `"unauthorized"` (401 -- Grafana answered,
    the credential is genuinely wrong), or `"unreachable"` (connection
    error, or any other status) -- e.g. Grafana still starting, or stuck in
    a crash loop. `_reconcile_grafana_admin_credential` (#3538) uses this
    distinction to give a failure message that points at the right fix: a
    rejected credential and an unreachable/crash-looping Grafana need
    opposite operator responses.
    """
    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            status_code = client.get("/api/org").status_code
    except httpx.HTTPError:
        return "unreachable"
    if status_code == 200:
        return "ok"
    if status_code == 401:
        return "unauthorized"
    return "unreachable"


def _grafana_admin_authenticates(grafana_ui_url: str, grafana_admin_password: str) -> bool:
    """Whether `grafana_admin_password` currently authenticates as `admin`
    against the running Grafana instance."""
    return _grafana_admin_auth_status(grafana_ui_url, grafana_admin_password) == "ok"


def _reset_grafana_admin_password(password: str) -> OpsResult:
    """Force Grafana's actual admin password to `password` via `grafana cli
    admin reset-admin-password` run inside the container.

    Unlike `GF_SECURITY_ADMIN_PASSWORD`, this takes effect regardless of
    whether the volume is fresh (first boot) or long-lived (env var is only
    applied at first boot -- irrelevant to an existing volume, which is
    exactly the deployment shape that reproduced #3458's 401s).
    """
    if not _compose_available():
        return OpsResult(
            False,
            "Cannot reset Grafana admin password",
            "Docker Compose not found -- install Docker to manage the observability stack.",
        )
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "exec",
        "-T",
        "grafana",
        "grafana",
        "cli",
        "admin",
        "reset-admin-password",
        password,
    ]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return OpsResult(
            False,
            "Failed to reset Grafana admin password",
            (cp.stderr or cp.stdout or "").strip(),
        )
    return OpsResult(True, "Reset Grafana admin password to the ops-managed credential")


def _reconcile_grafana_admin_credential(
    grafana_ui_url: str,
    cfg: ConfigParser,
    *,
    attempts: int = 3,
    delay_s: float = 2.0,
) -> tuple[str | None, OpsResult]:
    """Make the Grafana admin credential deterministic before anything else
    authenticates with it (#3458).

    Never authenticates with an empty password: `_grafana_admin_password`
    always returns a real value. Tries that value against the running
    instance first (retrying briefly for Grafana's own startup window); if
    it doesn't work -- unset config default drifted from an existing
    volume's real password, a hand-edited `.env`, whatever -- resets the
    container's password to it via `grafana cli admin reset-admin-password`
    and re-verifies. Returns `(password, result)`: `password` is `None` and
    `result.ok` is `False` when reconciliation could not be completed, so
    callers can skip credential-dependent follow-on checks and surface this
    ONE actionable failure instead of each of them separately 401ing.

    The final failure message distinguishes a genuinely rejected credential
    (Grafana answered 401 -- the stored password is wrong) from Grafana
    being unreachable or crash-looping the whole time (#3538: these need
    opposite operator responses, and were previously reported with the same
    "still doesn't authenticate" message regardless of which one happened).
    """
    password = _grafana_admin_password(cfg)
    last_status = "unreachable"

    with _quiet_httpx_retries():
        for attempt in range(attempts):
            last_status = _grafana_admin_auth_status(grafana_ui_url, password)
            if last_status == "ok":
                return password, OpsResult(True, "Grafana admin credential already reconciled")
            if attempt < attempts - 1:
                time.sleep(delay_s)

        reset_result = _reset_grafana_admin_password(password)
        if not reset_result.ok:
            return None, OpsResult(
                False,
                "Could not reconcile Grafana admin credential",
                f"{reset_result.message}"
                + (f": {reset_result.details}" if reset_result.details else "")
                + " -- check `nyxgpt ops logs grafana` and confirm the grafana container "
                "is running.",
            )

        for attempt in range(attempts):
            last_status = _grafana_admin_auth_status(grafana_ui_url, password)
            if last_status == "ok":
                return password, OpsResult(
                    True,
                    f"{reset_result.message} (stored at {_grafana_admin_password_path()})",
                )
            if attempt < attempts - 1:
                time.sleep(delay_s)

    if last_status == "unreachable":
        return None, OpsResult(
            False,
            "Grafana is unreachable after reset",
            "Reset via `grafana cli admin reset-admin-password` succeeded but Grafana never "
            "answered GET /api/org afterward -- this looks like Grafana crash-looping or still "
            "starting, not a rejected credential. Check `nyxgpt ops status` (a compose service "
            "stuck `restarting` is the tell) and `nyxgpt ops logs grafana` for the boot error.",
        )
    return None, OpsResult(
        False,
        "Grafana admin credential still doesn't authenticate after reset",
        f"Reset via `grafana cli admin reset-admin-password` succeeded but GET /api/org "
        f"still rejects the password stored at {_grafana_admin_password_path()} -- check "
        "`nyxgpt ops logs grafana` for a startup error.",
    )


def _verify_grafana_datasources_resolve(
    grafana_ui_url: str,
    grafana_admin_password: str,
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
) -> OpsResult:
    """Confirm every datasource declared in provisioning actually resolves in
    the running Grafana instance -- i.e. `GET /api/datasources` lists it.

    This directly targets the #3424 failure mode: a provisioning/plugin/env
    change that silently fails to apply (stale container, broken plugin
    download, a provisioning YAML error for one entry) leaves a declared
    datasource unreachable, and every panel that queries it shows "Datasource
    was not found" instead of a clear ops-level error. Retries briefly since
    Grafana may still be inside its startup/provisioning window right after
    `up -d`.
    """
    expected = set(_grafana_provisioned_datasource_uids())
    if not expected:
        return OpsResult(True, "No datasources declared in provisioning -- nothing to verify")

    last_error: Exception | None = None
    with _quiet_httpx_retries():
        for attempt in range(attempts):
            try:
                with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
                    resp = client.get("/api/datasources")
                    resp.raise_for_status()
                    actual = {ds.get("uid") for ds in resp.json()}
                missing = expected - actual
                if not missing:
                    return OpsResult(
                        True,
                        f"Verified {len(expected)} provisioned Grafana datasource(s) resolve",
                    )
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
                return OpsResult(
                    False,
                    f"Grafana datasource(s) failed to provision: {', '.join(sorted(missing))}",
                    "Declared in docker/grafana/provisioning/datasources/*.yml but not "
                    "returned by GET /api/datasources -- check `nyxgpt ops logs grafana` for a "
                    "provisioning error (bad plugin id, YAML syntax, unreachable datasource "
                    "type) near 'inserting datasource from configuration'.",
                )
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
    return OpsResult(
        False,
        "Could not reach Grafana to verify provisioned datasources",
        str(last_error) if last_error else "",
    )


def _grafana_expected_plugin_ids() -> list[str]:
    """Plugin ids declared in `docker-compose.yml`'s `GF_INSTALL_PLUGINS`.

    Regex-scraped for the same reason as `_grafana_provisioned_datasource_uids`
    -- no PyYAML runtime dependency, and this is repo-controlled config.
    """
    if not self_heal.COMPOSE_FILE.exists():
        return []
    match = re.search(r'GF_INSTALL_PLUGINS:\s*"([^"]*)"', self_heal.COMPOSE_FILE.read_text())
    if not match:
        return []
    return [p.strip() for p in match.group(1).split(",") if p.strip()]


def _verify_grafana_plugins_installed(
    grafana_ui_url: str,
    grafana_admin_password: str,
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
) -> OpsResult:
    """Confirm every plugin listed in `GF_INSTALL_PLUGINS` is actually
    installed and enabled, rather than assuming a successful env-var-driven
    download (#3424's "plugin installation is confirmed... rather than
    assumed from GF_INSTALL_PLUGINS" AC). `GF_INSTALL_PLUGINS` downloads
    each plugin from the network at container startup and fails silently
    (a logged error, not a crash) if that download fails -- e.g. no network
    access, registry outage, or a renamed/typo'd plugin id.
    """
    expected = _grafana_expected_plugin_ids()
    if not expected:
        return OpsResult(True, "No plugins declared in GF_INSTALL_PLUGINS -- nothing to verify")

    last_error: Exception | None = None
    with _quiet_httpx_retries():
        for attempt in range(attempts):
            try:
                missing: list[str] = []
                with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
                    for plugin_id in expected:
                        resp = client.get(f"/api/plugins/{plugin_id}/settings")
                        if resp.status_code != 200 or not resp.json().get("enabled"):
                            missing.append(plugin_id)
                if not missing:
                    return OpsResult(
                        True, f"Verified {len(expected)} GF_INSTALL_PLUGINS plugin(s) installed"
                    )
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
                return OpsResult(
                    False,
                    f"Grafana plugin(s) failed to install: {', '.join(missing)}",
                    "Listed in docker-compose.yml's GF_INSTALL_PLUGINS but not enabled per "
                    "GET /api/plugins/<id>/settings -- check `nyxgpt ops logs grafana` for a "
                    "plugin download error (no network access, registry outage, renamed/typo'd "
                    "plugin id).",
                )
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
    return OpsResult(
        False,
        "Could not reach Grafana to verify installed plugins",
        str(last_error) if last_error else "",
    )


# Grafana service account minted for `nyxgpt ops doctor`'s Loki log-volume
# check -- see `_grafana_doctor_token_path` and `_provision_grafana_doctor_token`.
GRAFANA_DOCTOR_SA_NAME = "nyxgpt-ops-doctor"
GRAFANA_DOCTOR_TOKEN_NAME = "nyxgpt-ops-doctor"


def _provision_grafana_doctor_token(grafana_ui_url: str, grafana_admin_password: str) -> OpsResult:
    """Mint a Viewer-scoped Grafana service-account token for doctor's Loki
    check, so it authenticates without depending on the shared admin
    password at query time (#3438).

    Grafana only returns a service-account token's secret at creation time
    (like a GitHub PAT) -- it can't be re-fetched later even to confirm it
    still works -- so this only mints a fresh one when
    `_grafana_doctor_token_path()` doesn't already hold one. A token that's
    since been revoked/gone stale is instead caught (and reported as an
    actionable `doctor` finding, not a silent skip) by
    `_loki_recent_volume_by_logger` at query time.
    """
    token_path = _grafana_doctor_token_path()
    if token_path.exists() and token_path.read_text().strip():
        return OpsResult(True, f"{token_path} already holds a Grafana service-account token")

    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            sa_id: Any = None
            resp = client.get(
                "/api/serviceaccounts/search", params={"query": GRAFANA_DOCTOR_SA_NAME}
            )
            resp.raise_for_status()
            for sa in resp.json().get("serviceAccounts", []):
                if isinstance(sa, dict) and sa.get("name") == GRAFANA_DOCTOR_SA_NAME:
                    sa_id = sa.get("id")
                    break
            if sa_id is None:
                resp = client.post(
                    "/api/serviceaccounts",
                    json={"name": GRAFANA_DOCTOR_SA_NAME, "role": "Viewer"},
                )
                resp.raise_for_status()
                sa_id = resp.json().get("id")

            resp = client.post(
                f"/api/serviceaccounts/{sa_id}/tokens",
                json={"name": GRAFANA_DOCTOR_TOKEN_NAME},
            )
            resp.raise_for_status()
            token: Any = resp.json().get("key")
    except Exception as e:
        return OpsResult(
            False,
            "Failed to provision Grafana service-account token for doctor's Loki check",
            f"{type(e).__name__}: {e}",
        )

    if not token:
        return OpsResult(
            False,
            "Grafana service-account token response missing a key",
            "Check `nyxgpt ops logs grafana` for a /api/serviceaccounts error.",
        )

    try:
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_path.write_text(str(token))
        token_path.chmod(0o600)
    except OSError as e:
        return OpsResult(
            False, f"Failed to write Grafana service-account token to {token_path}", str(e)
        )

    return OpsResult(True, f"Provisioned Grafana service-account token for doctor at {token_path}")


def _recreate_grafana_if_provisioning_drifted() -> OpsResult | None:
    """Force-recreate just the `grafana` Compose container when provisioning
    or env has drifted since this tool last reconciled it.

    `docker compose up -d` is a no-op for a container whose image/env didn't
    change from Compose's point of view even when the datasource YAML it
    mounts read-only did -- and env vars like `GF_INSTALL_PLUGINS` only take
    effect at container start. Recreating (not merely restarting) guarantees
    both are picked up on the next boot. Returns None if there's nothing to
    do (not drifted, Docker unavailable, or grafana isn't running yet -- the
    normal `up -d` right after this call handles first-boot).
    """
    if not _compose_available():
        return None
    if not _grafana_provisioning_drifted():
        return None
    if _compose_stack_snapshot().get("grafana") != "running":
        # Nothing running yet to be stale -- the normal `up -d` below will
        # create it fresh with current provisioning/env already.
        return None

    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "--profile",
        "monitoring",
        "up",
        "-d",
        "--force-recreate",
        "grafana",
    ]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return OpsResult(
            False,
            "Failed to recreate Grafana for provisioning/env drift",
            (cp.stderr or cp.stdout or "").strip(),
        )
    return OpsResult(
        True,
        "Recreated Grafana (provisioning/env drift detected) so plugin installs and "
        "datasource changes actually take effect",
    )


def _start_observability_stack(
    extra_compose_files: list[Path] | None = None, force_recreate: bool = False
) -> list[OpsResult]:
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
    for extra in extra_compose_files or []:
        base_cmd += ["-f", str(extra)]
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

    cmd = base_cmd + ["up", "-d"]
    if force_recreate:
        # Ensure containers attach to the current network even if ones from an
        # earlier bring-up linger on a different network (e.g. switching to the
        # terraform-net override) -- otherwise glitchtip-migrate et al. can't
        # resolve their peers and the stack silently half-starts.
        cmd += ["--force-recreate"]
    cmd += observability_services

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


def _reconcile_grafana_provisioning() -> list[OpsResult]:
    """Bring the observability stack up AND make Grafana's provisioning
    state deterministic: recreate on drift before starting, reconcile the
    admin credential (#3458), then verify the declared datasources and
    GF_INSTALL_PLUGINS plugins actually resolve afterward (#3424), and mint
    doctor's Loki service-account token if it doesn't already have one
    (#3438).

    This is the entrypoint `nyxgpt ops install` / `nyxgpt ops observability`
    / wizard toggles use instead of calling `_start_observability_stack`
    directly, so the extra drift/verification steps apply everywhere the
    stack gets (re)started but stay out of `_start_observability_stack`
    itself (which several other call sites -- including the terraform-network
    variant and tests -- use as a narrower "just run compose up" primitive).
    """
    drift_result = _recreate_grafana_if_provisioning_drifted()
    results = [drift_result] if drift_result is not None else []
    if drift_result is not None and not drift_result.ok:
        return results

    start_results = _start_observability_stack()
    results += start_results
    if not all(r.ok for r in start_results):
        return results

    _record_grafana_provisioning_fingerprint()

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if cfg_path.exists():
        try:
            cfg_parser = ConfigParser()
            cfg_parser.read(cfg_path)
            monitoring_config = get_monitoring_config(cfg_parser)
            grafana_ui_url = monitoring_config["grafana_ui_url"]

            # Bounded wait-for-healthy (#3538) before anything authenticates
            # against Grafana: `_start_observability_stack`/the drift
            # recreate above may have just (re)started the container, and a
            # container stuck crash-looping (e.g. a broken alerting-
            # provisioning file) should surface here as one clear failure
            # rather than as a misleading "credential doesn't authenticate".
            # Skipped as a no-op when Grafana isn't part of the running
            # Compose stack at all (e.g. Docker not found) -- the
            # credential-reconcile call below already handles that host
            # shape on its own terms.
            grafana_running = _compose_stack_snapshot().get("grafana") == "running"
            if grafana_running and not _wait_for_grafana_healthy():
                results.append(
                    OpsResult(
                        False,
                        "Grafana never became healthy",
                        "Check `nyxgpt ops status` (a compose service stuck `restarting` is "
                        "the tell) and `nyxgpt ops logs grafana` for the boot error.",
                    )
                )
                return results

            grafana_admin_password, credential_result = _reconcile_grafana_admin_credential(
                grafana_ui_url, cfg_parser
            )
            results.append(credential_result)
            # Only attempt the credential-dependent checks once the admin
            # password is known-good -- otherwise each would independently
            # 401 and turn one auth problem into three separate FAILs.
            if grafana_admin_password is not None:
                results.append(
                    _verify_grafana_plugins_installed(grafana_ui_url, grafana_admin_password)
                )
                results.append(
                    _verify_grafana_datasources_resolve(grafana_ui_url, grafana_admin_password)
                )
                results.append(
                    _provision_grafana_doctor_token(grafana_ui_url, grafana_admin_password)
                )
        except Exception as e:
            logger.warning(
                "Failed to verify Grafana plugins/datasources, skipping: %s",
                e,
                extra={"component": "ops"},
            )

    return results


def _start_observability_stack_terraform() -> list[OpsResult]:
    """Start the observability Compose profiles on the terraform network.

    A step in `nyxgpt ops install --terraform --local`, run after `terraform
    apply` has created the `nyxgpt-terraform` network and the core containers.
    Reuses `_start_observability_stack` with the terraform-net override so the
    observability containers join that network and interoperate with the
    terraform-managed core (Prometheus scrapes the tf-api, the api reaches
    otel-collector/glitchtip, etc.).
    """
    if not TERRAFORM_NET_OVERRIDE.exists():
        return [
            OpsResult(
                False,
                f"Missing {TERRAFORM_NET_OVERRIDE}",
                "Cannot attach observability to the terraform network without the override.",
            )
        ]
    return _start_observability_stack(
        extra_compose_files=[TERRAFORM_NET_OVERRIDE], force_recreate=True
    )


def _stop_observability_stack_terraform() -> list[OpsResult]:
    """Tear down the observability Compose stack attached to the terraform network.

    The mirror of `_start_observability_stack_terraform`, run BEFORE `terraform
    destroy`. `install --terraform --local` brings observability up on the
    `nyxgpt-terraform` network; `terraform destroy` then can't remove that
    network while those containers are still attached (it times out on the
    network delete). This `docker compose down` (with the terraform-net
    override) removes the observability containers and detaches them from the
    network -- the external network itself is left for terraform to destroy.
    Best-effort: a Docker-less host or a missing override just skips it.
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped observability teardown (Docker not found)")]
    if not TERRAFORM_NET_OVERRIDE.exists():
        return [
            OpsResult(True, f"Skipped observability teardown (no {TERRAFORM_NET_OVERRIDE.name})")
        ]
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "-f",
        str(TERRAFORM_NET_OVERRIDE),
    ]
    for profile in OBSERVABILITY_PROFILES:
        cmd += ["--profile", profile]
    cmd += ["down"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "Failed to tear down observability stack", _cp_details(cp))]
    return [OpsResult(True, "Observability stack torn down (detached from terraform network)")]


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
    results = _reconcile_grafana_provisioning()
    ok = _emit_results("observability", results)
    logger.info(
        "ops: observability %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "observability", "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("observability", "observability", result, message)
    return 0 if ok else 2


def reconcile_observability(enable: bool) -> list[OpsResult]:
    """Bring the observability Compose stack in line with a desired enabled state.

    Used by the web config wizard (#3354) after config.ini's
    monitoring/tracing/error_tracking/log_aggregation `enabled` flags change
    via the API, so enabling one of those sections in the wizard results in
    the Compose profile actually starting -- not just a flag flip -- and
    disabling all of them tears the stack back down. Mirrors `nyxgpt ops
    observability` / `nyxgpt ops stop --target observability` exactly, so
    callers (the config API) never need to shell out to `docker compose`
    themselves. Records its own ops lifecycle action/event (#3390) since,
    unlike `restart()`/`stop()`, this is the entrypoint the dashboard calls
    directly rather than going through the CLI-facing `observability()`.
    """
    results = _reconcile_grafana_provisioning() if enable else _stop_observability_stack()
    result, message = _ops_action_outcome(results)
    _record_ops_action("observability" if enable else "stop", "observability", result, message)
    return results


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
    seeds the live `docker/config.docker.ini` from its template (if missing)
    and derives Docker Compose's `.env` secrets from config.ini via
    `sync_env_from_config`, printing an OK/FAIL line per result.

    Seeding the Compose config here (not just in `nyxgpt ops install`) covers
    the Compose-only Quickstart, which runs `nyxgpt ops env-sync` before
    `docker compose up` without going through the native install flow -- so a
    fresh checkout gets the bind-mounted file created before Compose needs it.

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

    results = _generate_compose_config()
    results += sync_env_from_config(cfg_path=cfg_path, env_path=env_path)
    results += _sync_grafana_slack_webhook_secret(cfg_path=cfg_path)

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
# event:read lets Grafana's Infinity datasource (#3411) query issues/events
# via GlitchTip's Sentry-compatible REST API; the rest predate that use.
GLITCHTIP_TOKEN_SCOPES = ["org:read", "org:write", "project:read", "project:write", "event:read"]
GLITCHTIP_DEFAULT_ADMIN_EMAIL = "admin@nyxgpt.local"


def _glitchtip_grafana_token_path() -> Path:
    """Where the GlitchTip API token is written for Grafana's Infinity
    datasource to read (#3411) -- see
    docker/grafana/provisioning/datasources/glitchtip.yml's `$__file{}`
    reference and docs/docker-compose.md#grafana-single-pane-of-glass. Lives
    outside `~/.nyxGPT/volumes/grafana` (Grafana's own data dir) so it isn't
    swept by `nyxgpt ops down --volumes`, which deletes volume_dir()
    contents -- this is a credential, not app data.

    Computed fresh on every call (like `_provision_glitchtip`'s
    `native_cfg_path`), not a module-level constant, so tests can
    monkeypatch `Path.home`.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"


def _glitchtip_secrets_dir_unwritable_result(path: Path) -> OpsResult:
    """Actionable failure for a `~/.nyxGPT/secrets` that exists but this
    process can't write to -- shared by the install preflight and the token
    writer itself so both report the same guidance (#3432)."""
    return OpsResult(
        False,
        f"{path} exists but is not writable by {getpass.getuser()!r}",
        f"On Linux, dockerd runs as root and auto-creates a missing Docker bind-mount "
        f"source directory as root:root the first time a container starts -- if "
        f"Grafana started before this directory existed, that's almost certainly what "
        f"happened here. Fix with: sudo chown -R $(whoami) {path} && chmod 700 {path}, "
        "then re-run `nyxgpt ops install` (or `nyxgpt ops glitchtip-init`).",
    )


def _ensure_glitchtip_secrets_dir() -> list[OpsResult]:
    """Ensure `~/.nyxGPT/secrets` exists and is writable by the invoking user
    *before* anything bind-mounts it (#3432).

    On Linux, dockerd runs as root and auto-creates a missing bind-mount
    source directory as root:root the first time a container starts. Since
    `docker-compose.yml` mounts this directory read-only into Grafana
    (`${HOME}/.nyxGPT/secrets:/etc/nyxgpt-secrets:ro`), if Grafana starts
    (via `_reconcile_grafana_provisioning`/`_start_observability_stack_terraform`)
    before this directory exists, Docker creates it root-owned and every
    later attempt by this (non-root) process to write the GlitchTip token
    into it -- `_write_grafana_glitchtip_token` -- fails with a raw
    `PermissionError`. Running this as an early install step, ahead of
    those, means Docker always finds the directory already present and
    never touches its ownership. macOS (Docker Desktop) doesn't hit this:
    its VM handles bind-mount ownership differently.

    Run as a best-effort preflight step (`install()` / `_install_terraform_steps`
    catch and log any exception a step raises), so it never needs to raise
    itself -- it just reports whether the directory is now usable.
    """
    path = _glitchtip_grafana_token_path().parent
    if not path.exists():
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as e:
            return [OpsResult(False, f"Failed to create {path}", f"{type(e).__name__}: {e}")]
        return [OpsResult(True, f"Created {path}")]

    if not os.access(path, os.W_OK | os.X_OK):
        return [_glitchtip_secrets_dir_unwritable_result(path)]

    # Owned-and-writable is what matters; a chmod that fails here is harmless.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return [OpsResult(True, f"{path} exists and is writable")]


def _glitchtip_secrets_doctor_issues() -> list[str]:
    """`nyxgpt ops doctor` checks for the #3432 Linux bind-mount ownership
    failure mode: a `~/.nyxGPT/secrets` that exists but isn't writable by
    the current user (see `_ensure_glitchtip_secrets_dir`), and -- once the
    observability stack is actually up -- a missing GlitchTip token file,
    which leaves Grafana's GlitchTip datasource (glitchtip.yml) unable to
    authenticate even though Prometheus/Loki/Jaeger are fine.

    The second check only queries `_compose_stack_snapshot()` when Docker is
    on PATH, same guard `doctor()` already uses for the Cassandra container
    check -- avoids an unnecessary `docker compose ps` on a host without
    Docker at all.
    """
    issues: list[str] = []

    secrets_dir = _glitchtip_grafana_token_path().parent
    if secrets_dir.exists() and not os.access(secrets_dir, os.W_OK | os.X_OK):
        issues.append(
            f"{secrets_dir} exists but is not writable by {getpass.getuser()!r} "
            f"(run: sudo chown -R $(whoami) {secrets_dir} && nyxgpt ops install)"
        )

    if _which("docker") is not None and _compose_stack_snapshot().get("grafana") == "running":
        token_path = _glitchtip_grafana_token_path()
        if not token_path.exists():
            issues.append(
                f"Observability stack is up but {token_path} is missing -- Grafana's "
                "GlitchTip datasource can't authenticate (run: nyxgpt ops glitchtip-init)"
            )

    return issues


def _requirement_distribution_name(requirement: str) -> str:
    """Extract the distribution name from a PEP 508 requirement string.

    e.g. "opentelemetry-instrumentation-urllib>=0.45b0" ->
    "opentelemetry-instrumentation-urllib". Strips any environment marker
    (`; python_version ...`) and version specifier/extras.
    """
    without_marker = requirement.split(";", 1)[0].strip()
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", without_marker)
    return match.group(0) if match else without_marker


def _stale_venv_doctor_issues() -> list[str]:
    """`nyxgpt ops doctor` check for a Python venv that wasn't refreshed
    after a `git pull` added or bumped a declared dependency (#3487) -- the
    root cause behind a bare top-level import of a newly-added package (e.g.
    an OTel instrumentation) crashing every `nyxgpt` command with a raw
    `ModuleNotFoundError` instead of a targeted, actionable finding here.
    """
    pyproject_path = REPO_ROOT / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    try:
        declared = tomllib.loads(pyproject_path.read_text())["project"]["dependencies"]
    except Exception as e:
        logger.warning(
            "Failed to parse %s for the stale-venv doctor check: %s",
            pyproject_path,
            e,
            extra={"component": "ops"},
        )
        return []

    missing = []
    for requirement in declared:
        name = _requirement_distribution_name(requirement)
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)

    if not missing:
        return []

    return [
        "Installed environment is missing declared "
        f"dependenc{'y' if len(missing) == 1 else 'ies'}: {', '.join(sorted(missing))} "
        "-- your venv is likely stale after a pull (run: pip install -e .)"
    ]


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
    used, Docker isn't installed, or `error_tracking` is enabled in config but
    the container was torn down (`state == "absent"`, reported by self_heal's
    desired-state reconciliation) -- so a host with no GlitchTip never stalls
    `nyxgpt ops install`, and a standalone `nyxgpt ops glitchtip-init` run
    against a torn-down stack fails fast instead of polling out the full
    `timeout` for a container nothing in this call path starts. Otherwise
    waits out its health-check `start_period` (see docker-compose.yml), since
    a container freshly started by `_start_observability_stack` is not
    immediately reachable.
    """
    statuses = [s for s in self_heal.list_component_status() if s.service == "glitchtip"]
    if not statuses or statuses[0].state == "absent":
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
    derived `docker/config.docker.ini`.
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
    `docker/config.docker.ini`, whose `[error_tracking]` section carries
    hand-written documentation comments (seeded from its tracked `.example`
    template) that must survive `nyxgpt ops glitchtip-init` re-runs.
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
    the live `docker/config.docker.ini` is a derived, git-ignored artifact
    seeded from a tracked template, not a secrets file -- the DSN is a public
    key, safe to store there (see docs/self-healing.md) -- so its permissions
    are left untouched.

    Patches only the `dsn`/`enabled` lines in place (via `_patch_ini_value`)
    rather than round-tripping through `ConfigParser`, so any comments in
    `cfg_path` -- notably the documentation comments in
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


def _write_grafana_glitchtip_token(token: str) -> tuple[bool, OpsResult]:
    """Write `token` to `_glitchtip_grafana_token_path()` for Grafana's Infinity datasource.

    Returns `(changed, result)` -- `changed` is False when the file already
    holds this exact token, which callers use to skip an unnecessary Grafana
    restart (Grafana only re-reads provisioning files, including `$__file{}`
    targets, at startup).

    `_ensure_glitchtip_secrets_dir` runs earlier in `install()`/
    `_install_terraform_steps` specifically so this doesn't hit a
    root-owned bind-mount directory (#3432), but this still guards the
    write with its own try/except -- e.g. a standalone `nyxgpt ops
    glitchtip-init` run skips that preflight -- so a permission problem
    surfaces as this actionable `OpsResult` instead of an uncaught
    `PermissionError` traceback bubbling up through `_provision_glitchtip`.
    """
    path = _glitchtip_grafana_token_path()
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == token:
            return False, OpsResult(True, f"{path} already holds the current GlitchTip token")

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as e:
        result = _glitchtip_secrets_dir_unwritable_result(path.parent)
        return False, OpsResult(
            False,
            f"Cannot write GlitchTip token to {path}",
            f"{result.details}\n{type(e).__name__}: {e}",
        )
    return True, OpsResult(True, f"Wrote GlitchTip API token for Grafana to {path}")


def _grafana_container_healthy() -> bool:
    """Whether the `grafana` Compose container currently reports healthy."""
    for status in self_heal.list_component_status():
        if status.service == "grafana":
            return status.healthy
    return False


def _wait_for_grafana_healthy(timeout: float = 120.0, poll_interval: float = 3.0) -> bool:
    """Poll until the `grafana` container reports healthy, or `timeout` elapses.

    Mirrors `_wait_for_glitchtip_healthy` (#3432): returns False immediately
    (no polling) if `grafana` isn't part of the currently running Compose
    stack at all, so a host without the observability stack never stalls.
    Otherwise waits out Grafana's healthcheck `start_period` (see
    docker-compose.yml's `grafana` service) since a container just
    (re)started is not immediately reachable -- and, critically, a container
    stuck crash-looping (#3538, e.g. an alerting-provisioning file Grafana
    can't validate) never reports healthy, so this returns False instead of
    hanging forever, letting callers surface that as an actionable failure
    instead of reporting a restart as OK when Grafana never actually comes
    back up.
    """
    statuses = [s for s in self_heal.list_component_status() if s.service == "grafana"]
    if not statuses or statuses[0].state == "absent":
        return False
    if statuses[0].healthy:
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if _grafana_container_healthy():
            return True
    return False


def _restart_grafana_if_running(reason: str = "the new GlitchTip token") -> OpsResult:
    """Restart the `grafana` Compose container if it's currently running.

    Grafana only reads `$__file{}` provisioning targets (the GlitchTip
    Infinity token, see `_write_grafana_glitchtip_token`, and the Slack
    alerting webhook, see `_write_grafana_slack_webhook_secret`) at startup,
    so a freshly (re)written secret needs this to actually take effect.

    Waits for Grafana to report healthy again before returning (#3538) --
    without this, a restart that leaves Grafana crash-looping (e.g. a broken
    alerting-provisioning file) was previously reported as a plain "OK,
    restarted", with the crash loop only surfacing later, misleadingly, as
    an unrelated-looking credential-verify failure.
    """
    if not _compose_available():
        return OpsResult(True, "Skipped Grafana restart (Docker not found)")

    running = _compose_stack_snapshot()
    if running.get("grafana") != "running":
        return OpsResult(True, "Skipped Grafana restart (not running)")

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "restart", "grafana"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return OpsResult(False, "Failed to restart Grafana", (cp.stderr or cp.stdout or "").strip())

    if not _wait_for_grafana_healthy():
        return OpsResult(
            False,
            f"Restarted Grafana to pick up {reason}, but it never became healthy again",
            "Check `nyxgpt ops status` (a compose service stuck `restarting` is the tell) "
            "and `nyxgpt ops logs grafana` for the boot error.",
        )
    return OpsResult(True, f"Restarted Grafana to pick up {reason}")


def _slack_webhook_secret_path() -> Path:
    """Where the Slack incoming-webhook URL is written for Grafana's alerting
    contact point to read (#3466) -- see
    docker/grafana/provisioning/alerting/contact-points.yml's `$__file{}`
    reference. Lives in the same `~/.nyxGPT/secrets` directory as the
    GlitchTip Grafana token (`_glitchtip_grafana_token_path`), so the
    existing `_ensure_glitchtip_secrets_dir` preflight (#3432) already
    guards this write against the root-owned bind-mount directory failure
    mode too.

    Computed fresh on every call, not a module-level constant, so tests can
    monkeypatch `Path.home`.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "slack-webhook-url"


# Written to the secret file in place of an empty string when [monitoring]
# slack_webhook_url is unset (#3538). Grafana's alerting-provisioning
# validator requires a Slack integration to have a non-empty url/token/
# recipient -- an empty url doesn't mean "configured but broken", it fails
# validation identically to a missing one and crash-loops the whole Grafana
# container. This placeholder is syntactically a well-formed Slack webhook
# URL (satisfies validation, so Grafana boots) but not a real one -- delivery
# to it fails at send time, visible under Alerting -> Contact points -> Test,
# which is the "alerts still fire, Slack delivery silently fails" behavior
# #3466 actually intended.
GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL = (
    "https://hooks.slack.com/services/UNCONFIGURED/UNCONFIGURED/UNCONFIGURED"
)


def _write_grafana_slack_webhook_secret(url: str) -> tuple[bool, OpsResult]:
    """Write `url` to `_slack_webhook_secret_path()` for Grafana's
    `nyxgpt-slack` contact point -- `GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL`
    when `url` is empty/unset.

    Never writes an empty string, even though `url` may be `""` -- the
    contact point's `$__file{}` reference must resolve to a non-empty,
    syntactically-valid webhook URL at Grafana startup, or Grafana's
    alerting-provisioning validator refuses to boot the container entirely
    (#3538: confirmed by booting grafana/grafana:13.1.1 against this exact
    provisioning with an empty secret file -- it crash-loops, it does not
    degrade to "Slack delivery fails silently" as originally intended by
    #3466). The placeholder preserves that original intent instead: Grafana
    boots, and only the (already-broken, unconfigured) Slack delivery fails,
    visibly, under Grafana's Alerting -> Contact points -> Test.

    Returns `(changed, result)` -- `changed` is False when the file already
    holds this exact value, which callers use to skip an unnecessary
    Grafana restart (mirrors `_write_grafana_glitchtip_token`).
    """
    path = _slack_webhook_secret_path()
    resolved = url.strip() or GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == resolved:
            return False, OpsResult(True, f"{path} already holds the current Slack webhook URL")

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(resolved, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as e:
        result = _glitchtip_secrets_dir_unwritable_result(path.parent)
        return False, OpsResult(
            False,
            f"Cannot write Slack webhook URL to {path}",
            f"{result.details}\n{type(e).__name__}: {e}",
        )
    if url.strip():
        return True, OpsResult(True, f"Wrote Slack webhook URL for Grafana at {path}")
    return True, OpsResult(
        True,
        f"Wrote placeholder Slack webhook URL for Grafana at {path} "
        "(no [monitoring] slack_webhook_url configured)",
    )


def _sync_grafana_slack_webhook_secret(cfg_path: Path | None = None) -> list[OpsResult]:
    """Provision Grafana's Slack contact point secret from config.ini's
    `[monitoring] slack_webhook_url` (#3466).

    Run from both `install()` and `env_sync()` so the webhook is picked up
    whether the user runs the full native install or just the Compose-only
    Quickstart's `nyxgpt ops env-sync` before `docker compose up`.
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [OpsResult(True, "Skipped Slack webhook sync (no config.ini yet)")]
    cfg = load_config(cfg_path)

    url = get_monitoring_slack_webhook_url(cfg)
    changed, write_result = _write_grafana_slack_webhook_secret(url)
    results = [write_result]
    if changed:
        results.append(_restart_grafana_if_running("the new Slack webhook URL"))
    return results


def _send_grafana_test_alert(grafana_ui_url: str, grafana_admin_password: str) -> OpsResult:
    """POST a synthetic alert straight into Grafana's embedded Alertmanager
    (#3466) -- the same API real firing rules route through, so this
    exercises the whole pipeline end to end: it shows up in Grafana's
    Alerting -> Fired alerts UI, and reaches Slack via the nyxgpt-slack
    contact point/notification policy if a webhook is configured, exactly
    like a genuine CPU/memory/disk/self-heal/canary alert would.

    `endsAt` is 5 minutes out so a forgotten test alert resolves itself
    instead of paging anyone indefinitely.
    """
    now = datetime.now(UTC)
    payload = [
        {
            "labels": {"alertname": "NyxGPTAlertTest", "severity": "warning"},
            "annotations": {
                "summary": "Test alert triggered by `nyxgpt ops alert-test`",
                "description": (
                    "Synthetic alert used to verify the Grafana alerting pipeline (rules -> "
                    "notification policy -> Slack contact point) end to end. Resolves itself "
                    "automatically."
                ),
            },
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(minutes=5)).isoformat(),
        }
    ]
    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            resp = client.post("/api/alertmanager/grafana/api/v2/alerts", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        return OpsResult(
            False,
            "Failed to send test alert to Grafana's Alertmanager API",
            f"{type(e).__name__}: {e}",
        )
    return OpsResult(
        True,
        "Sent test alert to Grafana -- check the Alerting UI's Fired alerts tab "
        "(and Slack, if a webhook is configured) within a minute or two.",
    )


def alert_test(_args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops alert-test`.

    Fires a synthetic alert directly into Grafana's embedded Alertmanager --
    the acceptance test for the alerting pipeline: confirms rules ->
    notification policy -> Slack contact point are wired correctly without
    waiting for a real CPU/memory/disk/self-heal/canary threshold breach.
    No-ops with a clear, actionable message if monitoring is disabled, unset
    up, or Grafana isn't reachable.

    Returns 0 on success, else 2.
    """
    from nyxgpt.config import load_config

    logger.info("ops: alert-test starting", extra={"component": "ops", "action": "alert-test"})

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg_path.exists():
        results = [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]
    else:
        cfg = load_config(cfg_path)
        monitoring = get_monitoring_config(cfg)
        if not monitoring["enabled"]:
            results = [
                OpsResult(
                    False,
                    "Monitoring is disabled",
                    "Set [monitoring] enabled = true in config.ini, then run `nyxgpt ops "
                    "install` (or start the `monitoring` Compose profile) before testing "
                    "alerts.",
                )
            ]
        else:
            grafana_admin_password = _grafana_admin_password(cfg)
            results = [
                _send_grafana_test_alert(monitoring["grafana_ui_url"], grafana_admin_password)
            ]

    ok = _emit_results("alert-test", results)
    logger.info(
        "ops: alert-test %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "alert-test", "ok": ok},
    )
    return 0 if ok else 2


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

    results.append(_write_error_tracking_dsn(native_cfg_path, dsn, chmod_600=True))
    results.append(_write_error_tracking_dsn(COMPOSE_CONFIG_FILE, dsn, chmod_600=False))

    token_changed, token_write_result = _write_grafana_glitchtip_token(token)
    results.append(token_write_result)
    if token_changed:
        results.append(_restart_grafana_if_running())

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
