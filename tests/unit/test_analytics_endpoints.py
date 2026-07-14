"""Unit tests for the /api/v1/analytics/* endpoints (usage analytics).

Covers src/nyxgpt/app.py's analytics_usage/analytics_export route handlers.
The usage_analytics module itself is mocked so these tests focus on
request/response wiring.

Related: #2700
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_analytics_usage_returns_summary():
    summary = {
        "total_requests": 3,
        "total_prompt_tokens": 30,
        "total_completion_tokens": 15,
        "total_tokens": 45,
        "session_count": 2,
        "by_model": [
            {"model": "llama3.1:8b", "requests": 3, "prompt_tokens": 30, "completion_tokens": 15}
        ],
        "by_day": [
            {"date": "2026-07-14", "requests": 3, "prompt_tokens": 30, "completion_tokens": 15}
        ],
    }

    with patch("nyxgpt.app.usage_analytics_module.summary", return_value=summary) as mock_summary:
        client = TestClient(app)
        response = client.get("/api/v1/analytics/usage")

    assert response.status_code == 200
    assert response.json() == summary
    mock_summary.assert_called_once()


def test_analytics_export_defaults_to_json():
    with patch(
        "nyxgpt.app.usage_analytics_module.export_report",
        return_value=('{"summary": {}}', "application/json", "usage_report.json"),
    ) as mock_export:
        client = TestClient(app)
        response = client.get("/api/v1/analytics/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert 'filename="usage_report.json"' in response.headers["content-disposition"]
    args, _ = mock_export.call_args
    assert args[0] == "json"


def test_analytics_export_csv_sets_content_type_and_disposition():
    csv_content = "ts,session,model,prompt_tokens,completion_tokens,duration_s\n1.0,s1,m1,1,2,0.1\n"
    with patch(
        "nyxgpt.app.usage_analytics_module.export_report",
        return_value=(csv_content, "text/csv", "usage_report.csv"),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/analytics/export?format=csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="usage_report.csv"' in response.headers["content-disposition"]
    assert response.text == csv_content


def test_analytics_export_rejects_unsupported_format():
    with patch(
        "nyxgpt.app.usage_analytics_module.export_report",
        side_effect=ValueError("Unsupported export format: 'xml'. Use 'json' or 'csv'."),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/analytics/export?format=xml")

    assert response.status_code == 400
    assert "Unsupported export format" in response.json()["error"]["message"]
