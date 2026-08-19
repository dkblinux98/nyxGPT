#!/usr/bin/env bash
set -uo pipefail

# tests/test_gh_project_lib.sh
# Standalone regression test for scripts/agents/lib/gh_project.sh's
# set_field_with_retry(): retry count, backoff, and final-failure
# propagation. Sources the real library and stubs set_project_field_value
# so no network/gh calls happen.
#
# Usage: bash tests/test_gh_project_lib.sh

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

# Load the library. It only defines functions at source time, so this is safe.
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

# Stub out sleep so the retry backoff doesn't actually slow the test down,
# and record every backoff duration it was called with.
SLEEP_CALLS=()
sleep() { SLEEP_CALLS+=("$1"); }

# --- Test 1: always-failing field set exhausts all attempts and propagates failure ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  return 1
}

SLEEP_CALLS=()
if set_field_with_retry "item-1" "Status" "In Review" 3 2>/dev/null; then
  echo "[FAIL] set_field_with_retry should have returned failure when all attempts fail" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] set_field_with_retry returns failure after exhausting attempts"
fi
_assert_eq "call count equals configured attempts" "3" "$CALL_COUNT"
_assert_eq "backoff sleeps once per failed attempt" "3" "${#SLEEP_CALLS[@]}"
_assert_eq "backoff duration grows with attempt number" "2 4 6" "${SLEEP_CALLS[*]}"

# --- Test 2: field set succeeds on the final attempt ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  [[ "$CALL_COUNT" -ge 3 ]]
}

SLEEP_CALLS=()
if set_field_with_retry "item-2" "Status" "In Review" 3 2>/dev/null; then
  echo "[ok] set_field_with_retry returns success once a later attempt succeeds"
else
  echo "[FAIL] set_field_with_retry should have returned success on the 3rd attempt" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "stops retrying once an attempt succeeds" "3" "$CALL_COUNT"
_assert_eq "no backoff sleep after the final (successful) attempt" "2 4" "${SLEEP_CALLS[*]}"

# --- Test 3: field set succeeds on the first attempt (no retries needed) ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  return 0
}

SLEEP_CALLS=()
set_field_with_retry "item-3" "Status" "In Review" 3 2>/dev/null
_assert_eq "no retries when the first attempt succeeds" "1" "$CALL_COUNT"
_assert_eq "no backoff sleep when the first attempt succeeds" "0" "${#SLEEP_CALLS[@]}"

# assign_issue_verified's caller may capture its output via `$(...)` command
# substitution (as Test 6 does below), which runs in a subshell — plain
# variable increments inside stubbed issue_assign_only/_issue_assignee_logins
# wouldn't be visible to the parent shell in that case. Use temp-file
# counters instead, which survive the subshell.
ASSIGN_COUNT_FILE="$(mktemp)"
VERIFY_COUNT_FILE="$(mktemp)"
trap 'rm -f "$ASSIGN_COUNT_FILE" "$VERIFY_COUNT_FILE"' EXIT
_assign_calls() { cat "$ASSIGN_COUNT_FILE" 2>/dev/null || echo 0; }
_bump_assign_calls() { echo "$(($(_assign_calls) + 1))" > "$ASSIGN_COUNT_FILE"; }
_verify_calls() { cat "$VERIFY_COUNT_FILE" 2>/dev/null || echo 0; }
_bump_verify_calls() {
  local n
  n=$(($(_verify_calls) + 1))
  echo "$n" > "$VERIFY_COUNT_FILE"
  echo "$n"
}

# --- Test 4: assign_issue_verified succeeds immediately when the write ---
# --- lands and verification matches on the first attempt ---
echo 0 > "$ASSIGN_COUNT_FILE"
echo 0 > "$VERIFY_COUNT_FILE"
issue_assign_only() {
  _bump_assign_calls
  return 0
}
_issue_assignee_logins() {
  _bump_verify_calls >/dev/null
  echo "dkblinux98"
}

SLEEP_CALLS=()
if assign_issue_verified "42" "dkblinux98" 3 2>/dev/null; then
  echo "[ok] assign_issue_verified returns success on first-try match"
else
  echo "[FAIL] assign_issue_verified should have succeeded on the first attempt" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "no retries when write+verify match immediately" "1" "$(_assign_calls)"
_assert_eq "verification read happens once" "1" "$(_verify_calls)"
_assert_eq "no backoff sleep when the first attempt succeeds" "0" "${#SLEEP_CALLS[@]}"

# --- Test 5: PATCH call succeeds but the read-back mismatches (stale ---
# --- assignee) for two attempts, then matches on the third ---
echo 0 > "$ASSIGN_COUNT_FILE"
echo 0 > "$VERIFY_COUNT_FILE"
issue_assign_only() {
  _bump_assign_calls
  return 0
}
_issue_assignee_logins() {
  local n
  n="$(_bump_verify_calls)"
  if [[ "$n" -ge 3 ]]; then
    echo "dkblinux98"
  else
    echo "myGPT-review-agent"
  fi
}

SLEEP_CALLS=()
if assign_issue_verified "42" "dkblinux98" 3 2>/dev/null; then
  echo "[ok] assign_issue_verified recovers once verification matches"
else
  echo "[FAIL] assign_issue_verified should have succeeded on the 3rd attempt" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "retries the write on verification mismatch" "3" "$(_assign_calls)"
_assert_eq "re-reads assignees on each attempt" "3" "$(_verify_calls)"

# --- Test 6: verification never matches (stale assignee persists) ---
# --- exhausts all attempts, fails loud, and propagates failure ---
echo 0 > "$ASSIGN_COUNT_FILE"
echo 0 > "$VERIFY_COUNT_FILE"
issue_assign_only() {
  _bump_assign_calls
  return 0
}
_issue_assignee_logins() {
  _bump_verify_calls >/dev/null
  echo "myGPT-review-agent"
}

SLEEP_CALLS=()
# gh_project.sh sources with `set -e` still active, so a plain failing
# assignment (rather than an `if`/`&&` guarded one) would kill this script
# outright — suspend errexit just for the capture.
set +e
STDERR_OUT="$(assign_issue_verified "42" "dkblinux98" 3 2>&1 1>/dev/null)"
STATUS=$?
set -e
if [[ "$STATUS" -ne 0 ]]; then
  echo "[ok] assign_issue_verified returns failure when verification never matches"
else
  echo "[FAIL] assign_issue_verified should have failed when the assignee never converges" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "exhausts all attempts" "3" "$(_assign_calls)"
if [[ "$STDERR_OUT" == *"::error::"* ]]; then
  echo "[ok] assign_issue_verified fails loud with a ::error:: annotation"
else
  echo "[FAIL] assign_issue_verified should emit a ::error:: annotation on exhaustion" >&2
  echo "  stderr was: $STDERR_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- Test 7: count_fast_claude_steps ignores skipped steps and the ---
# --- auto-generated "Post *" cleanup steps (#3360). Both report near-zero ---
# --- durations; before this fix they always matched, so the usage-limit ---
# --- self-heal detector misdiagnosed *every* failure as a usage-limit hit ---
CLAUDE_STEP_PATTERN="Run Claude Code|Claude Fix Issues|Claude review fix"

JOBS_ALL_SKIPPED='{"jobs":[{"steps":[
  {"name":"Run Claude Code to implement issue (Initial)","conclusion":"skipped","started_at":"2026-07-27T00:00:00Z","completed_at":"2026-07-27T00:00:00Z"},
  {"name":"Claude Fix Issues (Attempt 2)","conclusion":"skipped","started_at":"2026-07-27T00:00:00Z","completed_at":"2026-07-27T00:00:00Z"},
  {"name":"Post Run Claude Code to implement issue (Initial)","conclusion":"success","started_at":"2026-07-27T00:00:01Z","completed_at":"2026-07-27T00:00:01Z"},
  {"name":"Submit PR for review","conclusion":"failure","started_at":"2026-07-27T00:05:00Z","completed_at":"2026-07-27T00:05:01Z"}
]}]}'
_assert_eq "skipped/Post Claude steps never count toward the usage-limit signature" \
  "0" "$(count_fast_claude_steps "$JOBS_ALL_SKIPPED" "$CLAUDE_STEP_PATTERN" true)"
_assert_eq "skipped/Post Claude steps never count toward the early-cutoff signature either" \
  "0" "$(count_fast_claude_steps "$JOBS_ALL_SKIPPED" "$CLAUDE_STEP_PATTERN" false)"

JOBS_GENUINE_FAILURE='{"jobs":[{"steps":[
  {"name":"Run Claude Code to implement issue (Initial)","conclusion":"failure","started_at":"2026-07-27T00:00:00Z","completed_at":"2026-07-27T00:00:05Z"}
]}]}'
_assert_eq "a genuinely fast-failing Claude step is still detected" \
  "1" "$(count_fast_claude_steps "$JOBS_GENUINE_FAILURE" "$CLAUDE_STEP_PATTERN" true)"

JOBS_LONG_RUNNING='{"jobs":[{"steps":[
  {"name":"Run Claude Code to implement issue (Initial)","conclusion":"success","started_at":"2026-07-27T00:00:00Z","completed_at":"2026-07-27T00:05:34Z"}
]}]}'
_assert_eq "a genuinely long-running successful Claude step is not flagged" \
  "0" "$(count_fast_claude_steps "$JOBS_LONG_RUNNING" "$CLAUDE_STEP_PATTERN" false)"

# --- Test 8: real_label_names filters workflow-control labels so an issue ---
# --- carrying "usage-limit-retry" can still pass the one-label invariant ---
# --- enforced before PR submission (the #3360 deadlock) ---
LABELS_WITH_RETRY_MARKER='[{"name":"Acceptance Failure"},{"name":"usage-limit-retry"}]'
REAL_LABELS="$(real_label_names "$LABELS_WITH_RETRY_MARKER")"
_assert_eq "usage-limit-retry is filtered out of the real label list" \
  "Acceptance Failure" "$REAL_LABELS"
_assert_eq "exactly one real label remains, satisfying the one-label invariant" \
  "1" "$(printf '%s\n' "$REAL_LABELS" | grep -c . || true)"

LABELS_NORMAL='[{"name":"Feature"}]'
_assert_eq "a normal single-label issue is unaffected" \
  "Feature" "$(real_label_names "$LABELS_NORMAL")"

# --- Test 9: assign_and_trigger_developer on a fresh (unassigned) issue ---
# --- just assigns -- no unassign dance, no fallback comment (the plain ---
# --- assignment already fires a real 'issues.assigned' event) ---
REPO_OWNER="test-owner"
REPO_NAME="test-repo"
DEV_AGENT="myGPT-developer-agent"

GH_CALLS=()
gh() {
  GH_CALLS+=("$*")
  case "$1 $2" in
    "issue edit") : ;;
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}
_issue_assignee_logins() { echo ""; } # no assignees yet (REST-backed read, stubbed directly)
ASSIGN_ONLY_CALLS=()
issue_assign_only() { ASSIGN_ONLY_CALLS+=("$1 $2"); }
COMMENT_CALLS=()
issue_comment() { COMMENT_CALLS+=("$1 $2"); }

# shellcheck disable=SC2218 # exercises the real gh_project.sh function
# sourced above; Test 12 below shadows it with a mock for a different unit.
assign_and_trigger_developer "77"
_assert_eq "fresh assignment calls issue_assign_only once" "1" "${#ASSIGN_ONLY_CALLS[@]}"
_assert_eq "fresh assignment targets the dev agent" "77 myGPT-developer-agent" "${ASSIGN_ONLY_CALLS[0]}"
_assert_eq "fresh assignment posts no fallback comment" "0" "${#COMMENT_CALLS[@]}"
UNASSIGN_SEEN=0
for c in "${GH_CALLS[@]}"; do [[ "$c" == "issue edit"* ]] && UNASSIGN_SEEN=1; done
_assert_eq "fresh assignment never calls the unassign dance" "0" "$UNASSIGN_SEEN"

# --- Test 10: assign_and_trigger_developer when the dev agent is already ---
# --- assigned unassigns then reassigns (to force a real event) and posts ---
# --- NOTHING. #3647 backed the reassignment with a retry-token comment as a ---
# --- second trigger; #3882 deleted that token, so the verified reassignment ---
# --- is the whole signal and no comment may be posted (a comment carries ---
# --- findings, never control). ---
GH_CALLS=()
gh() {
  GH_CALLS+=("$*")
  case "$1 $2" in
    "issue edit") : ;;
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}
_issue_assignee_logins() { echo "$DEV_AGENT"; } # already assigned (REST-backed read, stubbed directly)
ASSIGN_ONLY_CALLS=()
COMMENT_CALLS=()
sleep() { :; } # no real backoff in tests
# assign_issue_verified re-reads the assignees to prove the write landed;
# _issue_assignee_logins above already reports the dev agent, so the real
# helper verifies on its first attempt.

# shellcheck disable=SC2218 # exercises the real gh_project.sh function
# sourced above; Test 12 below shadows it with a mock for a different unit.
assign_and_trigger_developer "78"
_assert_eq "redispatch calls issue_assign_only once (the reassignment)" "1" "${#ASSIGN_ONLY_CALLS[@]}"
_assert_eq "redispatch targets the dev agent" "78 myGPT-developer-agent" "${ASSIGN_ONLY_CALLS[0]}"
_assert_eq "redispatch posts no comment at all (#3882)" "0" "${#COMMENT_CALLS[@]}"
UNASSIGN_SEEN=0
for c in "${GH_CALLS[@]}"; do [[ "$c" == "issue edit"* ]] && UNASSIGN_SEEN=1; done
_assert_eq "redispatch unassigns before reassigning" "1" "$UNASSIGN_SEEN"

# --- Test 10a: developer_claim_issue -- the other end of the same lever. ---
# --- Assignment IS the dispatch (#3882), so this decides which lanes an ---
# --- assignment may claim and which identities may assign. Executed here ---
# --- (and on a runner by assignment-dispatch-smoke.yml) rather than read: ---
# --- it used to be inline YAML in developer_auto_implement.yml, which ---
# --- nothing could run. ---
HUMAN_OWNER="dkblinux98"
SCRUM_AGENT="myGPT-scrummaster-agent"
REVIEW_AGENT="myGPT-review-agent"
STATUS_BACKLOG="Backlog"
STATUS_IN_PROGRESS="In Progress"
STATUS_IN_REVIEW="In Review"

CLAIM_STATUS_STUB=""
issue_status() { echo "$CLAIM_STATUS_STUB"; }
# The claim runs inside a command substitution below, so a shell-array
# recorder would be lost with that subshell -- the pitfall documented at the
# top of this file. Record the writes in a temp file instead.
CLAIM_WRITES="$(mktemp)"
set_issue_status() { echo "$1 $2" >> "$CLAIM_WRITES"; }
_claim_writes() { wc -l < "$CLAIM_WRITES" | tr -d ' '; }

_claim() { # <status> <assigner> -> "<rc>:<stdout>"
  CLAIM_STATUS_STUB="$1"
  : > "$CLAIM_WRITES"
  local out rc
  out="$(developer_claim_issue "90" "$2" 2>/dev/null)" && rc=0 || rc=$?
  echo "${rc}:${out}"
}

_assert_eq "an issue already In Progress proceeds untouched" \
  "0:In Progress" "$(_claim "In Progress" "$REVIEW_AGENT")"
_assert_eq "...and writes no status" "0" "$(_claim_writes)"

_assert_eq "a Backlog issue assigned by the owner is claimed" \
  "0:In Progress" "$(_claim "Backlog" "$HUMAN_OWNER")"

_assert_eq "an In Review issue assigned by the review agent is claimed (rework)" \
  "0:In Progress" "$(_claim "In Review" "$REVIEW_AGENT")"
_assert_eq "...by writing the status itself -- the worker owns the transition" \
  "1" "$(_claim_writes)"
_assert_eq "...to In Progress" "90 In Progress" "$(cat "$CLAIM_WRITES")"

_assert_eq "claude[bot] is a permitted assigner (D-020)" \
  "0:In Progress" "$(_claim "Backlog" "claude[bot]")"

_assert_eq "a stranger's assignment claims nothing" \
  "3:" "$(_claim "Backlog" "some-drive-by")"
_assert_eq "...and leaves the status alone" "0" "$(_claim_writes)"

_assert_eq "the Acceptance Failed holding lane is never claimable (D-001/D-008)" \
  "3:" "$(_claim "Acceptance Failed" "$HUMAN_OWNER")"
_assert_eq "...even for the owner, whose placement there is the signal" "0" "$(_claim_writes)"

_assert_eq "Acceptance Testing is not claimable either" \
  "3:" "$(_claim "Acceptance Testing" "$REVIEW_AGENT")"
_assert_eq "finished work is not claimable" \
  "3:" "$(_claim "For Release" "$HUMAN_OWNER")"
_assert_eq "an issue that is not on the board is not claimable" \
  "3:" "$(_claim "" "$HUMAN_OWNER")"

rm -f "$CLAIM_WRITES"
unset -f issue_status set_issue_status

# --- Test 11: classify_backlog_claim_state implements the #3665 start-guard ---
# --- decision matrix -- distinguishing *who* holds the claim instead of ---
# --- halting on any assignee (the #3647 guard's bug: assign_backlog.yml's ---
# --- routine SCRUM_AGENT stamp on every fresh Backlog issue was itself ---
# --- treated as "already claimed", permanently blocking the queue) ---
gh() {
  case "$1" in
    api) echo "${ISSUE_STATE_STUB:-OPEN}" ;; # REST issue read + ascii_upcase, stubbed at the gh layer
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}

SCRUM_AGENT="myGPT-scrummaster-agent"
DEV_AGENT="myGPT-developer-agent"
HUMAN_OWNER="dkblinux98"

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo ""; }
_assert_eq "unassigned Backlog issue classifies as claimable" \
  "claimable" "$(classify_backlog_claim_state "80")"

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo "$SCRUM_AGENT"; }
_assert_eq "scrummaster-assigned Backlog issue classifies as claimable (the routine assign_backlog.yml stamp)" \
  "claimable" "$(classify_backlog_claim_state "81")"

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo "$DEV_AGENT"; }
_assert_eq "dev-agent-assigned issue classifies as duplicate (in-flight start, not a block)" \
  "duplicate" "$(classify_backlog_claim_state "82")"

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo "$HUMAN_OWNER"; }
_assert_eq "human-owner-assigned issue classifies as human_hold" \
  "human_hold" "$(classify_backlog_claim_state "83")"

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo "myGPT-review-agent"; }
_assert_eq "unrecognized assignee classifies as anomaly" \
  "anomaly" "$(classify_backlog_claim_state "84")"

ISSUE_STATE_STUB="CLOSED"
_issue_assignee_logins() { echo ""; }
_assert_eq "closed issue classifies as closed" \
  "closed" "$(classify_backlog_claim_state "85")"

# --- Test 12: scrummaster_attempt_start acts on classify_backlog_claim_state's ---
# --- verdict (#3665 acceptance criteria a/b/c) -- stub the classifier and ---
# --- every mutating primitive directly so this exercises the decision logic ---
# --- alone, independent of Test 11's `gh` stubbing ---
STATUS_IN_PROGRESS="In Progress"

ASSIGN_VERIFIED_CALLS=()
assign_issue_verified() { ASSIGN_VERIFIED_CALLS+=("$1 $2"); }
TRIGGER_DEV_CALLS=()
assign_and_trigger_developer() { TRIGGER_DEV_CALLS+=("$1"); }
SET_STATUS_CALLS=()
set_issue_status() { SET_STATUS_CALLS+=("$1 $2"); }
COMMENT_CALLS=()
issue_comment() { COMMENT_CALLS+=("$1 $2"); }

# (a) A stale scrummaster self-claim (#3593's actual trigger state) is a
# routine "claimable" verdict with an existing assignee -- reclaimed via the
# normal (non-history-correct) order and started.
classify_backlog_claim_state() { echo "claimable"; }
_issue_assignee_logins() { echo "$SCRUM_AGENT"; }
ASSIGN_VERIFIED_CALLS=(); TRIGGER_DEV_CALLS=(); SET_STATUS_CALLS=(); COMMENT_CALLS=()
if scrummaster_attempt_start "3593" >/dev/null; then
  echo "[ok] scrummaster_attempt_start returns 0 for a stale scrummaster self-claim"
else
  echo "[FAIL] scrummaster_attempt_start should return 0 for a stale scrummaster self-claim" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "reclaiming a scrummaster-assigned issue does not re-assign the scrummaster" "0" "${#ASSIGN_VERIFIED_CALLS[@]}"
_assert_eq "reclaiming a scrummaster-assigned issue assigns the developer" "1" "${#TRIGGER_DEV_CALLS[@]}"
_assert_eq "reclaiming a scrummaster-assigned issue sets Status -> In Progress" "1" "${#SET_STATUS_CALLS[@]}"

# A genuinely unassigned Backlog issue gets the history-correct sequence:
# scrummaster assigned first, then developer, then Status.
_issue_assignee_logins() { echo ""; }
ASSIGN_VERIFIED_CALLS=(); TRIGGER_DEV_CALLS=(); SET_STATUS_CALLS=(); COMMENT_CALLS=()
scrummaster_attempt_start "3594" >/dev/null
_assert_eq "an unassigned Backlog issue is assigned to the scrummaster first" "1" "${#ASSIGN_VERIFIED_CALLS[@]}"
_assert_eq "history-correct start targets the scrummaster" "3594 $SCRUM_AGENT" "${ASSIGN_VERIFIED_CALLS[0]}"

# (b) A candidate already assigned to the dev agent is a duplicate in-flight
# start -- skip quietly (exit 10), no mutation, no comment. gh_project.sh's
# own `set -euo pipefail` is in effect in this sourcing shell, so a
# non-zero return from a bare top-level call would abort the test script --
# guard it like the earlier `set_field_with_retry` failure-path tests do.
classify_backlog_claim_state() { echo "duplicate"; }
ASSIGN_VERIFIED_CALLS=(); TRIGGER_DEV_CALLS=(); SET_STATUS_CALLS=(); COMMENT_CALLS=()
set +e
scrummaster_attempt_start "3595" >/dev/null
RC=$?
set -e
_assert_eq "a dev-assigned candidate returns 10 (quiet skip)" "10" "$RC"
_assert_eq "a dev-assigned candidate is never mutated" "0" "$((${#ASSIGN_VERIFIED_CALLS[@]} + ${#TRIGGER_DEV_CALLS[@]} + ${#SET_STATUS_CALLS[@]}))"
_assert_eq "a dev-assigned candidate posts no comment" "0" "${#COMMENT_CALLS[@]}"

# (c) An unrecognized assignee is an anomaly -- skip loudly (exit 11), a
# comment naming the issue and the anomalous assignee is posted, no mutation.
classify_backlog_claim_state() { echo "anomaly"; }
_issue_assignee_logins() { echo "myGPT-review-agent"; }
ASSIGN_VERIFIED_CALLS=(); TRIGGER_DEV_CALLS=(); SET_STATUS_CALLS=(); COMMENT_CALLS=()
set +e
scrummaster_attempt_start "3596" >/dev/null
RC=$?
set -e
_assert_eq "an anomalous candidate returns 11 (loud skip)" "11" "$RC"
_assert_eq "an anomalous candidate is never mutated" "0" "$((${#ASSIGN_VERIFIED_CALLS[@]} + ${#TRIGGER_DEV_CALLS[@]} + ${#SET_STATUS_CALLS[@]}))"
_assert_eq "an anomalous candidate posts exactly one report comment" "1" "${#COMMENT_CALLS[@]}"
case "${COMMENT_CALLS[0]:-}" in
  "3596 "*"myGPT-review-agent"*)
    echo "[ok] the anomaly comment names the issue and the anomalous assignee"
    ;;
  *)
    echo "[FAIL] the anomaly comment should name the issue and the anomalous assignee, got: ${COMMENT_CALLS[0]:-<empty>}" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

# --- Test 13: sweep_parked_blocked_issues (#3631) -- no parked issues is a ---
# --- no-op that still reports a clean "Promoted 0" summary ---
#
# sweep_parked_blocked_issues's stubbed set_issue_status/assign_issue_verified/
# issue_comment calls record into arrays declared in *this* shell -- calling
# the function via `$(...)` would run it in a subshell (like Test 4-6's
# assign_issue_verified capture above) and lose those array mutations when
# the subshell exits. Route stdout through a temp file instead so the
# function runs directly in this shell and its array writes survive.
SWEEP_OUT_FILE="$(mktemp)"
trap 'rm -f "$ASSIGN_COUNT_FILE" "$VERIFY_COUNT_FILE" "$SWEEP_OUT_FILE"' EXIT
# Call directly (no `$(...)` around the call itself) so the function runs in
# this shell, not a subshell; read its captured stdout back afterward.
_run_sweep() { sweep_parked_blocked_issues >"$SWEEP_OUT_FILE"; }

STATUS_IN_REVIEW="In Review"
STATUS_ACCEPTANCE_TESTING="Acceptance Testing"
STATUS_FOR_RELEASE="For Release"
HUMAN_OWNER="dkblinux98"

list_parked_blocked_issues() { :; }
blocked_by_issues() { :; }
_issue_open_state() { :; }
issue_status() { :; }
SET_STATUS_CALLS=(); ASSIGN_VERIFIED_CALLS=(); COMMENT_CALLS=()
set_issue_status() { SET_STATUS_CALLS+=("$1 $2"); }
assign_issue_verified() { ASSIGN_VERIFIED_CALLS+=("$1 $2"); }
issue_comment() { COMMENT_CALLS+=("$2"); }

DRY_RUN=0
_run_sweep
OUT="$(cat "$SWEEP_OUT_FILE")"
_assert_eq "no parked issues -> Promoted 0 summary" "Promoted 0 issue(s)." "$OUT"
_assert_eq "no parked issues -> no status mutation" "0" "${#SET_STATUS_CALLS[@]}"
_assert_eq "no parked issues -> no assignment" "0" "${#ASSIGN_VERIFIED_CALLS[@]}"

# --- Test 14: a parked issue whose single blocker has NOT completed is ---
# --- left alone (no mutation, not counted as promoted) ---
list_parked_blocked_issues() { echo "100"; }
blocked_by_issues() {
  case "$1" in
    100) echo "200" ;;
    *) : ;;
  esac
}
_issue_open_state() {
  case "$1" in
    200) echo "OPEN" ;;
    *) echo "" ;;
  esac
}
issue_status() {
  case "$1" in
    200) echo "In Progress" ;;
    *) echo "" ;;
  esac
}
SET_STATUS_CALLS=(); ASSIGN_VERIFIED_CALLS=(); COMMENT_CALLS=()

_run_sweep
OUT="$(cat "$SWEEP_OUT_FILE")"
_assert_eq "an unresolved blocker leaves the parked issue un-promoted" "Promoted 0 issue(s)." "$OUT"
_assert_eq "an unresolved blocker triggers no status mutation" "0" "${#SET_STATUS_CALLS[@]}"

# --- Test 15: a parked issue whose blocker is closed and already in ---
# --- Acceptance Testing promotes -- status set, owner assigned, one ---
# --- comment posted naming the completed blocker ---
list_parked_blocked_issues() { echo "101"; }
blocked_by_issues() {
  case "$1" in
    101) echo "201" ;;
    *) : ;;
  esac
}
_issue_open_state() {
  case "$1" in
    201) echo "CLOSED" ;;
    *) echo "" ;;
  esac
}
issue_status() {
  case "$1" in
    201) echo "Acceptance Testing" ;;
    *) echo "" ;;
  esac
}
SET_STATUS_CALLS=(); ASSIGN_VERIFIED_CALLS=(); COMMENT_CALLS=()

_run_sweep
OUT="$(cat "$SWEEP_OUT_FILE")"
_assert_eq "a fully-complete blocker promotes the parked issue" "Promoted 1 issue(s)." "$OUT"
_assert_eq "promotion sets Status -> Acceptance Testing" "101 Acceptance Testing" "${SET_STATUS_CALLS[0]:-}"
_assert_eq "promotion assigns the human owner" "101 dkblinux98" "${ASSIGN_VERIFIED_CALLS[0]:-}"
_assert_eq "promotion posts exactly one comment" "1" "${#COMMENT_CALLS[@]}"
case "${COMMENT_CALLS[0]:-}" in
  *"#201"*) echo "[ok] the promotion comment names the completed blocker" ;;
  *)
    echo "[FAIL] the promotion comment should name blocker #201, got: ${COMMENT_CALLS[0]:-<empty>}" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

# --- Test 16: DRY_RUN=1 resolves the same promotion decision but makes no ---
# --- mutating calls ---
SET_STATUS_CALLS=(); ASSIGN_VERIFIED_CALLS=(); COMMENT_CALLS=()
DRY_RUN=1
_run_sweep
OUT="$(cat "$SWEEP_OUT_FILE")"
DRY_RUN=0
_assert_eq "DRY_RUN still reports what would be promoted" "Promoted 1 issue(s)." "$OUT"
_assert_eq "DRY_RUN sets no status" "0" "${#SET_STATUS_CALLS[@]}"
_assert_eq "DRY_RUN assigns no owner" "0" "${#ASSIGN_VERIFIED_CALLS[@]}"
_assert_eq "DRY_RUN posts no comment" "0" "${#COMMENT_CALLS[@]}"

# --- Test 17: a parked blocker CHAIN (A blocked by B, B itself parked and ---
# --- blocked by C) resolves transitively in a single sweep run -- once C ---
# --- (already For Release) clears B, B's own promotion (recorded in the ---
# --- same-run resolved-state cache) immediately clears A too, so both ---
# --- promote in one pass instead of one hop per 30-minute sweep interval ---
list_parked_blocked_issues() { printf '%s\n' 301 302; }
blocked_by_issues() {
  case "$1" in
    301) echo "302" ;;  # A blocked by B
    302) echo "303" ;;  # B blocked by C
    *) : ;;
  esac
}
_issue_open_state() {
  case "$1" in
    303) echo "CLOSED" ;;  # C already fully accepted
    *) echo "" ;;
  esac
}
issue_status() {
  case "$1" in
    303) echo "For Release" ;;
    *) echo "" ;;
  esac
}
SET_STATUS_CALLS=(); ASSIGN_VERIFIED_CALLS=(); COMMENT_CALLS=()

_run_sweep
OUT="$(cat "$SWEEP_OUT_FILE")"
_assert_eq "a two-level parked chain promotes both issues in one run" "Promoted 2 issue(s)." "$OUT"
_assert_eq "both issues in the chain get Status -> Acceptance Testing" "2" "${#SET_STATUS_CALLS[@]}"
_assert_eq "both issues in the chain get the owner assigned" "2" "${#ASSIGN_VERIFIED_CALLS[@]}"
_assert_eq "both issues in the chain get a promotion comment" "2" "${#COMMENT_CALLS[@]}"

# --- Test 18: project_field_value (#3666) -- the fill-if-missing hygiene ---
# --- read helper. Stub graphql() directly (project_field_value's only ---
# --- collaborator) with a fixture item carrying a single-select value, an ---
# --- iteration value, and a text value, then check each field name resolves ---
# --- to the right one and an absent field name resolves to empty ---
graphql() {
  cat <<'JSON'
{
  "data": {
    "node": {
      "fieldValues": {
        "nodes": [
          {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "field": { "name": "Status" },
            "name": "In Review"
          },
          {
            "__typename": "ProjectV2ItemFieldIterationValue",
            "field": { "name": "Sprint" },
            "title": "Sprint 8"
          },
          {
            "__typename": "ProjectV2ItemFieldTextValue",
            "field": { "name": "Notes" },
            "text": "some free text"
          }
        ]
      }
    }
  }
}
JSON
}

_assert_eq "project_field_value reads a single-select field's selected option" \
  "In Review" "$(project_field_value "item-x" "Status")"
_assert_eq "project_field_value reads an iteration field's title" \
  "Sprint 8" "$(project_field_value "item-x" "Sprint")"
_assert_eq "project_field_value reads a text field's value" \
  "some free text" "$(project_field_value "item-x" "Notes")"
_assert_eq "project_field_value returns empty for a field the item has no value for" \
  "" "$(project_field_value "item-x" "Priority")"

# --- Test 13: unresolved_escalation_issues (#3687) -- filters the raw open- ---
# --- issues-assigned-to-owner fetch down to issues only (excludes PRs, ---
# --- which the same REST endpoint also returns for an assignee) ---
REPO_OWNER="test-owner"
REPO_NAME="test-repo"
HUMAN_OWNER="dkblinux98"

_open_issues_assigned_to() {
  [[ "$1" == "dkblinux98" ]] || { echo "[test] unexpected owner: $1" >&2; return 1; }
  cat <<'JSON'
[
  {"number": 201, "title": "spec ambiguity on auth flow", "pull_request": null},
  {"number": 202, "title": "some PR also assigned to owner", "pull_request": {"url": "x"}},
  {"number": 203, "title": "flaky suite escalation", "pull_request": null}
]
JSON
}

ISSUES_OUT="$(unresolved_escalation_issues)"
_assert_eq "unresolved_escalation_issues excludes PRs and lists both open issues" \
  "#201 spec ambiguity on auth flow
#203 flaky suite escalation" "$ISSUES_OUT"

_open_issues_assigned_to() { echo "[]"; }
_assert_eq "unresolved_escalation_issues is empty when nothing is assigned to the owner" \
  "" "$(unresolved_escalation_issues)"

# The release tracking issue is owner-assigned for the whole life of a
# release; counting it would permanently drop the pause gate's effective
# threshold from 2 to 1 (#3868 -- exactly how the 2026-08-18 post-acceptance
# drain deadlocked on one real parked issue plus the tracker).
_open_issues_assigned_to() {
  cat <<'JSON'
[
  {"number": 3521, "title": "Release v3.0.0", "pull_request": null},
  {"number": 201, "title": "spec ambiguity on auth flow", "pull_request": null}
]
JSON
}
RELEASE_ISSUE_NUMBER="3521"
_assert_eq "unresolved_escalation_issues excludes the release tracking issue" \
  "#201 spec ambiguity on auth flow" "$(unresolved_escalation_issues)"

RELEASE_ISSUE_NUMBER=""
_assert_eq "unresolved_escalation_issues keeps every issue when no release issue is configured" \
  "#3521 Release v3.0.0
#201 spec ambiguity on auth flow" "$(unresolved_escalation_issues)"
RELEASE_ISSUE_NUMBER="3521"

# --- Test 14: count_unresolved_escalations (#3687) -- always echoes a ---
# --- number, including zero (grep -c's exit-1-on-no-match must not leak) ---
unresolved_escalation_issues() { echo ""; }
_assert_eq "count_unresolved_escalations is 0 when nothing is unresolved" \
  "0" "$(count_unresolved_escalations)"

unresolved_escalation_issues() { printf '#201 a\n#203 b\n'; }
_assert_eq "count_unresolved_escalations counts each listed issue" \
  "2" "$(count_unresolved_escalations)"

# --- Test 14b: _escalation_pause_comment_id (#3687) -- exercises the REAL ---
# --- gh api + jq pipeline (only `gh` is stubbed, unlike the escalation_pause_gate ---
# --- tests below which stub this function out entirely) so a regression in ---
# --- the gh/jq invocation itself -- e.g. the Critical `gh api --jq --arg` ---
# --- bug this fixes -- is caught. Two pages are returned to also cover the ---
# --- --paginate-without-slurp pitfall (AGENTS.md): the matching comments ---
# --- are split across pages with a later, non-matching comment on page 1, ---
# --- so a per-page (unslurped) sort_by/last would pick the wrong id. ---
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    cat <<JSON
[{"id": 111, "created_at": "2026-08-03T00:00:00Z", "body": "unrelated, newer than the page-2 comments"}]
[{"id": 222, "created_at": "2026-08-01T00:00:00Z", "body": "old pause report $_ESCALATION_PAUSE_MARKER"}, {"id": 333, "created_at": "2026-08-02T00:00:00Z", "body": "newest pause report $_ESCALATION_PAUSE_MARKER"}]
JSON
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
_assert_eq "_escalation_pause_comment_id picks the latest matching comment across pages" \
  "333" "$(_escalation_pause_comment_id 3521)"

gh() { echo "[]"; }
_assert_eq "_escalation_pause_comment_id is empty when no comment matches the marker" \
  "" "$(_escalation_pause_comment_id 3521)"

# --- Test 15: escalation_pause_gate (#3687) -- the dispatch-pause decision ---
# --- itself: 0 or 1 unresolved escalations never pauses (even without a ---
# --- release issue configured); >=2 pauses and posts/updates a loud report ---
# --- on RELEASE_ISSUE_NUMBER; dropping back below 2 clears/updates it ---
RELEASE_ISSUE_NUMBER=""
unresolved_escalation_issues() { echo ""; }
if escalation_pause_gate; then
  echo "[ok] zero unresolved escalations: gate stays open"
else
  echo "[FAIL] zero unresolved escalations: gate should stay open" >&2
  FAILURES=$((FAILURES + 1))
fi

unresolved_escalation_issues() { echo "#201 only one"; }
if escalation_pause_gate; then
  echo "[ok] exactly one unresolved escalation: gate stays open (normal traffic)"
else
  echo "[FAIL] exactly one unresolved escalation: gate should stay open" >&2
  FAILURES=$((FAILURES + 1))
fi

RELEASE_ISSUE_NUMBER="3521"
unresolved_escalation_issues() { printf '#201 first\n#203 second\n'; }
_escalation_pause_comment_id() { echo ""; }
POSTED_ISSUES=()
POSTED_BODIES=()
issue_comment() { POSTED_ISSUES+=("$1"); POSTED_BODIES+=("$2"); }
PATCH_CALLS=()
gh() {
  if [[ "$1" == "api" && "$2" == "-X" && "$3" == "PATCH" ]]; then
    PATCH_CALLS+=("$*")
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}

if escalation_pause_gate; then
  echo "[FAIL] two unresolved escalations: gate should pause" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] two unresolved escalations: gate pauses"
fi
_assert_eq "pausing posts exactly one report (no prior comment to update)" "1" "${#POSTED_ISSUES[@]}"
_assert_eq "the report is posted on the release tracking issue" "3521" "${POSTED_ISSUES[0]}"
_assert_contains "the report lists both escalated issues" "${POSTED_BODIES[0]}" "#201 first"
_assert_contains "the report lists both escalated issues" "${POSTED_BODIES[0]}" "#203 second"
_assert_eq "pausing does not PATCH (no existing comment)" "0" "${#PATCH_CALLS[@]}"

# Already paused with an existing report comment -- update in place, don't
# post a second comment.
_escalation_pause_comment_id() { echo "999"; }
POSTED_ISSUES=()
POSTED_BODIES=()
PATCH_CALLS=()
if escalation_pause_gate; then
  echo "[FAIL] still >=2 unresolved with an existing report: gate should stay paused" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] still >=2 unresolved with an existing report: gate stays paused"
fi
_assert_eq "an existing report is updated, not duplicated" "0" "${#POSTED_ISSUES[@]}"
_assert_eq "updating the existing report PATCHes it once" "1" "${#PATCH_CALLS[@]}"
_assert_contains "the PATCH targets the existing comment id" "${PATCH_CALLS[0]}" "issues/comments/999"

# Count drops back below 2 with a stale report comment present -- the gate
# reopens and the stale report is updated to say so (not left dangling).
unresolved_escalation_issues() { echo "#201 first"; }
PATCH_CALLS=()
if escalation_pause_gate; then
  echo "[ok] dropping below 2 with a stale report: gate reopens"
else
  echo "[FAIL] dropping below 2 with a stale report: gate should reopen" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "reopening updates the stale report exactly once" "1" "${#PATCH_CALLS[@]}"
_assert_contains "the reopen PATCH targets the existing comment id" "${PATCH_CALLS[0]}" "issues/comments/999"

# --- Test 16: _slack_notify_recent (#3695) -- exercises the REAL gh api + ---
# --- jq + python3 cutoff pipeline (only `gh` is stubbed) so a regression ---
# --- in that pipeline itself is caught, not just the higher-level mock ---
# --- tests below. A large window makes a comment from 2020 count as ---
# --- "recent"; a 1-minute window does not (2020 is never within 1 minute ---
# --- of "now", whenever this test runs). ---
REPO_OWNER="test-owner"
REPO_NAME="test-repo"
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/42/comments" && "$3" == "--paginate" ]]; then
    cat <<'JSON'
[{"id": 1, "created_at": "2020-01-01T00:00:00Z", "body": "notified <!-- slack-notify:42:FATAL -->"}]
JSON
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
if _slack_notify_recent "42" "42:FATAL" 999999999; then
  echo "[ok] _slack_notify_recent: a huge window counts an old marker as recent"
else
  echo "[FAIL] _slack_notify_recent: a huge window should count an old marker as recent" >&2
  FAILURES=$((FAILURES + 1))
fi
if _slack_notify_recent "42" "42:FATAL" 1; then
  echo "[FAIL] _slack_notify_recent: a 1-minute window should not count a 2020 marker as recent" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] _slack_notify_recent: a 1-minute window does not count a 2020 marker as recent"
fi
if _slack_notify_recent "42" "42:OTHER_KEY" 999999999; then
  echo "[FAIL] _slack_notify_recent: a non-matching dedup key should never count as recent" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] _slack_notify_recent: a non-matching dedup key never counts as recent"
fi

# --- Test 17: notify_human_escalation (#3695) -- graceful degradation, ---
# --- dedup skip, success path (Slack call + marker comment), and Slack- ---
# --- failure fallback. curl/issue_comment/_slack_notify_recent are all ---
# --- stubbed here; the real gh/jq/python3 pipeline is covered by Test 16 ---
# --- above. ---
SLACK_BOT_TOKEN=""
SLACK_USER_ID=""
CURL_CALLS=0
curl() { CURL_CALLS=$((CURL_CALLS + 1)); echo '{"ok":true}'; }
COMMENT_CALLS=()
issue_comment() { COMMENT_CALLS+=("$1|$2"); }

notify_human_escalation "42" "FATAL" "diag" "action"
_assert_eq "missing SLACK_BOT_TOKEN/SLACK_USER_ID: no curl call attempted" "0" "$CURL_CALLS"
_assert_eq "missing SLACK_BOT_TOKEN/SLACK_USER_ID: no marker comment posted" "0" "${#COMMENT_CALLS[@]}"

SLACK_BOT_TOKEN="xoxb-test"
SLACK_USER_ID="U12345"
CURL_CALLS=0
COMMENT_CALLS=()
_slack_notify_recent() { return 0; } # dedup: already notified recently
notify_human_escalation "42" "FATAL" "diag" "action"
_assert_eq "recent duplicate: no curl call attempted" "0" "$CURL_CALLS"
_assert_eq "recent duplicate: no marker comment posted" "0" "${#COMMENT_CALLS[@]}"

# curl runs inside `response="$(curl ...)"` (command substitution), which is
# a subshell -- a plain CURL_CALLS=$((CURL_CALLS+1)) inside the stub would
# not be visible back here, so count calls via a file instead (file writes
# from a subshell persist; variable assignments do not).
CURL_CALL_LOG="$(mktemp)"
COMMENT_CALLS=()
_slack_notify_recent() { return 1; } # not a duplicate
curl() { echo called >>"$CURL_CALL_LOG"; echo '{"ok":true}'; }
notify_human_escalation "42" "FATAL" "one-line diagnosis" "merge abc123 to v3.0.0" "42:FATAL"
_assert_eq "success path: exactly one Slack API call" "1" "$(wc -l <"$CURL_CALL_LOG")"
_assert_eq "success path: exactly one marker comment posted" "1" "${#COMMENT_CALLS[@]}"
_assert_eq "success path: marker comment targets the right issue" "42" "${COMMENT_CALLS[0]%%|*}"
_assert_contains "success path: marker comment carries the dedup marker" \
  "${COMMENT_CALLS[0]}" "<!-- slack-notify:42:FATAL -->"

: >"$CURL_CALL_LOG"
COMMENT_CALLS=()
curl() { echo called >>"$CURL_CALL_LOG"; echo '{"ok":false,"error":"channel_not_found"}'; }
if notify_human_escalation "42" "FATAL" "diag" "action"; then
  echo "[ok] Slack API failure: notify_human_escalation still returns success (never blocks the caller)"
else
  echo "[FAIL] Slack API failure: notify_human_escalation must always return 0" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "Slack API failure: no marker comment posted (no false record of success)" "0" "${#COMMENT_CALLS[@]}"
rm -f "$CURL_CALL_LOG"

# --- Test 18: _release_issue_comments_json (#3694) -- exercises the REAL ---
# --- gh api + jq pipeline (only `gh` is stubbed) across two pages, the ---
# --- same --paginate-without-slurp pitfall coverage as Test 14b above, ---
# --- plus the `id` field cross_issue_anomaly_pause_gate needs that ---
# --- unresolved_escalation_issues' equivalent fetch doesn't carry. ---
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    cat <<JSON
[{"id": 1, "body": "page 1 comment", "author_association": "NONE", "created_at": "2026-08-09T00:00:00Z"}]
[{"id": 2, "body": "page 2 comment", "author_association": "OWNER", "created_at": "2026-08-09T00:01:00Z"}]
JSON
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
COMMENTS_OUT="$(_release_issue_comments_json 3521)"
_assert_eq "_release_issue_comments_json flattens both pages into one array" \
  "2" "$(echo "$COMMENTS_OUT" | jq 'length')"
_assert_eq "_release_issue_comments_json keeps the id field" \
  "1" "$(echo "$COMMENTS_OUT" | jq '.[0].id')"

# --- Test 19: cross_issue_anomaly_decision (#3694) -- the (issue, ---
# --- failed_step) decision wrapper: empty release issue fails open ---
# --- ("open", never blocks a run on missing config); a real comment ---
# --- thread with a matching marker from a DIFFERENT issue reports "skip" ---
ANOMALY_MARKER_3667="<!-- nyxgpt-anomaly: step=check_if_pr_already_exists issue=3667 opened=1000 -->"

_assert_eq "cross_issue_anomaly_decision with no release issue configured opens (fails open)" \
  "open" "$(cross_issue_anomaly_decision "" 3511 "Check if PR already exists" 1500 | jq -r '.action')"

gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    printf '[{"id": 9, "body": "%s", "author_association": "NONE", "created_at": "2026-08-09T00:00:00Z"}]\n' "$ANOMALY_MARKER_3667"
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
DECISION="$(cross_issue_anomaly_decision 3521 3511 "Check if PR already exists" 1500)"
_assert_eq "cross_issue_anomaly_decision reports skip for a matching open anomaly from another issue" \
  "skip" "$(echo "$DECISION" | jq -r '.action')"
_assert_eq "cross_issue_anomaly_decision reports the originating issue" \
  "3667" "$(echo "$DECISION" | jq -r '.origin_issue')"

DECISION="$(cross_issue_anomaly_decision 3521 3667 "Check if PR already exists" 1500)"
_assert_eq "cross_issue_anomaly_decision reports proceed for the origin issue itself" \
  "proceed" "$(echo "$DECISION" | jq -r '.action')"

# --- Test 20: open_cross_issue_anomaly (#3694) -- posts the tracking-record ---
# --- marker comment on the release issue for the issue `decide` said should ---
# --- open one ---
POSTED_ISSUES=()
POSTED_BODIES=()
issue_comment() { POSTED_ISSUES+=("$1"); POSTED_BODIES+=("$2"); }
open_cross_issue_anomaly 3521 3667 "Check if PR already exists" 1000 "https://github.com/test-owner/test-repo/actions/runs/111"
_assert_eq "open_cross_issue_anomaly posts exactly one comment" "1" "${#POSTED_ISSUES[@]}"
_assert_eq "open_cross_issue_anomaly posts on the release tracking issue" "3521" "${POSTED_ISSUES[0]}"
_assert_contains "the tracking record embeds the anomaly marker" "${POSTED_BODIES[0]}" "$ANOMALY_MARKER_3667"
_assert_contains "the tracking record links the originating issue" "${POSTED_BODIES[0]}" "#3667"

# --- Test 21: cross_issue_anomaly_pause_gate (#3694) -- the dispatch-pause ---
# --- decision: no open anomaly never pauses; an open anomaly pauses and ---
# --- posts/updates a loud report; resolving it (RESOLVE_ANOMALY, or the ---
# --- window elapsing) reopens the gate. Unlike cross_issue_anomaly_decision ---
# --- above (which takes now_epoch as an explicit argument), the gate ---
# --- itself checks against the real clock -- so its marker must carry a ---
# --- genuinely recent "opened" timestamp, not the synthetic epoch 1000 ---
# --- used above. ---
ANOMALY_MARKER_3667="<!-- nyxgpt-anomaly: step=check_if_pr_already_exists issue=3667 opened=$(( $(date +%s) - 300 )) -->"
RELEASE_ISSUE_NUMBER=""
gh() { echo "[test] unexpected gh invocation while no release issue is configured: $*" >&2; return 1; }
if cross_issue_anomaly_pause_gate; then
  echo "[ok] no release issue configured: gate stays open (fails open)"
else
  echo "[FAIL] no release issue configured: gate should stay open" >&2
  FAILURES=$((FAILURES + 1))
fi

RELEASE_ISSUE_NUMBER="3521"
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    printf '[{"id": 9, "body": "%s", "author_association": "NONE", "created_at": "2026-08-09T00:00:00Z"}]\n' "$ANOMALY_MARKER_3667"
    return 0
  elif [[ "$1" == "api" && "$2" == "-X" && "$3" == "PATCH" ]]; then
    PATCH_CALLS+=("$*")
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
POSTED_ISSUES=()
POSTED_BODIES=()
PATCH_CALLS=()
if cross_issue_anomaly_pause_gate; then
  echo "[FAIL] an open anomaly: gate should pause" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] an open anomaly: gate pauses"
fi
_assert_eq "pausing posts exactly one report (no prior comment to update)" "1" "${#POSTED_ISSUES[@]}"
_assert_eq "the report is posted on the release tracking issue" "3521" "${POSTED_ISSUES[0]}"
_assert_eq "pausing does not PATCH (no existing report comment)" "0" "${#PATCH_CALLS[@]}"

# Already paused with an existing report comment (the anomaly marker AND a
# prior pause-report comment are both present) -- update in place, don't
# post a second comment.
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    printf '[{"id": 9, "body": "%s", "author_association": "NONE", "created_at": "2026-08-09T00:00:00Z"},
             {"id": 42, "body": "prior pause report %s", "author_association": "NONE", "created_at": "2026-08-09T00:05:00Z"}]\n' \
      "$ANOMALY_MARKER_3667" "$_CROSS_ISSUE_ANOMALY_PAUSE_MARKER"
    return 0
  elif [[ "$1" == "api" && "$2" == "-X" && "$3" == "PATCH" ]]; then
    PATCH_CALLS+=("$*")
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
POSTED_ISSUES=()
POSTED_BODIES=()
PATCH_CALLS=()
if cross_issue_anomaly_pause_gate; then
  echo "[FAIL] still open with an existing report: gate should stay paused" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] still open with an existing report: gate stays paused"
fi
_assert_eq "an existing report is updated, not duplicated" "0" "${#POSTED_ISSUES[@]}"
_assert_eq "updating the existing report PATCHes it once" "1" "${#PATCH_CALLS[@]}"
_assert_contains "the PATCH targets the existing comment id" "${PATCH_CALLS[0]}" "issues/comments/42"

# The anomaly resolves (RESOLVE_ANOMALY from the owner) with a stale report
# comment present -- the gate reopens and the stale report is updated to
# say so (not left dangling).
gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/issues/3521/comments" && "$3" == "--paginate" ]]; then
    printf '[{"id": 9, "body": "%s", "author_association": "NONE", "created_at": "2026-08-09T00:00:00Z"},
             {"id": 42, "body": "prior pause report %s", "author_association": "NONE", "created_at": "2026-08-09T00:05:00Z"},
             {"id": 43, "body": "RESOLVE_ANOMALY", "author_association": "OWNER", "created_at": "2026-08-09T00:06:00Z"}]\n' \
      "$ANOMALY_MARKER_3667" "$_CROSS_ISSUE_ANOMALY_PAUSE_MARKER"
    return 0
  elif [[ "$1" == "api" && "$2" == "-X" && "$3" == "PATCH" ]]; then
    PATCH_CALLS+=("$*")
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
PATCH_CALLS=()
if cross_issue_anomaly_pause_gate; then
  echo "[ok] anomaly resolved with a stale report: gate reopens"
else
  echo "[FAIL] anomaly resolved with a stale report: gate should reopen" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "reopening updates the stale report exactly once" "1" "${#PATCH_CALLS[@]}"
_assert_contains "the reopen PATCH targets the existing comment id" "${PATCH_CALLS[0]}" "issues/comments/42"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
