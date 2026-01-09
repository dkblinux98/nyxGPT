#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() { echo "[error] $*" >&2; exit 1; }
log() { echo "[smoke] $*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

ISSUE=""
DO_PR=1
DO_MERGE=1
DO_BRANCH_DELETE=1
DO_ROLLBACK=1
DO_PAUSE=1

KIND="feat"
PREFIX="smoke"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [options]

Defaults:
  - Full flow: start issue -> create branch -> (empty commit) -> PR -> merge -> delete branch -> rollback
  - Pause between steps
  - Rollback: set issue open + Status=Backlog + Assignee=HUMAN_OWNER

Options:
  --issue N            Use specific issue number (skip scrummaster_next_issue)
  --kind feat|fix      Branch kind (default: feat)
  --prefix TEXT        Branch suffix prefix (default: smoke)

  --no-pr              Do NOT create PR (implies --no-merge)
  --no-merge           Do NOT merge PR
  --no-branch-delete   Do NOT delete branch (remote/local)
  --no-rollback        Do NOT rollback issue state
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
    --no-rollback) DO_ROLLBACK=0; shift 1;;
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

checkout_safely() {
  local target="$1"
  local current
  current="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$current" == "$target" ]]; then
    log "Already on branch: $target"
    return 0
  fi
  log "Checking out: $target (from $current)"
  git checkout "$target" >/dev/null 2>&1 || die "Failed to checkout $target"
  log "Now on: $(git rev-parse --abbrev-ref HEAD)"
}

delete_feature_branch_everywhere() {
  local feature_branch="$1"
  local safe_branch="$2"   # branch to be on before deleting locally

  [[ "$DO_BRANCH_DELETE" == "1" ]] || return 0
  [[ -n "$feature_branch" ]] || return 0

  # Ensure we are NOT on the feature branch before local delete
  local current
  current="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$current" == "$feature_branch" ]]; then
    checkout_safely "$safe_branch"
  fi

  # Remote delete (works from any branch)
  if git ls-remote --exit-code --heads origin "$feature_branch" >/dev/null 2>&1; then
    log "Deleting remote branch: $feature_branch"
    git push origin --delete "$feature_branch" >/dev/null 2>&1 || true
  else
    log "Remote branch already deleted: $feature_branch"
  fi

  # Local delete
  if git show-ref --verify --quiet "refs/heads/$feature_branch"; then
    log "Deleting local branch: $feature_branch"
    git branch -D "$feature_branch" >/dev/null 2>&1 || true
  else
    log "Local branch already deleted: $feature_branch"
  fi
}

rollback_issue() {
  local issue="$1"
  [[ "$DO_ROLLBACK" == "1" ]] || return 0

  log "Rollback: set issue #$issue to open + Status=$STATUS_BACKLOG + assignee=$HUMAN_OWNER"

  gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=open >/dev/null || true
  issue_assign_only "$issue" "$HUMAN_OWNER" || true
  set_issue_status "$issue" "$STATUS_BACKLOG" || true

  log "Rollback done."
}

START_BRANCH=""
CUR_BRANCH=""
PR_NUMBER=""

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
elif [[ "$DO_PR" == "1" ]]; then
  log "--no-merge set; leaving PR open."
  pause
fi

# Always return to base branch BEFORE branch deletion/rollback so local delete works.
checkout_safely "$BASE_BRANCH"
pause

# Ensure feature branch is deleted both remote and local.
delete_feature_branch_everywhere "$CUR_BRANCH" "$BASE_BRANCH"
pause

rollback_issue "$ISSUE"
pause

log "SMOKE COMPLETE: issue=$ISSUE pr=${PR_NUMBER:-none} branch=${CUR_BRANCH:-none}"
exit 0