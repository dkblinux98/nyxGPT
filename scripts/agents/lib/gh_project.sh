#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Shared library for myGPT agent scripts
# - SAFE TO SOURCE: no work is executed at import time
# - NO self-sourcing
# ============================================================

# -------------------------
# Logging / error handling
# -------------------------
_debug() { [[ "${DEBUG:-0}" == "1" ]] && echo "[debug] $*" >&2 || true; }

_die() {
  echo "[error] $*" >&2
  # If sourced, DO NOT exit the user's shell
  if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 1
  fi
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || _die "Missing required command: $1"
}

require_gh_auth() {
  require_cmd gh
  # Don't let gh stderr kill JSON parsing; just verify auth
  gh auth status -h github.com >/dev/null 2>&1 || _die "gh is not authenticated. Run: gh auth login"
}

# -------------------------
# Configuration (config.ini)
# -------------------------
CONFIG_FILE="${MYGPT_CONFIG_FILE:-$HOME/.myGPT/config.ini}"

load_config() {
  [[ -f "$CONFIG_FILE" ]] || _die "Config file not found: $CONFIG_FILE"

  while IFS= read -r line; do
    # strip comments
    line="${line%%\#*}"
    line="${line%%\;*}"
    # trim
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^\[.*\]$ ]] && continue

    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      local key="${line%%=*}"
      local val="${line#*=}"
      val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      export "$key=$val"
    fi
  done < "$CONFIG_FILE"

  # defaults if missing
  export REPO_OWNER="${REPO_OWNER:-dkblinux98}"
  export REPO_NAME="${REPO_NAME:-myGPT}"

  export PROJECT_OWNER="${PROJECT_OWNER:-dkblinux98}"
  export PROJECT_NUMBER="${PROJECT_NUMBER:-2}"

  export DEV_AGENT="${DEV_AGENT:-myGPT-developer-agent}"
  export REVIEW_AGENT="${REVIEW_AGENT:-myGPT-review-agent}"
  export SCRUM_AGENT="${SCRUM_AGENT:-myGPT-scrummaster-agent}"
  export HUMAN_OWNER="${HUMAN_OWNER:-dkblinux98}"

  export FIELD_STATUS="${FIELD_STATUS:-Status}"
  export FIELD_PRIORITY="${FIELD_PRIORITY:-Priority}"
  export FIELD_EFFORT="${FIELD_EFFORT:-Effort}"
  export FIELD_MODULE="${FIELD_MODULE:-Module}"
  export FIELD_SPRINT="${FIELD_SPRINT:-Sprint}"

  export STATUS_BACKLOG="${STATUS_BACKLOG:-Backlog}"
  export STATUS_IN_PROGRESS="${STATUS_IN_PROGRESS:-In Progress}"
  export STATUS_IN_REVIEW="${STATUS_IN_REVIEW:-In Review}"
  export STATUS_FOR_RELEASE="${STATUS_FOR_RELEASE:-For Release}"
}

# -------------------------
# GitHub helpers
# -------------------------
graphql() {
  local query="$1"
  shift || true

  require_cmd gh

  # Capture stdout+stderr so we never lose error messages, and so we can strip noise.
  local raw
  raw="$(gh api graphql -f query="$query" "$@" 2>&1)" || {
    echo "$raw" >&2
    _die "gh api graphql failed"
  }

  # Strip anything before the first '{' (e.g., curl trace lines)
  if [[ "$raw" != \{* ]]; then
    raw="{${raw#*\{}"
  fi

  # Validate JSON if jq exists (recommended)
  if command -v jq >/dev/null 2>&1; then
    echo "$raw" | jq -e . >/dev/null 2>&1 || {
      echo "[error] graphql() output was not valid JSON. Head:" >&2
      echo "$raw" | head -c 400 >&2
      _die "Invalid JSON from GitHub GraphQL"
    }
  fi

  _debug "GraphQL: ${query:0:160}..."
  echo "$raw"
}

get_release_branch() {
  # Optional pin:
  if [[ -n "${RELEASE_BRANCH:-}" ]]; then
    echo "$RELEASE_BRANCH"
    return
  fi

  # Try latest release/* branch, else main
  local branches rel
  branches="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/branches?per_page=100" --jq '.[].name' 2>/dev/null || true)"
  rel="$(echo "$branches" | grep -E '^release/' | sort | tail -n 1 || true)"
  if [[ -n "$rel" ]]; then
    echo "$rel"
  else
    echo "main"
  fi
}

# -------------------------
# Project v2 metadata cache
# -------------------------
__PROJECT_ID=""
__FIELDS_JSON=""

get_project_id() {
  [[ -n "$__PROJECT_ID" ]] && { echo "$__PROJECT_ID"; return; }

  # Try user project first
  local q_user='query($login:String!, $number:Int!){
    user(login:$login){ projectV2(number:$number){ id } }
  }'
  local resp
  resp="$(graphql "$q_user" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
  __PROJECT_ID="$(echo "$resp" | jq -r '.data.user.projectV2.id // empty')"

  # Fallback: org project
  if [[ -z "$__PROJECT_ID" ]]; then
    local q_org='query($login:String!, $number:Int!){
      organization(login:$login){ projectV2(number:$number){ id } }
    }'
    resp="$(graphql "$q_org" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
    __PROJECT_ID="$(echo "$resp" | jq -r '.data.organization.projectV2.id // empty')"
  fi

  [[ -n "$__PROJECT_ID" ]] || _die "Unable to locate Project v2 owner(${PROJECT_OWNER}) number(${PROJECT_NUMBER})."
  echo "$__PROJECT_ID"
}

load_fields_json() {
  [[ -n "$__FIELDS_JSON" ]] && { echo "$__FIELDS_JSON"; return; }

  local project_id
  project_id="$(get_project_id)"

  # IMPORTANT: Use only real schema types (no ProjectV2NumberField/TextField/etc.)
  local q='query($project:ID!){
    node(id:$project){
      ... on ProjectV2 {
        fields(first:100){
          nodes{
            __typename
            ... on ProjectV2FieldCommon { id name }
            ... on ProjectV2SingleSelectField { id name options { id name } }
            ... on ProjectV2IterationField { id name configuration { iterations { id title startDate duration } } }
          }
        }
      }
    }
  }'

  __FIELDS_JSON="$(graphql "$q" -F project="$project_id")"
  echo "$__FIELDS_JSON"
}

field_id_by_name() {
  local name="$1"
  load_fields_json | jq -r --arg name "$name" '
    .data.node.fields.nodes[]
    | select(.name==$name)
    | .id' | head -n 1
}

field_type_by_name() {
  local name="$1"
  load_fields_json | jq -r --arg name "$name" '
    .data.node.fields.nodes[]
    | select(.name==$name)
    | .__typename' | head -n 1
}

single_select_option_id() {
  local field_name="$1" option_name="$2"
  load_fields_json | jq -r --arg field "$field_name" --arg opt "$option_name" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2SingleSelectField")
    | .options[]
    | select(.name==$opt)
    | .id' | head -n 1
}

iteration_id_by_title() {
  local field_name="$1" title="$2"
  load_fields_json | jq -r --arg field "$field_name" --arg title "$title" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | select(.title==$title)
    | .id' | head -n 1
}

iteration_active_title() {
  local field_name="$1"
  local today
  today="$(date -u +%Y-%m-%d)"
  load_fields_json | jq -r --arg field "$field_name" --arg today "$today" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | select(.startDate <= $today)
    | .title' | tail -n 1
}

# -------------------------
# Issue + Project item linkage
# -------------------------
issue_node_id() {
  local issue_number="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.node_id'
}

project_item_id_for_issue() {
  local issue_number="$1"
  local project_id
  project_id="$(get_project_id)"
  local iid
  iid="$(issue_node_id "$issue_number")"

  local q='query($issue:ID!){
    node(id:$issue){
      ... on Issue {
        projectItems(first:50){
          nodes{ id project{ id } }
        }
      }
    }
  }'
  local resp
  resp="$(graphql "$q" -F issue="$iid")"

  echo "$resp" | jq -r --arg pid "$project_id" '
    .data.node.projectItems.nodes[]
    | select(.project.id==$pid)
    | .id' | head -n 1
}

ensure_issue_in_project() {
  local issue_number="$1"
  local project_id
  project_id="$(get_project_id)"

  local item_id
  item_id="$(project_item_id_for_issue "$issue_number" || true)"

  if [[ -n "$item_id" && "$item_id" != "null" ]]; then
    echo "$item_id"
    return
  fi

  local content_id
  content_id="$(issue_node_id "$issue_number")"

  local q='mutation($project:ID!, $content:ID!){
    addProjectV2ItemById(input:{projectId:$project, contentId:$content}){
      item { id }
    }
  }'
  local resp
  resp="$(graphql "$q" -F project="$project_id" -F content="$content_id")"

  item_id="$(echo "$resp" | jq -r '.data.addProjectV2ItemById.item.id // empty')"
  [[ -n "$item_id" ]] || _die "Failed to add issue #${issue_number} to project"
  echo "$item_id"
}

# -------------------------
# Project field updates
# -------------------------
set_project_field_value() {
  local item_id="$1" field_name="$2" value="$3"
  local project_id field_id ftype
  project_id="$(get_project_id)"
  field_id="$(field_id_by_name "$field_name")"
  [[ -n "$field_id" && "$field_id" != "null" ]] || _die "Project field not found: ${field_name}"
  ftype="$(field_type_by_name "$field_name")"

  local q='mutation($project:ID!, $item:ID!, $field:ID!, $value:ProjectV2FieldValue!){
    updateProjectV2ItemFieldValue(input:{
      projectId:$project,
      itemId:$item,
      fieldId:$field,
      value:$value
    }){ projectV2Item { id } }
  }'

  if [[ "$ftype" == "ProjectV2SingleSelectField" ]]; then
    local opt_id
    opt_id="$(single_select_option_id "$field_name" "$value")"
    [[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Option '${value}' not found for '${field_name}'"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" \
      -f value="{\"singleSelectOptionId\":\"$opt_id\"}" >/dev/null
    return
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
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" \
      -f value="{\"iterationId\":\"$it_id\"}" >/dev/null
    return
  fi

  # Default to text for other field types (works for your current set)
  graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" \
    -f value="{\"text\":\"$value\"}" >/dev/null
}

set_issue_status() {
  local issue="$1" status="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue")"
  set_project_field_value "$item_id" "$FIELD_STATUS" "$status"
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

create_sub_issue() {
  local parent="$1" title="$2" body_file="$3"
  [[ -f "$body_file" ]] || _die "Body file not found: $body_file"

  # carry over milestone if parent has one
  local milestone_number
  milestone_number="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${parent}" --jq '.milestone.number // empty' || true)"

  local args=(gh issue create --repo "${REPO_OWNER}/${REPO_NAME}" --title "$title" --body-file "$body_file")
  [[ -n "$milestone_number" ]] && args+=(--milestone "$milestone_number")

  "${args[@]}" --json number -q .number
}