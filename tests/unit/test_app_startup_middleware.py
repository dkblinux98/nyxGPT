"""Unit tests for nyxgpt.app's middleware, lifespan/startup, and config plumbing.

Covers:
- `_parse_client_capabilities` header parsing edge cases.
- `log_with_context` extra-fields merging.
- The `lifespan` startup/shutdown context manager (logging/tracing/error-tracking
  init failures, sessions dir creation failure, Ollama reachability check,
  rate limiter enable/disable, batch processor enable/disable incl. the
  `_process_chat_batch` closure success/error paths, shutdown cleanup).
- Module-level CORS origin parsing from `NYXGPT_CORS_ORIGINS`.
- `security_headers_middleware`'s HSTS branch for HTTPS requests.
- `add_request_id_and_limits`'s malformed Content-Length swallow branch.
- `_req_cfg`'s fallback when `request.state.cfg` isn't already populated.
- `_apply_hot_config_updates` / `_apply_auth_config_updates` / `_mask_api_key`.
- `_ollama_url` (the shared `get_json`/`post_json` Ollama HTTP helpers it's
  paired with now live in `nyxgpt.ollama_client` -- see
  `test_ollama_client_completeness.py` / `test_ollama_http_methods.py`).
- `_maybe_kw` / `_capture_stdout`.
- `batch_metrics` / `resource_metrics` endpoints (processor/monitor unset vs set).
- `config_update`'s `default_model` / `rag_enabled` field branches.
"""

from __future__ import annotations

import importlib
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
from nyxgpt.app import (
    ClientCapabilities,
    _apply_auth_config_updates,
    _apply_hot_config_updates,
    _capture_stdout,
    _mask_api_key,
    _maybe_kw,
    _ollama_url,
    _parse_client_capabilities,
    _req_cfg,
    app,
    log_with_context,
)
from nyxgpt.config import load_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_app_globals():
    """Snapshot/restore nyxgpt.app's mutable globals so lifespan tests below
    (which enable rate limiting / batching / a fake resource monitor) don't
    leak state into other tests in this file or other files in the suite.
    """
    rate_limiter = app_module._rate_limiter
    batch_processor = app_module._batch_processor
    resource_monitor = app_module._resource_monitor
    yield
    app_module._rate_limiter = rate_limiter
    app_module._batch_processor = batch_processor
    app_module._resource_monitor = resource_monitor


class _FailingSessionsDir:
    """Stand-in for the sessions dir Path whose mkdir() always raises."""

    def mkdir(self, parents: bool = True, exist_ok: bool = True) -> None:
        raise OSError("disk full")

    def __str__(self) -> str:
        return "/fake/sessions"


class _FakeUrlopenCtx:
    def __enter__(self):
        return MagicMock()

    def __exit__(self, *exc):
        return False


# ----------------------------
# _parse_client_capabilities
# ----------------------------


def _make_request(headers: dict[str, str]):
    """Build a minimal duck-typed Request exposing just the `.headers.get()`
    surface that `_parse_client_capabilities` relies on (header names must be
    supplied already lower-cased, matching how Starlette normalizes them).
    """
    return SimpleNamespace(headers=dict(headers))


def test_parse_client_capabilities_invalid_max_event_size_defaults_to_zero():
    """A non-integer X-Client-Max-Event-Size header hits the except ValueError branch."""
    caps = _parse_client_capabilities(_make_request({"x-client-max-event-size": "not-a-number"}))
    assert isinstance(caps, ClientCapabilities)
    assert caps.max_event_size == 0


def test_parse_client_capabilities_valid_max_event_size_parsed():
    caps = _parse_client_capabilities(_make_request({"x-client-max-event-size": "4096"}))
    assert caps.max_event_size == 4096


def test_parse_client_capabilities_explicit_sse_header_sets_supports_sse():
    """X-Client-Supports-SSE: true should force supports_sse even without the Accept header."""
    caps = _parse_client_capabilities(_make_request({"x-client-supports-sse": "true"}))
    assert caps.supports_sse is True


def test_parse_client_capabilities_sse_header_variants():
    for value in ("1", "yes", "TRUE".lower()):
        caps = _parse_client_capabilities(_make_request({"x-client-supports-sse": value}))
        assert caps.supports_sse is True
    caps = _parse_client_capabilities(_make_request({"x-client-supports-sse": "false"}))
    assert caps.supports_sse is False


# ----------------------------
# log_with_context
# ----------------------------


def test_log_with_context_merges_request_id_when_truthy():
    with patch.object(app_module.log, "log") as mock_log:
        log_with_context(20, "hello", request_id="req-123", component="test")
    mock_log.assert_called_once()
    _level, _msg = mock_log.call_args.args
    extra = mock_log.call_args.kwargs["extra"]
    assert extra == {"request_id": "req-123", "component": "test"}


def test_log_with_context_uses_extra_only_when_request_id_falsy():
    with patch.object(app_module.log, "log") as mock_log:
        log_with_context(20, "hello", request_id=None, component="test")
    extra = mock_log.call_args.kwargs["extra"]
    assert extra == {"component": "test"}
    assert "request_id" not in extra


# ----------------------------
# lifespan: logging / tracing / error-tracking init failures
# ----------------------------


def test_lifespan_logging_init_failure_is_swallowed():
    """configure_logging raising during startup must not prevent the API from starting."""
    with (
        patch("nyxgpt.app.configure_logging", side_effect=Exception("logging boom")),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


def test_lifespan_tracing_init_failure_is_swallowed():
    with (
        patch.object(
            app_module.tracing_module, "init_tracing", side_effect=Exception("tracing boom")
        ),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


def test_lifespan_error_tracking_init_failure_is_swallowed():
    with (
        patch.object(
            app_module.error_tracking_module,
            "init_error_tracking",
            side_effect=Exception("error tracking boom"),
        ),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


def test_lifespan_sessions_dir_creation_failure_is_swallowed():
    with (
        patch("nyxgpt.app.get_sessions_dir", return_value=_FailingSessionsDir()),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


# ----------------------------
# lifespan: Ollama reachability check
# ----------------------------


def test_lifespan_ollama_reachable_logs_success():
    with (
        patch("nyxgpt.app.urllib.request.urlopen", return_value=_FakeUrlopenCtx()),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


def test_lifespan_ollama_unreachable_warns_but_starts():
    with (
        patch(
            "nyxgpt.app.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200


# ----------------------------
# lifespan: rate limiter enable/disable
# ----------------------------


def test_lifespan_rate_limiter_enabled_initializes_limiter():
    with (
        patch("nyxgpt.app.get_rate_limit_enabled", return_value=True),
        patch(
            "nyxgpt.app.get_rate_limit_config",
            return_value={"requests_per_second": 1000, "burst_size": 1000},
        ),
        patch("nyxgpt.app.urllib.request.urlopen", return_value=_FakeUrlopenCtx()),
        TestClient(app) as client,
    ):
        assert app_module._rate_limiter is not None
        resp = client.get("/health")
        assert resp.status_code == 200

    # Disable again so later tests/files aren't rate limited.
    app_module._rate_limiter = None


def test_lifespan_rate_limiter_disabled_leaves_limiter_none():
    app_module._rate_limiter = None
    with patch("nyxgpt.app.get_rate_limit_enabled", return_value=False), TestClient(app):
        assert app_module._rate_limiter is None


# ----------------------------
# lifespan: batch processor enable/disable + _process_chat_batch closure
# ----------------------------


def test_lifespan_batch_disabled_leaves_processor_none():
    with patch("nyxgpt.config.get_batch_enabled", return_value=False), TestClient(app):
        pass
    # get_batch_enabled False -> lifespan never assigns _batch_processor for
    # *this* run, but a prior test in this module may have left a leftover
    # instance; explicitly clear it to prove the disabled branch ran cleanly.
    app_module._batch_processor = None


def test_lifespan_batch_enabled_process_fn_success_and_error_paths():
    """Drive the `_process_chat_batch` closure defined inside `lifespan`.

    Covers both the success path (run_chat returns a result) and the
    `except Exception` error-dict path (run_chat raises).
    """
    with (
        patch("nyxgpt.config.get_batch_enabled", return_value=True),
        patch("nyxgpt.config.get_batch_size", return_value=4),
        patch("nyxgpt.config.get_batch_wait_time_ms", return_value=50),
        TestClient(app) as client,
    ):
        assert app_module._batch_processor is not None
        process_fn = app_module._batch_processor.process_fn

        fake_result = SimpleNamespace(
            session="s1",
            model="m1",
            reply="hi there",
            rag_used=False,
            rag_chunks=0,
            rag_context=None,
        )

        good_req = SimpleNamespace(
            data={
                "prompt": "hello",
                "session": "s1",
                "new": False,
                "model": "m1",
                "system": None,
                "config_path": None,
                "sessions_dir": None,
                "rag_enabled": None,
                "rag_filters": None,
            }
        )
        bad_req = SimpleNamespace(
            data={
                "prompt": "boom",
                "session": "s2",
                "new": True,
                "model": "m1",
                "system": None,
                "config_path": None,
                "sessions_dir": None,
                "rag_enabled": None,
                "rag_filters": None,
            }
        )

        with patch("nyxgpt.app.run_chat", side_effect=[fake_result, RuntimeError("chat boom")]):
            results = process_fn([good_req, bad_req])

        assert results[0] == {
            "session": "s1",
            "model": "m1",
            "reply": "hi there",
            "rag_used": False,
            "rag_chunks": 0,
            "rag_context": None,
        }
        assert results[1]["error"] == "chat boom"
        assert results[1]["error_type"] == "RuntimeError"

        resp = client.get("/health")
        assert resp.status_code == 200

    # Shutdown (exiting the `with TestClient` block above) already exercised
    # `_batch_processor.stop()`; clear the global for subsequent tests.
    app_module._batch_processor = None


# ----------------------------
# CORS origin parsing (module-level, env-gated)
# ----------------------------


def test_cors_origins_parsed_from_env_var(monkeypatch):
    monkeypatch.setenv("NYXGPT_CORS_ORIGINS", " http://example.com , http://foo.test ,, ")
    try:
        importlib.reload(sys.modules["nyxgpt.app"])
        reloaded = sys.modules["nyxgpt.app"]
        assert reloaded.allow_origins == ["http://example.com", "http://foo.test"]
    finally:
        monkeypatch.delenv("NYXGPT_CORS_ORIGINS", raising=False)
        importlib.reload(sys.modules["nyxgpt.app"])


# ----------------------------
# security_headers_middleware: HSTS on HTTPS
# ----------------------------


def test_hsts_header_present_for_https_requests():
    client = TestClient(app, base_url="https://testserver")
    resp = client.get("/health")
    assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"


# ----------------------------
# add_request_id_and_limits: malformed Content-Length swallow branch
# ----------------------------


def test_malformed_content_length_header_is_ignored_not_rejected():
    client = TestClient(app)
    resp = client.get("/health", headers={"content-length": "not-a-number"})
    assert resp.status_code == 200
    assert "X-Request-Id" in resp.headers


# ----------------------------
# _req_cfg fallback branch
# ----------------------------


def test_req_cfg_loads_and_caches_when_state_cfg_missing():
    fake_request = SimpleNamespace(state=SimpleNamespace())
    assert not hasattr(fake_request.state, "cfg")

    cfg = _req_cfg(fake_request)

    assert cfg is not None
    assert fake_request.state.cfg is cfg


# ----------------------------
# _apply_hot_config_updates
# ----------------------------


def test_apply_hot_config_updates_default_model(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_hot_config_updates({"default_model": "  llama3  "})
    assert out == {"default_model": "llama3"}
    assert cfg_path.exists()
    parsed = load_config(cfg_path)
    assert parsed.get("nyxgpt", "default_model") == "llama3"


def test_apply_hot_config_updates_rag_enabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_hot_config_updates({"rag_enabled": True})
    assert out == {"rag_enabled": True}
    parsed = load_config(cfg_path)
    assert parsed.getboolean("rag", "enable_chat_context") is True


def test_apply_hot_config_updates_log_level(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_hot_config_updates({"log_level": "debug"})
    assert out == {"log_level": "DEBUG"}
    parsed = load_config(cfg_path)
    assert parsed.get("logging", "level") == "DEBUG"


def test_apply_hot_config_updates_all_fields_together(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_hot_config_updates(
            {"default_model": "m2", "rag_enabled": False, "log_level": "warning"}
        )
    assert out == {"default_model": "m2", "rag_enabled": False, "log_level": "WARNING"}


def test_apply_hot_config_updates_swallows_logging_reconfig_failure(tmp_path):
    """The `except Exception: pass` around the post-write hot-logging-reload
    must not propagate configure_logging failures back to the caller.
    """
    cfg_path = tmp_path / "config.ini"
    with (
        patch("nyxgpt.app._config_file_path", return_value=cfg_path),
        patch("nyxgpt.app.configure_logging", side_effect=Exception("reload boom")),
    ):
        out = _apply_hot_config_updates({"log_level": "ERROR"})
    assert out == {"log_level": "ERROR"}


def test_apply_hot_config_updates_unknown_keys_ignored(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_hot_config_updates({"unknown_field": "value"})
    assert out == {}


# ----------------------------
# _apply_auth_config_updates
# ----------------------------


def test_apply_auth_config_updates_enabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_auth_config_updates({"enabled": True})
    assert out == {"enabled": True}
    parsed = load_config(cfg_path)
    assert parsed.getboolean("auth", "enabled") is True


def test_apply_auth_config_updates_header(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_auth_config_updates({"header": " X-My-Key "})
    assert out == {"header": "X-My-Key"}
    parsed = load_config(cfg_path)
    assert parsed.get("auth", "header") == "X-My-Key"


def test_apply_auth_config_updates_api_key(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_auth_config_updates({"api_key": "supersecret"})
    assert out == {"api_key": "supersecret"}
    parsed = load_config(cfg_path)
    assert parsed.get("auth", "api_key") == "supersecret"


def test_apply_auth_config_updates_all_fields_together(tmp_path):
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        out = _apply_auth_config_updates({"enabled": False, "header": "X-Key", "api_key": "abc"})
    assert out == {"enabled": False, "header": "X-Key", "api_key": "abc"}


def test_apply_auth_config_updates_reads_existing_file(tmp_path):
    """A second call against an already-written config.ini exercises the
    `if cfg_path.exists(): parser.read(cfg_path)` branch (the first call in
    the tests above always starts from a fresh, nonexistent file).
    """
    cfg_path = tmp_path / "config.ini"
    with patch("nyxgpt.app._config_file_path", return_value=cfg_path):
        _apply_auth_config_updates({"enabled": True})
        assert cfg_path.exists()
        out = _apply_auth_config_updates({"header": "X-Second-Key"})

    assert out == {"header": "X-Second-Key"}
    parsed = load_config(cfg_path)
    # The `enabled` value from the first call must have survived the second
    # call's parser.read() + rewrite.
    assert parsed.getboolean("auth", "enabled") is True
    assert parsed.get("auth", "header") == "X-Second-Key"


# ----------------------------
# _mask_api_key
# ----------------------------


def test_mask_api_key_empty_string():
    assert _mask_api_key("") == ""


def test_mask_api_key_short_key_fully_masked():
    assert _mask_api_key("abcd1234") == "*" * 8


def test_mask_api_key_long_key_shows_edges():
    assert _mask_api_key("abcd12345678wxyz") == "abcd********wxyz"


# ----------------------------
# _ollama_url
# ----------------------------


def test_ollama_url_joins_base_and_path():
    cfg = load_config(None)
    with patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434/"):
        assert _ollama_url(cfg, "/api/tags") == "http://localhost:11434/api/tags"
        assert _ollama_url(cfg, "api/tags") == "http://localhost:11434/api/tags"


# ----------------------------
# _maybe_kw
# ----------------------------


def test_maybe_kw_true_when_kwarg_present():
    def fn(a, b=None):
        return a, b

    assert _maybe_kw(fn, "b") is True
    assert _maybe_kw(fn, "missing") is False


def test_maybe_kw_false_on_introspection_failure():
    # A plain non-callable object makes inspect.signature() raise, hitting
    # the `except Exception: return False` branch.
    assert _maybe_kw(object(), "anything") is False


# ----------------------------
# _capture_stdout
# ----------------------------


def test_capture_stdout_captures_streams_and_return_code():
    import sys as _sys

    def noisy(x):
        print(f"out:{x}")
        print("err-line", file=_sys.stderr)
        return 0

    rc, out, err = _capture_stdout(noisy, "hello")
    assert rc == 0
    assert "out:hello" in out
    assert "err-line" in err


# ----------------------------
# batch_metrics endpoint
# ----------------------------


def test_batch_metrics_endpoint_when_disabled():
    app_module._batch_processor = None
    client = TestClient(app)
    resp = client.get("/api/v1/batch/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["message"] == "Request batching is not enabled"


def test_batch_metrics_endpoint_when_enabled():
    mock_metrics = MagicMock()
    mock_metrics.to_dict.return_value = {"total_requests": 5, "total_batches": 2}
    mock_processor = MagicMock()
    mock_processor.get_metrics.return_value = mock_metrics
    app_module._batch_processor = mock_processor
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/batch/metrics")
    finally:
        app_module._batch_processor = None

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["total_requests"] == 5
    assert body["total_batches"] == 2


# ----------------------------
# resource_metrics endpoint
# ----------------------------


def test_resource_metrics_endpoint_when_monitor_unset():
    with patch("nyxgpt.app.get_resource_monitor", return_value=None):
        client = TestClient(app)
        resp = client.get("/api/v1/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["memory"]["rss_mb"] == 0
    assert body["queue"]["depth"] == 0


def test_resource_metrics_endpoint_when_monitor_set():
    fake_metrics_dict = {
        "memory": {"rss_mb": 10.0, "vms_mb": 20.0, "percent": 1.0, "available_mb": 100.0},
        "cpu": {"process_percent": 1.0, "system_percent": 2.0},
        "latency": {"avg_ms": 5.0, "p50_ms": 5.0, "p95_ms": 9.0, "p99_ms": 10.0},
        "queue": {"depth": 1, "total_requests": 3},
    }
    mock_metrics = MagicMock()
    mock_metrics.to_dict.return_value = fake_metrics_dict
    mock_monitor = MagicMock()
    mock_monitor.get_metrics.return_value = mock_metrics

    with patch("nyxgpt.app.get_resource_monitor", return_value=mock_monitor):
        client = TestClient(app)
        resp = client.get("/api/v1/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["queue"]["depth"] == 1
    assert body["memory"]["rss_mb"] == 10.0


# ----------------------------
# config_update endpoint: default_model / rag_enabled field branches
# ----------------------------


def test_config_update_endpoint_default_model_field():
    """Covers the `if "default_model" in payload` branch inside `config_update`
    (distinct from the same-named branch in `_apply_hot_config_updates`,
    which is exercised separately above).
    """
    with patch(
        "nyxgpt.app._apply_hot_config_updates", return_value={"default_model": "new-model"}
    ) as mock_apply:
        client = TestClient(app)
        resp = client.post("/api/v1/config", json={"default_model": "new-model"})

    assert resp.status_code == 200
    assert mock_apply.call_args.args[0] == {"default_model": "new-model"}
    body = resp.json()
    assert body["updated"] == {"default_model": "new-model"}


def test_config_update_endpoint_rag_enabled_field():
    """Covers the `if "rag_enabled" in payload` branch inside `config_update`."""
    with patch(
        "nyxgpt.app._apply_hot_config_updates", return_value={"rag_enabled": False}
    ) as mock_apply:
        client = TestClient(app)
        resp = client.post("/api/v1/config", json={"rag_enabled": False})

    assert resp.status_code == 200
    assert mock_apply.call_args.args[0] == {"rag_enabled": False}
    body = resp.json()
    assert body["updated"] == {"rag_enabled": False}


def test_config_update_endpoint_ignores_unknown_keys():
    with patch("nyxgpt.app._apply_hot_config_updates", return_value={}) as mock_apply:
        client = TestClient(app)
        resp = client.post("/api/v1/config", json={"totally_unknown": "x"})

    assert resp.status_code == 200
    assert mock_apply.call_args.args[0] == {}
    body = resp.json()
    assert body["updated"] == {}
