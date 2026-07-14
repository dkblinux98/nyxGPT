"""Unit tests for nyxgpt.usage_analytics (usage tracking / reporting).

Related: #2700
"""

from __future__ import annotations

import json
from configparser import ConfigParser

import pytest

from nyxgpt import usage_analytics

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_events():
    """Ensure each test starts with an empty in-memory event buffer."""
    usage_analytics._events.clear()
    yield
    usage_analytics._events.clear()


@pytest.fixture
def cfg(tmp_path):
    parser = ConfigParser()
    parser.add_section("logging")
    parser.set("logging", "dir", str(tmp_path))
    return parser


def test_record_returns_event_with_expected_fields(cfg):
    event = usage_analytics.record(
        session="default",
        model="llama3.1:8b",
        prompt_tokens=10,
        completion_tokens=20,
        duration_s=1.5,
        cfg=cfg,
    )

    assert event["session"] == "default"
    assert event["model"] == "llama3.1:8b"
    assert event["prompt_tokens"] == 10
    assert event["completion_tokens"] == 20
    assert event["duration_s"] == 1.5
    assert isinstance(event["ts"], float)


def test_record_clamps_negative_values(cfg):
    event = usage_analytics.record(
        session="s1", model="m1", prompt_tokens=-5, completion_tokens=-1, duration_s=-2.0, cfg=cfg
    )

    assert event["prompt_tokens"] == 0
    assert event["completion_tokens"] == 0
    assert event["duration_s"] == 0.0


def test_record_persists_to_disk(cfg):
    usage_analytics.record(
        session="s1", model="m1", prompt_tokens=1, completion_tokens=2, duration_s=0.1, cfg=cfg
    )

    log_file = usage_analytics._usage_log_path(cfg)
    assert log_file.exists()
    assert '"model": "m1"' in log_file.read_text(encoding="utf-8")


def test_recent_falls_back_to_disk_when_memory_empty(cfg):
    usage_analytics.record(
        session="s1", model="m1", prompt_tokens=1, completion_tokens=2, duration_s=0.1, cfg=cfg
    )
    usage_analytics._events.clear()

    events = usage_analytics.recent(limit=10, cfg=cfg)

    assert len(events) == 1
    assert events[0]["model"] == "m1"


def test_recent_returns_empty_list_when_nothing_recorded(cfg):
    assert usage_analytics.recent(limit=10, cfg=cfg) == []


def test_summary_aggregates_totals_and_breakdowns(cfg):
    usage_analytics.record(
        session="s1",
        model="llama3.1:8b",
        prompt_tokens=10,
        completion_tokens=5,
        duration_s=1.0,
        cfg=cfg,
    )
    usage_analytics.record(
        session="s2",
        model="llama3.1:8b",
        prompt_tokens=8,
        completion_tokens=4,
        duration_s=1.0,
        cfg=cfg,
    )
    usage_analytics.record(
        session="s1",
        model="mistral:7b",
        prompt_tokens=3,
        completion_tokens=2,
        duration_s=1.0,
        cfg=cfg,
    )

    summary = usage_analytics.summary(cfg=cfg)

    assert summary["total_requests"] == 3
    assert summary["total_prompt_tokens"] == 21
    assert summary["total_completion_tokens"] == 11
    assert summary["total_tokens"] == 32
    assert summary["session_count"] == 2

    by_model = {m["model"]: m for m in summary["by_model"]}
    assert by_model["llama3.1:8b"]["requests"] == 2
    assert by_model["llama3.1:8b"]["prompt_tokens"] == 18
    assert by_model["mistral:7b"]["requests"] == 1

    assert len(summary["by_day"]) == 1
    assert summary["by_day"][0]["requests"] == 3


def test_summary_returns_zeroed_totals_when_no_events(cfg):
    summary = usage_analytics.summary(cfg=cfg)

    assert summary["total_requests"] == 0
    assert summary["total_tokens"] == 0
    assert summary["session_count"] == 0
    assert summary["by_model"] == []
    assert summary["by_day"] == []


def test_export_report_json_includes_summary_and_events(cfg):
    usage_analytics.record(
        session="s1", model="m1", prompt_tokens=1, completion_tokens=2, duration_s=0.1, cfg=cfg
    )

    content, content_type, filename = usage_analytics.export_report("json", cfg=cfg)

    assert content_type == "application/json"
    assert filename == "usage_report.json"
    payload = json.loads(content)
    assert payload["summary"]["total_requests"] == 1
    assert len(payload["events"]) == 1
    assert payload["events"][0]["model"] == "m1"


def test_export_report_csv_includes_header_and_rows(cfg):
    usage_analytics.record(
        session="s1", model="m1", prompt_tokens=1, completion_tokens=2, duration_s=0.1, cfg=cfg
    )

    content, content_type, filename = usage_analytics.export_report("csv", cfg=cfg)

    assert content_type == "text/csv"
    assert filename == "usage_report.csv"
    lines = content.strip().splitlines()
    assert lines[0] == "ts,session,model,prompt_tokens,completion_tokens,duration_s"
    assert "s1" in lines[1]
    assert "m1" in lines[1]


def test_export_report_rejects_unsupported_format(cfg):
    with pytest.raises(ValueError):
        usage_analytics.export_report("xml", cfg=cfg)
