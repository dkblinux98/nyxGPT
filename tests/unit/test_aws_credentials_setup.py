"""Tests for the guided AWS credentials setup flow (P6-13, #3512)."""

from __future__ import annotations

import stat
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import aws_credentials_setup
from nyxgpt.aws_credentials_setup import (
    AWS_CREDENTIAL_FIELDS,
    DESTINATION_AMBIENT,
    DESTINATION_KEYCHAIN,
    DESTINATION_PROFILE,
    SECRET_STORE_REFERENCE_FIELDS,
    AwsCredentialsError,
    aws_credentials_status,
    cloud_reference_status,
    field_metadata,
    keychain_credentials_status,
    profile_credentials_status,
    read_keychain_credentials,
    run_aws_credentials_setup,
    save_aws_credentials,
    save_cloud_reference,
    save_secret_store_reference,
    secret_store_reference_status,
    write_keychain_credentials,
    write_profile_credentials,
)
from nyxgpt.secrets_setup import SecretValidationError

pytestmark = pytest.mark.unit

VALID_ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"
VALID_SECRET_ACCESS_KEY = "s3cr3t" + "x" * 30


# --- fields / metadata ---


def test_aws_credential_fields_covers_identity_material():
    keys = {f.key for f in AWS_CREDENTIAL_FIELDS}
    assert keys == {"profile", "region", "access_key_id", "secret_access_key"}


def test_only_key_pair_fields_are_marked_secret():
    secret_flags = {f.key: f.secret for f in AWS_CREDENTIAL_FIELDS}
    assert secret_flags == {
        "profile": False,
        "region": False,
        "access_key_id": True,
        "secret_access_key": True,
    }


def test_field_metadata_excludes_validators():
    metadata = field_metadata()
    assert len(metadata) == len(AWS_CREDENTIAL_FIELDS)
    for entry in metadata:
        assert "validator" not in entry
        assert set(entry) == {"key", "label", "description", "obtain", "secret"}


def test_secret_store_reference_fields_match_the_secrets_section():
    assert {f.key for f in SECRET_STORE_REFERENCE_FIELDS} == {
        "provider",
        "region",
        "ssm_prefix",
        "secretsmanager_id",
    }


# --- validators (exercised via save_aws_credentials) ---


def test_save_aws_credentials_rejects_unknown_destination(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="destination must be one of"):
        save_aws_credentials(tmp_path / "config.ini", "s3", "nyxgpt", "us-east-1")


def test_save_aws_credentials_rejects_bad_region(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="region"):
        save_aws_credentials(
            tmp_path / "config.ini",
            DESTINATION_AMBIENT,
            "nyxgpt",
            "not-a-region",
        )


def test_save_aws_credentials_rejects_bad_profile_name(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="letters, digits"):
        save_aws_credentials(
            tmp_path / "config.ini",
            DESTINATION_AMBIENT,
            "bad/profile!",
            "us-east-1",
        )


def test_save_aws_credentials_rejects_profile_name_with_whitespace(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="whitespace"):
        save_aws_credentials(
            tmp_path / "config.ini",
            DESTINATION_AMBIENT,
            "has a space",
            "us-east-1",
        )


def test_save_aws_credentials_requires_key_pair_for_profile_destination(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="required for the 'profile' destination"):
        save_aws_credentials(tmp_path / "config.ini", DESTINATION_PROFILE, "nyxgpt", "us-east-1")


def test_save_aws_credentials_requires_key_pair_for_keychain_destination(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="required for the 'keychain' destination"):
        save_aws_credentials(tmp_path / "config.ini", DESTINATION_KEYCHAIN, "nyxgpt", "us-east-1")


def test_save_aws_credentials_rejects_malformed_access_key_id(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="access key id"):
        save_aws_credentials(
            tmp_path / "config.ini",
            DESTINATION_PROFILE,
            "nyxgpt",
            "us-east-1",
            access_key_id="short",
            secret_access_key=VALID_SECRET_ACCESS_KEY,
        )


def test_save_aws_credentials_rejects_malformed_secret_access_key(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="secret access key"):
        save_aws_credentials(
            tmp_path / "config.ini",
            DESTINATION_PROFILE,
            "nyxgpt",
            "us-east-1",
            access_key_id=VALID_ACCESS_KEY_ID,
            secret_access_key="short",
        )


# --- destination: profile (~/.aws/credentials) ---


def test_write_profile_credentials_creates_file_chmod_0600(tmp_path: Path):
    creds_path = tmp_path / "aws" / "credentials"

    write_profile_credentials(
        "nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY, path=creds_path
    )

    assert creds_path.exists()
    mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert mode == 0o600
    parser = ConfigParser()
    parser.read(creds_path)
    assert parser.get("nyxgpt", "aws_access_key_id") == VALID_ACCESS_KEY_ID
    assert parser.get("nyxgpt", "aws_secret_access_key") == VALID_SECRET_ACCESS_KEY


def test_write_profile_credentials_preserves_other_profiles(tmp_path: Path):
    creds_path = tmp_path / "credentials"
    creds_path.write_text(
        "[default]\naws_access_key_id = AKIAOTHERPROFILE000\naws_secret_access_key = other\n"
    )

    write_profile_credentials(
        "nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY, path=creds_path
    )

    parser = ConfigParser()
    parser.read(creds_path)
    assert parser.get("default", "aws_access_key_id") == "AKIAOTHERPROFILE000"
    assert parser.get("nyxgpt", "aws_access_key_id") == VALID_ACCESS_KEY_ID


def test_profile_credentials_status_unset_when_file_missing(tmp_path: Path):
    status = profile_credentials_status("nyxgpt", path=tmp_path / "does-not-exist")
    assert status == {"set": False, "masked_access_key_id": None}


def test_profile_credentials_status_masked_when_set(tmp_path: Path):
    creds_path = tmp_path / "credentials"
    write_profile_credentials(
        "nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY, path=creds_path
    )

    status = profile_credentials_status("nyxgpt", path=creds_path)

    assert status["set"] is True
    assert status["masked_access_key_id"] is not None
    assert VALID_ACCESS_KEY_ID not in status["masked_access_key_id"]


def test_save_aws_credentials_profile_destination_writes_file_and_reference(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    creds_path = tmp_path / "aws-credentials"

    with patch.object(aws_credentials_setup, "AWS_CREDENTIALS_FILE", creds_path):
        save_aws_credentials(
            cfg_path,
            DESTINATION_PROFILE,
            "nyxgpt",
            "us-east-1",
            access_key_id=VALID_ACCESS_KEY_ID,
            secret_access_key=VALID_SECRET_ACCESS_KEY,
        )

    assert creds_path.exists()
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("cloud", "profile") == "nyxgpt"
    assert cfg.get("cloud", "region") == "us-east-1"
    assert cfg.get("cloud", "credentials_source") == "profile"
    # The key pair itself must never land in config.ini.
    assert VALID_ACCESS_KEY_ID not in cfg_path.read_text()
    assert VALID_SECRET_ACCESS_KEY not in cfg_path.read_text()


# --- destination: keychain (via optional `keyring`) ---


class _FakeKeyring:
    """Minimal in-memory stand-in for the `keyring` module's password store."""

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))


class _FailingKeyring:
    """Stand-in for `keyring` when it's installed but has no usable backend (e.g. headless CI)."""

    def get_password(self, service, username):
        raise RuntimeError("No recommended backend was available.")

    def set_password(self, service, username, password):
        raise RuntimeError("No recommended backend was available.")


def test_write_keychain_credentials_raises_when_keyring_missing():
    with (
        patch.object(aws_credentials_setup, "_keyring_module", return_value=None),
        pytest.raises(AwsCredentialsError, match="keyring is required"),
    ):
        write_keychain_credentials("nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY)


def test_read_keychain_credentials_degrades_to_none_when_backend_unusable():
    with patch.object(aws_credentials_setup, "_keyring_module", return_value=_FailingKeyring()):
        assert read_keychain_credentials("nyxgpt") is None


def test_keychain_credentials_status_degrades_when_backend_unusable():
    with patch.object(aws_credentials_setup, "_keyring_module", return_value=_FailingKeyring()):
        status = keychain_credentials_status("nyxgpt")

    assert status["set"] is False
    assert status["masked_access_key_id"] is None


def test_write_and_read_keychain_credentials_roundtrip():
    fake = _FakeKeyring()
    with patch.object(aws_credentials_setup, "_keyring_module", return_value=fake):
        write_keychain_credentials("nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY)
        result = read_keychain_credentials("nyxgpt")

    assert result == (VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY)


def test_keychain_credentials_status_unavailable_when_keyring_missing():
    with patch.object(aws_credentials_setup, "_keyring_module", return_value=None):
        status = keychain_credentials_status("nyxgpt")

    assert status == {"set": False, "masked_access_key_id": None, "available": False}


def test_keychain_credentials_status_masked_when_set():
    fake = _FakeKeyring()
    with patch.object(aws_credentials_setup, "_keyring_module", return_value=fake):
        write_keychain_credentials("nyxgpt", VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY)
        status = keychain_credentials_status("nyxgpt")

    assert status["set"] is True
    assert status["available"] is True
    assert VALID_ACCESS_KEY_ID not in status["masked_access_key_id"]


def test_save_aws_credentials_keychain_destination_never_touches_credentials_file(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    creds_path = tmp_path / "aws-credentials"
    fake = _FakeKeyring()

    with (
        patch.object(aws_credentials_setup, "AWS_CREDENTIALS_FILE", creds_path),
        patch.object(aws_credentials_setup, "_keyring_module", return_value=fake),
    ):
        save_aws_credentials(
            cfg_path,
            DESTINATION_KEYCHAIN,
            "nyxgpt",
            "us-west-2",
            access_key_id=VALID_ACCESS_KEY_ID,
            secret_access_key=VALID_SECRET_ACCESS_KEY,
        )
        assert read_keychain_credentials("nyxgpt") == (VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY)

    assert not creds_path.exists()
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("cloud", "credentials_source") == "keychain"


# --- destination: ambient (nothing to write) ---


def test_save_aws_credentials_ambient_destination_writes_only_reference(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"

    save_aws_credentials(cfg_path, DESTINATION_AMBIENT, "nyxgpt", "eu-west-1")

    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("cloud", "credentials_source") == "ambient"
    assert cfg.get("cloud", "profile") == "nyxgpt"
    assert cfg.get("cloud", "region") == "eu-west-1"


# --- config.ini [cloud] reference ---


def test_cloud_reference_status_defaults_to_blank():
    cfg = ConfigParser()
    assert cloud_reference_status(cfg) == {"profile": "", "region": "", "credentials_source": ""}


def test_save_cloud_reference_preserves_other_config_content(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")

    save_cloud_reference(cfg_path, "nyxgpt", "us-east-1", "profile")

    text = cfg_path.read_text()
    assert "default_model = qwen2.5:0.5b" in text
    assert "profile = nyxgpt" in text


# --- secret store reference (#3507's [secrets] section) ---


def test_secret_store_reference_status_reports_current_values():
    cfg = ConfigParser()
    cfg.add_section("secrets")
    cfg.set("secrets", "provider", "ssm")
    cfg.set("secrets", "ssm_prefix", "/nyxgpt")

    status = secret_store_reference_status(cfg)

    by_key = {e["key"]: e["value"] for e in status}
    assert by_key["provider"] == "ssm"
    assert by_key["ssm_prefix"] == "/nyxgpt"
    assert by_key["region"] == ""


def test_save_secret_store_reference_rejects_unknown_provider(tmp_path: Path):
    with pytest.raises(SecretValidationError, match="must be blank"):
        save_secret_store_reference(tmp_path / "config.ini", {"provider": "vault"})


def test_save_secret_store_reference_writes_valid_values(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"

    save_secret_store_reference(
        cfg_path,
        {"provider": "secretsmanager", "region": "us-east-1", "secretsmanager_id": "nyxgpt-prod"},
    )

    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("secrets", "provider") == "secretsmanager"
    assert cfg.get("secrets", "secretsmanager_id") == "nyxgpt-prod"


def test_save_secret_store_reference_accepts_blank_provider(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    save_secret_store_reference(cfg_path, {})
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("secrets", "provider", fallback="") == ""


# --- aws_credentials_status (aggregate, never leaks cleartext) ---


def test_aws_credentials_status_never_leaks_cleartext(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    creds_path = tmp_path / "aws-credentials"
    with patch.object(aws_credentials_setup, "AWS_CREDENTIALS_FILE", creds_path):
        save_aws_credentials(
            cfg_path,
            DESTINATION_PROFILE,
            "nyxgpt",
            "us-east-1",
            access_key_id=VALID_ACCESS_KEY_ID,
            secret_access_key=VALID_SECRET_ACCESS_KEY,
        )
        cfg = ConfigParser()
        cfg.read(cfg_path)
        status = aws_credentials_status(cfg)

    assert VALID_ACCESS_KEY_ID not in str(status)
    assert VALID_SECRET_ACCESS_KEY not in str(status)
    assert status["reference"]["credentials_source"] == "profile"
    assert status["profile_file_status"]["set"] is True


# --- run_aws_credentials_setup (CLI flow) ---


def test_run_aws_credentials_setup_ambient_destination_writes_reference_only(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    inputs = iter(
        [
            "nyxgpt",  # profile
            "us-east-1",  # region
            "3",  # destination choice: ambient
            "n",  # decline secret-store configuration
        ]
    )
    with patch("builtins.input", side_effect=lambda *_: next(inputs)):
        rc = run_aws_credentials_setup(cfg_path=cfg_path)

    assert rc == 0
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("cloud", "credentials_source") == "ambient"


def test_run_aws_credentials_setup_profile_destination_prompts_masked_key_pair(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    creds_path = tmp_path / "aws-credentials"
    inputs = iter(
        [
            "nyxgpt",  # profile
            "us-east-1",  # region
            "1",  # destination choice: profile
            "n",  # decline secret-store configuration
        ]
    )
    with (
        patch.object(aws_credentials_setup, "AWS_CREDENTIALS_FILE", creds_path),
        patch("builtins.input", side_effect=lambda *_: next(inputs)),
        patch(
            "nyxgpt.aws_credentials_setup.getpass.getpass",
            side_effect=[VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY],
        ),
    ):
        rc = run_aws_credentials_setup(cfg_path=cfg_path)

    assert rc == 0
    assert creds_path.exists()
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("cloud", "credentials_source") == "profile"


def test_run_aws_credentials_setup_invalid_destination_choice_cancels(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    inputs = iter(["nyxgpt", "us-east-1", "9"])
    with patch("builtins.input", side_effect=lambda *_: next(inputs)):
        rc = run_aws_credentials_setup(cfg_path=cfg_path)

    assert rc == 1
    assert not cfg_path.exists()


def test_run_aws_credentials_setup_can_also_configure_secret_store(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    inputs = iter(
        [
            "nyxgpt",  # profile
            "us-east-1",  # region
            "3",  # destination choice: ambient
            "y",  # configure secret store
            "ssm",  # provider
            "",  # region (keep blank)
            "/nyxgpt",  # ssm_prefix
            "",  # secretsmanager_id
        ]
    )
    with patch("builtins.input", side_effect=lambda *_: next(inputs)):
        rc = run_aws_credentials_setup(cfg_path=cfg_path)

    assert rc == 0
    cfg = ConfigParser()
    cfg.read(cfg_path)
    assert cfg.get("secrets", "provider") == "ssm"
    assert cfg.get("secrets", "ssm_prefix") == "/nyxgpt"


def test_run_aws_credentials_setup_reports_operational_error(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    inputs = iter(["nyxgpt", "us-east-1", "2"])  # destination choice: keychain
    with (
        patch("builtins.input", side_effect=lambda *_: next(inputs)),
        patch(
            "nyxgpt.aws_credentials_setup.getpass.getpass",
            side_effect=[VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY],
        ),
        patch.object(aws_credentials_setup, "_keyring_module", return_value=None),
    ):
        rc = run_aws_credentials_setup(cfg_path=cfg_path)

    assert rc == 1
