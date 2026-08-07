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

# --- Test 11: issue_claimable_for_start gates scrummaster_start_issue.sh's ---
# --- idempotency check (#3647 scope addition: duplicate concurrent ---
# --- dispatches must not both claim the same issue) ---
gh() {
  case "$1 $2" in
    "issue view") echo "${ISSUE_STATE_STUB:-OPEN}" ;;
    *) echo "[test] unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo ""; }
if issue_claimable_for_start "80"; then
  echo "[ok] issue_claimable_for_start true for an open, unassigned issue"
else
  echo "[FAIL] issue_claimable_for_start should be true for an open, unassigned issue" >&2
  FAILURES=$((FAILURES + 1))
fi

ISSUE_STATE_STUB="OPEN"
_issue_assignee_logins() { echo "myGPT-developer-agent"; }
if issue_claimable_for_start "81"; then
  echo "[FAIL] issue_claimable_for_start should be false once another dispatch already assigned the issue" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] issue_claimable_for_start false once another dispatch already assigned the issue"
fi

ISSUE_STATE_STUB="CLOSED"
_issue_assignee_logins() { echo ""; }
if issue_claimable_for_start "82"; then
  echo "[FAIL] issue_claimable_for_start should be false for a closed issue" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] issue_claimable_for_start false for a closed issue"
fi

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
