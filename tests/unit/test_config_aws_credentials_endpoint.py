"""Unit tests for the AWS credentials status endpoint (P6-13, #3512; #3805):

`GET /api/v1/config/aws-credentials`. The write endpoints (`POST
/api/v1/config/aws-credentials` and `POST
/api/v1/config/aws-credentials/secret-store`) were removed with the
`/admin/aws-credentials` screen by owner decision (#3805) -- AWS identity is
entered with `nyxgpt cloud credentials-setup`. The last two tests here are the
regression that keeps a write path from coming back.

All tests redirect config.ini and ~/.aws/credentials to temp paths so they
never touch the real files, mirroring `test_config_secrets_endpoint.py`'s
fixture setup.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
import nyxgpt.aws_credentials_setup as aws_credentials_setup_module
import nyxgpt.config as config_module
from nyxgpt.app import app

pytestmark = pytest.mark.unit

VALID_ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"
VALID_SECRET_ACCESS_KEY = "s3cr3t" + "x" * 30


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[ollama]\nbase_url = http://localhost:11434\n")
    monkeypatch.setattr(app_module, "_config_file_path", lambda: cfg_path)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(
        aws_credentials_setup_module, "AWS_CREDENTIALS_FILE", tmp_path / "aws-credentials"
    )
    monkeypatch.setattr(aws_credentials_setup_module, "_keyring_module", lambda: None)
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None
    yield cfg_path
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None


def test_get_aws_credentials_reports_field_metadata_and_blank_reference(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/config/aws-credentials")
    assert resp.status_code == 200
    data = resp.json()
    field_keys = {f["key"] for f in data["fields"]}
    assert field_keys == {"profile", "region", "access_key_id", "secret_access_key"}
    assert data["reference"] == {"profile": "", "region": "", "credentials_source": ""}
    assert data["profile_file_status"]["set"] is False


def test_post_aws_credentials_is_gone_and_never_writes(_isolated_config):
    """The removed write path must not accept an access key pair over HTTP (#3805)."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials",
        json={
            "destination": "profile",
            "profile": "nyxgpt",
            "region": "us-east-1",
            "access_key_id": VALID_ACCESS_KEY_ID,
            "secret_access_key": VALID_SECRET_ACCESS_KEY,
        },
    )
    assert resp.status_code in (404, 405)
    assert not (_isolated_config.parent / "aws-credentials").exists()
    assert "[cloud]" not in _isolated_config.read_text()


def test_post_aws_secret_store_is_gone(_isolated_config):
    """The `[secrets]` provider reference is set by the wizard or the CLI (#3805)."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials/secret-store",
        json={"provider": "ssm", "ssm_prefix": "/nyxgpt"},
    )
    assert resp.status_code in (404, 405)
    assert "[secrets]" not in _isolated_config.read_text()
