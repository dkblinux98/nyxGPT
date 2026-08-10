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

# Default stub: dispatch is not paused. Scenarios (a)-(c) below exercise
# the fall-through loop itself and don't care about the #3687
# escalation-pause backstop or the #3694 cross-issue-anomaly backstop, so
# they rely on these defaults. Scenario (d) overrides the escalation stub
# and scenario (e) overrides the anomaly stub to exercise each paused path.
_escalation_pause_check() { return 0; }
_cross_issue_anomaly_check() { return 0; }

# Parses paused=/pause_reason=/next_issue=/tried<<EOF...EOF out of
# scrummaster_dispatch_next's $GITHUB_OUTPUT-formatted stdout, the same way
# the workflow's downstream comment steps read `steps.start.outputs.*`.
_parse_paused() {
  echo "$1" | sed -n 's/^paused=//p'
}
_parse_pause_reason() {
  echo "$1" | sed -n 's/^pause_reason=//p'
}
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
_assert_eq "(c) all candidates blocked: not reported as paused" "false" "$(_parse_paused "$OUT")"
TRIED="$(_parse_tried "$OUT")"
_assert_contains "(c) all candidates blocked: the anomaly is reported" "$TRIED" "SKIPPED #90 reason=anomaly assignee=myGPT-review-agent"
_assert_contains "(c) all candidates blocked: the human hold is reported" "$TRIED" "SKIPPED #91 reason=human_hold"

# --- Scenario (d): the #3687 unresolved-escalation pause backstop is ---
# --- tripped (>=2 unresolved escalations) -- dispatch must not even ---
# --- attempt candidate selection, and must report paused=true with an ---
# --- empty next_issue/tried so the workflow's completion/no-issues/ ---
# --- queue-blocked comments all stay silent (a dedicated pause comment ---
# --- covers it instead) ---
_escalation_pause_check() { return 1; }
_select_next_candidate() {
  echo "[test] _select_next_candidate must not run while paused" >&2
  return 1
}
scrummaster_attempt_start() {
  echo "[test] scrummaster_attempt_start must not run while paused" >&2
  return 1
}

OUT="$(scrummaster_dispatch_next "0" 2>/dev/null)"
_assert_eq "(d) escalation-paused: reported as paused" "true" "$(_parse_paused "$OUT")"
_assert_eq "(d) escalation-paused: pause_reason is escalation" "escalation" "$(_parse_pause_reason "$OUT")"
_assert_eq "(d) escalation-paused: nothing started" "" "$(_parse_next_issue "$OUT")"
_assert_eq "(d) escalation-paused: nothing recorded as tried" "" "$(_parse_tried "$OUT")"

# Restore the default stub before scenario (e) so only the anomaly gate is
# exercised there.
_escalation_pause_check() { return 0; }

# --- Scenario (e): the #3694 cross-issue infrastructure-anomaly pause ---
# --- backstop is tripped -- dispatch must not attempt candidate selection ---
# --- either, and must report paused=true with pause_reason=cross_issue_anomaly ---
# --- so the workflow's comment step picks the right message ---
_cross_issue_anomaly_check() { return 1; }
_select_next_candidate() {
  echo "[test] _select_next_candidate must not run while anomaly-paused" >&2
  return 1
}
scrummaster_attempt_start() {
  echo "[test] scrummaster_attempt_start must not run while anomaly-paused" >&2
  return 1
}

OUT="$(scrummaster_dispatch_next "0" 2>/dev/null)"
_assert_eq "(e) anomaly-paused: reported as paused" "true" "$(_parse_paused "$OUT")"
_assert_eq "(e) anomaly-paused: pause_reason is cross_issue_anomaly" "cross_issue_anomaly" "$(_parse_pause_reason "$OUT")"
_assert_eq "(e) anomaly-paused: nothing started" "" "$(_parse_next_issue "$OUT")"
_assert_eq "(e) anomaly-paused: nothing recorded as tried" "" "$(_parse_tried "$OUT")"

# Restore the default stubs so the #3695 scenarios below only exercise the
# block they target, without re-triggering (d)/(e)'s "must not run" stubs.
_escalation_pause_check() { return 0; }
_cross_issue_anomaly_check() { return 0; }

# --- Scenario (f): #3695 human-channel notification -- the escalation- ---
# --- pause backstop (d) must fire _notify_dispatch_block with state ---
# --- "dispatch-paused", targeted at RELEASE_ISSUE_NUMBER (the same ---
# --- dispatch-wide target sprint_autopilot_kick/escalation_pause_gate ---
# --- already report to). No RELEASE_ISSUE_NUMBER configured -> silently ---
# --- skipped (same conservative default as escalation_pause_gate). ---
NOTIFY_CALLS=()
notify_human_escalation() { NOTIFY_CALLS+=("$*"); }

_escalation_pause_check() { return 1; }
RELEASE_ISSUE_NUMBER=""
scrummaster_dispatch_next "0" >/dev/null 2>&1
_assert_eq "(f) paused, no RELEASE_ISSUE_NUMBER: no Slack notification attempted" \
  "0" "${#NOTIFY_CALLS[@]}"

NOTIFY_CALLS=()
RELEASE_ISSUE_NUMBER="3521"
scrummaster_dispatch_next "0" >/dev/null 2>&1
_assert_eq "(f) paused, with RELEASE_ISSUE_NUMBER: exactly one Slack notification" \
  "1" "${#NOTIFY_CALLS[@]}"
_assert_eq "(f) paused: notification targets the release tracking issue with state dispatch-paused" \
  "3521 dispatch-paused" "$(echo "${NOTIFY_CALLS[0]}" | cut -d' ' -f1-2)"

# The #3694 anomaly-pause backstop notifies too, with its own state (so the
# message names the actual cause and the two backstops never de-duplicate
# against each other).
_escalation_pause_check() { return 0; }
_cross_issue_anomaly_check() { return 1; }
NOTIFY_CALLS=()
scrummaster_dispatch_next "0" >/dev/null 2>&1
_assert_eq "(f) anomaly-paused, with RELEASE_ISSUE_NUMBER: exactly one Slack notification" \
  "1" "${#NOTIFY_CALLS[@]}"
_assert_eq "(f) anomaly-paused: notification targets the release tracking issue with state anomaly-paused" \
  "3521 anomaly-paused" "$(echo "${NOTIFY_CALLS[0]}" | cut -d' ' -f1-2)"
_cross_issue_anomaly_check() { return 0; }

# --- Scenario (g): #3695 queue-blocked (scenario (c)'s all-unclaimable ---
# --- case) also fires _notify_dispatch_block, with state "queue-blocked" ---
_escalation_pause_check() { return 0; }
_select_next_candidate() {
  case "$1" in
    "") echo "90" ;;
    "90") echo "91" ;;
    *) echo "" ;;
  esac
}
scrummaster_attempt_start() {
  case "$1" in
    90) echo "SKIPPED #90 reason=anomaly assignee=myGPT-review-agent"; return 11 ;;
    91) echo "SKIPPED #91 reason=human_hold"; return 10 ;;
    *) echo "[test] unexpected issue $1" >&2; return 1 ;;
  esac
}

NOTIFY_CALLS=()
RELEASE_ISSUE_NUMBER="3521"
scrummaster_dispatch_next "0" >/dev/null 2>&1
_assert_eq "(g) queue-blocked: exactly one Slack notification" "1" "${#NOTIFY_CALLS[@]}"
_assert_eq "(g) queue-blocked: notification targets the release tracking issue with state queue-blocked" \
  "3521 queue-blocked" "$(echo "${NOTIFY_CALLS[0]}" | cut -d' ' -f1-2)"

# Started successfully (scenario (a) shape) -- no block, no notification.
NOTIFY_CALLS=()
_select_next_candidate() { echo "3593"; }
scrummaster_attempt_start() { echo "STARTED #3593"; return 0; }
scrummaster_dispatch_next "0" >/dev/null 2>&1
_assert_eq "(g) normal start: no Slack notification attempted" "0" "${#NOTIFY_CALLS[@]}"

unset -f notify_human_escalation
RELEASE_ISSUE_NUMBER=""

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
