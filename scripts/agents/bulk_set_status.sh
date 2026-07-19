#!/usr/bin/env bash
#
# bulk_set_status.sh — set the project Status field on a list of issues.
#
# Owner-driven acceptance tooling: after a stakeholder sweep, move batches of
# issues between board states (e.g. "In Review" -> "For Release") without
# clicking through the board. Uses the same sanctioned helpers as the merge
# flow (scripts/agents/lib/gh_project.sh).
#
# Env:
#   ISSUES   Comma/space-separated issue numbers (required).
#   STATUS   Target status option name, validated against the Status field
#            (required; e.g. "For Release").
#   DRY_RUN  "true" (default) = list what would change; "false" = apply.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/gh_project.sh"

DRY_RUN="${DRY_RUN:-true}"
ISSUES="${ISSUES:?ISSUES is required (comma/space-separated numbers)}"
STATUS="${STATUS:?STATUS is required (target Status option name)}"

load_config
require_gh_auth

# Validate the target status is a real option before touching anything.
opt_id="$(single_select_option_id "$STATUS_FIELD" "$STATUS")"
[[ -n "$opt_id" && "$opt_id" != "null" ]] || _die "Unknown ${STATUS_FIELD} option: '${STATUS}'"

# Normalize separators, validate numbers.
normalized="$(echo "$ISSUES" | tr ',' ' ')"
count=0
for n in $normalized; do
  [[ "$n" =~ ^[0-9]+$ ]] || _die "Not an issue number: '$n'"
  count=$((count + 1))
done
echo "[bulk-status] ${count} issue(s) -> '${STATUS}' (dry_run=${DRY_RUN})" >&2

if [[ "$DRY_RUN" != "false" ]]; then
  echo "[bulk-status] DRY RUN — would set: ${normalized}" >&2
  exit 0
fi

ok=0; failed=0
for n in $normalized; do
  if set_issue_status "$n" "$STATUS"; then
    echo "[bulk-status] #${n} -> ${STATUS}" >&2
    ok=$((ok + 1))
  else
    echo "[bulk-status] WARN: failed on #${n}" >&2
    failed=$((failed + 1))
  fi
done
echo "[bulk-status] Done — ${ok} updated, ${failed} failed." >&2
[[ "$failed" -eq 0 ]]
