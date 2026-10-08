#!/usr/bin/env bash
set -uo pipefail

# prune_stray_worktrees.sh -- remove every git worktree except this one, so a
# leftover worktree cannot block the next branch checkout (#4176).
#
# THE DEFECT THIS EXISTS FOR. On #4166 (run 37709148793) the developer agent
# asked exactly the right question during attempt 2 -- "do these failures
# already exist on the base?" -- and answered it with
#
#     git worktree add /tmp/base-wt v3.0.1
#
# which CHECKS OUT the branch name, and never removed it. Three steps later
# Phase 3's `claude-code-action` invocation died in its own branch setup:
#
#     fatal: 'v3.0.1' is already used by worktree at '/tmp/base-wt'
#
# Phase 3 is the step whose entire job is to diagnose an `unknown` failure, so
# the run escalated to the owner with no diagnosis at all -- the generic
# "Error type could not be determined" headline this issue is named for.
#
# A worktree left behind by an earlier step in the same job is a trap for every
# later step that checks out a branch, and `claude-code-action` checks out a
# branch on every invocation. The prompts now tell the agent to use
# `git worktree add --detach` (which holds no branch name), but prompt guidance
# is not a guard: this script runs before each invocation regardless, because
# the next trap will be set by something no prompt anticipated.
#
# BEST-EFFORT, NEVER FATAL. A worktree it cannot remove is reported as a
# warning and the run continues: failing here would block the very step this
# unblocks. The exit status is always 0.
#
# Usage: prune_stray_worktrees.sh [keep_path]
#   keep_path defaults to $GITHUB_WORKSPACE, then to the current directory.

KEEP="${1:-${GITHUB_WORKSPACE:-$PWD}}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[worktrees] not a git repository -- nothing to prune" >&2
  exit 0
fi

# `--porcelain` so the paths are parsed from a stable format rather than from
# the human listing's "<path>  <sha> [<branch>]" columns, which contain spaces
# in both the separator and (legally) the path.
KEEP_REAL="$(cd "$KEEP" 2>/dev/null && pwd -P)" || KEEP_REAL="$KEEP"
MAIN_REAL=""
STRAY=()
while IFS= read -r line; do
  [[ "$line" == worktree\ * ]] || continue
  path="${line#worktree }"
  real="$(cd "$path" 2>/dev/null && pwd -P)" || real="$path"
  # The FIRST entry `git worktree list` prints is the main worktree, which
  # cannot be removed (`git worktree remove` refuses) and must not be tried.
  if [[ -z "$MAIN_REAL" ]]; then
    MAIN_REAL="$real"
    continue
  fi
  [[ "$real" == "$KEEP_REAL" ]] && continue
  STRAY+=("$path")
done < <(git worktree list --porcelain 2>/dev/null)

for path in "${STRAY[@]+"${STRAY[@]}"}"; do
  echo "[worktrees] removing stray worktree: $path" >&2
  if ! git worktree remove --force "$path" >/dev/null 2>&1; then
    # `remove` refuses when the directory is already gone; dropping the
    # administrative record is what actually frees the branch, and `prune`
    # below does that. Deleting the directory first makes the record prunable.
    rm -rf "$path" 2>/dev/null \
      || echo "::warning::could not remove the worktree at ${path}; a branch it holds may block a later checkout" >&2
  fi
done

# Always prune, even with nothing to remove: a worktree whose directory was
# deleted by hand (or by the step above) leaves an administrative record that
# STILL holds its branch, and that record is invisible in the filesystem.
git worktree prune >/dev/null 2>&1 \
  || echo "::warning::git worktree prune failed; stale worktree records may still hold a branch" >&2

REMAINING="$(git worktree list --porcelain 2>/dev/null | grep -c '^worktree ' || true)"
echo "[worktrees] ${#STRAY[@]} stray worktree(s) removed; ${REMAINING:-?} worktree(s) remain" >&2
exit 0
