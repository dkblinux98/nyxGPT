"""Unit tests for the full config wizard endpoints (#3354):

`GET|POST /api/v1/config/sections` and `POST /api/v1/config/restart`.

All tests redirect config.ini to a temp path via `nyxgpt.app._config_file_path`
so they never touch the real `~/.nyxGPT/config.ini`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
import nyxgpt.config as config_module
from nyxgpt.app import app
from nyxgpt.ops import OpsResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point config reads/writes at a temp file and reset the module cache.

    Pre-populated with a minimal `[ollama]` section so GET requests (which
    load config via the app's `load_cfg_and_refresh_logging` middleware
    before the handler runs) succeed even before any POST has written
    anything -- mirrors the minimal fixture config `tests/conftest.py`
    creates for the real `~/.nyxGPT/config.ini`.
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[ollama]\nbase_url = http://localhost:11434\n")
    monkeypatch.setattr(app_module, "_config_file_path", lambda: cfg_path)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", cfg_path)
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None
    yield cfg_path
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None


def test_get_sections_returns_schema_and_values(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    data = resp.json()
    assert "sections" in data
    assert "schema" in data
    assert "nyxgpt" in data["sections"]
    assert "api" in data["sections"]
    section_names = {s["section"] for s in data["schema"]}
    assert "rag" in section_names


def test_post_sections_applies_and_returns_effective_values(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/sections",
        json={"nyxgpt": {"default_model": "llama3:8b"}, "logging": {"level": "debug"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"]["nyxgpt"]["default_model"] == "llama3:8b"
    assert data["applied"]["logging"]["level"] == "DEBUG"
    assert data["sections"]["nyxgpt"]["default_model"] == "llama3:8b"
    assert data["restart_required"] == []
    assert data["observability_reconciled"] is False


def test_post_sections_reports_restart_required_for_api_port(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] == ["api"]


def test_post_sections_no_restart_when_value_unchanged(_isolated_config):
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    resp = client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert resp.json()["restart_required"] == []


def test_post_sections_rejects_invalid_payload(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"api": {"port": "not-a-port"}})
    assert resp.status_code == 422
    assert "errors" in resp.json()["error"]["details"]


def test_post_sections_rejects_empty_payload(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={})
    assert resp.status_code == 400


def test_post_sections_never_echoes_secret_value(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"auth": {"api_key": "topsecret123"}})
    assert resp.status_code == 200
    data = resp.json()
    assert "topsecret123" not in resp.text
    assert data["sections"]["auth"]["api_key"]["set"] is True
    assert data["applied"]["auth"]["api_key"] == {"set": True, "masked": "tops****t123"}


def test_post_sections_triggers_observability_reconciliation(_isolated_config):
    client = TestClient(app)
    with patch(
        "nyxgpt.app.ops_module.reconcile_observability",
        return_value=[OpsResult(True, "started")],
    ) as mock_reconcile:
        resp = client.post("/api/v1/config/sections", json={"tracing": {"enabled": True}})

    assert resp.status_code == 200
    data = resp.json()
    assert data["observability_reconciled"] is True
    assert data["observability_result"] == {"ok": True, "messages": ["started"]}
    mock_reconcile.assert_called_once_with(True)


def test_post_sections_no_reconciliation_when_observability_field_unchanged(_isolated_config):
    client = TestClient(app)
    with patch("nyxgpt.app.ops_module.reconcile_observability") as mock_reconcile:
        resp = client.post("/api/v1/config/sections", json={"tracing": {"service_name": "svc"}})

    assert resp.status_code == 200
    assert resp.json()["observability_reconciled"] is False
    mock_reconcile.assert_not_called()


def test_restart_endpoint_schedules_and_returns_immediately(_isolated_config):
    client = TestClient(app)
    with patch("nyxgpt.app.ops_module.restart") as mock_restart:
        resp = client.post("/api/v1/config/restart", json={"target": "api"})
        assert resp.status_code == 200
        assert resp.json() == {"target": "api", "status": "scheduled"}
        # The restart call itself is deferred via threading.Timer, not called inline.
        mock_restart.assert_not_called()


def test_restart_endpoint_rejects_unknown_target(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/restart", json={"target": "not-a-real-target"})
    assert resp.status_code == 400


def test_restart_endpoint_defaults_to_all(_isolated_config):
    client = TestClient(app)
    with patch("nyxgpt.app.ops_module.restart"):
        resp = client.post("/api/v1/config/restart", json={})
    assert resp.status_code == 200
    assert resp.json()["target"] == "all"
