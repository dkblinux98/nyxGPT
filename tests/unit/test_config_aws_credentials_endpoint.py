"""Unit tests for the guided AWS credentials endpoints (P6-13, #3512):

`GET|POST /api/v1/config/aws-credentials` and
`POST /api/v1/config/aws-credentials/secret-store`.

All tests redirect config.ini and ~/.aws/credentials to temp paths so they
never touch the real files, mirroring `test_config_secrets_endpoint.py`'s
fixture setup.
"""

from __future__ import annotations

from unittest.mock import patch

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


def test_post_aws_credentials_profile_destination_writes_file_and_reference(_isolated_config):
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
    assert resp.status_code == 200
    data = resp.json()
    assert data["reference"]["credentials_source"] == "profile"
    assert data["profile_file_status"]["set"] is True

    on_disk = _isolated_config.read_text()
    assert VALID_ACCESS_KEY_ID not in on_disk
    assert VALID_SECRET_ACCESS_KEY not in on_disk
    assert "[cloud]" in on_disk


def test_post_aws_credentials_never_echoes_cleartext(_isolated_config):
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
    assert VALID_ACCESS_KEY_ID not in resp.text
    assert VALID_SECRET_ACCESS_KEY not in resp.text


def test_post_aws_credentials_ambient_destination_requires_no_key_pair(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials",
        json={"destination": "ambient", "profile": "nyxgpt", "region": "us-east-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["reference"]["credentials_source"] == "ambient"
    assert not (_isolated_config.parent / "aws-credentials").exists()


def test_post_aws_credentials_rejects_missing_required_fields(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/aws-credentials", json={"destination": "ambient"})
    assert resp.status_code == 400


def test_post_aws_credentials_rejects_invalid_value_with_422(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials",
        json={"destination": "ambient", "profile": "nyxgpt", "region": "not-a-region"},
    )
    assert resp.status_code == 422


def test_post_aws_credentials_keychain_destination_reports_operational_error_as_400(
    _isolated_config,
):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials",
        json={
            "destination": "keychain",
            "profile": "nyxgpt",
            "region": "us-east-1",
            "access_key_id": VALID_ACCESS_KEY_ID,
            "secret_access_key": VALID_SECRET_ACCESS_KEY,
        },
    )
    # _keyring_module patched to None in the fixture -- keychain destination
    # can't be satisfied, and that must surface as a clean 400, not a 500.
    assert resp.status_code == 400


def test_post_aws_secret_store_writes_reference_fields(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials/secret-store",
        json={"provider": "ssm", "ssm_prefix": "/nyxgpt"},
    )
    assert resp.status_code == 200
    data = resp.json()
    by_key = {e["key"]: e["value"] for e in data["secret_store"]}
    assert by_key["provider"] == "ssm"
    assert by_key["ssm_prefix"] == "/nyxgpt"

    on_disk = _isolated_config.read_text()
    assert "[secrets]" in on_disk
    assert "provider = ssm" in on_disk


def test_post_aws_secret_store_rejects_unknown_provider(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/aws-credentials/secret-store",
        json={"provider": "vault"},
    )
    assert resp.status_code == 422


def test_post_aws_secret_store_records_admin_activity(_isolated_config):
    client = TestClient(app)
    with patch.object(app_module.admin_activity_module, "record") as mock_record:
        resp = client.post(
            "/api/v1/config/aws-credentials/secret-store",
            json={"provider": "ssm"},
        )
    assert resp.status_code == 200
    mock_record.assert_called_once()
    assert mock_record.call_args[0][0] == "config.secret_store_reference_set"
