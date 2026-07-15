"""Local canary deployment for the nyxGPT API on a local Kubernetes cluster.

Runs a second `nyxgpt-api-canary` Deployment alongside the existing
`nyxgpt-api-stable` Deployment, both fronted by a single Service
(`nyxgpt-api-canary`, see k8s/service-canary.yaml). Traffic is split by
replica-count ratio: kube-proxy round-robins Service traffic evenly across
every matching Pod endpoint, so `canary_replicas / total_replicas`
approximates the canary's share of requests. There is no cloud traffic
manager or extra in-cluster proxy involved -- this targets the same
local-cluster workflow documented in docs/kubernetes.md (kind/minikube/k3s)
as the blue/green deployment in deploy.py.

Metrics-based promotion/rollback reads the process-wide ResourceMonitor
(error rate + p95 latency, see resource_monitor.py) since dedicated
Prometheus metrics (#2693) have not landed yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from nyxgpt.ops import OpsResult as CanaryResult
from nyxgpt.resource_monitor import get_resource_monitor

SERVICE_NAME = "nyxgpt-api-canary"
STABLE_DEPLOYMENT = "nyxgpt-api-stable"
CANARY_DEPLOYMENT = "nyxgpt-api-canary"
DEFAULT_NAMESPACE = "nyxgpt"
DEFAULT_TOTAL_REPLICAS = 4
HISTORY_LIMIT = 20

NOT_SUPPORTED_UNDER_COMPOSE = (
    "Canary deployment requires the Kubernetes deployment mode; not "
    "available under docker-compose. See docs/kubernetes.md."
)


def _which(prog: str) -> str | None:
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
    return NOT_SUPPORTED_UNDER_COMPOSE if _compose_mode() else fallback


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _state_path() -> Path:
    return Path.home() / ".nyxGPT" / "canary_state.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"active": False, "weight_percent": 0, "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("active", False)
            data.setdefault("weight_percent", 0)
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"active": False, "weight_percent": 0, "history": []}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def deployment_health(name: str, namespace: str = DEFAULT_NAMESPACE) -> CanaryResult:
    """Check whether the given Deployment is fully ready.

    Mirrors deploy.py's deployment_health() -- relies on the same `/health`
    readinessProbe configured on the stable/canary Deployments.
    """
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot check deployment health")
        )

    cp = _run(["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"])
    if cp.returncode != 0:
        return CanaryResult(False, f"Could not read deployment {name}", (cp.stderr or "").strip())

    try:
        data = json.loads(cp.stdout)
    except Exception as e:
        return CanaryResult(False, f"Could not parse status for {name}", str(e))

    spec_replicas = data.get("spec", {}).get("replicas", 0) or 0
    ready = data.get("status", {}).get("readyReplicas", 0) or 0

    if spec_replicas == 0:
        return CanaryResult(False, f"{name} has 0 desired replicas")
    if ready >= spec_replicas:
        return CanaryResult(True, f"{name} healthy ({ready}/{spec_replicas} ready)")
    return CanaryResult(False, f"{name} not healthy ({ready}/{spec_replicas} ready)")


def _scale(name: str, replicas: int, namespace: str = DEFAULT_NAMESPACE) -> CanaryResult:
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot scale deployment")
        )
    cp = _run(["kubectl", "scale", "deployment", name, "-n", namespace, f"--replicas={replicas}"])
    if cp.returncode != 0:
        return CanaryResult(False, f"kubectl scale failed for {name}", (cp.stderr or "").strip())
    return CanaryResult(True, f"Scaled {name} to {replicas} replicas")


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


def status(namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    state = _load_state()
    stable_health = deployment_health(STABLE_DEPLOYMENT, namespace)
    canary_health = deployment_health(CANARY_DEPLOYMENT, namespace)
    kubectl_present = _which("kubectl") is not None
    return {
        "namespace": namespace,
        "active": bool(state.get("active", False)),
        "weight_percent": state.get("weight_percent", 0),
        "stable": {"healthy": stable_health.ok, "message": stable_health.message},
        "canary": {"healthy": canary_health.ok, "message": canary_health.message},
        "metrics": metrics_snapshot(),
        "history": state.get("history", [])[-10:],
        "available": kubectl_present,
        "unavailable_reason": (
            None
            if kubectl_present
            else _kubectl_missing_message("kubectl not found; cannot check deployment health")
        ),
    }


def start(
    namespace: str = DEFAULT_NAMESPACE,
    weight_percent: int = 10,
    total_replicas: int = DEFAULT_TOTAL_REPLICAS,
) -> CanaryResult:
    """Start a canary rollout: scale up nyxgpt-api-canary to `weight_percent` of traffic."""
    state = _load_state()
    if state.get("active"):
        return CanaryResult(
            False, f"Canary rollout already in progress at {state.get('weight_percent', 0)}%"
        )
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot start canary rollout")
        )

    weight_percent = max(1, min(99, weight_percent))
    canary_replicas, stable_replicas = _split_replicas(total_replicas, weight_percent)

    canary_result = _scale(CANARY_DEPLOYMENT, canary_replicas, namespace)
    if not canary_result.ok:
        return canary_result
    stable_result = _scale(STABLE_DEPLOYMENT, stable_replicas, namespace)
    if not stable_result.ok:
        return stable_result

    state["active"] = True
    state["weight_percent"] = weight_percent
    state["total_replicas"] = total_replicas
    history = state.setdefault("history", [])
    history.append({"action": "start", "weight_percent": weight_percent, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state)

    return CanaryResult(
        True,
        f"Started canary rollout at {weight_percent}% ({canary_replicas}/{total_replicas} replicas)",
    )


def evaluate(
    namespace: str = DEFAULT_NAMESPACE,
    *,
    error_rate_threshold_percent: float = 5.0,
    latency_p95_threshold_ms: float = 2000.0,
    min_requests: int = 20,
) -> CanaryResult:
    """Compare live error-rate/latency metrics against thresholds.

    Automatically rolls back the canary if either threshold is breached.
    Returns ok=True (with an "insufficient data" note) when too few requests
    have been observed to judge the canary yet, so a quiet canary doesn't
    get auto-rolled-back for lack of traffic.
    """
    state = _load_state()
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")

    metrics = metrics_snapshot()
    if metrics["total_requests"] < min_requests:
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
        rollback_result = rollback(namespace)
        return CanaryResult(
            False,
            f"Metrics regression detected ({'; '.join(breaches)}); automatically rolled back",
            rollback_result.message,
        )

    return CanaryResult(
        True,
        f"Metrics within thresholds (error_rate={metrics['error_rate_percent']:.2f}%, "
        f"p95={metrics['p95_latency_ms']:.2f}ms); safe to promote",
    )


def promote(
    namespace: str = DEFAULT_NAMESPACE,
    step_percent: int = 25,
    total_replicas: int = DEFAULT_TOTAL_REPLICAS,
) -> CanaryResult:
    """Increase the canary's traffic share by `step_percent`, finalizing at 100%."""
    state = _load_state()
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")
    if _which("kubectl") is None:
        return CanaryResult(
            False, _kubectl_missing_message("kubectl not found; cannot promote canary rollout")
        )

    total = state.get("total_replicas", total_replicas)
    new_weight = min(100, state.get("weight_percent", 0) + max(1, step_percent))
    canary_replicas, stable_replicas = _split_replicas(total, new_weight)

    canary_result = _scale(CANARY_DEPLOYMENT, canary_replicas, namespace)
    if not canary_result.ok:
        return canary_result
    stable_result = _scale(STABLE_DEPLOYMENT, stable_replicas, namespace)
    if not stable_result.ok:
        return stable_result

    fully_promoted = new_weight >= 100
    state["weight_percent"] = new_weight
    state["active"] = not fully_promoted
    history = state.setdefault("history", [])
    history.append({"action": "promote", "weight_percent": new_weight, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state)

    if fully_promoted:
        return CanaryResult(
            True,
            "Canary fully promoted to 100% traffic; deploy the new image to "
            f"{STABLE_DEPLOYMENT} and rerun `nyxgpt canary start` for the next rollout",
        )
    return CanaryResult(
        True, f"Promoted canary to {new_weight}% ({canary_replicas}/{total} replicas)"
    )


def rollback(
    namespace: str = DEFAULT_NAMESPACE, total_replicas: int = DEFAULT_TOTAL_REPLICAS
) -> CanaryResult:
    """Cut all traffic back to nyxgpt-api-stable.

    Scales the canary Deployment to 0 first (removing it from the Service's
    endpoints, which stops it receiving traffic) before restoring stable --
    this is the emergency escape hatch and must not be blocked by a flaky
    stable-scale-up.
    """
    state = _load_state()
    if not state.get("active"):
        return CanaryResult(False, "No canary rollout in progress")
    if _which("kubectl") is None:
        return CanaryResult(
            False,
            _kubectl_missing_message("kubectl not found; cannot roll back canary rollout"),
        )

    total = state.get("total_replicas", total_replicas)
    previous_weight = state.get("weight_percent", 0)

    canary_result = _scale(CANARY_DEPLOYMENT, 0, namespace)
    if not canary_result.ok:
        return canary_result
    stable_result = _scale(STABLE_DEPLOYMENT, total, namespace)

    state["active"] = False
    state["weight_percent"] = 0
    history = state.setdefault("history", [])
    history.append(
        {"action": "rollback", "from_weight_percent": previous_weight, "ts": time.time()}
    )
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state)

    if not stable_result.ok:
        return CanaryResult(
            True,
            f"Canary traffic stopped (scaled to 0%), but restoring {STABLE_DEPLOYMENT} to "
            f"{total} replicas failed: {stable_result.message}",
        )
    return CanaryResult(True, f"Rolled back canary rollout from {previous_weight}% to 0%")


__all__ = [
    "CanaryResult",
    "SERVICE_NAME",
    "STABLE_DEPLOYMENT",
    "CANARY_DEPLOYMENT",
    "DEFAULT_NAMESPACE",
    "DEFAULT_TOTAL_REPLICAS",
    "deployment_health",
    "metrics_snapshot",
    "status",
    "start",
    "evaluate",
    "promote",
    "rollback",
]
