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
    "issue view") echo "" ;; # no assignees yet
    "issue edit") : ;;
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}
ASSIGN_ONLY_CALLS=()
issue_assign_only() { ASSIGN_ONLY_CALLS+=("$1 $2"); }
COMMENT_CALLS=()
issue_comment() { COMMENT_CALLS+=("$1 $2"); }

assign_and_trigger_developer "77"
_assert_eq "fresh assignment calls issue_assign_only once" "1" "${#ASSIGN_ONLY_CALLS[@]}"
_assert_eq "fresh assignment targets the dev agent" "77 myGPT-developer-agent" "${ASSIGN_ONLY_CALLS[0]}"
_assert_eq "fresh assignment posts no fallback comment" "0" "${#COMMENT_CALLS[@]}"
UNASSIGN_SEEN=0
for c in "${GH_CALLS[@]}"; do [[ "$c" == "issue edit"* ]] && UNASSIGN_SEEN=1; done
_assert_eq "fresh assignment never calls the unassign dance" "0" "$UNASSIGN_SEEN"

# --- Test 10: assign_and_trigger_developer when the dev agent is already ---
# --- assigned unassigns then reassigns (to force a real event) AND posts ---
# --- a RETRY_IMPLEMENTATION fallback comment, per #3647's redispatch fix ---
GH_CALLS=()
gh() {
  GH_CALLS+=("$*")
  case "$1 $2" in
    "issue view") echo "$DEV_AGENT" ;; # already assigned
    "issue edit") : ;;
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}
ASSIGN_ONLY_CALLS=()
COMMENT_CALLS=()
sleep() { :; } # no real backoff in tests

assign_and_trigger_developer "78"
_assert_eq "redispatch calls issue_assign_only once (the reassignment)" "1" "${#ASSIGN_ONLY_CALLS[@]}"
_assert_eq "redispatch targets the dev agent" "78 myGPT-developer-agent" "${ASSIGN_ONLY_CALLS[0]}"
_assert_eq "redispatch posts exactly one fallback comment" "1" "${#COMMENT_CALLS[@]}"
_assert_eq "fallback comment body is the RETRY_IMPLEMENTATION marker" "78 RETRY_IMPLEMENTATION" "${COMMENT_CALLS[0]}"
UNASSIGN_SEEN=0
for c in "${GH_CALLS[@]}"; do [[ "$c" == "issue edit"* ]] && UNASSIGN_SEEN=1; done
_assert_eq "redispatch unassigns before reassigning" "1" "$UNASSIGN_SEEN"

# --- Test 11: classify_backlog_claim_state implements the #3665 start-guard ---
# --- decision matrix -- distinguishing *who* holds the claim instead of ---
# --- halting on any assignee (the #3647 guard's bug: assign_backlog.yml's ---
# --- routine SCRUM_AGENT stamp on every fresh Backlog issue was itself ---
# --- treated as "already claimed", permanently blocking the queue) ---
gh() {
  case "$1 $2" in
    "issue view") echo "${ISSUE_STATE_STUB:-OPEN}" ;;
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

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
