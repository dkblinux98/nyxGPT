"""dump_issue_corpus.py writes the two inputs the refresh session used to hand-write.

The corpus and the merged-PR map were the last dashboard inputs produced
outside a workflow, which is what let the refresh drift into producing the
others locally too (2026-08-17 → 09-17). These pin the filters and the shapes
build_dashboard.py reads, with `gh` stubbed -- the walk itself is exercised
by the live retro_data_refresh.yml run cited in the PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRO = REPO_ROOT / "scripts" / "retrospective"


@pytest.fixture
def corpus(monkeypatch):
    monkeypatch.syspath_prepend(str(RETRO))
    spec = importlib.util.spec_from_file_location(
        "dump_issue_corpus", RETRO / "dump_issue_corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dump_issue_corpus"] = module
    spec.loader.exec_module(module)
    return module


def _issue(n, created, **extra):
    base = {
        "number": n,
        "title": f"issue {n}",
        "labels": [{"name": "Feature"}],
        "milestone": {"title": "Phase 6"},
        "created_at": created,
        "state": "open",
        "closed_at": None,
    }
    base.update(extra)
    return base


def test_fetch_issues_keeps_only_issues_created_in_the_window(corpus, monkeypatch):
    pages = [
        _issue(10, "2025-12-31T23:59:59Z"),  # before coverage, even though `since` returned it
        _issue(11, "2026-01-01T00:00:00Z", state="closed", closed_at="2026-01-02T00:00:00Z"),
        _issue(12, "2026-02-01T00:00:00Z", pull_request={"url": "x"}),  # a PR, not an issue
        _issue(13, "2026-03-01T00:00:00Z", milestone=None, labels=[]),
    ]
    monkeypatch.setattr(corpus, "gh", lambda *a: json.dumps(pages[:2]) + json.dumps(pages[2:]))
    issues = corpus.fetch_issues("o/r")
    assert [i["n"] for i in issues] == [11, 13]
    assert issues[0] == {
        "n": 11,
        "title": "issue 11",
        "labels": ["Feature"],
        "milestone": "Phase 6",
        "created": "2026-01-01T00:00:00Z",
        "state": "closed",
        "closed": "2026-01-02T00:00:00Z",
    }
    assert issues[1]["milestone"] is None and issues[1]["labels"] == []


def test_fetch_issues_walks_the_list_endpoint_not_search(corpus, monkeypatch):
    """Search caps a query at 1,000 results; the corpus will outgrow that."""
    seen = []

    def fake_gh(*args):
        seen.append(args)
        return "[]"

    monkeypatch.setattr(corpus, "gh", fake_gh)
    corpus.fetch_issues("o/r")
    assert seen and "repos/o/r/issues" in seen[0]
    assert not any("search/issues" in a for a in seen[0])
    assert "--paginate" in seen[0]


def test_fetch_pr_times_keeps_merged_prs_in_the_window_sorted_by_number(corpus, monkeypatch):
    pulls = [
        {"number": 30, "created_at": "2026-02-01T00:00:00Z", "merged_at": "2026-02-02T00:00:00Z"},
        {"number": 20, "created_at": "2026-01-01T00:00:00Z", "merged_at": None},  # closed unmerged
        {"number": 5, "created_at": "2025-12-01T00:00:00Z", "merged_at": "2025-12-20T00:00:00Z"},
        {"number": 25, "created_at": "2025-12-30T00:00:00Z", "merged_at": "2026-01-01T00:00:00Z"},
    ]
    monkeypatch.setattr(corpus, "gh", lambda *a: json.dumps(pulls))
    prs = corpus.fetch_pr_times("o/r")
    assert list(prs) == ["25", "30"]
    assert prs["25"] == ["2025-12-30T00:00:00Z", "2026-01-01T00:00:00Z"]


def test_carry_related_preserves_the_historical_fallback(corpus, tmp_path):
    existing = tmp_path / "all_issues.json"
    existing.write_text(
        json.dumps({"generated_at": "x", "issues": [{"n": 11, "related": 7}, {"n": 12}]})
    )
    fresh = [{"n": 11, "title": "a"}, {"n": 12, "title": "b"}, {"n": 13, "title": "c"}]
    out = corpus.carry_related(fresh, existing)
    assert out[0]["related"] == 7
    assert "related" not in out[1] and "related" not in out[2]


def test_carry_related_reads_the_bare_list_shape_too(corpus, tmp_path):
    existing = tmp_path / "all_issues.json"
    existing.write_text(json.dumps([{"n": 11, "related": 7}]))
    assert corpus.carry_related([{"n": 11}], existing)[0]["related"] == 7


def test_main_writes_both_files_stamped(corpus, monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, "DATA_DIR", tmp_path)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setattr(corpus, "fetch_issues", lambda repo: [{"n": 1, "title": "t"}])
    monkeypatch.setattr(corpus, "fetch_pr_times", lambda repo: {"2": ["a", "b"]})
    corpus.main()
    issues = json.loads((tmp_path / "all_issues.json").read_text())
    prs = json.loads((tmp_path / "pr_times.json").read_text())
    assert issues["issues"] == [{"n": 1, "title": "t"}]
    assert prs["prs"] == {"2": ["a", "b"]}
    assert issues["generated_at"] == prs["generated_at"]
    assert issues["generated_at"].endswith("+00:00")
