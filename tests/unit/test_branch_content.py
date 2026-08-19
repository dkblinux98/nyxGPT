"""The branch-deletion guard, exercised against real planted git repositories (#3862).

This is the load-bearing test for the cleanup rule. A naive "tidy up the agent's
orphan branches" pass, applied to the real branch set of 2026-08-18, would have
permanently destroyed 438 lines of test coverage: nothing else referenced those
files and no PR existed to recover them from. The three branches were
indistinguishable from the outside -- same author, no PR, closed issue, weeks
old -- and only a content check told the genuinely-redundant one apart.

So the fixtures here are the three shapes, not one:

1. **stranded** -- a branch carrying a file absent from the base. The guard must
   REFUSE. (Proves it does not over-delete.)
2. **rebased-but-landed, with a conflicting ledger** -- every byte landed via a
   different branch, so the original shows unmerged commits forever and its
   ``agents/LEDGER.md`` differs because the base moved ahead. The guard must
   DELETE. (Proves it does not under-delete and leave every rebased branch
   accumulating forever.)
3. **ancestor** -- the ordinary merged branch.

Each fixture also asserts what the *cheap* signals say, so the test fails loudly
if someone later "simplifies" the guard back to ancestry or commit counts: on
this data the stranded branch is 1 commit ahead and the fully-landed branch is 3,
i.e. ancestry ranks the safe-to-delete branch as the most unmerged of the set.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

_MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "branch_content.py"
_spec = importlib.util.spec_from_file_location("branch_content", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
branch_content = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = branch_content
_spec.loader.exec_module(branch_content)

branch_content_landed = branch_content.branch_content_landed

BASE = "v3.0.0"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _commits_ahead(repo: Path, branch: str) -> int:
    return len(_git(repo, "rev-list", f"{BASE}..{branch}").split())


def _is_ancestor(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, BASE],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


LEDGER_HEADER = "# nyxGPT Operating Ledger\n\n## Decisions\n\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo whose base branch and merge-base commit both already exist."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", BASE)
    _git(root, "config", "user.email", "agent@example.invalid")
    _git(root, "config", "user.name", "agent")
    _write(root, "README.md", "nyxGPT\n")
    _write(root, "agents/LEDGER.md", LEDGER_HEADER + "- **D-001** old decision\n")
    _commit(root, "chore: base")
    return root


def _branch_from_base(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", "-b", name, BASE)


def _back_to_base(repo: Path) -> None:
    _git(repo, "checkout", "-q", BASE)


# --------------------------------------------------------------------------
# Fixture 1: the stranded branch -- the guard MUST refuse to delete it.
# --------------------------------------------------------------------------


def test_a_branch_carrying_a_file_absent_from_the_base_is_never_deletable(repo: Path) -> None:
    """The #3789 shape: 320 lines of test-only work, on no other branch."""
    _branch_from_base(repo, "fix/3789-dev-install-mode")
    _write(repo, "tests/unit/test_ops_step_isolation.py", "def test_install_steps():\n    pass\n")
    _commit(repo, "test: install step isolation (#3789)")
    _back_to_base(repo)

    verdict = branch_content_landed(str(repo), BASE, "fix/3789-dev-install-mode")

    assert verdict.landed is False, (
        "the guard would have deleted the only copy of the branch's test coverage"
    )
    assert any("test_ops_step_isolation.py" in item for item in verdict.stranded), (
        f"the refusal must name the file that caused it; got {verdict.stranded}"
    )


def test_the_stranded_branch_looks_nearly_landed_to_the_cheap_signals(repo: Path) -> None:
    """Fault injection: prove the signals the guard must NOT use get this wrong.

    One commit ahead is the least-unmerged-looking branch in the set, and a
    commit-count or ``git branch --merged`` cleanup deletes it first.
    """
    _branch_from_base(repo, "fix/3789-dev-install-mode")
    _write(repo, "tests/unit/test_ops_step_isolation.py", "def test_install_steps():\n    pass\n")
    _commit(repo, "test: install step isolation (#3789)")
    _back_to_base(repo)

    assert _commits_ahead(repo, "fix/3789-dev-install-mode") == 1
    assert not _is_ancestor(repo, "fix/3789-dev-install-mode")
    assert branch_content_landed(str(repo), BASE, "fix/3789-dev-install-mode").landed is False


def test_a_branch_whose_deletion_never_landed_is_not_deletable(repo: Path) -> None:
    """A removal is content too: dropping the branch would silently resurrect the file."""
    _branch_from_base(repo, "chore/9001-remove-dead-script")
    (repo / "README.md").unlink()
    _commit(repo, "chore: drop README (#9001)")
    _back_to_base(repo)

    verdict = branch_content_landed(str(repo), BASE, "chore/9001-remove-dead-script")

    assert verdict.landed is False
    assert any("README.md" in item for item in verdict.stranded)


def test_a_branch_that_edits_a_shared_file_with_new_lines_is_not_deletable(repo: Path) -> None:
    _branch_from_base(repo, "feat/9002-edit")
    _write(repo, "README.md", "nyxGPT\nplus a line only this branch has\n")
    _commit(repo, "docs: extend README (#9002)")
    _back_to_base(repo)

    verdict = branch_content_landed(str(repo), BASE, "feat/9002-edit")

    assert verdict.landed is False
    assert any("README.md" in item for item in verdict.stranded)


def test_a_differing_binary_file_is_never_assumed_landed(repo: Path) -> None:
    """`git diff --numstat` reports "-" for binaries; that must not read as zero."""
    _write(repo, "assets/logo.bin", "")
    (repo / "assets/logo.bin").write_bytes(b"\x00\x01base\x00")
    _commit(repo, "chore: add binary")

    _branch_from_base(repo, "feat/9003-binary")
    (repo / "assets/logo.bin").write_bytes(b"\x00\x01branch-only\x00")
    _commit(repo, "chore: change binary (#9003)")
    _back_to_base(repo)

    verdict = branch_content_landed(str(repo), BASE, "feat/9003-binary")

    assert verdict.landed is False
    assert any("logo.bin" in item for item in verdict.stranded)


# --------------------------------------------------------------------------
# Fixture 2: rebased-but-landed, with a conflicting ledger -- MUST be deleted.
# --------------------------------------------------------------------------


def _plant_rebased_but_landed(repo: Path) -> str:
    """The exact #3836 shape.

    The branch's work is re-applied onto the base as different commits, so the
    branch keeps three commits that are not on the base forever. The base then
    appends a further ledger entry, so ``agents/LEDGER.md`` genuinely differs --
    and a merge of this branch really does conflict on that file, worth zero
    content.
    """
    branch = "fix/3836-create-issue-blocks"
    _branch_from_base(repo, branch)
    _write(repo, "scripts/agents/create_issue.sh", "#!/usr/bin/env bash\n--blocks writes native\n")
    _commit(repo, "fix: --blocks writes the native blocked-by edge (#3836)")
    _write(repo, "tests/test_create_issue_blocks.sh", "#!/usr/bin/env bash\nassert native\n")
    _commit(repo, "test: CI job proving native relationships only (#3836)")
    _write(repo, "agents/LEDGER.md", LEDGER_HEADER + "- **D-001** old decision\n- **D-030** blocks\n")
    _commit(repo, "docs: record the D-002 alignment (#3836)")

    _back_to_base(repo)
    # Re-applied elsewhere: identical bytes, different commits.
    _write(repo, "scripts/agents/create_issue.sh", "#!/usr/bin/env bash\n--blocks writes native\n")
    _write(repo, "tests/test_create_issue_blocks.sh", "#!/usr/bin/env bash\nassert native\n")
    _write(repo, "agents/LEDGER.md", LEDGER_HEADER + "- **D-001** old decision\n- **D-030** blocks\n")
    _commit(repo, "Merge pull request #3852 from claude/issue-3836")
    # ...and the base then moves ahead on the ledger, on its own.
    _write(
        repo,
        "agents/LEDGER.md",
        LEDGER_HEADER + "- **D-001** old decision\n- **D-030** blocks\n- **D-031** later fact\n",
    )
    _commit(repo, "docs: a later ledger entry (#3999)")
    return branch


def test_a_rebased_but_landed_branch_is_deletable_despite_a_differing_ledger(repo: Path) -> None:
    branch = _plant_rebased_but_landed(repo)

    verdict = branch_content_landed(str(repo), BASE, branch)

    assert verdict.landed is True, (
        f"every byte landed via another branch; refusing here leaves every rebased "
        f"branch accumulating forever. stranded={verdict.stranded}"
    )
    assert verdict.stranded == []


def test_the_fully_landed_branch_looks_the_most_unmerged_to_the_cheap_signals(repo: Path) -> None:
    """Fault injection for the other direction, and the reason ancestry is unusable.

    On this data ancestry ranks the safe-to-delete branch (3 commits ahead) as
    *more* unmerged than the stranded one (1 commit ahead). Any cleanup keyed on
    commit count, ``git branch --merged``, or mergeability keeps the redundant
    branch and deletes the irreplaceable one.
    """
    branch = _plant_rebased_but_landed(repo)

    _branch_from_base(repo, "fix/3789-dev-install-mode")
    _write(repo, "tests/unit/test_ops_step_isolation.py", "def test_install_steps():\n    pass\n")
    _commit(repo, "test: install step isolation (#3789)")
    _back_to_base(repo)

    assert _commits_ahead(repo, branch) == 3
    assert _commits_ahead(repo, "fix/3789-dev-install-mode") == 1
    assert not _is_ancestor(repo, branch)
    assert not _is_ancestor(repo, "fix/3789-dev-install-mode")

    # The content check is the only one of the two that gets both right.
    assert branch_content_landed(str(repo), BASE, branch).landed is True
    assert branch_content_landed(str(repo), BASE, "fix/3789-dev-install-mode").landed is False


def test_the_ledger_conflict_that_misled_the_owner_is_worth_zero_content(repo: Path) -> None:
    """A real merge conflict on `agents/LEDGER.md`, and the base is still a superset.

    git compares each side to the merge base and never asks whether one side
    already contains the other, so "it conflicts" is not evidence of divergence.
    """
    branch = _plant_rebased_but_landed(repo)

    added = _git(repo, "diff", "--numstat", BASE, branch, "--", "agents/LEDGER.md")
    assert added.split("\t")[0] == "0", "the branch must add zero ledger lines the base lacks"
    assert branch_content_landed(str(repo), BASE, branch).landed is True


# --------------------------------------------------------------------------
# Fixture 3: the ordinary cases.
# --------------------------------------------------------------------------


def test_an_ancestor_branch_is_deletable(repo: Path) -> None:
    _branch_from_base(repo, "feat/9004-merged")
    _write(repo, "docs/thing.md", "thing\n")
    _commit(repo, "docs: thing (#9004)")
    _git(repo, "checkout", "-q", BASE)
    _git(repo, "merge", "-q", "--ff-only", "feat/9004-merged")

    verdict = branch_content_landed(str(repo), BASE, "feat/9004-merged")

    assert verdict.landed is True


def test_a_branch_identical_to_the_base_tip_is_deletable(repo: Path) -> None:
    _branch_from_base(repo, "claude/bootstrap-session")

    assert branch_content_landed(str(repo), BASE, "claude/bootstrap-session").landed is True


def test_an_unresolvable_ref_never_reads_as_landed(repo: Path) -> None:
    verdict = branch_content_landed(str(repo), BASE, "no/such/branch")

    assert verdict.landed is False
    assert "cannot resolve" in verdict.reason


def test_unrelated_histories_never_read_as_landed(repo: Path, tmp_path: Path) -> None:
    """No merge base means nothing to compare -- report, do not delete."""
    _git(repo, "checkout", "-q", "--orphan", "orphan/9005")
    _git(repo, "rm", "-rq", "--cached", ".")
    for leftover in ("README.md", "agents/LEDGER.md"):
        (repo / leftover).unlink(missing_ok=True)
    _write(repo, "only-here.txt", "x\n")
    _commit(repo, "chore: orphan root")
    _back_to_base(repo)

    verdict = branch_content_landed(str(repo), BASE, "orphan/9005")

    assert verdict.landed is False
    assert "common ancestor" in verdict.reason


def test_the_cli_exit_code_is_the_verdict(repo: Path) -> None:
    """Shell callers gate deletion on the exit status, so it must be load-bearing."""
    _branch_from_base(repo, "fix/9006-stranded")
    _write(repo, "tests/unit/test_only_here.py", "def test_x():\n    pass\n")
    _commit(repo, "test: only here (#9006)")
    _back_to_base(repo)

    assert (
        branch_content.main(
            ["--repo", str(repo), "landed", "--base", BASE, "--branch", "fix/9006-stranded"]
        )
        == 1
    )
    assert (
        branch_content.main(["--repo", str(repo), "landed", "--base", BASE, "--branch", BASE]) == 0
    )
