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

# Create monitoring script for each agent type
create_monitor_script() {
    local agent_name="$1"
    shift
    local workflows=("$@")

    cat <<'SCRIPT_END'
#!/usr/bin/env bash
set -euo pipefail

AGENT_NAME="AGENT_NAME_PLACEHOLDER"
WORKFLOWS=(WORKFLOWS_PLACEHOLDER)
LAST_SEEN_RUN=""

echo "=== $AGENT_NAME AGENT ==="
echo "Monitoring workflows: ${WORKFLOWS[*]}"
echo ""

while true; do
    # Find latest active run (in_progress or queued)
    RUN_ID=""
    for workflow in "${WORKFLOWS[@]}"; do
        RUN_ID=$(gh run list --workflow="$workflow" --status in_progress --status queued --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || echo "")
        if [[ -n "$RUN_ID" ]]; then
            break
        fi
    done

    if [[ -n "$RUN_ID" ]]; then
        echo "$(date '+%H:%M:%S') - Found active run: $RUN_ID"
        echo "Watching..."
        LAST_SEEN_RUN="$RUN_ID"
        gh run watch "$RUN_ID" 2>&1 || true
        echo ""
        echo "Run $RUN_ID completed. Checking for new runs..."
        sleep 2
    else
        # No active runs, check for recently completed quick runs
        for workflow in "${WORKFLOWS[@]}"; do
            QUICK_RUN=$(gh run list --workflow="$workflow" --status completed --limit 5 --json databaseId,conclusion,startedAt,updatedAt,createdAt --jq '.[] | select(.databaseId != '${LAST_SEEN_RUN:-0}') | select(
                ((.updatedAt | fromdateiso8601) - (.startedAt // .createdAt | fromdateiso8601)) < 30
            ) | .databaseId' 2>/dev/null | head -1 || echo "")

            if [[ -n "$QUICK_RUN" ]]; then
                echo "$(date '+%H:%M:%S') - Found quick completed run: $QUICK_RUN (< 30s duration)"
                echo "Showing log..."
                LAST_SEEN_RUN="$QUICK_RUN"
                gh run view "$QUICK_RUN" --log 2>&1 || true
                echo ""
                echo "─────────────────────────────────────────────────"
                sleep 2
                break
            fi
        done

        if [[ -z "$QUICK_RUN" ]]; then
            echo "$(date '+%H:%M:%S') - No active or quick completed runs, checking again in 5s..."
            sleep 5
        fi
    fi
done
SCRIPT_END
}

# Create temp scripts for each agent
SCRUM_SCRIPT=$(mktemp)
DEV_SCRIPT=$(mktemp)
REVIEW_SCRIPT=$(mktemp)

# Generate Scrummaster monitoring script
create_monitor_script "SCRUMMASTER" "notify_scrum_ready.yml" "assign_backlog.yml" "auto-check-tasklist.yml" "add-to-release-issue-on-milestone.yml" | \
    sed 's/AGENT_NAME_PLACEHOLDER/SCRUMMASTER/' | \
    sed 's/WORKFLOWS_PLACEHOLDER/"notify_scrum_ready.yml" "assign_backlog.yml" "auto-check-tasklist.yml" "add-to-release-issue-on-milestone.yml"/' > "$SCRUM_SCRIPT"
chmod +x "$SCRUM_SCRIPT"

# Generate Developer monitoring script
create_monitor_script "DEVELOPER" "developer_auto_implement.yml" "claude.yml" | \
    sed 's/AGENT_NAME_PLACEHOLDER/DEVELOPER/' | \
    sed 's/WORKFLOWS_PLACEHOLDER/"developer_auto_implement.yml" "claude.yml"/' > "$DEV_SCRIPT"
chmod +x "$DEV_SCRIPT"

# Generate Reviewer monitoring script
create_monitor_script "REVIEWER" "review_agent_auto_review.yml" "claude-code-review.yml" | \
    sed 's/AGENT_NAME_PLACEHOLDER/REVIEWER/' | \
    sed 's/WORKFLOWS_PLACEHOLDER/"review_agent_auto_review.yml" "claude-code-review.yml"/' > "$REVIEW_SCRIPT"
chmod +x "$REVIEW_SCRIPT"

# Create tmux session with monitoring panes
tmux new-session -d -s "$SESSION_NAME" -n "Agents" "bash $SCRUM_SCRIPT; rm -f $SCRUM_SCRIPT"
tmux split-window -h -t "$SESSION_NAME:0" "bash $DEV_SCRIPT; rm -f $DEV_SCRIPT"
tmux split-window -v -t "$SESSION_NAME:0.0" "bash $REVIEW_SCRIPT; rm -f $REVIEW_SCRIPT"

# Adjust layout to tile evenly
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

echo "✅ Agent monitoring session started"
echo ""
echo "Each pane continuously polls for:"
echo "  - Active runs (in_progress/queued) → streams live with 'gh run watch'"
echo "  - Quick completed runs (< 30s) → shows full log with 'gh run view --log'"
echo ""
echo "Press Ctrl+b then d to detach (keeps running in background)"
echo "Run 'tmux attach -t agent-monitor' to reattach"
echo ""

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
