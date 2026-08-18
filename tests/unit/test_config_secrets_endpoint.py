"""Unit tests for the guided secrets status endpoint (#3505, #3805):

`GET /api/v1/config/secrets`. The write endpoints (`POST
/api/v1/config/secrets` and `POST /api/v1/config/secrets/sync`) were removed
with the `/admin/secrets` screen by owner decision (#3805) -- secret entry is
`nyxgpt secrets setup` and `nyxgpt ops secrets-sync`. The last two tests here
are the regression that keeps a write path from coming back.

All tests redirect config.ini to a temp path via `nyxgpt.app._config_file_path`
so they never touch the real `~/.nyxGPT/config.ini`, mirroring
`test_config_sections_endpoint.py`'s fixture setup.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
import nyxgpt.config as config_module
from nyxgpt.app import app

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


def test_post_secrets_is_gone_and_never_writes(_isolated_config):
    """The removed write path must not accept a secret over HTTP (#3805)."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/secrets",
        json={"section": "github", "key": "pat", "value": "ghp_" + "a" * 36},
    )
    assert resp.status_code in (404, 405)
    assert "ghp_" + "a" * 36 not in _isolated_config.read_text()


def test_post_secrets_sync_is_gone(_isolated_config):
    """Pushing secrets to GitHub Actions is `nyxgpt ops secrets-sync` (#3805)."""
    client = TestClient(app)
    resp = client.post("/api/v1/config/secrets/sync", json={"dry_run": True})
    assert resp.status_code in (404, 405)
