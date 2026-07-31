#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

log() { echo "[scrum] $*" >&2; }

usage() {
  cat <<'EOF'
Usage:
  scrummaster_next_issue.sh [--select-only] [--sprint-scoped]

Selects the next Backlog issue (lowest Phase, lowest issue number).
By default, automatically starts the issue (Status -> In Progress, assigns to DEV_AGENT).

Options:
  --select-only    Only select and print issue number, don't start it
  --sprint-scoped  Only consider issues whose Sprint field matches the
                    active Sprint iteration (SPRINT_FIELD, default "Sprint").
                    Used by sprint autopilot (#3480) so continuation never
                    pulls in work from outside the active sprint. Without
                    this flag, behavior is unchanged from before #3480
                    (Sprint is not considered at all).
  -h, --help       Show this help
EOF
}

# Parse arguments
SELECT_ONLY=0
SPRINT_SCOPED_FLAG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --select-only)
      SELECT_ONLY=1
      shift
      ;;
    --sprint-scoped)
      SPRINT_SCOPED_FLAG=1
      shift
      ;;
    *)
      echo "[error] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# ---- Init ----
load_config
require_gh_auth

project_id="$(get_project_id)"
log "Project id: ${project_id}"

status_opt_backlog="$(single_select_option_id "$STATUS_FIELD" "$STATUS_BACKLOG")"
[[ -n "$status_opt_backlog" && "$status_opt_backlog" != "null" ]] || _die "Status option not found: ${STATUS_BACKLOG}"
log "Status field '${STATUS_FIELD}' has Backlog option '${STATUS_BACKLOG}'."

SPRINT_FIELD="${SPRINT_FIELD:-Sprint}"
ACTIVE_SPRINT_TITLE=""
if [[ "$SPRINT_SCOPED_FLAG" == "1" ]]; then
  ACTIVE_SPRINT_TITLE="$(iteration_active_title "$SPRINT_FIELD" 2>/dev/null || echo "")"
  if [[ -z "$ACTIVE_SPRINT_TITLE" || "$ACTIVE_SPRINT_TITLE" == "null" ]]; then
    # No active Sprint: sprint scoping has nothing to scope to. Per #3480,
    # "no active sprint" falls back to today's unscoped manual-kick
    # behavior rather than reporting "no eligible work" -- only "autopilot
    # on AND a sprint is active" restricts selection.
    log "No active Sprint on field '${SPRINT_FIELD}' -- falling back to unscoped selection."
    SPRINT_SCOPED_FLAG=0
  else
    log "Sprint-scoped selection: active sprint '${ACTIVE_SPRINT_TITLE}'"
  fi
fi

MAX_PAGES="${MAX_PAGES:-200}"  # growth-safe; stops early once it finds a candidate
log "Pagination: up to ${MAX_PAGES} pages (stop at first candidate page)"

fetch_page() {
  local cursor="${1:-}"
  if [[ -n "$cursor" ]]; then
    log "Fetching page (after cursor)..."
    graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id" -f after="$cursor"
  else
    log "Fetching page (first page)..."
    graphql "$BACKLOG_PAGE_QUERY" -F project="$project_id"
  fi
}

# Summarizes a page and returns the best candidate ON THIS PAGE.
# Output JSON: { total_items, issue_items, open_issues, backlog_open, best_issue }
# Delegates to lib/summarize_backlog_page.py (shared with
# count_sprint_backlog_open() in gh_project.sh, and unit-tested directly in
# tests/unit/test_summarize_backlog_page.py, #3480).
summarize_page_file() {
  local json_file="${1:?json file required}"
  STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
    SPRINT_FIELD="$SPRINT_FIELD" SPRINT_SCOPED="$SPRINT_SCOPED_FLAG" \
    ACTIVE_SPRINT_TITLE="$ACTIVE_SPRINT_TITLE" \
    python3 "$DIR/lib/summarize_backlog_page.py" "$json_file"
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

    if [[ "$SELECT_ONLY" == "1" ]]; then
      log "Select-only mode: printing issue number without starting"
      echo "$best_issue"
      exit 0
    fi

    # Default behavior: start the issue automatically
    log "Starting issue #${best_issue}..."
    # Use absolute path to ensure we find scrummaster_start_issue.sh in same directory
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    "$SCRIPT_DIR/scrummaster_start_issue.sh" "$best_issue"
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