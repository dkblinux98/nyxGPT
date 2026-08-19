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
- **Kubernetes** (`nyxgpt ops install --kubernetes --local`): every Pod the
  install deploys -- the api and web Deployments (stable/canary), the
  Cassandra and Ollama StatefulSets, and the in-cluster observability overlay
  (`k8s/observability/`, #3787) -- checked via `kubectl get pods` and healed
  via `kubectl delete pod` (the owning controller recreates it) -- see
  `_list_kubernetes_component_status`. This is on top of, not instead of,
  Kubernetes' own kubelet liveness-probe restarts and `canary.py`'s
  metrics-gated rollout + auto-rollback; it adds the same "torn-down/stuck"
  recovery + unified dashboard visibility the other three modes get. In this
  mode the observability tier is read from the cluster, and the Compose
  survey below has no bearing on it (#3828). Deleting a Pod is a repair only
  for one state -- `Running` but not `Ready` -- and `nyxgpt.k8s_pod_state` is
  what draws that line (#3832); a `Pending` Pod is reported with the
  cluster's own reason and never deleted, because deletion cannot create the
  capacity an unschedulable Pod is waiting for.

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
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from nyxgpt import brew_services
from nyxgpt import metrics as prom_metrics
from nyxgpt.config import (
    get_error_tracking_enabled,
    get_log_aggregation_enabled,
    get_monitoring_enabled,
    get_tracing_enabled,
    load_config,
)
from nyxgpt.install_mode import DEV_LAUNCHD_LABELS, read_install_mode
from nyxgpt.k8s_pod_state import PodState, classify_pod
from nyxgpt.logging import get_correlation_id, get_log_dir, mint_correlation_id
from nyxgpt.subprocess_bounds import bounded_argv, timeout_message, timeout_result

logger = logging.getLogger(__name__)


def _resolve_compose_file() -> Path:
    """Resolve the docker-compose.yml the watchdog targets.

    Two layouts, two answers (#3621 -- previously three, with a REPO_ROOT
    module-path check and a config.ini `[paths] compose_file` fallback for
    the brew-installed-native-service case; both are gone now that the
    Compose file lives at a single fixed, ops-managed location regardless of
    how nyxGPT itself is installed):
    - api container: `self_heal.py` lives under site-packages with no repo
      checkout; the compose file is bind-mounted in and its in-container path
      passed via NYXGPT_COMPOSE_FILE (see the `api` service in
      docker-compose.yml and docs/self-healing.md).
    - every other layout (dev checkout, brew-installed native service,
      installed non-editable package): `nyxgpt ops install` syncs the
      packaged `nyxgpt.resources` docker-compose.yml to this fixed location
      (see `nyxgpt.ops._sync_packaged_resources`), so it's always here once
      install has run at least once.
    """
    override = os.environ.get("NYXGPT_COMPOSE_FILE", "").strip()
    if override:
        return Path(override)

    return Path.home() / ".nyxGPT" / "docker-compose.yml"


COMPOSE_FILE = _resolve_compose_file()

EVENT_LOG_LIMIT = 100
DEFAULT_CHECK_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_CONSECUTIVE_RESTARTS = 5
DEFAULT_BACKOFF_SECONDS = 30.0

# Rolling window over which the TOTAL number of automatic heal actions taken
# against one component is capped, independently of the consecutive-failure
# counter (#3832).
#
# The consecutive counter answers "are my restarts failing?" and resets the
# moment the component looks healthy once. That is the right question for a
# service that flaps, and it is defeated by two things: a component whose
# identity *changes* when it is healed (#3832's Pod deletions produced seven
# names in 4.5 minutes, each with a counter of its own, so a cap of 5 never
# fired), and a component that comes up healthy just long enough to zero the
# counter before failing again. This window is the guard that does not care
# why: attempts are counted against a stable identity
# (`ComponentStatus.budget_key`) and never reset by a glimpse of health, so no
# component can be healed without bound whatever the cause.
DEFAULT_HEAL_ATTEMPT_WINDOW_SECONDS = 900.0

# How many heal actions are allowed in that window, as a multiple of
# `max_consecutive_restarts`. Deliberately looser than the consecutive cap so
# the two guards stay distinct: the consecutive counter is what normally stops
# a failing component (and says so), and this one only catches what gets past
# it -- an identity that changed, or a health flicker that reset it.
HEAL_ATTEMPT_WINDOW_MULTIPLIER = 2

# Runs-to-completion services (e.g. one-shot DB migrations) that are never
# "down" in the self-heal sense -- they're expected to exit 0 and stay exited.
ONE_SHOT_SERVICES = {"glitchtip-migrate"}

# Component/service/pod names that flow into a subprocess argv
# (`docker compose restart <svc>`, `kubectl delete pod <name>`) are guarded
# at every call site with a DIRECT `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", x)`
# call: names must start alphanumeric so a crafted value can't be read as a
# CLI flag, and shell metacharacters/whitespace/path separators are excluded
# entirely (CodeQL alert #4, py/command-line-injection). The pattern is
# deliberately inlined at each guard rather than precompiled -- CodeQL's
# barrier-guard analysis recognizes the `re.fullmatch(...)` call form but
# NOT the `.fullmatch` method of a precompiled `re.Pattern`.

# Maps a core native component to the *stable* Homebrew formula its service
# is named after, for native/local-first mode health-checks/heals.
#
# This used to be a hand-maintained copy of ops.NATIVE_BREW_SERVICES, and the
# two agreeing by convention is what shipped #3853: a candidate install
# registers `nyxgpt-api@3.0.0rc`, neither copy knew versioned names existed,
# and both looked up a service nothing had registered. Both now import the
# one definition from `nyxgpt.brew_services`, which sits below both (ops.py
# already imports this module, so the shared vocabulary cannot live in
# either) -- the same shape D-022 settled for `k8s_pod_state.py`.
#
# Never look a component up by this name directly: resolve it against a live
# `brew services list` snapshot with `brew_services.resolve`.
NATIVE_BREW_SERVICES = brew_services.NATIVE_BREW_SERVICES
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

# Namespace the Kubernetes deployment lives in. Mirrors ops.K8S_NAMESPACE.
K8S_NAMESPACE = "nyxgpt"

# Which core component a Pod belongs to, keyed by its `app` label (see
# k8s/deployment-{stable,canary}.yaml, deployment-web-{stable,canary}.yaml,
# statefulset-cassandra.yaml, statefulset-ollama.yaml).
#
# All four core tiers, not just the api pool (#3828): the probe used to select
# `app=nyxgpt-api-canary-pool` alone, so in Kubernetes mode web, Cassandra and
# Ollama were watched by nothing at all -- the same "Pods Running is not a
# working nyxGPT" gap #3786 found in the installer, on the observability side.
K8S_CORE_POD_APPS: dict[str, str] = {
    "nyxgpt-api-canary-pool": "api",
    "nyxgpt-web-canary-pool": "web",
    "cassandra": "cassandra",
    "ollama": "ollama",
}

# Pod-template label every workload in `k8s/observability/` carries (#3787).
# Keyed on rather than an explicit workload list so a workload added to that
# overlay is watched without a matching edit here.
K8S_OBSERVABILITY_TIER_LABEL = "observability"

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

    `known` is `False` for a component whose real state could not be
    determined from here at all -- the probe that would answer the question
    failed to run (`state="unknown"`, `note` carrying the reason, e.g.
    "docker daemon unreachable -- `docker compose ps` exited 125"). It is
    *not* a health verdict: `healthy=False` on an unknown entry means "not
    established as healthy", never "down". Nothing may count an unknown
    component as unhealthy or act on it automatically -- see
    `_record_health_check` and `heal_now`. This is the distinction #3812
    added after eleven healthy observability containers were reported
    `absent`, and counted as "11 unhealthy", purely because the API process's
    session could not reach the Docker daemon.

    `tier` is populated for Kubernetes rows only, where the row is named
    after a Pod rather than after a component: `"core"` for the api/web/
    cassandra/ollama Pods and `"observability"` for a `k8s/observability/`
    workload's Pod. It is what lets `detected_mode` recognize a Kubernetes
    deployment (a Pod name is never in `CORE_APP_SERVICES`, which is why the
    Self-Heal page used to print "Nothing detected running" above a list of
    running Pods -- #3828) and what keeps an observability-only cluster from
    being read as a core deployment. Empty for every other source, whose rows
    are already named by component.

    `note` is an informational annotation, empty unless another mode's
    entry for this same component was shadowed by this one (see
    `_resolve_core_component_conflicts` / #3428) -- e.g. a stopped native
    `nyxgpt-cassandra` container left in place while Terraform mode is
    actually running Cassandra -- or unless `known` is `False`, in which case
    it carries why the state could not be determined, or unless `healable` is
    `False`, in which case it carries the cluster's own reason. It never
    affects `healthy`/`state`/`source`, which always describe the winning
    (active) entry only.

    `healable` is `False` when this component is unhealthy and *no action this
    module has* would converge it -- today that is a `Pending` Kubernetes Pod
    (#3832): an unschedulable one cannot be fixed by deleting it (deletion
    does not create capacity, and the replacement is Pending for the identical
    reason), and a starting one resolves itself. Unlike `desired=False`, a
    manual "Heal now" does *not* override it: the action would not be a repair
    for an operator either, and the loop that took it deleted seven Pods in
    4.5 minutes while erasing the Pod age an operator needed to diagnose the
    real cause. The reason goes in `note`, which is what the dashboard shows.

    `heal_key` is the stable identity heal attempts are budgeted against, when
    that differs from `service` -- empty means "same as `service`". A
    Kubernetes Pod is the case that needs it: healing *replaces* the Pod, so
    the next pass sees a different `service` name and, keyed by name, a fresh
    restart budget. Keyed by the owning ReplicaSet instead, the budget spans
    the recreations (#3832).
    """

    service: str
    container: str
    state: str
    health: str
    healthy: bool
    source: str = "compose"
    desired: bool = True
    note: str = ""
    known: bool = True
    tier: str = ""
    healable: bool = True
    heal_key: str = ""

    @property
    def budget_key(self) -> str:
        """Key this component's restart counters/attempt budget are stored under."""
        return self.heal_key or self.service

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
            "known": self.known,
            "tier": self.tier,
            "healable": self.healable,
            "heal_key": self.budget_key,
        }


@dataclass(frozen=True)
class ComposeProbe:
    """The result of one `docker compose ps` survey, and whether it could run at all.

    `available` answers "did the survey actually run?", not "is the binary
    installed?" -- see `compose_probe()` for why that distinction is the
    whole point (#3812). When `available` is `False`, `statuses` is empty and
    means nothing: it is the absence of an answer, not an answer of absence.
    `reason` is a short operator-facing sentence, empty when available.
    """

    available: bool
    reason: str = ""
    statuses: tuple[ComponentStatus, ...] = ()


@dataclass(frozen=True)
class ComponentSurvey:
    """Every component's status for one pass, plus the Compose probe behind it.

    `heal_now()` and `status()` both need the component rows *and* whether
    the Compose half of them could be determined. Bundling them means one
    `docker compose ps` per pass answers both, instead of the two
    independent calls that let the flag and the rows disagree in the first
    place.
    """

    components: list[ComponentStatus]
    compose_probe: ComposeProbe


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

    The bound has always been here, but until #3858 an expired one raised
    `subprocess.TimeoutExpired` straight past every caller -- including
    `/self-heal/status` and `/admin/overview`, where it is a 500 on a status
    read. It is now the same `TIMEOUT_RETURNCODE` result every other bounded
    helper returns (see `subprocess_bounds`), so "the command hung" reads as a
    failed probe like any other rather than as a traceback.
    """
    cmd = bounded_argv(cmd, timeout)
    try:
        result = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            f"Subprocess {timeout_message(timeout)}: {' '.join(cmd)}",
            extra={"component": "self_heal", "cmd": cmd, "timeout_seconds": timeout},
        )
        return timeout_result(cmd, exc, timeout)
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
        # Timestamps of recent automatic heal actions per budget key, for the
        # rolling-window cap (#3832) -- see DEFAULT_HEAL_ATTEMPT_WINDOW_SECONDS.
        "heal_attempts": {},
        # Last reason logged per budget key for a component no heal action
        # converges (#3832). The watchdog passes every ~15s and an
        # unschedulable Pod stays unschedulable until a human adds capacity,
        # so warning every pass is four identical lines a minute for as long
        # as the condition lasts -- noise that buries the transition an
        # operator needs to see. Warn on the transition (or a changed reason),
        # log the repeats at DEBUG. Kept with the state rather than in memory
        # so a watchdog restart does not re-announce a condition that never
        # changed.
        "unhealable_reported": {},
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
    return brew_services.parse_services_list(cp.stdout or "")


def _dev_mode_active() -> bool:
    """True if this machine's native api/web were installed in dev mode (#3789)."""
    return read_install_mode().is_dev


def _dev_launchagent_plist(label: str) -> Path:
    """Where `_install_dev_launchagent` (ops.py) puts the dev-mode plist for `label`."""
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_agent_loaded(label: str) -> bool:
    """True if LaunchAgent `label` is currently loaded, per `launchctl list`.

    False on any failure (launchctl missing, non-zero exit) -- the same
    "nothing to see, don't act on a guess" convention
    `_brew_services_snapshot` uses.
    """
    if _which("launchctl") is None:
        return False
    try:
        cp = _run(["launchctl", "list"], expected=True)
    except Exception as e:
        logger.warning("self-heal: failed to query launchctl list: %s", e)
        return False
    if cp.returncode != 0:
        return False
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[-1] == label:
            return True
    return False


def _restart_launchagent(label: str) -> HealResult:
    """Restart LaunchAgent `label` via `launchctl kickstart -k` (dev mode's api/web)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid LaunchAgent label: {label!r}")
    if _which("launchctl") is None:
        return HealResult(False, f"launchctl not found; cannot restart {label}")
    try:
        cp = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], timeout=60.0)
    except Exception as e:
        return HealResult(False, f"Failed to restart {label}", f"{type(e).__name__}: {e}")
    if cp.returncode != 0:
        details = (cp.stdout or "").strip() + (
            "\n" + (cp.stderr or "").strip() if (cp.stderr or "").strip() else ""
        )
        return HealResult(False, f"Failed to restart LaunchAgent: {label}", details.strip())
    return HealResult(True, f"Restarted LaunchAgent: {label}")


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


def _system_ollama_service_active() -> bool:
    """True if a distro-managed system-wide `ollama.service` is currently active.

    Mirrors `ops._system_ollama_service_active()` -- kept as a separate copy
    here for the same reason `NATIVE_SYSTEMD_SERVICES` is (see its comment).
    Checked with plain `systemctl is-active` (system scope, no `--user`).
    False (never raises) if systemctl is missing or the query fails.
    """
    if _which("systemctl") is None:
        return False
    cp = _run(["systemctl", "is-active", "ollama.service"], expected=True)
    return (cp.stdout or "").strip() == "active"


def _native_container_state(name: str) -> str:
    """Return the docker state ('running', 'exited', ...) for container `name`.

    Returns `"absent"` only when `docker` actually answered and reported no
    such container, and `"unknown"` when the question could not be put to
    Docker at all -- no binary, an unreachable daemon, a refused socket. The
    two are not interchangeable (#3812): folding an unqueryable daemon into
    "absent" is how a running container gets reported as gone. Callers skip
    `"unknown"` exactly as they skip `"absent"`, but from a state they can
    recognize rather than one that reads as a fact.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):  # inline barrier, CodeQL #4
        return "absent"
    if _which("docker") is None:
        return "unknown"
    try:
        cp = _run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"],
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to query docker state for %s: %s", name, e)
        return "unknown"
    if cp.returncode != 0:
        return "unknown"
    out = (cp.stdout or "").strip()
    return out.splitlines()[0].strip() if out else "absent"


def _native_container_health(name: str) -> str:
    """Return the Docker `HEALTHCHECK` status for container `name` (``''`` if none/unavailable)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):  # inline barrier, CodeQL #4
        return ""
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
        if state in ("absent", "unknown"):
            # "unknown" = Docker could not be asked (#3812); reported as
            # nothing here rather than as a container that isn't there.
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


def _kubernetes_pod_tier(labels: dict[str, Any]) -> str:
    """Classify one Pod from its labels: `"core"`, `"observability"` or `""`.

    `""` means "not ours" -- a Pod in the `nyxgpt` namespace that belongs to
    neither the core tiers nor the observability overlay (someone else's
    workload, a one-shot Job) is not something this watchdog may delete.
    """
    if labels.get("tier") == K8S_OBSERVABILITY_TIER_LABEL:
        return "observability"
    if labels.get("app") in K8S_CORE_POD_APPS:
        return "core"
    return ""


def _list_kubernetes_component_status(already_managed: set[str]) -> list[ComponentStatus]:
    """Health-check every Kubernetes-managed nyxGPT Pod via `kubectl get pods`.

    Covers `nyxgpt ops install --kubernetes --local` (see `k8s/`): the api and
    web stable/canary Deployments, the Cassandra and Ollama StatefulSets, and
    the in-cluster observability overlay (`k8s/observability/`, #3787) --
    every tier the install deploys, keyed by Pod label
    (`_kubernetes_pod_tier`). It used to select `app=nyxgpt-api-canary-pool`
    alone, which left everything but the api pool unobserved in Kubernetes
    mode (#3828).

    One `ComponentStatus` per Pod, not per Deployment -- `stable` alone can
    run several replicas, and each needs its own backoff/restart-count
    bookkeeping in `heal_now`. Healed via `kubectl delete pod`, which the
    Pod's Deployment/ReplicaSet/StatefulSet/DaemonSet then recreates -- on top
    of, not instead of, kubelet's own liveness-probe restarts and
    `canary.py`'s metrics-gated rollout + auto-rollback (see the module
    docstring). One `kubectl get pods` for the whole namespace, so widening
    the coverage costs no extra calls per pass.

    A Pod that is already terminating (`metadata.deletionTimestamp` set) is
    skipped: its replacement is on the way, and a Pod on its way out reads as
    "Running but not Ready" -- healing it again would spend a restart-budget
    attempt on a deletion that has already happened.

    What each Pod's state *means* is `nyxgpt.k8s_pod_state`'s job, shared with
    `ops.py` (#3832), and two of its distinctions land here:

    - a Pod whose state deletion cannot repair -- any `Pending` one, whether
      unschedulable or still pulling its image -- is reported `healable=False`
      with the cluster's own reason in `note`, so the operator sees
      "Insufficient memory" on the dashboard instead of a Pod that silently
      gets younger every 15 seconds;
    - `heal_key` is the owning ReplicaSet, not the Pod name, so the restart
      budget in `heal_now` survives the recreate that healing causes.

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
            ["kubectl", "get", "pods", "-n", K8S_NAMESPACE, "-o", "json"],
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
    items = data.get("items", []) if isinstance(data, dict) else []
    for pod in items:
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata", {})
        name = metadata.get("name", "")
        if not name or name in already_managed or metadata.get("deletionTimestamp"):
            continue
        tier = _kubernetes_pod_tier(metadata.get("labels") or {})
        if not tier:
            continue
        pod_state = classify_pod(pod)
        healable = pod_state.healthy or pod_state.deletion_may_recover
        statuses.append(
            ComponentStatus(
                service=name,
                container=name,
                state=pod_state.phase,
                health=pod_state.health_label,
                healthy=pod_state.healthy,
                source="kubernetes",
                tier=tier,
                healable=healable,
                heal_key=f"kubernetes/{pod_state.workload}",
                note="" if healable else _unhealable_pod_note(pod_state),
            )
        )
    return statuses


def _unhealable_pod_note(pod_state: PodState) -> str:
    """Operator-facing line for a Pod self-heal will not act on (#3832).

    Says what the cluster said, then what self-heal did about it and why --
    because "unhealthy, not healed" with no explanation is the state the
    operator was left to guess at while the watchdog quietly churned.
    """
    if pod_state.unschedulable:
        return (
            f"{pod_state.summary()} -- not healed: deleting a Pod cannot create capacity, "
            "and its replacement would be unschedulable for the same reason. Add node "
            "capacity or lower the workload's resource requests."
        )
    if pod_state.starting:
        return f"{pod_state.summary()} -- not healed: still starting, it converges on its own."
    return (
        f"{pod_state.summary()} -- not healed: deleting a Pod is only a repair for one that is "
        "Running but not Ready; this one is the controller's to replace."
    )


def kubernetes_mode_active(components: list[ComponentStatus]) -> bool:
    """True if `components` shows a core tier running as Kubernetes Pods.

    The single condition behind everything that has to behave differently in
    Kubernetes mode: the deployment mode the Self-Heal page prints, and
    whether the observability tier is read from the cluster or from Compose
    (`component_survey`/`status`). Observability Pods alone do not make it
    true -- `nyxgpt ops observability --kubernetes` can put that tier on a
    cluster while the core stack runs natively, and that deployment's core
    components are still native ones.
    """
    return any(c.source == "kubernetes" and c.tier == "core" for c in components)


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
        # Resolved against what `brew services list` actually reports, never
        # taken from the constant: a candidate install registers
        # `nyxgpt-api@3.0.0rc`, and reading the stable name reported every
        # component of a running rc stack as not installed -- so `nyxgpt up`
        # burned its whole timeout and exited 2 on a healthy machine (#3853).
        native_names = brew_services.resolve_all(NATIVE_BREW_SERVICES, native_snapshot)
        if _dev_mode_active():
            # A dev install (#3789) runs api/web under their own
            # LaunchAgents -- there is no keg for `brew services` to attach
            # to. Reading brew here would report the *keg's* leftover state
            # (or nothing) for the processes actually serving, and healing
            # from that would `brew services restart` the old build straight
            # onto the port the dev process holds.
            for component, label in DEV_LAUNCHD_LABELS.items():
                native_names[component] = label
                if not _dev_launchagent_plist(label).exists():
                    # Never installed here -- out of scope, exactly as an
                    # absent brew service is (the loop below skips a
                    # component with no snapshot entry).
                    native_snapshot.pop(label, None)
                    continue
                native_snapshot[label] = "started" if _launchd_agent_loaded(label) else "stopped"
    for component, native_name in native_names.items():
        if component == "ollama" and _is_linux() and _system_ollama_service_active():
            # A system-wide ollama.service still holding port 11434 (#3632).
            # `nyxgpt ops install` normally takes that port over -- it stops
            # and disables the system unit so nyxgpt-ollama.service can own
            # it (ops._takeover_system_ollama_service) -- so reaching here
            # means the takeover could not complete (no passwordless root),
            # and `ops doctor` is reporting the conflict with the command
            # that resolves it. Until then, report health via the unit that
            # is actually serving rather than restart-looping a
            # nyxgpt-ollama.service that can only lose the port race: fighting
            # the current arrangement would just crash-loop, and Ollama *is*
            # reachable on 11434 either way.
            statuses.append(
                ComponentStatus(
                    service="ollama",
                    container="ollama.service",
                    state="active",
                    health="",
                    healthy=True,
                    source="native",
                )
            )
            continue
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
    if state not in ("absent", "unknown"):  # unknown = Docker unreachable, not "gone" (#3812)
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
    state, mirroring the exemption `_parse_compose_ps` already
    applies on the present side).

    `_mark_disabled_present_services` calls this with `exclude_one_shot=False`
    -- it uses the result to recognize which *present* services belong to
    some observability profile at all, and a one-shot job that's present
    (i.e. it failed, since a successful one is never present -- see
    `_parse_compose_ps`) must stay recognized so a disabled
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


def observability_services() -> set[str]:
    """Compose service names owned by the currently-enabled observability profiles.

    The public form of `_desired_compose_services(_enabled_observability_profiles())`,
    for callers that deliberately did *not* start the observability stack and
    therefore must not treat it as pending -- `ops.up(--skip-observability)`'s
    health-wait being the one that needs it. Core app services are already
    excluded (see `_desired_compose_services`), so a Compose-mode `api`/`web`
    is never in here and never gets skipped by mistake.

    Returns an empty set when the profiles can't be resolved (no Docker, a
    Compose failure), which is the module's usual "can't tell, so don't act"
    fallback: the caller then waits on those components rather than silently
    ignoring something it can't account for.
    """
    return _desired_compose_services(_enabled_observability_profiles())


def _absent_desired_statuses(
    present_services: set[str], desired_services: set[str]
) -> list[ComponentStatus]:
    """Build a `ComponentStatus` for every desired observability service missing entirely.

    `present_services` is whatever `compose_probe()`'s survey already
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


def _unknown_desired_statuses(desired_services: set[str], reason: str) -> list[ComponentStatus]:
    """Build an `unknown` `ComponentStatus` per desired service when the probe couldn't run.

    The honest counterpart to `_absent_desired_statuses`, and the fix for
    #3812: "the survey could not run" and "the survey ran and found nothing"
    produce identical empty results, so which of the two happened has to come
    from the probe (`ComposeProbe.available`), not from the emptiness. When
    it could not run, every desired service is reported `known=False`,
    `state="unknown"`, carrying `reason` -- so the dashboard says "cannot
    determine from here, because X" instead of asserting eleven running
    containers are gone.

    `healthy=False` here means "not established as healthy", and callers must
    treat it that way: `_record_health_check` leaves unknowns out of the
    unhealthy count and `heal_now`'s automatic pass will not act on them.
    """
    return [
        ComponentStatus(
            service=service,
            container="",
            state="unknown",
            health="",
            healthy=False,
            source="compose",
            desired=True,
            note=reason,
            known=False,
        )
        for service in sorted(desired_services)
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

    Combines `compose_probe()`'s survey (whatever's currently up
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
    return component_survey().components


def component_survey() -> ComponentSurvey:
    """`list_component_status()` plus the Compose probe that produced its Compose half.

    Callers that only want the rows can keep using `list_component_status()`;
    callers that must also say *why* a row is unknown (`status()`, and
    `heal_now()`, which must not act on one) use this, so a single `docker
    compose ps` answers both questions and they cannot disagree (#3812).

    When the probe could not run, the desired observability services are
    reported unknown-with-reason (`_unknown_desired_statuses`) rather than
    absent -- an unqueryable stack is never rendered as a definite negative.
    """
    probe = compose_probe()
    compose_statuses = list(probe.statuses)
    compose_managed = {s.service for s in compose_statuses}

    desired_services = _desired_compose_services(_enabled_observability_profiles())
    compose_statuses = _mark_disabled_present_services(compose_statuses, desired_services)
    if probe.available:
        undetermined_statuses = _absent_desired_statuses(compose_managed, desired_services)
    else:
        undetermined_statuses = _unknown_desired_statuses(desired_services, probe.reason)

    native_statuses = _list_native_component_status()
    terraform_statuses = _list_terraform_component_status()
    core_statuses = _resolve_core_component_conflicts(
        compose_statuses + native_statuses + terraform_statuses
    )
    core_managed = {s.service for s in core_statuses}
    kubernetes_statuses = _list_kubernetes_component_status(core_managed)

    if kubernetes_mode_active(kubernetes_statuses):
        # Kubernetes mode: the observability tier runs *in-cluster* (#3787)
        # and is already reported above, Pod by Pod, by the same survey. The
        # Compose placeholders assert that the enabled profiles ought to be
        # running as Compose services here, which is false in this mode --
        # they rendered as a screen of "can't check"/"absent" rows for
        # workloads that were up and queryable all along (#3828).
        undetermined_statuses = []

    all_statuses = core_statuses + kubernetes_statuses + undetermined_statuses

    stopped = set(list_intentionally_stopped())
    if stopped:
        all_statuses = [
            replace(s, desired=False) if s.desired and s.service in stopped else s
            for s in all_statuses
        ]
    return ComponentSurvey(components=all_statuses, compose_probe=probe)


def _compose_probe_failure_detail(cp: subprocess.CompletedProcess[str]) -> str:
    """One-line operator-facing reason for a non-zero `docker compose ps`.

    Prefers Docker's own last stderr line (e.g. "permission denied while
    trying to connect to the Docker daemon socket ...") over the bare exit
    code, because that line is the actionable half: exit 125 alone doesn't
    tell an operator their session is missing the `docker` group. Falls back
    to the exit code when the command failed silently. Kept to a single
    trimmed line -- this string is rendered in the dashboard, not a log pane.
    """
    detail = f"`docker compose ps` exited {cp.returncode}"
    stderr_lines = [line.strip() for line in (cp.stderr or "").splitlines() if line.strip()]
    if stderr_lines:
        tail = stderr_lines[-1]
        if len(tail) > 200:
            tail = tail[:197] + "..."
        detail = f"{detail}: {tail}"
    elif not COMPOSE_FILE.exists():
        # #3588's case: the Compose file isn't reachable from this vantage
        # point (e.g. a Terraform-managed api container missing the bind
        # mount). Named explicitly when Docker itself said nothing useful,
        # because the path is the actionable half there.
        detail = f"{detail}: the Compose file is not reachable from here ({COMPOSE_FILE})"
    return detail


def compose_probe() -> ComposeProbe:
    """Run the Compose observability survey, reporting whether it *could* run.

    This is the single source of both halves -- the component rows and
    whether they mean anything -- because they are the same question asked
    once. `available=False` means `statuses` being empty carries no
    information about what is running, and callers must render the affected
    components as unknown rather than absent (see `_unknown_desired_statuses`).

    Three ways the survey can fail to run, each with its own `reason`:

    - **No `docker` on PATH.** Nothing to ask.
    - **`COMPOSE_FILE` not reachable from here.** Exactly what happened to a
      Terraform-managed `nyxgpt-tf-api` container before it got the same
      `docker-compose.yml` bind mount + `NYXGPT_COMPOSE_FILE` env var
      docker-compose.yml's own `api` service already sets (#3588).
    - **The command itself failed** -- a non-zero exit or an exception. This
      is the case #3588's existence checks structurally could not see and
      #3812 filed: on the rc12 Terraform/cloud install, `docker` was on PATH
      and the compose file was present, but the `systemd --user` session
      predated the `ec2-user` docker-group change, so every `docker compose
      ps` exited 125 (daemon unreachable). Both existence checks passed, the
      probe reported "available", the survey returned nothing, and eleven
      running-and-healthy observability containers were rendered `absent` and
      counted "11 unhealthy". A probe that says it can run must have run.

    Never raises: an unqueryable stack is a reportable state, not an error.
    """
    if _which("docker") is None:
        return ComposeProbe(available=False, reason="`docker` is not installed on this host")
    try:
        cp = _run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "-a", "--format", "json"],
            expected=True,
        )
    except Exception as e:
        reason = f"`docker compose ps` could not be run: {type(e).__name__}: {e}"
        logger.warning(
            "self-heal: %s -- observability survey unavailable this pass",
            reason,
            extra={"component": "self_heal", "compose_probe_reason": reason},
        )
        return ComposeProbe(available=False, reason=reason)
    if cp.returncode != 0:
        reason = _compose_probe_failure_detail(cp)
        logger.warning(
            "self-heal: %s querying %s -- observability survey unavailable this pass, "
            "components reported unknown rather than absent",
            reason,
            COMPOSE_FILE,
            extra={
                "component": "self_heal",
                "compose_probe_reason": reason,
                "returncode": cp.returncode,
            },
        )
        return ComposeProbe(available=False, reason=reason)
    return ComposeProbe(available=True, reason="", statuses=tuple(_parse_compose_ps(cp.stdout)))


def compose_probe_available() -> bool:
    """Whether the Compose observability survey can actually run from this process.

    Thin wrapper over `compose_probe()`, kept because `ops.infra_status()`
    and the dashboards want the boolean on its own. Prefer `compose_probe()`
    where the failure reason is also wanted -- it costs the same one probe.
    """
    return compose_probe().available


def _parse_compose_ps(stdout: str | None) -> list[ComponentStatus]:
    """Turn `docker compose ps -a --format json` output into `ComponentStatus` rows.

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
    statuses: list[ComponentStatus] = []
    for line in (stdout or "").splitlines():
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

    A component whose state could not be determined at all (`known=False` --
    the Compose probe could not run, see `compose_probe`) is likewise not
    counted, and its gauge is left at whatever the last successful pass
    observed rather than being set either way (#3812): reporting it unhealthy
    would alarm an operator about containers that are very likely running,
    and reporting it healthy would assert something this pass did not
    establish. Leaving the series stale is the standard Prometheus reading of
    "this could not be scraped", and keeps `SELFHEAL_UNHEALTHY_COMPONENTS` a
    count of what is genuinely known to be broken.
    """
    unhealthy = 0
    for s in statuses:
        is_unhealthy = not s.healthy and s.desired and s.known
        if is_unhealthy:
            unhealthy += 1
        logger.debug(
            "self-heal: health check %s healthy=%s state=%s health=%s known=%s",
            s.service,
            s.healthy,
            s.state,
            s.health or "n/a",
            s.known,
            extra={
                "component": "self_heal",
                "service": s.service,
                "healthy": s.healthy,
                "state": s.state,
                "health": s.health,
                "known": s.known,
            },
        )
        if not s.known:
            continue
        prom_metrics.SELFHEAL_COMPONENT_HEALTHY.labels(service=s.service).set(
            0.0 if is_unhealthy else 1.0
        )
    prom_metrics.SELFHEAL_UNHEALTHY_COMPONENTS.set(unhealthy)
    prom_metrics.SELFHEAL_LAST_CHECK_TIMESTAMP.set(time.time())
    return unhealthy


def restart_component(service: str) -> HealResult:
    """Restart a single Compose service: `docker compose restart <service>`."""
    # Inline barrier (CodeQL #4, py/command-line-injection): an externally-
    # influenced service name must be validated *in this function* so the
    # taint tracker recognizes the guard on the value that reaches the argv.
    # A prior helper-based check sanitized the same way but through one level
    # of indirection CodeQL doesn't trace, so the alert stayed open.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", service):
        return HealResult(False, f"Refused to act on invalid service name: {service!r}")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", service):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid service name: {service!r}")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid service name: {name!r}")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid container name: {name!r}")
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
    for s in compose_probe().statuses:
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

    Restarting the container is the whole Cassandra remedy, and it is
    sufficient: the API rebuilds its own client state, because the shared
    connection pool treats a shut-down driver `Cluster` as absent and
    rebuilds it (`CassandraConnectionPool._connect`, #3851). Until that fix
    a restart here left the API reporting `cassandra: unreachable` forever.
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
    if component in DEV_LAUNCHD_LABELS and _dev_mode_active():
        # Dev mode's api/web are LaunchAgents, not brew services (#3789) --
        # `brew services restart` here would start the old keg's build on the
        # port the dev process is already serving on.
        return _restart_launchagent(DEV_LAUNCHD_LABELS[component])
    base = NATIVE_BREW_SERVICES.get(component)
    if base is None:
        return HealResult(False, f"Unknown native component: {component}")
    # The heal has to act on the service the *probe* found unhealthy, which
    # on a candidate install is `nyxgpt-api@3.0.0rc` (#3853). Restarting the
    # stable name instead would either fail against a formula that is not
    # installed or -- worse, on a machine still carrying an older release's
    # keg -- start that keg onto the port the candidate is serving on.
    brew_name = brew_services.resolve(component, base, _brew_services_snapshot())
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


def _kubernetes_pod_state(pod_name: str) -> PodState | None:
    """Read one Pod's current state, or `None` if it could not be read at all.

    Deliberately its own `kubectl get pod` rather than a value threaded down
    from the survey: `heal_kubernetes_pod` is a public, destructive entry
    point reached from the dashboard's per-component button as well as from
    the watchdog, and the guard it needs has to hold for callers that never
    looked at a survey (#3832). `None` means "could not tell", which the
    caller must treat as "do not act" -- the same rule as #3812's Compose
    probe, applied to a delete.
    """
    # Inline barrier (CodeQL #4, py/command-line-injection) -- repeated here
    # rather than relied on from the caller, per this module's convention: the
    # value goes on a `kubectl` argv in THIS function.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pod_name):
        return None
    if _which("kubectl") is None:
        return None
    try:
        cp = _run(
            ["kubectl", "get", "pod", pod_name, "-n", K8S_NAMESPACE, "-o", "json"],
            timeout=15.0,
            expected=True,
        )
    except Exception as e:
        logger.warning("self-heal: failed to read state of pod %s: %s", pod_name, e)
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception as e:
        logger.warning("self-heal: failed to parse kubectl get pod output for %s: %s", pod_name, e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
        return None
    return classify_pod(data)


def heal_kubernetes_pod(pod_name: str) -> HealResult:
    """Heal a Kubernetes-managed Pod by deleting it: `kubectl delete pod <name>`.

    The owning Deployment's ReplicaSet recreates the Pod -- this is a
    stronger recovery action than kubelet's own in-place container restart
    on a failed liveness probe, useful for a Pod that's stuck rather than
    cleanly crash-looping.

    **Only ever deletes a `Running` Pod** (#3832). The Pod's state is re-read
    here, immediately before the delete, and anything else is refused with the
    cluster's own reason: a `Pending` Pod has no stuck container to recover,
    and if it is unschedulable the delete cannot create the capacity it is
    waiting for -- the ReplicaSet just makes another Pending Pod, which is how
    seven Pods were destroyed in 4.5 minutes with the Pod age (the operator's
    evidence) reset each time. The guard lives at the action, not only at the
    caller that decided to call it, so no future caller can reintroduce the
    loop; `_list_kubernetes_component_status` additionally never *offers* such
    a Pod to the automatic pass (`healable=False`).

    An explicit operator "Heal now" on a Running-and-Ready Pod is still a
    recycle, not a repair, and is allowed: the automatic pass only ever
    targets unhealthy components, so nothing in the loop reaches that case.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pod_name):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid pod name: {pod_name!r}")
    if _which("kubectl") is None:
        return HealResult(False, f"kubectl not found; cannot heal pod {pod_name}")

    pod_state = _kubernetes_pod_state(pod_name)
    if pod_state is None:
        return HealResult(
            False,
            f"Could not read the state of pod {pod_name}; refusing to delete it blind",
            "`kubectl get pod -o json` did not return a readable Pod -- an unread state is "
            "not a reason to destroy one.",
        )
    if not pod_state.running:
        return HealResult(
            False,
            f"Refused to delete pod {pod_name}: {pod_state.summary()}",
            _unhealable_pod_note(pod_state),
        )

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


def _compose_file_services() -> set[str]:
    """Service names declared in COMPOSE_FILE's top-level `services:` block.

    Parsed with a line scanner rather than a YAML library (PyYAML is not a
    runtime dependency): top-level keys start in column 0, and service names
    are the 2-space-indented keys directly inside `services:`. Returns an
    empty set when the compose file is missing/unreadable -- the same
    "can't tell, so don't act" fallback the rest of this module uses.
    """
    try:
        text = COMPOSE_FILE.read_text(encoding="utf-8")
    except OSError:
        return set()
    services: set[str] = set()
    in_services = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            in_services = line.startswith("services:")
            continue
        if in_services:
            m = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9._-]*):\s*(#.*)?$", line)
            if m:
                services.add(m.group(1))
    return services


def _compose_component_logs(service: str, *, tail: int = 200) -> HealResult:
    """Fetch recent logs for a Compose service: `docker compose logs <service>`.

    The fallback (and, for any service `component_logs` doesn't recognize as
    native/Terraform/Kubernetes-managed -- e.g. an observability container
    like `glitchtip`/`grafana`/`loki` -- the primary) path for reading a
    container's output without the user needing to run a raw `docker`/
    `docker compose` command themselves.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", service):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid service name: {service!r}")
    if _which("docker") is None:
        return HealResult(False, "docker not found; cannot fetch logs")
    # Resolve the (client-influenced) name against the compose file's own
    # service list and pass the DECLARED string to the argv, never the
    # client's (CodeQL #4, py/command-line-injection): regex validation is
    # not in this query's barrier set in any form, but equality-selection
    # from a trusted local collection removes the tainted value from the
    # flow entirely -- the same idiom heal_now uses with probe statuses.
    for declared in sorted(_compose_file_services()):
        if declared == service:
            service = declared
            break
    else:
        return HealResult(
            False,
            f"Unknown compose service: {service!r}",
            f"not declared in {COMPOSE_FILE.name}; no logs to fetch",
        )
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", container):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid container name: {container!r}")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pod_name):  # inline barrier, CodeQL #4
        return HealResult(False, f"Refused to act on invalid pod name: {pod_name!r}")
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
    `_docker_container_logs` just like a Terraform component.

    `api` combines its own structured logs (`~/.nyxGPT/logs/api.log`) with
    its native service's raw stdout/stderr, as labeled sections -- same
    pattern as `web` below (#3629). This matters because a startup failure
    that happens *before* `configure_logging` runs (e.g. the P6-1 bind-
    address refusal in `app.py`'s lifespan, see #3500) is printed straight to
    the process's stderr and never reaches `api.log` at all -- without the
    stderr section, `nyxgpt ops logs api` would report a hollow "no logs"
    while the actual reason the API is down sits in a native log path the
    operator has to hunt for by hand, exactly what this function exists to
    avoid. Read from Homebrew's launchd log path on macOS (`nyxgpt-api.log`/
    `.err.log` -- see docs/homebrew.md#api-logs), or the same names under
    `~/.nyxGPT/logs` on Linux, where `nyxgpt-api.service`'s
    `StandardOutput`/`StandardError` already point there directly (#3508).

    `ollama` reads only its aggregated `~/.nyxGPT/logs/ollama.log` -- no
    separate stderr section, unlike `api`/`web`: the log-follower agent that
    maintains it (`scripts/follow-ollama-logs.sh`, see docs/api.md#ollama-
    logs) already tails its native source with `2>&1`, so stdout and stderr
    are combined into that one file by construction rather than split across
    two native files the way `api`/`web`'s launchd/systemd logs are.

    `web` has no structured logging of its own yet (#3430), so it's read
    from its native service's own stdout/stderr: Homebrew's launchd log
    path on macOS (`nyxgpt-web.log`/`.err.log` -- see
    docs/homebrew.md#web-ui-logs), or the same names under `~/.nyxGPT/logs`
    on Linux, where `nyxgpt-web.service`'s `StandardOutput`/`StandardError`
    already point there directly (#3508).
    """
    if service == "cassandra":
        return _docker_container_logs(NATIVE_CASSANDRA_CONTAINER, tail=tail)

    if service == "ollama":
        log_file = get_log_dir() / "ollama.log"
        if not log_file.exists():
            return HealResult(False, f"No log file found for {service}: {log_file}")
        return HealResult(
            True,
            f"Fetched last {tail} log line(s) for {service}",
            _tail_text_file(log_file, tail),
        )

    if service == "api":
        structured_log = get_log_dir() / "api.log"
        if _is_linux():
            out_file: Path | None = get_log_dir() / "nyxgpt-api.log"
            err_file: Path | None = get_log_dir() / "nyxgpt-api.err.log"
        else:
            prefix = _brew_prefix()
            out_file = Path(prefix, "var", "log", "nyxgpt-api.log") if prefix else None
            err_file = Path(prefix, "var", "log", "nyxgpt-api.err.log") if prefix else None

        sections = []
        if structured_log.exists():
            sections.append(
                f"--- api.log ({structured_log}) ---\n{_tail_text_file(structured_log, tail)}"
            )
        if out_file is not None and out_file.exists():
            sections.append(f"--- stdout ({out_file}) ---\n{_tail_text_file(out_file, tail)}")
        if err_file is not None and err_file.exists():
            sections.append(f"--- stderr ({err_file}) ---\n{_tail_text_file(err_file, tail)}")

        if not sections:
            return HealResult(False, f"No log files found for api: {structured_log}")
        return HealResult(True, f"Fetched last {tail} log line(s) for api", "\n".join(sections))

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
    # Inline barrier (CodeQL #4, py/command-line-injection): the SRE-dashboard
    # `GET /self-heal/logs` endpoint passes its `service` query parameter
    # straight through to this function, and the `_compose_component_logs`
    # fallback below puts it directly on a `docker compose logs` argv. Same
    # pattern/message as restart_component/_bring_up_compose_service/
    # heal_kubernetes_pod -- validated here, in this function, so the taint
    # tracker recognizes the guard on the value that reaches the argv.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", service):
        return HealResult(False, f"Refused to act on invalid service name: {service!r}")
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


def _prune_heal_attempts(
    state: dict[str, Any], now: float, window_seconds: float
) -> dict[str, list[float]]:
    """Drop heal-attempt timestamps older than the window, in place.

    Also drops keys that empty out, so the state file does not accumulate one
    entry per Pod name ever seen -- the leak the old per-name `restart_counts`
    bookkeeping had (#3832). Tolerates a malformed/hand-edited state file the
    same way `_load_state` does: anything unreadable is treated as no history.
    """
    raw = state.get("heal_attempts")
    if not isinstance(raw, dict):
        raw = {}
    cutoff = now - window_seconds
    pruned: dict[str, list[float]] = {}
    for key, timestamps in raw.items():
        if not isinstance(key, str) or not isinstance(timestamps, list):
            continue
        kept = [
            float(ts) for ts in timestamps if isinstance(ts, (int, float)) and float(ts) > cutoff
        ]
        if kept:
            pruned[key] = kept
    state["heal_attempts"] = pruned
    return pruned


def heal_now(
    service: str | None = None,
    *,
    max_consecutive_restarts: int = DEFAULT_MAX_CONSECUTIVE_RESTARTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    attempt_window_seconds: float = DEFAULT_HEAL_ATTEMPT_WINDOW_SECONDS,
    max_attempts_per_window: int | None = None,
) -> dict[str, Any]:
    """Run one heal pass.

    With `service=None` (the watchdog's normal mode): checks every monitored
    component and restarts only the ones that are unhealthy/stopped,
    honoring per-component backoff and `max_consecutive_restarts` so a
    genuinely broken component stops being restarted after enough failed
    attempts rather than looping forever.

    Three guards keep "heal" from becoming churn, and they are independent
    on purpose (#3832):

    - **Nothing that cannot be repaired is acted on at all** -- a component
      reported `healable=False` (today: any `Pending` Kubernetes Pod) is
      skipped by the automatic pass *and* by an explicit "Heal now", because
      the action is not a repair for an operator either. Its reason is already
      on the component row (`note`), and a manual request gets it back as a
      recorded, non-ok event rather than silence.
    - **Consecutive failures** -- `max_consecutive_restarts`, reset when the
      component comes back healthy. Answers "are my restarts failing?".
    - **Rolling attempt cap** -- at most `max_attempts_per_window` automatic
      heal actions per `attempt_window_seconds` against one *stable* identity
      (`ComponentStatus.budget_key`: the owning ReplicaSet for a Pod, the
      service name for everything else), defaulting to twice the consecutive
      cap. This is the guard that does not care why: the consecutive counter
      resets on any glimpse of health and is keyed by a name that a Pod
      deletion *changes*, which is exactly how #3832's loop ran unbounded past
      a cap of 5.

    With `service` set (the dashboard's manual "heal now" button): restarts
    that one component immediately, bypassing the health check and backoff
    -- an explicit operator action shouldn't be blocked by the same guards
    that protect the automatic loop.

    A component the Compose probe could not determine (`known=False`, see
    `compose_probe`) is never a target of the automatic pass: acting on a
    guess would `docker compose up -d` containers that are probably already
    running, and every attempt would fail anyway for the same reason the
    probe did -- burning the restart budget until self-heal "gave up" on
    eleven healthy services (#3812). An explicit operator "Heal now" on one
    still goes through, like the `desired=False` and backoff overrides: the
    restart's own error is then reported back honestly.

    Returns {"checked": [...], "healed": [...], "undetermined": [...]} --
    the last listing the components whose state this pass could not
    determine, each with its reason in `note` -- and additionally an
    "error" key if an explicit `service` isn't currently a known container.
    """
    statuses = list_component_status()
    now = time.time()
    unhealthy_count = _record_health_check(statuses)
    attempt_cap = (
        max_attempts_per_window
        if max_attempts_per_window is not None
        else max(1, max_consecutive_restarts * HEAL_ATTEMPT_WINDOW_MULTIPLIER)
    )

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
        attempt_history = _prune_heal_attempts(state, now, attempt_window_seconds)
        unhealable_reported: dict[str, str] = state.setdefault("unhealable_reported", {})
        events: list[dict[str, Any]] = state.setdefault("events", [])

        for status in targets:
            key = status.budget_key

            if status.healthy and not manual:
                if restart_counts.get(key, 0) != 0:
                    logger.info(
                        "self-heal: %s recovered, resetting consecutive-restart count",
                        status.service,
                        extra={"component": "self_heal", "service": status.service},
                    )
                restart_counts[key] = 0
                unhealable_reported.pop(key, None)
                prom_metrics.SELFHEAL_RESTART_COUNT.labels(service=status.service).set(0)
                continue

            if not status.healable:
                # No action this module has would converge this component, so
                # taking one is churn with a side effect (#3832). Not
                # overridable by `manual`, unlike every other guard below:
                # `note` already carries the cluster's own reason, and an
                # operator clicking "Heal now" on an unschedulable Pod would
                # only reset the Pod age they need to diagnose it.
                detail = status.note or f"state={status.state} health={status.health or 'n/a'}"
                # First sighting of this condition (or a changed reason) is
                # the event; the identical repeats every 15s after it are not.
                changed = unhealable_reported.get(key) != detail
                unhealable_reported[key] = detail
                logger.log(
                    logging.WARNING if changed or manual else logging.DEBUG,
                    "self-heal: not healing %s -- no action converges it (%s)",
                    status.service,
                    detail,
                    extra={
                        "component": "self_heal",
                        "service": status.service,
                        "state": status.state,
                        "health": status.health,
                        "note": status.note,
                        "manual": manual,
                    },
                )
                if manual:
                    # An explicit request gets an explicit answer, in the same
                    # event log the dashboard already renders.
                    refusal = HealEvent(
                        ts=now,
                        service=status.service,
                        reason=f"state={status.state} health={status.health or 'n/a'}",
                        action="refused",
                        ok=False,
                        restart_count=restart_counts.get(key, 0),
                        message=status.note or "No heal action would converge this component",
                        evidence={
                            "probe_type": status.source,
                            "state": status.state,
                            "health": status.health,
                            "container": status.container,
                            "manual": True,
                            "healable": False,
                        },
                        correlation_id=get_correlation_id(),
                    )
                    events.append(refusal.to_dict())
                    healed.append(refusal.to_dict())
                continue

            # Healable again (or never unhealable): the next time no action
            # converges it, that is a new event and warns.
            unhealable_reported.pop(key, None)

            if not manual and not status.known:
                # State undetermined this pass (the Compose probe could not
                # run -- #3812). "Not known to be healthy" is not "known to
                # be broken": healing here would act on an unread state, and
                # the action would fail for the probe's own reason anyway.
                logger.info(
                    "self-heal: skipping %s, its state could not be determined this pass (%s)",
                    status.service,
                    status.note or "reason unavailable",
                    extra={
                        "component": "self_heal",
                        "service": status.service,
                        "compose_probe_reason": status.note,
                    },
                )
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

            count = restart_counts.get(key, 0)
            last_ts = last_restart_ts.get(key, 0.0)
            recent_attempts = attempt_history.get(key, [])

            if not manual:
                if len(recent_attempts) >= attempt_cap:
                    # The identity-independent cap (#3832): this many heal
                    # actions inside the window without the component settling
                    # means healing is not the answer, however the component's
                    # name or health flickered in between.
                    prom_metrics.SELFHEAL_GIVEUP_TOTAL.labels(service=status.service).inc()
                    logger.warning(
                        "self-heal: pausing heals of %s, %d attempt(s) in the last %.0fs without"
                        " converging (budget_key=%s, max=%d)",
                        status.service,
                        len(recent_attempts),
                        attempt_window_seconds,
                        key,
                        attempt_cap,
                        extra={
                            "component": "self_heal",
                            "service": status.service,
                            "budget_key": key,
                            "attempts_in_window": len(recent_attempts),
                            "attempt_window_seconds": attempt_window_seconds,
                            "max_attempts_per_window": attempt_cap,
                        },
                    )
                    continue
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
            restart_counts[key] = new_count
            last_restart_ts[key] = now
            if not manual:
                # Counted whatever the outcome and whatever happens to the
                # component's name afterwards -- an action taken is an action
                # taken (#3832). Manual requests are the operator's budget,
                # not the watchdog's, and are not rate-limited here.
                attempt_history.setdefault(key, []).append(now)

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
                "budget_key": key,
                "attempts_in_window_before": len(recent_attempts),
                "attempt_window_seconds": attempt_window_seconds,
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

    undetermined = sum(1 for s in targets if not s.known)
    logger.info(
        "self-heal: heal pass complete (checked=%d, unhealthy=%d, undetermined=%d, healed=%d, "
        "manual=%s)",
        len(checked),
        unhealthy_count,
        undetermined,
        len(healed),
        manual,
        extra={
            "component": "self_heal",
            "checked": len(checked),
            "unhealthy": unhealthy_count,
            "undetermined": undetermined,
            "healed": len(healed),
            "manual": manual,
        },
    )

    return {
        "checked": checked,
        "healed": healed,
        # Components this pass could not determine at all, each carrying its
        # reason in `note` -- reported separately so a caller never reads
        # "not healed" as "healthy" or as "still broken" (#3812).
        "undetermined": [s.to_dict() for s in targets if not s.known],
    }


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


def _is_core_component(c: ComponentStatus) -> bool:
    """Whether `c` is one of the core app tiers (api/web/ollama/cassandra).

    Two naming conventions, one question: a Compose/native/Terraform row is
    named after the component itself, while a Kubernetes row is named after a
    Pod and carries its tier in `tier` instead (see `ComponentStatus`).
    """
    if c.source == "kubernetes":
        return c.tier == "core"
    return c.service in CORE_APP_SERVICES


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

    A Kubernetes row is named after its Pod, never after a component, so it
    is recognized by `tier == "core"` instead of by name (#3828): matching on
    `CORE_APP_SERVICES` alone, a cluster running four core tiers reported
    "Nothing detected running" directly above the list of its own Pods.
    """
    core_sources = {c.source for c in components if _is_core_component(c)}
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

    `observability_source` names where the observability tier's rows came
    from: `"kubernetes"` when it was read in-cluster (see
    `kubernetes_mode_active`), `"compose"` otherwise. The page keys the
    Compose "cannot determine from here" banner off it, so a Kubernetes
    deployment is never told its in-cluster tier is unreadable because a
    `docker compose ps` it does not use could not run (#3828).

    Components come in three states, not two (#3812): present, absent, and
    unknown (`known=False`, `state="unknown"`). `unhealthy_count` counts only
    the first two -- a component nobody could query is not evidence of
    anything -- and `compose_probe_reason` carries the *why* to the page, so
    the operator reads "docker daemon unreachable -- `docker compose ps`
    exited 125" on screen instead of having to find it in a log.

    A component self-heal will not act on at all (`healable=False`, e.g. an
    unschedulable Pod -- #3832) is not "giving up": nothing was ever tried.
    The page renders its `note` -- the scheduler's own message -- and does not
    claim an exhausted restart budget that was never spent.
    """
    survey = component_survey()
    components = survey.components
    unhealthy_count = _record_health_check(components)
    state = _load_state()
    restart_counts: dict[str, int] = state.get("restart_counts", {})
    # Read-only view of the same window `heal_now` enforces, so the page's
    # "gave up" badge and the watchdog's behaviour cannot disagree -- expired
    # attempts stop counting here at the same moment they stop counting there.
    attempts = _prune_heal_attempts(dict(state), time.time(), DEFAULT_HEAL_ATTEMPT_WINDOW_SECONDS)
    max_consecutive_restarts = get_watchdog().max_consecutive_restarts
    component_dicts = []
    for c in components:
        d = c.to_dict()
        count = restart_counts.get(c.budget_key, 0)
        exhausted = count >= max_consecutive_restarts or len(attempts.get(c.budget_key, [])) >= max(
            1, max_consecutive_restarts * HEAL_ATTEMPT_WINDOW_MULTIPLIER
        )
        d["restart_count"] = count
        d["giving_up"] = not c.healthy and c.desired and c.known and c.healable and exhausted
        component_dicts.append(d)
    return {
        "enabled": is_enabled(),
        "mode": detected_mode(components),
        # Where the observability tier is read from this pass (#3828).
        # "kubernetes" means it was queried in-cluster and the Compose probe
        # says nothing about it -- the page must not then explain its
        # absence with the Compose "cannot determine from here" banner, which
        # is about a survey that has no bearing on this deployment.
        "observability_source": ("kubernetes" if kubernetes_mode_active(components) else "compose"),
        "compose_probe_available": survey.compose_probe.available,
        "compose_probe_reason": survey.compose_probe.reason,
        "components": component_dicts,
        "unhealthy_count": unhealthy_count,
        "unknown_count": sum(1 for c in components if not c.known),
        "events": recent_events(20),
    }


__all__ = [
    "COMPOSE_FILE",
    "ComponentStatus",
    "ComponentSurvey",
    "ComposeProbe",
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
    "kubernetes_mode_active",
    "compose_probe",
    "compose_probe_available",
    "component_survey",
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
