#!/usr/bin/env python3
"""The acceptance-status vocabulary -- ONE place answers "is this accepted?"
(#4204, ledger D-066).

Four places asked that question, each from its own independently hard-coded
list of Status values, and `Done` was in NONE of them:

  * `promote_accepted_features.sh`  -- a gate blocker had to be exactly
    `For Release`;
  * `sweep_parked_blocked_issues()` in `gh_project.sh` -- a blocker had to be
    CLOSED *and* `Acceptance Testing` or `For Release`;
  * `drain_gate.py`'s `sprint_unreleased` -- an item counted as still owed by
    the sprint unless it was exactly `For Release`;
  * `sprint_calc.py`'s `sprint_park_state` -- a closed item was
    "awaiting acceptance" unless it was exactly `For Release`.

`Done` is a lane THE LOOP ITSELF WRITES: `scripts/release_ceremony.sh`
Phase 3 sets every closed issue in the released milestone to `Done`. So an
issue that was accepted (`For Release`) and then *released* moved to a status
all four of those read as "not accepted yet" -- and nothing ever moves an
issue back out of `Done`, so anything parked behind it was unpromotable
PERMANENTLY, not merely until the next sweep. The reachable shape: a reopened
original (D-042) parked in `Acceptance Failed` whose blocker is released
under a different milestone, or none.

Two questions are asked of a Status, and they are not the same question:

  `is_accepted`            the owner has ACCEPTED this issue -- it is at or
                           past `For Release`. This is the promotion gate
                           ("has every blocker been accepted?") and the
                           sprint-completion test ("does the sprint still owe
                           anything?").

  `has_reached_acceptance` this issue is merged and has at least REACHED the
                           owner's acceptance queue. This is the
                           parked-blocked sweep's completeness test, the one
                           the runbook has always described as "Acceptance
                           Testing or beyond".

`Acceptance Failed` is deliberately in NEITHER list, even though it is
"beyond" `Acceptance Testing` in board order. The lane holds two populations
(#3780, D-042) and work is still owed on both: held rework whose fix has not
shipped, and an original the owner reopened because it failed acceptance.
Reading it as a completed blocker would promote parked work onto a blocker
the owner rejected. That is why this module exposes two named predicates
rather than an ORDERING over lanes -- the board's lanes are not a total order,
and pretending they are is how `Acceptance Failed` would have silently become
"accepted".

Env vars (all optional; the defaults are the owner's board option names):
  STATUS_ACCEPTANCE_TESTING   default "Acceptance Testing"
  STATUS_FOR_RELEASE          default "For Release"
  STATUS_DONE                 default "Done" -- the lane the release
                               ceremony's Phase 3 close-out writes

CLI, for the shell callers (`gh_project.sh` wraps these):
  acceptance_status.py vocabulary              -> {"accepted": [...],
                                                   "reached_acceptance": [...]}
  acceptance_status.py is-accepted <status>    -> exit 0 if accepted
  acceptance_status.py has-reached-acceptance <status>
"""

from __future__ import annotations

import json
import os
import sys

DEFAULT_ACCEPTANCE_TESTING = "Acceptance Testing"
DEFAULT_FOR_RELEASE = "For Release"
# The release ceremony's own close-out lane (scripts/release_ceremony.sh
# Phase 3). It is an owner-created option that already exists on the board --
# #4204 creates no Status option, it only stops pretending this one is not
# there.
DEFAULT_DONE = "Done"


def _opt(env: str, default: str, overrides: dict[str, str] | None = None) -> str:
    """One Status option name, from an explicit override, then the
    environment, then the owner's board default.

    `overrides` exists for the in-process callers (`sprint_calc.py` is handed
    its status names in its JSON payload, not in the environment) so they ask
    here too instead of keeping a literal of their own.
    """
    if overrides:
        value = (overrides.get(env) or "").strip()
        if value:
            return value
    return (os.getenv(env) or "").strip() or default


def accepted_statuses(overrides: dict[str, str] | None = None) -> tuple[str, ...]:
    """The statuses that mean "the owner has accepted this issue".

    `For Release` is the acceptance itself; `Done` is that same issue after
    the release ceremony closed its milestone out. Both are accepted, and
    `Done` is terminal -- nothing moves an issue back out of it.
    """
    return (
        _opt("STATUS_FOR_RELEASE", DEFAULT_FOR_RELEASE, overrides),
        _opt("STATUS_DONE", DEFAULT_DONE, overrides),
    )


def reached_acceptance_statuses(overrides: dict[str, str] | None = None) -> tuple[str, ...]:
    """The statuses that mean "merged and at least in the acceptance queue".

    `Acceptance Testing` plus everything `accepted_statuses` covers. NOT
    `Acceptance Failed` -- see this module's docstring.
    """
    return (
        _opt("STATUS_ACCEPTANCE_TESTING", DEFAULT_ACCEPTANCE_TESTING, overrides),
    ) + accepted_statuses(overrides)


def is_accepted(status: str | None, overrides: dict[str, str] | None = None) -> bool:
    """True when `status` means the owner has accepted the issue."""
    return (status or "").strip() in accepted_statuses(overrides)


def has_reached_acceptance(status: str | None, overrides: dict[str, str] | None = None) -> bool:
    """True when `status` means the issue is merged and at or past the
    owner's acceptance queue."""
    return (status or "").strip() in reached_acceptance_statuses(overrides)


def vocabulary(overrides: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Both lists at once, for a caller that wants to ask the question itself.

    The shell wrapper in `gh_project.sh` reads this ONCE per run and compares
    in-process, so a sweep over N blockers does not pay N interpreter
    start-ups -- and still has no vocabulary of its own.
    """
    return {
        "accepted": list(accepted_statuses(overrides)),
        "reached_acceptance": list(reached_acceptance_statuses(overrides)),
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: acceptance_status.py "
            "{vocabulary|is-accepted <status>|has-reached-acceptance <status>}",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "vocabulary":
        print(json.dumps(vocabulary()))
        return 0
    if cmd in ("is-accepted", "has-reached-acceptance"):
        if len(argv) < 2:
            print(f"usage: acceptance_status.py {cmd} <status>", file=sys.stderr)
            return 2
        predicate = is_accepted if cmd == "is-accepted" else has_reached_acceptance
        return 0 if predicate(argv[1]) else 1

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
