"""Unit tests for the guided secrets endpoints (#3505):

`GET|POST /api/v1/config/secrets` and `POST /api/v1/config/secrets/sync`.

All tests redirect config.ini to a temp path via `nyxgpt.app._config_file_path`
so they never touch the real `~/.nyxGPT/config.ini`, mirroring
`test_config_sections_endpoint.py`'s fixture setup.
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


def test_get_secrets_lists_the_three_guided_secrets(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/config/secrets")
    assert resp.status_code == 200
    full_keys = {s["full_key"] for s in resp.json()["secrets"]}
    assert full_keys == {"auth.api_key", "openai.api_key", "github.pat"}


def test_get_secrets_never_returns_cleartext(_isolated_config):
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n[github]\npat = ghp_topsecretvalue1234\n"
    )
    config_module._CACHED_CFG = None
    client = TestClient(app)
    resp = client.get("/api/v1/config/secrets")
    assert resp.status_code == 200
    assert "ghp_topsecretvalue1234" not in resp.text


def test_post_secrets_writes_a_value_and_returns_masked_only(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "github", "key": "pat", "value": "ghp_" + "a" * 36},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["set"] == "github.pat"
    assert "ghp_" + "a" * 36 not in resp.text
    assert any(s["full_key"] == "github.pat" and s["set"] for s in data["secrets"])

    on_disk = _isolated_config.read_text()
    assert "ghp_" + "a" * 36 in on_disk


def test_post_secrets_rejects_unknown_section_or_key(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "nyxgpt", "key": "default_model", "value": "whatever"},
    )
    assert resp.status_code == 404


def test_post_secrets_rejects_invalid_value(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "openai", "key": "api_key", "value": "short"},
    )
    assert resp.status_code == 422
    assert "short" not in resp.text


def test_post_secrets_generate_for_auth_api_key(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "auth", "key": "api_key", "generate": True},
    )
    assert resp.status_code == 200
    on_disk = _isolated_config.read_text()
    assert "[auth]" in on_disk and "api_key" in on_disk


def test_post_secrets_generate_rejected_for_key_without_generator(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "github", "key": "pat", "generate": True},
    )
    assert resp.status_code == 400


def test_post_secrets_requires_section_and_key(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/secrets", json={"value": "x"})
    assert resp.status_code == 400


def test_post_secrets_sync_wraps_ops_module_and_reports_names_only(_isolated_config):
    client = TestClient(app)
    with patch.object(
        app_module.ops_module,
        "sync_secrets_to_github_actions",
        return_value=[
            OpsResult(True, "Synced monitoring.slack_bot_token -> Actions secret SLACK_BOT_TOKEN")
        ],
    ) as mock_sync:
        resp = client.post("/api/v1/config/secrets/sync", json={"dry_run": False})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["dry_run"] is False
    assert data["results"][0]["message"].endswith("SLACK_BOT_TOKEN")
    mock_sync.assert_called_once_with(dry_run=False)


def test_post_secrets_sync_defaults_to_non_dry_run(_isolated_config):
    client = TestClient(app)
    with patch.object(
        app_module.ops_module, "sync_secrets_to_github_actions", return_value=[]
    ) as mock_sync:
        resp = client.post("/api/v1/config/secrets/sync", json={})

    assert resp.status_code == 200
    mock_sync.assert_called_once_with(dry_run=False)


def test_post_secrets_sync_reports_failure_when_any_result_fails(_isolated_config):
    client = TestClient(app)
    with patch.object(
        app_module.ops_module,
        "sync_secrets_to_github_actions",
        return_value=[
            OpsResult(True, "Synced a -> Actions secret A"),
            OpsResult(False, "Failed to sync b -> Actions secret B", "network error"),
        ],
    ):
        resp = client.post("/api/v1/config/secrets/sync", json={"dry_run": True})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert len(data["results"]) == 2
