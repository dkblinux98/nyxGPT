#!/usr/bin/env bash
set -uo pipefail

# tests/test_escalation_evidence.sh
#
# The shell half of #4176: the three defects that each turned a known cause
# into "Error type could not be determined. Manual investigation needed."
#
#   1. Final Verification recorded nothing -> `scripts/agents/run_final_verification.sh`
#      is EXECUTED here against a planted failing test, and must record the
#      gate and the failing node IDs.
#   2. `classify_error` had no signature for it -> the classifier is run over
#      the detail this module's own writer produced, so the two cannot drift.
#   3. A leftover worktree blocked the next checkout ->
#      `scripts/agents/prune_stray_worktrees.sh` is run against a real stray
#      worktree, with the failure PROVEN FIRST (the branch checkout dies) and
#      then proven fixed. A prune test that only checks the exit status would
#      pass on a script that pruned nothing (#3753's lesson, applied here).
#
# Plus the two readers the escalation headline depends on:
# `release_head_state_json` (is the base red, on which check, at which sha)
# and `first_error_after_marker` (why did Phase 3 itself fail).
#
# Usage: bash tests/test_escalation_evidence.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${NYXGPT_TEST_PYTHON:-python3}"

FAILURES=0
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "[FAIL] $desc: expected '$expected', got '$actual'" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output to contain '$needle', got: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output NOT to contain '$needle', got: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

# ---------------------------------------------------------------------------
# 1. classify_error over the detail the writer actually writes
# ---------------------------------------------------------------------------
echo "--- classify_error reads Final Verification's own reason ---"

DETAIL="$(printf 'FAILED tests/unit/test_a.py::test_x\nFAILED tests/unit/test_b.py::test_y\n' \
  | "$PYTHON" "$ROOT_DIR/scripts/agents/lib/escalation_evidence.py" verification-detail pytest 7)"

_assert_eq "a pytest gate failure classifies as verification_failed:pytest" \
  "verification_failed:pytest" "$(classify_error "$DETAIL")"

# THE ORDERING TEST. The detail names failing tests, which the older
# `FAILED.*test` signature also matches -- and `retriable:test_failure` would
# send the 3-attempt fix loop round again on a gate that runs AFTER attempt 3
# has already been spent. The specific signature must win.
_assert_not_contains "and NOT as the retriable test-failure class" \
  "$(classify_error "$DETAIL")" "retriable"

MYPY_DETAIL="$("$PYTHON" - <<'PY'
import sys
sys.path.insert(0, "scripts/agents/lib")
import escalation_evidence as ee
print(ee.format_verification_detail("mypy", None, None, ["src/nyxgpt/ops.py:9: error: bad"]))
PY
)"
_assert_eq "a mypy gate failure names the mypy gate" \
  "verification_failed:mypy" "$(classify_error "$MYPY_DETAIL")"

# No regression in the signatures that were already there (#3971's refusal
# sentence is an API -- tests/test_reviewable_head_gate.sh pins it too).
_assert_eq "the red-head refusal still routes to a developer round" \
  "retriable:ci_red" "$(classify_error "red head is not reviewable")"
_assert_eq "a bare step name is still unknown (that is the honest answer)" \
  "unknown" "$(classify_error "Submit PR for review")"

# ---------------------------------------------------------------------------
# 2. run_final_verification.sh, EXECUTED
# ---------------------------------------------------------------------------
echo "--- Final Verification records which gate failed, and which tests ---"

VERIFY_DIR="$TMP_ROOT/verify"
mkdir -p "$VERIFY_DIR"
cat > "$VERIFY_DIR/test_planted.py" <<'PY'
def test_passes():
    assert True


def test_fails_on_purpose():
    assert 1 == 2, "planted failure (#4176)"


def test_also_fails_on_purpose():
    raise AssertionError("second planted failure (#4176)")
PY

export NYXGPT_AGENT_ERROR_FILE="$TMP_ROOT/agent-error.txt"
: > "$NYXGPT_AGENT_ERROR_FILE"

# This section EXECUTES the gate, so it needs an interpreter that has pytest.
# `$NYXGPT_TEST_PYTHON` is set by tests/unit/test_escalation_headline.py (to
# the interpreter pytest is itself running under) and by the smoke workflow.
# Where there is none, the section is announced as skipped rather than
# silently passing -- and the smoke job greps for one of its [ok] lines, so a
# skip there is a job failure rather than a quiet gap.
if ! "$PYTHON" -c "import pytest" >/dev/null 2>&1; then
  echo "[SKIP] ${PYTHON} has no pytest -- the executed Final Verification checks are skipped."
  echo "[SKIP] Run with NYXGPT_TEST_PYTHON=<interpreter with pytest> to execute them."
  VERIFY_SKIPPED=1
else
  VERIFY_SKIPPED=0
fi

if [[ "$VERIFY_SKIPPED" -eq 0 ]]; then

VERIFY_OUT="$(
  cd "$VERIFY_DIR" &&
  NYXGPT_VERIFY_GATES="pytest" \
  NYXGPT_VERIFY_PYTEST_TARGET="test_planted.py" \
  NYXGPT_VERIFY_PYTEST_ARGS="-p no:cacheprovider --noconftest" \
  NYXGPT_VERIFY_LOG_DIR="$TMP_ROOT/logs" \
  NYXGPT_VERIFY_PYTHON="$PYTHON" \
    bash "$ROOT_DIR/scripts/agents/run_final_verification.sh" 2>&1
)" && VERIFY_RC=0 || VERIFY_RC=$?

_assert_eq "the gate still FAILS -- reporting better does not make it pass" "1" "$VERIFY_RC"
_assert_contains "and the run log names the gate as well" "$VERIFY_OUT" \
  "::error::Final Verification failed at the pytest gate."

RECORDED="$(read_agent_error_detail)"
_assert_contains "it records the gate that failed" "$RECORDED" "Final Verification failed: gate=pytest"
_assert_contains "it records the first failing node ID" "$RECORDED" "test_planted.py::test_fails_on_purpose"
_assert_contains "it records the second failing node ID" "$RECORDED" "test_planted.py::test_also_fails_on_purpose"
_assert_contains "it records how many failed" "$RECORDED" "2 failing test(s)"
_assert_not_contains "it does not record the tests that passed" "$RECORDED" "test_passes"
_assert_eq "and Phase 1 therefore classifies specifically, not 'unknown'" \
  "verification_failed:pytest" "$(classify_error "$RECORDED")"

# The other half: a clean tree records nothing and exits 0. Without this, a
# script that recorded a failure unconditionally would pass every assertion
# above (#3753: prove both directions).
cat > "$VERIFY_DIR/test_clean.py" <<'PY'
def test_passes():
    assert True
PY
: > "$NYXGPT_AGENT_ERROR_FILE"
if cd "$VERIFY_DIR" &&
   NYXGPT_VERIFY_GATES="pytest" \
   NYXGPT_VERIFY_PYTEST_TARGET="test_clean.py" \
   NYXGPT_VERIFY_PYTEST_ARGS="-p no:cacheprovider --noconftest" \
   NYXGPT_VERIFY_LOG_DIR="$TMP_ROOT/logs" \
   NYXGPT_VERIFY_PYTHON="$PYTHON" \
     bash "$ROOT_DIR/scripts/agents/run_final_verification.sh" >/dev/null 2>&1; then
  echo "[ok] a passing gate exits 0"
else
  echo "[FAIL] a passing gate should exit 0" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "and records nothing" "" "$(read_agent_error_detail)"
cd "$ROOT_DIR" || exit 1  # the `if cd ...` above moved this shell
fi  # VERIFY_SKIPPED

# ---------------------------------------------------------------------------
# 3. prune_stray_worktrees.sh, with the failure proven first
# ---------------------------------------------------------------------------
echo "--- a stray worktree cannot block the next checkout ---"

REPO="$TMP_ROOT/repo"
mkdir -p "$REPO"
(
  cd "$REPO" || exit 1
  git init --quiet --initial-branch=main .
  git config user.email ci@example.com
  git config user.name CI
  echo one > file.txt
  git add file.txt
  git commit --quiet -m "one"
  git branch v9.9.9
) || { echo "[FAIL] could not build the fixture repo" >&2; FAILURES=$((FAILURES + 1)); }

# The trap, set exactly as #4166 set it: a worktree created on the BRANCH NAME
# (no --detach), which holds it.
git -C "$REPO" worktree add "$TMP_ROOT/base-wt" v9.9.9 >/dev/null 2>&1

# FAULT INJECTION: prove the failure exists before proving the fix works.
if CHECKOUT_ERR="$(git -C "$REPO" checkout v9.9.9 2>&1)"; then
  echo "[FAIL] the fixture is wrong: checking out a worktree-held branch should fail" >&2
  FAILURES=$((FAILURES + 1))
else
  _assert_contains "a worktree-held branch cannot be checked out (the #4166 fatal)" \
    "$CHECKOUT_ERR" "is already used by worktree"
fi

PRUNE_OUT="$(cd "$REPO" && bash "$ROOT_DIR/scripts/agents/prune_stray_worktrees.sh" "$REPO" 2>&1)"
_assert_contains "the prune names the stray worktree it removed" "$PRUNE_OUT" "base-wt"

if git -C "$REPO" checkout v9.9.9 >/dev/null 2>&1; then
  echo "[ok] after the prune the branch checks out -- Phase 3 can run"
  git -C "$REPO" checkout main >/dev/null 2>&1
else
  echo "[FAIL] the branch is still held after the prune" >&2
  FAILURES=$((FAILURES + 1))
fi

# The main worktree is never removed: `git worktree remove` refuses it, and
# removing the workspace out from under the job would be a far worse failure
# than the one being fixed.
_assert_eq "the repository's own worktree survives" "true" \
  "$([[ -f "$REPO/file.txt" ]] && echo true || echo false)"

# A worktree whose DIRECTORY was deleted by hand still holds its branch, via
# an administrative record nothing in the filesystem shows. `git worktree
# prune` is what frees it, which is why the script always prunes.
git -C "$REPO" worktree add "$TMP_ROOT/ghost-wt" v9.9.9 >/dev/null 2>&1
rm -rf "$TMP_ROOT/ghost-wt"
if git -C "$REPO" checkout v9.9.9 >/dev/null 2>&1; then
  echo "[FAIL] the fixture is wrong: a deleted worktree's record should still hold the branch" >&2
  FAILURES=$((FAILURES + 1))
  git -C "$REPO" checkout main >/dev/null 2>&1
else
  echo "[ok] a deleted worktree's record still holds the branch (invisible in the filesystem)"
fi
(cd "$REPO" && bash "$ROOT_DIR/scripts/agents/prune_stray_worktrees.sh" "$REPO" >/dev/null 2>&1)
if git -C "$REPO" checkout v9.9.9 >/dev/null 2>&1; then
  echo "[ok] the prune frees a branch held by a stale worktree record"
  git -C "$REPO" checkout main >/dev/null 2>&1
else
  echo "[FAIL] the stale worktree record still holds the branch after the prune" >&2
  FAILURES=$((FAILURES + 1))
fi

# A detached worktree -- what the prompts now tell the agent to use -- holds no
# branch at all. This is the assertion behind that guidance.
git -C "$REPO" worktree add --detach "$TMP_ROOT/detached-wt" v9.9.9 >/dev/null 2>&1
if git -C "$REPO" checkout v9.9.9 >/dev/null 2>&1; then
  echo "[ok] a --detach worktree does not hold the branch name"
  git -C "$REPO" checkout main >/dev/null 2>&1
else
  echo "[FAIL] a --detach worktree should not hold the branch name" >&2
  FAILURES=$((FAILURES + 1))
fi
(cd "$REPO" && bash "$ROOT_DIR/scripts/agents/prune_stray_worktrees.sh" "$REPO" >/dev/null 2>&1)

# And it is never fatal: a run in a directory that is not a repository at all
# must not take the job down with it.
if (cd "$TMP_ROOT" && bash "$ROOT_DIR/scripts/agents/prune_stray_worktrees.sh" "$TMP_ROOT" >/dev/null 2>&1); then
  echo "[ok] outside a git repository the prune exits 0 rather than failing the job"
else
  echo "[FAIL] the prune must never fail the job" >&2
  FAILURES=$((FAILURES + 1))
fi

# ---------------------------------------------------------------------------
# 4. release_head_state_json -- one definition of "is the base red"
# ---------------------------------------------------------------------------
echo "--- the base-red finding the headline and the blast radius share ---"

# Read by the sourced library, not by this file (hence the disables).
# shellcheck disable=SC2034
REPO_OWNER="test-owner"
# shellcheck disable=SC2034
REPO_NAME="test-repo"
# shellcheck disable=SC2034
RELEASE_BRANCH="v9.9.9"

gh() {
  if [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/commits/v9.9.9" ]]; then
    echo "deadbeefcafe1234"
    return 0
  elif [[ "$1" == "api" && "$2" == "repos/test-owner/test-repo/commits/v9.9.9/check-runs" ]]; then
    printf '%s\n' "${STUB_FAILING_CHECKS:-}"
    return "${STUB_CHECKS_RC:-0}"
  fi
  echo "[test] unexpected gh invocation: $*" >&2
  return 1
}

STUB_FAILING_CHECKS="test" STUB_CHECKS_RC=0
STATE="$(release_head_state_json)"
_assert_eq "a red head is reported red" "true" "$(jq -r '.release_head_red' <<<"$STATE")"
_assert_eq "and names the failing check" "test" "$(jq -r '.red_checks[0]' <<<"$STATE")"
_assert_eq "and the head sha the escalation quotes" "deadbeefcafe1234" \
  "$(jq -r '.release_head_sha' <<<"$STATE")"
_assert_eq "and the branch it is about" "v9.9.9" "$(jq -r '.release_branch' <<<"$STATE")"

STUB_FAILING_CHECKS="" STUB_CHECKS_RC=0
STATE="$(release_head_state_json)"
_assert_eq "a green head is reported green, not unknown" "false" \
  "$(jq -r '.release_head_red' <<<"$STATE")"

# "Could not read the checks" is a THIRD answer. Rendering it as "no" is how
# an unreadable head gets reported as green, and the blast-radius report's
# whole design (#4134) is that an unanswerable question says so.
STUB_FAILING_CHECKS="" STUB_CHECKS_RC=1
STATE="$(release_head_state_json)"
_assert_eq "an unreadable head leaves the key absent -> 'not checked'" "null" \
  "$(jq -r '.release_head_red' <<<"$STATE")"

# And the headline must not call that red either -- the composition reads the
# same JSON the report does.
HEADLINE="$(jq -n -c --argjson h "$STATE" \
  '{error_class: "unknown", base_branch: $h.release_branch, base_red: $h.release_head_red}' \
  | "$PYTHON" "$ROOT_DIR/scripts/agents/lib/escalation_evidence.py" headline)"
_assert_not_contains "an unreadable base is never called red in the headline" \
  "$HEADLINE" "ALREADY RED"

# ---------------------------------------------------------------------------
# 5. first_error_after_marker -- why Phase 3 itself failed
# ---------------------------------------------------------------------------
echo "--- a Phase 3 that crashed can say why ---"

JOB_LOG="$(cat <<'LOG'
2026-10-08T01:00:00.0000000Z ##[group]Run anthropics/claude-code-action@c81e3bc
2026-10-08T01:00:01.0000000Z Error: the FIRST invocation had an unrelated hiccup
2026-10-08T01:40:00.0000000Z ##[group]Run ./scripts/agents/run_final_verification.sh
2026-10-08T01:40:01.0000000Z ::error::Final Verification failed at the pytest gate.
2026-10-08T01:49:52.0000000Z ##[group]Run anthropics/claude-code-action@c81e3bc
2026-10-08T01:49:55.0000000Z fatal: 'v3.0.1' is already used by worktree at '/tmp/base-wt'
2026-10-08T01:49:56.0000000Z Error: a later line that is not the first one
LOG
)"

LINE="$(first_error_after_marker "$JOB_LOG" "claude-code-action")"
_assert_eq "it reports the first error after the LAST invocation, not the first" \
  "fatal: 'v9.9.9' is already used by worktree at '/tmp/base-wt'" \
  "${LINE//v3.0.1/v9.9.9}"
_assert_not_contains "so an earlier invocation's hiccup is never reported as Phase 3's" \
  "$LINE" "unrelated hiccup"
_assert_eq "a log with no marker yields nothing (rather than a wrong guess)" "" \
  "$(first_error_after_marker "$JOB_LOG" "no-such-step")"
_assert_eq "an empty log yields nothing" "" "$(first_error_after_marker "" "claude-code-action")"

# And that line becomes the sentence the owner reads.
SENTENCE="$(jq -n -c --arg e "$LINE" '{error_class: "unknown", phase3_outcome: "failure", phase3_error: $e}' \
  | "$PYTHON" "$ROOT_DIR/scripts/agents/lib/escalation_evidence.py" headline)"
_assert_contains "the headline states that Phase 3 did not run" "$SENTENCE" \
  "Phase 3 diagnosis did not run"
_assert_contains "and quotes the step's own error" "$SENTENCE" "already used by worktree"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
