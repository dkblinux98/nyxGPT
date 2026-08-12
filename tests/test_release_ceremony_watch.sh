#!/usr/bin/env bash
set -uo pipefail

# tests/test_release_ceremony_watch.sh
# Guardrail tests for scripts/agents/release_ceremony_watch.sh (#3730), the
# automated release ceremony's trigger. The ceremony is irreversible
# (master fast-forward, tag, GitHub Release, PyPI publish), so what matters
# is that it fires ONLY for the release tracking issue, ONLY on the
# transition into `For Release`, and never twice for the same version.
#
# Runs the real script with a fake `gh` on PATH and `--check-only`, so no
# mutation can happen even if a guardrail were to regress.
#
# Usage: bash tests/test_release_ceremony_watch.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/release_ceremony_watch.sh"

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

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- fake gh: answers the three calls the watcher makes -------------------
mkdir -p "$WORK/bin"
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"auth status"*)
    exit 0 ;;
  *graphql*)
    # issue_status()'s project-item query
    printf '{"data":{"repository":{"issue":{"projectItems":{"nodes":[{"fieldValues":{"nodes":[{"field":{"name":"Status"},"name":"%s"}]}}]}}}}}\n' "$FAKE_STATUS"
    exit 0 ;;
  *comments*)
    echo "${FAKE_COMMENTS:-[]}"
    exit 0 ;;
  *issues/*)
    # `--jq '.title'` -- gh applies the filter itself, so print the title
    echo "$FAKE_TITLE"
    exit 0 ;;
esac
echo "[fake-gh] unexpected call: $args" >&2
exit 1
FAKE
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

_write_config() { # _write_config [release_issue_number]
  local release_issue="${1-}"
  cat >"$WORK/config.ini" <<EOF
REPO_OWNER=test-owner
REPO_NAME=test-repo
PROJECT_OWNER=test-owner
PROJECT_NUMBER=1
DEV_AGENT=dev
REVIEW_AGENT=rev
SCRUM_AGENT=scrum
HUMAN_OWNER=owner
STATUS_FIELD=Status
STATUS_BACKLOG=Backlog
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_FOR_RELEASE=For Release
RELEASE_BRANCH=v3.0.0
EOF
  [[ -n "$release_issue" ]] && echo "RELEASE_ISSUE_NUMBER=$release_issue" >>"$WORK/config.ini"
  export NYXGPT_CONFIG_FILE="$WORK/config.ini"
}

_run() { NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SCRIPT" --check-only 2>/dev/null | tail -1; }

export FAKE_TITLE="Release v3.0.0 — Phase 6"
export FAKE_COMMENTS='[]'

# --- Test 1: the release issue is still under acceptance -> no ceremony ---
_write_config 3521
export FAKE_STATUS="Acceptance Testing"
out="$(_run)"
_assert_eq "no ceremony while the release issue is still in Acceptance Testing" \
  "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the reason names the status" "$out" "Acceptance Testing"

# --- Test 2: the owner moves it to For Release -> the ceremony fires ---
export FAKE_STATUS="For Release"
out="$(_run)"
_assert_eq "moving the release issue to For Release fires the ceremony" \
  "true" "$(jq -r '.fire' <<<"$out")"
_assert_eq "the version comes from the release issue title" \
  "3.0.0" "$(jq -r '.version' <<<"$out")"

# --- Test 3: a marker for this version means it already ran -> no re-fire ---
# (the watcher polls every 15 minutes; without this the ceremony would
# re-run forever after a release)
export FAKE_COMMENTS='[{"body":"starting\n<!-- nyxgpt-release-ceremony:3.0.0 -->"}]'
out="$(_run)"
_assert_eq "an existing marker for this version suppresses a second ceremony" \
  "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the reason says it already started" "$out" "already started"

# --- Test 3b: a marker for a DIFFERENT version does not suppress it ---
export FAKE_COMMENTS='[{"body":"previous line\n<!-- nyxgpt-release-ceremony:2.1.0 -->"}]'
out="$(_run)"
_assert_eq "the previous line's marker does not suppress this line's ceremony" \
  "true" "$(jq -r '.fire' <<<"$out")"

# --- Test 4: FORCE_CEREMONY re-arms a marked version deliberately ---
export FAKE_COMMENTS='[{"body":"<!-- nyxgpt-release-ceremony:3.0.0 -->"}]'
out="$(FORCE_CEREMONY=1 NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SCRIPT" --check-only 2>/dev/null | tail -1)"
_assert_eq "force re-arms the trigger for a re-run" "true" "$(jq -r '.fire' <<<"$out")"

# --- Test 5: an unparseable release title is a conservative stop ---
export FAKE_COMMENTS='[]'
export FAKE_TITLE="Release tracking issue"
out="$(_run)"
_assert_eq "no version in the title -> no ceremony" "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the stop is explained" "$out" "conservative stop"

# --- Test 6: no release issue configured -> nothing to watch ---
_write_config ""
export FAKE_TITLE="Release v3.0.0"
export FAKE_STATUS="For Release"
out="$(_run)"
_assert_eq "no RELEASE_ISSUE_NUMBER -> no ceremony" "false" "$(jq -r '.fire' <<<"$out")"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
