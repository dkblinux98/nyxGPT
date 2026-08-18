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
  #
  # State matters in the holding lane since #3780: OPEN there is this
  # round's held rework (what the handlers file, and what a gate opening
  # releases), CLOSED is a feature the owner tested, failed and parked --
  # which the gate must leave alone. The lane fixtures below therefore say
  # OPEN explicitly.
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
trap 'rm -f "$STATUS_FILE" "$COMMENT_FILE" "$DISPATCH_FILE"' EXIT

set_issue_status() { echo "$1 -> $2" >>"$STATUS_FILE"; }
issue_comment() { printf '%s :: %s\n' "$1" "$2" >>"$COMMENT_FILE"; }
DISPATCH_FILE="$(mktemp)"
dispatch_next_issue() { printf '%s\n' "${1:-}" >>"$DISPATCH_FILE"; }
sprint_autopilot_paused() { return 1; }

# Bodies and labels of the held issues, for the related-feature ("rework")
# lookup drain_gate_state does. Empty bodies by default: a held issue with
# no marker exempts nothing. Labels default to "Acceptance Failure" -- the
# only label whose marker parks a feature -- so each test states only what
# it is actually varying.
#
# ISSUE_BLOCKS holds the NATIVE relationship (#3731): space-separated issue
# numbers each held issue blocks. Empty by default so the tests that predate
# native edges keep exercising the retired-marker fallback.
declare -A ISSUE_BODIES=()
declare -A ISSUE_LABELS=()
declare -A ISSUE_BLOCKS=()
gh() {
  local ref="$*" num rest n
  num="${ref##*issues/}"
  num="${num%% *}"
  rest="${num#*/}"
  num="${num%%/*}"
  if [[ "$rest" == "dependencies/blocking" ]]; then
    for n in ${ISSUE_BLOCKS[$num]:-}; do echo "$n"; done
    return 0
  fi
  jq -cn --arg b "${ISSUE_BODIES[$num]:-}" --arg l "${ISSUE_LABELS[$num]:-Acceptance Failure}" \
    '{body: $b, labels: ($l | split(",") | map(select(length > 0)))}'
}

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
      "$(_item 3700 "Acceptance Failed" OPEN)"
  else
    _page_response "false" "" \
      "$(_item 3701 "Acceptance Failed" OPEN)" \
      "$(_item 3800 "Backlog" OPEN)"
  fi
}

snapshot="$(acceptance_lane_snapshot)"
_assert_eq "snapshot collects the Acceptance Testing lane across pages" \
  "[3521,3600]" "$(jq -c '.acceptance_testing' <<<"$snapshot")"
_assert_eq "snapshot collects the Acceptance Failed lane across pages" \
  "[3700,3701]" "$(jq -c '.acceptance_failed' <<<"$snapshot")"
_assert_eq "an OPEN holding-lane item is held work, not a parked feature" \
  "[]" "$(jq -c '.acceptance_failed_parked' <<<"$snapshot")"

# --- Test 2: gate stays CLOSED while a non-release item is under test ---
# (single page from here on -- the paging stub above has consumed its
# cursor sequence and each test below asserts on gate behavior, not paging)
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3600 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed" OPEN)" \
    "$(_item 3701 "Acceptance Failed" OPEN)"
}
# The held issues were filed against a DIFFERENT feature, so #3600 really
# is still under test.
ISSUE_BODIES=([3700]="Related feature: #3599" [3701]="an improvement with no related feature")
state="$(drain_gate_state)"
_assert_eq "gate is closed while an unrelated #3600 is still in Acceptance Testing" "false" "$(jq -r '.open' <<<"$state")"
_assert_eq "the blocker is reported" "[3600]" "$(jq -c '.blockers' <<<"$state")"
_assert_eq "an unrelated feature earns no rework exemption" "[]" "$(jq -c '.rework_exempt' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "a closed gate releases nothing" "none" "$(jq -r '.action' <<<"$result")"
_assert_eq "a closed gate moves no issue" "" "$(cat "$STATUS_FILE")"
_assert_eq "a closed gate posts no comment" "" "$(cat "$COMMENT_FILE")"

# --- Test 2b: a feature awaiting rework is NOT a blocker ---
# Without this the gate deadlocks: #3600 parks closed in Acceptance Testing
# until its failure #3700 reaches For Release, #3700 cannot start until the
# gate opens, and the gate waits on #3600. The held Improvement #3701 rides
# along -- it parks nothing itself, but it drains with the batch.
ISSUE_BODIES=([3700]="Related feature: #3600
The upload page 500s." [3701]="Related feature: #3600")
ISSUE_LABELS=([3701]="Improvement")
state="$(drain_gate_state)"
_assert_eq "gate OPENS when the only item under test is a feature awaiting rework" \
  "true" "$(jq -r '.open' <<<"$state")"
_assert_eq "no blockers remain" "[]" "$(jq -c '.blockers' <<<"$state")"
_assert_eq "the awaiting-rework feature is reported as exempt" "[3600]" "$(jq -c '.rework_exempt' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "the deadlocked round releases the failure AND the improvement" "[3700,3701]" "$(jq -c '.released' <<<"$result")"
_assert_not_contains "the parked feature itself is never moved" "$(cat "$STATUS_FILE")" "3600 ->"

# --- Test 2c: a held IMPROVEMENT parks its issue too (#3731) ---
# Owner decision 2026-08-12, superseding 2026-08-01: `@improvement` writes
# the same native blocking relationship as `@acceptance-failure`, so the
# promotion sweep will not move #3600 while a held improvement blocks it.
# Not exempting it would deadlock the gate on its own held work -- the same
# reason failures are exempt. Same filter promote_accepted_features.sh
# applies, so the two sweeps still agree.
ISSUE_BODIES=([3700]="Related feature: #3600" [3701]="Related feature: #3600")
ISSUE_LABELS=([3700]="Improvement" [3701]="Improvement")
state="$(drain_gate_state)"
_assert_eq "gate OPENS when only held improvements block the issue under test" \
  "true" "$(jq -r '.open' <<<"$state")"
_assert_eq "no blockers remain" "[]" "$(jq -c '.blockers' <<<"$state")"
_assert_eq "an improvement earns its issue a rework exemption" "[3600]" "$(jq -c '.rework_exempt' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "improvements-only: the deadlocked round drains" "released" "$(jq -r '.action' <<<"$result")"
_assert_contains "improvements-only: the held improvement moves" "$(cat "$STATUS_FILE")" "3700 -> Backlog"

# --- Test 2d: a held issue with neither handler label parks nothing ---
# (the stub falls back to "Acceptance Failure" for an *empty* value, so the
# labels are set to a third label rather than cleared)
ISSUE_LABELS=([3700]="Feature" [3701]="Feature")
state="$(drain_gate_state)"
_assert_eq "a non-handler-labeled held issue earns no exemption" "false" "$(jq -r '.open' <<<"$state")"
_assert_eq "the issue under test is still a blocker" "[3600]" "$(jq -c '.blockers' <<<"$state")"

# --- Test 2e: the NATIVE relationship wins over a stale body marker ---
# The body still names #99 (a historical marker); the native edge says the
# held issue blocks #3600. The gate must follow the relationship.
ISSUE_BODIES=([3700]="Related feature: #99" [3701]="Related feature: #99")
ISSUE_LABELS=([3700]="Acceptance Failure" [3701]="Acceptance Failure")
ISSUE_BLOCKS=([3700]="3600" [3701]="3600")
state="$(drain_gate_state)"
_assert_eq "the native blocking edge decides the exemption" "[3600]" "$(jq -c '.rework_exempt' <<<"$state")"
_assert_eq "gate opens on the native relationship alone" "true" "$(jq -r '.open' <<<"$state")"

ISSUE_BODIES=()
ISSUE_LABELS=()
ISSUE_BLOCKS=()

# --- Test 3: the release issue is exempt -- gate OPENS with only it left ---
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed" OPEN)" \
    "$(_item 3701 "Acceptance Failed" OPEN)"
}
state="$(drain_gate_state)"
_assert_eq "gate opens when only the release issue remains under test" "true" "$(jq -r '.open' <<<"$state")"
_assert_eq "the release issue is reported as exempt" "[3521]" "$(jq -c '.release_issue_exempt' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "the open gate releases the held lane" "released" "$(jq -r '.action' <<<"$result")"
_assert_eq "both held issues moved" "[3700,3701]" "$(jq -c '.released' <<<"$result")"
_assert_contains "held issue #3700 moved to Backlog" "$(cat "$STATUS_FILE")" "3700 -> Backlog"
_assert_contains "held issue #3701 moved to Backlog" "$(cat "$STATUS_FILE")" "3701 -> Backlog"
_assert_not_contains "the exempt release issue is never moved" "$(cat "$STATUS_FILE")" "3521 -> Backlog"
_assert_eq "the queue was kicked" "true" "$(jq -r '.kicked' <<<"$result")"
kicks="$(grep -c . "$DISPATCH_FILE" || true)"
_assert_eq "the queue is dispatched exactly once for the whole batch" "1" "$kicks"
_assert_contains "the kick lands on the release tracking issue" "$(cat "$COMMENT_FILE")" "3521 ::"

# --- Test 3b: a feature the owner parked in the holding lane (#3780) ---
# Owner decision 2026-08-14: `Acceptance Failed` is also where the owner
# parks features they have tested and failed, "so that I don't get lost as
# to what I've tested that has failed". Those are CLOSED; this round's held
# rework is OPEN. The gate releases the rework and must leave the feature
# exactly where the owner put it -- the 2026-08-14 incident in
# agents/LEDGER.md is what happens when automation rearranges that lane.
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3508 "Acceptance Failed" CLOSED)" \
    "$(_item 3700 "Acceptance Failed" OPEN)"
}
# The held failure blocks the parked feature -- the shape that used to
# deadlock: the feature waits on its failure, the failure waits on the gate.
ISSUE_BLOCKS=([3700]="3508")

snapshot="$(acceptance_lane_snapshot)"
_assert_eq "a CLOSED holding-lane item is reported as an owner-parked feature" \
  "[3508]" "$(jq -c '.acceptance_failed_parked' <<<"$snapshot")"

state="$(drain_gate_state)"
_assert_eq "the gate still opens with a parked feature in the lane" "true" "$(jq -r '.open' <<<"$state")"
_assert_eq "only the OPEN rework counts as held" "[3700]" "$(jq -c '.held' <<<"$state")"
_assert_eq "the parked feature is reported, not held" "[3508]" "$(jq -c '.parked' <<<"$state")"

: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "the release moves the held rework" "[3700]" "$(jq -c '.released' <<<"$result")"
_assert_not_contains "the parked feature is never moved by the gate" \
  "$(cat "$STATUS_FILE")" "3508 ->"
_assert_not_contains "and it is never commented on by the gate" \
  "$(cat "$COMMENT_FILE")" "3508 ::"

# --- Test 3c: a lane holding ONLY parked features releases nothing ---
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3508 "Acceptance Failed" CLOSED)" \
    "$(_item 3596 "Acceptance Failed" CLOSED)"
}
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "a lane of parked features has nothing to release" "none" "$(jq -r '.action' <<<"$result")"
_assert_eq "and nothing moves" "" "$(cat "$STATUS_FILE")"
_assert_eq "and no kick is posted" "" "$(cat "$COMMENT_FILE")"
ISSUE_BLOCKS=()

# --- Test 4: an open gate with an empty holding lane is a no-op ---
graphql() {
  _page_response "false" "" "$(_item 3521 "Acceptance Testing")"
}
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "nothing held -> no action" "none" "$(jq -r '.action' <<<"$result")"
_assert_eq "nothing held -> no kick (idempotent polling)" "" "$(cat "$COMMENT_FILE")"

# --- Test 5: PAUSE_SPRINT suppresses the kick but still releases the lane ---
graphql() {
  _page_response "false" "" \
    "$(_item 3521 "Acceptance Testing")" \
    "$(_item 3700 "Acceptance Failed" OPEN)"
}
sprint_autopilot_paused() { return 0; }
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(drain_gate_release 2>/dev/null)"
_assert_eq "a paused sprint still moves the held issue" "[3700]" "$(jq -c '.released' <<<"$result")"
_assert_eq "a paused sprint posts no kick" "false" "$(jq -r '.kicked' <<<"$result")"
_assert_not_contains "the paused notice never names the kick token" \
  "$(cat "$DISPATCH_FILE")" "drain gate"
_assert_contains "the paused notice carries the informational marker" \
  "$(cat "$COMMENT_FILE")" "nyxgpt-autopilot-informational"
sprint_autopilot_paused() { return 1; }

# --- Test 6: DRY_RUN reports without mutating ---
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
result="$(DRY_RUN=1 drain_gate_release 2>/dev/null)"
_assert_eq "DRY_RUN still reports what it would release" "[3700]" "$(jq -c '.released' <<<"$result")"
_assert_eq "DRY_RUN moves nothing" "" "$(cat "$STATUS_FILE")"
_assert_eq "DRY_RUN posts nothing" "" "$(cat "$COMMENT_FILE")"

# --- Test 7: drain_gate_hold parks an issue in the holding lane ---
: >"$STATUS_FILE"
: >"$COMMENT_FILE"
: >"$DISPATCH_FILE"
drain_gate_hold 3702 "acceptance failure" >/dev/null 2>&1
_assert_contains "hold puts the issue in Acceptance Failed" "$(cat "$STATUS_FILE")" "3702 -> Acceptance Failed"
_assert_contains "hold explains the wait on the issue" "$(cat "$COMMENT_FILE")" "held in **Acceptance Failed**"
_assert_not_contains "holding an issue never kicks the queue" "$(cat "$DISPATCH_FILE")" "drain gate"

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
: >"$DISPATCH_FILE"
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
