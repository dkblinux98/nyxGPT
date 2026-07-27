#!/usr/bin/env bash
set -uo pipefail

# tests/test_snapshot_safe_add.sh
#
# Regression coverage for scripts/agents/snapshot_safe_add.sh (#3370): the
# developer workflow's snapshot step used to run a blanket `git add -A`,
# which on #3352 swept an untracked .venv-agent/ into a feature-branch
# commit (7,032 files / ~1.8M lines) and made verification crawl for 30+
# minutes. This exercises the replacement script against a throwaway git
# repo to confirm an untracked virtualenv is never staged, allowlisted
# source changes are staged, and the size guard trips (and unstages
# everything) on an oversized snapshot.
#
# Usage: bash tests/test_snapshot_safe_add.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/snapshot_safe_add.sh"

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
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected to find '$needle'" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: did not expect to find '$needle'" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_make_repo() {
  local dir
  dir="$(mktemp -d)"
  (
    cd "$dir"
    git init -q
    git config user.email test@example.com
    git config user.name "test"
    mkdir -p src tests
    echo "print('hi')" > src/foo.py
    echo "def test(): pass" > tests/test_foo.py
    git add -A
    git commit -q -m init
  )
  echo "$dir"
}

# --- Test 1: untracked venv is skipped; allowlisted src changes are staged ---
REPO1="$(_make_repo)"
(
  cd "$REPO1"
  echo "print('hi2')" >> src/foo.py
  echo "print('new')" > src/bar.py
  mkdir -p .venv-agent/lib/site-packages
  for i in $(seq 1 20); do echo "junk $i" > ".venv-agent/lib/site-packages/pkg$i.py"; done
  mkdir -p node_modules/some-pkg
  echo "junk" > node_modules/some-pkg/index.js

  bash "$SCRIPT" >/tmp/snapshot_test1_stdout.log 2>&1
  echo "exit=$?" >> /tmp/snapshot_test1_stdout.log
)
STAGED1="$(cd "$REPO1" && git diff --cached --name-only)"
_assert_contains "modified tracked src file is staged" "$STAGED1" "src/foo.py"
_assert_contains "new file under allowlisted src/ is staged" "$STAGED1" "src/bar.py"
_assert_not_contains "untracked venv is NOT staged" "$STAGED1" ".venv-agent"
_assert_not_contains "untracked node_modules is NOT staged" "$STAGED1" "node_modules"
rm -rf "$REPO1"

# --- Test 2: untracked path outside the allowlist (not junk, just unlisted) is skipped ---
REPO2="$(_make_repo)"
(
  cd "$REPO2"
  mkdir -p some_new_top_level_dir
  echo "stuff" > some_new_top_level_dir/file.txt
  bash "$SCRIPT" >/tmp/snapshot_test2_stdout.log 2>&1
)
STAGED2="$(cd "$REPO2" && git diff --cached --name-only)"
_assert_not_contains "unlisted top-level dir is NOT staged" "$STAGED2" "some_new_top_level_dir"
rm -rf "$REPO2"

# --- Test 3: size guard trips on an oversized snapshot and unstages everything ---
REPO3="$(_make_repo)"
(
  cd "$REPO3"
  mkdir -p src/generated
  for i in $(seq 1 10); do echo "line $i" > "src/generated/file$i.py"; done
)
set +e
(cd "$REPO3" && SNAPSHOT_MAX_FILES=2 SNAPSHOT_MAX_LINES=50000 bash "$SCRIPT" >/tmp/snapshot_test3_stdout.log 2>&1)
GUARD_EXIT=$?
set -e
_assert_eq "size guard exits non-zero" "1" "$GUARD_EXIT"
STAGED3="$(cd "$REPO3" && git diff --cached --name-only)"
_assert_eq "size guard leaves nothing staged" "" "$STAGED3"
GUARD_LOG="$(cat /tmp/snapshot_test3_stdout.log)"
_assert_contains "size guard message names the trip" "$GUARD_LOG" "Snapshot guard tripped"
rm -rf "$REPO3"

if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES assertion(s) failed" >&2
  exit 1
fi
echo "All assertions passed"
