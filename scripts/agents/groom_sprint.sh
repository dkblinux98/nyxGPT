#!/usr/bin/env bash
set -euo pipefail

# Grooming: write the sprint plan doc the developers pull from (#3908).
#
# `product_management/sprint_planning/sprint_<N>/PLAN.md` is the artifact that
# did not exist. Sprint scope was implicit, ordering was `lowest issue
# number`, effort was a field nobody derived from evidence, and no issue
# recorded which files it was expected to touch -- so the pull step's overlap
# check (#3883) had nothing to compare, and four PRs collided on
# `src/nyxgpt/app.py` on 2026-08-18.
#
# This script writes the DRAFT and stops. It seeds order from dependencies,
# priority and effort, and expected-files from the issue bodies; it does not
# claim that order is justified. The workflow that runs it then has the
# scrummaster agent review the draft, reorder on evidence, write the rationale
# and take developer feedback on contested estimates -- the judgment D-004
# reserves for an agent, on a decision a sort key cannot make.
#
# Usage:
#   groom_sprint.sh [--sprint TITLE] [--out PATH]
#
# With no --sprint, grooms the active iteration. Re-running mid-sprint is a
# regroom: the existing plan's rationale, deferrals, regroom log and any
# hand-curated expected-files are preserved (see lib/groom_plan.py).

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

log() { echo "[groom] $*" >&2; }

SPRINT_TITLE=""
OUT_PATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sprint) SPRINT_TITLE="${2:?--sprint needs a title}"; shift 2 ;;
    --out) OUT_PATH="${2:?--out needs a path}"; shift 2 ;;
    -h|--help) sed -n '5,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) _die "Unknown argument: $1" ;;
  esac
done

load_config
require_gh_auth

if [[ -z "$SPRINT_TITLE" ]]; then
  SPRINT_TITLE="$(iteration_active_title "$SPRINT_FIELD" || true)"
  [[ -n "$SPRINT_TITLE" ]] || _die "No active '${SPRINT_FIELD}' iteration to groom."
fi
log "Grooming sprint: ${SPRINT_TITLE}"

GROOM_PAGE_QUERY='query($project:ID!, $after:String){
  node(id:$project){
    ... on ProjectV2{
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes{
          content{
            __typename
            ... on Issue {
              number
              title
              body
              state
              milestone { title }
              labels(first:20){ nodes { name } }
              assignees(first:10){ nodes { login } }
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
                startDate
                duration
              }
            }
          }
        }
      }
    }
  }
}'

project_id="$(get_project_id)"
pages="$(mktemp)"; trap 'rm -f "$pages"' EXIT
cursor=""
for page in $(seq 1 "${MAX_PAGES:-200}"); do
  if [[ -n "$cursor" ]]; then
    resp="$(graphql "$GROOM_PAGE_QUERY" -F project="$project_id" -f after="$cursor")"
  else
    resp="$(graphql "$GROOM_PAGE_QUERY" -F project="$project_id")"
  fi
  printf '%s' "$resp" | jq -c . >>"$pages"
  has_next="$(printf '%s' "$resp" | jq -r '.data.node.items.pageInfo.hasNextPage')"
  cursor="$(printf '%s' "$resp" | jq -r '.data.node.items.pageInfo.endCursor')"
  [[ "$has_next" == "true" && -n "$cursor" && "$cursor" != "null" ]] || break
done

# Sprint members, with the fields the plan reports. Closed issues are left
# out: the plan is what will be pulled, and the retrospective reports what
# was done (§7).
items="$(
  SPRINT_FIELD="$SPRINT_FIELD" SPRINT_TITLE="$SPRINT_TITLE" \
  STATUS_FIELD="$STATUS_FIELD" RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
  python3 - "$pages" <<'PY'
import json, os, sys
sys.path.insert(0, "scripts/agents/lib")
from support_label import is_support_issue

sprint_field = os.environ["SPRINT_FIELD"]
sprint_title = os.environ["SPRINT_TITLE"]
status_field = os.environ["STATUS_FIELD"]
release_issue = os.environ.get("RELEASE_ISSUE", "")

out, window = [], {}
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        page = json.loads(line)
        for item in ((page.get("data") or {}).get("node") or {}).get("items", {}).get("nodes", []):
            content = item.get("content") or {}
            if content.get("__typename") != "Issue" or content.get("state") != "OPEN":
                continue
            if release_issue and str(content.get("number")) == str(release_issue):
                continue
            if is_support_issue((content.get("labels") or {}).get("nodes")):
                continue
            fields = {}
            in_sprint = False
            for value in (item.get("fieldValues") or {}).get("nodes", []):
                field = (value.get("field") or {}).get("name")
                if value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
                    fields[field] = value.get("name")
                elif value.get("__typename") == "ProjectV2ItemFieldIterationValue":
                    if field == sprint_field and value.get("title") == sprint_title:
                        in_sprint = True
                        window.setdefault("start", value.get("startDate") or "?")
                        window.setdefault("duration", value.get("duration"))
            if not in_sprint:
                continue
            out.append({
                "issue": int(content["number"]),
                "title": content.get("title", ""),
                "body": content.get("body", ""),
                "status": fields.get(status_field, ""),
                "priority": fields.get("Priority", ""),
                "effort": fields.get("Effort", ""),
                "milestone": ((content.get("milestone") or {}) or {}).get("title", ""),
            })
print(json.dumps({"items": out, "window": window}))
PY
)"

numbers="$(printf '%s' "$items" | jq -r '.items[].issue')"
log "Sprint members: $(printf '%s' "$numbers" | grep -c . || true)"

rel_entries=""
blk_entries=""
while read -r n; do
  [[ -n "$n" ]] || continue
  bb="$(blocked_by_issues "$n" | paste -sd, -)"
  bl="$(blocking_issues "$n" | paste -sd, -)"
  [[ -z "$bb" ]] || rel_entries="${rel_entries}${rel_entries:+,}\"$n\":[${bb}]"
  [[ -z "$bl" ]] || blk_entries="${blk_entries}${blk_entries:+,}\"$n\":[${bl}]"
done <<<"$numbers"

OUT_PATH="${OUT_PATH:-$(
  SPRINT_TITLE="$SPRINT_TITLE" python3 - <<'PY'
import os, re
title = os.environ["SPRINT_TITLE"]
match = re.search(r"(\d+)", title)
slug = match.group(1) if match else re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
print(f"product_management/sprint_planning/sprint_{slug}/PLAN.md")
PY
)}"
mkdir -p "$(dirname "$OUT_PATH")"

ITEMS_JSON="$items" \
BLOCKED_BY="{${rel_entries}}" \
BLOCKS="{${blk_entries}}" \
SPRINT_TITLE="$SPRINT_TITLE" \
OUT_PATH="$OUT_PATH" \
python3 - <<'PY'
import json, os, pathlib, sys
sys.path.insert(0, "scripts/agents/lib")
import groom_plan, sprint_plan

payload = json.loads(os.environ["ITEMS_JSON"])
items = payload["items"]
window = payload.get("window") or {}
blocked_by = {int(k): v for k, v in json.loads(os.environ["BLOCKED_BY"]).items()}
blocks = {int(k): v for k, v in json.loads(os.environ["BLOCKS"]).items()}

out = pathlib.Path(os.environ["OUT_PATH"])
previous = sprint_plan.parse_plan(out.read_text(encoding="utf-8")) if out.exists() else {}

start = window.get("start", "?")
duration = window.get("duration")
end = "?"
if start != "?" and duration:
    import datetime
    end = (datetime.date.fromisoformat(start) + datetime.timedelta(days=int(duration))).isoformat()

milestones = [i.get("milestone") for i in items if i.get("milestone")]
plan = groom_plan.build_plan(
    sprint=os.environ["SPRINT_TITLE"],
    window={"start": start, "end": end},
    milestone=max(set(milestones), key=milestones.count) if milestones else "",
    items=items,
    blocked_by=blocked_by,
    blocks=blocks,
    previous=previous,
)
out.write_text(sprint_plan.render_plan(plan), encoding="utf-8")
print(f"[groom] wrote {out} ({len(plan['order'])} issues)", file=sys.stderr)
print(out)
PY
