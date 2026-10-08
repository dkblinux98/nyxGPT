"""The one table of developer-failure error classes (#4179).

Owner report, 2026-10-08 (#4138, run 37722699004): `k3s-cloud-smoke` was red on
the head because of a one-line bug in that branch's own smoke script.
`developer_submit_for_review.sh` refused -- "a red head is not reviewable" --
which is #3971's contract that *the developer round continues*, and Phase 1
classified it correctly as `retriable:ci_red`. Phase 2 then ran, returned
FATAL, and the owner was woken with **"Error type: retriable:ci_red ...
Diagnosis: Unrecognized error type."** A class the pipeline itself calls
retriable was escalated as fatal.

Two independent defects produced that, and both are the same mistake: **Phase 2
classified the failure again, from scratch, worse.**

  1. Its harvest ran `gh run view <id> --log` while the run was still in
     progress. That endpoint serves a zip that only exists after the run
     completes and returns 403 mid-run -- the limitation Phase 1's own comment
     already documents. An empty harvest wrote `STATUS=UNKNOWN` and exited 1.
  2. Its `case` had no `ci_red` arm, so even WITH the text the class fell to
     `*)`, which wrote `STATUS=FATAL` and exited 1.

Either way the workflow mapped exit 1 to `fix_status=FATAL` and escalated.

So this module is the table, and it is the only place that decides what a class
does. Three readers that used to each hold their own opinion now read it:

  * `classify_error`'s predicates in `scripts/agents/lib/gh_project.sh`
    (`is_retriable_error` / `is_fatal_error`) -- via `error_class_disposition`.
  * `scripts/agents/developer_analyze_failure.sh` (Phase 2) -- the outcome for
    the class Phase 1 already decided, rather than a second classification.
  * `scripts/agents/lib/escalation_evidence.py` -- the last-resort headline
    explanation, which used to be a second hand-maintained dict.

`tests/unit/test_error_classes.py` enumerates every class `classify_error` can
emit by reading the shell function itself, so a new class added there without
an entry here fails the build -- and asserts that no `retriable` class is left
on the default (`defer`) outcome, which is exactly the hole `ci_red` fell
through.

**Why `defer` is the default and not `fatal`.** "Phase 2 found nothing" is not
a diagnosis. The old default asserted the strongest possible conclusion from
the weakest possible evidence, which is how an absent log became an owner page.
Deferring leaves Phase 1's class standing: a retriable class retries, an
unknown one goes to Phase 3 for a real diagnosis.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# --- Dispositions: what Phase 1's retriable/fatal predicates answer ---------

#: Retry is the defined response. Bounded by the unforgeable retry budget
#: (`retry_budget.py`), never unbounded.
RETRIABLE = "retriable"

#: Retrying would be futile -- the escalation is the correct outcome.
FATAL = "fatal"

#: Neither: the class names a condition whose disposition is not yet known, so
#: it goes to Phase 3 (Claude) for a real diagnosis. `unknown` and
#: `verification_failed:<gate>` are both this, and both are deliberately NOT
#: retriable -- see `developer_auto_implement.yml`'s Phase 3 gate.
DIAGNOSE = "diagnose"

# --- Phase 2 outcomes -------------------------------------------------------

#: Phase 2 asks GitHub a live question (is the issue still closed? has the rate
#: limit reset?) and decides from the answer. The only outcome that needs an
#: API call, and neither case needs the run log.
PHASE2_INVESTIGATE = "investigate"

#: Another developer round on the same branch, with the cause handed to it
#: (#3971's red-head contract). Reported as TRANSIENT to the workflow so the
#: auto-retry step re-assigns the developer agent, which IS the next round.
PHASE2_CONTINUE_ROUND = "continue_round"

#: Retry after `wait_seconds`, without asking anything.
PHASE2_TRANSIENT = "transient"

#: No retry. `explanation` is the diagnosis the escalation carries.
PHASE2_FATAL = "fatal"

#: THE DEFAULT. Phase 2 has nothing to add; Phase 1's class stands. Never an
#: escalation on its own -- see the module docstring.
PHASE2_DEFER = "defer"

#: Phase 2 exit codes, which `developer_auto_implement.yml` maps to
#: `fix_status`. 3 (DEFER) is the one #4179 adds: before it, "nothing to add"
#: and "fatal" shared exit 1.
EXIT_FIXED = 0
EXIT_FATAL = 1
EXIT_TRANSIENT = 2
EXIT_DEFER = 3

#: Phase 2 outcome -> the exit code the script leaves with.
OUTCOME_EXIT = {
    PHASE2_INVESTIGATE: EXIT_DEFER,  # overridden by whatever it finds
    PHASE2_CONTINUE_ROUND: EXIT_TRANSIENT,
    PHASE2_TRANSIENT: EXIT_TRANSIENT,
    PHASE2_FATAL: EXIT_FATAL,
    PHASE2_DEFER: EXIT_DEFER,
}


# --- The table --------------------------------------------------------------
#
# One row per class `classify_error` can emit. `phase2` is what Phase 2 does
# with it; `wait` is the backoff in seconds for the transient ones;
# `explanation` is the human sentence the escalation headline falls back to
# when the run holds no richer evidence.
#
# `verification_failed` is a FAMILY: the shell emits
# `verification_failed:<gate>`, so the key here is the prefix and `normalize`
# maps every member onto it.

ERROR_CLASSES: dict[str, dict[str, Any]] = {
    "fatal:issue_closed": {
        "disposition": FATAL,
        # Fatal, but not blindly: Phase 2's issue_closed arm distinguishes an
        # accidental closure (recent, assigned, no merged PR -> reopen and
        # continue) from a deliberate one. It is reachable only when the
        # script is called directly, since the workflow gates Phase 2 on
        # `retriable == 'true'`; the row records what the code does either way.
        "phase2": PHASE2_INVESTIGATE,
        "wait": 0,
        "explanation": "Issue is closed. This may be intentional or accidental.",
    },
    "fatal:auth_failure": {
        "disposition": FATAL,
        "phase2": PHASE2_FATAL,
        "wait": 0,
        "explanation": (
            "Authentication failed. Requires admin intervention to fix secrets/permissions."
        ),
    },
    "fatal:already_merged": {
        "disposition": FATAL,
        "phase2": PHASE2_FATAL,
        "wait": 0,
        "explanation": "Work already completed in a merged PR.",
    },
    "retriable:rate_limit": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_INVESTIGATE,
        "wait": 0,
        "explanation": "GitHub API rate limit hit. Retry after the limit resets.",
    },
    "retriable:network_timeout": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_TRANSIENT,
        "wait": 30,
        "explanation": "A network call timed out. Retry should resolve it.",
    },
    "retriable:api_overloaded": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_TRANSIENT,
        "wait": 120,
        "explanation": (
            "The API reported 529 Overloaded -- a statement about its capacity, not this run."
        ),
    },
    "retriable:stale_ref": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_TRANSIENT,
        "wait": 0,
        "explanation": "A git ref was stale. The next round fetches and prunes before working.",
    },
    "retriable:ci_red": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_CONTINUE_ROUND,
        "wait": 0,
        "explanation": (
            "A required check is failing on this branch's head, so the submission was "
            "refused (#3971). Fixing it is the developer round's work: the round continues "
            "on the same branch with the failing check named."
        ),
    },
    "retriable:test_failure": {
        "disposition": RETRIABLE,
        "phase2": PHASE2_TRANSIENT,
        "wait": 0,
        "explanation": "Tests failed. The developer round's 3-attempt fix loop owns this.",
    },
    "verification_failed": {
        "disposition": DIAGNOSE,
        # Phase 2 never sees it (the workflow routes it to Phase 3, which is
        # the point of #4176's specific class), and `defer` is the honest row:
        # there is no script outcome for it.
        "phase2": PHASE2_DEFER,
        "wait": 0,
        # The real sentence comes from `escalation_evidence.verification_sentence`,
        # which names the gate and the failing tests. This is the floor.
        "explanation": "Final Verification failed. The gate and the failing tests are below.",
    },
    "unknown": {
        "disposition": DIAGNOSE,
        "phase2": PHASE2_DEFER,
        "wait": 0,
        "explanation": "Error type could not be determined. Manual investigation needed.",
    },
}

#: The class `classify_error` emits when no signature matched. Kept as a name
#: because three modules refer to it.
UNKNOWN = "unknown"

#: The prefix of the `verification_failed:<gate>` family (#4176).
VERIFICATION_FAILED = "verification_failed"


def normalize(name: Any) -> str:
    """The table key for `name`, which may carry a family suffix.

    `verification_failed:pytest` -> `verification_failed`. Everything else is
    returned stripped, and an empty or unrecognised value becomes `unknown` --
    "I was handed nothing" and "no signature matched" are the same state, and
    both must behave like `unknown` rather than like a missing row.
    """
    text = str(name).strip() if name not in (None, "") else ""
    if not text:
        return UNKNOWN
    if text.startswith(VERIFICATION_FAILED + ":") or text == VERIFICATION_FAILED:
        return VERIFICATION_FAILED
    return text if text in ERROR_CLASSES else UNKNOWN


def row(name: Any) -> dict[str, Any]:
    """The table row for `name`, falling back to `unknown`'s."""
    return ERROR_CLASSES[normalize(name)]


def disposition(name: Any) -> str:
    """`retriable` / `fatal` / `diagnose` for `name`."""
    return str(row(name)["disposition"])


def phase2_outcome(name: Any) -> str:
    """What Phase 2 does with `name`."""
    return str(row(name)["phase2"])


def wait_seconds(name: Any) -> int:
    """The backoff Phase 2 records for `name`, in seconds."""
    return int(row(name)["wait"])


def explanation(name: Any) -> str:
    """The human sentence for `name` -- the escalation headline's last resort."""
    return str(row(name)["explanation"])


def exit_code(name: Any) -> int:
    """The exit code Phase 2 leaves with for `name`, before investigation."""
    return OUTCOME_EXIT.get(phase2_outcome(name), EXIT_DEFER)


def gate_of(name: Any) -> str:
    """The gate named by a `verification_failed:<gate>` class, or ""."""
    text = str(name).strip() if name not in (None, "") else ""
    prefix = VERIFICATION_FAILED + ":"
    return text[len(prefix) :].strip() if text.startswith(prefix) else ""


def is_retriable(name: Any) -> bool:
    return disposition(name) == RETRIABLE


def is_fatal(name: Any) -> bool:
    return disposition(name) == FATAL


def retriable_classes() -> list[str]:
    """Every table key whose disposition is `retriable`."""
    return [k for k, v in ERROR_CLASSES.items() if v["disposition"] == RETRIABLE]


# --- Which classes can the shell actually emit? -----------------------------
#
# Read out of `classify_error` itself rather than restated here. A list written
# twice is a list that drifts, and the drift IS this issue: Phase 2's `case`
# was a second enumeration of the same classes, missing one.

#: `echo "<class>"` inside classify_error, including the shell-interpolated
#: `verification_failed:${_gate:-unknown}` form.
_EMIT = re.compile(r'echo\s+"((?:retriable|fatal|verification_failed|unknown)[^"]*)"')


def shell_emitted_classes(gh_project_sh: str | Path) -> list[str]:
    """Every class `classify_error` in `gh_project_sh` can print.

    Scoped to the function body so an `echo "unknown"` elsewhere in a
    4000-line library is not mistaken for a class. Shell interpolation in the
    emitted string (`verification_failed:${_gate:-unknown}`) is folded onto its
    family key, which is what `normalize` does at runtime.
    """
    text = Path(gh_project_sh).read_text(encoding="utf-8")
    start = text.find("classify_error() {")
    if start < 0:
        raise AssertionError(f"classify_error() not found in {gh_project_sh}")
    end = text.find("\n}\n", start)
    body = text[start : end if end > start else len(text)]
    found: list[str] = []
    for raw in _EMIT.findall(body):
        name = VERIFICATION_FAILED if "${" in raw else raw
        if name not in found:
            found.append(name)
    return found


def main(argv: list[str]) -> int:
    commands = {
        "disposition": disposition,
        "phase2": phase2_outcome,
        "wait": wait_seconds,
        "explanation": explanation,
        "exit-code": exit_code,
        "gate": gate_of,
    }
    if argv and argv[0] == "table":
        print(json.dumps(ERROR_CLASSES, indent=2, sort_keys=True))
        return 0
    if argv and argv[0] == "classes":
        print("\n".join(ERROR_CLASSES))
        return 0
    if argv and argv[0] == "row":
        print(json.dumps(row(argv[1] if len(argv) > 1 else "")))
        return 0
    if len(argv) < 2 or argv[0] not in commands:
        print(
            "usage: error_classes.py "
            "{disposition|phase2|wait|explanation|exit-code|gate} <error_class>\n"
            "       error_classes.py {table|classes|row <error_class>}",
            file=sys.stderr,
        )
        return 2
    print(commands[argv[0]](argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
