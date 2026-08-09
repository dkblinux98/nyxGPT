#!/usr/bin/env bash
# Intelligent failure analysis for developer workflow
# Analyzes workflow failures and attempts auto-fixes for common issues
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_analyze_failure.sh <issue_number> <run_id>

Analyzes a failed workflow run and determines the best course of action.

Arguments:
  issue_number - The issue number being worked on
  run_id - The GitHub Actions run ID that failed

Exit codes:
  0 - Fixed automatically, safe to continue
  1 - Fatal error, needs human intervention
  2 - Transient error, retry recommended

Outputs to: /tmp/analysis_result.txt
  STATUS=FIXED|TRANSIENT|FATAL
  ERROR_TYPE=<type>
  ACTION=<what was done>
  WAIT_SECONDS=<optional, for rate limits>
EOF
}

ISSUE="${1:-}"
RUN_ID="${2:-}"

[[ -n "$ISSUE" && -n "$RUN_ID" ]] || { usage >&2; exit 2; }

load_config
require_gh_auth
require_cmd jq

# ============================================================
# Extract error from workflow logs
# ============================================================

echo "[analyze] Fetching error from run $RUN_ID..." >&2

# Get the error message from logs
ERROR_LOG=$(gh run view "$RUN_ID" --log 2>&1 | \
  grep -E "\[error\]|Error:|fatal:|FAILED" | \
  head -20 || echo "")

if [[ -z "$ERROR_LOG" ]]; then
  echo "[analyze] No error messages found in logs" >&2
  echo "STATUS=UNKNOWN" > /tmp/analysis_result.txt
  echo "ERROR_TYPE=unknown" >> /tmp/analysis_result.txt
  exit 1
fi

echo "[analyze] Error messages:" >&2
echo "$ERROR_LOG" >&2

# Classify the error
ERROR_CLASS=$(classify_error "$ERROR_LOG")
echo "[analyze] Error classification: $ERROR_CLASS" >&2

# Extract error type (e.g., "fatal:issue_closed" -> "issue_closed")
ERROR_TYPE="${ERROR_CLASS#*:}"

# ============================================================
# Handle each error type intelligently
# ============================================================

case "$ERROR_TYPE" in
  issue_closed)
    echo "[analyze] Handling issue_closed error..." >&2

    # Gather context
    ISSUE_STATE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" \
      --jq '{state: (.state | ascii_upcase), closedAt: .closed_at, assignedToMe: ([.assignees[].login] | contains(["'"$DEV_AGENT"'"]))}')

    STATE=$(echo "$ISSUE_STATE" | jq -r '.state')
    CLOSED_AT=$(echo "$ISSUE_STATE" | jq -r '.closedAt // ""')
    ASSIGNED_TO_ME=$(echo "$ISSUE_STATE" | jq -r '.assignedToMe')

    if [[ "$STATE" != "CLOSED" ]]; then
      echo "[analyze] Issue is now OPEN - problem may have been fixed" >&2
      echo "STATUS=FIXED" > /tmp/analysis_result.txt
      echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
      echo "ACTION=Issue was reopened (possibly by human)" >> /tmp/analysis_result.txt
      exit 0
    fi

    if [[ "$ASSIGNED_TO_ME" != "true" ]]; then
      echo "[fatal] Issue closed and not assigned to dev agent" >&2
      echo "STATUS=FATAL" > /tmp/analysis_result.txt
      echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
      echo "DIAGNOSIS=Issue closed and not assigned to developer agent" >> /tmp/analysis_result.txt
      exit 1
    fi

    # Check if closed recently (within last 6 hours)
    if [[ -n "$CLOSED_AT" ]]; then
      # Convert timestamp to seconds (macOS and Linux compatible)
      if [[ "$OSTYPE" == "darwin"* ]]; then
        CLOSED_TIMESTAMP=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${CLOSED_AT}" "+%s" 2>/dev/null || echo "0")
      else
        CLOSED_TIMESTAMP=$(date -d "${CLOSED_AT}" "+%s" 2>/dev/null || echo "0")
      fi
      NOW=$(date +%s)
      HOURS_AGO=$(( (NOW - CLOSED_TIMESTAMP) / 3600 ))

      echo "[analyze] Issue closed $HOURS_AGO hour(s) ago" >&2

      if [[ $HOURS_AGO -lt 6 ]]; then
        # Check for merged PR (REST Search API -- its own rate pool, separate
        # from both core REST and GraphQL)
        MERGED_PR=$(gh api search/issues \
          --method GET \
          -f q="${ISSUE} in:body type:pr state:merged repo:${REPO_OWNER}/${REPO_NAME}" \
          --jq '.items[0].number // ""' || echo "")

        if [[ -n "$MERGED_PR" ]]; then
          echo "[fatal] Issue has merged PR #$MERGED_PR - work complete" >&2
          echo "STATUS=FATAL" > /tmp/analysis_result.txt
          echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
          echo "DIAGNOSIS=Issue completed in merged PR #$MERGED_PR" >> /tmp/analysis_result.txt
          exit 1
        fi

        # Accidental closure detected - REOPEN
        echo "[fix] Reopening accidentally closed issue..." >&2

        gh issue reopen "$ISSUE" --comment "$(cat <<COMMENT
🤖 **Developer Agent**: Automatically reopening issue.

**Reason:** Issue was assigned for implementation but closed before PR could be created.

**Analysis:**
- Closed: $HOURS_AGO hour(s) ago
- Assigned to: @$DEV_AGENT
- No merged PR found
- Work in progress

This appears to be an accidental closure. Reopening to allow PR submission.
COMMENT
)"

        echo "[fix] Issue reopened successfully" >&2
        echo "STATUS=FIXED" > /tmp/analysis_result.txt
        echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
        echo "ACTION=Reopened accidentally closed issue" >> /tmp/analysis_result.txt
        exit 0
      else
        echo "[fatal] Issue closed $HOURS_AGO hours ago - likely intentional" >&2
        echo "STATUS=FATAL" > /tmp/analysis_result.txt
        echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
        echo "DIAGNOSIS=Issue closed more than 6 hours ago - likely intentional" >> /tmp/analysis_result.txt
        exit 1
      fi
    fi

    echo "[fatal] Unable to determine issue closure reason" >&2
    echo "STATUS=FATAL" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=issue_closed" >> /tmp/analysis_result.txt
    echo "DIAGNOSIS=Issue closed but unable to determine if accidental" >> /tmp/analysis_result.txt
    exit 1
    ;;

  rate_limit)
    echo "[analyze] Handling rate_limit error..." >&2

    # Get rate limit info
    RATE_INFO=$(gh api rate_limit --jq '.rate' || echo "{}")
    REMAINING=$(echo "$RATE_INFO" | jq -r '.remaining // 0')
    RESET_TIME=$(echo "$RATE_INFO" | jq -r '.reset // 0')

    if [[ "$REMAINING" -gt 0 ]]; then
      echo "[analyze] Rate limit recovered (${REMAINING} calls remaining)" >&2
      echo "STATUS=FIXED" > /tmp/analysis_result.txt
      echo "ERROR_TYPE=rate_limit" >> /tmp/analysis_result.txt
      echo "ACTION=Rate limit recovered" >> /tmp/analysis_result.txt
      exit 0
    fi

    # Calculate wait time
    NOW=$(date +%s)
    WAIT_SECONDS=$(( RESET_TIME - NOW ))

    if [[ $WAIT_SECONDS -lt 0 ]]; then
      WAIT_SECONDS=0
    fi

    if [[ $WAIT_SECONDS -gt 3600 ]]; then
      # More than 1 hour - probably a problem
      echo "[fatal] Rate limit won't reset for $(( WAIT_SECONDS / 60 )) minutes" >&2
      echo "STATUS=FATAL" > /tmp/analysis_result.txt
      echo "ERROR_TYPE=rate_limit" >> /tmp/analysis_result.txt
      echo "DIAGNOSIS=Rate limit reset time too far in future ($(( WAIT_SECONDS / 60 )) minutes)" >> /tmp/analysis_result.txt
      exit 1
    fi

    echo "[transient] Rate limit - wait $WAIT_SECONDS seconds" >&2
    echo "STATUS=TRANSIENT" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=rate_limit" >> /tmp/analysis_result.txt
    echo "WAIT_SECONDS=$WAIT_SECONDS" >> /tmp/analysis_result.txt
    exit 2
    ;;

  stale_ref)
    echo "[analyze] Handling stale_ref error..." >&2
    echo "[fix] This will be handled by agent in workflow (git fetch/prune)" >&2
    echo "STATUS=TRANSIENT" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=stale_ref" >> /tmp/analysis_result.txt
    echo "ACTION=Agent will fix with git fetch --prune" >> /tmp/analysis_result.txt
    exit 2
    ;;

  test_failure)
    echo "[analyze] Handling test_failure error..." >&2
    echo "[transient] Let developer agent's fix loop handle test failures" >&2
    echo "STATUS=TRANSIENT" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=test_failure" >> /tmp/analysis_result.txt
    exit 2
    ;;

  network_timeout)
    echo "[analyze] Handling network_timeout error..." >&2
    echo "[transient] Network issues - retry should resolve" >&2
    echo "STATUS=TRANSIENT" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=network_timeout" >> /tmp/analysis_result.txt
    echo "WAIT_SECONDS=30" >> /tmp/analysis_result.txt
    exit 2
    ;;

  auth_failure)
    echo "[fatal] Authentication failure - requires admin intervention" >&2
    echo "STATUS=FATAL" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=auth_failure" >> /tmp/analysis_result.txt
    echo "DIAGNOSIS=GitHub authentication failed - check repository secrets and permissions" >> /tmp/analysis_result.txt
    exit 1
    ;;

  already_merged)
    echo "[fatal] Work already merged" >&2
    echo "STATUS=FATAL" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=already_merged" >> /tmp/analysis_result.txt
    echo "DIAGNOSIS=PR already merged - work complete" >> /tmp/analysis_result.txt
    exit 1
    ;;

  *)
    echo "[unknown] Unknown error type: $ERROR_TYPE" >&2
    echo "STATUS=FATAL" > /tmp/analysis_result.txt
    echo "ERROR_TYPE=unknown" >> /tmp/analysis_result.txt
    echo "DIAGNOSIS=Error type not recognized - manual investigation needed" >> /tmp/analysis_result.txt
    exit 1
    ;;
esac
