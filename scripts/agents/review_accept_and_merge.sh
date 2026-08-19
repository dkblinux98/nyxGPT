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

# Get PR details. REST's mergeable/mergeable_state/state/merged shapes
# differ from the old GraphQL query -- re-derive the same three-value
# mergeable enum (MERGEABLE/CONFLICTING/UNKNOWN) and OPEN/CLOSED/MERGED
# state this script's downstream checks were written against.
pr_data="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR}" --jq '{
  headRefName: .head.ref,
  headSha: .head.sha,
  baseRefName: .base.ref,
  baseSha: .base.sha,
  mergeable: (if .mergeable == null then "UNKNOWN" elif .mergeable_state == "dirty" then "CONFLICTING" else "MERGEABLE" end),
  mergeStateStatus: (.mergeable_state | ascii_upcase),
  state: (if .merged then "MERGED" elif .state == "closed" then "CLOSED" else "OPEN" end)
}')"
pr_head_branch="$(echo "$pr_data" | jq -r '.headRefName')"
pr_head_sha="$(echo "$pr_data" | jq -r '.headSha')"
pr_base_sha="$(echo "$pr_data" | jq -r '.baseSha')"
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

  # Routing lives in one place for every conflict entry point (#3801):
  # scripts/agents/dispatch_conflict_resolution.sh hands the conflict to the
  # developer agent (merge `origin/<base>` into the PR branch, never rebase)
  # and reaches the owner ONLY on an agent-raised owner-only decision or
  # after the automated rounds stop converging. This script used to run its
  # own one-round-then-assign-the-owner logic; that second copy is gone so
  # the two paths cannot drift apart.
  conflict_out="$(DRY_RUN="$DRY_RUN" bash "$DIR/dispatch_conflict_resolution.sh" "$PR" "$ISSUE" || true)"
  echo "$conflict_out" >&2
  conflict_action="$(sed -n 's/^conflict-resolution: \([a-z]*\) .*/\1/p' <<<"$conflict_out" | tail -1)"

  case "$conflict_action" in
    dispatch)
      echo "[review] Conflict-resolution round dispatched to @${DEV_AGENT}; stopping the merge here." >&2
      exit 0
      ;;
    escalate)
      _die "ERROR: PR #${PR} conflicts need @${HUMAN_OWNER} (see the escalation comment on the PR). Workflow stopped."
      ;;
    *)
      # noop / unparseable: the conflict was not routed (no linked issue, a
      # round already in flight, or the dispatcher failed). Do not merge, and
      # do not silently drop it.
      _die "ERROR: PR #${PR} has merge conflicts and no resolution round was dispatched (dispatcher said: ${conflict_action:-<no result>}). Workflow stopped."
      ;;
  esac
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
  echo "[dry-run] would: set PR #$PR project Status -> '$STATUS_CLOSED'" >&2
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

# ---- Ledger ID reallocation, at the only instant it can be right ----
# #3862: agents/LEDGER.md's schema says to allocate "the next unused number",
# which is true only at the moment of merge. On a branch open for days it is a
# guess, and two branches open at once are each *correctly* handed the same
# number, because when each one asks, the other's entry exists nowhere. The
# collision is created at merge, by neither branch alone -- it happened to
# V-016, V-024 and V-025, and to D-021..D-023 before them.
#
# test_ledger_entry_ids_are_unique catches it and stays as the backstop; what
# it cannot do is resolve it, and the by-hand resolution got the theirs/mine
# sides backwards on the first attempt (#3836). So the branch's colliding IDs
# are reallocated here, deterministically, immediately before the merge. Only
# IDs BOTH sides invented since the merge base move, and only on the branch:
# the base is never rewritten, so nothing already merged shifts underneath a
# reference to it.
#
# Best-effort by design: a failure here leaves the pre-existing behavior (the
# unique-ID test blocks the merge), which is worse but not wrong.
reallocate_ledger_ids() {
  local head="$1" base="$2" ledger="agents/LEDGER.md" wt=""
  command -v python3 >/dev/null 2>&1 || return 0
  [[ -f "${DIR}/lib/ledger_ids.py" ]] || return 0
  git fetch origin "$head" "$base" >/dev/null 2>&1 || return 0
  # Nothing to do unless the PR actually touches the ledger.
  git diff --quiet "origin/${base}...origin/${head}" -- "$ledger" 2>/dev/null && return 0

  # A detached worktree so this script's own checkout is never disturbed --
  # it goes on to read scripts and merge from it.
  wt="$(mktemp -d)"
  if ! git worktree add --detach "$wt" "origin/${head}" >/dev/null 2>&1; then
    rm -rf "$wt"
    return 0
  fi

  local rc=0
  ( cd "$wt" && python3 "${DIR}/lib/ledger_ids.py" --ledger "$ledger" \
      reallocate --base "origin/${base}" --branch HEAD --write ) || rc=$?

  if [[ "$rc" == "1" ]] && ! git -C "$wt" diff --quiet -- "$ledger"; then
    echo "[review] Reallocated colliding ledger IDs on ${head}; pushing before the merge." >&2
    git -C "$wt" -c user.name="${REVIEW_AGENT}" -c user.email="${REVIEW_AGENT}@users.noreply.github.com" \
      commit -q -m "chore: reallocate colliding ledger IDs at merge time (#${ISSUE})" -- "$ledger" \
      && git -C "$wt" push origin "HEAD:refs/heads/${head}" >/dev/null 2>&1 \
      || _warn "Could not push the ledger reallocation to ${head}; the unique-ID test will block the merge instead."
  fi

  git worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
}

if [[ "$pr_state" != "MERGED" ]]; then
  reallocate_ledger_ids "$pr_head_branch" "$pr_base_branch" \
    || _warn "Ledger ID reallocation failed unexpectedly; continuing to the merge."
  # A reallocation pushes a commit, so the head SHA the closure gate below
  # verifies must be re-read. Checking the pre-reallocation SHA would compare
  # the wrong tree and refuse to close a perfectly good merge.
  pr_head_sha="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR}" --jq '.head.sha' 2>/dev/null || echo "$pr_head_sha")"
fi

# Merge PR via GitHub (this is the critical operation)
echo "[review] Merging PR #${PR}..." >&2
if ! gh pr merge "$PR" --repo "${REPO_OWNER}/${REPO_NAME}" --merge --delete-branch 2>&1; then
  echo "[review] ERROR: Merge failed. This is a critical failure." >&2
  echo "[review] System state: PR #${PR} may be partially merged or unchanged." >&2
  echo "[review] Manual intervention required: Check PR #${PR} on GitHub and complete merge manually if needed." >&2
  exit 1
fi
echo "[review] ✓ PR #${PR} merged successfully" >&2

# PR lane invariant (owner decision 2026-08-12, reaffirmed 2026-08-13,
# #3742): the merged PR's OWN project item moves to STATUS_CLOSED as part of
# this same operation. Previously only the issue got post-merge hygiene, so
# merged PR cards stranded in "In Review" and the owner swept them by hand
# (13 + 3 on 2026-08-10, 10 on 2026-08-13). Done agent-side on purpose: the
# board's built-in "Pull request merged" automation is GitHub-proprietary
# and was observed enabled while the debris accumulated. Best-effort with
# retries -- the merge already landed, so a board hiccup must not fail the
# run; reconcile_pr_lane.sh sweeps anything that slips through.
echo "[review] Setting PR #${PR} project Status to '${STATUS_CLOSED}'..." >&2
if close_pr_project_item "$PR"; then
  echo "[review] ✓ PR #${PR} project item -> ${STATUS_CLOSED}" >&2
else
  _warn "Failed to set PR #${PR} project Status to '${STATUS_CLOSED}'. PR is merged but its card may still sit in an active lane; the periodic PR-lane sweep will reconcile it."
fi

# ---- Closure gate: the work must be ON the branch, not merely reported ----
# #3862: #3789 and #3815 were both closed as `completed` while their fixes sat
# on branches that never reached the release branch -- 438 lines of test
# coverage, gone from the product and marked done. Whatever closed them was
# trusting that a run reported success, not that the work landed.
#
# "gh pr merge exited 0" is a report. The evidence is the content: every path
# the PR head touched has to be readable on the base branch now. A squash or
# rebase merge changes every SHA, so this is deliberately the same blob-level
# check the branch guard uses, not an ancestry test.
echo "[review] Verifying PR #${PR}'s content is actually on ${pr_base_branch}..." >&2
CONTENT_CHECK="${DIR}/lib/branch_content.py"

verify_merged_content_landed() {
  command -v python3 >/dev/null 2>&1 || {
    _warn "python3 unavailable; the merge cannot be verified."
    return 1
  }
  [[ -f "$CONTENT_CHECK" ]] || {
    _warn "branch_content.py not found at ${CONTENT_CHECK}; the merge cannot be verified."
    return 1
  }
  git fetch origin "$pr_base_branch" >/dev/null 2>&1 || {
    _warn "Could not fetch ${pr_base_branch}; the merge cannot be verified."
    return 1
  }
  # The head branch is gone by now (--delete-branch), but the SHA stays
  # reachable from the merge commit. Ask for it by name anyway: a shallow or
  # single-branch clone can hold the ref without the object, and "the object
  # is missing" must never read as "the content is missing".
  git rev-parse --verify --quiet "${pr_head_sha}^{commit}" >/dev/null 2>&1 \
    || git fetch origin "$pr_head_sha" >/dev/null 2>&1 \
    || _warn "Could not fetch PR head ${pr_head_sha}; the content check will report what it can see."

  # --since pins the divergence point, which is what turns this from "are the
  # PR's commits reachable" into "is the PR's work on the branch now". A merge
  # commit makes the head an ancestor even if a later commit deleted every file
  # it added -- that is the #3789 shape, and without --since this gate would
  # certify it (proved by scripts/closure-gate-smoke.sh's fault injection).
  local divergence
  divergence="$(git merge-base "$pr_base_sha" "$pr_head_sha" 2>/dev/null || true)"
  [[ -n "$divergence" ]] || {
    _warn "Could not compute the divergence point of PR #${PR}; the merge cannot be verified."
    return 1
  }
  python3 "$CONTENT_CHECK" landed \
    --base "origin/${pr_base_branch}" --branch "$pr_head_sha" --since "$divergence"
}

work_landed=0
if verify_merged_content_landed >/dev/null; then
  work_landed=1
  echo "[review] ✓ every path PR #${PR} touched is present on ${pr_base_branch}" >&2
fi

if [[ "$work_landed" != "1" ]]; then
  echo "::error::PR #${PR} reported a successful merge but its content is NOT verifiably on ${pr_base_branch}. Issue #${ISSUE} is deliberately left OPEN — closing it here is exactly how #3789 and #3815 were marked completed with their work stranded (#3862). Re-run the merge, or land the content, then close the issue." >&2
  issue_comment "$ISSUE" "⚠️ **Not closed.** PR #${PR} reported a merge into \`${pr_base_branch}\`, but the content check could not confirm that the work is on that branch, so this issue was left open on purpose (#3862). See the review run's log for the paths that are missing." \
    2>&1 || _warn "Failed to post the unverified-merge comment on #${ISSUE}."
  exit 1
fi

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
# send the next-issue dispatch event ourselves instead of waiting for a human
# to do it. The sprint boundary is the stop condition (owner policy 2026-08-10,
# #3706) -- once the ACTIVE sprint has no open Backlog issues left, post a
# loud park note instead of a kick, even if the release still has work
# queued in later sprints; a human kick is still required to start work
# outside the sprint (and deliberately may, the owner override). Off by
# default (SPRINT_AUTOPILOT unset/false): behavior is then exactly the
# pre-#3480 manual-kick flow. Best-effort: never fails the merge itself.
# The gated kick itself lives in sprint_autopilot_kick (lib/gh_project.sh),
# shared with the 3-cycle review escalation path so an escalation continues
# the queue the same way a merge does.
sprint_autopilot_kick "$ISSUE" merged || _warn "Autopilot: kick helper failed unexpectedly."

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
