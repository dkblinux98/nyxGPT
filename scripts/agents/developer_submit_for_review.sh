#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth
require_cmd mktemp

ISSUE="${1:?issue number required}"
PR_TITLE="${2:?pr title required}"
PR_BODY_FILE="${3:-}"

base_branch="$(get_release_branch)"

tmp_body=""
if [[ -n "$PR_BODY_FILE" ]]; then
  [[ -f "$PR_BODY_FILE" ]] || _die "PR body file not found: $PR_BODY_FILE"
  body_file="$PR_BODY_FILE"
else
  tmp_body="$(mktemp)"
  body_file="$tmp_body"
  cat >"$body_file" <<EOF
Closes #$ISSUE

## Summary
- 

## Tests
- 

## Notes
- 
EOF
fi

pr_url="$(gh pr create --repo "${REPO_OWNER}/${REPO_NAME}" --base "$base_branch" --title "$PR_TITLE" --body-file "$body_file")"
[[ -n "$pr_url" ]] || _die "Failed to create PR"

set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$REVIEW_AGENT"
issue_comment "$ISSUE" "PR opened: ${pr_url}\nAssigned to @${REVIEW_AGENT}. Status -> ${STATUS_IN_REVIEW}."

[[ -n "$tmp_body" ]] && rm -f "$tmp_body"
echo "$pr_url"
