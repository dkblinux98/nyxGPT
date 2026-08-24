"""AWS-backed secret resolution for cloud deploys (P6-10, #3507).

On a local deploy, `[auth] api_key` / `[openai] api_key` / `[github] pat`
are read straight out of `config.ini` -- see `config.py`'s getters. On a
cloud (AWS) deploy those values must never be baked into an AMI, user-data
script, tfvars file, or `config.ini` itself. This module fetches them at
read time from AWS SSM Parameter Store or Secrets Manager instead, so a
cloud instance can carry no plaintext credentials at all.

`config.py`'s `get_auth_api_key`/`get_openai_api_key`/`get_github_pat`
call `resolve_secret` when `[secrets] provider` is set; they fall back to
the local `config.ini` value (unaffected) when it isn't. See
`docs/cloud.md#cloud-secrets-ssm--secrets-manager` for the operator-facing
setup and rotation guide.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from nyxgpt.optional_imports import try_import

_logger = logging.getLogger(__name__)

SSM_PROVIDER = "ssm"
SECRETS_MANAGER_PROVIDER = "secretsmanager"

# How long a resolved value is reused before re-fetching. Long enough that a
# hot request path (e.g. the `[auth] api_key` check on every `/api/v1/*`
# request) doesn't call out to AWS per-request; short enough that a rotated
# secret takes effect without a process restart.
_CACHE_TTL_SECONDS = 300.0

# How long a *failed* resolution is remembered before retrying AWS. Much
# shorter than the success TTL (a fix should take effect quickly), but long
# enough that a sustained outage/misconfiguration (bad IAM, wrong prefix,
# missing boto3) degrades to "one AWS round trip every N seconds" instead of
# "one AWS round trip per caller" -- callers on a hot path (e.g. the
# `[auth] api_key` check on every `/api/v1/*` request) would otherwise retry
# unboundedly.
_NEGATIVE_CACHE_TTL_SECONDS = 30.0

_cache: dict[str, tuple[float, str]] = {}
_failure_cache: dict[str, tuple[float, str]] = {}


class CloudSecretsError(RuntimeError):
    """Raised when a configured cloud secrets provider fails to resolve a value."""


def clear_cache() -> None:
    """Clear the in-process resolved-secret cache (test helper, also useful after a manual rotation)."""
    _cache.clear()
    _failure_cache.clear()


def _cache_get(cache_key: str) -> str | None:
    """Return the cached value for `cache_key` if present and not yet expired, else `None`."""
    entry = _cache.get(cache_key)
    if entry is None:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        return None
    return value


def _failure_cache_get(cache_key: str) -> str | None:
    """Return the cached failure message for `cache_key` if present and not yet expired, else `None`."""
    entry = _failure_cache.get(cache_key)
    if entry is None:
        return None
    fetched_at, message = entry
    if time.monotonic() - fetched_at > _NEGATIVE_CACHE_TTL_SECONDS:
        return None
    return message


def _get_boto3_client(service: str, region: str | None, profile: str = "") -> Any:
    """Build a boto3 client for `service`, raising a clean error if boto3 isn't installed.

    `profile` is threaded in from the caller rather than read here (#3993):
    this module is imported by `nyxgpt.config` at module scope, so reading
    config.ini from inside it would close an import cycle -- and worse, these
    functions run *during* config resolution, so a `load_config()` here would
    recurse. `nyxgpt.config._resolve_cloud_secret` already holds the parsed
    config and passes the profile down; empty means boto3's default chain,
    which still honours `AWS_PROFILE`.
    """
    boto3 = try_import("boto3")
    if boto3 is None:
        raise CloudSecretsError(
            f"boto3 is required to resolve secrets from AWS {service}. "
            "Install with `pip install nyxgpt[cloud]`."
        )
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    try:
        # The bare `boto3.client` is kept for the no-profile case: it *is*
        # boto3's default session, and routing it through an explicit Session
        # would change nothing but the code path.
        if profile:
            return boto3.Session(profile_name=profile).client(service, **kwargs)
        return boto3.client(service, **kwargs)
    except Exception as exc:
        suffix = f" for profile {profile!r}" if profile else ""
        raise CloudSecretsError(f"Failed to create an AWS {service} client{suffix}: {exc}") from exc


def fetch_ssm_parameter(name: str, region: str | None = None, profile: str = "") -> str:
    """Fetch a single SecureString/String parameter from SSM Parameter Store."""
    client = _get_boto3_client("ssm", region, profile)
    try:
        response = client.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:
        raise CloudSecretsError(f"Failed to read SSM parameter {name!r}: {exc}") from exc
    value = response.get("Parameter", {}).get("Value")
    if value is None:
        raise CloudSecretsError(f"SSM parameter {name!r} has no Value")
    return str(value)


def fetch_secretsmanager_key(
    secret_id: str, key: str, region: str | None = None, profile: str = ""
) -> str:
    """Fetch `key` out of a Secrets Manager secret storing a JSON object of key/value pairs.

    A single secret holds every cloud-sourced credential (`auth_api_key`,
    `openai_api_key`, `github_pat`) as one JSON blob -- Secrets Manager bills
    per secret, so one secret with several keys is the natural fit (unlike
    SSM, which is per-parameter and priced for that).
    """
    client = _get_boto3_client("secretsmanager", region, profile)
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception as exc:
        raise CloudSecretsError(
            f"Failed to read Secrets Manager secret {secret_id!r}: {exc}"
        ) from exc
    raw = response.get("SecretString")
    if raw is None:
        raise CloudSecretsError(
            f"Secrets Manager secret {secret_id!r} has no SecretString "
            "(binary secrets aren't supported)"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CloudSecretsError(
            f"Secrets Manager secret {secret_id!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or key not in data:
        raise CloudSecretsError(f"Secrets Manager secret {secret_id!r} has no key {key!r}")
    return str(data[key])


def resolve_secret(
    provider: str,
    key: str,
    *,
    region: str | None = None,
    ssm_prefix: str = "/nyxgpt",
    secretsmanager_id: str = "nyxgpt",
    profile: str = "",
) -> str:
    """Resolve `key` (e.g. `"auth_api_key"`) via the given AWS provider.

    `provider` is `SSM_PROVIDER` (one parameter per key, at
    `f"{ssm_prefix}/{key}"`) or `SECRETS_MANAGER_PROVIDER` (one JSON secret
    holding every key, at `secretsmanager_id`). `profile` selects the AWS
    profile to authenticate with (#3993); empty means boto3's default chain.
    Cached for
    `_CACHE_TTL_SECONDS` per `(provider, region, profile, prefix/id, key)` --
    the profile is part of the key because the same parameter name in two
    accounts is two different secrets, and caching them together would serve
    one account's value for the other. Raises
    `CloudSecretsError` on any failure -- callers decide how to degrade. A
    fetch failure (unlike a success) is remembered for only
    `_NEGATIVE_CACHE_TTL_SECONDS`, so a sustained AWS-side failure still
    retries periodically rather than being cached for the full success TTL.
    """
    if provider not in (SSM_PROVIDER, SECRETS_MANAGER_PROVIDER):
        raise CloudSecretsError(
            f"Unknown [secrets] provider {provider!r} -- expected "
            f"{SSM_PROVIDER!r} or {SECRETS_MANAGER_PROVIDER!r}"
        )

    cache_key = f"{provider}:{region}:{profile}:{ssm_prefix}:{secretsmanager_id}:{key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    cached_failure = _failure_cache_get(cache_key)
    if cached_failure is not None:
        raise CloudSecretsError(cached_failure)

    try:
        if provider == SSM_PROVIDER:
            value = fetch_ssm_parameter(
                f"{ssm_prefix.rstrip('/')}/{key}", region=region, profile=profile
            )
        else:
            value = fetch_secretsmanager_key(secretsmanager_id, key, region=region, profile=profile)
    except CloudSecretsError as exc:
        _failure_cache[cache_key] = (time.monotonic(), str(exc))
        raise

    _cache[cache_key] = (time.monotonic(), value)
    _failure_cache.pop(cache_key, None)
    return value
