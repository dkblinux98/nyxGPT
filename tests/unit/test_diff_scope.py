"""The CI scope resolver answers for both event shapes (#4209).

`scripts/ci/diff_scope.py` replaced three inline copies of one decision in
`ci-tests.yml`, each of which read the base commit out of `github.event.before`
-- a field that is populated on a `push` and empty on a `pull_request`. That is
why the blocking gates could not simply be pointed at a PR head: every copy had
to independently learn about a second event shape, and a copy that got it wrong
would not fail loudly. It would resolve no base, take the "run everything"
path, and look exactly like a correct run that happened to be unscoped -- or,
worse under the pre-#4209 code, take the branch that silently linted nothing
(#3938).

So the properties asserted here are the ones that fail silently in production:
that a pull request resolves a base at all, that it resolves the MERGE BASE
rather than the base branch tip, that an all-zeros or absent predecessor means
"run everything" rather than "nothing changed", and that an event with no
predecessor at all (`workflow_dispatch`) is unscoped rather than empty.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "diff_scope.py"


def _load():
    spec = importlib.util.spec_from_file_location("diff_scope", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves annotations through
    # `sys.modules[cls.__module__]`, so a module loaded by path alone raises
    # AttributeError on the first frozen dataclass.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diff_scope = _load()

HEAD = "h" * 40
PR_BASE = "b" * 40
MERGE_BASE = "m" * 40
PUSH_BEFORE = "p" * 40


class FakeGit:
    """A `git` stand-in that records argv and answers from a script.

    The real module shells out, so the only way to assert *which* commit it
    diffed from is to watch the calls.
    """

    def __init__(
        self,
        *,
        known: Sequence[str] = (),
        merge_base: str | None = MERGE_BASE,
        changed: Sequence[str] = (),
        diff_raises: bool = False,
    ) -> None:
        self.known = set(known)
        self.merge_base = merge_base
        self.changed = list(changed)
        self.diff_raises = diff_raises
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "rev-parse":
            sha = args[-1].split("^")[0]
            if sha not in self.known:
                raise subprocess.CalledProcessError(1, ["git", *args])
            return f"{sha}\n"
        if args[0] == "merge-base":
            if self.merge_base is None:
                raise subprocess.CalledProcessError(1, ["git", *args])
            return f"{self.merge_base}\n"
        if args[0] == "diff":
            if self.diff_raises:
                raise subprocess.CalledProcessError(128, ["git", *args])
            return "".join(f"{path}\n" for path in self.changed)
        raise AssertionError(f"unexpected git call: {args}")

    @property
    def diffed_from(self) -> str | None:
        for call in self.calls:
            if call[0] == "diff":
                return call[2]
        return None


# --------------------------------------------------------------------------
# base_sha: the decision that did not exist for pull requests before #4209
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_a_pull_request_resolves_a_base(event: str) -> None:
    """The defect in one line: a PR used to resolve no base at all.

    `github.event.before` is empty on these events, so the pre-#4209 steps
    took their "no usable before-sha" path on every pull request -- which is
    why the gates could not be moved without this.
    """
    git = FakeGit(known=[PR_BASE, HEAD])
    assert (
        diff_scope.base_sha(event, push_before=None, pr_base=PR_BASE, head=HEAD, git=git)
        == MERGE_BASE
    )


def test_a_pull_request_diffs_from_the_merge_base_not_the_base_tip() -> None:
    """`base.sha` is the base branch tip, not where this PR started.

    This release branch takes several merges a day. Diffing from the tip would
    attribute every one of them to the PR under review: the `web` job would run
    on a PR that touched no web file, `full_suite` would be true for a
    docs-only PR, and -- the one that actually misleads -- the blocking eslint
    step would block this author on another author's standing debt, which is
    the exact defect the step's own comment says it exists to remove (#3938).
    """
    git = FakeGit(known=[PR_BASE, HEAD], changed=["docs/testing.md"])
    scope = diff_scope.resolve(
        "pull_request", push_before=None, pr_base=PR_BASE, head=HEAD, git=git
    )
    assert scope.base == MERGE_BASE
    assert git.diffed_from == MERGE_BASE


def test_a_pull_request_falls_back_to_the_base_tip_when_histories_are_unlinked() -> None:
    """A shallow clone can leave `merge-base` with nothing to answer.

    The base tip is a worse diff than the merge base and a far better one than
    "no identifiable base", which would run every gate on every PR.
    """
    git = FakeGit(known=[PR_BASE, HEAD], merge_base=None)
    assert (
        diff_scope.base_sha("pull_request", push_before=None, pr_base=PR_BASE, head=HEAD, git=git)
        == PR_BASE
    )


def test_a_push_resolves_the_event_before_sha() -> None:
    git = FakeGit(known=[PUSH_BEFORE, HEAD])
    assert (
        diff_scope.base_sha("push", push_before=PUSH_BEFORE, pr_base=None, head=HEAD, git=git)
        == PUSH_BEFORE
    )


def test_a_push_ignores_a_pull_request_base_and_vice_versa() -> None:
    """Neither event may read the other's field.

    A `pull_request` run has a `github.event.before` of "" and a `push` run has
    a `github.event.pull_request.base.sha` of "", so reading the wrong one is
    silent -- it resolves nothing and the run is simply unscoped.
    """
    git = FakeGit(known=[PUSH_BEFORE, PR_BASE, HEAD])
    assert (
        diff_scope.base_sha("push", push_before=None, pr_base=PR_BASE, head=HEAD, git=git) is None
    )
    assert (
        diff_scope.base_sha(
            "pull_request", push_before=PUSH_BEFORE, pr_base=None, head=HEAD, git=git
        )
        is None
    )


def test_the_all_zeros_sha_of_a_branch_creation_push_is_not_a_base() -> None:
    """The first push of the next release line sends `before=0000...`.

    git will happily be asked about it; the answer that matters is that there
    is no predecessor, so nothing may be scoped away.
    """
    git = FakeGit(known=[diff_scope.NULL_SHA, HEAD])
    assert (
        diff_scope.base_sha(
            "push", push_before=diff_scope.NULL_SHA, pr_base=None, head=HEAD, git=git
        )
        is None
    )
    assert git.calls == [], "the null SHA is rejected without asking git"


def test_an_unfetched_base_is_not_a_base() -> None:
    """A commit this checkout does not have cannot be diffed against."""
    git = FakeGit(known=[HEAD])
    assert (
        diff_scope.base_sha("push", push_before=PUSH_BEFORE, pr_base=None, head=HEAD, git=git)
        is None
    )


def test_an_event_with_no_predecessor_is_unscoped() -> None:
    """`workflow_dispatch`/`schedule` imply no previous state."""
    git = FakeGit(known=[HEAD])
    assert (
        diff_scope.base_sha("workflow_dispatch", push_before=None, pr_base=None, head=HEAD, git=git)
        is None
    )


# --------------------------------------------------------------------------
# Scope: paths -> which gates apply
# --------------------------------------------------------------------------


def _scope(changed: Sequence[str]) -> object:
    git = FakeGit(known=[PR_BASE, HEAD], changed=changed)
    return diff_scope.resolve("pull_request", push_before=None, pr_base=PR_BASE, head=HEAD, git=git)


@pytest.mark.parametrize(
    "path",
    [
        "web/src/app/models/page.tsx",
        "web/package.json",
        "web/next.config.ts",  # #3938: the file a git pathspec silently missed
    ],
)
def test_a_web_path_selects_the_web_gates(path: str) -> None:
    assert _scope([path]).web is True


@pytest.mark.parametrize("path", ["docs/testing.md", "agents/LEDGER.md", "terraform/main.tf"])
def test_a_non_web_path_does_not_select_the_web_gates(path: str) -> None:
    assert _scope([path]).web is False


@pytest.mark.parametrize(
    "path",
    [
        "src/nyxgpt/ops.py",
        "tests/unit/test_ops.py",  # a *test* is what broke determinism (#4020)
        "scripts/ci/diff_scope.py",
        "pyproject.toml",
    ],
)
def test_a_python_path_selects_the_python_gates(path: str) -> None:
    assert _scope([path]).python is True


def test_a_prose_only_change_selects_neither() -> None:
    scope = _scope(["docs/testing.md", "agents/LEDGER.md"])
    assert (scope.web, scope.python, scope.full_suite) == (False, False, False)


@pytest.mark.parametrize(
    "path", ["src/nyxgpt/api.py", "web/src/app/page.tsx", "pyproject.toml", "tests/conftest.py"]
)
def test_product_code_and_the_root_conftest_run_the_whole_suite(path: str) -> None:
    """These can reach any suite, so narrowing on them would be a guess."""
    assert _scope([path]).full_suite is True


def test_a_leaf_test_file_does_not_force_the_whole_suite() -> None:
    """`tests/unit/...` is selectable; only the ROOT conftest is not."""
    scope = _scope(["tests/unit/test_ops.py"])
    assert scope.python is True
    assert scope.full_suite is False


# --------------------------------------------------------------------------
# Unidentified means everything -- the property that keeps a miss expensive
# rather than silent
# --------------------------------------------------------------------------


def test_an_unidentifiable_base_selects_every_gate() -> None:
    git = FakeGit(known=[HEAD])
    scope = diff_scope.resolve(
        "push", push_before=diff_scope.NULL_SHA, pr_base=None, head=HEAD, git=git
    )
    assert scope.identified is False
    assert scope.base is None
    assert scope.changed == ()
    assert (scope.web, scope.python, scope.full_suite) == (True, True, True)


def test_a_failed_diff_selects_every_gate() -> None:
    """A base that exists but cannot be diffed is still "unknown", not "empty"."""
    git = FakeGit(known=[PR_BASE, HEAD], diff_raises=True)
    scope = diff_scope.resolve(
        "pull_request", push_before=None, pr_base=PR_BASE, head=HEAD, git=git
    )
    assert scope.identified is False
    assert (scope.web, scope.python, scope.full_suite) == (True, True, True)


def test_an_empty_diff_is_identified_and_selects_nothing() -> None:
    """Distinct from the case above: we know what changed, and it is nothing."""
    scope = _scope([])
    assert scope.identified is True
    assert (scope.web, scope.python, scope.full_suite) == (False, False, False)


# --------------------------------------------------------------------------
# The $GITHUB_OUTPUT contract the workflow's `if:` expressions read
# --------------------------------------------------------------------------


def test_github_output_emits_lowercase_booleans_the_workflow_compares_against() -> None:
    """`if: steps.scope.outputs.web == 'true'` is a STRING comparison.

    Python's `str(True)` is "True", which that expression reads as false --
    and a gate that silently does not run is this whole issue.
    """
    scope = _scope(["web/src/app/page.tsx"])
    text = diff_scope.github_output(scope)
    assert "web=true" in text.splitlines()
    assert "python=false" in text.splitlines()
    assert "identified=true" in text.splitlines()
    assert "True" not in text


def test_github_output_wraps_the_file_list_in_a_heredoc() -> None:
    """A diff is multi-line, so `name=value` would truncate it to one path."""
    scope = _scope(["web/a.tsx", "web/b.tsx"])
    lines = diff_scope.github_output(scope).splitlines()
    start = lines.index("changed<<NYXGPT_DIFF_SCOPE_EOF")
    end = lines.index("NYXGPT_DIFF_SCOPE_EOF")
    assert lines[start + 1 : end] == ["web/a.tsx", "web/b.tsx"]


def test_github_output_emits_an_empty_base_rather_than_the_word_none() -> None:
    git = FakeGit(known=[HEAD])
    scope = diff_scope.resolve("push", push_before=None, pr_base=None, head=HEAD, git=git)
    assert "base=" in diff_scope.github_output(scope).splitlines()


def test_the_module_runs_end_to_end_against_this_repository() -> None:
    """The real `git` path, exercised once -- the fakes above cannot see a
    wrong argv that git would reject."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    scope = diff_scope.resolve("pull_request", push_before=None, pr_base=base, head=head)
    assert scope.identified is True
    assert scope.base is not None
