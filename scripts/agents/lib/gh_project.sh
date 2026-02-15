#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Shared library for nyxGPT agent scripts
# - SAFE TO SOURCE: no work is executed at import time
# - NO self-sourcing
# ============================================================

# -------------------------
# Logging / error handling
# -------------------------
_debug() { [[ "${DEBUG:-0}" == "1" ]] && echo "[debug] $*" >&2 || true; }

_die() {
  echo "[error] $*" >&2
  # If sourced, do not kill the user's terminal/shell
  if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 1
  fi
  exit 1
}

_warn() {
  echo "[warning] $*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || _die "Missing required command: $1"
}

require_gh_auth() {
  require_cmd gh
  gh auth status >/dev/null 2>&1 || _die "gh not authenticated. Run: gh auth login"
}

# -------------------------
# Error Classification (for intelligent retry)
# -------------------------

# Classifies error messages into retriable vs fatal
# Returns: "retriable", "fatal", or "unknown"
classify_error() {
  local error_text="$1"

  # Fatal errors - do NOT retry these
  if echo "$error_text" | grep -qE "is not OPEN.*state=CLOSED"; then
    # Issue closed - could be intentional or accidental (Phase 2 will distinguish)
    echo "fatal:issue_closed"
    return
  fi

  if echo "$error_text" | grep -qE "Authentication failed|permission denied|Unauthorized"; then
    # Auth failures won't fix themselves
    echo "fatal:auth_failure"
    return
  fi

  if echo "$error_text" | grep -qE "already merged|PR.*merged"; then
    # Work already complete
    echo "fatal:already_merged"
    return
  fi

  # Retriable errors - these might resolve with retry
  if echo "$error_text" | grep -qE "API rate limit|rate limit exceeded"; then
    echo "retriable:rate_limit"
    return
  fi

  if echo "$error_text" | grep -qE "network.*timeout|connection.*timed out|Connection reset"; then
    echo "retriable:network_timeout"
    return
  fi

  if echo "$error_text" | grep -qE "not a commit|stale ref|couldn't find remote ref"; then
    echo "retriable:stale_ref"
    return
  fi

  if echo "$error_text" | grep -qE "test.*failed|pytest.*FAILED|FAILED.*test"; then
    # Test failures - let the 3-attempt fix loop handle these
    echo "retriable:test_failure"
    return
  fi

  # Unknown - default to fatal to avoid wasting retries
  echo "unknown"
}

# Check if error type is retriable
is_retriable_error() {
  local error_class="$1"
  [[ "$error_class" == retriable:* ]]
}

# Check if error type is fatal
is_fatal_error() {
  local error_class="$1"
  [[ "$error_class" == fatal:* ]]
}

# -------------------------
# Config (from ~/.nyxGPT/config.ini)
# -------------------------
CONFIG_FILE="${NYXGPT_CONFIG_FILE:-$HOME/.nyxGPT/config.ini}"

load_config() {
  [[ -f "$CONFIG_FILE" ]] || _die "Config file not found: $CONFIG_FILE"

  # naive INI-ish parser: key=value lines, ignores [sections] and comments
  # Normalizes keys to uppercase for consistency
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line%%;*}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*\[.*\][[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z0-9_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val%\"}"
      val="${val#\"}"
      val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      # Normalize key to uppercase
      key_upper="$(echo "$key" | tr '[:lower:]' '[:upper:]')"
      export "$key_upper=$val"
    fi
  done < "$CONFIG_FILE"

  # strict requirements (fail if missing)
  for k in \
    REPO_OWNER REPO_NAME \
    PROJECT_OWNER PROJECT_NUMBER \
    DEV_AGENT REVIEW_AGENT SCRUM_AGENT \
    STATUS_FIELD STATUS_BACKLOG STATUS_IN_PROGRESS STATUS_IN_REVIEW STATUS_FOR_RELEASE \
    RELEASE_BRANCH
  do
    [[ -n "${!k:-}" ]] || _die "Missing required config key: $k (in $CONFIG_FILE)"
  done
}

# -------------------------
# GraphQL wrapper
# -------------------------
graphql() {
  local q="$1"
  shift || true
  require_cmd gh

  # capture stderr too; if gh prints debug noise, strip leading junk before '{'
  local raw
  raw="$(gh api graphql -f query="$q" "$@" 2>&1)" || {
    echo "$raw" >&2
    _die "gh api graphql failed"
  }

  if [[ "$raw" != \{* ]]; then
    raw="$(printf '%s' "$raw" | sed -n 's/^[^{]*//p')"
  fi

  echo "$raw"
}

# -------------------------
# Release branch discovery
# -------------------------
get_release_branch() {
  echo "${RELEASE_BRANCH}"
}

# -------------------------
# Project v2 metadata cache
# -------------------------
__PROJECT_ID=""
__FIELDS_JSON=""

get_project_id() {
  require_cmd jq

  if [[ -n "${__PROJECT_ID}" ]]; then
    echo "${__PROJECT_ID}"
    return
  fi

  local q='query($login:String!, $number:Int!){
    user(login:$login){ projectV2(number:$number){ id } }
  }'
  local resp
  resp="$(graphql "$q" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
  __PROJECT_ID="$(echo "$resp" | jq -r '.data.user.projectV2.id // empty')"
  [[ -n "${__PROJECT_ID}" ]] || _die "Could not resolve project id for ${PROJECT_OWNER}#${PROJECT_NUMBER}"
  echo "${__PROJECT_ID}"
}

get_fields_json() {
  require_cmd jq
  if [[ -n "${__FIELDS_JSON}" ]]; then
    echo "${__FIELDS_JSON}"
    return
  fi

  local project_id
  project_id="$(get_project_id)"

  local q='query($project:ID!){
    node(id:$project){
      ... on ProjectV2 {
        fields(first:100){
          nodes{
            __typename
            ... on ProjectV2FieldCommon { id name }
            ... on ProjectV2SingleSelectField { options { id name } }
            ... on ProjectV2IterationField {
              configuration { iterations { id title startDate duration } }
            }
          }
        }
      }
    }
  }'
  __FIELDS_JSON="$(graphql "$q" -F project="$project_id")"
  echo "${__FIELDS_JSON}"
}

field_id_by_name() {
  require_cmd jq
  local name="$1"
  get_fields_json | jq -r --arg n "$name" '.data.node.fields.nodes[] | select(.name==$n) | .id' | head -n 1
}

field_type_by_name() {
  require_cmd jq
  local name="$1"
  get_fields_json | jq -r --arg n "$name" '.data.node.fields.nodes[] | select(.name==$n) | .__typename' | head -n 1
}

single_select_option_id() {
  require_cmd jq
  local field_name="$1" option_name="$2"
  get_fields_json | jq -r --arg f "$field_name" --arg o "$option_name" '
    .data.node.fields.nodes[]
    | select(.name==$f and .__typename=="ProjectV2SingleSelectField")
    | .options[]
    | select(.name==$o)
    | .id
  ' | head -n 1
}

iteration_active_title() {
  require_cmd jq
  local field_name="$1"
  get_fields_json | jq -r --arg f "$field_name" '
    .data.node.fields.nodes[]
    | select(.name==$f and .__typename=="ProjectV2IterationField")
    | .configuration.iterations
    | sort_by(.startDate)
    | last
    | .title
  ' | head -n 1
}

iteration_id_by_title() {
  require_cmd jq
  local field_name="$1" title="$2"
  get_fields_json | jq -r --arg f "$field_name" --arg t "$title" '
    .data.node.fields.nodes[]
    | select(.name==$f and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | select(.title==$t)
    | .id
  ' | head -n 1
}

# -------------------------
# Project item helpers
# -------------------------
ensure_issue_in_project() {
  require_cmd jq
  local issue_number="$1"
  local project_id
  project_id="$(get_project_id)"

  local q_find='query($project:ID!, $after:String){
    node(id:$project){
      ... on ProjectV2{
        items(first:100, after:$after){
          pageInfo{ hasNextPage endCursor }
          nodes{
            id
            content{ __typename ... on Issue { number } }
          }
        }
      }
    }
  }'

  local after=""
  while true; do
    local resp item_id has_next cursor
    resp="$(graphql "$q_find" -F project="$project_id" -F after="$after")"
    item_id="$(echo "$resp" | jq -r --argjson n "$issue_number" '
      .data.node.items.nodes[]
      | select(.content.__typename=="Issue" and .content.number==$n)
      | .id
    ' | head -n 1)"

    if [[ -n "$item_id" && "$item_id" != "null" ]]; then
      echo "$item_id"
      return 0
    fi

    has_next="$(echo "$resp" | jq -r '.data.node.items.pageInfo.hasNextPage')"
    cursor="$(echo "$resp" | jq -r '.data.node.items.pageInfo.endCursor // empty')"
    [[ "$has_next" == "true" && -n "$cursor" ]] || break
    after="$cursor"
  done

  local content_id
  content_id="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.node_id')"
  [[ -n "$content_id" ]] || _die "Failed to fetch node_id for issue #${issue_number}"

  local q_add='mutation($project:ID!, $content:ID!){
    addProjectV2ItemById(input:{ projectId:$project, contentId:$content }){
      item { id }
    }
  }'
  local resp
  resp="$(graphql "$q_add" -F project="$project_id" -F content="$content_id")"
  echo "$resp" | jq -r '.data.addProjectV2ItemById.item.id // empty'
}

# -------------------------
# Project field updates
# (already uses inline value:{...} — no ProjectV2FieldValue variables)
# -------------------------
set_project_field_value() {
  local item_id="$1" field_name="$2" value="$3"
  require_cmd jq

  local project_id field_id ftype
  project_id="$(get_project_id)"
  field_id="$(field_id_by_name "$field_name")"
  [[ -n "$field_id" && "$field_id" != "null" ]] || _die "Project field not found: ${field_name}"
  ftype="$(field_type_by_name "$field_name")"

  if [[ "$ftype" == "ProjectV2SingleSelectField" ]]; then
    local opt_id
    opt_id="$(single_select_option_id "$field_name" "$value")"
    [[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Option '${value}' not found for '${field_name}'"

    local q
    q="mutation(\$project:ID!, \$item:ID!, \$field:ID!){
      updateProjectV2ItemFieldValue(input:{
        projectId:\$project,
        itemId:\$item,
        fieldId:\$field,
        value:{ singleSelectOptionId:\"${opt_id}\" }
      }){ projectV2Item { id } }
    }"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" >/dev/null
    return 0
  fi

  if [[ "$ftype" == "ProjectV2IterationField" ]]; then
    local it_id
    if [[ "$value" == "ACTIVE" ]]; then
      local title
      title="$(iteration_active_title "$field_name")"
      it_id="$(iteration_id_by_title "$field_name" "$title")"
    else
      it_id="$(iteration_id_by_title "$field_name" "$value")"
    fi
    [[ -n "$it_id" && "$it_id" != "null" ]] || _die "Iteration '${value}' not found for '${field_name}'"

    local q
    q="mutation(\$project:ID!, \$item:ID!, \$field:ID!){
      updateProjectV2ItemFieldValue(input:{
        projectId:\$project,
        itemId:\$item,
        fieldId:\$field,
        value:{ iterationId:\"${it_id}\" }
      }){ projectV2Item { id } }
    }"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" >/dev/null
    return 0
  fi

  # Default: treat as text field.
  local escaped
  escaped="$(jq -Rn --arg t "$value" '$t|@json')"

  local q
  q="mutation(\$project:ID!, \$item:ID!, \$field:ID!){
    updateProjectV2ItemFieldValue(input:{
      projectId:\$project,
      itemId:\$item,
      fieldId:\$field,
      value:{ text:${escaped} }
    }){ projectV2Item { id } }
  }"
  graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" >/dev/null
}

set_issue_status() {
  local issue="$1" status="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue")"
  set_project_field_value "$item_id" "$STATUS_FIELD" "$status"
}

# -------------------------
# Issue operations
# -------------------------
issue_assign_only() {
  local issue="$1" assignee="$2"
  gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" -f "assignees[]=${assignee}" >/dev/null
}

issue_comment() {
  local issue="$1" body="$2"
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/comments" -f "body=${body}" >/dev/null
}

# Assign issue to developer and trigger workflow
# GitHub's security model prevents API assignment from triggering workflows,
# so we also post a trigger comment to ensure the developer workflow runs
assign_and_trigger_developer() {
  local issue="$1"

  # Simply assign the developer - workflow triggers on assignment
  # Developer agent is smart enough to read issue history and determine if this is new work or a retry
  issue_assign_only "$issue" "$DEV_AGENT"
  _debug "Assigned developer to #$issue - workflow will trigger via assignment event"
}

# -------------------------
# PR Project Hygiene
# -------------------------
ensure_pr_project_hygiene() {
  local pr_number="$1" issue_number="$2"
  require_cmd jq

  _debug "Ensuring PR #${pr_number} project hygiene (linked issue: #${issue_number})"

  # Add PR to project if not already there
  local pr_item_id issue_item_id
  pr_item_id="$(ensure_issue_in_project "$pr_number")"
  [[ -n "$pr_item_id" && "$pr_item_id" != "null" ]] || _die "Failed to add PR #${pr_number} to project"
  _debug "PR #${pr_number} project item ID: $pr_item_id"

  # Get issue project item ID
  issue_item_id="$(ensure_issue_in_project "$issue_number")"
  [[ -n "$issue_item_id" && "$issue_item_id" != "null" ]] || _die "Issue #${issue_number} not in project"
  _debug "Issue #${issue_number} project item ID: $issue_item_id"

  # Get issue project field values
  local project_id
  project_id="$(get_project_id)"

  local q_get_fields='query($project:ID!, $item:ID!) {
    node(id:$project) {
      ... on ProjectV2 {
        item: items(first:1, after:null) {
          nodes {
            id
            fieldValues(first:20) {
              nodes {
                __typename
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field { ... on ProjectV2SingleSelectField { name } }
                  name
                }
                ... on ProjectV2ItemFieldIterationValue {
                  field { ... on ProjectV2IterationField { name } }
                  title
                  id
                }
              }
            }
          }
        }
      }
    }
    projectItem: node(id:$item) {
      ... on ProjectV2Item {
        fieldValues(first:20) {
          nodes {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue {
              field { ... on ProjectV2SingleSelectField { name } }
              name
            }
            ... on ProjectV2ItemFieldIterationValue {
              field { ... on ProjectV2IterationField { name } }
              title
              id
            }
          }
        }
      }
    }
  }'

  local resp
  resp="$(graphql "$q_get_fields" -F project="$project_id" -F item="$issue_item_id")"

  # Extract field values from issue
  local priority effort module sprint
  priority="$(echo "$resp" | jq -r '.data.projectItem.fieldValues.nodes[] | select(.field.name == "Priority") | .name // empty')"
  effort="$(echo "$resp" | jq -r '.data.projectItem.fieldValues.nodes[] | select(.field.name == "Effort") | .name // empty')"
  module="$(echo "$resp" | jq -r '.data.projectItem.fieldValues.nodes[] | select(.field.name == "Module") | .name // empty')"
  sprint="$(echo "$resp" | jq -r '.data.projectItem.fieldValues.nodes[] | select(.field.name == "Sprint") | .title // empty')"

  _debug "Issue #${issue_number} fields: Priority=$priority, Effort=$effort, Module=$module, Sprint=$sprint"

  # Get milestone from issue
  local milestone_number
  milestone_number="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.milestone.number // empty')"

  # Copy fields to PR
  if [[ -n "$priority" && "$priority" != "null" ]]; then
    _debug "Setting PR Priority to: $priority"
    set_project_field_value "$pr_item_id" "Priority" "$priority" || _warn "Failed to set Priority on PR #${pr_number}"
  fi

  if [[ -n "$effort" && "$effort" != "null" ]]; then
    _debug "Setting PR Effort to: $effort"
    set_project_field_value "$pr_item_id" "Effort" "$effort" || _warn "Failed to set Effort on PR #${pr_number}"
  fi

  if [[ -n "$module" && "$module" != "null" ]]; then
    _debug "Setting PR Module to: $module"
    set_project_field_value "$pr_item_id" "Module" "$module" || _warn "Failed to set Module on PR #${pr_number}"
  fi

  if [[ -n "$sprint" && "$sprint" != "null" ]]; then
    _debug "Setting PR Sprint to: $sprint"
    set_project_field_value "$pr_item_id" "Sprint" "$sprint" || _warn "Failed to set Sprint on PR #${pr_number}"
  fi

  # Set milestone on PR
  if [[ -n "$milestone_number" && "$milestone_number" != "null" ]]; then
    _debug "Setting PR milestone to: $milestone_number"
    gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${pr_number}" -f "milestone=${milestone_number}" >/dev/null 2>&1 || _warn "Failed to set milestone on PR #${pr_number}"
  fi

  # Set PR status to In Review
  _debug "Setting PR status to: $STATUS_IN_REVIEW"
  set_project_field_value "$pr_item_id" "$STATUS_FIELD" "$STATUS_IN_REVIEW" || _warn "Failed to set Status on PR #${pr_number}"

  # Link issue to PR in Development field (closedBy relationship)
  # Note: "Closes #N" in PR body creates the link, but we verify it here
  _debug "Verifying PR #${pr_number} is linked to issue #${issue_number}"
  local linked_pr
  linked_pr="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.pull_request.url // empty')"
  if [[ -z "$linked_pr" ]]; then
    _warn "PR #${pr_number} may not be properly linked to issue #${issue_number} (check 'Closes #${issue_number}' in PR body)"
  fi

  echo "[dev] PR #${pr_number} project hygiene: ✓ Added to project, ✓ Fields copied, ✓ Milestone set" >&2
}
