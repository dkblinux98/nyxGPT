#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_sprint_report.sh [--dry-run]

Computes sprint standing for the active Sprint (done / in-review /
in-progress / remaining, velocity, projected completion vs the sprint end
date, and blockers) and posts a report comment on the release tracking
issue (RELEASE_ISSUE_NUMBER). When the projection says the sprint won't
complete by its end date, the report includes a concrete reorganization
proposal (which Backlog issues to move out) -- this script only proposes,
it never applies; see scrummaster_sprint_reorg_apply.sh for that,
stakeholder-gated behind an APPROVE_SPRINT_REORG comment.

If no Sprint is active, posts (or prints) a minimal note and exits 0 --
this is meant to run on an unattended daily schedule, so "nothing to report"
must never be treated as an error.

Options:
  --dry-run   Print the report instead of posting it (also works without
              RELEASE_ISSUE_NUMBER configured)
  -h, --help  Show this help

Environment:
  SPRINT_FIELD          Iteration field name (default: Sprint)
  RELEASE_ISSUE_NUMBER  Issue to post the report on (required unless --dry-run)
EOF
}

DRY_RUN=0
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

log() { echo "[sprint-report] $*" >&2; }

load_config
require_gh_auth
require_cmd jq
require_cmd python3

SPRINT_FIELD="${SPRINT_FIELD:-Sprint}"
RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}"
if [[ -z "$RELEASE_ISSUE" && "$DRY_RUN" != "1" ]]; then
  _die "RELEASE_ISSUE_NUMBER is not configured -- cannot post the sprint report. Set it in config, or pass --dry-run."
fi

post_or_print() {
  local body="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "$body"
  else
    issue_comment "$RELEASE_ISSUE" "$body"
  fi
}

active_sprint="$(iteration_active_title "$SPRINT_FIELD" 2>/dev/null || echo "")"
if [[ -z "$active_sprint" || "$active_sprint" == "null" ]]; then
  log "No active Sprint on field '${SPRINT_FIELD}' -- skipping report."
  post_or_print "ℹ️ **Sprint Report**: No active Sprint configured — nothing to report."
  exit 0
fi
log "Active sprint: ${active_sprint}"

# ---- Sprint window (start/end dates) from the iteration field config ----
iter_json="$(get_fields_json | jq -c --arg f "$SPRINT_FIELD" --arg t "$active_sprint" '
  .data.node.fields.nodes[]
  | select(.name==$f and .__typename=="ProjectV2IterationField")
  | .configuration.iterations[]
  | select(.title==$t)
')"
start_date="$(echo "$iter_json" | jq -r '.startDate // empty')"
duration="$(echo "$iter_json" | jq -r '.duration // empty')"
[[ -n "$start_date" ]] || _die "Could not resolve start date for sprint '${active_sprint}' on field '${SPRINT_FIELD}'."
end_date="$(python3 -c "
from datetime import date, timedelta
d = date.fromisoformat('${start_date}')
print((d + timedelta(days=int('${duration:-14}'))).isoformat())
")"
log "Sprint window: ${start_date} -> ${end_date} (duration ${duration:-14}d)"

# ---- Fetch sprint items: Status + Sprint + Priority per open issue ----
project_id="$(get_project_id)"
q='query($project:ID!, $after:String){
  node(id:$project){
    ... on ProjectV2{
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes{
          content{ __typename ... on Issue { number state } }
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

items_json="$(mktemp)"
trap 'rm -f "$items_json"' EXIT
echo "[]" >"$items_json"

cursor=""
MAX_PAGES="${MAX_PAGES:-200}"
for page in $(seq 1 "$MAX_PAGES"); do
  if [[ -n "$cursor" ]]; then
    resp="$(graphql "$q" -F project="$project_id" -F after="$cursor")"
  else
    resp="$(graphql "$q" -F project="$project_id")"
  fi

  page_items="$(echo "$resp" | jq -c --arg status_field "$STATUS_FIELD" --arg sprint_field "$SPRINT_FIELD" '
    [.data.node.items.nodes[]
     | select(.content.__typename=="Issue")
     | {
         number: .content.number,
         state: .content.state,
         status: ([.fieldValues.nodes[] | select(.__typename=="ProjectV2ItemFieldSingleSelectValue" and .field.name==$status_field) | .name][0] // null),
         priority: ([.fieldValues.nodes[] | select(.__typename=="ProjectV2ItemFieldSingleSelectValue" and .field.name=="Priority") | .name][0] // null),
         sprint: ([.fieldValues.nodes[] | select(.__typename=="ProjectV2ItemFieldIterationValue" and .field.name==$sprint_field) | .title][0] // null)
       }
    ]')"
  jq -s '.[0] + .[1]' "$items_json" <(echo "$page_items") >"${items_json}.tmp" && mv "${items_json}.tmp" "$items_json"

  has_next="$(echo "$resp" | jq -r '.data.node.items.pageInfo.hasNextPage')"
  next_cursor="$(echo "$resp" | jq -r '.data.node.items.pageInfo.endCursor // empty')"
  [[ "$has_next" == "true" && -n "$next_cursor" ]] || break
  cursor="$next_cursor"
done

sprint_items="$(jq -c --arg t "$active_sprint" '[.[] | select(.sprint == $t and .state == "OPEN")]' "$items_json")"
log "Sprint '${active_sprint}': $(echo "$sprint_items" | jq 'length') open issue(s) tracked."

counts="$(jq -c --arg backlog "$STATUS_BACKLOG" --arg in_progress "$STATUS_IN_PROGRESS" \
  --arg in_review "$STATUS_IN_REVIEW" --arg done "$STATUS_FOR_RELEASE" '
  {
    backlog: ([.[] | select(.status == $backlog)] | length),
    in_progress: ([.[] | select(.status == $in_progress)] | length),
    in_review: ([.[] | select(.status == $in_review)] | length),
    done: ([.[] | select(.status == $done)] | length)
  }' <<<"$sprint_items")"

backlog_issues="$(jq -c --arg backlog "$STATUS_BACKLOG" '
  [.[] | select(.status == $backlog) | {number: .number, priority: .priority}]' <<<"$sprint_items")"

# ---- Blockers: open PRs closing an in-sprint "In Review" issue that have ----
# ---- already been through at least one REQUEST_CHANGES review cycle ----
in_review_issues="$(jq -c --arg in_review "$STATUS_IN_REVIEW" '[.[] | select(.status == $in_review) | .number]' <<<"$sprint_items")"

blockers="[]"
if [[ "$(echo "$in_review_issues" | jq 'length')" -gt 0 ]]; then
  # --jq runs once per fetched page, so `[.[] | {number, body}]` yields one
  # array per page (concatenated docs), not a single merged array -- stream
  # flat items and slurp them into one array in a second jq pass instead
  # (see AGENTS.md).
  pr_bodies_json="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls?state=open&per_page=100" \
    --paginate --jq '.[] | {number, body}' 2>/dev/null | jq -s '.' || echo "[]")"
  pr_numbers="$(echo "$pr_bodies_json" | jq -r '.[].number')"
  while IFS= read -r pr_num; do
    [[ -n "$pr_num" ]] || continue
    body="$(echo "$pr_bodies_json" | jq -r --argjson n "$pr_num" '.[] | select(.number == $n) | .body // ""')"
    issue_num="$(echo "$body" | sed -n 's/.*Closes #\([0-9]*\).*/\1/p' | head -1)"
    [[ -n "$issue_num" ]] || continue
    is_in_review="$(jq --argjson n "$issue_num" 'index($n) != null' <<<"$in_review_issues")"
    [[ "$is_in_review" == "true" ]] || continue
    cr_count="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${pr_num}/reviews" \
      --jq '[.[] | select(.state=="CHANGES_REQUESTED")] | length' 2>/dev/null || echo 0)"
    [[ "${cr_count:-0}" -gt 0 ]] || continue
    blockers="$(jq -c --argjson pr "$pr_num" --argjson issue "$issue_num" --argjson cr "$cr_count" \
      '. + [{"pr": $pr, "issue": $issue, "changes_requested": $cr}]' <<<"$blockers")"
  done <<<"$pr_numbers"
fi
log "Blockers detected: $(echo "$blockers" | jq 'length')"

# Sprint boundaries are evaluated in the owner's timezone (SPRINT_TIMEZONE,
# default America/New_York) -- see sprint_today() in lib/gh_project.sh.
today="$(sprint_today)"

payload="$(jq -n \
  --arg sprint_title "$active_sprint" \
  --arg start_date "$start_date" \
  --arg end_date "$end_date" \
  --arg today "$today" \
  --argjson counts "$counts" \
  --argjson backlog_issues "$backlog_issues" \
  --argjson blockers "$blockers" \
  '{sprint_title:$sprint_title, start_date:$start_date, end_date:$end_date, today:$today, counts:$counts, backlog_issues:$backlog_issues, blockers:$blockers}')"

result="$(echo "$payload" | python3 "$DIR/lib/sprint_calc.py" report)"
markdown="$(echo "$result" | jq -r '.markdown')"
verdict="$(echo "$result" | jq -r '.verdict')"
log "Verdict: ${verdict}"

post_or_print "$markdown"
[[ "$DRY_RUN" == "1" ]] || log "Posted sprint report to issue #${RELEASE_ISSUE}"
