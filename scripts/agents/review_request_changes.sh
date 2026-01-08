#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  review_request_changes.sh [--dry-run] <parent_issue_number> <sub_issue_title> <body_file>

Creates an "Acceptance Failure" sub-issue, then:
  - Parent issue Status -> In Progress
  - Parent assignee -> DEV_AGENT
  - Sub-issue Status -> In Progress
  - Sub-issue assignee -> DEV_AGENT
Outputs the new sub-issue number to stdout.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd gh; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

ISSUE="${1:-}"
TITLE="${2:-}"
BODY_FILE="${3:-}"
if [[ -z "$ISSUE" || -z "$TITLE" || -z "$BODY_FILE" ]]; then usage >&2; exit 2; fi
[[ -f "$BODY_FILE" ]] || _die "Body file not found: $BODY_FILE"

load_config
require_gh_auth
require_cmd gh
require_cmd jq

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would: create_sub_issue parent=#${ISSUE} title='${TITLE}' body_file='${BODY_FILE}'" >&2
  echo "[dry-run] would: label sub-issue 'Acceptance Failure'" >&2
  echo "[dry-run] would: set_issue_status parent #${ISSUE} -> '$STATUS_IN_PROGRESS' + assign @$DEV_AGENT" >&2
  echo "[dry-run] would: set_issue_status sub -> '$STATUS_IN_PROGRESS' + assign @$DEV_AGENT" >&2
  exit 0
fi

sub="$(create_sub_issue "$ISSUE" "$TITLE" "$BODY_FILE")"

gh issue edit "$sub" --repo "${REPO_OWNER}/${REPO_NAME}" --add-label "Acceptance Failure" >/dev/null || true

set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
issue_assign_only "$ISSUE" "$DEV_AGENT"
issue_comment "$ISSUE" "Acceptance Failure created: #${sub}. Assigned back to @${DEV_AGENT}. Status -> ${STATUS_IN_PROGRESS}."

set_issue_status "$sub" "$STATUS_IN_PROGRESS"
issue_assign_only "$sub" "$DEV_AGENT"

echo "$sub"