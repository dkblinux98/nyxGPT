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

Environment:
  Reads SCRUMMASTER_AGENT_TOKEN from ~/.nyxGPT/config.ini automatically.
  Can be overridden by setting GH_TOKEN environment variable.
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
# Load config first to get SCRUMMASTER_AGENT_TOKEN
load_config

# Use SCRUMMASTER_AGENT_TOKEN from config if GH_TOKEN is not already set
if [[ -z "${GH_TOKEN:-}" ]]; then
  if [[ -n "${SCRUMMASTER_AGENT_TOKEN:-}" ]]; then
    export GH_TOKEN="$SCRUMMASTER_AGENT_TOKEN"
  else
    echo "[error] SCRUMMASTER_AGENT_TOKEN not found in config file: $CONFIG_FILE" >&2
    echo "[error] Please add SCRUMMASTER_AGENT_TOKEN to the [github] section" >&2
    exit 1
  fi
fi

require_gh_auth

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_PROGRESS'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$DEV_AGENT" >&2
  echo "[dry-run] would: comment on #$ISSUE" >&2
  exit 0
fi

set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
assign_and_trigger_developer "$ISSUE"
issue_comment "$ISSUE" "@${DEV_AGENT} selected as next work item by @${SCRUM_AGENT}. Status -> ${STATUS_IN_PROGRESS}."

echo "Started issue #$ISSUE -> ${STATUS_IN_PROGRESS}, assignee: $DEV_AGENT"
