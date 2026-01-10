#!/usr/bin/env bash
# Watch agent workflow logs in separate terminal panes

set -euo pipefail

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Install with: brew install tmux"
    exit 1
fi

# Check if gh CLI is installed
if ! command -v gh &> /dev/null; then
    echo "Error: gh CLI is not installed. Install with: brew install gh"
    exit 1
fi

SESSION_NAME="agent-monitor"

# Function to get latest run ID for workflows
get_latest_run() {
    local workflows=("$@")
    local run_id=""

    for workflow in "${workflows[@]}"; do
        run_id=$(gh run list --workflow="$workflow" --status in_progress --status queued --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || echo "")
        if [[ -n "$run_id" ]]; then
            echo "$run_id"
            return 0
        fi
    done

    echo ""
}

# Get run IDs for each agent type
echo "Searching for active workflow runs..."

SCRUM_RUN=$(get_latest_run "notify_scrum_ready.yml" "assign_backlog.yml" "auto-check-tasklist.yml" "add-to-release-issue-on-milestone.yml")
DEV_RUN=$(get_latest_run "developer_auto_implement.yml" "claude.yml")
REVIEW_RUN=$(get_latest_run "review_agent_auto_review.yml" "claude-code-review.yml")

# Kill existing session if it exists
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

# Create new session with first pane (Scrummaster)
if [[ -n "$SCRUM_RUN" ]]; then
    echo "Found Scrummaster run: $SCRUM_RUN"
    tmux new-session -d -s "$SESSION_NAME" -n "Agents" "gh run watch $SCRUM_RUN"
else
    echo "No active Scrummaster runs"
    tmux new-session -d -s "$SESSION_NAME" -n "Agents" "echo '=== SCRUMMASTER AGENT ==='; echo 'No active runs'; echo 'Waiting for runs...'; while true; do sleep 5; done"
fi

# Split horizontally for Developer pane
if [[ -n "$DEV_RUN" ]]; then
    echo "Found Developer run: $DEV_RUN"
    tmux split-window -h -t "$SESSION_NAME:0" "gh run watch $DEV_RUN"
else
    echo "No active Developer runs"
    tmux split-window -h -t "$SESSION_NAME:0" "echo '=== DEVELOPER AGENT ==='; echo 'No active runs'; echo 'Waiting for runs...'; while true; do sleep 5; done"
fi

# Split the left pane vertically for Reviewer pane
if [[ -n "$REVIEW_RUN" ]]; then
    echo "Found Reviewer run: $REVIEW_RUN"
    tmux split-window -v -t "$SESSION_NAME:0.0" "gh run watch $REVIEW_RUN"
else
    echo "No active Reviewer runs"
    tmux split-window -v -t "$SESSION_NAME:0.0" "echo '=== REVIEWER AGENT ==='; echo 'No active runs'; echo 'Waiting for runs...'; while true; do sleep 5; done"
fi

# Adjust layout to tile evenly
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

echo ""
echo "✅ Monitoring session started"
echo "   Scrummaster: ${SCRUM_RUN:-no active run}"
echo "   Developer:   ${DEV_RUN:-no active run}"
echo "   Reviewer:    ${REVIEW_RUN:-no active run}"
echo ""

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
