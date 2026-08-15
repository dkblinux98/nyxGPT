#!/usr/bin/env bash
set -euo pipefail

# dispatch_conflict_resolution.sh — route a conflicted PR (#3801).
#
# A PR that conflicts because the mainline moved under it is routine work,
# not an owner decision. Owner rule 2026-08-15: "merge conflicts shouldn't
# halt progress and shouldn't be escalated to me unless there's truly a
# decision to be made only I can make." So the default action here is to
# hand the conflict to the DEVELOPER AGENT — return the issue to In Progress
# and reassign it, which starts the fix path whose Step 2.5 merges
# `origin/<base>` into the PR branch (never rebases — owner standing rule,
# developer-runbook §2), resolves, re-runs the gates and pushes.
#
# The owner is assigned only when:
#   * the developer agent itself posted CONFLICT_REQUIRES_OWNER_DECISION
#     with the question (an owner-only decision, e.g. two owner-accepted
#     behaviors in semantic contradiction), or
#   * the automated rounds are exhausted (default 3) and the PR is still
#     conflicted, i.e. the loop is not converging.
#
# The routing decision itself is pure and unit-tested in
# scripts/agents/lib/conflict_resolution.py; this script is the GitHub I/O
# around it.
#
# Usage:
#   scripts/agents/dispatch_conflict_resolution.sh <PR> [ISSUE]
#   scripts/agents/dispatch_conflict_resolution.sh <PR> --escalate-from-comment
#
# Options:
#   --escalate-from-comment   Skip the mergeability read and escalate using
#                             the body in $COMMENT_BODY (used by
#                             conflict_owner_escalation.yml when the agent
#                             posts the owner-decision token).
#
# Env:
#   DRY_RUN=1                 Report the decision, mutate nothing.
#   CONFLICT_MAX_ROUNDS       Automated rounds before escalation (default 3).
#   CONFLICT_COOLDOWN_MINUTES Burst guard window (default 45).
#   MERGEABLE_POLL_ATTEMPTS   Polls while GitHub computes mergeability (5).
#
# Always prints one machine-readable result line on stdout:
#   conflict-resolution: <action> pr=<n> issue=<n> reason=<text>

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/gh_project.sh
source "$DIR/lib/gh_project.sh"

usage() { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; }

PR=""
ISSUE=""
ESCALATE_FROM_COMMENT=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --escalate-from-comment) ESCALATE_FROM_COMMENT=1 ;;
    *)
      if [[ -z "$PR" ]]; then PR="$arg"
      elif [[ -z "$ISSUE" ]]; then ISSUE="$arg"
      else usage >&2; exit 2
      fi
      ;;
  esac
done
[[ -n "$PR" ]] || { usage >&2; exit 2; }

load_config
require_gh_auth
require_cmd gh
require_cmd jq
require_cmd python3

MAX_ROUNDS="${CONFLICT_MAX_ROUNDS:-3}"
COOLDOWN_MINUTES="${CONFLICT_COOLDOWN_MINUTES:-45}"
POLL_ATTEMPTS="${MERGEABLE_POLL_ATTEMPTS:-5}"
LIB_PY="$DIR/lib/conflict_resolution.py"

_result_line() {
  local action="$1" reason="$2"
  echo "conflict-resolution: ${action} pr=${PR} issue=${ISSUE:-none} reason=${reason}"
}

# ---- PR facts -------------------------------------------------------------
# REST splits GraphQL's MERGEABLE/CONFLICTING/UNKNOWN enum into
# mergeable (bool/null) + mergeable_state — re-derive it, same shape the rest
# of the pipeline uses (review_accept_and_merge.sh, developer_auto_implement).
_read_pr() {
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR}" --jq '{
    headRefName: .head.ref,
    baseRefName: .base.ref,
    body: (.body // ""),
    mergeable: (if .mergeable == null then "UNKNOWN" elif .mergeable_state == "dirty" then "CONFLICTING" else "MERGEABLE" end),
    state: (if .merged then "MERGED" elif .state == "closed" then "CLOSED" else "OPEN" end)
  }'
}

pr_json="$(_read_pr)" || _die "ERROR: could not read PR #${PR}."
pr_state="$(jq -r '.state' <<<"$pr_json")"
pr_head="$(jq -r '.headRefName' <<<"$pr_json")"
pr_base="$(jq -r '.baseRefName' <<<"$pr_json")"
pr_mergeable="$(jq -r '.mergeable' <<<"$pr_json")"

# GitHub computes mergeability asynchronously: the pull_request webhook
# payload very often carries `mergeable: null`, which is exactly why the old
# handler missed conflicts that appeared after the base moved. Poll instead
# of guessing.
attempt=1
while [[ "$pr_mergeable" == "UNKNOWN" && "$pr_state" == "OPEN" && "$attempt" -lt "$POLL_ATTEMPTS" ]]; do
  sleep 5
  attempt=$((attempt + 1))
  pr_json="$(_read_pr)" || break
  pr_mergeable="$(jq -r '.mergeable' <<<"$pr_json")"
  pr_state="$(jq -r '.state' <<<"$pr_json")"
done

# Issue number: explicit argument wins, else the PR body's "Closes #N"
# (the PR rule every PR in this repo follows).
if [[ -z "$ISSUE" ]]; then
  ISSUE="$(jq -r '.body' <<<"$pr_json" | grep -oiE 'closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true)"
fi

echo "[conflict] PR #${PR}: state=${pr_state}, mergeable=${pr_mergeable}, head=${pr_head}, base=${pr_base}, issue=${ISSUE:-none}" >&2

# ---- decision -------------------------------------------------------------
if [[ "$ESCALATE_FROM_COMMENT" == "1" ]]; then
  question="$(printf '%s' "${COMMENT_BODY:-}" | python3 "$LIB_PY" question)"
  decision="$(jq -n --arg q "$question" \
    '{action: "escalate", reason: "developer agent reported an owner-only decision", rounds: 0, question: $q}')"
else
  # --jq runs once per fetched page, not once over the combined result set —
  # stream the comments across all pages, then slurp in a second jq pass
  # (see AGENTS.md).
  comments="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${PR}/comments" --paginate \
    --jq '.[] | {body: (.body // ""), created_at: .created_at}' 2>/dev/null | jq -s '.' || echo '[]')"
  decision="$(jq -n \
      --argjson comments "${comments:-[]}" \
      --arg mergeable "$pr_mergeable" \
      --arg state "$pr_state" \
      --argjson max_rounds "$MAX_ROUNDS" \
      --argjson cooldown "$COOLDOWN_MINUTES" \
      '{mergeable: $mergeable, state: $state, comments: $comments, max_rounds: $max_rounds, cooldown_minutes: $cooldown}' \
    | python3 "$LIB_PY" decide)"
fi

action="$(jq -r '.action' <<<"$decision")"
reason="$(jq -r '.reason' <<<"$decision")"
rounds="$(jq -r '.rounds' <<<"$decision")"
question="$(jq -r '.question' <<<"$decision")"

echo "[conflict] decision: ${action} (${reason})" >&2

if [[ "$action" == "noop" ]]; then
  _result_line "noop" "$reason"
  exit 0
fi

if [[ -z "$ISSUE" ]]; then
  # No linked issue means no dispatch target (the developer agent is started
  # from the issue, not the PR). Say so on the PR rather than silently
  # dropping the conflict.
  _warn "PR #${PR} has no 'Closes #N' issue link — cannot dispatch a resolution round."
  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    gh pr comment "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --body \
      "⚠️ **Merge conflict** with \`${pr_base}\`, and this PR has no \`Closes #N\` link, so no developer-agent round can be dispatched. Add the link (or resolve by merging \`origin/${pr_base}\` into \`${pr_head}\` — never rebase)." || true
  fi
  _result_line "noop" "no linked issue"
  exit 0
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[conflict] DRY_RUN: would ${action} (rounds=${rounds})" >&2
  _result_line "$action" "$reason"
  exit 0
fi

# ---- dispatch: the default path ------------------------------------------
if [[ "$action" == "dispatch" ]]; then
  ROUND_MARKER="<!-- conflict-resolution-round -->"
  MSG="🔀 **Merge conflict — automated resolution round $((rounds + 1)) dispatched to @${DEV_AGENT}** ${ROUND_MARKER}

PR #${PR} cannot merge into \`${pr_base}\`: the mainline moved while the PR was in review. This is routine and is **not** escalated to @${HUMAN_OWNER} (owner rule, 2026-08-15, #3801).

The developer agent will:
1. \`git fetch origin\` and **MERGE \`origin/${pr_base}\` into \`${pr_head}\`** — **NEVER REBASE** (owner standing rule; see \`agents/runbooks/developer-runbook.md\` §2).
2. Resolve every conflict with judgment: read both sides' intent, keep this PR's feature content **and** the owner-accepted behavior already merged into \`${pr_base}\`; discard neither wholesale.
3. For \`agents/LEDGER.md\`: respect the public/annex split (entry IDs absent by design stay absent — never "restore" one) and, when an entry ID collides with one the mainline already allocated, **RENUMBER YOURS** to the next unused number in that class, keeping both entries. IDs are never reused.
4. Re-run the full verification suite, then push the merge commit (which re-triggers review).

If — and only if — resolving needs a decision only the owner can make (e.g. two owner-accepted behaviors in genuine semantic contradiction), the agent stops and issues the owner-decision token on its own line with the specific question; that is the only route from a conflict to @${HUMAN_OWNER}, other than ${MAX_ROUNDS} non-converging rounds."

  gh pr comment "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --body "$MSG" || true
  issue_comment "$ISSUE" "$MSG" || _warn "Could not comment on issue #${ISSUE}."

  set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS" \
    || _warn "Could not set issue #${ISSUE} Status -> ${STATUS_IN_PROGRESS}."
  assign_and_trigger_developer "$ISSUE"

  echo "[conflict] Dispatched round $((rounds + 1)) (issue #${ISSUE} -> ${STATUS_IN_PROGRESS}, @${DEV_AGENT} reassigned)." >&2
  _result_line "dispatch" "$reason"
  exit 0
fi

# ---- escalate: only when a human genuinely must decide --------------------
ESCALATION_MSG="🚨 **Merge conflict escalated to @${HUMAN_OWNER}**

PR #${PR} conflicts with \`${pr_base}\` and the automated path stopped: ${reason}."
if [[ -n "$question" && "$question" != "null" ]]; then
  ESCALATION_MSG+="

**Decision needed from you:** ${question}"
else
  ESCALATION_MSG+="

No owner-only question was stated — the automated rounds simply did not converge, so the conflict needs a look rather than an answer."
fi
ESCALATION_MSG+="

Resolution is still by **merging \`origin/${pr_base}\` into \`${pr_head}\`** — never a rebase."

gh pr comment "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --body "$ESCALATION_MSG" || true
issue_comment "$ISSUE" "$ESCALATION_MSG" || _warn "Could not comment on issue #${ISSUE}."
assign_issue_verified "$ISSUE" "$HUMAN_OWNER" \
  || _warn "Could not verify issue #${ISSUE} assignment to @${HUMAN_OWNER} — check the assignee manually."
notify_human_escalation "$ISSUE" "Merge conflict needs an owner decision" \
  "${reason}${question:+ — $question}" \
  "Decide the question on PR #${PR}, then reassign to @${DEV_AGENT} to finish the merge (never rebase)." \
  "conflict:${PR}" \
  || _warn "Slack DM escalation failed for issue #${ISSUE} — the comment above stands."

_result_line "escalate" "$reason"
