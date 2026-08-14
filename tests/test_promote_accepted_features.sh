#!/usr/bin/env bash
set -uo pipefail

# tests/test_promote_accepted_features.sh
# End-to-end tests for scripts/agents/promote_accepted_features.sh after the
# switch to native relationships (#3731). A stub `gh` on PATH serves the
# issue list, the dependency endpoints and the project-Status GraphQL query
# from an in-test fixture, so the real script -- including
# issue_relationships.py and the transitive walk -- runs with no network.
#
# Runs in DRY_RUN mode: the sweep's decisions show up in its log, and no
# writes are attempted.
#
# Usage: bash tests/test_promote_accepted_features.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAILURES=0

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' unexpectedly found in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat > "$TMP/config.ini" <<'EOF'
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
STATUS_ACCEPTANCE_TESTING=Acceptance Testing
STATUS_ACCEPTANCE_FAILED=Acceptance Failed
RELEASE_BRANCH=v3.0.0
EOF

# Issue OPEN/CLOSED state, consulted only for items parked in the
# `Acceptance Failed` lane (#3780). Default CLOSED: every scenario that
# predates the dual-lane rule works in `Acceptance Testing`, where state is
# never read.
echo '{}' > "$TMP/states.json"

# --- Stub `gh` --------------------------------------------------------
# Fixtures come from files the tests rewrite between scenarios:
#   $TMP/issues.json   the labelled-issue list (both label queries share it,
#                      filtered by label the way the real API does)
#   $TMP/deps.json     {"blocking": {...}, "blocked_by": {...}}
#   $TMP/status.json   issue number -> project Status
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail

TMP="$STUB_TMP"
path=""
jq_filter=""
label=""
is_graphql=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    auth) exit 0 ;;
    graphql) is_graphql=1 ;;
    --jq) jq_filter="${args[$((i + 1))]}"; ((i++)) ;;
    -F) ((i++)) ;;
    -f) ((i++)) ;;
    repos/*) path="${args[$i]}" ;;
  esac
done

_emit() {
  if [[ -n "$jq_filter" ]]; then
    jq -r "$jq_filter"
  else
    cat
  fi
}

if [[ "$is_graphql" == "1" ]]; then
  # issue_status(): the query carries the number in -F num=<n>.
  num=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    [[ "${args[$i]}" == num=* ]] && num="${args[$i]#num=}"
  done
  status="$(jq -r --arg n "$num" '.[$n] // "Backlog"' "$TMP/status.json")"
  jq -n --arg s "$status" '{data: {repository: {issue: {projectItems: {nodes: [
    {fieldValues: {nodes: [{field: {name: "Status"}, name: $s}]}}]}}}}}'
  exit 0
fi

case "$path" in
  *"/dependencies/blocking")
    num="${path#*issues/}"; num="${num%%/*}"
    jq -c --arg n "$num" '[(.blocking[$n] // [])[] | {number: ., state: "closed"}]' "$TMP/deps.json" | _emit
    ;;
  *"/dependencies/blocked_by")
    num="${path#*issues/}"; num="${num%%/*}"
    jq -c --arg n "$num" '[(.blocked_by[$n] // [])[] | {number: ., state: "closed"}]' "$TMP/deps.json" | _emit
    ;;
  *"/issues?labels="*)
    label="${path#*labels=}"; label="${label%%&*}"
    label="${label//%20/ }"
    jq -c --arg l "$label" '[.[] | select(.labels | index($l))]' "$TMP/issues.json" | _emit
    ;;
  *"/issues/"*)
    num="${path#*issues/}"; num="${num%%/*}"
    # State comes from its own fixture so a scenario can park a CLOSED
    # feature and hold an OPEN one in the same lane (#3780).
    state="$(jq -r --arg n "$num" '.[$n] // "closed"' "$TMP/states.json")"
    jq -c --arg n "$num" --arg s "$state" \
      '((.[] | select(.number == ($n | tonumber))) // {number: ($n | tonumber), id: ($n | tonumber), body: ""}) + {state: $s}' \
      "$TMP/issues.json" | _emit
    ;;
  *)
    echo "{}" | _emit
    ;;
esac
STUB
chmod +x "$TMP/bin/gh"

_run_sweep() {
  STUB_TMP="$TMP" PATH="$TMP/bin:$PATH" NYXGPT_CONFIG_FILE="$TMP/config.ini" DRY_RUN=1 \
    bash "$ROOT_DIR/scripts/agents/promote_accepted_features.sh" 2>&1
}

# --- Scenario 1: native relationship, blocker accepted -> promote -----
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3733, "id": 3733, "labels": ["Acceptance Failure"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3733": [3730]}, "blocked_by": {"3730": [3733]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3730": "Acceptance Testing", "3733": "For Release"}
EOF
out="$(_run_sweep)"
_assert_contains "a feature whose native blocker is accepted is promoted" \
  "$out" "would promote #3730"

# --- Scenario 2: blocker not yet accepted -> wait ---------------------
cat > "$TMP/status.json" <<'EOF'
{"3730": "Acceptance Testing", "3733": "In Progress"}
EOF
out="$(_run_sweep)"
_assert_contains "an unaccepted blocker holds the feature back" \
  "$out" "#3730 waits in 'Acceptance Testing': blocker #3733 is 'In Progress'"
_assert_not_contains "a held feature is not promoted" "$out" "would promote #3730"

# --- Scenario 3: transitivity — a failure filed against a failure -----
# 3740 blocks 3733 blocks 3730. 3733 is accepted but 3740 is not, so 3730
# must still wait: the gate is the transitive closure, not the direct list.
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3733, "id": 3733, "labels": ["Acceptance Failure"], "body": ""},
 {"number": 3740, "id": 3740, "labels": ["Acceptance Failure"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3733": [3730], "3740": [3733]},
 "blocked_by": {"3730": [3733], "3733": [3740]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3730": "Acceptance Testing", "3733": "For Release", "3740": "In Progress"}
EOF
out="$(_run_sweep)"
_assert_contains "a transitive blocker holds the original issue back" \
  "$out" "#3730 waits in 'Acceptance Testing': blocker #3740 is 'In Progress'"
_assert_not_contains "the transitively blocked feature is not promoted" "$out" "would promote #3730"

cat > "$TMP/status.json" <<'EOF'
{"3730": "Acceptance Testing", "3733": "For Release", "3740": "For Release"}
EOF
out="$(_run_sweep)"
_assert_contains "the whole transitive chain accepted promotes the original" \
  "$out" "would promote #3730"

# --- Scenario 4: an Improvement gates too (owner decision 2026-08-12) -
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3741, "id": 3741, "labels": ["Improvement"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3741": [3730]}, "blocked_by": {"3730": [3741]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3730": "Acceptance Testing", "3741": "Backlog"}
EOF
out="$(_run_sweep)"
_assert_contains "an unaccepted improvement holds its issue back" \
  "$out" "#3730 waits in 'Acceptance Testing': blocker #3741 is 'Backlog'"

# --- Scenario 5: historical prose issue is healed into a native link --
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3000, "id": 3000, "labels": ["Acceptance Failure"], "body": "Related feature: #2999"}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {}, "blocked_by": {}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"2999": "Acceptance Testing", "3000": "For Release"}
EOF
out="$(_run_sweep)"
_assert_contains "a prose-only historical link is still resolved" \
  "$out" "would ensure #3000 is marked as blocking #2999"
_assert_contains "and its feature is still promotable" "$out" "would promote #2999"

# --- Scenario 6: a feature parked in `Acceptance Failed` (#3780) -------
# Owner decision 2026-08-14: the owner also parks features they have tested
# and failed in the holding lane. Such a feature is a promotion candidate
# exactly like one parked in `Acceptance Testing`.
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3733, "id": 3733, "labels": ["Acceptance Failure"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3733": [3508]}, "blocked_by": {"3508": [3733]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3508": "Acceptance Failed", "3733": "For Release"}
EOF
cat > "$TMP/states.json" <<'EOF'
{"3508": "closed"}
EOF
out="$(_run_sweep)"
_assert_contains "a feature parked in Acceptance Failed is a promotion candidate" \
  "$out" "would promote #3508 from 'Acceptance Failed' to 'For Release'"

# --- Scenario 6b: parked, but a blocker is still open -> left alone ----
# The lane placement is owner signal. With any blocker unaccepted the sweep
# moves nothing at all -- it does not "tidy" the feature into another lane.
cat > "$TMP/status.json" <<'EOF'
{"3508": "Acceptance Failed", "3733": "In Progress"}
EOF
out="$(_run_sweep)"
_assert_contains "a parked feature with an open blocker waits in its lane" \
  "$out" "#3508 waits in 'Acceptance Failed': blocker #3733 is 'In Progress'"
_assert_not_contains "a parked feature with an open blocker is never moved" \
  "$out" "would promote #3508"

# --- Scenario 6c: transitivity applies in the parked lane too ---------
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3733, "id": 3733, "labels": ["Acceptance Failure"], "body": ""},
 {"number": 3740, "id": 3740, "labels": ["Acceptance Failure"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3733": [3508], "3740": [3733]},
 "blocked_by": {"3508": [3733], "3733": [3740]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3508": "Acceptance Failed", "3733": "For Release", "3740": "Backlog"}
EOF
out="$(_run_sweep)"
_assert_contains "a transitive blocker holds the parked feature back too" \
  "$out" "#3508 waits in 'Acceptance Failed': blocker #3740 is 'Backlog'"
_assert_not_contains "the transitively blocked parked feature is not promoted" \
  "$out" "would promote #3508"

cat > "$TMP/status.json" <<'EOF'
{"3508": "Acceptance Failed", "3733": "For Release", "3740": "For Release"}
EOF
out="$(_run_sweep)"
_assert_contains "the whole closure accepted promotes the parked feature" \
  "$out" "would promote #3508 from 'Acceptance Failed' to 'For Release'"

# --- Scenario 6d: OPEN in the lane is held rework, never promoted ------
# #3730's holding-pen role is unchanged: an OPEN item in `Acceptance Failed`
# is this round's rework waiting for the drain gate, not a parked feature.
# Promoting it would declare unfixed work released.
cat > "$TMP/issues.json" <<'EOF'
[{"number": 3740, "id": 3740, "labels": ["Acceptance Failure"], "body": ""}]
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3740": [3733]}, "blocked_by": {"3733": [3740]}}
EOF
cat > "$TMP/status.json" <<'EOF'
{"3733": "Acceptance Failed", "3740": "For Release"}
EOF
cat > "$TMP/states.json" <<'EOF'
{"3733": "open"}
EOF
out="$(_run_sweep)"
_assert_contains "an OPEN item in the holding lane is held rework, not a candidate" \
  "$out" "#3733 is OPEN in 'Acceptance Failed' -- held rework"
_assert_not_contains "held rework is never promoted" "$out" "would promote #3733"

# --- Scenario 6e: any other lane is still not a candidate -------------
cat > "$TMP/status.json" <<'EOF'
{"3733": "In Progress", "3740": "For Release"}
EOF
echo '{}' > "$TMP/states.json"
out="$(_run_sweep)"
_assert_contains "an issue in an unrelated lane is not a promotion candidate" \
  "$out" "#3733 status 'In Progress' is neither 'Acceptance Testing' nor 'Acceptance Failed'"
_assert_not_contains "and it is not promoted" "$out" "would promote #3733"

# --- Scenario 7: nothing to do --------------------------------------
echo '[]' > "$TMP/issues.json"
out="$(_run_sweep)"
_assert_contains "an empty corpus is a clean no-op" "$out" "Nothing to do."

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
