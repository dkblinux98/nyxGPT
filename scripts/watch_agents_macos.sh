#!/usr/bin/env bash
# Watch agent workflow logs in separate macOS Terminal windows

set -euo pipefail

# Check if gh CLI is installed
if ! command -v gh &> /dev/null; then
    echo "Error: gh CLI is not installed. Install with: brew install gh"
    exit 1
fi

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"

# Open Scrummaster monitor in new Terminal window
osascript <<EOF
tell application "Terminal"
    do script "cd '$REPO_DIR' && watch -n 5 'echo \"=== SCRUMMASTER AGENT ===\"; gh run list --workflow=notify_scrum_ready.yml --workflow=assign_backlog.yml --workflow=auto-check-tasklist.yml --workflow=add-to-release-issue-on-milestone.yml --status in_progress --status queued --limit 10'"
    set custom title of front window to "Scrummaster Agent"
end tell
EOF

sleep 1

# Open Developer monitor in new Terminal window
osascript <<EOF
tell application "Terminal"
    do script "cd '$REPO_DIR' && watch -n 5 'echo \"=== DEVELOPER AGENT ===\"; gh run list --workflow=developer_auto_implement.yml --workflow=claude.yml --status in_progress --status queued --limit 10'"
    set custom title of front window to "Developer Agent"
end tell
EOF

sleep 1

# Open Reviewer monitor in new Terminal window
osascript <<EOF
tell application "Terminal"
    do script "cd '$REPO_DIR' && watch -n 5 'echo \"=== REVIEWER AGENT ===\"; gh run list --workflow=review_agent_auto_review.yml --workflow=claude-code-review.yml --status in_progress --status queued --limit 10'"
    set custom title of front window to "Reviewer Agent"
end tell
EOF

echo "✅ Opened 3 Terminal windows monitoring agent workflows"
echo "Close the terminal windows when done monitoring"
