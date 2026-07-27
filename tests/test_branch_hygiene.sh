#!/usr/bin/env bash
set -uo pipefail

# tests/test_branch_hygiene.sh
# Regression tests for the branch-hygiene helpers added to
# scripts/agents/lib/gh_project.sh for #3392: extract_issue_number,
# classify_mergeable, closed_unmerged_pr_exists, and
# cleanup_superseded_branches (used by developer_create_branch.sh and
# reconcile_dead_branches.sh).
#
# Usage: bash tests/test_branch_hygiene.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAILURES=0

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "[FAIL] $desc: expected '$expected', got '$actual'" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

# Referenced indirectly: interpolated inside gh_project.sh's own function
# bodies (closed_unmerged_pr_exists, classify_mergeable), not in this file.
# shellcheck disable=SC2034
REPO_OWNER="test-owner"
# shellcheck disable=SC2034
REPO_NAME="test-repo"

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

# --- Test group 1: extract_issue_number parses every branch-naming ---
# --- convention the agent loop uses, and rejects non-issue branches ---
_assert_eq "claude/issue-<n>-slug" "3392" "$(extract_issue_number "claude/issue-3392-20260727-1800")"
_assert_eq "feat/<n>-slug" "3352" "$(extract_issue_number "feat/3352-thing")"
_assert_eq "fix/<n>-slug" "2688" "$(extract_issue_number "fix/2688-thing")"
_assert_eq "chore/<n>-slug" "3358" "$(extract_issue_number "chore/3358-thing")"
_assert_eq "intentional non-issue branch yields empty" "" "$(extract_issue_number "v2.0.0-pre-nyxAgent-implementation")"
_assert_eq "release branch yields empty" "" "$(extract_issue_number "v2.0.0")"

# --- Git fixture for classify_mergeable: a real bare "origin" plus a work ---
# --- tree, so merge-base/patch-id comparisons run against real git history ---
# --- instead of being mocked away (this is the crux safety logic for #3392) ---
SANDBOX="$(mktemp -d)"
cleanup_sandbox() { rm -rf "$SANDBOX"; }
trap cleanup_sandbox EXIT

git init -q --bare "$SANDBOX/origin.git"
git clone -q "$SANDBOX/origin.git" "$SANDBOX/work"
cd "$SANDBOX/work" || exit 1
git config user.email "branch-hygiene-test@example.invalid"
git config user.name "Branch Hygiene Test"
git config commit.gpgsign false

echo base > file.txt
git add file.txt
git commit -q -m base
git branch -M v2.0.0
git push -q -u origin v2.0.0

# Scenario A: fully merged (fast-forwarded into v2.0.0) -> "merged"
git checkout -q -b claude/issue-9101-merged
echo "merged-change" >> file.txt
git commit -q -am "merged change"
git push -q origin claude/issue-9101-merged
git checkout -q v2.0.0
git merge -q --ff-only claude/issue-9101-merged
git push -q origin v2.0.0

# Scenario B: superseded — diverges from v2.0.0, never merged, but the exact
# same diff content lands on v2.0.0 via a separate, differently-shaped commit.
git checkout -q -b claude/issue-9102-superseded v2.0.0
echo "supersede-content" > file2.txt
git add file2.txt
git commit -q -m "add file2 on the branch"
git push -q origin claude/issue-9102-superseded
git checkout -q v2.0.0
echo "supersede-content" > file2.txt
git add file2.txt
git commit -q -m "same content landed independently on v2.0.0"
git push -q origin v2.0.0

# Scenario C: unmerged, issue still OPEN -> must be kept regardless of diff
git checkout -q -b claude/issue-9103-keep-open v2.0.0
echo "whatever" > file3.txt
git add file3.txt
git commit -q -m "open issue branch"
git push -q origin claude/issue-9103-keep-open

# Scenario D: unmerged, issue CLOSED, but diff content never landed on
# v2.0.0 -> must be kept (closed issue alone is not sufficient)
git checkout -q -b claude/issue-9104-keep-diff v2.0.0
echo "unique-content-never-landed" > file4.txt
git add file4.txt
git commit -q -m "content that never lands elsewhere"
git push -q origin claude/issue-9104-keep-diff

git checkout -q v2.0.0

# gh stub: classify_mergeable/closed_unmerged_pr_exists only ever call
# `gh issue view <n> ... --jq .state` or `gh pr list ...`. Route both
# through canned responses instead of hitting the network. Read via
# indirect expansion (${!varname}) in gh() below, not by name in this file.
# shellcheck disable=SC2034
ISSUE_STATE_9102="CLOSED"
# shellcheck disable=SC2034
ISSUE_STATE_9103="OPEN"
# shellcheck disable=SC2034
ISSUE_STATE_9104="CLOSED"
PR_LIST_STUB="[]"

gh() {
  case "$1 $2" in
    "issue view")
      local issue="$3" varname
      varname="ISSUE_STATE_${issue}"
      echo "${!varname:-OPEN}"
      ;;
    "pr list")
      echo "${PR_LIST_STUB}"
      ;;
    *)
      echo "[test] unexpected gh invocation: $*" >&2
      return 1
      ;;
  esac
}

_assert_eq "fully merged (fast-forwarded) branch classifies as merged" \
  "merged" "$(classify_mergeable "claude/issue-9101-merged" "9101" "v2.0.0")"

_assert_eq "unmerged + closed issue + equivalent diff already on base classifies as superseded" \
  "superseded" "$(classify_mergeable "claude/issue-9102-superseded" "9102" "v2.0.0")"

_assert_eq "unmerged + issue still open is kept (empty verdict)" \
  "" "$(classify_mergeable "claude/issue-9103-keep-open" "9103" "v2.0.0")"

_assert_eq "unmerged + closed issue but diff content never landed is kept (empty verdict)" \
  "" "$(classify_mergeable "claude/issue-9104-keep-diff" "9104" "v2.0.0")"

# --- Test group 3: closed_unmerged_pr_exists reads the explicit ---
# --- abandonment signal (PR closed without merging) straight from `gh` ---
PR_LIST_STUB='[{"merged":false,"baseRefName":"v2.0.0"}]'
if closed_unmerged_pr_exists "some/branch" "v2.0.0"; then
  echo "[ok] closed_unmerged_pr_exists true when a closed, unmerged PR targets base_branch"
else
  echo "[FAIL] closed_unmerged_pr_exists should be true for a closed PR with merged=false and matching base" >&2
  FAILURES=$((FAILURES + 1))
fi

PR_LIST_STUB='[{"merged":true,"baseRefName":"v2.0.0"}]'
if closed_unmerged_pr_exists "some/branch" "v2.0.0"; then
  echo "[FAIL] closed_unmerged_pr_exists should be false when the only closed PR was merged" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] closed_unmerged_pr_exists false when the only closed PR was merged"
fi

PR_LIST_STUB='[]'
if closed_unmerged_pr_exists "some/branch" "v2.0.0"; then
  echo "[FAIL] closed_unmerged_pr_exists should be false with no closed PRs" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] closed_unmerged_pr_exists false with no closed PRs at all"
fi

# --- Test group 4: cleanup_superseded_branches — the exact regression this ---
# --- issue's review flagged as Critical. open_pr_head_branches is stubbed to
# --- return empty (simulating its `gh pr list` call failing/returning
# --- nothing), which used to be the ONLY thing preventing deletion. Any
# --- candidate that classify_mergeable/closed_unmerged_pr_exists cannot
# --- positively confirm must now survive regardless.
git checkout -q -b feat/9200-final v2.0.0
git push -q origin feat/9200-final
git checkout -q -b claude/issue-9200-old-confirmed v2.0.0
git push -q origin claude/issue-9200-old-confirmed
git checkout -q -b claude/issue-9200-old-unconfirmed v2.0.0
git push -q origin claude/issue-9200-old-unconfirmed
git checkout -q v2.0.0

# Simulate the failure mode: the open-PR protection list comes back empty.
open_pr_head_branches() { echo ""; }

closed_unmerged_pr_exists() { return 1; }

classify_mergeable() {
  case "$1" in
    claude/issue-9200-old-confirmed) echo "merged" ;;
    *) echo "" ;;
  esac
}

DELETED_BRANCHES=()
delete_remote_branch() { DELETED_BRANCHES+=("$1"); }

cleanup_superseded_branches "9200" "feat/9200-final" "v2.0.0"

_deleted_contains() {
  local target="$1" b
  for b in "${DELETED_BRANCHES[@]:-}"; do
    [[ "$b" == "$target" ]] && return 0
  done
  return 1
}

if _deleted_contains "claude/issue-9200-old-confirmed"; then
  echo "[ok] confirmed-merged sibling branch is deleted"
else
  echo "[FAIL] confirmed-merged sibling branch should have been deleted" >&2
  FAILURES=$((FAILURES + 1))
fi

if _deleted_contains "claude/issue-9200-old-unconfirmed"; then
  echo "[FAIL] unconfirmed sibling branch was deleted despite open_pr_head_branches failing open (#3392 critical regression)" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] unconfirmed sibling branch survives even when open_pr_head_branches returns empty"
fi

if _deleted_contains "feat/9200-final"; then
  echo "[FAIL] the keep_branch itself must never be deleted" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] the keep_branch itself is never touched"
fi

cd "$ROOT_DIR" || exit 1

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
