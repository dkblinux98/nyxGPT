"""Tests for `nyxgpt ops secrets-sync`: config.ini -> GitHub Actions secrets (#3505)."""

from __future__ import annotations

import base64
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from nacl import public as nacl_public

from nyxgpt import ops

pytestmark = pytest.mark.unit


def _mock_client(base_url: str, handler) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[github]\n"
        "pat = ghp_" + "a" * 36 + "\n"
        "repo_owner = dkblinux98\n"
        "repo_name = nyxGPT\n" + extra
    )
    return cfg_path


# --- _secrets_sync_targets: mapping enforcement ---


def test_secrets_sync_targets_only_includes_mapped_keys_with_values():
    cfg = ConfigParser()
    cfg.add_section("github")
    cfg.set("github", "developer_agent_token", "dev-token-value")
    cfg.add_section("monitoring")
    cfg.set("monitoring", "slack_bot_token", "xoxb-bot-token")
    # Unmapped keys, even if non-empty, must never appear in targets.
    cfg.add_section("openai")
    cfg.set("openai", "api_key", "sk-should-not-sync")
    cfg.set("github", "pat", "ghp_should_not_sync_either")

    targets = ops._secrets_sync_targets(cfg)

    full_keys = {t[0] for t in targets}
    assert full_keys == {"github.developer_agent_token", "monitoring.slack_bot_token"}
    actions_names = {t[2] for t in targets}
    assert actions_names == {"DEVELOPER_AGENT_TOKEN", "SLACK_BOT_TOKEN"}


def test_secrets_sync_targets_skips_blank_mapped_keys():
    cfg = ConfigParser()
    cfg.add_section("github")
    cfg.set("github", "developer_agent_token", "  ")  # blank after strip
    assert ops._secrets_sync_targets(cfg) == []


def test_secrets_sync_targets_empty_for_fresh_config():
    assert ops._secrets_sync_targets(ConfigParser()) == []


# --- _encrypt_for_actions_secret: sealed-box roundtrip ---


def test_encrypt_for_actions_secret_roundtrips_via_sealed_box():
    private_key = nacl_public.PrivateKey.generate()
    public_key_b64 = base64.b64encode(bytes(private_key.public_key)).decode("ascii")

    encrypted_b64 = ops._encrypt_for_actions_secret(public_key_b64, "super-secret-value")

    sealed_box = nacl_public.SealedBox(private_key)
    decrypted = sealed_box.decrypt(base64.b64decode(encrypted_b64))
    assert decrypted == b"super-secret-value"


def test_encrypt_for_actions_secret_output_never_contains_plaintext():
    private_key = nacl_public.PrivateKey.generate()
    public_key_b64 = base64.b64encode(bytes(private_key.public_key)).decode("ascii")
    encrypted_b64 = ops._encrypt_for_actions_secret(public_key_b64, "plaintext-marker")
    assert "plaintext-marker" not in encrypted_b64


# --- sync_secrets_to_github_actions: guard rails ---


def test_sync_missing_config_file_fails(tmp_path: Path):
    results = ops.sync_secrets_to_github_actions(cfg_path=tmp_path / "missing.ini")
    assert len(results) == 1
    assert results[0].ok is False
    assert "Missing config" in results[0].message


def test_sync_no_mapped_secrets_configured_is_a_success_noop(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    mock_client.assert_not_called()
    assert len(results) == 1
    assert results[0].ok is True
    assert "nothing to sync" in results[0].message.lower()


def test_sync_dry_run_reports_targets_without_any_network_call(tmp_path: Path):
    cfg_path = _write_config(
        tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-should-not-be-shown\n"
    )

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path, dry_run=True)

    mock_client.assert_not_called()
    assert len(results) == 1
    assert results[0].ok is True
    assert "monitoring.slack_bot_token" in results[0].message
    assert "SLACK_BOT_TOKEN" in results[0].message
    assert "xoxb-should-not-be-shown" not in results[0].message


def test_sync_missing_pat_fails_before_any_network_call(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[monitoring]\nslack_bot_token = xoxb-token\n")

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    mock_client.assert_not_called()
    assert len(results) == 1
    assert results[0].ok is False
    assert "[github] pat" in results[0].message


def test_sync_missing_repo_owner_fails_before_any_network_call(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[github]\npat = ghp_" + "a" * 36 + "\n[monitoring]\nslack_bot_token = xoxb-token\n"
    )

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    mock_client.assert_not_called()
    assert len(results) == 1
    assert results[0].ok is False
    assert "repo_owner/repo_name" in results[0].message


# --- sync_secrets_to_github_actions: live (mocked) API ---


def _fake_public_key():
    private_key = nacl_public.PrivateKey.generate()
    key_b64 = base64.b64encode(bytes(private_key.public_key)).decode("ascii")
    return private_key, key_b64


def test_sync_success_pushes_encrypted_value_and_reports_name_only(tmp_path: Path):
    cfg_path = _write_config(tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-real-secret\n")
    private_key, key_b64 = _fake_public_key()

    put_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/secrets/public-key"):
            return httpx.Response(200, json={"key_id": "key-id-123", "key": key_b64})
        if request.method == "PUT":
            put_bodies.append(request.read())
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    assert len(results) == 1
    assert results[0].ok is True
    assert "monitoring.slack_bot_token" in results[0].message
    assert "SLACK_BOT_TOKEN" in results[0].message
    assert "xoxb-real-secret" not in results[0].message

    # The plaintext must never appear on the wire either -- only its sealed-box ciphertext.
    assert len(put_bodies) == 1
    assert b"xoxb-real-secret" not in put_bodies[0]

    # And the ciphertext genuinely decrypts back to the real value.
    import json as _json

    sent = _json.loads(put_bodies[0])
    assert sent["key_id"] == "key-id-123"
    decrypted = nacl_public.SealedBox(private_key).decrypt(
        base64.b64decode(sent["encrypted_value"])
    )
    assert decrypted == b"xoxb-real-secret"


def test_sync_public_key_fetch_failure_reports_single_result(tmp_path: Path):
    cfg_path = _write_config(tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-token\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    assert len(results) == 1
    assert results[0].ok is False
    assert "public key" in results[0].message.lower()


def test_sync_partial_failure_does_not_abort_remaining_secrets(tmp_path: Path):
    cfg_path = _write_config(tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-token\n")
    # Add a second mapped secret via the [github] section already present.
    parser = ConfigParser()
    parser.read(cfg_path)
    parser.set("github", "developer_agent_token", "dev-token-value")
    with open(cfg_path, "w") as f:
        parser.write(f)

    _private_key, key_b64 = _fake_public_key()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/secrets/public-key"):
            return httpx.Response(200, json={"key_id": "key-id-123", "key": key_b64})
        if request.url.path.endswith("/DEVELOPER_AGENT_TOKEN"):
            return httpx.Response(500, json={"message": "server error"})
        return httpx.Response(204)

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)

    by_secret = {r.message.split("Actions secret ")[-1]: r.ok for r in results}
    assert by_secret["DEVELOPER_AGENT_TOKEN"] is False
    assert by_secret["SLACK_BOT_TOKEN"] is True


# --- secrets_sync CLI wrapper ---


def test_secrets_sync_cli_wrapper_returns_zero_on_success(tmp_path: Path, capsys):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")
    args = MagicMock(config=str(cfg_path), dry_run=False)

    rc = ops.secrets_sync(args)

    assert rc == 0
    assert "nothing to sync" in capsys.readouterr().out.lower()


def test_secrets_sync_cli_wrapper_returns_nonzero_on_failure(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[monitoring]\nslack_bot_token = xoxb-token\n")  # no [github] pat
    args = MagicMock(config=str(cfg_path), dry_run=False)

    rc = ops.secrets_sync(args)

    assert rc == 2


def test_secrets_sync_cli_wrapper_never_prints_secret_values(tmp_path: Path, capsys):
    cfg_path = _write_config(tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-do-not-print\n")
    args = MagicMock(config=str(cfg_path), dry_run=True)

    ops.secrets_sync(args)

    assert "xoxb-do-not-print" not in capsys.readouterr().out


def test_secrets_sync_cli_wrapper_records_ops_lifecycle_action(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")
    args = MagicMock(config=str(cfg_path), dry_run=False)

    with patch.object(ops, "_record_ops_action") as mock_record:
        ops.secrets_sync(args)

    mock_record.assert_called_once()
    assert mock_record.call_args.args[0] == "secrets-sync"
