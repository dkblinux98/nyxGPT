#!/usr/bin/env python3
"""Execute the huddle session's shell control flow (#3911, #3775).

`huddle_session.yml` is 25 steps of which most are `run:` bodies, and until
this probe existed not one line of them had ever been executed anywhere: the
unit tests read the YAML, and CI's `test` gate does not map
`.github/workflows/*` onto them. The first review round of #3911 found a
settle-gating bug that would have spent two paid invocations on a huddle that
had already closed -- discoverable by running the shell once, invisible to any
amount of reading.

So this runs the real bodies, extracted from the real workflow file, on the
target platform, with the environment the runner would hand them:

  * **rounds and settling** -- an early settle stays settled, so a huddle that
    closes in round 1 does not run round 3 (the bug), and a huddle nobody
    settles runs to the round cap and stops;
  * **the transcript** -- assembled from the turn files, collapsed, and
    written even when a turn wrote nothing;
  * **the decision comment** -- carries the decision line, and a mediation
    turn that wrote no decision fails the step rather than silently
    dispatching nothing;
  * **the crash markers** -- both carry a thread id, including the `none`
    case where the session died before Slack answered;
  * **Slack degradation** -- with no channel configured every one of those
    steps still exits 0, which is #3910's contract and the reason the huddle
    survives an unconfigured chat integration.

`gh` is a recording shim on PATH: this probe must never touch GitHub. Slack is
degraded by simply not configuring it, so `huddle_channel.py` runs for real
down its NullChannel path.

Exits 0 when every case holds, 1 on the first failure, with the step's own
output attached -- run it from anywhere: `python3
scripts/agents/lib/huddle_session_probe.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SESSION = ROOT / ".github" / "workflows" / "huddle_session.yml"

FAILURES: list[str] = []

#: Which workflow file is under test. Overridden by argv so the smoke job can
#: point the probe at a planted pre-fix copy and require it to fail -- a guard
#: that cannot fail is not a guard (the `github-script-injection-smoke.yml`
#: template).
_UNDER_TEST = {"session": DEFAULT_SESSION}


def session() -> Path:
    return _UNDER_TEST["session"]


# -- reading the workflow ----------------------------------------------------
def huddle_steps() -> list[dict]:
    doc = yaml.safe_load(session().read_text(encoding="utf-8"))
    return doc["jobs"]["huddle"]["steps"]


def step_body(*, step_id: str = "", name_contains: str = "") -> str:
    """The `run:` body of one step, by id or by a substring of its name."""
    for step in huddle_steps():
        if step_id and step.get("id") == step_id:
            return str(step["run"])
        if name_contains and name_contains.lower() in str(step.get("name", "")).lower():
            return str(step["run"])
    raise LookupError(f"no step with id={step_id!r} name~={name_contains!r} in {session().name}")


# -- running one step --------------------------------------------------------
class StepRun:
    """What a step left behind: its exit code, its outputs, its `gh` calls."""

    def __init__(self, code: int, output: dict[str, str], log: str, gh_calls: list[str]):
        self.code = code
        self.output = output
        self.log = log
        self.gh_calls = gh_calls

    @property
    def gh_body(self) -> str:
        """The comment body the step handed `gh`, however it passed it."""
        return "\n".join(self.gh_calls)


def _gh_shim(bindir: Path, spool: Path) -> None:
    """A `gh` that records the comment it was asked to post and exits 0.

    `--body-file` is dereferenced here rather than recorded as a path: the
    assertions are about what would land on the PR, and the file is inside a
    temp dir that outlives neither.
    """
    shim = bindir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'SPOOL="{spool}"\n'
        'printf "CALL: %s\\n" "$*" >> "$SPOOL"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '    --body) printf "%s\\n" "$2" >> "$SPOOL"; shift 2;;\n'
        '    --body-file) cat "$2" >> "$SPOOL"; shift 2;;\n'
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def run_step(body: str, workdir: Path, env: dict[str, str]) -> StepRun:
    """Execute a step body the way the runner does: bash, with `env:` set."""
    outputs = workdir / "github_output"
    outputs.write_text("", encoding="utf-8")
    spool = workdir / "gh_calls"
    spool.write_text("", encoding="utf-8")
    bindir = workdir / "bin"
    bindir.mkdir(exist_ok=True)
    _gh_shim(bindir, spool)

    full = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_RUN_ID": "42424242",
        # No Slack: huddle_channel.py takes its NullChannel path for real.
        "SLACK_HUDDLE_CHANNEL": "",
        "SLACK_USER_TOKEN_DEV": "",
        "SLACK_USER_TOKEN_REVIEW": "",
        "SLACK_USER_TOKEN_SCRUM": "",
        **env,
    }
    proc = subprocess.run(  # noqa: S603
        ["bash", "-c", body],  # noqa: S607
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )
    parsed: dict[str, str] = {}
    for line in outputs.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key] = value
    return StepRun(
        proc.returncode,
        parsed,
        (proc.stdout + proc.stderr).strip(),
        spool.read_text(encoding="utf-8").splitlines(),
    )


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    if detail:
        for line in detail.splitlines():
            print(f"          {line}")


# -- the cases ---------------------------------------------------------------
def _turn(huddle_dir: Path, name: str, text: str) -> None:
    (huddle_dir / name).write_text(text, encoding="utf-8")


def case_an_early_settle_stays_settled(work: Path) -> None:
    """Round 1 settles -> round 2 never runs -> 04-review.md never exists.

    A settle check that reads only its own round's file calls that absence
    "not settled" and lets round 3's two turns run on a closed disagreement.
    """
    print("\n[1] a huddle settled in round 1 does not pay for round 3")
    huddle = work / "h1"
    huddle.mkdir()
    _turn(huddle, "01-dev.md", "## Developer Position (round 1)\n")
    _turn(huddle, "02-review.md", "## Review Position (round 1)\n\nHUDDLE_SETTLED\n")
    env = {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933"}

    r1 = run_step(step_body(step_id="settle1"), work, env)
    check("round 1 reports settled", r1.output.get("settled") == "true", r1.log)

    # Round 2 was skipped, so its file is absent -- exactly the runner's state.
    r2 = run_step(step_body(step_id="settle2"), work, {**env, "ALREADY": r1.output["settled"]})
    check("round 2 inherits the settle", r2.output.get("settled") == "true", r2.log)

    r3 = run_step(step_body(step_id="settle3"), work, {**env, "ALREADY": r2.output["settled"]})
    check("round 3 inherits the settle", r3.output.get("settled") == "true", r3.log)


def case_an_unsettled_huddle_runs_to_the_cap(work: Path) -> None:
    print("\n[2] a huddle nobody settles runs every round and stops at the cap")
    huddle = work / "h2"
    huddle.mkdir()
    for name in ("02-review.md", "04-review.md", "06-review.md"):
        _turn(huddle, name, "## Review Position\n\n### Where I still disagree\nstill this.\n")
    env = {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933"}

    already = "false"
    for step_id in ("settle1", "settle2", "settle3"):
        result = run_step(step_body(step_id=step_id), work, {**env, "ALREADY": already})
        check(f"{step_id} reports unsettled", result.output.get("settled") == "false", result.log)
        already = result.output.get("settled", "")

    rounds = {
        str(s.get("name", ""))
        for s in huddle_steps()
        if "claude-code-action" in str(s.get("uses", ""))
    }
    check("there is no round 4 to run", not any("Round 4" in name for name in rounds))
    check("the cap is 3 rounds of 2 turns plus the decision", len(rounds) == 7, str(sorted(rounds)))


def case_a_settle_line_must_be_its_own_line(work: Path) -> None:
    """`grep -qx` and not `grep -q`: a review turn that *discusses* settling
    ("I would write HUDDLE_SETTLED if...") must not close the huddle."""
    print("\n[3] the settle marker is matched as a whole line")
    huddle = work / "h3"
    huddle.mkdir()
    _turn(huddle, "02-review.md", "I would write HUDDLE_SETTLED if the test landed.\n")
    result = run_step(
        step_body(step_id="settle1"),
        work,
        {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933", "ALREADY": "false"},
    )
    check(
        "a mention does not settle the huddle", result.output.get("settled") == "false", result.log
    )


def case_the_transcript_survives_slack(work: Path) -> None:
    print("\n[4] the transcript is assembled from the turn files")
    huddle = work / "h4"
    huddle.mkdir()
    _turn(huddle, "01-dev.md", "## Developer Position (round 1)\n\nmy diagnosis\n")
    _turn(huddle, "02-review.md", "## Review Position (round 1)\n\nmy finding\n")
    result = run_step(
        step_body(name_contains="Append the transcript"),
        work,
        {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933"},
    )
    body = result.gh_body
    check("the step succeeds", result.code == 0, result.log)
    check("it is collapsed", "<details>" in body and "</details>" in body, body)
    check("every turn is in it", "my diagnosis" in body and "my finding" in body, body)
    check("turns are labelled", "### 01-dev" in body and "### 02-review" in body, body)
    check("it counts the turns", "(2 turns)" in body, body)


def case_an_empty_huddle_transcribes_nothing(work: Path) -> None:
    print("\n[5] a session that died before any turn posts no empty transcript")
    huddle = work / "h5"
    huddle.mkdir()
    result = run_step(
        step_body(name_contains="Append the transcript"),
        work,
        {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933"},
    )
    check("the step succeeds", result.code == 0, result.log)
    check("nothing was posted", not result.gh_calls, result.gh_body)


def case_the_decision_reaches_the_pr(work: Path) -> None:
    print("\n[6] the decision comment carries the line the dispatch keys on")
    huddle = work / "h6"
    huddle.mkdir()
    _turn(
        huddle,
        "90-decision.md",
        "## Huddle Decision\n\n**Decision:** proceed\n\n### Rationale\n"
        "the position answers the finding.\n\nHUDDLE_DECISION: proceed\n",
    )
    result = run_step(
        step_body(name_contains="Post the decision"),
        work,
        {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933", "THREAD_TS": ""},
    )
    body = result.gh_body
    check("the step succeeds with Slack unconfigured", result.code == 0, result.log)
    check("the decision line lands on the PR", "HUDDLE_DECISION: proceed" in body, body)
    check("the rationale lands with it", "answers the finding" in body, body)


def case_a_missing_decision_fails_loudly(work: Path) -> None:
    """The one place the session must NOT degrade: no decision means no fix
    cycle, and a silent success would leave the issue parked in In Review."""
    print("\n[7] a mediation turn that wrote no decision fails the step")
    huddle = work / "h7"
    huddle.mkdir()
    result = run_step(
        step_body(name_contains="Post the decision"),
        work,
        {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933", "THREAD_TS": ""},
    )
    check("the step fails", result.code != 0, result.log)
    check("it says why", "nothing to dispatch" in result.log, result.log)
    check("no comment was posted", not result.gh_calls, result.gh_body)


def case_the_crash_markers_are_recoverable(work: Path) -> None:
    print("\n[8] both crash markers name the thread")
    huddle = work / "h8"
    huddle.mkdir()
    env = {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933"}

    started = run_step(
        step_body(name_contains="Record that the session started"),
        work,
        {**env, "THREAD_TS": "1700000000.000100"},
    )
    check("started marker posted", started.code == 0, started.log)
    check(
        "it carries the thread",
        "HUDDLE_SESSION_STARTED thread=1700000000.000100" in started.gh_body,
        started.gh_body,
    )
    check("it carries the run", "run=42424242" in started.gh_body, started.gh_body)

    # The session died before Slack answered: `thread=` with nothing after it
    # reads as a truncated marker, so both markers default to `none`.
    failed = run_step(
        step_body(name_contains="Record a failed session"), work, {**env, "THREAD_TS": ""}
    )
    check("failure marker posted", failed.code == 0, failed.log)
    check(
        "it degrades to thread=none", "HUDDLE_FAILED thread=none" in failed.gh_body, failed.gh_body
    )

    started_none = run_step(
        step_body(name_contains="Record that the session started"), work, {**env, "THREAD_TS": ""}
    )
    check(
        "the started marker degrades the same way",
        "HUDDLE_SESSION_STARTED thread=none" in started_none.gh_body,
        started_none.gh_body,
    )


def case_slack_being_down_does_not_stop_the_huddle(work: Path) -> None:
    print("\n[9] an unconfigured Slack degrades every posting step, it never fails one")
    huddle = work / "h9"
    huddle.mkdir()
    _turn(huddle, "01-dev.md", "## Developer Position (round 1)\n\nmy diagnosis\n")
    env = {"HUDDLE_DIR": str(huddle), "PR_NUMBER": "3933", "THREAD_TS": ""}

    posted = run_step(step_body(name_contains="Round 1 - post the dev turn"), work, env)
    check("posting a turn exits 0", posted.code == 0, posted.log)
    check("and says why", "no huddle channel" in posted.log, posted.log)

    opened = run_step(
        step_body(name_contains="Open the huddle thread"),
        work,
        {**env, "REASON": "2nd unconverged cycle", "ISSUE_NUMBER": "3911"},
    )
    check("opening a thread exits 0", opened.code == 0, opened.log)
    check("with no thread id", opened.output.get("ts", "") == "", str(opened.output))
    check("and warns rather than errors", "::warning::" in opened.log, opened.log)

    empty = work / "h9-empty"
    empty.mkdir()
    missing = run_step(
        step_body(name_contains="Round 1 - post the dev turn"),
        work,
        {**env, "HUDDLE_DIR": str(empty)},
    )
    check("a turn that wrote no file does not fail the run", missing.code == 0, missing.log)


# -- proving the probe can fail ----------------------------------------------
#
# The shipped settle body and the pre-fix one it replaced, verbatim from the
# workflow. `--prove-it-fails` plants the latter and requires the cases above
# to reject it: a probe whose assertions nothing can violate would pass
# forever while the behaviour rotted underneath it.
_SETTLE_SHIPPED = (
    '          if [[ "$ALREADY" == "true" ]]; then\n'
    '            echo "settled=true" >> "$GITHUB_OUTPUT"\n'
    '            echo "::notice::Huddle already settled in an earlier round"\n'
    '          elif [[ -s "$FILE" ]] && grep -qx \'HUDDLE_SETTLED\' "$FILE"; then\n'
)
_SETTLE_PREFIX = '          if [[ -s "$FILE" ]] && grep -qx \'HUDDLE_SETTLED\' "$FILE"; then\n'


def prove_it_fails() -> int:
    """Plant the pre-fix settle gating and require the probe to catch it."""
    source = session().read_text(encoding="utf-8")
    if _SETTLE_SHIPPED not in source:
        print("::error::the settle body no longer matches the shape this injection plants.")
        print("::error::Update _SETTLE_SHIPPED/_SETTLE_PREFIX so the probe stays falsifiable.")
        return 1

    print("Planting the pre-fix settle gating (round N reads only its own file)")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        planted = work / "planted-huddle_session.yml"
        planted.write_text(source.replace(_SETTLE_SHIPPED, _SETTLE_PREFIX), encoding="utf-8")
        _UNDER_TEST["session"] = planted
        FAILURES.clear()
        case_an_early_settle_stays_settled(work)
        caught = list(FAILURES)
        FAILURES.clear()

    if not caught:
        print("::error::the probe accepted a huddle that runs round 3 after settling in round 1")
        return 1
    print(f"\nProbe correctly rejected the pre-fix gating ({len(caught)} assertion(s) fired).")
    return 0


def main(argv: list[str]) -> int:
    prove = "--prove-it-fails" in argv
    paths = [a for a in argv if not a.startswith("-")]
    if paths:
        _UNDER_TEST["session"] = Path(paths[0]).resolve()
    if not session().exists():
        print(f"::error::{session()} does not exist")
        return 1
    if not shutil.which("bash"):
        print("::error::this probe needs bash")
        return 1

    if prove:
        return prove_it_fails()

    print(f"Executing {session()}'s shell bodies")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        case_an_early_settle_stays_settled(work)
        case_an_unsettled_huddle_runs_to_the_cap(work)
        case_a_settle_line_must_be_its_own_line(work)
        case_the_transcript_survives_slack(work)
        case_an_empty_huddle_transcribes_nothing(work)
        case_the_decision_reaches_the_pr(work)
        case_a_missing_decision_fails_loudly(work)
        case_the_crash_markers_are_recoverable(work)
        case_slack_being_down_does_not_stop_the_huddle(work)

    if FAILURES:
        print(f"\n::error::{len(FAILURES)} huddle session behaviour(s) did not hold:")
        for label in FAILURES:
            print(f"::error::  {label}")
        return 1
    print("\nEvery huddle session behaviour held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
