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
                         Common: "Acceptance Failure", Bug, Feature, Improvement

  Project Fields:
  --module MODULE        Module field value (API, RAG, TUI, Web UI, CLI, Agent System, etc.)
  --phase PHASE          Phase field value (if different from current milestone)
  --priority PRIORITY    Priority: P0, P1, P2, P3 (or full: "P0 - Critical", etc.)
  --effort EFFORT        Effort: XS, S, M, L, XL
  --status STATUS        Status (default: Backlog)
  --sprint NUMBER        Sprint number (default: auto-detect current sprint)
  --milestone NUMBER     Milestone number (default: auto-detect current milestone)

  Relationships:
  --copy-from-issue NUM  Copy Module/Phase/Priority/Effort/Sprint/Milestone from issue
  --blocks NUM           Mark this issue as blocking issue NUM
  --pr NUM               Link to PR number (sets Development field)

  Assignment:
  --assignee USER        GitHub username to assign

  Other:
  --no-auto-sprint       Don't auto-detect current sprint
  --no-auto-milestone    Don't auto-detect current milestone
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

# --- Helper functions ---
get_current_sprint() {
  # Get current sprint based on today's date
  local today=$(date +%Y-%m-%d)
  gh api graphql -f query='
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        projectsV2(first: 1) {
          nodes {
            field(name: "Sprint") {
              ... on ProjectV2IterationField {
                configuration {
                  iterations {
                    id
                    title
                    startDate
                  }
                }
              }
            }
          }
        }
      }
    }' -f owner="$REPO_OWNER" -f name="$REPO_NAME" | \
    jq -r --arg today "$today" '
      .data.repository.projectsV2.nodes[0].field.configuration.iterations[]
      | select(.startDate <= $today)
      | .title' | tail -1
}

get_current_milestone() {
  # Get earliest open milestone
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=open" --jq '.[0].title // empty'
}

normalize_priority() {
  case "$1" in
    P0|p0|Critical|critical) echo "P0 - Critical" ;;
    P1|p1|High|high) echo "P1 - High" ;;
    P2|p2|Medium|medium) echo "P2 - Medium" ;;
    P3|p3|Low|low) echo "P3 - Low" ;;
    "P0 - Critical"|"P1 - High"|"P2 - Medium"|"P3 - Low") echo "$1" ;;
    *) echo "[warning] Unknown priority '$1', using 'P2 - Medium'" >&2; echo "P2 - Medium" ;;
  esac
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
SPRINT=""
ASSIGNEE=""
MILESTONE=""
COPY_FROM_ISSUE=""
BLOCKS=""
PR_LINK=""
AUTO_SPRINT=1
AUTO_MILESTONE=1
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
    --sprint)
      SPRINT="$2"
      AUTO_SPRINT=0
      shift 2
      ;;
    --assignee)
      ASSIGNEE="$2"
      shift 2
      ;;
    --milestone)
      MILESTONE="$2"
      AUTO_MILESTONE=0
      shift 2
      ;;
    --copy-from-issue)
      COPY_FROM_ISSUE="$2"
      shift 2
      ;;
    --blocks)
      BLOCKS="$2"
      shift 2
      ;;
    --pr)
      PR_LINK="$2"
      shift 2
      ;;
    --no-auto-sprint)
      AUTO_SPRINT=0
      shift
      ;;
    --no-auto-milestone)
      AUTO_MILESTONE=0
      shift
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

# --- Copy fields from another issue if requested ---
if [[ -n "$COPY_FROM_ISSUE" ]]; then
  echo "[create-issue] Copying fields from issue #${COPY_FROM_ISSUE}..." >&2

  # Get source issue's project item
  SOURCE_CONTENT_ID=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${COPY_FROM_ISSUE}" --jq '.node_id')
  SOURCE_ITEM=$(get_project_item "$SOURCE_CONTENT_ID")

  if [[ -z "$SOURCE_ITEM" || "$SOURCE_ITEM" == "null" ]]; then
    echo "[warning] Source issue #${COPY_FROM_ISSUE} not in project, cannot copy fields" >&2
  else
    # Query project fields from source issue
    SOURCE_FIELDS=$(gh api graphql -f query="
      query {
        node(id: \"$SOURCE_ITEM\") {
          ... on ProjectV2Item {
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field { ... on ProjectV2SingleSelectField { name } }
                  name
                }
                ... on ProjectV2ItemFieldIterationValue {
                  field { ... on ProjectV2IterationField { name } }
                  title
                }
              }
            }
          }
        }
      }" --jq '.data.node.fieldValues.nodes')

    # Copy fields if not already set
    [[ -z "$MODULE" ]] && MODULE=$(echo "$SOURCE_FIELDS" | jq -r '.[] | select(.field.name == "Module") | .name // empty')
    [[ -z "$PHASE" ]] && PHASE=$(echo "$SOURCE_FIELDS" | jq -r '.[] | select(.field.name == "Phase") | .name // empty')
    [[ -z "$PRIORITY" ]] && PRIORITY=$(echo "$SOURCE_FIELDS" | jq -r '.[] | select(.field.name == "Priority") | .name // empty')
    [[ -z "$EFFORT" ]] && EFFORT=$(echo "$SOURCE_FIELDS" | jq -r '.[] | select(.field.name == "Effort") | .name // empty')
    [[ -z "$SPRINT" ]] && SPRINT=$(echo "$SOURCE_FIELDS" | jq -r '.[] | select(.field.name == "Sprint") | .title // empty')

    # Copy milestone if not set
    if [[ -z "$MILESTONE" && "$AUTO_MILESTONE" == "1" ]]; then
      MILESTONE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${COPY_FROM_ISSUE}" --jq '.milestone.title // empty')
      [[ -n "$MILESTONE" ]] && AUTO_MILESTONE=0
    fi

    echo "[create-issue]   Copied from #${COPY_FROM_ISSUE}: Module=$MODULE, Priority=$PRIORITY, Effort=$EFFORT" >&2
  fi
fi

# --- Auto-detect current sprint if not set ---
if [[ -z "$SPRINT" && "$AUTO_SPRINT" == "1" ]]; then
  echo "[create-issue] Auto-detecting current sprint..." >&2
  SPRINT=$(get_current_sprint)
  [[ -n "$SPRINT" ]] && echo "[create-issue]   Current sprint: $SPRINT" >&2
fi

# --- Auto-detect current milestone if not set ---
if [[ -z "$MILESTONE" && "$AUTO_MILESTONE" == "1" ]]; then
  echo "[create-issue] Auto-detecting current milestone..." >&2
  MILESTONE=$(get_current_milestone)
  [[ -n "$MILESTONE" ]] && echo "[create-issue]   Current milestone: $MILESTONE" >&2
fi

# --- Normalize priority value ---
if [[ -n "$PRIORITY" ]]; then
  PRIORITY=$(normalize_priority "$PRIORITY")
fi

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
  echo "  Sprint: ${SPRINT:-not set}" >&2
  echo "  Assignee: ${ASSIGNEE:-not set}" >&2
  echo "  Milestone: ${MILESTONE:-not set}" >&2
  [[ -n "$COPY_FROM_ISSUE" ]] && echo "  Copy from: #$COPY_FROM_ISSUE" >&2
  [[ -n "$BLOCKS" ]] && echo "  Blocks: #$BLOCKS" >&2
  [[ -n "$PR_LINK" ]] && echo "  Linked PR: #$PR_LINK" >&2
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

ITEM_ID=$(ensure_issue_in_project "$ISSUE_NUMBER")

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

# Sprint
if [[ -n "$SPRINT" ]]; then
  set_project_field_value "$ITEM_ID" "Sprint" "$SPRINT" || \
    echo "[warning] Failed to set Sprint to '$SPRINT'" >&2
  echo "[create-issue]   Sprint: $SPRINT" >&2
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

# --- Step 4: Set relationships ---
if [[ -n "$BLOCKS" ]]; then
  echo "[create-issue] Setting blocking relationship..." >&2

  # Check if blocked issue is closed
  BLOCKED_STATE=$(gh issue view "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}" --json state --jq '.state')

  if [[ "$BLOCKED_STATE" == "CLOSED" ]]; then
    echo "[create-issue]   Blocked issue #${BLOCKS} is closed - reopening..." >&2

    # Reopen the blocked issue
    gh issue reopen "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}"

    # Post comment explaining why it was reopened
    gh issue comment "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}" --body "$(cat <<EOF
⚠️ **Reopened due to Acceptance Failure**

This issue was reopened because issue #${ISSUE_NUMBER} was created to address acceptance failures from the previous implementation.

**Acceptance Failure Issue:** #${ISSUE_NUMBER}
**Reason:** Unresolved issues from code review that must be completed before this work can be considered done.

**Next Steps:**
- Issue #${ISSUE_NUMBER} will be implemented
- When #${ISSUE_NUMBER} is complete and merged, both issues will close together
- The PR for #${ISSUE_NUMBER} will include: \`Closes #${ISSUE_NUMBER}\` and \`Closes #${BLOCKS}\`
EOF
)"
    echo "[create-issue]   ✓ Reopened issue #${BLOCKS}" >&2
  fi

  # Add "Blocks #N" to issue body via edit
  gh issue edit "$ISSUE_NUMBER" --add-label "blocks" --repo "${REPO_OWNER}/${REPO_NAME}" 2>/dev/null || true

  # Post comment to create blocking relationship
  gh issue comment "$ISSUE_NUMBER" --body "Blocks #${BLOCKS}" --repo "${REPO_OWNER}/${REPO_NAME}"

  # Post comment to blocked issue
  gh issue comment "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}" --body "Blocked by #${ISSUE_NUMBER} (Acceptance Failure)"

  echo "[create-issue]   Blocks: #$BLOCKS" >&2
fi

# --- Step 5: Link PR (Development field) ---
if [[ -n "$PR_LINK" ]]; then
  echo "[create-issue] Linking to PR #${PR_LINK}..." >&2
  # Link via comment (GitHub auto-links)
  gh issue comment "$ISSUE_NUMBER" --body "Related PR: #${PR_LINK}" --repo "${REPO_OWNER}/${REPO_NAME}"
  echo "[create-issue]   Linked PR: #$PR_LINK" >&2
fi

# --- Done ---
echo "" >&2
echo "✅ Issue #${ISSUE_NUMBER} created with full project hygiene" >&2
echo "   URL: $ISSUE_URL" >&2
echo "   Status: $STATUS" >&2
[[ -n "$SPRINT" ]] && echo "   Sprint: $SPRINT" >&2
[[ -n "$MODULE" ]] && echo "   Module: $MODULE" >&2
[[ -n "$PHASE" ]] && echo "   Phase: $PHASE" >&2
[[ -n "$PRIORITY" ]] && echo "   Priority: $PRIORITY" >&2
[[ -n "$EFFORT" ]] && echo "   Effort: $EFFORT" >&2
[[ -n "$ASSIGNEE" ]] && echo "   Assignee: @$ASSIGNEE" >&2
[[ -n "$MILESTONE" ]] && echo "   Milestone: $MILESTONE" >&2
[[ -n "$BLOCKS" ]] && echo "   Blocks: #$BLOCKS" >&2
[[ -n "$PR_LINK" ]] && echo "   Linked PR: #$PR_LINK" >&2

# Output just the issue number for scripting
echo "$ISSUE_NUMBER"
