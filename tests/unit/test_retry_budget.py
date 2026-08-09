"""Unit tests for scripts/agents/lib/retry_budget.py (#3689).

Pure computation only -- no gh/GraphQL involved -- covering the unforgeable
retry cap (keyed to (issue, failed step), reset only by an OWNER comment)
and the same-signature-disproof check.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "retry_budget.py"
)
_spec = importlib.util.spec_from_file_location("retry_budget", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
retry_budget = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = retry_budget
_spec.loader.exec_module(retry_budget)


def _bot_comment(body: str) -> dict:
    return {"body": body, "author_association": "NONE", "user": {"login": "myGPT-developer-agent"}}


def _owner_comment(body: str = "looking into it") -> dict:
    return {"body": body, "author_association": "OWNER", "user": {"login": "dkblinux98"}}


class TestSlugifyStep:
    def test_lowercases_and_replaces_non_alnum(self):
        assert (
            retry_budget.slugify_step("Check if PR already exists") == "check_if_pr_already_exists"
        )

    def test_collapses_repeated_separators(self):
        assert retry_budget.slugify_step("  Weird -- Step!!  ") == "weird_step"

    def test_empty_input_has_a_fallback(self):
        assert retry_budget.slugify_step("") == "unknown_step"
        assert retry_budget.slugify_step("###") == "unknown_step"


class TestComputeSignature:
    def test_deterministic_for_identical_input(self):
        sig1 = retry_budget.compute_signature(
            "Check if PR already exists", "404 Not Found\nfrom search/issues"
        )
        sig2 = retry_budget.compute_signature(
            "Check if PR already exists", "404 Not Found\nfrom search/issues"
        )
        assert sig1 == sig2

    def test_differs_for_different_errors(self):
        sig1 = retry_budget.compute_signature("step", "error A")
        sig2 = retry_budget.compute_signature("step", "error B")
        assert sig1 != sig2

    def test_ignores_leading_trailing_whitespace_per_line(self):
        sig1 = retry_budget.compute_signature("step", "  error text  \n  more  ")
        sig2 = retry_budget.compute_signature("step", "error text\nmore")
        assert sig1 == sig2


class TestMarkerRoundTrip:
    def test_render_then_parse(self):
        marker = retry_budget.render_marker("Check if PR already exists", "abc123def456", 2)
        parsed = retry_budget.parse_markers(f"some text\n{marker}\nmore text")
        assert parsed == [{"step": "check_if_pr_already_exists", "sig": "abc123def456", "n": 2}]

    def test_no_marker_returns_empty(self):
        assert retry_budget.parse_markers("just a regular comment, no marker here") == []

    def test_ignores_malformed_markers(self):
        assert retry_budget.parse_markers("<!-- nyxgpt-retry: step=foo -->") == []


class TestRetryDecision:
    def test_no_prior_retries_permits_first_retry(self):
        result = retry_budget.retry_decision([], "Check if PR already exists", "sig1")
        assert result["retry_count"] == 0
        assert result["cap_exceeded"] is False
        assert result["next_retry_number"] == 1
        assert result["last_signature"] is None
        assert result["same_signature_as_last"] is False

    def test_counts_markers_for_this_step_only(self):
        comments = [
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sigA", 1)),
            _bot_comment(retry_budget.render_marker("Run Verification (Attempt 1)", "sigZ", 1)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sigA", 2)),
        ]
        result = retry_budget.retry_decision(comments, "Check if PR already exists", "sigA")
        assert result["retry_count"] == 2
        assert result["next_retry_number"] == 3

    def test_cap_exceeded_at_max_retries_regardless_of_relabeling(self):
        # Three retries for the same step, each with a *different* invented
        # signature/label -- the old bug reset the count whenever the label
        # changed. The new cap is keyed to the step, not the label.
        comments = [
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig1", 1)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig2", 2)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig3", 3)),
        ]
        result = retry_budget.retry_decision(comments, "Check if PR already exists", "sig4")
        assert result["retry_count"] == 3
        assert result["cap_exceeded"] is True

    def test_only_owner_comment_resets_the_budget(self):
        comments = [
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig1", 1)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig2", 2)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig3", 3)),
        ]
        # A bot comment claiming "problem resolved" must NOT reset the
        # budget -- only an OWNER-authored comment does (#3689).
        non_owner_reset_attempt = comments + [
            _bot_comment("Problem automatically resolved!"),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig4", 1)),
        ]
        result = retry_budget.retry_decision(
            non_owner_reset_attempt, "Check if PR already exists", "sig5"
        )
        assert result["retry_count"] == 4
        assert result["cap_exceeded"] is True

    def test_owner_comment_resets_the_budget(self):
        comments = [
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig1", 1)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig2", 2)),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig3", 3)),
            _owner_comment("fixed the root cause manually, please retry"),
            _bot_comment(retry_budget.render_marker("Check if PR already exists", "sig4", 1)),
        ]
        result = retry_budget.retry_decision(comments, "Check if PR already exists", "sig4")
        assert result["retry_count"] == 1
        assert result["cap_exceeded"] is False

    def test_same_signature_as_last_true_when_identical(self):
        comments = [_bot_comment(retry_budget.render_marker("step", "sigA", 1))]
        result = retry_budget.retry_decision(comments, "step", "sigA")
        assert result["same_signature_as_last"] is True

    def test_same_signature_as_last_false_when_different(self):
        comments = [_bot_comment(retry_budget.render_marker("step", "sigA", 1))]
        result = retry_budget.retry_decision(comments, "step", "sigB")
        assert result["same_signature_as_last"] is False

    def test_custom_max_retries(self):
        comments = [_bot_comment(retry_budget.render_marker("step", "sig1", 1))]
        result = retry_budget.retry_decision(comments, "step", "sig2", max_retries=1)
        assert result["cap_exceeded"] is True
