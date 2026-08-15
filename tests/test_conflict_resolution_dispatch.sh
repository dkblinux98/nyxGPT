#!/usr/bin/env bash
set -uo pipefail

# tests/test_conflict_resolution_dispatch.sh
# End-to-end tests for scripts/agents/dispatch_conflict_resolution.sh (#3801).
#
# A stub `gh` on PATH serves the PR read, the comment thread, the project
# GraphQL query and every write, logging what the script tried to do. The
# real script runs -- including conflict_resolution.py -- with no network.
#
# What must hold, in the owner's words (2026-08-15): "merge conflicts
# shouldn't halt progress and shouldn't be escalated to me unless there's
# truly a decision to be made only I can make."
#   * a plain mainline-moved conflict reassigns the DEVELOPER AGENT and
#     never touches the owner
#   * the round comment instructs a MERGE and forbids a rebase
#   * the owner is assigned only on an agent-raised owner-only decision, or
#     when the automated rounds stop converging
#
# Usage: bash tests/test_conflict_resolution_dispatch.sh

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
RELEASE_BRANCH=v3.0.0
EOF

# --- Stub `gh` --------------------------------------------------------
# Fixtures the scenarios rewrite:
#   $TMP/pr.json        the PR as the script's --jq projection sees it
#   $TMP/comments.json  the PR comment thread (array of {body, created_at})
#   $TMP/writes.log     every mutating call the script attempted
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
TMP="$STUB_TMP"
args=("$@")
path=""
jq_filter=""
is_graphql=0
is_post=0
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    auth) exit 0 ;;
    graphql) is_graphql=1 ;;
    --jq) jq_filter="${args[$((i + 1))]}"; ((i++)) ;;
    -X) [[ "${args[$((i + 1))]:-}" != "GET" ]] && is_post=1; ((i++)) ;;
    -F) ((i++)) ;;
    repos/*) path="${args[$i]}" ;;
  esac
done

echo "gh $*" >> "$TMP/writes.log"

if [[ "${args[0]}" == "pr" || "${args[0]}" == "issue" ]]; then
  exit 0
fi

if [[ "$is_graphql" == "1" ]]; then
  # Enough shape for get_project_id / ensure_issue_in_project to walk.
  jq -n '{data: {user: {projectV2: {id: "proj-1"}},
                 organization: {projectV2: {id: "proj-1"}},
                 node: {items: {pageInfo: {hasNextPage: false, endCursor: null},
                                nodes: [{id: "item-1", content: {__typename: "Issue", number: 4242}}]}}}}'
  exit 0
fi

if [[ "$is_post" == "1" ]]; then
  echo '{}'
  exit 0
fi

_emit() {
  if [[ -n "$jq_filter" ]]; then jq -r "$jq_filter"; else cat; fi
}

case "$path" in
  *"/pulls/"*)
    # The script's projection filter is applied to this object; serve it
    # already projected and ignore the filter.
    cat "$TMP/pr.json"
    ;;
  *"/comments")
    jq -c '.[] | {body: .body, created_at: .created_at}' "$TMP/comments.json"
    ;;
  *"/issues/"*)
    jq -n '{assignees: [], state: "open", body: ""}' | _emit
    ;;
  *)
    echo '{}' | _emit
    ;;
esac
STUB
chmod +x "$TMP/bin/gh"

_reset() {
  : > "$TMP/writes.log"
  echo '[]' > "$TMP/comments.json"
  cat > "$TMP/pr.json" <<'EOF'
{"headRefName": "feat/4242-thing", "baseRefName": "v3.0.0", "body": "Closes #4242",
 "mergeable": "CONFLICTING", "state": "OPEN"}
EOF
}

_run() {
  STUB_TMP="$TMP" PATH="$TMP/bin:$PATH" NYXGPT_CONFIG_FILE="$TMP/config.ini" \
    MERGEABLE_POLL_ATTEMPTS=1 \
    bash "$ROOT_DIR/scripts/agents/dispatch_conflict_resolution.sh" "$@" 2>&1
}

# --- Scenario 1: the mainline moved -> developer agent, not the owner ---
_reset
out="$(_run 4242)"
writes="$(cat "$TMP/writes.log")"
_assert_contains "a plain conflict dispatches a developer-agent round" \
  "$out" "conflict-resolution: dispatch pr=4242 issue=4242"
_assert_contains "the developer agent is (re)assigned" "$writes" "assignees[]=dev-agent"
_assert_not_contains "the owner is NOT assigned" "$writes" "assignees[]=owner"
_assert_contains "the round comment instructs a merge of the base branch" \
  "$writes" "MERGE \`origin/v3.0.0\` into"
_assert_contains "the round comment forbids rebasing" "$writes" "NEVER REBASE"
_assert_contains "ledger split guidance rides in the round comment" \
  "$writes" "RENUMBER YOURS"

# --- Scenario 2: a clean PR is left alone -----------------------------
_reset
cat > "$TMP/pr.json" <<'EOF'
{"headRefName": "feat/4242-thing", "baseRefName": "v3.0.0", "body": "Closes #4242",
 "mergeable": "MERGEABLE", "state": "OPEN"}
EOF
out="$(_run 4242)"
_assert_contains "a mergeable PR is a noop" "$out" "conflict-resolution: noop"
_assert_not_contains "nothing is assigned for a clean PR" "$(cat "$TMP/writes.log")" "assignees[]="

# --- Scenario 3: rounds exhausted -> the owner, with the reason -------
_reset
python3 - "$TMP/comments.json" <<'PY'
import json, sys
rounds = [
    {"body": "round <!-- conflict-resolution-round -->", "created_at": f"2026-08-1{d}T09:00:00Z"}
    for d in (1, 2, 3)
]
json.dump(rounds, open(sys.argv[1], "w"))
PY
out="$(_run 4242)"
writes="$(cat "$TMP/writes.log")"
_assert_contains "non-converging rounds escalate" "$out" "conflict-resolution: escalate"
_assert_contains "escalation assigns the owner" "$writes" "assignees[]=owner"
_assert_contains "the escalation says why" "$writes" "did not converge"

# --- Scenario 4: agent-raised owner-only decision ---------------------
_reset
cat > "$TMP/comments.json" <<'EOF'
[{"body": "CONFLICT_REQUIRES_OWNER_DECISION\n\nEager vs lazy Cassandra start: both owner-accepted. Which wins?",
  "created_at": "2026-08-15T09:00:00Z"}]
EOF
out="$(_run 4242)"
writes="$(cat "$TMP/writes.log")"
_assert_contains "an agent-raised owner decision escalates" "$out" "conflict-resolution: escalate"
_assert_contains "the owner gets the specific question" "$writes" "Which wins?"
_assert_not_contains "no developer round is dispatched alongside it" "$writes" "assignees[]=dev-agent"

# --- Scenario 5: burst guard ------------------------------------------
# Nine merges in an afternoon must not produce nine rounds on one PR.
_reset
python3 - "$TMP/comments.json" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
recent = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump([{"body": "round <!-- conflict-resolution-round -->", "created_at": recent}],
          open(sys.argv[1], "w"))
PY
out="$(_run 4242)"
_assert_contains "a round still in flight suppresses a second dispatch" \
  "$out" "conflict-resolution: noop"
_assert_not_contains "and assigns nobody" "$(cat "$TMP/writes.log")" "assignees[]="

# --- Scenario 6: escalate-from-comment (the token workflow's entry) ---
_reset
out="$(COMMENT_BODY=$'CONFLICT_REQUIRES_OWNER_DECISION\n\nWhich behavior wins on startup?' \
  _run 4242 --escalate-from-comment)"
writes="$(cat "$TMP/writes.log")"
_assert_contains "the token workflow escalates directly" "$out" "conflict-resolution: escalate"
_assert_contains "carrying the question" "$writes" "Which behavior wins on startup?"

# --- Scenario 7: dry run decides but writes nothing -------------------
_reset
out="$(DRY_RUN=1 _run 4242)"
_assert_contains "dry run still reports the decision" "$out" "conflict-resolution: dispatch"
_assert_not_contains "dry run assigns nobody" "$(cat "$TMP/writes.log")" "assignees[]="

# --- Scenario 8: no linked issue -> say so, do not escalate -----------
_reset
cat > "$TMP/pr.json" <<'EOF'
{"headRefName": "feat/4242-thing", "baseRefName": "v3.0.0", "body": "No link here",
 "mergeable": "CONFLICTING", "state": "OPEN"}
EOF
out="$(_run 4242)"
_assert_contains "an unlinked conflicted PR reports noop" "$out" "reason=no linked issue"
_assert_not_contains "and still does not assign the owner" \
  "$(cat "$TMP/writes.log")" "assignees[]=owner"

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All conflict-resolution dispatch tests passed."
