#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

log() { echo "[scrum] $*" >&2; }

# ---- Init ----
load_config
require_gh_auth

project_id="$(get_project_id)"
log "Project id: ${project_id}"

status_opt_backlog="$(single_select_option_id "$FIELD_STATUS" "$STATUS_BACKLOG")"
[[ -n "$status_opt_backlog" && "$status_opt_backlog" != "null" ]] || _die "Status option not found: ${STATUS_BACKLOG}"
log "Status field '${FIELD_STATUS}' has Backlog option '${STATUS_BACKLOG}'."

MAX_PAGES="${MAX_PAGES:-200}"  # growth-safe; stops early once it finds a candidate
log "Pagination: up to ${MAX_PAGES} pages (stop at first candidate page)"

# ---- GraphQL query ----
q='query($project:ID!, $after:String){
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
            }
          }
          fieldValues(first:50){
            nodes{
              __typename
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

fetch_page() {
  local cursor="${1:-}"
  if [[ -n "$cursor" ]]; then
    log "Fetching page (after cursor)..."
    graphql "$q" -F project="$project_id" -f after="$cursor"
  else
    log "Fetching page (first page)..."
    graphql "$q" -F project="$project_id"
  fi
}

# Python: summarize a page and return the best candidate ON THIS PAGE
# Output JSON: { total_items, issue_items, open_issues, backlog_open, best_issue }
summarize_page_file() {
  local json_file="${1:?json file required}"
  python3 - "$json_file" <<'PY'
import json, os, re, sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    d = json.load(f)

FIELD_STATUS=os.getenv("FIELD_STATUS","Status")
STATUS_BACKLOG=os.getenv("STATUS_BACKLOG","Backlog")

def phase_num(title):
    if not title:
        return 10**9
    m = re.search(r'(\d+)', title)
    return int(m.group(1)) if m else 10**9

items = d["data"]["node"]["items"]["nodes"]
total = len(items)

issues = 0
open_issues = 0
backlog_open = 0
best = None  # (phase, issue_number)

for it in items:
    c = it.get("content") or {}
    if c.get("__typename") != "Issue":
        continue
    issues += 1
    if c.get("state") != "OPEN":
        continue
    open_issues += 1

    status = None
    for fv in (it.get("fieldValues") or {}).get("nodes", []):
        if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
            field = fv.get("field") or {}
            if field.get("name") == FIELD_STATUS:
                status = fv.get("name")
                break

    if status != STATUS_BACKLOG:
        continue

    backlog_open += 1
    ms_title = ((c.get("milestone") or {}) or {}).get("title")
    cand = (phase_num(ms_title), int(c["number"]))
    if best is None or cand < best:
        best = cand

out = {
    "total_items": total,
    "issue_items": issues,
    "open_issues": open_issues,
    "backlog_open": backlog_open,
    "best_issue": (best[1] if best else None),
}
print(json.dumps(out))
PY
}

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

cursor=""

for page in $(seq 1 "$MAX_PAGES"); do
  resp="$(fetch_page "$cursor")"
  printf %s "$resp" >"$tmp"

  if jq -e '.errors? // empty' "$tmp" >/dev/null; then
    jq '.errors' "$tmp" >&2
    _die "GraphQL returned errors on page ${page}."
  fi

  bytes="$(wc -c <"$tmp" | tr -d ' ')"
  has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$tmp")"
  next_cursor="$(jq -r '.data.node.items.pageInfo.endCursor' "$tmp")"

  summary="$(summarize_page_file "$tmp")"
  backlog_open="$(echo "$summary" | jq -r '.backlog_open')"
  best_issue="$(echo "$summary" | jq -r '.best_issue // empty')"

  log "Page ${page}: bytes=${bytes} hasNext=${has_next} backlog_open=${backlog_open} best_issue=${best_issue:-null}"

  if [[ -n "${best_issue:-}" && "${best_issue:-}" != "null" ]]; then
    log "Selected issue #${best_issue} (first candidate page ${page})"
    echo "$best_issue"
    exit 0
  fi

  if [[ "$has_next" != "true" ]]; then
    break
  fi
  if [[ -z "$next_cursor" || "$next_cursor" == "null" ]]; then
    _die "hasNextPage=true but endCursor was empty/null on page ${page}"
  fi
  cursor="$next_cursor"
done

log "No Backlog+OPEN issues found."
exit 1