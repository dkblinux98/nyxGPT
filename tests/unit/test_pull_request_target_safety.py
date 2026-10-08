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

THE CLASS, NOT THE INSTANCE. The secret-withholding fault is not specific to
`pr-hygiene`: it afflicts every workflow in this repository that runs on
`pull_request` and reads a repository secret. `pr_project_status_on_close.yml`
was the second one, and fixing `pr-hygiene` is what made it consequential --
a Dependabot PR now gets a board card, and Dependabot closing its own PR
(superseded version, dropped dependency) is a Dependabot-actored `closed`
event, so the only job that would stamp that card `Closed` would have died at
`require_gh_auth`, stranding the card in `In Review`. It moved in the same
change. `TestTheDependabotSecretlessClassIsEnumerated` below holds the rest of
the class enumerated with a stated reason each, in both directions, so the
class cannot grow silently the way it did between #3742 and #4167.
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
CLOSE_STAMP = WORKFLOWS / "pr_project_status_on_close.yml"
REQUIRED_CHECKS = ROOT / ".github" / "required-checks.txt"

#: Workflows that still run on `pull_request` while reading a repository
#: secret, i.e. that still carry the #4167 fault -- a Dependabot-actored run
#: gets the empty Dependabot secret store instead and the step dies. Each is
#: here because its failure mode was examined and found not to strand agent
#: state; the reason is the entry's value, and the point of the mapping is
#: that an UNLISTED one fails this file.
#:
#: Both of the workflows that DID strand state are absent on purpose:
#: `ensure_project_hygiene.yml` (the card never got created) and
#: `pr_project_status_on_close.yml` (the card never got stamped `Closed`) are
#: on `pull_request_target` as of #4167.
DEPENDABOT_SECRETLESS_TOLERATED = {
    "claude-code-review.yml": (
        "Gated out before it can fail: every `pull_request` path in `head-gate`'s "
        "`if:` requires `vars.REVIEW_AGENT` to be a requested reviewer or an "
        "assignee, and nothing puts the review agent on a Dependabot PR -- "
        "`pr-hygiene`'s issue-less path writes Status/Priority/Effort and no "
        "assignee. The job does not start, so the blank `REVIEW_AGENT_TOKEN` is "
        "never read."
    ),
    "notify-merge-conflicts.yml": (
        "Pre-existing and not armed by #4167: a conflicted Dependabot PR's "
        "`resolve` job cannot authenticate, so that one notification is lost. It "
        "strands nothing -- the lane is not written by this workflow -- and the "
        "`push`-to-release-branch entry point re-sweeps every open PR with "
        "secrets present, which is the trigger that creates base-moved conflicts "
        "in the first place. Moving it is a behavior decision (it would dispatch "
        "a developer-agent conflict round at a Dependabot PR), not a token fix, "
        "so it is reported rather than changed here."
    ),
    "support-intake-smoke.yml": (
        "Narrow and advisory: path-filtered to the support-intake files, which a "
        "Dependabot PR reaches only via a github-actions security update "
        "touching `support_intake_guard.yml` or this workflow. `label-exists` is "
        "a read-only `gh label list`; a blank token makes the check red and "
        "writes nothing. Classified `[not-required]`, so it cannot hold a head "
        "out of review either."
    ),
}

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


class TestTheCloseStampReachesARunThatHoldsTheSecret:
    """The other half of the lane, and the half #4167 itself armed.

    `pr-hygiene` on `pull_request_target` puts every Dependabot PR on the
    board in `In Review`. Dependabot closes its own PRs without merging when a
    newer version supersedes them, and that `closed` event is
    Dependabot-actored -- so if `pr_project_status_on_close.yml` stayed on
    `pull_request` it would die at `require_gh_auth` with the card already
    created, stranding it in `In Review` with nothing else to stamp it (the
    only other `close_pr_project_item` callers are the review agent's merge
    path and owner-actored merges). Fixing one trigger without the other
    converts a red check into board debris.
    """

    def test_the_close_stamp_runs_on_pull_request_target(self):
        triggers = _triggers(_load(CLOSE_STAMP))
        assert "pull_request_target" in triggers, (
            "pr_project_status_on_close.yml is back on a trigger GitHub strips "
            "secrets from for Dependabot PRs; a Dependabot supersede-close now "
            "strands its board card in In Review (#4167, #3742)."
        )
        assert "pull_request" not in triggers

    def test_it_still_fires_on_closed_only(self):
        types = _triggers(_load(CLOSE_STAMP))["pull_request_target"]["types"]
        assert sorted(types) == ["closed"]

    def test_the_job_still_calls_the_stamping_script(self):
        """The trigger move must not have disturbed what the job does."""
        job = _load(CLOSE_STAMP)["jobs"]["stamp-closed-lane"]
        bodies = "\n".join(str(s.get("run") or "") for s in job["steps"])
        assert "scripts/agents/pr_close_project_status.sh" in bodies


class TestTheDependabotSecretlessClassIsEnumerated:
    """#4167's fault is a class, and an unexamined member of it is the defect.

    The issue said "only `pr-hygiene` is affected". That was an observation of
    two `opened` events, not a search: `pr_project_status_on_close.yml` had the
    identical shape and was missed. So the class is enumerated here instead of
    rediscovered, and the enumeration is checked in BOTH directions -- a new
    `pull_request`+secrets workflow fails until someone states why its blank
    token is tolerable, and an entry that no longer applies fails too, so the
    list cannot rot into a permanent excuse.
    """

    @staticmethod
    def _pull_request_workflows_reading_a_secret() -> set[str]:
        found = set()
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if "pull_request" not in _triggers(_load(path)):
                continue
            if "secrets." in _uncommented(path):
                found.add(path.name)
        return found

    def test_the_enumeration_matches_the_tree(self):
        found = self._pull_request_workflows_reading_a_secret()
        listed = set(DEPENDABOT_SECRETLESS_TOLERATED)
        assert found == listed, (
            "GitHub withholds Actions secrets from a Dependabot-actored "
            "`pull_request` run, so each of these dies the way pr-hygiene did "
            f"(#4167). Unexamined: {sorted(found - listed)}. Listed but no longer "
            f"in that state (drop the entry): {sorted(listed - found)}."
        )

    def test_every_tolerated_entry_states_why(self):
        for name, reason in DEPENDABOT_SECRETLESS_TOLERATED.items():
            assert len(reason) > 80, f"{name}: a bare entry is not a reason"

    def test_the_two_lane_writing_workflows_are_not_in_the_class(self):
        """The discriminator is whether a blank token strands agent state."""
        found = self._pull_request_workflows_reading_a_secret()
        for name in ("ensure_project_hygiene.yml", "pr_project_status_on_close.yml"):
            assert name not in found, (
                f"{name} writes the project lane; on `pull_request` its token is "
                "blank for Dependabot and the write never happens (#4167)."
            )


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


#: Directories with nothing of ours in them (dependencies, build output, VCS).
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    "dist",
    "build",
    ".next",
    "coverage",
    "htmlcov",
}

#: The prose form of a trigger this repository no longer has. `pr-hygiene` and
#: `stamp-closed-lane` both moved to `pull_request_target`, so any sentence
#: naming a "`pull_request: closed` handler" now describes a handler that does
#: not exist. Written as a regex over the single-line form only: the two-line
#: YAML shape (`pull_request:` then `types: [closed]`) is a real trigger a
#: future workflow may legitimately use, and is not what this catches.
_RETIRED_TRIGGER_PROSE = re.compile(r"pull_request: ?closed")

_SCANNED_SUFFIXES = (".md", ".yml", ".yaml", ".sh", ".py", ".ts", ".tsx")

#: The two files that have to spell the retired form out in order to test for
#: it: this one (the pattern's positive cases) and the discriminates script
#: (which injects it on purpose, case 9). Nothing else is exempt -- a guard
#: that fails on its own fixtures is a guard the next person deletes, but an
#: exemption list that grows is the guard being switched off one file at a
#: time, so `test_only_the_guards_own_fixtures_are_exempt` pins it at these.
_FIXTURE_EXEMPT = {
    "tests/unit/test_pull_request_target_safety.py",
    "tests/test_pr_head_guard_discriminates.sh",
}


def _scannable_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if _SKIP_DIRS & set(rel.parts):
            continue
        if rel.as_posix() in _FIXTURE_EXEMPT:
            continue
        out.append(path)
    return sorted(out)


class TestNothingStillDescribesTheRetiredTrigger:
    """#1a: a claim this change makes false has to be fixed, not left to rot.

    Moving the close-stamp to `pull_request_target` falsified five sentences
    scattered across docs, scripts and a workflow header -- `scripts/agents/
    README.md`, `docs/github-tokens.md`, `scripts/agents/lib/gh_project.sh`,
    `scripts/agents/reconcile_pr_lane.sh` and `.github/workflows/
    sweep_pr_status.yml` each identified the handler by a trigger it no longer
    has. They were found by grep after review, which is the expensive way; the
    cheap way is to fail the build. This guard is the pin the review noted was
    missing, and it is the reason a later revert cannot quietly restore the
    stale description along with the stale trigger.
    """

    def test_no_file_names_a_pull_request_closed_handler(self):
        offenders = []
        for path in _scannable_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _RETIRED_TRIGGER_PROSE.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
        assert not offenders, (
            "the close-stamp and `pr-hygiene` run on `pull_request_target`; "
            "these lines still describe a `pull_request: closed` handler, which "
            "no longer exists (#4167): " + ", ".join(offenders)
        )

    def test_the_scan_actually_reaches_the_files_that_were_wrong(self):
        """Otherwise the assertion above could be passing over an empty list
        or a filtered-out directory -- the five files it is about must be in
        the scanned set by name."""
        scanned = {str(p.relative_to(ROOT)) for p in _scannable_files()}
        for name in (
            "scripts/agents/README.md",
            "docs/github-tokens.md",
            "scripts/agents/lib/gh_project.sh",
            "scripts/agents/reconcile_pr_lane.sh",
            ".github/workflows/sweep_pr_status.yml",
        ):
            assert name in scanned, name

    def test_only_the_guards_own_fixtures_are_exempt(self):
        """An exemption list is how a tree-wide scan stops reaching anything.
        Both entries must still exist (a rename would silently un-exempt or,
        worse, leave a dead entry covering a file someone later adds)."""
        assert len(_FIXTURE_EXEMPT) == 2
        for rel in _FIXTURE_EXEMPT:
            assert (ROOT / rel).is_file(), rel

    def test_the_pattern_does_not_condemn_the_legitimate_yaml_shape(self):
        """A future workflow genuinely triggering on closed PRs writes it
        across two lines. Condemning that would make the guard a nuisance
        someone deletes rather than a rule someone keeps."""
        assert not _RETIRED_TRIGGER_PROSE.search("  pull_request:\n    types: [closed]")
        assert not _RETIRED_TRIGGER_PROSE.search("pull_request_target: closed")
        assert _RETIRED_TRIGGER_PROSE.search("the `pull_request: closed` handler")
        assert _RETIRED_TRIGGER_PROSE.search("the pull_request:closed handler")


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
