#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  reconcile_dead_branches.sh [--delete] [base_branch]

On-demand REPORT of claude/*, feat/*, fix/*, and chore/* branches (#3392,
report-first since #3862). Nothing schedules this and nothing should: branch
cleanup is event-driven (ledger D-013), owned by the agent that created the
branch, at the moment work changes state. This script exists for the human
question "what is still out there?", not as a sweep.

A candidate branch is reported deletable only if one of these is true:
  - its CONTENT is provably on base_branch — an ancestor, or every path it
    touches already identical there (scripts/agents/lib/branch_content.py); or
  - it is the head of a PR that was closed WITHOUT merging (an explicit
    abandonment signal).

A branch is NEVER deletable because it is old, because it has no PR, because
its linked issue is closed, or because it looks unmerged to `git branch
--merged`. All four were disproven against a real branch set on 2026-08-18:
the fully-landed branch looked the *most* unmerged of the three, and acting on
any of those signals would have destroyed 438 lines of test coverage held
nowhere else. The head of any OPEN pull request, base_branch itself, and
master/main are always left alone.

Defaults to base_branch = the configured release branch.
Reports only unless --delete is passed; anything not provably landed is
reported either way, never deleted.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd git; require_cmd jq
  echo "OK"; exit 0
fi

# Report-only by default. The previous default was "delete unless --dry-run",
# which is the wrong way round for an operation whose failure mode is the
# permanent loss of the only copy of some work (#3862).
DRY_RUN=1
case "${1:-}" in
  --delete) DRY_RUN=0; shift ;;
  --dry-run) shift ;;   # accepted for compatibility; it is now the default
esac

load_config
require_gh_auth
require_cmd git
require_cmd jq

BASE_BRANCH="${1:-$(get_release_branch)}"

# extract_issue_number, closed_unmerged_pr_exists and classify_mergeable live
# in lib/gh_project.sh — shared with developer_create_branch.sh's
# cleanup_superseded_branches so both paths use the exact same content
# verification (#3392, #3862).

# Intentional non-issue branches that must never be swept (#3392).
ALWAYS_PROTECTED=("$BASE_BRANCH" "master" "main")

is_always_protected() {
  local b="$1" p
  for p in "${ALWAYS_PROTECTED[@]}"; do
    [[ "$b" == "$p" ]] && return 0
  done
  return 1
}

echo "[reconcile] base_branch=${BASE_BRANCH} report_only=${DRY_RUN}" >&2
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
      superseded) reason="every path it touches is already on ${BASE_BRANCH}" ;;
      *) reason="" ;;
    esac
  fi

  if [[ -z "$reason" ]]; then
    # The branch-guard lines above already named the files that would have
    # been destroyed; this is the headline for them.
    echo "[reconcile] KEEP  ${branch} — content NOT proven to be on ${BASE_BRANCH}" >&2
    kept_count=$((kept_count + 1))
    continue
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[reconcile] [report] deletable: ${branch} — ${reason}" >&2
  else
    echo "[reconcile] DELETE ${branch} — ${reason}" >&2
    delete_remote_branch "$branch"
  fi
  deleted_count=$((deleted_count + 1))
done

echo "[reconcile] Done. candidates=${#CANDIDATES[@]} deletable=${deleted_count} kept=${kept_count} report_only=${DRY_RUN}" >&2
