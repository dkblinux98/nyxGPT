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


def _entry(entry_id: str, fact: str = "a fact") -> str:
    return f"- **{entry_id}** · 2026-08-18 — {fact}.\n"


def _ledger_of(*entries: str) -> str:
    return SCHEMA_BLOCK + "".join(entries)


class TestMergeTimeReallocation:
    """#3862: the collision is created at merge, by neither branch alone.

    `next` allocates correctly and both branches are still handed the same
    number, because at the moment each one asks, the other's entry does not
    exist anywhere yet. `test_ledger_entry_ids_are_unique` then catches it on
    the branch that merges second -- correctly, but by then it is a human's
    problem, and resolving it by hand got the `theirs`/`mine` sides backwards
    on the first attempt (#3836/`V-030`). Reallocation moves the decision to
    the one instant it can be right: merge time.
    """

    def test_an_id_both_sides_invented_is_renumbered_on_the_branch(self) -> None:
        merge_base = _ledger_of(_entry("V-015"))
        base = _ledger_of(_entry("V-015"), _entry("V-016", "linux-observability bind mounts"))
        branch = _ledger_of(_entry("V-015"), _entry("V-016", "step enumeration is closed"))

        new_text, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {"V-016": "V-017"}
        assert "step enumeration is closed" in new_text
        assert "**V-017**" in new_text
        assert "**V-016**" not in new_text

    def test_the_base_is_never_rewritten_only_the_branch_yields(self) -> None:
        merge_base = _ledger_of(_entry("V-015"))
        base = _ledger_of(_entry("V-015"), _entry("V-016", "base fact"))
        branch = _ledger_of(_entry("V-015"), _entry("V-016", "branch fact"))

        new_text, _ = ledger_ids.reallocate(merge_base, base, branch)

        assert "base fact" not in new_text, "reallocation must not import the base's entries"

    def test_an_entry_the_branch_only_edits_keeps_its_id(self) -> None:
        """Editing a shared entry is not a collision, and renumbering it would
        break every reference to it in the tree."""
        merge_base = _ledger_of(_entry("D-013", "original wording"))
        base = _ledger_of(_entry("D-013", "original wording"), _entry("D-014", "base fact"))
        branch = _ledger_of(_entry("D-013", "sharpened wording"), _entry("D-014", "branch fact"))

        _, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {"D-014": "D-015"}
        assert "D-013" not in mapping

    def test_ids_the_base_never_touched_are_left_alone(self) -> None:
        merge_base = _ledger_of(_entry("V-015"))
        base = _ledger_of(_entry("V-015"))
        branch = _ledger_of(_entry("V-015"), _entry("V-016"))

        new_text, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {}
        assert new_text == branch

    def test_several_collisions_are_renumbered_in_document_order(self) -> None:
        """The real #3862 case: V-016, V-024 and V-025 all taken by other facts."""
        merge_base = _ledger_of(_entry("V-015"))
        base = _ledger_of(
            _entry("V-015"),
            _entry("V-016", "bind mounts"),
            _entry("V-024", "default branch"),
            _entry("V-025", "issue-hygiene guard"),
        )
        branch = _ledger_of(
            _entry("V-015"),
            _entry("V-016", "step enumeration"),
            _entry("V-024", "two review runs"),
            _entry("V-025", "gh is 2.97.0"),
        )

        _, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {"V-016": "V-026", "V-024": "V-027", "V-025": "V-028"}

    def test_reallocation_is_deterministic(self) -> None:
        merge_base = _ledger_of(_entry("D-001"))
        base = _ledger_of(_entry("D-001"), _entry("D-002", "base"))
        branch = _ledger_of(_entry("D-001"), _entry("D-002", "branch"), _entry("D-003", "branch"))

        first = ledger_ids.reallocate(merge_base, base, branch)
        second = ledger_ids.reallocate(merge_base, base, branch)

        assert first == second

    def test_cross_references_move_with_the_entry(self) -> None:
        """A renumbered entry that other prose points at must not orphan the link."""
        merge_base = _ledger_of(_entry("D-001"))
        base = _ledger_of(_entry("D-001"), _entry("D-002", "base fact"))
        branch = _ledger_of(_entry("D-001"), _entry("D-002", "branch fact")) + (
            "\nSee **D-002** and D-001 for the reasoning.\n"
        )

        new_text, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {"D-002": "D-003"}
        assert "See **D-003** and D-001 for the reasoning." in new_text

    def test_a_renumber_never_lands_on_a_number_already_in_use(self) -> None:
        """max + 1 across BOTH sides, never lowest-unused: the ledger's gaps are
        entries relocated to the owner's private annex and those IDs are taken.
        """
        merge_base = _ledger_of(_entry("V-015"))
        base = _ledger_of(_entry("V-015"), _entry("V-016", "base"), _entry("V-030", "base later"))
        branch = _ledger_of(_entry("V-015"), _entry("V-016", "branch"))

        new_text, mapping = ledger_ids.reallocate(merge_base, base, branch)

        assert mapping == {"V-016": "V-031"}
        assert set(parse_ids(new_text)).isdisjoint({"V-030"})


class TestReallocateCli:
    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        git("init", "-q", "-b", "v3.0.0")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "LEDGER.md").write_text(_ledger_of(_entry("V-015")), encoding="utf-8")
        git("add", "LEDGER.md")
        git("commit", "-qm", "merge base")
        # The branch appends V-016...
        git("checkout", "-q", "-b", "feat/1-branch")
        (repo / "LEDGER.md").write_text(
            _ledger_of(_entry("V-015"), _entry("V-016", "branch fact")), encoding="utf-8"
        )
        git("commit", "-qam", "branch entry")
        # ...and so, independently, does the base.
        git("checkout", "-q", "v3.0.0")
        (repo / "LEDGER.md").write_text(
            _ledger_of(_entry("V-015"), _entry("V-016", "base fact")), encoding="utf-8"
        )
        git("commit", "-qam", "base entry")
        git("checkout", "-q", "feat/1-branch")
        return repo

    def test_the_cli_rewrites_the_branch_and_reports_the_mapping(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        repo = self._repo(tmp_path)
        monkeypatch.chdir(repo)

        rc = main(["--ledger", "LEDGER.md", "reallocate", "--base", "v3.0.0", "--write"])

        assert rc == 1, "exit 1 signals 'collisions were reallocated', so callers can commit"
        assert "V-016 -> V-017" in capsys.readouterr().out
        rewritten = (repo / "LEDGER.md").read_text(encoding="utf-8")
        assert "**V-017**" in rewritten
        assert "branch fact" in rewritten
        assert "base fact" not in rewritten

    def test_report_only_leaves_the_file_untouched(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        repo = self._repo(tmp_path)
        monkeypatch.chdir(repo)
        before = (repo / "LEDGER.md").read_text(encoding="utf-8")

        assert main(["--ledger", "LEDGER.md", "reallocate", "--base", "v3.0.0"]) == 1
        assert (repo / "LEDGER.md").read_text(encoding="utf-8") == before

    def test_no_collision_exits_zero_and_changes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        repo = self._repo(tmp_path)
        monkeypatch.chdir(repo)
        # Give the base a number the branch did not invent.
        subprocess.run(
            ["git", "checkout", "-q", "v3.0.0"], cwd=repo, check=True, capture_output=True
        )
        (repo / "LEDGER.md").write_text(
            _ledger_of(_entry("V-015"), _entry("V-020", "base fact")), encoding="utf-8"
        )
        subprocess.run(
            ["git", "commit", "-qam", "different number"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "checkout", "-q", "feat/1-branch"], cwd=repo, check=True, capture_output=True
        )
        before = (repo / "LEDGER.md").read_text(encoding="utf-8")

        assert main(["--ledger", "LEDGER.md", "reallocate", "--base", "v3.0.0", "--write"]) == 0
        assert (repo / "LEDGER.md").read_text(encoding="utf-8") == before

    def test_unrelated_histories_exit_two_rather_than_guessing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """With no merge base there is no way to tell which entries the branch
        introduced, and guessing would renumber someone else's."""
        repo = self._repo(tmp_path)
        monkeypatch.chdir(repo)

        rc = main(["--ledger", "LEDGER.md", "reallocate", "--base", "v3.0.0^{tree}"])

        assert rc == 2
