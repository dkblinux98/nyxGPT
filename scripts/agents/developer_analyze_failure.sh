#!/usr/bin/env bash
# Phase 2 of the developer workflow's failure handling: ACT on the class Phase
# 1 already decided, and investigate live state only where that actually
# changes the answer.
#
# WHAT THIS USED TO DO, AND WHY IT WOKE THE OWNER (#4179). It classified the
# failure again, from scratch, and worse:
#
#   1. It harvested with `gh run view "$RUN_ID" --log`, which downloads a zip
#      that only exists after the run completes. Called mid-run -- which is the
#      only time this script runs -- it always returns 403, so the harvest came
#      back empty, and an empty harvest wrote `STATUS=UNKNOWN` and exited 1.
#      `developer_auto_implement.yml`'s own Phase 1 comment documents that
#      limitation, two hundred lines above the step that called it.
#   2. Its `case` had no `ci_red` arm, so even WITH the text the class fell to
#      `*)`, which wrote `STATUS=FATAL` and exited 1.
#
# The workflow maps exit 1 to `fix_status=FATAL`, which escalates. On #4138
# (run 37722699004) a required check was red on the branch's head because of a
# one-line bug in that branch's own smoke script; the submit script refused --
# "a red head is not reviewable", #3971's contract that the developer round
# CONTINUES -- Phase 1 classified it correctly as `retriable:ci_red`, and Phase
# 2 escalated it as fatal with the headline "Unrecognized error type."
#
# SO, THREE CHANGES, and the third is the one that stops the next instance:
#
#   * Phase 1's class is an INPUT (argv[3]). No re-classification.
#   * The error text comes from the agent error-detail file the failing script
#     wrote (`read_agent_error_detail`) or from the file Phase 1 recorded what
#     it classified into -- both on this runner. No log API call at all.
#   * `scripts/agents/lib/error_classes.py` -- one table, also read by Phase
#     1's predicates and by the escalation headline -- decides what each class
#     does here. A class with no specific handling DEFERS to Phase 1 instead of
#     declaring itself fatal, because "Phase 2 found nothing" is not a
#     diagnosis.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_analyze_failure.sh <issue_number> <run_id> [error_class]

Acts on a failed developer round, given the class Phase 1 derived for it.

Arguments:
  issue_number - The issue number being worked on
  run_id       - The GitHub Actions run ID that failed (recorded, not read:
                 the run-log API cannot answer mid-run)
  error_class  - Phase 1's classification, e.g. retriable:ci_red. Optional:
                 without it this script classifies the recorded error text
                 itself, and classifies `unknown` when there is none.

Environment:
  NYXGPT_PHASE1_ERROR_FILE - file holding the error text Phase 1 classified
                             (fallback when the failing script recorded none)
  NYXGPT_AGENT_ERROR_FILE  - the agent error-detail file (see gh_project.sh)
  NYXGPT_ANALYSIS_RESULT_FILE - where to write the result (default
                             /tmp/analysis_result.txt)

Exit codes:
  0 - Fixed automatically, safe to continue
  1 - Fatal error, needs human intervention
  2 - Transient / continue the round, retry recommended
  3 - Deferred: nothing to add, Phase 1's classification stands

Outputs to: $NYXGPT_ANALYSIS_RESULT_FILE
  STATUS=FIXED|TRANSIENT|FATAL|DEFER
  ERROR_TYPE=<type>
  ACTION=<what was done>
  DIAGNOSIS=<why it is fatal>              (FATAL only)
  WAIT_SECONDS=<optional, for rate limits>
  CI_RED_CHECKS=<comma-separated check names>   (retriable:ci_red only)
EOF
}

ISSUE="${1:-}"
RUN_ID="${2:-}"
ERROR_CLASS_IN="${3:-}"

[[ -n "$ISSUE" && -n "$RUN_ID" ]] || { usage >&2; exit 2; }

load_config
require_gh_auth
require_cmd jq

RESULT_FILE="${NYXGPT_ANALYSIS_RESULT_FILE:-/tmp/analysis_result.txt}"

# ============================================================
# The evidence: what the failing step recorded, on this runner
# ============================================================
# Both sources are local files. The run-log API is deliberately NOT consulted:
# it cannot answer while the run is in progress, and a harvest that cannot
# answer is what used to be reported as a fatal diagnosis.

ERROR_TEXT="$(read_agent_error_detail)"
if [[ -n "$ERROR_TEXT" ]]; then
  echo "[analyze] Using the failure reason the failing step recorded for itself" >&2
elif [[ -n "${NYXGPT_PHASE1_ERROR_FILE:-}" && -s "${NYXGPT_PHASE1_ERROR_FILE}" ]]; then
  ERROR_TEXT="$(cat "$NYXGPT_PHASE1_ERROR_FILE")"
  echo "[analyze] Using the error text Phase 1 classified (${NYXGPT_PHASE1_ERROR_FILE})" >&2
fi

if [[ -n "$ERROR_CLASS_IN" ]]; then
  ERROR_CLASS="$ERROR_CLASS_IN"
  echo "[analyze] Phase 1 classified this failure: $ERROR_CLASS" >&2
elif [[ -n "$ERROR_TEXT" ]]; then
  ERROR_CLASS="$(classify_error "$ERROR_TEXT")"
  echo "[analyze] No class was passed in; classified the recorded text: $ERROR_CLASS" >&2
else
  ERROR_CLASS="unknown"
  echo "[analyze] No class and no recorded error text -- deferring to Phase 1" >&2
fi

[[ -n "$ERROR_TEXT" ]] && { echo "[analyze] Recorded failure reason:" >&2; printf '%s\n' "$ERROR_TEXT" >&2; }

# Everything after the first colon, for the ERROR_TYPE line the workflow and
# the comment steps read (e.g. "retriable:ci_red" -> "ci_red").
ERROR_TYPE="${ERROR_CLASS#*:}"

# What does the TABLE say to do with this class?
OUTCOME="$(python3 "$DIR/lib/error_classes.py" phase2 "$ERROR_CLASS" 2>/dev/null || echo "defer")"
TABLE_WAIT="$(python3 "$DIR/lib/error_classes.py" wait "$ERROR_CLASS" 2>/dev/null || echo 0)"
EXPLANATION="$(python3 "$DIR/lib/error_classes.py" explanation "$ERROR_CLASS" 2>/dev/null || echo "")"
echo "[analyze] Error-class table: outcome=$OUTCOME wait=$TABLE_WAIT" >&2

_result() {
  printf '%s\n' "$@" > "$RESULT_FILE"
}

# ============================================================
# Act on it
# ============================================================

case "$OUTCOME" in
  fatal)
    echo "[fatal] $ERROR_CLASS: $EXPLANATION" >&2
    _result "STATUS=FATAL" "ERROR_TYPE=$ERROR_TYPE" "DIAGNOSIS=$EXPLANATION"
    exit 1
    ;;

  transient)
    echo "[transient] $ERROR_CLASS: $EXPLANATION" >&2
    if [[ "$TABLE_WAIT" -gt 0 ]]; then
      _result "STATUS=TRANSIENT" "ERROR_TYPE=$ERROR_TYPE" "WAIT_SECONDS=$TABLE_WAIT"
    else
      _result "STATUS=TRANSIENT" "ERROR_TYPE=$ERROR_TYPE"
    fi
    exit 2
    ;;

  continue_round)
    # #3971's contract, delivered: the fix for a red head is ANOTHER DEVELOPER
    # ROUND on the same branch, not a hand-off. Reported as TRANSIENT so the
    # workflow's auto-retry step re-assigns the developer agent -- the
    # assignment IS the next round (#3882) -- bounded by the unforgeable retry
    # budget, which is what keeps "continue" from meaning "forever".
    #
    # The failing check names ride along so the auto-retry comment can name
    # them: a retry that loses the cause just burns the budget. The names come
    # from the refusal the submit script recorded (`red-head-check:` lines,
    # written by `red_head_check_lines`); the NEXT round also re-derives them
    # live from its PR head, which is the unforgeable copy.
    CI_RED_PAIRS="$(printf '%s' "$ERROR_TEXT" \
      | python3 "$DIR/lib/escalation_evidence.py" red-head-checks 2>/dev/null || echo "")"
    CI_RED_CHECKS="$(printf '%s\n' "$CI_RED_PAIRS" | cut -f1 | paste -sd, - | sed 's/^,*//;s/,*$//')"
    CI_RED_URLS="$(printf '%s\n' "$CI_RED_PAIRS" | cut -f2 | grep -v '^$' | paste -sd' ' - || true)"
    echo "[continue] Red head on this branch -- the round continues. Failing checks: ${CI_RED_CHECKS:-unnamed}" >&2
    _result "STATUS=TRANSIENT" \
      "ERROR_TYPE=$ERROR_TYPE" \
      "ACTION=Continuing the developer round on this branch to fix ${CI_RED_CHECKS:-the failing required check(s)}" \
      "CI_RED_CHECKS=${CI_RED_CHECKS:-}" \
      "CI_RED_URLS=${CI_RED_URLS:-}"
    exit 2
    ;;

  investigate)
    : # handled below -- the two classes whose answer depends on live state
    ;;

  *)
    # THE OLD DEFAULT WAS `STATUS=FATAL`. That is the defect: Phase 2 having
    # nothing to add is not evidence of anything, and treating it as a fatal
    # diagnosis is how a class the pipeline calls retriable reached the owner
    # as a page. Defer -- Phase 1's class stands, and `unknown` goes to Phase
    # 3 for a real diagnosis.
    echo "[defer] No Phase 2 handling for '$ERROR_CLASS' -- deferring to Phase 1's classification" >&2
    _result "STATUS=DEFER" \
      "ERROR_TYPE=$ERROR_TYPE" \
      "ACTION=Phase 2 had nothing to add; Phase 1's classification ($ERROR_CLASS) stands"
    exit 3
    ;;
esac

# ============================================================
# investigate: the two classes whose answer is live state
# ============================================================

case "$ERROR_TYPE" in
  issue_closed)
    echo "[analyze] Handling issue_closed error..." >&2

    # Gather context
    ISSUE_STATE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${ISSUE}" \
      --jq '{state: (.state | ascii_upcase), closedAt: .closed_at, assignedToMe: ([.assignees[].login] | contains(["'"$DEV_AGENT"'"]))}')

    STATE=$(echo "$ISSUE_STATE" | jq -r '.state')
    CLOSED_AT=$(echo "$ISSUE_STATE" | jq -r '.closedAt // ""')
    ASSIGNED_TO_ME=$(echo "$ISSUE_STATE" | jq -r '.assignedToMe')

    if [[ "$STATE" != "CLOSED" ]]; then
      echo "[analyze] Issue is now OPEN - problem may have been fixed" >&2
      _result "STATUS=FIXED" "ERROR_TYPE=issue_closed" \
        "ACTION=Issue was reopened (possibly by human)"
      exit 0
    fi

    if [[ "$ASSIGNED_TO_ME" != "true" ]]; then
      echo "[fatal] Issue closed and not assigned to dev agent" >&2
      _result "STATUS=FATAL" "ERROR_TYPE=issue_closed" \
        "DIAGNOSIS=Issue closed and not assigned to developer agent"
      exit 1
    fi

    # Check if closed recently (within last 6 hours)
    if [[ -n "$CLOSED_AT" ]]; then
      # Convert timestamp to seconds (macOS and Linux compatible)
      if [[ "$OSTYPE" == "darwin"* ]]; then
        CLOSED_TIMESTAMP=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${CLOSED_AT}" "+%s" 2>/dev/null || echo "0")
      else
        CLOSED_TIMESTAMP=$(date -d "${CLOSED_AT}" "+%s" 2>/dev/null || echo "0")
      fi
      NOW=$(date +%s)
      HOURS_AGO=$(( (NOW - CLOSED_TIMESTAMP) / 3600 ))

      echo "[analyze] Issue closed $HOURS_AGO hour(s) ago" >&2

      if [[ $HOURS_AGO -lt 6 ]]; then
        # Check for merged PR (REST Search API -- its own rate pool, separate
        # from both core REST and GraphQL)
        MERGED_PR=$(gh api search/issues \
          --method GET \
          -f q="${ISSUE} in:body type:pr state:merged repo:${REPO_OWNER}/${REPO_NAME}" \
          --jq '.items[0].number // ""' || echo "")

        if [[ -n "$MERGED_PR" ]]; then
          echo "[fatal] Issue has merged PR #$MERGED_PR - work complete" >&2
          _result "STATUS=FATAL" "ERROR_TYPE=issue_closed" \
            "DIAGNOSIS=Issue completed in merged PR #$MERGED_PR"
          exit 1
        fi

        # Accidental closure detected - REOPEN
        echo "[fix] Reopening accidentally closed issue..." >&2

        gh issue reopen "$ISSUE" --comment "$(cat <<COMMENT
🤖 **Developer Agent**: Automatically reopening issue.

**Reason:** Issue was assigned for implementation but closed before PR could be created.

**Analysis:**
- Closed: $HOURS_AGO hour(s) ago
- Assigned to: @$DEV_AGENT
- No merged PR found
- Work in progress

This appears to be an accidental closure. Reopening to allow PR submission.
COMMENT
)"

        echo "[fix] Issue reopened successfully" >&2
        _result "STATUS=FIXED" "ERROR_TYPE=issue_closed" \
          "ACTION=Reopened accidentally closed issue"
        exit 0
      else
        echo "[fatal] Issue closed $HOURS_AGO hours ago - likely intentional" >&2
        _result "STATUS=FATAL" "ERROR_TYPE=issue_closed" \
          "DIAGNOSIS=Issue closed more than 6 hours ago - likely intentional"
        exit 1
      fi
    fi

    echo "[fatal] Unable to determine issue closure reason" >&2
    _result "STATUS=FATAL" "ERROR_TYPE=issue_closed" \
      "DIAGNOSIS=Issue closed but unable to determine if accidental"
    exit 1
    ;;

  rate_limit)
    echo "[analyze] Handling rate_limit error..." >&2

    # Get rate limit info
    RATE_INFO=$(gh api rate_limit --jq '.rate' || echo "{}")
    REMAINING=$(echo "$RATE_INFO" | jq -r '.remaining // 0')
    RESET_TIME=$(echo "$RATE_INFO" | jq -r '.reset // 0')

    if [[ "$REMAINING" -gt 0 ]]; then
      echo "[analyze] Rate limit recovered (${REMAINING} calls remaining)" >&2
      _result "STATUS=FIXED" "ERROR_TYPE=rate_limit" "ACTION=Rate limit recovered"
      exit 0
    fi

    # Calculate wait time
    NOW=$(date +%s)
    WAIT_SECONDS=$(( RESET_TIME - NOW ))

    if [[ $WAIT_SECONDS -lt 0 ]]; then
      WAIT_SECONDS=0
    fi

    if [[ $WAIT_SECONDS -gt 3600 ]]; then
      # More than 1 hour - probably a problem
      echo "[fatal] Rate limit won't reset for $(( WAIT_SECONDS / 60 )) minutes" >&2
      _result "STATUS=FATAL" "ERROR_TYPE=rate_limit" \
        "DIAGNOSIS=Rate limit reset time too far in future ($(( WAIT_SECONDS / 60 )) minutes)"
      exit 1
    fi

    echo "[transient] Rate limit - wait $WAIT_SECONDS seconds" >&2
    _result "STATUS=TRANSIENT" "ERROR_TYPE=rate_limit" "WAIT_SECONDS=$WAIT_SECONDS"
    exit 2
    ;;

  *)
    # The table said `investigate` and no investigation exists. That is a
    # table/script disagreement, not a fatal failure of the run --
    # tests/unit/test_error_classes.py fails the build on it, and here it
    # defers like any other unhandled class.
    echo "[defer] The table asks for an investigation of '$ERROR_CLASS' and this script has none" >&2
    _result "STATUS=DEFER" "ERROR_TYPE=$ERROR_TYPE" \
      "ACTION=Phase 2 has no investigation for $ERROR_CLASS; Phase 1's classification stands"
    exit 3
    ;;
esac
