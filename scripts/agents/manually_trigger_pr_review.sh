#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  manually_trigger_pr_review.sh <pr_number>

Description:
  Manually triggers a code review for a PR. This is useful for:
  - Re-triggering a review after the PR was updated
  - Starting a review if automatic trigger failed
  - Forcing a fresh review after fixing issues

Behavior:
  - Triggers the Claude Code Review workflow via workflow_dispatch
  - Review-agent owns this process
  - Workflow uses REVIEW_AGENT_TOKEN for all operations

Notes:
  - Requires ~/.nyxGPT/config.ini (or $NYXGPT_CONFIG_FILE) and gh auth.
  - Must have permissions to trigger workflows

EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi

PR="${1:-}"
[[ -n "$PR" ]] || { usage; exit 2; }

# Strict config
load_config

require_gh_auth
require_cmd gh

REPO="${REPO_OWNER}/${REPO_NAME}"

echo "[review] Triggering review workflow for PR #$PR" >&2

# Trigger the workflow
gh workflow run claude-code-review.yml \
  --repo "$REPO" \
  -f pr_number="$PR"

echo "[review] ✓ Review workflow triggered for PR #$PR" >&2
echo "[review] Check workflow status: gh run list --repo $REPO --workflow=claude-code-review.yml" >&2
