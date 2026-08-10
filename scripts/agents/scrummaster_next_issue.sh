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
                    pulls in work from outside the active sprint. The
                    boundary is hard (#3706): issues in a future sprint, or
                    with no sprint set, are skipped with a log line and are
                    never dispatched automatically -- there is no
                    release-wide fall-through. Without this flag, behavior
                    is unchanged from before #3480 (Sprint is not considered
                    at all), which is what a human `READY_FOR_NEXT_ISSUE`
                    kick uses to pull work forward deliberately.
  -h, --help       Show this help

Environment:
  EXCLUDE_ISSUES   Comma-separated issue numbers to skip as candidates
                    (#3665). Lets a caller retry selection within one run to
                    fall through to the next eligible issue after an
                    earlier candidate turned out to be unclaimable, without
                    re-selecting the same blocked issue forever.
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

# Release wall (outer boundary): when a release tracking issue is
# configured, selection NEVER crosses into another release's work -- every
# candidate's milestone must carry the current release's version (parsed
# from the tracking issue's title, e.g. "Release v2.0.0"). This applies to
# manual kicks too: agents merge to RELEASE_BRANCH, so starting next-release
# work before the owner performs the release ceremony (new branch + new
# tracking issue + repo-var flip) would land it on the wrong branch.
#
# Inside that wall, --sprint-scoped is a HARD boundary as well, not a
# preference (owner policy 2026-08-10, #3706): there is no release-wide
# fall-through when the active sprint drains. The earlier fall-through was
# introduced with a "sprint scoping is soft" rationale attributed to an
# owner decision of 2026-07-31; the owner has since stated that attribution
# was wrong, and that the auto loop is bound by the current sprint.
RELEASE_VERSION=""
if [[ -n "${RELEASE_ISSUE_NUMBER:-}" ]]; then
  RELEASE_VERSION="$(release_version_from_issue "$RELEASE_ISSUE_NUMBER" 2>/dev/null || echo "")"
  if [[ -n "$RELEASE_VERSION" ]]; then
    log "Release wall: only issues whose milestone carries '${RELEASE_VERSION}' are eligible."
  else
    log "Release issue #${RELEASE_ISSUE_NUMBER} title has no vX.Y.Z version -- release wall disabled."
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
    RELEASE_VERSION="$RELEASE_VERSION" \
    RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}" \
    EXCLUDE_ISSUES="${EXCLUDE_ISSUES:-}" \
    python3 "$DIR/lib/summarize_backlog_page.py" "$json_file"
}

tmp="$(mktemp)"
# Per-page {sprint title: open Backlog count} objects, accumulated during a
# sprint-scoped pass so an empty-handed pass can report exactly what it
# skipped instead of silently pulling it forward (#3706).
skipped_tmp="$(mktemp)"
trap 'rm -f "$tmp" "$skipped_tmp"' EXIT

# One full paging pass over the project items with the current scoping env
# (SPRINT_SCOPED_FLAG / ACTIVE_SPRINT_TITLE / RELEASE_VERSION). Exits the
# script directly when a candidate is selected; returns 1 if the pass
# exhausted all pages without one.
selection_pass() {
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

  if [[ "$SPRINT_SCOPED_FLAG" == "1" ]]; then
    echo "$summary" | jq -c '.sprint_counts' >>"$skipped_tmp"
    other_sprint="$(echo "$summary" | jq -r --arg s "$ACTIVE_SPRINT_TITLE" \
      '[.sprint_counts | to_entries[] | select(.key != $s) | .value] | add // 0')"
    if [[ "${other_sprint:-0}" != "0" ]]; then
      log "Page ${page}: skipped ${other_sprint} Backlog issue(s) outside active sprint '${ACTIVE_SPRINT_TITLE}' (not dispatched automatically)."
    fi
  fi

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

return 1
}

selection_pass || true

# The sprint boundary is hard (#3706): when the active sprint has no
# eligible Backlog work left, selection stops here. It does NOT fall back to
# release-wide selection -- work queued in a future sprint (or with no
# sprint set) waits for that sprint's window to open, or for a deliberate
# human `READY_FOR_NEXT_ISSUE` kick, which runs unscoped and can pull it
# forward on purpose.
if [[ "$SPRINT_SCOPED_FLAG" == "1" ]]; then
  waiting="$(jq -s -r --arg s "$ACTIVE_SPRINT_TITLE" '
    reduce .[] as $p ({}; reduce ($p | to_entries[]) as $e (.; .[$e.key] = ((.[$e.key] // 0) + $e.value)))
    | to_entries
    | map(select(.key != $s and .value > 0))
    | sort_by(.key)
    | map("  - \(if .key == "" then "(no sprint set)" else .key end): \(.value)")
    | join("\n")
  ' "$skipped_tmp" 2>/dev/null || echo "")"
  log "Active sprint '${ACTIVE_SPRINT_TITLE}' has no eligible Backlog work -- stopping at the sprint boundary (no release-wide fall-through, #3706)."
  if [[ -n "$waiting" ]]; then
    log "Backlog waiting outside the active sprint (not dispatched automatically):"
    while IFS= read -r line; do log "$line"; done <<<"$waiting"
  fi
fi

log "No Backlog+OPEN issues found."
exit 1