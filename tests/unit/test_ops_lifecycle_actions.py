"""Unit tests for #3390: `nyxgpt ops`/admin-dashboard lifecycle actions recorded as
observable events.

Covers `nyxgpt_ops_actions_total` emission (and its `_ops_action_outcome`/
`_record_ops_action` helpers) for every wrapped command -- install, down, restart,
stop, observability, and the Terraform/Kubernetes paths -- plus the admin
dashboard's manual "Heal Now" button. Follows the mocked-subprocess/patch.object
patterns already used throughout tests/unit/test_ops.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nyxgpt import metrics as prom_metrics
from nyxgpt import ops

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _force_macos_native_path(monkeypatch):
    """Pin `platform.system()` to "Darwin" -- see test_ops.py's fixture of the
    same name. This file's install/restart/stop call sites assume the
    macOS-only (Homebrew/launchd) native path; the Linux/systemd path
    (#3508) has its own tests in test_ops_systemd.py.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")


class CP:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ops_actions_total(command: str, service: str, result: str) -> float:
    """Current `nyxgpt_ops_actions_total` sample for one (command, service, result)."""
    value = prom_metrics.REGISTRY.get_sample_value(
        "nyxgpt_ops_actions_total",
        {"command": command, "service": service, "result": result},
    )
    return value or 0


# --- _ops_action_outcome ---


def test_ops_action_outcome_empty_is_success():
    assert ops._ops_action_outcome([]) == ("success", "")


def test_ops_action_outcome_all_ok_is_success():
    results = [ops.OpsResult(True, "did a thing"), ops.OpsResult(True, "did another")]
    result, message = ops._ops_action_outcome(results)
    assert result == "success"
    assert "did a thing" in message


def test_ops_action_outcome_any_failure_is_failure():
    results = [ops.OpsResult(True, "ok"), ops.OpsResult(False, "bad thing", "details")]
    result, message = ops._ops_action_outcome(results)
    assert result == "failure"
    assert "bad thing" in message


def test_ops_action_outcome_refusal_message_is_refused():
    results = [ops.OpsResult(False, "Refusing to restart local api: a Compose deployment...")]
    result, message = ops._ops_action_outcome(results)
    assert result == "refused"
    assert "Refusing" in message


def test_ops_action_outcome_refusal_wins_over_plain_failure():
    results = [
        ops.OpsResult(False, "some unrelated failure"),
        ops.OpsResult(False, "Refusing to start: port collision"),
    ]
    result, _ = ops._ops_action_outcome(results)
    assert result == "refused"


# --- _record_ops_action / record_manual_restart ---


def test_record_ops_action_increments_counter_and_logs(caplog):
    before = _ops_actions_total("restart", "api", "success")
    with caplog.at_level("INFO", logger="nyxgpt.ops"):
        ops._record_ops_action("restart", "api", "success", "Restarted api")
    after = _ops_actions_total("restart", "api", "success")
    assert after == before + 1
    assert any("lifecycle action" in r.message for r in caplog.records)
    record = next(r for r in caplog.records if "lifecycle action" in r.message)
    assert record.command == "restart"
    assert record.service == "api"
    assert record.result == "success"


def test_record_ops_action_failure_logs_at_warning(caplog):
    before = _ops_actions_total("down", "cassandra", "failure")
    with caplog.at_level("WARNING", logger="nyxgpt.ops"):
        ops._record_ops_action("down", "cassandra", "failure", "boom")
    after = _ops_actions_total("down", "cassandra", "failure")
    assert after == before + 1
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_record_manual_restart_records_success_as_restart_command():
    before = _ops_actions_total("restart", "web", "success")
    ops.record_manual_restart("web", True, "Restarted web")
    after = _ops_actions_total("restart", "web", "success")
    assert after == before + 1


def test_record_manual_restart_records_failure():
    before = _ops_actions_total("restart", "ollama", "failure")
    ops.record_manual_restart("ollama", False, "still down")
    after = _ops_actions_total("restart", "ollama", "failure")
    assert after == before + 1


# --- correlation id (#3390 <-> subprocess env, #3430) ---


def test_mint_correlation_id_returns_distinct_hex_ids():
    from nyxgpt.logging import mint_correlation_id

    a = mint_correlation_id()
    b = mint_correlation_id()
    assert a != b
    int(a, 16)  # must be valid hex
    assert len(a) == 32


def test_record_ops_action_includes_correlation_id_from_env(caplog, monkeypatch):
    monkeypatch.setenv("NYXGPT_CORRELATION_ID", "env-corr-id")
    with caplog.at_level("INFO", logger="nyxgpt.ops"):
        ops._record_ops_action("restart", "api", "success", "Restarted api")

    record = next(r for r in caplog.records if "lifecycle action" in r.message)
    assert record.correlation_id == "env-corr-id"
    assert "correlation_id=env-corr-id" in record.message


def test_record_ops_action_prefers_request_id_over_env_correlation_id(caplog, monkeypatch):
    from nyxgpt.logging import request_id_var

    monkeypatch.setenv("NYXGPT_CORRELATION_ID", "env-corr-id")
    token = request_id_var.set("dashboard-request-id")
    try:
        with caplog.at_level("INFO", logger="nyxgpt.ops"):
            ops._record_ops_action("restart", "web", "success", "Restarted web")
    finally:
        request_id_var.reset(token)

    record = next(r for r in caplog.records if "lifecycle action" in r.message)
    assert record.correlation_id == "dashboard-request-id"


def test_record_ops_action_defaults_correlation_id_when_unset(caplog, monkeypatch):
    from nyxgpt.logging import request_id_var

    monkeypatch.delenv("NYXGPT_CORRELATION_ID", raising=False)
    token = request_id_var.set(None)
    try:
        with caplog.at_level("INFO", logger="nyxgpt.ops"):
            ops._record_ops_action("restart", "api", "success", "Restarted api")
    finally:
        request_id_var.reset(token)

    record = next(r for r in caplog.records if "lifecycle action" in r.message)
    assert record.correlation_id == "-"


# --- install() ---


def test_install_records_success_action():
    ok = [ops.OpsResult(True, "ok")]
    before = _ops_actions_total("install", "all", "success")
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok),
        patch.object(ops, "_ensure_web_deps", return_value=ok),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok),
        patch.object(ops, "_install_homebrew_api", return_value=ok),
        patch.object(ops, "_install_homebrew_web", return_value=ok),
        patch.object(ops, "_ensure_ollama_service", return_value=ok),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok),
        patch.object(ops, "sync_env_from_config", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=ok),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )
    assert rc == 0
    after = _ops_actions_total("install", "all", "success")
    assert after == before + 1


def test_install_records_failure_action():
    ok = [ops.OpsResult(True, "ok")]
    bad = [ops.OpsResult(False, "bad", "details")]
    before = _ops_actions_total("install", "all", "failure")
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=bad),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok),
        patch.object(ops, "_ensure_web_deps", return_value=ok),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok),
        patch.object(ops, "_install_homebrew_api", return_value=ok),
        patch.object(ops, "_install_homebrew_web", return_value=ok),
        patch.object(ops, "_ensure_ollama_service", return_value=ok),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok),
        patch.object(ops, "sync_env_from_config", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=ok),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )
    assert rc == 2
    after = _ops_actions_total("install", "all", "failure")
    assert after == before + 1


# --- restart() ---


def test_restart_records_success_action_per_target():
    ok = [ops.OpsResult(True, "ok")]
    before = _ops_actions_total("restart", "api", "success")
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", return_value=ok),
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
    assert rc == 0
    after = _ops_actions_total("restart", "api", "success")
    assert after == before + 1


def test_restart_records_refused_action_on_compose_conflict():
    before = _ops_actions_total("restart", "api", "refused")
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"api": "running"}),
        patch.object(ops, "_restart_brew_service") as rb,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
    assert rc == 2
    rb.assert_not_called()
    after = _ops_actions_total("restart", "api", "refused")
    assert after == before + 1


def test_restart_records_failure_action_for_target_all():
    ok = [ops.OpsResult(True, "ok")]
    bad = [ops.OpsResult(False, "bad", "details")]
    before = _ops_actions_total("restart", "all", "failure")
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", side_effect=[ok, bad, ok]),
        patch.object(ops, "_restart_docker_container", return_value=ok),
        patch.object(ops, "_restart_launchagent", return_value=ok),
        patch.object(ops, "_restart_observability_stack", return_value=ok),
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.restart(args)
    assert rc == 2
    after = _ops_actions_total("restart", "all", "failure")
    assert after == before + 1


# --- stop() ---


def test_stop_records_success_action():
    before = _ops_actions_total("stop", "ollama", "success")
    with patch.object(
        ops,
        "detect_deployment_mode",
        return_value=ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    ):
        args = MagicMock()
        args.target = "ollama"
        rc = ops.stop(args)
    assert rc == 0
    after = _ops_actions_total("stop", "ollama", "success")
    assert after == before + 1


# --- down() (native path) ---


def test_down_records_success_action_for_scope():
    ok = [ops.OpsResult(True, "ok")]
    before = _ops_actions_total("down", "app", "success")
    with (
        patch.object(ops, "_stop_brew_service", return_value=ok),
        patch.object(ops, "_stop_docker_container", return_value=ok),
        patch.object(ops, "_stop_launchagent", return_value=ok),
        patch.object(ops, "_compose_available", return_value=False),
    ):
        args = SimpleNamespace(
            terraform=False,
            kubernetes=False,
            app_only=True,
            observability_only=False,
            volumes=False,
            yes_really=False,
        )
        rc = ops.down(args)
    assert rc == 0
    after = _ops_actions_total("down", "app", "success")
    assert after == before + 1


# --- Terraform (shared install_terraform_local / _install_terraform / _down_terraform_steps) ---


def test_install_terraform_steps_records_success():
    ok = [ops.OpsResult(True, "ok")]
    before = _ops_actions_total("install", "terraform", "success")
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok),
        patch.object(ops, "_generate_compose_config", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_start_observability_stack_terraform", return_value=ok),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
        patch.object(ops, "_terraform_stack_health", return_value=ok),
    ):
        results = ops.install_terraform_local(api_key="k")
    assert all(r.ok for r in results)
    after = _ops_actions_total("install", "terraform", "success")
    assert after == before + 1


def test_install_terraform_steps_records_refused_on_port_collision():
    collision = ops.OpsResult(False, "Refusing to start: port collision")
    before = _ops_actions_total("install", "terraform", "refused")
    with patch.object(ops, "_refuse_port_collision", return_value=collision):
        results = ops.install_terraform_local()
    assert results == [collision]
    after = _ops_actions_total("install", "terraform", "refused")
    assert after == before + 1


def test_down_terraform_steps_records_success():
    before = _ops_actions_total("down", "terraform", "success")
    with (
        patch.object(ops, "_which", lambda prog: "/usr/local/bin/terraform"),
        patch.object(
            ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="destroyed")
        ),
    ):
        results = ops.down_terraform()
    assert all(r.ok for r in results)
    after = _ops_actions_total("down", "terraform", "success")
    assert after == before + 1


def test_down_terraform_steps_records_failure():
    before = _ops_actions_total("down", "terraform", "failure")
    with patch.object(ops, "_which", lambda prog: None):
        results = ops.down_terraform()
    assert not any(r.ok for r in results)
    after = _ops_actions_total("down", "terraform", "failure")
    assert after == before + 1


# --- Kubernetes (shared install_kubernetes_local / down_kubernetes) ---


def test_install_kubernetes_steps_records_success():
    ok = [ops.OpsResult(True, "ok")]
    before = _ops_actions_total("install", "kubernetes", "success")
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        # See the note in tests/unit/test_ops.py: both of these shell out, so
        # leaving them real would make this test depend on the machine's
        # docker/cluster state rather than on the code under test.
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok),
        # Observability is applied in this mode too (#3787), and waited for
        # before the health snapshot reads Pod phases (#3826).
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
    ):
        results = ops.install_kubernetes_local(api_key="k")
    assert all(r.ok for r in results)
    after = _ops_actions_total("install", "kubernetes", "success")
    assert after == before + 1


def test_install_kubernetes_steps_records_refused_on_port_collision():
    collision = ops.OpsResult(False, "Refusing to start: port collision")
    before = _ops_actions_total("install", "kubernetes", "refused")
    with patch.object(ops, "_refuse_port_collision", return_value=collision):
        results = ops.install_kubernetes_local()
    assert results == [collision]
    after = _ops_actions_total("install", "kubernetes", "refused")
    assert after == before + 1


def test_down_kubernetes_steps_records_success():
    before = _ops_actions_total("down", "kubernetes", "success")
    with (
        patch.object(ops, "_which", lambda prog: "/usr/local/bin/kubectl"),
        patch.object(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="deleted")),
    ):
        results = ops.down_kubernetes()
    assert all(r.ok for r in results)
    after = _ops_actions_total("down", "kubernetes", "success")
    assert after == before + 1


# --- observability() CLI / reconcile_observability() (dashboard) ---


def test_observability_cli_records_success_action():
    before = _ops_actions_total("observability", "observability", "success")
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "synced")]),
        patch.object(
            ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "up")]
        ),
    ):
        rc = ops.observability(MagicMock(kubernetes=False))
    assert rc == 0
    after = _ops_actions_total("observability", "observability", "success")
    assert after == before + 1


def test_reconcile_observability_enable_records_observability_command():
    before = _ops_actions_total("observability", "observability", "success")
    with patch.object(
        ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "up")]
    ):
        results = ops.reconcile_observability(True)
    assert all(r.ok for r in results)
    after = _ops_actions_total("observability", "observability", "success")
    assert after == before + 1


def test_reconcile_observability_disable_records_stop_command():
    before = _ops_actions_total("stop", "observability", "success")
    with patch.object(ops, "_stop_observability_stack", return_value=[ops.OpsResult(True, "down")]):
        results = ops.reconcile_observability(False)
    assert all(r.ok for r in results)
    after = _ops_actions_total("stop", "observability", "success")
    assert after == before + 1
