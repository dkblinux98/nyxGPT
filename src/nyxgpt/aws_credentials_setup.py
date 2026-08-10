"""Guided AWS credential collection for cloud deploy (P6-13, #3512).

Extends #3505's guided-secrets pattern (masked entry, plain-language
what-it-is/where-to-get-it help, one definition shared by the CLI and the
`/admin` wizard) to the AWS identity nyxGPT itself needs to call the AWS
API: `nyxgpt cloud allow-ip`, the eventual Terraform-driven deploy flow, and
#3507's `cloud_secrets.resolve_secret` all go through boto3's normal
credential resolution.

Unlike `secrets_setup.py`'s `GUIDED_SECRETS`, the access key ID / secret
access key collected here are **never** written to `config.ini` (this
issue's Definition of Done) -- boto3 already reads credentials from a few
well-known places, so this module routes an entered value to one of those
instead of inventing a new one nyxGPT would have to teach every AWS call
site to check:

- ``profile`` (default): writes to ``~/.aws/credentials`` under a named
  profile -- byte-for-byte what ``aws configure --profile <name>`` would
  produce. boto3 and the ``aws`` CLI both read this file natively once
  ``AWS_PROFILE``/``--profile``/``[cloud] profile`` names it.
- ``keychain``: stores the pair in the OS keychain via the optional
  ``keyring`` dependency (``pip install nyxgpt[cloud]``), for an operator
  who would rather not have a plaintext credentials file on disk at all.
- ``ambient``: credentials are already available some other way nyxGPT
  doesn't manage -- an existing profile, an EC2 instance role, an SSO
  session, or environment variables. Nothing is written; this just records
  which profile/region the rest of nyxGPT should pass to boto3.

Only the *reference* -- profile name, region, and which destination was
used -- is non-secret and lives in `config.ini`'s new `[cloud]` section
(excluded from the general schema-driven wizard in `config_wizard.py`, like
`openai`/`github`, because it has this module's dedicated flow instead).

This module also carries the guided flow for #3507's `[secrets]` section:
the AWS SSM/Secrets Manager *reference* (provider/region/prefix/id) used to
resolve nyxGPT's own application secrets on a cloud deploy. Those fields
aren't secret values themselves -- the actual application secrets stay in
SSM/Secrets Manager -- so they're validated and written the same way as
every other setting, just grouped into this flow rather than requiring a
detour through the general config wizard to finish a cloud-deploy setup.
"""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import cloud_secrets, config_wizard
from nyxgpt.config import DEFAULT_CONFIG_PATH
from nyxgpt.optional_imports import try_import
from nyxgpt.secrets_setup import SecretValidationError, mask_secret

AWS_DIR = Path.home() / ".aws"
AWS_CREDENTIALS_FILE = AWS_DIR / "credentials"

# Namespaced service name under which keychain entries are stored via
# `keyring` -- distinguishes nyxGPT's own entries from anything else an
# operator's OS keychain might hold.
KEYCHAIN_SERVICE = "nyxgpt-aws"

DESTINATION_PROFILE = "profile"
DESTINATION_KEYCHAIN = "keychain"
DESTINATION_AMBIENT = "ambient"
DESTINATIONS = (DESTINATION_PROFILE, DESTINATION_KEYCHAIN, DESTINATION_AMBIENT)

DEFAULT_PROFILE_NAME = "nyxgpt"
DEFAULT_REGION = "us-east-1"


class AwsCredentialsError(RuntimeError):
    """Raised for an operational failure (missing dependency, file I/O) -- not a validation error."""


def _read_config(cfg_path: Path) -> ConfigParser:
    """Read `cfg_path` into a plain `ConfigParser`, mirroring `secrets_setup._read_config`.

    Avoids `nyxgpt.config.load_config`'s global hot-reload cache and startup
    validation, both wrong for a setup flow that may run against a
    `config.ini` still being built up, or a non-default `--config` path.
    """
    parser = ConfigParser()
    if cfg_path.exists():
        parser.read(cfg_path, encoding="utf-8")
    return parser


def _validate_nonempty(value: str) -> str:
    """Require a non-empty, whitespace-free value (the common case)."""
    v = value.strip()
    if not v:
        raise SecretValidationError("must not be empty")
    if any(c.isspace() for c in v):
        raise SecretValidationError("must not contain whitespace")
    return v


def _validate_optional_str(value: str) -> str:
    """Accept any string, including blank -- used for optional reference fields."""
    return value.strip()


def _validate_access_key_id(value: str) -> str:
    """Validate an AWS access key id's shape: non-empty, no whitespace, 16-32 alphanumeric chars."""
    v = _validate_nonempty(value)
    if not (16 <= len(v) <= 32) or not v.isalnum():
        raise SecretValidationError(
            "doesn't look like an AWS access key id (expected 16-32 alphanumeric "
            "characters, e.g. starting with AKIA)"
        )
    return v


def _validate_secret_access_key(value: str) -> str:
    """Validate an AWS secret access key's shape: non-empty, no whitespace, plausibly long."""
    v = _validate_nonempty(value)
    if len(v) < 30:
        raise SecretValidationError("doesn't look like an AWS secret access key (too short)")
    return v


_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$")


def _validate_region(value: str) -> str:
    """Validate an AWS region's shape, e.g. `us-east-1` or `us-gov-west-1`."""
    v = _validate_nonempty(value)
    if not _REGION_RE.fullmatch(v):
        raise SecretValidationError("doesn't look like an AWS region (expected e.g. us-east-1)")
    return v


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_profile_name(value: str) -> str:
    """Validate an AWS CLI profile name: non-empty, letters/digits/`_`/`.`/`-` only."""
    v = _validate_nonempty(value)
    if not _PROFILE_NAME_RE.fullmatch(v):
        raise SecretValidationError("must contain only letters, digits, '_', '.', or '-'")
    return v


def _validate_secret_store_provider(value: str) -> str:
    """Validate `[secrets] provider`: blank, `ssm`, or `secretsmanager`."""
    v = value.strip()
    if v and v not in (cloud_secrets.SSM_PROVIDER, cloud_secrets.SECRETS_MANAGER_PROVIDER):
        raise SecretValidationError(
            f"must be blank, {cloud_secrets.SSM_PROVIDER!r}, or "
            f"{cloud_secrets.SECRETS_MANAGER_PROVIDER!r}"
        )
    return v


@dataclass(frozen=True)
class AwsCredentialField:
    """One field in the guided AWS credential flow.

    Attributes:
        key: Identifier used in API payloads (`profile`, `region`,
            `access_key_id`, `secret_access_key`).
        label: Plain-language name shown to the user.
        description: What this field is for.
        obtain: Where to get it / how to pick it.
        validator: Parses/validates raw input, raising `SecretValidationError`.
        secret: Whether this value must be masked on entry and never
            round-tripped to a client (true for the key pair, false for
            profile/region).
    """

    key: str
    label: str
    description: str
    obtain: str
    validator: Callable[[str], str]
    secret: bool


AWS_CREDENTIAL_FIELDS: tuple[AwsCredentialField, ...] = (
    AwsCredentialField(
        key="profile",
        label="AWS profile name",
        description=(
            "The AWS CLI profile name nyxGPT uses for its own AWS API calls "
            "(cloud allow-ip, cloud deploy, SSM/Secrets Manager resolution)."
        ),
        obtain=(
            "Pick any name -- 'nyxgpt' is a fine default. Must match an existing "
            "profile if the destination below is 'ambient'."
        ),
        validator=_validate_profile_name,
        secret=False,
    ),
    AwsCredentialField(
        key="region",
        label="AWS region",
        description="Default region for nyxGPT's AWS API calls when not overridden per-command.",
        obtain=(
            "https://docs.aws.amazon.com/general/latest/gr/rande.html -- pick the "
            "region your resources are in, e.g. us-east-1."
        ),
        validator=_validate_region,
        secret=False,
    ),
    AwsCredentialField(
        key="access_key_id",
        label="AWS access key ID",
        description=(
            "Identifies the IAM user/role nyxGPT authenticates as. Only needed for "
            "the 'profile' and 'keychain' destinations -- 'ambient' credentials "
            "(instance role, SSO, environment variables) don't need one entered here."
        ),
        obtain=(
            "https://console.aws.amazon.com/iam -- IAM > Users > your user > "
            "Security credentials > Create access key."
        ),
        validator=_validate_access_key_id,
        secret=True,
    ),
    AwsCredentialField(
        key="secret_access_key",
        label="AWS secret access key",
        description="Paired with the access key ID above.",
        obtain=(
            "Shown once, alongside the access key ID, when you create it in the IAM "
            "console -- if lost, create a new key pair."
        ),
        validator=_validate_secret_access_key,
        secret=True,
    ),
)

_BY_KEY: dict[str, AwsCredentialField] = {f.key: f for f in AWS_CREDENTIAL_FIELDS}


@dataclass(frozen=True)
class SecretStoreReferenceField:
    """One field of #3507's `[secrets]` provider reference -- not a secret value itself."""

    key: str
    label: str
    description: str
    validator: Callable[[str], str]


SECRET_STORE_REFERENCE_FIELDS: tuple[SecretStoreReferenceField, ...] = (
    SecretStoreReferenceField(
        key="provider",
        label="Secret store provider",
        description=(
            "Blank for a local deploy ([auth]/[openai]/[github] read from config.ini "
            "as usual). 'ssm' or 'secretsmanager' on a cloud deploy so nyxGPT's own "
            "secrets are resolved from AWS instead -- see docs/cloud.md."
        ),
        validator=_validate_secret_store_provider,
    ),
    SecretStoreReferenceField(
        key="region",
        label="Secret store region",
        description=(
            "AWS region for SSM/Secrets Manager calls. Optional -- falls back to "
            "boto3's normal region resolution."
        ),
        validator=_validate_optional_str,
    ),
    SecretStoreReferenceField(
        key="ssm_prefix",
        label="SSM parameter prefix",
        description="Parameter path prefix when provider = 'ssm', e.g. /nyxgpt.",
        validator=_validate_optional_str,
    ),
    SecretStoreReferenceField(
        key="secretsmanager_id",
        label="Secrets Manager secret name",
        description="Secret name/ARN when provider = 'secretsmanager'.",
        validator=_validate_optional_str,
    ),
)


def field_metadata() -> list[dict[str, Any]]:
    """Return `AWS_CREDENTIAL_FIELDS` as JSON-serializable metadata (no validators)."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "description": f.description,
            "obtain": f.obtain,
            "secret": f.secret,
        }
        for f in AWS_CREDENTIAL_FIELDS
    ]


# --- config.ini [cloud] reference (profile/region/destination -- never the key pair) ---


def cloud_reference_status(cfg: ConfigParser) -> dict[str, str]:
    """Return the current `[cloud]` reference: profile, region, and credentials destination."""
    return {
        "profile": cfg.get("cloud", "profile", fallback="").strip(),
        "region": cfg.get("cloud", "region", fallback="").strip(),
        "credentials_source": cfg.get("cloud", "credentials_source", fallback="").strip(),
    }


def save_cloud_reference(
    cfg_path: Path, profile: str, region: str, credentials_source: str
) -> None:
    """Write the non-secret `[cloud]` reference to `config.ini` via the byte-preserving merge."""
    config_wizard.apply_updates(
        cfg_path,
        {"cloud": {"profile": profile, "region": region, "credentials_source": credentials_source}},
    )


# --- destination: AWS CLI profile file (~/.aws/credentials) ---


def _read_aws_credentials_file(path: Path) -> ConfigParser:
    """Read `path` (an AWS CLI-style credentials file) into a plain `ConfigParser`."""
    parser = ConfigParser()
    if path.exists():
        parser.read(path, encoding="utf-8")
    return parser


def write_profile_credentials(
    profile: str, access_key_id: str, secret_access_key: str, path: Path | None = None
) -> None:
    """Write/replace an access key pair under `[profile]` in `~/.aws/credentials`.

    Byte-for-byte equivalent to what ``aws configure --profile <profile>``
    would write -- boto3 and the `aws` CLI both read this file natively, so
    nyxGPT never needs its own credential-loading code path once this runs.
    Every other profile already in the file is left untouched.
    """
    if path is None:
        path = AWS_CREDENTIALS_FILE
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parser = _read_aws_credentials_file(path)
        if not parser.has_section(profile):
            parser.add_section(profile)
        parser.set(profile, "aws_access_key_id", access_key_id)
        parser.set(profile, "aws_secret_access_key", secret_access_key)
        with path.open("w", encoding="utf-8") as fh:
            parser.write(fh)
        path.chmod(0o600)
    except OSError as exc:
        raise AwsCredentialsError(f"Failed to write {path}: {exc}") from exc


def profile_credentials_status(profile: str, path: Path | None = None) -> dict[str, Any]:
    """Return whether `profile` has stored credentials in `~/.aws/credentials`, masked."""
    if path is None:
        path = AWS_CREDENTIALS_FILE
    parser = _read_aws_credentials_file(path)
    if not parser.has_section(profile):
        return {"set": False, "masked_access_key_id": None}
    access_key_id = parser.get(profile, "aws_access_key_id", fallback="").strip()
    return {
        "set": bool(access_key_id),
        "masked_access_key_id": mask_secret(access_key_id) if access_key_id else None,
    }


# --- destination: OS keychain (via the optional `keyring` dependency) ---


def _keyring_module() -> Any:
    """Return the imported `keyring` module, or None if it isn't installed."""
    return try_import("keyring")


def write_keychain_credentials(profile: str, access_key_id: str, secret_access_key: str) -> None:
    """Store an access key pair for `profile` in the OS keychain via `keyring`."""
    keyring = _keyring_module()
    if keyring is None:
        raise AwsCredentialsError(
            "keyring is required to store AWS credentials in the OS keychain. "
            "Install with `pip install nyxgpt[cloud]`."
        )
    try:
        keyring.set_password(KEYCHAIN_SERVICE, f"{profile}:access_key_id", access_key_id)
        keyring.set_password(KEYCHAIN_SERVICE, f"{profile}:secret_access_key", secret_access_key)
    except Exception as exc:
        raise AwsCredentialsError(f"Failed to write to the OS keychain: {exc}") from exc


def read_keychain_credentials(profile: str) -> tuple[str, str] | None:
    """Return `(access_key_id, secret_access_key)` for `profile` from the OS keychain, or None.

    Also returns None (rather than raising) if the `keyring` package is
    installed but has no usable backend at runtime -- e.g. a headless/CI
    environment with no OS keychain daemon. A status check (`keychain_credentials_status`,
    and transitively `GET /api/v1/config/aws-credentials`) must degrade to
    "nothing stored" in that case instead of crashing the request.
    """
    keyring = _keyring_module()
    if keyring is None:
        return None
    try:
        access_key_id = keyring.get_password(KEYCHAIN_SERVICE, f"{profile}:access_key_id")
        secret_access_key = keyring.get_password(KEYCHAIN_SERVICE, f"{profile}:secret_access_key")
    except Exception:
        return None
    if not access_key_id or not secret_access_key:
        return None
    return access_key_id, secret_access_key


def keychain_credentials_status(profile: str) -> dict[str, Any]:
    """Return whether `profile` has stored credentials in the OS keychain, masked."""
    available = _keyring_module() is not None
    creds = read_keychain_credentials(profile) if available else None
    if creds is None:
        return {"set": False, "masked_access_key_id": None, "available": available}
    access_key_id, _ = creds
    return {"set": True, "masked_access_key_id": mask_secret(access_key_id), "available": available}


# --- orchestration ---


def save_aws_credentials(
    cfg_path: Path,
    destination: str,
    profile: str,
    region: str,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
) -> None:
    """Validate and route AWS identity material to `destination`, never to config.ini.

    `destination` is one of `DESTINATIONS`. `profile`/`keychain` require
    `access_key_id`/`secret_access_key`; `ambient` doesn't touch either --
    the operator already has credentials available some other way.
    Regardless of destination, only the non-secret reference (profile name,
    region, and which destination was chosen) is written to config.ini's
    `[cloud]` section, so `cloud.py`/`cloud_secrets.py` can find it again.
    """
    if destination not in DESTINATIONS:
        raise SecretValidationError(f"destination must be one of {DESTINATIONS}")

    validated_profile = _validate_profile_name(profile)
    validated_region = _validate_region(region)

    if destination in (DESTINATION_PROFILE, DESTINATION_KEYCHAIN):
        if not access_key_id or not secret_access_key:
            raise SecretValidationError(
                f"access_key_id and secret_access_key are required for the {destination!r} destination"
            )
        validated_access_key_id = _validate_access_key_id(access_key_id)
        validated_secret_access_key = _validate_secret_access_key(secret_access_key)
        if destination == DESTINATION_PROFILE:
            write_profile_credentials(
                validated_profile, validated_access_key_id, validated_secret_access_key
            )
        else:
            write_keychain_credentials(
                validated_profile, validated_access_key_id, validated_secret_access_key
            )

    save_cloud_reference(cfg_path, validated_profile, validated_region, destination)


def aws_credentials_status(cfg: ConfigParser) -> dict[str, Any]:
    """Return the guided AWS credentials flow's full status: fields, reference, and storage state.

    Never returns cleartext -- shared by the CLI and
    `GET /api/v1/config/aws-credentials`.
    """
    reference = cloud_reference_status(cfg)
    profile = reference["profile"] or DEFAULT_PROFILE_NAME
    return {
        "fields": field_metadata(),
        "reference": reference,
        "profile_file_status": profile_credentials_status(profile),
        "keychain_status": keychain_credentials_status(profile),
        "secret_store": secret_store_reference_status(cfg),
    }


# --- secret-store reference (#3507's [secrets] section) ---


def secret_store_reference_status(cfg: ConfigParser) -> list[dict[str, Any]]:
    """Return `SECRET_STORE_REFERENCE_FIELDS` metadata plus each field's current value."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "description": f.description,
            "value": cfg.get("secrets", f.key, fallback="").strip(),
        }
        for f in SECRET_STORE_REFERENCE_FIELDS
    ]


def save_secret_store_reference(cfg_path: Path, values: dict[str, str]) -> dict[str, str]:
    """Validate and write `[secrets]` reference fields (#3507) -- provider/region/ssm_prefix/secretsmanager_id.

    These aren't secret values themselves -- the actual application secrets
    stay in SSM/Secrets Manager, resolved by `cloud_secrets.resolve_secret`
    at read time -- so they're safe for config.ini like every other setting.
    """
    validated: dict[str, str] = {}
    for field in SECRET_STORE_REFERENCE_FIELDS:
        raw = values.get(field.key, "")
        if not isinstance(raw, str):
            raise SecretValidationError(f"{field.key} must be a string")
        validated[field.key] = field.validator(raw)
    config_wizard.apply_updates(cfg_path, {"secrets": validated})
    return validated


# --- CLI ---


def _prompt_masked(prompt: str) -> str:
    """Prompt for masked input via `getpass`, handling Ctrl-D/Ctrl-C like the rest of the CLI."""
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n\nAWS credentials setup cancelled.")
        sys.exit(0)


def run_aws_credentials_setup(cfg_path: Path | None = None) -> int:
    """Run the interactive guided AWS credentials setup: `nyxgpt cloud credentials-setup`.

    Collects profile/region, then a destination for the access key pair
    (`~/.aws/credentials`, the OS keychain, or "already configured
    elsewhere"), and optionally the #3507 secret-store reference. Returns 0
    on success, 1 on a validation/operational error.
    """
    if cfg_path is None:
        cfg_path = DEFAULT_CONFIG_PATH

    print("=" * 60)
    print("nyxGPT Guided AWS Credentials Setup")
    print("=" * 60)
    print(f"\nReference (profile, region, destination) is written to: {cfg_path}")
    print("Access keys, if entered, are never written there -- see below.\n")

    cfg = _read_config(cfg_path)
    reference = cloud_reference_status(cfg)

    profile_default = reference["profile"] or DEFAULT_PROFILE_NAME
    profile_raw = input(f"AWS profile name [{profile_default}]: ").strip() or profile_default
    region_default = reference["region"] or DEFAULT_REGION
    region_raw = input(f"AWS region [{region_default}]: ").strip() or region_default

    print("\nHow should nyxGPT get AWS credentials?")
    print("  1) Enter an access key pair -- written to ~/.aws/credentials")
    print("  2) Enter an access key pair -- stored in the OS keychain instead of a file")
    print("  3) Already configured elsewhere (existing profile, instance role, SSO, env vars)")
    choice = input("Choice [1]: ").strip() or "1"
    destination = {
        "1": DESTINATION_PROFILE,
        "2": DESTINATION_KEYCHAIN,
        "3": DESTINATION_AMBIENT,
    }.get(choice)
    if destination is None:
        print(f"Invalid choice {choice!r} -- cancelled.")
        return 1

    access_key_id = None
    secret_access_key = None
    if destination in (DESTINATION_PROFILE, DESTINATION_KEYCHAIN):
        access_key_id = _prompt_masked(f"{_BY_KEY['access_key_id'].label}: ")
        secret_access_key = _prompt_masked(f"{_BY_KEY['secret_access_key'].label}: ")

    try:
        save_aws_credentials(
            cfg_path, destination, profile_raw, region_raw, access_key_id, secret_access_key
        )
    except (SecretValidationError, AwsCredentialsError) as e:
        print(f"Error: {e}")
        return 1

    print(f"\nSaved -- profile={profile_raw!r} region={region_raw!r} destination={destination!r}.")
    if destination == DESTINATION_PROFILE:
        print(f"Access key written to {AWS_CREDENTIALS_FILE} under [{profile_raw}].")
    elif destination == DESTINATION_KEYCHAIN:
        print("Access key stored in the OS keychain.")

    print("-" * 60)
    configure_store = input(
        "\nConfigure a cloud secret store for nyxGPT's own secrets too? (y/N): "
    )
    if configure_store.strip().lower() in ("y", "yes"):
        print("Leave blank to skip / keep the current value.\n")
        cfg = _read_config(cfg_path)
        values: dict[str, str] = {}
        for field in SECRET_STORE_REFERENCE_FIELDS:
            current = cfg.get("secrets", field.key, fallback="")
            print(f"{field.label}: {field.description}")
            entered = input(f"  [{current}]: ").strip()
            values[field.key] = entered or current
        try:
            save_secret_store_reference(cfg_path, values)
        except SecretValidationError as e:
            print(f"Error: {e}")
            return 1
        print("Secret store reference saved.")

    print("\nGuided AWS credentials setup complete.")
    return 0


__all__ = [
    "AWS_CREDENTIAL_FIELDS",
    "AWS_CREDENTIALS_FILE",
    "SECRET_STORE_REFERENCE_FIELDS",
    "DESTINATIONS",
    "DESTINATION_AMBIENT",
    "DESTINATION_KEYCHAIN",
    "DESTINATION_PROFILE",
    "AwsCredentialField",
    "AwsCredentialsError",
    "SecretStoreReferenceField",
    "aws_credentials_status",
    "cloud_reference_status",
    "field_metadata",
    "keychain_credentials_status",
    "profile_credentials_status",
    "read_keychain_credentials",
    "run_aws_credentials_setup",
    "save_aws_credentials",
    "save_cloud_reference",
    "save_secret_store_reference",
    "secret_store_reference_status",
    "write_keychain_credentials",
    "write_profile_credentials",
]
