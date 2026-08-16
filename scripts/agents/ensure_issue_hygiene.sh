#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ensure_issue_hygiene.sh — fill-if-missing project hygiene for one issue
#
# Invoked by .github/workflows/ensure_project_hygiene.yml on `issues: opened`.
# It lives here rather than inline in the workflow so the invariant below can
# be executed against a stubbed `gh` (tests/test_issue_hygiene.sh) instead of
# only being read.
#
# THE INVARIANT (owner rule, 2026-08-16, #3816):
#   On a first run over an issue, hygiene must not change an
#   already-populated field. It fills blanks; it never overwrites.
#
# Why the previous shape was not enough: #3666 already made every field
# fill-if-missing, but each field's check and its write were separated by the
# rest of the job (Milestone's check ran several API calls before its write).
# A deliberate write landing in that window was silently overwritten — #3814
# lost its Status to a default `Backlog` on 2026-08-16 by about a second,
# while #3813, same code, survived. Two things close it here:
#
#   1. A settle wait before any write, so the actor that created the issue
#      (create_issue.sh, handle_acceptance_failure.yml, a human filling
#      fields in the UI) gets a head start. This is the PR half of the same
#      workflow's long-standing pattern: wait, re-read, bail if it appeared.
#   2. Every write goes through fill_project_field_if_empty, which re-reads
#      the field immediately before the mutation. The value reported in the
#      log is the caller's earlier read; the value that DECIDES is the one
#      taken a single round trip before the write.
#
# Usage: ensure_issue_hygiene.sh ISSUE_NUMBER
# Env:
#   HYGIENE_SETTLE_SECONDS  seconds to wait before writing (default 60; the
#                           tests set 0 so the suite is not a sleep test)
# ============================================================

ISSUE="${1:-}"
if [[ -z "$ISSUE" ]]; then
  echo "usage: $(basename "$0") ISSUE_NUMBER" >&2
  exit 2
fi

SETTLE_SECONDS="${HYGIENE_SETTLE_SECONDS:-60}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/agents/lib/gh_project.sh
source "$SCRIPT_DIR/lib/gh_project.sh"
load_config
require_gh_auth

echo "Applying fill-if-missing project hygiene for issue #$ISSUE..."

# ---- board state, before we add anything -------------------------------
# "Not on the board yet" and "on the board with no Status" are different
# situations that both read back as an empty Status (#3816). The first is a
# genuinely fresh issue; the second means another actor already created the
# card and may be part-way through populating it, or the owner cleared the
# field on purpose. Record which one this is instead of collapsing them.
PRE_ITEM_ID="$(find_issue_project_item "$ISSUE")"
if [[ -z "$PRE_ITEM_ID" ]]; then
  BOARD_STATE="new"
  echo "Board state: issue #$ISSUE is not on the project board yet — adding it"
else
  PRE_STATUS="$(project_field_value "$PRE_ITEM_ID" "$STATUS_FIELD")"
  if [[ -n "$PRE_STATUS" ]]; then
    BOARD_STATE="populated"
    echo "Board state: issue #$ISSUE is already on the board with Status '$PRE_STATUS'"
  else
    BOARD_STATE="existing-unset"
    echo "Board state: issue #$ISSUE is already on the board with no Status — another actor may be mid-write"
  fi
fi

ITEM_ID="$(ensure_issue_in_project "$ISSUE")"
[[ -n "$ITEM_ID" ]] || { echo "::error::Could not resolve a project item for issue #$ISSUE"; exit 1; }
echo "Issue #$ISSUE in project: $ITEM_ID (board state: $BOARD_STATE)"

# ---- settle ------------------------------------------------------------
# Give the creating actor its head start before anything is written. Nothing
# below depends on the sleep for correctness — every write re-reads — but it
# turns the common case from "two writers race" into "the other writer has
# already finished", which is what the log then shows.
if [[ "$SETTLE_SECONDS" -gt 0 ]]; then
  echo "Waiting ${SETTLE_SECONDS}s for any concurrent field writes to land..."
  sleep "$SETTLE_SECONDS"
fi

# Plain issue data (REST; no Projects-v2 dependency). Re-read per field where
# the value decides a write — this copy is for the Module keyword match, which
# only reads title/body.
ISSUE_DATA=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" \
  --jq '{title, body, labels, milestone}')
TITLE=$(echo "$ISSUE_DATA" | jq -r '.title')
BODY=$(echo "$ISSUE_DATA" | jq -r '.body // ""')

FILLED=()

# Every field below is fill-if-missing: project_field_value is the ONLY signal
# consulted (owner design principle, #3666) -- a field that already carries any
# value, however it got there, is left untouched. The decisive read happens
# inside fill_project_field_if_empty, immediately before the write (#3816), so
# a value that appears between the report below and the mutation still wins.

# ---- Status ------------------------------------------------------------
CURRENT_STATUS="$(project_field_value "$ITEM_ID" "$STATUS_FIELD")"
if [[ -n "$CURRENT_STATUS" ]]; then
  echo "✓ Status: already '$CURRENT_STATUS' — leaving as-is"
else
  rc=0
  fill_project_field_if_empty "$ITEM_ID" "$STATUS_FIELD" "$STATUS_BACKLOG" || rc=$?
  case "$rc" in
    0)
      echo "✓ Status: $STATUS_BACKLOG (was unset)"
      FILLED+=("Status: $STATUS_BACKLOG")
      ;;
    2)
      echo "✓ Status: a value appeared while hygiene was running — leaving it as-is"
      ;;
    *)
      echo "::error::Failed to set Status on issue #$ISSUE"
      exit 1
      ;;
  esac
fi

# ---- Priority ----------------------------------------------------------
CURRENT_PRIORITY="$(project_field_value "$ITEM_ID" "Priority")"
if [[ -n "$CURRENT_PRIORITY" ]]; then
  echo "✓ Priority: already '$CURRENT_PRIORITY' — leaving as-is"
else
  rc=0
  fill_project_field_if_empty "$ITEM_ID" "Priority" "P1 - High" || rc=$?
  case "$rc" in
    0)
      echo "✓ Priority: P1 - High (was unset)"
      FILLED+=("Priority: P1 - High")
      ;;
    2) echo "✓ Priority: a value appeared while hygiene was running — leaving it as-is" ;;
    *)
      echo "::error::Failed to set Priority on issue #$ISSUE"
      exit 1
      ;;
  esac
fi

# ---- Effort ------------------------------------------------------------
CURRENT_EFFORT="$(project_field_value "$ITEM_ID" "Effort")"
if [[ -n "$CURRENT_EFFORT" ]]; then
  echo "✓ Effort: already '$CURRENT_EFFORT' — leaving as-is"
else
  rc=0
  fill_project_field_if_empty "$ITEM_ID" "Effort" "XS" || rc=$?
  case "$rc" in
    0)
      echo "✓ Effort: XS (was unset)"
      FILLED+=("Effort: XS")
      ;;
    2) echo "✓ Effort: a value appeared while hygiene was running — leaving it as-is" ;;
    *)
      echo "::error::Failed to set Effort on issue #$ISSUE"
      exit 1
      ;;
  esac
fi

# ---- Label (Feature by default) ----------------------------------------
# Only when the issue carries no recognized label yet. The single-label rule
# (developer_submit_for_review.sh requires exactly one) means adding Feature on
# top of an existing Improvement/Release Management label created a two-label
# deadlock that had to be trimmed by hand every time (#3390, #3413, #3415).
# Labels are re-read here rather than reused from ISSUE_DATA above, for the
# same reason the project fields are: the label may have been applied since.
LABELS=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" --jq '[.labels[].name] | join(" ")')
if echo "$LABELS" | grep -qE "Acceptance Failure|Improvement|Release Management|Feature"; then
  echo "✓ Label: already labeled ($LABELS), leaving as-is"
else
  gh issue edit "$ISSUE" --add-label "Feature"
  echo "✓ Label: Feature (was unset)"
  FILLED+=("Label: Feature")
fi

# ---- Module ------------------------------------------------------------
# Determined from title/body keywords, only when unset. The sre branch runs
# FIRST: observability/SRE issues routinely mention "web"/"api"/"log" in
# passing, and the old greedy web|ui first-match mis-stamped them (e.g. #3415
# stamped web-ui because its body said "Web tier"). The project's Module
# option was renamed observability -> sre (owner, 2026-07-29).
CURRENT_MODULE="$(project_field_value "$ITEM_ID" "Module")"
if [[ -n "$CURRENT_MODULE" ]]; then
  echo "✓ Module: already '$CURRENT_MODULE' — leaving as-is"
else
  MODULE=""
  if echo "$TITLE $BODY" | grep -qiE "\bsre\b|observab|monitor|metric|grafana|prometheus|loki|jaeger|glitchtip|self.heal|tracing|instrumentation"; then
    MODULE="sre"
  elif echo "$TITLE $BODY" | grep -qiE "web|ui|frontend|react|vue"; then
    MODULE="web-ui"
  elif echo "$TITLE $BODY" | grep -qiE "api|endpoint|rest|graphql"; then
    MODULE="api"
  elif echo "$TITLE $BODY" | grep -qiE "rag|embedding|vector|semantic"; then
    MODULE="rag"
  elif echo "$TITLE $BODY" | grep -qiE "cli|command.line|terminal"; then
    MODULE="cli"
  elif echo "$TITLE $BODY" | grep -qiE "tui|terminal.ui"; then
    MODULE="tui"
  elif echo "$TITLE $BODY" | grep -qiE "test|testing|pytest"; then
    MODULE="testing"
  elif echo "$TITLE $BODY" | grep -qiE "doc|documentation|readme"; then
    MODULE="documentation"
  elif echo "$TITLE $BODY" | grep -qiE "security|auth|permission"; then
    MODULE="security"
  elif echo "$TITLE $BODY" | grep -qiE "logging|\blogs?\b"; then
    # Logging/observability catch-all that didn't match the sre branch above
    # (option renamed observability -> sre).
    MODULE="sre"
  else
    MODULE="api" # Default fallback
  fi

  rc=0
  fill_project_field_if_empty "$ITEM_ID" "Module" "$MODULE" || rc=$?
  case "$rc" in
    0)
      echo "✓ Module: $MODULE (was unset)"
      FILLED+=("Module: $MODULE")
      ;;
    2) echo "✓ Module: a value appeared while hygiene was running — leaving it as-is" ;;
    *)
      echo "::error::Failed to set Module on issue #$ISSUE"
      exit 1
      ;;
  esac
fi

# ---- Sprint ------------------------------------------------------------
# Current sprint = the latest iteration that has already STARTED, evaluated in
# SPRINT_TIMEZONE (owner rule: sprint midnights are Eastern, not UTC). A failed
# Sprint write must never abort hygiene — Milestone still needs applying, and
# Sprint is "current sprint (if active)".
CURRENT_SPRINT="$(project_field_value "$ITEM_ID" "Sprint")"
if [[ -n "$CURRENT_SPRINT" ]]; then
  echo "✓ Sprint: already '$CURRENT_SPRINT' — leaving as-is"
else
  # iteration_active_title _die()s on a transport/API failure, so a rate limit
  # fails the step loud (red, re-runnable) instead of silently reporting "no
  # active sprint".
  ACTIVE_SPRINT="$(iteration_active_title "Sprint")"
  [[ "$ACTIVE_SPRINT" == "null" ]] && ACTIVE_SPRINT=""

  if [[ -n "$ACTIVE_SPRINT" ]]; then
    rc=0
    fill_project_field_if_empty "$ITEM_ID" "Sprint" "$ACTIVE_SPRINT" || rc=$?
    case "$rc" in
      0)
        echo "✓ Sprint: $ACTIVE_SPRINT (was unset)"
        FILLED+=("Sprint: $ACTIVE_SPRINT")
        ;;
      2) echo "✓ Sprint: a value appeared while hygiene was running — leaving it as-is" ;;
      *) echo "⚠ Sprint: could not set '$ACTIVE_SPRINT' (iteration may have ended) — continuing" ;;
    esac
  else
    echo "⚠ Sprint: No active sprint found — skipping"
  fi
fi

# ---- Milestone ---------------------------------------------------------
# Milestone is an issue attribute, not a project field, so it is read straight
# from the issue. There is no conditional-write API for it either, so the read
# is taken immediately before the write for the same reason as the fields.
CURRENT_MILESTONE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" --jq '.milestone.title // empty')
if [[ -n "$CURRENT_MILESTONE" ]]; then
  echo "✓ Milestone: already '$CURRENT_MILESTONE' — leaving as-is"
else
  # Distinguish an API error from a genuinely empty open-milestone list: a
  # transient failure (e.g. rate limit) must fail the step loud (red,
  # re-runnable) rather than silently reporting "no open milestone found".
  if ! MILESTONES_JSON=$(gh api repos/"$REPO_OWNER"/"$REPO_NAME"/milestones 2>&1); then
    echo "::error::Failed to list milestones for issue #$ISSUE: $MILESTONES_JSON"
    exit 1
  fi

  OPEN_MILESTONE=$(echo "$MILESTONES_JSON" | jq -r \
    'map(select(.state=="open")) | sort_by(.due_on) | .[0].title // empty')

  if [[ -n "$OPEN_MILESTONE" ]]; then
    # Re-read immediately before the write.
    RECHECK_MILESTONE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" --jq '.milestone.title // empty')
    if [[ -n "$RECHECK_MILESTONE" ]]; then
      echo "✓ Milestone: '$RECHECK_MILESTONE' appeared while hygiene was running — leaving it as-is"
    else
      gh issue edit "$ISSUE" --milestone "$OPEN_MILESTONE"
      echo "✓ Milestone: $OPEN_MILESTONE (was unset)"
      FILLED+=("Milestone: $OPEN_MILESTONE")
    fi
  else
    echo "⚠ Milestone: No open milestone found"
  fi
fi

echo ""
echo "✅ Project hygiene complete for issue #$ISSUE"

# Comment only when hygiene actually filled something -- a fully pre-populated
# issue (e.g. create_issue.sh already ran) gets no redundant "configured"
# comment, and neither does an issue whose fields all appeared mid-run.
if [[ "${#FILLED[@]}" -gt 0 ]]; then
  COMMENT_BODY="✅ **Project Hygiene**: filled missing fields on issue #$ISSUE:"$'\n\n'
  for line in "${FILLED[@]}"; do
    COMMENT_BODY+="- ${line}"$'\n'
  done
  gh issue comment "$ISSUE" --body "$COMMENT_BODY"
else
  echo "No fields needed filling — skipping comment"
fi
