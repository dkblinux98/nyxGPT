#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth
require_cmd jq
require_cmd python3

project_id="$(get_project_id)"
status_opt_backlog="$(single_select_option_id "$FIELD_STATUS" "$STATUS_BACKLOG")"
[[ -n "$status_opt_backlog" && "$status_opt_backlog" != "null" ]] || _die "Status option not found: ${STATUS_BACKLOG}"

q='query($project:ID!){
  node(id:$project){
    ... on ProjectV2{
      items(first:200){
        nodes{
          content{ __typename ... on Issue { number state } }
          fieldValues(first:50){
            nodes{
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { field { ... on ProjectV2SingleSelectField { name } } name }
            }
          }
        }
      }
    }
  }
}'
resp="$(graphql "$q" -F project="$project_id")"

cands="$(python3 - <<'PY'
import json, os, sys
d=json.load(sys.stdin)
FIELD_STATUS=os.getenv("FIELD_STATUS","Status")
STATUS_BACKLOG=os.getenv("STATUS_BACKLOG","Backlog")
nums=[]
for it in d["data"]["node"]["items"]["nodes"]:
    c=it.get("content") or {}
    if c.get("__typename")!="Issue": 
        continue
    if c.get("state")!="OPEN":
        continue
    status=None
    for fv in (it.get("fieldValues") or {}).get("nodes",[]):
        if fv.get("__typename")=="ProjectV2ItemFieldSingleSelectValue" and fv.get("field",{}).get("name")==FIELD_STATUS:
            status=fv.get("name")
    if status==STATUS_BACKLOG:
        nums.append(int(c["number"]))
nums.sort()
print("\\n".join(map(str, nums)))
PY
<<<"$resp")"

[[ -n "$cands" ]] || exit 1

best_phase=999999999
best_issue=999999999

while read -r num; do
  [[ -z "$num" ]] && continue
  ms_title="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${num}" --jq '.milestone.title // empty' || true)"
  phase=999999999
  if [[ -n "$ms_title" && "$ms_title" =~ ([0-9]+) ]]; then
    phase="${BASH_REMATCH[1]}"
  fi
  if (( phase < best_phase )) || (( phase == best_phase && num < best_issue )); then
    best_phase=$phase
    best_issue=$num
  fi
done <<< "$cands"

[[ "$best_issue" -lt 999999999 ]] || exit 1
echo "$best_issue"
