#!/usr/bin/env bash
set -euo pipefail

# scripts/smoke_agents.sh
# Full end-to-end agent smoke test (local), with pause + auto-restore by default.
# Bash 3.2 compatible.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() { echo "[error] $*" >&2; exit 1; }
log() { echo "[smoke] $*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

# ---------- Defaults (as requested) ----------
ISSUE=""
DO_PR=1
DO_MERGE=1
DO_BRANCH_DELETE=1
DO_RESTORE=1
DO_PAUSE=1

KIND="feat"
PREFIX="smoke"
SMOKE_DIR=".agent-smoke"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [options]

Defaults:
  - Full flow: start issue -> create branch -> commit -> PR -> merge -> delete branch
  - Pause between steps
  - Auto-restore issue state (assignees, open/closed, project status)

Options (turn off behaviors):
  --issue N            Use specific issue number (skip scrummaster_next_issue)
  --kind feat|fix      Branch kind (default: feat)
  --prefix TEXT        Branch suffix prefix (default: smoke)

  --no-pr              Do NOT create PR (implies --no-merge)
  --no-merge           Do NOT merge PR
  --no-branch-delete   Do NOT delete remote branch
  --no-restore         Do NOT restore issue state
  --no-pause           Do NOT pause between steps

  -h, --help           Show help

Examples:
  scripts/smoke_agents.sh
  scripts/smoke_agents.sh --issue 2602 --no-restore
  scripts/smoke_agents.sh --no-merge --no-branch-delete
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="$2"; shift 2;;
    --kind) KIND="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --no-pr) DO_PR=0; DO_MERGE=0; shift 1;;
    --no-merge) DO_MERGE=0; shift 1;;
    --no-branch-delete) DO_BRANCH_DELETE=0; shift 1;;
    --no-restore) DO_RESTORE=0; shift 1;;
    --no-pause) DO_PAUSE=0; shift 1;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

case "$KIND" in feat|fix) ;; *) die "--kind must be feat or fix";; esac

need gh
need jq
need git

export MYGPT_CONFIG_FILE="${MYGPT_CONFIG_FILE:-$HOME/.myGPT/config.ini}"
[[ -f "$MYGPT_CONFIG_FILE" ]] || die "Config not found: $MYGPT_CONFIG_FILE"

# shellcheck source=/dev/null
source scripts/agents/lib/gh_project.sh
load_config
require_gh_auth

REPO_FULL="${REPO_OWNER}/${REPO_NAME}"
BASE_BRANCH="$(get_release_branch)"

pause() {
  [[ "$DO_PAUSE" == "1" ]] || return 0
  echo "" >&2
  read -r -p "[pause] Press Enter to continue..." _ </dev/tty || true
}

ensure_clean_tree() {
  if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree not clean. Commit/stash changes before running smoke test."
  fi
}

# Capture current issue state for restore
ORIG_STATE=""
ORIG_ASSIGNEES_FILE=""
ORIG_STATUS=""

capture_issue_state() {
  local issue="$1"
  ORIG_ASSIGNEES_FILE="$(mktemp)"
  # state + assignees
  gh api "repos/$REPO_FULL/issues/$issue" \
    --jq '{state:.state, assignees:[.assignees[].login]}' > "$SMOKE_DIR/orig_issue_${issue}.json"

  ORIG_STATE="$(jq -r '.state' "$SMOKE_DIR/orig_issue_${issue}.json")"
  jq -r '.assignees[]?' "$SMOKE_DIR/orig_issue_${issue}.json" > "$ORIG_ASSIGNEES_FILE"

  # project status (best-effort; issue should be in project after ensure_issue_in_project)
  local item_id project_id
  item_id="$(ensure_issue_in_project "$issue")"
  project_id="$(get_project_id)"

  # Query a page of items until we find the issue and extract status
  local q='query($project:ID!, $after:String){
    node(id:$project){
      ... on ProjectV2{
        items(first:100, after:$after){
          pageInfo{ hasNextPage endCursor }
          nodes{
            id
            content{ __typename ... on Issue { number } }
            fieldValues(first:50){
              nodes{
                __typename
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field{ ... on ProjectV2SingleSelectField { name } }
                  name
                }
              }
            }
          }
        }
      }
    }
  }'

  local after="" found=""
  while :; do
    local resp
    resp="$(graphql "$q" -F project="$project_id" -F after="$after")"
    ORIG_STATUS="$(echo "$resp" | jq -r --argjson n "$issue" --arg f "$STATUS_FIELD" '
      .data.node.items.nodes[]
      | select(.content.__typename=="Issue" and .content.number==$n)
      | (.fieldValues.nodes[]
          | select(.__typename=="ProjectV2ItemFieldSingleSelectValue" and .field.name==$f)
          | .name) // empty
    ' | head -n 1)"

    found="$(echo "$resp" | jq -r --argjson n "$issue" '
      (.data.node.items.nodes[] | select(.content.__typename=="Issue" and .content.number==$n) | "yes") // empty
    ' | head -n 1)"

    [[ "$found" == "yes" ]] && break

    local has_next cursor
    has_next="$(echo "$resp" | jq -r '.data.node.items.pageInfo.hasNextPage')"
    cursor="$(echo "$resp" | jq -r '.data.node.items.pageInfo.endCursor // empty')"
    [[ "$has_next" == "true" && -n "$cursor" ]] || break
    after="$cursor"
  done

  log "Captured issue state: state=$ORIG_STATE orig_status=${ORIG_STATUS:-"(unknown)"} assignees_count=$(wc -l < "$ORIG_ASSIGNEES_FILE" | tr -d ' ')"
}

restore_issue_state() {
  local issue="$1"
  log "Restoring issue state..."

  # Restore open/closed
  if [[ "$ORIG_STATE" == "OPEN" ]]; then
    gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=open >/dev/null
  else
    gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=closed >/dev/null
  fi

  # Restore assignees (set exact list)
  local assignees_json
  assignees_json="$(jq -Rn '
    [inputs | select(length>0)]
  ' < "$ORIG_ASSIGNEES_FILE")"

  gh api -X PATCH "repos/$REPO_FULL/issues/$issue" \
    --input <(jq -n --argjson a "$assignees_json" '{assignees:$a}') >/dev/null

  # Restore project status if we captured it
  if [[ -n "${ORIG_STATUS:-}" ]]; then
    set_issue_status "$issue" "$ORIG_STATUS"
  fi

  log "Restore complete."
}

# Track PR and branch for cleanup/restore context
PR_NUMBER=""
CUR_BRANCH=""
START_BRANCH=""

cleanup_branch() {
  local branch="$1"
  [[ "$DO_BRANCH_DELETE" == "1" ]] || return 0
  log "Deleting remote branch: $branch"
  git push origin --delete "$branch" >/dev/null 2>&1 || true
}

# ---------- Start ----------
mkdir -p "$SMOKE_DIR"
ensure_clean_tree

START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "repo=$REPO_FULL base_branch=$BASE_BRANCH start_branch=$START_BRANCH config=$MYGPT_CONFIG_FILE"

# Choose issue
if [[ -z "$ISSUE" ]]; then
  log "Selecting next backlog issue..."
  ISSUE="$(./scripts/agents/scrummaster_next_issue.sh)"
fi
[[ -n "$ISSUE" ]] || die "Could not determine issue number"
log "ISSUE=$ISSUE"
pause

# Capture original state for restore
if [[ "$DO_RESTORE" == "1" ]]; then
  capture_issue_state "$ISSUE"
  pause
fi

# Scrummaster start issue (In Progress + assign developer)
log "Scrummaster: start issue (In Progress + assign developer)"
./scripts/agents/scrummaster_start_issue.sh "$ISSUE"
log "OK"
pause

# Developer create branch
log "Developer: create branch"
./scripts/agents/developer_create_branch.sh "$ISSUE" "$KIND" "$PREFIX"
CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "now on branch: $CUR_BRANCH"
pause

# Make a harmless commit so PR has content (local smoke file)
SMOKE_FILE="$SMOKE_DIR/issue-${ISSUE}.txt"
date +"smoke test %Y-%m-%d %H:%M:%S %z" > "$SMOKE_FILE"
git add "$SMOKE_FILE"
git commit -m "chore(smoke): issue #$ISSUE"
git push -u origin HEAD
log "Pushed smoke commit."
pause

# Developer submit PR (optional)
if [[ "$DO_PR" == "1" ]]; then
  log "Developer: submit PR for review"
  PR_NUMBER="$(./scripts/agents/developer_submit_for_review.sh "$ISSUE" | tail -n 1 | tr -d '[:space:]' || true)"
  [[ -n "$PR_NUMBER" ]] || die "developer_submit_for_review did not output a PR number"
  log "PR created: #$PR_NUMBER"
  pause
else
  log "--no-pr set; skipping PR/merge steps."
fi

# Review accept+merge (default ON)
if [[ "$DO_PR" == "1" && "$DO_MERGE" == "1" ]]; then
  log "Review: accept + merge"
  ./scripts/agents/review_accept_and_merge.sh "$ISSUE"
  log "Merged."
  pause
elif [[ "$DO_PR" == "1" ]]; then
  log "--no-merge set; leaving PR open."
fi

# Delete branch (default ON)
if [[ -n "$CUR_BRANCH" ]]; then
  cleanup_branch "$CUR_BRANCH"
  pause
fi

# Return to original branch
log "Returning to $START_BRANCH"
git checkout "$START_BRANCH" >/dev/null 2>&1 || true
log "Back on $START_BRANCH"
pause

# Restore issue/project state (default ON)
if [[ "$DO_RESTORE" == "1" ]]; then
  restore_issue_state "$ISSUE"
  pause
else
  log "--no-restore set; leaving issue as-is."
fi

log "SMOKE COMPLETE: issue=$ISSUE pr=${PR_NUMBER:-none} branch=${CUR_BRANCH:-none}"
exit 0