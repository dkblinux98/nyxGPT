#!/usr/bin/env bash
set -euo pipefail

# Common library for GitHub Project v2 field updates (GraphQL) and gh helpers.
# Requires: gh CLI authenticated; token scopes: repo, project

_debug() {
  if [[ "${DEBUG:-0}" == "1" ]]; then
    echo "[debug] $*" >&2
  fi
}

_die() {
  echo "[error] $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || _die "Missing required command: $1"
}

require_gh_auth() {
  require_cmd gh
  gh auth status -h github.com >/dev/null 2>&1 || _die "gh is not authenticated. Run: gh auth login"
}

# Defaults
REPO_OWNER="${REPO_OWNER:-dkblinux98}"
REPO_NAME="${REPO_NAME:-myGPT}"
PROJECT_OWNER="${PROJECT_OWNER:-dkblinux98}"
PROJECT_NUMBER="${PROJECT_NUMBER:-2}"

DEV_AGENT="${DEV_AGENT:-mygpt-developer-agent}"
REVIEW_AGENT="${REVIEW_AGENT:-mygpt-review-agent}"
SCRUM_AGENT="${SCRUM_AGENT:-mygpt-scrummaster-agent}"
HUMAN_OWNER="${HUMAN_OWNER:-dkblinux98}"

FIELD_STATUS="${FIELD_STATUS:-Status}"
FIELD_PRIORITY="${FIELD_PRIORITY:-Priority}"
FIELD_EFFORT="${FIELD_EFFORT:-Effort}"
FIELD_MODULE="${FIELD_MODULE:-Module}"
FIELD_SPRINT="${FIELD_SPRINT:-Sprint}"
FIELD_PHASE="${FIELD_PHASE:-Phase}"

STATUS_BACKLOG="${STATUS_BACKLOG:-Backlog}"
STATUS_IN_PROGRESS="${STATUS_IN_PROGRESS:-In Progress}"
STATUS_IN_REVIEW="${STATUS_IN_REVIEW:-In Review}"
STATUS_FOR_RELEASE="${STATUS_FOR_RELEASE:-For Release}"

# Caches
__PROJECT_ID=""
__FIELDS_JSON=""

graphql() {
  local query="$1"
  shift || true
  _debug "GraphQL query: ${query:0:200}..."
  # Use gh api graphql. Additional -f variables can be passed.
  gh api graphql -f query="$query" "$@"
}

get_release_branch() {
  # Prefer explicit RELEASE_BRANCH, otherwise pick newest release/* by commit date.
  if [[ -n "${RELEASE_BRANCH:-}" ]]; then
    echo "$RELEASE_BRANCH"
    return
  fi
  # Try to find latest release/* branch.
  local branches
  branches="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/branches?per_page=100" --jq '.[].name' 2>/dev/null || true)"
  if [[ -z "$branches" ]]; then
    echo "main"
    return
  fi
  local release_branches
  release_branches="$(echo "$branches" | grep -E '^release/' || true)"
  if [[ -z "$release_branches" ]]; then
    echo "main"
    return
  fi
  # Pick lexicographically last as a decent heuristic.
  echo "$release_branches" | sort | tail -n 1
}

get_project_id() {
  if [[ -n "$__PROJECT_ID" ]]; then
    echo "$__PROJECT_ID"
    return
  fi
  local q
  q='query($login:String!, $number:Int!){
    user(login:$login){
      projectV2(number:$number){ id }
    }
  }'
  local resp
  resp="$(graphql "$q" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
  __PROJECT_ID="$(echo "$resp" | jq -r '.data.user.projectV2.id')"
  [[ "$__PROJECT_ID" != "null" && -n "$__PROJECT_ID" ]] || _die "Unable to locate Project v2 user(${PROJECT_OWNER}) number(${PROJECT_NUMBER})."
  echo "$__PROJECT_ID"
}

load_fields_json() {
  if [[ -n "$__FIELDS_JSON" ]]; then
    echo "$__FIELDS_JSON"
    return
  fi
  local project_id
  project_id="$(get_project_id)"
  local q
  q='query($project:ID!){
    node(id:$project){
      ... on ProjectV2 {
        fields(first:100){
          nodes{
            __typename
            ... on ProjectV2FieldCommon { id name }
            ... on ProjectV2SingleSelectField { id name options { id name } }
            ... on ProjectV2IterationField { id name configuration { iterations { id title startDate duration } } }
            ... on ProjectV2NumberField { id name }
            ... on ProjectV2TextField { id name }
            ... on ProjectV2DateField { id name }
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
  local fj
  fj="$(load_fields_json)"
  echo "$fj" | jq -r --arg name "$name" '.data.node.fields.nodes[] | select(.name==$name) | .id' | head -n 1
}

field_type_by_name() {
  local name="$1"
  local fj
  fj="$(load_fields_json)"
  echo "$fj" | jq -r --arg name "$name" '.data.node.fields.nodes[] | select(.name==$name) | .__typename' | head -n 1
}

single_select_option_id() {
  local field_name="$1"
  local option_name="$2"
  local fj
  fj="$(load_fields_json)"
  echo "$fj" | jq -r --arg field "$field_name" --arg opt "$option_name" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2SingleSelectField")
    | .options[]
    | select(.name==$opt)
    | .id' | head -n 1
}

iteration_id_by_title() {
  local field_name="$1"
  local title="$2"
  local fj
  fj="$(load_fields_json)"
  echo "$fj" | jq -r --arg field "$field_name" --arg title "$title" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | select(.title==$title)
    | .id' | head -n 1
}

iteration_active_title() {
  # best-effort: find iteration where today is within [startDate, startDate+duration days)
  local field_name="$1"
  local fj today
  fj="$(load_fields_json)"
  today="$(date -u +%Y-%m-%d)"
  echo "$fj" | jq -r --arg field "$field_name" --arg today "$today" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | . as $it
    | ($it.startDate) as $sd
    | ($it.duration|tonumber) as $dur
    | ($sd + "T00:00:00Z") as $sdz
    | ($sd) as $sd_date
    | select($sd_date <= $today)  # coarse filter
    | $it.title' | tail -n 1
}

# Project item helpers
issue_node_id() {
  local issue_number="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.node_id'
}

issue_url() {
  local issue_number="$1"
  echo "https://github.com/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}"
}

project_item_id_for_issue() {
  local issue_number="$1"
  local project_id
  project_id="$(get_project_id)"
  local iid
  iid="$(issue_node_id "$issue_number")"
  local q
  q='query($issue:ID!){
    node(id:$issue){
      ... on Issue {
        projectItems(first:50){
          nodes{
            id
            project{ id }
          }
        }
      }
    }
  }'
  local resp
  resp="$(graphql "$q" -F issue="$iid")"
  echo "$resp" | jq -r --arg pid "$project_id" '.data.node.projectItems.nodes[] | select(.project.id==$pid) | .id' | head -n 1
}

ensure_issue_in_project() {
  local issue_number="$1"
  local project_id item_id
  project_id="$(get_project_id)"
  item_id="$(project_item_id_for_issue "$issue_number" || true)"
  if [[ -n "$item_id" && "$item_id" != "null" ]]; then
    echo "$item_id"
    return
  fi
  # Add item to project
  local content_id
  content_id="$(issue_node_id "$issue_number")"
  local q
  q='mutation($project:ID!, $content:ID!){
    addProjectV2ItemById(input:{projectId:$project, contentId:$content}){ item { id } }
  }'
  local resp
  resp="$(graphql "$q" -F project="$project_id" -F content="$content_id")"
  item_id="$(echo "$resp" | jq -r '.data.addProjectV2ItemById.item.id')"
  [[ "$item_id" != "null" && -n "$item_id" ]] || _die "Failed to add issue #${issue_number} to project"
  echo "$item_id"
}

set_project_field_value() {
  local item_id="$1"
  local field_name="$2"
  local value="$3"

  local project_id field_id ftype
  project_id="$(get_project_id)"
  field_id="$(field_id_by_name "$field_name")"
  [[ -n "$field_id" && "$field_id" != "null" ]] || _die "Project field not found: ${field_name}"
  ftype="$(field_type_by_name "$field_name")"

  local q resp
  q='mutation($project:ID!, $item:ID!, $field:ID!, $value:ProjectV2FieldValue!){
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
    [[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Option '${value}' not found for single-select field '${field_name}'"
    resp="$(graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"singleSelectOptionId\":\"$opt_id\"}")"
    _debug "$resp"
    return
  fi

  if [[ "$ftype" == "ProjectV2IterationField" ]]; then
    local it_id
    it_id="$(iteration_id_by_title "$field_name" "$value")"
    if [[ -z "$it_id" || "$it_id" == "null" ]]; then
      # Try active iteration if value is "ACTIVE"
      if [[ "$value" == "ACTIVE" ]]; then
        local title
        title="$(iteration_active_title "$field_name")"
        [[ -n "$title" && "$title" != "null" ]] || _die "Unable to determine ACTIVE iteration title for ${field_name}"
        it_id="$(iteration_id_by_title "$field_name" "$title")"
      fi
    fi
    [[ -n "$it_id" && "$it_id" != "null" ]] || _die "Iteration '${value}' not found for field '${field_name}'"
    resp="$(graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"iterationId\":\"$it_id\"}")"
    _debug "$resp"
    return
  fi

  if [[ "$ftype" == "ProjectV2NumberField" ]]; then
    [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || _die "Non-numeric value for number field '${field_name}': ${value}"
    resp="$(graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"number\":$value}")"
    _debug "$resp"
    return
  fi

  if [[ "$ftype" == "ProjectV2TextField" ]]; then
    resp="$(graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"text\":\"$value\"}")"
    _debug "$resp"
    return
  fi

  if [[ "$ftype" == "ProjectV2DateField" ]]; then
    resp="$(graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"date\":\"$value\"}")"
    _debug "$resp"
    return
  fi

  _die "Unsupported field type for '${field_name}': ${ftype}"
}

set_issue_status() {
  local issue_number="$1"
  local status="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue_number")"
  set_project_field_value "$item_id" "$FIELD_STATUS" "$status"
}

set_issue_priority() {
  local issue_number="$1"
  local value="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue_number")"
  set_project_field_value "$item_id" "$FIELD_PRIORITY" "$value"
}

set_issue_effort() {
  local issue_number="$1"
  local value="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue_number")"
  set_project_field_value "$item_id" "$FIELD_EFFORT" "$value"
}

set_issue_module() {
  local issue_number="$1"
  local value="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue_number")"
  set_project_field_value "$item_id" "$FIELD_MODULE" "$value"
}

set_issue_sprint() {
  local issue_number="$1"
  local value="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue_number")"
  set_project_field_value "$item_id" "$FIELD_SPRINT" "$value"
}

issue_assign() {
  local issue_number="$1"
  local assignee="$2"
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}/assignees" \
    -f "assignees[]=${assignee}" >/dev/null
}

issue_unassign_all() {
  local issue_number="$1"
  # remove all assignees
  local assignees
  assignees="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.assignees[].login' || true)"
  if [[ -z "$assignees" ]]; then return; fi
  while read -r a; do
    [[ -z "$a" ]] && continue
    gh api -X DELETE "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}/assignees" -f "assignees[]=${a}" >/dev/null || true
  done <<< "$assignees"
}

issue_comment() {
  local issue_number="$1"
  local body="$2"
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}/comments" -f "body=${body}" >/dev/null
}

create_sub_issue() {
  local parent_issue_number="$1"
  local title="$2"
  local body_file="$3"
  [[ -f "$body_file" ]] || _die "Body file not found: $body_file"

  # Inherit milestone + labels except "Acceptance Failure" will be added by caller
  local milestone_number
  milestone_number="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${parent_issue_number}" --jq '.milestone.number // empty' || true)"

  local args=(gh issue create --repo "${REPO_OWNER}/${REPO_NAME}" --title "$title" --body-file "$body_file")
  if [[ -n "$milestone_number" ]]; then
    args+=(--milestone "$milestone_number")
  fi

  local out
  out="$("${args[@]}" --json number -q .number)"
  echo "$out"
}

link_sub_issue() {
  # Best-effort: GitHub sub-issues is not always available via gh for all repos.
  # We'll just comment backlinks if GraphQL is not available.
  local parent="$1"
  local child="$2"
  # Add backlink comment on both
  issue_comment "$parent" "Created sub-issue #${child}."
  issue_comment "$child" "Sub-issue of #${parent}."
}

