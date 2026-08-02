#!/usr/bin/env bash
# Verify (and optionally repair) the project fields of the Phase 6 issue set
# (#3500-#3516, filed 2026-07-31 by create_phase6.sh via file_phase6_issues.yml,
# plus #3555 = P6-18 and #3558 = P6-19, filed 2026-08-01).
#
# WHY: ensure_project_hygiene.yml raced create_issue.sh on #3500 — its Status
# check ran in the seconds before create_issue.sh set Backlog, so it stamped
# defaults (Sprint 6, the Phase 5.5 milestone, Effort XS, auto-detected Module)
# over the intended values. This script compares every Phase 6 issue against
# the plan's expected fields and, with REPAIR=1, restores any that drifted.
#
# Run via .github/workflows/verify_phase6_issues.yml.
#
# ENV:
#   REPAIR=0   (default) report mismatches only, exit 1 if any found
#   REPAIR=1   fix mismatches via the sanctioned project-field helpers

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
load_config
require_gh_auth

REPAIR="${REPAIR:-0}"

MILESTONE="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=open&per_page=50" \
  --jq '[.[] | select(.title | startswith("Phase 6"))][0].title // empty')"
[[ -n "$MILESTONE" ]] || _die "No open 'Phase 6*' milestone found."

# issue|key|sprint|module|effort  (Priority is P1 - High and Status Backlog for all)
EXPECTED="
3500|P6-1|Sprint 7|security|S
3501|P6-2|Sprint 7|security|S
3502|P6-3|Sprint 7|testing|S
3503|P6-4|Sprint 7|documentation|S
3504|P6-5|Sprint 7|cli|L
3505|P6-6|Sprint 7|cli|M
3506|P6-7|Sprint 7|documentation|S
3507|P6-10|Sprint 7|security|M
3508|P6-14|Sprint 7|cli|L
3555|P6-18|Sprint 7|testing|L
3558|P6-19|Sprint 7|cli|M
3509|P6-8|Sprint 8|cli|XL
3510|P6-9|Sprint 8|cli|M
3511|P6-12|Sprint 8|cli|L
3512|P6-13|Sprint 8|cli|M
3513|P6-11|Sprint 8|cli|XL
3514|P6-15|Sprint 8|sre|L
3515|P6-17|Sprint 8|testing|M
3516|P6-16|Sprint 8|cli|XL
"

mismatches=0
repairs=0

while IFS='|' read -r issue key want_sprint want_module want_effort; do
  [[ -z "$issue" ]] && continue

  actual="$(graphql "query(\$owner:String!, \$name:String!, \$num:Int!) {
    repository(owner:\$owner, name:\$name) {
      issue(number:\$num) {
        milestone { title }
        projectItems(first:5) {
          nodes {
            id
            fieldValues(first:20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue { field { ... on ProjectV2SingleSelectField { name } } name }
                ... on ProjectV2ItemFieldIterationValue { field { ... on ProjectV2IterationField { name } } title }
              }
            }
          }
        }
      }
    }
  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$issue")"

  a_milestone="$(echo "$actual" | jq -r '.data.repository.issue.milestone.title // ""')"
  item="$(echo "$actual" | jq -c '.data.repository.issue.projectItems.nodes[0] // empty')"
  item_id="$(echo "$item" | jq -r '.id // ""')"
  a_status="$(echo "$item"  | jq -r '.fieldValues.nodes[] | select(.field.name=="Status") | .name' 2>/dev/null | head -1)"
  a_sprint="$(echo "$item"  | jq -r '.fieldValues.nodes[] | select(.field.name=="Sprint") | .title' 2>/dev/null | head -1)"
  a_module="$(echo "$item"  | jq -r '.fieldValues.nodes[] | select(.field.name=="Module") | .name' 2>/dev/null | head -1)"
  a_effort="$(echo "$item"  | jq -r '.fieldValues.nodes[] | select(.field.name=="Effort") | .name' 2>/dev/null | head -1)"
  a_priority="$(echo "$item" | jq -r '.fieldValues.nodes[] | select(.field.name=="Priority") | .name' 2>/dev/null | head -1)"

  bad=""
  [[ "$a_milestone" == "$MILESTONE" ]] || bad+=" milestone='$a_milestone'"
  [[ "$a_status"   == "Backlog" ]]      || bad+=" status='$a_status'"
  [[ "$a_sprint"   == "$want_sprint" ]] || bad+=" sprint='$a_sprint'"
  [[ "$a_module"   == "$want_module" ]] || bad+=" module='$a_module'"
  [[ "$a_effort"   == "$want_effort" ]] || bad+=" effort='$a_effort'"
  [[ "$a_priority" == "P1 - High" ]]    || bad+=" priority='$a_priority'"

  if [[ -z "$bad" ]]; then
    echo "[verify] #$issue $key OK ($want_sprint, $want_module, $want_effort)"
    continue
  fi

  mismatches=$((mismatches + 1))
  echo "[verify] #$issue $key MISMATCH:$bad (want: $want_sprint/$want_module/$want_effort/P1 - High/Backlog/'$MILESTONE')"

  if [[ "$REPAIR" == "1" ]]; then
    [[ -n "$item_id" ]] || item_id="$(ensure_issue_in_project "$issue")"
    [[ "$a_milestone" == "$MILESTONE" ]]  || gh issue edit "$issue" --repo "${REPO_OWNER}/${REPO_NAME}" --milestone "$MILESTONE"
    [[ "$a_status"   == "Backlog" ]]      || set_issue_status "$issue" "Backlog"
    [[ "$a_sprint"   == "$want_sprint" ]] || set_project_field_value "$item_id" "Sprint" "$want_sprint"
    [[ "$a_module"   == "$want_module" ]] || set_project_field_value "$item_id" "Module" "$want_module"
    [[ "$a_effort"   == "$want_effort" ]] || set_project_field_value "$item_id" "Effort" "$want_effort"
    [[ "$a_priority" == "P1 - High" ]]    || set_project_field_value "$item_id" "Priority" "P1 - High"
    repairs=$((repairs + 1))
    echo "[verify] #$issue $key repaired"
  fi
done <<< "$EXPECTED"

echo ""
if [[ "$mismatches" -eq 0 ]]; then
  echo "[verify] All Phase 6 issues in the expected table carry the expected fields."
elif [[ "$REPAIR" == "1" ]]; then
  echo "[verify] Repaired $repairs of $mismatches mismatched issue(s). Re-run without REPAIR to confirm."
else
  echo "[verify] $mismatches issue(s) mismatched. Re-run with repair=true to fix."
  exit 1
fi
