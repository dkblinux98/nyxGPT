#!/usr/bin/env bash
set -euo pipefail

# scripts/retrospective/merge_data_branch.sh
#
# Lands the retrospective data branch (claude/retro-data) on the default
# branch through a pull request (#3815).
#
# Why a pull request: a repository ruleset requires changes to the default
# branch to arrive that way, so the old `git push origin HEAD:$DEFAULT` in
# retro_data_merge.yml was rejected with GH013 ("Changes must be made through
# a pull request"). Opening a PR and merging it satisfies the rule by
# construction and needs no ruleset bypass. The merge without human review
# remains the owner-approved exception for this tooling (2026-07-31); the
# guard below is what bounds it — anything outside scripts/retrospective/ is
# refused, so the exception cannot be used to slip code past review.
#
# Usage:
#   merge_data_branch.sh
#
# Env:
#   REPO             owner/name                     (required)
#   BASE_REF         branch to merge into           (required)
#   DATA_BRANCH      branch to merge                (default: claude/retro-data)
#   REMOTE           git remote                     (default: origin)
#   GH_TOKEN         token for gh                   (required by gh)
#   MERGE_TIMEOUT    seconds to wait for mergeable  (default: 1800 — a data
#                    PR waits on the same required checks as any other, and
#                    the pytest job alone runs for minutes)
#   POLL_INTERVAL    seconds between checks         (default: 15)
#
# Exits 0 printing "nothing to merge" when the data branch holds nothing the
# base branch does not already have.

_die() { echo "[merge-retro-data] ERROR: $*" >&2; exit 1; }
_log() { echo "[merge-retro-data] $*" >&2; }

usage() { sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

REPO="${REPO:-}"
BASE_REF="${BASE_REF:-}"
DATA_BRANCH="${DATA_BRANCH:-claude/retro-data}"
REMOTE="${REMOTE:-origin}"
MERGE_TIMEOUT="${MERGE_TIMEOUT:-1800}"
POLL_INTERVAL="${POLL_INTERVAL:-15}"

[[ -n "$REPO" ]] || _die "REPO (owner/name) is required"
[[ -n "$BASE_REF" ]] || _die "BASE_REF is required"
command -v gh >/dev/null 2>&1 || _die "gh is required"

if ! git ls-remote --exit-code --heads "$REMOTE" "$DATA_BRANCH" >/dev/null 2>&1; then
  _log "$DATA_BRANCH does not exist on $REMOTE"
  echo "nothing to merge"
  exit 0
fi

git fetch --quiet "$REMOTE" \
  "+refs/heads/${BASE_REF}:refs/retro/base" \
  "+refs/heads/${DATA_BRANCH}:refs/retro/data"

if git merge-base --is-ancestor refs/retro/data refs/retro/base; then
  _log "$DATA_BRANCH is already contained in $BASE_REF"
  echo "nothing to merge"
  exit 0
fi

# ---- Guard: this path skips code review, so it may only carry retro data ----
merge_base="$(git merge-base refs/retro/base refs/retro/data)"
outside="$(git diff --name-only "$merge_base" refs/retro/data \
  | grep -v '^scripts/retrospective/' || true)"
if [[ -n "$outside" ]]; then
  echo "Refusing to merge: $DATA_BRANCH changes files outside scripts/retrospective/:" >&2
  echo "$outside" >&2
  exit 1
fi

changed="$(git diff --name-only "$merge_base" refs/retro/data | tr '\n' ' ')"
_log "data branch changes: $changed"

# ---- Open (or reuse) the pull request ----
pr="$(gh pr list --repo "$REPO" --head "$DATA_BRANCH" --base "$BASE_REF" \
  --state open --json number --jq '.[0].number // empty')"

if [[ -z "$pr" ]]; then
  body="Automated retrospective data refresh (owner tooling, see \
\`scripts/retrospective/REFRESH_RUNBOOK.md\`).

Files: ${changed}

Merged without review by the owner-approved exception for this tooling
(2026-07-31). It arrives as a pull request because the default branch's
ruleset requires one (#3815); \`merge_data_branch.sh\` refuses to open this
PR at all if the branch touches anything outside \`scripts/retrospective/\`."
  url="$(gh pr create --repo "$REPO" --base "$BASE_REF" --head "$DATA_BRANCH" \
    --title "chore(retro): refresh retrospective data" --body "$body")"
  pr="${url##*/}"
  [[ "$pr" =~ ^[0-9]+$ ]] || _die "could not read the PR number from: $url"
  _log "opened PR #$pr"
else
  _log "reusing open PR #$pr"
fi

# ---- Wait until GitHub says it can be merged ----
deadline=$((SECONDS + MERGE_TIMEOUT))
while :; do
  read -r merged state <<<"$(gh api "repos/${REPO}/pulls/${pr}" \
    --jq '[(.merged|tostring), (.mergeable_state // "unknown")] | join(" ")')"

  if [[ "$merged" == "true" ]]; then
    _log "PR #$pr is already merged"
    echo "merged"
    exit 0
  fi

  case "$state" in
    clean|unstable|has_hooks)
      break
      ;;
    dirty)
      _die "PR #$pr conflicts with $BASE_REF — re-run the dump to rebuild $DATA_BRANCH on the current tip"
      ;;
    behind)
      _log "PR #$pr is behind $BASE_REF; updating the branch"
      gh api -X PUT "repos/${REPO}/pulls/${pr}/update-branch" >/dev/null 2>&1 || \
        _log "update-branch failed; will re-check"
      ;;
    *)
      _log "PR #$pr not mergeable yet (state: $state); waiting"
      ;;
  esac

  if (( SECONDS >= deadline )); then
    _die "PR #$pr never became mergeable within ${MERGE_TIMEOUT}s (last state: $state)"
  fi
  sleep "$POLL_INTERVAL"
done

# ---- Merge ----
if ! gh pr merge "$pr" --repo "$REPO" --merge --delete-branch; then
  # Losing a race with another lander is not a failure — only an unmerged PR is.
  if [[ "$(gh api "repos/${REPO}/pulls/${pr}" --jq '.merged|tostring')" == "true" ]]; then
    _log "PR #$pr was merged by another run"
    echo "merged"
    exit 0
  fi
  _die "merging PR #$pr failed — the refresh is still on $DATA_BRANCH, nothing is lost"
fi

# A green `gh pr merge` is not proof: verify the PR really is merged (#3815 was
# a green dump whose push had been rejected).
if [[ "$(gh api "repos/${REPO}/pulls/${pr}" --jq '.merged|tostring')" != "true" ]]; then
  _die "PR #$pr reports merged=false after merging"
fi

_log "merged PR #$pr into $BASE_REF"
echo "merged"
