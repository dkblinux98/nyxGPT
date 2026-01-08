#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_start_issue.sh [--dry-run] <issue_number>

Sets:
  - Issue Status -> In Progress
  - Assignee -> DEV_AGENT
  - Comment notifying assignment

Options:
  --dry-run   Print actions without making changes
  -h, --help  Show this help
EOF
}

DRY_RUN=0

# --- args ---
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

ISSUE="${1:-}"
if [[ -z "$ISSUE" ]]; then
  usage >&2
  exit 2
fi

if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "[error] issue_number must be numeric, got: '$ISSUE'" >&2
  exit 2
fi

# --- config/auth ---
load_config
require_gh_auth

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_PROGRESS'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$DEV_AGENT" >&2
  echo "[dry-run] would: comment on #$ISSUE" >&2
  exit 0
fi

set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
issue_assign_only "$ISSUE" "$DEV_AGENT"
issue_comment "$ISSUE" "@${DEV_AGENT} selected as next work item by @${SCRUM_AGENT}. Status -> ${STATUS_IN_PROGRESS}."

echo "Started issue #$ISSUE -> ${STATUS_IN_PROGRESS}, assignee: $DEV_AGENT"