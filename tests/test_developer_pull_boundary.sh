#!/usr/bin/env bash
set -uo pipefail

# tests/test_developer_pull_boundary.sh
#
# End-to-end test of the pull (scripts/agents/developer_pull_next.sh, #3883)
# against a fake `gh` on PATH -- no network, no credentials.
#
# It carries forward every scenario the sprint-boundary regression test held
# for the retired scrummaster_next_issue.sh (#3706): with --sprint-scoped,
# selection stops when the ACTIVE sprint has no eligible work instead of
# falling through to release-wide work, and stops conservatively when no
# iteration is active at all. Before #3706 that fall-through dispatched
# issues a sprint reorg had just moved into a future sprint (observed
# 2026-08-09/10). The pull replaced the *decision*, not those boundaries, so
# the coverage moves with it rather than lapsing.
#
# It also covers what only the pull can get wrong: plan order must beat issue
# number, and a candidate whose expected-files collide with in-flight work
# must yield to the next candidate rather than be pulled in parallel.
#
# Usage: bash tests/test_developer_pull_boundary.sh

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
# Answers every call shape the pull makes: project id, fields, item pages,
# the release tracking issue's title, open PRs, and per-issue blocked_by
# dependencies. $FAKE_GH_PRS_FILE / $FAKE_GH_BLOCKED_FILE let a scenario
# make work in flight or a candidate ineligible.
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
if [[ "$1" == "api" ]]; then
  case "$2" in
    *"/dependencies/blocked_by"*) cat "${FAKE_GH_BLOCKED_FILE:-/dev/null}"; exit 0 ;;
    *"/pulls/"*"/files"*) cat "${FAKE_GH_PR_FILES_FILE:-/dev/null}"; exit 0 ;;
    *"/pulls?"*) cat "${FAKE_GH_PRS_FILE:-/dev/null}"; exit 0 ;;
    repos/*/issues/*) echo "Release v3.0.0"; exit 0 ;;
  esac
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
export FAKE_GH_PRS_FILE="$TMP_DIR/prs.json"
export FAKE_GH_PR_FILES_FILE="$TMP_DIR/pr-files.json"
export FAKE_GH_BLOCKED_FILE="$TMP_DIR/blocked.json"
echo '[]' >"$FAKE_GH_PRS_FILE"
echo '[]' >"$FAKE_GH_PR_FILES_FILE"
# blocked_by is read through `gh api --jq '.[]...'`, so the fake answers
# with what jq would print: one issue number per line, empty for none.
: >"$FAKE_GH_BLOCKED_FILE"

_write_items() {
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
  printf '%s' "{\"content\":{\"__typename\":\"Issue\",\"number\":${number},\"state\":\"OPEN\",\"milestone\":{\"title\":\"Phase 6 — Enterprise Deployment Hardening (v3.0.0)\"},\"labels\":{\"nodes\":[]},\"assignees\":{\"nodes\":[]}},\"fieldValues\":{\"nodes\":[{\"__typename\":\"ProjectV2ItemFieldSingleSelectValue\",\"field\":{\"name\":\"Status\"},\"name\":\"${status}\"}${sprint_fv}]}}"
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

# The plan the pull reads. Written into a scratch checkout root so the real
# repo's plans (if any) never influence the test.
PLAN_ROOT="$TMP_DIR/repo"
mkdir -p "$PLAN_ROOT/product_management/sprint_planning/sprint_8" "$PLAN_ROOT/scripts/agents"
ln -s "$ROOT_DIR/scripts/agents/lib" "$PLAN_ROOT/scripts/agents/lib"

_write_plan() {
  # $1 = JSON body of the plan
  cat >"$PLAN_ROOT/product_management/sprint_planning/sprint_8/PLAN.md" <<EOF
# Sprint plan -- Sprint 8

<!-- sprint-plan:json -->
\`\`\`json
$1
\`\`\`
EOF
}

_no_plan() { rm -f "$PLAN_ROOT/product_management/sprint_planning/sprint_8/PLAN.md"; }

_run_pull() {
  # Runs the pull from the scratch root (so the plan lookup is scoped to the
  # test's own plan doc), printing "<exit code>|<stdout>"; stderr lands in
  # $TMP_DIR/stderr.
  local rc out
  out="$(cd "$PLAN_ROOT" && "$ROOT_DIR/scripts/agents/developer_pull_next.sh" "$@" 2>"$TMP_DIR/stderr")"
  rc=$?
  printf '%s|%s' "$rc" "$out"
}

# --- Scenario A: active sprint drained, release still has future-sprint ---
# --- work -> pull nothing rather than crossing the boundary ---
_no_plan
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 101 Backlog "")" "$(_item 102 "In Progress" "Sprint 8")"
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "sprint-scoped pull takes nothing when the active sprint is drained" "0|" "$result"
_assert_contains "logs the active sprint it scoped to" "$stderr" "active sprint 'Sprint 8'"
_assert_contains "says why nothing was pulled" "$stderr" "No eligible candidate"

# --- Scenario B: the active sprint has work -> it is pulled, and a ---
# --- lower-numbered future-sprint issue is not preferred ---
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 199 Backlog "Sprint 8")"
result="$(_run_pull --sprint-scoped)"
_assert_eq "pulls the active sprint's issue over a lower-numbered future-sprint one" "0|199" "$result"

# --- Scenario C: an unscoped run (the owner's manual override) still pulls --
# --- forward across sprints, inside the release wall ---
_write_items "$(_item 100 Backlog "Sprint 9")" "$(_item 101 Backlog "")"
result="$(_run_pull)"
_assert_eq "unscoped pull still takes future-sprint work forward" "0|100" "$result"

# --- Scenario D: no iteration's window contains today + --sprint-scoped ---
# --- -> conservative stop, never an unscoped fallback ---
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
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "sprint-scoped pull stops when no iteration is active" "1|" "$result"
_assert_contains "logs the conservative stop rather than an unscoped fallback" "$stderr" "conservative stop"

# --- Scenario E: the owner override is unaffected by a missing active ---
# --- iteration -- an unscoped pull never passes --sprint-scoped ---
result="$(_run_pull)"
_assert_eq "unscoped pull still works with no active iteration" "0|100" "$result"
FAKE_GH_FIELDS_FILE="$TMP_DIR/fields.json"

# --- Scenario F: the plan's order decides, not the issue number ---
_write_items "$(_item 100 Backlog "Sprint 8")" "$(_item 199 Backlog "Sprint 8")"
_write_plan '{"sprint":"Sprint 8","order":[{"issue":199,"expected_files":["src/b.py"]},{"issue":100,"expected_files":["src/a.py"]}]}'
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "pulls the plan's first issue, not the lowest number" "0|199" "$result"
_assert_contains "records why that issue and not the other" "$stderr" "position 1 in the sprint plan"

# --- Scenario G: expected-files overlapping in-flight work defers the ---
# --- candidate instead of scheduling the conflict ---
_write_items "$(_item 100 Backlog "Sprint 8")" "$(_item 199 Backlog "Sprint 8")" \
  "$(_item 300 "In Progress" "Sprint 8")"
_write_plan '{"sprint":"Sprint 8","order":[{"issue":199,"expected_files":["src/b.py"]},{"issue":100,"expected_files":["src/a.py"]},{"issue":300,"expected_files":["src/b.py"]}]}'
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "defers the overlapping candidate and pulls the next one" "0|100" "$result"
_assert_contains "names the file it collided on" "$stderr" "skipped #199: file_overlap"

# --- Scenario H: a PR's diff belongs to the issue it DECLARES, not to ---
# --- every issue it mentions. A prose "related to #NNNN" used to donate ---
# --- the whole diff to that issue and defer candidates that never ---
# --- overlapped it. ---
_write_items "$(_item 100 Backlog "Sprint 8")" "$(_item 300 "In Progress" "Sprint 8")"
_write_plan '{"sprint":"Sprint 8","order":[{"issue":100,"expected_files":["src/a.py"]},{"issue":300,"expected_files":["src/untouched.py"]}]}'
cat >"$FAKE_GH_PRS_FILE" <<'EOF'
[{"number":900,"head":{"ref":"feat/9001-unrelated"},"title":"unrelated work",
  "body":"Related to #300, but this PR does not implement it."}]
EOF
cat >"$FAKE_GH_PR_FILES_FILE" <<'EOF'
[{"filename":"src/a.py"}]
EOF
result="$(_run_pull --sprint-scoped)"
_assert_eq "a mere mention does not donate a PR's diff to the in-flight issue" "0|100" "$result"

# --- and the declared link DOES attribute it: the same PR, now branched ---
# --- for #300, makes its diff #300's footprint and defers the overlap ---
cat >"$FAKE_GH_PRS_FILE" <<'EOF'
[{"number":900,"head":{"ref":"feat/300-canary"},"title":"work","body":"Closes #300"}]
EOF
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "a declared link makes the PR's real diff the in-flight footprint" "0|" "$result"
_assert_contains "and the candidate is deferred on the file it collides with" "$stderr" "skipped #100: file_overlap"

# --- Scenario I: a plan is read only for the sprint being pulled ---------
# --- Grooming is an owner-initiated planning event: until the owner has ---
# --- groomed THIS sprint there is no plan to obey, and no neighbouring ---
# --- sprint's plan may stand in for it. ---
echo '[]' >"$FAKE_GH_PRS_FILE"
echo '[]' >"$FAKE_GH_PR_FILES_FILE"
_write_items "$(_item 100 Backlog "Sprint 8")" "$(_item 199 Backlog "Sprint 8")"

# A plan exists, but for a different sprint: it must not be read.
_write_plan '{"sprint":"Sprint 7","order":[{"issue":199,"expected_files":["src/b.py"]}]}'
result="$(_run_pull --sprint-scoped)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "another sprint's plan does not order this sprint's pull" "0|100" "$result"
_assert_contains "and it says the sprint is ungroomed" "$stderr" "no groomed plan for 'Sprint 8'"

# The active sprint's own plan is read, as before.
_write_plan '{"sprint":"Sprint 8","order":[{"issue":199,"expected_files":["src/b.py"]}]}'
result="$(_run_pull --sprint-scoped)"
_assert_eq "the groomed plan for this sprint does order it" "0|199" "$result"

# An unscoped pull -- the owner reaching across the boundary -- has no
# sprint whose plan could apply, so it reads none even though one exists.
result="$(_run_pull)"
stderr="$(cat "$TMP_DIR/stderr")"
_assert_eq "an unscoped pull ignores the plan entirely" "0|100" "$result"
_assert_contains "and says why" "$stderr" "no sprint plan applies"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
