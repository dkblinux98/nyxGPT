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
#   NYXGPT_VERIFY_PYTEST_TARGET what pytest runs (default tests/unit/)
#   NYXGPT_VERIFY_PYTEST_ARGS   pytest args (default: -v + coverage)
#   NYXGPT_VERIFY_LOG_DIR       where the per-gate logs land (default /tmp)
#   NYXGPT_VERIFY_PYTHON        the interpreter (default `python`, which is
#                               what setup-python puts on PATH in the workflow;
#                               the shell suite points it at its own)

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${DIR}/lib/gh_project.sh"

LIB_DIR="${DIR}/lib"
LOG_DIR="${NYXGPT_VERIFY_LOG_DIR:-/tmp}"
GATES="${NYXGPT_VERIFY_GATES:-black ruff mypy pytest tsc routes}"
PYTEST_TARGET="${NYXGPT_VERIFY_PYTEST_TARGET:-tests/unit/}"
PYTEST_ARGS="${NYXGPT_VERIFY_PYTEST_ARGS:---cov=src/nyxgpt --cov-report=term-missing}"
PY="${NYXGPT_VERIFY_PYTHON:-python}"

mkdir -p "$LOG_DIR"

# Records the reason and fails. The exit is non-zero by design: this is still
# the gate, and nothing about reporting a failure better makes it pass.
_fail_gate() {
  local gate="$1" total="${2:-}" lines="$3"
  local detail
  detail="$(printf '%s\n' "$lines" \
    | python3 "${LIB_DIR}/escalation_evidence.py" verification-detail "$gate" "$total")"
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
    _fail_gate black "" "$(grep -E '^would reformat|^error' "${LOG_DIR}/final_black.log" | head -20)"
  fi
  echo "::endgroup::"
fi

# -- ruff -------------------------------------------------------------------
if _gate_enabled ruff; then
  echo "::group::Final ruff verification"
  if ! "$PY" -m ruff check src/ tests/ 2>&1 | tee "${LOG_DIR}/final_ruff.log"; then
    echo "::endgroup::"
    _fail_gate ruff "" "$(grep -E '^[^ ].*:[0-9]+:[0-9]+:|^error' "${LOG_DIR}/final_ruff.log" | head -20)"
  fi
  echo "::endgroup::"
fi

# -- mypy -------------------------------------------------------------------
if _gate_enabled mypy; then
  echo "::group::Final mypy verification"
  # --no-incremental for the same reason as attempt 1 (see #3730).
  if ! "$PY" -m mypy --no-incremental src/ 2>&1 | tee "${LOG_DIR}/final_mypy.log"; then
    echo "::endgroup::"
    _fail_gate mypy "" "$(grep -E 'error:' "${LOG_DIR}/final_mypy.log" | head -20)"
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
    FAILED_NODES="$(grep -E '^FAILED ' "${LOG_DIR}/final_pytest.log" | sed 's/ - .*$//' | head -20)"
    # The count comes from pytest's own totals line, not from the lines above:
    # the short summary is what `-rf` prints, and a collection ERROR has no
    # `FAILED` line at all. Reporting "0 failing tests" for a run pytest says
    # failed is exactly the kind of confident wrong answer this issue is about.
    TOTAL="$(grep -oE '[0-9]+ failed' "${LOG_DIR}/final_pytest.log" | tail -1 | grep -oE '[0-9]+' || true)"
    if [[ -z "$FAILED_NODES" ]]; then
      FAILED_NODES="$(grep -E '^ERROR |^E   ' "${LOG_DIR}/final_pytest.log" | head -20)"
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
    _fail_gate tsc "" "$(grep -E 'error TS|^error' "${LOG_DIR}/final_tsc.log" | head -20)"
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
