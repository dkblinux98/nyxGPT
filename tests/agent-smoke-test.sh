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
  - Full flow: start issue -> create branch -> empty commit -> PR -> merge ->
               checkout release branch -> delete feature branch -> git pull -> rollback
  - Pause between steps
  - Rollback: set issue open + Status=Backlog + Assignee=HUMAN_OWNER

Options:
  --issue N            Use specific issue number
  --kind feat|fix      Branch kind (default: feat)
  --prefix TEXT        Branch suffix prefix (default: smoke)

  --no-pr              Do NOT create PR (implies --no-merge)
  --no-merge           Do NOT merge PR
  --no-branch-delete   Do NOT delete branch
  --no-rollback        Do NOT rollback issue
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
  echo ""
  read -r -p "[pause] Press Enter to continue..." _ </dev/tty || true
}

ensure_clean_tree() {
  [[ -z "$(git status --porcelain)" ]] || die "Working tree not clean."
}

checkout_safely() {
  local target="$1"
  local current
  current="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$current" != "$target" ]]; then
    log "Checking out $target (from $current)"
    git checkout "$target"
  fi
}

delete_feature_branch_everywhere() {
  local branch="$1"
  [[ "$DO_BRANCH_DELETE" == "1" ]] || return 0

  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    log "Deleting remote branch $branch"
    git push origin --delete "$branch" || true
  fi

  if git show-ref --verify --quiet "refs/heads/$branch"; then
    log "Deleting local branch $branch"
    git branch -D "$branch" || true
  fi
}

rollback_issue() {
  [[ "$DO_ROLLBACK" == "1" ]] || return 0
  local issue="$1"

  log "Rollback: issue #$issue → open, Backlog, $HUMAN_OWNER"
  gh api -X PATCH "repos/$REPO_FULL/issues/$issue" -f state=open >/dev/null || true
  issue_assign_only "$issue" "$HUMAN_OWNER" || true
  set_issue_status "$issue" "$STATUS_BACKLOG" || true
}

ensure_clean_tree
START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "repo=$REPO_FULL base=$BASE_BRANCH start=$START_BRANCH"
pause

if [[ -z "$ISSUE" ]]; then
  ISSUE="$(./scripts/agents/scrummaster_next_issue.sh)"
fi
log "ISSUE=$ISSUE"
pause

log "Scrummaster: start issue"
./scripts/agents/scrummaster_start_issue.sh "$ISSUE"
pause

log "Developer: create branch"
./scripts/agents/developer_create_branch.sh "$ISSUE" "$KIND" "$PREFIX"
CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
pause

log "Create empty commit"
git commit --allow-empty -m "chore(smoke): issue #$ISSUE"
git push -u origin HEAD
pause

if [[ "$DO_PR" == "1" ]]; then
  PR_NUMBER="$(./scripts/agents/developer_submit_for_review.sh "$ISSUE" | tail -1)"
  log "PR #$PR_NUMBER created"
  pause
fi

if [[ "$DO_PR" == "1" && "$DO_MERGE" == "1" ]]; then
  ./scripts/agents/review_accept_and_merge.sh "$PR_NUMBER" "$ISSUE"
  pause
fi

# ---- GIT HYGIENE SECTION (new, intentional, correct) ----
checkout_safely "$BASE_BRANCH"

rollback_issue "$ISSUE"
pause

delete_feature_branch_everywhere "$CUR_BRANCH"

log "Updating $BASE_BRANCH with fast-forward pull"
git pull --ff-only

pause
# --------------------------------------------------------

log "SMOKE COMPLETE: issue=$ISSUE pr=${PR_NUMBER:-none} branch=${CUR_BRANCH}"
exit 0