#!/usr/bin/env bash
set -uo pipefail

# tests/test_sprint_autopilot_lib.sh
# Regression tests for the sprint-autopilot helpers added to
# scripts/agents/lib/gh_project.sh for #3480: count_sprint_backlog_open
# (the autopilot stop-condition data source), sprint_autopilot_paused (the
# PAUSE_SPRINT kill switch), and clear_project_field_value (used by
# scrummaster_sprint_reorg_apply.sh). Stubs `graphql`/`gh` so no network
# calls happen; count_sprint_backlog_open still runs the real
# summarize_backlog_page.py subprocess end-to-end.
#
# Usage: bash tests/test_sprint_autopilot_lib.sh

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

# shellcheck disable=SC2034
REPO_OWNER="test-owner"
# shellcheck disable=SC2034
REPO_NAME="test-repo"
STATUS_FIELD="Status"
STATUS_BACKLOG="Backlog"

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

_issue_page() {
  # $1=number $2=status $3=sprint_title (or empty for none)
  local number="$1" status="$2" sprint="$3"
  local sprint_fv=""
  if [[ -n "$sprint" ]]; then
    sprint_fv=',{"__typename":"ProjectV2ItemFieldIterationValue","field":{"name":"Sprint"},"title":"'"$sprint"'"}'
  fi
  cat <<EOF
{"content":{"__typename":"Issue","number":${number},"state":"OPEN"},"fieldValues":{"nodes":[{"__typename":"ProjectV2ItemFieldSingleSelectValue","field":{"name":"Status"},"name":"${status}"}${sprint_fv}]}}
EOF
}

_page_response() {
  # $1=has_next(true/false) $2=cursor $3...=item json strings
  local has_next="$1" cursor="$2"
  shift 2
  local items
  items="$(IFS=,; echo "$*")"
  cat <<EOF
{"data":{"node":{"items":{"pageInfo":{"hasNextPage":${has_next},"endCursor":"${cursor}"},"nodes":[${items}]}}}}
EOF
}

# count_sprint_backlog_open's own result is returned via command
# substitution (a subshell), so a plain variable increment inside the
# stubbed graphql() wouldn't be visible to the parent shell for the
# call-count assertions below -- same subshell pitfall documented in
# test_gh_project_lib.sh. Use a temp-file counter instead.
GRAPHQL_CALLS_FILE="$(mktemp)"
trap 'rm -f "$GRAPHQL_CALLS_FILE"' EXIT
_graphql_calls() { cat "$GRAPHQL_CALLS_FILE" 2>/dev/null || echo 0; }
_bump_graphql_calls() { echo "$(($(_graphql_calls) + 1))" > "$GRAPHQL_CALLS_FILE"; }

# --- Test 1: count_sprint_backlog_open counts only Backlog+OPEN issues in ---
# --- the given sprint, across a single page ---
get_project_id() { echo "proj-1"; }
echo 0 > "$GRAPHQL_CALLS_FILE"
graphql() {
  _bump_graphql_calls
  _page_response "false" "" \
    "$(_issue_page 1 Backlog "Sprint 6")" \
    "$(_issue_page 2 Backlog "Sprint 5")" \
    "$(_issue_page 3 "In Progress" "Sprint 6")" \
    "$(_issue_page 4 Backlog "Sprint 6")"
}

result="$(count_sprint_backlog_open "Sprint" "Sprint 6")"
_assert_eq "counts only Backlog+OPEN issues in the active sprint" "2" "$result"
_assert_eq "made exactly one page request" "1" "$(_graphql_calls)"

# --- Test 2: count_sprint_backlog_open sums across multiple pages ---
echo 0 > "$GRAPHQL_CALLS_FILE"
graphql() {
  _bump_graphql_calls
  if [[ "$(_graphql_calls)" -eq 1 ]]; then
    _page_response "true" "cursor-1" \
      "$(_issue_page 10 Backlog "Sprint 6")" \
      "$(_issue_page 11 Backlog "Sprint 6")"
  else
    _page_response "false" "" \
      "$(_issue_page 12 Backlog "Sprint 6")"
  fi
}

result="$(count_sprint_backlog_open "Sprint" "Sprint 6")"
_assert_eq "sums backlog_open across pages" "3" "$result"
_assert_eq "fetched two pages" "2" "$(_graphql_calls)"

# --- Test 3: count_sprint_backlog_open returns 0 when the sprint has no ---
# --- open Backlog issues (autopilot's "sprint complete" branch) ---
graphql() {
  _page_response "false" "" \
    "$(_issue_page 20 "In Review" "Sprint 6")" \
    "$(_issue_page 21 "For Release" "Sprint 6")"
}
result="$(count_sprint_backlog_open "Sprint" "Sprint 6")"
_assert_eq "zero remaining backlog issues" "0" "$result"

# --- Test 4: sprint_autopilot_paused reads the most recent PAUSE/RESUME ---
# --- control comment (kill switch, #3480) ---
#
# The mock below simulates real `gh api ... --paginate --jq FILTER`
# semantics rather than pre-computing the answer in Python: it splits
# MOCK_COMMENTS_JSON into two "pages" and runs the *actual* --jq FILTER
# (via the real jq binary) against each page separately, exactly like `gh`
# does across real HTTP pages -- then gh_project.sh's own second `jq -s`
# pass combines the two pages' streamed output. This exercises the real
# two-stage pipeline end to end, so a regression that moves `last`/`length`
# back into the per-page --jq (the #3663 pagination bug) is caught here
# instead of being masked by a mock that bypasses jq entirely.
gh() {
  local filter=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--jq" ]]; then
      filter="$2"
      shift
    fi
    shift
  done
  local n half
  n="$(jq 'length' <<<"$MOCK_COMMENTS_JSON")"
  half=$(((n + 1) / 2))
  jq -c ".[0:$half]" <<<"$MOCK_COMMENTS_JSON" | jq -r "$filter"
  jq -c ".[$half:]" <<<"$MOCK_COMMENTS_JSON" | jq -r "$filter"
}

MOCK_COMMENTS_JSON='[{"body":"some other comment","created_at":"2026-07-01T00:00:00Z"},{"body":"PAUSE_SPRINT","created_at":"2026-07-02T00:00:00Z"}]'
if sprint_autopilot_paused "2759"; then
  echo "[ok] sprint_autopilot_paused is true when PAUSE_SPRINT is the latest control comment"
else
  echo "[FAIL] sprint_autopilot_paused should be true after a PAUSE_SPRINT comment" >&2
  FAILURES=$((FAILURES + 1))
fi

MOCK_COMMENTS_JSON='[{"body":"PAUSE_SPRINT","created_at":"2026-07-02T00:00:00Z"},{"body":"RESUME_SPRINT","created_at":"2026-07-03T00:00:00Z"}]'
if sprint_autopilot_paused "2759"; then
  echo "[FAIL] sprint_autopilot_paused should be false once RESUME_SPRINT is the latest control comment" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] sprint_autopilot_paused is false after a later RESUME_SPRINT comment"
fi

MOCK_COMMENTS_JSON='[]'
if sprint_autopilot_paused "2759"; then
  echo "[FAIL] sprint_autopilot_paused should default to false with no control comments" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] sprint_autopilot_paused defaults to false with no PAUSE_SPRINT/RESUME_SPRINT comments"
fi

# --- Test 4b: the true latest control comment sits on a *later page* than ---
# --- another match on an earlier page (the exact #3663 regression shape: ---
# --- `last` must be computed across the combined pages, not per page) ---
MOCK_COMMENTS_JSON='[{"body":"noise","created_at":"2026-07-01T00:00:00Z"},{"body":"RESUME_SPRINT","created_at":"2026-07-02T00:00:00Z"},{"body":"PAUSE_SPRINT","created_at":"2026-07-03T00:00:00Z"}]'
if sprint_autopilot_paused "2759"; then
  echo "[ok] sprint_autopilot_paused is true when the latest PAUSE_SPRINT falls on a later page than an earlier RESUME_SPRINT"
else
  echo "[FAIL] sprint_autopilot_paused should be true: PAUSE_SPRINT (page 2) is chronologically after RESUME_SPRINT (page 1)" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- Test 5: clear_project_field_value resolves the field id and sends a ---
# --- clearProjectV2ItemFieldValue mutation ---
field_id_by_name() { echo "field-123"; }
CAPTURED_QUERY=""
CAPTURED_ARGS=""
graphql() {
  CAPTURED_QUERY="$1"
  shift
  CAPTURED_ARGS="$*"
  echo '{"data":{"clearProjectV2ItemFieldValue":{"projectV2Item":{"id":"item-1"}}}}'
}

clear_project_field_value "item-1" "Sprint" >/dev/null
case "$CAPTURED_QUERY" in
  *clearProjectV2ItemFieldValue*) echo "[ok] clear_project_field_value sends a clearProjectV2ItemFieldValue mutation" ;;
  *)
    echo "[FAIL] clear_project_field_value did not send the expected mutation" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac
case "$CAPTURED_ARGS" in
  *"project=proj-1"*"item=item-1"*"field=field-123"*) echo "[ok] clear_project_field_value passes resolved project/item/field ids" ;;
  *)
    echo "[FAIL] clear_project_field_value args were '$CAPTURED_ARGS'" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

# --- Test 6: release_backlog_by_sprint buckets the release's open Backlog ---
# --- issues by sprint title, summed across pages (#3706 park-note input) ---
_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected to find '$needle' in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: did not expect '$needle' in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

echo 0 > "$GRAPHQL_CALLS_FILE"
graphql() {
  _bump_graphql_calls
  if [[ "$(_graphql_calls)" -eq 1 ]]; then
    _page_response "true" "cursor-1" \
      "$(_issue_page 30 Backlog "Sprint 8")" \
      "$(_issue_page 31 Backlog "Sprint 9")" \
      "$(_issue_page 32 "In Progress" "Sprint 9")"
  else
    _page_response "false" "" \
      "$(_issue_page 33 Backlog "Sprint 9")" \
      "$(_issue_page 34 Backlog "")"
  fi
}
result="$(release_backlog_by_sprint "" "Sprint")"
_assert_eq "release_backlog_by_sprint sums buckets across pages" \
  '{"Sprint 8":1,"Sprint 9":2,"":1}' "$result"

# --- Tests 7-9: sprint_autopilot_kick is SPRINT-gated, not release-gated ---
# --- (#3706). The release still has Sprint 9 work in every case below; ---
# --- only the ACTIVE sprint's pool decides continue vs park. ---
SPRINT_AUTOPILOT="true"
RELEASE_ISSUE_NUMBER="2759"
SPRINT_FIELD="Sprint"
sprint_autopilot_paused() { return 1; }
release_version_from_issue() { echo "v3.0.0"; }
release_backlog_by_sprint() { echo '{"Sprint 8":0,"Sprint 9":11,"":2}'; }

COMMENT_FILE="$(mktemp)"
trap 'rm -f "$GRAPHQL_CALLS_FILE" "$COMMENT_FILE"' EXIT
issue_comment() { printf '%s' "$2" > "$COMMENT_FILE"; }

# Test 7: active sprint still has Backlog work -> kick.
iteration_active_title() { echo "Sprint 8"; }
count_sprint_backlog_open() { echo "4"; }
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "kicks while the active sprint has open Backlog work" "$body" "READY_FOR_NEXT_ISSUE"
_assert_contains "kick names the active sprint, not the release, as the pool" "$body" 'Sprint "Sprint 8" still has 4 open Backlog issue(s)'

# Test 8: active sprint drained, release NOT drained -> park (the #3706
# behavior change: the old release-gated decision kicked here).
#
# The "no kick" assertion is on the BARE token, not a token+newline needle:
# notify_scrum_ready.yml triggers on a plain substring test, so any
# occurrence anywhere in the body -- prose included -- dispatches work. The
# original park note named the token in its override line and therefore
# kicked every time it "parked" (#3706 review).
count_sprint_backlog_open() { echo "0"; }
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_not_contains "parks at the sprint boundary even though the release has work left" "$body" "READY_FOR_NEXT_ISSUE"
_assert_contains "park note carries the informational marker that excludes it from dispatch" "$body" "nyxgpt-autopilot-informational"
_assert_contains "park note says the sprint is complete" "$body" "Sprint Autopilot — sprint complete"
_assert_contains "park note names acceptance testing as the next step" "$body" "Next step: acceptance testing of this sprint"
_assert_contains "park note reports what waits in a future sprint" "$body" "- Sprint 9: 11 open Backlog issue(s)"
_assert_contains "park note reports the no-sprint bucket" "$body" "- _No sprint set_: 2 open Backlog issue(s)"
_assert_contains "park note totals the work waiting outside the sprint" "$body" "**13**"
_assert_contains "park note points at the docs for the manual-kick override" "$body" 'documented in `docs/sprint-autopilot.md`'

# Test 9: no active iteration at all -> conservative park, no kick. The
# no-sprint bucket must still be reported here: with no active sprint the
# "" key is the *no sprint set* bucket, not the active sprint, so excluding
# it hid waiting work and undercounted the total (#3706 review).
iteration_active_title() { echo ""; }
count_sprint_backlog_open() { echo "7"; }  # must be ignored: nothing is active
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "parks when no sprint iteration is active" "$body" "No sprint iteration is currently active"
_assert_not_contains "posts no kick when no sprint iteration is active" "$body" "READY_FOR_NEXT_ISSUE"
_assert_contains "no-active-sprint park note keeps the no-sprint bucket" "$body" "- _No sprint set_: 2 open Backlog issue(s)"
_assert_contains "no-active-sprint park note totals every waiting bucket" "$body" "**13**"

# Test 10: the PAUSE_SPRINT notice is informational too -- it must not name
# the kick token (notify_scrum_ready.yml is not gated on PAUSE_SPRINT, so a
# paused notice that named it dispatched work despite the pause).
sprint_autopilot_paused() { return 0; }
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "paused notice is posted" "$body" 'autopilot is paused'
_assert_not_contains "paused notice does not name the kick token" "$body" "READY_FOR_NEXT_ISSUE"
_assert_contains "paused notice carries the informational marker" "$body" "nyxgpt-autopilot-informational"
sprint_autopilot_paused() { return 1; }

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
