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
  - If pr_body_file is omitted, a deterministic PR body is generated from issue data.

Then:
  - Issue Status -> In Review
  - Assign -> REVIEW_AGENT
  - Comment with PR link

Outputs:
  - PR number to stdout

Notes:
  - Must be run from the feature branch (not master/release branch).
  - Requires ~/.myGPT/config.ini (or $MYGPT_CONFIG_FILE) and gh auth.

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
BASE_BRANCH="$(get_release_branch)"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
  _die "Detached HEAD; checkout a branch first."
fi
if [[ "$CURRENT_BRANCH" == "master" || "$CURRENT_BRANCH" == "main" || "$CURRENT_BRANCH" == "$BASE_BRANCH" ]]; then
  _die "Refusing to run from '$CURRENT_BRANCH'. Checkout a feature branch."
fi

# ---- Fetch issue data ----
issue_json="$(gh issue view "$ISSUE" --repo "$REPO" --json title,body,labels,url,milestone,state -q '.')"
issue_title="$(echo "$issue_json" | jq -r '.title')"
issue_body="$(echo "$issue_json" | jq -r '.body // ""')"
issue_url="$(echo "$issue_json" | jq -r '.url')"
issue_state="$(echo "$issue_json" | jq -r '.state')"
milestone_title="$(echo "$issue_json" | jq -r '.milestone.title // ""')"

if [[ "$issue_state" != "OPEN" ]]; then
  _die "Issue #$ISSUE is not OPEN (state=$issue_state). Refusing to submit for review."
fi

# ---- Enforce exactly one label (portable; no mapfile) ----
label_count="$(echo "$issue_json" | jq -r '.labels | length')"
if [[ "$label_count" != "1" ]]; then
  echo "[error] Issue #$ISSUE must have exactly one label; found ${label_count}:" >&2
  echo "$issue_json" | jq -r '.labels[].name' 2>/dev/null | sed 's/^/[error] - /' >&2 || true
  _die "Fix the issue labels and retry."
fi
PREFIX_LABEL="$(echo "$issue_json" | jq -r '.labels[0].name')"

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

  {
    echo "Closes #${ISSUE}"
    echo
    echo "## Summary"
    echo "$summary"
    echo
    echo "## Context"
    echo "- Issue: ${issue_url}"
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
  [[ -n "$tmp_body" ]] && rm -f "$tmp_body"
  echo "0"
  exit 0
fi

# Create PR
pr_url="$(gh pr create --repo "$REPO" --base "$BASE_BRANCH" --head "$CURRENT_BRANCH" --title "$PR_TITLE" --body-file "$body_file")"
[[ -n "$pr_url" ]] || _die "Failed to create PR"

pr_number="$(gh pr view "$pr_url" --repo "$REPO" --json number -q .number)"

# Update tracking
set_issue_status "$ISSUE" "$STATUS_IN_REVIEW"
issue_assign_only "$ISSUE" "$REVIEW_AGENT"
issue_comment "$ISSUE" $'PR opened for review:\n'"- ${pr_url}"$'\n\n'"Assigned to @${REVIEW_AGENT}. Status -> ${STATUS_IN_REVIEW}."

[[ -n "$tmp_body" ]] && rm -f "$tmp_body"

echo "[dev] created PR #${pr_number} ${pr_url}" >&2
echo "$pr_number"