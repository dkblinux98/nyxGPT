"""Local blue/green deployment for the nyxGPT API on a local Kubernetes cluster.

Manages traffic cutover between two `nyxgpt-api` Deployments (`nyxgpt-api-blue`
and `nyxgpt-api-green`) fronted by a single Service, by patching the Service's
selector. This targets the same local-cluster workflow documented in
docs/kubernetes.md (kind/minikube/k3s) -- there is no cloud load balancer
involved.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from nyxgpt import metrics as prom_metrics
from nyxgpt.ops import OpsResult as DeployResult

logger = logging.getLogger(__name__)

SERVICE_NAME = "nyxgpt-api"
DEPLOYMENT_PREFIX = "nyxgpt-api"
COLORS = ("blue", "green")
DEFAULT_NAMESPACE = "nyxgpt"
HISTORY_LIMIT = 20

NOT_SUPPORTED_UNDER_COMPOSE = (
    "Blue/green deployment requires the Kubernetes deployment mode; not "
    "available under docker-compose. See docs/kubernetes.md."
)


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


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text without raising on non-zero exit."""
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _other_color(color: str) -> str:
    """Return the color not currently passed in ("blue" <-> "green")."""
    return "green" if color == "blue" else "blue"


def _state_path() -> Path:
    """Return the path to the local blue/green deployment state file."""
    return Path.home() / ".nyxGPT" / "deploy_state.json"


def _load_state() -> dict[str, Any]:
    """Load blue/green deployment state from disk, defaulting to blue-active with no history.

    Tolerates a missing or corrupt state file by falling back to defaults.
    """
    path = _state_path()
    if not path.exists():
        return {"active": "blue", "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("active", "blue")
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"active": "blue", "history": []}


def _save_state(state: dict[str, Any]) -> None:
    """Persist blue/green deployment state to disk as JSON, creating parent dirs as needed."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_active_color(namespace: str = DEFAULT_NAMESPACE) -> str:
    """Return the color currently receiving traffic.

    Reads the live Service selector when kubectl/the cluster is reachable,
    otherwise falls back to the last color recorded locally.
    """
    if _which("kubectl") is not None:
        cp = _run(
            [
                "kubectl",
                "get",
                "service",
                SERVICE_NAME,
                "-n",
                namespace,
                "-o",
                "jsonpath={.spec.selector.color}",
            ]
        )
        color = (cp.stdout or "").strip()
        if cp.returncode == 0 and color in COLORS:
            return color
    active = _load_state().get("active", "blue")
    return str(active) if active in COLORS else "blue"


def deployment_health(color: str, namespace: str = DEFAULT_NAMESPACE) -> DeployResult:
    """Check whether the given color's Deployment is fully ready.

    This mirrors the readinessProbe (`GET /health`) already configured on the
    Deployment (see k8s/deployment-blue.yaml / deployment-green.yaml): a
    Deployment only reports its Pods as Ready once the probe passes.
    """
    if color not in COLORS:
        return DeployResult(False, f"Unknown color: {color}")

    if _which("kubectl") is None:
        return DeployResult(
            False, _kubectl_missing_message("kubectl not found; cannot check deployment health")
        )

    name = f"{DEPLOYMENT_PREFIX}-{color}"
    cp = _run(["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"])
    if cp.returncode != 0:
        return DeployResult(False, f"Could not read deployment {name}", (cp.stderr or "").strip())

    try:
        data = json.loads(cp.stdout)
    except Exception as e:
        return DeployResult(False, f"Could not parse status for {name}", str(e))

    spec_replicas = data.get("spec", {}).get("replicas", 0) or 0
    status_block = data.get("status", {})
    ready = status_block.get("readyReplicas", 0) or 0
    updated = status_block.get("updatedReplicas", 0) or 0

    if spec_replicas == 0:
        return DeployResult(False, f"{name} has 0 desired replicas")
    if ready >= spec_replicas and updated >= spec_replicas:
        return DeployResult(True, f"{name} healthy ({ready}/{spec_replicas} ready)")
    return DeployResult(False, f"{name} not healthy ({ready}/{spec_replicas} ready)")


def status(namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """Return a snapshot of blue/green deployment state for `namespace`.

    Includes the active/inactive colors, per-color health, the last 10
    switch history entries, and whether kubectl is available (with a
    reason string when it isn't -- e.g. unsupported under docker-compose).
    """
    active = get_active_color(namespace)
    colors: dict[str, Any] = {}
    for color in COLORS:
        health = deployment_health(color, namespace)
        colors[color] = {"healthy": health.ok, "message": health.message}
        prom_metrics.DEPLOY_ACTIVE_COLOR.labels(color=color).set(1 if color == active else 0)

    state = _load_state()
    kubectl_present = _which("kubectl") is not None
    return {
        "namespace": namespace,
        "active": active,
        "inactive": _other_color(active),
        "colors": colors,
        "history": state.get("history", [])[-10:],
        "available": kubectl_present,
        "unavailable_reason": (
            None
            if kubectl_present
            else _kubectl_missing_message("kubectl not found; cannot check deployment health")
        ),
    }


def switch(
    target: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    *,
    force: bool = False,
) -> DeployResult:
    """Cut traffic over to `target` (defaults to the currently inactive color).

    Refuses to switch unless the target Deployment is healthy, unless
    `force=True` (used by rollback()).
    """
    if _which("kubectl") is None:
        message = _kubectl_missing_message("kubectl not found; cannot switch deployment")
        logger.warning("deploy: switch failed, %s", message, extra={"component": "deploy"})
        return DeployResult(False, message)

    active = get_active_color(namespace)
    target = target or _other_color(active)
    if target not in COLORS:
        logger.warning(
            "deploy: switch failed, unknown color %s",
            target,
            extra={"component": "deploy", "target": target},
        )
        return DeployResult(False, f"Unknown color: {target}")
    if target == active:
        logger.info(
            "deploy: switch no-op, %s is already active",
            target,
            extra={"component": "deploy", "active": active, "target": target},
        )
        return DeployResult(False, f"{target} is already active")

    if not force:
        health = deployment_health(target, namespace)
        if not health.ok:
            logger.warning(
                "deploy: refusing switch from %s to %s, target unhealthy: %s",
                active,
                target,
                health.message,
                extra={
                    "component": "deploy",
                    "from_color": active,
                    "to_color": target,
                    "healthy": False,
                    "decision": "refused",
                },
            )
            return DeployResult(False, f"Refusing to switch: {health.message}", health.details)

    logger.info(
        "deploy: switching traffic from %s to %s (force=%s)",
        active,
        target,
        force,
        extra={
            "component": "deploy",
            "from_color": active,
            "to_color": target,
            "force": force,
            "decision": "attempting",
        },
    )

    patch = json.dumps({"spec": {"selector": {"app": DEPLOYMENT_PREFIX, "color": target}}})
    cp = _run(["kubectl", "patch", "service", SERVICE_NAME, "-n", namespace, "-p", patch])
    if cp.returncode != 0:
        prom_metrics.DEPLOY_SWITCHES_TOTAL.labels(
            from_color=active, to_color=target, result="failed"
        ).inc()
        logger.error(
            "deploy: kubectl patch failed switching %s -> %s: %s",
            active,
            target,
            (cp.stderr or "").strip(),
            extra={
                "component": "deploy",
                "from_color": active,
                "to_color": target,
                "outcome": "failed",
            },
        )
        return DeployResult(False, "kubectl patch failed", (cp.stderr or "").strip())

    state = _load_state()
    state["active"] = target
    history = state.setdefault("history", [])
    history.append({"from": active, "to": target, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state)

    prom_metrics.DEPLOY_SWITCHES_TOTAL.labels(from_color=active, to_color=target, result="ok").inc()
    prom_metrics.DEPLOY_ACTIVE_COLOR.labels(color=target).set(1)
    prom_metrics.DEPLOY_ACTIVE_COLOR.labels(color=active).set(0)
    logger.info(
        "deploy: switched traffic from %s to %s",
        active,
        target,
        extra={
            "component": "deploy",
            "from_color": active,
            "to_color": target,
            "outcome": "ok",
        },
    )

    return DeployResult(True, f"Switched traffic from {active} to {target}")


def rollback(namespace: str = DEFAULT_NAMESPACE) -> DeployResult:
    """Switch traffic back to the color active before the last switch.

    Bypasses the health gate in switch() -- rollback is the emergency escape
    hatch and must not be blocked by a flaky readiness check.
    """
    logger.info("deploy: rollback requested", extra={"component": "deploy", "action": "rollback"})

    state = _load_state()
    history = state.get("history", [])
    if not history:
        logger.warning(
            "deploy: rollback failed, no deployment history to roll back to",
            extra={"component": "deploy", "action": "rollback"},
        )
        prom_metrics.DEPLOY_ROLLBACKS_TOTAL.labels(result="failed").inc()
        return DeployResult(False, "No deployment history to roll back to")

    previous = history[-1].get("from")
    if previous not in COLORS:
        logger.warning(
            "deploy: rollback failed, no valid previous color recorded",
            extra={"component": "deploy", "action": "rollback"},
        )
        prom_metrics.DEPLOY_ROLLBACKS_TOTAL.labels(result="failed").inc()
        return DeployResult(False, "No valid previous color recorded")

    result = switch(target=previous, namespace=namespace, force=True)
    prom_metrics.DEPLOY_ROLLBACKS_TOTAL.labels(result="ok" if result.ok else "failed").inc()
    log = logger.info if result.ok else logger.error
    log(
        "deploy: rollback to %s %s: %s",
        previous,
        "succeeded" if result.ok else "failed",
        result.message,
        extra={
            "component": "deploy",
            "action": "rollback",
            "target": previous,
            "outcome": "ok" if result.ok else "failed",
        },
    )
    return result


__all__ = [
    "DeployResult",
    "COLORS",
    "DEFAULT_NAMESPACE",
    "get_active_color",
    "deployment_health",
    "status",
    "switch",
    "rollback",
]
