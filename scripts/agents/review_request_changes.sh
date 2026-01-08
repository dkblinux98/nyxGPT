#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth

ISSUE="${1:?parent issue number required}"
TITLE="${2:?sub-issue title required}"
BODY_FILE="${3:?body file required}"

# Create sub-issue
sub="$(create_sub_issue "$ISSUE" "$TITLE" "$BODY_FILE")"

# Label Acceptance Failure
gh issue edit "$sub" --repo "${REPO_OWNER}/${REPO_NAME}" --add-label "Acceptance Failure" >/dev/null || true

# Ensure sub-issue in project and inherit key fields from parent (Priority/Module/Sprint/Effort)
parent_item="$(ensure_issue_in_project "$ISSUE")"
sub_item="$(ensure_issue_in_project "$sub")"

q_vals='query($item:ID!){
  node(id:$item){
    ... on ProjectV2Item{
      fieldValues(first:50){
        nodes{
          __typename
          ... on ProjectV2ItemFieldSingleSelectValue{ field{... on ProjectV2SingleSelectField{name}} name }
          ... on ProjectV2ItemFieldNumberValue{ field{... on ProjectV2NumberField{name}} number }
          ... on ProjectV2ItemFieldIterationValue{ field{... on ProjectV2IterationField{name}} title }
          ... on ProjectV2ItemFieldTextValue{ field{... on ProjectV2TextField{name}} text }
        }
      }
    }
  }
}'
vals="$(graphql "$q_vals" -F item="$parent_item")"

python3 - <<'PY' >/tmp/parent_fields.json
import os, json
vals=json.loads(os.environ["VALS"])
fields = {"Priority":"","Module":"","Sprint":"","Effort":""}
for n in vals["data"]["node"]["fieldValues"]["nodes"]:
  name=n.get("field",{}).get("name")
  if name in fields:
    fields[name]= n.get("name") or n.get("title") or n.get("text") or (str(n.get("number")) if n.get("number") is not None else "")
print(json.dumps(fields))
PY
VALS="$vals"

fields="$(cat /tmp/parent_fields.json)"
prio="$(echo "$fields" | jq -r '.Priority')"
module="$(echo "$fields" | jq -r '.Module')"
sprint="$(echo "$fields" | jq -r '.Sprint')"
effort="$(echo "$fields" | jq -r '.Effort')"

[[ -n "$prio" && "$prio" != "null" ]] && set_project_field_value "$sub_item" "$FIELD_PRIORITY" "$prio" || true
[[ -n "$module" && "$module" != "null" ]] && set_project_field_value "$sub_item" "$FIELD_MODULE" "$module" || true
[[ -n "$sprint" && "$sprint" != "null" ]] && set_project_field_value "$sub_item" "$FIELD_SPRINT" "$sprint" || true
[[ -n "$effort" && "$effort" != "null" ]] && set_project_field_value "$sub_item" "$FIELD_EFFORT" "$effort" || true

# Link (best-effort)
link_sub_issue "$ISSUE" "$sub"

# Bounce parent issue back to In Progress and assign to dev agent
set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
issue_unassign_all "$ISSUE"
issue_assign "$ISSUE" "$DEV_AGENT"
issue_comment "$ISSUE" "Acceptance Failure created: #${sub}. Assigned back to @${DEV_AGENT}. Status -> ${STATUS_IN_PROGRESS}."

# Set sub-issue status to In Progress too and assign to dev agent
set_issue_status "$sub" "$STATUS_IN_PROGRESS"
issue_unassign_all "$sub"
issue_assign "$sub" "$DEV_AGENT"

echo "$sub"
