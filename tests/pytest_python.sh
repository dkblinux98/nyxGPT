#!/usr/bin/env bash
# tests/pytest_python.sh -- the one place that answers "which interpreter on
# this machine can run `python -m pytest`?" for every shell suite (#4195).
#
# THE DEFECT THIS EXISTS FOR. Three shell suites each answered that question
# their own way, and all three got an answer that depends on how the suite was
# invoked rather than on the state of the tree:
#
#   * tests/test_escalation_evidence.sh took `${NYXGPT_TEST_PYTHON:-python3}`.
#     Run from pytest, `$NYXGPT_TEST_PYTHON` is the interpreter pytest is
#     running under, so its EXECUTED section ran. Run the documented way --
#     `bash tests/test_escalation_evidence.sh` -- `python3` is whatever is on
#     PATH, which on a CI runner and on most dev machines is a system
#     interpreter with no pytest, so the section was SKIPPED and the suite
#     still printed "All tests passed." and exited 0.
#   * tests/test_pr_head_guard_discriminates.sh ran a bare `python3 -m pytest`
#     and read a non-zero exit as "the guard is red, as it must be". Where
#     `python3` has no pytest, every one of its nine fault-injection cases
#     reported `[ok]` without the guard ever starting: a guard that cannot run
#     reads exactly like a guard that caught everything.
#   * tests/test_red_head_round.sh had half the fix (it resolves through
#     `command -v` so a stub directory on PATH cannot shadow the name) and not
#     the other half (it never checks the interpreter it picked has pytest).
#
# So: one resolver, used by all of them, and a "could not run" that is loud.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/pytest_python.sh"
#   PYTHON="$(require_pytest_python)" || exit $?     # or:
#   PYTHON="$(resolve_pytest_python)" && ... || ...  # when a suite handles it
#
# `require_pytest_python` exits NO_PYTEST_PYTHON_EXIT (77) when the machine has
# no usable interpreter. 77 rather than 1 because "this machine cannot answer
# the question" is a third outcome, and collapsing it into either of the other
# two is the whole defect: as 0 it is a pass over an unexecuted gate, as 1 it
# is a red test that says nothing about the code. The pytest wrappers map 77 to
# `pytest.skip`; the smoke jobs let it fail the job, because a CI runner that
# cannot run pytest is a broken job and not a tolerable skip.

# Distinct from any pytest exit status (0-5) and from the suites' own 1.
NO_PYTEST_PYTHON_EXIT=77

# The order is "most specific to this run" first:
#   NYXGPT_TEST_PYTHON  -- set by the pytest wrappers to `sys.executable`, and
#                          by the smoke jobs to setup-python's interpreter.
#   VIRTUAL_ENV         -- an activated venv, which is where a developer's
#                          pytest lives even when `python3` is the system one.
#   python3 / python    -- PATH, for a machine where that is the dev install.
#   .venv / venv        -- the two conventional in-repo venv names, so
#                          `bash tests/<suite>.sh` works from a plain checkout
#                          without the developer having activated anything.
_pytest_python_candidates() {
  local root="${1:-}"
  printf '%s\n' \
    "${NYXGPT_TEST_PYTHON:-}" \
    "${VIRTUAL_ENV:+${VIRTUAL_ENV}/bin/python}" \
    python3 \
    python \
    "${root:+${root}/.venv/bin/python}" \
    "${root:+${root}/venv/bin/python}"
}

# _resolve_python <root_dir> [import_requirement]
#
# Prints the first candidate that exists and -- when `import_requirement` is
# given -- can import it. Returns 1 with no output otherwise, so the caller can
# assign the result unconditionally.
_resolve_python() {
  local root="${1:-}" requirement="${2:-}" candidate resolved
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    # `command -v` rather than using the bare name: tests/test_red_head_round.sh
    # learned this the hard way -- a suite that puts a stub directory on PATH
    # shadows `python3`, and a shim that re-execs the bare name loops. An
    # absolute path is also what the suites hand on to their own subprocesses.
    resolved="$(command -v "$candidate" 2>/dev/null)" || continue
    [[ -n "$resolved" ]] || continue
    if [[ -z "$requirement" ]] || "$resolved" -c "import ${requirement}" >/dev/null 2>&1; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done < <(_pytest_python_candidates "$root")
  return 1
}

# resolve_pytest_python [root_dir]
#
# The first candidate that can `import pytest`, for a suite that EXECUTES
# pytest. Returns 1 with no output when there is none.
resolve_pytest_python() {
  _resolve_python "${1:-}" pytest
}

# resolve_python [root_dir]
#
# The first candidate that exists at all, for a suite whose `$PYTHON` only runs
# stdlib scripts (tests/test_red_head_round.sh). Same candidate order and the
# same `command -v` absolutisation; it just does not demand pytest, because
# demanding a dependency a suite does not use is its own way of making the
# answer depend on the environment rather than on the tree.
resolve_python() {
  _resolve_python "${1:-}" ""
}

# require_python [root_dir]
#
# Prints any interpreter, or explains on stderr and exits 77.
require_python() {
  local resolved
  if resolved="$(resolve_python "${1:-}")"; then
    printf '%s\n' "$resolved"
    return 0
  fi
  {
    echo "[NO PYTHON] no python interpreter found on this machine."
    echo "[NO PYTHON] Tried: \$NYXGPT_TEST_PYTHON, \$VIRTUAL_ENV/bin/python, python3,"
    echo "[NO PYTHON] python, .venv/bin/python, venv/bin/python."
    echo "[NO PYTHON] Exiting ${NO_PYTEST_PYTHON_EXIT} -- NOT a pass."
  } >&2
  exit "$NO_PYTEST_PYTHON_EXIT"
}

# require_pytest_python [root_dir]
#
# Prints the interpreter, or explains on stderr and exits 77. Call it as
# `PYTHON="$(require_pytest_python "$ROOT_DIR")" || exit $?` -- the command
# substitution runs in a subshell, so the `|| exit $?` is what propagates 77
# out of the suite.
require_pytest_python() {
  local resolved
  if resolved="$(resolve_pytest_python "${1:-}")"; then
    printf '%s\n' "$resolved"
    return 0
  fi
  {
    echo "[NO PYTEST] no interpreter on this machine can run \`python -m pytest\`."
    echo "[NO PYTEST] This suite EXECUTES pytest, so it cannot answer anything here."
    echo "[NO PYTEST] Tried: \$NYXGPT_TEST_PYTHON, \$VIRTUAL_ENV/bin/python, python3,"
    echo "[NO PYTEST] python, .venv/bin/python, venv/bin/python."
    echo "[NO PYTEST] Fix: \`pip install -e \".[dev]\"\` in a venv and run from it, or"
    echo "[NO PYTEST] pass NYXGPT_TEST_PYTHON=<interpreter with pytest>."
    echo "[NO PYTEST] Exiting ${NO_PYTEST_PYTHON_EXIT} -- NOT a pass."
  } >&2
  exit "$NO_PYTEST_PYTHON_EXIT"
}
