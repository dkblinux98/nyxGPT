"""Canary deployment for nyxGPT components on a local Kubernetes cluster.

Runs a second `<component>-canary` Deployment alongside the existing
`<component>-stable` Deployment, both fronted by a single Service (see
k8s/service-canary.yaml / k8s/service-web-canary.yaml). Traffic is split by
replica-count ratio: kube-proxy round-robins Service traffic evenly across
every matching Pod endpoint, so `canary_replicas / total_replicas`
approximates the canary's share of requests. There is no cloud traffic
manager or extra in-cluster proxy involved -- this targets the same
local-cluster workflow documented in docs/kubernetes.md (kind/minikube/k3s).

This is the sole deployment model as of #3409 -- blue/green (deploy.py) was
retired in favor of canary, which is a strict superset for traffic purposes
(0%/100% reproduces blue/green's cutover) plus metrics-gated gradual shift
and auto-rollback. `api` and `web` are covered as of #3419 (every public
function below takes a `component: str = "api"` parameter -- see
`COMPONENTS`); `ollama` is deliberately not implemented (see
`OLLAMA_UNSUPPORTED_REASON` and docs/kubernetes.md#ollama-canary-feasibility
for why), and the Cassandra data-migration story remains deferred per the
#3409 owner decision.

Metrics-based promotion/rollback reads the process-wide ResourceMonitor
(error rate + p95 latency, see resource_monitor.py) since dedicated
Prometheus metrics (#2693) have not landed yet.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import metrics as prom_metrics
from nyxgpt import ops as ops_module
from nyxgpt.ops import OpsResult as CanaryResult
from nyxgpt.resource_monitor import get_resource_monitor

logger = logging.getLogger(__name__)

SERVICE_NAME = "nyxgpt-api-canary"
STABLE_DEPLOYMENT = "nyxgpt-api-stable"
CANARY_DEPLOYMENT = "nyxgpt-api-canary"
IMAGE_REPOSITORY = "nyxgpt-api"
DEFAULT_NAMESPACE = "nyxgpt"
DEFAULT_TOTAL_REPLICAS = 4
DEFAULT_ROLLOUT_TIMEOUT_SECONDS = 180
HISTORY_LIMIT = 20

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
DEFAULT_WEB_TOTAL_REPLICAS = 4

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
    default_total_replicas: int
    supported: bool = True
    unsupported_reason: str = ""
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
        default_total_replicas=DEFAULT_TOTAL_REPLICAS,
    ),
    "web": ComponentSpec(
        key="web",
        stable_deployment=WEB_STABLE_DEPLOYMENT,
        canary_deployment=WEB_CANARY_DEPLOYMENT,
        service_name=WEB_SERVICE_NAME,
        image_repository=WEB_IMAGE_REPOSITORY,
        container_name=WEB_CONTAINER_NAME,
        default_total_replicas=DEFAULT_WEB_TOTAL_REPLICAS,
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
        default_total_replicas=0,
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


def _run(cmd: list[str], *, expected: bool = False) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text without raising on non-zero exit.

    Non-zero exits are logged with the command and a stderr tail so failed
    kubectl/rollout invocations show up in Loki instead of only reaching the
    caller via the returned `CompletedProcess` (#3415 gap 5). Pass `expected=True`
    for read-only probes where a non-zero exit is a normal outcome, to log at
    DEBUG instead of WARNING.
    """
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
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


def deployment_health(name: str, namespace: str = DEFAULT_NAMESPACE) -> TrackHealth:
    """Check whether the given Deployment is fully ready, and what version it's running.

    Mirrors the readinessProbe (`GET /health`) already configured on the
    stable/canary Deployments: a Deployment only reports its Pods as Ready
    once the probe passes.
    """
    if _which("kubectl") is None:
        return TrackHealth(
            "not_deployed",
            _kubectl_missing_message("kubectl not found; cannot check deployment health"),
        )

    cp = _run(["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"], expected=True)
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
        return TrackHealth("error", f"Could not read deployment {name}", stderr)

    try:
        data = json.loads(cp.stdout)
    except Exception as e:
        return TrackHealth("error", f"Could not parse status for {name}", str(e))

    containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    image = str(containers[0].get("image", "")) if containers else ""
    version = image.split(":", 1)[1] if ":" in image else image

    spec_replicas = data.get("spec", {}).get("replicas", 0) or 0
    ready = data.get("status", {}).get("readyReplicas", 0) or 0

    if spec_replicas == 0:
        return TrackHealth("not_deployed", f"{name} has 0 desired replicas (idle)", version)
    if ready >= spec_replicas:
        return TrackHealth("healthy", f"{name} healthy ({ready}/{spec_replicas} ready)", version)
    return TrackHealth("unhealthy", f"{name} not healthy ({ready}/{spec_replicas} ready)", version)


def current_mode() -> str:
    """Best-effort classification of which deployment mode this process is running under.

    Not inferred from a failed kubectl call against the canary/stable Deployments
    (`deployment_health` above is the honest per-track answer to that question) --
    this checks the same signals #3193/#3344's mode-aware self-heal dispatch and
    `ops.detect_deployment_mode()`/`ops.terraform_stack_state()` already use: the
    NYXGPT_COMPOSE_FILE marker, then a running Terraform-managed container stack,
    then a populated Kubernetes namespace, falling back to "native" (Homebrew
    services, no Terraform/Kubernetes stack detected). Returns one of "compose",
    "terraform", "kubernetes", "native".
    """
    if _compose_mode():
        return "compose"
    try:
        tf_state = ops_module.terraform_stack_state()
        if any(state != "absent" for state in tf_state.values()):
            return "terraform"
    except Exception:
        pass
    if _which("kubectl") is not None:
        cp = _run(
            ["kubectl", "-n", DEFAULT_NAMESPACE, "get", "pods", "--no-headers"], expected=True
        )
        if cp.returncode == 0 and (cp.stdout or "").strip():
            return "kubernetes"
    return "native"


def _mode_message(mode: str) -> str | None:
    """Explain why canary doesn't apply outside Kubernetes mode, and which mode provides it."""
    if mode == "kubernetes":
        return None
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
        ]
    )
    if cp.returncode != 0:
        return CanaryResult(
            False,
            f"Rollout of {name} did not become healthy within {timeout_seconds}s",
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
    """Split `total` replicas into (canary, stable) counts for a weight_percent.

    Canary gets at least 1 replica once weight_percent > 0 (otherwise it
    would receive no traffic at all despite being "active").
    """
    if weight_percent <= 0:
        return 0, total
    if weight_percent >= 100:
        return total, 0
    canary = max(1, round(total * weight_percent / 100))
    canary = min(canary, total - 1) if total > 1 else canary
    return canary, total - canary


def metrics_snapshot() -> dict[str, Any]:
    """Return the current error-rate/latency snapshot from the ResourceMonitor."""
    monitor = get_resource_monitor()
    if monitor is None:
        return {"total_requests": 0, "error_rate_percent": 0.0, "p95_latency_ms": 0.0}
    metrics = monitor.get_metrics()
    return {
        "total_requests": metrics.total_requests,
        "error_rate_percent": metrics.error_rate_percent,
        "p95_latency_ms": metrics.p95_request_latency_ms,
    }


def status(namespace: str = DEFAULT_NAMESPACE, component: str = "api") -> dict[str, Any]:
    """Return a snapshot of canary rollout state for `namespace`/`component`.

    Includes whether a rollout is active, its current traffic weight,
    stable/canary health (as an honest not_deployed/unhealthy/healthy/error
    state plus the version each is running), a live error-rate/latency
    metrics snapshot, the last 10 history entries, the currently detected
    deployment mode (with an explanation when it isn't Kubernetes), and
    whether kubectl is available (with a reason string when it isn't). An
    unknown or unsupported `component` (e.g. `ollama`, see
    `OLLAMA_UNSUPPORTED_REASON`) reports `available: False` with the reason
    in both `unavailable_reason` and `mode_message` instead of raising.
    """
    mode = current_mode()
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return {
            "namespace": namespace,
            "component": component,
            "active": False,
            "weight_percent": 0,
            "stable": {"state": "not_deployed", "message": err.message, "version": ""},
            "canary": {"state": "not_deployed", "message": err.message, "version": ""},
            "metrics": metrics_snapshot(),
            "history": [],
            "available": False,
            "unavailable_reason": err.message,
            "mode": mode,
            "mode_supported": False,
            "mode_message": err.message,
        }

    state = _load_state(component)
    stable_health = deployment_health(spec.stable_deployment, namespace)
    canary_health = deployment_health(spec.canary_deployment, namespace)
    kubectl_present = _which("kubectl") is not None
    active = bool(state.get("active", False))
    weight_percent = state.get("weight_percent", 0)

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
        "metrics": metrics_snapshot(),
        "history": state.get("history", [])[-10:],
        "available": kubectl_present,
        "unavailable_reason": (
            None
            if kubectl_present
            else _kubectl_missing_message("kubectl not found; cannot check deployment health")
        ),
        "mode": mode,
        "mode_supported": mode == "kubernetes",
        "mode_message": _mode_message(mode),
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
    total_replicas: int = DEFAULT_TOTAL_REPLICAS,
    *,
    component: str = "api",
) -> CanaryResult:
    """Start a canary rollout: scale up the canary Deployment to `weight_percent` of traffic."""
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
    canary_replicas, stable_replicas = _split_replicas(total_replicas, weight_percent)

    logger.info(
        "canary: starting rollout at %d%% (canary=%d, stable=%d)",
        weight_percent,
        canary_replicas,
        stable_replicas,
        extra={
            "component": "canary",
            "action": "start",
            "weight_percent": weight_percent,
            "canary_component": component,
        },
    )

    canary_result = _scale(spec.canary_deployment, canary_replicas, namespace)
    if not canary_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="start", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="start", result="failed"
        ).inc()
        ops_module.record_canary_action(
            "start", "failure", canary_result.message, component=component
        )
        logger.error(
            "canary: start failed scaling canary: %s",
            canary_result.message,
            extra={
                "component": "canary",
                "action": "start",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return canary_result
    stable_result = _scale(spec.stable_deployment, stable_replicas, namespace)
    if not stable_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="start", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="start", result="failed"
        ).inc()
        ops_module.record_canary_action(
            "start", "failure", stable_result.message, component=component
        )
        logger.error(
            "canary: start failed scaling stable: %s",
            stable_result.message,
            extra={
                "component": "canary",
                "action": "start",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return stable_result

    state["active"] = True
    state["weight_percent"] = weight_percent
    state["total_replicas"] = total_replicas
    history = state.setdefault("history", [])
    history.append({"action": "start", "weight_percent": weight_percent, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="start", result="ok").inc()
        prom_metrics.CANARY_ROLLOUT_ACTIVE.set(1)
        prom_metrics.CANARY_WEIGHT_PERCENT.set(weight_percent)
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="start", result="ok"
    ).inc()
    prom_metrics.CANARY_COMPONENT_ROLLOUT_ACTIVE.labels(component=component).set(1)
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(weight_percent)
    message = (
        f"Started canary rollout at {weight_percent}% ({canary_replicas}/{total_replicas} replicas)"
    )
    ops_module.record_canary_action("start", "success", message, component=component)
    logger.info(
        "canary: %s",
        message,
        extra={
            "component": "canary",
            "action": "start",
            "outcome": "ok",
            "weight_percent": weight_percent,
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
    """Compare live error-rate/latency metrics against thresholds.

    Automatically rolls back the canary if either threshold is breached.
    Returns ok=True (with an "insufficient data" note) when too few requests
    have been observed to judge the canary yet, so a quiet canary doesn't
    get auto-rolled-back for lack of traffic.
    """
    spec, err = _component_spec(component)
    if spec is None:
        assert err is not None
        return err
    state = _load_state(component)
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")

    metrics = metrics_snapshot()
    if metrics["total_requests"] < min_requests:
        if component == "api":
            prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="insufficient_data").inc()
        prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(
            component=component, result="insufficient_data"
        ).inc()
        logger.info(
            "canary: evaluate holding, insufficient data (%d/%d requests)",
            metrics["total_requests"],
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
            f"Insufficient data to evaluate ({metrics['total_requests']}/{min_requests} "
            f"requests observed); holding at {state.get('weight_percent', 0)}%",
        )

    breaches = []
    if metrics["error_rate_percent"] > error_rate_threshold_percent:
        breaches.append(
            f"error rate {metrics['error_rate_percent']:.2f}% > {error_rate_threshold_percent}%"
        )
    if metrics["p95_latency_ms"] > latency_p95_threshold_ms:
        breaches.append(
            f"p95 latency {metrics['p95_latency_ms']:.2f}ms > {latency_p95_threshold_ms}ms"
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
            f"Metrics regression detected ({'; '.join(breaches)}); automatically rolled back",
            rollback_result.message,
        )

    if component == "api":
        prom_metrics.CANARY_EVALUATIONS_TOTAL.labels(result="pass").inc()
    prom_metrics.CANARY_COMPONENT_EVALUATIONS_TOTAL.labels(component=component, result="pass").inc()
    logger.info(
        "canary: evaluate passed (error_rate=%.2f%%, p95=%.2fms); safe to promote",
        metrics["error_rate_percent"],
        metrics["p95_latency_ms"],
        extra={
            "component": "canary",
            "action": "evaluate",
            "outcome": "pass",
            "canary_component": component,
        },
    )
    return CanaryResult(
        True,
        f"Metrics within thresholds (error_rate={metrics['error_rate_percent']:.2f}%, "
        f"p95={metrics['p95_latency_ms']:.2f}ms); safe to promote",
    )


def _finalize_promotion(
    state: dict[str, Any],
    canary_health: TrackHealth,
    namespace: str,
    total: int,
    spec: ComponentSpec,
    component: str,
) -> CanaryResult:
    """Complete a promotion: copy canary's image to stable, then return weight to 100% stable.

    Refuses (via the `promote()` health gate below) unless the canary is currently
    healthy. Stops -- leaving canary running and stable untouched -- if stable's
    rollout onto the new version doesn't become healthy, so an operator can retry
    or roll the canary back rather than being left in an ambiguous half-promoted
    state.
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
    stable_result = _scale(spec.stable_deployment, total, namespace)

    state["weight_percent"] = 0
    state["active"] = False
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
        f"Promoted {version} to {spec.stable_deployment} at 100% traffic; canary scaled back to 0"
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
    total_replicas: int = DEFAULT_TOTAL_REPLICAS,
    *,
    component: str = "api",
) -> CanaryResult:
    """Increase the canary's traffic share by `step_percent`.

    At 100%, instead of leaving the canary holding all the traffic, this
    copies the canary's image version onto stable, waits for stable's
    rollout to become healthy, then scales canary back to 0 and stable back
    to `total_replicas` -- completing the deploy -> gate -> promote cycle
    with stable now running the promoted version (see #3409). Refuses to
    shift more traffic to an unhealthy canary at every step, including this
    final one.
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

    total = state.get("total_replicas", total_replicas)
    new_weight = min(100, state.get("weight_percent", 0) + max(1, step_percent))

    if new_weight >= 100:
        return _finalize_promotion(state, canary_health, namespace, total, spec, component)

    canary_replicas, stable_replicas = _split_replicas(total, new_weight)

    logger.info(
        "canary: promoting rollout from %d%% to %d%% (canary=%d, stable=%d)",
        state.get("weight_percent", 0),
        new_weight,
        canary_replicas,
        stable_replicas,
        extra={
            "component": "canary",
            "action": "promote",
            "weight_percent": new_weight,
            "canary_component": component,
        },
    )

    canary_result = _scale(spec.canary_deployment, canary_replicas, namespace)
    if not canary_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="promote", result="failed"
        ).inc()
        ops_module.record_canary_action(
            "promote", "failure", canary_result.message, component=component
        )
        logger.error(
            "canary: promote failed scaling canary: %s",
            canary_result.message,
            extra={
                "component": "canary",
                "action": "promote",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return canary_result
    stable_result = _scale(spec.stable_deployment, stable_replicas, namespace)
    if not stable_result.ok:
        if component == "api":
            prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="failed").inc()
        prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
            component=component, action="promote", result="failed"
        ).inc()
        ops_module.record_canary_action(
            "promote", "failure", stable_result.message, component=component
        )
        logger.error(
            "canary: promote failed scaling stable: %s",
            stable_result.message,
            extra={
                "component": "canary",
                "action": "promote",
                "outcome": "failed",
                "canary_component": component,
            },
        )
        return stable_result

    state["weight_percent"] = new_weight
    history = state.setdefault("history", [])
    history.append({"action": "promote", "weight_percent": new_weight, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state, component)

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="promote", result="ok").inc()
        prom_metrics.CANARY_WEIGHT_PERCENT.set(new_weight)
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="promote", result="ok"
    ).inc()
    prom_metrics.CANARY_COMPONENT_WEIGHT_PERCENT.labels(component=component).set(new_weight)
    message = f"Promoted canary to {new_weight}% ({canary_replicas}/{total} replicas)"
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
    total_replicas: int = DEFAULT_TOTAL_REPLICAS,
    *,
    trigger: str = "manual",
    component: str = "api",
) -> CanaryResult:
    """Cut all traffic back to the stable Deployment.

    Scales the canary Deployment to 0 first (removing it from the Service's
    endpoints, which stops it receiving traffic) before restoring stable --
    this is the emergency escape hatch and must not be blocked by a flaky
    stable-scale-up.

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

    total = state.get("total_replicas", total_replicas)
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
    stable_result = _scale(spec.stable_deployment, total, namespace)

    state["active"] = False
    state["weight_percent"] = 0
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
            f"{total} replicas failed: {stable_result.message}",
        )

    if component == "api":
        prom_metrics.CANARY_EVENTS_TOTAL.labels(action="rollback", result="ok").inc()
    prom_metrics.CANARY_COMPONENT_EVENTS_TOTAL.labels(
        component=component, action="rollback", result="ok"
    ).inc()
    message = f"Rolled back canary rollout from {previous_weight}% to 0%"
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
    "DEFAULT_TOTAL_REPLICAS",
    "DEFAULT_WEB_TOTAL_REPLICAS",
    "OLLAMA_UNSUPPORTED_REASON",
    "current_mode",
    "deployment_health",
    "metrics_snapshot",
    "status",
    "deploy",
    "start",
    "evaluate",
    "promote",
    "rollback",
]
