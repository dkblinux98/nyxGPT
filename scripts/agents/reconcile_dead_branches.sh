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

# Bounds how many base-branch commits since the divergence point we'll hash
# for patch-id comparison. Above this, the branch predates too much history
# to check safely/quickly, so it's left for manual review instead of guessed at.
MAX_BASE_COMMITS_TO_SCAN="${MAX_BASE_COMMITS_TO_SCAN:-1000}"

# Intentional non-issue branches that must never be swept (#3392).
ALWAYS_PROTECTED=("$BASE_BRANCH" "master" "main" "v2.0.0-pre-nyxAgent-implementation")

is_always_protected() {
  local b="$1" p
  for p in "${ALWAYS_PROTECTED[@]}"; do
    [[ "$b" == "$p" ]] && return 0
  done
  return 1
}

extract_issue_number() {
  local b="$1"
  if [[ "$b" =~ ^claude/issue-([0-9]+)- ]]; then
    echo "${BASH_REMATCH[1]}"; return 0
  fi
  if [[ "$b" =~ ^(feat|fix|chore)/([0-9]+)- ]]; then
    echo "${BASH_REMATCH[2]}"; return 0
  fi
  echo ""
}

# True if `branch` is the head of a PR that was closed without merging into
# BASE_BRANCH — an explicit abandonment signal, safe to act on immediately.
closed_unmerged_pr_exists() {
  local branch="$1" count
  count="$(gh pr list --repo "${REPO_OWNER}/${REPO_NAME}" --head "$branch" --state closed \
      --json merged,baseRefName --limit 20 2>/dev/null \
    | jq --arg base "$BASE_BRANCH" '[.[] | select(.merged == false and .baseRefName == $base)] | length')"
  [[ "${count:-0}" -gt 0 ]]
}

# Prints one of: "merged", "superseded", or "" (keep — not confirmed safe).
classify_mergeable() {
  local branch="$1" issue="$2"

  git fetch origin "$branch" >/dev/null 2>&1 || { echo ""; return 0; }

  if git merge-base --is-ancestor "origin/${branch}" "origin/${BASE_BRANCH}" 2>/dev/null; then
    echo "merged"
    return 0
  fi

  # Unmerged from here on: only ever "superseded" (never "merged"), and only
  # if the linked issue is closed and the diff content already landed.
  [[ -n "$issue" ]] || { echo ""; return 0; }

  local issue_state
  issue_state="$(gh issue view "$issue" --repo "${REPO_OWNER}/${REPO_NAME}" --json state --jq '.state' 2>/dev/null || echo "")"
  [[ "$issue_state" == "CLOSED" ]] || { echo ""; return 0; }

  local mb
  mb="$(git merge-base "origin/${branch}" "origin/${BASE_BRANCH}" 2>/dev/null || echo "")"
  [[ -n "$mb" ]] || { echo ""; return 0; }

  local base_commit_count
  base_commit_count="$(git rev-list --count "${mb}..origin/${BASE_BRANCH}" 2>/dev/null || echo 0)"
  if (( base_commit_count > MAX_BASE_COMMITS_TO_SCAN )); then
    echo ""
    return 0
  fi

  local branch_ids base_ids id missing=0
  branch_ids="$(git rev-list "${mb}..origin/${branch}" 2>/dev/null \
    | while read -r c; do git show "$c" | git patch-id --stable 2>/dev/null | awk '{print $1}'; done | sort -u)"
  [[ -n "$branch_ids" ]] || { echo ""; return 0; }

  base_ids="$(git rev-list "${mb}..origin/${BASE_BRANCH}" 2>/dev/null \
    | while read -r c; do git show "$c" | git patch-id --stable 2>/dev/null | awk '{print $1}'; done | sort -u)"

  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    echo "$base_ids" | grep -qx "$id" || { missing=1; break; }
  done <<< "$branch_ids"

  if [[ "$missing" == "0" ]]; then
    echo "superseded"
  else
    echo ""
  fi
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
  if closed_unmerged_pr_exists "$branch"; then
    reason="closed PR without merge (explicit abandonment)"
  else
    issue="$(extract_issue_number "$branch")"
    verdict="$(classify_mergeable "$branch" "$issue")"
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
