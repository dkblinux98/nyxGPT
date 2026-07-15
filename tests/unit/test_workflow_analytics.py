"""Unit tests for src/nyxgpt/workflow_analytics.py (admin dashboard CI analytics).

Related: #2844, #3189
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import workflow_analytics, workflow_log_store

pytestmark = pytest.mark.unit


def test_summary_reports_unavailable_when_store_path_cannot_be_resolved():
    with patch.object(
        workflow_log_store, "get_db_path", side_effect=OSError("read-only filesystem")
    ):
        result = workflow_analytics.summary()

    assert result["available"] is False
    assert "read-only filesystem" in result["reason"]
    assert result["stats"] is None
    assert result["recent_runs"] == []


def test_summary_reports_not_collected_when_db_missing(tmp_path):
    missing_db = tmp_path / "does-not-exist" / "workflow_runs.sqlite3"

    with patch.object(workflow_log_store, "get_db_path", return_value=missing_db):
        result = workflow_analytics.summary()

    assert result == {"available": True, "collected": False, "stats": None, "recent_runs": []}


def test_summary_aggregates_stats_and_recent_runs(tmp_path):
    db_path = tmp_path / "workflow_runs.sqlite3"

    conn = workflow_log_store.get_connection(db_path)
    now = time.time()
    workflow_log_store.ingest_runs(
        conn,
        [
            {
                "run_id": 1,
                "workflow_name": "Developer Agent",
                "status": "completed",
                "conclusion": "success",
                "branch": "feat/2844-slug",
                "issue_number": 2844,
                "title": "feat: thing",
                "url": "https://example.com/runs/1",
                "created_at": now - 60,
                "updated_at": now,
                "duration_s": 60.0,
                "collected_at": now,
            }
        ],
    )
    conn.close()

    with patch.object(workflow_log_store, "get_db_path", return_value=db_path):
        result = workflow_analytics.summary(days=30, limit=10)

    assert result["available"] is True
    assert result["collected"] is True
    assert result["stats"]["total_runs"] == 1
    assert result["stats"]["success_rate"] == 100.0
    assert len(result["recent_runs"]) == 1
    assert result["recent_runs"][0]["run_id"] == 1


def test_summary_works_from_an_installed_layout_without_the_repo_scripts_dir(tmp_path):
    """Regression test for #3189: analytics must not depend on a sibling
    `scripts/` directory that only exists in a source checkout.

    Simulates a deployed/installed package by copying just `src/nyxgpt` into an
    isolated directory (no `scripts/`, no repo root) and importing it there,
    via a subprocess with a `PYTHONPATH` restricted to that directory only.
    """
    repo_root = Path(__file__).resolve().parents[2]
    fake_site_packages = tmp_path / "site-packages"
    shutil.copytree(repo_root / "src" / "nyxgpt", fake_site_packages / "nyxgpt")
    assert not (fake_site_packages / "scripts").exists()
    assert not (tmp_path / "scripts").exists()

    script = (
        "from nyxgpt import workflow_analytics; "
        "import json; "
        "print(json.dumps(workflow_analytics.summary()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(fake_site_packages), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "collector script not found" not in result.stdout
