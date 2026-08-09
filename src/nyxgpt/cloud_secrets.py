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

_cache: dict[str, tuple[float, str]] = {}


class CloudSecretsError(RuntimeError):
    """Raised when a configured cloud secrets provider fails to resolve a value."""


def clear_cache() -> None:
    """Clear the in-process resolved-secret cache (test helper, also useful after a manual rotation)."""
    _cache.clear()


def _cache_get(cache_key: str) -> str | None:
    """Return the cached value for `cache_key` if present and not yet expired, else `None`."""
    entry = _cache.get(cache_key)
    if entry is None:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        return None
    return value


def _get_boto3_client(service: str, region: str | None) -> Any:
    """Build a boto3 client for `service`, raising a clean error if boto3 isn't installed."""
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
        return boto3.client(service, **kwargs)
    except Exception as exc:
        raise CloudSecretsError(f"Failed to create an AWS {service} client: {exc}") from exc


def fetch_ssm_parameter(name: str, region: str | None = None) -> str:
    """Fetch a single SecureString/String parameter from SSM Parameter Store."""
    client = _get_boto3_client("ssm", region)
    try:
        response = client.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:
        raise CloudSecretsError(f"Failed to read SSM parameter {name!r}: {exc}") from exc
    value = response.get("Parameter", {}).get("Value")
    if value is None:
        raise CloudSecretsError(f"SSM parameter {name!r} has no Value")
    return str(value)


def fetch_secretsmanager_key(secret_id: str, key: str, region: str | None = None) -> str:
    """Fetch `key` out of a Secrets Manager secret storing a JSON object of key/value pairs.

    A single secret holds every cloud-sourced credential (`auth_api_key`,
    `openai_api_key`, `github_pat`) as one JSON blob -- Secrets Manager bills
    per secret, so one secret with several keys is the natural fit (unlike
    SSM, which is per-parameter and priced for that).
    """
    client = _get_boto3_client("secretsmanager", region)
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
) -> str:
    """Resolve `key` (e.g. `"auth_api_key"`) via the given AWS provider.

    `provider` is `SSM_PROVIDER` (one parameter per key, at
    `f"{ssm_prefix}/{key}"`) or `SECRETS_MANAGER_PROVIDER` (one JSON secret
    holding every key, at `secretsmanager_id`). Cached for
    `_CACHE_TTL_SECONDS` per `(provider, region, prefix/id, key)`. Raises
    `CloudSecretsError` on any failure -- callers decide how to degrade.
    """
    cache_key = f"{provider}:{region}:{ssm_prefix}:{secretsmanager_id}:{key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if provider == SSM_PROVIDER:
        value = fetch_ssm_parameter(f"{ssm_prefix.rstrip('/')}/{key}", region=region)
    elif provider == SECRETS_MANAGER_PROVIDER:
        value = fetch_secretsmanager_key(secretsmanager_id, key, region=region)
    else:
        raise CloudSecretsError(
            f"Unknown [secrets] provider {provider!r} -- expected "
            f"{SSM_PROVIDER!r} or {SECRETS_MANAGER_PROVIDER!r}"
        )

    _cache[cache_key] = (time.monotonic(), value)
    return value
