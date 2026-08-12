"""Unit tests for the native issue-relationship model (#3731).

Owner decision 2026-08-12: the link between an issue filed during acceptance
testing (`@acceptance-failure` / `@improvement`) and the issue it was filed
against is a GitHub **native** blocked-by/blocks relationship — never body
prose, never a comment marker. These cover the pure half of that model
(`scripts/agents/lib/issue_relationships.py`):

  * `parse_related_feature_prose` — the retired marker, read-only fallback
  * `resolve_related_feature(s)` — native first, prose only when no edge
  * `transitive_*` — "and transitively anything blocked by that one",
    including cycle safety
  * `feature_blockers` — the map the promotion sweep gates on

plus the bash half (`tests/test_issue_relationships_lib.sh`), run end to end
with `gh` stubbed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "issue_relationships.py"
_spec = importlib.util.spec_from_file_location("issue_relationships", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rel
_spec.loader.exec_module(rel)


# --- retired prose marker (historical fallback only) ------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Related feature: #3730", 3730),
        ("Parent feature: #3521", 3521),
        ("intro\n\nRelated feature: #42\nmore", 42),
        ("related feature: #7", 7),
        ("Related feature: #3730\nRelated feature: #3729", 3730),
        ("", None),
        (None, None),
        ("mentions #3730 in passing", None),
        ("Blocked by: #3730", None),
    ],
)
def test_parse_related_feature_prose(body, expected):
    assert rel.parse_related_feature_prose(body) == expected


# --- native first, prose fallback -------------------------------------


def test_native_edge_wins_over_prose():
    """A native edge is the storage; stale prose must never override it."""
    assert rel.resolve_related_feature(blocks=[3730], body="Related feature: #99") == 3730


def test_prose_used_only_when_no_native_edge():
    assert rel.resolve_related_feature(blocks=[], body="Related feature: #99") == 99
    assert rel.resolve_related_feature(blocks=None, body="Related feature: #99") == 99


def test_no_edge_and_no_prose_resolves_to_nothing():
    assert rel.resolve_related_feature(blocks=[], body="a plain feature issue") is None
    assert rel.resolve_related_features(blocks=[], body=None) == []


def test_accepts_raw_rest_dependency_shape():
    """`/dependencies/blocking` returns objects, not bare numbers."""
    payload = [{"number": 3730, "state": "open"}, {"number": 3729, "state": "closed"}]
    assert rel.resolve_related_features(blocks=payload) == [3730, 3729]


def test_duplicate_edges_collapse():
    assert rel.resolve_related_features(blocks=[7, 7, {"number": 7}, 8]) == [7, 8]


# --- transitivity ------------------------------------------------------


def test_transitive_blockers_walks_the_chain():
    """A failure filed against a failure gates the original issue too."""
    blocked_by = {3730: [3733], 3733: [3740], 3740: []}
    assert rel.transitive_blockers(3730, blocked_by) == [3733, 3740]


def test_transitive_blocked_walks_the_other_direction():
    blocks = {3740: [3733], 3733: [3730], 3730: []}
    assert rel.transitive_blocked(3740, blocks) == [3730, 3733]


def test_transitive_excludes_the_root():
    assert rel.transitive_blockers(1, {1: [2], 2: [1]}) == [2]


def test_transitive_terminates_on_a_cycle():
    """A mis-entered A-blocks-B-blocks-A pair must not hang the sweep."""
    assert rel.transitive_blockers(1, {1: [2], 2: [3], 3: [2]}) == [2, 3]


def test_transitive_on_unknown_root_is_empty():
    assert rel.transitive_blockers(999, {1: [2]}) == []


def test_transitive_tolerates_string_keys_and_rest_shapes():
    edges = {"1": [{"number": 2}], "2": [{"number": 3}]}
    assert rel.transitive_blockers(1, edges) == [2, 3]


# --- the gate map the promotion sweep uses ----------------------------


def test_feature_blockers_groups_by_target():
    records = [
        {"number": 3733, "blocks": [3730], "body": ""},
        {"number": 3740, "blocks": [3730], "body": ""},
        {"number": 3741, "blocks": [3729], "body": ""},
    ]
    assert rel.feature_blockers(records) == {3730: [3733, 3740], 3729: [3741]}


def test_feature_blockers_falls_back_to_prose_for_historical_issues():
    records = [
        {"number": 3733, "blocks": [], "body": "Related feature: #3730"},
        {"number": 3740, "blocks": [3730], "body": ""},
    ]
    assert rel.feature_blockers(records) == {3730: [3733, 3740]}


def test_feature_blockers_ignores_self_reference():
    assert rel.feature_blockers([{"number": 5, "blocks": [5]}]) == {}


def test_feature_blockers_of_nothing_is_empty():
    assert rel.feature_blockers([]) == {}


# --- CLI (the surface bash calls) -------------------------------------


def _run_cli(args, stdin=""):
    return subprocess.run(
        [sys.executable, str(_MODULE_PATH), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_resolve_related_prefers_native():
    out = _run_cli(
        ["resolve-related"],
        json.dumps({"blocks": [3730], "body": "Related feature: #99"}),
    )
    assert out.returncode == 0
    assert out.stdout.split() == ["3730"]


def test_cli_resolve_related_falls_back_to_prose():
    out = _run_cli(["resolve-related"], json.dumps({"blocks": [], "body": "Related feature: #99"}))
    assert out.stdout.split() == ["99"]


def test_cli_parse_prose_prints_nothing_when_absent():
    assert _run_cli(["parse-prose"], "no marker here").stdout == ""


def test_cli_transitive():
    out = _run_cli(["transitive", "1"], json.dumps({"1": [2], "2": [3]}))
    assert out.stdout.split() == ["2", "3"]


def test_cli_feature_blockers():
    out = _run_cli(["feature-blockers"], json.dumps([{"number": 2, "blocks": [1]}]))
    assert json.loads(out.stdout) == {"1": [2]}


def test_cli_bad_usage_exits_two():
    assert _run_cli([]).returncode == 2
    assert _run_cli(["nonsense"]).returncode == 2


class TestShellHelpers:
    """Runs the bash half (`gh_project.sh` relationship helpers) end to end.

    The shell suite stubs `gh`, so this exercises the real helpers — including
    the transitive walk and the native-first/prose-fallback resolution —
    without touching GitHub. Wired into pytest because `pytest -v` is the gate
    this repo actually runs; `bash tests/test_issue_relationships_lib.sh`
    still works for local debugging.
    """

    def test_shell_suite_passes(self):
        suite = REPO_ROOT / "tests" / "test_issue_relationships_lib.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
