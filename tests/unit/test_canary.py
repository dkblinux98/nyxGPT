import json
from unittest.mock import MagicMock

import pytest

from nyxgpt import canary
from nyxgpt import metrics as prom_metrics


class CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _deployment_json(*, replicas=1, ready=1):
    return json.dumps({"spec": {"replicas": replicas}, "status": {"readyReplicas": ready}})


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "_state_path", lambda: tmp_path / "canary_state.json")
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary.time, "time", lambda: 1234.0)
    monkeypatch.setattr(canary, "get_resource_monitor", lambda: None)
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)


@pytest.mark.unit
def test_deployment_health_healthy(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(stdout=_deployment_json()))
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.ok
    assert "healthy" in result.message


@pytest.mark.unit
def test_deployment_health_not_ready(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(stdout=_deployment_json(ready=0)))
    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")
    assert not result.ok
    assert "not healthy" in result.message


@pytest.mark.unit
def test_deployment_health_zero_replicas(monkeypatch):
    monkeypatch.setattr(
        canary, "_run", lambda cmd: CP(stdout=_deployment_json(replicas=0, ready=0))
    )
    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")
    assert not result.ok
    assert "0 desired replicas" in result.message


@pytest.mark.unit
def test_deployment_health_unparseable_status(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(stdout="not json"))
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert not result.ok
    assert "Could not parse status" in result.message


@pytest.mark.unit
def test_load_state_handles_corrupted_json(tmp_path):
    """A corrupted state file must not raise; _load_state() should fall back
    to the default state instead of propagating the JSON parse error."""
    (tmp_path / "canary_state.json").write_text("not valid json{", encoding="utf-8")

    state = canary._load_state()

    assert state == {"active": False, "weight_percent": 0, "history": []}


@pytest.mark.unit
def test_scale_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary._scale("nyxgpt-api-canary", 2, "nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert not result.ok
    assert "kubectl not found" not in result.message
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
@pytest.mark.parametrize(
    "total,weight,expected",
    [
        (4, 0, (0, 4)),
        (4, 10, (1, 3)),
        (4, 50, (2, 2)),
        (4, 100, (4, 0)),
        (1, 50, (1, 0)),
    ],
)
def test_split_replicas(total, weight, expected):
    assert canary._split_replicas(total, weight) == expected


@pytest.mark.unit
def test_start_scales_canary_and_stable(monkeypatch):
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert result.ok
    assert "10%" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"]
    assert "--replicas=1" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=3" in calls[1]

    state = canary._load_state()
    assert state["active"] is True
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_start_refuses_when_already_active(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "history": []})
    run_mock = MagicMock()
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.start(namespace="nyxgpt")

    assert not result.ok
    assert "already in progress" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_start_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary.start(namespace="nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_start_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = canary.start(namespace="nyxgpt")
    assert not result.ok
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
def test_start_returns_error_when_canary_scale_fails(monkeypatch):
    def fake_run(cmd):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="canary scale boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # Rollout must not be recorded as started since the scale failed.
    state = canary._load_state()
    assert state.get("active") is not True


@pytest.mark.unit
def test_start_returns_error_when_stable_scale_fails(monkeypatch):
    def fake_run(cmd):
        if "nyxgpt-api-stable" in cmd:
            return CP(returncode=1, stderr="stable scale boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-stable" in result.message
    state = canary._load_state()
    assert state.get("active") is not True


@pytest.mark.unit
def test_status_reports_active_state_and_health(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "history": []})
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.CanaryResult(True, f"{name} healthy"),
    )

    data = canary.status("nyxgpt")

    assert data["namespace"] == "nyxgpt"
    assert data["active"] is True
    assert data["weight_percent"] == 25
    assert data["stable"]["healthy"] is True
    assert data["canary"]["healthy"] is True
    assert data["metrics"] == {
        "total_requests": 0,
        "error_rate_percent": 0.0,
        "p95_latency_ms": 0.0,
    }
    assert data["available"] is True
    assert data["unavailable_reason"] is None


@pytest.mark.unit
def test_status_reports_unavailable_when_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")

    data = canary.status("nyxgpt")

    assert data["available"] is False
    assert "Kubernetes deployment mode" in data["unavailable_reason"]


@pytest.mark.unit
def test_evaluate_no_active_rollout():
    result = canary.evaluate("nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_evaluate_insufficient_data(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 5, "error_rate_percent": 0.0, "p95_latency_ms": 100.0},
    )

    result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "Insufficient data" in result.message


@pytest.mark.unit
def test_evaluate_passes_within_thresholds(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 50, "error_rate_percent": 1.0, "p95_latency_ms": 500.0},
    )

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert result.ok
    assert "safe to promote" in result.message


@pytest.mark.unit
def test_evaluate_triggers_automatic_rollback_on_error_rate_breach(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 50, "error_rate_percent": 12.0, "p95_latency_ms": 200.0},
    )
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert not result.ok
    assert "automatically rolled back" in result.message
    state = canary._load_state()
    assert state["active"] is False
    assert state["weight_percent"] == 0


@pytest.mark.unit
def test_evaluate_triggers_automatic_rollback_on_latency_breach(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 50, "error_rate_percent": 0.0, "p95_latency_ms": 5000.0},
    )
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert not result.ok
    assert "p95 latency" in result.message


@pytest.mark.unit
def test_promote_increases_weight(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "35%" in result.message
    state = canary._load_state()
    assert state["weight_percent"] == 35
    assert state["active"] is True


@pytest.mark.unit
def test_promote_finalizes_at_100_percent(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "fully promoted" in result.message
    state = canary._load_state()
    assert state["weight_percent"] == 100
    assert state["active"] is False


@pytest.mark.unit
def test_promote_no_active_rollout():
    result = canary.promote(namespace="nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_promote_kubectl_missing(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_which", lambda _: None)

    result = canary.promote(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_promote_returns_error_when_canary_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})

    def fake_run(cmd):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # Weight must be unchanged since the scale failed before state was saved.
    state = canary._load_state()
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_promote_returns_error_when_stable_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})

    def fake_run(cmd):
        if "nyxgpt-api-stable" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-stable" in result.message
    state = canary._load_state()
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_rollback_scales_canary_to_zero_and_restores_stable(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "Rolled back canary rollout from 50% to 0%" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"]
    assert "--replicas=0" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=4" in calls[1]

    state = canary._load_state()
    assert state["active"] is False
    assert state["weight_percent"] == 0


@pytest.mark.unit
def test_rollback_no_active_rollout():
    result = canary.rollback(namespace="nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_rollback_kubectl_missing(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_which", lambda _: None)

    result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl not found" in result.message
    # Must not have been marked as rolled back since we bailed before scaling.
    state = canary._load_state()
    assert state["active"] is True


@pytest.mark.unit
def test_rollback_returns_error_when_canary_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=1, stderr="boom"))

    result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # State must be unchanged since we returned before saving.
    state = canary._load_state()
    assert state["active"] is True
    assert state["weight_percent"] == 50


@pytest.mark.unit
def test_rollback_reports_partial_failure_but_still_cuts_canary_traffic(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})

    def fake_run(cmd):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=0)
        return CP(returncode=1, stderr="boom")

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "Canary traffic stopped" in result.message
    state = canary._load_state()
    assert state["active"] is False


@pytest.mark.unit
def test_metrics_snapshot_returns_zeros_when_monitor_unset(monkeypatch):
    monkeypatch.setattr(canary, "get_resource_monitor", lambda: None)
    assert canary.metrics_snapshot() == {
        "total_requests": 0,
        "error_rate_percent": 0.0,
        "p95_latency_ms": 0.0,
    }


def _metric_value(name, **labels):
    return prom_metrics.REGISTRY.get_sample_value(name, labels or None)


@pytest.mark.unit
def test_start_logs_and_records_metrics(monkeypatch, caplog):
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert result.ok
    assert "canary: starting rollout at 10%" in caplog.text
    assert "canary: started rollout at 10%" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="start", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_rollout_active") == 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 10


@pytest.mark.unit
def test_start_logs_and_records_metric_on_scale_failure(monkeypatch, caplog):
    def fake_run(cmd):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    with caplog.at_level("ERROR"):
        result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "canary: start failed scaling canary" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="start", result="failed") >= 1


@pytest.mark.unit
def test_evaluate_pass_logs_and_records_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 50, "error_rate_percent": 1.0, "p95_latency_ms": 500.0},
    )

    with caplog.at_level("INFO"):
        result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "canary: evaluate passed" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="pass") >= 1


@pytest.mark.unit
def test_evaluate_insufficient_data_logs_and_records_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 5, "error_rate_percent": 0.0, "p95_latency_ms": 100.0},
    )

    with caplog.at_level("INFO"):
        result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "canary: evaluate holding, insufficient data" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="insufficient_data") >= 1


@pytest.mark.unit
def test_evaluate_regression_logs_and_triggers_auto_rollback(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(
        canary,
        "metrics_snapshot",
        lambda: {"total_requests": 50, "error_rate_percent": 12.0, "p95_latency_ms": 200.0},
    )
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.evaluate(
            "nyxgpt",
            error_rate_threshold_percent=5.0,
            latency_p95_threshold_ms=2000.0,
            min_requests=20,
        )

    assert not result.ok
    assert "canary: evaluate detected regression" in caplog.text
    assert "trigger=auto" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="regression") >= 1
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="ok") >= 1


@pytest.mark.unit
def test_promote_logs_and_records_metrics(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "canary: promoting rollout from 10% to 35%" in caplog.text
    assert "canary: promoted rollout to 35%" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="promote", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 35


@pytest.mark.unit
def test_promote_fully_promoted_logs_and_clears_active_gauge(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "canary: rollout fully promoted to 100%" in caplog.text
    assert _metric_value("nyxgpt_canary_rollout_active") == 0


@pytest.mark.unit
def test_rollback_logs_and_records_metric_manual_trigger(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "canary: rolling back from 50% (trigger=manual)" in caplog.text
    assert "canary: rolled back from 50% to 0% (trigger=manual)" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_rollout_active") == 0
    assert _metric_value("nyxgpt_canary_weight_percent") == 0


@pytest.mark.unit
def test_rollback_logs_and_records_metric_on_canary_scale_failure(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd: CP(returncode=1, stderr="boom"))

    with caplog.at_level("ERROR"):
        result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "canary: rollback failed scaling canary to 0" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="failed") >= 1


@pytest.mark.unit
def test_rollback_logs_partial_failure_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})

    def fake_run(cmd):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=0)
        return CP(returncode=1, stderr="boom")

    monkeypatch.setattr(canary, "_run", fake_run)

    with caplog.at_level("WARNING"):
        result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "canary: rollback partially failed" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="partial") >= 1


@pytest.mark.unit
def test_status_updates_rollout_gauges(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 42, "history": []})
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.CanaryResult(True, f"{name} healthy"),
    )

    canary.status("nyxgpt")

    assert _metric_value("nyxgpt_canary_rollout_active") == 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 42
