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
# Deliberately NOT named DISPATCH_FILE: Test 23 already uses that name for
# the RC-publish dispatch mock, and sharing it would make a kick assertion
# read another feature's writes.
KICK_FILE="$(mktemp)"
trap 'rm -f "$GRAPHQL_CALLS_FILE" "$COMMENT_FILE" "$KICK_FILE"' EXIT
issue_comment() { printf '%s' "$2" > "$COMMENT_FILE"; }
# Since #3882 the kick is a repository_dispatch, not a token in the note.
# "Did it kick?" is therefore a question about the event the helper sends --
# asking it of the comment body is what made these assertions stale, and is
# the same category error (prose as an API) that #3706 and #3790 were.
dispatch_next_issue() { printf '%s\n' "${1:-}" >> "$KICK_FILE"; return 0; }
_dispatched() { [[ -s "$KICK_FILE" ]] && echo yes || echo no; }
_reset_dispatch() { : > "$KICK_FILE"; }

# The park decision now reads the sprint's whole population (#3709), so
# these tests stub sprint_population_snapshot instead of the Backlog-only
# count, and stub the parked-issue scan (its own tests are below).
STATUS_FOR_RELEASE="For Release"
STATUS_IN_PROGRESS="In Progress"
_snapshot='{"open":{},"closed":{}}'
sprint_population_snapshot() { echo "$_snapshot"; }
autopilot_scan_parked() { echo '{"resumable":[],"waiting":[],"exhausted":[],"active":[],"selected":null}'; }
_autopilot_post_resume() { echo "$1" > "$RESUME_FILE"; }
RESUME_FILE="$(mktemp)"
# `trap` replaces, it does not accumulate: this line used to drop the files
# the earlier trap covered, leaking one temp file per run for each.
trap 'rm -f "$GRAPHQL_CALLS_FILE" "$COMMENT_FILE" "$KICK_FILE" "$RESUME_FILE"' EXIT

# Test 7: active sprint still has Backlog work -> kick.
iteration_active_title() { echo "Sprint 8"; }
count_sprint_backlog_open() { echo "4"; }
_snapshot='{"open":{"Backlog":[1,2,3,4]},"closed":{}}'
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "kicks while the active sprint has open Backlog work" "yes" "$(_dispatched)"
_assert_contains "note names the active sprint, not the release, as the pool" "$body" 'Sprint "Sprint 8" has 4 open item(s)'

# Test 7b (#3709): the continue kick still reports parked issues waiting on
# gates -- they must never be silently dropped, park or kick.
autopilot_scan_parked() { echo '{"resumable":[],"waiting":[{"issue":3516,"open_blockers":[3514]}],"exhausted":[],"active":[],"selected":null}'; }
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "continue kick reports issues waiting on gates" "$body" "#3516 (waiting on #3514)"
_assert_eq "continue kick still dispatches" "yes" "$(_dispatched)"
autopilot_scan_parked() { echo '{"resumable":[],"waiting":[],"exhausted":[],"active":[],"selected":null}'; }

# Test 8 (#3709): Backlog empty but work still In Progress / In Review. The
# pre-#3709 decision announced "sprint complete -- acceptance next" here,
# which was the reported defect: the sprint is demonstrably unfinished.
#
# The "no kick" assertion asks whether the dispatch event was sent. It used
# to test the note's text for the kick token, because the workflow triggered
# on a plain substring match -- so the original park note named the token in
# its override line and kicked every time it "parked" (#3706 review). That
# whole failure mode is gone with the comment trigger (#3882); what is left
# to assert is that a park sends no event.
count_sprint_backlog_open() { echo "0"; }
_snapshot='{"open":{"In Progress":[3513],"In Review":[3514]},"closed":{"Acceptance Testing":[3510]}}'
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "parks at the sprint boundary even though the release has work left" "no" "$(_dispatched)"
_assert_contains "park note carries the informational marker that excludes it from dispatch" "$body" "nyxgpt-autopilot-informational"
_assert_contains "in-flight park note does not claim completion" "$body" "work still in flight"
_assert_contains "in-flight park note counts every open issue, not just Backlog" "$body" "2 issue(s) are still open"
_assert_not_contains "in-flight park note never says sprint complete" "$body" "sprint complete"
_assert_contains "park note reports what waits in a future sprint" "$body" "- Sprint 9: 11 open Backlog issue(s)"
_assert_contains "park note reports the no-sprint bucket" "$body" "- _No sprint set_: 2 open Backlog issue(s)"
_assert_contains "park note totals the work waiting outside the sprint" "$body" "**13**"
# The owner override is the assignment lever (#3882), not a comment kick:
# the note must name the action and still point at the doc explaining it.
_assert_contains "park note names the manual-start override" "$body" "assigns the developer agent to an issue"
_assert_contains "park note points at the docs for the manual-start override" "$body" 'docs/sprint-autopilot.md'

# Test 8b (#3709): everything closed, but not everything accepted ->
# "agentic work complete", explicitly NOT sprint completion (owner
# definition, 2026-08-10).
_snapshot='{"open":{},"closed":{"Acceptance Testing":[3510,3513],"For Release":[3509]}}'
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "all-closed sprint reports agentic work complete" "$body" "agentic work complete; awaiting owner acceptance"
_assert_contains "agentic-complete note lists what awaits acceptance" "$body" "#3510, #3513"
_assert_contains "agentic-complete note denies sprint completion" "$body" 'This is not "sprint complete."'
_assert_eq "agentic-complete note posts no kick" "no" "$(_dispatched)"

# Test 8c (#3709): every item accepted to For Release -> the only state
# that may call the sprint done.
_snapshot='{"open":{},"closed":{"For Release":[3509,3510]}}'
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "sprint-complete note requires every item in For Release" "$body" "Sprint Autopilot — sprint complete"
_assert_contains "sprint-complete note lists the accepted items" "$body" "#3509, #3510"
_assert_eq "sprint-complete note posts no kick" "no" "$(_dispatched)"

# Test 8d (#3709): a parked issue whose gates have all closed is resumed
# before the park, and the park note says so.
autopilot_scan_parked() { echo '{"resumable":[{"issue":3513,"open_blockers":[]}],"waiting":[{"issue":3516,"open_blockers":[3514]}],"exhausted":[],"active":[],"selected":3513}'; }
: > "$RESUME_FILE"
_snapshot='{"open":{"In Progress":[3513,3516]},"closed":{}}'
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "auto-resume posts the retry trigger on the selected parked issue" "3513" "$(cat "$RESUME_FILE")"
_assert_contains "park note reports the auto-resumed issue" "$body" "Auto-resumed:** #3513"
_assert_contains "park note reports the issue still waiting on its gate" "$body" "#3516 (waiting on #3514)"
_assert_eq "auto-resume does not turn a park into a kick" "no" "$(_dispatched)"
autopilot_scan_parked() { echo '{"resumable":[],"waiting":[],"exhausted":[],"active":[],"selected":null}'; }

# Test 8e (#3709): if the population snapshot is unavailable, the decision
# degrades to the pre-#3709 Backlog-only count rather than parking a sprint
# that still has work.
sprint_population_snapshot() { return 1; }
count_sprint_backlog_open() { echo "5"; }
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "falls back to the Backlog-only count when the snapshot fails" "yes" "$(_dispatched)"
_assert_contains "fallback kick still names the sprint pool" "$body" 'has 5 open item(s)'
count_sprint_backlog_open() { echo "0"; }
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "fallback park posts no kick" "no" "$(_dispatched)"
_assert_not_contains "fallback park claims no completion state it cannot prove" "$body" "sprint complete"
sprint_population_snapshot() { echo "$_snapshot"; }
_snapshot='{"open":{},"closed":{}}'

# Test 9: no active iteration at all -> conservative park, no kick. The
# no-sprint bucket must still be reported here: with no active sprint the
# "" key is the *no sprint set* bucket, not the active sprint, so excluding
# it hid waiting work and undercounted the total (#3706 review).
iteration_active_title() { echo ""; }
count_sprint_backlog_open() { echo "7"; }  # must be ignored: nothing is active
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "parks when no sprint iteration is active" "$body" "No sprint iteration is currently active"
_assert_eq "posts no kick when no sprint iteration is active" "no" "$(_dispatched)"
_assert_contains "no-active-sprint park note keeps the no-sprint bucket" "$body" "- _No sprint set_: 2 open Backlog issue(s)"
_assert_contains "no-active-sprint park note totals every waiting bucket" "$body" "**13**"

# Test 10: the PAUSE_SPRINT notice is informational too -- it must not name
# the retired kick token (before #3882 the kick was a comment token and the
# pull workflow was not gated on PAUSE_SPRINT, so a paused notice that named
# it dispatched work despite the pause).
sprint_autopilot_paused() { return 0; }
_reset_dispatch
sprint_autopilot_kick 123 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "paused notice is posted" "$body" 'autopilot is paused'
_assert_not_contains "paused notice does not name the kick token" "$body" "READY_FOR_NEXT_ISSUE"
_assert_contains "paused notice carries the informational marker" "$body" "nyxgpt-autopilot-informational"
sprint_autopilot_paused() { return 1; }

# ---------------------------------------------------------------------------
# #3709: sprint population snapshot + parked-issue auto-resume helpers
# ---------------------------------------------------------------------------

# Restore the real implementations the tests above stubbed out.
unset -f sprint_population_snapshot autopilot_scan_parked _autopilot_post_resume
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

_issue_page_state() {
  # $1=number $2=status $3=sprint_title $4=state
  local number="$1" status="$2" sprint="$3" state="$4"
  local sprint_fv=""
  if [[ -n "$sprint" ]]; then
    sprint_fv=',{"__typename":"ProjectV2ItemFieldIterationValue","field":{"name":"Sprint"},"title":"'"$sprint"'"}'
  fi
  cat <<EOF
{"content":{"__typename":"Issue","number":${number},"state":"${state}"},"fieldValues":{"nodes":[{"__typename":"ProjectV2ItemFieldSingleSelectValue","field":{"name":"Status"},"name":"${status}"}${sprint_fv}]}}
EOF
}

# --- Test 11: sprint_population_snapshot buckets the ACTIVE sprint's whole ---
# --- population (open + closed) by Status, merged across pages (#3709) ---
echo 0 > "$GRAPHQL_CALLS_FILE"
get_project_id() { echo "proj-1"; }
graphql() {
  _bump_graphql_calls
  if [[ "$(_graphql_calls)" -eq 1 ]]; then
    _page_response "true" "cursor-1" \
      "$(_issue_page_state 1 Backlog "Sprint 8" OPEN)" \
      "$(_issue_page_state 2 "In Progress" "Sprint 8" OPEN)" \
      "$(_issue_page_state 3 "In Progress" "Sprint 9" OPEN)"
  else
    _page_response "false" "" \
      "$(_issue_page_state 4 "In Review" "Sprint 8" OPEN)" \
      "$(_issue_page_state 5 "Acceptance Testing" "Sprint 8" CLOSED)" \
      "$(_issue_page_state 6 "For Release" "Sprint 8" CLOSED)"
  fi
}
result="$(sprint_population_snapshot "Sprint" "Sprint 8")"
_assert_eq "snapshot buckets open sprint issues by status across pages" \
  '{"Backlog":[1],"In Progress":[2],"In Review":[4]}' "$(jq -c '.open' <<<"$result")"
_assert_eq "snapshot buckets closed sprint issues by status" \
  '{"Acceptance Testing":[5],"For Release":[6]}' "$(jq -c '.closed' <<<"$result")"
_assert_eq "snapshot paged until the cursor ran out" "2" "$(_graphql_calls)"

# --- Test 12: _issue_open_gate_refs reads native blocked_by deps, falling ---
# --- back to the interim prose "Blocked by:" refs ONLY when there are no ---
# --- native edges, and keeping only OPEN blockers. Fallback, not union: ---
# --- native relationships are the only storage (#3731), and prose is read ---
# --- solely so issues filed before that decision still gate correctly. An ---
# --- issue that has a native edge has been converted, and its stale prose ---
# --- must not resurrect a gate the conversion dropped. ---
blocked_by_issues() { [[ "$1" == "3516" ]] && echo "3515"; return 0; }
_issue_open_state() {
  case "$1" in
    3509) echo "CLOSED" ;;
    *) echo "OPEN" ;;
  esac
}
result="$(_issue_open_gate_refs 3516 "Some prose #9999.

Blocked by: #3509 (P6-11), #3514
" | tr '\n' ' ')"
_assert_eq "native deps win outright when present" "3515 " "$result"

# With no native edge, the prose refs are read -- open ones only.
blocked_by_issues() { return 0; }
result="$(_issue_open_gate_refs 3516 "Some prose #9999.

Blocked by: #3509 (P6-11), #3514
" | tr '\n' ' ')"
_assert_eq "falls back to prose gates, dropping closed ones" "3514 " "$result"
_assert_eq "an issue with no declared gates has none open" "" "$(_issue_open_gate_refs 3510 "no gates here")"

# --- Test 13: autopilot_scan_parked classifies the sprint's In Progress ---
# --- issues into resumable / waiting / exhausted / active (#3709) ---
STATUS_IN_PROGRESS="In Progress"
gh() { echo '{"title":"issue title","body":"body"}'; }
_issue_open_pr_numbers() { [[ "$1" == "3509" ]] && echo "4242"; return 0; }
_issue_active_dev_run_ids() { return 0; }
_issue_open_gate_refs() { [[ "$1" == "3516" ]] && echo "3514"; return 0; }
_autopilot_resume_budget() {
  if [[ "$1" == "3515" ]]; then echo '{"exhausted":true}'; else echo '{"exhausted":false}'; fi
}
scan="$(autopilot_scan_parked '{"open":{"In Progress":[3509,3513,3515,3516],"Backlog":[9999]}}')"
_assert_eq "issue with an open PR is active, not parked" '[3509]' "$(jq -c '.active' <<<"$scan")"
_assert_eq "parked issue with a still-open gate waits" '[{"issue":3516,"open_blockers":[3514]}]' "$(jq -c '.waiting' <<<"$scan")"
_assert_eq "parked issue out of auto-resume budget is reported, not retried" '[3515]' "$(jq -c '[.exhausted[].issue]' <<<"$scan")"
_assert_eq "parked, ungated, in-budget issue is the one resumed" "3513" "$(jq -r '.selected' <<<"$scan")"
_assert_eq "Backlog issues are not scanned (dispatch owns those)" "0" "$(jq -r '[.active[], .waiting[], .exhausted[], .resumable[]] | map(select(. == 9999 or (type == "object" and .issue == 9999))) | length' <<<"$scan")"

scan="$(autopilot_scan_parked '{"open":{"Backlog":[1]},"closed":{}}')"
_assert_eq "a sprint with no In Progress issues selects nothing" "null" "$(jq -r '.selected' <<<"$scan")"

# --- Test 14: _autopilot_post_resume re-assigns the developer agent and ---
# --- leaves the budget-marked comment as the record (#3709 / #3689 / #3882). ---
# --- The comment used to BE the trigger, ending in a retry token the ---
# --- developer workflow substring-matched; assignment is the trigger now, ---
# --- and the marker (which parked_resume.py counts) still rides along. ---
_autopilot_resume_budget() { echo '{"count":1,"exhausted":false,"next_resume_number":2,"max_resumes":3}'; }
issue_comment() { printf '%s' "$2" > "$COMMENT_FILE"; echo "$1" > "$RESUME_FILE"; }
RESUME_ASSIGN_CALLS=()
assign_and_trigger_developer() { RESUME_ASSIGN_CALLS+=("$1"); }
_autopilot_post_resume 3513
body="$(cat "$COMMENT_FILE")"
_assert_eq "resume comment is posted on the parked issue itself" "3513" "$(cat "$RESUME_FILE")"
_assert_eq "resume re-assigns the developer agent exactly once" "1" "${#RESUME_ASSIGN_CALLS[@]}"
_assert_eq "resume re-assigns the parked issue" "3513" "${RESUME_ASSIGN_CALLS[0]}"
_assert_not_contains "resume comment issues no retry token" "$body" "RETRY_IMPLEMENTATION"
_assert_contains "resume comment carries the budget marker" "$body" "<!-- nyxgpt-autoresume: issue=3513 n=2 -->"
_assert_contains "resume comment reports where it is in the budget" "$body" "auto-resume (2/3)"

# --- Test 15: _issue_open_pr_numbers uses the plain pulls list (never ---
# --- search/issues, #3694) and matches closing keywords or the branch ---
# --- convention from developer_create_branch.sh ---
# Re-source to restore the real _issue_open_pr_numbers/_issue_active_dev_run_ids
# over the stubs Test 13 installed (bash keeps one definition per name).
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
MOCK_API_JSON='[
  {"number":1,"body":"Closes #3513","head":{"ref":"fix/3513-thing"}},
  {"number":2,"body":"Refs #3513 only","head":{"ref":"fix/9999-other"}},
  {"number":3,"body":"","head":{"ref":"feat/3513-branch-match"}},
  {"number":4,"body":"Fixes #35130","head":{"ref":"fix/35130-longer"}}
]'
gh() {
  local filter="" prev=""
  for arg in "$@"; do
    [[ "$prev" == "--jq" ]] && filter="$arg"
    prev="$arg"
  done
  jq -r "$filter" <<<"$MOCK_API_JSON"
}
result="$(_issue_open_pr_numbers 3513 | tr '\n' ' ')"
_assert_eq "matches closing-keyword bodies and branch-convention heads only" "1 3 " "$result"

# --- Test 16: _issue_active_dev_run_ids attributes runs by display_title ---
# --- (the runs API carries no issue number) and ignores completed runs ---
MOCK_API_JSON='{"workflow_runs":[
  {"id":11,"status":"completed","display_title":"my issue"},
  {"id":12,"status":"in_progress","display_title":"my issue"},
  {"id":13,"status":"queued","display_title":"another issue"}
]}'
result="$(_issue_active_dev_run_ids "my issue" | tr '\n' ' ')"
_assert_eq "only live runs for this issue count as active" "12 " "$result"
_assert_eq "no title means no attribution and no false 'active'" "" "$(_issue_active_dev_run_ids "")"

# ---------------------------------------------------------------------------
# #3729: the rc publish at agentic-work-complete
# ---------------------------------------------------------------------------
# The autopilot cuts the candidate an acceptance round installs the moment the
# sprint parks at agentic-work-complete. What has to be true: it fires on that
# state and no other, it publishes `rc` and can never publish `stable`, and a
# repeat observation of the same parked state cuts no duplicate.

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
RELEASE_BRANCH="v3.0.0"
REPO_OWNER="dkblinux98"
REPO_NAME="nyxGPT"

DISPATCH_FILE="$(mktemp)"
trap 'rm -f "$GRAPHQL_CALLS_FILE" "$COMMENT_FILE" "$RESUME_FILE" "$DISPATCH_FILE"' EXIT

# `gh workflow run ...` is the only gh call these tests exercise; everything
# else the helper needs comes from the stubbed preflight.
gh() {
  if [[ "${1:-}" == "workflow" ]]; then
    printf '%s\n' "$*" > "$DISPATCH_FILE"
    [[ "${MOCK_DISPATCH_FAILS:-0}" == "1" ]] && return 1
    return 0
  fi
  return 1
}
MOCK_DISPATCH_FAILS=0
_dispatch_args() { cat "$DISPATCH_FILE" 2>/dev/null || echo ""; }

_awaiting='{"state":"awaiting_acceptance","awaiting_acceptance":[3510,3513]}'

# --- Test 17: a park that reached agentic-work-complete dispatches rc ---
_autopilot_rc_preflight() { echo '{"dispatch":true,"version":"3.0.0rc2","last_sha":"old","release":"3.0.0","reason":"moved"}'; }
: > "$DISPATCH_FILE"
result="$(_autopilot_publish_rc "$_awaiting")"
_assert_eq "agentic-work-complete dispatches a candidate" "true" "$(jq -r '.dispatched' <<<"$result")"
_assert_eq "the park note is handed the resolved candidate version" "3.0.0rc2" "$(jq -r '.version' <<<"$result")"
_assert_contains "the dispatch names the publish workflow" "$(_dispatch_args)" "release-publish-pypi.yml"
_assert_contains "the dispatch targets the release branch" "$(_dispatch_args)" "--ref v3.0.0"

# --- Test 18: the kick path can only ever publish candidates ---
_assert_contains "the dispatched channel is rc" "$(_dispatch_args)" "channel=rc"
_assert_not_contains "the kick path never dispatches stable" "$(_dispatch_args)" "stable"
_assert_eq "the reported channel is rc" "rc" "$(jq -r '.channel' <<<"$result")"

# Forcing any other channel refuses outright rather than publishing it: a
# release is cut by the ceremony (tag + confirmation), never by this path.
: > "$DISPATCH_FILE"
AUTOPILOT_RC_CHANNEL="stable"
result="$(_autopilot_publish_rc "$_awaiting" 2>/dev/null)"
_assert_eq "a non-rc channel is refused, not published" "false" "$(jq -r '.dispatched' <<<"$result")"
_assert_eq "refusing a non-rc channel dispatches nothing at all" "" "$(_dispatch_args)"
AUTOPILOT_RC_CHANNEL="rc"

# --- Test 19: every other park state publishes nothing ---
for _state in work_in_flight sprint_complete empty ""; do
  : > "$DISPATCH_FILE"
  result="$(_autopilot_publish_rc "{\"state\":\"${_state}\"}")"
  _assert_eq "park state '${_state:-none}' publishes no candidate" "{}" "$result"
  _assert_eq "park state '${_state:-none}' dispatches nothing" "" "$(_dispatch_args)"
done
: > "$DISPATCH_FILE"
result="$(_autopilot_publish_rc "")"
_assert_eq "an unreadable park state publishes nothing" "{}" "$result"

# --- Test 20: an unchanged tip is a no-op that still names the candidate ---
# This is the idempotency the owner decision calls for: repeated observations
# of the same parked state must not produce a second rcN.
_autopilot_rc_preflight() { echo '{"dispatch":false,"version":"3.0.0rc1","last_sha":"same","release":"3.0.0","reason":"v3.0.0 is unchanged since the last published candidate (same) -- no new candidate is needed."}'; }
: > "$DISPATCH_FILE"
result="$(_autopilot_publish_rc "$_awaiting")"
_assert_eq "an unchanged tip dispatches nothing" "" "$(_dispatch_args)"
_assert_eq "an unchanged tip reports the no-op" "true" "$(jq -r '.noop' <<<"$result")"
_assert_eq "the no-op still names what to install" "3.0.0rc1" "$(jq -r '.version' <<<"$result")"

# --- Test 21: an unavailable preflight is "unknown", never "no" ---
# The pipeline carries the same tip guard, so dispatching blind is safe --
# staying silent because a lookup failed would skip the acceptance round's
# candidate entirely.
_autopilot_rc_preflight() { return 1; }
: > "$DISPATCH_FILE"
result="$(_autopilot_publish_rc "$_awaiting")"
_assert_eq "a failed preflight still dispatches" "true" "$(jq -r '.dispatched' <<<"$result")"
_assert_contains "a blind dispatch is still rc" "$(_dispatch_args)" "channel=rc"
_assert_eq "a blind dispatch claims no version it does not know" "" "$(jq -r '.version' <<<"$result")"

# --- Test 22: a refused dispatch is reported, not swallowed ---
_autopilot_rc_preflight() { echo '{"dispatch":true,"version":"3.0.0rc2","last_sha":"old","release":"3.0.0","reason":"moved"}'; }
MOCK_DISPATCH_FAILS=1
result="$(_autopilot_publish_rc "$_awaiting" 2>/dev/null)"
_assert_eq "a refused dispatch is not reported as dispatched" "false" "$(jq -r '.dispatched' <<<"$result")"
_assert_eq "a refused dispatch is flagged as an error" "true" "$(jq -r '.error' <<<"$result")"
MOCK_DISPATCH_FAILS=0

# --- Test 23: the kick wires it end-to-end and the park note names it ---
# The point of the feature: the owner reads the park note and knows what to
# install, without dispatching anything by hand.
SPRINT_AUTOPILOT="true"
RELEASE_ISSUE_NUMBER="2759"
SPRINT_FIELD="Sprint"
STATUS_FOR_RELEASE="For Release"
sprint_autopilot_paused() { return 1; }
release_version_from_issue() { echo "v3.0.0"; }
release_backlog_by_sprint() { echo '{"Sprint 8":0}'; }
iteration_active_title() { echo "Sprint 8"; }
issue_comment() { printf '%s' "$2" > "$COMMENT_FILE"; }
autopilot_scan_parked() { echo '{"resumable":[],"waiting":[],"exhausted":[],"active":[],"selected":null}'; }
_autopilot_post_resume() { echo "$1" > "$RESUME_FILE"; }
sprint_population_snapshot() { echo '{"open":{},"closed":{"Acceptance Testing":[3510,3513]}}'; }
: > "$DISPATCH_FILE"
_reset_dispatch
sprint_autopilot_kick 3510 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_contains "the agentic-complete park dispatches a candidate" "$(_dispatch_args)" "channel=rc"
_assert_contains "the park note names the candidate" "$body" "3.0.0rc2"
_assert_contains "the park note gives the install command" "$body" "pip install nyxgpt==3.0.0rc2"
_assert_eq "publishing a candidate does not turn a park into a kick" "no" "$(_dispatched)"

# A sprint still working publishes nothing and says nothing about candidates.
sprint_population_snapshot() { echo '{"open":{"In Progress":[3513]},"closed":{}}'; }
: > "$DISPATCH_FILE"
_reset_dispatch
sprint_autopilot_kick 3510 merged 2>/dev/null
body="$(cat "$COMMENT_FILE")"
_assert_eq "an in-flight park dispatches no candidate" "" "$(_dispatch_args)"
_assert_not_contains "an in-flight park note mentions no candidate" "$body" "Release candidate"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
