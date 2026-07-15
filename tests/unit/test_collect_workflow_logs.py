"""Unit tests for scripts/collect_workflow_logs.py (the CLI wrapper).

The storage/query/analytics logic itself lives in `nyxgpt.workflow_log_store`
and is covered by tests/unit/test_workflow_log_store.py; these tests only
exercise the CLI's argument parsing and its wiring to that module.

Related: #2844, #3189
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nyxgpt import workflow_log_store

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "collect_workflow_logs.py"
_spec = importlib.util.spec_from_file_location("collect_workflow_logs", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
collect_workflow_logs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = collect_workflow_logs
_spec.loader.exec_module(collect_workflow_logs)


def _raw_run(run_id: int, conclusion: str = "success") -> dict:
    return {
        "databaseId": run_id,
        "workflowName": "Developer Agent",
        "status": "completed",
        "conclusion": conclusion,
        "headBranch": "feat/2844-add-thing",
        "createdAt": "2026-07-14T07:00:00Z",
        "updatedAt": "2026-07-14T07:05:00Z",
        "url": f"https://github.com/dkblinux98/nyxGPT/actions/runs/{run_id}",
        "displayTitle": "some commit message",
    }


def test_cli_reuses_the_installed_store_module():
    assert collect_workflow_logs.store is workflow_log_store
    assert collect_workflow_logs.DEFAULT_DB_PATH == workflow_log_store.DEFAULT_DB_PATH


def test_collect_command_delegates_to_store(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(
        workflow_log_store, "fetch_workflow_runs", lambda repo, limit=200: [_raw_run(1)]
    )

    rc = collect_workflow_logs.main(
        ["--db", str(db_path), "collect", "--repo", "dkblinux98/nyxGPT"]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Collected 1 workflow run(s)" in out

    conn = workflow_log_store.get_connection(db_path)
    try:
        rows = workflow_log_store.query_runs(conn)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["run_id"] == 1


def test_stats_command_prints_json_summary(tmp_path, capsys):
    db_path = tmp_path / "runs.sqlite3"
    conn = workflow_log_store.get_connection(db_path)
    workflow_log_store.ingest_runs(conn, [workflow_log_store.to_record(_raw_run(1))])
    conn.close()

    rc = collect_workflow_logs.main(["--db", str(db_path), "stats", "--json"])

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total_runs"] == 1
    assert summary["success_rate"] == 100.0


def test_purge_command_deletes_old_runs(tmp_path, capsys):
    db_path = tmp_path / "runs.sqlite3"
    conn = workflow_log_store.get_connection(db_path)
    old_run = _raw_run(1)
    old_run["createdAt"] = "2020-01-01T00:00:00Z"
    old_run["updatedAt"] = "2020-01-01T00:05:00Z"
    workflow_log_store.ingest_runs(conn, [workflow_log_store.to_record(old_run)])
    conn.close()

    rc = collect_workflow_logs.main(["--db", str(db_path), "purge", "--retention-days", "90"])

    assert rc == 0
    assert "Purged 1 run(s)" in capsys.readouterr().out
