#!/usr/bin/env bash
set -uo pipefail

# tests/test_retire_rc_formulas.sh
# Tests for scripts/retire_rc_formulas.sh (#3730): the automated release
# ceremony's final step, which removes a shipped line's `-rc` formulas from
# the remote Homebrew tap. Runs against a real LOCAL bare repo standing in
# for the tap (TAP_CLONE_URL), so the clone/commit/push/verify path is
# exercised end to end without GitHub.
#
# Usage: bash tests/test_retire_rc_formulas.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/retire_rc_formulas.sh"

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

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Builds a fresh bare "tap" whose -rc formulas carry $1 as their version.
_make_tap() {
  local rc_version="$1"
  local bare="$WORK/tap.git" seed="$WORK/seed"
  rm -rf "$bare" "$seed"
  git init --quiet --bare -b main "$bare"
  git init --quiet -b main "$seed"
  mkdir -p "$seed/Formula"
  for name in nyxgpt-api nyxgpt-web; do
    printf 'class X < Formula\n  version "3.0.0"\nend\n' >"$seed/Formula/${name}.rb"
    printf 'class XRc < Formula\n  version "%s"\nend\n' "$rc_version" >"$seed/Formula/${name}-rc.rb"
  done
  git -C "$seed" config user.email t@example.com
  git -C "$seed" config user.name tester
  git -C "$seed" add -A
  git -C "$seed" commit --quiet -m seed
  git -C "$seed" push --quiet "$bare" main
  echo "$bare"
}

_tap_files() {
  git -C "$1" ls-tree -r --name-only main | sort | tr '\n' ' '
}

# --- Test 1: rc formulas of the released line are retired ---
BARE="$(_make_tap "3.0.0rc4")"
out="$(TAP_CLONE_URL="$BARE" bash "$SCRIPT" 3.0.0 2>&1)"
rc=$?
_assert_eq "retirement succeeds" "0" "$rc"
_assert_eq "only the stable formulas remain" \
  "Formula/nyxgpt-api.rb Formula/nyxgpt-web.rb " "$(_tap_files "$BARE")"

# --- Test 2: candidates for a LATER line are left alone ---
BARE="$(_make_tap "3.1.0rc1")"
TAP_CLONE_URL="$BARE" bash "$SCRIPT" 3.0.0 >/dev/null 2>&1
_assert_eq "a newer line's candidates survive the older line's ceremony" \
  "Formula/nyxgpt-api-rc.rb Formula/nyxgpt-api.rb Formula/nyxgpt-web-rc.rb Formula/nyxgpt-web.rb " \
  "$(_tap_files "$BARE")"

# --- Test 3: DRY_RUN changes nothing ---
BARE="$(_make_tap "3.0.0rc4")"
DRY_RUN=1 TAP_CLONE_URL="$BARE" bash "$SCRIPT" 3.0.0 >/dev/null 2>&1
_assert_eq "DRY_RUN leaves the tap untouched" \
  "Formula/nyxgpt-api-rc.rb Formula/nyxgpt-api.rb Formula/nyxgpt-web-rc.rb Formula/nyxgpt-web.rb " \
  "$(_tap_files "$BARE")"

# --- Test 4: re-running after a retirement is a no-op, not an error ---
BARE="$(_make_tap "3.0.0rc4")"
TAP_CLONE_URL="$BARE" bash "$SCRIPT" 3.0.0 >/dev/null 2>&1
out="$(TAP_CLONE_URL="$BARE" bash "$SCRIPT" 3.0.0 2>&1)"
rc=$?
_assert_eq "a second retirement run exits clean" "0" "$rc"
_assert_eq "the tap is unchanged by the second run" \
  "Formula/nyxgpt-api.rb Formula/nyxgpt-web.rb " "$(_tap_files "$BARE")"

# --- Test 5: an unconfigured tap is a skip, not a failure ---
out="$(env -u TAP_CLONE_URL -u TAP_REPO -u TAP_TOKEN bash "$SCRIPT" 3.0.0 2>&1)"
rc=$?
_assert_eq "no tap configured -> exit 0" "0" "$rc"
case "$out" in
  *"no remote tap"*) echo "[ok] the skip says why" ;;
  *) echo "[FAIL] expected a 'no remote tap' notice, got: $out" >&2; FAILURES=$((FAILURES + 1)) ;;
esac

# --- Test 6: a malformed version is rejected before anything is cloned ---
BARE="$(_make_tap "3.0.0rc4")"
TAP_CLONE_URL="$BARE" bash "$SCRIPT" "3.0" >/dev/null 2>&1
_assert_eq "a non x.y.z version is refused" "2" "$?"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
