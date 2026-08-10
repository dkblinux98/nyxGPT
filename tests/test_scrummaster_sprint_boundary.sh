#!/usr/bin/env bash
set -uo pipefail

# tests/test_scrummaster_sprint_boundary.sh
#
# Regression test for the sprint boundary in
# scripts/agents/scrummaster_next_issue.sh (#3706): with --sprint-scoped,
# selection must stop when the ACTIVE sprint has no eligible Backlog work
# instead of falling through to release-wide selection. Before #3706 the
# fall-through dispatched issues that a sprint reorg had just moved into a
# future sprint (observed 2026-08-09/10), crossing the sprint boundary with
# no planning event.
#
# Runs the real script end-to-end against a fake `gh` on PATH (no network,
# no credentials): the fake answers the project-id, fields, item-page, and
# issue-title calls the script makes.
#
# Usage: bash tests/test_scrummaster_sprint_boundary.sh

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
    echo "[FAIL] $desc: expected to find '$needle' in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- Fake gh ----------------------------------------------------------
# Answers the four call shapes scrummaster_next_issue.sh makes. The item
# page it returns comes from $ITEMS_JSON, set per scenario below.
mkdir -p "$TMP_DIR/bin"
cat >"$TMP_DIR/bin/gh" <<'FAKE_GH'
#!/usr/bin/env bash
if [[ "$1" == "auth" ]]; then
  exit 0
fi
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  query=""
  for a in "$@"; do
    [[ "$a" == query=* ]] && query="${a#query=}"
  done
  if [[ "$query" == *"fields(first:100)"* ]]; then
    cat "$FAKE_GH_FIELDS_FILE"
  elif [[ "$query" == *"items(first:100"* ]]; then
    cat "$FAKE_GH_ITEMS_FILE"
  else
    echo '{"data":{"user":{"projectV2":{"id":"proj-1"}}}}'
  fi
  exit 0
fi
# gh api repos/<owner>/<repo>/issues/<n> --jq '.title'
if [[ "$1" == "api" && "$2" == repos/* ]]; then
  echo "Release v3.0.0"
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 1
FAKE_GH
chmod +x "$TMP_DIR/bin/gh"
export PATH="$TMP_DIR/bin:$PATH"

# Sprint 8 started in the past (active); Sprint 9 has not started yet, so
# iteration_active_title() resolves to "Sprint 8" on any run date.
export FAKE_GH_FIELDS_FILE="$TMP_DIR/fields.json"
cat >"$FAKE_GH_FIELDS_FILE" <<'EOF'
{"data":{"node":{"fields":{"nodes":[
  {"__typename":"ProjectV2SingleSelectField","id":"f-status","name":"Status",
   "options":[{"id":"opt-backlog","name":"Backlog"},{"id":"opt-wip","name":"In Progress"}]},
  {"__typename":"ProjectV2IterationField","id":"f-sprint","name":"Sprint",
   "configuration":{"iterations":[
     {"id":"it-8","title":"Sprint 8","startDate":"2020-01-01","duration":14},
     {"id":"it-9","title":"Sprint 9","startDate":"2099-01-01","duration":14}]}}
]}}}}
EOF

export FAKE_GH_ITEMS_FILE="$TMP_DIR/items.json"

_write_items() {
  # $@ = item JSON fragments
  local items
  items="$(IFS=,; echo "$*")"
  cat >"$FAKE_GH_ITEMS_FILE" <<EOF
{"data":{"node":{"items":{"pageInfo":{"hasNextPage":false,"endCursor":""},"nodes":[${items}]}}}}
EOF
}

_item() {
  # $1=number $2=status $3=sprint title (empty for none)
  local number="$1" status="$2" sprint="$3" sprint_fv=""
  if [[ -n "$sprint" ]]; then
    sprint_fv=',{"__typename":"ProjectV2ItemFieldIterationValue","field":{"name":"Sprint"},"title":"'"$sprint"'"}'
  fi
  printf '%s' "{\"content\":{\"__typename\":\"Issue\",\"number\":${number},\"state\":\"OPEN\",\"milestone\":{\"title\":\"Phase 6 — Enterprise Deployment Hardening (v3.0.0)\"}},\"fieldValues\":{\"nodes\":[{\"__typename\":\"ProjectV2ItemFieldSingleSelectValue\",\"field\":{\"name\":\"Status\"},\"name\":\"${status}\"}${sprint_fv}]}}"
}

# ---- Config -----------------------------------------------------------
export NYXGPT_CONFIG_FILE="$TMP_DIR/config.ini"
cat >"$NYXGPT_CONFIG_FILE" <<'EOF'
REPO_OWNER=test-owner
REPO_NAME=test-repo
PROJECT_OWNER=test-owner
PROJECT_NUMBER=1
DEV_AGENT=dev-agent
REVIEW_AGENT=review-agent
SCRUM_AGENT=scrum-agent
HUMAN_OWNER=owner
STATUS_FIELD=Status
STATUS_BACKLOG=Backlog
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_FOR_RELEASE=For Release
RELEASE_BRANCH=v3.0.0
RELEASE_ISSUE_NUMBER=3521
SPRINT_FIELD=Sprint
EOF

_run_select() {
  # Runs the selector, printing "<exit code>|<stdout>" and leaving stderr
  # (the [scrum] log) in $TMP_DIR/stderr.
  local rc out
  out="$("$ROOT_DIR/scripts/agents/scrummaster_next_issue.sh" --select-only "$@" 2>"$TMP_DIR/stderr")"
  rc=$?
  printf '%s|%s' "$rc" "$out"
}

# --- Scenario A: active sprint drained, release still has future-sprint ---
# --- work -> stop at the boundary, dispatch nothing, log what was skipped ---
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 101 Backlog "")" "$(_item 102 "In Progress" "Sprint 8")"
result="$(_run_select --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "sprint-scoped selection dispatches nothing when the active sprint is drained" "1|" "$result"
_assert_contains "logs the active sprint it scoped to" "$stderr" "active sprint 'Sprint 8'"
_assert_contains "logs the per-page skip of out-of-sprint candidates" "$stderr" "skipped 2 Backlog issue(s) outside active sprint 'Sprint 8'"
_assert_contains "logs the sprint-boundary stop instead of falling through" "$stderr" "stopping at the sprint boundary"
_assert_contains "reports the future sprint holding the waiting work" "$stderr" "- Sprint 9: 1"
_assert_contains "reports the no-sprint bucket" "$stderr" "- (no sprint set): 1"

# --- Scenario B: the active sprint still has work -> it is selected, and ---
# --- the lower-numbered future-sprint issue is NOT preferred ---
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 199 Backlog "Sprint 8")"
result="$(_run_select --sprint-scoped)"
_assert_eq "selects the active sprint's issue over a lower-numbered future-sprint one" "0|199" "$result"

# --- Scenario C: an unscoped run (the human manual-kick override) still ---
# --- pulls forward across sprints, inside the release wall ---
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 101 Backlog "")"
result="$(_run_select)"
_assert_eq "unscoped selection still pulls future-sprint work forward" "0|100" "$result"

# --- Scenario D: NO iteration's window contains today + --sprint-scoped ---
# --- -> conservative stop. This used to fall back to unscoped selection, ---
# --- which dispatched future-sprint work automatically whenever a kick ---
# --- landed after the active window closed (the scrummaster-dispatch ---
# --- concurrency queue makes that delay real) or an old run was re-run. ---
cat >"$TMP_DIR/fields-no-active.json" <<'EOF'
{"data":{"node":{"fields":{"nodes":[
  {"__typename":"ProjectV2SingleSelectField","id":"f-status","name":"Status",
   "options":[{"id":"opt-backlog","name":"Backlog"},{"id":"opt-wip","name":"In Progress"}]},
  {"__typename":"ProjectV2IterationField","id":"f-sprint","name":"Sprint",
   "configuration":{"iterations":[
     {"id":"it-9","title":"Sprint 9","startDate":"2099-01-01","duration":14},
     {"id":"it-10","title":"Sprint 10","startDate":"2099-02-01","duration":14}]}}
]}}}}
EOF
FAKE_GH_FIELDS_FILE="$TMP_DIR/fields-no-active.json"
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 101 Backlog "")"
result="$(_run_select --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "sprint-scoped selection stops when no iteration is active" "1|" "$result"
_assert_contains "logs the conservative stop rather than an unscoped fallback" "$stderr" "stopping (conservative stop, #3706)"

# --- Scenario E: the owner override is unaffected by a missing active ---
# --- iteration -- an unscoped kick never passes --sprint-scoped ---
result="$(_run_select)"
_assert_eq "unscoped selection still works with no active iteration" "0|100" "$result"
FAKE_GH_FIELDS_FILE="$TMP_DIR/fields.json"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
