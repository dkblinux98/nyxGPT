"""Self-heal watchdog for the local Docker Compose deployed stack.

Periodically checks the health of every container in the `docker-compose.yml`
project (whichever profiles are currently up: the core stack plus any of
monitoring/logging/tracing/errors) via `docker compose ps` and restarts any
that are unhealthy or stopped, with capped consecutive-restart backoff so a
genuinely broken component doesn't get restart-looped forever.

This targets Compose, the documented primary local "full stack" deploy path
(see docs/deployment.md) -- Terraform intentionally only manages the core
four services (see docs/terraform.md) and the Kubernetes path already has
its own recovery mechanisms (deploy.py blue/green, canary.py auto-rollback).

State (whether the watchdog is enabled, per-service restart counts, and the
recent event history shown on the SRE/admin dashboard) is persisted to
`~/.nyxGPT/self_heal_state.json`, mirroring deploy.py/canary.py's
`deploy_state.json`/`canary_state.json`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import metrics as prom_metrics

logger = logging.getLogger(__name__)

# Repo root: .../nyxGPT/src/nyxgpt/self_heal.py -> parents[2] is repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_compose_file() -> Path:
    """Resolve the docker-compose.yml the watchdog targets.

    Three layouts, three answers:
    - api container: `self_heal.py` lives under site-packages with no repo
      checkout; the compose file is bind-mounted in and its in-container path
      passed via NYXGPT_COMPOSE_FILE (see the `api` service in
      docker-compose.yml and docs/self-healing.md).
    - bare checkout (dev machine, `nyxgpt` running from the repo/venv): the
      repo root computed above is correct.
    - brew-installed native service: the module lives in the Homebrew Cellar,
      so neither of the above applies; `nyxgpt ops install` records the repo's
      compose path in config.ini `[paths] compose_file`, read here.
    """
    override = os.environ.get("NYXGPT_COMPOSE_FILE", "").strip()
    if override:
        return Path(override)

    repo_compose = REPO_ROOT / "docker-compose.yml"
    if repo_compose.exists():
        return repo_compose

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if cfg_path.exists():
        parser = ConfigParser()
        parser.read(cfg_path)
        configured = parser.get("paths", "compose_file", fallback="").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.exists():
                return configured_path

    return repo_compose


COMPOSE_FILE = _resolve_compose_file()

EVENT_LOG_LIMIT = 100
DEFAULT_CHECK_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_CONSECUTIVE_RESTARTS = 5
DEFAULT_BACKOFF_SECONDS = 30.0

# Runs-to-completion services (e.g. one-shot DB migrations) that are never
# "down" in the self-heal sense -- they're expected to exit 0 and stay exited.
ONE_SHOT_SERVICES = {"glitchtip-migrate"}


@dataclass(frozen=True)
class ComponentStatus:
    """A single component's status, as read from `docker compose ps`."""

    service: str
    container: str
    state: str
    health: str
    healthy: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON responses."""
        return {
            "service": self.service,
            "container": self.container,
            "state": self.state,
            "health": self.health,
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class HealResult:
    """Outcome of a single restart/log-fetch action against a component."""

    ok: bool
    message: str
    details: str = ""


@dataclass(frozen=True)
class HealEvent:
    """A single recorded self-heal action, as shown in the dashboard event log."""

    ts: float
    service: str
    reason: str
    action: str
    ok: bool
    restart_count: int
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON responses/state storage."""
        return {
            "ts": self.ts,
            "service": self.service,
            "reason": self.reason,
            "action": self.action,
            "ok": self.ok,
            "restart_count": self.restart_count,
            "message": self.message,
        }


def _which(prog: str) -> str | None:
    """Return the resolved path of `prog` on PATH, or None if not found."""
    return shutil.which(prog)


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text instead of raising on failure."""
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)


def _state_path() -> Path:
    """Path to the on-disk self-heal state file (`~/.nyxGPT/self_heal_state.json`)."""
    return Path.home() / ".nyxGPT" / "self_heal_state.json"


_state_lock = threading.Lock()


def _default_state() -> dict[str, Any]:
    """Build the default (disabled, empty history) self-heal state dict."""
    return {"enabled": False, "events": [], "restart_counts": {}, "last_restart_ts": {}}


def _load_state() -> dict[str, Any]:
    """Load self-heal state from disk, merging in any missing default keys.

    Returns the default state if the file doesn't exist or fails to parse.
    """
    path = _state_path()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = _default_state()
            merged.update(data)
            return merged
    except Exception:
        pass
    return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    """Persist `state` to disk as JSON, creating the parent directory if needed."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_enabled() -> bool:
    """Whether the self-heal watchdog is currently allowed to take action."""
    with _state_lock:
        return bool(_load_state().get("enabled", False))


def seed_enabled_default(default_enabled: bool) -> None:
    """Seed the runtime enabled flag from config.ini on first run only.

    Once `~/.nyxGPT/self_heal_state.json` exists, the dashboard toggle
    (`set_enabled`) is the source of truth and config.ini's `[self_heal]
    enabled` is only the initial value for a fresh install.
    """
    with _state_lock:
        if _state_path().exists():
            return
        state = _default_state()
        state["enabled"] = default_enabled
        _save_state(state)


def set_enabled(enabled: bool) -> bool:
    """Enable/disable the watchdog and return the resulting state."""
    with _state_lock:
        state = _load_state()
        state["enabled"] = enabled
        _save_state(state)
        return enabled


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` most recent heal events, newest last.

    `limit` is clamped between 1 and `EVENT_LOG_LIMIT`, the number of events
    actually retained in state.
    """
    with _state_lock:
        events = _load_state().get("events", [])
    bounded = max(1, min(limit, EVENT_LOG_LIMIT))
    return list(events[-bounded:])


def list_component_status() -> list[ComponentStatus]:
    """Query `docker compose ps -a` for every container the project has created.

    Only reports containers that actually exist -- an opt-in profile
    (monitoring/logging/tracing/errors) that was never started isn't
    reported as "down", it's simply absent from the result.
    """
    if _which("docker") is None:
        return []
    try:
        cp = _run(["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "-a", "--format", "json"])
    except Exception as e:
        logger.warning("self-heal: failed to query docker compose ps: %s", e)
        return []
    if cp.returncode != 0:
        return []

    statuses: list[ComponentStatus] = []
    for line in (cp.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        service = data.get("Service", "")
        if not service or service in ONE_SHOT_SERVICES:
            continue
        state = data.get("State", "")
        health = data.get("Health", "")
        healthy = state == "running" and health in ("", "healthy")
        statuses.append(
            ComponentStatus(
                service=service,
                container=data.get("Name", ""),
                state=state,
                health=health,
                healthy=healthy,
            )
        )
    return statuses


def _record_health_check(statuses: list[ComponentStatus]) -> int:
    """Log each component's health-check result and update the unhealthy-count gauge.

    Logged at DEBUG (routine, high-frequency per-component data -- set
    `[logging] level = DEBUG` in config.ini to see it) rather than INFO, so a
    healthy stack doesn't spam the log file every `check_interval_seconds`.
    Returns the number of unhealthy components, for callers that also need
    the count (e.g. `heal_now`'s pass summary).
    """
    unhealthy = 0
    for s in statuses:
        if not s.healthy:
            unhealthy += 1
        logger.debug(
            "self-heal: health check %s healthy=%s state=%s health=%s",
            s.service,
            s.healthy,
            s.state,
            s.health or "n/a",
            extra={
                "component": "self_heal",
                "service": s.service,
                "healthy": s.healthy,
                "state": s.state,
                "health": s.health,
            },
        )
    prom_metrics.SELFHEAL_UNHEALTHY_COMPONENTS.set(unhealthy)
    return unhealthy


def restart_component(service: str) -> HealResult:
    """Restart a single Compose service: `docker compose restart <service>`."""
    if _which("docker") is None:
        return HealResult(False, "docker not found; cannot restart component")
    try:
        cp = _run(["docker", "compose", "-f", str(COMPOSE_FILE), "restart", service], timeout=120.0)
    except Exception as e:
        return HealResult(False, f"Failed to restart {service}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to restart {service}", details.strip())
    return HealResult(True, f"Restarted {service}")


def component_logs(service: str, *, tail: int = 200) -> HealResult:
    """Fetch recent logs for a single Compose service: `docker compose logs <service>`.

    Backs `nyxgpt ops logs` -- the wrapped way to read a container's output
    (e.g. the GlitchTip registration confirmation link the `errors` profile
    prints to stdout via its console email backend) without the user needing
    to run a raw `docker`/`docker compose` command themselves.
    """
    if _which("docker") is None:
        return HealResult(False, "docker not found; cannot fetch logs")
    try:
        cp = _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "logs",
                "--no-color",
                "--tail",
                str(tail),
                service,
            ],
            timeout=30.0,
        )
    except Exception as e:
        return HealResult(False, f"Failed to fetch logs for {service}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to fetch logs for {service}", details.strip())
    return HealResult(
        True, f"Fetched last {tail} log line(s) for {service}", (cp.stdout or "").strip()
    )


def heal_now(
    service: str | None = None,
    *,
    max_consecutive_restarts: int = DEFAULT_MAX_CONSECUTIVE_RESTARTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> dict[str, Any]:
    """Run one heal pass.

    With `service=None` (the watchdog's normal mode): checks every monitored
    component and restarts only the ones that are unhealthy/stopped,
    honoring per-service backoff and `max_consecutive_restarts` so a
    genuinely broken component stops being restarted after enough failed
    attempts rather than looping forever.

    With `service` set (the dashboard's manual "heal now" button): restarts
    that one component immediately, bypassing the health check and backoff
    -- an explicit operator action shouldn't be blocked by the same guards
    that protect the automatic loop.

    Returns {"checked": [...], "healed": [...]}, and additionally an
    "error" key if an explicit `service` isn't currently a known container.
    """
    statuses = list_component_status()
    now = time.time()
    unhealthy_count = _record_health_check(statuses)

    targets = statuses
    if service is not None:
        targets = [s for s in statuses if s.service == service]
        if not targets:
            logger.warning(
                "self-heal: heal-now requested for unknown/not-running component %s",
                service,
                extra={"component": "self_heal", "service": service},
            )
            return {
                "checked": [],
                "healed": [],
                "error": f"Unknown or not-running component: {service}",
            }

    checked = [s.to_dict() for s in targets]
    healed: list[dict[str, Any]] = []
    manual = service is not None

    with _state_lock:
        state = _load_state()
        restart_counts: dict[str, int] = state.setdefault("restart_counts", {})
        last_restart_ts: dict[str, float] = state.setdefault("last_restart_ts", {})
        events: list[dict[str, Any]] = state.setdefault("events", [])

        for status in targets:
            if status.healthy and not manual:
                if restart_counts.get(status.service, 0) != 0:
                    logger.info(
                        "self-heal: %s recovered, resetting consecutive-restart count",
                        status.service,
                        extra={"component": "self_heal", "service": status.service},
                    )
                restart_counts[status.service] = 0
                prom_metrics.SELFHEAL_RESTART_COUNT.labels(service=status.service).set(0)
                continue

            count = restart_counts.get(status.service, 0)
            last_ts = last_restart_ts.get(status.service, 0.0)

            if not manual:
                if count >= max_consecutive_restarts:
                    logger.warning(
                        "self-heal: giving up on %s, %d consecutive restart(s) already failed"
                        " (max=%d)",
                        status.service,
                        count,
                        max_consecutive_restarts,
                        extra={
                            "component": "self_heal",
                            "service": status.service,
                            "restart_count": count,
                            "max_consecutive_restarts": max_consecutive_restarts,
                        },
                    )
                    continue
                remaining = backoff_seconds - (now - last_ts)
                if remaining > 0:
                    logger.debug(
                        "self-heal: skipping restart of %s, backoff active (%.1fs remaining)",
                        status.service,
                        remaining,
                        extra={
                            "component": "self_heal",
                            "service": status.service,
                            "backoff_remaining_seconds": remaining,
                        },
                    )
                    continue

            reason = (
                "manual heal-now"
                if manual
                else f"state={status.state} health={status.health or 'n/a'}"
            )
            logger.info(
                "self-heal: attempting restart of %s (reason=%s, attempt=%d)",
                status.service,
                reason,
                count + 1,
                extra={
                    "component": "self_heal",
                    "service": status.service,
                    "reason": reason,
                    "attempt": count + 1,
                    "manual": manual,
                },
            )
            result = restart_component(status.service)

            new_count = count + 1
            restart_counts[status.service] = new_count
            last_restart_ts[status.service] = now

            prom_metrics.SELFHEAL_RESTARTS_TOTAL.labels(
                service=status.service, result="ok" if result.ok else "failed"
            ).inc()
            prom_metrics.SELFHEAL_RESTART_COUNT.labels(service=status.service).set(new_count)
            if result.ok:
                prom_metrics.SELFHEAL_LAST_RECOVERY_TIMESTAMP.labels(service=status.service).set(
                    now
                )

            log = logger.info if result.ok else logger.error
            log(
                "self-heal: restart of %s %s (restart_count=%d): %s",
                status.service,
                "succeeded" if result.ok else "failed",
                new_count,
                result.message,
                extra={
                    "component": "self_heal",
                    "service": status.service,
                    "ok": result.ok,
                    "restart_count": new_count,
                    "reason": reason,
                },
            )

            event = HealEvent(
                ts=now,
                service=status.service,
                reason=reason,
                action="restart",
                ok=result.ok,
                restart_count=new_count,
                message=result.message,
            )
            events.append(event.to_dict())
            healed.append(event.to_dict())

        state["events"] = events[-EVENT_LOG_LIMIT:]
        _save_state(state)

    logger.info(
        "self-heal: heal pass complete (checked=%d, unhealthy=%d, healed=%d, manual=%s)",
        len(checked),
        unhealthy_count,
        len(healed),
        manual,
        extra={
            "component": "self_heal",
            "checked": len(checked),
            "unhealthy": unhealthy_count,
            "healed": len(healed),
            "manual": manual,
        },
    )

    return {"checked": checked, "healed": healed}


class Watchdog:
    """Background thread that periodically calls `heal_now()` when enabled."""

    def __init__(
        self,
        interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        max_consecutive_restarts: int = DEFAULT_MAX_CONSECUTIVE_RESTARTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        """Configure the watchdog's check cadence and restart limits.

        Does not start the background thread; call `start()` for that.
        """
        self.interval_seconds = interval_seconds
        self.max_consecutive_restarts = max_consecutive_restarts
        self.backoff_seconds = backoff_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background heal-check loop in a daemon thread.

        No-op (with a warning logged) if the loop is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Self-heal watchdog already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            "Self-heal watchdog started (interval=%.0fs)",
            self.interval_seconds,
            extra={"component": "self_heal"},
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background loop to stop and join it (waiting up to `timeout` seconds)."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Self-heal watchdog stopped", extra={"component": "self_heal"})

    def _loop(self) -> None:
        """Background loop: call `heal_now()` on each interval while enabled.

        Runs until `stop()` is called. Exceptions from a heal pass are
        logged and swallowed so one failed pass doesn't kill the thread.
        """
        while not self._stop_event.is_set():
            try:
                if is_enabled():
                    heal_now(
                        max_consecutive_restarts=self.max_consecutive_restarts,
                        backoff_seconds=self.backoff_seconds,
                    )
            except Exception:
                logger.exception("self-heal: error during automatic heal pass")
            self._stop_event.wait(self.interval_seconds)


_watchdog: Watchdog | None = None


def get_watchdog() -> Watchdog:
    """Return the process-wide `Watchdog` singleton, creating it on first call."""
    global _watchdog
    if _watchdog is None:
        _watchdog = Watchdog()
    return _watchdog


def status() -> dict[str, Any]:
    """Aggregate status for `GET /api/v1/self-heal/status`."""
    components = list_component_status()
    unhealthy_count = _record_health_check(components)
    return {
        "enabled": is_enabled(),
        "components": [c.to_dict() for c in components],
        "unhealthy_count": unhealthy_count,
        "events": recent_events(20),
    }


__all__ = [
    "COMPOSE_FILE",
    "ComponentStatus",
    "HealResult",
    "HealEvent",
    "Watchdog",
    "is_enabled",
    "seed_enabled_default",
    "set_enabled",
    "recent_events",
    "list_component_status",
    "restart_component",
    "component_logs",
    "heal_now",
    "get_watchdog",
    "status",
]
