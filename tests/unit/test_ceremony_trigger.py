"""Unit tests for scripts/agents/lib/ceremony_trigger.py (#3730).

The release ceremony is irreversible (master fast-forward, tag, GitHub
Release, PyPI publish), so its trigger guardrails are the part worth
pinning down: it fires for the release tracking issue only, on the
transition into `For Release` only, and only with a parseable version.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "ceremony_trigger.py"
)
_spec = importlib.util.spec_from_file_location("ceremony_trigger", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
ceremony_trigger = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ceremony_trigger
_spec.loader.exec_module(ceremony_trigger)


def _state(**overrides):
    state = {
        "issue": 3521,
        "release_issue": 3521,
        "status": "For Release",
        "for_release_status": "For Release",
        "title": "Release v3.0.0 — Phase 6",
        "already_fired": False,
    }
    state.update(overrides)
    return state


def test_fires_when_the_release_issue_moves_to_for_release():
    result = ceremony_trigger.decide(_state())
    assert result["fire"] is True
    assert result["version"] == "3.0.0"


def test_does_not_fire_for_any_other_issue():
    result = ceremony_trigger.decide(_state(issue=3730))
    assert result["fire"] is False
    assert "not the release tracking issue" in result["reason"]


def test_does_not_fire_for_another_status():
    for status in ("Acceptance Testing", "Backlog", "Done", ""):
        result = ceremony_trigger.decide(_state(status=status))
        assert result["fire"] is False, status


def test_does_not_fire_twice_for_the_same_version():
    """The watcher polls; without the marker check a finished release would
    re-run the whole ceremony on every poll."""
    result = ceremony_trigger.decide(_state(already_fired=True))
    assert result["fire"] is False
    assert "already started" in result["reason"]
    assert result["version"] == "3.0.0"


def test_does_not_fire_without_a_parseable_version():
    result = ceremony_trigger.decide(_state(title="Release tracking issue"))
    assert result["fire"] is False
    assert "conservative stop" in result["reason"]


def test_does_not_fire_without_a_configured_release_issue():
    result = ceremony_trigger.decide(_state(release_issue=None))
    assert result["fire"] is False


def test_honors_a_renamed_for_release_option():
    result = ceremony_trigger.decide(_state(status="Ready to Ship", for_release_status="Ready to Ship"))
    assert result["fire"] is True


def test_issue_numbers_compare_across_string_and_int():
    assert ceremony_trigger.decide(_state(issue="3521", release_issue=3521))["fire"] is True


def test_marker_is_version_scoped():
    assert ceremony_trigger.marker_for("3.0.0") == "<!-- nyxgpt-release-ceremony:3.0.0 -->"
    assert ceremony_trigger.marker_for("3.0.0") != ceremony_trigger.marker_for("3.1.0")
