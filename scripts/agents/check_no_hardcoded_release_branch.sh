#!/usr/bin/env bash
set -euo pipefail

# scripts/agents/check_no_hardcoded_release_branch.sh
#
# Guard for the 2026-08-04 owner principle (#3614): no YAML workflow or
# script may hardcode a release-branch name. Release branches roll every
# version (v2.0.0 -> v2.1.0 -> v3.0.0 -> ...), so a literal branch/base/ref
# is a time bomb that breaks the next roll. The base must always resolve
# dynamically (vars.RELEASE_BRANCH, falling back to the live repo default
# branch) — see get_release_branch() in scripts/agents/lib/gh_project.sh
# and the base-resolution logic in .github/workflows/cleanup_stale_branches.yml.
#
# Scans .github/workflows/ and scripts/agents/ for any "vX.Y.Z"-shaped
# string, then excludes the specific sites #3614 confirmed are NOT branch
# references: GitHub Action version pins (@v1, @v4, ...), a one-shot
# milestone-label bootstrap script already run (create_phase6.sh), a doc
# example (README-notification-workflows.md), this guard script's own
# docstring, and comments describing the "Release vX.Y.Z" issue-title /
# milestone-title matching convention (a string pattern, not a branch).
# Anything else that matches is a real, un-reviewed hit and fails the check.
#
# Usage: bash scripts/agents/check_no_hardcoded_release_branch.sh
# Exit 0 = clean. Exit 1 = a hardcoded release-branch literal (or an
# unreviewed vX.Y.Z hit that needs triage) was found; hits are printed.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Known-safe, previously reviewed non-branch mentions (#3614). Extending
# this list is the deliberate way to allow a new comment/doc reference;
# anything not listed here that matches must be fixed or triaged.
ALLOWLIST_FILES=(
  ".github/workflows/add-to-release-issue-on-milestone.yml"
  ".github/workflows/README-notification-workflows.md"
  "scripts/agents/create_phase6.sh"
  "scripts/agents/check_no_hardcoded_release_branch.sh"
  "scripts/agents/developer_pull_next.sh"
  "scripts/agents/lib/gh_project.sh"
  "scripts/agents/lib/summarize_backlog_page.py"
  "scripts/agents/verify_phase6_fields.sh"
)

allowlist_pattern="$(IFS='|'; echo "${ALLOWLIST_FILES[*]}")"

hits="$(grep -rnE 'v[0-9]+\.[0-9]+\.[0-9]+' .github/workflows scripts/agents \
  | grep -v -E "^(${allowlist_pattern}):" \
  | grep -v -E '@v[0-9]' || true)"

if [[ -n "$hits" ]]; then
  echo "Hardcoded release-branch literal(s) found (not on the allowlist):" >&2
  echo "$hits" >&2
  exit 1
fi

echo "OK: no hardcoded release-branch literal found in .github/workflows/ or scripts/agents/."
exit 0
