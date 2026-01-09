#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  review_accept_and_merge.sh [--dry-run] <pr_number_or_url> <issue_number>

Merges the PR into the current release branch (merge commit) and deletes the PR branch, then:
  - Issue Status -> In Review
  - Issue assignee -> HUMAN_OWNER
  - Comment on issue with merge info
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd gh; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

PR="${1:-}"
ISSUE="${2:-}"
if [[ -z "$PR" || -z "$ISSUE" ]]; then usage >&2; exit 2; fi

load_config
require_gh_auth
require_cmd gh
require_cmd jq
require_cmd git

base_branch="$(get_release_branch)"

# Determine the PR head branch so we can clean it up locally too.
pr_head_branch="$(gh pr view "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --json headRefName -q '.headRefName')"
[[ -n "$pr_head_branch" ]] || _die "Could not determine PR head branch"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] base_branch=$base_branch" >&2
  echo "[dry-run] pr_head_branch=$pr_head_branch" >&2
  echo "[dry-run] would: gh pr merge $PR --merge --delete-branch" >&2
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_REVIEW'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$HUMAN_OWNER" >&2
  echo "[dry-run] would: git checkout $base_branch && git pull --ff-only origin $base_branch" >&2
  echo "[dry-run] would: delete local branch $pr_head_branch (if exists)" >&2
  exit 0
fi

#
# Merge PR; GitHub will enforce approvals if branch protection requires it.
gh pr merge "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --merge --delete-branch

# ---- Local git hygiene ----
# We merge via GitHub, so update the local checkout of the release branch.
# Require a clean working tree to avoid clobbering local changes.
if [[ -n "$(git status --porcelain)" ]]; then
  _die "Working tree not clean; commit/stash your changes before running this script."
fi

# Make sure we are on the release branch and up to date with origin.
# Use ff-only to avoid surprise merges; if it fails, the user can resolve explicitly.
git fetch origin "$base_branch" >/dev/null 2>&1 || true

git checkout "$base_branch" >/dev/null 2>&1 || true

if ! git pull --ff-only origin "$base_branch"; then
  _die "Could not fast-forward local '$base_branch'. Run: git pull --rebase origin $base_branch (or resolve divergence) and re-run local cleanup."
fi

# Delete the local feature branch if it exists (remote branch should already be deleted by --delete-branch).
if git show-ref --verify --quiet "refs/heads/${pr_head_branch}"; then
  git branch -D "$pr_head_branch" >/dev/null
fi

# Ensure remote branch is deleted (no-op if already deleted).
if git ls-remote --exit-code --heads origin "$pr_head_branch" >/dev/null 2>&1; then
  git push origin --delete "$pr_head_branch" >/dev/null 2>&1 || true
fi

set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$HUMAN_OWNER"
issue_comment "$ISSUE" "PR merged into \`${base_branch}\` and branch deleted. Status -> ${STATUS_IN_REVIEW}. Assigned -> @${HUMAN_OWNER}."

echo "Merged PR ($PR). Issue #$ISSUE -> ${STATUS_IN_REVIEW}"