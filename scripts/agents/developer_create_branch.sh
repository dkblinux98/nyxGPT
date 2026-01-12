#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_create_branch.sh [--dry-run] <issue_number> [feat|fix|chore] [slug]

Creates and checks out a branch off the current release branch.
Prints the branch name to stdout.

Examples:
  developer_create_branch.sh 2602 feat "project-setup"
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd git; require_cmd gh
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

ISSUE="${1:-}"
KIND="${2:-feat}"
SLUG="${3:-issue-${ISSUE}}"
if [[ -z "$ISSUE" ]]; then usage >&2; exit 2; fi

load_config
require_gh_auth
require_cmd git

# Determine base branch: parent feature branch for sub-issues, release branch for top-level issues
if is_sub_issue "$ISSUE"; then
  parent_issue="$(get_parent_issue "$ISSUE")"
  base_branch="$(get_pr_branch_for_issue "$parent_issue")"
  echo "Sub-issue detected: branching off parent feature branch $base_branch (parent issue: #$parent_issue)" >&2
else
  base_branch="$(get_release_branch)"
  echo "Top-level issue: branching off release branch $base_branch" >&2
fi

branch="${KIND}/${ISSUE}-${SLUG}"
branch="$(echo "$branch" | tr ' ' '-' | tr -cd '[:alnum:]/._-')"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] base_branch=$base_branch" >&2
  echo "[dry-run] branch=$branch" >&2
  echo "[dry-run] would: git fetch origin $base_branch" >&2
  echo "[dry-run] would: git checkout -b $branch origin/$base_branch" >&2
  echo "[dry-run] would: git push -u origin $branch" >&2
  echo "$branch"
  exit 0
fi

git fetch origin "$base_branch" >&2

# Check if branch already exists remotely
BRANCH_ACTION="created"
if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
  BRANCH_ACTION="reused"
  echo "Remote branch $branch already exists, reusing it..." >&2

  # Delete local branch if it exists
  if git show-ref --verify --quiet "refs/heads/${branch}"; then
    git branch -D "$branch" >&2 || true
  fi

  # Check out existing remote branch
  git checkout -b "$branch" "origin/$branch" >&2
else
  echo "Creating new branch $branch from $base_branch..." >&2

  # Delete local branch if it exists
  if git show-ref --verify --quiet "refs/heads/${branch}"; then
    git branch -D "$branch" >&2 || true
  fi

  # Create and push new branch
  git checkout -b "$branch" "origin/$base_branch" >&2
  git push -u origin "$branch" >&2
fi

# Optional breadcrumb on the issue (non-fatal)
if [[ "$BRANCH_ACTION" == "reused" ]]; then
  issue_comment "$ISSUE" "Developer branch reused: \`${branch}\` (existing PR will be updated)." >/dev/null 2>&1 || true
else
  issue_comment "$ISSUE" "Developer branch created: \`${branch}\` (base: \`${base_branch}\`)." >/dev/null 2>&1 || true
fi

echo "$branch"