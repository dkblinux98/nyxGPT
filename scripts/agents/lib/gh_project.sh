#!/usr/bin/env bash
set -euo pipefail

_debug() { [[ "${DEBUG:-0}" == "1" ]] && echo "[debug] $*" >&2 || true; }
_die() { echo "[error] $*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || _die "Missing required command: $1"; }

require_gh_auth() {
  require_cmd gh
  gh auth status -h github.com >/dev/null 2>&1 || _die "gh is not authenticated. Run: gh auth login"
}

CONFIG_FILE="${MYGPT_CONFIG_FILE:-$HOME/.myGPT/config.ini}"

load_config() {
  [[ -f "$CONFIG_FILE" ]] || _die "Config file not found: $CONFIG_FILE"
  while IFS= read -r line; do
    line="${line%%\#*}"
    line="${line%%\;*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^\[.*\]$ ]] && continue
    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      key="${line%%=*}"
      val="${line#*=}"
      val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      export "$key=$val"
    fi
  done < "$CONFIG_FILE"
}

load_config

REPO_OWNER="${REPO_OWNER:-dkblinux98}"
REPO_NAME="${REPO_NAME:-myGPT}"

PROJECT_OWNER="${PROJECT_OWNER:-dkblinux98}"
PROJECT_NUMBER="${PROJECT_NUMBER:-2}"

DEV_AGENT="${DEV_AGENT:-myGPT-developer-agent}"
REVIEW_AGENT="${REVIEW_AGENT:-myGPT-review-agent}"
SCRUM_AGENT="${SCRUM_AGENT:-myGPT-scrummaster-agent}"
HUMAN_OWNER="${HUMAN_OWNER:-dkblinux98}"

FIELD_STATUS="${FIELD_STATUS:-Status}"
FIELD_PRIORITY="${FIELD_PRIORITY:-Priority}"
FIELD_EFFORT="${FIELD_EFFORT:-Effort}"
FIELD_MODULE="${FIELD_MODULE:-Module}"
FIELD_SPRINT="${FIELD_SPRINT:-Sprint}"

STATUS_BACKLOG="${STATUS_BACKLOG:-Backlog}"
STATUS_IN_PROGRESS="${STATUS_IN_PROGRESS:-In Progress}"
STATUS_IN_REVIEW="${STATUS_IN_REVIEW:-In Review}"
STATUS_FOR_RELEASE="${STATUS_FOR_RELEASE:-For Release}"

__PROJECT_ID=""
__FIELDS_JSON=""

graphql() {
  local query="$1"
  shift || true
  _debug "GraphQL: ${query:0:160}..."
  gh api graphql -f query="$query" "$@"
}

get_release_branch() {
  if [[ -n "${RELEASE_BRANCH:-}" ]]; then
    echo "$RELEASE_BRANCH"; return
  fi
  local branches
  branches="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/branches?per_page=100" --jq '.[].name' 2>/dev/null || true)"
  local rel
  rel="$(echo "$branches" | grep -E '^release/' | sort | tail -n 1 || true)"
  if [[ -n "$rel" ]]; then echo "$rel"; else echo "main"; fi
}

get_project_id() {
  [[ -n "$__PROJECT_ID" ]] && { echo "$__PROJECT_ID"; return; }
  local q='query($login:String!, $number:Int!){
    user(login:$login){ projectV2(number:$number){ id } }
  }'
  local resp
  resp="$(graphql "$q" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
  __PROJECT_ID="$(echo "$resp" | jq -r '.data.user.projectV2.id')"
  [[ "$__PROJECT_ID" != "null" && -n "$__PROJECT_ID" ]] || _die "Unable to locate Project v2 user(${PROJECT_OWNER}) number(${PROJECT_NUMBER})."
  echo "$__PROJECT_ID"
}

load_fields_json() {
  [[ -n "$__FIELDS_JSON" ]] && { echo "$__FIELDS_JSON"; return; }
  local project_id; project_id="$(get_project_id)"
  local q='query($project:ID!){
    node(id:$project){
      ... on ProjectV2 {
        fields(first:100){
          nodes{
            __typename
            ... on ProjectV2FieldCommon { id name }
            ... on ProjectV2SingleSelectField { id name options { id name } }
            ... on ProjectV2IterationField { id name configuration { iterations { id title startDate duration } } }
            ... on ProjectV2Field { id name }
          }
        }
      }
    }
  }'
  __FIELDS_JSON="$(graphql "$q" -F project="$project_id")"
  echo "$__FIELDS_JSON"
}

field_id_by_name() { local name="$1"; load_fields_json | jq -r --arg name "$name" '.data.node.fields.nodes[] | select(.name==$name) | .id' | head -n 1; }
field_type_by_name() { local name="$1"; load_fields_json | jq -r --arg name "$name" '.data.node.fields.nodes[] | select(.name==$name) | .__typename' | head -n 1; }

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
  local today; today="$(date -u +%Y-%m-%d)"
  load_fields_json | jq -r --arg field "$field_name" --arg today "$today" '
    .data.node.fields.nodes[]
    | select(.name==$field and .__typename=="ProjectV2IterationField")
    | .configuration.iterations[]
    | select(.startDate <= $today)
    | .title' | tail -n 1
}

issue_node_id() { local issue_number="$1"; gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.node_id'; }

project_item_id_for_issue() {
  local issue_number="$1"
  local project_id; project_id="$(get_project_id)"
  local iid; iid="$(issue_node_id "$issue_number")"
  local q='query($issue:ID!){
    node(id:$issue){
      ... on Issue { projectItems(first:50){ nodes{ id project{ id } } } }
    }
  }'
  local resp; resp="$(graphql "$q" -F issue="$iid")"
  echo "$resp" | jq -r --arg pid "$project_id" '.data.node.projectItems.nodes[] | select(.project.id==$pid) | .id' | head -n 1
}

ensure_issue_in_project() {
  local issue_number="$1"
  local project_id; project_id="$(get_project_id)"
  local item_id; item_id="$(project_item_id_for_issue "$issue_number" || true)"
  if [[ -n "$item_id" && "$item_id" != "null" ]]; then echo "$item_id"; return; fi
  local content_id; content_id="$(issue_node_id "$issue_number")"
  local q='mutation($project:ID!, $content:ID!){
    addProjectV2ItemById(input:{projectId:$project, contentId:$content}){ item { id } }
  }'
  local resp; resp="$(graphql "$q" -F project="$project_id" -F content="$content_id")"
  item_id="$(echo "$resp" | jq -r '.data.addProjectV2ItemById.item.id')"
  [[ "$item_id" != "null" && -n "$item_id" ]] || _die "Failed to add issue #${issue_number} to project"
  echo "$item_id"
}

set_project_field_value() {
  local item_id="$1" field_name="$2" value="$3"
  local project_id field_id ftype
  project_id="$(get_project_id)"
  field_id="$(field_id_by_name "$field_name")"
  [[ -n "$field_id" && "$field_id" != "null" ]] || _die "Project field not found: ${field_name}"
  ftype="$(field_type_by_name "$field_name")"

  local q='mutation($project:ID!, $item:ID!, $field:ID!, $value:ProjectV2FieldValue!){
    updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,value:$value}){ projectV2Item { id } }
  }'

  if [[ "$ftype" == "ProjectV2SingleSelectField" ]]; then
    local opt_id; opt_id="$(single_select_option_id "$field_name" "$value")"
    [[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Option '${value}' not found for '${field_name}'"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"singleSelectOptionId\":\"$opt_id\"}" >/dev/null
    return
  fi

  if [[ "$ftype" == "ProjectV2IterationField" ]]; then
    local it_id
    if [[ "$value" == "ACTIVE" ]]; then
      local title; title="$(iteration_active_title "$field_name")"
      it_id="$(iteration_id_by_title "$field_name" "$title")"
    else
      it_id="$(iteration_id_by_title "$field_name" "$value")"
    fi
    [[ -n "$it_id" && "$it_id" != "null" ]] || _die "Iteration '${value}' not found for '${field_name}'"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"iterationId\":\"$it_id\"}" >/dev/null
    return
  fi

  local lname; lname="$(echo "$field_name" | tr '[:upper:]' '[:lower:]')"
  if [[ "$field_name" == "$FIELD_EFFORT" || "$lname" =~ effort|points|size ]]; then
    [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || _die "Non-numeric value for '${field_name}': ${value}"
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"number\":$value}" >/dev/null
    return
  fi

  if [[ "$lname" =~ date ]]; then
    graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"date\":\"$value\"}" >/dev/null
    return
  fi

  graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" -f value="{\"text\":\"$value\"}" >/dev/null
}

set_issue_status() { local issue="$1" status="$2"; set_project_field_value "$(ensure_issue_in_project "$issue")" "$FIELD_STATUS" "$status"; }

issue_assign_only() {
  local issue="$1" assignee="$2"
  gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" -f "assignees[]=${assignee}" >/dev/null
}

issue_comment() { local issue="$1" body="$2"; gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/comments" -f "body=${body}" >/dev/null; }

create_sub_issue() {
  local parent="$1" title="$2" body_file="$3"
  [[ -f "$body_file" ]] || _die "Body file not found: $body_file"
  local milestone_number
  milestone_number="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${parent}" --jq '.milestone.number // empty' || true)"
  local args=(gh issue create --repo "${REPO_OWNER}/${REPO_NAME}" --title "$title" --body-file "$body_file")
  [[ -n "$milestone_number" ]] && args+=(--milestone "$milestone_number")
  "${args[@]}" --json number -q .number
}
