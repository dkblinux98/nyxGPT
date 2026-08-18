"""Unit tests for scripts/agents/lib/ledger_ids.py (#3806).

`test_operating_ledger.py` pins the contract -- `agents/LEDGER.md` IDs are
never reused -- and it caught the V-034/V-035 collision on the PR, correctly.
These tests cover the allocator that stops the collision being created in the
first place: max + 1 (never lowest-unused, because the ledger's gaps are IDs
relocated to the owner's private annex) computed across the working copy *and*
the live release branch (because two PRs open at once each see a base without
the other's entries).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

_MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "ledger_ids.py"
_spec = importlib.util.spec_from_file_location("ledger_ids", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
ledger_ids = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ledger_ids
_spec.loader.exec_module(ledger_ids)

next_id = ledger_ids.next_id
parse_ids = ledger_ids.parse_ids
read_git_ref = ledger_ids.read_git_ref
main = ledger_ids.main

SCHEMA_BLOCK = """\
## Entry schema

```
- **D-000** · YYYY-MM-DD · <who> — <the decision>.
```

```
- **V-000** · YYYY-MM-DD — <the fact>.
```

## Verifications

"""


def _ledger(*entries: str) -> str:
    return SCHEMA_BLOCK + "".join(f"- **{e}** · 2026-08-18 — a fact.\n" for e in entries)


class TestParsing:
    def test_placeholder_ids_are_not_entries(self) -> None:
        """The schema block's `-000` rows document the shape; they own no number."""
        assert parse_ids(SCHEMA_BLOCK) == []

    def test_ids_are_returned_in_document_order_with_duplicates_kept(self) -> None:
        """Collapsing duplicates would hide the very defect being looked for."""
        assert parse_ids(_ledger("V-034", "V-035", "V-034")) == ["V-034", "V-035", "V-034"]

    def test_only_line_leading_entries_count(self) -> None:
        """Prose that names an ID mid-sentence does not define it."""
        assert parse_ids(SCHEMA_BLOCK + "Superseded by **V-034**, see above.\n") == []


class TestNextId:
    def test_allocates_max_plus_one_not_lowest_unused(self) -> None:
        """The public ledger's gaps are IDs relocated to the owner's private
        annex: taken, but invisible here. Filling a gap reuses one.
        """
        assert next_id("V", _ledger("V-001", "V-004")) == "V-005"

    def test_the_v034_incident_does_not_recur(self) -> None:
        """The regression. Reading only the tail of the file gives V-034; the
        allocator must see the whole file and every tree it is given.
        """
        branch_scanned_by_eye = _ledger("V-030", "V-031", "V-032", "V-033")
        live_base = _ledger("V-033", "V-034", "V-035")
        assert next_id("V", branch_scanned_by_eye) == "V-034"
        assert next_id("V", branch_scanned_by_eye, live_base) == "V-036"

    def test_first_id_of_an_unused_kind_is_one(self) -> None:
        assert next_id("Q", _ledger("V-009")) == "Q-001"

    def test_kinds_are_allocated_independently(self) -> None:
        text = _ledger("D-012", "V-034", "P-002")
        assert next_id("D", text) == "D-013"
        assert next_id("V", text) == "V-035"
        assert next_id("P", text) == "P-003"

    def test_ids_are_zero_padded_to_three_digits(self) -> None:
        assert next_id("V", _ledger("V-008")) == "V-009"

    def test_an_empty_tree_contributes_nothing(self) -> None:
        """A base branch predating the ledger must not reset the count."""
        assert next_id("V", _ledger("V-034"), "") == "V-035"

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown ledger entry kind"):
            next_id("X", _ledger("V-001"))


class TestReadGitRef:
    def test_reads_the_file_at_a_ref(self) -> None:
        assert "## Verifications" in read_git_ref("HEAD", "agents/LEDGER.md")

    def test_a_ref_without_the_file_owns_no_ids(self) -> None:
        """A base branch predating the ledger is not an error."""
        assert read_git_ref("HEAD", "agents/does-not-exist-3806.md") == ""

    def test_a_missing_ref_raises_rather_than_silently_allocating(self) -> None:
        """Returning "" for a bad ref would quietly degrade back to the
        branch-only scan that produced the collision.
        """
        with pytest.raises(RuntimeError, match="cannot read"):
            read_git_ref("no/such/ref-3806", "agents/LEDGER.md")


class TestCli:
    def test_next_prints_the_allocated_id(self, tmp_path: Path, capsys) -> None:
        ledger = tmp_path / "LEDGER.md"
        ledger.write_text(_ledger("V-034", "V-035"), encoding="utf-8")
        assert main(["--ledger", str(ledger), "next", "V"]) == 0
        assert capsys.readouterr().out.strip() == "V-036"

    def test_next_against_a_real_base_ref_skips_what_the_base_already_owns(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        """End to end against a real git repo, in the incident's shape: the
        base has merged another PR's V-034/V-035 while this branch was open.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        ledger = repo / "LEDGER.md"

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        git("init", "-q", "-b", "base")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        ledger.write_text(_ledger("V-033", "V-034", "V-035"), encoding="utf-8")
        git("add", "LEDGER.md")
        git("commit", "-qm", "base merged another PR's entries")

        # The branch's own copy predates them, exactly as an open PR's does.
        ledger.write_text(_ledger("V-033"), encoding="utf-8")

        monkeypatch.chdir(repo)
        assert main(["--ledger", "LEDGER.md", "next", "V", "--base", "base"]) == 0
        assert capsys.readouterr().out.strip() == "V-036"

    def test_a_bad_base_ref_exits_two_rather_than_handing_out_a_number(
        self, tmp_path: Path, capsys
    ) -> None:
        ledger = tmp_path / "LEDGER.md"
        ledger.write_text(_ledger("V-034"), encoding="utf-8")
        assert main(["--ledger", str(ledger), "next", "V", "--base", "no/such/ref-3806"]) == 2
        assert "cannot read" in capsys.readouterr().err

    def test_a_missing_ledger_exits_two(self, tmp_path: Path, capsys) -> None:
        assert main(["--ledger", str(tmp_path / "absent.md"), "next", "V"]) == 2
        assert capsys.readouterr().err

    def test_the_repository_ledger_allocates_past_every_entry_it_defines(self) -> None:
        """A live check against the real file: the number this hands out must
        not already exist in it.
        """
        text = (REPO_ROOT / "agents" / "LEDGER.md").read_text(encoding="utf-8")
        allocated = next_id("V", text)
        assert allocated not in parse_ids(text)
