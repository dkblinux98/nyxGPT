#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth

ISSUE="${1:?issue number required}"
PR_TITLE="${2:?pr title required}"
PR_BODY_FILE="${3:-}"

base_branch="$(get_release_branch)"

# Create PR body
tmp_body=""
if [[ -n "$PR_BODY_FILE" ]]; then
  [[ -f "$PR_BODY_FILE" ]] || _die "PR body file not found: $PR_BODY_FILE"
  body_file="$PR_BODY_FILE"
else
  tmp_body="$(mktemp)"
  body_file="$tmp_body"
  cat >"$body_file" <<EOF
Closes #$ISSUE

## Summary
- 

## Tests
- 

## Notes
- 
EOF
fi

# Create PR. Use "Closes #ISSUE" so closure happens on merge (not now).
pr_url="$(gh pr create --repo "${REPO_OWNER}/${REPO_NAME}" --base "$base_branch" --title "$PR_TITLE" --body-file "$body_file")"
[[ -n "$pr_url" ]] || _die "Failed to create PR"

# Ensure the ISSUE is in project and set issue -> In Review, assign review agent.
set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_unassign_all "$ISSUE"
issue_assign "$ISSUE" "$REVIEW_AGENT"
issue_comment "$ISSUE" "PR opened: ${pr_url}\nAssigned to @${REVIEW_AGENT}. Status -> ${STATUS_IN_REVIEW}."

# Ensure PR is in project and mirror fields from issue where applicable (Priority/Module/Sprint/Effort).
# Add PR to project:
project_id="$(get_project_id)"
# get PR node id
pr_number="$(gh pr view "$pr_url" --json number -q .number)"
pr_node_id="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${pr_number}" --jq '.node_id')"

q_add='mutation($project:ID!, $content:ID!){
  addProjectV2ItemById(input:{projectId:$project, contentId:$content}){ item { id } }
}'
resp_add="$(graphql "$q_add" -F project="$project_id" -F content="$pr_node_id")"
pr_item_id="$(echo "$resp_add" | jq -r '.data.addProjectV2ItemById.item.id')"
if [[ "$pr_item_id" == "null" || -z "$pr_item_id" ]]; then
  # Might already exist; best-effort ignore
  _debug "PR might already be in project"
fi

# Best-effort: copy select fields from issue's project item.
# We will read field values from issue item by querying Project item fieldValues, then set on PR item if available.
issue_item_id="$(ensure_issue_in_project "$ISSUE")"

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
vals="$(graphql "$q_vals" -F item="$issue_item_id")"

def get_val(field):
  import json, os, sys
  v=json.loads(os.environ["VALS"])
  nodes=v["data"]["node"]["fieldValues"]["nodes"]
  for n in nodes:
    name=n.get("field",{}).get("name")
    if name==field:
      if "name" in n and n["name"] is not None: return n["name"]
      if "number" in n and n["number"] is not None: return str(n["number"])
      if "title" in n and n["title"] is not None: return n["title"]
      if "text" in n and n["text"] is not None: return n["text"]
  return ""

python3 - <<'PY' >/tmp/issue_fields.json
import os, json
vals=json.loads(os.environ["VALS"])
fields = {
  "Priority": "",
  "Module": "",
  "Sprint": "",
  "Effort": "",
}
for k in list(fields.keys()):
  nodes=vals["data"]["node"]["fieldValues"]["nodes"]
  for n in nodes:
    if n.get("field",{}).get("name")==k:
      fields[k]= n.get("name") or n.get("title") or n.get("text") or (str(n.get("number")) if n.get("number") is not None else "")
print(json.dumps(fields))
PY
VALS="$vals"

if [[ -n "${pr_item_id:-}" && "$pr_item_id" != "null" ]]; then
  fields="$(cat /tmp/issue_fields.json)"
  prio="$(echo "$fields" | jq -r '.Priority')"
  module="$(echo "$fields" | jq -r '.Module')"
  sprint="$(echo "$fields" | jq -r '.Sprint')"
  effort="$(echo "$fields" | jq -r '.Effort')"

  [[ -n "$prio" && "$prio" != "null" ]] && set_project_field_value "$pr_item_id" "$FIELD_PRIORITY" "$prio" || true
  [[ -n "$module" && "$module" != "null" ]] && set_project_field_value "$pr_item_id" "$FIELD_MODULE" "$module" || true
  [[ -n "$sprint" && "$sprint" != "null" ]] && set_project_field_value "$pr_item_id" "$FIELD_SPRINT" "$sprint" || true
  [[ -n "$effort" && "$effort" != "null" ]] && set_project_field_value "$pr_item_id" "$FIELD_EFFORT" "$effort" || true

  # Set PR Status to In Review too (optional); only if field exists and option exists
  set_project_field_value "$pr_item_id" "$FIELD_STATUS" "$STATUS_IN_REVIEW" || true
fi

if [[ -n "$tmp_body" ]]; then rm -f "$tmp_body"; fi

echo "$pr_url"
