#!/usr/bin/env bash
set -uo pipefail

# tests/test_admin_set_fields.sh
#
# Which question does this suite answer? (#3811, ledger V-043)
#
#   "When the board read FAILS, does `admin_set_fields.sh` say so — or does it
#    report a clean run in which it wrote nothing?"
#
# `item_id_for_content` used to pipe `graphql` into `jq`. A pipeline reports
# its LAST segment's status, so a failed GraphQL read came back as empty
# output and exit 0 — which this script's contract reads as "the item is not
# on the project board". The owner would then see
#
#   [admin-fields] #123 not on the project board — board fields skipped
#   [admin-fields] Done — 1 item(s) fully updated, 0 with failures.
#
# for a batch that never touched the board at all, and the script would exit
# 0. That is the whole defect: not a crash, a *successful-looking* no-op.
#
# Inspection cannot settle this — the behaviour depends on how bash propagates
# a non-zero status out of a command substitution used as an `if` condition
# under `set -e`, which is exactly the class of thing this repo has been wrong
# about before (V-043's `_die` half). So the suite runs the real script.
#
# Both directions are proved, per the macos-brew-smoke.yml fault-injection
# template: a copy of the script with the fix reverted to the piped form must
# report the clean skip and exit 0 (the defect reproduces on demand, so the
# pass is not luck), while the shipped script fails loud — and a genuine "not
# on the board" still skips quietly and exits 0, so the new branch cannot
# become a blanket "everything is an error".
#
# Usage: bash tests/test_admin_set_fields.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/admin_set_fields.sh"

FAILURES=0
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

_ok() { echo "[ok] $1"; }
_fail() {
  echo "[FAIL] $1" >&2
  [[ -n "${2:-}" ]] && echo "$2" >&2
  FAILURES=$((FAILURES + 1))
}

_assert_eq() {
  local desc="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then _ok "$desc"; else
    _fail "$desc: expected '$expected', got '$actual'"
  fi
}

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then _ok "$desc"; else
    _fail "$desc: expected output to contain '$needle'" "$haystack"
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then _ok "$desc"; else
    _fail "$desc: expected output NOT to contain '$needle'" "$haystack"
  fi
}

# ---- config + stub wiring ----------------------------------------------
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
export NYXGPT_CONFIG_FILE="$CONFIG"

# A `gh` stub that answers each GraphQL query by shape. Two knobs drive the
# cases: whether the item lookup FAILS (the rate-limited / transient read) and
# whether it succeeds but finds nothing (genuinely not on the board).
BIN="$TMP_ROOT/bin"
mkdir -p "$BIN"
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail

if [[ "${1:-}" == "auth" ]]; then exit 0; fi

query=""
for arg in "$@"; do
  case "$arg" in
    query=*) query="${arg#query=}" ;;
  esac
done

case "$query" in
  *projectV2\(number*)
    echo '{"data":{"user":{"projectV2":{"id":"PVT_test"}}}}' ;;
  *fields\(first*)
    echo '{"data":{"node":{"fields":{"nodes":[
      {"__typename":"ProjectV2SingleSelectField","id":"F_status","name":"Status",
       "options":[{"id":"OPT_backlog","name":"Backlog"}]}
    ]}}}}' ;;
  *issueOrPullRequest*)
    if [[ "${GH_STUB_ITEM_READ_FAILS:-0}" == "1" ]]; then
      echo "API rate limit exceeded" >&2
      exit 1
    fi
    if [[ "${GH_STUB_ITEM_ON_BOARD:-1}" == "1" ]]; then
      echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[
        {"id":"PVTI_test","project":{"id":"PVT_test"}}
      ]}}}}}'
    else
      echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[]}}}}}'
    fi ;;
  *updateProjectV2ItemFieldValue*)
    echo "field-write" >> "${GH_STUB_DIR}/mutations.log"
    echo '{"data":{"updateProjectV2ItemFieldValue":{"projectV2Item":{"id":"PVTI_test"}}}}' ;;
  *)
    echo '{"data":{}}' ;;
esac
STUB
chmod +x "$BIN/gh"
export PATH="$BIN:$PATH"

_reset_stub() {
  export GH_STUB_DIR="$TMP_ROOT/stub_$1"
  rm -rf "$GH_STUB_DIR"
  mkdir -p "$GH_STUB_DIR"
  : > "$GH_STUB_DIR/mutations.log"
}

# Runs a copy of the tool (shipped or fault-injected) over one item.
_run() {
  local script="$1"
  ITEMS=123 STATUS=Backlog DRY_RUN=false bash "$script" 2>&1
}

# ---- case 1: the happy path still writes the field ----------------------
_reset_stub case1
export GH_STUB_ITEM_READ_FAILS=0 GH_STUB_ITEM_ON_BOARD=1
out="$(_run "$SCRIPT")"; rc=$?
_assert_eq "an item on the board: exit 0" "$rc" "0"
_assert_contains "an item on the board: Status is written" "$out" "Status -> Backlog"
_assert_eq "an item on the board: one field mutation" \
  "$(wc -l < "$GH_STUB_DIR/mutations.log" | tr -d ' ')" "1"

# ---- case 2: genuinely not on the board is still a quiet skip -----------
# The counterpart case. Without it, "fail loud on a failed read" could be
# satisfied by failing on everything, which would make the tool unusable on
# the mixed batches it exists for.
_reset_stub case2
export GH_STUB_ITEM_READ_FAILS=0 GH_STUB_ITEM_ON_BOARD=0
out="$(_run "$SCRIPT")"; rc=$?
_assert_eq "not on the board: exit 0" "$rc" "0"
_assert_contains "not on the board: says so" "$out" "not on the project board"
_assert_not_contains "not on the board: no failure warning" "$out" "failed to read the project board"
_assert_eq "not on the board: nothing written" \
  "$(wc -l < "$GH_STUB_DIR/mutations.log" | tr -d ' ')" "0"

# ---- case 3: a FAILED read is reported as a failure ---------------------
_reset_stub case3
export GH_STUB_ITEM_READ_FAILS=1 GH_STUB_ITEM_ON_BOARD=1
out="$(_run "$SCRIPT")"; rc=$?
_assert_eq "failed read: non-zero exit" "$rc" "1"
_assert_contains "failed read: names the failure" "$out" "failed to read the project board"
_assert_not_contains "failed read: does NOT claim the item is off the board" \
  "$out" "not on the project board"
_assert_contains "failed read: counted as a failure" "$out" "1 with failures"
_assert_eq "failed read: nothing written" \
  "$(wc -l < "$GH_STUB_DIR/mutations.log" | tr -d ' ')" "0"

# ---- case 4: fault injection — the defect reproduces on demand ----------
# Revert `item_id_for_content` to the piped form and the caller to the `||
# true` that went with it. If this copy does NOT report a clean skip, the
# suite is not exercising the mechanism it claims to.
# The copy sources `lib/gh_project.sh` relative to its own directory, so it
# needs one — the point is to run the SHIPPED library against a reverted
# caller, not a reverted library.
mkdir -p "$TMP_ROOT/injected"
ln -sfn "$ROOT_DIR/scripts/agents/lib" "$TMP_ROOT/injected/lib"
INJECTED="$TMP_ROOT/injected/admin_set_fields_piped.sh"
python3 - "$SCRIPT" "$INJECTED" <<'PY'
import sys

src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()

fixed_read = '''  local resp
  resp="$(graphql "query(\\$owner:String!, \\$name:String!, \\$num:Int!) {'''
piped_read = '''  graphql "query(\\$owner:String!, \\$name:String!, \\$num:Int!) {'''
assert fixed_read in text, "the shipped read no longer has the form this injection reverts"
text = text.replace(fixed_read, piped_read, 1)

fixed_tail = '''  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$num")" || return 1
  echo "$resp" \\
    | jq -r'''
piped_tail = '''  }" -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F num="$num" \\
    | jq -r'''
assert fixed_tail in text
text = text.replace(fixed_tail, piped_tail, 1)

fixed_call = '''    if item_id="$(item_id_for_content "$n")"; then'''
piped_call = '''    item_id="$(item_id_for_content "$n" || true)"
    if true; then'''
assert fixed_call in text
text = text.replace(fixed_call, piped_call, 1)

open(dst, "w", encoding="utf-8").write(text)
PY

_reset_stub case4
export GH_STUB_ITEM_READ_FAILS=1 GH_STUB_ITEM_ON_BOARD=1
out="$(_run "$INJECTED")"; rc=$?
_assert_eq "WITHOUT the fix: a failed read exits 0" "$rc" "0"
_assert_contains "WITHOUT the fix: reports the item as off the board" \
  "$out" "not on the project board"
_assert_contains "WITHOUT the fix: reports a clean run" "$out" "0 with failures"

# ---- result -------------------------------------------------------------
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All admin_set_fields checks passed."
  exit 0
fi
echo "${FAILURES} check(s) failed." >&2
exit 1
