"""The macOS smoke gate must execute the user path, not inspect an install (#3860).

Why this file exists, rather than a ledger entry saying the same thing.
`macos-brew-smoke.yml` had two install jobs and neither invoked the product:
`keg-install` asserted the wrapper file existed and ran `brew test`,
`published-tap` asserted the keg version and the wrapper file, and `brew test`
itself asserted only that the keg's venv existed and `import nyxgpt.app`
resolved. All three are true of a keg with no reachable CLI, no started
service, no served request and no supported teardown -- which is exactly the
keg that shipped. Six defects on the certified path (#3850, #3851, #3853,
#3854, #3857, #3859) reached owner acceptance over green runs of that gate, and
the Phase 6 capstone (#3516), whose acceptance criterion *is* that end-to-end
scenario, closed on the same component-shaped evidence.

The macOS half of that coverage cannot be exercised from this test suite -- it
needs a real `macos-15` runner with Homebrew. What *can* be enforced here is
that the coverage does not quietly disappear again: the script exists, it
asserts every probe the issue's acceptance criteria name, the workflow runs it,
the tolerated-failure allowlist stays narrow and stays mirrored in the document
that justifies it, and the `test do` blocks run the product instead of stating
that a file is present. Every assertion below is a specific way the gate was
hollow before, so a regression reads as the original defect returning rather
than as a cosmetic test failure.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "macos-user-path-smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"
LIVE_VERIFICATION_DOC = REPO_ROOT / "docs" / "live-verification-ci.md"
REVIEW_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "review-runbook.md"

API_FORMULAS = (
    REPO_ROOT / "homebrew" / "nyxgpt-api.rb",
    REPO_ROOT / "homebrew" / "tap" / "nyxgpt-api.rb.tmpl",
)
WEB_FORMULAS = (
    REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
    REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
)


def _script() -> str:
    return SMOKE_SCRIPT.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job_run_text(job: dict) -> str:
    """Every `run:` body in a job, concatenated -- what the runner will execute."""
    return "\n".join(step.get("run", "") for step in job["steps"])


def _tolerated_steps() -> list[str]:
    """The allowlist the script ships, parsed from its assignment line."""
    for line in _script().splitlines():
        if line.startswith("TOLERATED_STEPS='"):
            return [s.strip() for s in line.split("'")[1].split(",") if s.strip()]
    raise AssertionError("the tolerated-failure allowlist is gone from the script")


def test_the_user_path_script_is_executable() -> None:
    """The workflow invokes it directly, not through `bash <path>`."""
    assert SMOKE_SCRIPT.exists(), f"{SMOKE_SCRIPT} is missing"
    assert os.access(SMOKE_SCRIPT, os.X_OK), (
        f"{SMOKE_SCRIPT} is not executable, so the workflow's "
        "`./scripts/macos-user-path-smoke.sh` invocation would fail on the runner"
    )


# Each probe, and the defect that reached owner acceptance because the gate
# never issued it.
PROBES = (
    ("nyxgpt --version", "#3850: the CLI was never invoked by name"),
    ("nyxgpt up ", "#3516: the scenario's single command was never run"),
    ("http://127.0.0.1:8000", "#3853: the API was never asked anything"),
    ("/health", "#3853: the health probe was never issued"),
    ("/api/v1/sessions", "#3851: a stack that starts but cannot reach its datastore"),
    ("http://127.0.0.1:3000", "#3857: the web UI was never requested"),
    ("nyxgpt ops status", "#3854: brew's own caveats point elsewhere"),
    ("nyxgpt down", "#3859: the supported stop was never exercised"),
    ("nyxgpt ops uninstall", "#3859: the wrapped teardown was never exercised"),
    ("brew uninstall", "#3859: removal was never exercised"),
    ("brew untap", "#3859: the tap was never removed"),
    ("launchctl list", "#3859: leftover launchd jobs were never checked"),
    ("Library/LaunchAgents", "#3859: leftover plists were never checked"),
)


@pytest.mark.parametrize(("probe", "why"), PROBES)
def test_the_script_exercises_every_step_of_the_user_path(probe: str, why: str) -> None:
    assert probe in _script(), (
        f"scripts/macos-user-path-smoke.sh no longer exercises {probe} ({why}). "
        "Each of these is a step the previous gate skipped; dropping one puts "
        "that defect class back outside CI."
    )


def test_the_script_tolerates_only_the_container_steps_this_runner_cannot_run() -> None:
    """A smoke test that shrugs at failures is the thing being replaced.

    Hosted macOS images ship no Docker (and the Apple Silicon runners expose no
    nested virtualisation, so Colima cannot substitute), so the Cassandra
    container and the observability Compose profiles genuinely cannot start
    there -- that boundary is named in `docs/live-verification-ci.md`. The
    tolerance has to stop there: the api/web/ollama services, the config, the
    install-mode record and the env sync are the user path itself, and a
    failure in any of them is a defect, not a runner limit.
    """
    tolerated = _tolerated_steps()
    never_tolerated = (
        "native api service",
        "native web service",
        "ollama service",
        "config",
        "install mode",
        "env sync",
    )
    for step in never_tolerated:
        assert step not in tolerated, (
            f"{step!r} was added to the tolerated-failure allowlist -- a failure "
            "in that step is a defect on the user path, not a runner limit"
        )


def _required_steps() -> list[str]:
    """The steps the script requires to have been *seen* running."""
    for line in _script().splitlines():
        if line.startswith("REQUIRED_STEPS='"):
            return [s.strip() for s in line.split("'")[1].split(",") if s.strip()]
    raise AssertionError(
        "REQUIRED_STEPS is gone from the script -- without it, 'no untolerated "
        "failure' passes vacuously on a `nyxgpt up` that never ran (#3860)"
    )


def test_the_up_assertion_has_a_positive_half() -> None:
    """Absence of failure is not evidence of success.

    Run 32203021217 proved it: with no `nyxgpt` on PATH (#3850) the command
    exited 127 having printed nothing, the parser found no `[FAIL]` line to
    attribute, and the script reported "every nyxgpt up step ... succeeded"
    over a command that did not run. That is the hollow-gate shape this whole
    issue is about, reproduced inside the fix for it.
    """
    required = _required_steps()
    tolerated = set(_tolerated_steps())
    assert required, "the required-step list is empty, so nothing must be observed to run"
    assert not (set(required) & tolerated), (
        f"{sorted(set(required) & tolerated)} is both required to succeed and tolerated "
        "when it fails; the two sets are complements"
    )
    script = _script()
    assert 'if [ "$UP_RC" = "127" ]' in script, (
        "a `nyxgpt up` that does not exist (exit 127) no longer fails the script "
        "on its own (#3850)"
    )
    assert "UNSEEN" in script, "the parser no longer reports required steps it never saw"


@pytest.mark.parametrize("step", _required_steps(), ids=lambda s: s.replace(" ", "-"))
def test_every_required_step_is_a_step_ops_install_actually_streams(step: str) -> None:
    """A typo here reads as "the install never reached that step" forever.

    The names are matched against `ops install`'s banner text, so they have to
    be the literal step names in `ops.py`'s step list -- not a paraphrase.
    """
    source = (REPO_ROOT / "src" / "nyxgpt" / "ops.py").read_text(encoding="utf-8")
    assert f'("{step}"' in source, (
        f"{step!r} is required to appear in `nyxgpt up`'s output but is not a step "
        "name in src/nyxgpt/ops.py's install step list; the assertion would fail "
        "forever rather than measure anything"
    )


def test_every_tolerated_step_is_justified_in_the_document_that_bounds_it() -> None:
    """The allowlist and its rationale drift apart the moment they are separate.

    `docs/live-verification-ci.md` is the owner-facing list of what CI cannot
    produce, and the review contract (`review-runbook.md` §1c) lets a reviewer
    accept a gap only when it is named there. An allowlist entry with no entry
    in that document is an untested path nobody agreed to leave untested.
    """
    doc = LIVE_VERIFICATION_DOC.read_text(encoding="utf-8")
    for step in _tolerated_steps():
        assert step in doc, (
            f"the script tolerates a failing {step!r} step but "
            "docs/live-verification-ci.md does not say why CI cannot run it"
        )


def test_the_script_asserts_its_own_precondition_instead_of_degrading() -> None:
    """`brew services` has to work here, or the run measures the runner.

    The alternative -- quietly skipping the service checks when the launchd
    domain is unavailable -- reproduces the exact failure this file exists to
    stop: a job that reports green while asserting nothing about the product.
    """
    script = _script()
    assert "brew services list" in script
    assert "docs/live-verification-ci.md" in script, (
        "the precondition failure must point at the documented what-CI-cannot-cover "
        "list rather than inviting the next session to weaken the script"
    )


def _script_ere(fragment: str) -> str:
    """The first extended regex the script passes to `grep -E`/`-iE` containing `fragment`.

    Extracted rather than restated: a copy of the pattern in this file would
    pass while the script shipped a different one, which is the exact failure
    mode these tests exist to remove.
    """
    for match in re.finditer(r'grep -q?[a-zA-Z]*E\s+"([^"]+)"', _script()):
        if fragment in match.group(1):
            return match.group(1)
    raise AssertionError(
        f"no grep -E pattern in the script mentions {fragment!r}; the assertion it "
        "belonged to was removed or rewritten"
    )


def _ops_status_line(prefix: str, component: str, state: str) -> str:
    """A `nyxgpt ops status` line, rendered from `ops.py`'s own format literal.

    The point is that this test cannot go stale in the safe direction: the
    sample is the product's format string, so a change to how status prints
    fails here instead of silently making the script's grep unmatchable.
    """
    source = (REPO_ROOT / "src" / "nyxgpt" / "ops.py").read_text(encoding="utf-8")
    match = re.search(rf'print\(f"(  {prefix}\s*\{{component\}}: [^"]*)"\)', source)
    assert match, (
        f"src/nyxgpt/ops.py no longer prints a {prefix!r} deployment-mode line in the "
        "shape scripts/macos-user-path-smoke.sh greps for"
    )
    return (
        match.group(1)
        .replace("{component}", component)
        .replace("{state}", state)
        .replace("{suffix}", "")
    )


def _grep_matches(pattern: str, text: str) -> bool:
    """`grep -iE`, run for real -- POSIX classes are not Python regex syntax."""
    return (
        subprocess.run(
            ["grep", "-qiE", pattern],
            input=text,
            text=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(("prefix", "state"), [("native", "started"), ("compose", "running")])
def test_the_status_assertion_matches_what_ops_status_actually_prints(
    prefix: str, state: str
) -> None:
    """A pattern that cannot match is a permanently red assertion, not a gate.

    The first cut anchored on `^[[:space:]]*api\\b`, but no status line begins
    with a bare component name -- `ops.py` prints `  native  api: started` and
    `  compose web: running`. That assertion would have failed forever,
    including after #3850-#3859 land, reporting a phantom #3854 and
    contradicting this issue's own criterion that each assertion passes once
    the fixes land. Inspection missed it once; this executes the pattern.
    """
    pattern = _script_ere("native|compose|terraform")
    for component in ("api", "web"):
        line = _ops_status_line(prefix, component, state)
        assert _grep_matches(pattern.replace("${component}", component), line), (
            f"the script's status pattern does not match {line!r}, which is what "
            "nyxgpt ops status prints -- the assertion can never pass"
        )


def test_the_status_assertion_rejects_a_component_that_is_not_running() -> None:
    """Naming the component is not the claim; `native  api: none` names it too.

    #3854 is about what the operator is told is running after they ran
    `nyxgpt up`, so a `none` state has to fail the gate rather than satisfy it.
    """
    pattern = _script_ere("started|running")
    assert _grep_matches(pattern, _ops_status_line("native", "api", "started"))
    assert not _grep_matches(pattern, _ops_status_line("native", "api", "none")), (
        "the script accepts a status line reporting the api service as absent, so a "
        "machine where nyxgpt up started nothing would still pass this step (#3854)"
    )


def test_the_self_heal_assertion_matches_what_self_heal_status_prints() -> None:
    """Same contract, same failure mode, for the #3853 probe."""
    source = (REPO_ROOT / "src" / "nyxgpt" / "cli.py").read_text(encoding="utf-8")
    match = re.search(r"print\(f\"( \[\{marker\}\] \{c\['service'\]\}: [^\"]*)\"\)", source)
    assert match, (
        "src/nyxgpt/cli.py no longer prints the ` [<marker>] <service>: ` line the "
        "user-path script greps for; the #3853 probe cannot match"
    )
    line = (
        match.group(1)
        .replace("{marker}", "OK")
        .replace("{c['service']}", "api")
        .replace("{c['state']}", "running")
        .replace("{health}", "healthy")
        .replace("{suffix}", "")
    )
    pattern = _script_ere(r"\[OK\]").replace("${component}", "api")
    assert _grep_matches(pattern, line), (
        f"the script's self-heal pattern does not match {line!r}, which is what "
        "`nyxgpt self-heal status` prints"
    )


def test_the_script_asserts_the_health_wait_never_names_a_started_component() -> None:
    """The other end of #3853, and the line the owner actually saw.

    `nyxgpt up` printed `Still unhealthy: web` while the web service was
    `started` the whole time, because the probe looked up `nyxgpt-web` and the
    candidate install had registered `nyxgpt-web@3.0.0rc`. The script asserts
    api/web never appear in that line.

    Deliberately conditional on the line being present, and deliberately not
    an assertion on `up`'s exit code: this runner has no Docker daemon, so the
    `docker engine` step fails, `install()` returns 2 and `up` returns that
    before the health wait ever runs. Asserting `up` returns 0 here would
    assert something no hosted macOS runner can produce -- the honest gate is
    the probe assertion above (non-vacuous on every run) plus this one, which
    is non-vacuous exactly when the wait did run.
    """
    script = _script()
    assert 'grep -q "Still unhealthy" "$WORK/up.log"' in script, (
        "the script no longer reads `nyxgpt up`'s own health-wait verdict, so the "
        "line the owner saw in #3853 is asserted nowhere"
    )
    pattern = _script_ere(r"(^|[ ,:])${component}([,.]|$)")
    for component in ("api", "web"):
        line = (
            "WARNING: not every component reported healthy within the timeout -- "
            f"Still unhealthy: {component}. run `nyxgpt ops status` for details."
        )
        assert _grep_matches(pattern.replace("${component}", component), line), (
            f"the script's pattern does not match {line!r}, which is what "
            "`nyxgpt up` prints on a timeout -- the assertion can never fire"
        )
    # Must not match a component that is merely a substring of another word,
    # or the assertion fires on healthy runs and gets deleted as noise.
    assert not _grep_matches(
        pattern.replace("${component}", "api"),
        "WARNING: not every component reported healthy -- Still unhealthy: cassandra.",
    ), "the pattern matches a line naming a different component"


def test_the_up_health_wait_line_is_the_one_ops_actually_prints() -> None:
    """Pinned against `ops.py`, not against a remembered string."""
    source = (REPO_ROOT / "src" / "nyxgpt" / "ops.py").read_text(encoding="utf-8")
    assert "f\" Still unhealthy: {', '.join(pending)}.\"" in source, (
        "`nyxgpt up` no longer prints ` Still unhealthy: <components>.` on a "
        "health-wait timeout, so the user-path script greps for a line that "
        "cannot appear (#3853)"
    )
    assert (
        'grep -q "Still unhealthy"' in _script()
    ), "the script and ops.py have drifted about the health-wait timeout line"


def test_the_conflict_job_captures_install_exit_codes_without_tripping_errexit() -> None:
    """A refusal is the designed pass case, and it was killing the step.

    GitHub runs a `run:` body with no `shell:` key as `/bin/bash -e {0}`, so
    errexit is on before the script's own `set -uo pipefail` and is not cleared
    by it. `brew install <candidate> > candidate.log 2>&1` therefore aborted the
    step the moment Homebrew honoured `conflicts_with` -- the job was red
    exactly when the behavior it tests worked, and none of its state assertions
    ran (run 32200501142).
    """
    runs = _job_run_text(_workflow()["jobs"]["stable-over-candidate"])
    for log in ("candidate.log", "stable-on-top.log"):
        assert re.search(rf"> {re.escape(log)} 2>&1 \|\| rc=\$\?", runs), (
            f"the install writing {log} no longer captures its exit code in a `||` "
            "list, so the inherited errexit aborts the step on a legitimate "
            "conflicts_with refusal before a single assertion runs"
        )


def test_the_published_tap_job_runs_the_user_path() -> None:
    job = _workflow()["jobs"]["published-tap"]
    runs = _job_run_text(job)
    assert "scripts/macos-user-path-smoke.sh" in runs, (
        "the published-tap job no longer drives the user-path script, so the "
        "owner's literal command path is inspected again rather than executed"
    )
    assert any(
        "actions/checkout" in (step.get("uses") or "") for step in job["steps"]
    ), "published-tap runs a script out of the repository, so it needs a checkout"

    # The keg-verification step's checks are a subset of the script's, so a
    # step-ordering gate there means the first open defect hides the state of
    # everything after it. Run 32202247518 stopped at `command -v nyxgpt`
    # (#3850, against the published rc12) and never reached the user path.
    step = next(s for s in job["steps"] if s.get("name") == "Run the user path end to end")
    condition = str(step.get("if", ""))
    assert "cancelled()" in condition and "steps.install.conclusion" in condition, (
        "the user-path step runs only when every earlier step passed, so the "
        "gate reports the first defect on the path instead of the whole path -- "
        "the same 'one broken step hides the rest' shape the script itself avoids"
    )


def test_the_teardown_ordering_is_asserted_not_assumed() -> None:
    """`ops uninstall` must be checked against what `down` deliberately leaves (#3859).

    The whole defect is the difference between the two commands: `down` stops
    a stack and leaves it installed, so a teardown check that runs only after
    `ops uninstall` cannot tell "the command works" from "there was nothing
    there". The script asserts the machine is still registered between them,
    so a green run means the residue check had residue to clear.
    """
    script = _script()
    down_at = script.index("nyxgpt down")
    uninstall_at = script.index("nyxgpt ops uninstall")
    removal_at = script.index("brew uninstall")
    assert down_at < uninstall_at < removal_at, (
        "the wrapped teardown must run after `nyxgpt down` and before the "
        "Homebrew removal -- that ordering is what the formula caveats tell "
        "the operator to follow, so it is the ordering the gate has to execute"
    )
    between = script[down_at:uninstall_at]
    assert "the uninstall check below tests nothing" in between, (
        "nothing asserts the machine is still registered after `nyxgpt down`, "
        "so the residue check after the teardown could pass against a machine "
        "that had nothing to remove"
    )
    assert "idempotent" in script or "second nyxgpt ops uninstall" in script, (
        "the teardown routinely runs against partially-removed machines; the "
        "second run is what proves absence is not treated as failure"
    )


def test_the_keg_install_job_proves_the_teardown_on_every_pr() -> None:
    """The published-tap job needs a publish; this defect does not (#3859).

    `keg-install` builds from the working tree and runs on pull requests, so
    the teardown assertion lands on the PR that changes it rather than after
    the next rc cut. It injects the three `com.nyxgpt.*` agents (this job
    never runs `nyxgpt ops install`, so they would not otherwise exist and the
    check would be green on a machine that never reproduced the bug) and
    proves both halves: the plists survive `nyxgpt ops down`, and they do not
    survive `nyxgpt ops uninstall`.
    """
    job = _workflow()["jobs"]["keg-install"]
    step = next(
        (s for s in job["steps"] if str(s.get("name", "")).startswith("Teardown")),
        None,
    )
    assert step is not None, (
        "keg-install no longer proves the teardown, so #3859's fix is only "
        "exercised by a job that needs a published candidate"
    )
    run = step["run"]
    for label in ("com.nyxgpt.cassandra-logs", "com.nyxgpt.ollama-logs", "com.nyxgpt.ollama-env"):
        assert label in run, f"the injection no longer stages {label}"
    assert "injection failed" in run, (
        "nothing asserts the injected agents were really staged, so the "
        "teardown assertion can pass against a machine with nothing on it"
    )
    assert "nyxgpt ops down" in run and "tests nothing" in run, (
        "the negative control is gone: without it, a check that passes says "
        "nothing about whether the command before this fix would have failed"
    )
    assert "nyxgpt ops uninstall" in run


def test_the_stable_over_candidate_job_covers_the_present_counterpart_case() -> None:
    """`keg-install`'s tap deliberately carries no stable formula (#3753).

    That keeps the absent-counterpart case covered and leaves the case that
    decides whether `conflicts_with` is load-bearing -- stable installed,
    candidate attempted on top -- covered nowhere. It is the case the owner hit
    (#3853, ledger Q-002).
    """
    jobs = _workflow()["jobs"]
    assert "stable-over-candidate" in jobs, (
        "the stable-installed-then-candidate job is gone; the only conflicts_with "
        "shape under test would be the one where the named formula is absent"
    )
    runs = _job_run_text(jobs["stable-over-candidate"])
    assert "build_homebrew_artifacts.py 3.0.0 " in runs and "--channel rc" in runs, (
        "the job has to stamp both channels from this checkout, or the tap it "
        "installs from has no stable counterpart to conflict with"
    )
    assert "brew uninstall nyxgpt-api" in runs, (
        "the control that installs the same candidate once the stable is gone is "
        "missing, so a candidate that simply fails to build would pass this job"
    )
    assert "brew tap-trust nyxgpt/both-channels" in runs, (
        "the throwaway tap is not trusted, so brew refuses to load the stable "
        "formula when it resolves the candidate's conflicts_with (#3770's shape, "
        "reproduced in run 32202247518) -- the job then measures the tap-trust "
        "gate rather than conflicts_with"
    )


REVERSE_DIRECTION_STEP = "The other direction -- the stable onto an installed candidate"


def _conflict_step(name: str) -> dict:
    """One named step of `stable-over-candidate`, by its exact `name:`."""
    steps = _workflow()["jobs"]["stable-over-candidate"]["steps"]
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"`stable-over-candidate` no longer has a step named {name!r}; "
        f"it has {[s.get('name') for s in steps]}"
    )


def test_the_conflict_job_proves_the_refusal_was_the_declared_conflict() -> None:
    """ "The candidate is not installed" is not by itself `conflicts_with` holding.

    An untrusted tap refuses the install too -- "Refusing to load formula ...
    from untrusted tap", which is what this job actually hit in run 32202247518
    (#3770's shape). Both refusals leave the same observable state, so without
    reading the reason a tap-trust regression reads as the conflict working and
    the job silently stops measuring what it exists to measure.
    """
    run = _conflict_step("Install the stable, then the candidate on top of it")["run"]
    assert "conflicting formulae are installed" in run, (
        "the job no longer checks WHY brew refused the candidate, so any refusal "
        "-- including an untrusted-tap refusal -- passes as conflicts_with holding"
    )
    assert "candidate.log" in run, (
        "the refusal reason has to be read out of the captured install log; "
        "brew's message is not on stdout when the install is redirected"
    )


def test_the_conflict_jobs_own_direction_stays_a_hard_assertion() -> None:
    """AC4's direction is proven to work, so a regression there is a real defect.

    Run 32204454740: candidate-onto-stable is refused, `stable installed: 1 /
    candidate installed: 0`. Nothing about #3853 excuses that direction
    breaking, so it keeps `::error::` + `exit 1`.
    """
    run = _conflict_step("Install the stable, then the candidate on top of it")["run"]
    assert "::error::stable and candidate are installed simultaneously" in run, (
        "the candidate-onto-stable direction no longer fails when both channels "
        "end up installed -- that direction demonstrably works today, so a green "
        "run over the both-installed state there would be a hollow gate"
    )
    assert re.search(r"::error::stable and candidate[^\n]*\n\s*exit 1", run), (
        "the candidate-onto-stable both-installed outcome logs an error but no "
        "longer exits non-zero, so the check would report green"
    )


def test_the_reverse_direction_is_a_hard_assertion_again() -> None:
    """The D-026 debt, paid (#3853).

    Between PR #3925 and #3853's fix this direction *recorded* the
    both-installed outcome as a `::warning::` rather than failing on it: it
    reproduced an open defect whose fix direction was parked by ledger Q-002,
    and a gate that hard-fails on a defect the project has decided not to fix
    yet is a permanent red that trains readers to ignore it.

    Q-002 is answered and the packaging half has landed -- the stable formula
    declares `conflicts_with` its own line's candidate
    (`build_homebrew_artifacts.render_stable_formula`) -- so both install
    orders are the same assertion again. That symmetry is the property: a
    one-sided `conflicts_with` guarded one order and nothing at all in the
    other, which is how the owner's Mac ended up with two complete stacks.

    This test replaces `test_the_reverse_direction_records_3853_instead_of_failing_on_it`,
    which pinned the warning text and had to be updated with the flip.
    """
    step = _conflict_step(REVERSE_DIRECTION_STEP)
    run = step["run"]

    assert "::warning::" not in run, (
        "the reverse direction still records the both-installed state as a "
        "warning. #3853 is fixed: the stable formula declares conflicts_with "
        "its line's candidate, so brew must refuse this outright and the step "
        "must fail if it does not"
    )

    both_installed = re.search(
        r'if \[ "\$stable_installed" = "1" \] && \[ "\$candidate_installed" = "1" \]; then'
        r"(.*?)\n *fi\b",
        run,
        re.S,
    )
    assert both_installed is not None, "the both-installed branch is gone from the step"
    branch = both_installed.group(1)
    assert "::error::" in branch and "exit 1" in branch, (
        "the reverse direction no longer fails when both channels end up "
        "installed. That is the exact state #3853 was reported from -- two "
        "kegs for one component, each registering a keep_alive service on the "
        "same port -- and it is now guarded by packaging, so a green run over "
        "it would be a hollow gate"
    )
    assert "#3853" in branch, (
        "the both-installed failure no longer names #3853, so the log stops "
        "saying which defect a regression here has reintroduced"
    )
    # A refusal has to be attributable, exactly as in the other direction: an
    # untrusted tap refuses too (#3770), and "the stable is not installed"
    # cannot tell the two apart on its own.
    assert "conflicting formulae are installed" in run, (
        "the step no longer checks *why* brew refused, so an untrusted-tap "
        "refusal would read as conflicts_with working"
    )

    for hard in (
        "no nyxgpt on PATH after the stable install attempt",
        "nyxgpt is on PATH but does not run after the stable install attempt",
    ):
        assert re.search(rf"::error::{re.escape(hard)}[^\n]*exit 1", run), (
            f"the machine-left-usable assertion {hard!r} is no longer hard; #3853 "
            "does not excuse a broken CLI, so these stay failures"
        )


RETIRED_COUNTERPART_STEP = "Measure: the same conflict after the candidate is retired from the tap"


def test_the_job_measures_the_retired_counterpart_case_instead_of_assuming_it() -> None:
    """Ledger Q-007: what happens once the ceremony deletes the rc formula.

    #3853's packaging fix makes the *stable* formula declare
    `conflicts_with "<name>@<line>rc"`, and the release ceremony deletes that
    formula from the tap once the line ships
    (`scripts/retire_rc_formulas.sh`). Whether the conflict still holds for a
    machine that still has the keg, and what a clean machine is shown, are
    both unestablished -- and #3853 exists precisely because a behaviour
    everyone believed was never run. So the job reproduces the state and
    prints the answer rather than either guessing or ignoring it.

    Recorded, not asserted, per ledger D-026: a gate fails on what is settled
    and records what is open. The machine-left-usable checks stay hard,
    because no open question excuses a broken CLI.
    """
    run = _conflict_step(RETIRED_COUNTERPART_STEP)["run"]

    assert "rm -f" in run and "nyxgpt-api@3.0.0rc.rb" in run, (
        "the step no longer removes the candidate formula from the tap, so it "
        "measures the counterpart-present case the previous step already covers"
    )
    assert "MEASURED:" in run, (
        "the step no longer prints its findings under a greppable marker, so "
        "the answer to Q-007 would be buried in a brew log"
    )
    for hard in (
        "no nyxgpt on PATH after the retired-counterpart install attempt",
        "nyxgpt is on PATH but does not run after the retired-counterpart install attempt",
    ):
        assert re.search(rf"::error::{re.escape(hard)}[^\n]*exit 1", run), (
            f"the machine-left-usable assertion {hard!r} is no longer hard; an "
            "open question does not excuse a broken CLI"
        )
    assert "should be removed from" in run, (
        "the step no longer recognises Homebrew's own advice to delete the "
        "declaration -- that wording is the outcome the ledger entry warns "
        "against following, so it has to be detected by name"
    )


def test_the_open_question_about_retirement_is_recorded_in_the_ledger() -> None:
    """A measurement nobody reads is a measurement that was not taken.

    The step above prints an answer on every formula PR; this pins the entry
    that tells a future session the question exists and where the answer
    appears.
    """
    ledger = (REPO_ROOT / "agents" / "LEDGER.md").read_text(encoding="utf-8")
    assert "**Q-007**" in ledger, "the retired-counterpart question is not in the ledger"
    question = ledger[ledger.index("**Q-007**") :]
    question = question[: question.index("\n\n")]
    # The ledger wraps at ~78 columns, so the step name is split across lines.
    assert RETIRED_COUNTERPART_STEP in " ".join(question.split()), (
        "the Q-007 entry no longer names the step that answers it, so a future "
        "session has the question and no way to close it"
    )


def test_the_reverse_direction_comment_records_what_was_measured() -> None:
    """The comment carries the evidence, not an instruction to the next session.

    Two things it must not do. It must not tell a future session to refuse a
    change that was later ratified -- an earlier revision said "Do not soften
    this assertion to get a green check", which after the huddle ratified
    exactly that would have trapped both the #3853 fixer and the next
    reviewer. And it must not assert today's check colour, which expires the
    moment the behaviour changes and cannot be verified from the file.

    What it must keep is the measurement: run 32202943938 is the reason
    anyone knows `conflicts_with` is directional, and the block is the only
    place in-tree that says so next to the step that proved it.
    """
    # The step still has to exist under that name; `yaml.safe_load` drops
    # comments, so the block itself is read off the raw file -- everything
    # between this step's `- name:` line and its `run:` line.
    _conflict_step(REVERSE_DIRECTION_STEP)
    raw = WORKFLOW.read_text(encoding="utf-8")
    start = raw.index(f"- name: {REVERSE_DIRECTION_STEP}")
    comment = raw[start : raw.index("run: |", start)]

    assert "Do not soften this assertion" not in comment, (
        "the comment tells future sessions to refuse the ratified record-don't-fail "
        "decision; it would trap the #3853 fixer and the next reviewer"
    )
    assert "RED TODAY" not in comment, (
        "the comment asserts today's check color, which expires the moment the "
        "behavior changes and cannot be verified from the file"
    )
    assert "#3853" in comment and "32202943938" in comment, (
        "the comment no longer cites the run that established this behaviour. "
        "That run is why the project knows conflicts_with is directional at "
        "all (ledger Q-002); without the citation the assertion below reads as "
        "an assumption"
    )


IDENTITY_STEP = "The install-identity reconcile leaves exactly one service set (#3861)"


def test_the_conflict_job_also_proves_the_install_identity_reconcile() -> None:
    """#3861's executed evidence rides this job rather than a second one.

    The packaging steps above answer "can two channels be installed at once?".
    They say nothing about what an install then *does* about the one it is
    replacing -- which is the question #3861 is about, and the one whose
    answer was "nothing" while two `keep_alive` service sets fought over
    ports 8000/3000 on the owner's Mac. Since #3853 declared `conflicts_with`
    in both directions the state is no longer reachable by installing, so the
    step stages it (see the staging guard below) -- and it still has to be
    covered, because packaging guards machines that install *after* that
    declaration shipped, not the ones already in the state.
    """
    run = _conflict_step(IDENTITY_STEP)["run"]
    assert "brew services start nyxgpt-api@3.0.0rc" in run and (
        "brew services start nyxgpt-api" in run
    ), (
        "the step no longer registers both service sets, so it reconciles a "
        "machine that was never in the state the reconcile exists for"
    )
    assert "_reconcile_install_mode" in run, (
        "the step no longer runs the real reconcile -- an assertion about the "
        "registered services that nothing reconciled proves nothing"
    )
    for direction in (
        "reconcile_from nyxgpt-api@3.0.0rc nyxgpt-api",
        "reconcile_from nyxgpt-api nyxgpt-api@3.0.0rc",
    ):
        assert direction in run, (
            f"the {direction!r} direction is gone; `conflicts_with` is directional "
            "and so is the leftover it leaves behind, so both are covered here"
        )
    assert "still registered after the reconcile (#3861)" in run and "exit 1" in run, (
        "the step no longer fails when the previous install's service survives "
        "the reconcile, which is the entire defect"
    )


def test_the_identity_step_stages_the_second_keg_by_unlinking_not_by_editing() -> None:
    """The two-keg machine is staged, and the staging must not leak (#3853 merge).

    Before #3853 this step relied on the stable install simply succeeding on
    top of the candidate: `conflicts_with` was declared on one side only, so
    that order was guarded by nothing (ledger Q-002). Both orders are now
    refused, which is the fix -- and it takes away the state this step exists
    to reconcile, so the step has to stage it.

    *How* it stages it is the part a future session would get wrong. The first
    attempt edited the tap's stable formula with `sed '/conflicts_with/d'` and
    broke it outright (run 32227410541: the declaration spans two lines, so
    deleting the first left an orphan `because:` and "syntax errors found").
    The same run printed the right answer in brew's own refusal -- "Please
    `brew unlink nyxgpt-api@3.0.0rc` before continuing" -- because
    `conflicts_with` is checked against the **linked** keg, not the installed
    one. So the staging unlinks and never edits: the tap keeps the formula
    `render_stable_formula` produced, which is what the measurement step after
    this one reads.

    Both halves of the scaffolding also have to be undone, or that step starts
    from a two-keg machine with the wrong `nyxgpt` on PATH.
    """
    run = _conflict_step(IDENTITY_STEP)["run"]
    assert "brew unlink nyxgpt-api@3.0.0rc" in run, (
        "the step no longer stages the second keg. Since #3853 brew refuses "
        "the stable onto a *linked* candidate, so without the unlink this step "
        "reconciles a machine with one keg on it and proves nothing"
    )
    assert "sed -i" not in run, (
        "the staging edits a file in place again. It must not: the only file "
        "worth editing here is the tap's stable formula, the step after this "
        "one measures that formula's own conflicts_with, and the two-line "
        "declaration does not survive a line-wise edit (run 32227410541)"
    )
    for guard, why in (
        (
            "::error::could not stage the stable keg alongside the candidate",
            "a staging install that silently did nothing would leave this step "
            "reconciling a one-keg machine and passing",
        ),
        (
            "::error::the candidate keg is gone",
            "staging that *replaced* the candidate rather than joining it would "
            "also leave one keg, and the retire assertions would then be about a "
            "service no keg owns",
        ),
    ):
        assert guard in run, f"{guard!r} is gone: {why}"
    assert "brew uninstall --ignore-dependencies nyxgpt-api" in run, (
        "the staged keg is no longer removed, so the measurement step after "
        "this one starts from a two-keg machine instead of the "
        "candidate-only one its own comment describes"
    )
    assert "brew link --overwrite nyxgpt-api@3.0.0rc" in run, (
        "the candidate is never relinked, so every step after this one runs "
        "with no `nyxgpt` on PATH -- the unlink is scaffolding and has to be "
        "undone with the keg it was for"
    )


def test_the_identity_step_proves_the_old_gate_could_not_have_caught_it() -> None:
    """Non-vacuity (#3775): a check that never sees the failing state is not a check.

    The pre-#3861 gate was `previous.mode != target`, and both sides of this
    scenario are `artifact` -- so the old code could not have acted, and this
    step is genuine evidence rather than a demonstration of something already
    working. The moment that stops being true the model has regressed to
    something a mode comparison can see, and the step says so instead of
    passing.
    """
    run = _conflict_step(IDENTITY_STEP)["run"]
    assert "previous.mode != target" in run, (
        "the step no longer states which gate it is proving insufficient, so a "
        "green run cannot be read as evidence about the artifact-to-artifact case"
    )
    assert "old_gate_would_have_acted" in run and "sys.exit" in run, (
        "the step no longer FAILS when the two identities differ by mode; it "
        "would then pass on a scenario the pre-#3861 code already handled"
    )
    assert "nothing would be retired" in run, (
        "the step no longer checks that the recorded previous service is not "
        "the target's own -- it would then assert the absence of a service the "
        "reconcile was never asked to stop"
    )


def test_the_identity_step_asserts_the_launchagent_plist_is_gone() -> None:
    """A "stopped" service is not a de-registered one (#3861 review).

    The first run of this step (32222041921) passed `brew services stop` and
    still read both services back as registered: brew exits 0 for a service
    that is registered but not running and reports nothing either way about
    the plist, and a plist left in ~/Library/LaunchAgents is what launchd
    starts again at the next login. A check that reads only `brew services
    list` would also pass the moment that output stopped matching what is on
    disk -- in either direction -- so the file itself is asserted gone.
    """
    run = _conflict_step(IDENTITY_STEP)["run"]
    assert "plist_gone" in run and "homebrew.mxcl" in run, (
        "the step no longer checks the LaunchAgent plist, so a retire that "
        "stops a service and leaves it registered would pass again"
    )
    for formula in ("plist_gone nyxgpt-api", "plist_gone 'nyxgpt-api@3.0.0rc'"):
        assert formula in run, (
            f"{formula!r} is gone: both directions retire a service, so both "
            "have to prove the registration went with it"
        )


def test_the_identity_step_captures_the_post_retire_column_verbatim() -> None:
    """The measurement two red runs never took (#3861 review round 3).

    Runs 32222041921 and 32228088507 both showed a column-based read reporting
    a service that launchd had already forgotten, and neither captured brew's
    rows *after* the retire -- so the tree could name the observable but not
    the mechanism. Two produce it identically: a Status column that outlives
    the registration, or ANSI-coloured state text no literal comparison
    matches. `cat -v` renders the escapes instead of letting the log viewer
    swallow them, so one run's output separates them.

    Advisory, not an assertion: the plist checks are what pass or fail the
    step, and they are correct under either mechanism. This is here so the
    next session can write down which one it was from a measurement rather
    than from a hypothesis (D-005).
    """
    run = _conflict_step(IDENTITY_STEP)["run"]
    capture = "brew services list 2>&1 | cat -v"
    assert capture in run, (
        "the post-retire `brew services list` capture is gone, so the next red "
        "run again cannot say whether the column outlived the registration or "
        "was simply coloured"
    )
    reconcile_at = run.index("reconcile_from nyxgpt-api@3.0.0rc nyxgpt-api")
    assert run.index(capture) > reconcile_at, (
        "the verbatim capture no longer runs after the first reconcile, so it "
        "reads the column before the retire it is supposed to measure"
    )


def test_the_rc_keg_is_stamped_so_the_channel_it_detects_is_its_own() -> None:
    """The candidate keg has to declare a candidate version, or the step lies.

    `ops._native_service_version` reads the installed distribution's metadata
    to decide which channel's formula an artifact install resolves. The rc
    tarball vendors `pyproject.toml` verbatim, so an unstamped checkout would
    put the STABLE version inside the candidate keg -- and the identity step
    would detect "stable" from inside the candidate's own venv, quietly
    measuring the same channel twice.
    """
    run = _job_run_text(_workflow()["jobs"]["stable-over-candidate"])
    assert 'version = "3.0.0rc0"' in run, (
        "the rc build no longer stamps a candidate version into pyproject.toml, "
        "so the candidate keg's venv declares the stable version"
    )
    assert "pyproject.toml.orig" in run, (
        "the stamp is no longer restored, so every step after it sees a mutated " "checkout"
    )
    assert "trap 'cp \"$RUNNER_TEMP/pyproject.toml.orig\" pyproject.toml' EXIT" in run, (
        "the restore is no longer guarded by a trap: under `set -e` a failed rc "
        "build skips it, and every later step -- including the failure log dump "
        "that would explain the build -- reads a mutated version"
    )


@pytest.mark.parametrize("formula", API_FORMULAS + WEB_FORMULAS, ids=lambda p: p.name)
def test_every_formula_names_the_teardown_before_uninstall(formula: Path) -> None:
    """`brew uninstall` is where the operator is standing when they remove it (#3859).

    Homebrew has no uninstall hook, so the ordering cannot be enforced -- only
    stated, at install time, in the one place the operator reads. Removing the
    keg without the teardown leaves the service running from deleted files,
    and after `brew untap` there is no formula name left to stop it with.
    """
    text = formula.read_text(encoding="utf-8")
    assert "def caveats" in text, (
        f"{formula.name} has no caveats block, so nothing tells the operator "
        "that removal has a required first step (#3854, #3859)"
    )
    caveats = text.split("def caveats")[1].split("  end")[0]
    assert "nyxgpt ops uninstall" in caveats, (
        f"{formula.name}'s caveats no longer name the teardown command, so the "
        "operator is back to `brew uninstall` with no way to stop what it orphans"
    )
    assert "brew uninstall" in caveats and caveats.index("nyxgpt ops uninstall") < caveats.index(
        "brew uninstall"
    ), (
        f"{formula.name}'s caveats put `brew uninstall` before the teardown -- "
        "the order is the whole point, since uninstalling first is what leaves "
        "services running with no supported command to stop them"
    )


@pytest.mark.parametrize("formula", API_FORMULAS, ids=lambda p: p.name)
def test_api_formula_test_blocks_run_the_cli(formula: Path) -> None:
    """`import nyxgpt.app` resolving is true of a keg with no reachable CLI."""
    block = formula.read_text(encoding="utf-8").split("  test do")[-1]
    assert 'shell_output("#{bin}/nyxgpt --version")' in block, (
        f"{formula.name}'s test block no longer runs the CLI through bin/, so it "
        "is back to asserting only that a Python import resolves (#3850)"
    )


@pytest.mark.parametrize("formula", WEB_FORMULAS, ids=lambda p: p.name)
def test_web_formula_test_blocks_run_the_server(formula: Path) -> None:
    """File existence is also true of a keg that crash-loops on start.

    This formula has shipped exactly that: `npm prune --omit=dev` removed
    typescript, `next start` could not transpile `next.config.ts`, and `.next`
    existed throughout (#3406).
    """
    block = formula.read_text(encoding="utf-8").split("  test do")[-1]
    assert (
        'spawn "/bin/bash", bin/"nyxgpt-web"' in block
    ), f"{formula.name}'s test block no longer starts the wrapper the service runs"
    assert (
        'assert_equal "200", code' in block
    ), f"{formula.name}'s test block no longer requires the server to answer"


# Every place in the tree that enumerates what CI cannot execute. They are
# separate files by necessity (a runbook, a prompt, a doc), which is exactly
# why they drifted: #3860 made the macOS operate path CI-executed in
# `docs/live-verification-ci.md` and left five other files still calling it
# owner-acceptance-only.
NOT_COVERABLE_CLAIM_SITES = (
    REPO_ROOT / "docs" / "live-verification-ci.md",
    REPO_ROOT / "docs" / "testing.md",
    REPO_ROOT / "agents" / "runbooks" / "review-runbook.md",
    REPO_ROOT / "agents" / "runbooks" / "developer-runbook.md",
    REPO_ROOT / "agents" / "prompts" / "review-agent.prompt.md",
)


@pytest.mark.parametrize("path", NOT_COVERABLE_CLAIM_SITES, ids=lambda p: p.name)
def test_no_file_calls_the_macos_operate_path_uncoverable_without_naming_its_gate(
    path: Path,
) -> None:
    """The contradiction #3860 left behind must not be able to come back.

    After this issue the tree said both things at once: `live-verification-ci.md`
    that the macOS operate path is executed in CI, and five other files that it
    is structurally impossible to execute there. The second is what a future
    reviewer cites to exempt a macOS service-lifecycle change from executed
    evidence -- i.e. the loophole this issue exists to close.

    What this pins is narrow and deliberate: a paragraph that talks about the
    *operate* path has to name the gate that runs it. It cannot judge the
    polarity of a sentence, so it will not catch every possible re-statement --
    but a fresh "the operate path is owner acceptance" bullet written without
    reference to #3860 or the workflow that executes it is precisely the shape
    that drifted, and that shape trips here.
    """
    text = path.read_text(encoding="utf-8")
    for paragraph in re.split(r"\n[ \t]*\n", text):
        if not re.search(r"\boperate\b", paragraph):
            continue
        assert "macos-brew-smoke" in paragraph or "#3860" in paragraph, (
            f"{path.relative_to(REPO_ROOT)} discusses the *operate* path without naming "
            "the gate that executes it:\n\n"
            f"{paragraph.strip()[:400]}\n\n"
            "Since #3860 the macOS brew-services operate path runs on a real macos-15 "
            "runner (macos-brew-smoke.yml, published-tap job). A passage that leaves it "
            "on the not-CI-coverable list is the exemption a reviewer will cite."
        )


def test_the_review_contract_refuses_component_evidence_for_a_scenario_criterion() -> None:
    """#3516 closed on install evidence for a scenario criterion (#3860).

    Without this rule the workflow improvements above can all be satisfied and
    the next capstone can still close on the same partial evidence, because
    nothing in the review contract said that a scenario criterion needs a job
    that ran the scenario.
    """
    runbook = REVIEW_RUNBOOK.read_text(encoding="utf-8")
    assert "Scenario criteria need scenario evidence" in runbook, (
        "the review contract no longer distinguishes component evidence from "
        "evidence that the user's own sequence was executed (#3860)"
    )
    assert "#3516" in runbook, (
        "the rule lost the case that produced it, which is what makes it " "un-arguable in a review"
    )
