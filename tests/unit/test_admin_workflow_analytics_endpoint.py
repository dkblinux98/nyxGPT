"""Unit tests for GET /api/v1/admin/workflow-analytics (src/nyxgpt/app.py::admin_workflow_analytics).

The workflow_analytics module itself is mocked so these tests focus on
request/response wiring.

Related: #2844
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_admin_workflow_analytics_returns_summary():
    summary = {
        "available": True,
        "collected": True,
        "stats": {
            "window_days": 30,
            "total_runs": 5,
            "success_rate": 80.0,
            "avg_duration_s": 120.0,
            "failures": 1,
            "by_workflow": [],
            "by_day": [],
            "top_failing": [],
        },
        "recent_runs": [],
    }

    with patch(
        "nyxgpt.app.workflow_analytics_module.summary", return_value=summary
    ) as mock_summary:
        client = TestClient(app)
        response = client.get("/api/v1/admin/workflow-analytics")

    assert response.status_code == 200
    assert response.json() == summary
    mock_summary.assert_called_once_with(days=30, limit=50)


def test_admin_workflow_analytics_forwards_query_params():
    summary = {"available": True, "collected": False, "stats": None, "recent_runs": []}

    with patch(
        "nyxgpt.app.workflow_analytics_module.summary", return_value=summary
    ) as mock_summary:
        client = TestClient(app)
        response = client.get("/api/v1/admin/workflow-analytics?days=7&limit=10")

    assert response.status_code == 200
    mock_summary.assert_called_once_with(days=7, limit=10)


def test_admin_workflow_analytics_reports_unavailable_collector():
    summary = {
        "available": False,
        "reason": "collector script not found",
        "stats": None,
        "recent_runs": [],
    }

    with patch("nyxgpt.app.workflow_analytics_module.summary", return_value=summary):
        client = TestClient(app)
        response = client.get("/api/v1/admin/workflow-analytics")

    assert response.status_code == 200
    assert response.json()["available"] is False
