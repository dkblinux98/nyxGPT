"""The class sweep reaches the PR body, or the body says nobody swept (#4183).

`scripts/agents/lib/class_sweep.py` is the one place that decides what a PR body
says about the sweep, for both callers (a fresh submission and a review-fix
round's re-request). These tests pin the properties the gate rests on:

* a recorded sweep lands under the marker the review prompt greps for;
* no sweep produces an explicit NOT PROVIDED receipt -- never silence, which is
  the failure #4183 was filed about;
* the receipt never reads as a sweep, and a real sweep is never downgraded to a
  receipt by a later round that happens to have no file;
* applying it twice leaves one section, because the review-fix path applies it
  to a body that already has one;
* the out-of-class claim carries an issue number, or the section says it does
  not (#4195, PR #4197) -- D-067's fourth obligation, which until then was
  prose that nothing checked, so a sweep could say "filed, not fixed here" over
  an issue that did not exist and only a tracker search would catch it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "class_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("class_sweep", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class_sweep = _load()

BODY = "Closes #4183\n\n## Summary\nA change.\n\n## Testing\n- [ ] tests\n"

SWEEP = (
    "**Class:** the rule\n"
    "**Search:** `grep -rn thing`\n\n"
    "| Surface | Found | Action |\n\n"
    "**Out of class, filed:** none\n"
)


@pytest.fixture
def sweep_file(tmp_path: Path) -> Path:
    path = tmp_path / "class-sweep.md"
    path.write_text(SWEEP, encoding="utf-8")
    return path


def test_recorded_sweep_lands_under_the_marker(sweep_file: Path) -> None:
    body, outcome = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(sweep_file), sweep_file)
    assert outcome == "attached"
    assert class_sweep.SECTION_MARKER in body
    assert "**Class:** the rule" in body
    # The rest of the body survives -- the section is appended, not a rewrite.
    assert "Closes #4183" in body and "## Testing" in body


def test_a_missing_sweep_produces_an_explicit_receipt(tmp_path: Path) -> None:
    absent = tmp_path / "nope.md"
    body, outcome = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(absent), absent)
    assert outcome == "not-provided"
    assert class_sweep.MISSING_MARKER in body
    assert "NOT PROVIDED" in body
    # The reviewer is told what to do with it, and where the rule lives.
    assert "review-runbook.md" in body and "#4183" in body


@pytest.mark.parametrize("content", ["", "   \n\n\t\n"])
def test_an_empty_sweep_file_is_no_sweep_at_all(tmp_path: Path, content: str) -> None:
    """An empty section would read as a clean result; the receipt must win."""
    path = tmp_path / "class-sweep.md"
    path.write_text(content, encoding="utf-8")
    assert class_sweep.read_sweep(path) is None
    _, outcome = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(path), path)
    assert outcome == "not-provided"


def test_a_directory_in_place_of_the_sweep_file_is_no_sweep(tmp_path: Path) -> None:
    """An unreadable path must degrade to the receipt, never raise."""
    assert class_sweep.read_sweep(tmp_path) is None


def test_the_receipt_does_not_read_as_a_sweep(tmp_path: Path) -> None:
    """The two markers must not be substrings of one another.

    Everything downstream -- the review prompt's grep, this module's own
    idempotence check -- distinguishes "swept" from "nobody swept" by the
    marker. A receipt whose marker contains the section marker would report
    itself as a completed sweep, which is worse than having no receipt.
    """
    assert class_sweep.SECTION_MARKER not in class_sweep.MISSING_MARKER
    assert class_sweep.MISSING_MARKER not in class_sweep.SECTION_MARKER
    receipt = class_sweep.render_section(None, tmp_path / "class-sweep.md")
    assert class_sweep.SECTION_MARKER not in receipt


def test_applying_twice_leaves_exactly_one_section(sweep_file: Path) -> None:
    """The review-fix path re-applies to a body that already has a section."""
    sweep = class_sweep.read_sweep(sweep_file)
    once, _ = class_sweep.apply_to_body(BODY, sweep, sweep_file)
    twice, outcome = class_sweep.apply_to_body(once, sweep, sweep_file)
    assert outcome == "attached"
    assert twice.count(class_sweep.SECTION_MARKER) == 1
    assert twice.count(class_sweep.SECTION_HEADING) == 1
    # ...and stable, so a third round cannot keep growing the body.
    assert twice == once


def test_a_fresh_sweep_replaces_an_earlier_receipt(tmp_path: Path, sweep_file: Path) -> None:
    """Round 1 swept nothing; round 2 swept the class. The body must say so."""
    absent = tmp_path / "nope.md"
    with_receipt, _ = class_sweep.apply_to_body(BODY, None, absent)
    assert class_sweep.MISSING_MARKER in with_receipt

    fixed, outcome = class_sweep.apply_to_body(
        with_receipt, class_sweep.read_sweep(sweep_file), sweep_file
    )
    assert outcome == "attached"
    assert class_sweep.MISSING_MARKER not in fixed
    assert class_sweep.SECTION_MARKER in fixed


def test_a_written_sweep_is_never_downgraded_to_a_receipt(tmp_path: Path, sweep_file: Path) -> None:
    """A later round with no file must not manufacture the finding.

    The review-fix path runs on every re-request, including rounds that touched
    nothing about the sweep. If "no file this time" overwrote the section, the
    reviewer would be handed a NOT PROVIDED block for a sweep that exists.
    """
    with_sweep, _ = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(sweep_file), sweep_file)
    unchanged, outcome = class_sweep.apply_to_body(with_sweep, None, tmp_path / "nope.md")
    assert outcome == "kept"
    assert unchanged == with_sweep


def test_a_receipt_is_regenerated_rather_than_duplicated(tmp_path: Path) -> None:
    absent = tmp_path / "nope.md"
    once, _ = class_sweep.apply_to_body(BODY, None, absent)
    twice, outcome = class_sweep.apply_to_body(once, None, absent)
    assert outcome == "not-provided"
    assert twice.count(class_sweep.MISSING_MARKER) == 1
    assert twice == once


def test_a_trailing_section_does_not_swallow_following_headings(sweep_file: Path) -> None:
    """A section is bounded by the next `## ` heading, not by the end of file."""
    body = (
        "Closes #4183\n\n"
        "## Class sweep\n"
        f"{class_sweep.SECTION_MARKER}\n"
        "old sweep\n\n"
        "## CI override (developer)\n"
        "<!-- nyxgpt-ci-override -->\n"
        "the reason\n"
    )
    out, outcome = class_sweep.apply_to_body(body, class_sweep.read_sweep(sweep_file), sweep_file)
    assert outcome == "attached"
    assert "old sweep" not in out
    assert "## CI override (developer)" in out and "the reason" in out


# --- D-067's fourth obligation: the out-of-class claim names its issue -------
#
# #4197 described an out-of-class defect in detail, said "filed, not fixed
# here", and nothing had been filed. The sweep passed every other check; what
# caught it was the reviewer searching the tracker by hand. These tests pin the
# replacement for "the reviewer must notice an absence".


def _section(sweep: str) -> str:
    return class_sweep.render_section(sweep, "/tmp/class-sweep.md")


def test_an_out_of_class_claim_with_no_issue_number_is_called_out() -> None:
    sweep = (
        "**Class:** the rule\n\n"
        "**Out of class, to file:** one, found while verifying criterion 6.\n\n"
        "`tests/test_pr_head_guard_discriminates.sh` mutates the real working tree.\n"
    )
    assert class_sweep.out_of_class_gap(sweep) == "uncited"
    section = _section(sweep)
    assert class_sweep.UNCITED_MARKER in section
    assert "no issue named" in section
    # The note has to be actionable on both sides of the hand-off.
    assert "create_issue.sh" in section and "review-runbook.md" in section


def test_an_out_of_class_claim_naming_its_issue_is_accepted() -> None:
    sweep = (
        "**Class:** the rule\n\n"
        "**Out of class, filed:** #4198 — the PR-head guard suite mutates the\n"
        "real working tree; different class, so filed rather than fixed here.\n"
    )
    assert class_sweep.out_of_class_gap(sweep) is None
    assert class_sweep.UNCITED_MARKER not in _section(sweep)


@pytest.mark.parametrize("value", ["none", "None.", "n/a", "nothing found", "no findings", "zero"])
def test_nothing_found_is_a_complete_out_of_class_answer(value: str) -> None:
    """A reported search that found nothing satisfies the gate (CLAUDE.md)."""
    sweep = f"**Class:** the rule\n\n**Out of class, filed:** {value}\n"
    assert class_sweep.out_of_class_gap(sweep) is None
    assert class_sweep.UNCITED_MARKER not in _section(sweep)


def test_a_sweep_silent_about_out_of_class_findings_says_so() -> None:
    """The template asks for the line; silence is the thing #4183 was filed about."""
    sweep = "**Class:** the rule\n**Search:** `grep -rn thing`\n"
    assert class_sweep.out_of_class_gap(sweep) == "missing"
    section = _section(sweep)
    assert class_sweep.UNCITED_MARKER in section
    assert "not stated" in section
    # ...and it must not read as the harsher "you claimed one and named none".
    assert "no issue named" not in section


def test_the_citation_check_is_scoped_to_the_claim_not_the_whole_sweep() -> None:
    """The vacuity this check would otherwise have.

    Every sweep cites `#4183`, `#4174` and the issue it is fixing, somewhere.
    "Does the sweep mention an issue number" would therefore pass on every
    sweep ever written and guard nothing -- the exact vacuous shape #4195 had
    to fix in one of its own guards. The claim's own paragraph is the unit.
    """
    sweep = (
        "**Class:** the rule (#4183, D-067)\n"
        "**Search:** `rg -n thing` — see #4174 for why a rule needs a guard\n\n"
        "| Surface | Found | Action |\n"
        "|---|---|---|\n"
        "| `tests/x.sh` | same bug as #4136 | fixed |\n\n"
        "**Out of class, to file:** one, described above.\n"
    )
    assert class_sweep.out_of_class_gap(sweep) == "uncited"


def test_a_claim_whose_value_merely_contains_none_is_not_cleared() -> None:
    """ "nothing found" is read from the start of the value, not from anywhere."""
    sweep = (
        "**Class:** the rule\n\n"
        "**Out of class, filed:** one about fixture isolation; the others were none.\n"
    )
    assert class_sweep.out_of_class_gap(sweep) == "uncited"


def test_a_free_form_out_of_class_paragraph_still_counts_its_citation() -> None:
    """Wording is not the subject; whether a number is attached is."""
    cited = "**Class:** the rule\n\nOne finding was out of class and is now #4198.\n"
    assert class_sweep.out_of_class_gap(cited) is None

    uncited = "**Class:** the rule\n\nOne finding was out of class; see the note above.\n"
    assert class_sweep.out_of_class_gap(uncited) == "uncited"


def test_the_real_round_that_prompted_this_check_is_flagged() -> None:
    """The #4197 sweep text itself, which passed every other check."""
    sweep = (
        "**Consolidated:** `tests/pytest_python.sh`\n\n"
        "**Out of class, to file:** one, found while verifying criterion 6.\n\n"
        "`tests/test_pr_head_guard_discriminates.sh` mutates the **real working\n"
        "tree in place** — restoring only from an `EXIT` trap. The fix is a\n"
        "fixture copy, but that is a different class and scaling scope is the\n"
        "owner's call — filing rather than fixing here.\n"
    )
    assert class_sweep.out_of_class_gap(sweep) == "uncited"


def test_the_out_of_class_note_does_not_read_as_a_sweep() -> None:
    """Same property as the NOT PROVIDED receipt: markers stay distinguishable."""
    assert class_sweep.SECTION_MARKER not in class_sweep.UNCITED_MARKER
    assert class_sweep.UNCITED_MARKER not in class_sweep.SECTION_MARKER
    assert class_sweep.MISSING_MARKER not in class_sweep.UNCITED_MARKER
    assert class_sweep.UNCITED_MARKER not in class_sweep.MISSING_MARKER
    note = class_sweep.render_out_of_class_note("uncited")
    assert class_sweep.SECTION_MARKER not in note


def test_the_note_is_regenerated_rather_than_duplicated(tmp_path: Path) -> None:
    """The review-fix round re-applies to a body that already carries the note."""
    path = tmp_path / "class-sweep.md"
    path.write_text("**Class:** the rule\n\n**Out of class, to file:** one.\n", encoding="utf-8")
    sweep = class_sweep.read_sweep(path)
    once, _ = class_sweep.apply_to_body(BODY, sweep, path)
    twice, outcome = class_sweep.apply_to_body(once, sweep, path)
    assert outcome == "attached"
    assert twice.count(class_sweep.UNCITED_MARKER) == 1
    assert twice == once


def test_a_round_that_files_the_issue_clears_the_note(tmp_path: Path) -> None:
    """Round 1 claimed without filing; round 2 filed. The body must stop saying so."""
    path = tmp_path / "class-sweep.md"
    path.write_text("**Class:** the rule\n\n**Out of class, to file:** one.\n", encoding="utf-8")
    flagged, _ = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(path), path)
    assert class_sweep.UNCITED_MARKER in flagged

    path.write_text("**Class:** the rule\n\n**Out of class, filed:** #4198\n", encoding="utf-8")
    cleared, outcome = class_sweep.apply_to_body(flagged, class_sweep.read_sweep(path), path)
    assert outcome == "attached"
    assert class_sweep.UNCITED_MARKER not in cleared
    assert class_sweep.SECTION_MARKER in cleared


def test_an_absent_sweep_does_not_also_get_the_out_of_class_note(tmp_path: Path) -> None:
    """The NOT PROVIDED receipt already says strictly more; two notes is noise."""
    assert class_sweep.out_of_class_gap(None) is None
    receipt, _ = class_sweep.apply_to_body(BODY, None, tmp_path / "nope.md")
    assert class_sweep.UNCITED_MARKER not in receipt
    assert class_sweep.MISSING_MARKER in receipt


def test_the_note_never_changes_the_reported_outcome(tmp_path: Path) -> None:
    """`developer_submit_for_review.sh` switches on four outcomes and no more.

    A fifth would fall through to its `*)` arm and be reported as "the helper
    did not run" -- a worse message than the truth, on a submission that is
    otherwise fine. The note rides inside the section instead.
    """
    path = tmp_path / "class-sweep.md"
    path.write_text("**Class:** the rule\n\n**Out of class, to file:** one.\n", encoding="utf-8")
    _, outcome = class_sweep.apply_to_body(BODY, class_sweep.read_sweep(path), path)
    assert outcome == "attached"


def test_cli_rewrites_the_body_file_and_reports_the_outcome(
    tmp_path: Path, sweep_file: Path
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "apply",
            "--body-file",
            str(body_file),
            "--sweep",
            str(sweep_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "attached"
    assert class_sweep.SECTION_MARKER in body_file.read_text(encoding="utf-8")


def test_cli_is_advisory_when_the_body_file_cannot_be_read(tmp_path: Path) -> None:
    """A broken call must never be the reason finished work misses review."""
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "apply",
            "--body-file",
            str(tmp_path / "does-not-exist.md"),
            "--sweep",
            str(tmp_path / "nope.md"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "error"
