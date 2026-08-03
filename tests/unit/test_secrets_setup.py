"""Tests for the guided secrets setup flow (#3505)."""

from __future__ import annotations

import stat
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import secrets_setup
from nyxgpt.secrets_setup import (
    GUIDED_SECRETS,
    SecretValidationError,
    find_guided_secret,
    mask_secret,
    run_secrets_setup,
    secret_status,
    validate_secret_value,
    write_secret,
)

pytestmark = pytest.mark.unit


# --- GUIDED_SECRETS / find_guided_secret ---


def test_guided_secrets_covers_the_three_human_provided_keys():
    full_keys = {s.full_key for s in GUIDED_SECRETS}
    assert full_keys == {"auth.api_key", "openai.api_key", "github.pat"}


def test_find_guided_secret_returns_matching_spec():
    spec = find_guided_secret("github", "pat")
    assert spec is not None
    assert spec.full_key == "github.pat"


def test_find_guided_secret_returns_none_for_unknown_key():
    assert find_guided_secret("nyxgpt", "default_model") is None


def test_only_auth_api_key_has_a_generator():
    generators = {s.full_key: s.generate is not None for s in GUIDED_SECRETS}
    assert generators == {
        "auth.api_key": True,
        "openai.api_key": False,
        "github.pat": False,
    }


# --- mask_secret ---


def test_mask_secret_empty_string():
    assert mask_secret("") == ""


def test_mask_secret_short_value_fully_masked():
    assert mask_secret("short") == "*****"


def test_mask_secret_long_value_keeps_edges():
    masked = mask_secret("ghp_abcdefghijklmnopqrstuvwxyz")
    assert masked.startswith("ghp_")
    assert masked.endswith("wxyz")
    assert "*" in masked
    # never reveals the full value
    assert masked != "ghp_abcdefghijklmnopqrstuvwxyz"


# --- validators ---


def test_validate_auth_api_key_rejects_empty():
    spec = find_guided_secret("auth", "api_key")
    with pytest.raises(SecretValidationError, match="empty"):
        validate_secret_value(spec, "   ")


def test_validate_auth_api_key_rejects_whitespace():
    spec = find_guided_secret("auth", "api_key")
    with pytest.raises(SecretValidationError, match="whitespace"):
        validate_secret_value(spec, "has a space")


def test_validate_openai_key_rejects_too_short():
    spec = find_guided_secret("openai", "api_key")
    with pytest.raises(SecretValidationError, match="at least"):
        validate_secret_value(spec, "sk-short")


def test_validate_openai_key_accepts_long_value():
    spec = find_guided_secret("openai", "api_key")
    value = "sk-" + "a" * 30
    assert validate_secret_value(spec, value) == value


def test_validate_github_pat_rejects_unknown_prefix():
    spec = find_guided_secret("github", "pat")
    with pytest.raises(SecretValidationError, match="doesn't look like a GitHub token"):
        validate_secret_value(spec, "not_a_real_token_but_long_enough")


def test_validate_github_pat_accepts_known_prefix():
    spec = find_guided_secret("github", "pat")
    value = "ghp_" + "a" * 36
    assert validate_secret_value(spec, value) == value


def test_validate_github_pat_rejects_too_short_even_with_prefix():
    spec = find_guided_secret("github", "pat")
    with pytest.raises(SecretValidationError, match="at least"):
        validate_secret_value(spec, "ghp_short")


# --- secret_status ---


def test_secret_status_reports_unset_secrets():
    cfg = ConfigParser()
    status = secret_status(cfg)
    assert len(status) == 3
    for entry in status:
        assert entry["set"] is False
        assert entry["masked"] is None


def test_secret_status_reports_set_secret_masked_only():
    cfg = ConfigParser()
    cfg.add_section("github")
    cfg.set("github", "pat", "ghp_abcdefghijklmnopqrstuvwxyz")
    status = secret_status(cfg)
    github_entry = next(e for e in status if e["full_key"] == "github.pat")
    assert github_entry["set"] is True
    assert github_entry["masked"] is not None
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in str(github_entry)


def test_secret_status_never_leaks_cleartext_value():
    cfg = ConfigParser()
    cfg.add_section("openai")
    secret_value = "sk-topsecretvalue1234567890"
    cfg.set("openai", "api_key", secret_value)
    status = secret_status(cfg)
    assert secret_value not in str(status)


# --- write_secret ---


def test_write_secret_writes_validated_value_and_chmods_0600(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    spec = find_guided_secret("github", "pat")
    value = "ghp_" + "a" * 36

    written = write_secret(cfg_path, spec, value)

    assert written == value
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("github", "pat") == value
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600


def test_write_secret_preserves_other_config_content(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")

    spec = find_guided_secret("auth", "api_key")
    write_secret(cfg_path, spec, "a-strong-generated-key")

    text = cfg_path.read_text()
    assert "default_model = qwen2.5:0.5b" in text
    assert "a-strong-generated-key" in text


def test_write_secret_raises_on_invalid_value_without_writing(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    spec = find_guided_secret("openai", "api_key")

    with pytest.raises(SecretValidationError):
        write_secret(cfg_path, spec, "short")

    assert not cfg_path.exists()


# --- run_secrets_setup (CLI flow: prompt / skip / validate / generate) ---


def test_run_secrets_setup_generates_auth_key_by_default(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    with (
        patch("builtins.input", return_value=""),  # "Generate? (Y/n)" -> default yes
        patch("nyxgpt.secrets_setup.getpass.getpass", return_value=""),  # openai/github skipped
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "api_key", fallback="") != ""
    assert parser.get("openai", "api_key", fallback="") == ""
    assert parser.get("github", "pat", fallback="") == ""


def test_run_secrets_setup_prompts_and_writes_each_secret(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    github_pat = "ghp_" + "b" * 36
    with (
        patch("builtins.input", return_value="n"),  # decline auth.api_key generation
        patch(
            "nyxgpt.secrets_setup.getpass.getpass",
            side_effect=["a-manual-auth-key", "sk-" + "c" * 30, github_pat],
        ),
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "api_key") == "a-manual-auth-key"
    assert parser.get("openai", "api_key") == "sk-" + "c" * 30
    assert parser.get("github", "pat") == github_pat


def test_run_secrets_setup_skips_already_set_secrets_without_reconfigure(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[auth]\napi_key = existing-key\n[openai]\napi_key = sk-existing\n[github]\npat = ghp_existing\n"
    )

    with (
        patch("builtins.input") as mock_input,
        patch("nyxgpt.secrets_setup.getpass.getpass") as mock_getpass,
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    mock_input.assert_not_called()
    mock_getpass.assert_not_called()
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "api_key") == "existing-key"
    assert parser.get("openai", "api_key") == "sk-existing"
    assert parser.get("github", "pat") == "ghp_existing"


def test_run_secrets_setup_reconfigure_reprompts_set_secrets(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[auth]\napi_key = old-key\n")

    new_github_pat = "ghp_" + "d" * 36
    with (
        patch("builtins.input", return_value="n"),
        patch(
            "nyxgpt.secrets_setup.getpass.getpass",
            side_effect=["new-key", "sk-" + "e" * 30, new_github_pat],
        ),
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=True)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "api_key") == "new-key"
    assert parser.get("github", "pat") == new_github_pat


def test_run_secrets_setup_blank_input_skips_a_secret(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    with (
        patch("builtins.input", return_value="n"),
        patch("nyxgpt.secrets_setup.getpass.getpass", side_effect=["a-manual-auth-key", "", ""]),
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "api_key") == "a-manual-auth-key"
    assert parser.get("openai", "api_key", fallback="") == ""
    assert parser.get("github", "pat", fallback="") == ""


def test_run_secrets_setup_retries_on_invalid_value_then_succeeds(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    valid_pat = "ghp_" + "f" * 36
    with (
        patch("builtins.input", return_value="n"),
        patch(
            "nyxgpt.secrets_setup.getpass.getpass",
            side_effect=[
                "a-manual-auth-key",
                "sk-" + "g" * 30,
                "bad-prefix-but-long-enough-value",
                valid_pat,
            ],
        ),
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("github", "pat") == valid_pat


def test_run_secrets_setup_skips_after_too_many_invalid_attempts(tmp_path: Path, capsys):
    cfg_path = tmp_path / "config.ini"
    with (
        patch("builtins.input", return_value="n"),
        patch(
            "nyxgpt.secrets_setup.getpass.getpass",
            side_effect=[
                "a-manual-auth-key",
                "sk-" + "h" * 30,
                "bad1-but-long-enough-value",
                "bad2-but-long-enough-value",
                "bad3-but-long-enough-value",
            ],
        ),
    ):
        rc = run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert rc == 0
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("github", "pat", fallback="") == ""
    assert "skipping" in capsys.readouterr().out.lower()


def test_run_secrets_setup_never_prints_secret_values(tmp_path: Path, capsys):
    cfg_path = tmp_path / "config.ini"
    secret_value = "sk-" + "i" * 30
    with (
        patch("builtins.input", return_value="n"),
        patch(
            "nyxgpt.secrets_setup.getpass.getpass",
            side_effect=["a-manual-auth-key", secret_value, ""],
        ),
    ):
        run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    assert secret_value not in capsys.readouterr().out


def test_run_secrets_setup_writes_config_with_0600_permissions(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    with (
        patch("builtins.input", return_value=""),
        patch("nyxgpt.secrets_setup.getpass.getpass", return_value=""),
    ):
        run_secrets_setup(cfg_path=cfg_path, reconfigure=False)

    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600


def test_run_secrets_setup_exits_cleanly_on_keyboard_interrupt(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    with (
        patch("builtins.input", return_value="n"),
        patch("nyxgpt.secrets_setup.getpass.getpass", side_effect=KeyboardInterrupt),
        pytest.raises(SystemExit) as exc_info,
    ):
        run_secrets_setup(cfg_path=cfg_path, reconfigure=False)
    assert exc_info.value.code == 0


def test_read_config_returns_empty_parser_when_file_missing(tmp_path: Path):
    cfg = secrets_setup._read_config(tmp_path / "does-not-exist.ini")
    assert cfg.sections() == []
