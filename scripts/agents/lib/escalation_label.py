"""The `Escalation` label -- what "handed to the owner" is RECORDED as (#4134).

Before this, escalation was not a step. About a dozen call sites handed work
to the owner and no two agreed: some assigned the owner and sent the Slack DM,
some only sent the DM, some only assigned. There was therefore nothing to
read, so the dispatch pause (#3687) *inferred* escalation from "open, assigned
to the owner, not in an exempt lane" -- and that guess was wrong twice (#3868,
the release tracking issue counting forever; 2026-08-19, normal merges
tripping it and the queue sitting idle ~10 hours), with a third gap still open
(an issue the owner assigns to themselves in Backlog or In Progress).

The owner created the `Escalation` label so the state is written down instead
of guessed. Owner decisions, 2026-10-03:

* an escalation **replaces** the issue's single label with `Escalation` (the
  one-label rule still holds), assigns the owner, and sends the Slack DM;
* the issue **stays in its current Status lane** -- the owner tracks
  escalations on their own board, and a lane move would destroy the signal of
  where the work actually was;
* **the owner restores the original label themselves.** Automation must never
  put it back, and must never remove `Escalation`.

That last rule is why the replaced label is RECORDED rather than remembered:
the escalation comment carries `<!-- escalation-replaced-label: … -->` so the
owner can see what to restore, and so the consumers that read an issue's type
label (`acceptance_role` in drain_gate.py, the promotion sweep, the
retrospective) can still tell an escalated Acceptance Failure from an
escalated Feature. Without it, escalating an acceptance failure would silently
reclassify it: `acceptance_role` would stop seeing a rework label, the drain
gate could release a parked original, and the promotion sweep could promote
and close an issue whose fix never shipped.

`effective_labels` is that translation, in one place, so no consumer invents
its own.

One cause, one escalation
-------------------------
The retired count-of-2 pause was really protecting against escalations piling
up overnight. The actual defect it was aimed at is that agents escalate from
the tunnel vision of a single issue: one root cause surfaces as several
separate escalations, and an agent working another issue rediscovers it from
scratch. So an escalation carries a **cause key**, and the first escalation for
a cause registers `<!-- escalation-cause: … -->` on the release tracking issue.
A later issue hitting the same cause links to that origin instead of repeating
the diagnosis and the DM -- the same collapse #3694 applies to cross-issue
infrastructure anomalies, and deliberately the same marker-on-the-release-issue
shape, re-derived from the live thread on every check so there is no hidden
counter to drift.

A cause stays open while its origin issue still carries `Escalation`. That
needs no resolve command and no automation that touches the label: the owner
removing `Escalation` (restoring the real label) IS the resolution.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from typing import Any

#: The owner-created label. Agents never create, edit or delete labels
#: (CLAUDE.md §Tooling); this name is assumed to exist and every write that
#: needs it fails loudly if it does not.
ESCALATION_LABEL = "Escalation"

#: Records the label the escalation replaced, so the owner knows what to
#: restore and so type-label consumers can still read the issue's real type.
REPLACED_MARKER_PREFIX = "<!-- escalation-replaced-label: "
_REPLACED_RE = re.compile(r"<!--\s*escalation-replaced-label:\s*(?P<label>.*?)\s*-->")

#: Registers the FIRST escalation for a cause, on the release tracking issue.
CAUSE_MARKER_PREFIX = "<!-- escalation-cause: "
_CAUSE_RE = re.compile(
    r"<!--\s*escalation-cause:\s*(?P<cause>[^\s]+)\s+origin=(?P<origin>\d+)\s*-->"
)


def label_names(labels: Iterable[Any] | None) -> list[str]:
    """Label names from either payload shape GitHub hands us.

    REST/webhook payloads carry objects (`[{"name": "Escalation"}]`); the
    GraphQL project query and several `gh --jq` call sites flatten them to
    plain strings. Callers should not have to care which one they got -- the
    same tolerance `support_label.label_names` provides, duplicated rather
    than imported so neither module depends on the other's import path (these
    run as scripts from `_LIB_DIR`, not as a package).
    """
    names: list[str] = []
    for label in labels or []:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def is_escalated(labels: Iterable[Any] | None) -> bool:
    """True when this issue is currently escalated to the owner.

    An escalated issue is never dispatched, resumed, auto-kicked, released by
    the drain gate or submitted for review: the owner holds it, and the label
    is the only thing that says so.
    """
    return ESCALATION_LABEL in label_names(labels)


def replaced_marker(label: str) -> str:
    """The machine record of the label an escalation replaced."""
    return f"{REPLACED_MARKER_PREFIX}{label} -->"


def replaced_label(comment_bodies: Iterable[str] | None) -> str:
    """The label the most recent escalation on this issue replaced, or "".

    Comments are read in the order given (chronological, as GitHub returns
    them), and the LAST marker wins: an issue escalated twice reports the
    label the second escalation took away, which is the one the owner has yet
    to restore.
    """
    found = ""
    for body in comment_bodies or []:
        for match in _REPLACED_RE.finditer(body or ""):
            label = match.group("label").strip()
            if label:
                found = label
    return found


def effective_labels(labels: Iterable[Any] | None, replaced: str | None) -> list[str]:
    """The issue's labels as a TYPE consumer should read them.

    `Escalation` is a state, not a type. Substituting the recorded prior label
    for it is what keeps every reader of the type label -- `acceptance_role`,
    `promote_accepted_features.sh`, the retrospective -- correct across an
    escalation, without any automation restoring the label on the issue (which
    the owner forbade).

    With nothing recorded, `Escalation` is simply dropped: better that a
    consumer sees "no type label" and treats the issue as untyped than that it
    reads the state as a type.
    """
    names = [n for n in label_names(labels) if n != ESCALATION_LABEL]
    if replaced and replaced not in names:
        names.append(replaced)
    return names


def cause_marker(cause: str, origin: int) -> str:
    """The registry marker for the first escalation of `cause`."""
    return f"{CAUSE_MARKER_PREFIX}{cause} origin={int(origin)} -->"


def cause_origin(comment_bodies: Iterable[str] | None, cause: str) -> int | None:
    """The issue that first escalated `cause`, from the registry thread.

    The LAST matching marker wins, so a cause the owner resolved and that
    later recurred points at the recurrence rather than at the history. The
    caller still has to check that the origin issue *currently* carries
    `Escalation` -- a registry entry alone does not mean the cause is open,
    and treating it as if it did would suppress a real escalation forever.
    """
    found: int | None = None
    for body in comment_bodies or []:
        for match in _CAUSE_RE.finditer(body or ""):
            if match.group("cause") == cause:
                found = int(match.group("origin"))
    return found


def _usage() -> int:
    print(
        "usage: escalation_label.py "
        "{is-escalated|replaced-label|effective-labels|replaced-marker <label>"
        "|cause-marker <cause> <origin>|cause-origin <cause>}",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    if not argv:
        return _usage()
    cmd = argv[0]

    if cmd == "is-escalated":
        # stdin: the issue's `.labels` (objects or strings).
        print("true" if is_escalated(json.load(sys.stdin)) else "false")
        return 0
    if cmd == "replaced-label":
        # stdin: JSON array of comment bodies, chronological.
        print(replaced_label(json.load(sys.stdin)))
        return 0
    if cmd == "effective-labels":
        # stdin: {"labels": [...], "comments": ["body", ...]} -> JSON array of
        # names. One call does the whole translation so the shell never has to
        # know the substitution rule.
        payload = json.load(sys.stdin)
        print(
            json.dumps(
                effective_labels(payload.get("labels"), replaced_label(payload.get("comments")))
            )
        )
        return 0
    if cmd == "replaced-marker":
        if len(argv) < 2:
            return _usage()
        print(replaced_marker(argv[1]))
        return 0
    if cmd == "cause-marker":
        if len(argv) < 3 or not argv[2].isdigit():
            return _usage()
        print(cause_marker(argv[1], int(argv[2])))
        return 0
    if cmd == "cause-origin":
        if len(argv) < 2:
            return _usage()
        # stdin: JSON array of the release issue's comment bodies.
        origin = cause_origin(json.load(sys.stdin), argv[1])
        print("" if origin is None else origin)
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
