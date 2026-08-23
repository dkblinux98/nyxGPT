#!/usr/bin/env bash
# Promote issues whose acceptance blockers are all accepted (owner flow,
# 2026-08-02; native relationships since #3731, owner decision 2026-08-12).
#
# An issue filed during acceptance testing -- an Acceptance Failure
# (@acceptance-failure) or an Improvement (@improvement) -- is a separate
# issue that BLOCKS the issue it was filed against, expressed through
# GitHub's native blocked-by/blocks relationship. The blocked issue parks
# closed while its blockers are reworked; when EVERY blocker -- directly and
# TRANSITIVELY (a blocker of a blocker gates too) -- reaches "For Release",
# this sweep promotes it to "For Release" and comments the promotion on it.
#
# TWO lanes hold such a parked issue (owner decision 2026-08-14, #3780):
# "Acceptance Testing" (the 2026-08-02 flow) and "Acceptance Failed", where
# the owner also parks what they have tested and failed, "so that I don't
# get lost as to what I've tested that has failed". Both are promotion
# candidates and are treated identically here.
#
# What is NOT a candidate in the holding lane is handler-FILED rework -- an
# @acceptance-failure / @improvement issue -- because it has not been fixed
# yet and promoting it would declare unfixed work released. Until 2026-08-22
# that was decided by the issue's STATE (open there == rework), and owner
# standard D-042 broke that proxy: an owner reopening an ORIGINAL is the
# signal that it did not pass acceptance, so an open item in that lane is
# now either. `acceptance_role` decides instead -- a rework label plus the
# native blocking edge, the same predicate the drain gate and both handlers
# use, so no two of them can disagree about one issue.
#
# A reopened original that clears its blockers is promoted, assigned back to
# HUMAN_OWNER and CLOSED here: the reopen asked a question, and this is the
# only place that answers it. Nothing else moves an issue out of that lane;
# its placement is owner signal, and only this all-blockers-accepted
# promotion may change it.
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
frole=""
ACCEPTANCE_STATUS="${STATUS_ACCEPTANCE_TESTING:-Acceptance Testing}"
FAILED_STATUS="${STATUS_ACCEPTANCE_FAILED:-Acceptance Failed}"

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
  parked_lane=0
  # Read once, up front: both parking lanes need it now. An OPEN candidate
  # is a reopened original (D-042) wherever it sits, and the promotion below
  # closes it and hands it back to the owner.
  fstate="$(_issue_open_state "$feature")"
  if [[ "$fstatus" == "$FAILED_STATUS" ]]; then
    # The owner's other parking lane (#3780/D-042). Two populations sit
    # here, and the discriminator is NOT the issue's state:
    #
    #   * handler-FILED rework (an @acceptance-failure / @improvement issue)
    #     -- not a promotion candidate; it has not been fixed yet, and
    #     promoting it would declare unfixed work released.
    #   * an ORIGINAL, closed by its merge or REOPENED by the owner as the
    #     signal that it failed acceptance (D-042, 2026-08-22) -- a
    #     promotion candidate in both states.
    #
    # Before D-042 this read `state != CLOSED -> held rework`, which made a
    # reopened original unpromotable forever: the reopen is the owner's
    # signal, not a work order. issue_acceptance_role is the same predicate
    # the drain gate uses, so the lane the gate refuses to release and the
    # lane this sweep promotes from are the same set by construction.
    if [[ "$fstate" != "CLOSED" ]]; then
      frole="$(issue_acceptance_role "$feature" || true)"
      case "$frole" in
        original)
          log "#$feature is OPEN in '$FAILED_STATUS' -- a reopened original awaiting its blockers (D-042)"
          ;;
        rework | work)
          log "#$feature is OPEN in '$FAILED_STATUS' -- held rework ('$frole'), not a promotion candidate (blockers: ${direct[*]})"
          continue
          ;;
        *)
          log "[warn] #$feature could not be classified (rework vs original) -- leaving it in '$FAILED_STATUS' for the next sweep"
          continue
          ;;
      esac
    fi
    parked_lane=1
  elif [[ "$fstatus" != "$ACCEPTANCE_STATUS" ]]; then
    log "#$feature status '$fstatus' is neither '$ACCEPTANCE_STATUS' nor '$FAILED_STATUS' -- not a promotion candidate (blockers: ${direct[*]})"
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
      # Not promotable -> nothing moves. A parked feature stays exactly
      # where the owner put it, in either lane (#3780).
      log "#$feature waits in '$fstatus': blocker #$blocker is '$astatus' (needs '$STATUS_FOR_RELEASE')"
      all_accepted=0
      break
    fi
  done
  [[ "$all_accepted" == "1" ]] || continue

  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would promote #$feature from '$fstatus' to '$STATUS_FOR_RELEASE' (all of: ${gate[*]} accepted)"
    if [[ "$fstate" == "OPEN" ]]; then
      log "DRY_RUN: would assign #$feature to @${HUMAN_OWNER} and close it (D-042)"
    fi
    continue
  fi

  set_issue_status "$feature" "$STATUS_FOR_RELEASE"

  # A reopened original is still OPEN: the reopen was the owner's "this did
  # not pass acceptance" signal (D-042), and this promotion is where that
  # signal is answered -- hand it back to the owner and close it. A feature
  # that was already closed keeps its assignee and stays closed; nothing
  # here reopens or reassigns what it did not reopen.
  if [[ "$fstate" == "OPEN" ]]; then
    if [[ -n "${HUMAN_OWNER:-}" ]]; then
      assign_issue_verified "$feature" "$HUMAN_OWNER" \
        || log "[warn] Could not assign #$feature to @${HUMAN_OWNER}"
    fi
    gh issue close "$feature" --repo "${REPO_OWNER}/${REPO_NAME}" >/dev/null 2>&1 \
      || log "[warn] Could not close #$feature"
  fi

  gate_refs="$(printf '#%s, ' "${gate[@]}")"
  # Plain `if`, not `[[ ]] && …`: under `set -e` a false test as the whole
  # statement would exit the sweep.
  from_note=""
  if [[ "$parked_lane" == "1" ]]; then
    from_note=" It was parked in **${FAILED_STATUS}** — the lane the owner also uses for features they have tested and failed — and this promotion is the only move the machinery makes out of it (owner decision 2026-08-14, #3780)."
  fi
  closed_note=""
  if [[ "$fstate" == "OPEN" ]]; then
    closed_note=" It was reopened as the signal that it failed acceptance (owner standard D-042); that is now answered, so it is assigned back to @${HUMAN_OWNER:-the human owner} and closed."
  fi
  gh issue comment "$feature" --repo "${REPO_OWNER}/${REPO_NAME}" --body \
    "✅ **Scrummaster Agent**: every issue blocking this one (${gate_refs%, }) has been accepted (For Release) — promoting this issue to **For Release**. Blocking is read from GitHub's native relationships, transitively (owner decision 2026-08-12, #3731).${from_note}${closed_note}"
  log "Promoted #$feature from '$fstatus' to '$STATUS_FOR_RELEASE' (blockers accepted: ${gate[*]})"
  promoted=$((promoted + 1))
done < <(jq -r 'keys[]' <<<"$blockers_json")

log "Done. Promoted ${promoted} issue(s)."
