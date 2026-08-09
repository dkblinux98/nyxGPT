#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  review_accept_and_merge.sh [--dry-run] <pr_number_or_url> <issue_number>

Merges the PR into the current release branch (merge commit) and deletes the PR branch, then:
  - If the issue has open native blocked-by dependencies (owner process rule,
    2026-08-04, #3631): Status -> In Review (parked), owner NOT assigned,
    comment explains why and lists the open blockers. The whole dependency
    set moves to Acceptance Testing together once every blocker completes
    (see sweep_parked_blocked_issues.sh).
  - Otherwise: Issue Status -> Acceptance Testing, Issue assignee ->
    HUMAN_OWNER, comment on issue with merge info
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd gh; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

PR="${1:-}"
ISSUE="${2:-}"
if [[ -z "$PR" || -z "$ISSUE" ]]; then usage >&2; exit 2; fi

load_config
require_gh_auth
require_cmd gh
require_cmd jq

echo "[review] ===== Starting merge process for PR #${PR}, Issue #${ISSUE} =====" >&2

# ---- Pre-merge validation ----
echo "[review] Validating PR is mergeable..." >&2

# Get PR details
pr_data="$(gh pr view "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --json headRefName,baseRefName,mergeable,mergeStateStatus,state)"
pr_head_branch="$(echo "$pr_data" | jq -r '.headRefName')"
pr_base_branch="$(echo "$pr_data" | jq -r '.baseRefName')"
pr_mergeable="$(echo "$pr_data" | jq -r '.mergeable')"
pr_merge_state="$(echo "$pr_data" | jq -r '.mergeStateStatus')"
pr_state="$(echo "$pr_data" | jq -r '.state')"

[[ -n "$pr_head_branch" ]] || _die "ERROR: Could not determine PR head branch. PR may not exist."

echo "[review] PR #${PR}: head=$pr_head_branch, base=$pr_base_branch, state=$pr_state" >&2
echo "[review] PR #${PR}: mergeable=$pr_mergeable, mergeStateStatus=$pr_merge_state" >&2

# Check if PR is already merged or closed
if [[ "$pr_state" == "MERGED" ]]; then
  echo "[review] PR #${PR} is already merged. Proceeding to post-merge bookkeeping." >&2
elif [[ "$pr_state" == "CLOSED" ]]; then
  _die "ERROR: PR #${PR} is closed but not merged. Cannot proceed. Manual intervention required."
fi

# Check if PR has merge conflicts
if [[ "$pr_mergeable" == "CONFLICTING" ]]; then
  echo "[review] PR #${PR} has merge conflicts with ${pr_base_branch}." >&2

  # One automated conflict-resolution round before escalating to a human:
  # return the issue to In Progress and reassign the developer agent, whose
  # fix-round path detects the CONFLICTING state and merges the base branch
  # into the PR branch (resolving conflicts, re-running gates). The marker
  # string below is the loop guard -- if a prior automated round already ran
  # on this PR and it is conflicted again, escalate to the human owner.
  CONFLICT_ROUND_MARKER="Automated conflict-resolution round"
  prior_rounds=$(gh pr view "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --json comments \
    --jq "[.comments[] | select(.body | contains(\"${CONFLICT_ROUND_MARKER}\"))] | length" \
    2>/dev/null || echo 0)
  if [[ "${prior_rounds:-0}" -eq 0 ]]; then
    echo "[review] Dispatching automated conflict-resolution round to developer agent..." >&2
    AUTO_MSG="⚠️ **Merge Conflicts Detected** — ${CONFLICT_ROUND_MARKER} dispatched.

PR #${PR} cannot merge into \`${pr_base_branch}\` because the base branch moved while the PR was in review. The developer agent is being reassigned to: merge \`origin/${pr_base_branch}\` into \`${pr_head_branch}\`, resolve the conflicts preserving both sides' intents, re-run all validation gates, and push. The push re-triggers review; if the PR conflicts again after this round, it escalates to @${HUMAN_OWNER}."
    gh pr comment "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --body "$AUTO_MSG" || true
    issue_comment "$ISSUE" "$AUTO_MSG"
    if set_issue_status "$ISSUE" "In Progress" && assign_and_trigger_developer "$ISSUE"; then
      echo "[review] Conflict-resolution round dispatched (issue #${ISSUE} -> In Progress, developer reassigned)." >&2
      exit 0
    fi
    _warn "Could not dispatch conflict-resolution round — falling back to human escalation."
  else
    echo "[review] A prior automated conflict-resolution round already ran (${prior_rounds}x) — escalating to human." >&2
  fi

  echo "[review] ERROR: PR #${PR} has merge conflicts and cannot be merged automatically." >&2
  echo "[review] Assigning to human owner for manual resolution..." >&2

  # Keep issue in "In Review" status and assign to human owner
  # Note: Status is already "In Review" from previous review workflow, so we only reassign
  assign_issue_verified "$ISSUE" "$HUMAN_OWNER" \
    || _warn "Could not verify issue #${ISSUE} assignment to @${HUMAN_OWNER} — check assignee manually."

  # Comment on both PR and issue
  CONFLICT_MSG="⚠️ **Merge Conflicts Detected**

PR #${PR} has merge conflicts with base branch \`${pr_base_branch}\` and cannot be merged automatically.

**To resolve:**
1. \`git checkout ${pr_head_branch}\`
2. \`git pull origin ${pr_base_branch}\`
3. Resolve conflicts in affected files
4. \`git add . && git commit\`
5. \`git push origin ${pr_head_branch}\`

Issue #${ISSUE} has been assigned to @${HUMAN_OWNER} for manual resolution.
The Slack notification workflow should have alerted about this conflict."

  gh pr comment "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --body "$CONFLICT_MSG" || true
  issue_comment "$ISSUE" "$CONFLICT_MSG"

  _die "ERROR: Cannot merge PR #${PR} due to conflicts. Assigned to human owner. Workflow stopped."
fi

# Warn if mergeable state is not clean
if [[ "$pr_mergeable" == "UNKNOWN" ]]; then
  _warn "PR mergeability is UNKNOWN. GitHub may still be calculating. Proceeding anyway..."
fi

# Check if base branch exists
echo "[review] Checking if base branch ${pr_base_branch} exists..." >&2
base_exists=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/branches/${pr_base_branch}" --jq '.name' 2>/dev/null || echo "")
if [[ -z "$base_exists" ]]; then
  _die "ERROR: Base branch '${pr_base_branch}' does not exist. Cannot merge. Manual intervention required."
fi

echo "[review] Pre-merge validation passed" >&2

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] base_branch=$pr_base_branch" >&2
  echo "[dry-run] pr_head_branch=$pr_head_branch" >&2
  echo "[dry-run] would: gh pr merge $PR --merge --delete-branch" >&2
  echo "[dry-run] would: gh issue close $ISSUE" >&2
  dry_run_open_blockers="$(open_blocked_by_issues "$ISSUE" 2>/dev/null || true)"
  if [[ -n "$dry_run_open_blockers" ]]; then
    echo "[dry-run] issue #$ISSUE has open blockers ($(echo "$dry_run_open_blockers" | tr '\n' ' ')) -- would park at '$STATUS_IN_REVIEW', skip owner assignment (#3631)" >&2
  else
    echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_ACCEPTANCE_TESTING'" >&2
    echo "[dry-run] would: assign issue #$ISSUE -> @$HUMAN_OWNER" >&2
  fi
  exit 0
fi

# ---- CRITICAL PATH: Merge and update issue ----
echo "[review] ===== Beginning critical path =====" >&2

# Tracks whether the human-owner handoff (the one bookkeeping step that must
# never fail silently, per #3332) actually landed. The merge itself is
# already done by this point, so a failure here doesn't abort the rest of
# the best-effort steps below — but it does make the job exit non-zero so
# the failure surfaces in the Actions run instead of being buried in a warn.
OWNER_ASSIGN_FAILED=0

# Merge PR via GitHub (this is the critical operation)
echo "[review] Merging PR #${PR}..." >&2
if ! gh pr merge "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --merge --delete-branch 2>&1; then
  echo "[review] ERROR: Merge failed. This is a critical failure." >&2
  echo "[review] System state: PR #${PR} may be partially merged or unchanged." >&2
  echo "[review] Manual intervention required: Check PR #${PR} on GitHub and complete merge manually if needed." >&2
  exit 1
fi
echo "[review] ✓ PR #${PR} merged successfully" >&2

# Close the issue (GitHub state) - required because merge to non-default branch doesn't auto-close
echo "[review] Closing issue #${ISSUE}..." >&2
if ! gh issue close "$ISSUE" --repo "${REPO_OWNER}/${REPO_NAME}" --comment "Merged via review-agent. Issue closed and moved to Acceptance Testing status for stakeholder acceptance." 2>&1; then
  _warn "Failed to close issue #${ISSUE}. PR is merged but issue may still be open. Continuing..."
else
  # Manually trigger auto-check-tasklist workflow as safety measure
  # GitHub's anti-loop security may prevent issues: [closed] from triggering workflows when done via API
  echo "[review] Triggering auto-check-tasklist workflow via repository_dispatch..." >&2
  if ! gh api "repos/${REPO_OWNER}/${REPO_NAME}/dispatches" \
    -X POST \
    -f event_type="issue-closed" \
    -f "client_payload[issue_number]=${ISSUE}" 2>&1; then
    _warn "Failed to trigger auto-check-tasklist workflow manually. Workflow may not run. Continuing..."
  fi
fi

# Blocked-issue park gate (owner process rule, 2026-08-04, #3631): a merged
# issue with open native blocked_by dependencies cannot be meaningfully
# accepted yet -- its own acceptance criteria may depend on the unfinished
# blockers. Park it at STATUS_IN_REVIEW instead of Acceptance Testing and
# skip the owner handoff; sweep_parked_blocked_issues.sh moves the whole
# dependency set to Acceptance Testing together once every blocker
# completes (merged and itself Acceptance Testing or beyond).
PARKED=0
echo "[review] Checking issue #${ISSUE} for open blocking dependencies..." >&2
mapfile -t open_blockers < <(open_blocked_by_issues "$ISSUE")

if [[ "${#open_blockers[@]}" -gt 0 ]]; then
  PARKED=1
  blocker_refs="$(printf '#%s, ' "${open_blockers[@]}")"
  blocker_refs="${blocker_refs%, }"
  echo "[review] Issue #${ISSUE} has open blockers: ${blocker_refs} -- parking at '${STATUS_IN_REVIEW}' instead of '${STATUS_ACCEPTANCE_TESTING}'." >&2

  if ! set_issue_status "$ISSUE" "$STATUS_IN_REVIEW" 2>&1; then
    _warn "Failed to set issue status to ${STATUS_IN_REVIEW}. PR is merged but project status may be incorrect. Continuing..."
  fi

  PARK_MSG="⏸️ **Parked, not Acceptance Testing**: PR #${PR} merged into \`${pr_base_branch}\` and branch deleted, but issue #${ISSUE} has open blocking dependencies (${blocker_refs}) -- its acceptance criteria cannot be meaningfully tested until they land. Status -> ${STATUS_IN_REVIEW}; owner not assigned yet (owner process rule, 2026-08-04). This issue moves to ${STATUS_ACCEPTANCE_TESTING} together with its whole blocker set once every blocker is merged and itself in ${STATUS_ACCEPTANCE_TESTING} or beyond."
  if ! issue_comment "$ISSUE" "$PARK_MSG" 2>&1; then
    _warn "Failed to post park comment. PR is merged and issue parked, but comment missing."
  fi
else
  # Set issue status to Acceptance Testing (owner acceptance gate, 2026-07-31)
  echo "[review] Setting issue #${ISSUE} status to '${STATUS_ACCEPTANCE_TESTING}'..." >&2
  if ! set_issue_status "$ISSUE" "$STATUS_ACCEPTANCE_TESTING" 2>&1; then
    _warn "Failed to set issue status. PR is merged but project status may be incorrect. Continuing..."
  fi

  # Assign issue to human owner
  echo "[review] Assigning issue #${ISSUE} to @${HUMAN_OWNER}..." >&2
  if ! assign_issue_verified "$ISSUE" "$HUMAN_OWNER"; then
    echo "::error::PR #${PR} is merged but issue #${ISSUE} could not be verified as assigned to @${HUMAN_OWNER} — it may still show @${REVIEW_AGENT} as assignee. Manual fix: gh issue edit ${ISSUE} --add-assignee ${HUMAN_OWNER}" >&2
    OWNER_ASSIGN_FAILED=1
  fi

  # Assign PR to human owner
  echo "[review] Assigning PR #${PR} to @${HUMAN_OWNER}..." >&2
  if ! gh api -X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${PR}" -f "assignees[]=${HUMAN_OWNER}" >/dev/null 2>&1; then
    _warn "Failed to assign PR to ${HUMAN_OWNER}. PR is merged but PR assignee may be incorrect. Continuing..."
  fi

  # Post final comment
  echo "[review] Posting completion comment..." >&2
  if ! issue_comment "$ISSUE" "PR #${PR} merged into \`${pr_base_branch}\` and branch deleted. Status -> ${STATUS_ACCEPTANCE_TESTING}. Assigned -> @${HUMAN_OWNER}." 2>&1; then
    _warn "Failed to post comment. PR is merged and issue updated, but comment missing."
  fi
fi

echo "[review] ✓ Critical path complete" >&2

# ---- OPTIONAL: Sprint autopilot kick (#3480) ----
# Self-continuing loop: while the active Sprint still has open Backlog work,
# post READY_FOR_NEXT_ISSUE ourselves instead of waiting for a human to do
# it. The sprint boundary is the stop condition -- once the sprint has no
# open Backlog issues left, post a completion note instead of a kick; a
# human kick is still required to start work outside the sprint. Off by
# default (SPRINT_AUTOPILOT unset/false): behavior is then exactly the
# pre-#3480 manual-kick flow. Best-effort: never fails the merge itself.
echo "[review] ===== Sprint autopilot =====" >&2
SPRINT_AUTOPILOT_VALUE="${SPRINT_AUTOPILOT:-false}"
if [[ "$SPRINT_AUTOPILOT_VALUE" != "true" ]]; then
  echo "[review] Sprint autopilot disabled (SPRINT_AUTOPILOT=${SPRINT_AUTOPILOT_VALUE}) -- no auto-kick." >&2
elif [[ -z "${RELEASE_ISSUE_NUMBER:-}" ]]; then
  _warn "SPRINT_AUTOPILOT is on but RELEASE_ISSUE_NUMBER is not configured -- skipping auto-kick."
elif sprint_autopilot_paused "$RELEASE_ISSUE_NUMBER"; then
  echo "[review] Sprint autopilot paused (PAUSE_SPRINT) -- no auto-kick." >&2
  issue_comment "$RELEASE_ISSUE_NUMBER" "⏸️ **Sprint Autopilot**: Issue #${ISSUE} merged, but autopilot is paused (\`PAUSE_SPRINT\`) -- no automatic kick posted. Comment \`RESUME_SPRINT\` to continue, or \`READY_FOR_NEXT_ISSUE\` to kick manually." \
    || _warn "Failed to post autopilot-paused notice."
else
  # The continue/park decision is RELEASE-gated, not sprint-gated (owner
  # decision 2026-07-31): sprint dates drift and future sprints exist on the
  # board before their release starts, so the boundary is the release
  # version carried by the tracking issue's title and the milestone titles.
  # The autopilot continues while the CURRENT release has open Backlog work
  # (any sprint) and parks when it drains; it never crosses into the next
  # release -- the gate reopens when the owner points RELEASE_ISSUE_NUMBER /
  # RELEASE_BRANCH at the next release as part of the release ceremony.
  release_version="$(release_version_from_issue "$RELEASE_ISSUE_NUMBER" 2>/dev/null || echo "")"
  if [[ -z "$release_version" ]]; then
    _warn "Autopilot: could not parse a vX.Y.Z version from release issue #${RELEASE_ISSUE_NUMBER}'s title -- no auto-kick (conservative stop)."
  else
    remaining="$(count_release_backlog_open "$release_version" 2>/dev/null || echo "")"
    decision="$(python3 "${_LIB_DIR}/sprint_calc.py" autopilot-decision "${remaining:-0}")"
    if [[ "$decision" == "continue" ]]; then
      issue_comment "$RELEASE_ISSUE_NUMBER" "🔁 **Sprint Autopilot**: Issue #${ISSUE} merged. Release ${release_version} still has ${remaining} open Backlog issue(s) -- continuing automatically.

READY_FOR_NEXT_ISSUE" \
        && echo "[review] Autopilot: posted READY_FOR_NEXT_ISSUE (release ${release_version} has ${remaining} remaining)." >&2 \
        || _warn "Autopilot: failed to post READY_FOR_NEXT_ISSUE kick."
    else
      issue_comment "$RELEASE_ISSUE_NUMBER" "🏁 **Sprint Autopilot**: Issue #${ISSUE} merged. Release ${release_version} has no open Backlog issues remaining -- the release backlog is drained and autopilot is parked. Merged work is in **Acceptance Testing** for stakeholder sign-off. Autopilot resumes automatically when \`RELEASE_ISSUE_NUMBER\` and \`RELEASE_BRANCH\` point at the next release; it never crosses a release boundary on its own." \
        && echo "[review] Autopilot: release ${release_version} drained -- parked, no kick." >&2 \
        || _warn "Autopilot: failed to post release-drained note."
    fi
  fi
fi

# ---- OPTIONAL: Branch cleanup ----
echo "[review] ===== Performing optional cleanup =====" >&2

# Check if remote branch still exists (it should be deleted by --delete-branch, but verify)
echo "[review] Checking if remote branch ${pr_head_branch} was deleted..." >&2
head_exists=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/branches/${pr_head_branch}" --jq '.name' 2>/dev/null || echo "")
if [[ -n "$head_exists" ]]; then
  echo "[review] Remote branch ${pr_head_branch} still exists, attempting to delete..." >&2
  if gh api -X DELETE "repos/${REPO_OWNER}/${REPO_NAME}/git/refs/heads/${pr_head_branch}" 2>&1; then
    echo "[review] ✓ Remote branch deleted" >&2
  else
    _warn "Could not delete remote branch ${pr_head_branch}. Manual cleanup may be needed."
  fi
else
  echo "[review] ✓ Remote branch already deleted" >&2
fi

# ---- OPTIONAL: Local git cleanup (only if not in CI) ----
if [[ "${CI:-false}" != "true" && "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  echo "[review] Performing local git cleanup..." >&2

  if [[ -n "$(git status --porcelain 2>/dev/null || echo '')" ]]; then
    _warn "Working tree not clean. Skipping local cleanup. You may need to manually: git checkout ${pr_base_branch} && git pull"
  else
    # Try to update local base branch
    if git fetch origin "$pr_base_branch" >/dev/null 2>&1 && \
       git checkout "$pr_base_branch" >/dev/null 2>&1 && \
       git pull --ff-only origin "$pr_base_branch" >/dev/null 2>&1; then
      echo "[review] ✓ Local ${pr_base_branch} updated" >&2

      # Try to delete local feature branch
      if git show-ref --verify --quiet "refs/heads/${pr_head_branch}" 2>/dev/null; then
        git branch -D "$pr_head_branch" >/dev/null 2>&1 || true
        echo "[review] ✓ Local branch ${pr_head_branch} deleted" >&2
      fi
    else
      _warn "Could not update local git. You may need to manually: git checkout ${pr_base_branch} && git pull"
    fi
  fi
else
  echo "[review] Skipping local git cleanup (running in CI)" >&2
fi

echo "[review] ===== Merge process complete =====" >&2

if [[ "$PARKED" == "1" ]]; then
  echo "SUCCESS: Merged PR #${PR}. Issue #${ISSUE} closed and parked at ${STATUS_IN_REVIEW} (blocked by: ${blocker_refs})."
  exit 0
fi

if [[ "$OWNER_ASSIGN_FAILED" == "1" ]]; then
  echo "FAILURE: Merged PR #${PR} and closed issue #${ISSUE}, but the @${HUMAN_OWNER} assignment could not be verified. See the ::error:: above — manual assignee fix required." >&2
  exit 1
fi

echo "SUCCESS: Merged PR #${PR}. Issue #${ISSUE} closed and set to ${STATUS_ACCEPTANCE_TESTING}, assigned to @${HUMAN_OWNER}."
