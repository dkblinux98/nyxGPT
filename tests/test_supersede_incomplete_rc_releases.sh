#!/usr/bin/env bash
set -uo pipefail

# tests/test_supersede_incomplete_rc_releases.sh
# Tests for scripts/supersede_incomplete_rc_releases.sh (#3747): the sweep the
# rc tap job runs before it cuts a candidate release, which retires
# prereleases whose service tarballs are missing -- the state
# `3.0.0rc3` was left in when the old upload-after-publish step hit
# `HTTP 422: Cannot upload assets to an immutable release`.
#
# Runs the real script against a stateful fake `gh`, so the delete path, the
# "delete refused -> annotate the notes" fallback and the "neither works"
# warning are all exercised without touching GitHub.
#
# Usage: bash tests/test_supersede_incomplete_rc_releases.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/supersede_incomplete_rc_releases.sh"

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
  local desc="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- fake gh: a tiny stateful releases API -------------------------------
# State lives in $FAKE_STATE: `tags` (one tag per line, in list order),
# `<tag>.assets` (space-joined asset names, i.e. what `--jq '[.assets[].name]
# | join(" ")'` would print), `<tag>.body`, plus the `deleted`/`edited`
# journals the assertions read.
mkdir -p "$WORK/bin"
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
STATE="$FAKE_STATE"
case "${1:-} ${2:-}" in
  "release list")
    cat "$STATE/tags"
    exit 0 ;;
  "release view")
    tag="$3"
    [[ -e "$STATE/$tag.assets" ]] || { echo "release not found" >&2; exit 1; }
    if [[ "$*" == *"--json assets"* ]]; then cat "$STATE/$tag.assets"; exit 0; fi
    if [[ "$*" == *"--json body"* ]]; then cat "$STATE/$tag.body"; exit 0; fi
    ;;
  "release delete")
    tag="$3"
    if [[ "${FAKE_DELETE_FAILS:-0}" == "1" ]]; then
      echo "HTTP 422: Cannot delete an immutable release" >&2
      exit 1
    fi
    echo "$tag" >>"$STATE/deleted"
    rm -f "$STATE/$tag.assets" "$STATE/$tag.body"
    grep -vx "$tag" "$STATE/tags" >"$STATE/tags.new" || true
    mv "$STATE/tags.new" "$STATE/tags"
    exit 0 ;;
  "release edit")
    tag="$3"
    if [[ "${FAKE_EDIT_FAILS:-0}" == "1" ]]; then
      echo "HTTP 422: release is immutable" >&2
      exit 1
    fi
    notes=""
    while [[ $# -gt 0 ]]; do
      [[ "$1" == "--notes-file" ]] && notes="$2"
      shift
    done
    [[ -n "$notes" ]] || { echo "no --notes-file" >&2; exit 1; }
    cp "$notes" "$STATE/$tag.body"
    echo "$tag" >>"$STATE/edited"
    exit 0 ;;
esac
echo "[fake-gh] unexpected call: $*" >&2
exit 1
FAKE
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

# Seeds the fake API with:
#   3.0.0rc2  complete   (both tarballs)
#   3.0.0rc3  incomplete (the #3747 leftover: no assets at all)
#   3.1.0rc1  incomplete, but on ANOTHER line
#   3.0.0     the release itself, not a candidate
_seed() {
  export FAKE_STATE="$WORK/state"
  rm -rf "$FAKE_STATE"
  mkdir -p "$FAKE_STATE"
  printf '%s\n' 3.0.0rc2 3.0.0rc3 3.1.0rc1 3.0.0 >"$FAKE_STATE/tags"
  echo "nyxgpt-api-3.0.0rc2.tar.gz nyxgpt-web-3.0.0rc2.tar.gz" >"$FAKE_STATE/3.0.0rc2.assets"
  echo "" >"$FAKE_STATE/3.0.0rc3.assets"
  echo "" >"$FAKE_STATE/3.1.0rc1.assets"
  echo "nyxgpt-api-3.0.0.tar.gz nyxgpt-web-3.0.0.tar.gz" >"$FAKE_STATE/3.0.0.assets"
  for tag in 3.0.0rc2 3.0.0rc3 3.1.0rc1 3.0.0; do
    echo "original notes for $tag" >"$FAKE_STATE/$tag.body"
  done
  : >"$FAKE_STATE/deleted"
  : >"$FAKE_STATE/edited"
}

_run() { bash "$SCRIPT" "$@" --repo test-owner/test-repo 2>&1; }

_journal() { tr '\n' ' ' <"$FAKE_STATE/$1" | sed 's/ *$//'; }

# --- Test 1: an incomplete candidate is deleted, a complete one is not ---
_seed
FAKE_DELETE_FAILS=0 out="$(_run 3.0.0)"
_assert_eq "the incomplete candidate is deleted" "3.0.0rc3" "$(_journal deleted)"
_assert_eq "nothing was annotated -- the delete succeeded" "" "$(_journal edited)"
_assert_contains "the complete candidate is reported as left alone" \
  "3.0.0rc2 carries both tarballs -- left alone" "$out"

# --- Test 2: another line's candidates and the release itself are never touched ---
_seed
FAKE_DELETE_FAILS=0 _run 3.0.0 >/dev/null
_assert_contains "the other line's candidate survives" "3.1.0rc1" "$(cat "$FAKE_STATE/tags")"
_assert_contains "the release itself survives" "3.0.0" "$(cat "$FAKE_STATE/tags")"
_assert_eq "only this line's incomplete candidate was deleted" "3.0.0rc3" "$(_journal deleted)"

# --- Test 3: delete refused (immutable) -> the notes carry the banner ---
_seed
export FAKE_DELETE_FAILS=1
out="$(_run 3.0.0)"
unset FAKE_DELETE_FAILS
_assert_eq "nothing was deleted" "" "$(_journal deleted)"
_assert_eq "the leftover was annotated instead" "3.0.0rc3" "$(_journal edited)"
BODY="$(cat "$FAKE_STATE/3.0.0rc3.body")"
_assert_contains "the banner says superseded" "Superseded -- do not install" "$BODY"
_assert_contains "the banner names a missing asset" "nyxgpt-api-3.0.0rc3.tar.gz" "$BODY"
_assert_contains "the original notes are kept" "original notes for 3.0.0rc3" "$BODY"

# --- Test 4: neither delete nor edit works -> a warning, not a failure ---
# A leftover nobody can retire must never block the next candidate.
_seed
export FAKE_DELETE_FAILS=1 FAKE_EDIT_FAILS=1
out="$(_run 3.0.0)"
rc=$?
unset FAKE_DELETE_FAILS FAKE_EDIT_FAILS
_assert_eq "the sweep still succeeds" "0" "$rc"
_assert_contains "and says so out loud" "could be neither deleted nor annotated" "$out"

# --- Test 5: --dry-run mutates nothing ---
_seed
out="$(_run 3.0.0 --dry-run)"
_assert_eq "dry run deletes nothing" "" "$(_journal deleted)"
_assert_eq "dry run edits nothing" "" "$(_journal edited)"
_assert_contains "dry run reports what it would do" "DRY-RUN: 3.0.0rc3 is missing" "$out"

# --- Test 6: a half-complete release counts as incomplete ---
# The web tarball is the one that failed to upload on run 31663088319.
_seed
echo "nyxgpt-api-3.0.0rc3.tar.gz" >"$FAKE_STATE/3.0.0rc3.assets"
FAKE_DELETE_FAILS=0 _run 3.0.0 >/dev/null
_assert_eq "one tarball is not enough" "3.0.0rc3" "$(_journal deleted)"

# --- Test 7: usage guardrails ---
_seed
bash "$SCRIPT" --repo test-owner/test-repo >/dev/null 2>&1
_assert_eq "a release line is required" "2" "$?"
bash "$SCRIPT" 3.0.0rc3 --repo test-owner/test-repo >/dev/null 2>&1
_assert_eq "a candidate version is not a release line" "2" "$?"
GITHUB_REPOSITORY="" bash "$SCRIPT" 3.0.0 >/dev/null 2>&1
_assert_eq "a repository is required" "2" "$?"

echo
if [[ $FAILURES -eq 0 ]]; then
  echo "All supersede-incomplete-rc-releases tests passed."
  exit 0
fi
echo "$FAILURES test(s) failed." >&2
exit 1
