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

# Create new session with first pane (All active runs)
tmux new-session -d -s "$SESSION_NAME" -n "Agents" \
  "watch -n 5 'echo \"=== ACTIVE AGENT WORKFLOWS ===\"; echo; gh run list --limit 15 | grep -E \"in_progress|queued\" || echo \"No active runs\"; echo; echo \"To watch a specific run: gh run watch <run_id>\"'"

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
