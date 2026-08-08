#!/usr/bin/env bash
set -uo pipefail

# tests/test_scrummaster_dispatch_next.sh
#
# Standalone regression test for scripts/agents/scrummaster_dispatch_next.sh's
# fall-through dispatch loop (#3665): a single unclaimable Backlog candidate
# (stray assignee, deliberate human hold, etc.) must not halt the whole
# dispatch the way the #3647-era guard did (#3593 stalled the sprint loop
# ~5 days). Sources the real script -- its BASH_SOURCE/$0 guard means
# sourcing only defines _select_next_candidate/scrummaster_dispatch_next,
# it does not load config or hit `gh` -- and stubs both functions so no
# network/gh calls happen.
#
# Usage: bash tests/test_scrummaster_dispatch_next.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAILURES=0

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "[FAIL] $desc: expected '$expected', got '$actual'" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output to contain '$needle', got: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# Sourcing the script only defines its functions (BASH_SOURCE != $0 here),
# so this never loads config or calls `gh`.
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/scrummaster_dispatch_next.sh"

# Parses next_issue=/tried<<EOF...EOF out of scrummaster_dispatch_next's
# $GITHUB_OUTPUT-formatted stdout, the same way the workflow's downstream
# comment steps read `steps.start.outputs.*`.
_parse_next_issue() {
  echo "$1" | sed -n 's/^next_issue=//p'
}
_parse_tried() {
  echo "$1" | sed -n '/^tried<<NYXGPT_TRIED_EOF$/,/^NYXGPT_TRIED_EOF$/p' \
    | sed '1d;$d'
}

# --- Scenario (a): a stale scrummaster self-claim (#3593's actual trigger ---
# --- state) is claimable on the first attempt -- reclaimed and started, ---
# --- no fall-through needed ---
_select_next_candidate() { echo "3593"; }
scrummaster_attempt_start() {
  [[ "$1" == "3593" ]] || { echo "[test] unexpected issue $1" >&2; return 1; }
  echo "STARTED #3593"
  return 0
}

OUT="$(scrummaster_dispatch_next "0" 2>/dev/null)"
_assert_eq "(a) stale self-claim: started issue is reported" "3593" "$(_parse_next_issue "$OUT")"
_assert_eq "(a) stale self-claim: no candidates were skipped" "" "$(_parse_tried "$OUT")"

# --- Scenario (b): the first candidate is a duplicate in-flight start ---
# --- (already assigned to the dev agent) -- falls through and starts the ---
# --- next eligible candidate instead of ending the dispatch. (Selection
# --- runs inside a `$(...)` subshell in scrummaster_dispatch_next, so a
# --- plain array accumulated in this stub wouldn't be visible back here --
# --- the exclude-propagation itself is what makes "81"'s case return "82"
# --- at all, and is exercised more strongly by scenario (c) below.) ---
_select_next_candidate() {
  case "$1" in
    "") echo "81" ;;
    "81") echo "82" ;;
    *) echo "" ;;
  esac
}
scrummaster_attempt_start() {
  case "$1" in
    81)
      echo "SKIPPED #81 reason=duplicate"
      return 10
      ;;
    82)
      echo "STARTED #82"
      return 0
      ;;
    *)
      echo "[test] unexpected issue $1" >&2
      return 1
      ;;
  esac
}

OUT="$(scrummaster_dispatch_next "0" 2>/dev/null)"
_assert_eq "(b) dev-assigned candidate: next candidate is started" "82" "$(_parse_next_issue "$OUT")"
_assert_contains "(b) dev-assigned candidate: the skipped candidate is recorded" \
  "$(_parse_tried "$OUT")" "SKIPPED #81 reason=duplicate"

# --- Scenario (c): every eligible candidate is unclaimable -- the dispatch ---
# --- starts nothing and reports every blocking issue and reason, instead ---
# --- of a silent "success" (the #3593 failure mode) ---
_select_next_candidate() {
  case "$1" in
    "") echo "90" ;;
    "90") echo "91" ;;
    *) echo "" ;;
  esac
}
scrummaster_attempt_start() {
  case "$1" in
    90)
      echo "SKIPPED #90 reason=anomaly assignee=myGPT-review-agent"
      return 11
      ;;
    91)
      echo "SKIPPED #91 reason=human_hold"
      return 10
      ;;
    *)
      echo "[test] unexpected issue $1" >&2
      return 1
      ;;
  esac
}

OUT="$(scrummaster_dispatch_next "0" 2>/dev/null)"
_assert_eq "(c) all candidates blocked: nothing started" "" "$(_parse_next_issue "$OUT")"
TRIED="$(_parse_tried "$OUT")"
_assert_contains "(c) all candidates blocked: the anomaly is reported" "$TRIED" "SKIPPED #90 reason=anomaly assignee=myGPT-review-agent"
_assert_contains "(c) all candidates blocked: the human hold is reported" "$TRIED" "SKIPPED #91 reason=human_hold"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
