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
# Idempotent: with the lane already empty it releases nothing and posts
# nothing, so it is safe to poll (acceptance_drain_gate.yml runs it on a
# schedule and after every promotion sweep).
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

# A partial release is a real problem — the lane is half-open and the kick
# has already gone out — so surface it rather than exiting 0 quietly.
failed_count="$(jq -r '(.failed // []) | length' <<<"$result")"
if [[ "$failed_count" != "0" ]]; then
  echo "[drain-gate] $failed_count issue(s) could not be moved to ${STATUS_BACKLOG}" >&2
  exit 1
fi
