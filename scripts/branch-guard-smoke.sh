#!/usr/bin/env bash
set -euo pipefail

# Executed evidence for #3862's branch-deletion guard (ledger D-006, D-031).
#
# The question this answers by RUNNING, not by reading: given three branches
# that are indistinguishable from the outside -- same author, no PR, closed
# issue, weeks old -- does the cleanup delete exactly the one that is
# redundant, and refuse the two that hold the only copy of their work?
#
# It plants the real 2026-08-18 shapes in a scratch repository with a real
# bare `origin`, and it is deliberately a FAULT-INJECTION job (#3753's
# template): each case first shows what the cheap signal says, and only then
# what the guard says. Without that half, a job that merely runs the guard
# passes on a machine where the bug cannot reproduce, and the whole point of
# this issue is that the intuitive signals invert the truth on this data.
#
# Run locally: bash scripts/branch-guard-smoke.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="${ROOT_DIR}/scripts/agents/lib/branch_content.py"
BASE="v3.0.0"

FAILURES=0
_ok()   { echo "[ok] $*"; }
_fail() { echo "[FAIL] $*" >&2; FAILURES=$((FAILURES + 1)); }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

WORK="$SANDBOX/work"
git init -q --bare "$SANDBOX/origin.git"
git clone -q "$SANDBOX/origin.git" "$WORK" 2>/dev/null
cd "$WORK"
git config user.email "branch-guard-smoke@example.invalid"
git config user.name "Branch Guard Smoke"
git config commit.gpgsign false

mk() { mkdir -p "$(dirname "$1")"; printf '%s' "$2" > "$1"; }

# ---- the merge base every branch diverges from -------------------------------
mk README.md 'nyxGPT
'
mk agents/LEDGER.md '# Ledger
- **D-001** old decision
'
git add -A && git commit -q -m "chore: base"
git branch -M "$BASE"
git push -q -u origin "$BASE"

# ---- branch A: stranded (the #3789 shape) ------------------------------------
# 320 lines of pure test-isolation hardening, referenced by nothing else.
git checkout -q -b fix/3789-dev-install-mode "$BASE"
mk tests/unit/test_ops_step_isolation.py 'def test_install_step_list_is_fully_enumerated():
    pass
'
git add -A && git commit -q -m "test: install step isolation (#3789)"
git push -q origin fix/3789-dev-install-mode

# ---- branch B: rebased-but-landed, conflicting ledger (the #3836 shape) ------
# Every byte reached the release branch via a different branch (PR #3852), so
# this one keeps commits that are not on the base forever, and merging it
# genuinely conflicts on agents/LEDGER.md -- worth zero content.
git checkout -q -b fix/3836-create-issue-blocks "$BASE"
mk scripts/agents/create_issue.sh '#!/usr/bin/env bash
# --blocks writes the native edge
'
git add -A && git commit -q -m "fix: --blocks writes the native blocked-by edge (#3836)"
mk tests/test_create_issue_blocks.sh '#!/usr/bin/env bash
assert_native_only
'
git add -A && git commit -q -m "test: CI job proving native relationships only (#3836)"
mk agents/LEDGER.md '# Ledger
- **D-001** old decision
- **D-030** --blocks alignment
'
git add -A && git commit -q -m "docs: record the D-002 alignment (#3836)"
git push -q origin fix/3836-create-issue-blocks

git checkout -q "$BASE"
mk scripts/agents/create_issue.sh '#!/usr/bin/env bash
# --blocks writes the native edge
'
mk tests/test_create_issue_blocks.sh '#!/usr/bin/env bash
assert_native_only
'
mk agents/LEDGER.md '# Ledger
- **D-001** old decision
- **D-030** --blocks alignment
'
git add -A && git commit -q -m "Merge pull request #3852 from claude/issue-3836"
mk agents/LEDGER.md '# Ledger
- **D-001** old decision
- **D-030** --blocks alignment
- **D-031** a later, unrelated decision
'
git add -A && git commit -q -m "docs: the base moves ahead on the ledger"
git push -q origin "$BASE"

# ---- branch C: an ordinary merged branch -------------------------------------
git checkout -q -b feat/9000-ordinary "$BASE"
mk docs/thing.md 'thing
'
git add -A && git commit -q -m "docs: thing (#9000)"
git checkout -q "$BASE"
git merge -q --ff-only feat/9000-ordinary
git push -q origin "$BASE" && git push -q origin feat/9000-ordinary

echo
echo "=== Fault injection: what the cheap signals say ============================"
# This half must FAIL to distinguish the branches. If it ever starts
# distinguishing them, the fixture has drifted and the evidence below is
# no longer evidence.
stranded_ahead="$(git rev-list --count "${BASE}..fix/3789-dev-install-mode")"
landed_ahead="$(git rev-list --count "${BASE}..fix/3836-create-issue-blocks")"
echo "commits not on ${BASE}: stranded=${stranded_ahead}  fully-landed=${landed_ahead}"

if [[ "$landed_ahead" -gt "$stranded_ahead" ]]; then
  _ok "commit count ranks the FULLY-LANDED branch as the most unmerged (${landed_ahead} > ${stranded_ahead}) — the signal is inverted, as filed"
else
  _fail "fixture drift: commit count no longer inverts the truth, so this job proves nothing"
fi

if git merge-base --is-ancestor fix/3836-create-issue-blocks "$BASE" 2>/dev/null; then
  _fail "fixture drift: the rebased branch became an ancestor"
else
  _ok "ancestry calls the fully-landed branch unmerged — deleting on ancestry keeps the wrong branch"
fi

# A real merge conflict, worth zero content: proof that "it conflicts" is not
# evidence of divergence either.
git checkout -q -b smoke/conflict-probe "$BASE"
if git merge -q --no-commit --no-ff fix/3836-create-issue-blocks >/dev/null 2>&1; then
  echo "note: this fixture merges cleanly; the conflict case is covered in tests/unit/test_branch_content.py"
  git merge --abort >/dev/null 2>&1 || true
else
  _ok "merging the fully-landed branch CONFLICTS — mergeability is not evidence either"
  git merge --abort >/dev/null 2>&1 || true
fi
git checkout -q "$BASE"
git branch -qD smoke/conflict-probe

echo
echo "=== The guard, executed ===================================================="

check() {
  local branch="$1" expect="$2" desc="$3" rc=0
  python3 "$GUARD" --repo "$WORK" landed --base "origin/${BASE}" --branch "origin/${branch}" \
    >/dev/null 2>"$SANDBOX/err.txt" || rc=$?
  if [[ "$expect" == "landed" && "$rc" == "0" ]]; then
    _ok "$desc"
  elif [[ "$expect" == "stranded" && "$rc" == "1" ]]; then
    _ok "$desc"
    sed 's/^/       /' "$SANDBOX/err.txt"
  else
    _fail "$desc (expected ${expect}, guard exit ${rc})"
    sed 's/^/       /' "$SANDBOX/err.txt" >&2
  fi
}

git fetch -q origin

# THE load-bearing assertion of this issue: the branch holding the only copy of
# its work is refused, and the refusal names the file.
check fix/3789-dev-install-mode stranded \
  "REFUSES the branch carrying a file absent from ${BASE} (438 lines survive)"
# Captured, not piped: the guard exits non-zero here by design, and under
# `pipefail` that exit status would masquerade as the grep's.
refusal="$(python3 "$GUARD" --repo "$WORK" landed --base "origin/${BASE}" \
  --branch origin/fix/3789-dev-install-mode 2>&1 || true)"
if [[ "$refusal" == *"test_ops_step_isolation.py"* ]]; then
  _ok "the refusal names the file that would have been destroyed"
else
  _fail "the refusal must name the file that caused it; got: ${refusal}"
fi

check fix/3836-create-issue-blocks landed \
  "DELETES the rebased-but-landed branch despite 3 unmerged commits and a differing ledger"
check feat/9000-ordinary landed "DELETES an ordinary merged branch"

# Fail-closed: an unusable check must never authorise a deletion.
if python3 "$GUARD" --repo "$WORK" landed --base "origin/${BASE}" \
     --branch "origin/no-such-branch" >/dev/null 2>&1; then
  _fail "a branch that cannot be resolved was reported as landed"
else
  _ok "an unresolvable ref fails closed (keep, never delete)"
fi

echo
echo "=== The shell gate agent scripts actually call ============================="
cd "$ROOT_DIR"
if bash tests/test_branch_hygiene.sh >"$SANDBOX/hygiene.log" 2>&1; then
  _ok "classify_mergeable / cleanup_superseded_branches route through the guard"
else
  _fail "tests/test_branch_hygiene.sh failed"
  cat "$SANDBOX/hygiene.log" >&2
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All branch-guard smoke checks passed."
  exit 0
fi
echo "${FAILURES} branch-guard smoke check(s) failed." >&2
exit 1
