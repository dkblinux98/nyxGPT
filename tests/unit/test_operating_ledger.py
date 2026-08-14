"""Structural guards for the operating ledger (`agents/LEDGER.md`, #3774).

The ledger is the system of record for cross-session agent memory: it only
works if every session is actually pointed at it and if its entries keep the
shape the schema promises. Both of those are prose obligations spread across
`CLAUDE.md`, the agent prompts and the runbooks -- exactly the kind of wiring
that rots silently when one of those files is rewritten. These tests are the
CI-enforced floor under it, run by the existing `pytest tests/unit/` gate.

They deliberately check *structure*, not content: that the ledger exists with
its four entry sections, that IDs are unique and well-formed, that every
verification carries a repeatable method and a staleness condition, and that
the bootstrap/prompt/runbook wiring still references it. Judging whether an
entry's claim is true is a review-agent job, not a test's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "agents" / "LEDGER.md"

# Sections the entry schema defines. Decisions/Verifications/Parked/Open
# questions are the four entry kinds required by #3774's acceptance criteria;
# Superseded is the pruning target that keeps retired beliefs visible instead
# of deleted (and re-derivable).
REQUIRED_SECTIONS = (
    "## Decisions",
    "## Verifications",
    "## Parked",
    "## Open questions",
    "## Superseded",
)

# An entry opens a line as `- **X-000**`; the schema block itself uses the
# reserved `-000` placeholder, which is excluded from real-entry checks.
ENTRY_RE = re.compile(r"^- \*\*([DVPQS])-(\d{3})\*\*", re.MULTILINE)
PLACEHOLDER_ID = "000"


@pytest.fixture(scope="module")
def ledger_text() -> str:
    """The ledger's raw text."""
    assert LEDGER_PATH.is_file(), f"operating ledger missing at {LEDGER_PATH}"
    return LEDGER_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of a top-level ``## heading`` section."""
    start = text.index(heading) + len(heading)
    rest = text[start:]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_ledger_defines_every_required_section(ledger_text: str) -> None:
    """All four entry kinds plus Superseded must exist as top-level sections."""
    missing = [s for s in REQUIRED_SECTIONS if s not in ledger_text]
    assert not missing, f"operating ledger is missing sections: {missing}"


def test_ledger_entry_ids_are_unique(ledger_text: str) -> None:
    """IDs are never reused -- including after supersession."""
    ids = [f"{kind}-{num}" for kind, num in ENTRY_RE.findall(ledger_text) if num != PLACEHOLDER_ID]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate ledger entry IDs: {duplicates}"


def test_ledger_is_seeded_with_each_entry_kind(ledger_text: str) -> None:
    """A schema with no entries teaches a session nothing.

    #3774 requires settled facts seeded at creation, so each kind must carry at
    least one real entry rather than only the schema placeholder.
    """
    for kind, heading in (
        ("D", "## Decisions"),
        ("V", "## Verifications"),
        ("P", "## Parked"),
        ("Q", "## Open questions"),
    ):
        body = _section(ledger_text, heading)
        found = [n for k, n in ENTRY_RE.findall(body) if k == kind and n != PLACEHOLDER_ID]
        assert found, f"{heading} has no seeded {kind}- entries"


def test_every_verification_records_method_and_staleness(ledger_text: str) -> None:
    """`Method:` and `Re-verify when:` are what separate a verification from a
    recollection: one says how the fact was established, the other says when to
    stop trusting it. An entry missing either is an unverified claim wearing a
    verification's clothes.
    """
    body = _section(ledger_text, "## Verifications")
    # Entries are separated by the blank line preceding the next `- **V-`.
    chunks = re.split(r"\n(?=- \*\*V-)", body.strip())
    entries = [c for c in chunks if c.startswith("- **V-")]
    assert entries, "no verification entries found to check"

    for entry in entries:
        entry_id = entry[4:9]
        assert "Method:" in entry, f"verification {entry_id} records no Method"
        assert "Re-verify when:" in entry, f"verification {entry_id} records no 'Re-verify when'"


def test_every_parked_entry_records_reason_and_revisit(ledger_text: str) -> None:
    """A parked item without a revisit condition is an abandoned one -- and a
    parked item without a reason gets re-proposed by the next session.
    """
    body = _section(ledger_text, "## Parked")
    chunks = re.split(r"\n(?=- \*\*P-)", body.strip())
    entries = [c for c in chunks if c.startswith("- **P-")]
    assert entries, "no parked entries found to check"

    for entry in entries:
        entry_id = entry[4:9]
        assert "Reason:" in entry, f"parked item {entry_id} records no Reason"
        assert "Revisit when:" in entry, f"parked item {entry_id} records no 'Revisit when'"


def test_every_open_question_records_who_can_answer(ledger_text: str) -> None:
    """An open question nobody can route is a note, not a question."""
    body = _section(ledger_text, "## Open questions")
    chunks = re.split(r"\n(?=- \*\*Q-)", body.strip())
    entries = [c for c in chunks if c.startswith("- **Q-")]
    assert entries, "no open questions found to check"

    for entry in entries:
        entry_id = entry[4:9]
        assert "Needs:" in entry, f"open question {entry_id} records no 'Needs'"


@pytest.mark.parametrize(
    "doc",
    [
        "CLAUDE.md",
        "AGENTS.md",
        "agents/prompts/developer-agent.prompt.md",
        "agents/prompts/review-agent.prompt.md",
        "agents/prompts/scrummaster-agent.prompt.md",
        "agents/runbooks/developer-runbook.md",
        "agents/runbooks/review-runbook.md",
        "agents/runbooks/scrummaster-runbook.md",
        "agents/charters/developer-agent.md",
        "agents/charters/review-agent.md",
        "agents/charters/scrummaster-agent.md",
    ],
)
def test_ledger_is_wired_into_operating_docs(doc: str) -> None:
    """Every doc a session bootstraps from must point at the ledger.

    An unreferenced ledger is a ledger nobody reads, which returns the system
    to reconstructing state from memory -- the failure #3774 exists to fix.
    """
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "LEDGER.md" in text, f"{doc} does not reference agents/LEDGER.md"


def test_bootstrap_reading_list_includes_the_ledger() -> None:
    """The ledger must be on CLAUDE.md's required-reading list specifically --
    a passing mention elsewhere in the file is not the same instruction.
    """
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    bootstrap = text[text.index("### 1. Core Operating Instructions") :]
    bootstrap = bootstrap[: bootstrap.index("### After Reading")]
    assert "agents/LEDGER.md" in bootstrap, "ledger is not on the CLAUDE.md bootstrap reading list"
