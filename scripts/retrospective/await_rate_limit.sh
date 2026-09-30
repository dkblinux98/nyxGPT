#!/usr/bin/env bash
set -euo pipefail

# scripts/retrospective/await_rate_limit.sh
#
# Blocks until the authenticated token has at least MIN_REMAINING core REST
# calls left in the current hour, then prints the budget it found.
#
# Why: retro_data_refresh.yml runs every retrospective dump back to back under
# one token, and the heavy ones are call-per-item walks (one job log per
# Claude round for churn, one /timing call per run for spend, one reviews
# call per pull request for review rounds). On 2026-09-17 the churn dump ran
# for 76 minutes and the review-rounds dump dispatched right after it died
# mid-walk and then failed on its very first call when re-dispatched -- the
# shape of an exhausted hourly budget, which `gh` reports only on stderr the
# dump did not surface. A dump that starts against an empty budget cannot
# succeed, so each one waits for the budget instead of consuming a runner
# minute per failure. The remaining/reset numbers are printed so a rate-limit
# failure is diagnosable from the run log.
#
# Usage:
#   await_rate_limit.sh
#
# Env:
#   GH_TOKEN        token the dumps run under (required by gh)
#   MIN_REMAINING   calls that must be available before returning (default 1500)
#   MAX_WAIT        seconds to wait in total before giving up (default 3900 --
#                   one reset window plus slack; a budget that does not come
#                   back inside that is a different problem)

MIN_REMAINING="${MIN_REMAINING:-1500}"
MAX_WAIT="${MAX_WAIT:-3900}"

_log() { echo "[await-rate-limit] $*" >&2; }

usage() {
  awk 'NR <= 2 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
    "${BASH_SOURCE[0]}"
}
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

command -v gh >/dev/null 2>&1 || { _log "ERROR: gh is required"; exit 1; }

waited=0
while :; do
  core="$(gh api rate_limit --jq '.resources.core | "\(.remaining) \(.limit) \(.reset)"')" \
    || { _log "ERROR: could not read the rate limit"; exit 1; }
  read -r remaining limit reset <<<"$core"
  now="$(date +%s)"
  if (( remaining >= MIN_REMAINING )); then
    _log "core budget: ${remaining}/${limit} remaining (resets in $(( reset > now ? reset - now : 0 ))s)"
    exit 0
  fi
  wait_for=$(( reset - now + 15 ))
  (( wait_for < 30 )) && wait_for=30
  if (( waited + wait_for > MAX_WAIT )); then
    _log "ERROR: core budget ${remaining}/${limit} below ${MIN_REMAINING} and the reset is ${wait_for}s away; already waited ${waited}s of ${MAX_WAIT}s"
    exit 1
  fi
  _log "core budget ${remaining}/${limit} below ${MIN_REMAINING}; waiting ${wait_for}s for the reset"
  sleep "$wait_for"
  waited=$(( waited + wait_for ))
done
