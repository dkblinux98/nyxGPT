#!/usr/bin/env bash
#
# pr_close_project_status.sh — stamp a merged/closed PR's project item to
# the terminal PR lane (STATUS_CLOSED). The event-driven half of the #3742
# lane invariant.
#
# review_accept_and_merge.sh already stamps the PRs it merges. This covers
# every other way a PR leaves the review lane:
#   * closed without merging (rejected, superseded, abandoned),
#   * merged by a path other than the review agent (owner merge, ceremony),
# and re-stamping an already-Closed card is a no-op, so running both is safe.
#
# Deliberately agent-side rather than a board automation: the built-in
# "Pull request merged" rule is GitHub-proprietary, is not retroactive, and
# was enabled while merged PRs kept stranding in "In Review".
#
# Usage:
#   pr_close_project_status.sh [--dry-run] <pr_number>
#
# Env:
#   DRY_RUN  "true" = plan only (same as --dry-run). Default "false".
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  pr_close_project_status.sh [--dry-run] <pr_number>

Sets the project Status of a merged/closed PR's own project item to the
terminal PR lane (STATUS_CLOSED, default "Closed"). No-op for a PR that is
still open.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

DRY_RUN="${DRY_RUN:-false}"
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=true; shift; fi

PR="${1:-}"
[[ -n "$PR" ]] || { usage >&2; exit 2; }
[[ "$PR" =~ ^[0-9]+$ ]] || _die "Not a PR number: '${PR}'"

load_config
require_gh_auth
require_cmd jq

# Guard against a reopened PR: only a PR that is actually merged or closed
# belongs in the terminal lane. A reopened PR is back in review and keeps
# whatever lane the review flow gave it.
pr_state="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR}" \
  --jq 'if .merged then "MERGED" elif .state == "closed" then "CLOSED" else "OPEN" end')"
echo "[pr-lane] PR #${PR} state: ${pr_state}" >&2

if [[ "$pr_state" == "OPEN" ]]; then
  echo "[pr-lane] PR #${PR} is open — leaving its project Status untouched." >&2
  exit 0
fi

current="$(pr_status "$PR" 2>/dev/null || true)"
echo "[pr-lane] PR #${PR} current Status: '${current:-<unset>}' -> '${STATUS_CLOSED}'" >&2

if [[ "$current" == "$STATUS_CLOSED" ]]; then
  echo "[pr-lane] Already '${STATUS_CLOSED}' — nothing to do." >&2
  exit 0
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[pr-lane] DRY RUN — would set PR #${PR} Status -> '${STATUS_CLOSED}'." >&2
  exit 0
fi

if close_pr_project_item "$PR"; then
  echo "[pr-lane] ✓ PR #${PR} project item -> ${STATUS_CLOSED}" >&2
else
  _die "Failed to set PR #${PR} project Status to '${STATUS_CLOSED}'."
fi
