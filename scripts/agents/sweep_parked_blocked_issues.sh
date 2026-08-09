#!/usr/bin/env bash
# Batch-promotes merged-but-blocked issues parked in "In Review" (owner
# process rule, 2026-08-04, #3631): review_accept_and_merge.sh parks a
# merged issue at In Review instead of Acceptance Testing when it has open
# native blocked_by dependencies, since the owner cannot meaningfully accept
# a feature whose acceptance criteria depend on unfinished work. This sweep
# finds every such parked issue whose blockers have ALL completed (merged
# and themselves in Acceptance Testing or beyond) and promotes it -- see
# sweep_parked_blocked_issues() in lib/gh_project.sh for the fixed-point
# resolution loop that lets a parked blocker chain resolve transitively in
# one run.
#
# Run via .github/workflows/sweep_parked_blocked_issues.yml (cron + dispatch).
#
# ENV:
#   DRY_RUN=1   report what would be promoted, change nothing
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
load_config
require_gh_auth
require_cmd jq

sweep_parked_blocked_issues
