"""Unit tests for distributed tracing (nyxgpt.tracing and config getters)."""

from __future__ import annotations

from configparser import ConfigParser

import pytest
from fastapi.testclient import TestClient

from nyxgpt import tracing
from nyxgpt.app import app
from nyxgpt.config import get_tracing_config, get_tracing_enabled

pytestmark = pytest.mark.unit


def _cfg(**tracing_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if tracing_options:
        cfg["tracing"] = tracing_options
    return cfg


def test_get_tracing_enabled_defaults_to_false() -> None:
    assert get_tracing_enabled(_cfg()) is False


def test_get_tracing_enabled_reads_config() -> None:
    assert get_tracing_enabled(_cfg(enabled="true")) is True
    assert get_tracing_enabled(_cfg(enabled="false")) is False


def test_get_tracing_config_defaults() -> None:
    result = get_tracing_config(_cfg())

    assert result == {
        "enabled": False,
        "service_name": "nyxgpt-api",
        "otlp_endpoint": "http://localhost:4318/v1/traces",
        "jaeger_ui_url": "http://localhost:16686",
    }


def test_get_tracing_config_reads_overrides() -> None:
    cfg = _cfg(
        enabled="true",
        service_name="my-service",
        otlp_endpoint="http://collector:4318/v1/traces",
        jaeger_ui_url="http://jaeger:16686",
    )

    result = get_tracing_config(cfg)

    assert result["enabled"] is True
    assert result["service_name"] == "my-service"
    assert result["otlp_endpoint"] == "http://collector:4318/v1/traces"
    assert result["jaeger_ui_url"] == "http://jaeger:16686"


def test_traced_span_is_noop_when_disabled() -> None:
    """With tracing disabled (the default), traced_span must not raise and
    must yield None -- callers shouldn't need to special-case it."""
    with tracing.traced_span("some.operation", key="value") as span:
        assert span is None


def test_traced_span_reraises_exceptions_when_disabled() -> None:
    with pytest.raises(ValueError), tracing.traced_span("some.operation"):
        raise ValueError("boom")


def test_traced_decorator_preserves_return_value_when_disabled() -> None:
    @tracing.traced("some.function")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_init_tracing_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_tracing must not touch the OTel SDK or instrument anything when
    the config says tracing is disabled (the default for every deployment
    unless the operator explicitly opts in)."""
    monkeypatch.setattr(tracing, "_enabled", True)

    tracing.init_tracing(app=None, tracing_config={"enabled": False})

    assert tracing.is_tracing_enabled() is False


def test_tracing_status_endpoint_reports_disabled_by_default() -> None:
    """The test config fixture has no [tracing] section, so the endpoint
    must report the safe default: disabled, not active."""
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["jaeger_ui_url"] == "http://localhost:16686"
