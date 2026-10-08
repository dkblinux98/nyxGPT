"""The evidence an escalation headline is composed from (#4176).

Owner report, 2026-10-08: developer-agent escalations routinely arrive headed
**"Error type could not be determined. Manual investigation needed."** -- "the
usual unhelpful reason" -- while the same comment's own blast-radius section
says, three lines further down, *"Is `<release branch>` red? yes -- `test`
failing. Everything built on this head is affected"*. The pipeline knew the
cause and printed the sentence for not knowing it.

(The release line is written as `<release branch>` throughout, not as the
literal it was: `tests/unit/test_no_hardcoded_release_version.py` scans
docstrings, because a docstring is what readers copy. The run link below is
where the concrete line is recorded.)

The worked example is #4166 (run 37709148793). Three independent defects, each
sufficient on its own, and this module exists because of the third:

  1. Final Verification recorded no failure reason, so Phase 1 classified the
     failed step's NAME ("Final Verification (Must Pass)"), matched no
     signature and returned `unknown`. Fixed by `format_verification_detail`
     below, which `scripts/agents/run_final_verification.sh` writes through
     `write_agent_error_detail`, and by `classify_error`'s
     `verification_failed:<gate>` branch.
  2. Phase 3 -- which exists to diagnose `unknown`s -- died in the action's own
     branch setup (`fatal: '<release branch>' is already used by worktree
     at '/tmp/base-wt'`), so no diagnosis existed at all. A Phase 3 that CRASHED
     read identically to one that concluded "unknown". Fixed by
     `scripts/agents/prune_stray_worktrees.sh` and by `phase3_crash_sentence`.
  3. **The headline was a lookup on the error class, not a composition of the
     evidence.** `errorExplanations[errorClass]` with both diagnoses empty maps
     `unknown` to the generic sentence -- never reading the Phase 1 failure
     detail, never reading the base-red finding, never saying Phase 3 failed to
     run.

So the headline is COMPOSED here, from whatever evidence the run holds, in the
order the owner would read it: Phase 3's diagnosis, then the red base, then
what Final Verification actually failed on, then "Phase 3 could not run", and
only with none of those the generic sentence. `headline` is pure over a dict,
so every combination is unit-testable without a workflow run
(`tests/unit/test_escalation_headline.py`).

**One module owns both ends of the format.** `format_verification_detail`
writes the error-detail file and `parse_verification_detail` reads it back. A
writer in bash and a reader in JavaScript was the arrangement that produced
defect 1: the two drifted and nobody could see it from either side.

**The generic sentence is kept, not reworded.** Rewording it was the narrow
fix the issue names and rejects: a prettier sentence for "no evidence" leaves
all three evidence paths dark. It now appears only when the run genuinely holds
nothing -- which is the one case where it is true.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# The error-class table (#4179). Same directory, so `python3
# scripts/agents/lib/escalation_evidence.py` finds it on sys.path[0]; the
# explicit insert is for importers that put only the repo root there.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import error_classes  # noqa: E402

#: How many failing test node IDs the detail file names before it switches to
#: a count. Enough to see the pattern (one module? all of them?), few enough
#: that an escalation comment stays readable.
FAILING_TEST_LIMIT = 20

#: The marker line `format_verification_detail` writes and `classify_error`
#: greps for. Changing it breaks the shell classifier, so
#: tests/unit/test_escalation_headline.py pins the string and
#: tests/test_escalation_evidence.sh pins the classifier's reading of it.
DETAIL_MARKER = "Final Verification failed: gate="

#: The marker line `red_head_check_lines` (gh_project.sh) writes into the
#: agent error-detail file for each failing required check, and
#: `parse_red_head_detail` below reads back. Pinned on both sides by
#: tests/unit/test_error_classes.py -- shell writes it, Python reads it, which
#: is the handshake #4176's verification detail has in the other direction.
RED_HEAD_CHECK_PREFIX = "red-head-check: "

#: The refusal sentence `developer_submit_for_review.sh` prints and
#: `classify_error` greps for (#3971). Present here because
#: `parse_red_head_detail` needs to recognise a refusal that named no checks.
RED_HEAD_MARKER = "red head is not reviewable"

#: The sentence this whole module exists to stop being the headline. It is the
#: text `escalate_fatal` used to print for `unknown`, kept verbatim so the
#: owner can tell "there was genuinely no evidence" from the old behaviour.
GENERIC_HEADLINE = error_classes.explanation("unknown")

#: Human-readable explanations for the classes that ARE self-explanatory. The
#: last-resort layer, below every piece of real evidence.
#:
#: READ FROM THE ONE TABLE (#4179). This used to be a second hand-maintained
#: dict, which is the same defect in prose that Phase 2's missing `ci_red` arm
#: was in control flow: `retriable:ci_red` had no entry here either, so a
#: ci_red escalation fell through `.get(class) or ["unknown"]` to "Error type
#: could not be determined" -- the exact sentence #4176 removed.
ERROR_EXPLANATIONS = {
    name: str(row["explanation"]) for name, row in error_classes.ERROR_CLASSES.items()
}

#: Gate -> what failing it means, in the words of the command that failed.
_GATE_LABELS = {
    "black": "black formatting",
    "ruff": "ruff linting",
    "mypy": "mypy type checking",
    "pytest": "pytest",
    "tsc": "TypeScript type checking",
    "routes": "web route validation",
}


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _lines(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    return [_text(v) for v in value if _text(v)]


# ---------------------------------------------------------------------------
# The error-detail file: written by run_final_verification.sh, read back here
# ---------------------------------------------------------------------------


def format_verification_detail(
    gate: str,
    failures: Any = None,
    total: int | None = None,
    errors: Any = None,
) -> str:
    """The text Final Verification records as its own failure reason.

    `gate` is which check failed (black / ruff / mypy / pytest / tsc /
    routes). `failures` are pytest's `FAILED <nodeid>` node IDs -- taken from
    its `-rf` short summary, never grepped out of `-v` output, because the
    short summary is the only part of pytest's output whose shape is a
    contract. `total` is how many there were in all (the file names the first
    `FAILING_TEST_LIMIT`). `errors` are the first error lines for the gates
    that do not have node IDs.

    The first line is machine-read by `classify_error`; everything after it is
    for the owner.
    """
    gate = _text(gate) or "unknown"
    nodes = _lines(failures)
    error_lines = _lines(errors)
    count = total if isinstance(total, int) and total >= 0 else len(nodes)

    out = [f"{DETAIL_MARKER}{gate}"]
    if nodes:
        out.append(f"{count} failing test(s):")
        out.extend(
            f"FAILED {n}" if not n.startswith("FAILED") else n for n in nodes[:FAILING_TEST_LIMIT]
        )
        if count > len(nodes[:FAILING_TEST_LIMIT]):
            out.append(f"... and {count - len(nodes[:FAILING_TEST_LIMIT])} more failing test(s)")
    elif count and gate == "pytest":
        # pytest failed and reported a count but no parseable node IDs (a
        # collection error, for instance). Say the count rather than nothing:
        # "pytest failed" plus a number is still evidence.
        out.append(f"{count} failing test(s), node IDs not parseable from the short summary")
    for line in error_lines[:FAILING_TEST_LIMIT]:
        out.append(line)
    return "\n".join(out)


def parse_verification_detail(text: Any) -> dict[str, Any] | None:
    """`format_verification_detail`'s inverse, or None if this is not one.

    Tolerant on purpose: the detail file is also the place a FUTURE gate
    writes, and a reader that only accepts today's exact rendering would turn
    the next gate's real evidence back into the generic sentence.
    """
    raw = _text(text)
    if not raw or DETAIL_MARKER not in raw:
        return None
    lines = raw.splitlines()
    gate = ""
    for line in lines:
        if DETAIL_MARKER in line:
            gate = line.split(DETAIL_MARKER, 1)[1].strip() or "unknown"
            break
    nodes = [ln[len("FAILED ") :].strip() for ln in lines if ln.strip().startswith("FAILED ")]
    total = len(nodes)
    for line in lines:
        match = re.search(r"(\d+)\s+failing test", line)
        if match:
            total = max(total, int(match.group(1)))
            break
    errors = [
        ln.strip()
        for ln in lines
        if ln.strip()
        and DETAIL_MARKER not in ln
        and not ln.strip().startswith("FAILED ")
        and not re.search(r"failing test\(s\)", ln)
        and not ln.strip().startswith("... and ")
    ]
    return {"gate": gate, "failing_tests": nodes, "failing_total": total, "errors": errors}


# ---------------------------------------------------------------------------
# The red-head refusal: written by red_head_check_lines (shell), read here
# ---------------------------------------------------------------------------


def parse_red_head_detail(text: Any) -> dict[str, Any] | None:
    """The failing required checks named by a red-head refusal, or None.

    Returns `{"checks": [{"name": ..., "url": ...}, ...]}` for a refusal that
    named them, and `{"checks": []}` for one that did not -- a refusal with no
    readable check list is still a refusal, and the difference between "no
    checks named" and "not a refusal at all" is the difference between
    continuing the round and escalating it.

    Tolerant in the same way `parse_verification_detail` is: the check list is
    read from the structured lines when they are there, and from the refusal's
    own `Required checks FAILED on head <sha>: a, b` sentence when they are
    not (refusals recorded before #4179 carry only the sentence).
    """
    raw = _text(text)
    if not raw or RED_HEAD_MARKER not in raw:
        return None

    checks: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if RED_HEAD_CHECK_PREFIX not in stripped:
            continue
        rest = stripped.split(RED_HEAD_CHECK_PREFIX, 1)[1].strip()
        if not rest:
            continue
        name, _, url = rest.partition(" ")
        if name and name not in seen:
            seen.add(name)
            checks.append({"name": name, "url": url.strip()})

    if not checks:
        match = re.search(r"Required checks FAILED on head \S+:\s*(.+)", raw)
        if match:
            for name in (n.strip() for n in match.group(1).split(",")):
                if name and name not in seen:
                    seen.add(name)
                    checks.append({"name": name, "url": ""})

    sha = ""
    sha_match = re.search(r"Required checks FAILED on head (\S+)", raw)
    if sha_match:
        sha = sha_match.group(1).rstrip(":")
    return {"checks": checks, "head_sha": sha}


def red_head_checks(text: Any) -> list[dict[str, str]]:
    """Just the check list from a red-head refusal (empty if it is not one)."""
    parsed = parse_red_head_detail(text)
    if not parsed:
        return []
    checks = parsed.get("checks")
    return checks if isinstance(checks, list) else []


# ---------------------------------------------------------------------------
# The sentences
# ---------------------------------------------------------------------------


def base_red_sentence(ev: dict[str, Any]) -> str:
    """ "This failure is inherited from a base that is already red."

    Only emitted on a POSITIVE finding. `base_red` absent or None means the
    head's check state could not be read, and an unanswered question must not
    be rendered as either answer -- the same rule blast_radius.report follows
    for its four questions.
    """
    if ev.get("base_red") is not True:
        return ""
    branch = _text(ev.get("base_branch")) or "the release branch"
    sha = _text(ev.get("base_sha"))[:7]
    checks = _lines(ev.get("base_checks"))
    where = f"`{branch}`@`{sha}`" if sha else f"`{branch}`"
    named = ", ".join(f"`{c}`" for c in checks) if checks else "a required check"
    return (
        f"Inherited failure: {where} is ALREADY RED -- {named} is failing on the base "
        f"itself, so this run failed on code this issue did not change. Fix the base; "
        f"this issue is a symptom."
    )


def verification_sentence(ev: dict[str, Any]) -> str:
    """What Final Verification actually failed on: the gate, and the tests."""
    detail = ev.get("verification")
    if not isinstance(detail, dict):
        detail = parse_verification_detail(ev.get("verification_detail"))
    if not isinstance(detail, dict) or not detail.get("gate"):
        return ""
    gate = _text(detail.get("gate"))
    label = _GATE_LABELS.get(gate, gate)
    nodes = _lines(detail.get("failing_tests"))
    total = detail.get("failing_total")
    total = total if isinstance(total, int) and total > 0 else len(nodes)
    if nodes:
        shown = nodes[:FAILING_TEST_LIMIT]
        more = f" (+{total - len(shown)} more)" if total > len(shown) else ""
        return (
            f"Final Verification failed at the {label} gate: {total} failing test(s) -- "
            + ", ".join(f"`{n}`" for n in shown)
            + more
            + "."
        )
    errors = _lines(detail.get("errors"))
    if errors:
        return f"Final Verification failed at the {label} gate: {errors[0]}"
    if total:
        return f"Final Verification failed at the {label} gate: {total} failing test(s)."
    return f"Final Verification failed at the {label} gate."


def red_head_sentence(ev: dict[str, Any]) -> str:
    """Which required check is red on THIS branch's head, and where to read it.

    Distinct from `base_red_sentence`, which is about the release branch: this
    one is the issue's own head, so it is the developer round's work rather
    than an inherited failure (#3971). A ci_red escalation can only mean the
    retry budget ran out, so the sentence names the check both to say what to
    fix and so the cause key can be keyed on it.
    """
    checks = red_head_checks(ev.get("verification_detail"))
    if not checks:
        return ""
    named = ", ".join(f"`{c['name']}`" + (f" ({c['url']})" if c.get("url") else "") for c in checks)
    plural = "checks are" if len(checks) > 1 else "check is"
    return (
        f"Red head: the required {plural} failing on this branch's own head -- {named}. "
        f"The submission was refused because a red head is not reviewable (#3971); "
        f"fixing the check is the developer round's work."
    )


def phase3_crash_sentence(ev: dict[str, Any]) -> str:
    """ "Phase 3 diagnosis did not run" -- said plainly, never implied.

    A Phase 3 that crashed and a Phase 3 that concluded "unknown" used to be
    reported identically, because `claude_result` is gated on
    `claude_analysis.conclusion == 'success'` and a skipped step's outputs are
    empty -- indistinguishable from an empty diagnosis. The escalation then
    presented the absence of a diagnosis as a diagnosis.
    """
    if _text(ev.get("phase3_outcome")).lower() != "failure":
        return ""
    first = _lines(ev.get("phase3_error"))
    tail = (
        first[0]
        if first
        else "the step's log was not available mid-run -- see the failed run's log for the step's own error"
    )
    return f"Phase 3 diagnosis did not run: the deep-analysis step itself failed -- {tail}"


def headline(ev: dict[str, Any]) -> str:
    """The escalation's one-line diagnosis, composed from the evidence.

    Order (the acceptance criterion of #4176): Phase 3's diagnosis, the red
    base, Final Verification's own reason, "Phase 3 could not run", and only
    with none of those the generic sentence.

    The parts COMPOSE rather than short-circuit, with one exception. A red base
    and a list of failing tests are not competing answers -- they are "whose
    fault" and "what broke", and the owner needs both, which is why the
    acceptance run asserts the headline names the red base AND the failing
    tests. The exception is the generic sentence, which is emitted only when
    every other part is empty.
    """
    parts = [
        _text(ev.get("phase3_diagnosis")) or _text(ev.get("phase2_diagnosis")),
        base_red_sentence(ev),
        verification_sentence(ev),
        red_head_sentence(ev),
        phase3_crash_sentence(ev),
    ]
    composed = [p for p in parts if p]
    if composed:
        return " ".join(composed)
    # The last resort, read from the one error-class table (#4179) -- which
    # normalises `verification_failed:<gate>` onto its family and anything
    # unrecognised onto `unknown`, so a class with no row still gets the
    # honest sentence rather than a KeyError.
    return error_classes.explanation(_text(ev.get("error_class")))


def cause_key(ev: dict[str, Any]) -> str:
    """The escalation's cause key: what is broken, not which issue noticed.

    A red base breaks every issue that builds on it, so the key is the BASE --
    one escalation for the base, with the later issues linking to it, which is
    CLAUDE.md's "one systemic cause gets ONE escalation". Keyed on the branch
    and the failing check names rather than the head sha: the fault is "`test`
    is failing on the release branch", and it is the same fault after the next
    commit lands on a branch that is still red.

    Without a red base the key stays what it was -- the failed step -- because
    then the evidence really is specific to this run.
    """
    if ev.get("base_red") is True:
        branch = _text(ev.get("base_branch")) or "release-branch"
        checks = ",".join(_lines(ev.get("base_checks"))) or "unnamed-check"
        return f"base-red:{branch}:{checks}"
    # A red head that survived its retry budget is keyed on the FAILING CHECK,
    # not on the step that noticed it (#4179). Every such failure reports the
    # same step -- "Submit PR for review" -- so the old key collapsed two
    # unrelated broken checks into one cause while splitting one broken check
    # across every issue that hits it. The check name is the thing that is
    # actually failing; the escalation for #4138's `k3s-cloud-smoke` and the
    # next issue's belong together.
    head_checks = red_head_checks(ev.get("verification_detail"))
    if head_checks:
        return "head-red:" + ",".join(c["name"] for c in head_checks)
    step = _text(ev.get("failed_step")) or "unknown-step"
    return f"developer-failure:{step}"


def analysis_phase(ev: dict[str, Any]) -> str:
    """Which phase the headline's evidence came from, for the comment."""
    if _text(ev.get("phase3_diagnosis")):
        return "Phase 3 (Claude reasoning)"
    if _text(ev.get("phase2_diagnosis")):
        return "Phase 2 (Script analysis)"
    if ev.get("base_red") is True:
        return "Phase 1 (base-red finding)"
    if verification_sentence(ev):
        return "Phase 1 (Final Verification's own reason)"
    if red_head_sentence(ev):
        return "Phase 1 (the refused red head)"
    if phase3_crash_sentence(ev):
        return "Phase 3 (did not run)"
    return "Phase 1 (Error classification)"


def compose(ev: dict[str, Any]) -> dict[str, Any]:
    """Everything the escalation steps need, decided in one place."""
    text = headline(ev)
    return {
        "headline": text,
        "cause": cause_key(ev),
        "analysis_phase": analysis_phase(ev),
        "generic": text == ERROR_EXPLANATIONS["unknown"],
    }


def _stdin_json(default: Any) -> Any:
    """Parsed stdin, or `default` when there is nothing parseable there.

    Same reasoning as blast_radius._stdin_json: this runs inside a shell
    pipeline on a failure path, and a traceback here would replace the
    escalation's headline with nothing at all.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def main(argv: list[str]) -> int:
    commands = {
        "headline",
        "compose",
        "cause",
        "verification-detail",
        "parse-detail",
        "red-head-checks",
    }
    if not argv or argv[0] not in commands:
        print(
            "usage: escalation_evidence.py {headline|compose|cause|parse-detail}  "
            "# evidence JSON on stdin\n"
            "       escalation_evidence.py red-head-checks  "
            "# refusal text on stdin, '<name>\\t<url>' lines out\n"
            "       escalation_evidence.py verification-detail <gate> [total]  "
            "# failure lines on stdin",
            file=sys.stderr,
        )
        return 2

    if argv[0] == "red-head-checks":
        # Text in, lines out: the workflow steps that name the failing check
        # in a comment are shell, and a tab-separated pair is what shell can
        # read without a JSON parser.
        for check in red_head_checks(sys.stdin.read()):
            print(f"{check['name']}\t{check.get('url', '')}")
        return 0

    if argv[0] == "verification-detail":
        if len(argv) < 2:
            print("usage: verification-detail <gate> [total]", file=sys.stderr)
            return 2
        total = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        raw = sys.stdin.read()
        nodes = [
            ln.strip()[len("FAILED ") :].strip()
            for ln in raw.splitlines()
            if ln.strip().startswith("FAILED ")
        ]
        others = [
            ln.strip()
            for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("FAILED ")
        ]
        print(format_verification_detail(argv[1], nodes, total, others))
        return 0

    ev = _stdin_json({})
    if not isinstance(ev, dict):
        ev = {}
    if argv[0] == "headline":
        print(headline(ev))
    elif argv[0] == "cause":
        print(cause_key(ev))
    elif argv[0] == "parse-detail":
        print(json.dumps(parse_verification_detail(ev.get("verification_detail"))))
    else:
        print(json.dumps(compose(ev)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
