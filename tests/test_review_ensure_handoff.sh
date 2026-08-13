#!/usr/bin/env bash
set -uo pipefail

# tests/test_review_ensure_handoff.sh
# Standalone regression test for scripts/agents/review_ensure_handoff.sh
# (#3704): the dispatch-mode REQUEST_CHANGES handoff backstop. Stubs `gh`
# with canned PR reviews/comments so no network calls happen, and runs the
# script in dry-run mode so it plans without mutating anything.
#
# Usage: bash tests/test_review_ensure_handoff.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/review_ensure_handoff.sh"

FAILURES=0
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output to contain '$needle', got:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output NOT to contain '$needle', got:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# Minimal config satisfying load_config's strict key list.
CONFIG="$TMP_ROOT/config.ini"
cat > "$CONFIG" <<'EOF'
REPO_OWNER=dkblinux98
REPO_NAME=nyxGPT
PROJECT_OWNER=dkblinux98
PROJECT_NUMBER=1
DEV_AGENT=myGPT-developer-agent
REVIEW_AGENT=myGPT-review-agent
SCRUM_AGENT=myGPT-scrummaster-agent
HUMAN_OWNER=dkblinux98
STATUS_FIELD=Status
STATUS_BACKLOG=Backlog
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_FOR_RELEASE=For Release
RELEASE_BRANCH=v3.0.0
EOF

# Fake `gh` on PATH. Reads its canned payloads from files the test rewrites
# between cases, so a single stub covers every scenario.
BIN="$TMP_ROOT/bin"
mkdir -p "$BIN"
cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  auth) exit 0 ;;
  api)
    case "$2" in
      *"/reviews") cat "$GH_STUB_DIR/reviews.json" ;;
      *"/comments") cat "$GH_STUB_DIR/comments.json" ;;
      *) echo '{"body": "Closes #3511"}' ;;
    esac
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$BIN/gh"

export GH_STUB_DIR="$TMP_ROOT"
export PATH="$BIN:$PATH"
export NYXGPT_CONFIG_FILE="$CONFIG"
# `gh api --jq '.[]'` streams objects; the stubs below are already in that
# shape, and the script slurps them back into an array with jq -s.
export REVIEW_HANDOFF_WAIT_SECONDS=1
export REVIEW_HANDOFF_POLL_SECONDS=1
export REVIEW_HANDOFF_DRY_RUN=1

RC_REVIEW='{"user":{"login":"myGPT-review-agent"},"state":"CHANGES_REQUESTED","submitted_at":"2026-08-09T19:39:00Z","body":"## Code Review - REQUEST_CHANGES"}'

# --- Test 1: verdict with no handoff footprint -> backstop plans a repair ---
echo "$RC_REVIEW" > "$TMP_ROOT/reviews.json"
: > "$TMP_ROOT/comments.json"

OUT="$(bash "$SCRIPT" 3684 2>&1)"
_assert_contains "dropped handoff is detected" "$OUT" "no handoff yet"
_assert_contains "dropped handoff routes back to the developer" "$OUT" "action=return_to_developer"

# --- Test 2: event chain already handled it -> immediate no-op ---
echo '{"created_at":"2026-08-09T19:41:00Z","body":"🔄 **Review Agent**: Changes requested (review loop 2/3)"}' \
  > "$TMP_ROOT/comments.json"

OUT="$(bash "$SCRIPT" 3684 2>&1)"
_assert_contains "existing handoff footprint stands the backstop down" "$OUT" "Nothing to repair"
_assert_not_contains "no repair is planned when the chain worked" "$OUT" "DRY RUN"

# --- Test 3: APPROVE verdict is left to the merge path ---
printf '%s\n%s\n' "$RC_REVIEW" \
  '{"user":{"login":"myGPT-review-agent"},"state":"APPROVED","submitted_at":"2026-08-09T20:00:00Z","body":"## Code Review - APPROVE"}' \
  > "$TMP_ROOT/reviews.json"
: > "$TMP_ROOT/comments.json"

OUT="$(bash "$SCRIPT" 3684 2>&1)"
_assert_contains "approve is not touched by the backstop" "$OUT" "latest-verdict-approved"

# --- Test 4: a mid-wait handoff by the event chain aborts the repair ---
echo "$RC_REVIEW" > "$TMP_ROOT/reviews.json"
: > "$TMP_ROOT/comments.json"
(
  sleep 1
  echo '{"created_at":"2026-08-09T19:41:00Z","body":"HUDDLE_TRIGGERED"}' > "$TMP_ROOT/comments.json"
) &
OUT="$(REVIEW_HANDOFF_WAIT_SECONDS=4 bash "$SCRIPT" 3684 2>&1)"
wait
_assert_contains "late event-chain handoff stands the backstop down" "$OUT" "backstop stands down"

# --- Test 5: a long thread does not blow the execve argument limit (#3736) ---
# The threads that reach three review cycles, a huddle or an escalation are
# exactly the long ones -- incident PR #3728 was already at ~96KB of comment
# JSON. Building the plan payload with `jq -n --argjson comments "$comments"`
# put the whole thread in one execve argument, capped at MAX_ARG_STRLEN
# (131072 bytes), so the backstop died with "Argument list too long" at the
# moment it was most needed. The payload is assembled with the `printf`
# builtin now, which has no such limit. ~240KB here, comfortably past the cap.
echo "$RC_REVIEW" > "$TMP_ROOT/reviews.json"
FILLER="$(printf 'x%.0s' {1..4000})"
: > "$TMP_ROOT/comments.json"
for i in {1..60}; do
  printf '{"created_at":"2026-08-09T19:%02d:00Z","id":%d,"body":"chatter %s"}\n' \
    "$((i % 60))" "$i" "$FILLER" >> "$TMP_ROOT/comments.json"
done
THREAD_BYTES="$(wc -c < "$TMP_ROOT/comments.json")"
if [[ "$THREAD_BYTES" -le 131072 ]]; then
  echo "[FAIL] long-thread fixture is only ${THREAD_BYTES}B, below MAX_ARG_STRLEN" >&2
  FAILURES=$((FAILURES + 1))
fi

OUT="$(bash "$SCRIPT" 3684 2>&1)"
_assert_not_contains "a ${THREAD_BYTES}B thread does not exceed the argument limit" \
  "$OUT" "Argument list too long"
_assert_contains "a long thread still plans the repair" "$OUT" "action=return_to_developer"

if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES test(s) failed" >&2
  exit 1
fi

echo "All review_ensure_handoff.sh tests passed"
