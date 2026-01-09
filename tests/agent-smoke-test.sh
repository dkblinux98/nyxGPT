#!/usr/bin/env bash
set -euo pipefail

# scripts/agent-smoke-test.sh
# Full end-to-end agent smoke test (local), with pause + auto-restore by default.
# Bash 3.2 compatible.
#
# This script tests the *control plane*:
#   - Scrummaster selects & starts issue
#   - Developer creates branch
#   - Creates an EMPTY commit (no file changes required)
#   - Developer submits PR
#   - Review merges + updates status/assignee and (attempts) branch delete
#   - Ensures branch is actually deleted (deletes if it still exists)
#   - Optionally restores issue/project state
#
# NOTE: This does NOT test "Claude implements the issue" (that is GitHub Actions).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() { echo "[error] $*" >&2; exit 1; }
log() { echo "[smoke] $*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

ISSUE=""
DO_PR=1
DO_MERGE=1
DO_BRANCH_DELETE=1
DO_RESTORE=1
DO_PAUSE=1

KIND="feat"
PREFIX="smoke"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [options]

Defaults:
  - Full flow: start issue -> create branch -> (empty commit) -> PR -> merge -> delete branch
  - Pause between steps
  - Auto-restore issue state (assignees, open/closed, project status)

Options (turn off behaviors):
  --issue N            Use specific issue number (skip scrummaster_next_issue)
  --kind feat|fix      Branch kind (default: feat)
  --prefix TEXT        Branch suffix prefix (default: smoke)

  --no-pr              Do NOT create PR (implies --no-merge)
  --no-merge           Do NOT merge PR
  --no-branch-delete   Do NOT delete remote branch
  --no-restore         Do NOT restore issue/project state
  --no-pause           Do NOT pause between steps

  -h, --help           Show help
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

SMOKE_TMP_DIR=""
ORIG_STATE=""
ORIG_ASSIGNEES_FILE=""
ORIG_STATUS=""

cleanup_tmp() {
  [[ -n "${SMOKE_TMP_DIR:-}" && -d "$SMOKE_TMP_DIR" ]] && rm -rf "$SMOKE_TMP_DIR" || true
  [[ -n "${ORIG_ASSIGNEES_FILE:-}" && -f "$ORIG_ASSIGNEES_FILE" ]] && rm -f "$ORIG_ASSIGNEES_FILE" || true
}
trap cleanup_tmp EXIT

capture_issue_state() {
  local issue="$1"

  SMOKE_TMP_DIR="$(mktemp -d)"
  ORIG_ASSIGNEES_FILE="$(mktemp)"

  gh api "repos/$REPO_FULL/issues/$issue" \
    --jq '{state:.state, assignees:[.assignees[].login]}' > "$SMOKE_TMP_DIR/orig_issue.json"

  ORIG_STATE="$(jq -r '.state' "$SMOKE_TMP_DIR/orig_issue.json")"
  jq -r '.assignees[]?' "$SMOKE_TMP_DIR/orig_issue.json" > "$ORIG_ASSIGNEES_FILE"

  ensure_issue_in_project "$issue" >/dev/null

  local project_id
  project_id="$(get_project_id)"

  local q='query($project:ID!, $after:String){
    node(id:$project){
      ... on ProjectV2{
        items(first:100, after:$after){
          pageInfo{ hasNextPage endCursor }
          nodes{
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
  ORIG_STATUS=""
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

  orig="$(echo "$ORIG_STATE" | tr '[:upper:]' '[:lower:]')"
  if [[ "$orig" == "open" ]]; then
    gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=open >/dev/null
  elif [[ "$orig" == "closed" ]]; then
    gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=closed >/dev/null
  else
    log "Unknown original state '$ORIG_STATE' (skipping state restore)"
  fi

  local assignees_json
  assignees_json="$(jq -Rn '[inputs | select(length>0)]' < "$ORIG_ASSIGNEES_FILE")"
  gh api -X PATCH "repos/$REPO_FULL/issues/$issue" \
    --input <(jq -n --argjson a "$assignees_json" '{assignees:$a}') >/dev/null

  if [[ -n "${ORIG_STATUS:-}" ]]; then
    set_issue_status "$issue" "$ORIG_STATUS"
  fi

  log "Restore complete."
}

START_BRANCH=""
CUR_BRANCH=""
PR_NUMBER=""

ensure_remote_branch_deleted() {
  local branch="$1"
  [[ "$DO_BRANCH_DELETE" == "1" ]] || return 0
  [[ -n "$branch" ]] || return 0

  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    log "Remote branch still exists; deleting: $branch"
    git push origin --delete "$branch" >/dev/null 2>&1 || true
  else
    log "Remote branch already deleted: $branch"
  fi
}

# ---------- Start ----------
ensure_clean_tree

START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "repo=$REPO_FULL base_branch=$BASE_BRANCH start_branch=$START_BRANCH config=$MYGPT_CONFIG_FILE"
pause

if [[ -z "$ISSUE" ]]; then
  log "Selecting next backlog issue..."
  ISSUE="$(./scripts/agents/scrummaster_next_issue.sh)"
fi
[[ -n "$ISSUE" ]] || die "Could not determine issue number"
log "ISSUE=$ISSUE"
pause

if [[ "$DO_RESTORE" == "1" ]]; then
  capture_issue_state "$ISSUE"
  pause
fi

log "Scrummaster: start issue (In Progress + assign developer)"
./scripts/agents/scrummaster_start_issue.sh "$ISSUE"
log "OK"
pause

log "Developer: create branch"
./scripts/agents/developer_create_branch.sh "$ISSUE" "$KIND" "$PREFIX"
CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "now on branch: $CUR_BRANCH"
pause

log "Creating EMPTY smoke commit (no file changes)"
git commit --allow-empty -m "chore(smoke): issue #$ISSUE"
git push -u origin HEAD
log "Pushed empty commit."
pause

if [[ "$DO_PR" == "1" ]]; then
  log "Developer: submit PR for review"
  PR_NUMBER="$(./scripts/agents/developer_submit_for_review.sh "$ISSUE" | tail -n 1 | tr -d '[:space:]' || true)"
  [[ -n "$PR_NUMBER" ]] || die "developer_submit_for_review did not output a PR number"
  log "PR created: #$PR_NUMBER"
  pause
else
  log "--no-pr set; skipping PR/merge steps."
fi

if [[ "$DO_PR" == "1" && "$DO_MERGE" == "1" ]]; then
  log "Review: accept + merge"
  ./scripts/agents/review_accept_and_merge.sh "$PR_NUMBER" "$ISSUE"
  log "Merged."
  pause

  # review script *should* delete branch, but verify and delete if it didn't
  ensure_remote_branch_deleted "$CUR_BRANCH"
  pause
elif [[ "$DO_PR" == "1" ]]; then
  log "--no-merge set; leaving PR open."

  # If you want branch cleanup even without merge, keep this:
  ensure_remote_branch_deleted "$CUR_BRANCH"
  pause
fi

log "Returning to $START_BRANCH"
git checkout "$START_BRANCH" >/dev/null 2>&1 || true
log "Back on $START_BRANCH"
pause

if [[ "$DO_RESTORE" == "1" ]]; then
  restore_issue_state "$ISSUE"
  pause
else
  log "--no-restore set; leaving issue as-is."
fi

log "SMOKE COMPLETE: issue=$ISSUE pr=${PR_NUMBER:-none} branch=${CUR_BRANCH:-none}"
exit 0