"""Unit tests for src/nyxgpt/workflow_log_store.py (workflow log collection/analytics).

Related: #2844, #3189
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

from nyxgpt import workflow_log_store

pytestmark = pytest.mark.unit


def _recent_window(duration_s: int = 300) -> tuple[str, str]:
    """A (created_at, updated_at) pair ending just now, `duration_s` apart.

    The "recent run" fixtures must stay inside the 30-day analytics window that
    `query_runs(since_days=...)` and `compute_summary(days=...)` apply, so they
    are anchored to the clock. A hardcoded literal date silently ages out of
    that window and starts failing the suite on a date unrelated to any change.
    Both stamps come off one `now` so the duration is exact.
    """
    updated = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    created = updated - timedelta(seconds=duration_s)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return created.strftime(fmt), updated.strftime(fmt)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    workflow_log_store.init_db(connection)
    yield connection
    connection.close()


def _raw_run(
    run_id: int,
    workflow_name: str = "Developer Agent",
    status: str = "completed",
    conclusion: str | None = "success",
    branch: str = "feat/2844-add-thing",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict:
    # Default to a run that started 10 minutes ago and finished 5 minutes ago:
    # always inside the analytics window, always exactly 300s long.
    default_created, default_updated = _recent_window()
    created_at = created_at if created_at is not None else default_created
    updated_at = updated_at if updated_at is not None else default_updated
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
        assert workflow_log_store.parse_issue_number("feat/2844-add-thing") == 2844

    def test_fix_branch(self):
        assert workflow_log_store.parse_issue_number("fix/1234-bug") == 1234

    def test_claude_issue_branch(self):
        assert workflow_log_store.parse_issue_number("claude/issue-2844-20260714-0753") == 2844

    def test_no_issue_number(self):
        assert workflow_log_store.parse_issue_number("main") is None

    def test_none_branch(self):
        assert workflow_log_store.parse_issue_number(None) is None


class TestToRecord:
    def test_computes_duration_and_issue_number(self):
        record = workflow_log_store.to_record(_raw_run(1))
        assert record["run_id"] == 1
        assert record["workflow_name"] == "Developer Agent"
        assert record["conclusion"] == "success"
        assert record["issue_number"] == 2844
        assert record["duration_s"] == pytest.approx(300.0)

    def test_missing_conclusion_becomes_none(self):
        record = workflow_log_store.to_record(_raw_run(1, conclusion=""))
        assert record["conclusion"] is None


class TestIngestAndQuery:
    def test_ingest_then_query_roundtrip(self, conn):
        records = [workflow_log_store.to_record(_raw_run(1))]
        count = workflow_log_store.ingest_runs(conn, records)
        assert count == 1

        rows = workflow_log_store.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 1
        assert rows[0]["workflow_name"] == "Developer Agent"

    def test_reingesting_same_run_id_upserts_not_duplicates(self, conn):
        workflow_log_store.ingest_runs(conn, [workflow_log_store.to_record(_raw_run(1))])
        workflow_log_store.ingest_runs(
            conn, [workflow_log_store.to_record(_raw_run(1, conclusion="failure"))]
        )

        rows = workflow_log_store.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["conclusion"] == "failure"

    def test_query_filters_by_workflow(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, workflow_name="Developer Agent")),
                workflow_log_store.to_record(_raw_run(2, workflow_name="Review Agent")),
            ],
        )

        rows = workflow_log_store.query_runs(conn, workflow="Review Agent")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_filters_by_issue_number(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, branch="feat/2844-a")),
                workflow_log_store.to_record(_raw_run(2, branch="feat/9999-b")),
            ],
        )

        rows = workflow_log_store.query_runs(conn, issue_number=2844)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 1

    def test_query_filters_by_conclusion(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, conclusion="success")),
                workflow_log_store.to_record(_raw_run(2, conclusion="failure")),
            ],
        )

        rows = workflow_log_store.query_runs(conn, conclusion="failure")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_filters_by_status(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, status="completed")),
                workflow_log_store.to_record(_raw_run(2, status="in_progress")),
            ],
        )

        rows = workflow_log_store.query_runs(conn, status="in_progress")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_filters_by_branch(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, branch="feat/2844-a")),
                workflow_log_store.to_record(_raw_run(2, branch="feat/9999-b")),
            ],
        )

        rows = workflow_log_store.query_runs(conn, branch="feat/9999-b")
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_filters_by_since_days(self, conn):
        old_run = _raw_run(1, created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:05:00Z")
        recent_run = _raw_run(2)
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(old_run),
                workflow_log_store.to_record(recent_run),
            ],
        )

        rows = workflow_log_store.query_runs(conn, since_days=30)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2

    def test_query_respects_limit(self, conn):
        workflow_log_store.ingest_runs(
            conn, [workflow_log_store.to_record(_raw_run(i)) for i in range(1, 6)]
        )
        rows = workflow_log_store.query_runs(conn, limit=2)
        assert len(rows) == 2

    def test_ingest_empty_list_is_noop(self, conn):
        assert workflow_log_store.ingest_runs(conn, []) == 0


class TestComputeSummary:
    def test_success_rate_and_avg_duration(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(_raw_run(1, conclusion="success")),
                workflow_log_store.to_record(_raw_run(2, conclusion="success")),
                workflow_log_store.to_record(_raw_run(3, conclusion="failure")),
            ],
        )
        summary = workflow_log_store.compute_summary(conn, days=30)
        assert summary["total_runs"] == 3
        assert summary["success_rate"] == pytest.approx(66.7, abs=0.1)
        assert summary["failures"] == 1
        assert summary["avg_duration_s"] == pytest.approx(300.0)

    def test_by_workflow_breakdown(self, conn):
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(
                    _raw_run(1, workflow_name="Developer Agent", conclusion="success")
                ),
                workflow_log_store.to_record(
                    _raw_run(2, workflow_name="Review Agent", conclusion="failure")
                ),
                workflow_log_store.to_record(
                    _raw_run(3, workflow_name="Review Agent", conclusion="failure")
                ),
            ],
        )
        summary = workflow_log_store.compute_summary(conn, days=30)
        by_workflow = {w["workflow"]: w for w in summary["by_workflow"]}
        assert by_workflow["Developer Agent"]["runs"] == 1
        assert by_workflow["Review Agent"]["failures"] == 2
        assert summary["top_failing"][0]["workflow"] == "Review Agent"

    def test_empty_store_returns_zeroed_summary(self, conn):
        summary = workflow_log_store.compute_summary(conn, days=30)
        assert summary["total_runs"] == 0
        assert summary["success_rate"] == 0.0
        assert summary["by_workflow"] == []
        assert summary["top_failing"] == []

    def test_runs_outside_window_excluded(self, conn):
        old_run = _raw_run(1, created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:05:00Z")
        workflow_log_store.ingest_runs(conn, [workflow_log_store.to_record(old_run)])
        summary = workflow_log_store.compute_summary(conn, days=30)
        assert summary["total_runs"] == 0


class TestPurgeOld:
    def test_purges_runs_older_than_retention(self, conn):
        old_run = _raw_run(1, created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:05:00Z")
        recent_run = _raw_run(2)
        workflow_log_store.ingest_runs(
            conn,
            [
                workflow_log_store.to_record(old_run),
                workflow_log_store.to_record(recent_run),
            ],
        )

        deleted = workflow_log_store.purge_old(conn, retention_days=90)
        assert deleted == 1

        rows = workflow_log_store.query_runs(conn)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 2


class TestRunGhCommand:
    def test_returns_stdout_on_success(self, monkeypatch):
        class FakeCompletedProcess:
            stdout = '[{"databaseId": 1}]'

        captured_calls = []

        def fake_run(cmd, capture_output, text, check):
            captured_calls.append(cmd)
            assert capture_output is True
            assert text is True
            assert check is True
            return FakeCompletedProcess()

        monkeypatch.setattr(workflow_log_store.subprocess, "run", fake_run)

        result = workflow_log_store.run_gh_command(["run", "list"])

        assert result == '[{"databaseId": 1}]'
        assert captured_calls == [["gh", "run", "list"]]

    def test_returns_empty_string_on_called_process_error(self, monkeypatch, capsys):
        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["gh", "run", "list"])

        monkeypatch.setattr(workflow_log_store.subprocess, "run", fake_run)

        result = workflow_log_store.run_gh_command(["run", "list"])

        assert result == ""
        assert "Error running gh command" in capsys.readouterr().err

    def test_returns_empty_string_when_gh_not_installed(self, monkeypatch, capsys):
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("gh: command not found")

        monkeypatch.setattr(workflow_log_store.subprocess, "run", fake_run)

        result = workflow_log_store.run_gh_command(["run", "list"])

        assert result == ""
        assert "Error running gh command" in capsys.readouterr().err


class TestFetchWorkflowRuns:
    def test_parses_json_result_and_builds_gh_args(self, monkeypatch):
        raw_runs = [_raw_run(1), _raw_run(2)]
        captured_args = []

        def fake_run_gh_command(args):
            captured_args.append(args)
            return json.dumps(raw_runs)

        monkeypatch.setattr(workflow_log_store, "run_gh_command", fake_run_gh_command)

        runs = workflow_log_store.fetch_workflow_runs("dkblinux98/nyxGPT", limit=50)

        assert runs == raw_runs
        assert captured_args == [
            [
                "run",
                "list",
                "--repo",
                "dkblinux98/nyxGPT",
                "--limit",
                "50",
                "--json",
                "databaseId,workflowName,status,conclusion,createdAt,updatedAt,url,headBranch,displayTitle",
            ]
        ]

    def test_returns_empty_list_when_gh_command_fails(self, monkeypatch):
        monkeypatch.setattr(workflow_log_store, "run_gh_command", lambda args: "")

        assert workflow_log_store.fetch_workflow_runs("dkblinux98/nyxGPT") == []


class TestCollect:
    def test_filters_to_completed_runs_by_default(self, conn, monkeypatch):
        runs = [
            _raw_run(1, status="completed", conclusion="success"),
            _raw_run(2, status="in_progress", conclusion=None),
        ]
        monkeypatch.setattr(workflow_log_store, "fetch_workflow_runs", lambda repo, limit=200: runs)

        count = workflow_log_store.collect("dkblinux98/nyxGPT", conn)
        assert count == 1
        rows = workflow_log_store.query_runs(conn)
        assert rows[0]["run_id"] == 1

    def test_include_in_progress_stores_all(self, conn, monkeypatch):
        runs = [
            _raw_run(1, status="completed", conclusion="success"),
            _raw_run(2, status="in_progress", conclusion=None),
        ]
        monkeypatch.setattr(workflow_log_store, "fetch_workflow_runs", lambda repo, limit=200: runs)

        count = workflow_log_store.collect("dkblinux98/nyxGPT", conn, include_in_progress=True)
        assert count == 2


class TestGetDbPath:
    def test_default_path_used_when_none_given(self):
        path = workflow_log_store.get_db_path(None)
        assert path == workflow_log_store.DEFAULT_DB_PATH

    def test_custom_path_creates_parent_dir(self, tmp_path):
        custom = tmp_path / "nested" / "runs.sqlite3"
        path = workflow_log_store.get_db_path(str(custom))
        assert path == custom
        assert custom.parent.exists()


def test_init_db_is_idempotent():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    workflow_log_store.init_db(connection)
    workflow_log_store.init_db(connection)  # should not raise
    connection.close()


def test_ingest_records_collected_at_timestamp(conn):
    before = time.time()
    workflow_log_store.ingest_runs(conn, [workflow_log_store.to_record(_raw_run(1))])
    after = time.time()

    rows = workflow_log_store.query_runs(conn)
    assert before <= rows[0]["collected_at"] <= after
