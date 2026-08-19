#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ensure_issue_hygiene.sh — fill-if-missing project hygiene for one issue, and
# the non-completed closure rule (--closure)
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
# THE OTHER RULE (owner rule, 2026-08-18, #3871), under --closure:
#   An issue closed with any state_reason other than `completed` must end up
#   carrying the `Phase X: Rejected` milestone, assigned to the owner, and
#   with every project field cleared. Phase X is how a dead issue leaves the
#   picture (owner rule, 2026-08-19), so the milestone is the only marker it
#   keeps -- a rejected issue that holds a Status still sits in a lane, and
#   one that holds a Sprint still counts against that sprint's population.
#
# That one is deliberately NOT fill-if-missing. A closure that was not a
# completion is a decision about the issue's disposition, and the fields have
# to reflect it whatever they held before -- so it writes over a populated
# Phase, and clears a populated Sprint, on purpose. The two rules share this
# file because they share the board plumbing, and they are kept in separate
# code paths so neither can be read as the other. What --closure never does
# is touch Status: parked `Acceptance Failed` placements are owner signal
# (D-001/D-008), and this rule is not entitled to overwrite them.
#
# Usage: ensure_issue_hygiene.sh ISSUE_NUMBER
#        ensure_issue_hygiene.sh --closure ISSUE_NUMBER
# Env:
#   HYGIENE_SETTLE_SECONDS  seconds to wait before writing (default 60; the
#                           tests set 0 so the suite is not a sleep test)
# ============================================================

MODE="fill"
if [[ "${1:-}" == "--closure" ]]; then
  MODE="closure"
  shift
fi

ISSUE="${1:-}"
if [[ -z "$ISSUE" ]]; then
  echo "usage: $(basename "$0") [--closure] ISSUE_NUMBER" >&2
  exit 2
fi

SETTLE_SECONDS="${HYGIENE_SETTLE_SECONDS:-60}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/agents/lib/gh_project.sh
source "$SCRIPT_DIR/lib/gh_project.sh"
load_config
require_gh_auth

# ---- the closure rule (#3871) ------------------------------------------
# Applied on `issues: closed` when the reason is not `completed`. Everything
# it needs is here and it returns; the fill-if-missing body below never runs
# in this mode.
#
# Why this exists: the rule was being applied by hand. #3870 was closed
# not_planned, its assignee fixed over REST, and its Phase and Sprint left
# for the owner to touch -- because Projects v2 is GraphQL-only and a remote
# Claude session's proxy blocks GraphQL, so the session that closed the issue
# structurally could not finish it.
apply_closure_rule() {
  local state reason item_id assignees rc
  local issue_state_line
  # The milestone is the marker (owner rule, 2026-08-19). It is a GitHub
  # milestone, not a project field -- see `Phase X: Rejected` on the board,
  # and `EXCLUDED_MILESTONES` in scripts/retrospective/build_dashboard.py,
  # which is what makes rejected work drop out of the statistics.
  local phase_milestone="${PHASE_X_MILESTONE:-Phase X: Rejected}"

  # Read into a variable first, then split: a `read < <(gh api …)` process
  # substitution reports the *read's* status, so a failing `gh api` dies under
  # `set -e` with no context instead of this function's `::error::` form.
  issue_state_line="$(
    gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" \
      --jq '"\(.state) \(.state_reason // "")"'
  )" || { echo "::error::Failed reading state of issue #$ISSUE"; return 1; }
  read -r state reason <<<"$issue_state_line"

  if [[ "$state" != "closed" ]]; then
    echo "Issue #$ISSUE is $state, not closed -- the closure rule does not apply."
    return 0
  fi

  if [[ "$reason" == "completed" ]]; then
    echo "✓ Issue #$ISSUE was closed as completed -- untouched by the closure rule."
    return 0
  fi

  if [[ -z "$reason" ]]; then
    # Closing through the API without a reason leaves this null, and a null
    # is not evidence of a non-completion. Treating it as one would let a
    # backfill over historical issues reassign the lot to the owner.
    echo "::notice::Issue #$ISSUE is closed with no recorded state_reason -- treating it as completed."
    echo "Re-close it with an explicit reason if the closure rule should apply."
    return 0
  fi

  echo "Issue #$ISSUE closed as '${reason}' -- applying the closure rule (#3871)."

  # The milestone is set whether or not the issue is on the board: it is the
  # marker, and an off-board issue still has to carry it.
  local current_milestone
  current_milestone="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" \
    --jq '.milestone.title // ""')" || current_milestone=""
  if [[ "$current_milestone" == "$phase_milestone" ]]; then
    echo "✓ Milestone: already '${phase_milestone}'"
  else
    # Never create a milestone (owner rule: agents do not create project
    # metadata). A missing one fails loudly rather than leaving the issue
    # looking handled.
    if ! gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=all&per_page=100" \
         --jq '.[].title' | grep -qxF "$phase_milestone"; then
      echo "::error::Milestone '${phase_milestone}' does not exist -- cannot apply the closure rule to #$ISSUE."
      echo "Create it by hand (agents never create milestones), then re-run."
      return 1
    fi
    gh issue edit "$ISSUE" --milestone "$phase_milestone" >/dev/null \
      || { echo "::error::Failed setting the milestone on issue #$ISSUE"; return 1; }
    echo "✓ Milestone: ${current_milestone:-(none)} -> ${phase_milestone}"
  fi

  # One assignee, the owner (owner rule): assign_issue_verified PATCHes the
  # whole list and reads it back, so "exactly the owner" is checked rather
  # than assumed. An agent assignee left on a dead issue is what makes
  # `_open_issues_assigned_to` miscount later.
  assignees="$(_issue_assignee_logins "$ISSUE" 2>/dev/null || echo "")"
  if [[ "$assignees" == "$HUMAN_OWNER" ]]; then
    echo "✓ Assignee: ${HUMAN_OWNER}, and only ${HUMAN_OWNER}, already assigned"
  else
    assign_issue_verified "$ISSUE" "$HUMAN_OWNER" \
      || { echo "::error::Failed assigning ${HUMAN_OWNER} to issue #$ISSUE"; return 1; }
    echo "✓ Assignee: ${assignees:-(none)} -> ${HUMAN_OWNER}"
  fi

  item_id="$(find_issue_project_item "$ISSUE")"
  if [[ -z "$item_id" ]]; then
    echo "Issue #$ISSUE is not on the ${PROJECT_OWNER}#${PROJECT_NUMBER} board -- nothing to strip."
    echo "Closure rule applied to issue #$ISSUE."
    return 0
  fi

  # Strip EVERY project field. The whole purpose of Phase X is to take dead
  # issues out of the picture (owner rule, 2026-08-19): a rejected issue that
  # keeps a Status sits in a lane, one that keeps a Sprint counts against a
  # sprint's population, and one that keeps Priority/Effort/Module skews the
  # statistics computed over those fields. The milestone is the only marker
  # it needs, and the only thing this leaves behind.
  #
  # Status IS cleared here, which is the one place this rule writes it. The
  # D-001/D-008 rule that lane placement is owner signal governs *live* work;
  # an issue closed as not-planned or duplicate has no lane to be signalled
  # about, and leaving it in one is exactly the debris this removes.
  local field current
  for field in "$STATUS_FIELD" Priority Effort Module Phase Sprint; do
    [[ -n "$field" ]] || continue
    rc=0
    current="$(project_field_value "$item_id" "$field" 2>/dev/null)" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
      # A field this board does not define is not an error: the rule strips
      # what is there, and boards differ.
      echo "· ${field}: not readable on this board -- skipped"
      continue
    fi
    if [[ -z "$current" ]]; then
      echo "✓ ${field}: already clear"
      continue
    fi
    if clear_project_field_value "$item_id" "$field" 2>/dev/null; then
      echo "✓ ${field}: cleared (was '${current}')"
    else
      echo "::error::Failed clearing ${field} on issue #$ISSUE"
      return 1
    fi
  done

  echo "Closure rule applied to issue #$ISSUE."
  return 0
}

if [[ "$MODE" == "closure" ]]; then
  apply_closure_rule
  exit $?
fi

echo "Applying fill-if-missing project hygiene for issue #$ISSUE..."

# ---- what to do when a project read or write fails ---------------------
# Called at top level (never inside a command substitution -- an `exit` in a
# subshell exits only the subshell) whenever a project-item operation fails.
#
# A project item that GitHub can no longer resolve is not this run's failure:
# the card was deleted, or the issue was moved off this board, WHILE hygiene
# was working on it. There is then nothing left to fill and the correct
# outcome is a clean finish. #3811 / run 31926723483 is the case: a support
# ticket that should never have reached the development board was moved off
# it by hand mid-run, and hygiene -- having already set Status and Priority
# -- died on "Could not resolve to a node with the global id 'PVTI_...'",
# leaving an unexplained red run behind a correct human action.
#
# Anything else stays loud. A rate limit or transport error means the item is
# probably fine and simply unwritten, which is a real failure to re-run.
handle_project_item_failure() {
  local context="$1"
  case "$(project_item_state "$ITEM_ID")" in
    gone)
      echo "::notice::Project item for issue #$ISSUE no longer exists (while $context)."
      echo "The issue was removed from the ${PROJECT_OWNER}#${PROJECT_NUMBER} board while hygiene was running —"
      echo "there is nothing further to fill. Finishing cleanly rather than failing the run."
      exit 0
      ;;
    present)
      echo "::error::Failed while $context on issue #$ISSUE (its project item still exists)"
      exit 1
      ;;
    *)
      echo "::error::Failed while $context on issue #$ISSUE, and could not determine whether its project item still exists"
      exit 1
      ;;
  esac
}

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
  # This read is descriptive, not decisive -- every write below re-reads
  # through fill_project_field_if_empty. It runs before ITEM_ID is resolved,
  # so it cannot use handle_project_item_failure; a failure here (including
  # the card vanishing between the lookup and this call) just leaves the
  # board state unreported, and ensure_issue_in_project re-resolves next.
  rc=0
  PRE_STATUS="$(project_field_value "$PRE_ITEM_ID" "$STATUS_FIELD")" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    BOARD_STATE="unreadable"
    echo "Board state: could not read Status on the existing project item for issue #$ISSUE — continuing"
  elif [[ -n "$PRE_STATUS" ]]; then
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
rc=0
CURRENT_STATUS="$(project_field_value "$ITEM_ID" "$STATUS_FIELD")" || rc=$?
[[ "$rc" -eq 0 ]] || handle_project_item_failure "reading Status"
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
    *) handle_project_item_failure "setting Status" ;;
  esac
fi

# ---- Priority ----------------------------------------------------------
rc=0
CURRENT_PRIORITY="$(project_field_value "$ITEM_ID" "Priority")" || rc=$?
[[ "$rc" -eq 0 ]] || handle_project_item_failure "reading Priority"
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
    *) handle_project_item_failure "setting Priority" ;;
  esac
fi

# ---- Effort ------------------------------------------------------------
rc=0
CURRENT_EFFORT="$(project_field_value "$ITEM_ID" "Effort")" || rc=$?
[[ "$rc" -eq 0 ]] || handle_project_item_failure "reading Effort"
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
    *) handle_project_item_failure "setting Effort" ;;
  esac
fi

# ---- Label (Feature by default) ----------------------------------------
# Only when the issue carries no recognized label yet. The single-label rule
# (developer_submit_for_review.sh requires exactly one) means adding Feature on
# top of an existing Improvement/Release Management label created a two-label
# deadlock that had to be trimmed by hand every time (#3390, #3413, #3415).
# Labels are re-read here rather than reused from ISSUE_DATA above, for the
# same reason the project fields are: the label may have been applied since.
# "Real" means "not a workflow-control label" -- real_label_names is the same
# helper the submit script's check uses, so the two cannot disagree. This used
# to be a hardcoded list of four names, which made any label added to the
# project later invisible: an `Agent`-labeled issue looked unlabeled, got
# Feature stamped on top, and deadlocked at submit time exactly as this comment
# describes. A rule about HOW MANY labels has to count labels, not recognise
# names.
LABELS_JSON=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" --jq '.labels')
LABELS="$(real_label_names "$LABELS_JSON" | paste -sd, -)"
if [[ -n "$LABELS" ]]; then
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
rc=0
CURRENT_MODULE="$(project_field_value "$ITEM_ID" "Module")" || rc=$?
[[ "$rc" -eq 0 ]] || handle_project_item_failure "reading Module"
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
    *) handle_project_item_failure "setting Module" ;;
  esac
fi

# ---- Sprint ------------------------------------------------------------
# Current sprint = the latest iteration that has already STARTED, evaluated in
# SPRINT_TIMEZONE (owner rule: sprint midnights are Eastern, not UTC). A failed
# Sprint write must never abort hygiene — Milestone still needs applying, and
# Sprint is "current sprint (if active)".
rc=0
CURRENT_SPRINT="$(project_field_value "$ITEM_ID" "Sprint")" || rc=$?
[[ "$rc" -eq 0 ]] || handle_project_item_failure "reading Sprint"
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
