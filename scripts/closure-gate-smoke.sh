#!/usr/bin/env bash
set -euo pipefail

# Executed evidence for #3862's closure gate (ledger D-006, D-030).
#
# #3789 and #3815 were both closed as `completed` while their fixes sat on
# branches that never reached the release branch. Whatever closed them was
# trusting that a run reported success, not that the work landed --
# `gh pr merge` exiting 0 is a report, and a report is what was already there.
#
# review_accept_and_merge.sh now refuses to close an issue unless every path
# the PR head touched is readable on the base branch. That gate is only worth
# anything if it says NO in the case it was built for, so this job proves both
# halves against real merges on a real runner:
#
#   * a genuine merge commit          -> verified, the issue would be closed
#   * a SQUASH merge (every SHA new)  -> verified, so the gate is not a
#                                        disguised ancestry test
#   * a merge that silently dropped a
#     file (the #3789 shape)          -> REFUSED, the issue stays open
#
# Run locally: bash scripts/closure-gate-smoke.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="${ROOT_DIR}/scripts/agents/lib/branch_content.py"
BASE="v3.0.0"

FAILURES=0
_ok()   { echo "[ok] $*"; }
_fail() { echo "[FAIL] $*" >&2; FAILURES=$((FAILURES + 1)); }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

REPO="$SANDBOX/repo"
mkdir -p "$REPO"
cd "$REPO"
git init -q -b "$BASE"
git config user.email "closure-gate-smoke@example.invalid"
git config user.name "Closure Gate Smoke"
git config commit.gpgsign false

mk() { mkdir -p "$(dirname "$1")"; printf '%s' "$2" > "$1"; }

mk README.md 'nyxGPT
'
git add -A && git commit -q -m "chore: base"

# `origin` is this same repository, so the gate's `origin/<base>` refs resolve
# exactly as they do in the review runner's checkout.
git remote add origin "$REPO"

# The check exactly as review_accept_and_merge.sh makes it: the base REF (its
# tip now) versus the PR HEAD SHA (the branch name is gone by then --
# --delete-branch), with --since pinning the divergence point recorded by the
# PR. Any drift between this and the real call makes the evidence worthless.
verified() {
  local head_sha="$1" base_sha_at_pr_time="$2" divergence
  git fetch -q origin "$BASE"
  divergence="$(git merge-base "$base_sha_at_pr_time" "$head_sha")"
  python3 "$GUARD" --repo "$REPO" landed \
    --base "origin/${BASE}" --branch "$head_sha" --since "$divergence" >/dev/null 2>&1
}

_show() {
  local head_sha="$1" base_sha_at_pr_time="$2" divergence
  divergence="$(git merge-base "$base_sha_at_pr_time" "$head_sha")"
  python3 "$GUARD" --repo "$REPO" landed \
    --base "origin/${BASE}" --branch "$head_sha" --since "$divergence" 2>&1 | sed 's/^/       /' || true
}

echo "=== Case 1: an ordinary merge commit ======================================"
base1="$(git rev-parse HEAD)"
git checkout -q -b feat/1-ordinary
mk src/feature.py 'VALUE = 1
'
git add -A && git commit -q -m "feat: a feature (#1)"
head1="$(git rev-parse HEAD)"
git checkout -q "$BASE"
git merge -q --no-ff -m "Merge pull request #1" feat/1-ordinary
if verified "$head1" "$base1"; then
  _ok "a real merge verifies — the issue is closed on evidence, not on a report"
else
  _fail "a genuine merge was reported unverified; this would stop the pipeline closing anything"
fi

echo
echo "=== Case 2: a squash merge (every SHA is new) ============================="
# If the gate were ancestry in disguise, this is where it would break: the
# head SHA is on no branch afterwards.
base2="$(git rev-parse HEAD)"
git checkout -q -b feat/2-squashed
mk src/squashed.py 'VALUE = 2
'
git add -A && git commit -q -m "feat: part one (#2)"
mk src/squashed.py 'VALUE = 22
'
git add -A && git commit -q -m "feat: part two (#2)"
head2="$(git rev-parse HEAD)"
git checkout -q "$BASE"
git merge -q --squash feat/2-squashed
git commit -q -m "feat: squashed (#2)"
if git merge-base --is-ancestor "$head2" "$BASE" 2>/dev/null; then
  _fail "fixture drift: the squashed head is an ancestor, so this proves nothing"
else
  _ok "the squashed head is NOT an ancestor of ${BASE} — ancestry would refuse this merge"
fi
if verified "$head2" "$base2"; then
  _ok "the content check verifies the squash anyway"
else
  _fail "a squash merge was reported unverified"
fi

echo
echo "=== Case 3 (fault injection): a merge that dropped a file ================="
# The #3789 shape, reproduced: the merge lands, the run reports success, and
# one file never arrives. This is the case the gate exists for.
base3="$(git rev-parse HEAD)"
git checkout -q -b fix/3-acceptance-failure
mk src/fix.py 'FIXED = True
'
mk tests/unit/test_ops_step_isolation.py 'def test_install_step_list_is_fully_enumerated():
    pass
'
git add -A && git commit -q -m "fix: address acceptance failure (#3)"
head3="$(git rev-parse HEAD)"
git checkout -q "$BASE"
git merge -q --no-ff -m "Merge pull request #3" fix/3-acceptance-failure
git rm -q tests/unit/test_ops_step_isolation.py
git commit -q -m "the test file never actually reached the release branch"

if verified "$head3" "$base3"; then
  _fail "the gate verified a merge that dropped tests/unit/test_ops_step_isolation.py — this is exactly how #3789 closed as completed with its work stranded"
else
  _ok "REFUSED: the gate will not close an issue whose work is not on ${BASE}"
  _show "$head3" "$base3"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All closure-gate smoke checks passed."
  exit 0
fi
echo "${FAILURES} closure-gate smoke check(s) failed." >&2
exit 1
