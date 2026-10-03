"""The `Escalation` label and the blast-radius report (#4134).

Three decisions are tested here, all pure:

* `is_escalated` / `effective_labels` -- the substitution that keeps every
  reader of an issue's TYPE label correct across an escalation, without any
  automation restoring the label (which the owner forbade);
* `cause_origin` -- the one-cause-one-escalation registry lookup;
* `blast_radius.report` / `is_systemic` -- the investigation an agent owes the
  owner before escalating, and specifically that an unanswered question prints
  as "not checked" rather than vanishing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(LIB))

import blast_radius  # noqa: E402
import escalation_label as el  # noqa: E402


class TestIsEscalated:
    def test_accepts_both_payload_shapes(self):
        """REST gives objects, the GraphQL project query gives strings."""
        assert el.is_escalated([{"name": "Escalation"}])
        assert el.is_escalated(["Feature", "Escalation"])

    def test_unescalated_and_empty(self):
        assert not el.is_escalated([{"name": "Feature"}])
        assert not el.is_escalated([])
        assert not el.is_escalated(None)

    def test_is_case_sensitive_because_the_owner_created_one_exact_name(self):
        """`escalation` is not the owner's label. Matching it loosely would
        let a label nobody created act as a hold."""
        assert not el.is_escalated(["escalation"])


class TestReplacedLabel:
    def test_reads_the_marker_the_escalation_comment_records(self):
        comment = f"🚨 Escalated\n\n{el.replaced_marker('Acceptance Failure')}"
        assert el.replaced_label([comment]) == "Acceptance Failure"

    def test_the_last_marker_wins(self):
        """An issue escalated twice: the label still missing is the one the
        SECOND escalation took away."""
        bodies = [el.replaced_marker("Feature"), el.replaced_marker("Improvement")]
        assert el.replaced_label(bodies) == "Improvement"

    def test_no_marker_is_the_empty_string(self):
        assert el.replaced_label(["just a comment"]) == ""
        assert el.replaced_label([]) == ""
        assert el.replaced_label(None) == ""

    def test_an_empty_recorded_label_is_not_recorded(self):
        assert el.replaced_label(["<!-- escalation-replaced-label:  -->"]) == ""


class TestEffectiveLabels:
    def test_substitutes_the_recorded_type_back_in(self):
        """This is the criterion: an escalated Acceptance Failure must still
        read as an Acceptance Failure to `acceptance_role`, or the drain gate
        parks it forever and the promotion sweep closes the wrong thing."""
        assert el.effective_labels([{"name": "Escalation"}], "Acceptance Failure") == [
            "Acceptance Failure"
        ]

    def test_drops_escalation_when_nothing_was_recorded(self):
        """Better "untyped" than "typed `Escalation`": a state read as a type
        is how an escalated issue gets swept as if it were rework."""
        assert el.effective_labels(["Escalation"], "") == []

    def test_leaves_an_unescalated_issue_alone(self):
        assert el.effective_labels(["Feature"], "") == ["Feature"]

    def test_does_not_duplicate_a_label_already_present(self):
        """The relabel removes the old label one call at a time, so an issue
        can legitimately carry both for a moment."""
        assert el.effective_labels(["Escalation", "Feature"], "Feature") == ["Feature"]


class TestCauseRegistry:
    def test_round_trips_a_cause(self):
        body = f"🚨 Escalation raised\n\n{el.cause_marker('red-head:CI', 4100)}"
        assert el.cause_origin([body], "red-head:CI") == 4100

    def test_a_different_cause_does_not_match(self):
        body = el.cause_marker("red-head:CI", 4100)
        assert el.cause_origin([body], "conflict:4200") is None

    def test_the_latest_registration_wins(self):
        """A cause the owner resolved and that later recurred must point at
        the recurrence, not at the history."""
        bodies = [el.cause_marker("ci", 10), el.cause_marker("ci", 20)]
        assert el.cause_origin(bodies, "ci") == 20

    def test_no_registry_entry(self):
        assert el.cause_origin([], "anything") is None
        assert el.cause_origin(None, "anything") is None


class TestBlastRadiusReport:
    def _findings(self, **over):
        base = {
            "cause": "red-head:ci",
            "signature": "red-head:ci",
            "release_branch": "v9.9.9",
        }
        base.update(over)
        return base

    def test_an_unchecked_question_says_so(self):
        """The whole point: a skipped investigation must not read like one
        that asked and found nothing."""
        text = blast_radius.report(self._findings())
        assert "not checked" in text
        assert text.count("not checked") == 3  # head, other work, recent changes
        assert "no open escalation found for this cause" in text

    def test_a_red_head_names_the_checks(self):
        text = blast_radius.report(
            self._findings(release_head_red=True, red_checks=["CI - Tests", "Security Scan"])
        )
        assert "`CI - Tests`" in text and "`Security Scan`" in text
        assert "Everything built on this head is affected" in text

    def test_a_green_head_is_stated_as_no_not_omitted(self):
        text = blast_radius.report(self._findings(release_head_red=False))
        assert "Is `v9.9.9` red?** no." in text

    def test_a_prior_escalation_redirects_the_owner_to_it(self):
        text = blast_radius.report(self._findings(prior_escalation=4100))
        assert "#4100" in text
        assert "linked to it rather than diagnosed again" in text

    def test_recent_commits_are_capped(self):
        commits = [f"sha{i} subject {i}" for i in range(20)]
        text = blast_radius.report(self._findings(recent_commits=commits))
        assert "sha0 subject 0" in text
        assert f"sha{blast_radius.RECENT_COMMIT_LIMIT} " not in text

    def test_systemic_when_the_head_is_red(self):
        assert blast_radius.is_systemic(self._findings(release_head_red=True))

    def test_systemic_when_other_work_shows_the_signature(self):
        assert blast_radius.is_systemic(self._findings(same_signature=["#1 same step"]))

    def test_systemic_when_a_cause_is_already_escalated(self):
        assert blast_radius.is_systemic(self._findings(prior_escalation=7))

    def test_a_one_off_says_so(self):
        text = blast_radius.report(
            self._findings(release_head_red=False, same_signature=[], recent_commits=[])
        )
        assert "escalated as a one-off" in text
        assert not blast_radius.is_systemic(
            self._findings(release_head_red=False, same_signature=[])
        )


class TestSameSignatureRefs:
    def test_finds_an_anomaly_registry_entry_for_the_same_step(self):
        """#3694's markers are the other registry on the release issue, and a
        developer failure keyed on the same step IS the same fault."""
        body = "<!-- nyxgpt-anomaly: step=check-if-pr-exists issue=3600 opened=100 -->"
        assert blast_radius.same_signature_refs(
            [body], "developer-failure:check-if-pr-exists", exclude=[4134]
        ) == [3600]

    def test_finds_an_escalation_cause_entry(self):
        body = el.cause_marker("red-head:ci", 4100)
        assert blast_radius.same_signature_refs([body], "red-head:ci") == [4100]

    def test_excludes_the_escalating_issue_itself(self):
        body = el.cause_marker("ci", 4134)
        assert blast_radius.same_signature_refs([body], "ci", exclude=[4134]) == []

    def test_ignores_a_different_signature(self):
        body = el.cause_marker("conflict:99", 4100)
        assert blast_radius.same_signature_refs([body], "red-head:ci") == []

    def test_does_not_scan_free_prose(self):
        """A plain text scan would return every process issue number the
        thread quotes -- "(#3694)", "(#3730)" -- which is noise, not evidence."""
        body = "Dispatch paused (#3694) because red-head:ci broke on #4200"
        assert blast_radius.same_signature_refs([body], "red-head:ci") == []

    def test_an_empty_signature_matches_nothing(self):
        body = el.cause_marker("ci", 4100)
        assert blast_radius.same_signature_refs([body], "") == []
