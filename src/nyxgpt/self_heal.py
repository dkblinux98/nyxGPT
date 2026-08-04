"""Self-heal watchdog for the local deployed stack (native, Compose, Terraform, Kubernetes).

Periodically checks the health of every core app component -- `api`, `web`,
`ollama`, `cassandra` -- plus any Compose container that's currently up
(the core stack, if deployed via Compose, plus any of
monitoring/logging/tracing/errors) and restarts anything unhealthy or
stopped, with capped consecutive-restart backoff so a genuinely broken
component doesn't get restart-looped forever.

Four deployment modes are covered:
- **Docker Compose**: containers listed via `docker compose ps`, healed via
  `docker compose restart <service>`.
- **Native/local-first** (the default local deployment -- see
  `nyxgpt ops install`): `api`/`web`/`ollama` run as Homebrew services on
  macOS or systemd --user units on Linux (#3508), and `cassandra` runs as
  the plain (non-Compose) `nyxgpt-cassandra` Docker container. Healed via
  `brew services restart <name>` / `systemctl --user restart <unit>` /
  `docker restart <container>` -- the same mechanisms `nyxgpt ops restart`
  uses.
- **Terraform** (`nyxgpt ops install --terraform --local`): `ollama`/
  `cassandra`/`api`/`web` run as the plain (non-Compose) `nyxgpt-tf-*`
  Docker containers defined in `terraform/main.tf`. Checked/healed directly
  via `docker ps`/`docker restart <container>`, the same way native mode's
  `nyxgpt-cassandra` is -- see `_list_terraform_component_status`.
- **Kubernetes** (`nyxgpt ops install --kubernetes --local`): the `nyxgpt-api`
  Deployments' Pods (stable/canary, see `k8s/`), checked via
  `kubectl get pods` and healed via `kubectl delete pod` (the Deployment's
  controller recreates it) -- see `_list_kubernetes_component_status`. This
  is on top of, not instead of, Kubernetes' own kubelet liveness-probe
  restarts and `canary.py`'s metrics-gated rollout + auto-rollback; it adds
  the same "torn-down/stuck" recovery + unified dashboard visibility the
  other three modes get.

A component reported by more than one mode at once -- e.g. a stopped native
`nyxgpt-cassandra` container deliberately left in place after switching to
Terraform mode, see #3428 -- is resolved to whichever mode is actually
running it; only when none of them is running does a fixed mode priority
break the tie. This is `_resolve_core_component_conflicts` (see
`list_component_status`), so no two modes double-heal (or collide on) the
same component, and an inactive mode's leftover container/service never
shadows the active mode's real one.

**Desired state for the observability profiles**: `docker compose ps`
only reports containers that exist -- a profile that was torn down
entirely (e.g. `nyxgpt ops down`) has no containers to be "unhealthy",
so watching states alone can't recover it. For `monitoring`/
`log_aggregation`/`tracing`/`error_tracking`, this module additionally
checks config.ini directly (`_enabled_observability_profiles`): if a
profile's flag is on, its Compose services are desired-running, and an
absent one is healed the same as an unhealthy one (`docker compose up -d
<service>` instead of `restart`, since there's no container to restart).
Disabling the feature flag (not just tearing the containers down) is the
supported way to keep a profile off with auto-heal enabled.

**Desired state for the core components**: whether `api`/`web`/`ollama`/
`cassandra` should be running is an operator decision (`nyxgpt ops down`/
`stop`), not a config.ini feature flag, so it's tracked separately: an
*intentional-stop registry* (`mark_intentionally_stopped`/
`clear_intentionally_stopped`, persisted alongside `enabled`) records which
core components an operator deliberately stopped. `list_component_status`
flags those `desired=False` the same way a disabled observability profile
is, so the automatic heal pass leaves them alone regardless of which
deployment mode stopped them (native `brew services`/`docker stop`, or a
Compose `docker compose stop`) -- this is what lets `nyxgpt ops down`/`stop`
avoid fighting with an *enabled* watchdog instead of needing to disable it
globally (see #3406). `nyxgpt ops install`/`restart`/`up` of a component
clears its marker, since bringing it back up is itself the "this is desired
again" signal.

State (whether the watchdog is enabled, per-service restart counts, the
intentional-stop registry, and the recent event history shown on the
SRE/admin dashboard) is persisted to `~/.nyxGPT/self_heal_state.json`,
mirroring canary.py's `canary_state.json`.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from configparser import ConfigParser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from nyxgpt import metrics as prom_metrics
from nyxgpt.config import (
    get_error_tracking_enabled,
    get_log_aggregation_enabled,
    get_monitoring_enabled,
    get_tracing_enabled,
    load_config,
)
from nyxgpt.logging import get_correlation_id, get_log_dir, mint_correlation_id

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

# Maps a core native component to its Homebrew service name, for native/
# local-first mode health-checks/heals. Mirrors ops.NATIVE_BREW_SERVICES --
# kept as a separate copy here (rather than importing ops.py) since ops.py
# already imports this module (see the module docstring); ops.py is the
# place that actually installs/creates these services, this module only
# ever restarts what's already there.
NATIVE_BREW_SERVICES: dict[str, str] = {
    "api": "nyxgpt-api",
    "web": "nyxgpt-web",
    "ollama": "ollama",
}
# Linux twin of NATIVE_BREW_SERVICES -- mirrors ops.NATIVE_SYSTEMD_SERVICES
# for the same reason NATIVE_BREW_SERVICES is a separate copy here (#3508).
NATIVE_SYSTEMD_SERVICES: dict[str, str] = {
    "api": "nyxgpt-api",
    "web": "nyxgpt-web",
    "ollama": "nyxgpt-ollama",
}
# Mirrors ops.CASSANDRA_CONTAINER_NAME -- the one Docker-managed piece of a
# native/local-first install.
NATIVE_CASSANDRA_CONTAINER = "nyxgpt-cassandra"


def _is_macos() -> bool:
    """True if running on macOS -- the Homebrew services native path."""
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    """True if running on Linux -- the systemd --user native path (#3508)."""
    return platform.system() == "Linux"


# Maps a core component to its Terraform-managed container name (see
# terraform/main.tf). Mirrors ops.TERRAFORM_CONTAINERS -- kept as a separate
# copy here for the same reason as NATIVE_BREW_SERVICES above.
TERRAFORM_CONTAINERS: dict[str, str] = {
    "ollama": "nyxgpt-tf-ollama",
    "cassandra": "nyxgpt-tf-cassandra",
    "api": "nyxgpt-tf-api",
    "web": "nyxgpt-tf-web",
}

# Namespace and label selector for the Kubernetes-managed `nyxgpt-api` Pods
# (stable/canary both use `app=nyxgpt-api-canary-pool` -- see
# k8s/deployment-stable.yaml / deployment-canary.yaml). Mirrors ops.K8S_NAMESPACE.
K8S_NAMESPACE = "nyxgpt"
K8S_POD_LABEL_SELECTOR = "app=nyxgpt-api-canary-pool"

# Docker Compose profiles that make up the opt-in observability suite.
# Mirrors ops.OBSERVABILITY_PROFILES -- kept as a separate copy (rather than
# importing ops.py) for the same reason as NATIVE_BREW_SERVICES above: ops.py
# already imports this module.
OBSERVABILITY_PROFILES: list[str] = ["monitoring", "logging", "tracing", "errors"]

# Core app services in docker-compose.yml, excluded from the desired-state
# check below -- whether the core stack itself should be running is a
# deployment-mode question (native vs. Compose), not a config feature flag,
# and is out of scope here (see #3348 for native core-component coverage).
# Mirrors ops.CORE_APP_SERVICES.
CORE_APP_SERVICES: frozenset[str] = frozenset({"nyxgpt", "ollama", "cassandra", "api", "web"})

# Maps a config.ini section that gates an observability profile to the
# Compose profile name it controls. Two of the four differ from their
# section name (`log_aggregation` -> `logging`, `error_tracking` ->
# `errors`) -- that split predates this module (see config.py's
# get_log_aggregation_enabled/get_error_tracking_enabled) and is mirrored
# here rather than changed.
OBSERVABILITY_PROFILE_SECTIONS: dict[str, str] = {
    "monitoring": "monitoring",
    "log_aggregation": "logging",
    "tracing": "tracing",
    "error_tracking": "errors",
}


@dataclass(frozen=True)
class ComponentStatus:
    """A single component's status, from a Compose, native, Terraform, or Kubernetes check.

    `source` is `"compose"` (the default, for `docker compose ps`-derived
    entries), `"native"` (a Homebrew service or the native `nyxgpt-cassandra`
    container, checked directly rather than through Compose), `"terraform"`
    (a `nyxgpt-tf-*` container, checked directly via `docker ps`), or
    `"kubernetes"` (a `nyxgpt-api` Pod, checked via `kubectl get pods`).

    `desired` is `True` unless this is a *present* Compose container whose
    observability profile is currently disabled in config.ini (e.g. someone
    turned `monitoring` off via the config wizard, which stops but doesn't
    remove its containers -- see `list_component_status`). It's `True` for
    everything else, including a `state="absent"` entry (absent-but-desired
    is exactly what makes it desired) -- `heal_now` skips automatic (not
    manual) healing for `desired=False` components, so a deliberate
    feature-flag disable isn't undone by the next auto-heal pass.

    `note` is an informational annotation, empty unless another mode's
    entry for this same component was shadowed by this one (see
    `_resolve_core_component_conflicts` / #3428) -- e.g. a stopped native
    `nyxgpt-cassandra` container left in place while Terraform mode is
    actually running Cassandra. It never affects `healthy`/`state`/`source`,
    which always describe the winning (active) entry only.
    """

    service: str
    container: str
    state: str
    health: str
    healthy: bool
    source: str = "compose"
    desired: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON responses."""
        return {
            "service": self.service,
            "container": self.container,
            "state": self.state,
            "health": self.health,
            "healthy": self.healthy,
            "source": self.source,
            "desired": self.desired,
            "note": self.note,
        }


@dataclass(frozen=True)
class HealResult:
    """Outcome of a single restart/log-fetch action against a component."""

    ok: bool
    message: str
    details: str = ""


@dataclass(frozen=True)
class HealEvent:
    """A single recorded self-heal action, as shown in the dashboard event log.

    `evidence` captures the raw probe data that triggered the decision
    (source, state, health, threshold values at decision time) so an
    operator can read *why* a heal fired from the event itself instead of
    having to reproduce the failure (#3415 gap 7, see #3381).
    """

    ts: float
    service: str
    reason: str
    action: str
    ok: bool
    restart_count: int
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""

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
            "evidence": self.evidence,
            "correlation_id": self.correlation_id,
        }


def _which(prog: str) -> str | None:
    """Return the resolved path of `prog` on PATH, or None if not found."""
    return shutil.which(prog)


def _run(
    cmd: list[str], timeout: float = 30.0, *, expected: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text instead of raising on failure.

    Non-zero exits are logged with the command and a stderr tail so a failed
    probe/restart action is visible in Loki even when the caller only checks
    `returncode` (#3415 gap 5). Pass `expected=True` for read-only probes where
    a non-zero exit is a normal outcome, to log at DEBUG instead of WARNING.
    """
    result = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        level = logging.DEBUG if expected else logging.WARNING
        logger.log(
            level,
            f"Subprocess exited non-zero (rc={result.returncode}): {' '.join(cmd)}",
            extra={
                "component": "self_heal",
                "cmd": cmd,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-2000:] if result.stderr else "",
            },
        )
    return result


def _state_path() -> Path:
    """Path to the on-disk self-heal state file (`~/.nyxGPT/self_heal_state.json`)."""
    return Path.home() / ".nyxGPT" / "self_heal_state.json"


_state_lock = threading.Lock()


def _default_state() -> dict[str, Any]:
    """Build the default (disabled, empty history) self-heal state dict."""
    return {
        "enabled": False,
        "events": [],
        "restart_counts": {},
        "last_restart_ts": {},
        "intentionally_stopped": [],
    }


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


def is_intentionally_stopped(component: str) -> bool:
    """Whether `component` was deliberately stopped by an operator (`nyxgpt ops down`/`stop`).

    This is separate from `enabled` -- it's per-component, not a global
    kill switch -- so an armed watchdog can keep healing genuine crashes of
    every *other* component while leaving this one alone. See the module
    docstring's "Desired state for the core components" section.
    """
    with _state_lock:
        return component in set(_load_state().get("intentionally_stopped", []))


def mark_intentionally_stopped(component: str) -> None:
    """Record that `component` was deliberately stopped by an operator.

    Called by `nyxgpt ops down`/`stop` instead of disabling the watchdog
    globally (the old, now-removed behavior -- see #3406): the next heal
    pass sees this component `desired=False` (via `list_component_status`)
    and leaves it alone, while every other component stays protected by an
    enabled watchdog.
    """
    with _state_lock:
        state = _load_state()
        stopped = set(state.get("intentionally_stopped", []))
        stopped.add(component)
        state["intentionally_stopped"] = sorted(stopped)
        _save_state(state)


def clear_intentionally_stopped(component: str) -> None:
    """Clear `component`'s intentional-stop marker (it's being brought back up).

    Called by `nyxgpt ops install`/`restart`/`up` for the component(s) they
    bring up -- bringing a component back up is itself the "this is desired
    again" signal, so self-heal resumes guarding it against future crashes.
    A component never marked stopped is a no-op.
    """
    with _state_lock:
        state = _load_state()
        stopped = set(state.get("intentionally_stopped", []))
        stopped.discard(component)
        state["intentionally_stopped"] = sorted(stopped)
        _save_state(state)


def list_intentionally_stopped() -> list[str]:
    """Return every component currently marked as deliberately stopped."""
    with _state_lock:
        return list(_load_state().get("intentionally_stopped", []))


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` most recent heal events, newest last.

    `limit` is clamped between 1 and `EVENT_LOG_LIMIT`, the number of events
    actually retained in state.
    """
    with _state_lock:
        events = _load_state().get("events", [])
    bounded = max(1, min(limit, EVENT_LOG_LIMIT))
    return list(events[-bounded:])


def _brew_services_snapshot() -> dict[str, str]:
    """Return {brew_service_name: state} parsed from `brew services list`.

    Empty on any failure (brew not on PATH, non-zero exit, unreadable
    output) -- callers treat "not in this dict" as "not installed /
    nothing to monitor", not as an error.
    """
    if _which("brew") is None:
        return {}
    try:
        cp = _run(["brew", "services", "list"], expected=True)
    except Exception as e:
        logger.warning("self-heal: failed to query brew services list: %s", e)
        return {}
    if cp.returncode != 0:
        return {}
    snapshot: dict[str, str] = {}
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            snapshot[parts[0]] = parts[1]
    return snapshot


def _systemd_user_dir() -> Path:
    """Return `~/.config/systemd/user`, the systemd --user unit search path.

    Mirrors `ops._systemd_user_dir()` -- kept as a separate copy here for
    the same reason `NATIVE_SYSTEMD_SERVICES` is (see its comment).
    """
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_services_snapshot() -> dict[str, str]:
    """Return {unit_name: state} for nyxGPT-managed systemd --user units (Linux, #3508).

    Mirrors `_brew_services_snapshot()`'s vocabulary ("started" for a live
    unit) *and* its "not in this dict" == "not installed" contract: a unit
    is only included if its file actually exists in
    `~/.config/systemd/user/` -- `systemctl --user is-active` on a
    never-installed unit name reports "inactive"/"unknown" the same way it
    would for an installed-but-stopped one, so checking activity alone would
    misreport a fresh machine that's never run `nyxgpt ops install` as
    having every native component "down" instead of simply absent. Empty on
    any failure (systemctl not on PATH, no session bus reachable, non-zero
    exit).
    """
    if _which("systemctl") is None:
        return {}
    unit_dir = _systemd_user_dir()
    snapshot: dict[str, str] = {}
    for unit in NATIVE_SYSTEMD_SERVICES.values():
        if not (unit_dir / f"{unit}.service").exists():
            continue
        try:
            cp = _run(["systemctl", "--user", "is-active", f"{unit}.service"], expected=True)
        except Exception as e:
            logger.warning("self-heal: failed to query systemctl --user is-active %s: %s", unit, e)
            continue
        state = (cp.stdout or "").strip()
        snapshot[unit] = "started" if state == "active" else "none"
    return snapshot


def _native_container_state(name: str) -> str:
    """Return the docker state ('running', 'exited', ...) for container `name`, or 'absent'."""
    if _which("docker") is None:
        return "absent"
    try:
        cp = _run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"],
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to query docker state for %s: %s", name, e)
        return "absent"
    if cp.returncode != 0:
        return "absent"
    out = (cp.stdout or "").strip()
    return out.splitlines()[0].strip() if out else "absent"


def _native_container_health(name: str) -> str:
    """Return the Docker `HEALTHCHECK` status for container `name` (``''`` if none/unavailable)."""
    if _which("docker") is None:
        return ""
    try:
        cp = _run(
            [
                "docker",
                "inspect",
                "-f",
                "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name,
            ],
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to query docker health for %s: %s", name, e)
        return ""
    if cp.returncode != 0:
        return ""
    return (cp.stdout or "").strip()


def _list_terraform_component_status() -> list[ComponentStatus]:
    """Health-check the Terraform-managed core containers (`nyxgpt-tf-*`) directly via Docker.

    Mirrors `_list_native_component_status`'s native-Cassandra check, applied
    to all four core components: Terraform (`nyxgpt ops install --terraform
    --local`) runs `ollama`/`cassandra`/`api`/`web` as plain (non-Compose)
    Docker containers (see `terraform/main.tf`), invisible to both `docker
    compose ps` and `brew services list`.

    Returns every present component unconditionally -- deciding which mode
    actually owns a component that Compose/native also reports is
    `_resolve_core_component_conflicts`'s job (see `list_component_status`),
    not this probe's: a stopped leftover from another mode still needs to be
    seen here so the conflict resolver can compare it against Terraform's own
    state rather than being blind to it (#3428 -- a leftover reported first,
    unconditionally, used to shadow the real Terraform container instead of
    losing to it).

    Only reports a component whose container actually exists -- before
    `terraform apply` (or after `terraform destroy`), there's nothing to
    report, not a "down" component.
    """
    statuses: list[ComponentStatus] = []
    for component, container in TERRAFORM_CONTAINERS.items():
        state = _native_container_state(container)
        if state == "absent":
            continue
        health = _native_container_health(container)
        healthy = state == "running" and health in ("", "healthy", "starting")
        statuses.append(
            ComponentStatus(
                service=component,
                container=container,
                state=state,
                health=health,
                healthy=healthy,
                source="terraform",
            )
        )
    return statuses


def _list_kubernetes_component_status(already_managed: set[str]) -> list[ComponentStatus]:
    """Health-check the Kubernetes-managed `nyxgpt-api` Pods via `kubectl get pods`.

    Covers `nyxgpt ops install --kubernetes --local` (see `k8s/`): the
    stable/canary Deployments' Pods, one `ComponentStatus` per Pod (not per
    Deployment -- `stable` alone can run several replicas, and each needs
    its own backoff/restart-count bookkeeping in `heal_now`). Healed via
    `kubectl delete pod`, which the Pod's Deployment/ReplicaSet then
    recreates -- on top of, not instead of, kubelet's own liveness-probe
    restarts and `canary.py`'s metrics-gated rollout + auto-rollback (see
    the module docstring).

    Returns an empty list (never raises) if `kubectl` isn't on PATH, there's
    no reachable cluster, or the namespace/Pods don't exist yet -- the same
    "can't tell, so report nothing" fallback the rest of this module uses.
    `already_managed` guards against a Pod name coincidentally matching
    something `_resolve_core_component_conflicts` already resolved, though in
    practice Pod names never collide with the native/Compose/Terraform
    component names it carries.
    """
    if _which("kubectl") is None:
        return []
    try:
        cp = _run(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                K8S_NAMESPACE,
                "-l",
                K8S_POD_LABEL_SELECTOR,
                "-o",
                "json",
            ],
            timeout=15.0,
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to query kubernetes pods: %s", e)
        return []
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception as e:
        logger.warning("self-heal: failed to parse kubectl get pods output: %s", e)
        return []

    statuses: list[ComponentStatus] = []
    for pod in data.get("items", []):
        name = pod.get("metadata", {}).get("name", "")
        if not name or name in already_managed:
            continue
        status = pod.get("status", {})
        phase = status.get("phase", "")
        conditions = status.get("conditions", [])
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        healthy = phase == "Running" and ready
        statuses.append(
            ComponentStatus(
                service=name,
                container=name,
                state=phase,
                health="ready" if ready else "not-ready",
                healthy=healthy,
                source="kubernetes",
            )
        )
    return statuses


def _list_native_component_status() -> list[ComponentStatus]:
    """Health-check the native/local-first core components directly.

    In the default local-first deployment, `nyxgpt-api`/`nyxgpt-web`/`ollama` run as
    Homebrew services and `nyxgpt-cassandra` is a plain (non-Compose) Docker
    container -- none of that is visible to `docker compose ps`, which is why
    self-heal previously reported zero core components outside a Compose
    deployment (see #3348).

    Returns every present component unconditionally, even one Compose or
    Terraform also reports -- deciding which mode actually owns a
    double-reported component is `_resolve_core_component_conflicts`'s job
    (see `list_component_status`), not this probe's. In particular, a
    *stopped* native container/service left in place after switching to
    another deployment mode (e.g. a deliberately-kept-around
    `nyxgpt-cassandra` -- see #3428) must still be visible here so the
    conflict resolver can see it's not running and let the active mode win,
    rather than this function silently hiding it and losing the comparison
    entirely.

    Only reports a component that's actually installed/created -- a native
    service never set up via `nyxgpt ops install`, or a not-yet-created
    Cassandra container, isn't "down", it's simply out of scope until
    installed. Reads Homebrew services on macOS or systemd --user units on
    Linux (#3508), whichever `platform.system()` reports.
    """
    statuses: list[ComponentStatus] = []

    if _is_linux():
        native_snapshot = _systemd_services_snapshot()
        native_names = NATIVE_SYSTEMD_SERVICES
    else:
        native_snapshot = _brew_services_snapshot()
        native_names = NATIVE_BREW_SERVICES
    for component, native_name in native_names.items():
        state = native_snapshot.get(native_name)
        if state is None:
            continue
        statuses.append(
            ComponentStatus(
                service=component,
                container=native_name,
                state=state,
                health="",
                healthy=state == "started",
                source="native",
            )
        )

    state = _native_container_state(NATIVE_CASSANDRA_CONTAINER)
    if state != "absent":
        statuses.append(
            ComponentStatus(
                service="cassandra",
                container=NATIVE_CASSANDRA_CONTAINER,
                state=state,
                health="",
                healthy=state == "running",
                source="native",
            )
        )

    return statuses


def _enabled_observability_profiles() -> set[str]:
    """Compose profiles whose feature flag is enabled in config.ini right now.

    This is the desired-state signal self-heal reconciles observability
    containers against: if a profile is enabled here, its containers are
    *expected* to be running regardless of whether `docker compose ps`
    currently reports them at all (see `_list_absent_desired_components`) --
    that's the difference between "watching states" and "watching intent".

    Returns an empty set (never raises) if config.ini is missing or
    unreadable -- an absent/broken config just means nothing is desired yet,
    not an error self-heal should propagate.
    """
    try:
        cfg = load_config()
    except Exception as e:
        logger.debug("self-heal: no config.ini for desired-state check: %s", e)
        return set()

    section_enabled = {
        "monitoring": get_monitoring_enabled(cfg),
        "log_aggregation": get_log_aggregation_enabled(cfg),
        "tracing": get_tracing_enabled(cfg),
        "error_tracking": get_error_tracking_enabled(cfg),
    }
    return {
        OBSERVABILITY_PROFILE_SECTIONS[section]
        for section, enabled in section_enabled.items()
        if enabled
    }


def _desired_compose_services(profiles: set[str], *, exclude_one_shot: bool = True) -> set[str]:
    """Resolve the Compose service names gated by `profiles` (empty if none enabled).

    Mirrors `ops._start_observability_stack`'s own resolution: `docker
    compose --profile X ... config --services`, minus `CORE_APP_SERVICES`
    (which this desired-state check doesn't cover -- see that constant's
    docstring) and, when `exclude_one_shot` (the default), minus
    `ONE_SHOT_SERVICES` (a run-to-completion job is never "desired but
    absent" -- exiting 0 with no running container *is* its healthy end
    state, mirroring the exemption `_list_compose_component_status` already
    applies on the present side).

    `_mark_disabled_present_services` calls this with `exclude_one_shot=False`
    -- it uses the result to recognize which *present* services belong to
    some observability profile at all, and a one-shot job that's present
    (i.e. it failed, since a successful one is never present -- see
    `_list_compose_component_status`) must stay recognized so a disabled
    profile still clears its `desired` flag instead of leaving it stuck
    `True` forever.

    Returns an empty set on any Docker/Compose failure, the same "can't
    tell, so don't act" fallback the rest of this module uses.
    """
    if not profiles or _which("docker") is None:
        return set()
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    for profile in sorted(profiles):
        cmd += ["--profile", profile]
    try:
        cp = _run(cmd + ["config", "--services"], expected=True)
    except Exception as e:
        logger.warning("self-heal: failed to resolve desired compose services: %s", e)
        return set()
    if cp.returncode != 0:
        return set()
    services = {s.strip() for s in (cp.stdout or "").splitlines() if s.strip()} - CORE_APP_SERVICES
    if exclude_one_shot:
        services -= ONE_SHOT_SERVICES
    return services


def _absent_desired_statuses(
    present_services: set[str], desired_services: set[str]
) -> list[ComponentStatus]:
    """Build a `ComponentStatus` for every desired observability service missing entirely.

    `present_services` is whatever `_list_compose_component_status()` already
    found (containers that exist, healthy or not); `desired_services` is
    `_desired_compose_services(_enabled_observability_profiles())`. Anything
    desired but not present was torn down (e.g. `nyxgpt ops down`) while its
    feature flag is still on in config.ini -- self-heal treats that the same
    as "unhealthy" (`healthy=False`, `state="absent"`), so both the automatic
    watchdog and "heal all unhealthy now" bring it back via
    `_bring_up_compose_service` instead of silently reporting nothing to
    check.
    """
    return [
        ComponentStatus(
            service=service,
            container="",
            state="absent",
            health="",
            healthy=False,
            source="compose",
            desired=True,
        )
        for service in sorted(desired_services - present_services)
    ]


def _mark_disabled_present_services(
    compose_statuses: list[ComponentStatus], desired_services: set[str]
) -> list[ComponentStatus]:
    """Flag `desired=False` on present Compose statuses outside the enabled set.

    Disabling an observability profile via the config wizard/API *stops* its
    containers rather than removing them (`ops._stop_observability_stack`),
    so they keep showing up here as "present, unhealthy" -- without this,
    `heal_now`'s automatic pass would restart them right back, undoing a
    deliberate disable. Only checks services already known to belong to
    *some* observability profile (`compose_statuses` minus what's currently
    desired minus the core app services) before making the one extra Docker
    call this needs, so a normal healthy pass with nothing extra present
    costs nothing beyond the existing `ps -a` call.
    """
    present_services = {s.service for s in compose_statuses}
    maybe_disabled = present_services - desired_services - CORE_APP_SERVICES
    if not maybe_disabled:
        return compose_statuses

    all_observability_services = _desired_compose_services(
        set(OBSERVABILITY_PROFILES), exclude_one_shot=False
    )
    disabled_services = maybe_disabled & all_observability_services
    if not disabled_services:
        return compose_statuses

    return [
        replace(s, desired=False) if s.service in disabled_services else s for s in compose_statuses
    ]


# Priority among core-component sources when more than one reports the same
# component and *none* of the candidates is actually running (e.g. two
# stopped leftovers with no active deployment at all) -- preserves the
# historical compose > native > terraform preference for that ambiguous
# edge case. Whenever at least one candidate IS running, `_resolve_core_
# component_conflicts` picks that one regardless of this order.
_CORE_SOURCE_PRIORITY: dict[str, int] = {"compose": 0, "native": 1, "terraform": 2}


def _resolve_core_component_conflicts(
    statuses: list[ComponentStatus],
) -> list[ComponentStatus]:
    """Pick one authoritative status per core component out of Compose/native/Terraform entries.

    Native and Terraform mode default to the same host ports and share
    Cassandra's `~/.nyxGPT/volumes/cassandra` data directory, and an operator
    may deliberately keep a *stopped* leftover from an inactive mode around
    (e.g. the native `nyxgpt-cassandra` container as the path back to native
    mode -- see #3428). Simply excluding a component from a later mode's
    probe as soon as an earlier mode reports *any* entry for it (the old
    behavior) let that stopped leftover permanently shadow the real, running
    container from a mode checked later -- reporting the wrong health and,
    worse, making `heal_now` eligible to "restart" the leftover instead of
    the container actually serving traffic.

    Instead: whichever candidate is actually running (`state` in `("running",
    "started")`) wins outright, regardless of which mode reported it or in
    what order. Only when *no* candidate is running (e.g. every mode's entry
    for this component is stopped) does `_CORE_SOURCE_PRIORITY` break the
    tie, preserving prior behavior for that fully-down edge case. Every
    losing candidate is folded into the winner's `note` as an "inert
    leftover" annotation -- visible for troubleshooting, but never able to
    affect `healthy`/`state`/`source`, and never a target `heal_now` can
    dispatch to (see `restart_native_component`'s own belt-and-suspenders
    guard for Cassandra specifically).
    """
    by_component: dict[str, list[ComponentStatus]] = {}
    for s in statuses:
        by_component.setdefault(s.service, []).append(s)

    resolved: list[ComponentStatus] = []
    for candidates in by_component.values():
        if len(candidates) == 1:
            resolved.append(candidates[0])
            continue

        ranked = sorted(
            candidates,
            key=lambda s: (
                0 if s.state in ("running", "started") else 1,
                _CORE_SOURCE_PRIORITY.get(s.source, 99),
            ),
        )
        winner, leftovers = ranked[0], ranked[1:]
        note = "; ".join(
            f"inert {s.source} leftover: {s.container or s.service} (state={s.state})"
            for s in leftovers
        )
        resolved.append(replace(winner, note=note) if note else winner)
    return resolved


def list_component_status() -> list[ComponentStatus]:
    """Health-check every monitored component across every deployment mode.

    Combines `_list_compose_component_status()` (whatever's currently up
    under Docker Compose, with any component whose observability profile is
    currently disabled flagged `desired=False` -- see
    `_mark_disabled_present_services`), `_list_native_component_status()`
    (native/local-first's core components) and `_list_terraform_component_status()`
    (Terraform's), reconciled by `_resolve_core_component_conflicts` into one
    authoritative entry per core component (see that function for why a
    stopped leftover from an inactive mode must never win), plus
    `_list_kubernetes_component_status()` (Pods, which never collide by name
    with the other three) and `_absent_desired_statuses()` (enabled
    observability profiles with no running containers at all) -- so the same
    call surfaces the right set regardless of which deployment mode -- or mix
    of modes, including inactive-mode leftovers -- is actually present.

    Finally, any component an operator deliberately stopped (`nyxgpt ops
    down`/`stop` -- see `list_intentionally_stopped`) is flagged
    `desired=False` too, the same mechanism used for a disabled observability
    profile: `heal_now`'s automatic pass leaves it alone, regardless of which
    mode stopped it, until `nyxgpt ops install`/`restart`/`up` clears the
    marker.
    """
    compose_statuses = _list_compose_component_status()
    compose_managed = {s.service for s in compose_statuses}

    desired_services = _desired_compose_services(_enabled_observability_profiles())
    compose_statuses = _mark_disabled_present_services(compose_statuses, desired_services)
    absent_statuses = _absent_desired_statuses(compose_managed, desired_services)

    native_statuses = _list_native_component_status()
    terraform_statuses = _list_terraform_component_status()
    core_statuses = _resolve_core_component_conflicts(
        compose_statuses + native_statuses + terraform_statuses
    )
    core_managed = {s.service for s in core_statuses}
    kubernetes_statuses = _list_kubernetes_component_status(core_managed)

    all_statuses = core_statuses + kubernetes_statuses + absent_statuses

    stopped = set(list_intentionally_stopped())
    if stopped:
        all_statuses = [
            replace(s, desired=False) if s.desired and s.service in stopped else s
            for s in all_statuses
        ]
    return all_statuses


def compose_probe_available() -> bool:
    """Whether the Compose observability survey can actually run from this process.

    `False` means `_list_compose_component_status()` finding nothing can't be
    trusted as "genuinely not running" -- e.g. no `docker` binary on PATH, or
    `COMPOSE_FILE` doesn't exist from this process's vantage point. The latter
    is exactly what happened to a Terraform-managed `nyxgpt-tf-api` container
    before it got the same `docker-compose.yml` bind mount + `NYXGPT_COMPOSE_
    FILE` env var docker-compose.yml's own `api` service already sets (#3588):
    `_resolve_compose_file()`'s module-path and config.ini fallbacks both fail
    inside that container, so `COMPOSE_FILE` resolves to a path that was never
    mounted, and `docker compose ps` against it fails every pass -- silently
    reporting zero observability containers indistinguishable from "nothing is
    running". `status()`/`ops.infra_status()` surface this so the Self-Heal
    and Infrastructure Status pages can say "can't check from here" instead of
    a false "nothing running".
    """
    return _which("docker") is not None and COMPOSE_FILE.exists()


def _list_compose_component_status() -> list[ComponentStatus]:
    """Query `docker compose ps -a` for every container the project has created.

    Only reports containers that actually exist -- an opt-in profile
    (monitoring/logging/tracing/errors) that was never started isn't
    reported as "down", it's simply absent from the result.

    A member of `ONE_SHOT_SERVICES` (e.g. `glitchtip-migrate`) is skipped
    entirely once it has run to completion (`state="exited"`, `ExitCode ==
    0`) -- that's its healthy end state, not a container self-heal should
    track. But if it exited non-zero, the migration genuinely failed, so it's
    still reported here (`healthy=False`) rather than silently swallowed --
    the exemption is for the "not a long-running service" shape, not for
    masking a real failure.
    """
    if _which("docker") is None:
        return []
    try:
        cp = _run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "-a", "--format", "json"],
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to query docker compose ps: %s", e)
        return []
    if cp.returncode != 0:
        logger.warning(
            "self-heal: docker compose ps exited %s querying %s -- observability survey "
            "unavailable this pass (see compose_probe_available)",
            cp.returncode,
            COMPOSE_FILE,
        )
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
        if not service:
            continue
        state = data.get("State", "")
        if service in ONE_SHOT_SERVICES:
            if state == "exited" and data.get("ExitCode", 0) == 0:
                continue
            statuses.append(
                ComponentStatus(
                    service=service,
                    container=data.get("Name", ""),
                    state=state,
                    health=data.get("Health", ""),
                    healthy=False,
                )
            )
            continue
        health = data.get("Health", "")
        # A container still inside its Docker start_period reports "starting":
        # it hasn't failed its health check yet, so treat it as healthy here.
        # Restarting a "starting" container is premature, and for the api
        # container -- whose self-heal watchdog issues the restart from inside
        # itself -- it produces a permanent crash loop: the restart kills the
        # api before it can finish starting, so it never becomes healthy and
        # the next watchdog pass restarts it again. Self-heal still acts on an
        # explicit "unhealthy" (or a non-running state), just not on "starting".
        healthy = state == "running" and health in ("", "healthy", "starting")
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
    """Log each component's health-check result and update the self-heal gauges.

    Logged at DEBUG (routine, high-frequency per-component data -- set
    `[logging] level = DEBUG` in config.ini to see it) rather than INFO, so a
    healthy stack doesn't spam the log file every `check_interval_seconds`.
    Returns the number of unhealthy components, for callers that also need
    the count (e.g. `heal_now`'s pass summary). A stopped-but-`desired=False`
    component (its observability profile is disabled -- see
    `_mark_disabled_present_services`) isn't counted: it's intentionally
    down, not something an operator needs to act on. The same alarming
    condition (`not healthy and desired`) also drives the labeled
    `SELFHEAL_COMPONENT_HEALTHY` gauge, so the aggregate count and the named
    per-service series can never disagree (#3575).
    """
    unhealthy = 0
    for s in statuses:
        is_unhealthy = not s.healthy and s.desired
        if is_unhealthy:
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
        prom_metrics.SELFHEAL_COMPONENT_HEALTHY.labels(service=s.service).set(
            0.0 if is_unhealthy else 1.0
        )
    prom_metrics.SELFHEAL_UNHEALTHY_COMPONENTS.set(unhealthy)
    prom_metrics.SELFHEAL_LAST_CHECK_TIMESTAMP.set(time.time())
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


def _bring_up_compose_service(service: str) -> HealResult:
    """Bring up a desired-but-absent Compose service: `docker compose ... up -d <service>`.

    Used instead of `restart_component` for a component reported `state="absent"`
    by `_list_absent_desired_components` -- `docker compose restart` is a
    no-op (or an error) on a service with no container to restart, since a
    torn-down profile (`nyxgpt ops down`) has no container at all, not just a
    stopped one. Passes every observability profile flag (mirroring
    `ops._start_observability_stack`), since Compose requires a service's
    profile to be active for it to resolve even when named explicitly.
    """
    if _which("docker") is None:
        return HealResult(False, f"docker not found; cannot start {service}")
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        cmd += ["--profile", profile]
    cmd += ["up", "-d", service]
    try:
        cp = _run(cmd, timeout=120.0)
    except Exception as e:
        return HealResult(False, f"Failed to start {service}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to start {service}", details.strip())
    return HealResult(True, f"Started {service}")


def _restart_brew_service(name: str) -> HealResult:
    """Restart Homebrew service `name` via `brew services restart` (native mode)."""
    if _which("brew") is None:
        return HealResult(False, f"brew not found; cannot restart {name}")
    try:
        cp = _run(["brew", "services", "restart", name], timeout=60.0)
    except Exception as e:
        return HealResult(False, f"Failed to restart {name}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to restart brew service: {name}", details.strip())
    return HealResult(True, f"Restarted brew service: {name}")


def _restart_systemd_service(unit: str) -> HealResult:
    """Restart systemd --user unit `unit` via `systemctl --user restart` (native mode, Linux)."""
    if _which("systemctl") is None:
        return HealResult(False, f"systemctl not found; cannot restart {unit}")
    try:
        cp = _run(["systemctl", "--user", "restart", f"{unit}.service"], timeout=60.0)
    except Exception as e:
        return HealResult(False, f"Failed to restart {unit}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to restart systemd unit: {unit}", details.strip())
    return HealResult(True, f"Restarted systemd unit: {unit}")


def _restart_native_container(name: str) -> HealResult:
    """Restart Docker container `name` via `docker restart` (native mode's Cassandra)."""
    if _which("docker") is None:
        return HealResult(False, f"docker not found; cannot restart {name}")
    try:
        cp = _run(["docker", "restart", name], timeout=60.0)
    except Exception as e:
        return HealResult(False, f"Failed to restart {name}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to restart docker container: {name}", details.strip())
    return HealResult(True, f"Restarted docker container: {name}")


def _cassandra_active_elsewhere() -> str | None:
    """Name the container/service actively running Cassandra outside native mode, if any.

    Guards `restart_native_component("cassandra")` against `docker restart`ing
    (which starts, if stopped) the native `nyxgpt-cassandra` container while
    Terraform's `nyxgpt-tf-cassandra` (or a Compose `cassandra` service) is
    already bound to the same host port (9042) and the same shared
    `~/.nyxGPT/volumes/cassandra` data directory -- a heal action that would
    *create* the exact outage it's meant to fix (#3193, #3428).
    `list_component_status`'s `_resolve_core_component_conflicts` already
    keeps `heal_now` from choosing this dispatch path in that situation (the
    conflict resolver reports the running Terraform/Compose entry instead of
    a native one), so this is a last-resort check for `restart_native_component`
    called directly, or a race between listing status and issuing the
    restart. Returns `None` (no collision) if neither is running.
    """
    terraform_container = TERRAFORM_CONTAINERS["cassandra"]
    if _native_container_state(terraform_container) == "running":
        return terraform_container
    for s in _list_compose_component_status():
        if s.service == "cassandra" and s.state == "running":
            return s.container or "cassandra (compose)"
    return None


def restart_native_component(component: str) -> HealResult:
    """Restart a native/local-first core component.

    `api`/`web`/`ollama` restart via `brew services restart <name>` on
    macOS or `systemctl --user restart <unit>` on Linux (#3508);
    `cassandra` (the one Docker-managed piece of a native install) restarts
    via `docker restart <container>` -- the same mechanisms `nyxgpt ops
    restart` uses, so the user never needs a raw `brew`/`systemctl`/`docker`
    command.

    Refuses (rather than silently skipping) to touch the native Cassandra
    container if `_cassandra_active_elsewhere` finds Terraform or Compose
    already running it -- see that function's docstring.
    """
    if component == "cassandra":
        collision = _cassandra_active_elsewhere()
        if collision is not None:
            return HealResult(
                False,
                "Refused to restart native Cassandra: port/volume collision",
                f"{collision} is already running Cassandra -- starting "
                f"{NATIVE_CASSANDRA_CONTAINER} too would collide on port 9042 and the "
                "shared ~/.nyxGPT/volumes/cassandra data directory (see #3193, #3428). "
                "Leave the native container stopped until you deliberately switch back "
                "to native mode.",
            )
        return _restart_native_container(NATIVE_CASSANDRA_CONTAINER)
    if _is_linux():
        unit = NATIVE_SYSTEMD_SERVICES.get(component)
        if unit is None:
            return HealResult(False, f"Unknown native component: {component}")
        return _restart_systemd_service(unit)
    brew_name = NATIVE_BREW_SERVICES.get(component)
    if brew_name is None:
        return HealResult(False, f"Unknown native component: {component}")
    return _restart_brew_service(brew_name)


def restart_terraform_component(component: str) -> HealResult:
    """Restart a Terraform-managed core component: `docker restart nyxgpt-tf-<component>`.

    The same primitive `nyxgpt ops restart` would use for a Terraform
    deployment, so the user never needs a raw `docker` command.
    """
    container = TERRAFORM_CONTAINERS.get(component)
    if container is None:
        return HealResult(False, f"Unknown terraform component: {component}")
    return _restart_native_container(container)


def heal_kubernetes_pod(pod_name: str) -> HealResult:
    """Heal a Kubernetes-managed Pod by deleting it: `kubectl delete pod <name>`.

    The owning Deployment's ReplicaSet recreates the Pod -- this is a
    stronger recovery action than kubelet's own in-place container restart
    on a failed liveness probe, useful for a Pod that's stuck rather than
    cleanly crash-looping.
    """
    if _which("kubectl") is None:
        return HealResult(False, f"kubectl not found; cannot heal pod {pod_name}")
    try:
        cp = _run(["kubectl", "delete", "pod", pod_name, "-n", K8S_NAMESPACE], timeout=60.0)
    except Exception as e:
        return HealResult(False, f"Failed to delete pod {pod_name}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to delete pod {pod_name}", details.strip())
    return HealResult(True, f"Deleted pod {pod_name} for recreation")


def _compose_component_logs(service: str, *, tail: int = 200) -> HealResult:
    """Fetch recent logs for a Compose service: `docker compose logs <service>`.

    The fallback (and, for any service `component_logs` doesn't recognize as
    native/Terraform/Kubernetes-managed -- e.g. an observability container
    like `glitchtip`/`grafana`/`loki` -- the primary) path for reading a
    container's output without the user needing to run a raw `docker`/
    `docker compose` command themselves.
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


def _tail_text_file(path: Path, tail: int) -> str:
    """Return the last `tail` lines of `path`, or "" if it's missing/unreadable."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        logger.warning("self-heal: failed to read log file %s: %s", path, e)
        return ""
    return "\n".join(lines[-tail:]) if tail > 0 else ""


def _brew_prefix() -> str | None:
    """Resolve Homebrew's install prefix (e.g. `/opt/homebrew`), or None if it can't be found.

    Tries `brew --prefix` first, falling back to the two conventional
    locations (Apple Silicon/Intel) -- mirrors
    `scripts/follow-ollama-logs.sh`'s own `native_ollama_log()` resolution.
    """
    if _which("brew") is not None:
        try:
            cp = _run(["brew", "--prefix"], expected=True)
        except Exception as e:
            logger.warning("self-heal: failed to resolve brew --prefix: %s", e)
            cp = None
        if cp is not None and cp.returncode == 0:
            prefix = (cp.stdout or "").strip()
            if prefix:
                return prefix
    for candidate in ("/opt/homebrew", "/usr/local"):
        if Path(candidate, "var", "log").is_dir():
            return candidate
    return None


def _docker_container_logs(container: str, *, tail: int) -> HealResult:
    """Fetch recent logs for a plain (non-Compose) Docker container: `docker logs <container>`.

    Backs the native `nyxgpt-cassandra` container and any Terraform-managed
    (`nyxgpt-tf-*`) component -- both plain `docker run`/Terraform-created
    containers rather than Compose services, so `docker compose logs` can't
    see them at all.
    """
    if _which("docker") is None:
        return HealResult(False, "docker not found; cannot fetch logs")
    try:
        cp = _run(["docker", "logs", "--tail", str(tail), container], timeout=30.0)
    except Exception as e:
        return HealResult(
            False, f"Failed to fetch logs for {container}", f"{type(e).__name__}: {e}"
        )
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to fetch logs for {container}", details.strip())
    output = (cp.stdout or "").strip()
    if (cp.stderr or "").strip():
        output = (output + "\n" + cp.stderr.strip()).strip()
    return HealResult(True, f"Fetched last {tail} log line(s) for {container}", output)


def _kubernetes_pod_logs(pod_name: str, *, tail: int) -> HealResult:
    """Fetch recent logs for a Kubernetes-managed Pod: `kubectl logs <pod>`."""
    if _which("kubectl") is None:
        return HealResult(False, f"kubectl not found; cannot fetch logs for {pod_name}")
    try:
        cp = _run(
            ["kubectl", "logs", "-n", K8S_NAMESPACE, "--tail", str(tail), pod_name],
            timeout=30.0,
        )
    except Exception as e:
        return HealResult(False, f"Failed to fetch logs for {pod_name}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to fetch logs for {pod_name}", details.strip())
    return HealResult(
        True, f"Fetched last {tail} log line(s) for {pod_name}", (cp.stdout or "").strip()
    )


def _native_component_logs(service: str, *, tail: int) -> HealResult:
    """Fetch recent logs for a native/local-first core component from its real source.

    `cassandra` is the plain `nyxgpt-cassandra` Docker container, read via
    `_docker_container_logs` just like a Terraform component. `api`'s own
    structured logs and `ollama`'s aggregated logs (written regardless of
    mode by the ollama log-follower agent -- see docs/api.md#ollama-logs)
    are both plain files under `~/.nyxGPT/logs` (`api.log`/`ollama.log`).
    `web` has no structured logging of its own yet (#3430), so it's read
    from its native service's own stdout/stderr: Homebrew's launchd log
    path on macOS (`nyxgpt-web.log`/`.err.log` -- see
    docs/homebrew.md#web-ui-logs), or the same names under `~/.nyxGPT/logs`
    on Linux, where `nyxgpt-web.service`'s `StandardOutput`/`StandardError`
    already point there directly (#3508).
    """
    if service == "cassandra":
        return _docker_container_logs(NATIVE_CASSANDRA_CONTAINER, tail=tail)

    if service in ("api", "ollama"):
        log_file = get_log_dir() / f"{service}.log"
        if not log_file.exists():
            return HealResult(False, f"No log file found for {service}: {log_file}")
        return HealResult(
            True,
            f"Fetched last {tail} log line(s) for {service}",
            _tail_text_file(log_file, tail),
        )

    if service == "web":
        if _is_linux():
            out_file = get_log_dir() / "nyxgpt-web.log"
            err_file = get_log_dir() / "nyxgpt-web.err.log"
        else:
            prefix = _brew_prefix()
            if prefix is None:
                return HealResult(False, "Homebrew not found; cannot locate web service logs")
            out_file = Path(prefix, "var", "log", "nyxgpt-web.log")
            err_file = Path(prefix, "var", "log", "nyxgpt-web.err.log")
        if not out_file.exists() and not err_file.exists():
            return HealResult(False, f"No log files found for web: {out_file}, {err_file}")
        sections = []
        if out_file.exists():
            sections.append(f"--- stdout ({out_file}) ---\n{_tail_text_file(out_file, tail)}")
        if err_file.exists():
            sections.append(f"--- stderr ({err_file}) ---\n{_tail_text_file(err_file, tail)}")
        return HealResult(True, f"Fetched last {tail} log line(s) for web", "\n".join(sections))

    return HealResult(False, f"Unknown native component: {service}")


def component_logs(service: str, *, tail: int = 200) -> HealResult:
    """Fetch recent logs for a component, dispatched by its actual deployment mode.

    Backs `nyxgpt ops logs` -- the wrapped way to read a component's output
    without the user needing to run a raw `docker`/`docker compose`/`brew`/
    `kubectl` command themselves. `docker compose logs` only ever sees
    Compose containers, so a Compose-only implementation is blind in native/
    Terraform/Kubernetes deployments: it reports a hollow success (an empty
    `docker compose logs` on a service Compose never created) instead of the
    real log content, or an error (#3442).

    Resolves the requesting `service`'s actual source the same way `ops
    status`/self-heal already do (`list_component_status`) and dispatches
    accordingly: native components read their real log files/containers
    (`_native_component_logs`), Terraform/Kubernetes components read their
    plain Docker container/Pod logs directly. A `state="absent"` match (e.g.
    an observability profile enabled in config.ini but torn down) has no log
    source at all, so it fails explicitly rather than returning a hollow
    success. Anything `list_component_status` doesn't recognize (an
    observability container like `glitchtip`, or a genuinely unknown service
    name) falls through to `_compose_component_logs`, which already reports
    a clear failure for a nonexistent service.
    """
    status = next((s for s in list_component_status() if s.service == service), None)
    if status is not None:
        if status.state == "absent":
            return HealResult(
                False,
                f"{service} is not currently running; no logs to show",
                "state=absent -- no container/process exists to read logs from",
            )
        if status.source == "native":
            return _native_component_logs(service, tail=tail)
        if status.source == "terraform":
            return _docker_container_logs(status.container, tail=tail)
        if status.source == "kubernetes":
            return _kubernetes_pod_logs(status.container, tail=tail)

    return _compose_component_logs(service, tail=tail)


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

            if not manual and not status.desired:
                # Its observability profile flag is off in config.ini --
                # disabling the flag is a supported way to keep a component
                # down (see docs/self-healing.md#desired-state-for-observability-profiles),
                # so the automatic pass must not restart it back. A manual
                # "Heal now" click (service explicitly requested) still can,
                # same override as the backoff/max-restarts guards below.
                logger.info(
                    "self-heal: skipping restart of %s, its observability profile is disabled"
                    " in config",
                    status.service,
                    extra={"component": "self_heal", "service": status.service},
                )
                continue

            count = restart_counts.get(status.service, 0)
            last_ts = last_restart_ts.get(status.service, 0.0)

            if not manual:
                if count >= max_consecutive_restarts:
                    prom_metrics.SELFHEAL_GIVEUP_TOTAL.labels(service=status.service).inc()
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
            # Reuse the ambient correlation id (CLI env var or dashboard
            # request_id) when this heal was operator-triggered; mint a
            # fresh one for the autonomous watchdog, which has neither, so
            # this attempt's HealEvent and the restart subprocess it drives
            # (inherited via os.environ) can still be joined (#3390, #3430).
            correlation_id = get_correlation_id()
            if correlation_id == "-":
                correlation_id = mint_correlation_id()
            os.environ["NYXGPT_CORRELATION_ID"] = correlation_id
            logger.info(
                "self-heal: attempting restart of %s (reason=%s, attempt=%d, correlation_id=%s)",
                status.service,
                reason,
                count + 1,
                correlation_id,
                extra={
                    "component": "self_heal",
                    "service": status.service,
                    "reason": reason,
                    "attempt": count + 1,
                    "manual": manual,
                    "correlation_id": correlation_id,
                },
            )
            if status.source == "native":
                result = restart_native_component(status.service)
            elif status.source == "terraform":
                result = restart_terraform_component(status.service)
            elif status.source == "kubernetes":
                result = heal_kubernetes_pod(status.container)
            elif status.state == "absent":
                # Enabled in config but no container at all (e.g. `nyxgpt ops
                # down`) -- `docker compose restart` can't bring back what
                # doesn't exist, so bring it up instead.
                result = _bring_up_compose_service(status.service)
            else:
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

            evidence = {
                "probe_type": status.source,
                "state": status.state,
                "health": status.health,
                "healthy_before": status.healthy,
                "container": status.container,
                "manual": manual,
                "restart_count_before": count,
                "max_consecutive_restarts": max_consecutive_restarts,
                "backoff_seconds": backoff_seconds,
                "heal_result_details": result.details,
            }

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
                    "evidence": evidence,
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
                evidence=evidence,
                correlation_id=correlation_id,
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


def detected_mode(components: list[ComponentStatus]) -> str:
    """Best-guess deployment mode from which source the core app components report from.

    Mirrors `ops.detect_deployment_mode()`'s native-vs-Compose distinction,
    extended to terraform/kubernetes -- lets the Self-Heal page state its
    detected mode explicitly instead of leaving Kubernetes mode looking
    like "no components found" (#3410). Core components are normally all
    reported by a single mode at once (mixing modes for the same component
    is exactly what `list_component_status`'s `_resolve_core_component_
    conflicts` prevents, see #3428), so the first source found among the
    core services below is reported.
    """
    core_sources = {c.source for c in components if c.service in CORE_APP_SERVICES}
    for source in ("terraform", "kubernetes", "compose", "native"):
        if source in core_sources:
            return source
    return "none"


def status() -> dict[str, Any]:
    """Aggregate status for `GET /api/v1/self-heal/status`.

    Each component dict is annotated with `restart_count` (consecutive
    automatic restart attempts since it last recovered) and `giving_up`
    (`True` once that count has hit the watchdog's `max_consecutive_restarts`
    and the automatic loop has stopped retrying -- see `heal_now`). Without
    this, a component stuck unhealthy looks identical whether self-heal is
    still actively retrying it or has already given up and is waiting on an
    operator (#3575).
    """
    components = list_component_status()
    unhealthy_count = _record_health_check(components)
    restart_counts: dict[str, int] = _load_state().get("restart_counts", {})
    max_consecutive_restarts = get_watchdog().max_consecutive_restarts
    component_dicts = []
    for c in components:
        d = c.to_dict()
        count = restart_counts.get(c.service, 0)
        d["restart_count"] = count
        d["giving_up"] = not c.healthy and c.desired and count >= max_consecutive_restarts
        component_dicts.append(d)
    return {
        "enabled": is_enabled(),
        "mode": detected_mode(components),
        "compose_probe_available": compose_probe_available(),
        "components": component_dicts,
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
    "is_intentionally_stopped",
    "mark_intentionally_stopped",
    "clear_intentionally_stopped",
    "list_intentionally_stopped",
    "recent_events",
    "detected_mode",
    "compose_probe_available",
    "list_component_status",
    "restart_component",
    "restart_native_component",
    "restart_terraform_component",
    "heal_kubernetes_pod",
    "component_logs",
    "heal_now",
    "get_watchdog",
    "status",
]
