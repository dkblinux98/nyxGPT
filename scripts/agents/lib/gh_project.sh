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

  # HTTP 529 is the API saying "overloaded", which is a statement about its
  # capacity and not about this run. On 2026-08-18 every Review Fix request on
  # #3832 returned 529 for ~3.3 minutes until the action's own retries were
  # exhausted; nothing had been committed or pushed, so there was nothing to
  # repair -- and the run was escalated to the owner as a FATAL anyway, because
  # no signature matched and unknown defaults to fatal.
  if echo "$error_text" | grep -qE "529|[Oo]verloaded"; then
    echo "retriable:api_overloaded"
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

  # Holding lane for acceptance failures/improvements filed during a round
  # (owner-created Project option, 2026-08-12, #3730 -- the drain gate). Same
  # treatment as Acceptance Testing above: optional config key with a literal
  # default, so no new repo variable is required.
  STATUS_ACCEPTANCE_FAILED="${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}"

  # Terminal lane for a PR's own project item (owner decision, 2026-08-12,
  # reaffirmed 2026-08-13, #3742). A PR card is a review-lane artifact, not
  # a work item: once the PR is merged or closed it must leave the active
  # lanes so it stops drowning the owner's acceptance queue. Same treatment
  # as the two statuses above -- optional config key with a literal default,
  # so no new repo variable is required.
  STATUS_CLOSED="${STATUS_CLOSED:-Closed}"

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
    # Explicit, not left to _die (#3811). _die `return`s rather than `exit`s
    # when the library is sourced, and it was relying on `set -e` to turn
    # that into an abort -- but `set -e` is suppressed inside a command
    # substitution whose status is being TESTED, which is exactly how a
    # caller checks for a failed call. Without this, `x="$(graphql ...)" ||
    # handle_it` fell through: the function carried on past the error and
    # echoed the error text as if it were the response.
    return 1
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
# Lookup-only: the issue's project item id, or empty output if the issue is
# not on the board at all. Split out of ensure_issue_in_project (#3816) so a
# caller can tell "not in the project yet" apart from "in the project with an
# empty Status" -- reading Status alone collapses those two into the same
# empty string, and hygiene must not treat a deliberately-unset field on an
# existing card the way it treats a brand-new card.
find_issue_project_item() {
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

  return 0
}

# Does `item_id` still name a live project item? Echoes one of:
#
#   present  GitHub resolved it
#   gone     GitHub says it does not exist ("Could not resolve to a node
#            with the global id ..."), i.e. the card was deleted or the
#            issue was moved off this board since the id was read
#   unknown  the question could not be answered (rate limit, transport)
#
# Why this is not just "did the call fail": those three demand different
# responses, and collapsing them is how a routine board move became a red
# run. #3811 / run 31926723483: a support ticket was hand-moved off the
# development project while hygiene was mid-flight, the next field read hit
# a dangling PVTI_ id, `graphql` _die()d, and the whole run failed with an
# unexplained global-id error. A vanished item means there is nothing left
# to fill -- which is a clean finish, not a failure. A transport error still
# has to be loud, because the item may be perfectly fine and unwritten.
#
# Deliberately does NOT go through graphql(): that helper _die()s on a
# non-zero `gh` exit, and the whole point here is to inspect that failure.
project_item_state() {
  require_cmd jq
  local item_id="$1"
  local q='query($item:ID!){ node(id:$item){ ... on ProjectV2Item { id } } }'

  local raw rc=0
  raw="$(gh api graphql -f query="$q" -F item="$item_id" 2>&1)" || rc=$?

  if [[ $rc -eq 0 ]] && [[ "$(echo "$raw" | jq -r '.data.node.id // empty' 2>/dev/null)" == "$item_id" ]]; then
    echo "present"
    return 0
  fi

  # GitHub reports a dangling node id as a NOT_FOUND GraphQL error, which
  # `gh` surfaces on stderr with a non-zero exit. Match the error type where
  # it is machine-readable and fall back to the message text, which is what
  # `gh` prints when it cannot parse the body as JSON.
  local err_type=""
  err_type="$(echo "$raw" | jq -r '[.errors[]?.type] | join(",")' 2>/dev/null || true)"
  if [[ "$err_type" == *NOT_FOUND* ]] || [[ "$raw" == *"Could not resolve to a node with the global id"* ]]; then
    echo "gone"
    return 0
  fi

  _warn "Could not determine whether project item ${item_id} still exists: ${raw}"
  echo "unknown"
}

ensure_issue_in_project() {
  require_cmd jq
  local issue_number="$1"
  local project_id
  project_id="$(get_project_id)"

  local existing
  existing="$(find_issue_project_item "$issue_number")"
  if [[ -n "$existing" ]]; then
    echo "$existing"
    return 0
  fi

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
# Project field reads (fill-if-missing hygiene, #3666)
# -------------------------
# Current value of `field_name` on project item `item_id`: the selected
# option name for a single-select field, the iteration title for an
# iteration field, or the raw string for a text field. Empty output means
# the field is unset. This is the ONLY signal fill-if-missing hygiene may
# use to decide whether to write a field -- no comment reading, no markers
# (owner design principle, #3666): a field with any existing value must be
# left untouched, and this is how callers check that before writing.
project_field_value() {
  require_cmd jq
  local item_id="$1" field_name="$2"
  local q='query($item:ID!) {
    node(id:$item) {
      ... on ProjectV2Item {
        fieldValues(first:50) {
          nodes {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue {
              field { ... on ProjectV2SingleSelectField { name } }
              name
            }
            ... on ProjectV2ItemFieldIterationValue {
              field { ... on ProjectV2IterationField { name } }
              title
            }
            ... on ProjectV2ItemFieldTextValue {
              field { ... on ProjectV2FieldCommon { name } }
              text
            }
          }
        }
      }
    }
  }'
  # NOT `graphql ... | jq ...` (#3811): in a pipeline the wrapper's failure
  # is the *first* segment's status, which the pipeline discards, so a failed
  # read returned empty output and exit 0 -- indistinguishable from "the
  # field is unset". That is the clobber fill_project_field_if_empty exists
  # to prevent, arrived at from the other direction: a rate-limited read
  # would have reported every field empty and hygiene would have written its
  # defaults over all of them. Take the response into a variable first so a
  # failed read is a failed read.
  local resp
  resp="$(graphql "$q" -F item="$item_id")" || return 1
  echo "$resp" | jq -r --arg f "$field_name" '
    .data.node.fieldValues.nodes[]
    | select(.field.name == $f)
    | (.name // .title // .text // empty)
  ' | head -n 1
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

# Writes `value` to `field_name` ONLY if the field is empty at the instant of
# writing (#3816). The read is taken here, immediately before the mutation, so
# that a value written by somebody else after the caller's own earlier check
# is still seen and still wins: hygiene's defaults must never land on top of a
# deliberate write.
#
# The window cannot be closed completely -- Projects v2 has no compare-and-set
# mutation -- but it shrinks from "however long the rest of the job takes"
# (Milestone's check used to run ~170 lines and several API calls before its
# write) to a single round trip. Callers that also want to REPORT the existing
# value read it themselves first; this guard is the one that decides.
#
# Exit codes: 0 written, 2 skipped because the field already had a value,
# non-zero otherwise (a failed write). Call it in an `if` or with `|| rc=$?`
# -- a bare call under `set -e` would abort the caller on the skip.
fill_project_field_if_empty() {
  local item_id="$1" field_name="$2" value="$3"

  local current
  # A failed read must NOT read as "the field is empty" -- that is the clobber
  # this guard exists to prevent, arrived at from the other direction.
  if ! current="$(project_field_value "$item_id" "$field_name")"; then
    _warn "Could not re-read ${field_name} before writing it — not writing '${value}'"
    return 1
  fi
  if [[ -n "$current" ]]; then
    _debug "fill_project_field_if_empty: ${field_name} already '${current}' — not writing '${value}'"
    return 2
  fi

  set_project_field_value "$item_id" "$field_name" "$value"
}

set_issue_status() {
  local issue="$1" status="$2"
  local item_id
  item_id="$(ensure_issue_in_project "$issue")"
  set_project_field_value "$item_id" "$STATUS_FIELD" "$status"
}

# Project Status of an issue (first project item), empty if none. Shared by
# promote_accepted_features.sh and the parked-blocked-issue sweep (#3631) --
# both need to read another issue's Status field without mutating it.
issue_status() {
  local num="$1"
  # Response into a variable, NOT `graphql ... | jq ...` (#3811) -- see the
  # note in project_field_value: in a pipeline the wrapper's failure is the
  # first segment's status, which the pipeline discards, so a failed read
  # became empty output and exit 0. Here that reads as "this issue has no
  # Status", which is a promotion/resume decision: a rate-limited read would
  # have looked exactly like a blocker that is not yet accepted.
  local resp
  resp="$(graphql "query(\$owner:String!, \$name:String!, \$num:Int!) {
    repository(owner:\$owner, name:\$name) {
      issue(number:\$num) {
        projectItems(first:5) {
          nodes {
            fieldValues(first:20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue { field { ... on ProjectV2SingleSelectField { name } } name }
              }
            }
          }
        }
      }
    }
  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$num")" || return 1
  echo "$resp" \
    | jq -r --arg field "$STATUS_FIELD" '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[]? | select(.field.name==$field) | .name' \
    | head -1
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
            ... on Issue {
              number
              state
              milestone { title }
              # Needed so the summarizer can refuse `Support`-labeled user
              # reports as candidates (#3745) -- it can only filter on a
              # label the query actually fetches.
              labels(first:20){ nodes { name } }
            }
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

# Prints the active sprint's ENTIRE issue population as
# {"open":{status:[numbers]}, "closed":{status:[numbers]}} -- the input to
# the autopilot's park decision (#3709).
#
# count_sprint_backlog_open() below answers only "is there eligible Backlog
# work?", which is the right question for *dispatch* but the wrong one for
# *park*: a sprint with an empty Backlog and three issues still In Progress
# is not complete. This pages the same project query once and hands the full
# population to sprint_calc.py sprint-park-state.
sprint_population_snapshot() {
  local sprint_field="$1" sprint_title="$2"
  require_cmd jq
  require_cmd python3

  local project_id cursor="" tmp acc has_next next_cursor
  project_id="$(get_project_id)"
  tmp="$(mktemp)"
  acc="$(mktemp)"

  local max_pages="${MAX_PAGES:-200}"
  local page
  for ((page = 1; page <= max_pages; page++)); do
    if [[ -n "$cursor" ]]; then
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -F after="$cursor" >"$tmp"
    else
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" >"$tmp"
    fi

    STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
      SPRINT_FIELD="$sprint_field" SPRINT_SCOPED=1 ACTIVE_SPRINT_TITLE="$sprint_title" \
      RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
      python3 "${_LIB_DIR}/summarize_backlog_page.py" "$tmp" >>"$acc"

    has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
    next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
    [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
    cursor="$next_cursor"
  done

  # Pages carry disjoint items, so merging is a key-wise list concat.
  jq -s -c '
    reduce .[] as $p ({open:{}, closed:{}};
        .open = (reduce ($p.sprint_open_issues_by_status // {} | to_entries[]) as $e
                   (.open; .[$e.key] = ((.[$e.key] // []) + $e.value)))
      | .closed = (reduce ($p.sprint_closed_issues_by_status // {} | to_entries[]) as $e
                   (.closed; .[$e.key] = ((.[$e.key] // []) + $e.value))))' "$acc"

  rm -f "$tmp" "$acc"
}

# Counts OPEN issues with Status=Backlog whose Sprint iteration field equals
# `sprint_title`. This is the sprint-autopilot DISPATCH condition (owner
# policy 2026-08-10, #3706 -- the auto loop is bound by the current sprint):
# review_accept_and_merge.sh dispatches next-issue selection while this is > 0.
# Once it hits 0 the loop stops dispatching, but whether that is "complete"
# is decided by sprint_population_snapshot + sprint_calc.py (#3709), not by
# this count alone.
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

# Release wall helpers. The release tracking issue's title carries the
# release version ("Release v2.0.0"), and the owner's milestone naming
# carries it too ("Phase 5.5: ... (v2.0.0)", "Phase 6 — ... (v3.0.0)").
# Matching the two is the OUTER boundary the loop must never cross: agents
# merge to RELEASE_BRANCH, so next-release work would land on the wrong
# branch. It opens via the release ceremony -- pointing RELEASE_ISSUE_NUMBER
# (and RELEASE_BRANCH) at the next release -- with no separate on/off switch.
#
# History note (#3706, 2026-08-10): earlier comments here described the
# release as the boundary that bounds AUTOMATIC WORK, citing an "owner
# decision 2026-07-31". The owner has stated that attribution was wrong --
# release-gating across sprint boundaries was never their intention, and the
# rationale was agent-authored. The standing owner policy (2026-08-10) is
# that the automatic loop is bound by the CURRENT SPRINT; the release wall
# survives only as the outer branch-safety boundary. Process rule from the
# same decision: a code comment claiming "owner decision" must cite a
# traceable source (issue number or owner comment link); an uncited claim is
# agent rationale, not policy.

# Prints the vX.Y.Z version parsed from the release tracking issue's title,
# or nothing if the issue/title has no version.
release_version_from_issue() {
  local release_issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${release_issue}" \
    --jq '.title' 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

# Buckets the release's open Backlog issues by Sprint iteration title and
# prints them as a JSON object, e.g. {"Sprint 8":0,"Sprint 9":11,"":2} (the
# "" key means "no Sprint set"). Same paging as count_sprint_backlog_open,
# release-filtered instead of sprint-filtered.
#
# This is NOT the continue/park decision input -- that is the active
# sprint's count (#3706). It exists so the park note can say what the loop
# is parked in front of: how much release work is queued behind the sprint
# boundary and in which sprint it sits.
release_backlog_by_sprint() {
  local release_version="$1" sprint_field="${2:-${SPRINT_FIELD:-Sprint}}"
  require_cmd jq
  require_cmd python3

  local project_id cursor="" tmp acc has_next next_cursor
  project_id="$(get_project_id)"
  tmp="$(mktemp)"
  acc="$(mktemp)"

  local max_pages="${MAX_PAGES:-200}"
  local page
  for ((page = 1; page <= max_pages; page++)); do
    if [[ -n "$cursor" ]]; then
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -F after="$cursor" >"$tmp"
    else
      graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" >"$tmp"
    fi

    STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
      SPRINT_FIELD="$sprint_field" RELEASE_VERSION="$release_version" SPRINT_SCOPED=0 \
      RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
      python3 "${_LIB_DIR}/summarize_backlog_page.py" "$tmp" | jq -c '.sprint_counts' >>"$acc"

    has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
    next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
    [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
    cursor="$next_cursor"
  done

  # Sum the per-page objects into one (pages carry disjoint items, so a
  # plain key-wise add is the whole merge).
  jq -s -c 'reduce .[] as $p ({}; reduce ($p | to_entries[]) as $e (.; .[$e.key] = ((.[$e.key] // 0) + $e.value)))' "$acc"

  rm -f "$tmp" "$acc"
}

# Machine marker stamped on every INFORMATIONAL sprint-autopilot comment
# (park note, paused notice) -- anything that is a status report rather than
# a dispatch kick. It was added for #3706, where the park note named the kick
# token and the dispatch trigger was a bare substring test with the agent
# accounts on its actor allowlist -- so every "park" was in fact a kick. That
# particular hazard is gone: #3882 made the kick a repository_dispatch, which
# prose cannot imitate. The marker is still load-bearing because these notes
# name PAUSE_SPRINT, which remains a live token: the shared gate
# (lib/comment_tokens.py, INFORMATIONAL_MARKERS) disqualifies any comment
# carrying it. Keep in sync with AUTOPILOT_INFO_MARKER in lib/sprint_calc.py
# and lib/comment_tokens.py.
AUTOPILOT_INFO_MARKER="<!-- nyxgpt-autopilot-informational -->"

# True if the most recent PAUSE_SPRINT/RESUME_SPRINT control comment on
# `release_issue` is a PAUSE_SPRINT -- the sprint-autopilot kill switch
# (#3480). No comment of either kind means "not paused" (default-on once
# SPRINT_AUTOPILOT itself is enabled).
sprint_autopilot_paused() {
  local release_issue="$1"
  require_cmd jq

  # --jq runs once per fetched page -- stream matching comments across all
  # pages first, then slurp+sort+last in a second jq pass (see AGENTS.md).
  local last_state
  last_state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${release_issue}/comments" --paginate \
    --jq '.[] | select(.body | test("^\\s*(PAUSE_SPRINT|RESUME_SPRINT)\\s*$"))' 2>/dev/null \
    | jq -s -r 'sort_by(.created_at) | last | .body // empty' \
    | tr -d '[:space:]')"
  [[ "$last_state" == "PAUSE_SPRINT" ]]
}

# -------------------------
# Acceptance drain gate (#3730)
# -------------------------
# Owner decision 2026-08-12: acceptance failures and improvements filed
# during an acceptance round land in the `Acceptance Failed` Status lane
# and are NOT worked immediately. The fix process starts only once the
# `Acceptance Testing` lane has DRAINED -- empty except the release
# tracking issue, which is exempt and stays there until the whole release
# is accepted. On drain, the held items move to `Backlog` for normal
# scrummaster selection and the queue is kicked once.
#
# Rationale (from the issue): picking failures up the moment they are
# filed floods Acceptance Testing with freshly-merged fixes mid-round and
# burns RC cycles while the owner is still testing. The gate makes the
# loop respect the owner's rhythm: test everything, drain, test the next
# candidate.
#
# Agent-process issues bypass the gate (issue_bypasses_drain_gate below).

# Every issue in the `Acceptance Testing` and `Acceptance Failed` lanes, as
# {"acceptance_testing":[...],"acceptance_failed":[...],
#  "acceptance_failed_parked":[...]}. Pages the same project-items query the
# selector and the autopilot use.
#
# Deliberately state-agnostic for the lane lists: an item awaiting acceptance
# is normally a CLOSED issue, so "has the lane drained?" has to count closed
# items too -- unlike count_sprint_backlog_open, which asks about
# dispatchable work.
#
# `acceptance_failed_parked` is the CLOSED subset of the holding lane: the
# features the owner tested, failed and parked there (owner decision
# 2026-08-14, #3780). The gate never moves those; only the promotion sweep
# does, once their whole blocked-by closure is accepted.
acceptance_lane_snapshot() {
  require_cmd jq
  require_cmd python3

  # Subshell + EXIT trap: the lib runs under `set -e`, so a mid-loop
  # `graphql` failure would otherwise leave both temp files behind. A
  # RETURN trap would outlive this function; a subshell scopes it exactly.
  (
    local project_id cursor="" tmp acc has_next next_cursor
    project_id="$(get_project_id)"
    tmp="$(mktemp)"
    acc="$(mktemp)"
    trap 'rm -f "$tmp" "$acc"' EXIT

    local max_pages="${MAX_PAGES:-200}"
    local page
    for ((page = 1; page <= max_pages; page++)); do
      if [[ -n "$cursor" ]]; then
        graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -F after="$cursor" >"$tmp"
      else
        graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" >"$tmp"
      fi

      STATUS_FIELD="$STATUS_FIELD" \
        STATUS_ACCEPTANCE_TESTING="${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}" \
        STATUS_ACCEPTANCE_FAILED="${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}" \
        python3 "${_LIB_DIR}/drain_gate.py" summarize "$tmp" >>"$acc"

      has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
      next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
      [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
      cursor="$next_cursor"
    done

    python3 "${_LIB_DIR}/drain_gate.py" merge <"$acc"
  )
}

# The issues parked awaiting rework by the currently held issues, as a JSON
# array. Read from each held issue's NATIVE blocking relationship (#3731),
# with the retired "Related feature: #N" body marker kept as the historical
# fallback. Those issues park CLOSED in Acceptance Testing -- or in
# Acceptance Failed, where the owner keeps what they have tested and failed
# (#3780) -- until everything blocking them reaches For Release
# (promote_accepted_features.sh), so without this the gate would deadlock:
# the issue waits on its failure, the failure waits on the gate, and the
# gate waits on the issue.
#
# Labels are fetched alongside the edges because only issues carrying a
# rework label park anything -- the same filter promote_accepted_features.sh
# applies, so the two sweeps always agree. Since #3731 that covers held
# Improvements too: they write the same blocking relationship, so exempting
# fewer issues than the sweep parks would reintroduce the deadlock
# (drain_gate.py rework_features applies the rule).
#
# An issue that cannot be read contributes nothing -- one unreadable issue
# must not silently open the gate wider than it should, and the next poll
# retries.
drain_gate_rework_features() {
  local held_json="$1"
  require_cmd jq
  require_cmd python3

  local issues=() issue payload blocks
  while IFS= read -r issue; do
    [[ -n "$issue" ]] || continue
    payload="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" \
      --jq '{body: (.body // ""), labels: [.labels[]?.name]}' 2>/dev/null)" || {
      _warn "drain_gate_rework_features: could not read #${issue} -- ignoring its blocking relationship"
      continue
    }
    [[ -n "$payload" ]] || continue
    blocks="$(blocking_issues "$issue" | jq -R -n -c '[inputs | select(length > 0) | tonumber]' 2>/dev/null)"
    [[ -n "$blocks" ]] || blocks="[]"
    payload="$(jq -c --argjson b "$blocks" '. + {blocks: $b}' <<<"$payload")"
    issues+=("$payload")
  done < <(jq -r '.[]' <<<"$held_json")

  # Nothing readable held -> nothing parks a feature. (Guarded before the
  # array expansion: `set -u` on bash 3.2 errors on an empty "${a[@]}".)
  if [[ "${#issues[@]}" -eq 0 ]]; then
    echo "[]"
    return 0
  fi

  # One object per line -> `jq -s` slurps them once, instead of re-parsing
  # the whole accumulator per held issue.
  printf '%s\n' "${issues[@]}" \
    | jq -s -c '.' \
    | python3 "${_LIB_DIR}/drain_gate.py" rework \
    | jq -c '.rework_features'
}

# {"open":bool,"blockers":[...],"held":[...],...} for the live board.
# `open` means Acceptance Testing holds nothing but exempt items: the
# release issue, and the features whose own failures this gate is holding.
drain_gate_state() {
  require_cmd jq
  require_cmd python3
  local snapshot held rework
  snapshot="$(acceptance_lane_snapshot)" || return 1
  # Owner-parked features (CLOSED items in the holding lane, #3780) are not
  # held work: they are subtracted here so the rework lookup does not spend
  # an API call per parked feature, and `decide` subtracts them again from
  # `held` so a gate opening can never move one.
  held="$(jq -c '.acceptance_failed - (.acceptance_failed_parked // [])' <<<"$snapshot")"
  rework="$(drain_gate_rework_features "$held")" || rework="[]"
  [[ -n "$rework" ]] || rework="[]"
  snapshot="$(jq -c --argjson r "$rework" '. + {rework_features: $r}' <<<"$snapshot")"
  RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
    python3 "${_LIB_DIR}/drain_gate.py" decide <<<"$snapshot"
}

# True when `issue` is agent-process work and may be worked immediately
# instead of being held in the Acceptance Failed lane. The rule is encoded
# in drain_gate.py (body marker / owner-authored process exception /
# optional label list), not inferred from context here.
issue_bypasses_drain_gate() {
  local issue="$1"
  require_cmd python3
  local issue_json
  issue_json="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" \
    --jq '{title, body, labels}' 2>/dev/null)" || return 1
  [[ -n "$issue_json" ]] || return 1
  [[ "$(python3 "${_LIB_DIR}/drain_gate.py" bypass <<<"$issue_json")" == "true" ]]
}

# Same rule, applied to text that is not (yet) an issue -- the owner's
# `@acceptance-failure` / `@improvement` comment body, which becomes the
# filed issue's body. Keeps the process-exception rule in ONE place
# (drain_gate.py) instead of re-implementing the match in each handler.
text_bypasses_drain_gate() {
  local text="$1"
  require_cmd jq
  require_cmd python3
  [[ "$(jq -n --arg b "$text" '{body: $b}' \
    | python3 "${_LIB_DIR}/drain_gate.py" bypass)" == "true" ]]
}

# Places `issue` in the Acceptance Failed lane (the gate's holding area)
# and explains on the issue why it is not being worked yet.
drain_gate_hold() {
  local issue="$1" kind="${2:-acceptance failure}"
  local lane="${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}"
  set_issue_status "$issue" "$lane" \
    || { _warn "drain_gate_hold: could not set status '${lane}' on #${issue}"; return 1; }
  issue_comment "$issue" "⏸️ **Drain gate (#3730)**: this ${kind} is held in **${lane}** until the current acceptance round finishes.

The fix process starts when **${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}** has drained (empty except the release tracking issue). At that point this issue moves to **${STATUS_BACKLOG}** automatically and the scrummaster queue is kicked once — no manual step." \
    || _warn "drain_gate_hold: could not comment on #${issue}"
  return 0
}

# Marker for the single kick a gate opening posts, so the release issue's
# history shows which drain released which batch.
DRAIN_GATE_KICK_MARKER="<!-- nyxgpt-drain-gate-release -->"

# Opens the gate: moves every HELD Acceptance Failed item to Backlog and
# kicks the scrummaster queue ONCE. Held means the round's open rework; the
# owner's parked (CLOSED) features in the same lane are reported and left
# exactly where they are (#3780). Prints a JSON result. Idempotent by
# construction -- once the lane is empty a later run releases nothing and
# posts no kick, so a poll every few minutes is free of side effects.
#
# DRY_RUN=1 reports what it would do without mutating anything.
drain_gate_release() {
  require_cmd jq
  local state held count released=() failed=()
  state="$(drain_gate_state)" || {
    _warn "drain_gate_release: could not read gate state"
    return 1
  }

  if [[ "$(jq -r '.open' <<<"$state")" != "true" ]]; then
    local blockers
    blockers="$(jq -r '.blockers | join(", #")' <<<"$state")"
    echo "[drain-gate] Gate CLOSED: Acceptance Testing still holds #${blockers}." >&2
    jq -n -c --argjson s "$state" '$s + {action: "none", reason: "gate closed"}'
    return 0
  fi

  # Never silently drop what the gate deliberately leaves behind: an owner
  # parked it there (#3780) and the run log has to show it was seen.
  local parked
  parked="$(jq -r '.parked // [] | join(", #")' <<<"$state")"
  [[ -z "$parked" ]] \
    || echo "[drain-gate] Owner-parked features left in ${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}: #${parked} (moved only by the promotion sweep, #3780)." >&2

  held="$(jq -r '.held[]' <<<"$state")"
  count="$(_count_lines "$held")"
  if [[ "$count" == "0" ]]; then
    echo "[drain-gate] Gate OPEN, nothing held -- nothing to do." >&2
    jq -n -c --argjson s "$state" '$s + {action: "none", reason: "nothing held"}'
    return 0
  fi

  echo "[drain-gate] Gate OPEN: releasing ${count} held issue(s) into ${STATUS_BACKLOG}." >&2
  local rework_exempt
  rework_exempt="$(jq -r '.rework_exempt // [] | join(", #")' <<<"$state")"
  [[ -z "$rework_exempt" ]] \
    || echo "[drain-gate] Awaiting-rework features exempt from the drain check: #${rework_exempt}." >&2
  local issue
  while IFS= read -r issue; do
    [[ -n "$issue" ]] || continue
    if [[ "${DRY_RUN:-0}" == "1" ]]; then
      echo "[drain-gate] DRY_RUN: would move #${issue} -> ${STATUS_BACKLOG}" >&2
      released+=("$issue")
      continue
    fi
    if set_issue_status "$issue" "$STATUS_BACKLOG"; then
      released+=("$issue")
      issue_comment "$issue" "▶️ **Drain gate (#3730)**: the acceptance round has drained — moving this issue to **${STATUS_BACKLOG}** for normal scrummaster selection." \
        || _warn "drain_gate_release: could not comment on #${issue}"
    else
      failed+=("$issue")
      _warn "drain_gate_release: could not move #${issue} to ${STATUS_BACKLOG}"
    fi
  done <<<"$held"

  # One kick for the whole batch, not one per issue: the dispatcher
  # serializes on the `scrummaster-dispatch` concurrency group and picks
  # the next issue itself, so N kicks would be N redundant runs.
  local kicked=false refs=""
  if [[ ${#released[@]} -gt 0 && "${DRY_RUN:-0}" != "1" ]]; then
    refs="$(printf '#%s ' "${released[@]}")"
    if [[ -z "${RELEASE_ISSUE_NUMBER:-}" ]]; then
      _warn "drain_gate_release: RELEASE_ISSUE_NUMBER not configured -- released the lane but cannot kick the queue."
    elif sprint_autopilot_paused "$RELEASE_ISSUE_NUMBER"; then
      # Same rule as the autopilot: while PAUSE_SPRINT is in force nothing
      # dispatches. The note names PAUSE_SPRINT, which is still a live
      # token, so it carries the informational marker that
      # disqualifies it at the shared gate (#3706; the kick token it also
      # had to avoid is gone -- the kick is an event since #3882).
      issue_comment "$RELEASE_ISSUE_NUMBER" "⏸️ **Drain gate (#3730)**: acceptance round drained and ${refs% } moved to ${STATUS_BACKLOG}, but the sprint is paused (\`PAUSE_SPRINT\`) — no kick posted. Comment \`RESUME_SPRINT\` to continue.

${AUTOPILOT_INFO_MARKER}" \
        || _warn "drain_gate_release: could not post the paused notice."
    else
      # Count what actually moved, not what was held: a partial release
      # would otherwise report a batch it did not deliver.
      issue_comment "$RELEASE_ISSUE_NUMBER" "▶️ **Drain gate (#3730)**: **${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}** has drained (only this release issue remains). Released ${#released[@]} held issue(s) into ${STATUS_BACKLOG}: ${refs% }.

${DRAIN_GATE_KICK_MARKER}

${AUTOPILOT_INFO_MARKER}" \
        || _warn "drain_gate_release: could not post the release note."
      dispatch_next_issue "drain gate: acceptance round drained" \
        && kicked=true \
        || _warn "drain_gate_release: could not dispatch next-issue selection."
    fi
  fi

  jq -n -c --argjson s "$state" --argjson kicked "$kicked" \
    --argjson released "$(printf '%s\n' "${released[@]:-}" | jq -R 'select(length>0) | tonumber' | jq -s -c .)" \
    --argjson failed "$(printf '%s\n' "${failed[@]:-}" | jq -R 'select(length>0) | tonumber' | jq -s -c .)" \
    '$s + {action: "released", released: $released, failed: $failed, kicked: $kicked}'
}

# -------------------------
# Dependency-aware auto-resume of parked In Progress issues (#3709)
# -------------------------
# An In Progress issue can end up PARKED: no open PR closing it and no
# in-flight developer run -- e.g. it refused earlier because a prose
# "Blocked by: #N" gate was open at the time, or its runs died in an
# incident. Before #3709 nothing picked those back up when their blockers
# merged, so a human re-triggered the developer by hand at every gate
# opening (the Sprint 8 cloud chain #3509 -> #3510 -> #3513 -> #3514/#3515/#3516 was
# hand-walked that way). Owner requirement: no babysitting -- the loop
# drives its own chain.
#
# Every input below is observable state (open PRs, workflow runs, issue
# bodies, comment threads) re-derived on each kick -- no hidden counters
# (house principle). The decision math lives in lib/parked_resume.py.
DEV_WORKFLOW_FILE="${DEV_WORKFLOW_FILE:-developer_auto_implement.yml}"
# How far back the run scan looks. The question is only "is a run for this
# issue live right now?", and live runs are always among the most recent.
AUTOPILOT_RUN_SCAN_LIMIT="${AUTOPILOT_RUN_SCAN_LIMIT:-50}"

# Numbers of OPEN PRs that close `issue` -- via the plain pulls list, NOT
# `gh api search/issues` (the endpoint whose deterministic failure caused
# the 2026-08-09 multi-issue incident, #3694: the loop's own liveness checks
# must not depend on it). Matches either a closing keyword in the PR body or
# the developer_create_branch.sh branch convention `<kind>/<issue>-<slug>`.
_issue_open_pr_numbers() {
  local issue="$1"
  require_cmd jq
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls?state=open&per_page=100" --paginate \
    --jq '.[] | {number: .number, body: (.body // ""), head: (.head.ref // "")}' 2>/dev/null \
    | jq -r --arg n "$issue" '
        select((.body | test("(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\\s+#" + $n + "\\b"))
               or (.head | test("^[A-Za-z]+/" + $n + "-")))
        | .number' 2>/dev/null
}

# Ids of developer-agent workflow runs for `issue_title` that have not
# completed. Issue-triggered runs carry the issue title in `display_title`
# (verified against the live API, 2026-08-10) -- there is no issue-number
# field on the runs API, so the title is the attribution key.
_issue_active_dev_run_ids() {
  local issue_title="$1"
  require_cmd jq
  [[ -n "$issue_title" ]] || return 0
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${DEV_WORKFLOW_FILE}/runs?per_page=${AUTOPILOT_RUN_SCAN_LIMIT}" \
    --jq '.workflow_runs[] | {id: .id, status: .status, display_title: (.display_title // "")}' 2>/dev/null \
    | jq -r --arg t "$issue_title" 'select(.display_title == $t and .status != "completed") | .id' 2>/dev/null
}

# Blocker issue numbers of `issue` that are still OPEN, one per line.
#
# Native relationships are the source of truth (#3731): the walk starts from
# `issue`'s native blocked_by dependencies and follows them TRANSITIVELY --
# an open blocker's own open blockers gate this issue too. Traversal stops at
# closed blockers: a closed blocker is done, and whatever once blocked *it*
# no longer holds anything back.
#
# The prose `Blocked by: #N` body references (INTERIM -- see
# lib/parked_resume.py) are a FALLBACK for issues written before native
# relationships, consulted only when the issue has no native blocked_by
# edges. Nothing writes prose gates any more.
_issue_open_gate_refs() {
  local issue="$1" body="$2"
  require_cmd python3
  local -a frontier=()
  local r state
  while IFS= read -r r; do
    [[ -n "$r" ]] && frontier+=("$r")
  done < <(blocked_by_issues "$issue")

  if [[ "${#frontier[@]}" -eq 0 ]]; then
    while IFS= read -r r; do
      [[ -n "$r" ]] && frontier+=("$r")
    done < <(printf '%s' "$body" | python3 "${_LIB_DIR}/parked_resume.py" parse-blocked-by 2>/dev/null)
  fi

  local -A seen=()
  seen["$issue"]=1
  while [[ "${#frontier[@]}" -gt 0 ]]; do
    r="${frontier[0]}"
    frontier=("${frontier[@]:1}")
    [[ -n "${seen[$r]:-}" ]] && continue
    seen["$r"]=1
    state="$(_issue_open_state "$r")"
    [[ "$state" == "OPEN" ]] || continue
    echo "$r"
    local next
    while IFS= read -r next; do
      [[ -n "$next" && -z "${seen[$next]:-}" ]] && frontier+=("$next")
    done < <(blocked_by_issues "$r")
  done
  return 0
}

# The auto-resume budget for `issue`, as parked_resume.py renders it:
# {"count":N,"exhausted":bool,"next_resume_number":N,"max_resumes":N}.
# Re-derived from the live comment thread every time -- the markers in the
# thread ARE the counter.
_autopilot_resume_budget() {
  local issue="$1"
  require_cmd jq
  require_cmd python3
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/comments" --paginate \
    --jq '[.[] | {body: (.body // ""), author_association: (.author_association // "")}]' 2>/dev/null \
    | jq -s -c 'add // []' \
    | python3 "${_LIB_DIR}/parked_resume.py" budget 2>/dev/null \
    || echo '{"count":0,"exhausted":false,"next_resume_number":1,"max_resumes":3}'
}

# Scans the active sprint's In Progress issues (from a
# sprint_population_snapshot JSON) and echoes the parked_resume.py `scan`
# result: {resumable, waiting, exhausted, active, selected}. Read-only --
# posting the resume is _autopilot_post_resume's job.
autopilot_scan_parked() {
  local snapshot="$1"
  require_cmd jq
  require_cmd python3

  local in_progress_status="${STATUS_IN_PROGRESS:-In Progress}"
  local -a candidates=()
  local n issue_json title body parked open_blockers exhausted

  while IFS= read -r n; do
    [[ -n "$n" ]] || continue
    issue_json="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${n}" \
      --jq '{title: (.title // ""), body: (.body // "")}' 2>/dev/null || echo '{}')"
    title="$(jq -r '.title // ""' <<<"$issue_json" 2>/dev/null || echo "")"
    body="$(jq -r '.body // ""' <<<"$issue_json" 2>/dev/null || echo "")"

    parked=true
    if [[ -n "$(_issue_open_pr_numbers "$n")" ]]; then
      parked=false
    elif [[ -n "$(_issue_active_dev_run_ids "$title")" ]]; then
      parked=false
    fi

    open_blockers="[]"
    exhausted=false
    if [[ "$parked" == "true" ]]; then
      open_blockers="$(_issue_open_gate_refs "$n" "$body" \
        | jq -R -n -c '[inputs | select(length > 0) | tonumber]' 2>/dev/null || echo '[]')"
      exhausted="$(_autopilot_resume_budget "$n" | jq -r '.exhausted' 2>/dev/null || echo false)"
      [[ "$exhausted" == "true" || "$exhausted" == "false" ]] || exhausted=false
    fi

    candidates+=("$(jq -n -c --argjson i "$n" --argjson p "$parked" \
      --argjson b "$open_blockers" --argjson e "$exhausted" \
      '{issue: $i, parked: $p, open_blockers: $b, budget_exhausted: $e}')")
  done < <(jq -r --arg s "$in_progress_status" '.open[$s][]? // empty' <<<"$snapshot" 2>/dev/null)

  if [[ "${#candidates[@]}" -eq 0 ]]; then
    echo '[]' | python3 "${_LIB_DIR}/parked_resume.py" scan
    return 0
  fi
  printf '%s\n' "${candidates[@]}" | jq -s -c . \
    | python3 "${_LIB_DIR}/parked_resume.py" scan
}

# Resumes a parked issue: assign it back to the developer agent (#3882), and
# leave the budget-marked comment as the record of why.
#
# The assignment is the whole trigger. This used to post a retry token and
# rely on developer_auto_implement.yml pattern-matching the comment body --
# the mechanism that let a workflow's own prose start work (#3706, #3790).
# The comment still carries the auto-resume marker, because
# parked_resume.py counts *markers*, not tokens, when it decides whether the
# budget is spent.
_autopilot_post_resume() {
  local issue="$1"
  require_cmd jq
  require_cmd python3
  local budget n max marker
  budget="$(_autopilot_resume_budget "$issue")"
  n="$(jq -r '.next_resume_number // 1' <<<"$budget" 2>/dev/null || echo 1)"
  max="$(jq -r '.max_resumes // 3' <<<"$budget" 2>/dev/null || echo 3)"
  marker="$(python3 "${_LIB_DIR}/parked_resume.py" marker "$issue" "$n")"

  issue_comment "$issue" "🔁 **Sprint Autopilot — auto-resume (${n}/${max})**: this issue is In Progress but parked (no open PR, no running developer job), and every dependency it declares is now closed. Reassigning it to @${DEV_AGENT:-the developer agent} to restart implementation (#3709) -- no human trigger needed.

If this run stops again without progress, the auto-resume budget (${max}) will be spent and the issue is reported as gate-stuck on the release tracking issue instead of being retried forever. A comment from the repo owner resets the budget (#3689).

${marker}"

  # The assignment, not the comment, is what starts the work (#3882).
  assign_and_trigger_developer "$issue"
}

# -------------------------
# Release candidate at agentic-work-complete (#3729)
# -------------------------
# The owner's acceptance cadence is: wait for the sprint to reach "agentic
# work complete", run a full acceptance round, file failures/improvements,
# repeat. The moment the autopilot detects that state is exactly the moment
# a candidate should exist on every platform, so the park transition
# dispatches the #3727 publish pipeline (PyPI + Homebrew tap in one run)
# instead of leaving the owner to dispatch it by hand.
#
# Three properties, each enforced below rather than merely intended:
#
#   * it reuses the park detection (`sprint_park_state`) -- there is no
#     second state machine deciding when the sprint is done;
#   * the channel is `rc`, hard-coded and re-checked at the dispatch (the
#     kick path can never publish `stable`; a release is cut only by
#     scripts/release_ceremony.sh, which carries a tag and a confirmation
#     token this path does not have);
#   * repeat observations of the same parked state are no-ops. The
#     authority for that is the pipeline's own tip guard, so a candidate is
#     never duplicated even if this preflight is unavailable.

#: The one park state that means "agentic work complete" (#3709): every
#: sprint item closed into acceptance, nothing open, nothing in flight.
#: `sprint_complete` deliberately does NOT publish -- by then the owner has
#: already accepted the round this candidate would have been for.
AUTOPILOT_RC_PARK_STATE="awaiting_acceptance"
#: Never anything else. See the guard in _autopilot_publish_rc.
AUTOPILOT_RC_CHANNEL="rc"
AUTOPILOT_PUBLISH_WORKFLOW="release-publish-pypi.yml"

# Asks src/nyxgpt/release_candidate.py what an rc dispatch would publish
# from `branch` right now: the next `3.0.0rcN`, or the already-published
# candidate when the branch tip has not moved since it was cut. The two API
# reads happen here (the agent runners have `gh` and `curl` authenticated,
# not necessarily the package's HTTP stack) and the decision happens there,
# so this path and the pipeline share one implementation of "has the tip
# moved?". Echoes the decision JSON, or nothing at all when the preflight
# cannot run -- callers must treat that as "unknown", never as "no".
_autopilot_rc_preflight() {
  local branch="$1"
  local tip published runs
  tip="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/commits/${branch}" --jq '.sha' 2>/dev/null || echo "")"
  published="$(curl -fsSL --max-time 20 "https://pypi.org/pypi/nyxgpt/json" 2>/dev/null |
    jq -c '[.releases | keys[]]' 2>/dev/null || echo "")"
  jq -e 'type == "array"' <<<"${published:-null}" >/dev/null 2>&1 || published='[]'
  # Only the fields the decision reads -- naming them keeps it obvious that
  # the run title is load-bearing (it is how an rc run is told apart from a
  # dev or stable dispatch). `status` is one of them (#3771): the listing is
  # NOT narrowed to successes, because a cut that is still in flight has no
  # conclusion yet and is precisely the run this preflight must not race --
  # dispatching alongside it is how two candidates get cut from one tip.
  # (The pipeline's own guard queues behind the concurrency group instead and
  # counts successes only; this preflight never queues, so it looks wider.)
  # A page now holds runs of every status, so it is 100 rather than 30 -- a
  # busy line would otherwise push the last real candidate off it.
  runs="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${AUTOPILOT_PUBLISH_WORKFLOW}/runs?event=workflow_dispatch&per_page=100" \
    --jq '{workflow_runs: [.workflow_runs[] | {id, event, status, conclusion, head_sha, created_at, display_title, name}]}' 2>/dev/null || echo "")"
  jq -e 'has("workflow_runs")' <<<"${runs:-null}" >/dev/null 2>&1 || runs='{}'

  jq -n -c --argjson p "$published" --argjson r "$runs" '{published: $p, runs: $r}' |
    PYTHONPATH="${_LIB_DIR}/../../../src" python3 -m nyxgpt.release_candidate \
      --autopilot-preflight --branch "$branch" --head-sha "$tip" 2>/dev/null
}

# Dispatches the rc publish for a park that just reached agentic-work-
# complete and echoes what the park note should say about it (JSON; `{}`
# when this park state is not the one that publishes). Best-effort like
# every other autopilot step: a failure here is reported in the note and
# never breaks the park.
_autopilot_publish_rc() {
  local park_state="${1:-}"
  require_cmd jq
  local state branch preflight version reason runs_url

  jq -e . <<<"${park_state:-null}" >/dev/null 2>&1 || park_state='{}'
  state="$(jq -r '.state // empty' <<<"$park_state" 2>/dev/null || echo "")"
  if [[ "$state" != "$AUTOPILOT_RC_PARK_STATE" ]]; then
    # Not the transition into agentic-work-complete: publish nothing. Work
    # still in flight has no candidate to test, and a sprint already
    # accepted to For Release has had its acceptance round.
    echo '{}'
    return 0
  fi

  if [[ "$AUTOPILOT_RC_CHANNEL" != "rc" ]]; then
    # Unreachable by design, and deliberately fatal to the dispatch rather
    # than merely surprising: this path must never be able to publish a
    # release. Cutting one is the ceremony's job (tag + confirmation token).
    _warn "Autopilot: refusing to publish channel '${AUTOPILOT_RC_CHANNEL}' -- the kick path publishes candidates only."
    jq -n -c --arg c "$AUTOPILOT_RC_CHANNEL" \
      '{dispatched: false, error: true, reason: ("refused to publish channel " + $c + " -- the autopilot publishes candidates only")}'
    return 0
  fi

  branch="$(get_release_branch 2>/dev/null || echo "")"
  if [[ -z "$branch" ]]; then
    _warn "Autopilot: no RELEASE_BRANCH configured -- cannot dispatch a release candidate."
    jq -n -c '{dispatched: false, error: true, channel: "rc", reason: "no RELEASE_BRANCH is configured"}'
    return 0
  fi

  preflight="$(_autopilot_rc_preflight "$branch" 2>/dev/null || echo "")"
  jq -e 'has("dispatch")' <<<"${preflight:-null}" >/dev/null 2>&1 || preflight=""

  version=""
  reason=""
  if [[ -n "$preflight" ]]; then
    version="$(jq -r '.version // ""' <<<"$preflight")"
    reason="$(jq -r '.reason // ""' <<<"$preflight")"
    if [[ "$(jq -r '.dispatch // false' <<<"$preflight")" != "true" ]]; then
      # The tip has not moved since the last candidate, so the pipeline
      # would resolve this to SKIP. Don't spend a run to be told that --
      # report the candidate the owner should install instead.
      echo "[review] Autopilot: ${branch} is unchanged since ${version:-the last candidate} -- no new rc dispatched." >&2
      jq -n -c --arg b "$branch" --arg v "$version" --arg r "$reason" \
        '{dispatched: false, noop: true, channel: "rc", branch: $b, version: $v, reason: $r}'
      return 0
    fi
  fi

  runs_url="https://github.com/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${AUTOPILOT_PUBLISH_WORKFLOW}"
  if gh workflow run "$AUTOPILOT_PUBLISH_WORKFLOW" -R "${REPO_OWNER}/${REPO_NAME}" \
    --ref "$branch" -f channel="$AUTOPILOT_RC_CHANNEL" >/dev/null 2>&1; then
    echo "[review] Autopilot: dispatched an rc publish on ${branch}${version:+ (${version})}." >&2
    jq -n -c --arg b "$branch" --arg v "$version" --arg u "$runs_url" \
      '{dispatched: true, channel: "rc", branch: $b, version: $v, runs_url: $u}'
  else
    _warn "Autopilot: failed to dispatch the rc publish on ${branch}."
    jq -n -c --arg b "$branch" --arg v "$version" --arg u "$runs_url" \
      '{dispatched: false, error: true, channel: "rc", branch: $b, version: $v, runs_url: $u,
        reason: "GitHub refused the workflow dispatch"}'
  fi
}

# Sprint-autopilot continuation kick (#3480), shared by the post-merge path
# (review_accept_and_merge.sh) and the 3-cycle review escalation path
# (review_agent_auto_review.yml): both outcomes free the reviewer for the
# next issue, so both send the same dispatch-next-issue event on the
# release tracking issue. `verb` is "merged", "escalated", or "anomaly" and
# only changes the comment wording; every gate (SPRINT_AUTOPILOT,
# RELEASE_ISSUE_NUMBER, PAUSE_SPRINT, release version parse, sprint-drained
# park) is identical. Best-effort by design: always returns 0 so a kick
# failure never fails the merge, escalation, or anomaly detection that
# invoked it.
# Ask the scrummaster to select and start the next issue (#3882).
#
# This replaces posting READY_FOR_NEXT_ISSUE into an issue comment. The comment
# was never a message to a human: it was one workflow asking another to run,
# expressed as text that a job `if:` then had to pattern-match -- which is how a
# workflow's own guidance text twice started work it was announcing the end of
# (#3706, #3790). A repository_dispatch says the same thing as an event, so
# nothing that merely mentions it can trigger it.
dispatch_next_issue() {
  local reason="${1:-}"

  if gh api "repos/${REPO_OWNER}/${REPO_NAME}/dispatches" \
      -f "event_type=dispatch-next-issue" \
      -f "client_payload[reason]=${reason}" >/dev/null 2>&1; then
    echo "[dispatch] requested next-issue selection (${reason})" >&2
    return 0
  fi

  _warn "dispatch_next_issue: could not send the dispatch event (${reason})."
  return 1
}

sprint_autopilot_kick() {
  local issue="$1" verb="${2:-merged}"
  local event_phrase continue_phrase
  if [[ "$verb" == "escalated" ]]; then
    event_phrase="Issue #${issue} escalated to the owner after 3 review cycles"
    continue_phrase="continuing with other work"
  elif [[ "$verb" == "anomaly" ]]; then
    # Distinct from "escalated" (#3694 review finding): this issue was not
    # escalated to the owner and no review cycles occurred -- it hit a
    # cross-issue infrastructure anomaly another issue already diagnosed.
    # Reusing the "escalated" phrase here would misreport the cause on the
    # release tracking issue, the exact channel this feature exists to keep
    # accurate.
    event_phrase="Issue #${issue} paused pending a cross-issue infrastructure anomaly (#3694)"
    continue_phrase="continuing with other work"
  else
    event_phrase="Issue #${issue} merged"
    continue_phrase="continuing automatically"
  fi

  echo "[review] ===== Sprint autopilot =====" >&2
  local autopilot_value="${SPRINT_AUTOPILOT:-false}"
  if [[ "$autopilot_value" != "true" ]]; then
    echo "[review] Sprint autopilot disabled (SPRINT_AUTOPILOT=${autopilot_value}) -- no auto-kick." >&2
  elif [[ -z "${RELEASE_ISSUE_NUMBER:-}" ]]; then
    _warn "SPRINT_AUTOPILOT is on but RELEASE_ISSUE_NUMBER is not configured -- skipping auto-kick."
  elif sprint_autopilot_paused "$RELEASE_ISSUE_NUMBER"; then
    echo "[review] Sprint autopilot paused (PAUSE_SPRINT) -- no auto-kick." >&2
    # Informational, NOT a kick. The kick is now a repository_dispatch
    # (#3882), which no comment can imitate, so the old hazard this note
    # guarded against -- a "paused" notice whose own text re-triggered the
    # then-comment-driven kick, which was not gated on PAUSE_SPRINT (#3706
    # review) -- cannot recur. The marker stays because this note names
    # `PAUSE_SPRINT`, which IS still a live token: it is what tells the
    # shared comment-token gate this is a status report, not a command.
    issue_comment "$RELEASE_ISSUE_NUMBER" "⏸️ **Sprint Autopilot**: ${event_phrase}, but autopilot is paused (\`PAUSE_SPRINT\`) -- no next issue dispatched. Comment \`RESUME_SPRINT\` to continue, or start an issue by hand by assigning the developer agent to it (\`docs/sprint-autopilot.md\`).

${AUTOPILOT_INFO_MARKER}" \
      || _warn "Failed to post autopilot-paused notice."
  else
    # The continue/park decision is SPRINT-gated (owner policy 2026-08-10,
    # #3706): the auto loop is bound by the current sprint, so sprint
    # membership is a real work boundary and not bookkeeping. The autopilot
    # continues while the ACTIVE sprint iteration (the one whose date window
    # contains today per SPRINT_TIMEZONE) still has open Backlog work, and
    # parks with a loud note when that pool drains -- even if the release
    # has plenty queued in later sprints. Crossing into the next sprint
    # needs a planning event, or the owner assigning an issue directly.
    #
    # The release version is still resolved, but only as the OUTER
    # branch-safety wall and to describe what is waiting behind the sprint
    # boundary in the park note. It is no longer the decision input; the
    # earlier "release-gated (owner decision 2026-07-31)" rationale here was
    # agent-authored and misattributed (see the release wall helpers above).
    #
    # Drift caveat: because the sprint boundary is now load-bearing, sprint
    # iteration date windows must be kept current on the project board. A
    # stale window means "no active sprint", which parks the loop.
    local sprint_field active_sprint release_version remaining decision
    sprint_field="${SPRINT_FIELD:-Sprint}"
    active_sprint="$(iteration_active_title "$sprint_field" 2>/dev/null || echo "")"
    [[ "$active_sprint" != "null" ]] || active_sprint=""
    release_version="$(release_version_from_issue "$RELEASE_ISSUE_NUMBER" 2>/dev/null || echo "")"

    if [[ -z "$release_version" ]]; then
      _warn "Autopilot: could not parse a vX.Y.Z version from release issue #${RELEASE_ISSUE_NUMBER}'s title -- no auto-kick (conservative stop)."
    elif [[ -z "$active_sprint" ]]; then
      # No iteration's window contains today: there is no sprint to work,
      # so park rather than falling back to release-wide selection.
      _warn "Autopilot: no active sprint iteration on field '${sprint_field}' -- parking (conservative stop)."
      _autopilot_park_note "$event_phrase" "" "$release_version"
    else
      # #3709, in two parts:
      #
      # 1. Auto-resume first. Every kick rescans the sprint's In Progress
      #    issues for PARKED ones (no open PR, no live developer run) whose
      #    declared dependencies have all closed, and restarts one of them.
      #    This runs before the park decision AND before the continue kick:
      #    a chain gate that just opened is work the loop already owns.
      # 2. Then decide on the WHOLE open population, not just Backlog. A
      #    sprint with an empty Backlog but live In Progress / In Review
      #    work is not complete, and even an all-closed sprint is only
      #    "complete" once every item is accepted to For Release (owner
      #    definition, 2026-08-10, #3709).
      local snapshot park_state state scan selected gate_lines
      snapshot="$(sprint_population_snapshot "$sprint_field" "$active_sprint" 2>/dev/null || echo "")"
      jq -e 'has("open")' <<<"${snapshot:-null}" >/dev/null 2>&1 || snapshot=""

      scan='{}'
      selected=""
      gate_lines=""
      if [[ -n "$snapshot" ]]; then
        scan="$(autopilot_scan_parked "$snapshot" 2>/dev/null || echo '{}')"
        selected="$(jq -r '.selected // empty' <<<"$scan" 2>/dev/null || echo "")"
        if [[ -n "$selected" ]]; then
          if _autopilot_post_resume "$selected"; then
            echo "[review] Autopilot: auto-resumed parked issue #${selected} (all gates closed)." >&2
          else
            _warn "Autopilot: failed to post auto-resume trigger on #${selected}."
            selected=""
          fi
        fi
        gate_lines="$(python3 "${_LIB_DIR}/parked_resume.py" gate-lines <<<"$scan" 2>/dev/null || echo "")"
      fi

      park_state=""
      if [[ -n "$snapshot" ]]; then
        park_state="$(jq -n -c --argjson s "$snapshot" --arg b "$STATUS_BACKLOG" \
          --arg f "${STATUS_FOR_RELEASE:-For Release}" \
          '$s + {status_backlog: $b, status_for_release: $f}' \
          | python3 "${_LIB_DIR}/sprint_calc.py" sprint-park-state 2>/dev/null || echo "")"
      fi

      if [[ -n "$park_state" ]]; then
        state="$(jq -r '.state // empty' <<<"$park_state" 2>/dev/null || echo "")"
        remaining="$(jq -r '.backlog_open // 0' <<<"$park_state" 2>/dev/null || echo 0)"
        decision="$([[ "$state" == "continue" ]] && echo continue || echo complete)"
      else
        # Snapshot unavailable (GraphQL/parse failure): fall back to the
        # pre-#3709 Backlog-only count so a data hiccup degrades to the old
        # behavior rather than parking a sprint that still has work.
        _warn "Autopilot: sprint population snapshot unavailable -- falling back to the Backlog-only count."
        remaining="$(count_sprint_backlog_open "$sprint_field" "$active_sprint" 2>/dev/null || echo "")"
        decision="$(python3 "${_LIB_DIR}/sprint_calc.py" autopilot-decision "${remaining:-0}")"
      fi

      if [[ "$decision" == "continue" ]]; then
        # Audit note first (it is for humans reading the release issue), then
        # the dispatch event that actually starts the work.
        ( issue_comment "$RELEASE_ISSUE_NUMBER" "🔁 **Sprint Autopilot**: ${event_phrase}. Sprint \"${active_sprint}\" has ${remaining} open item(s) -- ${continue_phrase}.

**Parked-issue scan (auto-resume, #3709):**

${gate_lines}

${AUTOPILOT_INFO_MARKER}" ) >/dev/null 2>&1 || _warn "Autopilot: could not post the continue note."

        dispatch_next_issue "autopilot: ${event_phrase}" \
          && echo "[review] Autopilot: dispatched next-issue selection (sprint ${active_sprint} has ${remaining} remaining)." >&2 \
          || _warn "Autopilot: failed to dispatch next-issue selection."
      else
        echo "[review] Autopilot: sprint ${active_sprint} has no dispatchable Backlog work (park state: ${state:-unknown}) -- parked, no kick." >&2
        # #3729: a park that just reached agentic-work-complete is the start
        # of an acceptance round, so cut the candidate that round installs
        # before writing the note -- the note then names it.
        local rc_publish
        rc_publish="$(_autopilot_publish_rc "$park_state" 2>/dev/null || echo '{}')"
        _autopilot_park_note "$event_phrase" "$active_sprint" "$release_version" "$park_state" "$scan" "$rc_publish"
      fi
    fi
  fi
  return 0
}

# Posts the sprint-boundary park note (#3706) on the release tracking issue:
# what completed, that the loop is parked at the sprint boundary, what is
# still queued in the release per future sprint, and how work resumes.
# Best effort like its caller -- a failure to gather the breakdown degrades
# to a note without one rather than skipping the note entirely.
_autopilot_park_note() {
  local event_phrase="$1" active_sprint="$2" release_version="$3"
  local park_state="${4:-}" resume_scan="${5:-}" rc_publish="${6:-}"
  local by_sprint body
  by_sprint="$(release_backlog_by_sprint "$release_version" "${SPRINT_FIELD:-Sprint}" 2>/dev/null || echo "")"
  [[ -n "$by_sprint" ]] || by_sprint="{}"
  # An empty/failed park state renders the neutral "parked" wording rather
  # than any completion claim (#3709) -- never announce a state the data
  # doesn't support.
  jq -e . <<<"${park_state:-null}" >/dev/null 2>&1 || park_state="{}"
  jq -e . <<<"${resume_scan:-null}" >/dev/null 2>&1 || resume_scan="{}"
  # What the rc dispatch did (#3729), so the note names the candidate this
  # acceptance round installs. `{}` renders no candidate section at all.
  jq -e . <<<"${rc_publish:-null}" >/dev/null 2>&1 || rc_publish="{}"

  body="$(jq -n --arg e "$event_phrase" --arg s "$active_sprint" --arg v "$release_version" \
    --argjson b "$by_sprint" --argjson p "$park_state" --argjson r "$resume_scan" \
    --argjson c "$rc_publish" \
    '{event_phrase:$e, sprint_title:$s, release_version:$v, by_sprint:$b, park_state:$p, resume_scan:$r, rc_publish:$c}' \
    | python3 "${_LIB_DIR}/sprint_calc.py" park-note)"

  issue_comment "$RELEASE_ISSUE_NUMBER" "$body" \
    || _warn "Autopilot: failed to post sprint-complete park note."
}

# -------------------------
# Human-channel escalation notifications (Slack DM, #3695)
# -------------------------
# On 2026-08-09 a Phase 3 self-heal FATAL diagnosis on #3513 sat unread in a
# GitHub issue thread for ~8 hours while the pipeline burned ~50 redundant
# retries -- comments alone don't reach a human who isn't actively watching.
# This sends a DM to HUMAN_OWNER's Slack account for terminal agent outcomes
# (review-agent 3-cycle escalation, self-heal FATAL/FIXED_REQUIRES_MERGE,
# retry-budget exhaustion (#3689), scrummaster queue-blocked/pause reports).
#
# Owner decision 2026-08-09: reuse this repo's existing SLACK_BOT_TOKEN +
# SLACK_USER_ID Actions secrets (SLACK_BOT_TOKEN already wired for
# notify-merge-conflicts.yml; SLACK_USER_ID is the owner's Slack member id
# for the DM channel param) -- no new secrets, no provider choice made by
# this code. Every caller's GitHub-comment escalation path runs first and
# unconditionally; this is purely additive and must never block or fail
# that path, so every failure mode here (missing secrets, curl error,
# non-ok Slack response) is a `_warn` + `return 0`, never a propagated
# failure.
_SLACK_NOTIFY_MARKER_PREFIX="<!-- slack-notify:"

# True if a dedup marker for `dedup_key` was posted on `issue`'s comments
# within `window_minutes` minutes (default 60). Prevents the same terminal
# state re-firing a Slack DM on every retry/re-check of a stuck loop
# (acceptance criterion: de-duplicated within a window). Best effort: a
# lookup failure is treated as "not a duplicate" so a transient API/parse
# error never permanently silences the channel.
_slack_notify_recent() {
  local issue="$1" dedup_key="$2" window_minutes="${3:-60}"
  require_cmd jq
  local marker="${_SLACK_NOTIFY_MARKER_PREFIX}${dedup_key} -->"
  local cutoff
  cutoff="$(python3 -c "
import datetime, sys
print((datetime.datetime.utcnow() - datetime.timedelta(minutes=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))
" "$window_minutes" 2>/dev/null)"
  [[ -n "$cutoff" ]] || return 1

  # --paginate emits one JSON array per page -- slurp (-s) and flatten one
  # level before filtering so the window check runs over the full combined
  # comment history, not per-page (see AGENTS.md's --paginate note).
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/comments" --paginate 2>/dev/null \
    | jq -s --arg marker "$marker" --arg cutoff "$cutoff" \
      '[.[][] | select(.body | contains($marker)) | select(.created_at >= $cutoff)] | length > 0' 2>/dev/null \
    | grep -q true
}

# Sends a Slack DM to HUMAN_OWNER for a terminal agent outcome. Args:
#   $1 issue      issue/PR number the escalation is about
#   $2 state      short terminal-state label (e.g. "FATAL", "review-escalation")
#   $3 diagnosis  one-line diagnosis
#   $4 action     recommended human action (e.g. "merge <sha> to v3.0.0")
#   $5 dedup_key  stable key identifying this issue+step
#                 (default: "${issue}:${state}")
#   $6 window_minutes  dedup window in minutes (default: 60)
#
# Absent SLACK_BOT_TOKEN/SLACK_USER_ID degrades gracefully to comment-only
# behavior (no-op here; the caller's own GitHub comment already carries the
# escalation). Always returns 0 -- see header comment above.
notify_human_escalation() {
  local issue="$1" state="$2" diagnosis="$3" action="$4"
  local dedup_key="${5:-${issue}:${state}}"
  local window_minutes="${6:-60}"
  require_cmd jq

  if [[ -z "${SLACK_BOT_TOKEN:-}" || -z "${SLACK_USER_ID:-}" ]]; then
    _warn "notify_human_escalation: SLACK_BOT_TOKEN/SLACK_USER_ID not configured -- skipping Slack DM (comment-only fallback stands)."
    return 0
  fi

  if _slack_notify_recent "$issue" "$dedup_key" "$window_minutes"; then
    _debug "notify_human_escalation: recent notification found for '${dedup_key}' -- skipping duplicate."
    return 0
  fi

  local issue_url text payload response ok
  issue_url="https://github.com/${REPO_OWNER}/${REPO_NAME}/issues/${issue}"
  text="$(printf ':rotating_light: *%s* on <%s|#%s>\n*Diagnosis:* %s\n*Recommended action:* %s' \
    "$state" "$issue_url" "$issue" "$diagnosis" "$action")"
  payload="$(jq -n --arg channel "$SLACK_USER_ID" --arg text "$text" '{channel: $channel, text: $text}')"

  response="$(curl -sS -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
    -H "Content-Type: application/json; charset=utf-8" \
    -d "$payload" 2>/dev/null)" || response=""
  ok="$(echo "$response" | jq -r '.ok // false' 2>/dev/null)"

  if [[ "$ok" != "true" ]]; then
    _warn "notify_human_escalation: Slack API call failed for issue #${issue} (response=${response:-<empty>}) -- falling back to comment-only."
    return 0
  fi

  local marker
  marker="${_SLACK_NOTIFY_MARKER_PREFIX}${dedup_key} -->"
  issue_comment "$issue" "$(printf ':envelope: Notified @%s via Slack DM (%s).\n\n%s' "${HUMAN_OWNER:-the human owner}" "$state" "$marker")" \
    || _warn "notify_human_escalation: Slack DM sent but failed to post dedup marker comment on #${issue}."

  return 0
}

# -------------------------
# Unresolved-escalation pause backstop (#3687)
# -------------------------
# "Unresolved escalation" = an open issue currently assigned to
# HUMAN_OWNER, excluding the release tracking issue (RELEASE_ISSUE_NUMBER),
# which is owner-assigned by design for the whole life of a release
# (#3868). Both escalation paths (the review agent's 3-cycle breaker
# and the huddle's type-(c) immediate/spec-ambiguity escalation) end in
# assign_issue_verified(issue, HUMAN_OWNER) on a still-open issue; the
# owner resolving it means reassigning it away or closing it. Purely
# derived from live issue state -- no hidden counter to drift out of sync.
_ESCALATION_PAUSE_MARKER="<!-- escalation-pause-backstop:3687 -->"

# One line per open issue assigned to `owner` (default HUMAN_OWNER):
# "#<number> <title>". Empty output if none, or if no owner is configured.
# The release tracking issue (RELEASE_ISSUE_NUMBER) is excluded: it is
# owner-assigned by design for the whole life of a release, so counting it
# would permanently inflate the escalation count by one and drop the pause
# gate's effective threshold from 2 to 1 (#3868) -- the same exemption the
# drain gate applies (drain_gate_release's release_issue_exempt).
unresolved_escalation_issues() {
  local owner="${1:-${HUMAN_OWNER:-}}"
  require_cmd jq
  if [[ -z "$owner" ]]; then
    _warn "unresolved_escalation_issues: no owner configured (HUMAN_OWNER unset)"
    return 0
  fi
  # Raw fetch and jq filtering are separate commands (rather than gh's
  # own --jq) so tests can stub the `gh` call with canned JSON and let the
  # real jq filter run -- same split as real_label_names above.
  _open_issues_assigned_to "$owner" \
    | jq -r --arg release "${RELEASE_ISSUE_NUMBER:-}" \
      '.[] | select(.pull_request == null)
           | select(($release == "") or ((.number | tostring) != $release))
           | "#\(.number) \(.title)"'
}

# Raw (unfiltered) JSON array of open issues/PRs assigned to `owner`. Split
# out so tests can stub the `gh` call in isolation.
_open_issues_assigned_to() {
  local owner="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues" \
    --method GET \
    -f state=open \
    -f assignee="$owner" \
    --paginate 2>/dev/null || echo "[]"
}

# Number of non-empty lines in `$1`. Always echoes a number and returns 0,
# even when the count is zero (grep -c would otherwise exit 1). Shared by
# count_unresolved_escalations and escalation_pause_gate so the two never
# drift apart.
_count_lines() {
  local n
  n="$(grep -c . <<<"$1")" || true
  echo "${n:-0}"
}

# Count of unresolved_escalation_issues.
count_unresolved_escalations() {
  local owner="${1:-${HUMAN_OWNER:-}}"
  _count_lines "$(unresolved_escalation_issues "$owner")"
}

# Finds the id of our most recent pause-backstop report comment on
# release_issue, if any (empty if none). Split out so tests can stub it
# without a real gh/GraphQL round trip.
#
# `gh api --jq` has no `--arg` support, so the marker filter can't be
# chained onto gh's own --jq -- fetch the raw paginated JSON (one array per
# page) and filter/aggregate in a separate `jq -s` call instead, slurping
# and flattening one level before sort_by/last so the aggregation runs over
# every page combined, not per-page (see AGENTS.md's --paginate note).
_escalation_pause_comment_id() {
  local release_issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${release_issue}/comments" --paginate 2>/dev/null \
    | jq -s --arg marker "$_ESCALATION_PAUSE_MARKER" \
      '[.[][] | select(.body | contains($marker))] | sort_by(.created_at) | last | .id // empty' \
    || true
}

# Dispatch gate for the unresolved-escalation pause backstop. Dispatch
# continues unconditionally through the first unresolved escalation (one
# escalated item is normal traffic); with >=2 unresolved, new dispatch
# pauses and this posts (or updates, if already posted) a loud report on
# RELEASE_ISSUE_NUMBER listing the escalated issues. It resumes
# automatically -- there is no separate "resume" action, the gate simply
# re-evaluates live board state on every call and reopens once the count
# drops below 2 (updating the report comment to say so).
#
# Returns 0 if dispatch may proceed, 1 if dispatch is paused. Best-effort
# on the reporting side: a comment failure is warned, not fatal -- the
# pause/resume decision itself always reflects the live count.
escalation_pause_gate() {
  local owner="${HUMAN_OWNER:-}" release_issue="${RELEASE_ISSUE_NUMBER:-}"
  local issues count comment_id body

  issues="$(unresolved_escalation_issues "$owner")"
  count="$(_count_lines "$issues")"

  if [[ -z "$release_issue" ]]; then
    if [[ "$count" -ge 2 ]]; then
      _warn "escalation_pause_gate: ${count} unresolved escalations but RELEASE_ISSUE_NUMBER is not configured -- cannot report, gate stays open."
    fi
    return 0
  fi

  comment_id="$(_escalation_pause_comment_id "$release_issue")"

  if [[ "$count" -ge 2 ]]; then
    body="$(printf '⏸️ **Scrummaster**: dispatch paused -- %s unresolved escalations\n\n%s\n\nDispatch resumes automatically once the unresolved count drops below 2 (no action needed beyond resolving the escalations below).\n\n%s' \
      "$count" "$issues" "$_ESCALATION_PAUSE_MARKER")"
    if [[ -n "$comment_id" ]]; then
      gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/comments/${comment_id}" -f "body=${body}" >/dev/null 2>&1 \
        || _warn "escalation_pause_gate: failed to update pause report on #${release_issue}"
    else
      issue_comment "$release_issue" "$body" \
        || _warn "escalation_pause_gate: failed to post pause report on #${release_issue}"
    fi
    return 1
  fi

  if [[ -n "$comment_id" ]]; then
    body="$(printf '▶️ **Scrummaster**: dispatch resumed -- unresolved escalations dropped below 2\n\n%s' "$_ESCALATION_PAUSE_MARKER")"
    gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/comments/${comment_id}" -f "body=${body}" >/dev/null 2>&1 \
      || _warn "escalation_pause_gate: failed to clear pause report on #${release_issue}"
  fi
  return 0
}

# -------------------------
# Cross-issue infrastructure-anomaly collapse (#3694)
# -------------------------
# The 2026-08-09 postmortem (product_management/AGENTIC_SDLC_DESIGN.md §9;
# issue #3694's "Problem / Motivation" carries the same account): a single
# infra fault (`gh api search/issues` failing deterministically in
# the "Check if PR already exists" step) failed the same step on 5 in-flight
# issues, and each ran its own independent Phase 1-3 self-heal diagnosis --
# ~45-50 Claude invocations for one fault. The same step failing on
# different issues within a short window is one infrastructure event, not N
# coding problems.
#
# The "run history" signal this derives from is the trail of failure
# comments developer_auto_implement.yml already posts on every failed run --
# plain issue-comment REST calls, deliberately NOT `gh api search/issues`
# (the endpoint that caused the incident), so detection itself can't be
# taken out by the same class of fault. The single tracking record is a
# marker comment on RELEASE_ISSUE_NUMBER, re-derived fresh from the live
# comment thread on every check -- same level-triggered shape as
# escalation_pause_gate above, no hidden counter to drift out of sync. It
# self-expires after CROSS_ISSUE_ANOMALY_WINDOW_MINUTES and can be cleared
# early by an OWNER-authored `RESOLVE_ANOMALY` comment.
CROSS_ISSUE_ANOMALY_WINDOW_MINUTES="${CROSS_ISSUE_ANOMALY_WINDOW_MINUTES:-60}"
_CROSS_ISSUE_ANOMALY_PAUSE_MARKER="<!-- cross-issue-anomaly-pause:3694 -->"

# Chronological (body, author_association, created_at) triples for
# `release_issue`'s comment thread, as the compact JSON array
# cross_issue_anomaly.py expects. Split out so tests can stub the `gh` call
# in isolation, mirroring _escalation_pause_comment_id's fetch above.
_release_issue_comments_json() {
  local release_issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${release_issue}/comments" --paginate \
    --jq '[.[] | {id: .id, body: .body, author_association: .author_association, created_at: .created_at}]' \
    | jq -s 'add // []'
}

# Cross-issue anomaly decision for (issue, failed_step): echoes the JSON
# `cross_issue_anomaly.py decide` produces --
# {"action":"skip"|"open"|"proceed", "origin_issue":N, ...}. "skip" means a
# DIFFERENT issue already opened a matching, unresolved, in-window tracking
# record for this step -- the caller should skip its own diagnosis and link
# to origin_issue instead. Best-effort: any gh/jq/python failure degrades to
# "open" (treat this issue as a fresh, standalone occurrence) so a
# detection flake costs one extra one-off diagnosis, never a stuck run.
cross_issue_anomaly_decision() {
  local release_issue="$1" issue="$2" failed_step="$3" now_epoch="$4"
  require_cmd jq; require_cmd python3
  local fallback="{\"action\":\"open\",\"origin_issue\":${issue}}"
  if [[ -z "$release_issue" ]]; then
    echo "$fallback"
    return 0
  fi
  local comments_json
  comments_json="$(_release_issue_comments_json "$release_issue" 2>/dev/null)" || comments_json="[]"
  echo "$comments_json" \
    | python3 "${_LIB_DIR}/cross_issue_anomaly.py" decide "$issue" "$failed_step" "$now_epoch" "$CROSS_ISSUE_ANOMALY_WINDOW_MINUTES" \
    2>/dev/null || echo "$fallback"
}

# Posts (or reuses) the single tracking-record marker comment for a newly-
# opened anomaly -- "one diagnosis performed once, referenced by every
# affected issue" (#3694). Called once, by the issue that `decide` returned
# action="open" for. Idempotent in effect: a second call for the same
# (step, still-open) pair just posts a fresh marker that `decide` will treat
# as the newer of two markers for that step -- harmless, but callers should
# still gate this on decide's actual "open" action to avoid the extra
# comment. Best-effort: a post failure is warned, not fatal.
open_cross_issue_anomaly() {
  local release_issue="$1" origin_issue="$2" failed_step="$3" now_epoch="$4" run_url="$5"
  [[ -z "$release_issue" ]] && return 0
  require_cmd python3
  local marker body
  marker="$(python3 "${_LIB_DIR}/cross_issue_anomaly.py" marker "$failed_step" "$origin_issue" "$now_epoch")"
  body="$(printf '🚨 **Cross-issue infrastructure anomaly detected** (#3694)\n\nStep `%s` failed on #%s and is being diagnosed now. If the same step fails on another issue within %s minutes, that issue will link here instead of repeating the diagnosis.\n\n[Failed run](%s)\n\nComment `RESOLVE_ANOMALY` (as the repo owner) once the underlying infrastructure fault is fixed, to close this out early.\n\n%s' \
    "$failed_step" "$origin_issue" "$CROSS_ISSUE_ANOMALY_WINDOW_MINUTES" "$run_url" "$marker")"
  issue_comment "$release_issue" "$body" \
    || _warn "open_cross_issue_anomaly: failed to post tracking record on #${release_issue}"
}

# Dispatch gate composing with escalation_pause_gate (#3687): dispatch
# pauses while ANY step currently has an open, unresolved cross-issue
# anomaly marker on RELEASE_ISSUE_NUMBER (cleared by an OWNER
# `RESOLVE_ANOMALY` comment, or by the detection window elapsing). Returns 0
# if dispatch may proceed, 1 if paused; posts/updates a loud report exactly
# like escalation_pause_gate.
cross_issue_anomaly_pause_gate() {
  local release_issue="${RELEASE_ISSUE_NUMBER:-}"
  [[ -z "$release_issue" ]] && return 0
  require_cmd jq; require_cmd python3

  local comments_json is_open comment_id body
  comments_json="$(_release_issue_comments_json "$release_issue" 2>/dev/null)" || comments_json="[]"
  is_open="$(echo "$comments_json" \
    | python3 "${_LIB_DIR}/cross_issue_anomaly.py" any-open "$(date +%s)" "$CROSS_ISSUE_ANOMALY_WINDOW_MINUTES" \
    2>/dev/null)" || is_open="false"

  comment_id="$(echo "$comments_json" \
    | jq -r --arg marker "$_CROSS_ISSUE_ANOMALY_PAUSE_MARKER" \
      '[.[] | select(.body | contains($marker))] | sort_by(.created_at) | last | .id // empty' 2>/dev/null || true)"

  if [[ "$is_open" == "true" ]]; then
    body="$(printf '⏸️ **Scrummaster**: dispatch paused -- an unresolved cross-issue infrastructure anomaly is open\n\nSee the anomaly tracking comment above for the affected step and originating issue. Comment `RESOLVE_ANOMALY` (as the repo owner) once the infrastructure fault is fixed, or wait for the %s-minute detection window to elapse.\n\n%s' \
      "$CROSS_ISSUE_ANOMALY_WINDOW_MINUTES" "$_CROSS_ISSUE_ANOMALY_PAUSE_MARKER")"
    if [[ -n "$comment_id" ]]; then
      gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/comments/${comment_id}" -f "body=${body}" >/dev/null 2>&1 \
        || _warn "cross_issue_anomaly_pause_gate: failed to update pause report on #${release_issue}"
    else
      issue_comment "$release_issue" "$body" \
        || _warn "cross_issue_anomaly_pause_gate: failed to post pause report on #${release_issue}"
    fi
    return 1
  fi

  if [[ -n "$comment_id" ]]; then
    body="$(printf '▶️ **Scrummaster**: dispatch resumed -- cross-issue infrastructure anomaly cleared\n\n%s' "$_CROSS_ISSUE_ANOMALY_PAUSE_MARKER")"
    gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/comments/${comment_id}" -f "body=${body}" >/dev/null 2>&1 \
      || _warn "cross_issue_anomaly_pause_gate: failed to clear pause report on #${release_issue}"
  fi
  return 0
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
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" \
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

# -------------------------
# Native issue-dependency helpers (blocked_by), shared by
# promote_accepted_features.sh (heals the failure->feature blocking link)
# and the merged-but-blocked park/sweep flow (#3631).
# -------------------------

# All issue numbers listed in `issue`'s native blocked_by dependencies
# (regardless of state), one per line. Empty output means no blockers.
blocked_by_issues() {
  local issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/dependencies/blocked_by" \
    --jq '.[].number' 2>/dev/null || true
}

# Issue numbers among `issue`'s blocked_by dependencies that are still open
# (REST state=="open"), one per line. Empty output means no open blockers.
open_blocked_by_issues() {
  local issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/dependencies/blocked_by" \
    --jq '.[] | select(.state=="open") | .number' 2>/dev/null || true
}

# -------------------------
# A PR's issue is a link, not a sentence (owner rule, 2026-08-19)
# -------------------------
# GitHub already knows which issue a PR closes: the "Development" sidebar
# link, and the `closingIssuesReferences` edge behind it, which is what
# actually closes the issue on merge. Every consumer here used to re-derive
# that by grepping `Closes #N` out of the PR body -- prose standing in for a
# relationship the platform stores natively, which is the same mistake as
# driving workflows from comment tokens (#3882) and as the retired
# `Related feature: #N` convention (D-002).
#
# The practical cost was a hard failure on PRs whose issue was linked but
# whose body did not spell it out: "PR #N body does not contain 'Closes #N'"
# stopped the merge automation dead on #3921, #3927, #3929 and #3933.
#
# Reads the native edge first, then falls back to the body convention for
# PRs opened before this existed. Prints nothing when the PR closes no issue
# -- which is a legitimate state (a machinery PR with no issue behind it),
# and the caller decides what that means.
pr_linked_issue() {
  local pr="$1" q num

  q='query($owner:String!, $name:String!, $pr:Int!){
    repository(owner:$owner, name:$name){
      pullRequest(number:$pr){
        closingIssuesReferences(first:5){ nodes { number } }
      }
    }
  }'
  num="$(graphql "$q" -f owner="$REPO_OWNER" -f name="$REPO_NAME" -F pr="$pr" 2>/dev/null \
    | jq -r '.data.repository.pullRequest.closingIssuesReferences.nodes[0].number // empty' 2>/dev/null)"
  if [[ -n "$num" && "$num" != "null" ]]; then
    echo "$num"
    return 0
  fi

  # Fallback: the body convention. Still read, never required.
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${pr}" --jq '.body // ""' 2>/dev/null \
    | grep -oiE 'closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true
}

# -------------------------
# Native relationships as the ONLY relationship storage (#3731)
# -------------------------
# Owner decision 2026-08-12: the link between an issue filed during
# acceptance testing and the issue it was filed against is a native
# blocked-by/blocks relationship. The retired `Related feature: #N` body
# marker is read as a fallback for historical issues only -- nothing writes
# it any more. Graph math lives in lib/issue_relationships.py.

# All issue numbers `issue` BLOCKS (native `blocking` dependencies), one per
# line. For a handler-filed acceptance failure / improvement this is the
# feature it was filed against.
blocking_issues() {
  local issue="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/dependencies/blocking" \
    --jq '.[].number' 2>/dev/null || true
}

# Marks `blocker` as blocking `issue` (i.e. adds `issue` <- blocked_by
# <- `blocker`). Idempotent: a link that already exists is reported as
# success, since the API rejects the duplicate and there is nothing to do.
# Returns non-zero only when the link genuinely could not be established --
# callers treat that as best-effort and let the promotion sweep heal it.
mark_issue_blocked_by() {
  local issue="$1" blocker="$2" existing blocker_dbid
  existing="$(blocked_by_issues "$issue" | tr '\n' ',')"
  if [[ ",${existing}" == *",${blocker},"* ]]; then
    return 0
  fi
  blocker_dbid="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${blocker}" --jq '.id' 2>/dev/null || true)"
  [[ -n "$blocker_dbid" ]] || return 1
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}/dependencies/blocked_by" \
    -F issue_id="$blocker_dbid" >/dev/null 2>&1
}

# Walks a native dependency direction transitively from `issue` and prints
# every reachable issue number, ascending, excluding `issue` itself.
# `direction` is "blocked_by" (everything transitively blocking `issue` --
# its full acceptance gate) or "blocking" (everything `issue` transitively
# blocks). Edges are collected here; the closure/cycle safety is
# issue_relationships.py's job.
_transitive_dependency_issues() {
  local issue="$1" direction="$2"
  require_cmd jq
  require_cmd python3
  local -a frontier=("$issue")
  local -A visited=()
  local edges_file node neighbour
  edges_file="$(mktemp)"
  : > "$edges_file"

  while [[ "${#frontier[@]}" -gt 0 ]]; do
    node="${frontier[0]}"
    frontier=("${frontier[@]:1}")
    [[ -n "${visited[$node]:-}" ]] && continue
    visited["$node"]=1
    local -a neighbours=()
    while IFS= read -r neighbour; do
      [[ -n "$neighbour" ]] || continue
      neighbours+=("$neighbour")
      [[ -n "${visited[$neighbour]:-}" ]] || frontier+=("$neighbour")
    done < <(if [[ "$direction" == "blocking" ]]; then blocking_issues "$node"; else blocked_by_issues "$node"; fi)
    printf '%s\n' "${neighbours[@]+"${neighbours[@]}"}" \
      | jq -R -n --argjson k "$node" -c '{key: ($k | tostring), value: [inputs | select(length > 0) | tonumber]}' \
      >> "$edges_file"
  done

  jq -s -c 'from_entries' "$edges_file" \
    | python3 "${_LIB_DIR}/issue_relationships.py" transitive "$issue"
  rm -f "$edges_file"
}

# Everything transitively blocking `issue`: the complete set that must be
# accepted before `issue` can be promoted.
transitive_blocked_by_issues() {
  _transitive_dependency_issues "$1" "blocked_by"
}

# Everything `issue` transitively blocks -- the "and transitively anything
# blocked by that one" half of the owner's rule.
transitive_blocking_issues() {
  _transitive_dependency_issues "$1" "blocking"
}

# The feature an acceptance-failure / improvement issue was filed against:
# the first native `blocking` edge, falling back to the retired
# `Related feature: #N` body marker for issues filed before #3731. Prints
# nothing when the issue is related to nothing (a plain feature issue).
related_feature_of() {
  local issue="$1" body="${2-}"
  require_cmd jq
  require_cmd python3
  local blocks
  blocks="$(blocking_issues "$issue" | jq -R -n -c '[inputs | select(length > 0) | tonumber]' 2>/dev/null || echo '[]')"
  if [[ -z "${2+set}" ]]; then
    body="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" --jq '.body // ""' 2>/dev/null || echo "")"
  fi
  jq -n -c --argjson b "${blocks:-[]}" --arg body "$body" '{blocks: $b, body: $body}' \
    | python3 "${_LIB_DIR}/issue_relationships.py" resolve-related 2>/dev/null | head -1
}

# Assign issue to developer and trigger workflow
# Forces a new assignment event even if developer already assigned
# by unassigning first, then reassigning
assign_and_trigger_developer() {
  local issue="$1"

  # A closed issue is not workable, and this is where that is decided (#3906).
  # The submit path already refuses one (developer_submit_for_review.sh: "Issue
  # is not OPEN ... Refusing to submit"), but nothing refused to *start* the
  # work, so the two halves disagreed: on #3825 the review agent dispatched a
  # rework cycle to an issue that had closed 26 minutes earlier when its PR
  # merged, the developer implemented and validated a correct fix, and the
  # submit guard then refused it -- identically on every retry. The work was
  # real and mainline-blocking; it had to be re-landed by hand.
  #
  # Refuse here instead, and say what the next step is. A closed issue means
  # the work shipped; follow-up work is a new issue, never a second pass at a
  # finished one. Deliberately not reopening it: a closed issue parked in
  # Acceptance Failed is owner signal (D-008), and nothing here may overwrite
  # that.
  local issue_state
  issue_state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" \
    --jq '.state | ascii_upcase' 2>/dev/null || echo "")"

  if [[ "$issue_state" == "CLOSED" ]]; then
    _warn "Issue #${issue} is CLOSED -- refusing to dispatch the developer agent."
    # Subshell: issue_comment exits on failure, and a failed courtesy
    # comment must not swallow the refusal itself.
    ( issue_comment "$issue" "🚫 **Dispatch refused**: this issue is closed, so work started here could not be submitted for review — the submit script refuses a closed issue and would refuse it identically on every retry (#3825, #3906).

If rework is genuinely needed, it is a **new issue**: a closed issue means the work shipped, and a merged PR cannot carry a second pass. File the follow-up and dispatch that.

<!-- nyxgpt-token-mention -->" ) >/dev/null 2>&1 || true
    return 12
  fi

  # Check if developer is already assigned
  local current_assignee
  current_assignee=$(_issue_assignee_logins "$issue" 2>/dev/null | tr ',' '\n' | grep -x "$DEV_AGENT" || echo "")

  if [[ -n "$current_assignee" ]]; then
    # Developer already assigned - unassign first to force a new 'assigned'
    # event (reassigning the same login fires none).
    #
    # #3882 removed the second trigger path this branch used to carry: a
    # bare retry token posted as a comment, which developer_auto_implement.yml
    # pattern-matched. That was the #3647 answer to one observed miss (a
    # REQUEST_CHANGES redispatch that started nothing) -- and it is the same
    # prose-as-API mechanism that later re-triggered a workflow from its own
    # output ~500 times (#3790). Assignment is the lever; a comment carries
    # findings.
    #
    # What backs the write instead: assign_issue_verified re-reads the
    # assignees and retries until the PATCH has actually landed (a silent
    # no-op PATCH is the failure mode a fire-and-forget call cannot see), the
    # per-issue concurrency group in developer_auto_implement.yml serializes
    # whatever it starts, and review_ensure_handoff.sh (#3704) is the
    # backstop for a handoff that produces no developer run at all.
    _debug "Developer already assigned to #$issue - unassigning first to force event"
    gh issue edit "$issue" --remove-assignee "$DEV_AGENT"
    sleep 1  # Brief pause to ensure event processes
    assign_issue_verified "$issue" "$DEV_AGENT" \
      || _warn "Could not verify the re-assignment of #$issue to @${DEV_AGENT} -- the developer run may not have been dispatched."
    _debug "Reassigned developer to #$issue - workflow will trigger via assignment event"
    return
  fi

  # New assignment - assign developer, which alone fires the 'issues.assigned'
  # event (no fallback comment needed here: the assignee actually changes).
  # Developer agent reads issue history to determine if this is new work or a retry
  issue_assign_only "$issue" "$DEV_AGENT"
  _debug "Assigned developer to #$issue - workflow will trigger via assignment event"
}

# The other end of the same lever (#3882): the developer agent claiming an
# issue it has just been assigned. Assignment IS the dispatch, and the actor
# doing the work owns the status transition -- exactly as a person would put
# a card in progress when they pick it up, rather than requiring a second
# actor to stage the board and then announce it in a comment.
#
# Prints the status to proceed under; returns
#   0  proceed (already In Progress, or claimed into it now)
#   3  refuse  (the caller stops the run; this is a policy stop, not a failure)
#
# What it refuses, and why each one is not an oversight:
#   * a lane that means "held" -- `Acceptance Failed` and `Acceptance Testing`
#     are owner signal (D-001/D-008) and being assigned does not release them;
#     `For Release` is finished work;
#   * an issue that is not on the board at all -- there is no state to claim;
#   * an assigner who is not a permitted identity, so a stray assignment by
#     anyone else leaves the issue exactly as it was.
#
# Lives here rather than inline in developer_auto_implement.yml so the
# decision can be executed against stub state (tests/test_gh_project_lib.sh,
# run on a runner by assignment-dispatch-smoke.yml) instead of only read.
developer_claim_issue() {
  local issue="$1" assigner="${2:-}"
  local status

  status="$(issue_status "$issue")"
  if [[ -z "$status" ]]; then
    echo "::warning::Issue #${issue} not found in project." >&2
    return 3
  fi
  echo "::notice::Issue #${issue} status=${status}" >&2

  if [[ "$status" == "$STATUS_IN_PROGRESS" ]]; then
    echo "$status"
    return 0
  fi

  # Backlog is new work; In Review is rework handed back by a review round,
  # a huddle decision, a conflict round or a human override.
  case "$status" in
    "$STATUS_BACKLOG" | "$STATUS_IN_REVIEW") ;;
    *)
      echo "::warning::Status '${status}' is not claimable by assignment." >&2
      return 3
      ;;
  esac

  case "$assigner" in
    "$HUMAN_OWNER" | "$SCRUM_AGENT" | "$REVIEW_AGENT" | "$DEV_AGENT" | "claude[bot]") ;;
    *)
      echo "::warning::Assigner '${assigner}' is not a permitted dispatcher." >&2
      return 3
      ;;
  esac

  set_issue_status "$issue" "$STATUS_IN_PROGRESS"
  echo "::notice::Claimed #${issue}: ${status} -> ${STATUS_IN_PROGRESS} (assigned by ${assigner})" >&2
  echo "$STATUS_IN_PROGRESS"
}

# Classifies the current claim state of a Backlog issue for
# scrummaster_start_issue.sh's start-guard decision matrix (owner ruling,
# 2026-08-08, #3665). The #3647 guard treated ANY existing assignee as
# "already claimed" and halted the whole dispatch -- but assign_backlog.yml
# deliberately assigns SCRUM_AGENT to every fresh Backlog issue, so that
# guard turned the normal steady state into a permanent, silent,
# head-of-line block (#3593 stalled the sprint loop ~5 days). This
# distinguishes *who* holds the claim instead of halting on any assignee.
# Prints exactly one of:
#
#   claimable   -- unassigned, or assigned only to SCRUM_AGENT (the normal
#                  Backlog-ownership state). Caller proceeds with the start.
#   duplicate   -- DEV_AGENT is among the assignees: a concurrent/earlier
#                  dispatch already started this issue (the #3647 burst-race
#                  pattern, a known/benign playbook). Not a block -- skip
#                  quietly, try the next candidate.
#   human_hold  -- assigned only to HUMAN_OWNER: a deliberate hold. No
#                  process may reassign it. Skip quietly, try the next
#                  candidate.
#   anomaly     -- any other assignee combination (unrecognized -- e.g. the
#                  review agent while still Backlog). Caller must report
#                  loudly (never silently reassign) and try the next
#                  candidate.
#   closed      -- issue is no longer OPEN. Skip quietly.
#   drain_gate_held -- the issue sits in the `Acceptance Failed` holding
#                  lane (#3730): the owner is still testing this
#                  acceptance round. Skip quietly; the drain watcher moves
#                  it to Backlog when the round drains.
classify_backlog_claim_state() {
  local issue="$1"
  local state assignees
  state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" --jq '.state | ascii_upcase' 2>/dev/null || echo "UNKNOWN")"
  if [[ "$state" != "OPEN" ]]; then
    echo "closed"
    return 0
  fi

  # Drain gate (#3730), enforced at the start point rather than trusted to
  # the Backlog filter alone: a held issue is never started, whatever
  # selected it. The gate opening is what moves an item OUT of this lane
  # (drain_gate_release), so anything still in it is still gated -- no
  # second gate-state lookup is needed here, and a costly project-wide
  # page-through is avoided on every dispatch.
  local held_lane status
  held_lane="${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}"
  status="$(issue_status "$issue" 2>/dev/null || echo "")"
  if [[ "$status" == "$held_lane" ]]; then
    echo "drain_gate_held"
    return 0
  fi

  assignees="$(_issue_assignee_logins "$issue" 2>/dev/null || echo "")"

  if [[ -z "$assignees" || "$assignees" == "$SCRUM_AGENT" ]]; then
    echo "claimable"
    return 0
  fi

  if [[ ",${assignees}," == *",${DEV_AGENT},"* ]]; then
    echo "duplicate"
    return 0
  fi

  if [[ -n "${HUMAN_OWNER:-}" && "$assignees" == "$HUMAN_OWNER" ]]; then
    echo "human_hold"
    return 0
  fi

  echo "anomaly"
  return 0
}

# Executes the #3665 start-guard decision matrix for one Backlog candidate:
# classifies its claim state via classify_backlog_claim_state, then either
# performs the start (history-correct for a freshly unassigned issue --
# assign scrummaster, assign developer, then Status -> In Progress -- or the
# normal order for an already scrummaster-assigned issue) or skips per the
# matrix. Split out of scrummaster_start_issue.sh so both the script and
# scrummaster_dispatch_next.sh's fall-through loop share one implementation,
# and so it can be unit-tested by stubbing the individual primitives below
# instead of mocking `gh` end to end.
#
# Prints a human-readable status line plus, on any skip, a machine-parseable
# "SKIPPED #<issue> reason=<reason>[ assignee=<logins>]" line. Returns:
#   0   started
#   10  skipped quietly (duplicate in-flight start, a deliberate human hold,
#       or the issue is no longer open) -- not a block
#   11  skipped loudly (unrecognized assignee) -- a comment was posted on
#       the issue
scrummaster_attempt_start() {
  local issue="$1"
  local claim_state assignees

  claim_state="$(classify_backlog_claim_state "$issue")"

  case "$claim_state" in
    closed)
      echo "SKIPPED #$issue reason=closed"
      echo "Skipped issue #$issue -- not OPEN, no start."
      return 10
      ;;
    duplicate)
      echo "SKIPPED #$issue reason=duplicate"
      echo "Skipped issue #$issue -- @${DEV_AGENT} already assigned (a concurrent/earlier dispatch already started it). Not a block; moving on."
      return 10
      ;;
    drain_gate_held)
      echo "SKIPPED #$issue reason=drain_gate_held"
      echo "Skipped issue #$issue -- held in '${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}' while the acceptance round is still under test (#3730). Released automatically when ${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing} drains; moving on."
      return 10
      ;;
    human_hold)
      echo "SKIPPED #$issue reason=human_hold"
      echo "Skipped issue #$issue -- held by @${HUMAN_OWNER:-the human owner}. Deliberate hold; moving on."
      return 10
      ;;
    anomaly)
      assignees="$(_issue_assignee_logins "$issue" 2>/dev/null || echo "")"
      issue_comment "$issue" "⚠️ **Scrummaster Agent**: Backlog issue #${issue} has an unexpected assignee (\`${assignees}\`) -- expected unassigned, @${SCRUM_AGENT}, or @${HUMAN_OWNER:-the human owner}. Skipping and moving to the next candidate; please inspect the assignment history and resolve manually. (No process reassigns this automatically.)"
      echo "SKIPPED #$issue reason=anomaly assignee=${assignees}"
      echo "Skipped issue #$issue -- unrecognized assignee(s): ${assignees}. Reported on the issue."
      return 11
      ;;
    claimable)
      assignees="$(_issue_assignee_logins "$issue" 2>/dev/null || echo "")"
      if [[ -z "$assignees" ]]; then
        # Unassigned Backlog is treated as belonging to the scrummaster
        # (owner ruling 2026-08-08): keep the assignment history correct by
        # assigning the scrummaster first so the timeline reads identically
        # to an issue that was stamped by assign_backlog.yml at creation,
        # then continue with the normal history-correct sequence: dev
        # assignment, then Status -> In Progress.
        assign_issue_verified "$issue" "$SCRUM_AGENT"
        assign_and_trigger_developer "$issue"
        set_issue_status "$issue" "$STATUS_IN_PROGRESS"
      else
        set_issue_status "$issue" "$STATUS_IN_PROGRESS"
        assign_and_trigger_developer "$issue"
      fi
      issue_comment "$issue" "@${DEV_AGENT} selected as next work item by @${SCRUM_AGENT}. Status -> ${STATUS_IN_PROGRESS}."
      echo "STARTED #$issue"
      echo "Started issue #$issue -> ${STATUS_IN_PROGRESS}, assignee: $DEV_AGENT"
      return 0
      ;;
    *)
      echo "[error] classify_backlog_claim_state returned unexpected state '${claim_state}' for issue #${issue}" >&2
      return 1
      ;;
  esac
}

# -------------------------
# Branch hygiene helpers (shared by developer_create_branch.sh and
# reconcile_dead_branches.sh, #3392)
# -------------------------

# Prints the headRefName of every OPEN pull request, one per line. These are
# always protected from any branch-deletion sweep.
open_pr_head_branches() {
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls?state=open&per_page=100" \
    --paginate --jq '.[].head.ref'
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
  # --paginate emits one JSON array per page (not merged) -- slurp (-s) all
  # pages into an outer array and flatten one level (`.[][]`) before
  # counting, so `length` reflects the true total (see AGENTS.md).
  count="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls?head=${REPO_OWNER}:${branch}&state=closed&per_page=100" \
      --paginate 2>/dev/null \
    | jq -s --arg base "$base_branch" '[.[][] | select(.merged_at == null and .base.ref == $base)] | length')"
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
  issue_state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}" --jq '.state | ascii_upcase' 2>/dev/null || echo "")"
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
# Gate 1 alone is not sufficient: if the REST pulls-list call behind it fails
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

# -------------------------
# PR lane invariant (#3742, owner decision 2026-08-12/2026-08-13)
# -------------------------
# ensure_pr_project_hygiene() puts a PR card into STATUS_IN_REVIEW at
# creation; nothing used to take it back out, so merged and closed PRs
# accumulated in the owner's acceptance queue (13 + 3 swept by hand on
# 2026-08-10, 10 more on 2026-08-13). The invariant is now agent-side:
# every path that merges or closes a PR stamps that PR's project item to
# STATUS_CLOSED itself. The board's built-in "Pull request merged"
# automation is deliberately NOT relied on -- it is GitHub-proprietary (the
# agent system must stay portable to non-GitHub deployments), it is not
# retroactive, and it was observed enabled while the debris kept piling up.

# Project item id for PR `pr_number` in this project, adding the PR to the
# project if it has no item yet. Empty output + non-zero means the item
# could not be resolved or created.
#
# Not ensure_issue_in_project(): that scan matches only `... on Issue`
# content, so a PR (content typename PullRequest) never matches and it falls
# through to the add path on every call, resolving the PR's *issue* node id.
# Here the item is read straight off the PR, and the add fallback uses the
# PullRequest node id.
pr_project_item_id() {
  require_cmd jq
  local pr_number="$1"
  local project_id item_id content_id

  project_id="$(get_project_id)"

  local q_find='query($owner:String!, $name:String!, $num:Int!){
    repository(owner:$owner, name:$name){
      pullRequest(number:$num){
        projectItems(first:20){ nodes { id project { id } } }
      }
    }
  }'
  # Response into a variable, not a pipeline (#3811, same class as
  # project_field_value): a swallowed read reads as "this PR is not on the
  # board yet" and sends the function down the add path, so a failed lookup
  # would answer a question it never got an answer to.
  local find_resp
  find_resp="$(graphql "$q_find" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$pr_number")" || return 1
  item_id="$(echo "$find_resp" \
    | jq -r --arg p "$project_id" '
        .data.repository.pullRequest.projectItems.nodes[]?
        | select(.project.id == $p)
        | .id
      ' | head -n 1)"

  if [[ -n "$item_id" && "$item_id" != "null" ]]; then
    echo "$item_id"
    return 0
  fi

  content_id="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${pr_number}" --jq '.node_id' 2>/dev/null || true)"
  [[ -n "$content_id" && "$content_id" != "null" ]] || return 1

  local q_add='mutation($project:ID!, $content:ID!){
    addProjectV2ItemById(input:{ projectId:$project, contentId:$content }){
      item { id }
    }
  }'
  local add_resp
  add_resp="$(graphql "$q_add" -F project="$project_id" -F content="$content_id")" || return 1
  item_id="$(echo "$add_resp" | jq -r '.data.addProjectV2ItemById.item.id // empty')"
  [[ -n "$item_id" ]] || return 1
  echo "$item_id"
}

# Current project Status of PR `pr_number` (empty if the PR has no item or
# no Status set). The read half of the lane invariant -- used by the sweep
# and by tests asserting no closed PR is left in an active lane.
pr_status() {
  require_cmd jq
  local pr_number="$1" project_id
  project_id="$(get_project_id)"

  local q='query($owner:String!, $name:String!, $num:Int!){
    repository(owner:$owner, name:$name){
      pullRequest(number:$num){
        projectItems(first:20){
          nodes{
            project { id }
            fieldValues(first:50){
              nodes{
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field { ... on ProjectV2SingleSelectField { name } }
                  name
                }
              }
            }
          }
        }
      }
    }
  }'
  # Response into a variable, not a pipeline (#3811, same class as
  # project_field_value): a swallowed read reads as "this PR has no Status",
  # and the lane sweep would then stamp a lane the PR may already be in --
  # or move a card off a correct one -- on the strength of a failed API call.
  local resp
  resp="$(graphql "$q" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$pr_number")" || return 1
  echo "$resp" \
    | jq -r --arg p "$project_id" --arg f "$STATUS_FIELD" '
        .data.repository.pullRequest.projectItems.nodes[]?
        | select(.project.id == $p)
        | .fieldValues.nodes[]?
        | select(.field.name == $f)
        | .name // empty
      ' | head -n 1
}

# Sets PR `pr_number`'s project Status to `status`, with the same retry
# treatment ensure_pr_project_hygiene uses on the way in.
set_pr_status() {
  local pr_number="$1" status="$2"
  local item_id
  item_id="$(pr_project_item_id "$pr_number")" || true
  if [[ -z "$item_id" || "$item_id" == "null" ]]; then
    _warn "Could not resolve a project item for PR #${pr_number} -- Status '${status}' not set."
    return 1
  fi
  set_field_with_retry "$item_id" "$STATUS_FIELD" "$status"
}

# The lane invariant itself: move a merged/closed PR's card to
# STATUS_CLOSED. Idempotent -- re-stamping an already-Closed card is a
# no-op write, so the merge flow, the pull_request:closed handler and the
# periodic sweep can all run over the same PR safely.
close_pr_project_item() {
  local pr_number="$1"
  local current
  current="$(pr_status "$pr_number" 2>/dev/null || true)"
  if [[ "$current" == "$STATUS_CLOSED" ]]; then
    _debug "PR #${pr_number} project item already '${STATUS_CLOSED}'."
    return 0
  fi
  set_pr_status "$pr_number" "$STATUS_CLOSED"
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
  # Note: the closing link is what matters here; "Closes #N" is one way it
  _debug "Verifying PR #${pr_number} is linked to issue #${issue_number}"
  local linked_pr
  linked_pr="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue_number}" --jq '.pull_request.url // empty')"
  if [[ -z "$linked_pr" ]]; then
    _warn "PR #${pr_number} may not be properly linked to issue #${issue_number} (check 'Closes #${issue_number}' in PR body)"
  fi

  echo "[dev] PR #${pr_number} project hygiene: ✓ Added to project, ✓ Fields copied, ✓ Milestone set" >&2
}

# -------------------------
# Parked-blocked-issue park/sweep (#3631, owner process rule 2026-08-04)
# -------------------------
# A merged issue with open native blocked_by dependencies cannot be
# meaningfully accepted -- its own acceptance criteria may depend on the
# unfinished blockers -- so review_accept_and_merge.sh parks it at
# Status=STATUS_IN_REVIEW (issue closed, not Acceptance Testing) instead of
# handing it straight to the owner. This sweep finds every such parked issue
# and promotes it once ALL of its blockers have completed.

# Issue numbers (one per line) that are currently parked: the project Status
# equals `park_status` (the STATUS_IN_REVIEW value) AND the issue itself is
# CLOSED. A normal issue still awaiting PR review is OPEN + In Review and is
# never matched by this -- only the review_accept_and_merge.sh park
# signature (merged/closed but blocked) is.
list_parked_blocked_issues() {
  local park_status="$1"
  require_cmd jq

  local project_id cursor="" tmp has_next next_cursor
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

    jq -r --arg field "$STATUS_FIELD" --arg status "$park_status" '
      .data.node.items.nodes[]
      | select(.content.__typename == "Issue" and .content.state == "CLOSED")
      | select(
          [.fieldValues.nodes[]
           | select(.__typename == "ProjectV2ItemFieldSingleSelectValue" and .field.name == $field)
           | .name] | index($status) != null
        )
      | .content.number
    ' "$tmp"

    has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
    next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$tmp")"
    [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
    cursor="$next_cursor"
  done

  rm -f "$tmp"
}

# REST state (OPEN/CLOSED, upper-cased to match historical GraphQL semantics)
# of an issue, split out from the sweep so tests can stub it without mocking
# `gh` end-to-end.
_issue_open_state() {
  local n="$1"
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${n}" --jq '.state | ascii_upcase' 2>/dev/null || echo ""
}

# Runs the sweep: lists parked issues (list_parked_blocked_issues), then
# iterates to a fixed point promoting any whose blockers (blocked_by_issues)
# have ALL completed -- "complete" = the blocker is CLOSED and its Status is
# STATUS_ACCEPTANCE_TESTING or STATUS_FOR_RELEASE. A promotion made mid-run
# updates the same in-memory resolved-state cache the completeness check
# reads, so a parked blocker chain (A blocked by B blocked by C) resolves
# transitively in one run instead of one hop per sweep interval. Honors
# DRY_RUN=1 (report only; still simulates promotions in the cache so
# downstream chain links are evaluated as they would really resolve).
#
# Reads: STATUS_IN_REVIEW, STATUS_ACCEPTANCE_TESTING, STATUS_FOR_RELEASE,
# HUMAN_OWNER (from load_config). Prints a final "Promoted N issue(s)." line
# to stdout; all progress detail goes to stderr.
sweep_parked_blocked_issues() {
  require_cmd jq
  local dry_run="${DRY_RUN:-0}"
  local n p b info bstate bstatus blocker_refs
  local iter changed all_complete promoted_count max_iterations

  local -a parked=()
  while IFS= read -r n; do
    [[ -n "$n" ]] && parked+=("$n")
  done < <(list_parked_blocked_issues "$STATUS_IN_REVIEW")

  if [[ "${#parked[@]}" -eq 0 ]]; then
    echo "[sweep-parked] No parked issues found. Nothing to do." >&2
    echo "Promoted 0 issue(s)."
    return 0
  fi

  echo "[sweep-parked] Parked candidates: ${parked[*]}" >&2

  # issue -> "STATE:STATUS", seeded lazily; a promotion made this run
  # updates this cache directly instead of re-querying (avoids relying on
  # read-after-write consistency, and lets same-run chains resolve).
  local -A resolved=()
  local -A promoted_this_run=()

  promoted_count=0
  max_iterations=$((${#parked[@]} + 1))

  for ((iter = 1; iter <= max_iterations; iter++)); do
    changed=0
    for p in "${parked[@]}"; do
      [[ -n "${promoted_this_run[$p]:-}" ]] && continue

      local -a blockers=()
      while IFS= read -r b; do
        [[ -n "$b" ]] && blockers+=("$b")
      done < <(blocked_by_issues "$p")

      all_complete=1
      for b in "${blockers[@]}"; do
        if [[ -z "${resolved[$b]:-}" ]]; then
          bstate="$(_issue_open_state "$b")"
          bstatus="$(issue_status "$b")"
          resolved["$b"]="${bstate}:${bstatus}"
        fi
        info="${resolved[$b]}"
        bstate="${info%%:*}"
        bstatus="${info#*:}"
        if [[ "$bstate" != "CLOSED" ]] || { [[ "$bstatus" != "$STATUS_ACCEPTANCE_TESTING" ]] && [[ "$bstatus" != "$STATUS_FOR_RELEASE" ]]; }; then
          all_complete=0
          break
        fi
      done

      if [[ "$all_complete" == "1" ]]; then
        blocker_refs=""
        if [[ "${#blockers[@]}" -gt 0 ]]; then
          blocker_refs="$(printf '#%s, ' "${blockers[@]}")"
          blocker_refs="${blocker_refs%, }"
        fi

        if [[ "$dry_run" == "1" ]]; then
          echo "[sweep-parked] DRY_RUN: would promote #$p to '${STATUS_ACCEPTANCE_TESTING}', assign @${HUMAN_OWNER} (blockers complete: ${blocker_refs:-none})" >&2
        else
          set_issue_status "$p" "$STATUS_ACCEPTANCE_TESTING" \
            || echo "[sweep-parked] [warn] Failed to set status on #$p to ${STATUS_ACCEPTANCE_TESTING}" >&2
          assign_issue_verified "$p" "$HUMAN_OWNER" \
            || echo "[sweep-parked] [warn] Could not verify #$p assigned to @${HUMAN_OWNER}" >&2
          issue_comment "$p" "✅ **Review Agent**: every blocking dependency (${blocker_refs:-none}) is now merged and complete -- moving this parked issue to **${STATUS_ACCEPTANCE_TESTING}** together with its blocker set (owner process rule, 2026-08-04, #3631) and assigning @${HUMAN_OWNER} for acceptance." \
            || echo "[sweep-parked] [warn] Failed to post promotion comment on #$p" >&2
          echo "[sweep-parked] Promoted #$p to '${STATUS_ACCEPTANCE_TESTING}' (blockers complete: ${blocker_refs:-none})" >&2
        fi

        resolved["$p"]="CLOSED:${STATUS_ACCEPTANCE_TESTING}"
        promoted_this_run["$p"]=1
        promoted_count=$((promoted_count + 1))
        changed=1
      fi
    done
    [[ "$changed" == "1" ]] || break
  done

  echo "[sweep-parked] Done. Promoted ${promoted_count} issue(s)." >&2
  echo "Promoted ${promoted_count} issue(s)."
}
