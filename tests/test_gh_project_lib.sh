#!/usr/bin/env bash
set -uo pipefail

# tests/test_gh_project_lib.sh
# Standalone regression test for scripts/agents/lib/gh_project.sh's
# set_field_with_retry(): retry count, backoff, and final-failure
# propagation. Sources the real library and stubs set_project_field_value
# so no network/gh calls happen.
#
# Usage: bash tests/test_gh_project_lib.sh

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

# Load the library. It only defines functions at source time, so this is safe.
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

# Stub out sleep so the retry backoff doesn't actually slow the test down,
# and record every backoff duration it was called with.
SLEEP_CALLS=()
sleep() { SLEEP_CALLS+=("$1"); }

# --- Test 1: always-failing field set exhausts all attempts and propagates failure ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  return 1
}

SLEEP_CALLS=()
if set_field_with_retry "item-1" "Status" "In Review" 3 2>/dev/null; then
  echo "[FAIL] set_field_with_retry should have returned failure when all attempts fail" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] set_field_with_retry returns failure after exhausting attempts"
fi
_assert_eq "call count equals configured attempts" "3" "$CALL_COUNT"
_assert_eq "backoff sleeps once per failed attempt" "3" "${#SLEEP_CALLS[@]}"
_assert_eq "backoff duration grows with attempt number" "2 4 6" "${SLEEP_CALLS[*]}"

# --- Test 2: field set succeeds on the final attempt ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  [[ "$CALL_COUNT" -ge 3 ]]
}

SLEEP_CALLS=()
if set_field_with_retry "item-2" "Status" "In Review" 3 2>/dev/null; then
  echo "[ok] set_field_with_retry returns success once a later attempt succeeds"
else
  echo "[FAIL] set_field_with_retry should have returned success on the 3rd attempt" >&2
  FAILURES=$((FAILURES + 1))
fi
_assert_eq "stops retrying once an attempt succeeds" "3" "$CALL_COUNT"
_assert_eq "no backoff sleep after the final (successful) attempt" "2 4" "${SLEEP_CALLS[*]}"

# --- Test 3: field set succeeds on the first attempt (no retries needed) ---
CALL_COUNT=0
set_project_field_value() {
  CALL_COUNT=$((CALL_COUNT + 1))
  return 0
}

SLEEP_CALLS=()
set_field_with_retry "item-3" "Status" "In Review" 3 2>/dev/null
_assert_eq "no retries when the first attempt succeeds" "1" "$CALL_COUNT"
_assert_eq "no backoff sleep when the first attempt succeeds" "0" "${#SLEEP_CALLS[@]}"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
