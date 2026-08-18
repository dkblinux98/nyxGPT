"""Unit tests for the /api/v1/admin/* endpoints (admin dashboard).

Covers src/nyxgpt/app.py's admin_overview/admin_activity_list/admin_access_*
route handlers. External dependencies (canary status, the resource
monitor, and config file writes) are mocked so no cluster or real config
mutation is required.

Related: #2698
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
import nyxgpt.config as config_module
import nyxgpt.restart_state as restart_state_module
from nyxgpt.app import app

pytestmark = pytest.mark.unit


def _auth_cfg(enabled=False, header="X-API-Key", api_key=""):
    return {"enabled": enabled, "header": header, "api_key": api_key}


# --- GET /admin/overview ---


def test_admin_overview_aggregates_status():
    canary_status = {"namespace": "nyxgpt", "active": False}

    with (
        patch("nyxgpt.app.canary_module.status", return_value=canary_status),
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["canary"] == canary_status
    assert body["resource_metrics"] is None
    assert "info" in body
    assert "observability" in body
    assert set(body["observability"]) == {
        "monitoring",
        "tracing",
        "error_tracking",
        "log_aggregation",
    }
    assert "auth_enabled" in body


def test_admin_overview_degrades_gracefully_when_canary_status_fails():
    with (
        patch("nyxgpt.app.canary_module.status", side_effect=RuntimeError("kubectl not found")),
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/overview")

    assert response.status_code == 200
    body = response.json()
    # The sub-section degrades to a generic error payload; the raw exception
    # detail ("kubectl not found") is logged server-side but never returned to
    # the client (CodeQL py/stack-trace-exposure).
    assert "error" in body["canary"]
    assert "kubectl not found" not in body["canary"]["error"]
    assert body["canary"]["error"] == "unavailable"


# --- GET /admin/activity ---


def test_admin_activity_returns_recent_events():
    events = [
        {"ts": 1.0, "action": "config.updated", "detail": "log_level=DEBUG"},
        {"ts": 2.0, "action": "deploy.switch", "detail": "blue -> green"},
    ]

    with patch("nyxgpt.app.admin_activity_module.recent", return_value=events) as mock_recent:
        client = TestClient(app)
        response = client.get("/api/v1/admin/activity")

    assert response.status_code == 200
    assert response.json() == {"events": events}
    mock_recent.assert_called_once()


def test_admin_activity_clamps_limit_to_valid_range():
    with patch("nyxgpt.app.admin_activity_module.recent", return_value=[]) as mock_recent:
        client = TestClient(app)
        response = client.get("/api/v1/admin/activity?limit=10000")

    assert response.status_code == 200
    args, kwargs = mock_recent.call_args
    assert args[0] == 500


# --- GET /admin/access ---


def test_admin_access_get_masks_api_key():
    with patch(
        "nyxgpt.app._auth_cfg",
        return_value=_auth_cfg(enabled=True, header="X-API-Key", api_key="supersecretvalue"),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/access", headers={"X-API-Key": "supersecretvalue"})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["api_key_set"] is True
    assert body["api_key_masked"] == "supe********alue"
    assert "api_key" not in body


def test_admin_access_get_reports_no_key_set():
    with patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg(enabled=False, api_key="")):
        client = TestClient(app)
        response = client.get("/api/v1/admin/access")

    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] is False
    assert body["api_key_masked"] is None


# --- POST /admin/access ---


def test_admin_access_update_rejects_empty_payload():
    with patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg()):
        client = TestClient(app)
        response = client.post("/api/v1/admin/access", json={})

    assert response.status_code == 400


def test_admin_access_update_toggles_enabled():
    with (
        patch(
            "nyxgpt.app._auth_cfg",
            return_value=_auth_cfg(enabled=True, api_key="existing-key"),
        ),
        patch("nyxgpt.app._apply_auth_config_updates") as mock_apply,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/admin/access",
            json={"enabled": True},
            headers={"X-API-Key": "existing-key"},
        )

    assert response.status_code == 200
    mock_apply.assert_called_once_with({"enabled": True})
    mock_record.assert_called_once()
    assert response.json()["enabled"] is True


def test_admin_access_update_rotate_returns_new_key_once():
    with (
        patch(
            "nyxgpt.app._auth_cfg",
            return_value=_auth_cfg(enabled=True, api_key="brand-new-generated-key"),
        ),
        patch("nyxgpt.app._apply_auth_config_updates") as mock_apply,
        patch("nyxgpt.app.admin_activity_module.record"),
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/admin/access",
            json={"rotate": True},
            headers={"X-API-Key": "brand-new-generated-key"},
        )

    assert response.status_code == 200
    body = response.json()
    # The freshly generated key is returned once in the response body...
    assert isinstance(body["api_key"], str) and len(body["api_key"]) > 16
    assert body["api_key"] != "brand-new-generated-key"  # not the pre-rotation key
    assert body["api_key_masked"] != body["api_key"]

    # ...and the same generated value is what got persisted to config.
    (call_updates,), _ = mock_apply.call_args
    assert call_updates["api_key"] == body["api_key"]


def test_admin_access_update_rotate_rejected_when_cloud_provider_configured():
    """When `[secrets] provider` is set, `get_auth_api_key` always prefers
    the AWS-resolved value over config.ini, so a rotation written here
    would be inert (the middleware keeps enforcing the old cloud-stored
    key while this endpoint reports the new one as active). It must be
    rejected rather than silently no-op'd. See #3507."""
    with (
        patch(
            "nyxgpt.app._auth_cfg",
            return_value=_auth_cfg(enabled=True, api_key="existing-key"),
        ),
        patch("nyxgpt.app.get_secrets_provider", return_value="ssm"),
        patch("nyxgpt.app._apply_auth_config_updates") as mock_apply,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/admin/access",
            json={"rotate": True},
            headers={"X-API-Key": "existing-key"},
        )

    assert response.status_code == 400
    assert "cloud" in response.json()["error"]["message"].lower()
    mock_apply.assert_not_called()
    mock_record.assert_not_called()


def test_admin_access_update_records_activity_event():
    with (
        patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg(enabled=False)),
        patch("nyxgpt.app._apply_auth_config_updates"),
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post("/api/v1/admin/access", json={"header": "X-Custom-Key"})

    assert response.status_code == 200
    mock_record.assert_called_once()
    args, _ = mock_record.call_args
    assert args[0] == "access.updated"


# --- POST /admin/access and the pending-restart notice (#3806) ---
#
# The Access Management panel is the *third* writer of `[auth] api_key`,
# alongside the Configuration Wizard (`POST /config/sections`) and the CLI
# (`nyxgpt secrets setup`). All three write a key the `web` tier read once at
# process start, so all three must raise the same notice -- a rotation here
# that stayed silent would rebuild the exact 401 wall #3806 exists to close,
# on the very page that hosts the notice. These tests exercise the real
# `_apply_auth_config_updates` (no mock) against a real temp config.


@pytest.fixture
def isolated_auth_config(tmp_path, monkeypatch):
    """Redirect config.ini *and* the pending-restart state file into `tmp_path`.

    Both are needed together: the endpoint's classification is read from the
    config it just wrote, and the notice it raises is persisted to
    `~/.nyxGPT/pending-restart.json` -- which an un-redirected test would read
    and then delete from the developer's own machine.
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n\n"
        "[auth]\nenabled = true\napi_key = original-key\nheader = X-API-Key\n"
    )
    monkeypatch.setattr(app_module, "_config_file_path", lambda: cfg_path)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setenv("NYXGPT_PENDING_RESTART_PATH", str(tmp_path / "pending-restart.json"))
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None
    restart_state_module.reset()
    yield cfg_path
    restart_state_module.reset()
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None


def test_dashboard_rotation_flags_web_not_api(isolated_auth_config):
    """#3806's worked example, reached from the dashboard instead of the wizard."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/access",
        json={"rotate": True},
        headers={"X-API-Key": "original-key"},
    )
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]

    # The api tier honours the rotation immediately -- the old key is already
    # rejected, which is precisely why the frozen web tier now 401s.
    assert (
        client.get(
            "/api/v1/infra/restart-status", headers={"X-API-Key": "original-key"}
        ).status_code
        == 401
    )

    status = client.get("/api/v1/infra/restart-status", headers={"X-API-Key": new_key}).json()
    assert status["pending"]["web"]["keys"] == ["auth.api_key"]
    assert status["restart_command"] == "nyxgpt ops restart web"
    assert status["session_disrupting"] == ["web"]


def test_dashboard_toggle_of_auth_enabled_flags_web(isolated_auth_config):
    """`[auth] enabled` is restart-required for `web` too -- the wrapper only
    exports the key at all when it reads `enabled = true`."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/access",
        json={"enabled": False},
        headers={"X-API-Key": "original-key"},
    )
    assert resp.status_code == 200

    status = client.get("/api/v1/infra/restart-status").json()
    assert status["pending"]["web"]["keys"] == ["auth.enabled"]


def test_dashboard_header_change_raises_no_notice(isolated_auth_config):
    """`[auth] header` is not classified restart-required, so it must stay quiet.

    A writer that flagged every key it touched would make the notice
    meaningless; only the classification decides.
    """
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/access",
        json={"header": "X-Other-Key"},
        headers={"X-API-Key": "original-key"},
    )
    assert resp.status_code == 200
    # The api tier reads the header name live, so the follow-up call already
    # has to use the new one.
    status = client.get("/api/v1/infra/restart-status", headers={"X-Other-Key": "original-key"})
    assert status.json()["pending"] == {}


def test_dashboard_revert_retires_the_notice(isolated_auth_config):
    """Putting the key back to the value `web` is still running clears the
    notice with no restart -- the same self-resolving revert the wizard has."""
    client = TestClient(app)
    rotated = client.post(
        "/api/v1/admin/access",
        json={"rotate": True},
        headers={"X-API-Key": "original-key"},
    ).json()["api_key"]
    assert (
        "web"
        in client.get("/api/v1/infra/restart-status", headers={"X-API-Key": rotated}).json()[
            "pending"
        ]
    )

    # `POST /admin/access` only ever *generates* a key, so the revert arrives
    # through the wizard writer -- which is the point: the two writers share
    # one state file, so either can retire what the other raised.
    resp = client.post(
        "/api/v1/config/sections",
        json={"auth": {"api_key": "original-key"}},
        headers={"X-API-Key": rotated},
    )
    assert resp.status_code == 200
    assert resp.json()["restart_pending"] == {}
    assert (
        client.get("/api/v1/infra/restart-status", headers={"X-API-Key": "original-key"}).json()[
            "pending"
        ]
        == {}
    )


def test_dashboard_rotation_preserves_unrelated_config(isolated_auth_config):
    """The auth writer rewrites config.ini wholesale; other sections must survive."""
    client = TestClient(app)
    client.post(
        "/api/v1/admin/access",
        json={"rotate": True},
        headers={"X-API-Key": "original-key"},
    )
    assert "base_url = http://localhost:11434" in isolated_auth_config.read_text()
