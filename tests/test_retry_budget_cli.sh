#!/usr/bin/env bash
set -uo pipefail

# tests/test_retry_budget_cli.sh
# Smoke test for scripts/agents/lib/retry_budget.py's CLI surface (#3689) --
# the exact invocation shape developer_auto_implement.yml uses (JSON on
# stdin, positional args, one line of output). The underlying logic is
# covered exhaustively by tests/unit/test_retry_budget.py; this just checks
# the CLI wiring (arg parsing, JSON in/out) end to end.
#
# Usage: bash tests/test_retry_budget_cli.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/lib/retry_budget.py"

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

# --- signature: deterministic hash from stdin + step name arg ---
SIG1="$(printf '404 Not Found' | python3 "$SCRIPT" signature "Check if PR already exists")"
SIG2="$(printf '404 Not Found' | python3 "$SCRIPT" signature "Check if PR already exists")"
_assert_eq "signature is deterministic across invocations" "$SIG1" "$SIG2"

SIG3="$(printf 'a different error' | python3 "$SCRIPT" signature "Check if PR already exists")"
if [[ "$SIG1" == "$SIG3" ]]; then
  echo "[FAIL] signature should differ for a different error body" >&2
  FAILURES=$((FAILURES + 1))
else
  echo "[ok] signature differs for a different error body"
fi

# --- marker: round-trips through decide() when embedded in a comment ---
MARKER="$(python3 "$SCRIPT" marker "Check if PR already exists" "$SIG1" 1)"
case "$MARKER" in
  "<!-- nyxgpt-retry: step=check_if_pr_already_exists sig=${SIG1} n=1 -->")
    echo "[ok] marker renders the expected format" ;;
  *)
    echo "[FAIL] unexpected marker format: $MARKER" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

# --- decide: counts the rendered marker back out of a comment thread ---
COMMENTS_JSON=$(cat <<JSON
[{"body": "some unrelated comment", "author_association": "NONE"},
 {"body": "$MARKER", "author_association": "NONE"}]
JSON
)
DECISION=$(echo "$COMMENTS_JSON" | python3 "$SCRIPT" decide "Check if PR already exists" "$SIG1")
RETRY_COUNT=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["retry_count"])')
_assert_eq "decide counts the one marker present in the thread" "1" "$RETRY_COUNT"

SAME_SIG=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["same_signature_as_last"])')
_assert_eq "decide reports same_signature_as_last=True for the identical signature" "True" "$SAME_SIG"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
