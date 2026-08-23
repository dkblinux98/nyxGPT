import json
from types import SimpleNamespace

import pytest

from nyxgpt import cloud_secrets


@pytest.fixture(autouse=True)
def _clear_cache():
    cloud_secrets.clear_cache()
    yield
    cloud_secrets.clear_cache()


class _FakeSSMClient:
    def __init__(self, params: dict[str, str]) -> None:
        self.params = params
        self.calls: list[dict] = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["Name"]
        if name not in self.params:
            raise RuntimeError(f"ParameterNotFound: {name}")
        return {"Parameter": {"Value": self.params[name]}}


class _FakeSecretsManagerClient:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets
        self.calls: list[dict] = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        secret_id = kwargs["SecretId"]
        if secret_id not in self.secrets:
            raise RuntimeError(f"ResourceNotFoundException: {secret_id}")
        return {"SecretString": self.secrets[secret_id]}


# --- fetch_ssm_parameter ----------------------------------------------------


def test_fetch_ssm_parameter_returns_value(monkeypatch):
    client = _FakeSSMClient({"/nyxgpt/auth_api_key": "sk-ssm-secret"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.fetch_ssm_parameter("/nyxgpt/auth_api_key")

    assert value == "sk-ssm-secret"
    assert client.calls == [{"Name": "/nyxgpt/auth_api_key", "WithDecryption": True}]


def test_fetch_ssm_parameter_wraps_client_errors(monkeypatch):
    client = _FakeSSMClient({})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError, match="ParameterNotFound"):
        cloud_secrets.fetch_ssm_parameter("/nyxgpt/missing")


def test_fetch_ssm_parameter_raises_when_value_missing(monkeypatch):
    class _NoValueClient:
        def get_parameter(self, **kwargs):
            return {"Parameter": {}}

    monkeypatch.setattr(
        cloud_secrets, "_get_boto3_client", lambda service, region, profile="": _NoValueClient()
    )

    with pytest.raises(cloud_secrets.CloudSecretsError, match="no Value"):
        cloud_secrets.fetch_ssm_parameter("/nyxgpt/auth_api_key")


# --- fetch_secretsmanager_key ------------------------------------------------


def test_fetch_secretsmanager_key_returns_value(monkeypatch):
    client = _FakeSecretsManagerClient({"nyxgpt": json.dumps({"auth_api_key": "sk-sm-secret"})})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.fetch_secretsmanager_key("nyxgpt", "auth_api_key")

    assert value == "sk-sm-secret"
    assert client.calls == [{"SecretId": "nyxgpt"}]


def test_fetch_secretsmanager_key_wraps_client_errors(monkeypatch):
    client = _FakeSecretsManagerClient({})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError, match="ResourceNotFoundException"):
        cloud_secrets.fetch_secretsmanager_key("nyxgpt", "auth_api_key")


def test_fetch_secretsmanager_key_raises_when_no_secret_string(monkeypatch):
    class _BinaryOnlyClient:
        def get_secret_value(self, **kwargs):
            return {"SecretBinary": b"\x00\x01"}

    monkeypatch.setattr(
        cloud_secrets, "_get_boto3_client", lambda service, region, profile="": _BinaryOnlyClient()
    )

    with pytest.raises(cloud_secrets.CloudSecretsError, match="no SecretString"):
        cloud_secrets.fetch_secretsmanager_key("nyxgpt", "auth_api_key")


def test_fetch_secretsmanager_key_raises_on_invalid_json(monkeypatch):
    client = _FakeSecretsManagerClient({"nyxgpt": "not-json"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError, match="not valid JSON"):
        cloud_secrets.fetch_secretsmanager_key("nyxgpt", "auth_api_key")


def test_fetch_secretsmanager_key_raises_when_key_missing(monkeypatch):
    client = _FakeSecretsManagerClient({"nyxgpt": json.dumps({"openai_api_key": "sk-x"})})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError, match="no key 'auth_api_key'"):
        cloud_secrets.fetch_secretsmanager_key("nyxgpt", "auth_api_key")


# --- resolve_secret -----------------------------------------------------------


def test_resolve_secret_dispatches_to_ssm(monkeypatch):
    client = _FakeSSMClient({"/nyxgpt/auth_api_key": "sk-ssm-secret"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert value == "sk-ssm-secret"


def test_resolve_secret_dispatches_to_secretsmanager(monkeypatch):
    client = _FakeSecretsManagerClient({"nyxgpt": json.dumps({"github_pat": "ghp-secret"})})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.resolve_secret(cloud_secrets.SECRETS_MANAGER_PROVIDER, "github_pat")

    assert value == "ghp-secret"


def test_resolve_secret_uses_custom_ssm_prefix(monkeypatch):
    client = _FakeSSMClient({"/custom/openai_api_key": "sk-custom"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.resolve_secret(
        cloud_secrets.SSM_PROVIDER, "openai_api_key", ssm_prefix="/custom"
    )

    assert value == "sk-custom"


def test_resolve_secret_uses_custom_secretsmanager_id(monkeypatch):
    client = _FakeSecretsManagerClient({"my-secret": json.dumps({"github_pat": "ghp-custom"})})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    value = cloud_secrets.resolve_secret(
        cloud_secrets.SECRETS_MANAGER_PROVIDER, "github_pat", secretsmanager_id="my-secret"
    )

    assert value == "ghp-custom"


def test_resolve_secret_rejects_unknown_provider():
    with pytest.raises(cloud_secrets.CloudSecretsError, match="Unknown \\[secrets\\] provider"):
        cloud_secrets.resolve_secret("vault", "auth_api_key")


def test_resolve_secret_caches_and_does_not_refetch(monkeypatch):
    client = _FakeSSMClient({"/nyxgpt/auth_api_key": "sk-ssm-secret"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    first = cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    second = cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert first == second == "sk-ssm-secret"
    assert len(client.calls) == 1


def test_resolve_secret_cache_expires(monkeypatch):
    client = _FakeSSMClient({"/nyxgpt/auth_api_key": "sk-ssm-secret"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    times = iter([100.0, 500.0, 500.0])
    monkeypatch.setattr(cloud_secrets.time, "monotonic", lambda: next(times))

    cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert len(client.calls) == 2


def test_resolve_secret_caches_failures_and_does_not_retry_immediately(monkeypatch):
    client = _FakeSSMClient({})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError, match="ParameterNotFound"):
        cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    with pytest.raises(cloud_secrets.CloudSecretsError, match="ParameterNotFound"):
        cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    # Second call hit the negative cache, not AWS again.
    assert len(client.calls) == 1


def test_resolve_secret_retries_after_negative_cache_expires(monkeypatch):
    client = _FakeSSMClient({})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    times = iter([100.0, 200.0, 200.0])
    monkeypatch.setattr(cloud_secrets.time, "monotonic", lambda: next(times))

    with pytest.raises(cloud_secrets.CloudSecretsError):
        cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    with pytest.raises(cloud_secrets.CloudSecretsError):
        cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert len(client.calls) == 2


def test_resolve_secret_success_after_failure_clears_negative_cache(monkeypatch):
    client = _FakeSSMClient({})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    with pytest.raises(cloud_secrets.CloudSecretsError):
        cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    client.params["/nyxgpt/auth_api_key"] = "sk-recovered"
    cloud_secrets.clear_cache()
    value = cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert value == "sk-recovered"
    # A subsequent call reuses the success cache rather than the (now stale) failure.
    value_again = cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    assert value_again == "sk-recovered"
    assert len(client.calls) == 2


def test_resolve_secret_rejects_unknown_provider_without_touching_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": calls.append(1))

    with pytest.raises(cloud_secrets.CloudSecretsError, match="Unknown \\[secrets\\] provider"):
        cloud_secrets.resolve_secret("vault", "auth_api_key")
    with pytest.raises(cloud_secrets.CloudSecretsError, match="Unknown \\[secrets\\] provider"):
        cloud_secrets.resolve_secret("vault", "auth_api_key")

    # Never touched AWS -- the unknown-provider check is a static config
    # error, independent of the success/failure caches.
    assert calls == []


def test_clear_cache_forces_refetch(monkeypatch):
    client = _FakeSSMClient({"/nyxgpt/auth_api_key": "sk-ssm-secret"})
    monkeypatch.setattr(cloud_secrets, "_get_boto3_client", lambda service, region, profile="": client)

    cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")
    cloud_secrets.clear_cache()
    cloud_secrets.resolve_secret(cloud_secrets.SSM_PROVIDER, "auth_api_key")

    assert len(client.calls) == 2


# --- _get_boto3_client ---------------------------------------------------------


def test_get_boto3_client_raises_helpful_error_when_boto3_missing(monkeypatch):
    monkeypatch.setattr(cloud_secrets, "try_import", lambda name: None)
    with pytest.raises(cloud_secrets.CloudSecretsError, match=r"nyxgpt\[cloud\]"):
        cloud_secrets._get_boto3_client("ssm", None)


def test_get_boto3_client_passes_region(monkeypatch):
    calls = []
    fake_boto3 = SimpleNamespace(client=lambda service, **kwargs: calls.append((service, kwargs)))
    monkeypatch.setattr(
        cloud_secrets, "try_import", lambda name: fake_boto3 if name == "boto3" else None
    )

    cloud_secrets._get_boto3_client("ssm", "us-east-1")

    assert calls == [("ssm", {"region_name": "us-east-1"})]
