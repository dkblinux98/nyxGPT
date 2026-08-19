#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_start_issue.sh [--dry-run] <issue_number>

For a claimable Backlog issue (unassigned, or assigned only to the
scrummaster agent), sets:
  - Issue Status -> In Progress
  - Assignee -> DEV_AGENT
  - Comment notifying assignment

For an issue that is NOT claimable (see classify_backlog_claim_state in
lib/gh_project.sh), nothing is mutated except an optional loud comment for
an unrecognized assignee. Exit codes distinguish the outcome so callers
(e.g. developer_pull_next_issue.yml) can fall through to the next candidate
instead of ending the dispatch on a single bad-state issue (#3665):

  0   started
  10  skipped quietly (duplicate in-flight start, a deliberate human hold,
      or the issue is no longer open) -- not a block, try the next candidate
  11  skipped loudly (unrecognized assignee) -- a comment was posted on the
      issue; try the next candidate

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
  echo "[dry-run] would: classify_backlog_claim_state #$ISSUE and act per the decision matrix" >&2
  echo "[dry-run] would (if claimable): set_issue_status #$ISSUE -> '$STATUS_IN_PROGRESS'" >&2
  echo "[dry-run] would (if claimable): assign issue #$ISSUE -> @$DEV_AGENT" >&2
  echo "[dry-run] would (if claimable): comment on #$ISSUE" >&2
  exit 0
fi

# Start-guard decision matrix (#3665, owner ruling 2026-08-08): the #3647
# idempotency guard used to treat ANY existing assignee as "already claimed"
# and skip -- but assign_backlog.yml deliberately assigns SCRUM_AGENT to
# every fresh Backlog issue, so that guard silently turned the normal
# steady state into a permanent head-of-line block (#3593 stalled the loop
# ~5 days). scrummaster_attempt_start (lib/gh_project.sh) distinguishes
# *who* holds the claim so only genuinely unclaimable issues are skipped,
# and every skip is reported via a distinct exit code instead of
# masquerading as success.
scrummaster_attempt_start "$ISSUE"
