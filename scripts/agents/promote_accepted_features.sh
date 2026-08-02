#!/usr/bin/env bash
# Promote features whose acceptance failures are all accepted (owner flow,
# 2026-08-02): an acceptance failure is a separate issue RELATED to its
# feature ("Related feature: #N" body line, legacy "Parent feature: #N"
# accepted too) and marked as blocking it. The feature parks closed in
# "Acceptance Testing" while its failures are reworked; when EVERY related
# failure issue reaches "For Release", this sweep promotes the feature to
# "For Release" and comments the promotion on it.
#
# Also heals the native blocking relationship: any related failure issue
# not yet marked as blocking its feature gets the dependency added
# (idempotent), so the feature's Relationships panel always shows what is
# holding it back.
#
# Run via .github/workflows/promote_accepted_features.yml (cron + dispatch).
#
# ENV:
#   DRY_RUN=1   report what would be promoted/linked, change nothing
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
load_config
require_gh_auth

DRY_RUN="${DRY_RUN:-0}"
ACCEPTANCE_STATUS="${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}"

log() { echo "[promote] $*" >&2; }

# Project Status of an issue (first project item), empty if none.
issue_status() {
  local num="$1"
  graphql "query(\$owner:String!, \$name:String!, \$num:Int!) {
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
  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$num" \
    | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[]? | select(.field.name=="Status") | .name' \
    | head -1
}

# feature<TAB>af rows from every Acceptance Failure issue carrying a marker.
rows="$(gh issue list --repo "${REPO_OWNER}/${REPO_NAME}" \
  --label "Acceptance Failure" --state all --limit 500 --json number,body \
  -q '.[] | . as $i | ($i.body | capture("(Parent|Related) feature: #(?<f>[0-9]+)")? | .f // empty) as $f
      | select($f != "") | "\($f)\t\($i.number)"')"

if [[ -z "$rows" ]]; then
  log "No acceptance-failure issues with a related-feature marker found. Nothing to do."
  exit 0
fi

declare -A afs_by_feature=()
while IFS=$'\t' read -r feature af; do
  [[ -n "$feature" && -n "$af" ]] || continue
  afs_by_feature["$feature"]+="${af} "
done <<< "$rows"

promoted=0
for feature in "${!afs_by_feature[@]}"; do
  af_list=(${afs_by_feature[$feature]})

  # Heal blocking links: each failure issue must be marked as blocking its
  # feature (idempotent; failures here never abort the sweep).
  blocked_by="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${feature}/dependencies/blocked_by" \
    --jq '[.[].number] | join(",")' 2>/dev/null || echo "")"
  for af in "${af_list[@]}"; do
    if [[ ",${blocked_by}," != *",${af},"* ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        log "DRY_RUN: would mark #$af as blocking #$feature"
      else
        af_dbid="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${af}" --jq '.id')"
        gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/issues/${feature}/dependencies/blocked_by" \
          -F issue_id="$af_dbid" >/dev/null 2>&1 \
          && log "Marked #$af as blocking #$feature" \
          || log "[warn] Could not mark #$af as blocking #$feature"
      fi
    fi
  done

  fstatus="$(issue_status "$feature")"
  if [[ "$fstatus" != "$ACCEPTANCE_STATUS" ]]; then
    log "#$feature status '$fstatus' != '$ACCEPTANCE_STATUS' -- not a promotion candidate (failures: ${af_list[*]})"
    continue
  fi

  all_accepted=1
  for af in "${af_list[@]}"; do
    astatus="$(issue_status "$af")"
    if [[ "$astatus" != "$STATUS_FOR_RELEASE" ]]; then
      log "#$feature waits: related failure #$af is '$astatus' (needs '$STATUS_FOR_RELEASE')"
      all_accepted=0
      break
    fi
  done
  [[ "$all_accepted" == "1" ]] || continue

  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would promote #$feature to '$STATUS_FOR_RELEASE' (all of: ${af_list[*]} accepted)"
    continue
  fi

  set_issue_status "$feature" "$STATUS_FOR_RELEASE"
  af_refs="$(printf '#%s, ' "${af_list[@]}")"
  gh issue comment "$feature" --repo "${REPO_OWNER}/${REPO_NAME}" --body \
    "✅ **Scrummaster Agent**: every related acceptance-failure issue (${af_refs%, }) has been accepted (For Release) — promoting this issue to **For Release** (owner flow, 2026-08-02)."
  log "Promoted #$feature to '$STATUS_FOR_RELEASE' (failures accepted: ${af_list[*]})"
  promoted=$((promoted + 1))
done

log "Done. Promoted ${promoted} issue(s)."
