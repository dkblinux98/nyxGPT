#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth

PR="${1:?pr number or url required}"
ISSUE="${2:?issue number required}"

base_branch="$(get_release_branch)"

# Ensure issue status -> For Release and assign to human owner BEFORE merge (so it's visible even if the issue auto-closes)
set_issue_status "$ISSUE" "$STATUS_FOR_RELEASE"
issue_unassign_all "$ISSUE"
issue_assign "$ISSUE" "$HUMAN_OWNER"

# Merge PR into release branch
# Use --delete-branch to enforce branch hygiene
gh pr merge "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --merge --delete-branch

issue_comment "$ISSUE" "✅ Merged PR ${PR} into ${base_branch}. Status -> ${STATUS_FOR_RELEASE}. Assigned to @${HUMAN_OWNER}."

echo "Merged PR ${PR}; Issue #${ISSUE} -> For Release"
