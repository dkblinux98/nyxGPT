#!/usr/bin/env bash
#
# admin_set_fields.sh — owner tooling: set assignee / milestone / board fields
# on a batch of issues OR pull requests by number.
#
# Companion to bulk_set_status.sh, which covers only the Status field and only
# resolves issues. This tool exists because board cleanup (e.g. the dead-PR
# recipe: assign owner, milestone "Phase X", clear Sprint, Status Closed) needs
# writes that no session-side API surface can perform on PRs (owner decision,
# 2026-08-09).
#
# Env:
#   ITEMS         Comma/space-separated issue/PR numbers (required).
#   ASSIGNEE      Login to add as assignee (optional).
#   MILESTONE     Milestone title to set, resolved to its number (optional).
#   CLEAR_SPRINT  "true" to clear the Sprint iteration field (default false).
#   SPRINT        Iteration title to set ("ACTIVE" = current iteration; optional;
#                 mutually exclusive with CLEAR_SPRINT).
#   STATUS        Status option name to set (optional; validated).
#   DRY_RUN       "true" (default) = report what would change; "false" = apply.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/gh_project.sh"

DRY_RUN="${DRY_RUN:-true}"
ITEMS="${ITEMS:?ITEMS is required (comma/space-separated issue/PR numbers)}"
ASSIGNEE="${ASSIGNEE:-}"
MILESTONE="${MILESTONE:-}"
CLEAR_SPRINT="${CLEAR_SPRINT:-false}"
SPRINT="${SPRINT:-}"
STATUS="${STATUS:-}"
SPRINT_FIELD="${SPRINT_FIELD:-Sprint}"

load_config
require_gh_auth
require_cmd jq

[[ -n "$ASSIGNEE" || -n "$MILESTONE" || "$CLEAR_SPRINT" == "true" || -n "$SPRINT" || -n "$STATUS" ]] \
  || _die "Nothing to do: set at least one of ASSIGNEE, MILESTONE, CLEAR_SPRINT, SPRINT, STATUS"
[[ -z "$SPRINT" || "$CLEAR_SPRINT" != "true" ]] \
  || _die "SPRINT and CLEAR_SPRINT are mutually exclusive"

# Resolve/validate inputs up front so a typo fails before any write.
milestone_number=""
if [[ -n "$MILESTONE" ]]; then
  milestone_number="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=all&per_page=100" \
    --jq ".[] | select(.title == \"${MILESTONE}\") | .number" | head -1)"
  [[ -n "$milestone_number" ]] || _die "No milestone titled '${MILESTONE}' exists (create it first; this tool never creates milestones)"
fi
if [[ -n "$STATUS" ]]; then
  opt_id="$(single_select_option_id "$STATUS_FIELD" "$STATUS")"
  [[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Unknown ${STATUS_FIELD} option: '${STATUS}'"
fi

# Board item id for an issue OR pull request number (first item in the
# configured project). ensure_issue_in_project only resolves issues, so this
# uses issueOrPullRequest; empty result = not on the board (board writes skip).
item_id_for_content() {
  local num="$1"
  graphql "query(\$owner:String!, \$name:String!, \$num:Int!) {
    repository(owner:\$owner, name:\$name) {
      issueOrPullRequest(number:\$num) {
        ... on Issue { projectItems(first:5) { nodes { id project { id } } } }
        ... on PullRequest { projectItems(first:5) { nodes { id project { id } } } }
      }
    }
  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$num" \
    | jq -r --arg pid "$(get_project_id)" \
        '.data.repository.issueOrPullRequest.projectItems.nodes[]? | select(.project.id == $pid) | .id' \
    | head -1
}

normalized="$(echo "$ITEMS" | tr ',' ' ')"
for n in $normalized; do
  [[ "$n" =~ ^[0-9]+$ ]] || _die "Not an issue/PR number: '$n'"
done

echo "[admin-fields] items: ${normalized} (dry_run=${DRY_RUN})" >&2
[[ -n "$ASSIGNEE" ]] && echo "[admin-fields]   assignee += ${ASSIGNEE}" >&2
[[ -n "$MILESTONE" ]] && echo "[admin-fields]   milestone -> '${MILESTONE}' (#${milestone_number})" >&2
[[ "$CLEAR_SPRINT" == "true" ]] && echo "[admin-fields]   ${SPRINT_FIELD} -> (cleared)" >&2
[[ -n "$SPRINT" ]] && echo "[admin-fields]   ${SPRINT_FIELD} -> '${SPRINT}'" >&2
[[ -n "$STATUS" ]] && echo "[admin-fields]   ${STATUS_FIELD} -> '${STATUS}'" >&2

if [[ "$DRY_RUN" != "false" ]]; then
  echo "[admin-fields] DRY RUN — no changes made." >&2
  exit 0
fi

ok=0; failed=0
for n in $normalized; do
  item_ok=1
  if [[ -n "$ASSIGNEE" ]]; then
    gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${n}/assignees" \
      -f "assignees[]=${ASSIGNEE}" >/dev/null \
      && echo "[admin-fields] #${n} assignee += ${ASSIGNEE}" >&2 \
      || { _warn "#${n}: failed to add assignee"; item_ok=0; }
  fi
  if [[ -n "$milestone_number" ]]; then
    gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${n}" \
      -F "milestone=${milestone_number}" >/dev/null \
      && echo "[admin-fields] #${n} milestone -> ${MILESTONE}" >&2 \
      || { _warn "#${n}: failed to set milestone"; item_ok=0; }
  fi
  if [[ "$CLEAR_SPRINT" == "true" || -n "$SPRINT" || -n "$STATUS" ]]; then
    item_id="$(item_id_for_content "$n" || true)"
    if [[ -z "$item_id" || "$item_id" == "null" ]]; then
      echo "[admin-fields] #${n} not on the project board — board fields skipped" >&2
    else
      if [[ -n "$STATUS" ]]; then
        set_project_field_value "$item_id" "$STATUS_FIELD" "$STATUS" \
          && echo "[admin-fields] #${n} ${STATUS_FIELD} -> ${STATUS}" >&2 \
          || { _warn "#${n}: failed to set ${STATUS_FIELD}"; item_ok=0; }
      fi
      if [[ "$CLEAR_SPRINT" == "true" ]]; then
        clear_project_field_value "$item_id" "$SPRINT_FIELD" \
          && echo "[admin-fields] #${n} ${SPRINT_FIELD} cleared" >&2 \
          || { _warn "#${n}: failed to clear ${SPRINT_FIELD}"; item_ok=0; }
      fi
      if [[ -n "$SPRINT" ]]; then
        set_project_field_value "$item_id" "$SPRINT_FIELD" "$SPRINT" \
          && echo "[admin-fields] #${n} ${SPRINT_FIELD} -> ${SPRINT}" >&2 \
          || { _warn "#${n}: failed to set ${SPRINT_FIELD}"; item_ok=0; }
      fi
    fi
  fi
  if [[ "$item_ok" -eq 1 ]]; then ok=$((ok + 1)); else failed=$((failed + 1)); fi
done
echo "[admin-fields] Done — ${ok} item(s) fully updated, ${failed} with failures." >&2
[[ "$failed" -eq 0 ]]
