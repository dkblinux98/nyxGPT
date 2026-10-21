"""A sweep that notices a board change is dispatched by the actor, not polled (#4201).

`promote_accepted_features.yml` and `sweep_parked_blocked_issues.yml` ran on
`cron: "*/30 * * * *"` — 96 runs a day between them, each checking out the
release branch and walking every parked issue's transitive blocked-by closure
through the API to report `Promoted 0 issue(s)`. Both the work and the trigger
were wrong for the same reason: what they are waiting on is a *known actor*
changing a lane, and GitHub throttles a busy cron to every 3–8 hours, so the
poll was unreliable as well as expensive (first principle 1).

This is the third removal of that shape — the release ceremony's 15-minute
cron (ledger D-060, 2026-10-05) and the drain gate's (2026-08-25) were the
first two — so the rule is checked here rather than written down again:

1. neither sweep carries a sub-daily cron, and both stay dispatchable;
2. **no** workflow carries a sub-daily cron except the one that is genuinely a
   timer rather than a poll (see ``TIMERS`` below) — that is the class, not the
   two instances the issue named;
3. the dispatch decision lives in exactly ONE function (D-066), and every
   actor that changes the state a sweep waits on calls it;
4. the helper is best-effort *by execution*: a refused dispatch still exits 0,
   because a merge, ceremony or gate release must never fail over a sweep that
   can be asked for again;
5. **no prose promises a timer** — not "runs on a cron", not "promoted
   automatically" with nothing named as the promoter. This half is checked
   here because the first pass at #4201 fixed one of each pair of sibling
   sentences and missed the other: the prose pass matched poll-phrasings ("the
   next sweep", "within 30 minutes") but not the ``(cron + dispatch)`` header
   form or the "promoted automatically once" body-note form, so
   ``sweep_parked_blocked_issues.sh``'s own header still advertised a cron the
   same commit deleted. A claim about a trigger is cheap to assert and
   expensive to disbelieve, so it gets a guard rather than another note.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"

#: The two sweeps this rule was written for.
SWEEPS = ("promote_accepted_features.yml", "sweep_parked_blocked_issues.yml")

#: The one shared entry point. Named here so a second copy of the decision
#: fails this file rather than quietly diverging (#4179 ran for seven weeks on
#: exactly that).
HELPER = "dispatch_acceptance_sweeps"

#: Workflows allowed a sub-daily cron, each with the reason it is a TIMER and
#: not a poll for board state. A timer waits on the passage of time, which no
#: actor can announce; a poll waits on something an actor did, which is what
#: this rule forbids. Adding an entry here is a claim — write the reason.
TIMERS = {
    "usage_limit_retry.yml": (
        "waits on a wall-clock `retry_after` timestamp recorded when a Claude "
        "usage window was exhausted. Nothing can notify the loop that the "
        "window reopened, so the passage of time IS the event. Assessed under "
        "#4201 and deliberately left on its cron."
    ),
}

#: Every actor that changes the state a sweep waits on, and must therefore
#: dispatch it. The two acceptance handlers are deliberately ABSENT: they only
#: ever ADD a blocker, so a sweep dispatched from them can promote nothing —
#: see the comment at each handler's `mark_issue_blocked_by` call.
ACTORS = (
    Path("scripts/agents/review_accept_and_merge.sh"),
    Path("scripts/agents/drain_gate.sh"),
    Path("scripts/release_ceremony.sh"),
)


#: An actual invocation of `gh workflow run`, as opposed to the command being
#: quoted in owner-facing copy or a log line (which AC3 of #4201 requires).
_INVOKES_GH_WORKFLOW_RUN = re.compile(
    r"(?:^|[;&|]\s*|\b(?:if|elif|then|else|do)\s+|!\s*)gh workflow run\b"
)

#: The four files that describe their OWN trigger: each sweep's workflow and
#: each sweep's script. These are where a reader goes to learn how the sweep
#: starts, so a stale cron claim here is the most expensive one in the tree —
#: and `sweep_parked_blocked_issues.sh` carried exactly that until this guard.
SELF_DESCRIBING = (
    Path(".github/workflows/promote_accepted_features.yml"),
    Path(".github/workflows/sweep_parked_blocked_issues.yml"),
    Path("scripts/agents/promote_accepted_features.sh"),
    Path("scripts/agents/sweep_parked_blocked_issues.sh"),
)

#: Words that make a cron mention HISTORY rather than a live claim. The two
#: sweeps' headers explain at length what the cron was and why it went, which
#: is the opposite of promising one — so the guard asks for the tense, not for
#: silence about crons.
_RETROSPECTIVE = re.compile(
    r"\b(?:was|were|used to|removed|retired|until|no longer|dispatch-only|"
    r"instead of|rather than|not|absent|absence|gone|without)\b",
    re.IGNORECASE,
)

#: A live claim that something starts on a timer.
_TIMER_CLAIM = re.compile(r"\bcron\b|\bschedule[sd]?\b|\bpoll(?:s|ed|ing)?\b", re.IGNORECASE)

#: The promotion promise with no promoter named. Matched in BOTH orders
#: ("promoted ... automatically", "automatically promotes ...") because the
#: instance this guard was written for used the first and a sibling two
#: sections away in the same document used the second.
_UNATTRIBUTED_PROMOTION = (
    re.compile(r"promot(?:e|ed|es|ion)\w*.{0,120}?automatic", re.IGNORECASE | re.DOTALL),
    re.compile(r"automatic\w*.{0,80}?promot", re.IGNORECASE | re.DOTALL),
)

#: "Promote" is overloaded in this tree — a canary track is promoted too, and
#: `docs/api.md` quite correctly describes that one as automatic. Only the
#: board sense is in this class, so the paragraph has to be about a lane.
_BOARD_SENSE = re.compile(r"For Release|block(?:er|ing|ed)|acceptance", re.IGNORECASE)

#: Directories with no prose of ours in them.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)

_PROSE_SUFFIXES = frozenset({".md", ".sh", ".yml", ".yaml", ".py"})


def _prose_files() -> list[Path]:
    """Every text file of ours, so this is the class and not a named list."""
    found = []
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in _PROSE_SUFFIXES or not path.is_file():
            continue
        if _SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        if path == Path(__file__):
            continue  # this file quotes the forbidden forms in order to ban them
        found.append(path)
    return sorted(found)


#: What separates one paragraph from the next. A bare `#` is the blank line of
#: a shell or YAML comment block, and without it a 50-line file header reads as
#: ONE paragraph — which is how the first version of this guard sat green on
#: the very defect it was written for: a retrospective sentence 30 lines above
#: excused the live claim.
_BLOCK_BREAKS = frozenset({"", "#", "#!", "//", "--"})


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Paragraph blocks, with the line number each one starts on.

    Per-*line* matching would miss the real instances: markdown prose wraps,
    and "promoted to For Release ... automatically" straddled a line break in
    `docs/how-this-project-is-run.md`. Requiring the attribution in the same
    paragraph is also the honest bar — a reader takes the claim and its
    qualifier together, not one line at a time.
    """
    blocks, start, buffer = [], 1, []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() not in _BLOCK_BREAKS:
            if not buffer:
                start = number
            buffer.append(line)
            continue
        if buffer:
            blocks.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        blocks.append((start, " ".join(buffer)))
    return blocks


def _triggers(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads a bare `on:` key as the boolean True.
    return loaded[True] if True in loaded else loaded["on"]


def _crons(path: Path) -> list[str]:
    schedule = _triggers(path).get("schedule") or []
    return [entry["cron"] for entry in schedule if "cron" in entry]


def _is_sub_daily(cron: str) -> bool:
    """True when this cron fires more than once a day.

    Both halves matter: `*/30 * * * *` is caught by the minute field and
    `13,43 * * * *` only by it, while a hypothetical `0 */4 * * *` is caught by
    the hour field alone.
    """
    minute, hour = cron.split()[0], cron.split()[1]
    multi = ("*", "/", ",", "-")
    return any(ch in minute for ch in multi) or any(ch in hour for ch in multi)


@pytest.mark.parametrize("sweep", SWEEPS)
def test_the_sweeps_carry_no_cron(sweep: str) -> None:
    """Dispatch-only, as D-060 made the ceremony and 2026-08-25 the gate."""
    assert _crons(WORKFLOWS / sweep) == [], (
        f"{sweep} must not poll for board state. If a safety net is genuinely "
        "needed it is at most daily, and the reason belongs in the workflow "
        "header — see ledger D-070."
    )


@pytest.mark.parametrize("sweep", SWEEPS)
def test_the_sweeps_stay_dispatchable(sweep: str) -> None:
    """The owner's board move raises no event, so the dispatch IS the trigger.

    A user project emits no `projects_v2_item`, so removing the cron without
    keeping `workflow_dispatch` would strand every owner-driven promotion.
    """
    assert "workflow_dispatch" in _triggers(WORKFLOWS / sweep)


def test_no_workflow_polls_for_board_state() -> None:
    """The class, not the two instances the issue happened to name (#4183)."""
    offenders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for cron in _crons(path):
            if _is_sub_daily(cron) and path.name not in TIMERS:
                offenders.append(f"{path.name}: {cron!r}")
    assert not offenders, (
        "sub-daily cron(s) found: "
        + "; ".join(offenders)
        + ". A workflow whose job is to notice that a known actor changed "
        "state is dispatched by that actor (#4201). If it is a genuine timer, "
        "add it to TIMERS in this file with the reason."
    )


def test_the_dispatch_decision_has_one_source() -> None:
    """One place names the sweep workflows; everything else asks for them."""
    assert LIB.read_text(encoding="utf-8").count(f"\n{HELPER}()") == 1

    offenders = []
    for root in (REPO_ROOT / "scripts", WORKFLOWS):
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".sh", ".yml", ".py"} or path == LIB:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if line.lstrip().startswith("#"):
                    continue  # naming a workflow in prose is not dispatching it
                if not _INVOKES_GH_WORKFLOW_RUN.search(line):
                    # The owner-facing copy quotes the command on purpose
                    # (`gh workflow run promote_accepted_features.yml`) — that
                    # is documentation, which AC3 requires, not a second
                    # dispatch path. Only an actual invocation counts.
                    continue
                if any(sweep in line for sweep in SWEEPS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, (
        f"these dispatch a sweep directly instead of calling {HELPER}: {offenders}. "
        "Correcting N copies leaves N places for the next divergence (D-066)."
    )


@pytest.mark.parametrize("actor", ACTORS, ids=lambda p: p.name)
def test_every_actor_dispatches_the_sweeps(actor: Path) -> None:
    """A lane change nobody announces is a lane change nothing acts on."""
    body = (REPO_ROOT / actor).read_text(encoding="utf-8")
    calls = [
        line for line in body.splitlines() if HELPER in line and not line.lstrip().startswith("#")
    ]
    assert calls, f"{actor} changes board state but never calls {HELPER}"


@pytest.mark.parametrize("relative", SELF_DESCRIBING, ids=lambda p: p.name)
def test_no_sweep_advertises_a_cron_it_does_not_have(relative: Path) -> None:
    """The file that tells you how the sweep starts has to be right (AC5).

    `scripts/agents/sweep_parked_blocked_issues.sh` said "Run via
    .github/workflows/sweep_parked_blocked_issues.yml (cron + dispatch)" in the
    same commit that deleted that cron, while its sibling header in
    `promote_accepted_features.sh` was corrected. One of each pair is how a
    class survives a fix.
    """
    offenders = []
    for number, block in _paragraphs((REPO_ROOT / relative).read_text(encoding="utf-8")):
        if _TIMER_CLAIM.search(block) and not _RETROSPECTIVE.search(block):
            offenders.append(f"{relative}:{number}: {block.strip()[:120]}")
    assert not offenders, (
        "a sweep's own header claims a trigger it does not have:\n"
        + "\n".join(offenders)
        + "\n\nBoth sweeps are dispatch-only (ledger D-070). Say so, or say in "
        "the past tense what the cron used to be and why it went."
    )


def test_no_prose_promises_an_automatic_promotion_with_no_promoter() -> None:
    """ "Promoted automatically" is now a half-truth, and the half that is
    missing is the one the reader needs.

    Promotion still happens without anyone asking for it *when an agent made
    the last move* — the merge path dispatches the sweep. The commonest case is
    the other one: the owner moves the last blocker to "For Release" on the
    board, which raises no event at all, and nothing happens until somebody
    dispatches. So every paragraph making the promise must name the dispatch in
    the same breath. Tree-wide on purpose: the class is "prose promising the
    poll", not the five sentences review and this sweep happened to find.
    """
    offenders = []
    for path in _prose_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "promot" not in text.lower():
            continue  # cheap gate: most of the tree never mentions promotion
        for number, block in _paragraphs(text):
            if not any(pattern.search(block) for pattern in _UNATTRIBUTED_PROMOTION):
                continue
            if not _BOARD_SENSE.search(block):
                continue
            if "dispatch" in block.lower():
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {block.strip()[:140]}")
    assert not offenders, (
        "these promise an automatic promotion without naming what triggers "
        "it:\n" + "\n".join(offenders) + "\n\nSince #4201 (ledger D-070) the "
        "promotion sweeps have no schedule: the actor that makes the last move "
        "dispatches them, and an owner's board move raises no event. Name the "
        "dispatch in the same paragraph as the promise."
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_refused_dispatch_does_not_fail_the_caller(tmp_path: Path) -> None:
    """Executed, not inspected: the property only holds when it runs.

    This is the half that cannot be read off the source with confidence — the
    helper loops over two workflows and collects a failure, and the question is
    what the *function* returns to a merge that has already happened. So a stub
    `gh` that refuses every dispatch is put on PATH and the real function is
    sourced and called.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    script = textwrap.dedent(f"""
        set -euo pipefail
        REPO_OWNER=o REPO_NAME=r RELEASE_BRANCH=v9.9.9
        source "{LIB}"
        {HELPER} "stubbed refusal"
        echo "RC=$?"
        """)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, (
        "a refused dispatch must not fail the caller — a merge, ceremony or "
        f"gate release that dies here loses the work:\n{result.stderr}"
    )
    assert "RC=0" in result.stdout
    assert "refused the dispatch" in result.stderr, (
        "best-effort is not silent: the failure has to be reported where "
        "somebody reads the run log"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_an_unset_release_branch_does_not_kill_the_caller(tmp_path: Path) -> None:
    """The failure mode that looks handled and is not.

    Reading the branch through a helper that echoes `${RELEASE_BRANCH}` bare
    makes bash exit the command substitution's subshell *fatally* under
    `set -eu` — straight past the `|| echo ""` that appears to cover it — so a
    merge calling this would die after it had already merged. `load_config`
    requires the variable, which makes this unreachable in the loop; it is
    asserted anyway because "unreachable" is the wrong thing to stake a
    completed merge on.
    """
    script = textwrap.dedent(f"""
        set -euo pipefail
        REPO_OWNER=o REPO_NAME=r
        source "{LIB}"
        {HELPER} "no ref configured"
        echo "SURVIVED"
        """)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "SURVIVED" in result.stdout
    assert "were NOT dispatched" in result.stderr, (
        "a dispatch that could not happen must say so, and name the by-hand "
        f"command: {result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("result", "dry_run", "wanted", "why"),
    [
        ('{"action":"released","released":[1,2]}', "0", True, "released two"),
        ('{"action":"none","released":[]}', "0", False, "nothing held"),
        ('{"action":"none","reason":"gate closed"}', "0", False, "gate shut"),
        ('{"action":"released","released":[1]}', "1", False, "dry run"),
        ("not json at all", "0", False, "unreadable result"),
    ],
    ids=["released", "nothing-held", "gate-closed", "dry-run", "unreadable"],
)
def test_the_gate_dispatch_is_edge_triggered(
    result: str, dry_run: str, wanted: bool, why: str, tmp_path: Path
) -> None:
    """The property that stops a gate -> sweeps -> gate cycle, executed.

    `acceptance_drain_gate.yml` triggers on `workflow_run` completion of both
    sweeps. A gate run that dispatched them on every evaluation would restart
    itself forever — the #3790 shape. Only an actual release may dispatch, and
    a DRY_RUN (which still reports what it *would* move) may not.
    """
    script = textwrap.dedent(f"""
        set -uo pipefail
        source "{LIB}"
        DRY_RUN={dry_run}
        if drain_gate_wants_sweeps {result!r}; then echo WANTED; else echo SKIPPED; fi
        """)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    expected = "WANTED" if wanted else "SKIPPED"
    assert expected in out, f"{why}: expected {expected}, got {out!r}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_dispatch_names_the_release_branch(tmp_path: Path) -> None:
    """`gh workflow run` without `--ref` runs the DEFAULT branch's copy.

    That is the trap `manually_trigger_pr_review.sh` documents: the dispatch
    succeeds and executes a copy of the workflow nobody chose. The sweeps check
    out `RELEASE_BRANCH`, so the run has to come from it too.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text('#!/usr/bin/env bash\necho "$@" >> "$GH_ARGV_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "argv.log"

    script = textwrap.dedent(f"""
        set -euo pipefail
        REPO_OWNER=o REPO_NAME=r RELEASE_BRANCH=v9.9.9
        source "{LIB}"
        {HELPER} "stubbed success"
        """)
    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GH_ARGV_LOG": str(log),
        },
    )
    argv = log.read_text(encoding="utf-8")
    for sweep in SWEEPS:
        assert (
            f"workflow run {sweep} -R o/r --ref v9.9.9" in argv
        ), f"{sweep} was not dispatched on the release branch:\n{argv}"
