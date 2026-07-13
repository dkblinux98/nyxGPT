"""Local blue/green deployment for the nyxGPT API on a local Kubernetes cluster.

Manages traffic cutover between two `nyxgpt-api` Deployments (`nyxgpt-api-blue`
and `nyxgpt-api-green`) fronted by a single Service, by patching the Service's
selector. This targets the same local-cluster workflow documented in
docs/kubernetes.md (kind/minikube/k3s) -- there is no cloud load balancer
involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from nyxgpt.ops import OpsResult as DeployResult

SERVICE_NAME = "nyxgpt-api"
DEPLOYMENT_PREFIX = "nyxgpt-api"
COLORS = ("blue", "green")
DEFAULT_NAMESPACE = "nyxgpt"
HISTORY_LIMIT = 20


def _which(prog: str) -> str | None:
    return shutil.which(prog)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _other_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def _state_path() -> Path:
    return Path.home() / ".nyxGPT" / "deploy_state.json"


def _load_state() -> dict[str, Any]:
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
        return DeployResult(False, "kubectl not found; cannot check deployment health")

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
    active = get_active_color(namespace)
    colors: dict[str, Any] = {}
    for color in COLORS:
        health = deployment_health(color, namespace)
        colors[color] = {"healthy": health.ok, "message": health.message}

    state = _load_state()
    return {
        "namespace": namespace,
        "active": active,
        "inactive": _other_color(active),
        "colors": colors,
        "history": state.get("history", [])[-10:],
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
        return DeployResult(False, "kubectl not found; cannot switch deployment")

    active = get_active_color(namespace)
    target = target or _other_color(active)
    if target not in COLORS:
        return DeployResult(False, f"Unknown color: {target}")
    if target == active:
        return DeployResult(False, f"{target} is already active")

    if not force:
        health = deployment_health(target, namespace)
        if not health.ok:
            return DeployResult(False, f"Refusing to switch: {health.message}", health.details)

    patch = json.dumps({"spec": {"selector": {"app": DEPLOYMENT_PREFIX, "color": target}}})
    cp = _run(["kubectl", "patch", "service", SERVICE_NAME, "-n", namespace, "-p", patch])
    if cp.returncode != 0:
        return DeployResult(False, "kubectl patch failed", (cp.stderr or "").strip())

    state = _load_state()
    state["active"] = target
    history = state.setdefault("history", [])
    history.append({"from": active, "to": target, "ts": time.time()})
    state["history"] = history[-HISTORY_LIMIT:]
    _save_state(state)

    return DeployResult(True, f"Switched traffic from {active} to {target}")


def rollback(namespace: str = DEFAULT_NAMESPACE) -> DeployResult:
    """Switch traffic back to the color active before the last switch.

    Bypasses the health gate in switch() -- rollback is the emergency escape
    hatch and must not be blocked by a flaky readiness check.
    """
    state = _load_state()
    history = state.get("history", [])
    if not history:
        return DeployResult(False, "No deployment history to roll back to")

    previous = history[-1].get("from")
    if previous not in COLORS:
        return DeployResult(False, "No valid previous color recorded")

    return switch(target=previous, namespace=namespace, force=True)


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
