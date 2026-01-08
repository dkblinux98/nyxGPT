#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth

ISSUE="${1:?issue number required}"

# Ensure issue is in project; set status; assign to dev agent
set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
issue_unassign_all "$ISSUE"
issue_assign "$ISSUE" "$DEV_AGENT"

issue_comment "$ISSUE" "@${DEV_AGENT} selected as next work item by @${SCRUM_AGENT}. Status -> ${STATUS_IN_PROGRESS}."
echo "Started issue #$ISSUE -> In Progress, assignee: $DEV_AGENT"
