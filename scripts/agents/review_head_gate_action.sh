#!/usr/bin/env bash
set -euo pipefail

# review_head_gate_action.sh <pr> <head_sha> <state> <check_names>
#
# What the review trigger does when the head it was asked to review is not
# reviewable (#3971). `state` is the answer `await_required_checks` gave:
#
#   failed  - a required check on this head concluded failure. A red head is
#             the developer's problem, not the reviewer's: hand it back by
#             ASSIGNMENT (D-028 -- assignment is the lever, the comment carries
#             the findings) and spend no review invocation on relaying it.
#             ~36% of blocking findings in the 2026-08-13..19 window were this
#             relay, at ~18M tokens per reject/re-fix round-trip.
#
#   timeout - required checks were still pending when the wait cap expired.
#             Nobody is at fault, so nothing is handed back and no verdict is
#             posted; CI itself is stuck, which is an owner matter. Say so
#             once and notify.
#
# BOUNDED PER HEAD SHA, which is what stops the obvious loop: hand back -> the
# developer round re-requests review on the SAME unchanged head -> hand back
# again, forever. The first hand-back for a SHA leaves a marker; a second
# arrival for that same SHA escalates to the owner instead of repeating
# itself. A new push produces a new SHA and a fresh budget, exactly as it
# should.
#
# Executed by tests/test_reviewable_head_gate.sh against a stub `gh`.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

PR="${1:-}"
SHA="${2:-}"
STATE="${3:-}"
NAMES="${4:-}"

if [[ -z "$PR" || -z "$SHA" || -z "$STATE" ]]; then
  echo "[error] usage: review_head_gate_action.sh <pr> <head_sha> <failed|timeout> <check_names>" >&2
  exit 2
fi

load_config
require_gh_auth
require_cmd jq

REPO="${REPO_OWNER}/${REPO_NAME}"

# All comment bodies on the PR, once. Markers are read out of this rather than
# out of a per-marker API call, so the two lookups below cost one request.
# --jq runs once per fetched page -- stream the bodies, then concatenate
# (AGENTS.md).
pr_comment_bodies() {
  gh api "repos/${REPO}/issues/${PR}/comments" --paginate --jq '.[].body' 2>/dev/null || true
}

BODIES="$(pr_comment_bodies)"

_has_marker() {
  printf '%s' "$BODIES" | grep -Fq -- "$1"
}

case "$STATE" in
  failed)
    HANDBACK_MARKER="<!-- ci-red-handback: ${SHA} -->"
    ESCALATED_MARKER="<!-- ci-red-escalated: ${SHA} -->"

    ISSUE=""
    if ! ISSUE="$(pr_linked_issue "$PR")"; then
      # pr_linked_issue distinguishes "no issue" from "could not read"; a read
      # failure must not be reported as an issue-less PR.
      _warn "Could not read the linked issue for PR #${PR}; commenting without the hand-back."
      ISSUE=""
    fi

    if ! _has_marker "$HANDBACK_MARKER"; then
      BODY="🔴 **Review gate**: required checks are failing on this head — no review was run.

Failing required check(s) on \`${SHA}\`: \`${NAMES}\`

A red head is the developer's problem, not the reviewer's (#3971). GitHub already shows this on the PR page, so no review invocation was spent restating it, and this is **not** a REQUEST_CHANGES round — the review cycle counter is untouched.

**What to do:** fix the failing check(s), push, and the review starts on its own. If the same failure reproduces on the base branch without this change, re-submit with \`developer_submit_for_review.sh --ci-override \"<reason>\"\` — the reason is recorded in the PR body and verified by the review agent.

${HANDBACK_MARKER}"

      gh pr comment "$PR" --body "$BODY" >/dev/null \
        || _die "Could not comment the red-head hand-back on PR #${PR}"

      if [[ -n "$ISSUE" ]]; then
        assign_and_trigger_developer "$ISSUE" \
          || _warn "Could not assign issue #${ISSUE} back to the developer agent"
        echo "[gate] PR #${PR}: red head handed back to the developer (issue #${ISSUE})" >&2
      else
        echo "[gate] PR #${PR}: red head reported; no linked issue to hand back to" >&2
      fi
      exit 0
    fi

    if ! _has_marker "$ESCALATED_MARKER"; then
      # Second arrival on the SAME head: the developer round has already been
      # given this head once and it is still red. Another hand-back would be
      # the same round again; the owner is who can unstick it.
      HUMAN="${HUMAN_OWNER:-}"
      BODY="🛑 **Review gate**: this head is still red after a hand-back — escalating.

Failing required check(s) on \`${SHA}\`: \`${NAMES}\`

The developer round was already handed this exact head (see the earlier gate comment) and the head has not changed. Repeating the hand-back would repeat the round, so this stops here.

@${HUMAN} — the check(s) above need a decision: fix, disable, or accept via \`--ci-override\` with a reason the reviewer can verify.

${ESCALATED_MARKER}"

      gh pr comment "$PR" --body "$BODY" >/dev/null \
        || _die "Could not comment the red-head escalation on PR #${PR}"

      if [[ -n "$ISSUE" && -n "$HUMAN" ]]; then
        assign_issue_verified "$ISSUE" "$HUMAN" \
          || _warn "Could not verify issue #${ISSUE} assigned to @${HUMAN}"
        notify_human_escalation "$ISSUE" "red-head" \
          "Required check(s) ${NAMES} are failing on PR #${PR} head ${SHA}, and the head is unchanged since the developer round was handed it." \
          "Fix the failing check(s) on PR #${PR}, or re-submit with a verifiable --ci-override reason." \
          "${ISSUE}:red_head" \
          || _warn "Slack notification for the red-head escalation failed (comment stands)"
      fi
      echo "[gate] PR #${PR}: red head escalated to @${HUMAN}" >&2
      exit 0
    fi

    echo "[gate] PR #${PR}: red head already handed back and escalated for ${SHA} -- standing down" >&2
    ;;

  timeout)
    TIMEOUT_MARKER="<!-- ci-pending-timeout: ${SHA} -->"
    if _has_marker "$TIMEOUT_MARKER"; then
      echo "[gate] PR #${PR}: pending-timeout already reported for ${SHA} -- standing down" >&2
      exit 0
    fi

    BODY="⏳ **Review gate**: required checks are still pending — the review has not started.

Pending required check(s) on \`${SHA}\`: \`${NAMES}\`

A pending head is nobody's problem yet, so nothing was rejected and no review invocation was spent (#3971). The gate waited and gave up; a required check that has not concluded by now means CI is stuck rather than that this PR is wrong.

**To restart the review once the checks finish:** comment \`@review\` on this PR.

${TIMEOUT_MARKER}"

    gh pr comment "$PR" --body "$BODY" >/dev/null \
      || _die "Could not comment the pending-timeout notice on PR #${PR}"

    ISSUE=""
    pr_linked_issue "$PR" >/dev/null 2>&1 && ISSUE="$(pr_linked_issue "$PR")"
    if [[ -n "$ISSUE" ]]; then
      notify_human_escalation "$ISSUE" "ci-stuck" \
        "Required check(s) ${NAMES} on PR #${PR} head ${SHA} had not concluded when the review gate's wait expired." \
        "Check whether CI is stuck; comment @review on PR #${PR} to start the review once they finish." \
        "${ISSUE}:ci_stuck" \
        || _warn "Slack notification for the pending timeout failed (comment stands)"
    fi
    echo "[gate] PR #${PR}: pending timeout reported for ${SHA}" >&2
    ;;

  *)
    echo "[error] unknown gate state '$STATE' (expected failed|timeout)" >&2
    exit 2
    ;;
esac
