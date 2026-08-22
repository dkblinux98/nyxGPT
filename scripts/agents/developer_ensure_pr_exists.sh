#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  developer_ensure_pr_exists.sh [--dry-run] <issue_number> [branch]

The backstop that closes #3862's first defect: work reached `origin` and no
pull request was ever opened, so it was never reviewed, never merged and
never cleaned up.

With `branch` given, that one branch is checked. WITHOUT it, the branches are
DISCOVERED -- from origin and from the workspace's local refs -- rather than
read off the working tree. That distinction is the whole of #3862's second
round: `git branch --show-current` names a branch `claude-code-action` created
seconds ago for its own use, not the branch that carries the work. See the
"Branch DISCOVERY" comment below the argument parsing for the run that proved
it.

WHY THIS EXISTS. developer_auto_implement.yml pushes the work branch twice
before any PR is possible -- once at creation (developer_create_branch.sh) and
once from "Snapshot uncommitted implementation work", which is deliberate: a
long implementation that hits the Claude step's turn limit must not evaporate
with the runner. But "Submit PR for review" is gated on `success()`, so every
exit between those two points -- Final Verification failing, a Phase 1-3
escalation, a usage-limit abort, the job timing out -- leaves a branch on the
remote with no PR. Three branches sat there for weeks that way, two of them
holding the only copy of 438 lines of test coverage.

Run this with `if: always()` at the end of the job. For each branch it finds
it does exactly one of three things:

  1. **Nothing reached the remote** (no such branch on origin) -> nothing to do.
  2. **The branch's content is provably already on the release branch**
     (scripts/agents/lib/branch_content.py) -> delete it. This is the D-013
     supersession event: a retry onto a fresh branch, or a rebase-and-reapply,
     removes the branch it replaced in the same run. Guarded, so a branch
     carrying anything of its own is never the one that gets deleted.
  3. **Otherwise** -> open a DRAFT pull request, so the work is visible,
     reviewable, recoverable, and covered by `delete_branch_on_merge`.

Draft, not ready-for-review, on purpose: the run did NOT finish its checks, so
this is a rescue, not a submission. developer_submit_for_review.sh remains the
only path that submits work for review (CLAUDE.md, PR Rules) -- when it ran,
its PR is already there and this script finds it and stops.

The draft is a waypoint, not a resting place, and the machinery has to agree
with the instructions printed in its body:

  * The body carries `<!-- rescue-pr: issue-N -->`. developer_auto_implement.yml
    matches that marker among OPEN pull requests when its `Closes #N` lookups
    find nothing, so reassigning the issue continues ON THIS BRANCH instead of
    starting a fresh timestamped one and duplicating the rescued work.
  * When that continuation run passes verification, "Request review for
    existing PR" rewrites the body's `Refs #N` to `Closes #N` and takes the PR
    out of draft. developer_submit_for_review.sh adopts an existing open PR on
    the same head for the same reason.
  * Closing the draft is the discard signal: the marker is matched on OPEN PRs
    only, so a closed rescue leaves the issue free to be implemented again.

Without that loop the rescue would trade one accumulation mode for a worse
one -- an orphan branch at least gets deleted once its content lands, while a
stranded draft PR shields its head branch from every cleanup there is.

Never fails the caller: a rescue that breaks the job it is rescuing is worse
than the orphan it was preventing.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd git; require_cmd jq
  echo "OK"; exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; shift; fi

ISSUE="${1:-}"
if [[ -z "$ISSUE" ]]; then usage >&2; exit 2; fi
# Digits only: the issue number is interpolated into the branch-discovery
# regexes below and into REST paths. Anything else is a caller bug, and a
# caller bug must not become a pattern that matches branches at random.
if [[ ! "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "[ensure-pr] '${ISSUE}' is not an issue number." >&2
  exit 2
fi

load_config
require_gh_auth
require_cmd git
require_cmd jq

# >>> branch-discovery (#3862 round 2) >>>
# The sentinels are load-bearing: tests/test_ensure_pr_exists.sh case 4b cuts
# everything between them out and restores the retired `git branch
# --show-current` form, to show the same fixture going unrescued without this
# block. A check that cannot fail proves nothing (#3775), and this one has
# already passed once over a live defect.
# ----------------------------------------------------------------------
# Branch DISCOVERY (#3862, second round).
#
# WHY THIS IS NOT `git branch --show-current`. That is what the first cut
# did, and it is why the backstop was still standing there while
# `claude/issue-3956-20260819-1943` reached origin, held 3956's only copy of
# its work, got no PR, and was merged by hand two days later.
#
# Run 32291977186 (job 96194626814), read end to end:
#
#   19:43:28  Creating local branch claude/issue-3956-20260819-1943 ...
#   19:55:29  git-push.sh origin claude/issue-3956-20260819-1943   <- the work
#   20:04:46  Submit PR for review -> FAILS (issue #3956 is CLOSED)
#   20:04:56  Creating local branch claude/issue-3956-20260819-2004 ...
#   20:07:23  [ensure-pr] claude/issue-3956-20260819-2004 never reached
#             origin; nothing to review or clean up.
#
# The step ran. `always()` held. The guard fired. It was simply aimed at the
# wrong branch: `claude-code-action` creates and checks out a FRESH
# `claude/issue-<n>-<timestamp>` branch on every invocation, and this workflow
# invokes it six times -- including "Deep analysis with Claude (Phase 3)",
# which runs *after* a failed step, i.e. on exactly the path this backstop
# exists to cover. So on the failure path the workspace's current branch is
# always a never-pushed decoy created seconds earlier, and the branch carrying
# the work is no longer checked out anywhere. Deriving the target from the
# working tree at the end of the job is therefore not merely fragile; it is
# guaranteed wrong in the case that matters.
#
# The remote is the authority instead. With no explicit branch argument the
# candidate set is the union of:
#
#   * the current branch (the previous contract, kept -- it can only add),
#   * every LOCAL branch this run left behind whose name carries this issue
#     number, which captures each branch claude-code-action created regardless
#     of what it decided to call it, and
#   * every branch ON ORIGIN matching the naming conventions the agent loop
#     uses -- the same set cleanup_superseded_branches sweeps, so the two
#     halves of D-013 (rescue and cleanup) look at identical candidates.
#
# Every candidate then goes through the unchanged single-branch logic below,
# one child process each, so one branch's transient API failure cannot stop
# the others from being rescued. An explicit branch argument still means
# exactly that branch.
# ----------------------------------------------------------------------
if [[ -z "${2:-}" ]]; then
  candidates="$(
    {
      git branch --show-current 2>/dev/null || true
      # Name-agnostic, issue-scoped: the issue number must appear delimited by
      # non-digits, so `claude/issue-3956-20260819-1943` matches for 3956 and
      # nothing matches for 819 or 2026 on the strength of the date stamp.
      git for-each-ref --format='%(refname:short)' refs/heads/ 2>/dev/null \
        | grep -E "(^|[^0-9])${ISSUE}([^0-9]|\$)" || true
      git ls-remote --heads origin 2>/dev/null | awk '{print $2}' \
        | sed 's#^refs/heads/##' \
        | grep -E "^(feat|fix|chore)/${ISSUE}-|^claude/issue-${ISSUE}-" || true
    } | grep -v '^$' | sort -u
  )"

  if [[ -z "$candidates" ]]; then
    echo "[ensure-pr] No branch on origin or in this workspace belongs to #${ISSUE}; nothing to check." >&2
    exit 0
  fi

  echo "[ensure-pr] Candidate branches for #${ISSUE}:" >&2
  echo "$candidates" | sed 's/^/[ensure-pr]   /' >&2

  rc=0
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    if [[ "$DRY_RUN" == "1" ]]; then
      "$0" --dry-run "$ISSUE" "$candidate" || rc=$?
    else
      "$0" "$ISSUE" "$candidate" || rc=$?
    fi
  done <<< "$candidates"
  # Reported, never propagated: this runs `if: always()` as the last step of
  # the developer job.
  [[ "$rc" == "0" ]] || _warn "At least one candidate branch could not be processed (last rc=${rc})."
  exit 0
fi
# <<< branch-discovery (#3862 round 2) <<<

REPO="${REPO_OWNER}/${REPO_NAME}"
BASE_BRANCH="$(get_release_branch)"

BRANCH="$2"

if [[ -z "$BRANCH" || "$BRANCH" == "HEAD" ]]; then
  echo "[ensure-pr] Detached HEAD and no branch argument; nothing to check." >&2
  exit 0
fi
if [[ "$BRANCH" == "$BASE_BRANCH" || "$BRANCH" == "master" || "$BRANCH" == "main" ]]; then
  echo "[ensure-pr] On '${BRANCH}' (a protected branch); nothing to check." >&2
  exit 0
fi

git fetch origin "$BASE_BRANCH" >/dev/null 2>&1 || true

if ! git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "[ensure-pr] ${BRANCH} never reached origin; nothing to review or clean up." >&2
  exit 0
fi
git fetch origin "$BRANCH" >/dev/null 2>&1 || true

# Any PR at all -- open, closed or merged -- means the work was routed. A
# closed-unmerged PR is an explicit abandonment decision and reopening the
# question here would fight it.
#
# The fetch is captured separately from the count on purpose. Written as one
# `gh ... | jq ... || echo unknown` pipeline it is dead code under `set -o
# pipefail`: jq succeeds on empty stdin and prints `0`, THEN pipefail fails the
# pipeline and the fallback appends its own line, so a failed `gh` yields
# `pr_count=$'0\nunknown'` -- which is neither `== unknown` nor `-gt 0`, and the
# script proceeds exactly as if it had proved there were zero PRs. The one path
# that promises to fail closed was the one that failed open.
if ! pr_pages="$(gh api "repos/${REPO}/pulls?head=${REPO_OWNER}:${BRANCH}&state=all&per_page=100" \
    --paginate 2>/dev/null)"; then
  _warn "Could not list PRs for ${BRANCH}; leaving it alone rather than opening a duplicate."
  exit 0
fi
if ! pr_count="$(jq -s '[.[][]] | length' <<<"$pr_pages" 2>/dev/null)"; then
  _warn "Could not parse the PR list for ${BRANCH}; leaving it alone rather than opening a duplicate."
  exit 0
fi
if [[ "$pr_count" -gt 0 ]]; then
  echo "[ensure-pr] ${BRANCH} already has ${pr_count} pull request(s); nothing to do." >&2
  exit 0
fi

# No PR. Either the branch carries nothing of its own (delete it) or it
# carries work that must not be lost (open a draft PR for it).
if classify_mergeable "$BRANCH" "$ISSUE" "$BASE_BRANCH" | grep -qx -e merged -e superseded; then
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would delete ${BRANCH} — content provably on ${BASE_BRANCH}" >&2
    exit 0
  fi
  echo "[ensure-pr] Deleting ${BRANCH}: every path it touches is already on ${BASE_BRANCH}." >&2
  delete_remote_branch "$BRANCH"
  exit 0
fi

echo "[ensure-pr] ::warning::${BRANCH} is on origin with no pull request and content that is NOT on ${BASE_BRANCH}. Opening a draft PR so it is not stranded." >&2

issue_json="$(gh api "repos/${REPO}/issues/${ISSUE}" --jq '{title, labels}' 2>/dev/null || echo '{}')"
issue_title="$(echo "$issue_json" | jq -r '.title // ""')"
[[ -n "$issue_title" ]] || issue_title="issue #${ISSUE}"

PR_TITLE="wip: ${issue_title} (#${ISSUE})"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${GITHUB_RUN_ID:-unknown}"

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
{
  # Machine-readable, and the only thing that makes the "continue the work"
  # instruction below true. developer_auto_implement.yml's `check_pr` finds an
  # existing PR by searching for "Closes #ISSUE" in its body; this body carries
  # `Refs`, deliberately (see above), so without a marker of its own a
  # reassignment run would report pr_exists=false, start a fresh timestamped
  # branch, and duplicate the rescued work -- leaving this draft open forever
  # with its head branch shielded from every cleanup by that open PR. That is a
  # new accumulation mode inside the change whose purpose is ending
  # accumulation, so the marker is load-bearing, not decoration. Matched on
  # OPEN PRs only: closing this PR is the discard signal, and a closed rescue
  # must not keep the issue from being implemented again.
  echo "<!-- rescue-pr: issue-${ISSUE} -->"
  echo "## ⚠️ Rescue PR — this work did not complete its checks"
  echo
  echo "The developer-agent run for #${ISSUE} pushed \`${BRANCH}\` to \`origin\` and then"
  echo "ended before reaching \`developer_submit_for_review.sh\`. Without this PR the"
  echo "branch would sit on the remote unreviewed, unmerged and invisible — the defect"
  echo "filed as #3862, which stranded 438 lines of test coverage on two branches."
  echo
  echo "**This is not a submission for review.** It is deliberately a draft: the run's"
  echo "verification did not pass, so the work is incomplete by definition. Reviewing"
  echo "or merging it as-is is not expected."
  echo
  echo "What to do with it:"
  echo
  echo "- **Continue the work** — reassign the developer agent to #${ISSUE}. It finds"
  echo "  this draft by the marker at the top of this body, checks \`${BRANCH}\` out and"
  echo "  continues on it; when the run's verification passes it rewrites the reference"
  echo "  at the bottom of this body into a closing one, marks the PR ready and requests"
  echo "  review. (No closing keyword appears anywhere above that reference, and none"
  echo "  may be added: GitHub honours one anywhere in a body, immediately before the"
  echo "  issue number, so any sentence that writes one — including a sentence merely"
  echo "  explaining the rule — retires the issue the moment someone merges this"
  echo "  unfinished draft.)"
  echo "- **Discard it** — close this PR. Closing it without merging is the explicit"
  echo "  abandonment signal the branch cleanup acts on, so the branch goes with it,"
  echo "  and a later reassignment starts fresh rather than reopening this one."
  echo
  echo "## Context"
  echo "- Issue: ${GITHUB_SERVER_URL:-https://github.com}/${REPO}/issues/${ISSUE}"
  echo "- Head branch: \`${BRANCH}\`"
  echo "- Base branch: \`${BASE_BRANCH}\`"
  echo "- Run that produced it: ${RUN_URL}"
  echo
  echo "Refs #${ISSUE}"
} > "$body_file"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would run: gh pr create --draft --base ${BASE_BRANCH} --head ${BRANCH} --title '${PR_TITLE}'" >&2
  echo "[dry-run] --- PR body ---" >&2
  cat "$body_file" >&2
  echo "[dry-run] --- end PR body ---" >&2
  exit 0
fi

pr_url="$(gh pr create --repo "$REPO" --draft --base "$BASE_BRANCH" --head "$BRANCH" \
  --title "$PR_TITLE" --body-file "$body_file" 2>&1)" || {
  _warn "Could not open the rescue draft PR for ${BRANCH}: ${pr_url}"
  exit 0
}
echo "[ensure-pr] Draft PR opened: ${pr_url}" >&2

pr_number="${pr_url##*/}"

# Best-effort hygiene only. The issue's single label keeps the PR consistent
# with the one-label invariant; nothing here may fail the run.
label="$(real_label_names "$(echo "$issue_json" | jq -c '.labels // []')" | head -1)"
if [[ -n "$label" ]]; then
  gh pr edit "$pr_number" --repo "$REPO" --add-label "$label" >/dev/null 2>&1 \
    || _warn "Could not copy label '${label}' to the rescue PR."
fi

issue_comment "$ISSUE" "🛟 The developer-agent run ended before submitting for review, leaving \`${BRANCH}\` on the remote with no PR. Opened ${pr_url} as a **draft** so the work is not stranded (#3862). It is not ready for review — reassign the developer agent and it will continue on that same branch, or close the PR to discard the branch." \
  >/dev/null 2>&1 || _warn "Could not comment the rescue PR link on issue #${ISSUE}."

echo "$pr_number"
