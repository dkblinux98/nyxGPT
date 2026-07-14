"""Unit tests for scripts/collect_workflow_logs.py (workflow log collection/analytics).

Related: #2844
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "collect_workflow_logs.py"
_spec = importlib.util.spec_from_file_location("collect_workflow_logs", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
collect_workflow_logs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = collect_workflow_logs
_spec.loader.exec_module(collect_workflow_logs)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    collect_workflow_logs.init_db(connection)
    yield connection
    connection.close()


def _raw_run(
    run_id: int,
    workflow_name: str = "Developer Agent",
    status: str = "completed",
    conclusion: str | None = "success",
    branch: str = "feat/2844-add-thing",
    created_at: str = "2026-07-14T07:00:00Z",
    updated_at: str = "2026-07-14T07:05:00Z",
) -> dict:
    return {
        "databaseId": run_id,
        "workflowName": workflow_name,
        "status": status,
        "conclusion": conclusion,
        "headBranch": branch,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "url": f"https://github.com/dkblinux98/nyxGPT/actions/runs/{run_id}",
        "displayTitle": "some commit message",
    }


class TestParseIssueNumber:
    def test_feat_branch(self):
        assert collect_workflow_logs.parse_issue_number("feat/2844-add-thing") == 2844

    def test_fix_branch(self):
        assert collect_workflow_logs.parse_issue_number("fix/1234-bug") == 1234

    def test_claude_issue_branch(self):
        assert collect_workflow_logs.parse_issue_number("claude/issue-2844-20260714-0753") == 2844

    def test_no_issue_number(self):
        assert collect_workflow_logs.parse_issue_number("main") is None

    def test_none_branch(self):
        assert collect_workflow_logs.parse_issue_number(None) is None


class TestToRecord:
    def test_computes_duration_and_issue_number(self):
        record = collect_workflow_logs.to_record(_raw_run(1))
        assert record["run_id"] == 1
        assert record["workflow_name"] == "Developer Agent"
        assert record["conclusion"] == "success"
        assert record["issue_number"] == 2844
        assert record["duration_s"] == pytest.approx(300.0)

    def test_missing_conclusion_becomes_none(self):
        record = collect_workflow_logs.to_record(_raw_run(1, conclusion=""))
        assert record["conclusion"] is None


class TestIngestAndQuery:
    def test_ingest_then_query_roundtrip(self, conn):
        records = [collect_workflow_logs.to_record(_raw_run(1))]
        count = collect_workflow_logs.ingest_runs(conn, records)
        assert count == 1

        rows = collect_workflow_logs.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 1
        assert rows[0]["workflow_name"] == "Developer Agent"

    def test_reingesting_same_run_id_upserts_not_duplicates(self, conn):
        collect_workflow_logs.ingest_runs(conn, [collect_workflow_logs.to_record(_raw_run(1))])
        collect_workflow_logs.ingest_runs(
            conn, [collect_workflow_logs.to_record(_raw_run(1, conclusion="failure"))]
        )

        rows = collect_workflow_logs.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["conclusion"] == "failure"

    def test_query_filters_by_workflow(self, conn):
        collect_workflow_logs.ingest_runs(
            conn,
            [
                collect_workflow_logs.to_record(_raw_run(1, workflow_name="Developer Agent")),
                collect_workflow_logs.to_record(_raw_run(2, workflow_name="Review Agent")),
            ],
        )

        rows = collect_workflow_logs.query_runs(conn, workflow="Review Agent")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_filters_by_issue_number(self, conn):
        collect_workflow_logs.ingest_runs(
            conn,
            [
                collect_workflow_logs.to_record(_raw_run(1, branch="feat/2844-a")),
                collect_workflow_logs.to_record(_raw_run(2, branch="feat/9999-b")),
            ],
        )

        rows = collect_workflow_logs.query_runs(conn, issue_number=2844)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 1

    def test_query_filters_by_conclusion(self, conn):
        collect_workflow_logs.ingest_runs(
            conn,
            [
                collect_workflow_logs.to_record(_raw_run(1, conclusion="success")),
                collect_workflow_logs.to_record(_raw_run(2, conclusion="failure")),
            ],
        )

        rows = collect_workflow_logs.query_runs(conn, conclusion="failure")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_respects_limit(self, conn):
        collect_workflow_logs.ingest_runs(
            conn, [collect_workflow_logs.to_record(_raw_run(i)) for i in range(1, 6)]
        )
        rows = collect_workflow_logs.query_runs(conn, limit=2)
        assert len(rows) == 2

    def test_ingest_empty_list_is_noop(self, conn):
        assert collect_workflow_logs.ingest_runs(conn, []) == 0


class TestComputeSummary:
    def test_success_rate_and_avg_duration(self, conn):
        collect_workflow_logs.ingest_runs(
            conn,
            [
                collect_workflow_logs.to_record(_raw_run(1, conclusion="success")),
                collect_workflow_logs.to_record(_raw_run(2, conclusion="success")),
                collect_workflow_logs.to_record(_raw_run(3, conclusion="failure")),
            ],
        )
        summary = collect_workflow_logs.compute_summary(conn, days=30)
        assert summary["total_runs"] == 3
        assert summary["success_rate"] == pytest.approx(66.7, abs=0.1)
        assert summary["failures"] == 1
        assert summary["avg_duration_s"] == pytest.approx(300.0)

    def test_by_workflow_breakdown(self, conn):
        collect_workflow_logs.ingest_runs(
            conn,
            [
                collect_workflow_logs.to_record(
                    _raw_run(1, workflow_name="Developer Agent", conclusion="success")
                ),
                collect_workflow_logs.to_record(
                    _raw_run(2, workflow_name="Review Agent", conclusion="failure")
                ),
                collect_workflow_logs.to_record(
                    _raw_run(3, workflow_name="Review Agent", conclusion="failure")
                ),
            ],
        )
        summary = collect_workflow_logs.compute_summary(conn, days=30)
        by_workflow = {w["workflow"]: w for w in summary["by_workflow"]}
        assert by_workflow["Developer Agent"]["runs"] == 1
        assert by_workflow["Review Agent"]["failures"] == 2
        assert summary["top_failing"][0]["workflow"] == "Review Agent"

    def test_empty_store_returns_zeroed_summary(self, conn):
        summary = collect_workflow_logs.compute_summary(conn, days=30)
        assert summary["total_runs"] == 0
        assert summary["success_rate"] == 0.0
        assert summary["by_workflow"] == []
        assert summary["top_failing"] == []

    def test_runs_outside_window_excluded(self, conn):
        old_run = _raw_run(1, created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:05:00Z")
        collect_workflow_logs.ingest_runs(conn, [collect_workflow_logs.to_record(old_run)])
        summary = collect_workflow_logs.compute_summary(conn, days=30)
        assert summary["total_runs"] == 0


class TestPurgeOld:
    def test_purges_runs_older_than_retention(self, conn):
        old_run = _raw_run(1, created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:05:00Z")
        recent_run = _raw_run(2)
        collect_workflow_logs.ingest_runs(
            conn,
            [collect_workflow_logs.to_record(old_run), collect_workflow_logs.to_record(recent_run)],
        )

        deleted = collect_workflow_logs.purge_old(conn, retention_days=90)
        assert deleted == 1

        rows = collect_workflow_logs.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2


class TestCollect:
    def test_filters_to_completed_runs_by_default(self, conn, monkeypatch):
        runs = [
            _raw_run(1, status="completed", conclusion="success"),
            _raw_run(2, status="in_progress", conclusion=None),
        ]
        monkeypatch.setattr(
            collect_workflow_logs, "fetch_workflow_runs", lambda repo, limit=200: runs
        )

        count = collect_workflow_logs.collect("dkblinux98/nyxGPT", conn)
        assert count == 1
        rows = collect_workflow_logs.query_runs(conn)
        assert rows[0]["run_id"] == 1

    def test_include_in_progress_stores_all(self, conn, monkeypatch):
        runs = [
            _raw_run(1, status="completed", conclusion="success"),
            _raw_run(2, status="in_progress", conclusion=None),
        ]
        monkeypatch.setattr(
            collect_workflow_logs, "fetch_workflow_runs", lambda repo, limit=200: runs
        )

        count = collect_workflow_logs.collect("dkblinux98/nyxGPT", conn, include_in_progress=True)
        assert count == 2


class TestGetDbPath:
    def test_default_path_used_when_none_given(self):
        path = collect_workflow_logs.get_db_path(None)
        assert path == collect_workflow_logs.DEFAULT_DB_PATH

    def test_custom_path_creates_parent_dir(self, tmp_path):
        custom = tmp_path / "nested" / "runs.sqlite3"
        path = collect_workflow_logs.get_db_path(str(custom))
        assert path == custom
        assert custom.parent.exists()


def test_init_db_is_idempotent():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    collect_workflow_logs.init_db(connection)
    collect_workflow_logs.init_db(connection)  # should not raise
    connection.close()


def test_ingest_records_collected_at_timestamp(conn):
    before = time.time()
    collect_workflow_logs.ingest_runs(conn, [collect_workflow_logs.to_record(_raw_run(1))])
    after = time.time()

    rows = collect_workflow_logs.query_runs(conn)
    assert before <= rows[0]["collected_at"] <= after
