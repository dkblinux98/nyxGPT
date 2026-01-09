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

base_branch="$(get_release_branch)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] base_branch=$base_branch" >&2
  echo "[dry-run] would: gh pr merge $PR --merge --delete-branch" >&2
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_REVIEW'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$HUMAN_OWNER" >&2
  exit 0
fi

# Merge PR; GitHub will enforce approvals if branch protection requires it.
gh pr merge "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --merge --delete-branch

set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$HUMAN_OWNER"
issue_comment "$ISSUE" "PR merged into \`${base_branch}\` and branch deleted. Status -> ${STATUS_IN_REVIEW}. Assigned -> @${HUMAN_OWNER}."

echo "Merged PR ($PR). Issue #$ISSUE -> ${STATUS_IN_REVIEW"