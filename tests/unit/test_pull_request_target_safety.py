"""`pull_request_target` carries this repo's secrets; it must never run PR code (#4167).

WHY THE TRIGGER CHANGED. `pr-hygiene` in `ensure_project_hygiene.yml` is what
puts an issue-less PR on the board in `In Review`, which is how the review
agent finds it at all. It ran on `pull_request`, and GitHub treats a
Dependabot-authored `pull_request` run as a fork PR: Actions secrets are
withheld and replaced by the Dependabot secret store, which is empty here. So
`SCRUMMASTER_AGENT_TOKEN` arrived blank, the job died at `require_gh_auth`
("gh not authenticated") on **every** Dependabot PR, and none of those PRs
ever entered the pipeline -- #4163 and #4165 sat open and unassigned with no
Status while carrying a high and a **critical** advisory. `pull_request_target`
runs the base branch's copy of the workflow with the repository's own secrets
regardless of the actor, which fixes that.

WHAT IT COSTS, AND WHAT THIS FILE PINS. `pull_request_target` is the one
trigger that combines write-capable secrets with an arbitrary author's branch.
It is safe here for exactly one reason -- the job checks out
`vars.RELEASE_BRANCH` and makes API calls, and at no point reads, builds or
executes anything from the PR's tree. That reason is a property of the file,
not of the trigger, so a later edit that adds `ref: ${{
github.event.pull_request.head.sha }}`, a `gh pr checkout`, or an `npm
install` over the PR's lockfile would silently convert a bookkeeping job into
arbitrary code execution holding a `workflow`-scoped PAT. Nothing about the
diff would look alarming.

The invariant is therefore written over EVERY `pull_request_target` workflow
rather than over the one job that needs it today, because the next one will
be added by someone reading that it worked here. `project-hygiene-smoke.yml`'s
`pr-head-guard-discriminates` job executes these assertions against a
deliberately broken copy of the workflow, so the guard cannot pass by being
vacuous.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
HYGIENE = WORKFLOWS / "ensure_project_hygiene.yml"
REQUIRED_CHECKS = ROOT / ".github" / "required-checks.txt"

#: Expressions and commands that resolve to the pull request's HEAD -- i.e. to
#: code the PR's author controls. Any of these inside a `pull_request_target`
#: workflow is the documented privilege-escalation shape.
PR_HEAD_FORMS = (
    "github.event.pull_request.head.sha",
    "github.event.pull_request.head.ref",
    "github.event.pull_request.head.repo",
    "github.event.pull_request.merge_commit_sha",
    "github.head_ref",
    "refs/pull/",
    "gh pr checkout",
)


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _triggers(data: dict) -> dict:
    # PyYAML resolves the bare `on:` key to the boolean True.
    triggers = data.get("on", data.get(True))
    return triggers if isinstance(triggers, dict) else {}


def _pull_request_target_workflows() -> list[Path]:
    return [
        p for p in sorted(WORKFLOWS.glob("*.yml")) if "pull_request_target" in _triggers(_load(p))
    ]


def _uncommented(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


def _checkout_steps(job: dict) -> list[dict]:
    return [
        step
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and str(step.get("uses") or "").startswith("actions/checkout")
    ]


class TestTheTriggerIsWhatTheFixRequires:
    """A Dependabot PR has to reach a run that actually holds the secret."""

    def test_pr_hygiene_runs_on_pull_request_target(self):
        data = _load(HYGIENE)
        assert "pull_request_target" in _triggers(data), (
            "pr-hygiene is back on a trigger GitHub strips secrets from for "
            "Dependabot PRs; it will die at require_gh_auth again (#4167)."
        )
        cond = data["jobs"]["pr-hygiene"]["if"]
        assert "pull_request_target" in cond
        # The old name must be gone from the gate, not merely joined: a job
        # gated on `pull_request` under a `pull_request_target`-only trigger
        # never runs at all, which is the same outcome as the bug.
        assert not re.search(r"pull_request'", cond), cond

    def test_it_still_fires_on_opened_and_reopened_only(self):
        types = _triggers(_load(HYGIENE))["pull_request_target"]["types"]
        assert sorted(types) == ["opened", "reopened"]

    def test_no_pull_request_trigger_is_left_behind(self):
        """A leftover `pull_request:` would spin a run per PR in which every
        job's `if:` is false -- the cost of the old trigger with none of its
        effect."""
        assert "pull_request" not in _triggers(_load(HYGIENE))

    def test_the_issues_jobs_are_untouched(self):
        jobs = _load(HYGIENE)["jobs"]
        for job_id in ("add-to-project", "closure-hygiene"):
            assert "github.event_name == 'issues'" in jobs[job_id]["if"], job_id


class TestNoPullRequestTargetJobTouchesPRCode:
    """The property that makes the trigger safe, stated over every file."""

    def test_there_is_at_least_one_such_workflow(self):
        """Otherwise every assertion below passes over an empty list."""
        assert _pull_request_target_workflows()

    def test_no_pr_head_reference_appears_anywhere_in_them(self):
        offenders = []
        for path in _pull_request_target_workflows():
            text = _uncommented(path)
            for form in PR_HEAD_FORMS:
                if form in text:
                    offenders.append(f"{path.name}: {form}")
        assert not offenders, (
            "a `pull_request_target` workflow runs with this repository's "
            "secrets on a branch its author controls, so resolving anything "
            "to the PR head hands those secrets to that author: " + ", ".join(offenders)
        )

    def test_every_checkout_pins_an_explicit_base_ref(self):
        """A bare `actions/checkout` under `pull_request_target` takes the
        base branch today, which is safe -- but it is safe by default rather
        than by statement, and the next reader cannot tell the two apart.
        Requiring the `ref:` keeps the intent in the file."""
        offenders = []
        for path in _pull_request_target_workflows():
            for job_id, job in (_load(path).get("jobs") or {}).items():
                for step in _checkout_steps(job or {}):
                    ref = str((step.get("with") or {}).get("ref") or "")
                    if "vars.RELEASE_BRANCH" not in ref:
                        offenders.append(f"{path.name}:{job_id} ref={ref!r}")
        assert not offenders, (
            "these checkouts in `pull_request_target` workflows do not pin "
            "vars.RELEASE_BRANCH: " + ", ".join(offenders)
        )

    def test_nothing_in_them_installs_or_runs_the_prs_own_tree(self):
        """The subtler form of the same mistake: the checkout is correct and
        then a build step executes attacker-authored content anyway."""
        banned = re.compile(
            r"\b(npm (ci|install|run)|yarn install|pnpm install|pip install -e|"
            r"bundle install|make\b|setup\.py)",
        )
        offenders = []
        for path in _pull_request_target_workflows():
            for job_id, job in (_load(path).get("jobs") or {}).items():
                for step in (job or {}).get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    body = str(step.get("run") or "")
                    code = "\n".join(
                        ln for ln in body.splitlines() if not ln.strip().startswith("#")
                    )
                    hit = banned.search(code)
                    if hit:
                        offenders.append(f"{path.name}:{job_id} {hit.group(0)!r}")
        assert not offenders, (
            "a `pull_request_target` job must not run a build or dependency "
            "install -- the tree it would act on is the PR author's: " + ", ".join(offenders)
        )


class TestTheAgentPathIsUnaffected:
    """The fix must not change what happens to a PR the submit script handled.

    `developer_submit_for_review.sh` already sets Status, so hygiene's whole
    job on an agent PR is to notice that and do nothing. If that skip were
    lost, every agent submission would race the script for the lane -- the
    #3814 clobber shape, on PRs this time.
    """

    def test_the_already_has_status_skip_still_short_circuits(self):
        steps = _load(HYGIENE)["jobs"]["pr-hygiene"]["steps"]
        check = next(s for s in steps if s.get("id") == "check")
        assert "needs_hygiene=false" in check["run"]
        apply_step = next(s for s in steps if "Apply PR project hygiene" in str(s.get("name")))
        assert apply_step["if"] == "steps.check.outputs.needs_hygiene == 'true'"

    def test_the_apply_step_re_reads_status_after_the_head_start(self):
        """The second read is the half that actually wins the race: the
        submit script may set Status during the 60s wait."""
        steps = _load(HYGIENE)["jobs"]["pr-hygiene"]["steps"]
        apply_step = next(s for s in steps if "Apply PR project hygiene" in str(s.get("name")))
        body = apply_step["run"]
        assert "sleep 60" in body
        assert body.index("sleep 60") < body.index("gained Status")


class TestTheCheckStaysOffTheHeadGate:
    """A `pull_request_target` run is associated with the BASE commit.

    `required_check_state` matches names against the check runs GitHub
    reports for the PR's head SHA. `pr-hygiene` was already classified as
    project bookkeeping and so not-required; after #4167 it is also a check
    that may never attach to the head at all, and requiring it would hold
    every PR at `absent`/`pending` forever. Pinned so the classification
    cannot be flipped without reading this.
    """

    def test_pr_hygiene_is_not_a_required_check(self):
        text = REQUIRED_CHECKS.read_text(encoding="utf-8")
        required = text.split("[required]", 1)[1].split("[not-required]", 1)[0]
        not_required = text.split("[not-required]", 1)[1]
        names = lambda chunk: [  # noqa: E731
            ln.split("#", 1)[0].strip() for ln in chunk.splitlines() if ln.split("#", 1)[0].strip()
        ]
        assert "pr-hygiene" not in names(required)
        assert "pr-hygiene" in names(not_required)
