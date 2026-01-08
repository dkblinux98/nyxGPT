#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_submit_for_review.sh [--dry-run] <issue_number> <pr_title> [pr_body_file]

Creates a PR into the current release branch, then:
  - Issue Status -> In Review
  - Assign -> REVIEW_AGENT
  - Comment with PR link

Outputs PR number to stdout.

Notes:
  - Runs from the current git branch.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd gh; require_cmd git; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

ISSUE="${1:-}"
PR_TITLE="${2:-}"
PR_BODY_FILE="${3:-}"
if [[ -z "$ISSUE" || -z "$PR_TITLE" ]]; then usage >&2; exit 2; fi

load_config
require_gh_auth
require_cmd gh
require_cmd git
require_cmd jq

base_branch="$(get_release_branch)"

# Determine current branch
current_branch="$(git rev-parse --abbrev-ref HEAD)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] current_branch=$current_branch" >&2
  echo "[dry-run] base_branch=$base_branch" >&2
  echo "[dry-run] would: gh pr create --base $base_branch --head $current_branch" >&2
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_REVIEW'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$REVIEW_AGENT" >&2
  exit 0
fi

# Create PR
args=(gh pr create --repo "${REPO_OWNER}/${REPO_NAME}" --base "$base_branch" --head "$current_branch" --title "$PR_TITLE")
if [[ -n "$PR_BODY_FILE" ]]; then
  [[ -f "$PR_BODY_FILE" ]] || _die "PR body file not found: $PR_BODY_FILE"
  args+=(--body-file "$PR_BODY_FILE")
else
  args+=(--body "Implements #${ISSUE}.")
fi

pr_url="$("${args[@]}")"
pr_number="$(gh pr view "$pr_url" --repo "${REPO_OWNER}/${REPO_NAME}" --json number -q .number)"

set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$REVIEW_AGENT"
issue_comment "$ISSUE" "PR submitted for review: ${pr_url} (base: \`${base_branch}\`). Status -> ${STATUS_IN_REVIEW}. Assigned -> @${REVIEW_AGENT}."

echo "$pr_number"