"""A blocking gate is evaluated where the change is reviewed (#4209).

THE CLASS. A gate that is declared blocking must be *evaluated* on the head
where the change that can break it is reviewed. A gate that only runs after the
merge does not gate the change at all: it converts one PR's regression into the
next PR's unexplained red build, and the attribution lands on whichever author
happened to touch the same directory next.

THE INSTANCE. `ci-tests.yml` carried seven steps labelled "(blocking)" --
`ruff`, `black`, `mypy`, `pytest`, `tsc`, `eslint`, `vitest --coverage` -- on a
workflow with no `pull_request` trigger and three jobs each gated
`if: github.ref == RELEASE_BRANCH`. So for the whole life of every PR, none of
them was ever evaluated. #4207 shipped two failing web tests through a green
PR; the review round found them by running the suite by hand, and the 100%
coverage gate had been red on the release branch itself for long enough that
nobody could say which merge did it.

WHY A TEST AND NOT A NOTE. CLAUDE.md's own rule (#4174): a rule this project
states but does not ENFORCE is itself a defect class. The arrangement #4209
removed was *documented* as deliberate, with a comment explaining the
post-merge tripwire; the documentation was true about half of it and nothing
checked the other half. This file is that check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
REQUIRED_CHECKS = REPO_ROOT / ".github" / "required-checks.txt"
CI_TESTS = WORKFLOW_DIR / "ci-tests.yml"

#: How a step declares itself a gate in this tree. Every blocking step in
#: `ci-tests.yml` and `validate-web-routes.yml` is named this way, and the
#: convention is cheap to keep because the name is what shows in the log.
BLOCKING_MARKER = "(blocking)"

PR_TRIGGERS = ("pull_request", "pull_request_target")

#: Expression fragments that admit a non-push event. A job condition mentioning
#: any of these has considered the question; one that tests `github.ref` and
#: mentions none of them is push-only, whether or not its author meant it.
PR_ADMITTING_TOKENS = (
    "github.event_name",
    "pull_request",
    "github.base_ref",
    "github.head_ref",
)


def _workflows() -> dict[Path, dict]:
    loaded: dict[Path, dict] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        if isinstance(data, dict):
            loaded[path] = data
    return loaded


def _triggers(workflow: dict) -> dict:
    # `on` is YAML 1.1 truthy, so a safe_load turns the key into `True`.
    raw = workflow.get("on", workflow.get(True, {}))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return dict.fromkeys(raw)
    return raw if isinstance(raw, dict) else {}


def excludes_pull_requests(condition: object) -> bool:
    """Does this job-level `if:` keep the job off a pull-request head?

    Deliberately narrow. Only one shape in this tree can do it by accident,
    and it is the one #4209 found: a bare `github.ref == <release branch>`
    test, with nothing in the expression that admits an event other than a
    push. Every other ref-guarded job in the tree writes
    `github.event_name != 'push' || github.ref == ...` or
    `github.event_name == 'pull_request' || github.ref == ...`, and those are
    recognised here rather than enumerated as exceptions, so a new job written
    either way passes without anyone editing this file.
    """
    if not isinstance(condition, str) or not condition.strip():
        return False
    if "github.ref" not in condition:
        return False
    return not any(token in condition for token in PR_ADMITTING_TOKENS)


def _blocking_jobs() -> list[tuple[Path, str, dict, dict]]:
    found = []
    for path, workflow in _workflows().items():
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps") or []
            if any(
                BLOCKING_MARKER in str(step.get("name", ""))
                for step in steps
                if isinstance(step, dict)
            ):
                found.append((path, job_id, job, workflow))
    return found


def test_the_tree_actually_has_blocking_gates_to_check() -> None:
    """Guard against the guard going vacuous.

    If someone drops the "(blocking)" convention this file stops asserting
    anything and still passes -- the exact silent-pass shape #4209 is about.
    """
    assert len(_blocking_jobs()) >= 2, (
        "no job in .github/workflows declares a '(blocking)' step -- either the "
        "naming convention changed (update BLOCKING_MARKER) or the gates are gone"
    )


@pytest.mark.parametrize(
    "path,job_id",
    [(path, job_id) for path, job_id, _, _ in _blocking_jobs()],
    ids=[f"{path.name}:{job_id}" for path, job_id, _, _ in _blocking_jobs()],
)
def test_every_blocking_gate_is_reachable_on_a_pull_request(path: Path, job_id: str) -> None:
    """The invariant. Both halves of reachability, because either one alone
    produces a gate that never evaluates: the workflow must fire on a pull
    request, and the job's own condition must not then exclude it."""
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"][job_id]
    triggers = _triggers(workflow)

    assert any(name in triggers for name in PR_TRIGGERS), (
        f"{path.name}: job '{job_id}' has a '{BLOCKING_MARKER}' step but the "
        f"workflow has no {' or '.join(PR_TRIGGERS)} trigger, so the gate is "
        "only ever evaluated after the merge that broke it (#4209)"
    )
    assert not excludes_pull_requests(job.get("if")), (
        f"{path.name}: job '{job_id}' has a '{BLOCKING_MARKER}' step but its "
        f"`if:` tests github.ref without admitting a pull request:\n"
        f"    if: {job.get('if')}\n"
        "Write `github.event_name == 'pull_request' || github.ref == ...` as the "
        "rest of the tree does (#4209)."
    )


# --------------------------------------------------------------------------
# The discriminator itself, in both directions. An `if:` matcher that answers
# "fine" to everything is worse than no test, so the shapes it must separate
# are asserted rather than assumed.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        # The pre-#4209 form, verbatim.
        "github.ref == format('refs/heads/{0}', vars.RELEASE_BRANCH)",
        "github.ref == 'refs/heads/master'",
        "github.ref_name == vars.RELEASE_BRANCH && github.ref != ''",
    ],
)
def test_the_matcher_catches_a_push_only_condition(condition: str) -> None:
    assert excludes_pull_requests(condition) is True


@pytest.mark.parametrize(
    "condition",
    [
        # The two idioms the rest of this tree already uses.
        "github.event_name != 'push' || github.ref == format('refs/heads/{0}', vars.RELEASE_BRANCH)",
        "github.event_name == 'pull_request' || github.ref == format('refs/heads/{0}', vars.RELEASE_BRANCH)",
        # No ref test at all.
        "always()",
        "success() && github.actor != 'dependabot[bot]'",
        None,
        "",
    ],
)
def test_the_matcher_passes_a_condition_that_admits_a_pull_request(
    condition: object,
) -> None:
    assert excludes_pull_requests(condition) is False


# --------------------------------------------------------------------------
# The instance, asserted directly. The parametrised invariant above would also
# catch a regression here, but only while the "(blocking)" names survive -- and
# these three jobs are the ones the issue was filed about.
# --------------------------------------------------------------------------


def test_ci_tests_fires_on_pull_requests() -> None:
    triggers = _triggers(yaml.safe_load(CI_TESTS.read_text()))
    assert "pull_request" in triggers, (
        "ci-tests.yml must fire on pull requests: it carries every Python and "
        "web blocking gate this project has (#4209)"
    )
    assert "push" in triggers, (
        "ci-tests.yml must also keep firing on pushes -- the post-merge "
        "tripwire catches collisions between two PRs that could not see each "
        "other (owner decision, 2026-08-18)"
    )


@pytest.mark.parametrize("job_id", ["test", "determinism", "web"])
def test_each_ci_tests_job_runs_on_a_pull_request_head(job_id: str) -> None:
    job = yaml.safe_load(CI_TESTS.read_text())["jobs"][job_id]
    assert not excludes_pull_requests(
        job.get("if")
    ), f"ci-tests.yml job '{job_id}' is gated off pull requests: {job.get('if')}"


@pytest.mark.parametrize("job_id", ["test", "determinism", "web"])
def test_each_ci_tests_job_is_a_required_check(job_id: str) -> None:
    """Reachable is not the same as gating.

    A job that runs on the PR head but is not in `[required]` is watched by
    nothing: `developer_submit_for_review.sh` submits over it and the review
    trigger does not wait for it, so a red suite reaches a reviewer looking
    like a green head. `test_required_checks.py` polices the file's internal
    consistency; this asserts WHICH side of it these three are on.
    """
    text = REQUIRED_CHECKS.read_text()
    required_block = text.split("[required]", 1)[1].split("[not-required]", 1)[0]
    names = {
        line.split("#", 1)[0].strip()
        for line in required_block.splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert job_id in names, (
        f"'{job_id}' runs on PR heads but is not in [required] in "
        ".github/required-checks.txt, so nothing waits for it (#4209)"
    )


# --------------------------------------------------------------------------
# The third surface, and the one the sweep found rather than the issue naming
# it: the developer agent's own Final Verification gate. A gate that CI
# evaluates on the PR head is still not evaluated before the PR is SUBMITTED,
# and `run_final_verification.sh` -- whose workflow step says it "Runs ALL
# checks unconditionally on the entire codebase" -- ran six gates and not
# vitest. #4207's two failing web tests passed it.
# --------------------------------------------------------------------------

FINAL_VERIFICATION = REPO_ROOT / "scripts" / "agents" / "run_final_verification.sh"

#: The blocking gates, named once. Each must appear in `ci-tests.yml` as a
#: "(blocking)" step and in Final Verification's default gate list.
BLOCKING_GATES = ("black", "ruff", "mypy", "pytest", "tsc", "vitest")


def _final_verification_gates() -> list[str]:
    """The default `GATES` list, read from the script rather than restated."""
    text = FINAL_VERIFICATION.read_text()
    match = re.search(r'GATES="\$\{NYXGPT_VERIFY_GATES:-([^}]*)\}"', text)
    assert match, "could not find the default GATES list in run_final_verification.sh"
    return match.group(1).split()


@pytest.mark.parametrize("gate", BLOCKING_GATES)
def test_final_verification_runs_every_blocking_gate(gate: str) -> None:
    """The developer's pre-submit gate and CI's gate are the same set.

    They are stated in two places, so the two can disagree -- and did, for the
    one gate that mattered: `vitest --coverage` is blocking in `ci-tests.yml`
    and was absent here, so a web regression passed the pre-submit gate AND
    the PR (which never ran the job), and the release branch went red after
    the merge. Consolidating further would mean the shell script importing the
    workflow's step names; holding the two to one list is the proportionate
    answer, and it is the part that fails silently.
    """
    gates = _final_verification_gates()
    assert gate in gates, (
        f"'{gate}' is a blocking gate in ci-tests.yml but is not in "
        f"run_final_verification.sh's default gate list ({' '.join(gates)}), "
        "so the developer agent can submit a PR that fails it (#4209)"
    )


def test_final_verification_runs_the_same_coverage_command() -> None:
    """Same argv as CI and as a developer, so the three cannot drift."""
    assert "npm run test:coverage" in FINAL_VERIFICATION.read_text(), (
        "Final Verification's vitest gate must invoke web/package.json's "
        "test:coverage script, not its own copy of the vitest argv (#4209)"
    )


def test_the_vitest_gate_has_a_recorded_failure_reason() -> None:
    """#4176's recording covers the new gate too.

    `_fail_gate` writes the reason Phase 1's classifier reads. A gate whose
    name has no label there reaches the owner as "Error type could not be
    determined", which is the headline #4176 removed -- and a coverage
    regression is the awkward case: it fails with NO failing test, so a
    reason grepped only for failing tests would be empty.
    """
    evidence = (REPO_ROOT / "scripts" / "agents" / "lib" / "escalation_evidence.py").read_text()
    assert '"vitest":' in evidence, (
        "escalation_evidence.py's _GATE_LABELS must name the vitest gate, or a "
        "failed web suite is recorded as an unexplained verification failure"
    )
    script = FINAL_VERIFICATION.read_text()
    assert "ERROR: Coverage" in script, (
        "the vitest gate must grep for the coverage-threshold line: a coverage "
        "regression reports zero failing tests, so a test-only grep records an "
        "empty reason (#4176)"
    )


@pytest.mark.parametrize("script", ["test", "test:coverage"])
def test_the_web_gate_can_be_run_non_interactively(script: str) -> None:
    """The gate must terminate when it is run the documented way.

    `vitest` with no subcommand is WATCH mode unless `$CI` is set, so
    `npm run test:coverage` -- the command `docs/testing.md` and
    `web/tests/README.md` both tell a developer to run, and the only local way
    to answer the 100% coverage gate -- sat on a file watcher forever instead
    of printing a verdict. The other half of #4209 is that this gate was never
    evaluated in CI either, so between the two there was no path on which it
    ran at all; the release branch had been below the threshold for long
    enough that no merge could be blamed for it.

    `vitest run` is explicit and behaves the same way in both places, which is
    also what lets the CI step BE `npm run test:coverage` rather than a second
    spelling of it.
    """
    import json

    scripts = json.loads((REPO_ROOT / "web" / "package.json").read_text())["scripts"]
    command = scripts[script]
    assert "vitest run" in command, (
        f"web/package.json's '{script}' is `{command}`: bare `vitest` is watch "
        "mode outside CI, so the documented way to run this gate does not "
        "terminate (#4209)"
    )


def test_the_ci_coverage_step_runs_the_same_command_a_developer_does() -> None:
    """One source per decision, applied to the gate's own argv.

    The CI step said `npx vitest run --coverage` and the npm script said
    `vitest --coverage`. Two spellings of one gate is two things to keep in
    step, and they were already out of step.
    """
    jobs = yaml.safe_load(CI_TESTS.read_text())["jobs"]
    steps = [step for step in jobs["web"]["steps"] if "vitest" in str(step.get("name", "")).lower()]
    assert len(steps) == 1, "ci-tests.yml's web job must have one vitest step"
    assert "npm run test:coverage" in steps[0]["run"], (
        "the blocking coverage step must invoke web/package.json's "
        "test:coverage script, not its own copy of the vitest argv (#4209)"
    )


def test_all_three_ci_tests_jobs_share_one_scope_resolver() -> None:
    """One source per decision (CLAUDE.md).

    The three jobs asked "what did this change touch" three times, each
    reading `github.event.before` -- a field that is empty on a pull request.
    That duplication is why pointing the gates at a PR head was not a one-line
    change, and re-introducing an inline copy would let one job answer the
    question differently from the other two.
    """
    jobs = yaml.safe_load(CI_TESTS.read_text())["jobs"]
    for job_id in ("test", "determinism", "web"):
        resolvers = [
            step
            for step in jobs[job_id]["steps"]
            if "scripts/ci/diff_scope.py" in str(step.get("run", ""))
        ]
        assert len(resolvers) == 1, (
            f"ci-tests.yml job '{job_id}' must resolve its diff through "
            f"scripts/ci/diff_scope.py exactly once, found {len(resolvers)}"
        )
        env = resolvers[0].get("env") or {}
        assert "PUSH_BEFORE_SHA" in env and "PR_BASE_SHA" in env, (
            f"ci-tests.yml job '{job_id}' must hand the resolver BOTH event "
            "shapes' base fields -- handing it only one is the pre-#4209 defect"
        )

    # And nothing else may read an event's base field directly: a second
    # reader is a second copy of the decision, which is how one job came to
    # answer "what changed" differently from the other two.
    for job_id, job in jobs.items():
        for step in job["steps"]:
            run = str(step.get("run", ""))
            env = step.get("env") or {}
            raw = run + "".join(str(value) for value in env.values())
            if "scripts/ci/diff_scope.py" in run:
                continue
            assert "github.event.before" not in raw, (
                f"ci-tests.yml job '{job_id}', step "
                f"'{step.get('name', step.get('uses'))}' reads "
                "github.event.before directly -- it is empty on a pull "
                "request, which is the whole of #4209"
            )
            assert "github.event.pull_request.base.sha" not in raw, (
                f"ci-tests.yml job '{job_id}', step "
                f"'{step.get('name', step.get('uses'))}' reads the PR base "
                "SHA directly instead of through diff_scope.py"
            )
