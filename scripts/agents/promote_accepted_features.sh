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
# Run via .github/workflows/promote_accepted_features.yml, which is
# DISPATCH-ONLY since #4201 (ledger D-070): the actor that changes a lane
# dispatches this sweep, and the owner's own board moves are covered by one
# deliberate `gh workflow run promote_accepted_features.yml`. There is no
# cron. That is why the promotion loop below runs to a FIXED POINT -- the
# `*/30` cron was doubling as the cascade iterator, resolving one hop per
# tick, and with the poll gone a chain has to converge inside one run (the
# same property sweep_parked_blocked_issues() has always had, for the same
# reason).
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
#
# `Escalation` is swept too (#4134). An escalation REPLACES the issue's label,
# so an escalated acceptance failure carries `Escalation` and nothing else --
# it would drop straight out of a label query over the other two, and the
# issue it blocks would then never be promoted. It is swept here and
# `issue_acceptance_role` substitutes the recorded prior label back in, so
# such a candidate classifies as the rework it still is.
candidates="$(for label in "Acceptance%20Failure" "Improvement" "Escalation"; do
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

# Promotion runs to a fixed point (#4201). Promoting #A to For Release can
# complete #B's gate, and until the cron was removed that second hop simply
# waited for the next tick -- the poll was the iterator. Each pass re-reads
# the blockers' Status, so a chain of any length resolves in one run, and
# the loop stops on the first pass that promotes nothing (an upper bound of
# one pass per candidate plus one is kept as a hard stop, so a bug here
# cannot spin).
#
# Promotions made THIS run are recorded in `promoted_status` and consulted
# before the live read, exactly as sweep_parked_blocked_issues() does with
# its `resolved` cache: the Projects v2 write and the read behind
# issue_status() are not guaranteed read-after-write consistent, and a
# same-run chain must not depend on that.
promoted=0
declare -A promoted_this_run=()
declare -A promoted_status=()
healed=0
max_passes=$(($(jq 'length' <<<"$blockers_json") + 1))

# Status of a gate blocker, preferring what this run already did to it.
blocker_status() {
  local n="$1"
  if [[ -n "${promoted_status[$n]:-}" ]]; then
    echo "${promoted_status[$n]}"
  else
    issue_status "$n"
  fi
}

for ((pass = 1; pass <= max_passes; pass++)); do
pass_promoted=0
while IFS= read -r feature; do
  # Plain `if`, per this file's convention for a bare test (see the note at
  # the promotion comment below).
  if [[ -n "${promoted_this_run[$feature]:-}" ]]; then
    continue
  fi
  mapfile -t direct < <(jq -r --arg f "$feature" '.[$f][]' <<<"$blockers_json")

  # Heal missing native links (idempotent). A historical issue resolved
  # through the prose fallback gets a real relationship here, which is the
  # one-time backfill: after this sweep runs, its gate is native.
  #
  # First pass only: the write is idempotent, but re-issuing it on every
  # pass would multiply the sweep's API cost by the chain length for no
  # effect -- the links a later pass could need are already native by then.
  if [[ "$healed" == "0" ]]; then
    for blocker in "${direct[@]}"; do
      if [[ "$DRY_RUN" == "1" ]]; then
        log "DRY_RUN: would ensure #$blocker is marked as blocking #$feature"
      elif mark_issue_blocked_by "$feature" "$blocker"; then
        log "#$blocker is marked as blocking #$feature"
      else
        log "[warn] Could not mark #$blocker as blocking #$feature"
      fi
    done
  fi

  # An escalated issue is the owner's (#4134). Promoting it to For Release
  # and closing it -- which is what this sweep does to a reopened original --
  # would answer a question the owner is still holding. It becomes a
  # candidate again the moment they restore its real label; since #4201 that
  # restore is a board/label action no event reports, so the owner's one
  # dispatch (`gh workflow run promote_accepted_features.yml`) is what picks
  # it up. Nothing is lost by waiting -- only the sweep that notices it is
  # now asked for rather than armed.
  if [[ "$(issue_escalation_state "$feature")" == "yes" ]]; then
    log "#$feature is escalated to @${HUMAN_OWNER:-the owner} ('${ESCALATION_LABEL}', #4134) -- not promoted. It returns to this sweep when the owner restores its real label and dispatches it."
    continue
  fi

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
          log "[warn] #$feature could not be classified (rework vs original) -- leaving it in '$FAILED_STATUS' for the next dispatch of this sweep"
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
    astatus="$(blocker_status "$blocker")"
    # "Has this blocker been accepted?" is asked of the one vocabulary in
    # lib/acceptance_status.py (#4204, D-066). It used to be `!=
    # "$STATUS_FOR_RELEASE"`, which read a blocker the release ceremony had
    # moved to `Done` as unaccepted -- and since nothing moves an issue out of
    # `Done`, whatever it blocked was unpromotable permanently.
    # `|| rc=$?`, never a bare call: under `set -e` a false test standing as
    # the whole statement would exit the sweep (the same trap this file's
    # other plain-`if` notes are about).
    rc=0
    status_is_accepted "$astatus" || rc=$?
    if [[ "$rc" == "2" ]]; then
      log "[warn] Could not read the accepted-status vocabulary -- holding #$feature (blocker #$blocker is '$astatus')"
      all_accepted=0
      break
    fi
    if [[ "$rc" != "0" ]]; then
      # Not promotable -> nothing moves. A parked feature stays exactly
      # where the owner put it, in either lane (#3780).
      log "#$feature waits in '$fstatus': blocker #$blocker is '$astatus' (needs one of: $(accepted_statuses_json | jq -r 'join(", ")'))"
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
    # The simulated promotion is recorded too, so a dry run reports the
    # whole cascade a real run would perform rather than only its first
    # hop -- the same choice sweep_parked_blocked_issues() makes.
    promoted_this_run["$feature"]=1
    promoted_status["$feature"]="$STATUS_FOR_RELEASE"
    promoted=$((promoted + 1))
    pass_promoted=$((pass_promoted + 1))
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
  promoted_this_run["$feature"]=1
  promoted_status["$feature"]="$STATUS_FOR_RELEASE"
  promoted=$((promoted + 1))
  pass_promoted=$((pass_promoted + 1))
done < <(jq -r 'keys[]' <<<"$blockers_json")

healed=1
if [[ "$pass_promoted" == "0" ]]; then
  break
fi
log "Pass ${pass} promoted ${pass_promoted} issue(s) -- re-checking for a cascade"
done

log "Done. Promoted ${promoted} issue(s)."
