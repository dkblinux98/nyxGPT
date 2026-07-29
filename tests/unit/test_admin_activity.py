"""Unit tests for nyxgpt.admin_activity (admin dashboard audit trail).

Related: #2698
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import admin_activity

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_events():
    """Ensure each test starts with an empty in-memory event buffer."""
    admin_activity._events.clear()
    yield
    admin_activity._events.clear()


@pytest.fixture
def cfg(tmp_path):
    parser = ConfigParser()
    parser.add_section("logging")
    parser.set("logging", "dir", str(tmp_path))
    return parser


def test_record_returns_event_with_action_and_detail(cfg):
    event = admin_activity.record("config.updated", "default_model changed", cfg=cfg)

    assert event["action"] == "config.updated"
    assert event["detail"] == "default_model changed"
    assert isinstance(event["ts"], float)


def test_recent_returns_events_in_order(cfg):
    admin_activity.record("a", "first", cfg=cfg)
    admin_activity.record("b", "second", cfg=cfg)

    events = admin_activity.recent(limit=50, cfg=cfg)

    assert [e["action"] for e in events] == ["a", "b"]


def test_recent_respects_limit(cfg):
    for i in range(5):
        admin_activity.record(f"action-{i}", "detail", cfg=cfg)

    events = admin_activity.recent(limit=2, cfg=cfg)

    assert [e["action"] for e in events] == ["action-3", "action-4"]


def test_record_persists_to_disk(cfg):
    admin_activity.record("canary.deploy", "Deployed nyxgpt-api:1.2.3-abcd123", cfg=cfg)

    log_file = admin_activity._activity_log_path(cfg)
    assert log_file.exists()
    assert "canary.deploy" in log_file.read_text(encoding="utf-8")


def test_recent_falls_back_to_disk_when_memory_empty(cfg):
    admin_activity.record("canary.start", "10%", cfg=cfg)
    # Simulate a fresh process: in-memory buffer is empty but the file remains.
    admin_activity._events.clear()

    events = admin_activity.recent(limit=10, cfg=cfg)

    assert len(events) == 1
    assert events[0]["action"] == "canary.start"


def test_recent_returns_empty_list_when_nothing_recorded(cfg):
    assert admin_activity.recent(limit=10, cfg=cfg) == []


def test_record_swallows_oserror_when_disk_write_fails(tmp_path):
    """If the log dir can't be created (e.g. a path component is a file), record()
    must not raise -- activity logging is best-effort."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    parser = ConfigParser()
    parser.add_section("logging")
    parser.set("logging", "dir", str(blocker / "logs"))

    event = admin_activity.record("config.updated", "detail", cfg=parser)

    # The in-memory event was still recorded despite the disk failure.
    assert event["action"] == "config.updated"
    assert list(admin_activity._events)[-1] == event
    # No log directory could have been created since `blocker` is a file.
    assert not (blocker / "logs").exists()


def test_load_from_disk_swallows_oserror_on_read(cfg, monkeypatch):
    """If the on-disk log file exists but can't be read, _load_from_disk must
    return an empty list rather than propagating the error."""
    admin_activity.record("canary.start", "10%", cfg=cfg)
    admin_activity._events.clear()

    def _raise(self, *args, **kwargs):
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", _raise)

    events = admin_activity.recent(limit=10, cfg=cfg)

    assert events == []


def test_recent_from_disk_skips_lines_with_invalid_json(cfg):
    """Malformed JSONL lines (e.g. a partial write) must be skipped, not raise,
    and well-formed lines around them must still be returned."""
    log_file = admin_activity._activity_log_path(cfg)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("not valid json\n" '{"ts": 1.0, "action": "ok", "detail": "d"}\n')

    events = admin_activity.recent(limit=10, cfg=cfg)

    assert len(events) == 1
    assert events[0]["action"] == "ok"
