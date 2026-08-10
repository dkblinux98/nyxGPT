"""Live smoke-test harness backing `nyxgpt ops verify` (#3555 / P6-18).

Generates one known unit of traffic per acceptance-criteria path (a chat
round-trip, one RAG ingest per source -- document/upload/repo -- and a RAG
query), then asserts it actually landed via two independent live checks:
Prometheus instant queries for the expected counter deltas, and Grafana's
HTTP API re-executing each touched dashboard panel's own query. Finishes
with Playwright screenshots of the touched dashboards -- visual evidence the
review agent (multimodal) inspects directly, per the review deadlock this
issue closes (PR #3548/#3469: acceptance criteria demanding live
verification that neither the developer nor review agent could produce with
no running stack or eyes on rendered output).

This module is deployment-mode-agnostic (native vs. Docker Compose) pure
logic: it takes already-resolved URLs/paths and returns `VerifyCheck`
results. `ops.py`'s `verify()` CLI entrypoint owns booting/tearing down the
stack and resolving those inputs (deployment mode, Grafana admin
credentials, ...), then calls into this module.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nyxgpt.optional_imports import try_import

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "docker" / "grafana" / "dashboards"

# Dashboards touched by the traffic this harness generates -- see
# docker/grafana/dashboards/rag-performance.json (chat + RAG panels) and
# api-metrics.json (HTTP request panels every endpoint call above hits).
DEFAULT_TOUCHED_DASHBOARDS: tuple[str, ...] = ("rag-performance.json", "api-metrics.json")

# Grafana's `$__rate_interval` template variable has no meaning outside a
# dashboard's own time-range context; substituted with a concrete window
# wide enough to cover this harness's traffic when queried immediately
# after generating it.
RATE_INTERVAL_SUBSTITUTE = "5m"

# Counter queries asserted directly against Prometheus (independent of
# Grafana/dashboard wiring) -- keyed by a human name used in failure
# messages. These are plain global totals (none of the underlying counters
# carry a per-request/session label -- see src/nyxgpt/metrics.py), so the
# before/after snapshot taken immediately around one verify run's traffic is
# what makes the delta assertion meaningful, not a query-side filter.
EXPECTED_COUNTER_QUERIES: dict[str, str] = {
    "chat requests": "sum(nyxgpt_chat_requests_total)",
    "RAG document ingests": 'sum(nyxgpt_rag_ingests_total{source="document",result="success"})',
    "RAG upload ingests": 'sum(nyxgpt_rag_ingests_total{source="upload",result="success"})',
    "RAG repo ingests": 'sum(nyxgpt_rag_ingests_total{source="repo",result="success"})',
    "RAG queries": 'sum(nyxgpt_rag_queries_total{source="rag_query"})',
}


@dataclass(frozen=True)
class VerifyCheck:
    """Outcome of a single verify step or assertion."""

    ok: bool
    message: str
    details: str = ""


@dataclass(frozen=True)
class TrafficMarkers:
    """Identifiers stamped into generated traffic so assertions can key off this specific run."""

    run_id: str
    chat_session: str
    doc_id: str
    upload_doc_id: str
    repo_doc_prefix: str
    query_marker: str


def _api_client(base_url: str, api_key: str | None, timeout: float) -> httpx.Client:
    """Build an `httpx.Client` for the API, attaching `X-API-Key` when auth is enabled."""
    headers = {"X-API-Key": api_key} if api_key else {}
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


def _http_check(step: str, resp: httpx.Response) -> VerifyCheck:
    """Turn one HTTP response into a `VerifyCheck`, ok for any non-error status code."""
    if resp.status_code < 400:
        return VerifyCheck(True, f"{step}: HTTP {resp.status_code}")
    return VerifyCheck(False, f"{step}: HTTP {resp.status_code}", resp.text[:500])


# --- Traffic generation --------------------------------------------------


def generate_traffic(
    api_base_url: str,
    api_key: str | None = None,
    *,
    repo_index_path: str | None = None,
    timeout: float = 60.0,
) -> tuple[TrafficMarkers, list[VerifyCheck]]:
    """Generate one chat round-trip, one RAG ingest per source path
    (document/upload/repo), and one RAG query against a live API.

    `repo_index_path` must be a directory the API process can see (resolved
    by the caller -- it differs between native and containerized
    deployments); the repo-ingest step is skipped with a clear message if
    omitted. Returns the markers stamped into the generated traffic plus one
    `VerifyCheck` per step's own HTTP outcome.
    """
    run_id = uuid.uuid4().hex[:12]
    markers = TrafficMarkers(
        run_id=run_id,
        chat_session=f"verify-{run_id}",
        doc_id=f"verify-doc-{run_id}",
        upload_doc_id=f"verify-upload-{run_id}",
        repo_doc_prefix=f"verify-repo-{run_id}",
        query_marker=f"XYZZY-VERIFY-{run_id}",
    )
    checks: list[VerifyCheck] = []
    with _api_client(api_base_url, api_key, timeout) as client:
        checks.append(_chat_round_trip(client, markers))
        checks.append(_rag_ingest_document(client, markers))
        checks.append(_rag_ingest_upload(client, markers))
        if repo_index_path:
            checks.append(_rag_ingest_repo(client, markers, repo_index_path))
        else:
            checks.append(
                VerifyCheck(
                    False,
                    "RAG repo ingest: skipped",
                    "No repo_index_path resolved -- caller must provide a directory the API "
                    "process can see (see ops.py's _resolve_verify_repo_index_path).",
                )
            )
        checks.append(_rag_query(client, markers))
    return markers, checks


def _chat_round_trip(client: httpx.Client, markers: TrafficMarkers) -> VerifyCheck:
    """POST one chat turn under `markers.chat_session` and check a reply came back."""
    resp = client.post(
        "/api/v1/chat",
        json={
            "prompt": "Reply with exactly one word: OK",
            "session": markers.chat_session,
            "new": True,
        },
    )
    check = _http_check("Chat round-trip", resp)
    if not check.ok:
        return check
    reply = (resp.json() or {}).get("reply")
    if not reply:
        return VerifyCheck(False, "Chat round-trip: no reply in response", str(resp.json())[:500])
    return VerifyCheck(True, f"Chat round-trip: reply received ({reply[:60]!r})")


def _rag_ingest_document(client: httpx.Client, markers: TrafficMarkers) -> VerifyCheck:
    """Exercise the `document` RAG ingest source path (`POST /rag/ingest`)."""
    resp = client.post(
        "/api/v1/rag/ingest",
        json={
            "doc_id": markers.doc_id,
            "text": f"The secret verify phrase is {markers.query_marker}.",
            "ensure_schema": True,
        },
    )
    return _http_check("RAG document ingest", resp)


def _rag_ingest_upload(client: httpx.Client, markers: TrafficMarkers) -> VerifyCheck:
    """Exercise the `upload` RAG ingest source path (`POST /rag/upload`)."""
    content = f"# Verify\n\nThe secret upload phrase is {markers.query_marker}.\n".encode()
    resp = client.post(
        "/api/v1/rag/upload",
        files={"file": (f"{markers.upload_doc_id}.md", content, "text/markdown")},
    )
    return _http_check("RAG upload ingest", resp)


def _rag_ingest_repo(
    client: httpx.Client, markers: TrafficMarkers, repo_index_path: str
) -> VerifyCheck:
    """Exercise the `repo` RAG ingest source path (`POST /rag/index-repo`)."""
    resp = client.post(
        "/api/v1/rag/index-repo",
        json={
            "repo_path": repo_index_path,
            "doc_id_prefix": markers.repo_doc_prefix,
            "ensure_schema": True,
        },
    )
    return _http_check("RAG repo ingest", resp)


def _rag_query(client: httpx.Client, markers: TrafficMarkers) -> VerifyCheck:
    """Query for the ingested verify phrase and note (non-fatally) whether it surfaced."""
    resp = client.post("/api/v1/rag/query", json={"query": "What is the secret verify phrase?"})
    check = _http_check("RAG query", resp)
    if not check.ok:
        return check
    if markers.query_marker in resp.text:
        return VerifyCheck(True, "RAG query: surfaced the ingested verify phrase")
    return VerifyCheck(
        True,
        "RAG query: HTTP 200 but did not surface the ingested verify phrase",
        "Non-fatal (matches scripts/smoke-test.sh's own tolerance for retrieval not always "
        "ranking a just-ingested chunk first) -- the Prometheus/Grafana checks are the "
        "authoritative assertion that the query itself executed.",
    )


def write_verify_repo_fixture(directory: Path) -> Path:
    """Write a tiny 2-file fixture repo under `directory` for the repo-ingest step.

    Idempotent -- content is fixed, so re-running `verify` just overwrites
    the same files rather than accumulating garbage under the shared
    nyxgpt-data volume.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text(
        "# nyxgpt ops verify fixture\n\nUsed only to exercise the RAG repo-ingest path.\n"
    )
    (directory / "notes.md").write_text(
        "This file exists solely so `nyxgpt ops verify` has something to index.\n"
    )
    return directory


# --- Prometheus assertions -------------------------------------------------


def _prometheus_instant_query(
    prometheus_ui_url: str, query: str, *, timeout: float
) -> tuple[float | None, str | None]:
    """Run a Prometheus instant query, returning `(value, error)`.

    `value` is the summed scalar result (0.0 if the query resolved with no
    series -- a legitimate "not seen yet" reading, not an error).
    """
    try:
        resp = httpx.get(
            f"{prometheus_ui_url.rstrip('/')}/api/v1/query",
            params={"query": query},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if data.get("status") != "success":
        return None, str(data.get("error") or data)
    result = data.get("data", {}).get("result", [])
    total = sum(float(series["value"][1]) for series in result)
    return total, None


def snapshot_counters(
    prometheus_ui_url: str, queries: dict[str, str], *, timeout: float = 10.0
) -> dict[str, float]:
    """Snapshot each named counter query's current value (0.0 on any query error)."""
    snapshot: dict[str, float] = {}
    for name, query in queries.items():
        value, _error = _prometheus_instant_query(prometheus_ui_url, query, timeout=timeout)
        snapshot[name] = value if value is not None else 0.0
    return snapshot


def assert_counter_deltas(
    prometheus_ui_url: str,
    before: dict[str, float],
    queries: dict[str, str],
    *,
    min_delta: float = 1.0,
    poll_timeout: float = 60.0,
    poll_interval: float = 5.0,
) -> list[VerifyCheck]:
    """Assert each counter in `queries` rose by at least `min_delta` since `before`.

    Polls up to `poll_timeout` (Prometheus's scrape interval is 15s -- see
    docker/prometheus.yml -- so an immediate single query can race the next
    scrape) rather than failing on the first miss.
    """
    checks: list[VerifyCheck] = []
    deadline = time.monotonic() + poll_timeout
    remaining = dict(queries)
    last_seen: dict[str, float] = {}
    last_error: dict[str, str] = {}
    while remaining and time.monotonic() < deadline:
        for name in list(remaining):
            value, error = _prometheus_instant_query(
                prometheus_ui_url, remaining[name], timeout=10.0
            )
            if error is not None:
                last_error[name] = error
                continue
            last_seen[name] = value if value is not None else 0.0
            delta = last_seen[name] - before.get(name, 0.0)
            if delta >= min_delta:
                checks.append(
                    VerifyCheck(
                        True,
                        f"Prometheus counter delta OK: {name} (+{delta:g}, query: "
                        f"{remaining[name]!r})",
                    )
                )
                del remaining[name]
        if remaining:
            time.sleep(poll_interval)
    for name, query in remaining.items():
        if name in last_error:
            checks.append(
                VerifyCheck(
                    False,
                    f"Prometheus counter delta FAILED: {name} (query: {query!r})",
                    f"Query error: {last_error[name]}",
                )
            )
        else:
            delta = last_seen.get(name, 0.0) - before.get(name, 0.0)
            checks.append(
                VerifyCheck(
                    False,
                    f"Prometheus counter delta FAILED: {name} (query: {query!r})",
                    f"Expected delta >= {min_delta:g}, got {delta:g} after {poll_timeout:g}s "
                    f"of polling.",
                )
            )
    return checks


# --- Grafana dashboard panel assertions ------------------------------------


def _resolve_panel_expr(expr: str) -> str:
    """Substitute Grafana's dashboard-only template variables with a concrete query window."""
    return expr.replace("$__rate_interval", RATE_INTERVAL_SUBSTITUTE).replace(
        "$__interval", RATE_INTERVAL_SUBSTITUTE
    )


def extract_panel_queries(dashboard_path: Path) -> list[dict[str, Any]]:
    """Extract `(dashboard_title, panel_title, expr)` for every Prometheus-backed panel target.

    Reads the dashboard JSON directly (the same file Grafana provisions
    from -- see docker/grafana/provisioning/dashboards) rather than calling
    Grafana's dashboard-search API, so this works even before the dashboard
    has been provisioned into a running Grafana.
    """
    dashboard = json.loads(dashboard_path.read_text())
    title = dashboard.get("title", dashboard_path.stem)
    queries: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []):
        if panel.get("type") == "row":
            continue
        for target in panel.get("targets") or []:
            expr = target.get("expr")
            if not expr:
                continue
            queries.append(
                {
                    "dashboard_title": title,
                    "dashboard_uid": dashboard.get("uid", dashboard_path.stem),
                    "panel_title": panel.get("title", f"panel {panel.get('id')}"),
                    "expr": _resolve_panel_expr(expr),
                }
            )
    return queries


def assert_panel_queries(
    grafana_client: httpx.Client,
    dashboard_paths: list[Path],
    *,
    min_delta: float = 0.0,
) -> list[VerifyCheck]:
    """Re-execute each touched dashboard panel's own query through Grafana's HTTP API
    (the Prometheus datasource proxy) and assert it returns datapoints.

    Exercises the actual Grafana<->Prometheus wiring a dashboard uses to
    render (distinct from `assert_counter_deltas`, which queries Prometheus
    directly) -- a panel whose query is broken or whose datasource is
    misconfigured fails here even if Prometheus itself has the data. Any
    failure names the panel and the exact query, per the acceptance
    criteria.
    """
    checks: list[VerifyCheck] = []
    for dashboard_path in dashboard_paths:
        try:
            panels = extract_panel_queries(dashboard_path)
        except Exception as e:
            checks.append(
                VerifyCheck(
                    False,
                    f"Could not read dashboard {dashboard_path.name}",
                    f"{type(e).__name__}: {e}",
                )
            )
            continue
        for panel in panels:
            label = f"{panel['dashboard_title']} / {panel['panel_title']}"
            try:
                resp = grafana_client.get(
                    "/api/datasources/proxy/uid/prometheus/api/v1/query",
                    params={"query": panel["expr"]},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                checks.append(
                    VerifyCheck(
                        False,
                        f"Grafana panel query FAILED: {label} (query: {panel['expr']!r})",
                        f"{type(e).__name__}: {e}",
                    )
                )
                continue
            if data.get("status") != "success":
                checks.append(
                    VerifyCheck(
                        False,
                        f"Grafana panel query FAILED: {label} (query: {panel['expr']!r})",
                        str(data.get("error") or data)[:500],
                    )
                )
                continue
            result = data.get("data", {}).get("result", [])
            total = sum(float(series["value"][1]) for series in result) if result else 0.0
            if not result or total < min_delta:
                checks.append(
                    VerifyCheck(
                        False,
                        f"Grafana panel query returned no datapoints: {label} "
                        f"(query: {panel['expr']!r})",
                        f"Result: {result!r}",
                    )
                )
                continue
            checks.append(
                VerifyCheck(True, f"Grafana panel query OK: {label} (query: {panel['expr']!r})")
            )
    return checks


# --- Playwright screenshots --------------------------------------------------


def capture_dashboard_screenshots(
    grafana_ui_url: str,
    grafana_admin_password: str,
    dashboard_uids: list[str],
    out_dir: Path,
    *,
    viewport: tuple[int, int] = (1600, 1000),
) -> list[VerifyCheck]:
    """Capture a full-page PNG screenshot of each dashboard for visual review-agent inspection.

    Requires the optional `playwright` extra (`pip install "nyxgpt[verify]"`
    then `playwright install --with-deps chromium`); returns a single
    actionable failure check (not a crash) if it isn't installed, so a host
    without browsers configured still gets a clear, scriptable outcome.
    """
    sync_api = try_import("playwright.sync_api")
    if sync_api is None:
        return [
            VerifyCheck(
                False,
                "Playwright not installed -- dashboard screenshots skipped",
                'Run `pip install "nyxgpt[verify]"` then `playwright install --with-deps '
                "chromium` to enable visual evidence capture.",
            )
        ]

    out_dir.mkdir(parents=True, exist_ok=True)
    checks: list[VerifyCheck] = []
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    http_credentials={"username": "admin", "password": grafana_admin_password},
                )
                page = context.new_page()
                for uid in dashboard_uids:
                    dest = out_dir / f"{uid}.png"
                    try:
                        page.goto(
                            f"{grafana_ui_url.rstrip('/')}/d/{uid}?orgId=1&kiosk",
                            wait_until="networkidle",
                            timeout=30_000,
                        )
                        page.screenshot(path=str(dest), full_page=True)
                        checks.append(VerifyCheck(True, f"Screenshot captured: {uid} -> {dest}"))
                    except Exception as e:
                        checks.append(
                            VerifyCheck(
                                False,
                                f"Screenshot capture FAILED: {uid}",
                                f"{type(e).__name__}: {e}",
                            )
                        )
                context.close()
            finally:
                browser.close()
    except Exception as e:
        checks.append(
            VerifyCheck(False, "Playwright browser session failed", f"{type(e).__name__}: {e}")
        )
    return checks


def dashboard_uid(dashboard_path: Path) -> str:
    """Read a dashboard JSON file's `uid` field (falls back to its filename stem)."""
    try:
        data = json.loads(dashboard_path.read_text())
    except Exception:
        return dashboard_path.stem
    return str(data.get("uid") or dashboard_path.stem)


_IDENT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def resolve_touched_dashboards(names: list[str] | None) -> list[Path]:
    """Resolve dashboard filenames (default `DEFAULT_TOUCHED_DASHBOARDS`) under `DASHBOARDS_DIR`.

    Rejects anything that isn't a bare filename (no path separators) so a
    `--dashboards` value can never escape `DASHBOARDS_DIR`.
    """
    resolved: list[Path] = []
    for name in names or list(DEFAULT_TOUCHED_DASHBOARDS):
        stem = name[:-5] if name.endswith(".json") else name
        if not _IDENT_RE.match(stem):
            raise ValueError(f"Invalid dashboard name: {name!r}")
        path = DASHBOARDS_DIR / f"{stem}.json"
        if not path.exists():
            raise ValueError(f"Dashboard not found: {path}")
        resolved.append(path)
    return resolved
