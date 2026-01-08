#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/gh_project.sh
source "$DIR/lib/gh_project.sh"

require_gh_auth

# Best-effort deterministic selection:
# 1) Lowest Phase number (if FIELD_PHASE exists), else ignore
# 2) Lowest issue number
# Filters: Status == Backlog, isOpen
# If Sprint field is iteration, prefer ACTIVE sprint; otherwise no sprint filter.

project_id="$(get_project_id)"
fields_json="$(load_fields_json)"

phase_field_id="$(field_id_by_name "$FIELD_PHASE" || true)"
phase_type="$(field_type_by_name "$FIELD_PHASE" || true)"
status_field_id="$(field_id_by_name "$FIELD_STATUS")"
status_opt_backlog="$(single_select_option_id "$FIELD_STATUS" "$STATUS_BACKLOG")"

if [[ -z "$status_opt_backlog" || "$status_opt_backlog" == "null" ]]; then
  _die "Status option not found: ${STATUS_BACKLOG}"
fi

# Fetch items (limit 100)
q='query($project:ID!){
  node(id:$project){
    ... on ProjectV2{
      items(first:100){
        nodes{
          id
          content{
            __typename
            ... on Issue { number state }
          }
          fieldValues(first:50){
            nodes{
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { field { ... on ProjectV2SingleSelectField { name } } name }
              ... on ProjectV2ItemFieldTextValue { field { ... on ProjectV2TextField { name } } text }
              ... on ProjectV2ItemFieldNumberValue { field { ... on ProjectV2NumberField { name } } number }
              ... on ProjectV2ItemFieldIterationValue { field { ... on ProjectV2IterationField { name } } title }
            }
          }
        }
      }
    }
  }
}'
resp="$(graphql "$q" -F project="$project_id")"

# Build list of backlog open issues with optional phase parsing
python3 - <<'PY'
import json, re, sys, os
resp=json.load(sys.stdin)
items=resp["data"]["node"]["items"]["nodes"]
FIELD_STATUS=os.getenv("FIELD_STATUS","Status")
STATUS_BACKLOG=os.getenv("STATUS_BACKLOG","Backlog")
FIELD_PHASE=os.getenv("FIELD_PHASE","Phase")

cands=[]
for it in items:
    c=it.get("content") or {}
    if c.get("__typename")!="Issue": 
        continue
    if c.get("state")!="OPEN":
        continue
    num=c.get("number")
    status=None
    phase=None
    for fv in (it.get("fieldValues") or {}).get("nodes",[]):
        t=fv.get("__typename","")
        if t=="ProjectV2ItemFieldSingleSelectValue":
            if fv.get("field",{}).get("name")==FIELD_STATUS:
                status=fv.get("name")
        if t in ("ProjectV2ItemFieldTextValue","ProjectV2ItemFieldSingleSelectValue"):
            if fv.get("field",{}).get("name")==FIELD_PHASE:
                phase = fv.get("text") if "text" in fv else fv.get("name")
        if t=="ProjectV2ItemFieldNumberValue" and fv.get("field",{}).get("name")==FIELD_PHASE:
            phase=str(fv.get("number"))
    if status!=STATUS_BACKLOG:
        continue
    # parse phase number if like "Phase 1: ..." or "Phase 1" or "1"
    phase_n=10**9
    if phase:
        m=re.search(r'(\d+)', str(phase))
        if m:
            phase_n=int(m.group(1))
    cands.append((phase_n, int(num), num))

cands.sort()
if not cands:
    sys.exit(1)
print(cands[0][2])
PY <<EOF
$resp
EOF
