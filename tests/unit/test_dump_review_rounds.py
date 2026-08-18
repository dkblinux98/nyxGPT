"""Unit tests for scripts/retrospective/dump_review_rounds.py (#3667).

Only covers the pure parsing/aggregation logic (no `gh` subprocess calls):
the review-agent body parser and the dashboard-snapshot aggregator that
replace the Gmail notification-email parse with the GitHub PR-review API.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRO_DIR = REPO_ROOT / "scripts" / "retrospective"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dump_review_rounds():
    sys.path.insert(0, str(RETRO_DIR))
    try:
        _load("build_dashboard", RETRO_DIR / "build_dashboard.py")
        return _load("dump_review_rounds", RETRO_DIR / "dump_review_rounds.py")
    finally:
        sys.path.remove(str(RETRO_DIR))


REQUEST_CHANGES_BODY = """## Code Review - REQUEST_CHANGES

### Summary
Some summary text.

### Critical Issues
- **Vitest failure is a real regression**: full description here.
- **Homebrew acceptance criteria unaddressed**: description.
  - `homebrew/nyxgpt-api.rb`: nested detail, not a separate finding.

### Medium Issues (if any)
- **`nyxgpt ops down` can mismark components**: description.

#### Minor Issues
- Plain-text finding with no bold lead-in, full sentence kept as the title.
- None — this one starts with "None" but isn't the bare filler line.

### Decision
**REQUEST_CHANGES**
"""

APPROVE_BODY = """## Code Review - APPROVE

### Summary
Looks good.

### Decision
**APPROVE**
"""


class TestParseReviewBody:
    def test_non_request_changes_returns_none(self, dump_review_rounds):
        assert dump_review_rounds.parse_review_body(APPROVE_BODY) is None
        assert dump_review_rounds.parse_review_body("") is None
        assert dump_review_rounds.parse_review_body(None) is None

    def test_extracts_bold_titles_for_critical_and_medium(self, dump_review_rounds):
        findings = dump_review_rounds.parse_review_body(REQUEST_CHANGES_BODY)
        assert findings["critical"] == [
            "Vitest failure is a real regression",
            "Homebrew acceptance criteria unaddressed",
        ]
        assert findings["medium"] == ["`nyxgpt ops down` can mismark components"]

    def test_nested_bullets_are_not_separate_findings(self, dump_review_rounds):
        findings = dump_review_rounds.parse_review_body(REQUEST_CHANGES_BODY)
        assert not any("homebrew/nyxgpt-api.rb" in f for f in findings["critical"])

    def test_plain_bullet_without_bold_uses_whole_line(self, dump_review_rounds):
        findings = dump_review_rounds.parse_review_body(REQUEST_CHANGES_BODY)
        assert findings["minor"] == [
            "Plain-text finding with no bold lead-in, full sentence kept as the title.",
            'None — this one starts with "None" but isn\'t the bare filler line.',
        ]

    def test_bare_none_line_is_not_a_finding(self, dump_review_rounds):
        body = REQUEST_CHANGES_BODY.replace(
            '- None — this one starts with "None" but isn\'t the bare filler line.\n',
            "None.\n",
        )
        findings = dump_review_rounds.parse_review_body(body)
        assert findings["minor"] == [
            "Plain-text finding with no bold lead-in, full sentence kept as the title.",
        ]

    def test_one_round_per_review_id(self, dump_review_rounds):
        # Each review object is parsed independently; a PR with N REQUEST_CHANGES
        # reviews yields N parsed rounds regardless of shared content.
        reviews = [
            {"id": 1, "body": REQUEST_CHANGES_BODY},
            {"id": 2, "body": REQUEST_CHANGES_BODY},
            {"id": 3, "body": APPROVE_BODY},
        ]
        parsed = [dump_review_rounds.parse_review_body(r["body"]) for r in reviews]
        assert sum(1 for f in parsed if f is not None) == 2


class TestIterJsonObjects:
    def test_splits_concatenated_paginated_output(self, dump_review_rounds):
        text = '[{"a": 1}][{"a": 2}, {"a": 3}]'
        pages = list(dump_review_rounds.iter_json_objects(text))
        assert pages == [[{"a": 1}], [{"a": 2}, {"a": 3}]]

    def test_empty_text_yields_nothing(self, dump_review_rounds):
        assert list(dump_review_rounds.iter_json_objects("   ")) == []

    # --- #3808 regression ---------------------------------------------------
    # `raw_decode` returns the ABSOLUTE index where a document ended, not a
    # length. The old `idx += end` was therefore right only for the first
    # document and overshot for every one after it, either running past the
    # end (silently dropping the rest, exit 0) or landing mid-document and
    # raising "Expecting value". THREE pages is the minimum that detects it:
    # with two, the overshoot lands exactly at len(text) and the loop exits
    # having yielded both, so a 2-page fixture passes against the bug -- which
    # is why the two cases above did not catch it.

    def test_three_pages_are_all_yielded(self, dump_review_rounds):
        text = "".join(f'[{{"a": {i}}}]' for i in range(1, 4))
        pages = list(dump_review_rounds.iter_json_objects(text))
        assert pages == [[{"a": 1}], [{"a": 2}], [{"a": 3}]], (
            "pages after the second were dropped -- raw_decode's return value "
            "is an absolute index, not a length (#3808)"
        )

    def test_many_pages_of_differing_length_are_all_yielded(self, dump_review_rounds):
        # Uneven page sizes: an overshoot lands mid-document rather than past
        # the end, which is the variant that raised in the spend dump.
        payloads = [
            [{"id": 1}],
            [{"id": 2}, {"id": 3}, {"id": 4}],
            [{"id": 5}],
            [{"id": 6}, {"id": 7}],
        ]
        text = "".join(json.dumps(p) for p in payloads)
        assert list(dump_review_rounds.iter_json_objects(text)) == payloads

    def test_whitespace_between_pages_is_tolerated(self, dump_review_rounds):
        text = '[{"a": 1}]\n [{"a": 2}]\n\t[{"a": 3}]\n'
        assert list(dump_review_rounds.iter_json_objects(text)) == [
            [{"a": 1}],
            [{"a": 2}],
            [{"a": 3}],
        ]

    def test_single_implementation_shared_across_dumps(self, dump_review_rounds):
        """The helper is defined once (#3808) -- three copies is how one of
        them kept a bug the others had fixed."""
        import dump_spend

        assert dump_review_rounds.iter_json_objects is dump_spend.iter_json_objects

    # --- a bad page is named, never skipped (#3808) --------------------------
    # The original failure was a bare JSONDecodeError quoting a character
    # offset into an anonymous 3.9 MB string. Whatever the cause, the dump has
    # to say which endpoint, how far it got, and what the bytes actually were.

    def test_malformed_page_raises_a_named_error(self, dump_review_rounds):
        import dump_spend

        text = '[{"a": 1}][{"a": 2}]<html>rate limit exceeded</html>'
        with pytest.raises(dump_spend.PaginatedJSONError) as exc:
            list(dump_review_rounds.iter_json_objects(text, source="pulls of owner/repo"))

        err = exc.value
        assert err.source == "pulls of owner/repo"
        assert err.pages_read == 2  # two pages decoded before the break
        assert err.offset == text.index("<html>")
        assert err.total == len(text)
        assert "<html>rate limit exceeded" in err.snippet
        message = str(err)
        assert "pulls of owner/repo" in message
        assert "2 page(s) decoded cleanly" in message
        assert "rate limit exceeded" in message

    def test_truncated_final_page_is_an_error_not_a_short_result(self, dump_review_rounds):
        """A stream cut at a buffer boundary must not read as "that's all the
        data there was" -- that is the silent-undercount half of #3808."""
        import dump_spend

        text = '[{"a": 1}][{"a": 2}, {"a": 3'
        with pytest.raises(dump_spend.PaginatedJSONError) as exc:
            list(dump_review_rounds.iter_json_objects(text))
        assert exc.value.pages_read == 1
        assert "gh --paginate output" in str(exc.value)

    def test_the_error_is_a_valueerror(self, dump_review_rounds):
        """Callers that already guard json parsing keep working."""
        import dump_spend

        assert issubclass(dump_spend.PaginatedJSONError, ValueError)

    def test_a_bad_page_never_yields_silently(self, dump_review_rounds):
        """Pages before the break are still yielded, but the consumer cannot
        finish the loop without seeing the error."""
        import dump_spend

        text = '[{"a": 1}]not json at all'
        seen = []
        with pytest.raises(dump_spend.PaginatedJSONError):
            for page in dump_review_rounds.iter_json_objects(text):
                seen.append(page)
        assert seen == [[{"a": 1}]]


class TestBuildDashboardSnapshot:
    def _round(self, issue, pr, date, module="web-ui", critical=(), medium=(), minor=()):
        return {
            "issue": issue,
            "pr": pr,
            "title": f"[owner/repo] Title (#{issue}) (PR #{pr})",
            "module": module,
            "date": date,
            "critical": list(critical),
            "medium": list(medium),
            "minor": list(minor),
        }

    def test_aggregates_modules_days_and_issues_within_window(self, dump_review_rounds):
        rounds = [
            self._round(100, 200, "2026-08-01T00:00:00Z", medium=["a"], minor=["b", "c"]),
            self._round(100, 200, "2026-08-02T00:00:00Z", critical=["d"]),
            self._round(101, 201, "2026-08-02T00:00:00Z", module="rag", medium=["e"]),
        ]
        snap = dump_review_rounds.build_dashboard_snapshot(
            rounds,
            "2026-08-01T00:00:00Z",
            "2026-08-03T00:00:00Z",
            merged_prs=[],
            unreviewed_numbers=set(),
        )
        assert snap["modules"]["web-ui"] == {"C": 1, "M": 1, "m": 2, "rounds": 2, "issues": 1}
        assert snap["modules"]["rag"] == {"C": 0, "M": 1, "m": 0, "rounds": 1, "issues": 1}
        assert snap["days"]["2026-08-01"]["rounds"] == 1
        assert snap["days"]["2026-08-02"]["rounds"] == 2
        assert len(snap["issues"]) == 2
        top = snap["issues"][0]
        assert top["pr"] == 200 and top["rounds"] == 2
        assert ["Medium", "a"] in top["findings"]
        assert ["Critical", "d"] in top["findings"]
        assert snap["totals"]["rounds"] == 3

    def test_excludes_rounds_outside_window_but_still_counts_pr_as_rejected(
        self, dump_review_rounds
    ):
        rounds = [self._round(1, 300, "2026-01-01T00:00:00Z", critical=["old"])]
        merged_prs = [
            {"number": 300, "title": "T", "module": "web-ui", "merged": "2026-08-02T00:00:00Z"}
        ]
        snap = dump_review_rounds.build_dashboard_snapshot(
            rounds,
            "2026-08-01T00:00:00Z",
            "2026-08-03T00:00:00Z",
            merged_prs=merged_prs,
            unreviewed_numbers=set(),
        )
        # No rounds fall inside the window...
        assert snap["issues"] == []
        # ...but PR 300 was rejected at some point in its history, so a
        # since-merged copy of it is not "clean" even though the rejection
        # itself predates the window.
        assert snap["cleanPRs"] == []
        assert snap["totals"]["clean"] == 0

    def test_clean_prs_exclude_unreviewed_and_rejected(self, dump_review_rounds):
        rounds = [self._round(1, 10, "2026-08-01T00:00:00Z", critical=["x"])]
        merged_prs = [
            {
                "number": 10,
                "title": "Rejected then merged",
                "module": "web-ui",
                "merged": "2026-08-02T00:00:00Z",
            },
            {
                "number": 11,
                "title": "Never reviewed",
                "module": "cli",
                "merged": "2026-08-02T00:00:00Z",
            },
            {
                "number": 12,
                "title": "Clean pass",
                "module": "rag",
                "merged": "2026-08-02T00:00:00Z",
            },
        ]
        snap = dump_review_rounds.build_dashboard_snapshot(
            rounds,
            "2026-08-01T00:00:00Z",
            "2026-08-03T00:00:00Z",
            merged_prs=merged_prs,
            unreviewed_numbers={11},
        )
        assert [p["pr"] for p in snap["cleanPRs"]] == [12]
        assert snap["cleanByModule"] == {"rag": 1}
        assert snap["totals"] == {
            "rounds": 1,
            "C": 1,
            "M": 0,
            "m": 0,
            "items": 1,
            "clean": 1,
            "reviewed": 2,
            "merged": 3,
            "unreviewed": 1,
        }
