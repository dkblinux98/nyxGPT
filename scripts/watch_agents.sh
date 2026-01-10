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

# Kill existing session if it exists
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

# Create new session with first pane (Scrummaster)
tmux new-session -d -s "$SESSION_NAME" -n "Agents" \
  "watch -n 5 'echo \"=== SCRUMMASTER AGENT ===\"; gh run list --workflow=notify_scrum_ready.yml --workflow=assign_backlog.yml --workflow=auto-check-tasklist.yml --workflow=add-to-release-issue-on-milestone.yml --status in_progress --status queued --limit 10'"

# Split horizontally for Developer pane
tmux split-window -h -t "$SESSION_NAME:0" \
  "watch -n 5 'echo \"=== DEVELOPER AGENT ===\"; gh run list --workflow=developer_auto_implement.yml --workflow=claude.yml --status in_progress --status queued --limit 10'"

# Split the left pane vertically for Reviewer pane
tmux split-window -v -t "$SESSION_NAME:0.0" \
  "watch -n 5 'echo \"=== REVIEWER AGENT ===\"; gh run list --workflow=review_agent_auto_review.yml --workflow=claude-code-review.yml --status in_progress --status queued --limit 10'"

# Adjust layout to tile evenly
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
