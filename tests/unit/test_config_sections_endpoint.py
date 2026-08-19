"""Unit tests for the full config wizard endpoints (#3354):

`GET|POST /api/v1/config/sections` and `POST /api/v1/config/restart`.

All tests redirect config.ini to a temp path via `nyxgpt.app._config_file_path`
so they never touch the real `~/.nyxGPT/config.ini`, and stub observability
reconciliation so a save cannot reach the host -- see
`_no_observability_reconciliation`, which exists because that second half was
missing and a single test cost three verification runs (#3947).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nyxgpt.app as app_module
import nyxgpt.config as config_module
import nyxgpt.restart_state as restart_state_module
from nyxgpt.app import app
from nyxgpt.ops import OpsResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_restart_state(tmp_path, monkeypatch):
    """Isolate the pending-restart state file and reset it around every test.

    Since #3806 this state is persisted to `~/.nyxGPT/pending-restart.json`
    (so the CLI writer and the API reader share one set), which means an
    un-redirected test would read and *delete* the developer's real file.
    `$NYXGPT_PENDING_RESTART_PATH` points it at `tmp_path` instead.
    """
    monkeypatch.setenv("NYXGPT_PENDING_RESTART_PATH", str(tmp_path / "pending-restart.json"))
    restart_state_module.reset()
    yield
    restart_state_module.reset()


@pytest.fixture
def captured_timers(monkeypatch):
    """Capture `threading.Timer`s scheduled by app.py instead of letting them fire.

    The restart endpoints defer their work onto a timer because the target may
    be this very process. A test that merely asserts "not called inline" and
    then exits its `patch` block leaves a *live* timer armed: when it fires,
    seconds later and in whatever test is running by then, it calls the real
    `ops.restart` -- which on a developer machine restarts actual services,
    and in CI silently corrupted an unrelated test's mock call counts. Capture
    them, assert the deferral, and never start them.
    """
    scheduled: list[tuple[float, Any]] = []

    class _FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args or ()

        def start(self):
            scheduled.append((self.interval, self.function, self.args))

    monkeypatch.setattr(app_module.threading, "Timer", _FakeTimer)
    return scheduled


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point config reads/writes at a temp file and reset the module cache.

    Pre-populated with a minimal `[ollama]` section so GET requests (which
    load config via the app's `load_cfg_and_refresh_logging` middleware
    before the handler runs) succeed even before any POST has written
    anything -- mirrors the minimal fixture config `tests/conftest.py`
    creates for the real `~/.nyxGPT/config.ini`.
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[ollama]\nbase_url = http://localhost:11434\n")
    monkeypatch.setattr(app_module, "_config_file_path", lambda: cfg_path)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", cfg_path)
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None
    yield cfg_path
    config_module._CACHED_CFG = None
    config_module._CACHED_PATH = None
    config_module._CACHED_MTIME_NS = None


@pytest.fixture(autouse=True)
def _no_observability_reconciliation(monkeypatch):
    """Keep a wizard save in this file from reaching the real host (#3947).

    `POST /config/sections` calls `ops.reconcile_observability` whenever the
    payload touches a monitoring/tracing/error_tracking/log_aggregation
    `enabled` flag, and that function is not a stub: it creates bind-mount
    directories, writes `.env` and runs `docker compose up` on whatever
    machine the test happens to be on. `_isolated_config` redirects
    `app._config_file_path`, but that redirect does not reach `ops`, which
    resolves `Path.home() / ".nyxGPT"` for itself.

    One test here posted `{"monitoring": {"enabled": True, ...}}` and so
    started the observability stack mid-suite, leaving Grafana crash-looping;
    the *next* Docker-touching unit test (`test_env_sync_generates_compose_config`)
    then waited two minutes on its health and failed. That failure looked
    transient and unrelated three separate times. The two tests that are
    genuinely *about* reconciliation patch this same attribute themselves and
    layer over this stub unchanged.
    """
    monkeypatch.setattr(
        app_module.ops_module,
        "reconcile_observability",
        lambda enable: [OpsResult(True, "stubbed: no host reconciliation in unit tests")],
    )


def test_get_sections_returns_schema_and_values(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    data = resp.json()
    assert "sections" in data
    assert "schema" in data
    assert "nyxgpt" in data["sections"]
    assert "api" in data["sections"]
    section_names = {s["section"] for s in data["schema"]}
    assert "rag" in section_names


def test_get_sections_returns_effective_values_and_field_defaults(_isolated_config):
    """No [tracing]/[error_tracking] section in the fixture config -- must show
    the runtime fallback values, flagged as defaults, not blanks (#3385)."""
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sections"]["tracing"]["service_name"] == "nyxgpt-api"
    assert data["sections"]["tracing"]["otlp_endpoint"] == "http://localhost:4318/v1/traces"
    assert data["sections"]["error_tracking"]["environment"] == "development"
    assert data["field_defaults"]["tracing"]["service_name"] is True
    assert data["field_defaults"]["error_tracking"]["environment"] is True


def test_post_sections_response_includes_field_defaults(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"tracing": {"service_name": "svc"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["field_defaults"]["tracing"]["service_name"] is False
    # Untouched fields in the same section remain flagged as defaults.
    assert data["field_defaults"]["tracing"]["otlp_endpoint"] is True


def test_post_sections_applies_and_returns_effective_values(_isolated_config):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/sections",
        json={"nyxgpt": {"default_model": "llama3:8b"}, "logging": {"level": "debug"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"]["nyxgpt"]["default_model"] == "llama3:8b"
    assert data["applied"]["logging"]["level"] == "DEBUG"
    assert data["sections"]["nyxgpt"]["default_model"] == "llama3:8b"
    assert data["restart_required"] == []
    assert data["observability_reconciled"] is False


def test_post_sections_reports_restart_required_for_api_port(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] == ["api"]


def test_post_sections_no_restart_when_value_unchanged(_isolated_config):
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    resp = client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert resp.json()["restart_required"] == []


def test_post_sections_rejects_invalid_payload(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"api": {"port": "not-a-port"}})
    assert resp.status_code == 422
    assert "errors" in resp.json()["error"]["details"]


def test_post_sections_rejects_empty_payload(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={})
    assert resp.status_code == 400


def test_post_sections_never_echoes_secret_value(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"auth": {"api_key": "topsecret123"}})
    assert resp.status_code == 200
    data = resp.json()
    assert "topsecret123" not in resp.text
    assert data["sections"]["auth"]["api_key"]["set"] is True
    assert data["applied"]["auth"]["api_key"] == {"set": True, "masked": "tops****t123"}


def test_get_sections_does_not_hand_the_slack_bot_token_to_the_browser(_isolated_config):
    """#3947: the wizard's GET is the surface the token actually leaked through.

    `[monitoring] slack_bot_token` was declared `secret=False`, so
    `read_sections` returned it verbatim -- into the page, into page state,
    and back out on every save. The classification guard lives in
    `test_wizard_secret_classification.py`; this asserts the HTTP response
    itself, because that is what a browser (or a proxy, or a log) sees.
    """
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n"
        "[monitoring]\nslack_bot_token = xoxb-000000-live-bot-token\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    assert "xoxb-000000-live-bot-token" not in resp.text
    assert resp.json()["sections"]["monitoring"]["slack_bot_token"]["set"] is True


def test_saving_without_retyping_the_slack_bot_token_keeps_it(_isolated_config):
    """The wizard posts "" for a secret the user didn't retype -- it must survive.

    Flipping a live field to `secret=True` changes what the browser sends;
    this is the half that proves an existing user's token is not blanked by a
    plain Save (#3947).

    The save deliberately edits `grafana_ui_url` rather than `enabled`:
    `enabled` carries `observability=True`, so posting it would call the real
    `ops.reconcile_observability` and start Docker on the test host. It proves
    the same thing -- a genuine write to the same section, alongside a blank
    secret -- without that. `_no_observability_reconciliation` guards the file
    as a whole; this keeps the test itself honest about what it exercises.
    """
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n"
        "[monitoring]\ngrafana_ui_url = http://localhost:3001\n"
        "slack_bot_token = xoxb-000000-live-bot-token\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/sections",
        json={"monitoring": {"grafana_ui_url": "http://localhost:3009", "slack_bot_token": ""}},
    )
    assert resp.status_code == 200
    assert resp.json()["observability_reconciled"] is False
    assert "xoxb-000000-live-bot-token" not in resp.text
    on_disk = _isolated_config.read_text(encoding="utf-8")
    # The save really happened...
    assert "http://localhost:3009" in on_disk
    # ...and the untyped secret came through it untouched.
    assert "xoxb-000000-live-bot-token" in on_disk


def test_post_sections_triggers_observability_reconciliation(_isolated_config):
    client = TestClient(app)
    with patch(
        "nyxgpt.app.ops_module.reconcile_observability",
        return_value=[OpsResult(True, "started")],
    ) as mock_reconcile:
        resp = client.post("/api/v1/config/sections", json={"tracing": {"enabled": True}})

    assert resp.status_code == 200
    data = resp.json()
    assert data["observability_reconciled"] is True
    assert data["observability_result"] == {"ok": True, "messages": ["started"]}
    mock_reconcile.assert_called_once_with(True)


def test_post_sections_reconciliation_failure_does_not_leak_exception(_isolated_config):
    # When reconciliation raises, the response must carry only a generic
    # message -- the raw exception detail is logged server-side but never
    # returned to the client (CodeQL py/stack-trace-exposure).
    client = TestClient(app)
    with patch(
        "nyxgpt.app.ops_module.reconcile_observability",
        side_effect=RuntimeError("docker socket permission denied at /var/run/docker.sock"),
    ):
        resp = client.post("/api/v1/config/sections", json={"tracing": {"enabled": True}})

    assert resp.status_code == 200
    data = resp.json()
    assert "docker socket permission denied" not in resp.text
    assert data["observability_result"] == {
        "ok": False,
        "messages": ["Observability reconciliation failed"],
    }


def test_post_sections_no_reconciliation_when_observability_field_unchanged(_isolated_config):
    client = TestClient(app)
    with patch("nyxgpt.app.ops_module.reconcile_observability") as mock_reconcile:
        resp = client.post("/api/v1/config/sections", json={"tracing": {"service_name": "svc"}})

    assert resp.status_code == 200
    assert resp.json()["observability_reconciled"] is False
    mock_reconcile.assert_not_called()


def test_restart_endpoint_schedules_and_returns_immediately(_isolated_config, captured_timers):
    client = TestClient(app)
    with patch("nyxgpt.app.ops_module.restart") as mock_restart:
        resp = client.post("/api/v1/config/restart", json={"target": "api"})
        assert resp.status_code == 200
        assert resp.json() == {"target": "api", "status": "scheduled"}
        # The restart call itself is deferred via threading.Timer, not called inline.
        mock_restart.assert_not_called()
    # Deferred, but genuinely scheduled -- and captured rather than armed.
    assert len(captured_timers) == 1
    assert captured_timers[0][0] > 0


def test_restart_endpoint_rejects_unknown_target(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/restart", json={"target": "not-a-real-target"})
    assert resp.status_code == 400


def test_restart_endpoint_defaults_to_all(_isolated_config, captured_timers):
    client = TestClient(app)
    resp = client.post("/api/v1/config/restart", json={})
    assert resp.status_code == 200
    assert resp.json()["target"] == "all"
    assert len(captured_timers) == 1


# --- Restart-pending tracking + mode-aware restart-required (#3407) ---


def test_post_sections_marks_restart_pending(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert resp.status_code == 200

    status = client.get("/api/v1/infra/restart-status")
    assert status.status_code == 200
    pending = status.json()["pending"]
    assert "api" in pending
    assert pending["api"]["keys"] == ["api.port"]
    assert isinstance(pending["api"]["since"], float)


def test_post_sections_accumulates_restart_pending_keys_across_saves(_isolated_config):
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    client.post("/api/v1/config/sections", json={"api": {"host": "0.0.0.0"}})

    pending = client.get("/api/v1/infra/restart-status").json()["pending"]
    assert pending["api"]["keys"] == ["api.host", "api.port"]


def test_restart_status_empty_with_no_pending_restart(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/infra/restart-status")
    assert resp.status_code == 200
    assert resp.json() == {"pending": {}, "restart_command": None, "session_disrupting": []}


def test_restart_status_reports_the_wrapped_command_and_session_impact(_isolated_config):
    """The notice tells the user the CLI equivalent and warns before dropping their session (#3806)."""
    client = TestClient(app)
    client.post(
        "/api/v1/config/sections",
        json={"auth": {"api_key": "rotated-key"}},  # pragma: allowlist secret
    )

    data = client.get("/api/v1/infra/restart-status").json()
    assert data["pending"]["web"]["keys"] == ["auth.api_key"]
    assert data["restart_command"] == "nyxgpt ops restart web"
    # Restarting `web` from a page served by `web` drops that page's session.
    assert data["session_disrupting"] == ["web"]


def test_rotating_the_auth_key_flags_web_not_api(_isolated_config):
    """#3806's worked example: the api tier is live, the web tier is frozen at start."""
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections", json={"auth": {"api_key": "rotated-key"}})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] == ["web"]
    assert resp.json()["restart_pending"]["web"]["keys"] == ["auth.api_key"]


def test_reverting_a_restart_required_value_retires_the_notice(_isolated_config):
    """ "...until the restart happens or the value is reverted" -- no stuck banner (#3806)."""
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    assert "api" in client.get("/api/v1/infra/restart-status").json()["pending"]

    resp = client.post("/api/v1/config/sections", json={"api": {"port": 8000}})
    assert resp.json()["restart_pending"] == {}
    assert client.get("/api/v1/infra/restart-status").json()["pending"] == {}


def test_restart_required_endpoint_rejects_when_nothing_pending(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/infra/restart-required", json={})
    assert resp.status_code == 400


def test_restart_required_endpoint_rejects_unknown_target(_isolated_config):
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})
    resp = client.post("/api/v1/infra/restart-required", json={"target": "web"})
    assert resp.status_code == 400


def test_restart_required_endpoint_schedules_and_returns_immediately(
    _isolated_config, captured_timers
):
    client = TestClient(app)
    client.post("/api/v1/config/sections", json={"api": {"port": 9500}})

    with patch("nyxgpt.app.self_heal_module.heal_now") as mock_heal_now:
        resp = client.post("/api/v1/infra/restart-required", json={})
        assert resp.status_code == 200
        assert resp.json() == {"targets": ["api"], "status": "running"}
        # Deferred via threading.Timer, not called inline -- and captured, so
        # it can't fire live against a real self-heal later (see captured_timers).
        mock_heal_now.assert_not_called()

    assert captured_timers == [(0.5, app_module._do_restart_required, (["api"],))]


def test_do_restart_required_clears_pending_on_success(_isolated_config):
    restart_state_module.mark_pending("api", {"api.port": "8000"})
    with (
        patch(
            "nyxgpt.app.self_heal_module.heal_now",
            return_value={
                "checked": [],
                "healed": [{"service": "api", "ok": True, "message": "Restarted nyxgpt-api"}],
            },
        ),
        patch("nyxgpt.app.ops_module.record_manual_restart") as mock_record,
    ):
        app_module._do_restart_required(["api"])

    assert restart_state_module.snapshot() == {}
    mock_record.assert_called_once_with("api", True, "Restarted nyxgpt-api")


def test_do_restart_required_keeps_pending_on_failure(_isolated_config):
    restart_state_module.mark_pending("api", {"api.port": "8000"})
    with patch(
        "nyxgpt.app.self_heal_module.heal_now",
        return_value={
            "checked": [],
            "healed": [{"service": "api", "ok": False, "message": "brew not found"}],
        },
    ):
        app_module._do_restart_required(["api"])

    assert "api" in restart_state_module.snapshot()


# --- Drift reconciliation: stale_keys + POST /config/sections/stale-keys/remove (#3388) ---


def test_get_sections_reports_no_stale_keys_for_clean_config(_isolated_config):
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    assert resp.json()["stale_keys"] == {}


def test_get_sections_reports_stale_key(_isolated_config):
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n\n"
        "[nyxgpt]\ndefault_model = a\nretired_option = old\n"
    )
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    assert resp.json()["stale_keys"] == {"nyxgpt": ["retired_option"]}


def test_get_sections_never_reports_excluded_section_keys_as_stale(_isolated_config):
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n\n[openai]\napi_key = sk-whatever\n"
    )
    client = TestClient(app)
    resp = client.get("/api/v1/config/sections")
    assert resp.status_code == 200
    assert resp.json()["stale_keys"] == {}


def test_stale_keys_remove_deletes_confirmed_key(_isolated_config):
    _isolated_config.write_text(
        "[ollama]\nbase_url = http://localhost:11434\n\n"
        "[nyxgpt]\ndefault_model = a\nretired_option = old\n"
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/sections/stale-keys/remove",
        json={"remove": {"nyxgpt": ["retired_option"]}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["removed"] == {"nyxgpt": ["retired_option"]}
    assert data["stale_keys"] == {}

    written = _isolated_config.read_text()
    assert "retired_option" not in written
    assert "default_model = a" in written


def test_stale_keys_remove_ignores_keys_not_actually_stale(_isolated_config):
    """Can't be used to delete a real managed field -- only what find_stale_keys reports."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/config/sections/stale-keys/remove",
        json={"remove": {"nyxgpt": ["default_model"]}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["removed"] == {}
    written = _isolated_config.read_text()
    assert "[ollama]" in written


def test_stale_keys_remove_rejects_non_object_payload(_isolated_config):
    client = TestClient(app)
    resp = client.post("/api/v1/config/sections/stale-keys/remove", json={"remove": "nope"})
    assert resp.status_code == 400
