#!/usr/bin/env bash
# Watch workflow runs related to a specific issue
# Displays clickable links to GitHub Actions web UI

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: watch_agents.sh <issue_number>

Watch GitHub Actions workflows related to a specific issue.
Displays status and clickable links to view logs in web UI.

Monitors:
- Developer Auto-Implement workflow (issue assignment trigger)
- Review Agent workflow (PR opened/updated)
- Claude Code workflow (comment triggers)

Press Ctrl+C to exit.

Example:
  ./scripts/watch_agents.sh 2604
EOF
  exit 1
}

[[ $# -eq 0 ]] && usage
ISSUE="$1"

# Validate issue number
if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "Error: Issue number must be numeric" >&2
  exit 1
fi

REPO="dkblinux98/myGPT"
POLL_INTERVAL=30  # Poll every 30 seconds (120 API calls/hour max)
SEEN_RUNS=()

echo "🔍 Watching workflows for issue #$ISSUE"
echo "Repository: $REPO"
echo "Poll interval: ${POLL_INTERVAL}s"
echo "Press Ctrl+C to exit"
echo ""

# Format duration in human readable form
format_duration() {
  local seconds=$1
  if [[ $seconds -lt 60 ]]; then
    echo "${seconds}s"
  else
    echo "$((seconds / 60))m $((seconds % 60))s"
  fi
}

# Get status emoji
status_emoji() {
  case "$1" in
    in_progress|IN_PROGRESS) echo "🔄" ;;
    queued|QUEUED) echo "⏳" ;;
    completed|COMPLETED)
      case "$2" in
        success|SUCCESS) echo "✅" ;;
        failure|FAILURE) echo "❌" ;;
        cancelled|CANCELLED) echo "🚫" ;;
        skipped|SKIPPED) echo "⏭️" ;;
        *) echo "❓" ;;
      esac
      ;;
    *) echo "❓" ;;
  esac
}

# Check if we've already seen this run
is_seen() {
  local run_id=$1
  [[ ${#SEEN_RUNS[@]} -eq 0 ]] && return 1
  for seen in "${SEEN_RUNS[@]}"; do
    [[ "$seen" == "$run_id" ]] && return 0
  done
  return 1
}

while true; do
  CURRENT_TIME=$(date '+%H:%M:%S')
  NEW_RUNS_FOUND=0

  # Query runs related to this issue
  # Look for runs in the last 24 hours that might be related
  RUNS=$(gh run list \
    --repo "$REPO" \
    --limit 50 \
    --json databaseId,workflowName,status,conclusion,createdAt,updatedAt,event,url,headBranch \
    2>/dev/null || echo "[]")

  # Filter runs related to this issue
  # - Developer workflow triggered by issue assignment
  # - Review workflow on PRs for this issue
  # - Claude Code on comments mentioning issue
  # - Runs on branches matching feat/ISSUE-* or fix/ISSUE-*

  RELATED_RUNS=$(echo "$RUNS" | jq -r --arg issue "$ISSUE" '
    .[] | select(
      (.workflowName == "Developer Agent Auto-Implement") or
      (.workflowName == "Review Agent Auto-Review") or
      (.workflowName == "Claude Code") or
      (.headBranch | test("(feat|fix)/\($issue)-"))
    ) | @json
  ')

  if [[ -n "$RELATED_RUNS" ]]; then
    while IFS= read -r run_json; do
      RUN_ID=$(echo "$run_json" | jq -r '.databaseId')

      # Skip if already seen and completed
      if is_seen "$RUN_ID"; then
        STATUS=$(echo "$run_json" | jq -r '.status')
        [[ "$STATUS" == "completed" ]] && continue
      fi

      WORKFLOW=$(echo "$run_json" | jq -r '.workflowName')
      STATUS=$(echo "$run_json" | jq -r '.status')
      CONCLUSION=$(echo "$run_json" | jq -r '.conclusion // ""')
      URL=$(echo "$run_json" | jq -r '.url')
      BRANCH=$(echo "$run_json" | jq -r '.headBranch // ""')
      CREATED=$(echo "$run_json" | jq -r '.createdAt')
      UPDATED=$(echo "$run_json" | jq -r '.updatedAt')

      # Calculate duration
      CREATED_TS=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$CREATED" "+%s" 2>/dev/null || echo "0")
      UPDATED_TS=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED" "+%s" 2>/dev/null || echo "0")
      DURATION=$((UPDATED_TS - CREATED_TS))

      # Get emoji
      EMOJI=$(status_emoji "$STATUS" "$CONCLUSION")

      # Mark as seen if completed
      if [[ "$STATUS" == "completed" ]] && ! is_seen "$RUN_ID"; then
        SEEN_RUNS+=("$RUN_ID")
        NEW_RUNS_FOUND=1
      elif [[ "$STATUS" != "completed" ]]; then
        NEW_RUNS_FOUND=1
      fi

      # Display run info with clickable link
      printf "%s %s [%s] %s" "$CURRENT_TIME" "$EMOJI" "$STATUS" "$WORKFLOW"
      [[ -n "$BRANCH" ]] && printf " (%s)" "$BRANCH"
      [[ $DURATION -gt 0 ]] && printf " - %s" "$(format_duration $DURATION)"
      echo ""
      echo "    🔗 $URL"

      # Show conclusion if completed
      if [[ "$STATUS" == "completed" && -n "$CONCLUSION" ]]; then
        echo "    └─ Conclusion: $CONCLUSION"
      fi
      echo ""

    done <<< "$RELATED_RUNS"
  fi

  if [[ $NEW_RUNS_FOUND -eq 0 ]]; then
    echo "$CURRENT_TIME - No active or new runs for issue #$ISSUE (checking again in ${POLL_INTERVAL}s...)"
  fi

  sleep $POLL_INTERVAL
done
