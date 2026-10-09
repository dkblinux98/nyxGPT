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
  to a body that already has one.
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

SWEEP = "**Class:** the rule\n**Search:** `grep -rn thing`\n\n| Surface | Found | Action |\n"


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
