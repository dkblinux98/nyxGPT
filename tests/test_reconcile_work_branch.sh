#!/usr/bin/env bash
set -uo pipefail

# tests/test_reconcile_work_branch.sh
# Executed evidence (ledger D-006) for #4038's branch handoff:
# scripts/agents/reconcile_work_branch.sh.
#
# THE QUESTION THIS ANSWERS BY RUNNING. Reproduce the exact shape of run #4033
# -- attempt 1 pushes its work to `feat/4033-...`, verification fails, attempt 2
# starts on a fresh `claude/issue-4033-<timestamp>` branch cut from the release
# branch -- and ask: does the run end with ONE branch carrying BOTH attempts, or
# with two divergent branches and a rescue draft PR?
#
# Nothing here touches a real branch, a real remote or the network: each case
# plants a bare `origin` and a clone in a temp directory and runs the real
# script against them.
#
# The cases, and why each exists:
#
#   1  THE #4033 SHAPE. Attempt 1's commit is on the work branch and on origin;
#      attempt 2 commits somewhere else entirely. Both commits must survive, on
#      one branch, with the workspace standing on it -- because the next step in
#      the workflow is a verification whose verdict is attributed to that work.
#
#   1b FAULT INJECTION (#3753's template). The retired form -- the force-push
#      the review path used at developer_auto_implement.yml:864 until this
#      change -- is run against the IDENTICAL fixture and shown DESTROYING
#      attempt 1's commit. A guard that cannot fail proves nothing, and the two
#      halves together are what show the merge is load-bearing rather than
#      incidental. This is also why the review path's force-push had to go: it
#      is the same operation on the same shape.
#
#   2  THE SAFETY THE ISSUE REQUIRES PRESERVED. A fresh action branch with no
#      commits beyond the release branch must never overwrite a branch holding
#      the only copy of the work. It must also not leave the workspace standing
#      on that cut of the release branch, or the next verification reports a
#      verdict about the release branch as if it were the PR's (#3979).
#
#   3  FAST-FORWARD. When the stray genuinely descends from the work branch,
#      reconciling must not manufacture a merge commit, and the stray is
#      deleted -- but only on the blob-level proof (D-031).
#
#   4  ALREADY THERE. The agent obeyed the prompt and worked on the work branch:
#      the reconciliation must be a no-op, not a round trip through origin.
#
#   5  NOTHING REACHED ORIGIN. The work branch was never pushed, so the stray IS
#      the work; publish it under the work branch's name rather than reporting
#      nothing to do.
#
#   6  CONFLICT. Neither side may be chosen here -- that is a developer decision
#      (D-011). Both branches stay intact, the target keeps its own commits, the
#      stray reaches origin so the rescue backstop can see it, and the script
#      still exits 0 (it runs inside the job it must not red).
#
#   7  UNCOMMITTED WORK. The retry left changes unstaged. Moving the workspace
#      is this script's doing, so losing them would be this script's fault:
#      they are committed to the stray first and carried across.
#
# Usage: bash tests/test_reconcile_work_branch.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECONCILE="$ROOT_DIR/scripts/agents/reconcile_work_branch.sh"

RELEASE="v-test"
WORK="feat/4038-continue-the-work"
STRAY="claude/issue-4038-20260824-0213"

FAILURES=0

_ok()   { echo "[ok] $1"; }
_fail() { echo "[FAIL] $1" >&2; FAILURES=$((FAILURES + 1)); }

_assert_eq() {
  local desc="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then _ok "$desc"
  else _fail "$desc: expected '$expected', got '$actual'"; fi
}

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then _ok "$desc"
  else _fail "$desc: '$needle' not found in:"$'\n'"$haystack"; fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then _ok "$desc"
  else _fail "$desc: '$needle' unexpectedly present in:"$'\n'"$haystack"; fi
}

# --------------------------------------------------------------------------
# Fixture. `origin` is a real bare repository; `work` is a real clone standing
# exactly where a retry step's claude-code-action invocation leaves it.
#
#   origin/$RELEASE          base.txt
#   origin/$WORK             base.txt + implementation.py   <- attempt 1, pushed
#   workspace on $STRAY      cut from origin/$RELEASE       <- what attempt 2 sees
# --------------------------------------------------------------------------
_plant() {
  local dir="$1"
  rm -rf "$dir"; mkdir -p "$dir"

  git init -q --bare "$dir/origin.git"

  git init -q -b "$RELEASE" "$dir/work"
  (
    cd "$dir/work"
    git config user.email "dev@example.test"
    git config user.name "Developer Agent"
    git config commit.gpgsign false
    echo "base" > base.txt
    git add base.txt
    git commit -q -m "base"
    git remote add origin "$dir/origin.git"
    git push -q -u origin "$RELEASE"

    # Attempt 1: implement on the work branch and push it, exactly as
    # developer_create_branch.sh + the snapshot step do.
    git checkout -q -b "$WORK"
    echo "def implementation(): return 1" > implementation.py
    git add implementation.py
    git commit -q -m "feat: implement the issue (#4038)"
    git push -q -u origin "$WORK"

    # Verification fails. Attempt 2 is a NEW claude-code-action invocation, so
    # the action mints and checks out its own branch, cut from the release
    # branch. This single line is the whole defect: the tree in front of
    # attempt 2 has no implementation.py in it.
    git checkout -q -b "$STRAY" "origin/$RELEASE"
  )
}

# The commit attempt 2 makes, wherever it happens to be standing. It PUSHES by
# default because that is what really happens -- the retry prompt tells the
# agent to commit and the action's git-push.sh publishes the branch -- and it is
# what makes the delete guard's assertions non-vacuous: a stray that never
# reached origin cannot be an orphan there whether or not the guard works.
_attempt2_commits() {
  local dir="$1" file="${2:-fix.py}" push="${3:-push}"
  (
    cd "$dir/work"
    echo "# attempt 2 fix" > "$file"
    git add "$file"
    git commit -q -m "fix: address verification failures (attempt 2) (#4038)"
    [[ "$push" == "push" ]] && git push -q -u origin HEAD
  )
}

_origin_files() {
  git -C "$1/origin.git" ls-tree -r --name-only "refs/heads/$2" 2>/dev/null | sort | tr '\n' ' '
}
_origin_has_branch() {
  git -C "$1/origin.git" rev-parse --verify --quiet "refs/heads/$2" >/dev/null 2>&1
}

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# ==========================================================================
# Case 1 -- the #4033 shape: divergent branches must converge, losing nothing.
# ==========================================================================
echo "=== Case 1: attempt 2 on a fresh action branch continues, not restarts"
D="$TMP_ROOT/case1"
_plant "$D"
_attempt2_commits "$D"
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"
RC=$?

_assert_eq "case 1: exits 0 (it runs inside the job it must not red)" "$RC" "0"
_assert_contains "case 1: it noticed the workspace was not on the work branch" "$OUT" "not the work branch"

FILES="$(_origin_files "$D" "$WORK")"
_assert_contains "case 1: attempt 1's implementation survived on origin/$WORK" "$FILES" "implementation.py"
_assert_contains "case 1: attempt 2's fix reached origin/$WORK" "$FILES" "fix.py"

_assert_eq "case 1: the workspace ends on the work branch, so verification judges it" \
  "$(cd "$D/work" && git branch --show-current)" "$WORK"
_assert_eq "case 1: the work branch's tip contains attempt 2's commit" \
  "$(cd "$D/work" && git log --oneline origin/$WORK | grep -c 'attempt 2')" "1"

if _origin_has_branch "$D" "$STRAY"; then
  _fail "case 1: the stray branch is still on origin -- the run would end with an orphan and a rescue draft PR"
else
  _ok "case 1: no orphan branch left on origin (one branch, one PR)"
fi

# ==========================================================================
# Case 1b -- fault injection: the RETIRED force-push form, same fixture.
#
# This is developer_auto_implement.yml:864 as it stood before #4038, reduced to
# the two lines that decide the outcome. It is reproduced here rather than
# called, because the point is that this code no longer exists anywhere: the
# test is what keeps the reason for its removal executable.
# ==========================================================================
echo "=== Case 1b (fault injection): the retired force-push destroys attempt 1"
D="$TMP_ROOT/case1b"
_plant "$D"
_attempt2_commits "$D"
(
  cd "$D/work"
  git fetch -q origin --prune
  CURRENT="$(git branch --show-current)"
  # The retired safety check passes: the stray DOES have a commit of its own.
  if [ -n "$(git rev-list "origin/$RELEASE..HEAD" 2>/dev/null)" ]; then
    git push -q --force origin "HEAD:refs/heads/$WORK"
  fi
  echo "$CURRENT" > /dev/null
) >/dev/null 2>&1

FILES="$(_origin_files "$D" "$WORK")"
_assert_not_contains "case 1b: the retired form ERASED attempt 1's implementation" "$FILES" "implementation.py"
_assert_contains "case 1b: ...while keeping only attempt 2's fix" "$FILES" "fix.py"
echo "     ^ the same fixture case 1 passes on. The merge is what makes the difference."

# ==========================================================================
# Case 2 -- the force-push safety the issue requires preserved.
# ==========================================================================
echo "=== Case 2: an empty action branch never overwrites the work branch"
D="$TMP_ROOT/case2"
_plant "$D"   # no _attempt2_commits: the retry died before committing anything
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"

FILES="$(_origin_files "$D" "$WORK")"
_assert_contains "case 2: the only copy of the work is untouched" "$FILES" "implementation.py"
_assert_contains "case 2: it said why it carried nothing forward" "$OUT" "no commits beyond"
_assert_eq "case 2: the workspace is repositioned onto the work branch (not left on a cut of $RELEASE)" \
  "$(cd "$D/work" && git branch --show-current)" "$WORK"
_assert_eq "case 2: and that tree is the work's, so verification cannot judge $RELEASE by mistake" \
  "$(cd "$D/work" && test -f implementation.py && echo present)" "present"

# ==========================================================================
# Case 3 -- fast-forward, and the blob-level delete guard.
# ==========================================================================
echo "=== Case 3: a stray that descends from the work branch fast-forwards"
D="$TMP_ROOT/case3"
_plant "$D"
(
  cd "$D/work"
  # The agent checked the work branch out, then something moved it onto a
  # differently-named branch that still contains attempt 1's commit.
  git checkout -q -B "$STRAY" "origin/$WORK"
)
_attempt2_commits "$D"
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"

_assert_eq "case 3: no merge commit was manufactured" \
  "$(cd "$D/work" && git rev-list --count --merges "origin/$WORK")" "0"
FILES="$(_origin_files "$D" "$WORK")"
_assert_contains "case 3: attempt 1 still there" "$FILES" "implementation.py"
_assert_contains "case 3: attempt 2 landed" "$FILES" "fix.py"
if _origin_has_branch "$D" "$STRAY"; then
  _fail "case 3: the superseded stray was not cleaned up"
else
  _ok "case 3: the stray was deleted, on the blob-level proof (D-031)"
fi

# ==========================================================================
# Case 4 -- the agent obeyed the prompt.
# ==========================================================================
echo "=== Case 4: already on the work branch is a no-op"
D="$TMP_ROOT/case4"
_plant "$D"
( cd "$D/work" && git checkout -q -B "$WORK" "origin/$WORK" )
_attempt2_commits "$D"
BEFORE="$(cd "$D/work" && git rev-parse HEAD)"
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"
_assert_contains "case 4: it said there was nothing to reconcile" "$OUT" "nothing to reconcile"
_assert_eq "case 4: the local tip is untouched" "$(cd "$D/work" && git rev-parse HEAD)" "$BEFORE"

# ==========================================================================
# Case 5 -- the work branch never reached origin.
# ==========================================================================
echo "=== Case 5: nothing on origin -- publish the stray under the work branch's name"
D="$TMP_ROOT/case5"
_plant "$D"
git -C "$D/origin.git" update-ref -d "refs/heads/$WORK"
( cd "$D/work" && git fetch -q origin --prune )
_attempt2_commits "$D" "fix.py" "no-push"
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"

if _origin_has_branch "$D" "$WORK"; then
  _ok "case 5: the work branch now exists on origin"
else
  _fail "case 5: the work never reached origin -- it would be lost with the runner"
fi
_assert_contains "case 5: carrying attempt 2's fix" "$(_origin_files "$D" "$WORK")" "fix.py"
_assert_eq "case 5: and the workspace stands on it" \
  "$(cd "$D/work" && git branch --show-current)" "$WORK"

# ==========================================================================
# Case 6 -- conflict: choose neither side, destroy neither branch.
# ==========================================================================
echo "=== Case 6: a conflicting stray leaves both branches intact"
D="$TMP_ROOT/case6"
_plant "$D"
_attempt2_commits "$D" "implementation.py"   # same path, different content
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"
RC=$?

_assert_eq "case 6: still exits 0" "$RC" "0"
_assert_contains "case 6: it said the branches are still apart" "$OUT" "conflict"
_assert_contains "case 6: origin/$WORK kept attempt 1's version" \
  "$(git -C "$D/origin.git" show "refs/heads/$WORK:implementation.py")" "def implementation"
if _origin_has_branch "$D" "$STRAY"; then
  _ok "case 6: the stray reached origin, where the rescue backstop can find it"
else
  _fail "case 6: the stray's commits exist nowhere but the runner"
fi

# ==========================================================================
# Case 7 -- uncommitted retry work is carried, not dropped.
# ==========================================================================
echo "=== Case 7: uncommitted work survives the branch switch"
D="$TMP_ROOT/case7"
_plant "$D"
mkdir -p "$D/work/src"
echo "# uncommitted attempt 2 fix" > "$D/work/src/late_fix.py"
OUT="$( cd "$D/work" && "$RECONCILE" --target "$WORK" --release "$RELEASE" --label "attempt 2" 2>&1 )"

_assert_contains "case 7: it snapshot-committed rather than switching over the top of it" \
  "$OUT" "Snapshot-committed"
_assert_contains "case 7: the late fix reached origin/$WORK" \
  "$(_origin_files "$D" "$WORK")" "src/late_fix.py"
_assert_contains "case 7: alongside attempt 1's work" \
  "$(_origin_files "$D" "$WORK")" "implementation.py"

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All reconcile_work_branch.sh cases passed."
