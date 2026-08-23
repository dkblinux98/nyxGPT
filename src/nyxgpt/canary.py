"""Canary deployment for nyxGPT components on a local Kubernetes cluster.

Runs a second `<component>-canary` Deployment alongside the existing
`<component>-stable` Deployment, both fronted by a single Service (see
k8s/service-canary.yaml / k8s/service-web-canary.yaml). Traffic is split by
replica-count ratio: kube-proxy round-robins Service traffic evenly across
every matching Pod endpoint, so `canary_replicas / pool_replicas`
approximates the canary's share of requests. There is no cloud traffic
manager or extra in-cluster proxy involved -- this targets the same
local-cluster workflow documented in docs/kubernetes.md (kind/minikube/k3s).

The pool is NOT standing (#3833). Between rollouts the stable Deployment
rests at whatever replica count it was installed/scaled to (1 by default),
and a rollout *grows* the pool to the smallest size that can express the
requested weight -- up to a ceiling (`[canary] total_replicas`, 4 by
default) -- then returns stable to its resting count on promote or
rollback. Before #3833 the manifests shipped a standing `replicas: 4` pool
that a rollout merely subdivided, so every install paid for a 4-wide api
Deployment purely to make a 25% traffic step expressible.

This is the sole deployment model as of #3409 -- blue/green (deploy.py) was
retired in favor of canary, which is a strict superset for traffic purposes
(0%/100% reproduces blue/green's cutover) plus metrics-gated gradual shift
and auto-rollback. `api` and `web` are covered as of #3419 (every public
function below takes a `component: str = "api"` parameter -- see
`COMPONENTS`); `ollama` is deliberately not implemented (see
`OLLAMA_UNSUPPORTED_REASON` and docs/kubernetes.md#ollama-canary-feasibility
for why), and the Cassandra data-migration story remains deferred per the
#3409 owner decision.

Metrics-based promotion/rollback reads the *canary track's own* Pods
(`track_metrics`, #3829): the Pod list is filtered by the `track=` label the
stable/canary Deployments already carry, and each matching Pod's
unauthenticated Prometheus `/metrics` endpoint is read through the API
server's Pod proxy. It deliberately does NOT read this process's own
ResourceMonitor -- doing so gated the rollout on the vitals of whichever
`nyxgpt-api` Pod happened to serve the dashboard/CLI request, so a canary
with zero scheduled Pods was reported "safe to promote" on a stable Pod's
459 requests (#3829). No Prometheus server is required: the canary gate
reads the Pods directly, so it works whether or not the optional
observability stack is installed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nyxgpt import metrics as prom_metrics
from nyxgpt import ops as ops_module
from nyxgpt.ops import OpsResult as CanaryResult
from nyxgpt.subprocess_bounds import (
    PROBE_TIMEOUT_SECONDS,
    bounded_argv,
    timed_out,
    timeout_message,
    timeout_result,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "nyxgpt-api-canary"
STABLE_DEPLOYMENT = "nyxgpt-api-stable"
CANARY_DEPLOYMENT = "nyxgpt-api-canary"
IMAGE_REPOSITORY = "nyxgpt-api"
DEFAULT_NAMESPACE = "nyxgpt"
DEFAULT_ROLLOUT_TIMEOUT_SECONDS = 180
HISTORY_LIMIT = 20

# Every kubectl call here is a single API request, not a stream, so none of
# them has a legitimate reason to take half a minute -- this is the backstop
# for the mutating ones (`scale`, `set image`). Read-only probes on a polled
# endpoint use the much tighter `PROBE_TIMEOUT_SECONDS` instead, the per-Pod
# metrics reads (#3829) pass `METRICS_SCRAPE_TIMEOUT_SECONDS`, and
# `_wait_rollout` -- the one call that blocks by contract -- passes its own
# bound. See `subprocess_bounds` for why any of this exists (#3858).
DEFAULT_RUN_TIMEOUT_SECONDS = 30.0

# Track-scoped metrics (#3829). Both Deployments' Pods carry the same
# `app` label and are told apart by `track: stable|canary` (see
# k8s/deployment-stable.yaml / deployment-canary.yaml), which is what makes
# per-track attribution possible without a traffic manager.
API_POD_APP_LABEL = "nyxgpt-api-canary-pool"
WEB_POD_APP_LABEL = "nyxgpt-web-canary-pool"

# How long a single Pod `/metrics` read may take before it is abandoned. The
# dashboard's status poll fans out over the track's Pods, so a wedged Pod
# must degrade the panel to "not attributable", never hang it.
METRICS_SCRAPE_TIMEOUT_SECONDS = 5

# Upper bound on Pods scraped per track per call, so a large stable track
# cannot turn one status poll into an unbounded fan-out of kubectl
# invocations. Pods beyond the bound are reported in `pods_ready` but not
# read; `pods_scraped` is what the numbers are actually based on.
MAX_SCRAPED_PODS = 8

# Paths excluded from the traffic the canary gate judges. The kubelet's
# readiness/liveness probes hit `/health` every 10s and Prometheus (plus this
# module's own scrape) hits `/metrics`; counting them would let a canary that
# has served no real request cross `min_requests` on automated traffic alone
# within a few minutes of being scheduled -- the same "the gate cannot fail"
# defect this attribution work exists to close (#3829).
NON_TRAFFIC_PATHS = frozenset({"/health", "/metrics"})

# Metric families read out of a Pod's exposition (see metrics.py).
HTTP_REQUESTS_METRIC = "nyxgpt_http_requests_total"
HTTP_DURATION_METRIC = "nyxgpt_http_request_duration_seconds"

# What a stable Deployment rests at between rollouts when its live replica
# count cannot be read (#3833) -- the same number k8s/deployment-stable.yaml
# and k8s/deployment-web-stable.yaml ship. There is deliberately no
# "default pool size" constant any more: a rollout reads the stable
# Deployment's *live* replica count and returns it to exactly that on
# promote/rollback, so an operator who scales stable to some other number
# cannot have it silently re-inflated by the next `canary start`.
DEFAULT_RESTING_REPLICAS = 1

# How much Pod-level failure detail travels back to the dashboard (#3831). A
# stuck rollout can have every replica failing for the same reason, and
# Kubernetes' scheduler messages run long -- enough to name the cause, not so
# much that the error card becomes a log dump.
POD_REASON_LIMIT = 3
POD_REASON_MESSAGE_LIMIT = 300

NOT_SUPPORTED_UNDER_COMPOSE = (
    "Canary deployment requires the Kubernetes deployment mode; not "
    "available under docker-compose. See docs/kubernetes.md."
)

# `web` net-new k8s workloads and component parameter (#3419, follow-up to
# #3409). Constants and behavior for the `api` component (above) are
# untouched -- every public function below defaults `component="api"` and,
# on that default, resolves to exactly the same deployment/service names,
# state-file shape, and metric labels as before this component parameter
# existed, so on-disk state and existing callers keep working unmodified.
WEB_SERVICE_NAME = "nyxgpt-web-canary"
WEB_STABLE_DEPLOYMENT = "nyxgpt-web-stable"
WEB_CANARY_DEPLOYMENT = "nyxgpt-web-canary"
WEB_IMAGE_REPOSITORY = "nyxgpt-web"
WEB_CONTAINER_NAME = "nyxgpt-web"

# `ollama` is deliberately NOT implemented: every replica of a canary'd
# Ollama Deployment would need its own local model store (Ollama has no
# concept of a read replica -- `ollama serve` owns and writes to its own
# blob directory), so a stable/canary pair either shares one
# ReadWriteMany-mounted volume -- risking concurrent writers racing to pull/
# evict the same model blobs, and no upstream guidance that this is safe --
# or doubles local disk usage for models that can run several GB each. Both
# are real costs for a local-first, single-user deployment target with no
# solved-elsewhere precedent to lean on, so this documents the infeasibility
# (per this issue's AC) rather than shipping an unsound split. See
# docs/kubernetes.md#ollama-canary-feasibility for the full writeup; revisit
# if Ollama gains a supported multi-instance/shared-storage model.
OLLAMA_UNSUPPORTED_REASON = (
    "ollama canary is not implemented: every Deployment replica would need "
    "its own local model store since Ollama has no read-replica model, so a "
    "stable/canary split means either a shared volume with concurrent "
    "writers racing to pull/evict the same model blobs, or duplicating "
    "multi-GB models per track -- both real costs for a local-first, "
    "single-user target. See docs/kubernetes.md#ollama-canary-feasibility."
)


@dataclass(frozen=True)
class ComponentSpec:
    """Everything canary's generic (`_scale`/`_set_image`/`deployment_health`/build) plumbing
    needs to operate on one component's stable/canary Deployment pair.

    `build_context`/`build_fingerprint_paths`/`build_excludes`/`build_args`
    are only consulted for non-`api` components -- `deploy()` keeps calling
    `ops_module.build_and_load_k8s_image(tag)` with no extra kwargs for
    `api`, identical to the pre-#3419 call, so that path (and the tests
    mocking it with a single-argument stub) is unaffected.
    """

    key: str
    stable_deployment: str
    canary_deployment: str
    service_name: str
    image_repository: str
    container_name: str
    supported: bool = True
    unsupported_reason: str = ""
    # Label shared by this component's stable and canary Pods; combined with
    # `track=stable|canary` it selects exactly one track's Pods (#3829).
    pod_app_label: str = ""
    # Container port serving the Prometheus exposition, or None when this
    # component's Pods export no metrics at all -- `web` runs Next.js, which
    # has no /metrics endpoint, so its canary traffic is not measurable and
    # `track_metrics` says so rather than inventing a number.
    metrics_port: int | None = None
    metrics_path: str = "/metrics"
    build_context: Path | None = None
    build_fingerprint_paths: list[Path] | None = None
    build_excludes: frozenset[str] = frozenset()
    build_args: dict[str, str] | None = None


COMPONENTS: dict[str, ComponentSpec] = {
    "api": ComponentSpec(
        key="api",
        stable_deployment=STABLE_DEPLOYMENT,
        canary_deployment=CANARY_DEPLOYMENT,
        service_name=SERVICE_NAME,
        image_repository=IMAGE_REPOSITORY,
        container_name="nyxgpt-api",
        pod_app_label=API_POD_APP_LABEL,
        metrics_port=8000,
    ),
    "web": ComponentSpec(
        key="web",
        stable_deployment=WEB_STABLE_DEPLOYMENT,
        canary_deployment=WEB_CANARY_DEPLOYMENT,
        service_name=WEB_SERVICE_NAME,
        image_repository=WEB_IMAGE_REPOSITORY,
        container_name=WEB_CONTAINER_NAME,
        pod_app_label=WEB_POD_APP_LABEL,
        metrics_port=None,
        build_context=ops_module.REPO_ROOT / "web",
        build_fingerprint_paths=[ops_module.REPO_ROOT / "web"],
        build_excludes=ops_module._WEB_VENDOR_EXCLUDES,
        build_args={"NEXT_PUBLIC_API_BASE_URL": ops_module.TF_WEB_API_BASE_URL_DEFAULT},
    ),
    "ollama": ComponentSpec(
        key="ollama",
        stable_deployment="",
        canary_deployment="",
        service_name="",
        image_repository="",
        container_name="",
        supported=False,
        unsupported_reason=OLLAMA_UNSUPPORTED_REASON,
    ),
}


def _component_spec(component: str) -> tuple[ComponentSpec | None, CanaryResult | None]:
    """Resolve `component` to its `ComponentSpec`, or a `CanaryResult` explaining why not.

    Unknown components are rejected outright; `ollama` resolves to a known
    but `supported=False` spec, surfaced via `unsupported_reason` (see
    `OLLAMA_UNSUPPORTED_REASON`) rather than a generic "unknown component"
    message.
    """
    spec = COMPONENTS.get(component)
    if spec is None:
        return None, CanaryResult(
            False,
            f"Unknown canary component {component!r}; expected one of {sorted(COMPONENTS)}",
        )
    if not spec.supported:
        return None, CanaryResult(False, spec.unsupported_reason)
    return spec, None


def _which(prog: str) -> str | None:
    """Return the absolute path to `prog` on PATH, or None if it isn't found."""
    return shutil.which(prog)


def _compose_mode() -> bool:
    """True when this process is the docker-compose `api` container.

    Detected via NYXGPT_COMPOSE_FILE (see docker-compose.yml and
    self_heal.py), which has no k8s analog -- a Pod never has it set. There's
    no cluster for kubectl to reach under docker-compose, so callers use this
    to swap the generic "kubectl not found" message for one that names the
    actual constraint (see docs/kubernetes.md for the supported mode).
    """
    return bool(os.environ.get("NYXGPT_COMPOSE_FILE", "").strip())


def _kubectl_missing_message(fallback: str) -> str:
    """Return `fallback`, or the docker-compose-unsupported message if running under Compose."""
    return NOT_SUPPORTED_UNDER_COMPOSE if _compose_mode() else fallback


def _run(
    cmd: list[str],
    *,
    expected: bool = False,
    timeout: float | None = DEFAULT_RUN_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text without raising on non-zero exit.

    Non-zero exits are logged with the command and a stderr tail so failed
    kubectl/rollout invocations show up in Loki instead of only reaching the
    caller via the returned `CompletedProcess` (#3415 gap 5). Pass `expected=True`
    for read-only probes where a non-zero exit is a normal outcome, to log at
    DEBUG instead of WARNING.

    Bounded twice (#3858): `timeout` caps how long the caller's thread can be
    held -- these calls run in Starlette's threadpool behind `/canary/status`,
    where an unreachable cluster that blackholes rather than refuses would
    otherwise hold a worker forever -- and `bounded_argv` hands kubectl its own
    `--request-timeout` so it gives up with its own message first, except from
    inside a Pod, where that flag disables kubectl's service-account fallback
    (see `bounded_argv`) and only `timeout` applies. An expired
    bound comes back as a `TIMEOUT_RETURNCODE` result (see `timed_out`) rather
    than a `TimeoutExpired` traveling up into a handler, so one wedged Pod
    degrades a panel (the per-Pod metrics reads, #3829) instead of hanging the
    request. Pass `timeout=None` only for a call that blocks by contract and
    carries its own bound.
    """
    try:
        result = subprocess.run(
            bounded_argv(cmd, timeout),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Only a set bound can expire, but read the value back off the
        # exception rather than asserting it: `assert` is stripped under
        # `python -O`, and the narrowing has to hold in that build too.
        expired = timeout if timeout is not None else exc.timeout
        logger.log(
            logging.DEBUG if expected else logging.WARNING,
            f"Subprocess {timeout_message(expired)}: {' '.join(cmd)}",
            extra={"component": "canary", "cmd": cmd, "timeout_seconds": expired},
        )
        return timeout_result(cmd, exc, expired)
    if result.returncode != 0:
        level = logging.DEBUG if expected else logging.WARNING
        logger.log(
            level,
            f"Subprocess exited non-zero (rc={result.returncode}): {' '.join(cmd)}",
            extra={
                "component": "canary",
                "cmd": cmd,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-2000:] if result.stderr else "",
            },
        )
    return result


def _state_path() -> Path:
    """Return the path to the local canary rollout state file."""
    return Path.home() / ".nyxGPT" / "canary_state.json"


def _load_state(component: str = "api") -> dict[str, Any]:
    """Load canary rollout state from disk, defaulting to inactive/0% with no history.

    Tolerates a missing or corrupt state file by falling back to defaults.
    `api`'s state lives at the file's top level exactly as it always has
    (untouched by #3419, so existing on-disk state and callers keep
    working); other components' state lives nested under a `"components"`
    key so the two never collide.
    """
    default: dict[str, Any] = {"active": False, "weight_percent": 0, "history": []}
    path = _state_path()
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if component == "api":
                data.setdefault("active", False)
                data.setdefault("weight_percent", 0)
                data.setdefault("history", [])
                return data
            sub = data.get("components", {}).get(component)
            if not isinstance(sub, dict):
                return dict(default)
            sub.setdefault("active", False)
            sub.setdefault("weight_percent", 0)
            sub.setdefault("history", [])
            return sub
    except Exception as e:
        logger.warning(
            "Failed to load canary state from %s, using defaults: %s",
            path,
            e,
            extra={"component": "canary"},
        )
    return dict(default)


def _save_state(state: dict[str, Any], component: str = "api") -> None:
    """Persist canary rollout state to disk as JSON, creating parent dirs as needed.

    `api` writes `state` as the whole file's top level, byte-for-byte like
    before #3419 (any other component's nested `"components"` entry rides
    along unmodified since `_load_state("api")` returns the raw top-level
    dict, "components" key included). Other components merge `state` into
    that same file's `"components"` sub-object instead of overwriting it.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if component == "api":
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    components = existing.setdefault("components", {})
    components[component] = state
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class TrackHealth:
    """Health of a single stable/canary Deployment, honestly distinguishing *why* it isn't healthy.

    `state` is one of:
      - "not_deployed": the cluster is unreachable, the Deployment doesn't exist yet (e.g.
        `nyxgpt ops install --kubernetes` hasn't been run), or it exists with 0 desired
        replicas (the canary Deployment's normal idle state before a rollout starts).
        Neutral -- not an alarm.
      - "unhealthy": the Deployment exists with >0 desired replicas but not all its Pods
        are ready.
      - "healthy": the Deployment exists and all its Pods are ready.
      - "error": kubectl reached the cluster but failed in an unexpected way (e.g. RBAC
        denial, malformed response) -- distinguishable from "not_deployed" so a genuine
        problem is never silently rendered as "just not installed yet".

    This replaces the old ok/not-ok boolean, which rendered "Could not read deployment"
    (the normal outcome in non-Kubernetes modes) as a false "Unhealthy" alarm (#3409).
    """

    state: str
    message: str
    version: str = ""


def _with_reason(summary: str, reasons: list[str]) -> str:
    """Append the underlying reason(s) to `summary`, or return it unchanged.

    Kept in one place so "not healthy (0/1 ready)" and "rollout did not
    become healthy" always read the same way once the cause is known.
    """
    detail = "; ".join(r.strip() for r in reasons if r and r.strip())
    return f"{summary} -- {detail}" if detail else summary


def _pod_reason(pod: dict[str, Any]) -> str:
    """Return why this Pod is not serving, in Kubernetes' own words, or "".

    Reads the Pod's status in the order an operator would: the scheduler's
    verdict first (`PodScheduled=False` carries `Unschedulable` plus the
    "0/1 nodes are available: 1 Insufficient memory" sentence), then whatever
    a container is stuck waiting on or terminated with (`ImagePullBackOff`,
    `CrashLoopBackOff`, ...), then the readiness condition, and finally the
    bare phase. This is the sentence #3831 needed and could not see.
    """
    status = pod.get("status", {}) or {}
    conditions = [c for c in (status.get("conditions") or []) if isinstance(c, dict)]

    def _condition(kind: str) -> dict[str, Any] | None:
        for cond in conditions:
            if cond.get("type") == kind and str(cond.get("status")) == "False":
                return cond
        return None

    scheduled = _condition("PodScheduled")
    if scheduled:
        return _join_reason(scheduled.get("reason"), scheduled.get("message"))

    for key in ("initContainerStatuses", "containerStatuses"):
        for container in status.get(key) or []:
            if not isinstance(container, dict) or container.get("ready"):
                continue
            state = container.get("state", {}) or {}
            waiting = state.get("waiting") or {}
            if waiting.get("reason"):
                return _join_reason(waiting.get("reason"), waiting.get("message"))
            terminated = state.get("terminated") or {}
            if terminated.get("reason"):
                exit_code = terminated.get("exitCode")
                suffix = f" (exit {exit_code})" if exit_code is not None else ""
                return _join_reason(terminated.get("reason"), terminated.get("message")) + suffix

    ready = _condition("Ready") or _condition("ContainersReady")
    if ready:
        return _join_reason(ready.get("reason"), ready.get("message"))
    phase = str(status.get("phase") or "").strip()
    return phase if phase and phase != "Running" else ""


def _join_reason(reason: Any, message: Any) -> str:
    """Render a Kubernetes `reason`/`message` pair as one trimmed sentence."""
    reason_text = str(reason or "").strip()
    message_text = " ".join(str(message or "").split())
    if len(message_text) > POD_REASON_MESSAGE_LIMIT:
        message_text = message_text[: POD_REASON_MESSAGE_LIMIT - 1].rstrip() + "…"
    if reason_text and message_text:
        return f"{reason_text}: {message_text}"
    return reason_text or message_text


def pod_failure_reasons(selector: str, namespace: str = DEFAULT_NAMESPACE) -> list[str]:
    """Ask the cluster why the Pods matching `selector` are not serving.

    A Deployment's own status says only "0/1 ready"; the reason lives on its
    Pods. Best-effort by design -- an unreachable cluster, an RBAC denial or
    an unparseable response yields an empty list, so every caller degrades to
    the generic message it would have produced anyway (#3831).
    """
    if not selector or _which("kubectl") is None:
        return []
    cp = _run(
        ["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"],
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout)
    except Exception:
        return []
    reasons: list[str] = []
    for pod in data.get("items", []):
        if not isinstance(pod, dict):
            continue
        reason = _pod_reason(pod)
        if not reason:
            continue
        name = str(pod.get("metadata", {}).get("name", "")).strip()
        reasons.append(f"{name}: {reason}" if name else reason)
        if len(reasons) >= POD_REASON_LIMIT:
            break
    return reasons


def _selector_from_deployment(data: dict[str, Any]) -> str:
    """Build a `kubectl -l` selector from a Deployment's `spec.selector.matchLabels`."""
    labels = data.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not isinstance(labels, dict):
        return ""
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if isinstance(v, (str, int)))


def _deployment_selector(name: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    """Read `name`'s Pod selector from the cluster, or "" if it can't be read."""
    cp = _run(
        ["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"],
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return ""
    try:
        return _selector_from_deployment(json.loads(cp.stdout))
    except Exception:
        return ""


def deployment_health(name: str, namespace: str = DEFAULT_NAMESPACE) -> TrackHealth:
    """Check whether the given Deployment is fully ready, and what version it's running.

    Mirrors the readinessProbe (`GET /health`) already configured on the
    stable/canary Deployments: a Deployment only reports its Pods as Ready
    once the probe passes. When it isn't ready, the Pods' own reason is
    appended (see `pod_failure_reasons`) so the operator reads
    "Insufficient memory" rather than an unexplained "0/1 ready" (#3831).
    """
    if _which("kubectl") is None:
        return TrackHealth(
            "not_deployed",
            _kubectl_missing_message("kubectl not found; cannot check deployment health"),
        )

    cp = _run(
        ["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"],
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if timed_out(cp):
        # A status read that took too long is a degraded reading, not an
        # error the caller should raise on: the endpoint says so and stays
        # up (#3858). Named separately from the generic error branch below
        # because "timed out" and "kubectl said Forbidden" are different
        # problems with different remedies.
        return TrackHealth(
            "error",
            f"{name} health check {timeout_message(PROBE_TIMEOUT_SECONDS)} "
            "-- the cluster did not answer",
        )
    if cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        lowered = stderr.lower()
        if "notfound" in lowered.replace(" ", "") or "not found" in lowered:
            return TrackHealth(
                "not_deployed",
                f"{name} not deployed yet -- run `nyxgpt ops install --kubernetes` to create it",
            )
        if any(
            marker in lowered
            for marker in (
                "unable to connect",
                "connection refused",
                "no such host",
                "dial tcp",
                "the connection to the server",
            )
        ):
            return TrackHealth("not_deployed", "No reachable Kubernetes cluster")
        # The stderr goes in the message, not the version field: kubectl's own
        # sentence ("Forbidden: ...") is the only clue to an unexpected failure,
        # and rendering it as the track's "version" hid it in plain sight (#3831).
        return TrackHealth("error", _with_reason(f"Could not read deployment {name}", [stderr]))

    try:
        data = json.loads(cp.stdout)
    except Exception as e:
        return TrackHealth("error", _with_reason(f"Could not parse status for {name}", [str(e)]))

    containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    image = str(containers[0].get("image", "")) if containers else ""
    version = image.split(":", 1)[1] if ":" in image else image

    spec_replicas = data.get("spec", {}).get("replicas", 0) or 0
    ready = data.get("status", {}).get("readyReplicas", 0) or 0

    if spec_replicas == 0:
        return TrackHealth("not_deployed", f"{name} has 0 desired replicas (idle)", version)
    if ready >= spec_replicas:
        return TrackHealth("healthy", f"{name} healthy ({ready}/{spec_replicas} ready)", version)
    reasons = pod_failure_reasons(_selector_from_deployment(data), namespace)
    return TrackHealth(
        "unhealthy",
        _with_reason(f"{name} not healthy ({ready}/{spec_replicas} ready)", reasons),
        version,
    )


def _current_mode_with_reason() -> tuple[str, str]:
    """Best-effort classification of which deployment mode this process is running under.

    Not inferred from a failed kubectl call against the canary/stable Deployments
    (`deployment_health` above is the honest per-track answer to that question) --
    this checks the same signals #3193/#3344's mode-aware self-heal dispatch and
    `ops.detect_deployment_mode()`/`ops.terraform_stack_state()` already use: the
    NYXGPT_COMPOSE_FILE marker, then a running Terraform-managed container stack,
    then a populated Kubernetes namespace, falling back to "native" (Homebrew
    services, no Terraform/Kubernetes stack detected). Returns one of "compose",
    "terraform", "kubernetes", "native", "unknown".

    "unknown" is the answer when the Kubernetes probe *timed out* (#3858).
    Falling back to "native" there would be an assertion about the substrate
    that nothing checked -- a cluster that is configured but not answering is
    precisely the case where "you are not running Kubernetes" is most likely
    to be wrong, and the operator needs to see the probe failure rather than a
    confident wrong mode.
    """
    if _compose_mode():
        return "compose", ""
    tf_unreadable = False
    try:
        tf_state = ops_module.terraform_stack_state()
        # `_container_deployed`, not `!= "absent"` (#4022 review). Since this
        # change a denied Docker session reads `unknown` rather than collapsing
        # to `absent`, and `!= "absent"` promoted every one of those into a
        # confident "terraform" -- on a *native* install, reached from the API
        # process via `status()`, telling the operator canary "doesn't apply in
        # terraform mode". That is the exact inversion D-027 forbids for this
        # function, and it contradicts `terraform_stack_state`'s own docstring.
        if any(ops_module._container_deployed(state) for state in tf_state.values()):
            return "terraform", ""
        # Nothing readable is not the same as nothing there, but it must not
        # short-circuit the kubectl probe either: a machine whose Docker is
        # unreadable can still be running Kubernetes, and positive evidence
        # from kubectl outranks an unreadable Docker. Remembered here and
        # answered only if nothing else identifies the substrate.
        tf_unreadable = bool(tf_state) and all(
            state == ops_module.DOCKER_STATE_UNKNOWN for state in tf_state.values()
        )
    except Exception:
        pass
    if _which("kubectl") is not None:
        cp = _run(
            ["kubectl", "-n", DEFAULT_NAMESPACE, "get", "pods", "--no-headers"],
            expected=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if timed_out(cp):
            return "unknown", "kubectl-timeout"
        if cp.returncode == 0 and (cp.stdout or "").strip():
            return "kubernetes", ""
    # Only now: nothing positively identified the substrate, and the Terraform
    # read never happened. "native" would be a confident answer built on a
    # probe that failed, which is what D-027 forbids for this function.
    if tf_unreadable:
        return "unknown", "docker-unreadable"
    return "native", ""


def current_mode() -> str:
    """The deployment mode alone; see `_current_mode_with_reason` for the cause."""
    return _current_mode_with_reason()[0]


def _mode_message(mode: str, cause: str = "") -> str | None:
    """Explain why canary doesn't apply outside Kubernetes mode, and which mode provides it.

    `None` means "canary does apply here", which is only ever Kubernetes mode.
    Callers that have already established the mode is not Kubernetes want
    `_non_kubernetes_mode_message` instead, so they don't carry an `or "..."`
    fallback that can never run.
    """
    if mode == "kubernetes":
        return None
    return _non_kubernetes_mode_message(mode, cause)


def _non_kubernetes_mode_message(mode: str, cause: str = "") -> str:
    """The reason canary state is unavailable in `mode`, for a caller that knows it isn't Kubernetes.

    `cause` distinguishes the two ways the mode can be unknown. They need
    opposite operator responses, and naming the wrong one sends the reader to
    kubeconfig when the actual fault is a Docker group they are not in.
    """
    if mode == "unknown" and cause == "docker-unreadable":
        return (
            "Could not determine the deployment mode: this process cannot read Docker, "
            "so whether a Terraform-managed stack is running here is unknown -- no "
            "Kubernetes probe was run. Add yourself to the docker group "
            "(`sudo usermod -aG docker $USER`, then `sudo loginctl terminate-user $USER` "
            "or reboot) and re-run."
        )
    if mode == "unknown":
        return (
            f"Could not determine the deployment mode: the Kubernetes probe "
            f"{timeout_message(PROBE_TIMEOUT_SECONDS)} without an answer. Canary state cannot "
            f"be read until the cluster responds -- check that the current kubeconfig context "
            f"points at a reachable cluster."
        )
    return (
        f"Canary deployment is provided by Kubernetes mode; this process is currently "
        f"running in {mode} mode. Run `nyxgpt ops install --kubernetes` to enable it."
    )


def _scale(name: str, replicas: int, namespace: str = DEFAULT_NAMESPACE) -> CanaryResult:
    """Set Deployment `name`'s replica count via `kubectl scale`.

    Returns ok=True with a confirmation message on success, or ok=False with
    the kubectl error (or a "not found"/"kubectl missing" message) on failure.
    """
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot scale deployment")
        )
    cp = _run(["kubectl", "scale", "deployment", name, "-n", namespace, f"--replicas={replicas}"])
    if cp.returncode != 0:
        return CanaryResult(False, f"kubectl scale failed for {name}", (cp.stderr or "").strip())
    return CanaryResult(True, f"Scaled {name} to {replicas} replicas")


def _set_image(
    name: str, image: str, namespace: str = DEFAULT_NAMESPACE, *, container: str = "nyxgpt-api"
) -> CanaryResult:
    """Patch Deployment `name`'s `container`'s image via `kubectl set image`."""
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot set deployment image")
        )
    cp = _run(
        ["kubectl", "set", "image", f"deployment/{name}", f"{container}={image}", "-n", namespace]
    )
    if cp.returncode != 0:
        return CanaryResult(
            False, f"kubectl set image failed for {name}", (cp.stderr or "").strip()
        )
    return CanaryResult(True, f"Set {name} image to {image}")


def _wait_rollout(
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    timeout_seconds: int = DEFAULT_ROLLOUT_TIMEOUT_SECONDS,
) -> CanaryResult:
    """Block until Deployment `name`'s rollout finishes, via `kubectl rollout status`."""
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot wait for rollout")
        )
    cp = _run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{name}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ],
        # The command's own `--timeout` is the real bound (it is a watch, and
        # `--request-timeout` would cut the watch off); the Python bound sits
        # a grace period behind it, for the case where kubectl itself wedges
        # rather than honoring its flag (#3858).
        timeout=timeout_seconds + DEFAULT_RUN_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        # "timed out waiting for the condition" is not a diagnosis. Ask the Pods
        # why -- FailedScheduling/Insufficient memory, ImagePullBackOff,
        # CrashLoopBackOff -- and carry that back with the failure (#3831).
        reasons = pod_failure_reasons(_deployment_selector(name, namespace), namespace)
        return CanaryResult(
            False,
            _with_reason(
                f"Rollout of {name} did not become healthy within {timeout_seconds}s", reasons
            ),
            (cp.stderr or cp.stdout or "").strip(),
        )
    return CanaryResult(True, f"{name} rollout healthy")


def _git_short_sha() -> str:
    """Return the current commit's short SHA, or "" if git/the repo isn't available."""
    if _which("git") is None:
        return ""
    cp = _run(["git", "rev-parse", "--short", "HEAD"], expected=True)
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def _versioned_image_tag(spec: ComponentSpec) -> str:
    """Build a stamped, immutable image tag: `<image repository>:<project version>-<git short sha>`.

    Deliberately not the mutable `:local` tag `nyxgpt ops install --kubernetes` uses --
    a canary deploy needs stable and canary to be able to run two *different*,
    individually identifiable versions at once (see #3409).
    """
    version = ops_module.project_version()
    sha = _git_short_sha()
    tag = f"{version}-{sha}" if sha else version
    return f"{spec.image_repository}:{tag}"


def _split_replicas(total: int, weight_percent: int) -> tuple[int, int]:
    """Split a `total`-replica pool into (canary, stable) counts for a weight_percent.

    Both tracks get at least 1 replica for any weight strictly between 0 and
    100: a canary with 0 replicas receives no traffic at all despite being
    "active", and a *stable* with 0 replicas is a full cutover, not a canary.
    Because of that floor the returned pair sums to 2 rather than to `total`
    when `total` is 1 -- a partial split simply is not expressible in a
    one-replica pool, and silently returning `stable = 0` was the trap that
    made resting at 1 replica unsafe before #3833. Callers derive the pool
    they actually have to scale to from `canary + stable`, never from
    `total`.
    """
    if weight_percent <= 0:
        return 0, total
    if weight_percent >= 100:
        return total, 0
    pool = max(2, total)
    canary = min(max(1, round(pool * weight_percent / 100)), pool - 1)
    return canary, pool - canary


@dataclass(frozen=True)
class RolloutPlan:
    """A concrete replica split for a rollout, plus how close it got to the requested weight.

    Replica counts are integers, so most weights are not exactly
    expressible: 10% needs a 10-wide pool to be exact. Rather than silently
    serving a different weight than the operator asked for (or inflating the
    cluster to 10 Pods to honour a 10% ask literally), a plan records both
    the `requested_percent` and the `achieved_percent` it rounded to, and
    `describe()` says so in the command's own output (#3833).
    """

    canary_replicas: int
    stable_replicas: int
    requested_percent: int
    achieved_percent: int
    max_pool: int

    @property
    def pool(self) -> int:
        """Total replicas the pool has to be scaled to for this split."""
        return self.canary_replicas + self.stable_replicas

    @property
    def rounded(self) -> bool:
        """True when the achievable weight isn't the one that was asked for."""
        return self.achieved_percent != self.requested_percent

    def describe(self) -> str:
        """Render the split as "N% (canary/pool replicas)", naming any rounding."""
        text = f"{self.achieved_percent}% ({self.canary_replicas}/{self.pool} replicas)"
        if self.rounded:
            text += (
                f"; {self.requested_percent}% is not expressible in a pool of at most "
                f"{self.max_pool} replicas, so it rounded to {self.achieved_percent}% "
                f"-- raise `[canary] total_replicas` for finer steps"
            )
        return text


def _replica_count(count: int) -> str:
    """Render a replica count for a message, pluralized ("1 replica" / "3 replicas")."""
    return f"{count} replica" if count == 1 else f"{count} replicas"


def _plan_rollout(resting: int, weight_percent: int, max_pool: int) -> RolloutPlan:
    """Choose the replica split that comes closest to `weight_percent`, smallest pool wins.

    The pool is grown for the rollout, not carved out of a standing one
    (#3833): candidate sizes run from the stable Deployment's resting count
    (never below it -- a rollout must not cut serving capacity) up to
    `max_pool`, and the smallest candidate with the smallest weight error is
    chosen. So 50% costs 2 Pods, 25%/75% cost 4, and a 10% ask on a 4-Pod
    ceiling reports honestly that it is serving 25%.
    """
    requested = max(1, min(99, weight_percent))
    resting = max(1, resting)
    lowest = max(2, resting)
    highest = max(lowest, max_pool)
    best: RolloutPlan | None = None
    for pool in range(lowest, highest + 1):
        canary, stable = _split_replicas(pool, requested)
        achieved = round(100 * canary / (canary + stable))
        if best is None or abs(achieved - requested) < abs(best.achieved_percent - requested):
            best = RolloutPlan(canary, stable, requested, achieved, highest)
    assert best is not None  # the range always yields at least `lowest`
    return best


def _desired_replicas(name: str, namespace: str = DEFAULT_NAMESPACE) -> int | None:
    """Return Deployment `name`'s currently desired replica count, or None if it can't be read.

    Read live rather than assumed from a constant, so an operator who scales
    a Deployment themselves keeps that count across a rollout (#3833).
    """
    if _which("kubectl") is None:
        return None
    cp = _run(
        ["kubectl", "get", "deployment", name, "-n", namespace, "-o", "jsonpath={.spec.replicas}"],
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return None
    try:
        return int((cp.stdout or "").strip())
    except ValueError:
        return None


def _resting_replicas(name: str, namespace: str = DEFAULT_NAMESPACE) -> int:
    """Return the replica count a rollout must restore `name` to when it finishes.

    The live desired count, floored at 1: restoring a stable Deployment to 0
    would leave the Service with no stable endpoint at all once the canary
    is scaled down. Falls back to `DEFAULT_RESTING_REPLICAS` (what the
    manifests ship) when the Deployment can't be read.
    """
    live = _desired_replicas(name, namespace)
    if live is None or live < 1:
        return DEFAULT_RESTING_REPLICAS
    return live


def _first_replica_count(*values: Any) -> int:
    """Return the first of `values` that reads as a replica count >= 1, else the manifest default.

    State files are read back from disk and may predate a field or hold
    anything, so every replica count that comes from one is funnelled
    through here rather than trusted as an int.
    """
    for value in values:
        try:
            if value is not None and int(value) >= 1:
                return int(value)
        except (TypeError, ValueError):
            continue
    return DEFAULT_RESTING_REPLICAS


def _resting_from_state(state: dict[str, Any], fallback: int | None = None) -> int:
    """Return the resting replica count `start` recorded for the rollout in `state`.

    Falls back to a pre-#3833 state file's `total_replicas` -- under the old
    fixed-pool model that number *was* stable's steady-state count, so a
    rollout started before this change still restores the pool it came from
    -- then to the caller's value, then to `DEFAULT_RESTING_REPLICAS`.
    """
    return _first_replica_count(
        state.get("resting_replicas"), state.get("total_replicas"), fallback
    )


def _scale_pool(
    spec: ComponentSpec,
    plan: RolloutPlan,
    namespace: str,
    *,
    current_stable: int,
) -> tuple[str, CanaryResult] | None:
    """Scale both tracks to `plan`, growing before shrinking. None means both succeeded.

    Order matters while the pool is elastic (#3833). When stable has to grow,
    it grows *first*: scaling the canary up against a still-resting stable
    would briefly route a much larger share to the new version than the
    operator asked for. And if the canary scale then fails, stable is put
    back to `current_stable`, so a failed `start` doesn't leave an inflated
    pool standing -- exactly the cost this issue removed. When stable is
    shrinking (a later promote step), the canary grows first instead, so the
    pool never dips below the capacity it already had.

    Returns `(track, failure)` for the first failing scale, where `track` is
    "canary" or "stable".
    """
    if plan.stable_replicas > current_stable:
        stable_result = _scale(spec.stable_deployment, plan.stable_replicas, namespace)
        if not stable_result.ok:
            return "stable", stable_result
        canary_result = _scale(spec.canary_deployment, plan.canary_replicas, namespace)
        if not canary_result.ok:
            _scale(spec.stable_deployment, current_stable, namespace)
            return "canary", canary_result
        return None
    canary_result = _scale(spec.canary_deployment, plan.canary_replicas, namespace)
    if not canary_result.ok:
        return "canary", canary_result
    stable_result = _scale(spec.stable_deployment, plan.stable_replicas, namespace)
    if not stable_result.ok:
        return "stable", stable_result
    return None


@dataclass(frozen=True)
class TrackMetrics:
    """Request vitals attributed to ONE track's Pods, or an honest reason why they aren't.

    `attributable` is the field callers must branch on. It is False whenever
    the numbers cannot be tied to this track's Pods -- no cluster, no ready
    Pods (the canary's normal idle state), every scrape failed, or a
    component whose Pods export no metrics at all -- and in that case the
    numeric fields are zeros that mean "unknown", NOT "measured zero". A
    rollout gate must therefore never read `total_requests` without first
    checking `attributable`; conflating the two is exactly how #3829's
    "safe to promote" verdict was reached for a canary with no Pods.

    `total_requests`/`error_rate_percent`/`p95_latency_ms` exclude the
    `NON_TRAFFIC_PATHS` (`/health`, `/metrics`) so kubelet probes and
    Prometheus scrapes cannot masquerade as canary traffic.
    """

    track: str
    attributable: bool
    reason: str = ""
    source: str = ""
    pods_ready: int = 0
    pods_scraped: int = 0
    total_requests: int = 0
    error_rate_percent: float = 0.0
    p95_latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Render as the JSON shape the API/dashboard consume."""
        return {
            "track": self.track,
            "attributable": self.attributable,
            "reason": self.reason,
            "source": self.source,
            "pods_ready": self.pods_ready,
            "pods_scraped": self.pods_scraped,
            "total_requests": self.total_requests,
            "error_rate_percent": self.error_rate_percent,
            "p95_latency_ms": self.p95_latency_ms,
        }


_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>[^\s]+)\s*(?:[0-9]+)?$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a Prometheus sample's label set into a dict (quoted values, escapes tolerated)."""
    return {m.group(1): m.group(2) for m in _LABEL_RE.finditer(raw)}


@dataclass
class _ExpositionTotals:
    """Running aggregate of one or more Pods' HTTP exposition, in Prometheus terms."""

    requests: float = 0.0
    errors: float = 0.0
    # `le` upper bound -> cumulative observation count, summed across Pods.
    buckets: dict[float, float] = field(default_factory=dict)
    observations: float = 0.0


def _accumulate_exposition(text: str, totals: _ExpositionTotals) -> None:
    """Fold one Pod's `/metrics` body into `totals`, skipping non-traffic paths.

    Request counts and errors come from `nyxgpt_http_requests_total` (its
    `status` label is the single source for both, so the error *rate* is
    always a ratio of the same denominator), and latency from the
    `nyxgpt_http_request_duration_seconds` histogram's buckets. Samples for
    `NON_TRAFFIC_PATHS` are dropped on both.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        name = match.group("name")
        if not name.startswith((HTTP_REQUESTS_METRIC, HTTP_DURATION_METRIC)):
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isnan(value):
            continue
        labels = _parse_labels(match.group("labels") or "")
        if labels.get("path", "") in NON_TRAFFIC_PATHS:
            continue

        if name == HTTP_REQUESTS_METRIC:
            totals.requests += value
            if labels.get("status", "").startswith("5"):
                totals.errors += value
        elif name == f"{HTTP_DURATION_METRIC}_bucket":
            try:
                bound = float(labels.get("le", "nan"))
            except ValueError:
                continue
            if math.isnan(bound):
                continue
            totals.buckets[bound] = totals.buckets.get(bound, 0.0) + value
        elif name == f"{HTTP_DURATION_METRIC}_count":
            totals.observations += value


def _p95_from_buckets(buckets: dict[float, float], observations: float) -> float:
    """Estimate the 95th percentile in milliseconds from summed cumulative histogram buckets.

    The same linear interpolation Prometheus' `histogram_quantile` uses: find
    the first bucket whose cumulative count reaches the rank, then
    interpolate within it. Falls back to the largest finite bucket bound when
    the rank lands in the `+Inf` bucket (an observation above every bound),
    which is also what `histogram_quantile` reports.
    """
    if observations <= 0 or not buckets:
        return 0.0
    finite = sorted(b for b in buckets if not math.isinf(b))
    if not finite:
        return 0.0
    rank = 0.95 * observations
    previous_bound = 0.0
    previous_count = 0.0
    for bound in finite:
        count = buckets[bound]
        if count >= rank:
            span = count - previous_count
            if span <= 0:
                return bound * 1000.0
            within = (rank - previous_count) / span
            return (previous_bound + (bound - previous_bound) * within) * 1000.0
        previous_bound = bound
        previous_count = count
    return finite[-1] * 1000.0


def _ready_track_pods(spec: ComponentSpec, track: str, namespace: str) -> tuple[list[str], str]:
    """List the Ready Pod names on `track`, or return an empty list plus the reason.

    Filters on the Pods' own `track` label rather than on Deployment names, so
    the answer is about which Pods are actually serving, not about what the
    Deployment intends.
    """
    cp = _run(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={spec.pod_app_label},track={track}",
            "-o",
            "json",
        ],
        expected=True,
        timeout=METRICS_SCRAPE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return [], f"could not list {track} Pods: {(cp.stderr or '').strip() or 'kubectl failed'}"
    try:
        data = json.loads(cp.stdout)
    except Exception:
        return [], f"could not parse the {track} Pod list"

    ready: list[str] = []
    for item in data.get("items", []):
        status = item.get("status", {})
        if status.get("phase") != "Running":
            continue
        # A Pod being deleted is draining out of the Service's endpoints, and
        # after a `canary deploy` the draining Pods are the ones running the
        # PREVIOUS image -- counting their requests would credit the old
        # version's traffic to the version now being judged.
        if item.get("metadata", {}).get("deletionTimestamp"):
            continue
        conditions = status.get("conditions", []) or []
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            continue
        name = item.get("metadata", {}).get("name", "")
        if name:
            ready.append(name)
    if not ready:
        return [], f"the {track} track has no ready Pods"
    return sorted(ready), ""


def _scrape_pod_metrics(pod: str, port: int, path: str, namespace: str) -> str | None:
    """Read one Pod's Prometheus exposition through the API server's Pod proxy.

    Uses `kubectl get --raw` rather than a direct connection because Pod IPs
    are not routable from outside the cluster (the CLI's case) and this keeps
    a single code path for in-Pod and out-of-cluster callers. Needs only
    `get` on `pods/proxy` (see k8s/rbac.yaml); the endpoint is unauthenticated
    on the app side, like /health.
    """
    raw_path = f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy{path}"
    cp = _run(
        ["kubectl", "get", "--raw", raw_path],
        expected=True,
        timeout=METRICS_SCRAPE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout or ""


def track_metrics(
    track: str = "canary",
    namespace: str = DEFAULT_NAMESPACE,
    *,
    component: str = "api",
) -> TrackMetrics:
    """Return request vitals attributed to `track`'s Pods only (#3829).

    This is the input every rollout gate must use. It reads the Pods that
    carry `track=<track>` and aggregates their own counters -- never this
    process's, which belong to whichever Pod happens to be serving the
    caller's request and say nothing about the canary.

    Returns a `TrackMetrics` whose `attributable` is False (with a `reason`)
    whenever the numbers cannot be tied to the track: no kubectl/cluster, no
    ready Pods, no readable `/metrics` on any of them, or a component that
    exports none at all.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return TrackMetrics(track=track, attributable=False, reason=err.message)
    if spec.metrics_port is None:
        return TrackMetrics(
            track=track,
            attributable=False,
            reason=(
                f"{component} Pods export no /metrics endpoint, so {track}-track traffic "
                "is not measurable; judge this rollout on Pod health and your own checks"
            ),
        )
    if _which("kubectl") is None:
        return TrackMetrics(
            track=track,
            attributable=False,
            reason=_kubectl_missing_message(
                f"kubectl not found; cannot read the {track} track's metrics"
            ),
        )

    pods, reason = _ready_track_pods(spec, track, namespace)
    if not pods:
        return TrackMetrics(track=track, attributable=False, reason=reason)

    scraped = 0
    totals = _ExpositionTotals()
    for pod in pods[:MAX_SCRAPED_PODS]:
        body = _scrape_pod_metrics(pod, spec.metrics_port, spec.metrics_path, namespace)
        if body is None:
            continue
        _accumulate_exposition(body, totals)
        scraped += 1

    if scraped == 0:
        return TrackMetrics(
            track=track,
            attributable=False,
            reason=f"could not read /metrics from any of the {len(pods)} ready {track} Pod(s)",
            pods_ready=len(pods),
        )

    requests = int(totals.requests)
    error_rate = (totals.errors / totals.requests * 100.0) if totals.requests > 0 else 0.0
    return TrackMetrics(
        track=track,
        attributable=True,
        source="pods",
        pods_ready=len(pods),
        pods_scraped=scraped,
        total_requests=requests,
        error_rate_percent=error_rate,
        p95_latency_ms=_p95_from_buckets(totals.buckets, totals.observations),
    )


def status(namespace: str = DEFAULT_NAMESPACE, component: str = "api") -> dict[str, Any]:
    """Return a snapshot of canary rollout state for `namespace`/`component`.

    Includes whether a rollout is active, its current traffic weight,
    stable/canary health (as an honest not_deployed/unhealthy/healthy/error
    state plus the version each is running), the last 10 history entries, the
    currently detected deployment mode (with an explanation when it isn't
    Kubernetes), and whether kubectl is available (with a reason string when
    it isn't). An unknown or unsupported `component` (e.g. `ollama`, see
    `OLLAMA_UNSUPPORTED_REASON`) reports `available: False` with the reason
    in both `unavailable_reason` and `mode_message` instead of raising.

    The per-track health and metrics reads only happen in Kubernetes mode
    (#3858): in any other mode the tracks report the mode message rather than
    the result of a kubectl call that had no cluster to reach.

    `metrics` is the **canary track's** vitals -- the same input `evaluate()`
    gates on -- as a `TrackMetrics` dict, not this process's own counters
    (#3829). `stable_metrics` carries the stable track's for contrast and is
    only measured while a rollout is active: reading it costs one Pod-proxy
    call per stable Pod on every status poll, which is not worth spending
    when there is no canary to compare it against.
    """
    mode, mode_cause = _current_mode_with_reason()
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return {
            "namespace": namespace,
            "component": component,
            "active": False,
            "weight_percent": 0,
            "pool_replicas": 0,
            "resting_replicas": 0,
            "stable": {"state": "not_deployed", "message": err.message, "version": ""},
            "canary": {"state": "not_deployed", "message": err.message, "version": ""},
            "metrics": TrackMetrics(
                track="canary", attributable=False, reason=err.message
            ).as_dict(),
            "stable_metrics": TrackMetrics(
                track="stable", attributable=False, reason=err.message
            ).as_dict(),
            "history": [],
            "available": False,
            "unavailable_reason": err.message,
            "mode": mode,
            "mode_supported": False,
            "mode_message": err.message,
        }

    state = _load_state(component)
    # Outside Kubernetes there is no Deployment to ask about, so don't ask
    # (#3858) -- the same guard `ops.infra_status()` has applied since #3468.
    # On a native install this removes two `kubectl get deployment` calls per
    # poll rather than merely bounding them: cheaper, and it stops a stale
    # kubeconfig context from being dialed at all by a deployment that does
    # not use Kubernetes. The tracks report the mode as their reason, which is
    # the honest answer to "why is nothing deployed here".
    mode_supported = mode == "kubernetes"
    if mode_supported:
        stable_health = deployment_health(spec.stable_deployment, namespace)
        canary_health = deployment_health(spec.canary_deployment, namespace)
    else:
        skipped = TrackHealth("not_deployed", _non_kubernetes_mode_message(mode, mode_cause))
        stable_health = canary_health = skipped
    kubectl_present = _which("kubectl") is not None
    active = bool(state.get("active", False))
    weight_percent = state.get("weight_percent", 0)
    # The per-track metrics reads (#3829) are cluster calls too -- one Pod
    # list plus one Pod-proxy read per Pod -- so the same guard covers them.
    # `track_metrics` stops at `_which("kubectl")`, which is not enough here:
    # a developer machine can have kubectl installed and a stale context while
    # running natively, which is precisely the blackholing dial #3858 exists to
    # remove. Outside Kubernetes mode the panels report the mode as the reason
    # they are not attributable, which is the honest answer.
    if not mode_supported:
        unmeasured_reason = _non_kubernetes_mode_message(mode, mode_cause)
        canary_metrics = TrackMetrics(track="canary", attributable=False, reason=unmeasured_reason)
        stable_metrics = TrackMetrics(track="stable", attributable=False, reason=unmeasured_reason)
    else:
        canary_metrics = track_metrics("canary", namespace, component=component)
        if active:
            stable_metrics = track_metrics("stable", namespace, component=component)
        else:
            stable_metrics = TrackMetrics(
                track="stable",
                attributable=False,
                reason="not measured while no rollout is in progress",
            )

    if component == "api":
        prom_metrics.CANARY_ROLLOUT_ACTIVE.set(1 if active else 0)
        prom_metrics.CANARY_WEIGHT_PERCENT.set(weight_percent)
        for track, health in (("stable", stable_health), ("canary", canary_health)):
            if health.version:
                prom_metrics.CANARY_TRACK_VERSION_INFO.labels(
                    track=track, version=health.version
                ).set(1)
    prom_metrics.CANARY_COMPONENT_ROLLOUT_ACTIVE.labels(component=component).set(1 if active else 0)
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(weight_percent)
    for track, health in (("stable", stable_health), ("canary", canary_health)):
        if health.version:
            prom_metrics.CANARY_COMPONENT_TRACK_VERSION_INFO.labels(
                component=component, track=track, version=health.version
            ).set(1)

    return {
        "namespace": namespace,
        "component": component,
        "active": active,
        "weight_percent": weight_percent,
        # The pool is elastic (#3833): `pool_replicas` is what the rollout
        # grew it to, `resting_replicas` is what promote/rollback will
        # return stable to. Both are 0/absent-shaped when no rollout has
        # ever run, and read from the state file, so this stays a
        # kubectl-free field.
        "pool_replicas": int(state.get("total_replicas", 0) or 0) if active else 0,
        "resting_replicas": _resting_from_state(state) if active else 0,
        "stable": {
            "state": stable_health.state,
            "message": stable_health.message,
            "version": stable_health.version,
        },
        "canary": {
            "state": canary_health.state,
            "message": canary_health.message,
            "version": canary_health.version,
        },
        "metrics": canary_metrics.as_dict(),
        "stable_metrics": stable_metrics.as_dict(),
        "history": state.get("history", [])[-10:],
        "available": kubectl_present,
        "unavailable_reason": (
            None
            if kubectl_present
            else _kubectl_missing_message("kubectl not found; cannot check deployment health")
        ),
        "mode": mode,
        "mode_supported": mode_supported,
        "mode_message": _mode_message(mode, mode_cause),
    }


def deploy(
    namespace: str = DEFAULT_NAMESPACE,
    *,
    image: str | None = None,
    component: str = "api",
) -> CanaryResult:
    """Build the current checkout into a versioned image and deploy it to canary ONLY.

    Never touches stable -- a failed build/patch/rollout leaves stable exactly as it
    was (see #3409). Traffic weighting is a separate, deliberate action (`start`/
    `promote`); this only changes which code the canary Deployment's Pods run.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    if _which("kubectl") is None:
        message = _kubectl_missing_message(
            f"kubectl not found; cannot deploy to {component} canary"
        )
        ops_module.record_canary_action("deploy", "failure", message, component=component)
        return CanaryResult(False, message)

    tag = image or _versioned_image_tag(spec)
    logger.info(
        "canary: deploying %s to %s only",
        tag,
        spec.canary_deployment,
        extra={
            "component": "canary",
            "action": "deploy",
            "version": tag,
            "canary_component": component,
        },
    )

    if component == "api":
        # Unchanged from before #3419: exactly one positional argument, so
        # tests (and any other caller) mocking this with a single-arg stub
        # keep working.
        build_results = ops_module.build_and_load_k8s_image(tag)
    else:
        build_results = ops_module.build_and_load_k8s_image(
            tag,
            context=spec.build_context,
            fingerprint_paths=spec.build_fingerprint_paths,
            excludes=spec.build_excludes,
            build_args=spec.build_args,
        )
    if not all(r.ok for r in build_results):
        detail = "; ".join(r.message for r in build_results if not r.ok)
        ops_module.record_canary_action("deploy", "failure", detail, component=component)
        return CanaryResult(False, f"Failed to build/load {tag}", detail)

    set_result = _set_image(spec.canary_deployment, tag, namespace, container=spec.container_name)
    if not set_result.ok:
        ops_module.record_canary_action(
            "deploy", "failure", set_result.message, component=component
        )
        return set_result

    rollout_result = _wait_rollout(spec.canary_deployment, namespace)
    if not rollout_result.ok:
        message = (
            f"Deployed {tag} to {spec.canary_deployment} but its rollout did not become "
            "healthy; stable was not touched"
        )
        ops_module.record_canary_action("deploy", "failure", message, component=component)
        logger.error(
            "canary: %s: %s",
            message,
            rollout_result.message,
            extra={
                "component": "canary",
                "action": "deploy",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return CanaryResult(False, message, rollout_result.message)

    state = _load_state(component)
    history = state.setdefault("history", [])
    history.append({"action": "deploy", "version": tag, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="deploy", result="ok").inc()
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="deploy", result="ok"
    ).inc()
    message = f"Deployed {tag} to {spec.canary_deployment}"
    ops_module.record_canary_action("deploy", "success", message, component=component)
    logger.info(
        "canary: %s",
        message,
        extra={
            "component": "canary",
            "action": "deploy",
            "outcome": "ok",
            "version": tag,
            "canary_component": component,
        },
    )
    return CanaryResult(True, f"{message}; start or continue a rollout to shift traffic to it")


def start(
    namespace: str = DEFAULT_NAMESPACE,
    weight_percent: int = 10,
    total_replicas: int | None = None,
    *,
    component: str = "api",
) -> CanaryResult:
    """Start a canary rollout, growing the pool to serve `weight_percent` from the canary.

    `total_replicas` is a CEILING on how far the pool may be inflated for the
    duration of the rollout (`[canary] total_replicas`, 4 by default from the
    CLI/API), not a standing pool to subdivide (#3833). The pool starts from
    the stable Deployment's live replica count, grows only as far as the
    requested weight needs, and `promote`/`rollback` return stable to that
    same resting count. `None` means "inflate as little as possible" -- one
    replica beyond resting.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    state = _load_state(component)
    if state.get("active"):
        logger.info(
            "canary: start rejected, rollout already in progress at %s%%",
            state.get("weight_percent", 0),
            extra={
                "component": "canary",
                "action": "start",
                "outcome": "rejected",
                "canary_component": component,
            },
        )
        return CanaryResult(
            False, f"Canary rollout already in progress at {state.get('weight_percent', 0)}%"
        )
    if _which("kubectl") is None:
        message = _kubectl_missing_message("kubectl not found; cannot start canary rollout")
        logger.warning(
            "canary: start failed, %s",
            message,
            extra={"component": "canary", "action": "start", "canary_component": component},
        )
        return CanaryResult(False, message)

    weight_percent = max(1, min(99, weight_percent))
    resting = _resting_replicas(spec.stable_deployment, namespace)
    max_pool = resting + 1 if total_replicas is None else max(2, total_replicas)
    plan = _plan_rollout(resting, weight_percent, max_pool)

    logger.info(
        "canary: starting rollout at %d%% (canary=%d, stable=%d, resting=%d)",
        plan.achieved_percent,
        plan.canary_replicas,
        plan.stable_replicas,
        resting,
        extra={
            "component": "canary",
            "action": "start",
            "weight_percent": plan.achieved_percent,
            "requested_weight_percent": plan.requested_percent,
            "canary_component": component,
        },
    )

    failure = _scale_pool(spec, plan, namespace, current_stable=resting)
    if failure is not None:
        track, result = failure
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="start", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="start", result="failed"
        ).inc()
        ops_module.record_canary_action("start", "failure", result.message, component=component)
        logger.error(
            "canary: start failed scaling %s: %s",
            track,
            result.message,
            extra={
                "component": "canary",
                "action": "start",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return result

    state["active"] = True
    state["weight_percent"] = plan.achieved_percent
    state["total_replicas"] = plan.pool
    state["resting_replicas"] = resting
    state["stable_replicas"] = plan.stable_replicas
    history = state.setdefault("history", [])
    history.append({"action": "start", "weight_percent": plan.achieved_percent, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="start", result="ok").inc()
        prom_metrics.CANARY_ROLLOUT_ACTIVE.set(1)
        prom_metrics.CANARY_WEIGHT_PERCENT.set(plan.achieved_percent)
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="start", result="ok"
    ).inc()
    prom_metrics.CANARY_COMPONENT_ROLLOUT_ACTIVE.labels(component=component).set(1)
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(
        plan.achieved_percent
    )
    message = (
        f"Started canary rollout at {plan.describe()}; {spec.stable_deployment} returns to "
        f"{_replica_count(resting)} on promote or rollback"
    )
    ops_module.record_canary_action("start", "success", message, component=component)
    logger.info(
        "canary: %s",
        message,
        extra={
            "component": "canary",
            "action": "start",
            "outcome": "ok",
            "weight_percent": plan.achieved_percent,
            "canary_component": component,
        },
    )

    return CanaryResult(True, message)


def evaluate(
    namespace: str = DEFAULT_NAMESPACE,
    *,
    error_rate_threshold_percent: float = 5.0,
    latency_p95_threshold_ms: float = 2000.0,
    min_requests: int = 20,
    component: str = "api",
) -> CanaryResult:
    """Compare the CANARY TRACK's live error-rate/latency against thresholds.

    The metrics come from `track_metrics("canary", ...)` -- the canary Pods'
    own counters -- so a canary that has taken no traffic cannot be
    green-lit on the vitals of the stable Pod serving this request, and an
    auto-rollback cannot be provoked by load on stable (#3829).

    Automatically rolls back the canary if either threshold is breached.
    Returns ok=True with an explicit hold -- never "safe to promote" -- when
    the canary's traffic cannot be attributed at all (no ready Pods, no
    readable `/metrics`) or when too few of its requests have been observed
    to judge it yet, so a quiet canary doesn't get auto-rolled-back for lack
    of traffic.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    state = _load_state(component)
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")

    metrics = track_metrics("canary", namespace, component=component)
    if not metrics.attributable:
        if component == "api":
            prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="unattributable").inc()
        prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(
            component=component, result="unattributable"
        ).inc()
        logger.warning(
            "canary: evaluate holding, canary-track metrics unattributable (%s)",
            metrics.reason,
            extra={
                "component": "canary",
                "action": "evaluate",
                "outcome": "unattributable",
                "canary_component": component,
            },
        )
        return CanaryResult(
            True,
            f"Cannot evaluate the canary track: {metrics.reason}; holding at "
            f"{state.get('weight_percent', 0)}%",
        )

    if metrics.total_requests < min_requests:
        if component == "api":
            prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="insufficient_data").inc()
        prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(
            component=component, result="insufficient_data"
        ).inc()
        logger.info(
            "canary: evaluate holding, insufficient data (%d/%d canary-track requests)",
            metrics.total_requests,
            min_requests,
            extra={
                "component": "canary",
                "action": "evaluate",
                "outcome": "insufficient_data",
                "canary_component": component,
            },
        )
        return CanaryResult(
            True,
            f"Insufficient data to evaluate ({metrics.total_requests}/{min_requests} "
            f"canary-track requests observed across {metrics.pods_scraped} canary Pod(s)); "
            f"holding at {state.get('weight_percent', 0)}%",
        )

    breaches = []
    if metrics.error_rate_percent > error_rate_threshold_percent:
        breaches.append(
            f"error rate {metrics.error_rate_percent:.2f}% > {error_rate_threshold_percent}%"
        )
    if metrics.p95_latency_ms > latency_p95_threshold_ms:
        breaches.append(
            f"p95 latency {metrics.p95_latency_ms:.2f}ms > {latency_p95_threshold_ms}ms"
        )

    if breaches:
        if component == "api":
            prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="regression").inc()
        prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(
            component=component, result="regression"
        ).inc()
        logger.warning(
            "canary: evaluate detected regression (%s); rolling back",
            "; ".join(breaches),
            extra={
                "component": "canary",
                "action": "evaluate",
                "outcome": "regression",
                "canary_component": component,
            },
        )
        prom_metrics.CANARY_AUTO_ROLLBACK_TOTAL.labels(component=component).inc()
        rollback_result = rollback(namespace, trigger="auto", component=component)
        return CanaryResult(
            False,
            f"Canary-track metrics regression detected ({'; '.join(breaches)}); "
            "automatically rolled back",
            rollback_result.message,
        )

    if component == "api":
        prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="pass").inc()
    prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(component=component, result="pass").inc()
    logger.info(
        "canary: evaluate passed (error_rate=%.2f%%, p95=%.2fms); safe to promote",
        metrics.error_rate_percent,
        metrics.p95_latency_ms,
        extra={
            "component": "canary",
            "action": "evaluate",
            "outcome": "pass",
            "canary_component": component,
        },
    )
    return CanaryResult(
        True,
        f"Canary-track metrics within thresholds over {metrics.total_requests} requests "
        f"(error_rate={metrics.error_rate_percent:.2f}%, "
        f"p95={metrics.p95_latency_ms:.2f}ms); safe to promote",
    )


def _finalize_promotion(
    state: dict[str, Any],
    canary_health: TrackHealth,
    namespace: str,
    resting: int,
    spec: ComponentSpec,
    component: str,
    traffic_note: str = "",
) -> CanaryResult:
    """Complete a promotion: copy canary's image to stable, then return weight to 100% stable.

    Refuses (via the `promote()` health and canary-track traffic gates below)
    unless the canary is currently healthy and has actually served requests.
    `traffic_note` carries `promote()`'s caveat about the canary's measured
    traffic into the success message, so a forced or unmeasurable promotion
    says so in the operator's transcript.
    Stops -- leaving canary running and stable untouched -- if stable's
    rollout onto the new version doesn't become healthy, so an operator can retry
    or roll the canary back rather than being left in an ambiguous half-promoted
    state.

    `resting` is the replica count stable had before the rollout inflated the
    pool; deflating back to it here is what keeps the pool transient (#3833).
    """
    version = canary_health.version
    if not version:
        message = "Cannot determine the canary's image version to promote"
        ops_module.record_canary_action("promote", "failure", message, component=component)
        return CanaryResult(False, message)

    image = f"{spec.image_repository}:{version}"
    set_result = _set_image(spec.stable_deployment, image, namespace, container=spec.container_name)
    if not set_result.ok:
        ops_module.record_canary_action(
            "promote", "failure", set_result.message, component=component
        )
        return CanaryResult(
            False,
            f"Promotion failed updating {spec.stable_deployment}'s image; canary left untouched",
            set_result.message,
        )

    rollout_result = _wait_rollout(spec.stable_deployment, namespace)
    if not rollout_result.ok:
        ops_module.record_canary_action(
            "promote", "failure", rollout_result.message, component=component
        )
        return CanaryResult(
            False,
            f"{spec.stable_deployment} did not become healthy on {version}; canary left running "
            "so you can retry the promotion or roll back",
            rollout_result.message,
        )

    canary_result = _scale(spec.canary_deployment, 0, namespace)
    stable_result = _scale(spec.stable_deployment, resting, namespace)

    state["weight_percent"] = 0
    state["active"] = False
    state["total_replicas"] = resting
    state["stable_replicas"] = resting
    history = state.setdefault("history", [])
    history.append(
        {"action": "promote", "weight_percent": 100, "version": version, "ts": time.time()}
    )
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="ok").inc()
        prom_metrics.CANARY_WEIGHT_PERCENT.set(0)
        prom_metrics.CANARY_ROLLOUT_ACTIVE.set(0)
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="promote", result="ok"
    ).inc()
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(0)
    prom_metrics.CANARY_COMPONENT_ROLLOUT_ACTIVE.labels(component=component).set(0)
    message = (
        f"Promoted {version} to {spec.stable_deployment} at 100% traffic; canary scaled back to 0 "
        f"and stable back to its resting {_replica_count(resting)}{traffic_note}"
    )
    ops_module.record_canary_action("promote", "success", message, component=component)
    logger.info(
        "canary: %s",
        message,
        extra={
            "component": "canary",
            "action": "promote",
            "outcome": "ok",
            "weight_percent": 100,
            "canary_component": component,
        },
    )

    if not (canary_result.ok and stable_result.ok):
        return CanaryResult(
            True,
            f"Promoted {version} to {spec.stable_deployment}, but restoring steady-state replica "
            f"counts had an issue: {canary_result.message}; {stable_result.message}",
        )
    return CanaryResult(True, message)


def promote(
    namespace: str = DEFAULT_NAMESPACE,
    step_percent: int = 25,
    total_replicas: int | None = None,
    *,
    component: str = "api",
    force: bool = False,
) -> CanaryResult:
    """Increase the canary's traffic share by `step_percent`.

    The pool is re-planned for the new weight rather than re-sliced at a
    fixed width (#3833), so a step that needs more granularity grows the
    pool and one that needs less lets it shrink; `total_replicas` is the
    same ceiling `start` takes.

    At 100%, instead of leaving the canary holding all the traffic, this
    copies the canary's image version onto stable, waits for stable's
    rollout to become healthy, then scales canary back to 0 and stable back
    to the replica count it was resting at before the rollout -- completing
    the deploy -> gate -> promote cycle with stable now running the promoted
    version (see #3409). Refuses to shift more traffic to an unhealthy
    canary at every step, including this final one.

    Also refuses -- at every step -- to promote a canary track that has
    *measurably* served no traffic (#3829): a build no request has ever
    reached has not been canaried, whatever its Pods' health says. The gate
    only fires where traffic is measurable, so a component whose Pods export
    no metrics (`web`) is promoted on health alone with that stated in the
    result. `force=True` promotes anyway, for the case the gate cannot tell
    apart from a broken canary: an idle cluster nobody is sending requests
    to.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    state = _load_state(component)
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")
    if _which("kubectl") is None:
        message = _kubectl_missing_message("kubectl not found; cannot promote canary rollout")
        logger.warning(
            "canary: promote failed, %s",
            message,
            extra={"component": "canary", "action": "promote", "canary_component": component},
        )
        return CanaryResult(False, message)

    canary_health = deployment_health(spec.canary_deployment, namespace)
    if canary_health.state != "healthy":
        message = f"Refusing to shift more traffic to canary: {canary_health.message}"
        ops_module.record_canary_action("promote", "refused", message, component=component)
        logger.warning(
            "canary: %s",
            message,
            extra={
                "component": "canary",
                "action": "promote",
                "outcome": "refused",
                "canary_component": component,
            },
        )
        return CanaryResult(False, message)

    traffic_note = ""
    canary_traffic = track_metrics("canary", namespace, component=component)
    if canary_traffic.attributable and canary_traffic.total_requests <= 0:
        if not force:
            message = (
                "Refusing to promote a canary that has served no traffic: its "
                f"{canary_traffic.pods_ready} ready Pod(s) have handled 0 requests "
                "(health probes and metrics scrapes excluded). Send traffic through the "
                "Service and evaluate first, or force the promotion if this cluster is "
                "simply idle (`nyxgpt canary promote --force`, or the Promote page's "
                '"no canary traffic" override).'
            )
            ops_module.record_canary_action("promote", "refused", message, component=component)
            logger.warning(
                "canary: %s",
                message,
                extra={
                    "component": "canary",
                    "action": "promote",
                    "outcome": "refused",
                    "canary_component": component,
                },
            )
            return CanaryResult(False, message)
        traffic_note = " (forced: the canary track has served no traffic)"
    elif not canary_traffic.attributable:
        traffic_note = f" (canary traffic not verified: {canary_traffic.reason})"

    resting = _resting_from_state(state, total_replicas)
    max_pool = resting + 1 if total_replicas is None else max(2, total_replicas)
    new_weight = min(100, state.get("weight_percent", 0) + max(1, step_percent))

    if new_weight >= 100:
        return _finalize_promotion(
            state, canary_health, namespace, resting, spec, component, traffic_note
        )

    plan = _plan_rollout(resting, new_weight, max_pool)
    current_stable = _first_replica_count(
        _desired_replicas(spec.stable_deployment, namespace),
        state.get("stable_replicas"),
        resting,
    )

    logger.info(
        "canary: promoting rollout from %d%% to %d%% (canary=%d, stable=%d)",
        state.get("weight_percent", 0),
        plan.achieved_percent,
        plan.canary_replicas,
        plan.stable_replicas,
        extra={
            "component": "canary",
            "action": "promote",
            "weight_percent": plan.achieved_percent,
            "requested_weight_percent": plan.requested_percent,
            "canary_component": component,
        },
    )

    failure = _scale_pool(spec, plan, namespace, current_stable=current_stable)
    if failure is not None:
        track, result = failure
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="promote", result="failed"
        ).inc()
        ops_module.record_canary_action("promote", "failure", result.message, component=component)
        logger.error(
            "canary: promote failed scaling %s: %s",
            track,
            result.message,
            extra={
                "component": "canary",
                "action": "promote",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return result

    state["weight_percent"] = plan.achieved_percent
    state["total_replicas"] = plan.pool
    state["stable_replicas"] = plan.stable_replicas
    state["resting_replicas"] = resting
    history = state.setdefault("history", [])
    history.append(
        {"action": "promote", "weight_percent": plan.achieved_percent, "ts": time.time()}
    )
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="ok").inc()
        prom_metrics.CANARY_WEIGHT_PERCENT.set(plan.achieved_percent)
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="promote", result="ok"
    ).inc()
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(
        plan.achieved_percent
    )
    message = f"Promoted canary to {plan.describe()}{traffic_note}"
    ops_module.record_canary_action("promote", "success", message, component=component)
    logger.info(
        "canary: %s",
        message,
        extra={
            "component": "canary",
            "action": "promote",
            "outcome": "ok",
            "weight_percent": new_weight,
            "canary_component": component,
        },
    )
    return CanaryResult(True, message)


def rollback(
    namespace: str = DEFAULT_NAMESPACE,
    total_replicas: int | None = None,
    *,
    trigger: str = "manual",
    component: str = "api",
) -> CanaryResult:
    """Cut all traffic back to the stable Deployment and deflate the pool.

    Scales the canary Deployment to 0 first (removing it from the Service's
    endpoints, which stops it receiving traffic) before restoring stable --
    this is the emergency escape hatch and must not be blocked by a flaky
    stable-scale-up.

    Stable is restored to the count `start` recorded it resting at, not to
    `total_replicas` (#3833): the rollout is what inflated the pool, so
    ending the rollout is what has to give the capacity back. `total_replicas`
    survives only as a fallback for a state file written before that count
    was recorded.

    `trigger` is "manual" for an operator-initiated rollback (dashboard/CLI/
    API) or "auto" when called from `evaluate()`'s automatic regression
    rollback -- recorded on the `nyxgpt_canary_events_total` metric and in
    the log line so a dashboard/log query can distinguish the two.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    state = _load_state(component)
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")
    if _which("kubectl") is None:
        message = _kubectl_missing_message("kubectl not found; cannot roll back canary rollout")
        logger.warning(
            "canary: rollback failed, %s",
            message,
            extra={
                "component": "canary",
                "action": "rollback",
                "trigger": trigger,
                "canary_component": component,
            },
        )
        return CanaryResult(False, message)

    resting = _resting_from_state(state, total_replicas)
    previous_weight = state.get("weight_percent", 0)

    logger.info(
        "canary: rolling back from %d%% (trigger=%s)",
        previous_weight,
        trigger,
        extra={
            "component": "canary",
            "action": "rollback",
            "trigger": trigger,
            "weight_percent": previous_weight,
            "canary_component": component,
        },
    )

    canary_result = _scale(spec.canary_deployment, 0, namespace)
    if not canary_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="rollback", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="rollback", result="failed"
        ).inc()
        ops_module.record_canary_action(
            "rollback", "failure", canary_result.message, component=component
        )
        logger.error(
            "canary: rollback failed scaling canary to 0: %s",
            canary_result.message,
            extra={
                "component": "canary",
                "action": "rollback",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return canary_result
    stable_result = _scale(spec.stable_deployment, resting, namespace)

    state["active"] = False
    state["weight_percent"] = 0
    state["total_replicas"] = resting
    state["stable_replicas"] = resting
    history = state.setdefault("history", [])
    history.append(
        {"action": "rollback", "from_weight_percent": previous_weight, "ts": time.time()}
    )
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_ROLLOUT_ACTIVE.set(0)
        prom_metrics.CANARY_WEIGHT_PERCENT.set(0)
    prom_metrics.CANARY_COMPONENT_ROLLOUT_ACTIVE.labels(component=component).set(0)
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(0)

    if not stable_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="rollback", result="partial").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="rollback", result="partial"
        ).inc()
        ops_module.record_canary_action(
            "rollback", "partial", stable_result.message, component=component
        )
        logger.warning(
            "canary: rollback partially failed, canary stopped but stable restore failed: %s",
            stable_result.message,
            extra={
                "component": "canary",
                "action": "rollback",
                "outcome": "partial",
                "canary_component": component,
            },
        )
        return CanaryResult(
            True,
            f"Canary traffic stopped (scaled to 0%), but restoring {spec.stable_deployment} to "
            f"{_replica_count(resting)} failed: {stable_result.message}",
        )

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="rollback", result="ok").inc()
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="rollback", result="ok"
    ).inc()
    message = (
        f"Rolled back canary rollout from {previous_weight}% to 0%; {spec.stable_deployment} "
        f"restored to its resting {_replica_count(resting)}"
    )
    ops_module.record_canary_action("rollback", "success", message, component=component)
    logger.info(
        "canary: rolled back from %d%% to 0%% (trigger=%s)",
        previous_weight,
        trigger,
        extra={
            "component": "canary",
            "action": "rollback",
            "outcome": "ok",
            "trigger": trigger,
            "canary_component": component,
        },
    )
    return CanaryResult(True, message)


__all__ = [
    "CanaryResult",
    "TrackHealth",
    "TrackMetrics",
    "RolloutPlan",
    "ComponentSpec",
    "COMPONENTS",
    "SERVICE_NAME",
    "STABLE_DEPLOYMENT",
    "CANARY_DEPLOYMENT",
    "IMAGE_REPOSITORY",
    "WEB_SERVICE_NAME",
    "WEB_STABLE_DEPLOYMENT",
    "WEB_CANARY_DEPLOYMENT",
    "WEB_IMAGE_REPOSITORY",
    "DEFAULT_NAMESPACE",
    "DEFAULT_RESTING_REPLICAS",
    "OLLAMA_UNSUPPORTED_REASON",
    "MAX_SCRAPED_PODS",
    "NON_TRAFFIC_PATHS",
    "current_mode",
    "deployment_health",
    "track_metrics",
    "status",
    "deploy",
    "start",
    "evaluate",
    "promote",
    "rollback",
]
