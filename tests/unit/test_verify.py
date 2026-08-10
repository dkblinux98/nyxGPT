"""Unit tests for `nyxgpt.verify`, the pure traffic-generation/assertion logic
behind `nyxgpt ops verify` (#3555 / P6-18).

Mocks the HTTP layer entirely via `httpx.MockTransport` (no extra test
dependency needed -- httpx already ships it), matching the existing
unit/integration split in this repo: `tests/integration/` covers the same
assertions against a real running stack (see
`tests/integration/test_alert_test_live_grafana.py` for the precedent).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from nyxgpt import verify

# --- Traffic generation -----------------------------------------------------


def _handler_recording(calls: list[httpx.Request], responses: dict[str, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        body = responses.get(path, {"status_code": 200, "json": {}})
        return httpx.Response(body["status_code"], json=body["json"])

    return handler


def test_generate_traffic_hits_every_source_path(monkeypatch):
    calls: list[httpx.Request] = []
    responses = {
        "/api/v1/chat": {"status_code": 200, "json": {"reply": "OK"}},
        "/api/v1/rag/ingest": {"status_code": 200, "json": {"doc_id": "x", "status": "ok"}},
        "/api/v1/rag/upload": {"status_code": 200, "json": {"doc_id": "x", "status": "ok"}},
        "/api/v1/rag/index-repo": {"status_code": 200, "json": {"total_files": 2}},
        "/api/v1/rag/query": {"status_code": 200, "json": {"results": []}},
    }
    transport = httpx.MockTransport(_handler_recording(calls, responses))
    monkeypatch.setattr(
        verify,
        "_api_client",
        lambda base_url, api_key, timeout: httpx.Client(
            base_url=base_url, transport=transport, timeout=timeout
        ),
    )

    markers, checks = verify.generate_traffic(
        "http://api.test", "secret-key", repo_index_path="/root/.nyxGPT/fixture"
    )

    paths_hit = {c.url.path for c in calls}
    assert paths_hit == {
        "/api/v1/chat",
        "/api/v1/rag/ingest",
        "/api/v1/rag/upload",
        "/api/v1/rag/index-repo",
        "/api/v1/rag/query",
    }
    assert all(c.ok for c in checks)
    assert markers.chat_session.startswith("verify-")
    assert markers.query_marker.startswith("XYZZY-VERIFY-")


def test_generate_traffic_skips_repo_ingest_without_a_path(monkeypatch):
    calls: list[httpx.Request] = []
    responses = {
        "/api/v1/chat": {"status_code": 200, "json": {"reply": "OK"}},
        "/api/v1/rag/ingest": {"status_code": 200, "json": {"status": "ok"}},
        "/api/v1/rag/upload": {"status_code": 200, "json": {"status": "ok"}},
        "/api/v1/rag/query": {"status_code": 200, "json": {"results": []}},
    }
    transport = httpx.MockTransport(_handler_recording(calls, responses))
    monkeypatch.setattr(
        verify,
        "_api_client",
        lambda base_url, api_key, timeout: httpx.Client(
            base_url=base_url, transport=transport, timeout=timeout
        ),
    )

    _markers, checks = verify.generate_traffic("http://api.test", None, repo_index_path=None)

    repo_checks = [c for c in checks if "repo ingest" in c.message.lower()]
    assert len(repo_checks) == 1
    assert repo_checks[0].ok is False
    assert "skipped" in repo_checks[0].message.lower()
    assert not any(c.url.path == "/api/v1/rag/index-repo" for c in calls)


def test_generate_traffic_reports_failed_step(monkeypatch):
    calls: list[httpx.Request] = []
    responses = {
        "/api/v1/chat": {"status_code": 500, "json": {"detail": "boom"}},
        "/api/v1/rag/ingest": {"status_code": 200, "json": {"status": "ok"}},
        "/api/v1/rag/upload": {"status_code": 200, "json": {"status": "ok"}},
        "/api/v1/rag/index-repo": {"status_code": 200, "json": {}},
        "/api/v1/rag/query": {"status_code": 200, "json": {"results": []}},
    }
    transport = httpx.MockTransport(_handler_recording(calls, responses))
    monkeypatch.setattr(
        verify,
        "_api_client",
        lambda base_url, api_key, timeout: httpx.Client(
            base_url=base_url, transport=transport, timeout=timeout
        ),
    )

    _markers, checks = verify.generate_traffic(
        "http://api.test", None, repo_index_path="/root/.nyxGPT/fixture"
    )

    chat_check = next(c for c in checks if c.message.startswith("Chat round-trip"))
    assert chat_check.ok is False
    assert "500" in chat_check.message


def test_write_verify_repo_fixture_creates_two_files(tmp_path):
    directory = tmp_path / "verify-repo"
    result = verify.write_verify_repo_fixture(directory)
    assert result == directory
    assert (directory / "README.md").exists()
    assert (directory / "notes.md").exists()


# --- Prometheus assertions ---------------------------------------------------


def test_snapshot_counters_returns_zero_on_query_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(verify.httpx, "get", fake_get)
    snapshot = verify.snapshot_counters("http://prometheus.test", {"a": "sum(x)"})
    assert snapshot == {"a": 0.0}


def test_snapshot_counters_sums_result_series(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"value": [0, "3"]}, {"value": [0, "4"]}]},
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(verify.httpx, "get", fake_get)
    snapshot = verify.snapshot_counters("http://prometheus.test", {"a": "sum(x) by (y)"})
    assert snapshot == {"a": 7.0}


def test_assert_counter_deltas_passes_when_value_rose(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"value": [0, "5"]}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(verify.httpx, "get", fake_get)
    checks = verify.assert_counter_deltas(
        "http://prometheus.test",
        before={"chat requests": 2.0},
        queries={"chat requests": "sum(nyxgpt_chat_requests_total)"},
        min_delta=1.0,
        poll_timeout=1.0,
        poll_interval=0.01,
    )
    assert len(checks) == 1
    assert checks[0].ok is True
    assert "chat requests" in checks[0].message


def test_assert_counter_deltas_fails_when_value_did_not_rise(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"value": [0, "2"]}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(verify.httpx, "get", fake_get)
    checks = verify.assert_counter_deltas(
        "http://prometheus.test",
        before={"chat requests": 2.0},
        queries={"chat requests": "sum(nyxgpt_chat_requests_total)"},
        min_delta=1.0,
        poll_timeout=0.05,
        poll_interval=0.01,
    )
    assert len(checks) == 1
    assert checks[0].ok is False
    assert "chat requests" in checks[0].message
    assert "sum(nyxgpt_chat_requests_total)" in checks[0].message


def test_assert_counter_deltas_names_query_on_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(verify.httpx, "get", fake_get)
    checks = verify.assert_counter_deltas(
        "http://prometheus.test",
        before={"a": 0.0},
        queries={"a": "sum(x)"},
        poll_timeout=0.05,
        poll_interval=0.01,
    )
    assert checks[0].ok is False
    assert "sum(x)" in checks[0].message
    assert "ConnectError" in checks[0].details


# --- Grafana dashboard panel assertions -------------------------------------


def test_extract_panel_queries_reads_real_rag_performance_dashboard():
    path = verify.DASHBOARDS_DIR / "rag-performance.json"
    panels = verify.extract_panel_queries(path)

    assert panels, "expected at least one panel query"
    exprs = [p["expr"] for p in panels]
    assert any("nyxgpt_rag_queries_total" in e for e in exprs)
    assert any("nyxgpt_rag_ingests_total" in e for e in exprs)
    assert not any("$__rate_interval" in e for e in exprs)
    assert any("5m" in e for e in exprs if "increase(" in e)
    assert all(p["dashboard_title"] for p in panels)
    assert all(p["panel_title"] for p in panels)


def test_dashboard_uid_reads_real_file():
    path = verify.DASHBOARDS_DIR / "rag-performance.json"
    assert verify.dashboard_uid(path) == "nyxgpt-rag-performance"


def test_resolve_touched_dashboards_defaults_exist():
    paths = verify.resolve_touched_dashboards(None)
    assert [p.name for p in paths] == ["rag-performance.json", "api-metrics.json"]
    for p in paths:
        assert p.exists()


def test_resolve_touched_dashboards_rejects_path_traversal():
    with pytest.raises(ValueError):
        verify.resolve_touched_dashboards(["../../etc/passwd"])


def test_resolve_touched_dashboards_rejects_unknown_dashboard():
    with pytest.raises(ValueError):
        verify.resolve_touched_dashboards(["does-not-exist"])


def test_assert_panel_queries_reports_ok_when_datapoints_present(tmp_path):
    dashboard = tmp_path / "fake.json"
    dashboard.write_text(
        json.dumps(
            {
                "uid": "fake",
                "title": "Fake Dashboard",
                "panels": [
                    {
                        "id": 1,
                        "title": "Panel A",
                        "type": "timeseries",
                        "targets": [{"expr": "sum(nyxgpt_rag_queries_total)"}],
                    }
                ],
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"result": [{"value": [0, "1"]}]}}
        )

    client = httpx.Client(base_url="http://grafana.test", transport=httpx.MockTransport(handler))
    checks = verify.assert_panel_queries(client, [dashboard])

    assert len(checks) == 1
    assert checks[0].ok is True
    assert "Fake Dashboard / Panel A" in checks[0].message


def test_assert_panel_queries_names_panel_and_query_on_empty_result(tmp_path):
    dashboard = tmp_path / "fake.json"
    dashboard.write_text(
        json.dumps(
            {
                "uid": "fake",
                "title": "Fake Dashboard",
                "panels": [
                    {
                        "id": 1,
                        "title": "Panel A",
                        "type": "timeseries",
                        "targets": [{"expr": "sum(nyxgpt_rag_queries_total)"}],
                    }
                ],
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    client = httpx.Client(base_url="http://grafana.test", transport=httpx.MockTransport(handler))
    checks = verify.assert_panel_queries(client, [dashboard])

    assert len(checks) == 1
    assert checks[0].ok is False
    assert "Fake Dashboard / Panel A" in checks[0].message
    assert "sum(nyxgpt_rag_queries_total)" in checks[0].message


def test_assert_panel_queries_skips_row_panels(tmp_path):
    dashboard = tmp_path / "fake.json"
    dashboard.write_text(
        json.dumps(
            {
                "uid": "fake",
                "title": "Fake Dashboard",
                "panels": [{"id": 1, "title": "Row", "type": "row"}],
            }
        )
    )

    client = httpx.Client(
        base_url="http://grafana.test", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    checks = verify.assert_panel_queries(client, [dashboard])
    assert checks == []


# --- Playwright screenshots --------------------------------------------------


def test_capture_dashboard_screenshots_reports_actionable_error_when_playwright_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(verify, "try_import", lambda _name: None)
    checks = verify.capture_dashboard_screenshots(
        "http://grafana.test", "pw", ["dash-uid"], tmp_path
    )
    assert len(checks) == 1
    assert checks[0].ok is False
    assert "playwright install" in checks[0].details.lower()


def test_capture_dashboard_screenshots_happy_path(monkeypatch, tmp_path):
    page = MagicMock()
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context
    chromium = MagicMock()
    chromium.launch.return_value = browser
    playwright_instance = SimpleNamespace(chromium=chromium)

    class FakeSyncPlaywright:
        def __enter__(self):
            return playwright_instance

        def __exit__(self, *exc):
            return False

    fake_sync_api = SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright())
    monkeypatch.setattr(verify, "try_import", lambda _name: fake_sync_api)

    checks = verify.capture_dashboard_screenshots(
        "http://grafana.test", "pw", ["dash-a", "dash-b"], tmp_path
    )

    assert len(checks) == 2
    assert all(c.ok for c in checks)
    assert page.goto.call_count == 2
    assert page.screenshot.call_count == 2
    goto_urls = [call_args.args[0] for call_args in page.goto.call_args_list]
    assert "http://grafana.test/d/dash-a?orgId=1&kiosk" in goto_urls
    assert "http://grafana.test/d/dash-b?orgId=1&kiosk" in goto_urls
    browser.new_context.assert_called_once()
    _, kwargs = browser.new_context.call_args
    assert kwargs["http_credentials"] == {"username": "admin", "password": "pw"}


def test_capture_dashboard_screenshots_records_per_dashboard_failure(monkeypatch, tmp_path):
    page = MagicMock()
    page.goto.side_effect = RuntimeError("timeout")
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context
    chromium = MagicMock()
    chromium.launch.return_value = browser
    playwright_instance = SimpleNamespace(chromium=chromium)

    class FakeSyncPlaywright:
        def __enter__(self):
            return playwright_instance

        def __exit__(self, *exc):
            return False

    fake_sync_api = SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright())
    monkeypatch.setattr(verify, "try_import", lambda _name: fake_sync_api)

    checks = verify.capture_dashboard_screenshots("http://grafana.test", "pw", ["dash-a"], tmp_path)

    assert len(checks) == 1
    assert checks[0].ok is False
    assert "dash-a" in checks[0].message
