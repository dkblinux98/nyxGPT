#!/usr/bin/env bash
set -euo pipefail

# Selection, in the developer's context (#3883).
#
# This replaces scrummaster_next_issue.sh, whose whole content was the rule
# `lowest Phase, then lowest issue number` -- a condition expression standing
# in for a decision (D-004), and a push: the scrummaster picked and assigned.
# Nothing compared a candidate's likely file footprint against work already
# in flight, so on 2026-08-18 four PRs collided on `src/nyxgpt/app.py`.
#
# What decides now is the sprint plan the scrummaster grooms (#3908) plus the
# §6 pull algorithm in lib/pull_next_issue.py: plan order, relationships
# eligibility, WIP limit read from the board and open PRs, and a file-overlap
# check against in-flight work. Scheduling is the conflict strategy --
# overlapping candidates are deferred, never pulled in parallel.
#
# The guards did not move. This script only *selects*; claiming is still
# scrummaster_attempt_start (§5 ordering: Status In Progress, then assign),
# and the dispatch-pause backstops still gate the loop that calls this.
#
# Usage:
#   developer_pull_next.sh [--sprint-scoped] [--explain FILE]
#
# Prints the selected issue number on stdout (empty if nothing is eligible),
# and the decision's reasoning on stderr -- and to --explain FILE as JSON, so
# the caller can quote it in the dispatch comment. A wrong pull has to be
# visible to be corrected.
#
# Environment:
#   EXCLUDE_ISSUES  Comma-separated issue numbers to skip (the fall-through
#                   loop's retry set, #3665).
#   WIP_LIMIT       Max issues In Progress/In Review at once (default 2,
#                   owner decision 2026-08-08). A floor for safety, not a
#                   ceiling on the owner: raise it deliberately.
#   MAX_BLOCKER_LOOKUPS  How many candidates get a relationships round trip
#                   (default 12); beyond that a candidate is treated as
#                   unblocked and the claim step catches it.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

log() { echo "[pull] $*" >&2; }

SPRINT_SCOPED=0
EXPLAIN_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sprint-scoped) SPRINT_SCOPED=1; shift ;;
    --explain) EXPLAIN_FILE="${2:?--explain needs a path}"; shift 2 ;;
    -h|--help) sed -n '5,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) _die "Unknown argument: $1" ;;
  esac
done

load_config
require_gh_auth

WIP_LIMIT="${WIP_LIMIT:-2}"
MAX_BLOCKER_LOOKUPS="${MAX_BLOCKER_LOOKUPS:-12}"

# Same page walk as the old selector, plus assignees: §5 classifies a
# Backlog issue by who holds it, and a query cannot filter on a field it
# never fetched.
PULL_PAGE_QUERY='query($project:ID!, $after:String){
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
              }
            }
          }
        }
      }
    }
  }
}'

ACTIVE_SPRINT_TITLE=""
if [[ "$SPRINT_SCOPED" == "1" ]]; then
  ACTIVE_SPRINT_TITLE="$(iteration_active_title "$SPRINT_FIELD" || true)"
  # `null` is what the helper prints when no iteration's window contains
  # today -- an empty-string test alone would sail past it and scope the
  # pull to a sprint that does not exist.
  if [[ "$ACTIVE_SPRINT_TITLE" == "null" ]]; then ACTIVE_SPRINT_TITLE=""; fi
  if [[ -z "$ACTIVE_SPRINT_TITLE" ]]; then
    log "No active sprint on field '${SPRINT_FIELD}' -- conservative stop (#3706)."
    exit 1
  fi
  log "Sprint-scoped pull: active sprint '${ACTIVE_SPRINT_TITLE}'"
fi

RELEASE_VERSION=""
if [[ -n "${RELEASE_ISSUE_NUMBER:-}" ]]; then
  RELEASE_VERSION="$(release_version_from_issue "$RELEASE_ISSUE_NUMBER" 2>/dev/null || echo "")"
fi

# ---- board: every claimable Backlog candidate, and everything in flight ----
project_id="$(get_project_id)"
board="$(mktemp)"; trap 'rm -f "$board"' EXIT
cursor=""
: >"$board"
for page in $(seq 1 "${MAX_PAGES:-200}"); do
  if [[ -n "$cursor" ]]; then
    resp="$(graphql "$PULL_PAGE_QUERY" -F project="$project_id" -f after="$cursor")"
  else
    resp="$(graphql "$PULL_PAGE_QUERY" -F project="$project_id")"
  fi
  # One compact JSON object per line: board_pull_state.py reads the pages
  # back as JSONL, and `gh api graphql` pretty-prints by default.
  printf '%s' "$resp" | jq -c . >>"$board"
  has_next="$(printf '%s' "$resp" | jq -r '.data.node.items.pageInfo.hasNextPage')"
  cursor="$(printf '%s' "$resp" | jq -r '.data.node.items.pageInfo.endCursor')"
  [[ "$has_next" == "true" && -n "$cursor" && "$cursor" != "null" ]] || break
done

board_state="$(
  STATUS_FIELD="$STATUS_FIELD" \
  STATUS_BACKLOG="$STATUS_BACKLOG" \
  STATUS_IN_PROGRESS="$STATUS_IN_PROGRESS" \
  STATUS_IN_REVIEW="$STATUS_IN_REVIEW" \
  SPRINT_FIELD="$SPRINT_FIELD" \
  SPRINT_SCOPED="$SPRINT_SCOPED" \
  ACTIVE_SPRINT_TITLE="$ACTIVE_SPRINT_TITLE" \
  RELEASE_VERSION="$RELEASE_VERSION" \
  RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
  python3 "$DIR/lib/board_pull_state.py" "$board"
)"

# ---- the sprint plan: pull order and expected-files (#3908) ----
plan_json="$(
  SPRINT_TITLE="$ACTIVE_SPRINT_TITLE" python3 - <<'PY'
import json, os, pathlib, sys
sys.path.insert(0, "scripts/agents/lib")
import sprint_plan

root = pathlib.Path("product_management/sprint_planning")
title = os.environ.get("SPRINT_TITLE", "")
plan = {}
chosen = None
if root.is_dir():
    docs = sorted(root.glob("sprint_*/PLAN.md"))
    for doc in docs:
        parsed = sprint_plan.parse_plan(doc.read_text(encoding="utf-8"))
        if title and parsed.get("sprint") == title:
            plan, chosen = parsed, doc
            break
    if not plan and docs and not title:
        chosen = docs[-1]
        plan = sprint_plan.parse_plan(chosen.read_text(encoding="utf-8"))
print(json.dumps(plan))
print(f"[pull] plan doc: {chosen or 'none found -- pulling in board order'}", file=sys.stderr)
PY
)"

# ---- in-flight footprints: the open PR's real diff, else expected-files ----
in_flight="$(
  BOARD_STATE="$board_state" PLAN_JSON="$plan_json" \
  REPO_SLUG="${REPO_OWNER}/${REPO_NAME}" python3 - <<'PY'
import json, os, subprocess, sys
sys.path.insert(0, "scripts/agents/lib")
import sprint_plan

board = json.loads(os.environ["BOARD_STATE"])
plan = json.loads(os.environ["PLAN_JSON"])
repo = os.environ["REPO_SLUG"]

def gh(path):
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else []
    except Exception:
        return []

prs = gh(f"repos/{repo}/pulls?state=open&per_page=100")
pr_files = {}
for pr in prs if isinstance(prs, list) else []:
    number = pr.get("number")
    branch = pr.get("head", {}).get("ref", "")
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    issues = set()
    import re
    for text in (branch, body, title):
        for match in re.findall(r"(?:#|issue[-_/])(\d{3,6})", text):
            issues.add(int(match))
    if not issues:
        continue
    files = [f.get("filename", "") for f in gh(f"repos/{repo}/pulls/{number}/files?per_page=100")]
    for issue in issues:
        pr_files.setdefault(issue, []).extend(files)

flight = []
for issue in board.get("in_flight", []):
    files = pr_files.get(issue) or sprint_plan.expected_files(plan, issue)
    flight.append({"issue": issue, "files": files})
print(json.dumps(flight))
PY
)"

# ---- relationships eligibility, for the candidates we might actually take --
blocked_json="$(
  BOARD_STATE="$board_state" PLAN_JSON="$plan_json" \
  MAX_LOOKUPS="$MAX_BLOCKER_LOOKUPS" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, "scripts/agents/lib")
import pull_next_issue, sprint_plan

board = json.loads(os.environ["BOARD_STATE"])
plan = json.loads(os.environ["PLAN_JSON"])
order = pull_next_issue._ordered_candidates(board.get("candidates", []), sprint_plan.plan_order(plan))
print(json.dumps([c["issue"] for c in order][: int(os.environ["MAX_LOOKUPS"])]))
PY
)"

blocked_map="{}"
lookup_list="$(printf '%s' "$blocked_json" | jq -r '.[]')"
if [[ -n "$lookup_list" ]]; then
  entries=""
  while read -r n; do
    [[ -n "$n" ]] || continue
    # Only well-formed issue numbers: a helper that degrades to an error
    # string must not become a blocker list the decision then chokes on.
    open_blockers="$(open_blocked_by_issues "$n" | grep -E '^[0-9]+$' | paste -sd, - || true)"
    [[ -n "$open_blockers" ]] || continue
    entries="${entries}${entries:+,}\"$n\":[${open_blockers}]"
  done <<<"$lookup_list"
  blocked_map="{${entries}}"
fi

decision="$(
  jq -n \
    --argjson plan "$plan_json" \
    --argjson board "$board_state" \
    --argjson in_flight "$in_flight" \
    --argjson blocked_by "$blocked_map" \
    --arg backlog "$STATUS_BACKLOG" \
    --arg wip "$WIP_LIMIT" \
    --arg exclude "${EXCLUDE_ISSUES:-}" \
    --arg owner "${HUMAN_OWNER:-}" \
    --arg scrum "${SCRUM_AGENT:-}" \
    --arg dev "${DEV_AGENT:-}" \
    '{plan: $plan,
      candidates: $board.candidates,
      in_flight: $in_flight,
      blocked_by: $blocked_by,
      status_backlog: $backlog,
      wip_limit: ($wip | tonumber),
      exclude: ($exclude | split(",") | map(select(length > 0) | tonumber)),
      roles: {owner: $owner, scrum: $scrum, dev: $dev}}' \
  | python3 "$DIR/lib/pull_next_issue.py"
)"

log "$(printf '%s' "$decision" | jq -r '.reason')"
printf '%s' "$decision" | jq -r '.considered[]? | "[pull] skipped #\(.issue): \(.skipped)\(if .detail then " (" + .detail + ")" else "" end)"' >&2 || true

if [[ -n "$EXPLAIN_FILE" ]]; then
  printf '%s\n' "$decision" >"$EXPLAIN_FILE"
fi

printf '%s' "$decision" | jq -r '.issue // empty'
