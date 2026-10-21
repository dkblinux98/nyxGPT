#!/usr/bin/env bash
set -euo pipefail

# drain_gate.sh — acceptance drain gate watcher (#3730).
#
# Owner rhythm (decision 2026-08-12): test the whole acceptance round
# first, THEN let the agents drain the failures, then test the next
# candidate. Acceptance failures and improvements filed during a round are
# held in the `Acceptance Failed` lane; this script decides when that hold
# is released.
#
# The gate is OPEN when the `Acceptance Testing` lane holds nothing except
# the release tracking issue (exempt — it stays there until the whole
# release is accepted). On the opening, every held item moves to `Backlog`
# for normal scrummaster selection and the queue is kicked exactly once.
#
# Held means the OPEN part of the lane. The CLOSED items there are features
# the owner tested, failed and parked (owner decision 2026-08-14, #3780);
# they are reported as `parked` and never moved by this script.
#
# Idempotent: with the lane already empty it releases nothing and posts
# nothing, so it is safe to run at any time (acceptance_drain_gate.yml runs it
# after every promotion sweep, on a closing issue, and on owner dispatch --
# the 15-minute schedule was removed 2026-08-25, so the end-of-round opening
# is an owner action).
#
# Usage:
#   scripts/agents/drain_gate.sh state     # print gate state JSON, no writes
#   scripts/agents/drain_gate.sh release   # release the lane if the gate is open
#   DRY_RUN=1 scripts/agents/drain_gate.sh release   # report, don't mutate

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/gh_project.sh
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

CMD="${1:-release}"
case "$CMD" in
  -h|--help)
    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  state|release) ;;
  *)
    echo "[error] unknown command: $CMD (expected 'state' or 'release')" >&2
    exit 2
    ;;
esac

load_config
require_gh_auth

if [[ "$CMD" == "state" ]]; then
  drain_gate_state
  exit 0
fi

result="$(drain_gate_release)"
echo "$result"

# A gate opening is a board change the acceptance sweeps care about: the
# lane it opens on is `Acceptance Testing` having drained, and the items it
# leaves parked are exactly the promotion candidates (#3780/D-042). So
# dispatch the sweeps here rather than leaving a cron to find it (#4201).
#
# EDGE-TRIGGERED ON PURPOSE, and this is load-bearing, not an optimization:
# `acceptance_drain_gate.yml` triggers on `workflow_run` completion of both
# sweeps, so dispatching them on every evaluation would be a cycle --
# gate -> sweeps -> gate -> sweeps, the #3790 shape. Dispatching only when
# this run actually RELEASED something breaks it in one hop: the next
# evaluation finds the held lane already empty, releases nothing, and
# dispatches nothing. Evaluation is free of side effects by construction
# (drain_gate_release moves nothing when the gate is shut), which is what
# makes that convergence safe rather than merely likely.
#
# DRY_RUN still populates `.released` (it reports what it would move), so it
# is excluded explicitly: a report that dispatches two real runs is not a
# report.
released_count="$(jq -r '(.released // []) | length' <<<"$result" 2>/dev/null || echo 0)"
if [[ "${DRY_RUN:-0}" != "1" && "$released_count" != "0" ]]; then
  dispatch_acceptance_sweeps "drain gate released ${released_count} issue(s) to ${STATUS_BACKLOG}"
fi

# A partial release is a real problem — the lane is half-open and the kick
# has already gone out — so surface it rather than exiting 0 quietly.
failed_count="$(jq -r '(.failed // []) | length' <<<"$result")"
if [[ "$failed_count" != "0" ]]; then
  echo "[drain-gate] $failed_count issue(s) could not be moved to ${STATUS_BACKLOG}" >&2
  exit 1
fi
