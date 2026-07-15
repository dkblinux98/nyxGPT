import json
from unittest.mock import MagicMock

import pytest

from nyxgpt import deploy


class CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _deployment_json(*, replicas=1, ready=1, updated=1):
    return json.dumps(
        {
            "spec": {"replicas": replicas},
            "status": {"readyReplicas": ready, "updatedReplicas": updated},
        }
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_state_path", lambda: tmp_path / "deploy_state.json")
    monkeypatch.setattr(deploy, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(deploy.time, "time", lambda: 1234.0)
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)


@pytest.mark.unit
def test_get_active_color_reads_service_selector(monkeypatch):
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(stdout="green\n"))
    assert deploy.get_active_color("nyxgpt") == "green"


@pytest.mark.unit
def test_get_active_color_falls_back_to_state_when_kubectl_missing(monkeypatch):
    monkeypatch.setattr(deploy, "_which", lambda _: None)
    deploy._save_state({"active": "green", "history": []})
    assert deploy.get_active_color("nyxgpt") == "green"


@pytest.mark.unit
def test_deployment_health_healthy(monkeypatch):
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(stdout=_deployment_json()))
    result = deploy.deployment_health("blue", "nyxgpt")
    assert result.ok
    assert "healthy" in result.message


@pytest.mark.unit
def test_deployment_health_not_ready(monkeypatch):
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(stdout=_deployment_json(ready=0, updated=0)))
    result = deploy.deployment_health("green", "nyxgpt")
    assert not result.ok
    assert "not healthy" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing(monkeypatch):
    monkeypatch.setattr(deploy, "_which", lambda _: None)
    result = deploy.deployment_health("blue", "nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(deploy, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = deploy.deployment_health("blue", "nyxgpt")
    assert not result.ok
    assert "kubectl not found" not in result.message
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
def test_switch_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(deploy, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = deploy.switch(namespace="nyxgpt")
    assert not result.ok
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
def test_deployment_health_unknown_color():
    result = deploy.deployment_health("red", "nyxgpt")
    assert not result.ok
    assert "Unknown color" in result.message


@pytest.mark.unit
def test_switch_refuses_when_target_unhealthy(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    monkeypatch.setattr(
        deploy,
        "deployment_health",
        lambda color, ns: deploy.DeployResult(False, f"{color} not ready"),
    )
    run_mock = MagicMock()
    monkeypatch.setattr(deploy, "_run", run_mock)

    result = deploy.switch(namespace="nyxgpt")

    assert not result.ok
    assert "Refusing to switch" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_switch_success_patches_service_and_records_history(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    monkeypatch.setattr(
        deploy, "deployment_health", lambda color, ns: deploy.DeployResult(True, "healthy")
    )
    patch_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(deploy, "_run", patch_mock)

    result = deploy.switch(namespace="nyxgpt")

    assert result.ok
    assert "Switched traffic from blue to green" in result.message
    patch_mock.assert_called_once()
    cmd = patch_mock.call_args[0][0]
    assert cmd[:3] == ["kubectl", "patch", "service"]

    state = deploy._load_state()
    assert state["active"] == "green"
    assert state["history"][-1] == {"from": "blue", "to": "green", "ts": 1234.0}


@pytest.mark.unit
def test_switch_rejects_same_color(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    result = deploy.switch(target="blue", namespace="nyxgpt")
    assert not result.ok
    assert "already active" in result.message


@pytest.mark.unit
def test_switch_kubectl_patch_failure(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    monkeypatch.setattr(
        deploy, "deployment_health", lambda color, ns: deploy.DeployResult(True, "healthy")
    )
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(returncode=1, stderr="boom"))

    result = deploy.switch(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl patch failed" in result.message


@pytest.mark.unit
def test_rollback_with_no_history():
    result = deploy.rollback("nyxgpt")
    assert not result.ok
    assert "No deployment history" in result.message


@pytest.mark.unit
def test_rollback_switches_back_and_bypasses_health_gate(monkeypatch):
    deploy._save_state(
        {"active": "green", "history": [{"from": "blue", "to": "green", "ts": 1000.0}]}
    )
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "green")

    health_mock = MagicMock(return_value=deploy.DeployResult(False, "unhealthy, but forced"))
    monkeypatch.setattr(deploy, "deployment_health", health_mock)
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(returncode=0))

    result = deploy.rollback("nyxgpt")

    assert result.ok
    assert "Switched traffic from green to blue" in result.message
    health_mock.assert_not_called()


@pytest.mark.unit
def test_status_reports_active_inactive_and_history(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")

    def fake_health(color, ns):
        return deploy.DeployResult(color == "blue", f"{color} status")

    monkeypatch.setattr(deploy, "deployment_health", fake_health)
    deploy._save_state(
        {"active": "blue", "history": [{"from": "green", "to": "blue", "ts": 1000.0}]}
    )

    data = deploy.status("nyxgpt")

    assert data["namespace"] == "nyxgpt"
    assert data["active"] == "blue"
    assert data["inactive"] == "green"
    assert data["colors"]["blue"]["healthy"] is True
    assert data["colors"]["green"]["healthy"] is False
    assert data["history"] == [{"from": "green", "to": "blue", "ts": 1000.0}]
    assert data["available"] is True
    assert data["unavailable_reason"] is None


@pytest.mark.unit
def test_status_reports_unavailable_when_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    monkeypatch.setattr(deploy, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")

    data = deploy.status("nyxgpt")

    assert data["available"] is False
    assert "Kubernetes deployment mode" in data["unavailable_reason"]


@pytest.mark.unit
def test_history_is_capped(monkeypatch):
    monkeypatch.setattr(deploy, "get_active_color", lambda ns: "blue")
    monkeypatch.setattr(
        deploy, "deployment_health", lambda color, ns: deploy.DeployResult(True, "ok")
    )
    monkeypatch.setattr(deploy, "_run", lambda cmd: CP(returncode=0))

    state = {"active": "blue", "history": [{"from": "a", "to": "b", "ts": i} for i in range(25)]}
    deploy._save_state(state)

    deploy.switch(target="green", namespace="nyxgpt")

    saved = deploy._load_state()
    assert len(saved["history"]) == deploy.HISTORY_LIMIT
