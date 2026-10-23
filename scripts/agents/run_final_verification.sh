#!/usr/bin/env bash
set -uo pipefail

# run_final_verification.sh -- the developer agent's Final Verification gate,
# which RECORDS WHY IT FAILED (#4176).
#
# WHY THIS IS A SCRIPT. It used to be an inline `run:` block in
# `developer_auto_implement.yml` ("Final Verification (Must Pass)"), and it
# recorded nothing. Phase 1's classifier therefore had only the failed step's
# NAME to classify -- the per-job logs API returns nothing while the job is
# still running, so the harvest falls back to `$FAILED_STEPS` and the entire
# "error log" was the string `Final Verification (Must Pass)`. No signature in
# `classify_error` matches that, so the run was classified `unknown`, and
# `unknown` is the key that maps to "Error type could not be determined.
# Manual investigation needed." -- the headline the owner called "the usual
# unhelpful reason" (#4166, run 37709148793).
#
# The gate knows exactly why it failed: which check, and for pytest which
# tests. It writes that to the agent error-detail file (#3971) BEFORE failing,
# so the classifier reads a direct answer instead of guessing from a step name.
# Being a script also makes it EXECUTABLE in CI, which is how
# `.github/workflows/escalation-headline-smoke.yml` proves the recording works
# by running it against a deliberately red tree rather than by reading it.
#
# pytest's `-rf` SHORT SUMMARY is the source of the node IDs, deliberately: it
# is the one part of pytest's output whose shape is a contract (`FAILED
# <nodeid>` per line). Grepping `-v` output for "FAILED" picks up progress
# lines, parametrised ids split across columns and the summary rule itself.
#
# Env knobs exist for the smoke job, which must be able to make one gate fail
# on purpose:
#   NYXGPT_VERIFY_GATES         space-separated subset of the gates to run
#                               (default: black ruff mypy pytest tsc vitest
#                               routes -- `vitest` added by #4209, which is
#                               the issue about a blocking gate that nothing
#                               evaluated)
#   NYXGPT_VERIFY_PYTEST_TARGET what pytest runs (default tests/unit/)
#   NYXGPT_VERIFY_PYTEST_ARGS   pytest args (default: -v + coverage)
#   NYXGPT_VERIFY_LOG_DIR       where the per-gate logs land (default /tmp)
#   NYXGPT_VERIFY_PYTHON        the interpreter for EVERY python this script
#                               runs -- the gates AND `_fail_gate`'s own
#                               composition of the recorded reason (#4195).
#                               Defaults to `python` (what setup-python puts on
#                               PATH in the workflow), falling back to `python3`
#                               where there is no `python`; the shell suite
#                               points it at its own.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${DIR}/lib/gh_project.sh"

LIB_DIR="${DIR}/lib"
LOG_DIR="${NYXGPT_VERIFY_LOG_DIR:-/tmp}"
GATES="${NYXGPT_VERIFY_GATES:-black ruff mypy pytest tsc vitest routes}"
PYTEST_TARGET="${NYXGPT_VERIFY_PYTEST_TARGET:-tests/unit/}"
PYTEST_ARGS="${NYXGPT_VERIFY_PYTEST_ARGS:---cov=src/nyxgpt --cov-report=term-missing}"
# One interpreter for the whole run -- the gates AND `_fail_gate`'s composition
# of the recorded reason (#4195). `python` first because that is what
# `setup-python` puts on PATH in the workflow, then `python3`, which is the
# only one some machines have: the previous `${NYXGPT_VERIFY_PYTHON:-python}`
# would have failed every gate on such a machine with "python: command not
# found", a failure about the runner rather than about the tree.
if [[ -n "${NYXGPT_VERIFY_PYTHON:-}" ]]; then
  PY="$NYXGPT_VERIFY_PYTHON"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  PY="python3"
fi

mkdir -p "$LOG_DIR"

# Records the reason and fails. The exit is non-zero by design: this is still
# the gate, and nothing about reporting a failure better makes it pass.
_fail_gate() {
  local gate="$1" total="${2:-}" lines="$3"
  local detail
  # `$PY`, not a bare `python3` (#4195). Running the gates under one
  # interpreter and composing the recorded reason under whatever `python3`
  # PATH happens to resolve to made the recording depend on how the script was
  # invoked: from an activated venv the two are the same, from
  # `/path/to/venv/bin/python -m pytest` they are not, and on a machine with no
  # `python3` on PATH at all the detail came out EMPTY -- which is exactly the
  # "Error type could not be determined" headline this script exists to
  # prevent. One interpreter per run, named once above.
  detail="$(printf '%s\n' "$lines" \
    | "$PY" "${LIB_DIR}/escalation_evidence.py" verification-detail "$gate" "$total")"
  write_agent_error_detail "$detail"
  echo "::error::Final Verification failed at the ${gate} gate."
  printf '%s\n' "$detail"
  exit 1
}

_gate_enabled() {
  [[ " $GATES " == *" $1 "* ]]
}

# -- black ------------------------------------------------------------------
if _gate_enabled black; then
  echo "::group::Final black format check"
  if ! "$PY" -m black --check . 2>&1 | tee "${LOG_DIR}/final_black.log"; then
    echo "::endgroup::"
    _fail_gate black "" "$(grep -E '^would reformat|^error' "${LOG_DIR}/final_black.log" | sed -n '1,20p')"
  fi
  echo "::endgroup::"
fi

# -- ruff -------------------------------------------------------------------
if _gate_enabled ruff; then
  echo "::group::Final ruff verification"
  if ! "$PY" -m ruff check src/ tests/ 2>&1 | tee "${LOG_DIR}/final_ruff.log"; then
    echo "::endgroup::"
    _fail_gate ruff "" "$(grep -E '^[^ ].*:[0-9]+:[0-9]+:|^error' "${LOG_DIR}/final_ruff.log" | sed -n '1,20p')"
  fi
  echo "::endgroup::"
fi

# -- mypy -------------------------------------------------------------------
if _gate_enabled mypy; then
  echo "::group::Final mypy verification"
  # --no-incremental for the same reason as attempt 1 (see #3730).
  if ! "$PY" -m mypy --no-incremental src/ 2>&1 | tee "${LOG_DIR}/final_mypy.log"; then
    echo "::endgroup::"
    _fail_gate mypy "" "$(grep -E 'error:' "${LOG_DIR}/final_mypy.log" | sed -n '1,20p')"
  fi
  echo "::endgroup::"
fi

# -- pytest -----------------------------------------------------------------
if _gate_enabled pytest; then
  echo "::group::Final pytest verification (unit tests - integration tests skipped in CI)"
  # Integration tests require a running API server (not available in CI).
  # `-rf` adds the short summary this script parses the node IDs out of.
  # shellcheck disable=SC2086  # PYTEST_ARGS is a deliberate word list
  if ! "$PY" -m pytest "$PYTEST_TARGET" -v -rf $PYTEST_ARGS 2>&1 \
    | tee "${LOG_DIR}/final_pytest.log"; then
    echo "::endgroup::"
    FAILED_NODES="$(grep -E '^FAILED ' "${LOG_DIR}/final_pytest.log" | sed 's/ - .*$//' | sed -n '1,20p')"
    # The count comes from pytest's own totals line, not from the lines above:
    # the short summary is what `-rf` prints, and a collection ERROR has no
    # `FAILED` line at all. Reporting "0 failing tests" for a run pytest says
    # failed is exactly the kind of confident wrong answer this issue is about.
    TOTAL="$(grep -oE '[0-9]+ failed' "${LOG_DIR}/final_pytest.log" | tail -1 | grep -oE '[0-9]+' || true)"
    if [[ -z "$FAILED_NODES" ]]; then
      FAILED_NODES="$(grep -E '^ERROR |^E   ' "${LOG_DIR}/final_pytest.log" | sed -n '1,20p')"
    fi
    _fail_gate pytest "${TOTAL:-}" "$FAILED_NODES"
  fi
  echo "::endgroup::"
fi

# -- TypeScript -------------------------------------------------------------
if _gate_enabled tsc; then
  echo "::group::Final TypeScript verification"
  if ! (cd web && npm run type-check) 2>&1 | tee "${LOG_DIR}/final_tsc.log"; then
    echo "::endgroup::"
    _fail_gate tsc "" "$(grep -E 'error TS|^error' "${LOG_DIR}/final_tsc.log" | sed -n '1,20p')"
  fi
  echo "::endgroup::"
fi

# -- vitest -----------------------------------------------------------------
# #4209. This gate was MISSING, and it is the one the issue was filed about.
# `ci-tests.yml` labels `vitest --coverage` blocking and the config carries a
# 100% threshold (#3266), and the only two places a change could be measured
# against it both declined to: that job ran on pushes to the release branch
# only, and this script -- whose own workflow step says it "Runs ALL checks
# unconditionally on the entire codebase" -- ran six gates and not this one.
# So #4207's two failing web tests passed Final Verification, reached review
# green, and were found by a human running the suite by hand.
#
# Same command a developer runs (`npm run test:coverage`) and the same one
# ci-tests blocks on, so the three cannot drift -- which they already had: the
# npm script was `vitest` in watch mode and never terminated.
# `tests/unit/test_blocking_gates_reach_pull_requests.py` holds the three to
# one list.
if _gate_enabled vitest; then
  echo "::group::Final web unit tests and coverage gate"
  if ! (cd web && npm run test:coverage) 2>&1 | tee "${LOG_DIR}/final_vitest.log"; then
    echo "::endgroup::"
    # Two shapes, both named: `FAIL`/`×` lines for a failing test, and the
    # threshold line for a coverage regression -- which reports no failing
    # test at all and would otherwise record an empty reason.
    _fail_gate vitest "" "$(grep -E '^(ERROR: Coverage|FAIL )|✕|×|Tests  .*failed' \
      "${LOG_DIR}/final_vitest.log" | sed -n '1,20p')"
  fi
  echo "::endgroup::"
fi

# -- routes -----------------------------------------------------------------
if _gate_enabled routes; then
  echo "::group::Final route validation"
  if ! ./scripts/agents/validate-web-routes.sh 2>&1 | tee "${LOG_DIR}/final_routes.log"; then
    echo "::endgroup::"
    _fail_gate routes "" "$(grep -vE '^\s*$' "${LOG_DIR}/final_routes.log" | tail -20)"
  fi
  echo "::endgroup::"
fi

echo "✅ All verification checks passed on final attempt!"
