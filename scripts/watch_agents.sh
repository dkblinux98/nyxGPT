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
  "watch -n 5 'echo \"=== SCRUMMASTER AGENT ===\"; gh run list --workflow=\"Scrummaster Agent - Select and Start Next Issue\" --workflow=\"Assign Backlog Issues to scrummaster-agent\" --workflow=\"Auto-check Release Tracking Issues\" --workflow=\"Add issue to release issue on milestone assignment\" --status in_progress --status queued --limit 10'"

# Split horizontally for Developer pane
tmux split-window -h -t "$SESSION_NAME:0" \
  "watch -n 5 'echo \"=== DEVELOPER AGENT ===\"; gh run list --workflow=\"Developer Agent Auto-Implement\" --workflow=\"Claude Code\" --status in_progress --status queued --limit 10'"

# Split the left pane vertically for Reviewer pane
tmux split-window -v -t "$SESSION_NAME:0.0" \
  "watch -n 5 'echo \"=== REVIEWER AGENT ===\"; gh run list --workflow=\"Review Agent Auto-Review\" --workflow=\"Claude Code Review\" --status in_progress --status queued --limit 10'"

# Adjust layout to tile evenly
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
