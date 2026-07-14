"""Unit tests for src/nyxgpt/workflow_analytics.py (admin dashboard CI analytics).

Related: #2844
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import workflow_analytics

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_loaded_collector():
    """Ensure each test loads the collector module fresh rather than reusing a cached one."""
    sys.modules.pop(workflow_analytics._MODULE_NAME, None)
    yield
    sys.modules.pop(workflow_analytics._MODULE_NAME, None)


def test_summary_reports_unavailable_when_script_missing():
    with patch.object(
        workflow_analytics, "_SCRIPT_PATH", Path("/nonexistent/collect_workflow_logs.py")
    ):
        result = workflow_analytics.summary()

    assert result == {
        "available": False,
        "reason": "collector script not found",
        "stats": None,
        "recent_runs": [],
    }


def test_summary_reports_not_collected_when_db_missing(tmp_path):
    collector = workflow_analytics._load_collector()
    missing_db = tmp_path / "does-not-exist" / "workflow_runs.sqlite3"

    with patch.object(collector, "get_db_path", return_value=missing_db):
        result = workflow_analytics.summary()

    assert result == {"available": True, "collected": False, "stats": None, "recent_runs": []}


def test_summary_aggregates_stats_and_recent_runs(tmp_path):
    collector = workflow_analytics._load_collector()
    db_path = tmp_path / "workflow_runs.sqlite3"

    conn = collector.get_connection(db_path)
    now = time.time()
    collector.ingest_runs(
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

    with patch.object(collector, "get_db_path", return_value=db_path):
        result = workflow_analytics.summary(days=30, limit=10)

    assert result["available"] is True
    assert result["collected"] is True
    assert result["stats"]["total_runs"] == 1
    assert result["stats"]["success_rate"] == 100.0
    assert len(result["recent_runs"]) == 1
    assert result["recent_runs"][0]["run_id"] == 1
