"""Unit tests for error tracking (nyxgpt.error_tracking and config getters)."""

from __future__ import annotations

from configparser import ConfigParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nyxgpt import error_tracking
from nyxgpt.app import app
from nyxgpt.config import get_error_tracking_config, get_error_tracking_enabled

pytestmark = pytest.mark.unit


class _FakeScope:
    def __init__(self) -> None:
        self.extras: dict[str, Any] = {}

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value


class _FakeScopeCtx:
    def __init__(self, scope: _FakeScope) -> None:
        self._scope = scope

    def __enter__(self) -> _FakeScope:
        return self._scope

    def __exit__(self, *exc_info: Any) -> None:
        return None


def _cfg(**error_tracking_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if error_tracking_options:
        cfg["error_tracking"] = error_tracking_options
    return cfg


def test_get_error_tracking_enabled_defaults_to_false() -> None:
    assert get_error_tracking_enabled(_cfg()) is False


def test_get_error_tracking_enabled_reads_config() -> None:
    assert get_error_tracking_enabled(_cfg(enabled="true")) is True
    assert get_error_tracking_enabled(_cfg(enabled="false")) is False


def test_get_error_tracking_config_defaults() -> None:
    result = get_error_tracking_config(_cfg())

    assert result == {
        "enabled": False,
        "dsn": "",
        "environment": "development",
        "release": "",
        "traces_sample_rate": 0.0,
        "glitchtip_ui_url": "http://localhost:8080",
    }


def test_get_error_tracking_config_reads_overrides() -> None:
    cfg = _cfg(
        enabled="true",
        dsn="http://key@localhost:8080/1",
        environment="production",
        release="2.0.0",
        traces_sample_rate="0.25",
        glitchtip_ui_url="http://glitchtip:8080",
    )

    result = get_error_tracking_config(cfg)

    assert result["enabled"] is True
    assert result["dsn"] == "http://key@localhost:8080/1"
    assert result["environment"] == "production"
    assert result["release"] == "2.0.0"
    assert result["traces_sample_rate"] == 0.25
    assert result["glitchtip_ui_url"] == "http://glitchtip:8080"


def test_init_error_tracking_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_error_tracking must not touch the Sentry SDK when disabled --
    the safe default for every deployment unless an operator opts in."""
    monkeypatch.setattr(error_tracking, "_enabled", True)

    error_tracking.init_error_tracking({"enabled": False})

    assert error_tracking.is_error_tracking_enabled() is False


def test_init_error_tracking_is_noop_when_enabled_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with enabled = true, a missing DSN must keep tracking off --
    there is no default DSN, so nothing is ever reported anywhere by
    accident."""
    monkeypatch.setattr(error_tracking, "_enabled", True)
    init_calls = []
    monkeypatch.setattr(error_tracking.sentry_sdk, "init", lambda **kw: init_calls.append(kw))

    error_tracking.init_error_tracking({"enabled": True, "dsn": ""})

    assert error_tracking.is_error_tracking_enabled() is False
    assert init_calls == []


def test_init_error_tracking_enables_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", False)
    init_calls = []
    monkeypatch.setattr(error_tracking.sentry_sdk, "init", lambda **kw: init_calls.append(kw))

    error_tracking.init_error_tracking(
        {
            "enabled": True,
            "dsn": "http://key@localhost:8080/1",
            "environment": "production",
            "release": "2.0.0",
            "traces_sample_rate": 0.5,
        }
    )

    assert error_tracking.is_error_tracking_enabled() is True
    assert len(init_calls) == 1
    assert init_calls[0]["dsn"] == "http://key@localhost:8080/1"
    assert init_calls[0]["environment"] == "production"
    assert init_calls[0]["release"] == "2.0.0"
    assert init_calls[0]["traces_sample_rate"] == 0.5


def test_normalize_dsn_host_is_noop_natively(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a Compose container, "localhost" is already correct -- leave it alone."""
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)

    assert (
        error_tracking._normalize_dsn_host("http://key@localhost:8080/1")
        == "http://key@localhost:8080/1"
    )


def test_normalize_dsn_host_rewrites_localhost_in_compose_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the Compose api container, "localhost" means the api container
    itself, not the glitchtip container next to it -- rewrite to the Compose
    service name so a DSN copied verbatim from the GlitchTip UI still works."""
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")

    assert (
        error_tracking._normalize_dsn_host("http://key@localhost:8080/1")
        == "http://key@glitchtip:8080/1"
    )
    assert (
        error_tracking._normalize_dsn_host("http://key@127.0.0.1:8080/1")
        == "http://key@glitchtip:8080/1"
    )


def test_normalize_dsn_host_leaves_non_localhost_dsn_alone_in_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN that's already pointed at the Compose service (or anywhere else
    non-local) must be left untouched -- the rewrite is idempotent."""
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")

    assert (
        error_tracking._normalize_dsn_host("http://key@glitchtip:8080/1")
        == "http://key@glitchtip:8080/1"
    )


def test_init_error_tracking_normalizes_dsn_in_compose_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", False)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    init_calls = []
    monkeypatch.setattr(error_tracking.sentry_sdk, "init", lambda **kw: init_calls.append(kw))

    error_tracking.init_error_tracking({"enabled": True, "dsn": "http://key@localhost:8080/1"})

    assert error_tracking.is_error_tracking_enabled() is True
    assert init_calls[0]["dsn"] == "http://key@glitchtip:8080/1"


def test_capture_exception_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", False)
    captured = []
    monkeypatch.setattr(
        error_tracking.sentry_sdk, "capture_exception", lambda exc: captured.append(exc)
    )

    error_tracking.capture_exception(ValueError("boom"), request_id="abc")

    assert captured == []


def test_capture_exception_enabled_sets_context_and_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", True)
    fake_scope = _FakeScope()
    monkeypatch.setattr(
        error_tracking.sentry_sdk, "isolation_scope", lambda: _FakeScopeCtx(fake_scope)
    )
    captured = []
    monkeypatch.setattr(
        error_tracking.sentry_sdk, "capture_exception", lambda exc: captured.append(exc)
    )

    exc = ValueError("boom")
    error_tracking.capture_exception(exc, request_id="abc", path="/api/v1/chat")

    assert captured == [exc]
    assert fake_scope.extras == {"request_id": "abc", "path": "/api/v1/chat"}


def test_capture_message_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", False)
    captured = []
    monkeypatch.setattr(
        error_tracking.sentry_sdk,
        "capture_message",
        lambda message, level=None: captured.append((message, level)),
    )

    error_tracking.capture_message("client error", source="web-client")

    assert captured == []


def test_error_tracking_status_endpoint_reports_disabled_by_default() -> None:
    """The test config fixture has no [error_tracking] section, so the
    endpoint must report the safe default: disabled, not active."""
    client = TestClient(app)

    response = client.get("/api/v1/error-tracking")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["glitchtip_ui_url"] == "http://localhost:8080"


def test_error_tracking_status_endpoint_reports_active_when_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", True)
    client = TestClient(app)

    response = client.get("/api/v1/error-tracking")

    assert response.status_code == 200
    assert response.json()["active"] is True


def test_error_tracking_report_endpoint_signals_inactive_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured/disabled tracker must not look like a delivered event --
    the endpoint has to signal inactivity distinctly rather than a blanket 202."""
    monkeypatch.setattr(error_tracking, "_enabled", False)
    client = TestClient(app)

    response = client.post(
        "/api/v1/error-tracking/report",
        json={"message": "TypeError: boom", "stack": "at foo (bar.js:1:1)", "url": "/chat"},
    )

    assert response.status_code == 503
    assert response.json()["status"] == "inactive"


def test_error_tracking_report_endpoint_forwards_to_tracker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(error_tracking, "_enabled", True)
    captured = []
    monkeypatch.setattr(
        error_tracking.sentry_sdk,
        "isolation_scope",
        lambda: _FakeScopeCtx(_FakeScope()),
    )
    monkeypatch.setattr(
        error_tracking.sentry_sdk,
        "capture_message",
        lambda message, level=None: captured.append((message, level)),
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/error-tracking/report",
        json={"message": "TypeError: boom"},
    )

    assert response.status_code == 202
    assert captured == [("TypeError: boom", "error")]
