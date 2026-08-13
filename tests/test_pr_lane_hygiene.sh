#!/usr/bin/env bash
set -uo pipefail

# tests/test_pr_lane_hygiene.sh
# Regression suite for the #3742 PR lane invariant: a merged or closed PR's
# own project item ends up in the terminal "Closed" lane, never stranded in
# "In Review".
#
# Everything runs against tests/gh_stub_pr_lane.py, a stateful `gh` stub —
# Status writes are recorded and read back, so the assertions are about the
# lane the PR actually ends in, not about which API strings were emitted.
#
# Usage: bash tests/test_pr_lane_hygiene.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$ROOT_DIR/scripts/agents/lib/gh_project.sh"
MERGE_SCRIPT="$ROOT_DIR/scripts/agents/review_accept_and_merge.sh"
CLOSE_SCRIPT="$ROOT_DIR/scripts/agents/pr_close_project_status.sh"
SWEEP_SCRIPT="$ROOT_DIR/scripts/agents/reconcile_pr_lane.sh"

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
  if [[ "$actual" == "$expected" ]]; then
    _ok "$desc"
  else
    _fail "$desc: expected '$expected', got '$actual'"
  fi
}

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    _ok "$desc"
  else
    _fail "$desc: expected output to contain '$needle'" "$haystack"
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    _ok "$desc"
  else
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

BIN="$TMP_ROOT/bin"
mkdir -p "$BIN"
cat > "$BIN/gh" <<EOF
#!/usr/bin/env bash
exec python3 "$ROOT_DIR/tests/gh_stub_pr_lane.py" "\$@"
EOF
chmod +x "$BIN/gh"
export PATH="$BIN:$PATH"

# Fresh stub state per case: fixtures and the recorded Status writes.
_reset_stub() {
  export GH_STUB_DIR="$TMP_ROOT/stub_$1"
  rm -rf "$GH_STUB_DIR"
  mkdir -p "$GH_STUB_DIR"
}

# Reads a PR's board Status back through the production helper.
_pr_lane() {
  bash -c "source '$LIB'; load_config; pr_status $1" 2>/dev/null
}

# ---- case 1: close_pr_project_item moves a card out of In Review --------
_reset_stub case1
cat > "$GH_STUB_DIR/pr_items.json" <<'EOF'
{"101": {"item_id": "PVTI_pr101", "status": "In Review"}}
EOF

_assert_eq "starts in the review lane" "$(_pr_lane 101)" "In Review"

out="$(bash -c "source '$LIB'; load_config; close_pr_project_item 101" 2>&1)"
rc=$?
_assert_eq "close_pr_project_item succeeds (rc)" "$rc" "0"
_assert_eq "PR card ends in Closed" "$(_pr_lane 101)" "Closed"
_assert_not_contains "no warning on the happy path" "$out" "[warning]"

# ---- case 2: re-stamping an already-Closed card writes nothing ----------
before="$(wc -l < "$GH_STUB_DIR/mutations.log" | tr -d ' ')"
bash -c "source '$LIB'; load_config; close_pr_project_item 101" >/dev/null 2>&1
after="$(wc -l < "$GH_STUB_DIR/mutations.log" | tr -d ' ')"
_assert_eq "idempotent: no second mutation for an already-Closed card" "$after" "$before"

# ---- case 3: a PR with no project item yet is added, then stamped -------
_reset_stub case3
echo '{"202": {"item_id": null, "status": null}}' > "$GH_STUB_DIR/pr_items.json"
bash -c "source '$LIB'; load_config; set_pr_status 202 Closed" >/dev/null 2>&1
_assert_eq "PR missing from the board is added to it" \
  "$(grep -c 'PVTI_added_PR_node_202' "$GH_STUB_DIR/added.log" 2>/dev/null || echo 0)" "1"
_assert_contains "the added item is stamped Closed" \
  "$(cat "$GH_STUB_DIR/mutations.log")" "PVTI_added_PR_node_202	opt_closed	Closed"

# ---- case 4: the merge path itself leaves no PR in In Review -----------
_reset_stub case4
cat > "$GH_STUB_DIR/pr_items.json" <<'EOF'
{"303": {"item_id": "PVTI_pr303", "status": "In Review"}}
EOF
cat > "$GH_STUB_DIR/pulls.json" <<'EOF'
{"303": {"head": "feat/lane-invariant", "base": "v3.0.0", "merged": false, "state": "open"}}
EOF

merge_out="$(CI=true GITHUB_ACTIONS=true STUB_ISSUE=3742 \
  bash "$MERGE_SCRIPT" 303 3742 2>&1)"
merge_rc=$?
_assert_eq "merge flow exits 0" "$merge_rc" "0"
_assert_contains "merge flow reports the lane stamp" "$merge_out" "project item -> Closed"
_assert_eq "LANE INVARIANT: merged PR is not left in In Review" "$(_pr_lane 303)" "Closed"

# ---- case 5: dry-run merge announces the stamp without writing ---------
_reset_stub case5
cat > "$GH_STUB_DIR/pr_items.json" <<'EOF'
{"404": {"item_id": "PVTI_pr404", "status": "In Review"}}
EOF
echo '{"404": {"head": "feat/x", "base": "v3.0.0", "merged": false, "state": "open"}}' \
  > "$GH_STUB_DIR/pulls.json"
dry_out="$(bash "$MERGE_SCRIPT" --dry-run 404 3742 2>&1)"
_assert_contains "dry run plans the PR Status stamp" "$dry_out" "would: set PR #404 project Status -> 'Closed'"
_assert_eq "dry run changes nothing" "$(_pr_lane 404)" "In Review"

# ---- case 6: closed-without-merge PRs get the same treatment -----------
_reset_stub case6
cat > "$GH_STUB_DIR/pr_items.json" <<'EOF'
{"505": {"item_id": "PVTI_pr505", "status": "In Review"},
 "606": {"item_id": "PVTI_pr606", "status": "In Review"}}
EOF
cat > "$GH_STUB_DIR/pulls.json" <<'EOF'
{"505": {"head": "fix/rejected", "base": "v3.0.0", "merged": false, "state": "closed"},
 "606": {"head": "feat/still-open", "base": "v3.0.0", "merged": false, "state": "open"}}
EOF

bash "$CLOSE_SCRIPT" 505 >/dev/null 2>&1
_assert_eq "rejected (closed, unmerged) PR lands in Closed" "$(_pr_lane 505)" "Closed"

open_out="$(bash "$CLOSE_SCRIPT" 606 2>&1)"
_assert_eq "still-open PR keeps its review lane" "$(_pr_lane 606)" "In Review"
_assert_contains "open PR is explicitly skipped" "$open_out" "is open"

# ---- case 7: the sweep backfills strays and touches nothing else -------
_reset_stub case7
cat > "$GH_STUB_DIR/items_page.json" <<'EOF'
[
  {"item_id": "PVTI_a", "type": "PullRequest", "number": 701, "state": "MERGED",
   "status": "In Review", "title": "stranded merged PR"},
  {"item_id": "PVTI_b", "type": "PullRequest", "number": 702, "state": "CLOSED",
   "status": "In Progress", "title": "stranded rejected PR"},
  {"item_id": "PVTI_c", "type": "PullRequest", "number": 703, "state": "OPEN",
   "status": "In Review", "title": "PR still under review"},
  {"item_id": "PVTI_d", "type": "PullRequest", "number": 704, "state": "MERGED",
   "status": "Closed", "title": "already reconciled"},
  {"item_id": "PVTI_e", "type": "Issue", "number": 705, "status": "In Review"}
]
EOF

dry_sweep="$(DRY_RUN=true bash "$SWEEP_SCRIPT" 2>&1)"
_assert_contains "sweep finds the stranded merged PR" "$dry_sweep" "PR #701"
_assert_contains "sweep finds the stranded rejected PR" "$dry_sweep" "PR #702"
_assert_not_contains "sweep leaves open PRs alone" "$dry_sweep" "PR #703"
_assert_not_contains "sweep leaves already-Closed PRs alone" "$dry_sweep" "PR #704"
_assert_not_contains "sweep never touches issues" "$dry_sweep" "705"
_assert_contains "dry run says so" "$dry_sweep" "DRY RUN"
_assert_eq "dry run wrote nothing" \
  "$([[ -f "$GH_STUB_DIR/mutations.log" ]] && echo dirty || echo clean)" "clean"

apply_sweep="$(DRY_RUN=false bash "$SWEEP_SCRIPT" 2>&1)"
_assert_contains "apply moves both strays" "$apply_sweep" "Done — 2 moved, 0 failed."
_assert_contains "stray 1 stamped Closed" "$(cat "$GH_STUB_DIR/mutations.log")" "PVTI_a	opt_closed"
_assert_contains "stray 2 stamped Closed" "$(cat "$GH_STUB_DIR/mutations.log")" "PVTI_b	opt_closed"
_assert_not_contains "open PR untouched by apply" "$(cat "$GH_STUB_DIR/mutations.log")" "PVTI_c"
_assert_not_contains "issue untouched by apply" "$(cat "$GH_STUB_DIR/mutations.log")" "PVTI_e"

# The invariant converges: a second pass has nothing left to do.
second_pass="$(DRY_RUN=true bash "$SWEEP_SCRIPT" 2>&1)"
_assert_contains "sweep converges — nothing stranded on the second pass" \
  "$second_pass" "0 merged/closed PR card(s) to move"

# ---- case 8: narrow mode only sweeps the named lane --------------------
_reset_stub case8
cat > "$GH_STUB_DIR/items_page.json" <<'EOF'
[
  {"item_id": "PVTI_x", "type": "PullRequest", "number": 801, "state": "MERGED",
   "status": "In Review", "title": "in the named lane"},
  {"item_id": "PVTI_y", "type": "PullRequest", "number": 802, "state": "MERGED",
   "status": "Backlog", "title": "some other lane"}
]
EOF
narrow="$(SOURCE_STATUS="In Review" DRY_RUN=true bash "$SWEEP_SCRIPT" 2>&1)"
_assert_contains "narrow mode includes the named lane" "$narrow" "PR #801"
_assert_not_contains "narrow mode excludes other lanes" "$narrow" "PR #802"

# ---- summary ------------------------------------------------------------
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All PR-lane hygiene checks passed."
  exit 0
fi
echo "${FAILURES} check(s) failed." >&2
exit 1
