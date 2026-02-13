#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  create_issue.sh [options]

Creates a GitHub issue with proper project hygiene:
- Adds to project
- Sets Status to Backlog (or specified status)
- Sets project fields (Module, Phase, Priority, Effort)
- Optionally assigns to agent
- Optionally sets milestone

Options:
  --title TITLE          Issue title (required)
  --body BODY            Issue body (required, can use heredoc)
  --body-file FILE       Read body from file
  --label LABEL          Label to apply (can specify multiple times)
  --module MODULE        Module field value
  --phase PHASE          Phase field value
  --priority PRIORITY    Priority field value (Low, Medium, High, Critical)
  --effort EFFORT        Effort field value (XS, S, M, L, XL)
  --status STATUS        Status field value (default: Backlog)
  --assignee USER        GitHub username to assign
  --milestone NUMBER     Milestone number or name
  --dry-run              Print what would be created without creating
  -h, --help             Show this help

Examples:
  # Basic issue
  create_issue.sh \
    --title "Fix: memory leak in embeddings" \
    --label Bug \
    --body "Description here"

  # Full project hygiene
  create_issue.sh \
    --title "Feature: Add async support" \
    --label Feature \
    --module "RAG" \
    --phase "Phase 4 – Scale & Performance" \
    --priority High \
    --effort M \
    --assignee myGPT-developer-agent \
    --body-file /tmp/issue-body.md

  # From heredoc
  create_issue.sh \
    --title "Bug: timeout in API" \
    --label Bug \
    --body "$(cat <<'BODY'
## Problem
API times out after 30s

## Steps to Reproduce
1. Call /api/chat with large prompt
BODY
)"

Environment:
  Requires GH_TOKEN or agent token to be set
  Uses config from ~/.nyxGPT/config.ini (via NYXGPT_CONFIG_FILE)
EOF
}

# --- Parse arguments ---
TITLE=""
BODY=""
BODY_FILE=""
LABELS=()
MODULE=""
PHASE=""
PRIORITY=""
EFFORT=""
STATUS="Backlog"
ASSIGNEE=""
MILESTONE=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)
      TITLE="$2"
      shift 2
      ;;
    --body)
      BODY="$2"
      shift 2
      ;;
    --body-file)
      BODY_FILE="$2"
      shift 2
      ;;
    --label)
      LABELS+=("$2")
      shift 2
      ;;
    --module)
      MODULE="$2"
      shift 2
      ;;
    --phase)
      PHASE="$2"
      shift 2
      ;;
    --priority)
      PRIORITY="$2"
      shift 2
      ;;
    --effort)
      EFFORT="$2"
      shift 2
      ;;
    --status)
      STATUS="$2"
      shift 2
      ;;
    --assignee)
      ASSIGNEE="$2"
      shift 2
      ;;
    --milestone)
      MILESTONE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[error] Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# --- Validate required args ---
if [[ -z "$TITLE" ]]; then
  echo "[error] --title is required" >&2
  usage >&2
  exit 2
fi

if [[ -z "$BODY" && -z "$BODY_FILE" ]]; then
  echo "[error] Either --body or --body-file is required" >&2
  usage >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  [[ -f "$BODY_FILE" ]] || {
    echo "[error] Body file not found: $BODY_FILE" >&2
    exit 2
  }
  BODY="$(cat "$BODY_FILE")"
fi

# --- Load config and auth ---
load_config
require_gh_auth

# --- Dry run output ---
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] Would create issue with:" >&2
  echo "  Title: $TITLE" >&2
  echo "  Body: ${BODY:0:100}..." >&2
  echo "  Labels: ${LABELS[*]:-none}" >&2
  echo "  Module: ${MODULE:-not set}" >&2
  echo "  Phase: ${PHASE:-not set}" >&2
  echo "  Priority: ${PRIORITY:-not set}" >&2
  echo "  Effort: ${EFFORT:-not set}" >&2
  echo "  Status: $STATUS" >&2
  echo "  Assignee: ${ASSIGNEE:-not set}" >&2
  echo "  Milestone: ${MILESTONE:-not set}" >&2
  exit 0
fi

# --- Step 1: Create the issue ---
echo "[create-issue] Creating issue: $TITLE" >&2

GH_ARGS=("--title" "$TITLE" "--body" "$BODY")

for label in "${LABELS[@]}"; do
  GH_ARGS+=("--label" "$label")
done

if [[ -n "$ASSIGNEE" ]]; then
  GH_ARGS+=("--assignee" "$ASSIGNEE")
fi

if [[ -n "$MILESTONE" ]]; then
  GH_ARGS+=("--milestone" "$MILESTONE")
fi

ISSUE_URL=$(gh issue create "${GH_ARGS[@]}" --repo "${REPO_OWNER}/${REPO_NAME}")
ISSUE_NUMBER=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')

echo "[create-issue] ✓ Created issue #${ISSUE_NUMBER}" >&2

# --- Step 2: Add to project ---
echo "[create-issue] Adding issue #${ISSUE_NUMBER} to project..." >&2

CONTENT_ID=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE_NUMBER}" --jq '.node_id')
ITEM_ID=$(add_to_project "$CONTENT_ID")

if [[ -z "$ITEM_ID" || "$ITEM_ID" == "null" ]]; then
  echo "[warning] Failed to add issue to project - continuing with field updates" >&2
else
  echo "[create-issue] ✓ Added to project (item ID: ${ITEM_ID:0:20}...)" >&2
fi

# --- Step 3: Set project fields ---
echo "[create-issue] Setting project fields..." >&2

# Status (always set, defaults to Backlog)
if [[ -n "$STATUS" ]]; then
  set_project_field_value "$ITEM_ID" "$STATUS_FIELD" "$STATUS" || \
    echo "[warning] Failed to set Status to '$STATUS'" >&2
  echo "[create-issue]   Status: $STATUS" >&2
fi

# Module
if [[ -n "$MODULE" ]]; then
  set_project_field_value "$ITEM_ID" "Module" "$MODULE" || \
    echo "[warning] Failed to set Module to '$MODULE'" >&2
  echo "[create-issue]   Module: $MODULE" >&2
fi

# Phase
if [[ -n "$PHASE" ]]; then
  set_project_field_value "$ITEM_ID" "Phase" "$PHASE" || \
    echo "[warning] Failed to set Phase to '$PHASE'" >&2
  echo "[create-issue]   Phase: $PHASE" >&2
fi

# Priority
if [[ -n "$PRIORITY" ]]; then
  set_project_field_value "$ITEM_ID" "Priority" "$PRIORITY" || \
    echo "[warning] Failed to set Priority to '$PRIORITY'" >&2
  echo "[create-issue]   Priority: $PRIORITY" >&2
fi

# Effort
if [[ -n "$EFFORT" ]]; then
  set_project_field_value "$ITEM_ID" "Effort" "$EFFORT" || \
    echo "[warning] Failed to set Effort to '$EFFORT'" >&2
  echo "[create-issue]   Effort: $EFFORT" >&2
fi

# --- Done ---
echo "" >&2
echo "✅ Issue #${ISSUE_NUMBER} created with full project hygiene" >&2
echo "   URL: $ISSUE_URL" >&2
echo "   Status: $STATUS" >&2
[[ -n "$MODULE" ]] && echo "   Module: $MODULE" >&2
[[ -n "$PHASE" ]] && echo "   Phase: $PHASE" >&2
[[ -n "$PRIORITY" ]] && echo "   Priority: $PRIORITY" >&2
[[ -n "$EFFORT" ]] && echo "   Effort: $EFFORT" >&2
[[ -n "$ASSIGNEE" ]] && echo "   Assignee: @$ASSIGNEE" >&2
[[ -n "$MILESTONE" ]] && echo "   Milestone: $MILESTONE" >&2

# Output just the issue number for scripting
echo "$ISSUE_NUMBER"
