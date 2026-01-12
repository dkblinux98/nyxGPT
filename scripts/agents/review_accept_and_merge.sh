#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  review_accept_and_merge.sh [--dry-run] <pr_number_or_url> <issue_number>

Merges the PR into the current release branch (merge commit) and deletes the PR branch, then:
  - Issue Status -> In Review
  - Issue assignee -> HUMAN_OWNER
  - Comment on issue with merge info
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
  echo "[review] ERROR: PR #${PR} has merge conflicts and cannot be merged automatically." >&2
  echo "[review] Creating sub-issue for merge conflict resolution..." >&2

  # Create sub-issue for merge conflicts
  conflict_body_file="/tmp/merge-conflict-${ISSUE}.md"
  cat > "$conflict_body_file" <<CONFLICT_EOF
## Merge Conflict

PR #${PR} cannot be merged automatically due to conflicts with base branch \`${pr_base_branch}\`.

### How to Fix

1. Checkout branch: \`git checkout ${pr_head_branch}\`
2. Pull latest base: \`git pull origin ${pr_base_branch}\`
3. Resolve conflicts in the affected files
4. Commit resolution: \`git add . && git commit\`
5. Push: \`git push origin ${pr_head_branch}\`
6. Re-request review

### Parent Issue
Issue #${ISSUE}
CONFLICT_EOF

  create_sub_issue "$ISSUE" "Resolve Merge Conflicts" "$conflict_body_file"
  rm -f "$conflict_body_file"

  # Set parent issue back to In Progress and assign to developer
  set_issue_status "$ISSUE" "$STATUS_IN_PROGRESS"
  issue_assign_only "$ISSUE" "$DEV_AGENT"
  issue_comment "$ISSUE" "Merge conflicts detected in PR #${PR}. Sub-issue created for resolution. Assigned back to @${DEV_AGENT}."

  _die "ERROR: Cannot merge PR #${PR} due to conflicts. Sub-issue created. Workflow stopped."
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
  echo "[dry-run] would: set_issue_status #$ISSUE -> '$STATUS_IN_REVIEW'" >&2
  echo "[dry-run] would: assign issue #$ISSUE -> @$HUMAN_OWNER" >&2
  exit 0
fi

# ---- CRITICAL PATH: Merge and update issue ----
echo "[review] ===== Beginning critical path =====" >&2

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
if ! gh issue close "$ISSUE" --repo "${REPO_OWNER}/${REPO_NAME}" --comment "Merged via review-agent. Issue closed and moved to In Review status for stakeholder acceptance." 2>&1; then
  _warn "Failed to close issue #${ISSUE}. PR is merged but issue may still be open. Continuing..."
fi

# Set issue status to In Review
echo "[review] Setting issue #${ISSUE} status to '${STATUS_IN_REVIEW}'..." >&2
if ! set_issue_status "$ISSUE" "$STATUS_IN_REVIEW" 2>&1; then
  _warn "Failed to set issue status. PR is merged but project status may be incorrect. Continuing..."
fi

# Assign issue to human owner
echo "[review] Assigning issue #${ISSUE} to @${HUMAN_OWNER}..." >&2
if ! issue_assign_only "$ISSUE" "$HUMAN_OWNER" 2>&1; then
  _warn "Failed to assign issue to ${HUMAN_OWNER}. PR is merged but assignee may be incorrect. Continuing..."
fi

# Post final comment
echo "[review] Posting completion comment..." >&2
if ! issue_comment "$ISSUE" "PR #${PR} merged into \`${pr_base_branch}\` and branch deleted. Status -> ${STATUS_IN_REVIEW}. Assigned -> @${HUMAN_OWNER}." 2>&1; then
  _warn "Failed to post comment. PR is merged and issue updated, but comment missing."
fi

echo "[review] ✓ Critical path complete" >&2

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
echo "SUCCESS: Merged PR #${PR}. Issue #${ISSUE} closed and set to ${STATUS_IN_REVIEW}, assigned to @${HUMAN_OWNER}."
