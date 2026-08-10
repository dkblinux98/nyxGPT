#!/usr/bin/env bash
set -uo pipefail

# tests/test_cross_issue_anomaly_cli.sh
# Smoke test for scripts/agents/lib/cross_issue_anomaly.py's CLI surface
# (#3694) -- the exact invocation shape developer_auto_implement.yml and
# gh_project.sh use (JSON on stdin, positional args, one line of output).
# The underlying logic is covered exhaustively by
# tests/unit/test_cross_issue_anomaly.py; this just checks the CLI wiring
# (arg parsing, JSON in/out) end to end.
#
# Usage: bash tests/test_cross_issue_anomaly_cli.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/lib/cross_issue_anomaly.py"

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

# --- marker: renders the expected format ---
MARKER="$(python3 "$SCRIPT" marker "Check if PR already exists" 3667 1000)"
case "$MARKER" in
  "<!-- nyxgpt-anomaly: step=check_if_pr_already_exists issue=3667 opened=1000 -->")
    echo "[ok] marker renders the expected format" ;;
  *)
    echo "[FAIL] unexpected marker format: $MARKER" >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

# --- decide: no marker present -> this issue opens the tracking record ---
DECISION=$(echo "[]" | python3 "$SCRIPT" decide 3667 "Check if PR already exists" 1000)
ACTION=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')
_assert_eq "decide opens a new record when none exists" "open" "$ACTION"

# --- decide: matching marker from a DIFFERENT issue, within window -> skip ---
COMMENTS_JSON=$(cat <<JSON
[{"body": "$MARKER", "author_association": "NONE"}]
JSON
)
DECISION=$(echo "$COMMENTS_JSON" | python3 "$SCRIPT" decide 3511 "Check if PR already exists" 1500)
ACTION=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')
ORIGIN=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["origin_issue"])')
_assert_eq "decide skips for a different issue matching an open record" "skip" "$ACTION"
_assert_eq "decide reports the originating issue" "3667" "$ORIGIN"

# --- decide: an OWNER RESOLVE_ANOMALY comment closes the record early ---
RESOLVED_JSON=$(cat <<JSON
[{"body": "$MARKER", "author_association": "NONE"},
 {"body": "RESOLVE_ANOMALY", "author_association": "OWNER"}]
JSON
)
DECISION=$(echo "$RESOLVED_JSON" | python3 "$SCRIPT" decide 3511 "Check if PR already exists" 1500)
ACTION=$(echo "$DECISION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')
_assert_eq "decide reopens after an OWNER RESOLVE_ANOMALY comment" "open" "$ACTION"

# --- any-open: true while an unresolved marker is within window, false once resolved ---
ANY_OPEN=$(echo "$COMMENTS_JSON" | python3 "$SCRIPT" any-open 1500)
_assert_eq "any-open is true for an unresolved in-window marker" "true" "$ANY_OPEN"

ANY_OPEN=$(echo "$RESOLVED_JSON" | python3 "$SCRIPT" any-open 1500)
_assert_eq "any-open is false once RESOLVE_ANOMALY closes the only marker" "false" "$ANY_OPEN"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
