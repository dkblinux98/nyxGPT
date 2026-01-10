#!/usr/bin/env bash
# Trigger the scrummaster agent to select and start the next issue

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  trigger_next_issue.sh <release_tracking_issue_number>

Posts a READY_FOR_NEXT_ISSUE comment to the specified issue to trigger
the scrummaster agent workflow.

Example:
  trigger_next_issue.sh 2843

This will:
  1. Post "@myGPT-scrummaster-agent READY_FOR_NEXT_ISSUE" to issue #2843
  2. Trigger the scrummaster workflow to select the next backlog issue
  3. Auto-implement the selected issue via developer agent

Monitor progress with: ./scripts/watch_agents.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ISSUE="${1:-}"

if [[ -z "$ISSUE" ]]; then
  echo "Error: Issue number required" >&2
  usage >&2
  exit 1
fi

if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "Error: Issue number must be numeric, got: '$ISSUE'" >&2
  exit 1
fi

# Check if gh CLI is installed and authenticated
if ! command -v gh &> /dev/null; then
  echo "Error: gh CLI is not installed. Install with: brew install gh" >&2
  exit 1
fi

if ! gh auth status &> /dev/null; then
  echo "Error: gh CLI not authenticated. Run: gh auth login" >&2
  exit 1
fi

# Get repo from git remote (assumes origin)
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "")

if [[ -z "$REPO" ]]; then
  echo "Error: Could not determine repository. Are you in a git repo with 'origin' remote?" >&2
  exit 1
fi

echo "Posting trigger comment to issue #$ISSUE in $REPO..."

# Post the comment
gh issue comment "$ISSUE" --repo "$REPO" --body "@myGPT-scrummaster-agent READY_FOR_NEXT_ISSUE"

echo "✅ Successfully posted trigger comment to issue #$ISSUE"
echo ""
echo "The workflow will now:"
echo "  1. Select the next backlog issue (lowest Phase, lowest issue number)"
echo "  2. Move it to In Progress and assign to developer-agent"
echo "  3. Auto-implement with Claude Code"
echo "  4. Create a PR and submit for review"
echo ""
echo "Monitor progress:"
echo "  ./scripts/watch_agents.sh"
echo ""
echo "Or view the issue:"
echo "  https://github.com/$REPO/issues/$ISSUE"
