#!/usr/bin/env bash
set -uo pipefail

# tests/test_drain_gate_lib.sh
# Tests for the acceptance drain gate (#3730) helpers in
# scripts/agents/lib/gh_project.sh: acceptance_lane_snapshot,
# drain_gate_state, drain_gate_hold, drain_gate_release and
# issue_bypasses_drain_gate. `graphql`/`gh` are stubbed so no network calls
# happen; the real drain_gate.py subprocess still runs end to end.
#
# Usage: bash tests/test_drain_gate_lib.sh

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
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' unexpectedly found in: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

# shellcheck disable=SC2034
REPO_OWNER="test-owner"
# shellcheck disable=SC2034
REPO_NAME="test-repo"
STATUS_FIELD="Status"
STATUS_BACKLOG="Backlog"
STATUS_ACCEPTANCE_TESTING="Acceptance Testing"
STATUS_ACCEPTANCE_FAILED="Acceptance Failed"
RELEASE_ISSUE_NUMBER="3521"

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

get_project_id() { echo "proj-1"; }

_item() {
  # $1=number $2=status $3=state (default CLOSED)
  local number="$1" status="$2" state="${3:-CLOSED}"
  cat <<EOF
{"content":{"__typename":"Issue","number":${number},"state":"${state}"},"fieldValues":{"nodes":[{"__typename":"ProjectV2ItemFieldSingleSelectValue","field":{"name":"Status"},"name":"${status}"}]}}
EOF
}

_page_response() {
  # $1=has_next $2=cursor $3...=items
  local has_next="$1" cursor="$2"
  shift 2
  local items
  items="$(IFS=,; echo "$*")"
  cat <<EOF
{"data":{"node":{"items":{"pageInfo":{"hasNextPage":${has_next},"endCursor":"${cursor}"},"nodes":[${items}]}}}}
EOF
}

# Side-effect recorders. drain_gate_release's own output comes back through
# command substitution (a subshell), so stubs record into temp files rather
# than shell variables -- same pitfall documented in test_gh_project_lib.sh.
STATUS_FILE="$(mktemp)"
COMMENT_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE" "$COMMENT_FILE"' EXIT

set_issue_status() { echo "$1 -> $2" >>"$STATUS_FILE"; }
issue_comment() { printf '%s :: %s\n' "$1" "$2" >>"$COMMENT_FILE"; }
sprint_autopilot_paused() { return 1; }

# --- Test 1: acceptance_lane_snapshot buckets both lanes across pages ---
GRAPHQL_CALLS_FILE="$(mktemp)"
echo 0 >"$GRAPHQL_CALLS_FILE"
_graphql_calls() { cat "$GRAPHQL_CALLS_FILE"; }
graphql() {
  echo "$(($(_graphql_calls) + 1))" >"$GRAPHQL_CALLS_FILE"
  if [[ "$(_graphql_calls)" -eq 1 ]]; then
    _page_response "true" "cursor-1" \
      "$(_item 3521 "Acceptance Testing")" \
      "$(_item 3600 "Acceptance Testing")" \
      "$(_item 3700 "Acceptance Failed")"
  else
    _page_response "false" "" \
      "$(_item 3701 "Acceptance Failed")" \
      "$(_item 3800 "Backlog" OPEN)"
  fi
}

snapshot="$(acceptance_lane_snapshot)"
_assert_eq "snapshot collects the Acceptance Testing lane across pages" \
  "[3521,3600]" "$(jq -c '.acceptance_testing' <<<"$snapshot")"
_assert_eq "snapshot collects the Acceptance Failed lane across pages" \
  "[3700,3701]" "$(jq -c '.acceptance_failed' <<<"$snapshot")"

# --- Test 2: gate stays CLOSED while a non-release item is under test ---
# (single page from here on -- the paging stub above has consumed its
# cursor sequence and each test below asserts on gate behavior, not paging)
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3600 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed")" \
    "$(_item 3701 "Acceptance Failed")"
}
state="$(drain_gate_state)"
_assert_eq "gate is closed while #3600 is still in Acceptance Testing" "false" "$(jq -r '.open' <<<"$state")"
_assert_eq "the blocker is reported" "[3600]" "$(jq -c '.blockers' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "a closed gate releases nothing" "none" "$(jq -r '.action' <<<"$result")"
_assert_eq "a closed gate moves no issue" "" "$(cat "$STATUS_FILE")"
_assert_eq "a closed gate posts no comment" "" "$(cat "$COMMENT_FILE")"

# --- Test 3: the release issue is exempt -- gate OPENS with only it left ---
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed")" \
    "$(_item 3701 "Acceptance Failed")"
}
state="$(drain_gate_state)"
_assert_eq "gate opens when only the release issue remains under test" "true" "$(jq -r '.open' <<<"$state")"
_assert_eq "the release issue is reported as exempt" "[3521]" "$(jq -c '.release_issue_exempt' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "the open gate releases the held lane" "released" "$(jq -r '.action' <<<"$result")"
_assert_eq "both held issues moved" "[3700,3701]" "$(jq -c '.released' <<<"$result")"
_assert_contains "held issue #3700 moved to Backlog" "$(cat "$STATUS_FILE")" "3700 -> Backlog"
_assert_contains "held issue #3701 moved to Backlog" "$(cat "$STATUS_FILE")" "3701 -> Backlog"
_assert_not_contains "the exempt release issue is never moved" "$(cat "$STATUS_FILE")" "3521 -> Backlog"
_assert_eq "the queue was kicked" "true" "$(jq -r '.kicked' <<<"$result")"
kicks="$(grep -c "READY_FOR_NEXT_ISSUE" "$COMMENT_FILE")"
_assert_eq "the queue is kicked exactly once for the whole batch" "1" "$kicks"
_assert_contains "the kick lands on the release tracking issue" "$(cat "$COMMENT_FILE")" "3521 ::"

# --- Test 4: an open gate with an empty holding lane is a no-op ---
graphql() {
  _page_response "false" "" "$(_item 3521 "Acceptance Testing")"
}
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "nothing held -> no action" "none" "$(jq -r '.action' <<<"$result")"
_assert_eq "nothing held -> no kick (idempotent polling)" "" "$(cat "$COMMENT_FILE")"

# --- Test 5: PAUSE_SPRINT suppresses the kick but still releases the lane ---
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed")"
}
sprint_autopilot_paused() { return 0; }
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "a paused sprint still moves the held issue" "[3700]" "$(jq -c '.released' <<<"$result")"
_assert_eq "a paused sprint posts no kick" "false" "$(jq -r '.kicked' <<<"$result")"
_assert_not_contains "the paused notice never names the kick token" \
  "$(grep 3521 "$COMMENT_FILE")" "READY_FOR_NEXT_ISSUE"
_assert_contains "the paused notice carries the informational marker" \
  "$(cat "$COMMENT_FILE")" "nyxgpt-autopilot-informational"
sprint_autopilot_paused() { return 1; }

# --- Test 6: DRY_RUN reports without mutating ---
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
result="$(DRY_RUN=1 drain_gate_release 2>/dev/null)"
_assert_eq "DRY_RUN still reports what it would release" "[3700]" "$(jq -c '.released' <<<"$result")"
_assert_eq "DRY_RUN moves nothing" "" "$(cat "$STATUS_FILE")"
_assert_eq "DRY_RUN posts nothing" "" "$(cat "$COMMENT_FILE")"

# --- Test 7: drain_gate_hold parks an issue in the holding lane ---
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
drain_gate_hold 3702 "acceptance failure" >/dev/null 2>&1
_assert_contains "hold puts the issue in Acceptance Failed" "$(cat "$STATUS_FILE")" "3702 -> Acceptance Failed"
_assert_contains "hold explains the wait on the issue" "$(cat "$COMMENT_FILE")" "held in **Acceptance Failed**"
_assert_not_contains "holding an issue never kicks the queue" "$(cat "$COMMENT_FILE")" "READY_FOR_NEXT_ISSUE"

# --- Test 8: process issues bypass the gate; acceptance work does not ---
gh() { echo "$MOCK_ISSUE_JSON"; }
MOCK_ISSUE_JSON='{"title":"feat: drain-gated failure processing","body":"## Process exception\nThis issue bypasses the drain gate it implements.","labels":[{"name":"Improvement"}]}'
if issue_bypasses_drain_gate 3730; then
  echo "[ok] an owner-declared process exception bypasses the gate"
else
  echo "[FAIL] the process exception on #3730 should bypass the gate" >&2
  FAILURES=$((FAILURES + 1))
fi

MOCK_ISSUE_JSON='{"title":"bug: acceptance failure 1 for #3600","body":"Related feature: #3600","labels":[{"name":"Acceptance Failure"}]}'
if issue_bypasses_drain_gate 3700; then
  echo "[FAIL] an ordinary acceptance failure must NOT bypass the gate" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] an ordinary acceptance failure is gated"
fi

# --- Test 9: a held issue can never be STARTED while it is in the lane ---
# (the gate is enforced at the start point, not just by the Backlog filter)
SCRUM_AGENT="myGPT-scrummaster-agent"
DEV_AGENT="myGPT-developer-agent"
HUMAN_OWNER="dkblinux98"
gh() { echo "OPEN"; }
_issue_assignee_logins() { echo ""; }

issue_status() { echo "Acceptance Failed"; }
_assert_eq "an issue in the holding lane classifies as drain_gate_held" \
  "drain_gate_held" "$(classify_backlog_claim_state 3700)"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
# `|| rc=$?` rather than a bare call: sourcing gh_project.sh turns on
# `set -e`, so an unguarded non-zero return would end the test run here.
rc=0
out="$(scrummaster_attempt_start 3700)" || rc=$?
_assert_eq "starting a held issue is a quiet skip" "10" "$rc"
_assert_contains "the skip names the drain gate" "$out" "reason=drain_gate_held"
_assert_eq "a held issue is never moved to In Progress" "" "$(cat "$STATUS_FILE")"

issue_status() { echo "Backlog"; }
_assert_eq "a Backlog issue is still claimable once released" \
  "claimable" "$(classify_backlog_claim_state 3700)"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
