#!/usr/bin/env bash
set -euo pipefail

# scripts/agents/reconcile_work_branch.sh
#
# Put this run's work back onto ONE branch after a `claude-code-action`
# invocation moved the workspace off it (#4038).
#
# WHY THIS EXISTS.
#
# `anthropics/claude-code-action` mints and checks out a fresh
# `claude/issue-<n>-<timestamp>` branch on every invocation, cut from the
# release branch. `developer_auto_implement.yml` invokes it six times. So the
# workspace a retry step is handed is NOT the branch carrying the work the
# retry is supposed to continue -- it is a clean cut of the release branch, and
# the failure logs the retry is asked to read describe code that is not in
# front of it. Re-implementing the issue from scratch is the reasonable
# response to what the agent is shown, and it is what happened: on #4033
# attempt 1's work sat on `feat/4033-...` while attempt 2 spent sixteen minutes
# rebuilding it on `claude/issue-4033-20260824-0213`, and the run ended with two
# divergent branches, PR #4036, and a rescue draft PR #4037 the owner closed by
# hand.
#
# The behaviour is the ACTION's, not this repo's -- PR head-branch naming shows
# it starting at 2026-07-07 with no commit here to explain it, because `@v1` is a
# floating major tag. So the workflow reconciles branches explicitly rather than
# relying on continuity, and pins the action so the next such change is a
# deliberate upgrade (see `tests/unit/test_developer_retry_continuity.py`).
#
# WHAT IT DOES. Given the target branch -- read from RUN STATE by the caller,
# never from the working tree, because the working tree is exactly what is
# wrong on the failure path (D-031) -- it makes the workspace and `origin` agree
# that the work lives on that branch:
#
#   1. Already on the target                -> nothing to do.
#   2. Source carries nothing of its own    -> reposition onto the target only.
#      (a fresh action branch with no commits beyond the release branch must
#      never overwrite the branch holding the only copy of the work; this is
#      the safety check the review path has had since #3145)
#   3. Target does not exist on `origin`    -> publish the source as the target.
#   4. Otherwise                            -> FORWARD MERGE the source into the
#      target and push. Fast-forward when the target is an ancestor; a real
#      merge commit when they diverged.
#
# NEVER a force-push, and never a rebase. D-011 is the standing rule ("merge,
# don't rebase", no history rewriting on shared branches), and the force-push
# the review path used is unsafe in exactly the shape this script exists for:
# an action branch cut from the release branch does NOT contain the target's
# commits, so forcing it over the target destroys them. A merge is the only
# operation that keeps both sides in every case, and it subsumes the
# fast-forward the force-push was reaching for.
#
# The stray source branch is deleted only when its content is PROVABLY on the
# target -- `scripts/agents/lib/branch_content.py`, blob level, the D-031
# criterion. A push that failed, a race, or a conflict all leave it in place to
# be rescued by `developer_ensure_pr_exists.sh`.
#
# Never fails its caller. A reconciliation that reds the job it is fixing has
# made things worse than the divergence it was closing; every refusal is a
# `::warning::` and exit 0, and every refusal leaves both branches intact.
#
# Usage:
#   reconcile_work_branch.sh --target <branch> --release <branch> [--label <text>]
#
# Executed evidence: tests/test_reconcile_work_branch.sh (real git repos, real
# bare `origin`, no network), run by `.github/workflows/branch-guard-smoke.yml`.

# Helper paths resolve against THIS script, not the caller's CWD: git commands
# below act on the workspace the caller is standing in, which is not always the
# repository this script ships in (tests/test_reconcile_work_branch.sh drives it
# against a planted fixture, and that is what makes the evidence executable).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET=""
RELEASE=""
LABEL="work"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)  TARGET="${2:-}"; shift 2 ;;
    --release) RELEASE="${2:-}"; shift 2 ;;
    --label)   LABEL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '3,66p' "$0"; exit 0 ;;
    *) echo "[reconcile] Unknown argument: $1" >&2; exit 2 ;;
  esac
done

_warn() { echo "::warning::[reconcile] $*" >&2; }
_note() { echo "[reconcile] $*" >&2; }

if [[ -z "$TARGET" ]]; then
  # Not an error the caller can act on mid-run, and not a reason to red the
  # job: the caller gates on this too (the retry steps do not run at all when
  # the work branch is unknown). Say so and stand down.
  _warn "No --target given; the work branch is unknown, so nothing is reconciled."
  exit 0
fi
if [[ -z "$RELEASE" ]]; then
  _warn "No --release given; refusing to reconcile without knowing the base branch."
  exit 0
fi

git fetch origin --prune >/dev/null 2>&1 || _warn "Could not fetch origin; working from the refs already here."

SOURCE="$(git branch --show-current 2>/dev/null || true)"
# The SOURCE is read from the working tree on purpose, and it is the one read
# that is correct here: it names the branch the invocation that just finished
# was standing on, which is the only place its commits can be. The TARGET is
# the read that must not come from the tree, and it does not -- it is an
# argument.
if [[ -z "$SOURCE" ]]; then
  _warn "Detached HEAD after the ${LABEL} step; cannot tell which branch its commits are on. Leaving ${TARGET} alone."
  exit 0
fi

if [[ "$SOURCE" == "$TARGET" ]]; then
  _note "The ${LABEL} step worked on ${TARGET}; nothing to reconcile."
  exit 0
fi

_note "The ${LABEL} step ended on '${SOURCE}', not the work branch '${TARGET}'."

# Uncommitted work first. Switching branches with a dirty tree either carries
# the changes across (silently attributing them to the wrong branch) or fails;
# both lose the retry's work, and this script is the only thing that moves the
# workspace after a Claude step. Commit it to SOURCE through the same
# allowlisted stager the workflow's snapshot step uses -- a blanket `git add -A`
# committed a 7,032-file runner venv once already (#3352/#3370).
if [[ -n "$(git status --porcelain)" ]]; then
  if "$DIR/snapshot_safe_add.sh" >&2; then
    if [[ -n "$(git diff --cached --name-only)" ]]; then
      git commit -q -m "wip: snapshot uncommitted ${LABEL} work before reconciling onto ${TARGET}" \
        -m "Auto-committed by reconcile_work_branch.sh (#4038): the ${LABEL} step left uncommitted changes on ${SOURCE}, and reconciling moves the workspace to ${TARGET}." \
        || { _warn "Could not commit the uncommitted ${LABEL} work; leaving ${TARGET} alone rather than switching away from it."; exit 0; }
      _warn "Snapshot-committed uncommitted ${LABEL} work on ${SOURCE} before reconciling."
    fi
  else
    _warn "The snapshot size guard tripped on ${SOURCE}; refusing to switch branches and lose the uncommitted delta. ${TARGET} is unchanged."
    exit 0
  fi
fi

_checkout_target() {
  if git rev-parse --verify --quiet "origin/${TARGET}" >/dev/null; then
    git checkout -q -B "$TARGET" "origin/${TARGET}"
  else
    git checkout -q -B "$TARGET"
  fi
}

# Safety, unchanged from the review path's version (#3145): a fresh action
# branch that never committed anything carries nothing, and must never be
# allowed to become the target's new content. Reposition and stop.
#
# Repositioning even here is deliberate. The branch the workspace is standing
# on is a cut of the release branch, so leaving it there hands the next
# verification a tree that is not the PR's and lets it report a verdict about
# the release branch as if it were the work's -- #3979's defect, arriving
# through a different door.
if [[ -z "$(git rev-list "origin/${RELEASE}..HEAD" 2>/dev/null)" ]]; then
  _note "'${SOURCE}' has no commits beyond ${RELEASE}; nothing to carry forward."
  if ! _checkout_target; then
    _warn "Could not check out ${TARGET}; the workspace is still on ${SOURCE}."
    exit 0
  fi
  _note "Workspace repositioned onto ${TARGET} at $(git rev-parse --short HEAD)."
  exit 0
fi

if ! git rev-parse --verify --quiet "origin/${TARGET}" >/dev/null; then
  # The work branch never reached origin -- the run pushed nothing before
  # failing. The source IS the work; publish it under the target's name.
  _note "${TARGET} is not on origin; publishing '${SOURCE}' as ${TARGET}."
  if git push origin "HEAD:refs/heads/${TARGET}"; then
    git fetch origin "$TARGET" >/dev/null 2>&1 || true
    _checkout_target || _warn "Published ${TARGET} but could not check it out."
  else
    _warn "Could not publish ${TARGET} from '${SOURCE}'; the work is still on '${SOURCE}'."
  fi
  exit 0
fi

SOURCE_SHA="$(git rev-parse HEAD)"

if ! _checkout_target; then
  _warn "Could not check out ${TARGET}; leaving the work on '${SOURCE}'."
  exit 0
fi

# Forward merge. Fast-forwards when origin/TARGET is an ancestor of the source
# (the "the stray supersedes a stale tip" case the review path used to force
# past); a real merge commit when they diverged, which is the #4033 case and
# the one a force-push destroys.
if ! git merge --no-edit "$SOURCE_SHA" >&2; then
  git merge --abort >/dev/null 2>&1 || true
  # Do not choose between the two sides here -- that is a developer decision
  # (D-011), and this script's contract is that it never destroys either. Get
  # the stray onto origin so the rescue backstop can see it, and say plainly
  # that the branches are still apart.
  git push origin "${SOURCE_SHA}:refs/heads/${SOURCE}" >/dev/null 2>&1 \
    || _warn "Could not push '${SOURCE}' to origin either; its commits exist only on this runner."
  _warn "'${SOURCE}' and ${TARGET} conflict; both branches are intact and neither was overwritten. ${TARGET} does NOT yet carry the ${LABEL} step's commits."
  exit 0
fi

if ! git push origin "HEAD:refs/heads/${TARGET}"; then
  _warn "Could not push the reconciled ${TARGET} to origin; '${SOURCE}' still holds the ${LABEL} step's commits and is not being deleted."
  exit 0
fi
_note "Reconciled '${SOURCE}' onto ${TARGET} at $(git rev-parse --short HEAD) (merge, not force)."

# Delete the stray only on the blob-level proof (D-031). Re-read the remote
# first: "the push exited 0" is a report, not evidence.
git fetch origin "$TARGET" >/dev/null 2>&1 || true
if python3 "$DIR/lib/branch_content.py" landed \
     --base "origin/${TARGET}" --branch "$SOURCE_SHA" >&2; then
  if git ls-remote --exit-code --heads origin "$SOURCE" >/dev/null 2>&1; then
    git push origin --delete "$SOURCE" >/dev/null 2>&1 \
      || _warn "Could not delete the stray branch '${SOURCE}' from origin (non-fatal)."
  fi
  git branch -D "$SOURCE" >/dev/null 2>&1 || true
else
  _warn "Keeping '${SOURCE}' -- its content is not provably on ${TARGET}."
fi
exit 0
