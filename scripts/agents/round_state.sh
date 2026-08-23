#!/usr/bin/env bash
#
# round_state.sh -- derive an acceptance round's state from git and GitHub.
#
# WHY THIS EXISTS. Round status was composed by hand, from notes and from what
# a command printed, and it was wrong five separate times on 2026-08-22 --
# always in the same way. Each wrong claim came from a PROXY for the state
# rather than the state:
#
#   * a push command's exit status instead of the branch it produced (three
#     branches reported forward-merged when only one was),
#   * a `gh pr list` result read minutes later, after more had merged,
#   * a commit's SUBJECT instead of its diff (`fix: address acceptance
#     failure (#3825)` turned out to be CI hygiene touching no src/ file),
#   * a dispatch list instead of the repository (three issues reported "in
#     review" that had no branch and no PR at all).
#
# Every proxy was NEARLY right, which is why re-reading the summary never
# caught it. So this prints nothing it has not just computed.
#
# Three rules are encoded because each one fooled a human reader first:
#
#   1. A branch's freshness is `git rev-list --count origin/<br>..origin/<rel>`,
#      never "the push seemed to work".
#   2. An OPEN issue whose newest closing PR is MERGED is a RE-TEST FAILURE --
#      the fix landed and the owner reopened it. Reporting that as "merged" is
#      the single most misleading thing this script could do.
#   3. ...unless the issue has OPEN blockers, in which case it is WAITING for
#      them and has no work of its own (#3829, blocked by #3991).
#
# Usage:
#   scripts/agents/round_state.sh 3806 3811 3812 ...
#   scripts/agents/round_state.sh --lane "Acceptance Failed"   # whole lane
#
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ROUND_STATE_NO_CONFIG lets the test suite drive the classification rules with
# stubbed `gh`/`git` on PATH. load_config validates a real ~/.nyxGPT/config.ini,
# which a stub harness has no business providing; the rules under test do not
# depend on it. Everything else about the run is identical.
if [[ -z "${ROUND_STATE_NO_CONFIG:-}" ]]; then
  # shellcheck source=lib/gh_project.sh
  source "$DIR/lib/gh_project.sh"
  load_config
fi
command -v jq >/dev/null || { echo "[error] jq is required" >&2; exit 2; }
command -v gh >/dev/null || { echo "[error] gh is required" >&2; exit 2; }

# #3614: no script hardcodes a release-branch name. The normal path gets
# RELEASE_BRANCH from load_config; the NO_CONFIG test path must supply it.
# A default here would, after the line rolls, compute `lag` against the
# retired branch -- a nearly-right number, which is rule 1 broken from the
# inside and exactly what this script exists to refuse.
RELEASE="${RELEASE_BRANCH:?RELEASE_BRANCH must be set (load_config provides it; set it explicitly when ROUND_STATE_NO_CONFIG=1)}"
REPO="${REPO_OWNER}/${REPO_NAME}"

issues=()
if [[ "${1:-}" == "--lane" ]]; then
  lane="${2:?--lane needs a Status name}"
  mapfile -t issues < <(
    gh project item-list "${PROJECT_NUMBER}" --owner "${PROJECT_OWNER}" \
      --format json --limit 5000 |
      jq -r --arg s "$lane" '.items[]|select(.status==$s)|.content.number//empty'
  )
else
  issues=("$@")
fi
[[ "${#issues[@]}" -gt 0 ]] || { echo "usage: round_state.sh <issue>... | --lane <Status>" >&2; exit 2; }

git fetch origin -q 2>/dev/null || true

# One query for every PR, so the report is a single point-in-time snapshot
# rather than a series of reads that can disagree with each other.
# A failed query leaves $prs empty under `set -uo pipefail` (no -e), and every
# issue then classifies as "not started" on stdout while the error scrolls by
# on stderr -- the most misleading output this script can produce, reachable
# without any rule being wrong. Refuse instead. Truncation is the same defect
# slower: a merged closing PR outside the window reports as no PR at all.
PR_LIMIT="${ROUND_STATE_PR_LIMIT:-1000}"
prs="$(gh pr list --repo "$REPO" --state all --limit "$PR_LIMIT" \
        --json number,state,mergedAt,reviewDecision,mergeable,closingIssuesReferences)" \
  || { echo "[error] PR snapshot query failed -- refusing to report a round state it did not compute" >&2; exit 2; }
if [[ "$(jq 'length' <<<"$prs")" -ge "$PR_LIMIT" ]]; then
  echo "[warn] PR snapshot hit the ${PR_LIMIT}-PR window; older closing PRs are outside it and their issues may misreport as not started" >&2
fi

printf "%-7s %-8s %-28s %s\n" ISSUE ISSUE_ST EVIDENCE DETAIL
for n in "${issues[@]}"; do
  istate="$(gh issue view "$n" --repo "$REPO" --json state --jq .state 2>/dev/null || echo "?")"
  row="$(printf '%s' "$prs" | jq -r --argjson n "$n" \
        '[.[]|select(.closingIssuesReferences[]?.number==$n)]|sort_by(.number)|last // empty')"

  if [[ -z "$row" || "$row" == "null" ]]; then
    br="$(git ls-remote --heads origin 2>/dev/null | grep -oE "refs/heads/[^ ]*${n}[^ ]*" | head -1 | sed 's|refs/heads/||')"
    if [[ -n "$br" ]]; then
      printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "BRANCH, NO PR" "$br -- pushed, never submitted"
    else
      printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "NOTHING" "no PR, no branch -- not started"
    fi
    continue
  fi

  pr="$(jq -r .number <<<"$row")"; st="$(jq -r .state <<<"$row")"
  if [[ "$st" == "MERGED" ]]; then
    if [[ "$istate" == "OPEN" ]]; then
      blk="$(gh api "repos/$REPO/issues/$n/dependencies/blocked_by" \
             --jq '[.[]|select(.state=="open")|.number]|join(",")' 2>/dev/null || true)"
      if [[ -n "$blk" ]]; then
        printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "WAITING on blockers" "blocked by $blk -- no work of its own"
      else
        printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "REOPENED after PR#$pr" "old fix merged -- NEEDS NEW WORK"
      fi
    else
      printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "MERGED PR#$pr" "$(jq -r '.mergedAt' <<<"$row")"
    fi
    continue
  fi

  if [[ "$st" == "CLOSED" ]]; then
    printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "CLOSED PR#$pr -- abandoned" "no merged fix -- NEEDS NEW WORK"
    continue
  fi

  rd="$(jq -r '.reviewDecision // "PENDING"' <<<"$row")"
  mg="$(jq -r '.mergeable // "?"' <<<"$row")"
  br="$(git ls-remote --heads origin 2>/dev/null | grep -oE "refs/heads/[^ ]*${n}[^ ]*" | head -1 | sed 's|refs/heads/||')"
  lag="?"
  [[ -n "$br" ]] && lag="$(git rev-list --count "origin/${br}..origin/${RELEASE}" 2>/dev/null || echo '?')"
  printf "%-7s %-8s %-28s %s\n" "#$n" "$istate" "$st PR#$pr $rd" "mergeable=$mg lag=$lag"
done
