"""Unit tests for scripts/agents/lib/branch_residue.py (#4151).

The defect these pin: `developer_ensure_pr_exists.sh` asked "does this branch
have a pull request?" and a branch whose PR is MERGED and which then receives
commits answers yes. On 2026-10-04 that stranded 579 insertions on
`fix/3986-*` -- pushed 19 minutes after PR #4126 merged -- implementing the one
acceptance criterion the owner could not test, while #3986 sat closed in
`For Release` reading as accepted.

So the two properties with teeth are:

  * a merged PR plus commits the base does not have is `merged-residue`, not
    "has a PR, nothing to do"; and
  * nothing is ever ACTED on from a knowledge failure -- an unreadable PR list,
    an uncomputed git count and an unrecognised state all answer `unknown`.

The precedence (open > merged > closed-unmerged > none) and the squash-merge
exemption are the rest: a guard that fires on every squash-merged branch in the
repo is one nobody reads, and one that treats an open PR as "no PR" opens
duplicates.

The end-to-end behaviour -- the draft residue PR, its marker, the loud issue
comment, the repo-wide sweep, and the fault injection showing the retired form
stranding the same fixture -- lives in `tests/test_stranded_residue.sh`, run
below so `pytest -v` covers it too.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from shell_suite import bash4_or_skip

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "branch_residue.py"
)
_spec = importlib.util.spec_from_file_location("branch_residue", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
branch_residue = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = branch_residue
_spec.loader.exec_module(branch_residue)

BASE = "v3.0.0"


def _pr(number, state="closed", merged=True, base=BASE):
    """One PR as the `pulls?head=…&state=all` payload shapes it."""
    return {
        "number": number,
        "state": state,
        "merged_at": "2026-10-04T04:24:54Z" if merged else None,
        "base": {"ref": base},
    }


class TestPrState:
    """The PR-list half: which of four situations one head branch is in."""

    def test_unreadable_is_not_an_empty_list(self):
        """A failed REST call must never read as "there are no pull requests".

        That misreading is what would open a duplicate PR on every rate
        limit, and it is the live defect `tests/test_ensure_pr_exists.sh`
        case 1b reproduces.
        """
        assert branch_residue.pr_state(None, BASE)["state"] == branch_residue.STATE_UNREADABLE

    def test_no_pull_requests_at_all(self):
        assert branch_residue.pr_state([], BASE)["state"] == branch_residue.STATE_NONE

    def test_merged_into_the_release_branch(self):
        state = branch_residue.pr_state([_pr(4126)], BASE)
        assert state["state"] == branch_residue.STATE_MERGED
        assert state["merged_prs"] == [4126]

    def test_open_beats_merged(self):
        """An open PR carries the branch's CURRENT head, so it wins.

        Opening a second PR for a head that already has one would duplicate
        a live review, whatever else that head's history holds.
        """
        state = branch_residue.pr_state([_pr(4126), _pr(4200, state="open", merged=False)], BASE)
        assert state["state"] == branch_residue.STATE_OPEN
        assert state["open_prs"] == [4200]

    def test_merged_beats_closed_unmerged(self):
        state = branch_residue.pr_state([_pr(4100, merged=False), _pr(4126)], BASE)
        assert state["state"] == branch_residue.STATE_MERGED

    def test_closed_without_merging(self):
        state = branch_residue.pr_state([_pr(4130, merged=False)], BASE)
        assert state["state"] == branch_residue.STATE_CLOSED_UNMERGED
        assert state["closed_unmerged_prs"] == [4130]

    def test_merged_into_some_other_base_does_not_count(self):
        """ "Merged" must mean "merged into the release branch".

        A PR merged into another feature branch says nothing about whether
        this work reached the base being compared against — which is the
        only question asked here.
        """
        state = branch_residue.pr_state([_pr(4126, base="feat/other")], BASE)
        assert state["state"] == branch_residue.STATE_NONE
        assert state["merged_prs"] == []

    def test_open_against_another_base_still_counts_as_routed(self):
        """Deliberately asymmetric with the merged filter: see `pr_state`."""
        state = branch_residue.pr_state(
            [_pr(4200, state="open", merged=False, base="feat/other")], BASE
        )
        assert state["state"] == branch_residue.STATE_OPEN

    def test_paginated_pages_are_flattened(self):
        """`gh api --paginate` emits one array per page, not one merged array.

        A caller that forgets to slurp must not be classified as "no pull
        requests" — the most dangerous misreading in this module.
        """
        state = branch_residue.pr_state([[_pr(4126)], []], BASE)
        assert state["state"] == branch_residue.STATE_MERGED


class TestDisposition:
    """The action half: state plus git facts -> what to do."""

    def test_merged_with_residue_is_the_defect(self):
        assert (
            branch_residue.disposition(
                branch_residue.STATE_MERGED,
                unmerged_commits=1,
                content_landed=branch_residue.CONTENT_LANDED_FALSE,
            )
            == branch_residue.DISPOSITION_MERGED_RESIDUE
        )

    def test_merged_with_nothing_left_over_is_clean(self):
        assert (
            branch_residue.disposition(branch_residue.STATE_MERGED, unmerged_commits=0)
            == branch_residue.DISPOSITION_MERGED_CLEAN
        )

    def test_squash_merge_exemption(self):
        """Commits whose CONTENT is already on the base are not residue.

        A squash merge rewrites history, so every commit of a squash-merged
        branch reports as unmerged forever. Surfacing those would make the
        guard cry wolf on every squash-merged branch in the repo.
        """
        assert (
            branch_residue.disposition(
                branch_residue.STATE_MERGED,
                unmerged_commits=3,
                content_landed=branch_residue.CONTENT_LANDED_TRUE,
            )
            == branch_residue.DISPOSITION_MERGED_CLEAN
        )

    def test_unprovable_content_is_treated_as_residue(self):
        """`branch_content.py` fails closed, and this preserves the direction.

        For DELETION an unproven branch must be kept; for REPORTING an
        unproven branch must be surfaced. Both are "never silently lose the
        work", and only a positive proof clears a branch here.
        """
        assert (
            branch_residue.disposition(
                branch_residue.STATE_MERGED,
                unmerged_commits=1,
                content_landed=branch_residue.CONTENT_LANDED_UNKNOWN,
            )
            == branch_residue.DISPOSITION_MERGED_RESIDUE
        )

    def test_merged_without_the_git_half_is_unknown_not_clean(self):
        """The git question was never asked, so it must not be answered.

        Answering it from the PR list is the whole defect; a caller that
        skipped the count gets `unknown` and acts on nothing.
        """
        assert (
            branch_residue.disposition(branch_residue.STATE_MERGED, unmerged_commits=None)
            == branch_residue.DISPOSITION_UNKNOWN
        )

    @pytest.mark.parametrize(
        "state,expected",
        [
            (branch_residue.STATE_UNREADABLE, branch_residue.DISPOSITION_UNKNOWN),
            (branch_residue.STATE_OPEN, branch_residue.DISPOSITION_OPEN_PR),
            (branch_residue.STATE_CLOSED_UNMERGED, branch_residue.DISPOSITION_ABANDONED),
            (branch_residue.STATE_NONE, branch_residue.DISPOSITION_NO_PR),
            ("something-new", branch_residue.DISPOSITION_UNKNOWN),
        ],
    )
    def test_states_that_need_no_git_work(self, state, expected):
        """No git facts are passed: these four must decide without them.

        That is what keeps a branch with an open PR at one REST call and no
        fetch (first principle 1) — and an unrecognised state, which is a
        caller bug, must report ignorance rather than act.
        """
        assert branch_residue.disposition(state) == expected

    def test_no_pr_case_is_not_the_residue_case(self):
        """#3862 and #4151 must stay distinguishable (criterion 3).

        `no-pr` keeps #3862's handling (rescue draft, or deletion when the
        content is provably on the base); `merged-residue` gets this issue's.
        Collapsing them is how one gets handled as the other.
        """
        assert branch_residue.disposition(branch_residue.STATE_NONE) != branch_residue.disposition(
            branch_residue.STATE_MERGED,
            unmerged_commits=1,
            content_landed=branch_residue.CONTENT_LANDED_FALSE,
        )


class TestCli:
    """The two subcommands the shell calls."""

    def _run(self, args, stdin=""):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_pr_state_reads_stdin(self):
        import json

        result = self._run(["pr-state", "--base", BASE], stdin=json.dumps([_pr(4126)]))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["state"] == branch_residue.STATE_MERGED

    def test_unparseable_stdin_is_unreadable_not_empty(self):
        import json

        result = self._run(["pr-state", "--base", BASE], stdin="<html>502</html>")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["state"] == branch_residue.STATE_UNREADABLE

    def test_disposition_subcommand(self):
        result = self._run(
            [
                "disposition",
                "--state",
                branch_residue.STATE_MERGED,
                "--unmerged-count",
                "1",
                "--content-landed",
                branch_residue.CONTENT_LANDED_FALSE,
            ]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == branch_residue.DISPOSITION_MERGED_RESIDUE

    def test_empty_count_means_not_computed(self):
        result = self._run(
            ["disposition", "--state", branch_residue.STATE_MERGED, "--unmerged-count", ""]
        )
        assert result.stdout.strip() == branch_residue.DISPOSITION_UNKNOWN

    def test_non_numeric_count_is_not_read_as_zero(self):
        """A garbled count must not become "merged, nothing left over".

        `_count_lines` always prints a number, but the one failure mode worth
        being deaf to is the one that silently clears a branch.
        """
        result = self._run(
            [
                "disposition",
                "--state",
                branch_residue.STATE_MERGED,
                "--unmerged-count",
                "not-a-number",
            ]
        )
        assert result.stdout.strip() == branch_residue.DISPOSITION_UNKNOWN


class TestResidueShellBehaviour:
    """Runs `tests/test_stranded_residue.sh` (real bare origin, stub `gh`).

    The classification above is only half the guard: the residue draft PR, its
    marker, the loud comment on the closed issue, the repo-wide sweep and the
    fault injection that shows the retired "any PR exists" form stranding the
    same fixture all live in the shell suite. Wiring it in here means
    `pytest -v` — the gate this repo actually runs — executes it too.
    """

    def test_shell_suite_passes(self):
        if shutil.which("jq") is None:
            pytest.skip("jq is required by the shell suite")
        suite = Path(__file__).resolve().parents[1] / "test_stranded_residue.sh"
        result = subprocess.run(
            [bash4_or_skip(), str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
