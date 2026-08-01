"""`nyxgpt ops alert-test` against a real, running Grafana (#3545).

Unit tests in tests/unit/test_ops.py mock the HTTP layer entirely -- that's
exactly how #3466's original implementation (POSTing straight into Grafana's
embedded Alertmanager ingestion API) shipped looking correct while 400ing
against every real Grafana instance, since no test ever exercised the actual
API. This module fills that gap by running the real request against a real
Grafana, started the same way `nyxgpt ops install`/`nyxgpt ops observability`
would (see docs/ops.md). Skips gracefully -- via `require_grafana` -- when no
such instance is reachable, matching the Ollama/Cassandra convention in
conftest.py.
"""

from __future__ import annotations

import pytest

from nyxgpt import ops
from nyxgpt.config import get_monitoring_slack_webhook_url

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("require_grafana")
def test_alert_test_reaches_nyxgpt_slack_receiver_on_live_grafana(cfg):
    """`_send_grafana_test_alert` must exercise Grafana's real receiver-test
    API end to end: authenticate, resolve the `nyxgpt-slack` contact point by
    its computed k8s resource name, and get back a 200 with a `status` field
    -- never the 400 `alert-test` originally always produced against a real
    Grafana (#3545)."""
    grafana_ui_url = cfg.get("monitoring", "grafana_ui_url", fallback="http://127.0.0.1:3001")
    grafana_admin_password = ops._grafana_admin_password(cfg)
    webhook_configured = bool(get_monitoring_slack_webhook_url(cfg).strip())

    result = ops._send_grafana_test_alert(
        grafana_ui_url, grafana_admin_password, webhook_configured
    )

    # Whether this comes back `ok=True` (delivered, or intact-but-unconfigured)
    # or `ok=False` (a genuine delivery failure with a real webhook configured),
    # what matters is that Grafana's receiver-test API accepted and processed
    # the request instead of rejecting it outright -- the class of failure
    # #3545 exists to fix.
    assert "rejected" not in result.message
    assert "Failed to reach Grafana's receiver-test API" not in result.message


@pytest.mark.usefixtures("require_grafana")
def test_grafana_lists_nyxgpt_slack_receiver_at_computed_k8s_name(cfg):
    """The k8s resource name `_grafana_receiver_k8s_name` computes for the
    `nyxgpt-slack` contact point must match what a live Grafana actually
    assigns it -- this is the exact assumption `alert-test`'s URL depends on
    (#3545)."""
    grafana_ui_url = cfg.get("monitoring", "grafana_ui_url", fallback="http://127.0.0.1:3001")
    grafana_admin_password = ops._grafana_admin_password(cfg)
    expected_name = ops._grafana_receiver_k8s_name(ops.GRAFANA_SLACK_CONTACT_POINT_NAME)

    with ops._grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
        resp = client.get(
            "/apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/receivers"
        )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:500]}"
    names = {item["metadata"]["name"] for item in resp.json()["items"]}
    assert expected_name in names, (
        f"expected receiver {expected_name!r} not found among {sorted(names)} -- "
        "did contact-points.yml's nyxgpt-slack name change?"
    )
    integration_uids = {
        integration["uid"]
        for item in resp.json()["items"]
        if item["metadata"]["name"] == expected_name
        for integration in item["spec"]["integrations"]
    }
    assert ops.GRAFANA_SLACK_INTEGRATION_UID in integration_uids


@pytest.mark.usefixtures("require_grafana")
def test_alert_test_cli_exits_zero_against_live_grafana(tmp_path, monkeypatch, cfg):
    """End-to-end: `nyxgpt ops alert-test`'s CLI entrypoint against a live
    Grafana returns 0 -- covers config loading, monitoring-enabled check,
    and result formatting together, not just `_send_grafana_test_alert` in
    isolation (#3545)."""
    from unittest.mock import MagicMock

    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    grafana_ui_url = cfg.get("monitoring", "grafana_ui_url", fallback="http://127.0.0.1:3001")
    grafana_admin_password = ops._grafana_admin_password(cfg)
    slack_webhook_url = get_monitoring_slack_webhook_url(cfg)
    cfg_path.write_text(
        "[monitoring]\n"
        "enabled = true\n"
        f"grafana_ui_url = {grafana_ui_url}\n"
        f"grafana_admin_password = {grafana_admin_password}\n"
        f"slack_webhook_url = {slack_webhook_url}\n"
    )
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    rc = ops.alert_test(MagicMock())

    assert rc == 0
