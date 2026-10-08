#!/usr/bin/env bash
# Executed proof that the #4167 guard is not vacuous.
#
# WHICH QUESTION THIS ANSWERS. `tests/unit/test_pull_request_target_safety.py`
# is the only thing standing between `ensure_project_hygiene.yml`'s
# `pull_request_target` trigger and arbitrary PR-author code running with a
# `workflow`-scoped PAT. A guard like that passes on the day it is written no
# matter what it asserts -- the tree is already correct -- so a green run of
# it proves nothing about whether it would catch the edit it exists to catch.
# That is the #3753 green-by-luck shape, and the fix is the same one
# `macos-brew-smoke.yml` uses: inject the condition, prove both halves.
#
# So this script measures the guard against deliberately broken copies of the
# workflow, one per way the safety property can be lost:
#
#   1. the checkout `ref:` moved to the PR head commit
#   2. the checkout `ref:` moved to the PR head branch (`github.head_ref`)
#   3. a `gh pr checkout` inside a step body
#   4. a dependency install, which executes the PR's own tree even when the
#      checkout is correct
#   5. the trigger reverted to `pull_request`, i.e. the Dependabot defect
#      itself coming back
#
# Each must make the guard RED. The shipped tree must make it GREEN. A guard
# that passes case 1 through 5 is asserting nothing and this script says so.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF="$ROOT_DIR/.github/workflows/ensure_project_hygiene.yml"
GUARD="tests/unit/test_pull_request_target_safety.py"

PRISTINE="$(mktemp)"
cp "$WF" "$PRISTINE"
restore() { cp "$PRISTINE" "$WF"; rm -f "$PRISTINE"; }
trap restore EXIT

FAILURES=0

# `--noconftest` on purpose: this guard reads YAML off disk and needs no
# fixture, while `tests/conftest.py` imports the `nyxgpt` package (home
# sandbox, log guard, config) and would make the smoke job that runs this
# script install the whole project to answer a question about a workflow file.
# The full suite still collects the same file WITH conftest, so nothing here
# exempts it from the ordinary run.
_guard() {
  (cd "$ROOT_DIR" && python3 -m pytest "$GUARD" -q -p no:cacheprovider --noconftest \
      >/tmp/guard.out 2>&1)
}

_expect_green() {
  local desc="$1"
  if _guard; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc -- the guard is red on a tree that should pass:" >&2
    cat /tmp/guard.out >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_expect_red() {
  local desc="$1"
  if _guard; then
    echo "[FAIL] $desc -- the guard PASSED on a workflow that hands this" >&2
    echo "       repository's secrets to a PR author. The guard is vacuous." >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc (guard red, as it must be)"
  fi
}

# `sed -i` on the LAST match: the pr-hygiene job is the last job in the file,
# so its checkout is the last `ref:` line. Asserted rather than assumed --
# a silent no-op substitution would make every case below pass by accident.
_patch_last_ref() {
  local replacement="$1"
  python3 - "$WF" "$replacement" <<'PY'
import sys
path, replacement = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
needle = "          ref: ${{ vars.RELEASE_BRANCH }}"
i = text.rindex(needle)
open(path, "w", encoding="utf-8").write(
    text[:i] + "          ref: " + replacement + text[i + len(needle):]
)
PY
  grep -q -- "$replacement" "$WF" || { echo "::error::injection did not apply" >&2; exit 1; }
}

_patch_step_body() {
  python3 - "$WF" "$1" <<'PY'
import sys
path, line = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
needle = "          PR=${{ github.event.pull_request.number }}\n"
i = text.rindex(needle) + len(needle)
open(path, "w", encoding="utf-8").write(text[:i] + "          " + line + "\n" + text[i:])
PY
  grep -qF -- "$1" "$WF" || { echo "::error::injection did not apply" >&2; exit 1; }
}

echo "=== half 1: the shipped tree ==="
_expect_green "the guard passes on the tree as committed"

echo "=== half 2: each way the safety property can be lost ==="

_patch_last_ref '${{ github.event.pull_request.head.sha }}'
_expect_red "checkout moved to the PR head commit"
cp "$PRISTINE" "$WF"

_patch_last_ref '${{ github.head_ref }}'
_expect_red "checkout moved to the PR head branch"
cp "$PRISTINE" "$WF"

_patch_step_body 'gh pr checkout "$PR"'
_expect_red "a gh pr checkout inside a step body"
cp "$PRISTINE" "$WF"

_patch_step_body 'npm ci --prefix web'
_expect_red "a dependency install over the PR author's tree"
cp "$PRISTINE" "$WF"

# The defect itself: back on `pull_request`, Dependabot PRs get no secret and
# pr-hygiene dies at require_gh_auth, which is how #4163/#4165 never reached
# the board. The guard has to notice the regression, not just the escalation.
python3 - "$WF" <<'PY'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = text.replace("  pull_request_target:\n", "  pull_request:\n", 1)
text = text.replace(
    "if: github.event_name == 'pull_request_target'",
    "if: github.event_name == 'pull_request'",
    1,
)
open(path, "w", encoding="utf-8").write(text)
PY
_expect_red "the trigger reverted to pull_request (the #4167 defect returning)"
cp "$PRISTINE" "$WF"

echo "=== restored ==="
_expect_green "the guard passes again once the file is restored"

if ((FAILURES > 0)); then
  echo "FAILED: $FAILURES case(s)" >&2
  exit 1
fi
echo "All cases held: the guard is red on every unsafe shape and green on the shipped one."
