#!/usr/bin/env bash
set -euo pipefail

# review_ensure_handoff.sh -- dispatch-mode post-review handoff backstop (#3704)
#
# `review_agent_auto_review.yml` executes a review verdict only in reaction to
# a downstream event (the `pull_request_review` submission, or the
# `nyxgpt-structured-review` comment via `issue_comment`). When
# `claude-code-review.yml` runs via `workflow_dispatch` -- the recovery path
# for PRs whose automatic review never fired -- an APPROVE lands fine but a
# REQUEST_CHANGES has been observed to go nowhere: no fix cycle, no huddle,
# no escalation, until a human posts RETRY_IMPLEMENTATION by hand (PRs #3684,
# #3683, #3606 on 2026-08-09/10).
#
# This script closes the loop from inside the review run itself: it waits for
# the event chain to leave a footprint on the PR and, only if none appears,
# executes the same routing decision the primary workflow would have. The
# decision half lives in lib/review_handoff.py (unit-tested); routing comes
# from lib/sprint_calc.py's huddle_routing_decision, so this can never
# disagree with the primary path about loop/huddle/escalate (#3687).
#
# Idempotent: every action it takes writes one of lib/review_handoff.py's
# HANDOFF_MARKERS onto the PR, so a re-run (or a late-firing event chain)
# sees the footprint and no-ops.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  review_ensure_handoff.sh <pr_number>

Description:
  Verifies that the post-review handoff for a REQUEST_CHANGES verdict actually
  started, and performs it if the event chain dropped it.

Behavior:
  - No-op unless the review agent's latest verdict on the PR is
    CHANGES_REQUESTED (APPROVE is handled by the merge path).
  - Polls for up to REVIEW_HANDOFF_WAIT_SECONDS (default 240) for a handoff
    comment posted after the verdict, giving the normal event chain priority.
  - If none appears, executes the #3687 route itself:
      normal   -> issue to In Progress + assigned to the developer agent
      huddle   -> huddle trigger comment
      escalate -> issue and PR handed to the human owner

Environment:
  REVIEW_HANDOFF_WAIT_SECONDS  total wait before repairing (default 240)
  REVIEW_HANDOFF_POLL_SECONDS  poll interval (default 30)
  REVIEW_HANDOFF_DRY_RUN       when "1", print the plan and exit without acting

Notes:
  - Requires ~/.nyxGPT/config.ini (or $NYXGPT_CONFIG_FILE) and gh auth.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi

PR="${1:-}"
[[ -n "$PR" ]] || { usage; exit 2; }

load_config
require_gh_auth
require_cmd gh
require_cmd jq
require_cmd python3

REPO="${REPO_OWNER}/${REPO_NAME}"
WAIT_SECONDS="${REVIEW_HANDOFF_WAIT_SECONDS:-240}"
POLL_SECONDS="${REVIEW_HANDOFF_POLL_SECONDS:-30}"

log() { echo "[handoff] $*" >&2; }

# Both endpoints are paginated: --jq runs once per fetched page, so matching
# objects are streamed across pages first and slurped into one array after
# (see AGENTS.md).
#
# The two payloads are joined with `printf`, a bash *builtin*, so the thread
# is never passed to an external binary in argv. `jq -n --argjson reviews
# "$reviews" ...` would do exactly that, and a single execve argument is
# capped at MAX_ARG_STRLEN (128KB) -- a long thread aborts the backstop with
# "Argument list too long" just when it is needed most (#3736).
fetch_plan() {
  local reviews comments
  reviews="$(gh api "repos/${REPO}/pulls/${PR}/reviews" --paginate --jq '.[]' | jq -s '.')"
  comments="$(gh api "repos/${REPO}/issues/${PR}/comments" --paginate --jq '.[]' | jq -s '.')"
  printf '{"reviews":%s,"comments":%s}' "$reviews" "$comments" \
    | python3 "$DIR/lib/review_handoff.py" plan "$REVIEW_AGENT"
}

# Populated by eval'ing fetch_plan's key=value output.
action=""; reason=""; route=""; escalate_reason=""
disagreement_type=""; request_changes_count=""; loop_number=""

refresh_plan() {
  local plan
  plan="$(fetch_plan)"
  eval "$plan"
}

refresh_plan
if [[ "$action" == "none" ]]; then
  log "Nothing to repair on PR #${PR} (${reason})"
  exit 0
fi

log "PR #${PR}: REQUEST_CHANGES verdict with no handoff yet (route=${route}, type=${disagreement_type}, cycles=${request_changes_count})"
log "Waiting up to ${WAIT_SECONDS}s for the normal event chain before repairing..."

WAITED=0
while [[ "$WAITED" -lt "$WAIT_SECONDS" ]]; do
  sleep "$POLL_SECONDS"
  WAITED=$((WAITED + POLL_SECONDS))
  refresh_plan
  if [[ "$action" == "none" ]]; then
    log "Event chain handled PR #${PR} after ${WAITED}s (${reason}) -- backstop stands down"
    exit 0
  fi
done

if [[ "${REVIEW_HANDOFF_DRY_RUN:-0}" == "1" ]]; then
  log "DRY RUN: would execute action=${action} route=${route} loop=${loop_number}"
  exit 0
fi

# The link, not the sentence (owner rule, 2026-08-19): the native
# closing-issue edge first, the body convention only as a fallback.
ISSUE="$(pr_linked_issue "$PR")"
if [[ -z "$ISSUE" ]]; then
  echo "::error::PR #${PR} closes no issue -- cannot resolve the issue to hand off to" >&2
  exit 1
fi

log "Event chain dropped the handoff -- executing '${action}' for issue #${ISSUE}"

BACKSTOP_NOTE="_Posted by the dispatch-mode review handoff backstop (\`review_ensure_handoff.sh\`, #3704): the review verdict was submitted but no handoff followed within ${WAIT_SECONDS}s._"

case "$action" in
  return_to_developer)
    set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
    assign_and_trigger_developer "$ISSUE"

    BODY="$(printf '%s\n\n%s\n\n%s\n%s\n%s\n%s\n%s\n\n%s\n\n%s' \
      "🔄 **Review Agent**: Changes requested (review loop ${loop_number}/3)" \
      "Issue #${ISSUE} has been returned to **In Progress** and assigned to @${DEV_AGENT}." \
      "**Developer Instructions:**" \
      "1. Read the code review comment above to understand what needs to be fixed" \
      "2. Implement the necessary changes" \
      "3. Run all tests locally and fix any test failures (3-try loop)" \
      "4. Commit and push your fixes -- the PR will be automatically re-reviewed" \
      "**Note:** This is review iteration ${loop_number} of 3. After 3 review cycles, the issue will be escalated to @${HUMAN_OWNER:-the owner}." \
      "$BACKSTOP_NOTE")"
    gh pr comment "$PR" --body "$BODY"
    log "✓ Issue #${ISSUE} returned to @${DEV_AGENT}"
    ;;

  huddle)
    if [[ "$disagreement_type" == "b" ]]; then
      REASON_LINE="This is a **judgment call** (design/approach disagreement) -- those never loop through another blind fix cycle, they go straight to a huddle."
    else
      REASON_LINE="This is the **2nd** REQUEST_CHANGES cycle for a verifiable defect that still hasn't converged -- instead of a 3rd blind retry, the team huddles first."
    fi
    BODY="$(printf '%s\n\n%s\n\n%s\n%s\n%s\n%s\n\n%s\n\n%s' \
      "🤝 **Review Agent**: Huddle triggered" \
      "$REASON_LINE" \
      "**Review position:** see the code review comment above on this PR -- that is the review agent's position." \
      "**Next:** @${DEV_AGENT}, post a written position (what you believe the problem is, what was tried, what you propose) rather than attempting another fix. A fresh scrummaster mediation run will then read this thread and decide: proceed as-is / change approach / descope / escalate to owner." \
      "Issue #${ISSUE} stays in **In Review** status pending the huddle decision." \
      "See agents/runbooks/review-runbook.md for the huddle protocol (#3687)." \
      "HUDDLE_TRIGGERED" \
      "$BACKSTOP_NOTE")"
    gh pr comment "$PR" --body "$BODY"
    log "✓ Huddle triggered for issue #${ISSUE}"
    ;;

  escalate)
    ASSIGN_OK=1
    assign_issue_verified "$ISSUE" "$HUMAN_OWNER" || ASSIGN_OK=0
    gh pr edit "$PR" --add-assignee "$HUMAN_OWNER" || true

    if [[ "$escalate_reason" == "spec_ambiguity" ]]; then
      HEADLINE="⚠️ **Review Agent**: Escalated immediately — spec ambiguity (#3687 huddle protocol, type c)"
      DETAIL="@${HUMAN_OWNER} The review agent classified this REQUEST_CHANGES round as a spec ambiguity: the issue itself is unclear, or resolving it needs owner authority no agent conversation can supply. Escalating at cycle zero rather than looping."
      NOTIFY_DIAGNOSIS="Review agent classified a REQUEST_CHANGES round on PR #${PR} as spec ambiguity -- the issue is unclear, or needs owner authority no agent conversation can supply."
    else
      HEADLINE="⚠️ **Review Agent**: Escalated after 3 review cycles"
      DETAIL="@${HUMAN_OWNER} This PR has gone through 3 review cycles with requested changes, but issues persist."
      NOTIFY_DIAGNOSIS="PR #${PR} has gone through 3 review cycles with requested changes and issues still remain."
    fi

    BODY="$(printf '%s\n\n%s\n\n%s\n%s\n%s\n\n%s\n\n%s' \
      "$HEADLINE" \
      "$DETAIL" \
      "**Status:**" \
      "- Issue #${ISSUE} remains in **In Review** status" \
      "- Issue and PR assigned to you" \
      "[View all review comments](https://github.com/${REPO}/pull/${PR})" \
      "$BACKSTOP_NOTE")"
    gh pr comment "$PR" --body "$BODY"

    # Best-effort, exactly as on the primary escalation path: neither the
    # autopilot kick nor the Slack DM failing may block the escalation.
    # shellcheck disable=SC2015  # deliberate: any failure in the chain logs
    # a warning and the escalation itself still stands.
    sprint_autopilot_kick "$ISSUE" escalated \
      && notify_human_escalation "$ISSUE" 'review-escalation' "$NOTIFY_DIAGNOSIS" \
           "Review PR #${PR}: merge if acceptable, give the developer specific guidance, or close it." \
           "${ISSUE}:review_escalation" \
      || log "WARNING: autopilot kick / human notification failed for issue #${ISSUE} -- escalation itself is unaffected"

    if [[ "$ASSIGN_OK" != "1" ]]; then
      echo "::error::Escalation comment posted for issue #${ISSUE}, but the assignment to @${HUMAN_OWNER} could not be verified" >&2
      exit 1
    fi
    log "✓ Issue #${ISSUE} escalated to @${HUMAN_OWNER} (${escalate_reason})"
    ;;

  *)
    echo "::error::Unexpected handoff action '${action}' for PR #${PR}" >&2
    exit 1
    ;;
esac
