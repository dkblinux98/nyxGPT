#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth

ISSUE="${1:?parent issue number required}"
TITLE="${2:?sub-issue title required}"
BODY_FILE="${3:?body file required}"

sub="$(create_sub_issue "$ISSUE" "$TITLE" "$BODY_FILE")"
gh issue edit "$sub" --repo "${REPO_OWNER}/${REPO_NAME}" --add-label "Acceptance Failure" >/dev/null || true

set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
issue_assign_only "$ISSUE" "$DEV_AGENT"
issue_comment "$ISSUE" "Acceptance Failure created: #${sub}. Assigned back to @${DEV_AGENT}. Status -> ${STATUS_IN_PROGRESS}."

set_issue_status "$sub" "$STATUS_IN_PROGRESS"
issue_assign_only "$sub" "$DEV_AGENT"

echo "$sub"
