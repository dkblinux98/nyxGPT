#!/usr/bin/env bash
# Promote issues whose acceptance blockers are all accepted (owner flow,
# 2026-08-02; native relationships since #3731, owner decision 2026-08-12).
#
# An issue filed during acceptance testing -- an Acceptance Failure
# (@acceptance-failure) or an Improvement (@improvement) -- is a separate
# issue that BLOCKS the issue it was filed against, expressed through
# GitHub's native blocked-by/blocks relationship. The blocked issue parks
# closed in "Acceptance Testing" while its blockers are reworked; when EVERY
# blocker -- directly and TRANSITIVELY (a blocker of a blocker gates too) --
# reaches "For Release", this sweep promotes it to "For Release" and comments
# the promotion on it.
#
# Relationship storage is native only. The retired `Related feature: #N` body
# marker is still READ for issues filed before #3731 (documented historical
# fallback) and any such link is HEALED into a native relationship here, so
# old data converges on the new storage instead of needing a separate
# backfill. Nothing writes prose markers any more.
#
# Run via .github/workflows/promote_accepted_features.yml (cron + dispatch).
#
# ENV:
#   DRY_RUN=1   report what would be promoted/linked, change nothing
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
load_config
require_gh_auth

DRY_RUN="${DRY_RUN:-0}"
ACCEPTANCE_STATUS="${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}"

log() { echo "[promote] $*" >&2; }

# issue_status() (project Status of an issue) is shared from lib/gh_project.sh
# -- also used by the parked-blocked-issue sweep (#3631).

# Candidate blockers: every Acceptance Failure and Improvement issue. Both
# labels are swept because both commands now record a blocking relationship
# (owner decision 2026-08-12) -- an improvement filed against an issue holds
# its acceptance exactly like a failure does.
candidates="$(for label in "Acceptance%20Failure" "Improvement"; do
  gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues?labels=${label}&state=all&per_page=100" --paginate \
    --jq '.[] | select(has("pull_request") | not) | {number, body: (.body // "")}'
done | jq -s -c 'unique_by(.number)')"

if [[ "$(jq 'length' <<<"$candidates")" == "0" ]]; then
  log "No acceptance-failure or improvement issues found. Nothing to do."
  exit 0
fi

# Resolve each candidate's target issue: its native `blocks` edges, falling
# back to the retired body marker. issue_relationships.py owns that rule.
records="$(jq -c '.[]' <<<"$candidates" | while IFS= read -r rec; do
  n="$(jq -r '.number' <<<"$rec")"
  blocks="$(blocking_issues "$n" | jq -R -n -c '[inputs | select(length > 0) | tonumber]')"
  jq -c --argjson b "$blocks" '{number, body, blocks: $b}' <<<"$rec"
done | jq -s -c .)"

blockers_json="$(python3 "$DIR/lib/issue_relationships.py" feature-blockers <<<"$records")"

if [[ "$(jq 'length' <<<"$blockers_json")" == "0" ]]; then
  log "No acceptance-failure/improvement issue is related to another issue. Nothing to do."
  exit 0
fi

promoted=0
while IFS= read -r feature; do
  mapfile -t direct < <(jq -r --arg f "$feature" '.[$f][]' <<<"$blockers_json")

  # Heal missing native links (idempotent). A historical issue resolved
  # through the prose fallback gets a real relationship here, which is the
  # one-time backfill: after this sweep runs, its gate is native.
  for blocker in "${direct[@]}"; do
    if [[ "$DRY_RUN" == "1" ]]; then
      log "DRY_RUN: would ensure #$blocker is marked as blocking #$feature"
    elif mark_issue_blocked_by "$feature" "$blocker"; then
      log "#$blocker is marked as blocking #$feature"
    else
      log "[warn] Could not mark #$blocker as blocking #$feature"
    fi
  done

  fstatus="$(issue_status "$feature")"
  if [[ "$fstatus" != "$ACCEPTANCE_STATUS" ]]; then
    log "#$feature status '$fstatus' != '$ACCEPTANCE_STATUS' -- not a promotion candidate (blockers: ${direct[*]})"
    continue
  fi

  # The gate is the TRANSITIVE blocked_by closure, not just the direct
  # blockers: a failure filed against a failure holds the original issue too.
  # In DRY_RUN the heal above did not run, so fall back to the resolved
  # direct list when the native walk returns nothing.
  mapfile -t gate < <(transitive_blocked_by_issues "$feature")
  if [[ "${#gate[@]}" -eq 0 ]]; then
    gate=("${direct[@]}")
  fi

  all_accepted=1
  for blocker in "${gate[@]}"; do
    astatus="$(issue_status "$blocker")"
    if [[ "$astatus" != "$STATUS_FOR_RELEASE" ]]; then
      log "#$feature waits: blocker #$blocker is '$astatus' (needs '$STATUS_FOR_RELEASE')"
      all_accepted=0
      break
    fi
  done
  [[ "$all_accepted" == "1" ]] || continue

  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would promote #$feature to '$STATUS_FOR_RELEASE' (all of: ${gate[*]} accepted)"
    continue
  fi

  set_issue_status "$feature" "$STATUS_FOR_RELEASE"
  gate_refs="$(printf '#%s, ' "${gate[@]}")"
  gh issue comment "$feature" --repo "${REPO_OWNER}/${REPO_NAME}" --body \
    "✅ **Scrummaster Agent**: every issue blocking this one (${gate_refs%, }) has been accepted (For Release) — promoting this issue to **For Release**. Blocking is read from GitHub's native relationships, transitively (owner decision 2026-08-12, #3731)."
  log "Promoted #$feature to '$STATUS_FOR_RELEASE' (blockers accepted: ${gate[*]})"
  promoted=$((promoted + 1))
done < <(jq -r 'keys[]' <<<"$blockers_json")

log "Done. Promoted ${promoted} issue(s)."
