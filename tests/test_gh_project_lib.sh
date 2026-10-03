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

# --- Test 8: real_label_names reports EVERY label (#4134). The ---
# --- "workflow-control" class it used to filter existed for exactly one ---
# --- name, `usage-limit-retry`, which only existed because two workflows ---
# --- created it -- and the exemption was the hiding place the next invented ---
# --- label would have used. Both are retired; the one-label invariant is ---
# --- now enforced over a name-blind count. ---
LABELS_TWO='[{"name":"Acceptance Failure"},{"name":"usage-limit-retry"}]'
REAL_LABELS="$(real_label_names "$LABELS_TWO")"
_assert_eq "nothing is exempt from the count any more" \
  "2" "$(printf '%s\n' "$REAL_LABELS" | grep -c . || true)"
_assert_contains "both names are reported" "$REAL_LABELS" "usage-limit-retry"

LABELS_NORMAL='[{"name":"Feature"}]'
_assert_eq "a normal single-label issue is unaffected" \
  "Feature" "$(real_label_names "$LABELS_NORMAL")"

# `Escalation` is a label like any other here, which is what guarantees
# hygiene never stamps `Feature` beside it: hygiene asks "how many labels?",
# and a name-blind count cannot get that wrong for a name it has never heard
# of (#3390/#3413/#3415, and #4134's hygiene criterion).
_assert_eq "an escalated issue reads as having exactly one label" \
  "Escalation" "$(real_label_names '[{"name":"Escalation"}]')"

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
# The REST open/closed read, stubbed at the helper rather than at `gh` -- the
# same seam `_issue_assignee_logins` uses above. Defaults to OPEN so every
# lane/assigner assertion below still exercises the rule it names.
CLAIM_ISSUE_STATE="OPEN"
_issue_open_state() { echo "$CLAIM_ISSUE_STATE"; }
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

# --- #3956: a CLOSED issue is not claimable, whatever lane it is in. ---
# The dispatcher's own closed-issue refusal (#3825/#3906,
# assign_and_trigger_developer) is point-in-time and cannot see a merge that
# lands after it. On #3956 a conflict round was dispatched at 19:13:22Z, the
# runner queue held the job 28 minutes, PR #3965 merged and closed the issue
# at 19:36:31Z, and the job claimed it at 19:43:05Z -- then, seeing a MERGED
# PR, ran the acceptance-failure path on an issue with no reported failure.
_claim_closed() { # <status> <assigner> -> "<rc>:<stdout>"
  CLAIM_ISSUE_STATE="CLOSED"
  local r; r="$(_claim "$1" "$2")"
  CLAIM_ISSUE_STATE="OPEN"
  echo "$r"
}

_assert_eq "a closed issue in a claimable lane is refused (#3956)" \
  "3:" "$(_claim_closed "Backlog" "$HUMAN_OWNER")"
_assert_eq "...and writes no status, so the board keeps its post-merge lane" \
  "0" "$(_claim_writes)"
_assert_eq "a closed issue handed back as rework is refused too" \
  "3:" "$(_claim_closed "In Review" "$REVIEW_AGENT")"
_assert_eq "the already-In-Progress shortcut does not smuggle a closed issue through" \
  "3:" "$(_claim_closed "In Progress" "$REVIEW_AGENT")"
CLAIM_ISSUE_STATE=""
_assert_eq "an unreadable state fails open -- a REST hiccup must not stall dispatch" \
  "0:In Progress" "$(_claim "Backlog" "$HUMAN_OWNER")"
CLAIM_ISSUE_STATE="OPEN"

rm -f "$CLAIM_WRITES"
unset -f issue_status set_issue_status _issue_open_state

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

# The matrix consults the `Escalation` label first (#4134) -- an escalated
# issue is skipped in every lane, because an escalation does not move the
# lane. Default to "not escalated" so each case below states only the
# assignee it is actually varying; the escalated case is asserted at the end.
issue_labels_json() { echo '["Feature"]'; }

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

# --- Test 13: issue_labels_json (#4134) -- the one read every escalation ---
# --- gate depends on. It must answer with a JSON ARRAY or refuse: a ---
# --- caller that cannot tell "no labels" from "could not ask" treats an ---
# --- unreadable issue as unescalated and dispatches work the owner holds. ---
REPO_OWNER="test-owner"
REPO_NAME="test-repo"
HUMAN_OWNER="dkblinux98"

# Test 11 above stubs issue_labels_json to isolate the claim matrix from the
# label read; this test IS the label read, so put the real one back.
unset -f issue_labels_json
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

gh() { echo '["Feature"]'; }
_assert_eq "issue_labels_json passes a JSON array through" \
  '["Feature"]' "$(issue_labels_json 4134)"

gh() { echo '{"message":"Not Found"}'; }
rc=0
issue_labels_json 4134 >/dev/null 2>&1 || rc=$?
_assert_eq "issue_labels_json refuses a non-array answer" "1" "$rc"

gh() { return 1; }
rc=0
issue_labels_json 4134 >/dev/null 2>&1 || rc=$?
_assert_eq "issue_labels_json refuses a failed fetch" "1" "$rc"

# --- Test 13b: issue_escalation_state (#4134) -- three answers, because ---
# --- each of them authorizes something different ---
issue_labels_json() { echo '["Escalation"]'; }
_assert_eq "an Escalation-labeled issue reads yes" "yes" "$(issue_escalation_state 4134)"
issue_labels_json() { echo '["Feature"]'; }
_assert_eq "an ordinary issue reads no" "no" "$(issue_escalation_state 4134)"
issue_labels_json() { return 1; }
_assert_eq "an unreadable issue reads unknown, never no" \
  "unknown" "$(issue_escalation_state 4134)"

# --- Test 13c: issue_effective_labels_json (#4134) -- an escalated issue ---
# --- still reads as its REAL type. Without this an escalated Acceptance ---
# --- Failure classifies as an `original`: the drain gate would park it ---
# --- forever and the promotion sweep would close it as accepted. ---
issue_labels_json() { echo '["Escalation"]'; }
_issue_comment_bodies_json() {
  jq -cn '["🚨 Escalated\n\n<!-- escalation-replaced-label: Acceptance Failure -->"]'
}
_assert_eq "the recorded prior label is substituted back in" \
  '["Acceptance Failure"]' "$(issue_effective_labels_json 4134)"

# No comment thread fetch at all on the normal path: the labels already
# answer the question, and an extra paginated read per held issue is a cost
# the gate pays on every sweep.
issue_labels_json() { echo '["Feature"]'; }
_issue_comment_bodies_json() { echo "[test] must not be called" >&2; return 1; }
_assert_eq "an unescalated issue is passed through untouched" \
  '["Feature"]' "$(issue_effective_labels_json 4134)"

# --- Test 14: escalation_cause_origin (#4134) -- one cause, one ---
# --- escalation. A registry entry alone is NOT enough: the cause is open ---
# --- only while its origin issue still carries `Escalation`, which is ---
# --- what makes "the owner restores the label" the resolution and needs ---
# --- no automation to ever touch that label. ---
RELEASE_ISSUE_NUMBER="3521"
_issue_comment_bodies_json() {
  if [[ "$1" == "3521" ]]; then
    jq -cn '["🚨 Escalation raised on #4100\n\n<!-- escalation-cause: red-head:ci origin=4100 -->"]'
  else
    echo '[]'
  fi
}
issue_escalation_state() { [[ "$1" == "4100" ]] && echo "yes" || echo "no"; }
_assert_eq "a registered cause whose origin is still escalated reports the origin" \
  "4100" "$(escalation_cause_origin "red-head:ci")"
_assert_eq "a different cause reports nothing" \
  "" "$(escalation_cause_origin "conflict:99")"

# The owner answered #4100 and restored its label -> the cause is closed,
# and the next issue to hit it escalates properly instead of being linked to
# a resolved escalation forever.
issue_escalation_state() { echo "no"; }
_assert_eq "a cause whose origin is no longer escalated is closed" \
  "" "$(escalation_cause_origin "red-head:ci")"

RELEASE_ISSUE_NUMBER=""
_assert_eq "no release issue configured: no cause registry, no crash" \
  "" "$(escalation_cause_origin "red-head:ci")"

# --- Test 15: _escalation_replace_label (#4134) -- ADD FIRST, THEN ---
# --- REMOVE. `gh issue edit --add-label` refuses to create a label that ---
# --- does not exist, so if the owner has deleted `Escalation` the add ---
# --- fails and the issue must keep its real label rather than be left ---
# --- bare. This is the ordering the whole function exists for. ---
RELEASE_ISSUE_NUMBER="3521"
# A FILE, not an array: `_escalation_replace_label` is called inside a
# command substitution (it prints the replaced label), so anything the stub
# appends to a shell array dies with the subshell.
LABEL_LOG="$(mktemp)"
_label_calls() { grep -c . "$LABEL_LOG" || true; }
gh() {
  if [[ "$1" == "issue" && "$2" == "edit" ]]; then
    printf '%s\n' "$*" >>"$LABEL_LOG"
    return 0
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}
issue_labels_json() { echo '["Escalation"]'; }   # the post-add verification read
REPLACED="$(_escalation_replace_label 4134 '["Acceptance Failure"]')"
_assert_eq "the replaced label is reported back to the caller" \
  "Acceptance Failure" "$REPLACED"
_assert_contains "Escalation is added first" "$(sed -n 1p "$LABEL_LOG")" "--add-label Escalation"
_assert_contains "the prior label is removed after" "$(sed -n 2p "$LABEL_LOG")" "--remove-label Acceptance Failure"
_assert_eq "exactly one add and one remove" "2" "$(_label_calls)"

# The owner deleted `Escalation`: the add fails, and NOTHING is removed.
: >"$LABEL_LOG"
gh() {
  if [[ "$1" == "issue" && "$2" == "edit" ]]; then
    printf '%s\n' "$*" >>"$LABEL_LOG"
    [[ "$*" == *"--add-label"* ]] && return 1
    return 0
  fi
  return 1
}
rc=0
_escalation_replace_label 4134 '["Acceptance Failure"]' >/dev/null 2>&1 || rc=$?
_assert_eq "a failed add is reported as a failure" "1" "$rc"
_assert_eq "a failed add removes nothing" "1" "$(_label_calls)"

# The add reported success but the label is not there (a silent no-op, the
# failure mode assign_issue_verified exists for). Still refuses to strip.
: >"$LABEL_LOG"
gh() {
  if [[ "$1" == "issue" && "$2" == "edit" ]]; then printf '%s\n' "$*" >>"$LABEL_LOG"; return 0; fi
  return 1
}
issue_labels_json() { echo '["Acceptance Failure"]'; }
rc=0
_escalation_replace_label 4134 '["Acceptance Failure"]' >/dev/null 2>&1 || rc=$?
_assert_eq "an unverified add is reported as a failure" "1" "$rc"
_assert_eq "an unverified add removes nothing" "1" "$(_label_calls)"
rm -f "$LABEL_LOG"

# --- Test 15b: escalate_to_owner (#4134) -- THE escalation step. One ---
# --- call must do all of it: relabel (recording what it replaced), ---
# --- assign the owner VERIFIED, comment with the blast radius, DM once ---
# --- per CAUSE -- and leave the Status lane alone. ---
LANE_WRITES=()
set_issue_status() { LANE_WRITES+=("$*"); }
ASSIGNED=()
assign_issue_verified() { ASSIGNED+=("$1 -> $2"); return 0; }
NOTIFIED=()
notify_human_escalation() { NOTIFIED+=("$*"); return 0; }
POSTED_ISSUES=()
POSTED_BODIES=()
issue_comment() { POSTED_ISSUES+=("$1"); POSTED_BODIES+=("$2"); }
blast_radius_report() { echo "### Blast radius (stub for #$1, cause $2)"; }
issue_labels_json() { echo '["Acceptance Failure"]'; }
_escalation_replace_label() { echo "Acceptance Failure"; }
_issue_comment_bodies_json() { echo '[]'; }

rc=0
escalate_to_owner 4134 "review-escalation" "three cycles, still red" \
  "merge, guide or close PR #4200" "review-cycle-limit:4134" "extra detail" || rc=$?
_assert_eq "escalate_to_owner succeeds when the assignment verifies" "0" "$rc"
_assert_eq "the Status lane is NEVER touched" "0" "${#LANE_WRITES[@]}"
_assert_eq "the owner is assigned, verified" "4134 -> dkblinux98" "${ASSIGNED[0]}"
_assert_contains "the DM is deduped on the CAUSE, not the issue" \
  "${NOTIFIED[0]}" "escalation:review-cycle-limit:4134"
_assert_contains "the comment records the replaced label" \
  "${POSTED_BODIES[0]}" "escalation-replaced-label: Acceptance Failure"
_assert_contains "the comment tells the owner to restore it themselves" \
  "${POSTED_BODIES[0]}" "Restore \`Acceptance Failure\` yourself"
_assert_contains "the comment carries the blast radius" \
  "${POSTED_BODIES[0]}" "Blast radius"
_assert_contains "the caller's own detail is included" \
  "${POSTED_BODIES[0]}" "extra detail"
_assert_contains "the cause is registered on the release tracking issue" \
  "${POSTED_BODIES[1]}" "escalation-cause: review-cycle-limit:4134 origin=4134"
_assert_eq "the cause registration lands on the release issue" "3521" "${POSTED_ISSUES[1]}"

# An issue that is ALREADY escalated is not escalated again: re-recording a
# replaced label would overwrite the FIRST record, which is the one the owner
# has yet to restore.
POSTED_ISSUES=(); POSTED_BODIES=(); ASSIGNED=(); NOTIFIED=()
issue_labels_json() { echo '["Escalation"]'; }
rc=0
escalate_to_owner 4134 "review-escalation" "d" "a" "c" >/dev/null 2>&1 || rc=$?
_assert_eq "re-escalating an escalated issue is a no-op success" "0" "$rc"
_assert_eq "nothing is commented" "0" "${#POSTED_BODIES[@]}"
_assert_eq "nothing is reassigned" "0" "${#ASSIGNED[@]}"
_assert_eq "nothing is re-notified" "0" "${#NOTIFIED[@]}"

# A SECOND issue broken by the SAME cause: labelled and assigned (so it is
# paused too), linked to the origin, and NOT diagnosed again. This is the
# collapse that replaces the retired count-of-2 pause -- one escalation per
# cause, and unrelated work keeps dispatching because nothing touches it.
POSTED_ISSUES=(); POSTED_BODIES=(); ASSIGNED=(); NOTIFIED=()
issue_labels_json() { echo '["Feature"]'; }
escalation_cause_origin() { echo "4100"; }
BLAST_CALLS=0
blast_radius_report() { BLAST_CALLS=$((BLAST_CALLS + 1)); echo "### Blast radius"; }
rc=0
escalate_to_owner 4200 "red-head" "same broken check" "fix the check" "red-head:ci" || rc=$?
_assert_eq "the linked escalation succeeds" "0" "$rc"
_assert_eq "the second issue is NOT diagnosed again" "0" "$BLAST_CALLS"
_assert_contains "it points at the origin escalation" "${POSTED_BODIES[0]}" "same cause as #4100"
_assert_eq "it is still assigned to the owner (so it is paused too)" \
  "4200 -> dkblinux98" "${ASSIGNED[0]}"
_assert_eq "the cause is not re-registered" "1" "${#POSTED_BODIES[@]}"
_assert_contains "the DM dedup key is the cause, so no second DM is sent" \
  "${NOTIFIED[0]}" "escalation:red-head:ci"

# A failed owner assignment is reported as a failure -- the whole point of
# escalating is that someone finds out (#3332) -- while the label and the
# comment still stand.
POSTED_BODIES=()
escalation_cause_origin() { echo ""; }
assign_issue_verified() { return 1; }
rc=0
escalate_to_owner 4300 "red-head" "d" "a" "cause-x" >/dev/null 2>&1 || rc=$?
_assert_eq "an unverified owner assignment fails the escalation call" "1" "$rc"

# No owner configured: refuses rather than silently labelling an issue
# nobody was handed.
HUMAN_OWNER=""
rc=0
escalate_to_owner 4300 "s" "d" "a" "c" >/dev/null 2>&1 || rc=$?
_assert_eq "no HUMAN_OWNER configured: escalate_to_owner refuses" "1" "$rc"
HUMAN_OWNER="dkblinux98"

unset -f set_issue_status assign_issue_verified notify_human_escalation \
  issue_comment blast_radius_report issue_labels_json _escalation_replace_label \
  _issue_comment_bodies_json escalation_cause_origin issue_escalation_state
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

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
# #3911 put a second family of tokens on this path. Clear them explicitly:
# inherited from a real environment they would make the degradation
# assertions below pass or fail for reasons that have nothing to do with the
# code under test.
AGENT_ROLE=""
SLACK_USER_TOKEN_DEV=""
SLACK_USER_TOKEN_REVIEW=""
SLACK_USER_TOKEN_SCRUM=""
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

# --- Test 17b: notify_human_escalation attribution (#3911) -- the DM is  ---
# --- sent under the RAISING AGENT's Slack identity, and the fallback to  ---
# --- the shared bot is preserved so attribution can never cost a         ---
# --- notification (#3695's guarantee outranks #3911's sender name).      ---
#
# The stub records the bearer token of every call, because "which identity
# sent it" is the entire behaviour here and it is invisible in the response.
SLACK_USER_ID="U12345"
DEV_AGENT="myGPT-developer-agent"
REVIEW_AGENT="myGPT-review-agent"
SCRUM_AGENT="myGPT-scrummaster-agent"
_slack_notify_recent() { return 1; } # never a duplicate for these cases
AUTH_LOG="$(mktemp)"
TEXT_LOG="$(mktemp)"

# Records each call's bearer token and message text, and answers according to
# SLACK_STUB_FAIL_TOKEN (which token, if any, Slack should reject).
curl() {
  local arg prev="" token="" text=""
  for arg in "$@"; do
    case "$arg" in
      "Authorization: Bearer "*) token="${arg#Authorization: Bearer }" ;;
    esac
    [[ "$prev" == "-d" ]] && text="$arg"
    prev="$arg"
  done
  echo "$token" >>"$AUTH_LOG"
  echo "$text" >>"$TEXT_LOG"
  if [[ -n "${SLACK_STUB_FAIL_TOKEN:-}" && "$token" == "$SLACK_STUB_FAIL_TOKEN" ]]; then
    echo '{"ok":false,"error":"not_allowed_token_type"}'
  else
    echo '{"ok":true}'
  fi
}

_reset_attribution_case() {
  : >"$AUTH_LOG"
  : >"$TEXT_LOG"
  COMMENT_CALLS=()
  SLACK_STUB_FAIL_TOKEN=""
  AGENT_ROLE=""
  SLACK_BOT_TOKEN="xoxb-bot"
  SLACK_USER_TOKEN_DEV=""
  SLACK_USER_TOKEN_REVIEW=""
  SLACK_USER_TOKEN_SCRUM=""
}

# 1. The developer agent's escalation goes out under the developer identity.
_reset_attribution_case
AGENT_ROLE="dev"
SLACK_USER_TOKEN_DEV="xoxp-dev"
notify_human_escalation "42" "FATAL" "diag" "action"
_assert_eq "attribution: exactly one Slack call (no bot retry after success)" "1" "$(wc -l <"$AUTH_LOG")"
_assert_eq "attribution: sent with the developer agent's user token, not the bot's" \
  "xoxp-dev" "$(head -1 "$AUTH_LOG")"
_assert_contains "attribution: the message body names the raising agent" \
  "$(cat "$TEXT_LOG")" "Raised by:* @myGPT-developer-agent"
_assert_contains "attribution: the marker comment records the sending identity" \
  "${COMMENT_CALLS[0]}" "as @myGPT-developer-agent"

# 2. Each role reaches for its own token -- review and scrum are not aliases
#    of the developer path.
_reset_attribution_case
AGENT_ROLE="review"
SLACK_USER_TOKEN_REVIEW="xoxp-review"
SLACK_USER_TOKEN_DEV="xoxp-dev"
notify_human_escalation "42" "review-escalation" "diag" "action"
_assert_eq "attribution: the review agent sends with the review token" \
  "xoxp-review" "$(head -1 "$AUTH_LOG")"

_reset_attribution_case
AGENT_ROLE="scrummaster-agent" # the long spelling resolves too
SLACK_USER_TOKEN_SCRUM="xoxp-scrum"
notify_human_escalation "42" "queue-blocked" "diag" "action"
_assert_eq "attribution: the scrummaster sends with the scrum token" \
  "xoxp-scrum" "$(head -1 "$AUTH_LOG")"

# 3. THE LOAD-BEARING CASE: a user token Slack refuses must not swallow the
#    escalation. The bot retry runs and the owner is still notified.
_reset_attribution_case
AGENT_ROLE="dev"
SLACK_USER_TOKEN_DEV="xoxp-dev"
SLACK_STUB_FAIL_TOKEN="xoxp-dev"
notify_human_escalation "42" "FATAL" "diag" "action"
_assert_eq "rejected user token: both identities are tried" "2" "$(wc -l <"$AUTH_LOG")"
_assert_eq "rejected user token: the retry uses the bot token" "xoxb-bot" "$(tail -1 "$AUTH_LOG")"
_assert_eq "rejected user token: the owner is still notified (marker posted)" \
  "1" "${#COMMENT_CALLS[@]}"
_assert_contains "rejected user token: the marker says the bot sent it on the agent's behalf" \
  "${COMMENT_CALLS[0]}" "from the shared bot identity, on behalf of @myGPT-developer-agent"

# 4. An agent token with no bot token configured still delivers -- the
#    pre-#3911 code required SLACK_BOT_TOKEN specifically.
_reset_attribution_case
SLACK_BOT_TOKEN=""
AGENT_ROLE="review"
SLACK_USER_TOKEN_REVIEW="xoxp-review"
notify_human_escalation "42" "review-escalation" "diag" "action"
_assert_eq "no bot token: the agent identity alone is enough to send" \
  "xoxp-review" "$(head -1 "$AUTH_LOG")"
_assert_eq "no bot token: the marker comment is still posted" "1" "${#COMMENT_CALLS[@]}"

# 5. An unrecognized role attributes nothing rather than guessing, and falls
#    straight through to the bot -- exactly the pre-#3911 behaviour.
_reset_attribution_case
AGENT_ROLE="huddle"
SLACK_USER_TOKEN_DEV="xoxp-dev"
notify_human_escalation "42" "FATAL" "diag" "action"
_assert_eq "unknown role: sent as the bot, never as a guessed agent" \
  "xoxb-bot" "$(head -1 "$AUTH_LOG")"
if grep -q "Raised by" "$TEXT_LOG"; then
  echo "[FAIL] unknown role: must not claim an agent it could not identify" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] unknown role: no 'Raised by' line invented"
fi

# 6. Both identities failing still degrades to comment-only, unchanged.
_reset_attribution_case
AGENT_ROLE="dev"
SLACK_USER_TOKEN_DEV="xoxp-dev"
curl() { echo '{"ok":false,"error":"channel_not_found"}'; }
if notify_human_escalation "42" "FATAL" "diag" "action"; then
  echo "[ok] both identities failing: still returns 0 (never blocks the caller)"
else
  echo "[FAIL] both identities failing: notify_human_escalation must always return 0" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "both identities failing: no marker comment (no false record of success)" \
  "0" "${#COMMENT_CALLS[@]}"

rm -f "$AUTH_LOG" "$TEXT_LOG"
unset SLACK_STUB_FAIL_TOKEN
AGENT_ROLE=""
SLACK_USER_TOKEN_DEV=""
SLACK_USER_TOKEN_REVIEW=""
SLACK_USER_TOKEN_SCRUM=""

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
