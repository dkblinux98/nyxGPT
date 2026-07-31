#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_sprint_reorg_apply.sh [--dry-run]

Applies the most recent unapplied sprint reorganization proposal posted by
scrummaster_sprint_report.sh on the release tracking issue
(RELEASE_ISSUE_NUMBER). This script does not itself check for the
stakeholder's APPROVE_SPRINT_REORG trigger comment -- the calling workflow
gates on that; this script is the mechanical "apply what was proposed" step
(propose-don't-execute lives in scrummaster_sprint_report.sh; approval is
the stakeholder-agent's role, #3480).

For each proposed candidate issue, clears its Sprint field (removing it
from the active Sprint) and posts a summary of exactly what moved. If no
unapplied proposal is found, exits 0 and makes no changes. Re-running after
a successful apply is a no-op (idempotency marker on the proposal comment).

Options:
  --dry-run   Print what would change without applying it
  -h, --help  Show this help
EOF
}

DRY_RUN=0
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

log() { echo "[sprint-reorg-apply] $*" >&2; }

load_config
require_gh_auth
require_cmd jq

SPRINT_FIELD="${SPRINT_FIELD:-Sprint}"
RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}"
[[ -n "$RELEASE_ISSUE" ]] || _die "RELEASE_ISSUE_NUMBER is not configured."

# Idempotency guard: mirrors the CONFLICT_ROUND_MARKER pattern in
# review_accept_and_merge.sh -- once a proposal comment's id appears in an
# "applied" summary, re-running this script (retry, duplicate trigger) must
# not re-apply it.
APPLIED_MARKER="SPRINT_REORG_APPLIED_FOR_COMMENT:"

comments_json="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${RELEASE_ISSUE}/comments" --paginate)"

proposal_comment="$(echo "$comments_json" | jq -c '
  [.[] | select(.body | contains("SPRINT_REORG_PROPOSAL"))] | sort_by(.created_at) | last // empty')"

if [[ -z "$proposal_comment" || "$proposal_comment" == "null" ]]; then
  log "No sprint reorganization proposal found on issue #${RELEASE_ISSUE} -- nothing to apply."
  exit 0
fi

comment_id="$(echo "$proposal_comment" | jq -r '.id')"
already_applied="$(echo "$comments_json" | jq --arg m "${APPLIED_MARKER}${comment_id}" \
  '[.[] | select(.body | contains($m))] | length')"
if [[ "${already_applied:-0}" -gt 0 ]]; then
  log "Proposal in comment ${comment_id} was already applied -- nothing to do."
  exit 0
fi

proposal_json="$(echo "$proposal_comment" | jq -r '.body' \
  | grep -o 'SPRINT_REORG_PROPOSAL: {.*}' | sed -e 's/^SPRINT_REORG_PROPOSAL: //' -e 's/ -->$//')"
[[ -n "$proposal_json" ]] || _die "Found a proposal comment (id ${comment_id}) but could not parse its SPRINT_REORG_PROPOSAL JSON."

sprint_title="$(echo "$proposal_json" | jq -r '.sprint')"
action="$(echo "$proposal_json" | jq -r '.action')"
candidates="$(echo "$proposal_json" | jq -c '.candidates')"
log "Applying proposal from comment ${comment_id}: action=${action} sprint='${sprint_title}' candidates=${candidates}"

if [[ "$action" != "move_out" ]]; then
  _die "Unsupported reorg proposal action: '${action}' (only move_out is implemented)."
fi

moved=()
failed=()
while IFS= read -r issue_num; do
  [[ -n "$issue_num" ]] || continue
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would clear Sprint field '${SPRINT_FIELD}' on issue #${issue_num}"
    moved+=("$issue_num")
    continue
  fi

  item_id="$(ensure_issue_in_project "$issue_num" 2>/dev/null || echo "")"
  if [[ -n "$item_id" && "$item_id" != "null" ]] && clear_project_field_value "$item_id" "$SPRINT_FIELD"; then
    moved+=("$issue_num")
    log "Cleared Sprint on issue #${issue_num}"
  else
    failed+=("$issue_num")
    _warn "Failed to clear Sprint on issue #${issue_num}"
  fi
done < <(echo "$candidates" | jq -r '.[]')

moved_list="none"
[[ "${#moved[@]}" -gt 0 ]] && moved_list="$(
  IFS=,
  echo "${moved[*]}"
)"

summary="✅ **Sprint Reorg Applied** (approved via \`APPROVE_SPRINT_REORG\`)

Proposal from https://github.com/${REPO_OWNER}/${REPO_NAME}/issues/${RELEASE_ISSUE}#issuecomment-${comment_id}:
- Moved out of \"${sprint_title}\": ${moved_list}"

if [[ "${#failed[@]}" -gt 0 ]]; then
  failed_list="$(
    IFS=,
    echo "${failed[*]}"
  )"
  summary="${summary}
- Failed to update (manual fix needed): ${failed_list}"
fi

summary="${summary}

${APPLIED_MARKER}${comment_id}"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "$summary"
else
  issue_comment "$RELEASE_ISSUE" "$summary"
  log "Posted apply summary to issue #${RELEASE_ISSUE}"
fi

if [[ "${#failed[@]}" -gt 0 ]]; then
  exit 1
fi
