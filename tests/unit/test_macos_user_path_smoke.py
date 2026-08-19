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
