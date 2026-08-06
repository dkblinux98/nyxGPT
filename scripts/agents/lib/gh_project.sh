#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Shared library for nyxGPT agent scripts
# - SAFE TO SOURCE: no work is executed at import time
# - NO self-sourcing
# ============================================================

# Directory this file lives in, resolved at source time (not inside a
# function -- BASH_SOURCE[0] there would resolve to gh_project.sh itself,
# which is what we want, but computing it once up front keeps every caller
# consistent). Used to locate the sibling python helpers (sprint_calc.py,
# summarize_backlog_page.py, #3480).
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
# Usage-limit signature detection (self-heal)
# -------------------------
# Shared by developer_auto_implement.yml (Phase 0 + early-cutoff) and
# claude-code-review.yml. Counts Claude action steps from a
# `gh api .../actions/runs/<id>/jobs` payload that finished suspiciously
# fast (the "every model call failed instantly" usage-limit signature).
#
# Skipped steps (never actually ran) and the auto-generated "Post <name>"
# cleanup steps both report near-zero durations and must never be counted —
# doing so made the detector fire on every failure, not just genuine
# usage-limit hits (#3360).
#
# Args:
#   $1 jobs_json       - JSON from `gh api .../actions/runs/<id>/jobs`
#   $2 name_pattern     - case-insensitive regex matching the Claude step name(s)
#   $3 require_failure  - "true": only count steps whose conclusion is
#                         "failure" (use on failure()-gated paths, so a
#                         non-Claude step failing elsewhere in the job is
#                         never miscounted as a Claude usage-limit hit);
#                         "false": count any non-skipped match (use on the
#                         early-cutoff path, which runs after overall job
#                         success)
count_fast_claude_steps() {
  local jobs_json="$1"
  local name_pattern="$2"
  local require_failure="${3:-false}"
  echo "$jobs_json" | jq -r --arg pat "$name_pattern" --arg rf "$require_failure" '
    [.jobs[].steps[]?
     | select(.name | test($pat; "i"))
     | select(.name | test("^Post "; "i") | not)
     | select(if $rf == "true" then .conclusion == "failure" else .conclusion != "skipped" end)
     | select(.started_at != null and .completed_at != null)
     | ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601))
     | select(. >= 0 and . < 90)
    ] | length' 2>/dev/null || echo 0
}

# -------------------------
# Workflow-control labels
# -------------------------
# Labels the self-heal automation adds/removes to track its own retry state
# (e.g. "usage-limit-retry"). These are not "the issue's label" and must
# never count toward one-label-invariant checks like PR title/prefix
# generation — doing so let a stray usage-limit-retry label permanently
# deadlock PR submission (#3360).
WORKFLOW_CONTROL_LABELS_JSON='["usage-limit-retry"]'

# Given a `gh issue/pr view --json labels` `.labels` array (as compact JSON),
# prints the names of the "real" (non-workflow-control) labels, one per line.
real_label_names() {
  local labels_json="$1"
  echo "$labels_json" | jq -r --argjson ctrl "$WORKFLOW_CONTROL_LABELS_JSON" '
    .[].name | select(. as $n | ($ctrl | index($n)) | not)
  '
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

  # Post-merge/acceptance handoff status (owner-created Project option,
  # 2026-07-31). Optional config key with a literal default so existing
  # environments need no new repo variable; override via config if the
  # option is ever renamed.
  STATUS_ACCEPTANCE_TESTING="${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}"

  # Timezone in which sprint-date boundaries are evaluated (owner rule,
  # 2026-07-31: "midnight is midnight EDT, not UTC"). Iteration start dates
  # are timezone-less, so every "has this sprint started/ended?" comparison
  # must compute *today* in the owner's timezone -- with UTC, sprints
  # flipped at 8pm Eastern. America/New_York tracks EST/EDT automatically.
  # Optional config key; override via repo config if the owner relocates.
  SPRINT_TIMEZONE="${SPRINT_TIMEZONE:-America/New_York}"
}

# Today's date (YYYY-MM-DD) in the sprint timezone -- use this, never
# `date -u`, for any comparison against Sprint iteration start/end dates.
sprint_today() {
  TZ="${SPRINT_TIMEZONE:-America/New_York}" date +%Y-%m-%d
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
  # "Active" = the latest iteration that has already STARTED. The iterations
  # list includes future iterations too, so a bare `sort_by | last` returns
  # the furthest-future sprint the moment the owner schedules it ahead of
  # time (2026-07-31: adding Sprint 7/8 made "active" jump to Sprint 8 while
  # Sprint 6 was still running). Filter to startDate <= today first.
  get_fields_json | jq -r --arg f "$field_name" --arg today "$(sprint_today)" '
    .data.node.fields.nodes[]
    | select(.name==$f and .__typename=="ProjectV2IterationField")
    | .configuration.iterations
    | map(select(.startDate <= $today))
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

# Clears (unsets) a project field on an item -- used by
# scrummaster_sprint_reorg_apply.sh to move an issue out of a Sprint
# iteration field. There is no "unset" case in set_project_field_value
# because every existing caller always sets a concrete value (#3480).
clear_project_field_value() {
  local item_id="$1" field_name="$2"
  require_cmd jq

  local project_id field_id
  project_id="$(get_project_id)"
  field_id="$(field_id_by_name "$field_name")"
  [[ -n "$field_id" && "$field_id" != "null" ]] || _die "Project field not found: ${field_name}"

  local q
  q="mutation(\$project:ID!, \$item:ID!, \$field:ID!){
    clearProjectV2ItemFieldValue(input:{
      projectId:\$project,
      itemId:\$item,
      fieldId:\$field
    }){ projectV2Item { id } }
  }"
  graphql "$q" -F project="$project_id" -F item="$item_id" -F field="$field_id" >/dev/null
}

# -------------------------
# Sprint autopilot (#3480)
# -------------------------
# Shared page query for scrummaster_next_issue.sh's --sprint-scoped guard and
# count_sprint_backlog_open() below: fetches Status + Sprint iteration field
# values alongside issue number/state/milestone, one project-items page at a
# time. Kept in one place so both callers can't drift out of sync.
BACKLOG_PAGE_QUERY='query($project:ID!, $after:String){
  node(id:$project){
    ... on ProjectV2{
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes{
          content{
            __typename
            ... on Issue { number state milestone { title } }
          }
          fieldValues(first:50){
            nodes{
              __typename
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
    }
  }
}'

# Counts OPEN issues with Status=Backlog whose Sprint iteration field equals
# `sprint_title`. This is the sprint-autopilot stop condition:
# review_accept_and_merge.sh posts READY_FOR_NEXT_ISSUE while this is > 0,
# and posts a "sprint complete" note (no kick) once it hits 0.
count_sprint_backlog_open() {
  local sprint_field="$1" sprint_title="$2"
  require_cmd jq
  require_cmd python3

  local project_id cursor="" total=0 tmp has_next next_cursor page_count
  project_id="$(get_project_id)"
  tmp="$(mktemp)"

  local max_pages="${MAX_PAGES:-200}"
  local page
  for ((page = 1; page <= max_pages; page++)); do
    if [[ -n "$cursor" ]]; then
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -F after="$cursor" >"$tmp"
    else
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" >"$tmp"
    fi

    page_count="$(STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
      SPRINT_FIELD="$sprint_field" SPRINT_SCOPED=1 ACTIVE_SPRINT_TITLE="$sprint_title" \
      python3 "${_LIB_DIR}/summarize_backlog_page.py" "$tmp" | jq -r '.backlog_open')"
    total=$((total + page_count))

    has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
    next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
    [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
    cursor="$next_cursor"
  done

  rm -f "$tmp"
  echo "$total"
}

# Release wall helpers (owner decision 2026-07-31). The release tracking
# issue's title carries the release version ("Release v2.0.0"), and the
# owner's milestone naming carries it too ("Phase 5.5: ... (v2.0.0)",
# "Phase 6 — ... (v3.0.0)"). Matching the two is the version boundary the
# autopilot must never cross on its own: sprint dates drift and sprints can
# straddle a release, but milestone membership only changes when the owner
# changes it. The gate opens via the release ceremony -- pointing
# RELEASE_ISSUE_NUMBER (and RELEASE_BRANCH) at the next release -- with no
# separate on/off switch to remember.

# Prints the vX.Y.Z version parsed from the release tracking issue's title,
# or nothing if the issue/title has no version.
release_version_from_issue() {
  local release_issue="$1"
  gh issue view "$release_issue" --repo "${REPO_OWNER}/${REPO_NAME}" --json title \
    --jq '.title' 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

# Counts open Backlog issues whose milestone title contains the given
# release version -- the autopilot's continue/park decision input. Same
# paging as count_sprint_backlog_open, release-filtered instead of
# sprint-filtered (an in-release issue in ANY sprint keeps the loop alive).
count_release_backlog_open() {
  local release_version="$1"
  require_cmd jq
  require_cmd python3

  local project_id cursor="" total=0 tmp has_next next_cursor page_count
  project_id="$(get_project_id)"
  tmp="$(mktemp)"

  local max_pages="${MAX_PAGES:-200}"
  local page
  for ((page = 1; page <= max_pages; page++)); do
    if [[ -n "$cursor" ]]; then
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -F after="$cursor" >"$tmp"
    else
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" >"$tmp"
    fi

    page_count="$(STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
      RELEASE_VERSION="$release_version" SPRINT_SCOPED=0 \
      RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
      python3 "${_LIB_DIR}/summarize_backlog_page.py" "$tmp" | jq -r '.backlog_open')"
    total=$((total + page_count))

    has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
    next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
    [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
    cursor="$next_cursor"
  done

  rm -f "$tmp"
  echo "$total"
}

# True if the most recent PAUSE_SPRINT/RESUME_SPRINT control comment on
# `release_issue` is a PAUSE_SPRINT -- the sprint-autopilot kill switch
# (#3480). No comment of either kind means "not paused" (default-on once
# SPRINT_AUTOPILOT itself is enabled).
sprint_autopilot_paused() {
  local release_issue="$1"
  require_cmd jq

  local last_state
  last_state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${release_issue}/comments" --paginate \
    --jq '[.[] | select(.body | test("^\\s*(PAUSE_SPRINT|RESUME_SPRINT)\\s*$"))] | sort_by(.created_at) | last | .body // empty' \
    2>/dev/null | tr -d '[:space:]')"
  [[ "$last_state" == "PAUSE_SPRINT" ]]
}

# -------------------------
# Issue operations
# -------------------------
issue_assign_only() {
  local issue="$1" assignee="$2"
  gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" -f "assignees[]=${assignee}" >/dev/null
}

# Reads the current assignee logins for `issue` as a sorted, comma-joined
# string. Split out from assign_issue_verified so tests can stub it without
# touching `gh`.
_issue_assignee_logins() {
  local issue="$1"
  gh issue view "$issue" --repo "${REPO_OWNER}/${REPO_NAME}" --json assignees \
    --jq '[.assignees[].login] | sort | join(",")'
}

# Replaces the assignee list on `issue` with exactly `assignee`, verifying
# the write actually landed and retrying transient failures.
#
# issue_assign_only's raw PATCH call can silently no-op (rate limit, token
# hiccup, transient 5xx) while still returning success, or fail outright —
# either way every existing caller treated that as a soft `_warn` and moved
# on. That let the accept-and-merge critical path (and the 3-cycle
# escalation path) leave a closed issue assigned to the review agent
# instead of HUMAN_OWNER with no loud signal anywhere (#3332). This mirrors
# set_field_with_retry below, added for the same reason on project fields.
assign_issue_verified() {
  local issue="$1" assignee="$2" attempts="${3:-3}"
  local i actual
  for ((i = 1; i <= attempts; i++)); do
    if issue_assign_only "$issue" "$assignee"; then
      actual="$(_issue_assignee_logins "$issue" 2>/dev/null || echo "")"
      if [[ "$actual" == "$assignee" ]]; then
        return 0
      fi
      _warn "Assignee verification mismatch for issue #${issue} (attempt ${i}/${attempts}): expected '${assignee}', got '${actual:-<empty>}'"
    else
      _warn "issue_assign_only failed for issue #${issue} (attempt ${i}/${attempts})"
    fi
    [[ "$i" -lt "$attempts" ]] && sleep $((2 * i))
  done
  echo "::error::Failed to verify issue #${issue} is assigned to @${assignee} after ${attempts} attempts — it may still show a stale assignee. Manual fix: gh issue edit ${issue} --add-assignee ${assignee}" >&2
  return 1
}

issue_comment() {
  local issue="$1" body="$2"
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/comments" -f "body=${body}" >/dev/null
}

# Assign issue to developer and trigger workflow
# Forces a new assignment event even if developer already assigned
# by unassigning first, then reassigning
assign_and_trigger_developer() {
  local issue="$1"

  # Check if developer is already assigned
  local current_assignee
  current_assignee=$(gh issue view "$issue" --json assignees --jq '.assignees[].login' | grep -x "$DEV_AGENT" || echo "")

  if [[ -n "$current_assignee" ]]; then
    # Developer already assigned - unassign first to force new assignment event
    # This ensures workflow triggers even on retry/reopen scenarios
    _debug "Developer already assigned to #$issue - unassigning first to force event"
    gh issue edit "$issue" --remove-assignee "$DEV_AGENT"
    sleep 1  # Brief pause to ensure event processes
  fi

  # Assign developer - this will trigger workflow via 'issues.assigned' event
  # Developer agent reads issue history to determine if this is new work or a retry
  issue_assign_only "$issue" "$DEV_AGENT"
  _debug "Assigned developer to #$issue - workflow will trigger via assignment event"
}

# -------------------------
# Branch hygiene helpers (shared by developer_create_branch.sh and
# reconcile_dead_branches.sh, #3392)
# -------------------------

# Prints the headRefName of every OPEN pull request, one per line. These are
# always protected from any branch-deletion sweep.
open_pr_head_branches() {
  gh pr list --repo "${REPO_OWNER}/${REPO_NAME}" --state open \
    --json headRefName --limit 200 --jq '.[].headRefName'
}

# Best-effort remote branch delete: never fails the caller, since branch
# cleanup is always a secondary/optional step relative to whatever primary
# operation (branch creation, merge, sweep) triggered it.
delete_remote_branch() {
  local branch="$1"
  git push origin --delete "$branch" >/dev/null 2>&1 \
    || gh api -X DELETE "repos/${REPO_OWNER}/${REPO_NAME}/git/refs/heads/${branch}" >/dev/null 2>&1 \
    || _warn "Could not delete remote branch ${branch} (may already be gone)."
}

# Parses the issue number out of an agent-managed branch name
# (claude/issue-<n>-* or (feat|fix|chore)/<n>-*). Prints "" for anything
# else (e.g. a hand-created branch, master, main).
extract_issue_number() {
  local b="$1"
  if [[ "$b" =~ ^claude/issue-([0-9]+)- ]]; then
    echo "${BASH_REMATCH[1]}"; return 0
  fi
  if [[ "$b" =~ ^(feat|fix|chore)/([0-9]+)- ]]; then
    echo "${BASH_REMATCH[2]}"; return 0
  fi
  echo ""
}

# True if `branch` is the head of a PR that was closed without merging into
# `base_branch` — an explicit abandonment signal, safe to act on immediately.
closed_unmerged_pr_exists() {
  local branch="$1" base_branch="$2" count
  count="$(gh pr list --repo "${REPO_OWNER}/${REPO_NAME}" --head "$branch" --state closed \
      --json merged,baseRefName --limit 20 2>/dev/null \
    | jq --arg base "$base_branch" '[.[] | select(.merged == false and .baseRefName == $base)] | length')"
  [[ "${count:-0}" -gt 0 ]]
}

# Bounds how many base-branch commits since the divergence point
# classify_mergeable will patch-id-hash for supersede comparison. Above this,
# the branch predates too much history to check safely/quickly, so it's left
# for manual review instead of guessed at.
MAX_BASE_COMMITS_TO_SCAN="${MAX_BASE_COMMITS_TO_SCAN:-1000}"

# Prints one of "merged", "superseded", or "" (keep — not confirmed safe) for
# `branch` against `base_branch`. This is the ONLY safety gate agent scripts
# may rely on before deleting a remote branch (#3392): a branch is deletable
# solely because it is fully merged/contained in base_branch, or because its
# linked issue is closed AND every commit unique to the branch has an
# equivalent (same patch-id) commit already on base_branch (the same change
# landed via a different branch/SHA). An unmerged branch whose issue is still
# open, or whose content never landed anywhere, always yields "".
classify_mergeable() {
  local branch="$1" issue="$2" base_branch="$3"

  git fetch origin "$branch" >/dev/null 2>&1 || { echo ""; return 0; }

  if git merge-base --is-ancestor "origin/${branch}" "origin/${base_branch}" 2>/dev/null; then
    echo "merged"
    return 0
  fi

  # Unmerged from here on: only ever "superseded" (never "merged"), and only
  # if the linked issue is closed and the diff content already landed.
  [[ -n "$issue" ]] || { echo ""; return 0; }

  local issue_state
  issue_state="$(gh issue view "$issue" --repo "${REPO_OWNER}/${REPO_NAME}" --json state --jq '.state' 2>/dev/null || echo "")"
  [[ "$issue_state" == "CLOSED" ]] || { echo ""; return 0; }

  local mb
  mb="$(git merge-base "origin/${branch}" "origin/${base_branch}" 2>/dev/null || echo "")"
  [[ -n "$mb" ]] || { echo ""; return 0; }

  local base_commit_count
  base_commit_count="$(git rev-list --count "${mb}..origin/${base_branch}" 2>/dev/null || echo 0)"
  if (( base_commit_count > MAX_BASE_COMMITS_TO_SCAN )); then
    echo ""
    return 0
  fi

  local branch_ids base_ids id missing=0
  branch_ids="$(git rev-list "${mb}..origin/${branch}" 2>/dev/null \
    | while read -r c; do git show "$c" | git patch-id --stable 2>/dev/null | awk '{print $1}'; done | sort -u)"
  [[ -n "$branch_ids" ]] || { echo ""; return 0; }

  base_ids="$(git rev-list "${mb}..origin/${base_branch}" 2>/dev/null \
    | while read -r c; do git show "$c" | git patch-id --stable 2>/dev/null | awk '{print $1}'; done | sort -u)"

  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    echo "$base_ids" | grep -qx "$id" || { missing=1; break; }
  done <<< "$branch_ids"

  if [[ "$missing" == "0" ]]; then
    echo "superseded"
  else
    echo ""
  fi
}

# Deletes prior attempt branches for `issue` (every naming convention the
# agent loop uses: (feat|fix|chore)/<issue>-* and claude/issue-<issue>-*),
# leaving `keep_branch` alone. Without this, every retry branch created by
# developer_create_branch.sh survives forever once the PR that actually
# merges comes from a *later* branch (#3392).
#
# Two independent gates must both agree before a candidate is deleted:
#   1. it is not the head of any currently OPEN pull request, and
#   2. classify_mergeable/closed_unmerged_pr_exists positively confirms it is
#      merged, superseded, or explicitly abandoned (closed without merge).
# Gate 1 alone is not sufficient: if the `gh pr list` call behind it fails
# transiently (rate limit, network blip, auth hiccup), it can silently
# report zero open PRs, which previously left gate 2 blank and deleted every
# sibling branch unconditionally — including the head of a live, unmerged PR
# whose commits exist nowhere else. Gate 2 makes that impossible: a branch
# that classify_mergeable can't positively confirm is always kept.
cleanup_superseded_branches() {
  local issue="$1" keep_branch="$2" base_branch="$3"
  local protected candidates cand

  protected="$(open_pr_head_branches 2>/dev/null || true)"
  candidates="$(git ls-remote --heads origin 2>/dev/null \
    | awk '{print $2}' | sed 's#^refs/heads/##' \
    | grep -E "^(feat|fix|chore)/${issue}-|^claude/issue-${issue}-" || true)"

  [[ -n "$candidates" ]] || return 0

  while IFS= read -r cand; do
    [[ -n "$cand" && "$cand" != "$keep_branch" ]] || continue
    if echo "$protected" | grep -qx "$cand"; then
      echo "[dev] Keeping prior branch $cand (head of an open PR)" >&2
      continue
    fi

    local reason=""
    if closed_unmerged_pr_exists "$cand" "$base_branch"; then
      reason="closed PR without merge (explicit abandonment)"
    else
      local verdict
      verdict="$(classify_mergeable "$cand" "$issue" "$base_branch")"
      case "$verdict" in
        merged) reason="fully merged/contained in ${base_branch}" ;;
        superseded) reason="issue #${issue} closed + equivalent commits present on ${base_branch}" ;;
        *) reason="" ;;
      esac
    fi

    if [[ -z "$reason" ]]; then
      echo "[dev] Keeping prior branch $cand (not confirmed merged/superseded)" >&2
      continue
    fi

    echo "[dev] Deleting superseded branch: $cand (${reason})" >&2
    delete_remote_branch "$cand"
  done <<< "$candidates"
}

# -------------------------
# PR Project Hygiene
# -------------------------
# Retry a field-set a few times before giving up; transient API failures and
# token hiccups were silently producing PRs with no project fields.
set_field_with_retry() {
  local item_id="$1" field="$2" value="$3" attempts="${4:-3}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if set_project_field_value "$item_id" "$field" "$value"; then
      return 0
    fi
    _warn "set ${field}='${value}' failed (attempt ${i}/${attempts})"
    sleep $((2 * i))
  done
  return 1
}

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

  # Set PR status to In Review. This is the hygiene-critical field: fail loud
  # (after retries) instead of warn-and-continue, so a dead token or API
  # outage can never again silently produce a bare PR on the board.
  _debug "Setting PR status to: $STATUS_IN_REVIEW"
  set_field_with_retry "$pr_item_id" "$STATUS_FIELD" "$STATUS_IN_REVIEW" \
    || _die "Failed to set Status on PR #${pr_number} after retries"

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
