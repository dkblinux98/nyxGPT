#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  reconcile_dead_branches.sh [--dry-run] [base_branch]

Periodic sweep for dead claude/*, feat/*, fix/*, and chore/* branches (#3392).
A candidate branch is deleted only if one of these is true:
  - it is fully merged/contained in base_branch (git merge-base --is-ancestor), or
  - it is the head of a PR that was closed WITHOUT merging (an explicit
    abandonment signal), or
  - it is superseded: its linked issue (parsed from the branch name) is
    CLOSED, AND every commit unique to the branch has an equivalent
    (same patch-id/diff) commit on base_branch — i.e. the same change
    landed via a different branch/commit SHA.

A branch is never deleted just because it is old or unmerged. The head of
any OPEN pull request, base_branch itself, master/main, and branches with no
recognizable issue number (e.g. v2.0.0-pre-nyxAgent-implementation) are
always left alone.

Defaults to base_branch = the configured release branch.
Use --dry-run to preview without deleting anything.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd git; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

load_config
require_gh_auth
require_cmd git
require_cmd jq

BASE_BRANCH="${1:-$(get_release_branch)}"

# extract_issue_number, closed_unmerged_pr_exists, classify_mergeable, and
# MAX_BASE_COMMITS_TO_SCAN live in lib/gh_project.sh — shared with
# developer_create_branch.sh's cleanup_superseded_branches so both paths use
# the exact same merge/supersede verification (#3392).

# Intentional non-issue branches that must never be swept (#3392).
ALWAYS_PROTECTED=("$BASE_BRANCH" "master" "main" "v2.0.0-pre-nyxAgent-implementation")

is_always_protected() {
  local b="$1" p
  for p in "${ALWAYS_PROTECTED[@]}"; do
    [[ "$b" == "$p" ]] && return 0
  done
  return 1
}

echo "[reconcile] base_branch=${BASE_BRANCH} dry_run=${DRY_RUN}" >&2
git fetch origin "$BASE_BRANCH" >&2

echo "[reconcile] Fetching open PR head branches (protected)..." >&2
OPEN_PR_HEADS="$(open_pr_head_branches 2>/dev/null || true)"

is_open_pr_head() {
  local b="$1"
  echo "$OPEN_PR_HEADS" | grep -qx "$b"
}

echo "[reconcile] Listing candidate branches (claude/*, feat/*, fix/*, chore/*)..." >&2
mapfile -t CANDIDATES < <(git ls-remote --heads origin 2>/dev/null \
  | awk '{print $2}' | sed 's#^refs/heads/##' \
  | grep -E '^(claude|feat|fix|chore)/' || true)

echo "[reconcile] ${#CANDIDATES[@]} candidate branch(es) found." >&2

deleted_count=0
kept_count=0

for branch in "${CANDIDATES[@]}"; do
  [[ -n "$branch" ]] || continue

  if is_always_protected "$branch"; then
    echo "[reconcile] KEEP  ${branch} — protected (base/master/main/intentional)" >&2
    kept_count=$((kept_count + 1))
    continue
  fi

  if is_open_pr_head "$branch"; then
    echo "[reconcile] KEEP  ${branch} — head of an open PR" >&2
    kept_count=$((kept_count + 1))
    continue
  fi

  reason=""
  if closed_unmerged_pr_exists "$branch" "$BASE_BRANCH"; then
    reason="closed PR without merge (explicit abandonment)"
  else
    issue="$(extract_issue_number "$branch")"
    verdict="$(classify_mergeable "$branch" "$issue" "$BASE_BRANCH")"
    case "$verdict" in
      merged) reason="fully merged/contained in ${BASE_BRANCH}" ;;
      superseded) reason="issue #${issue} closed + equivalent commits present on ${BASE_BRANCH}" ;;
      *) reason="" ;;
    esac
  fi

  if [[ -z "$reason" ]]; then
    echo "[reconcile] KEEP  ${branch} — not confirmed merged/superseded" >&2
    kept_count=$((kept_count + 1))
    continue
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[reconcile] [dry-run] would delete ${branch} — ${reason}" >&2
  else
    echo "[reconcile] DELETE ${branch} — ${reason}" >&2
    delete_remote_branch "$branch"
  fi
  deleted_count=$((deleted_count + 1))
done

echo "[reconcile] Done. candidates=${#CANDIDATES[@]} deleted=${deleted_count} kept=${kept_count} dry_run=${DRY_RUN}" >&2
