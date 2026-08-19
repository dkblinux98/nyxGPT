#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_submit_for_review.sh [--dry-run] <issue_number> [pr_title] [pr_body_file]

Behavior:
  - Creates a PR from the current git branch into the current release branch (RELEASE_BRANCH)
  - If pr_title is omitted, it is generated from issue label/title:
      "<SingleLabel>: <Issue Title> (#<N>)"
    The issue MUST have exactly one label, otherwise the script fails.
    Workflow-control labels (e.g. "usage-limit-retry", added by self-heal
    automation) are ignored for this check — they never count toward or
    against the one-label invariant.
  - If pr_body_file is omitted, a deterministic PR body is generated from issue data.

Then:
  - PR Assignee -> REPO_OWNER
  - PR Reviewer -> REVIEW_AGENT
  - Issue Status -> In Review
  - Issue Assign -> REVIEW_AGENT
  - Comment with PR link

Outputs:
  - PR number to stdout

Notes:
  - Must be run from the feature branch (not master/release branch).
  - Requires ~/.nyxGPT/config.ini (or $NYXGPT_CONFIG_FILE) and gh auth.

EOF
}

DRY_RUN=0
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

ISSUE="${1:-}"
[[ -n "$ISSUE" ]] || { usage; exit 2; }

# Strict config
load_config

require_gh_auth
require_cmd git
require_cmd jq
require_cmd mktemp
require_cmd python3

REPO="${REPO_OWNER}/${REPO_NAME}"

# All PRs target the release branch
BASE_BRANCH="$(get_release_branch)"
echo "[dev] PR will target release branch $BASE_BRANCH" >&2

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
  _die "Detached HEAD; checkout a branch first."
fi
if [[ "$CURRENT_BRANCH" == "master" || "$CURRENT_BRANCH" == "main" || "$CURRENT_BRANCH" == "$BASE_BRANCH" ]]; then
  _die "Refusing to run from '$CURRENT_BRANCH'. Checkout a feature branch."
fi

# ---- Fetch issue data ----
issue_json="$(gh api "repos/${REPO}/issues/${ISSUE}" \
  --jq '{title, body, labels, url: .html_url, milestone, state: (.state | ascii_upcase)}')"
issue_title="$(echo "$issue_json" | jq -r '.title')"
issue_body="$(echo "$issue_json" | jq -r '.body // ""')"
issue_url="$(echo "$issue_json" | jq -r '.url')"
issue_state="$(echo "$issue_json" | jq -r '.state')"
milestone_title="$(echo "$issue_json" | jq -r '.milestone.title // ""')"

if [[ "$issue_state" != "OPEN" ]]; then
  _die "Issue #$ISSUE is not OPEN (state=$issue_state). Refusing to submit for review."
fi

# ---- Enforce exactly one label, ignoring workflow-control labels (portable; no mapfile) ----
labels_json="$(echo "$issue_json" | jq -c '.labels')"
real_labels="$(real_label_names "$labels_json")"
label_count="$(printf '%s\n' "$real_labels" | grep -c . || true)"
if [[ "$label_count" != "1" ]]; then
  echo "[error] Issue #$ISSUE must have exactly one label (excluding workflow-control labels); found ${label_count}:" >&2
  [[ -n "$real_labels" ]] && printf '%s\n' "$real_labels" | sed 's/^/[error] - /' >&2
  _die "Fix the issue labels and retry."
fi
PREFIX_LABEL="$real_labels"

# ---- Optional overrides (args) ----
PR_TITLE="${2:-}"
PR_BODY_FILE="${3:-}"

if [[ -z "$PR_TITLE" ]]; then
  PR_TITLE="${PREFIX_LABEL}: ${issue_title} (#${ISSUE})"
fi

tmp_body=""
body_file="$PR_BODY_FILE"

first_paragraph() {
  python3 - <<'PY' "$1"
import sys
body = sys.argv[1] or ""
lines = body.replace("\r\n", "\n").split("\n")

i = 0
while i < len(lines) and not lines[i].strip():
    i += 1

para = []
while i < len(lines) and lines[i].strip():
    para.append(lines[i].rstrip())
    i += 1

print("\n".join(para).strip())
PY
}

extract_tasklist() {
  python3 - <<'PY' "$1"
import sys, re
body = sys.argv[1] or ""
lines = body.replace("\r\n", "\n").split("\n")
tasks = []
for line in lines:
    if re.match(r'^\s*[-*]\s+\[[ xX]\]\s+.+', line):
        tasks.append(line.rstrip())
print("\n".join(tasks))
PY
}

summary="$(first_paragraph "$issue_body")"
[[ -n "$summary" ]] || summary="See issue for details."

tasklist="$(extract_tasklist "$issue_body")"

labels_md="- \`${PREFIX_LABEL}\`"

if [[ -z "$body_file" ]]; then
  tmp_body="$(mktemp)"
  body_file="$tmp_body"

  # What this issue blocks, read from the native relationship -- the sole
  # storage since #3731 (ledger D-002). Reported as context only: an
  # acceptance failure NEVER closes the issue it blocks. That issue stays
  # closed and parked (handle_acceptance_failure.yml), and
  # promote_accepted_features.sh moves it to For Release once its whole
  # blocked-by closure is accepted. Adding `Closes #N` here would close it on
  # merge and bypass that gate.
  BLOCKED_ISSUES="$(blocking_issues "$ISSUE" | paste -sd ',' - | sed 's/,/, #/g')"
  [[ -z "$BLOCKED_ISSUES" ]] || BLOCKED_ISSUES="#${BLOCKED_ISSUES}"

  {
    echo "Closes #${ISSUE}"
    echo
    echo "## Summary"
    echo "$summary"
    echo
    echo "## Context"
    echo "- Issue: ${issue_url}"
    if [[ -n "$BLOCKED_ISSUES" ]]; then
      echo "- Blocks acceptance of: ${BLOCKED_ISSUES} (promoted by the acceptance sweep, not closed by this PR)"
    fi
    [[ -n "$milestone_title" ]] && echo "- Milestone: ${milestone_title}" || echo "- Milestone: (none)"
    echo "- Label:"
    echo "$labels_md"
    echo "- Base branch: \`${BASE_BRANCH}\`"
    echo "- Head branch: \`${CURRENT_BRANCH}\`"
    echo
    echo "## Checklist"
    if [[ -n "$tasklist" ]]; then
      echo "$tasklist"
    else
      echo "- (no task list found in issue body)"
    fi
    echo
    echo "## Testing"
    echo "- [ ] Added/updated tests"
    echo "- [ ] All tests pass"
    echo
    echo "## Notes"
    echo "- "
  } > "$body_file"
else
  [[ -f "$body_file" ]] || _die "PR body file not found: $body_file"
fi

echo "[dev] repo=$REPO" >&2
echo "[dev] issue=#$ISSUE branch=$CURRENT_BRANCH base=$BASE_BRANCH" >&2
echo "[dev] pr_title=$PR_TITLE" >&2
echo "[dev] body_file=$body_file" >&2

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would run: gh pr create --repo \"$REPO\" --base \"$BASE_BRANCH\" --head \"$CURRENT_BRANCH\" --title \"$PR_TITLE\" --body-file \"$body_file\"" >&2
  echo "[dry-run] would set issue status -> \"$STATUS_IN_REVIEW\", assign -> \"$REVIEW_AGENT\", comment PR link" >&2
  # The generated body is the thing worth inspecting in a dry run -- and the
  # temp file holding it is deleted on the next line.
  echo "[dry-run] --- PR body ---" >&2
  cat "$body_file" >&2
  echo "[dry-run] --- end PR body ---" >&2
  [[ -n "$tmp_body" ]] && rm -f "$tmp_body"
  echo "0"
  exit 0
fi

# Adopt, don't duplicate (#3862). This branch may already carry an OPEN pull
# request that is not a submission -- a rescue draft opened by
# developer_ensure_pr_exists.sh when an earlier run died before reaching this
# script. `gh pr create` against such a head fails outright ("a pull request
# already exists"), and the old unconditional create turned that into a _die,
# so the rescue draft became permanent debris: never marked ready, never
# reviewed, and its head branch shielded from every cleanup by the open PR.
# Take the existing PR over instead -- give it this script's body (which
# carries `Closes #ISSUE`, unlike the rescue body's deliberate `Refs`), its
# title, and take it out of draft. From here on it is an ordinary submission
# and the rest of this script treats it as one.
existing_pr="$(gh pr list --repo "$REPO" --head "$CURRENT_BRANCH" --state open \
  --json number --jq '.[0].number // empty' 2>/dev/null || echo "")"

if [[ -n "$existing_pr" ]]; then
  echo "[dev] Adopting existing open PR #${existing_pr} on ${CURRENT_BRANCH} instead of opening a second one" >&2
  gh pr edit "$existing_pr" --repo "$REPO" --title "$PR_TITLE" --body-file "$body_file" >&2 \
    || _die "Failed to update existing PR #${existing_pr} with the submission body"
  # Idempotent by design: `gh pr ready` on an already-ready PR is a no-op that
  # exits 0, so this costs nothing on the ordinary path.
  gh pr ready "$existing_pr" --repo "$REPO" >&2 \
    || _warn "Could not mark PR #${existing_pr} ready for review"
  pr_url="${issue_url%/issues/*}/pull/${existing_pr}"
  pr_number="$existing_pr"
else
  # Create PR
  pr_url="$(gh pr create --repo "$REPO" --base "$BASE_BRANCH" --head "$CURRENT_BRANCH" --title "$PR_TITLE" --body-file "$body_file")"
  [[ -n "$pr_url" ]] || _die "Failed to create PR"

  # PR number is the trailing path segment of the create URL
  # (https://github.com/OWNER/REPO/pull/NNN) -- avoids a redundant read.
  pr_number="${pr_url##*/}"
fi

# Set PR assignee and reviewer to review-agent
# PR and issue remain assigned to review-agent until merge completes successfully
if ! gh pr edit "$pr_number" --repo "$REPO" --add-assignee "$REVIEW_AGENT" >&2; then
  _warn "Failed to set PR assignee to $REVIEW_AGENT"
fi
echo "[dev] PR assignee set: @$REVIEW_AGENT" >&2

# Request review from review-agent (critical - must succeed)
if ! gh pr edit "$pr_number" --repo "$REPO" --add-reviewer "$REVIEW_AGENT" >&2; then
  _die "Failed to request review from $REVIEW_AGENT. PR created but reviewer not set. Manual intervention required for PR #${pr_number}"
fi
echo "[dev] Reviewer set: @$REVIEW_AGENT" >&2

# Copy label from issue to PR
gh pr edit "$pr_number" --repo "$REPO" --add-label "$PREFIX_LABEL" >&2 || _warn "Failed to add label '$PREFIX_LABEL' to PR"
echo "[dev] Label copied: $PREFIX_LABEL" >&2

# Copy milestone from issue to PR (if present)
if [[ -n "$milestone_title" ]]; then
  gh pr edit "$pr_number" --repo "$REPO" --milestone "$milestone_title" >&2 || _warn "Failed to set milestone '$milestone_title' on PR"
  echo "[dev] Milestone copied: $milestone_title" >&2
fi

# Link PR to issue in Development field (GitHub auto-links via "Closes #N", but verify)
gh issue develop "$ISSUE" --repo "$REPO" --checkout false --branch "$CURRENT_BRANCH" 2>/dev/null || {
  _warn "Could not explicitly link PR to issue Development field (may already be linked via 'Closes #N')"
}

# Ensure PR project hygiene: add to project and copy fields from issue
ensure_pr_project_hygiene "$pr_number" "$ISSUE"

# Update tracking
set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$REVIEW_AGENT"
issue_comment "$ISSUE" $'PR opened for review:\n'"- ${pr_url}"$'\n\n'"Assigned to @${REVIEW_AGENT}. Status -> ${STATUS_IN_REVIEW}."

[[ -n "$tmp_body" ]] && rm -f "$tmp_body"

echo "[dev] created PR #${pr_number} ${pr_url}" >&2
echo "$pr_number"
