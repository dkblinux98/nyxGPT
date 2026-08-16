#!/usr/bin/env bash
set -uo pipefail

# tests/test_issue_hygiene.sh
# Regression suite for the #3816 invariant: on a first run over an issue,
# project hygiene must not change an already-populated field. It fills
# blanks; it never overwrites.
#
# Everything runs scripts/agents/ensure_issue_hygiene.sh (the body of
# ensure_project_hygiene.yml's add-to-project job) against
# tests/gh_stub_issue_hygiene.py, a stateful `gh` stub. Field writes are
# recorded and read back, so the assertions are about the value a field
# actually ends up holding.
#
# The cases that matter are the RACE cases. This defect's signature is a
# successful run that overwrote a good value, so a suite that only checked
# "hygiene populates an empty issue" would stay green through the bug. The
# stub therefore injects the concurrent deliberate write *between* hygiene's
# check of a field and its write of that field -- the exact window that cost
# #3814 its Status on 2026-08-16 -- and case R0 runs the same scenario
# against a copy of the script with the guard removed, to show the injection
# really does reproduce the clobber.
#
# Read counts: the race threshold is expressed in stub reads, and the read
# sequence for a fresh issue is
#   Status(report) Status(guard) Priority(report) Priority(guard)
#   Effort(report) Effort(guard) Module(report) Module(guard)
#   Sprint(report) Sprint(guard)
# A refactor that changes that sequence will trip these thresholds. That is
# not a vacuous failure mode: every race case also asserts the "a value
# appeared while hygiene was running" line, which only the guard prints, so a
# race landing too early cannot pass silently.
#
# Usage: bash tests/test_issue_hygiene.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HYGIENE="$ROOT_DIR/scripts/agents/ensure_issue_hygiene.sh"

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

# The suite is about ordering, not about waiting: skip the settle sleep.
export HYGIENE_SETTLE_SECONDS=0

BIN="$TMP_ROOT/bin"
mkdir -p "$BIN"
cat > "$BIN/gh" <<EOF
#!/usr/bin/env bash
exec python3 "$ROOT_DIR/tests/gh_stub_issue_hygiene.py" "\$@"
EOF
chmod +x "$BIN/gh"
export PATH="$BIN:$PATH"

# The unguarded variant, used only to prove the injection reproduces the
# defect: fill_project_field_if_empty and set_project_field_value take the
# same (item, field, value) arguments, so swapping the name is exactly the
# pre-#3816 "check once, write unconditionally" shape.
mkdir -p "$TMP_ROOT/unguarded"
ln -s "$ROOT_DIR/scripts/agents/lib" "$TMP_ROOT/unguarded/lib"
UNGUARDED="$TMP_ROOT/unguarded/ensure_issue_hygiene_unguarded.sh"
sed 's/fill_project_field_if_empty/set_project_field_value/g' "$HYGIENE" > "$UNGUARDED"

_reset_stub() {
  export GH_STUB_DIR="$TMP_ROOT/stub_$1"
  rm -rf "$GH_STUB_DIR"
  mkdir -p "$GH_STUB_DIR"
  cat > "$GH_STUB_DIR/milestones.json" <<'EOF'
[{"title": "Phase 6 — Enterprise Deployment Hardening (v3.0.0)", "state": "open", "due_on": "2026-09-30"}]
EOF
}

# Value a field holds in the stub after the run.
_field() { jq -r --arg f "$1" '.fields[$f] // ""' "$GH_STUB_DIR/state.json"; }
_milestone() { jq -r '.milestone // ""' "$GH_STUB_DIR/state.json"; }
_labels() { jq -r '.labels | join(",")' "$GH_STUB_DIR/state.json"; }
_mutations() { cat "$GH_STUB_DIR/mutations.log" 2>/dev/null || true; }

_write_issue() {
  cat > "$GH_STUB_DIR/issue.json"
}
_write_board() {
  cat > "$GH_STUB_DIR/board.json"
}

# ---- case 1: a fully populated issue is left completely alone ----------
_reset_stub case1
_write_issue <<'EOF'
{"number": 3814, "title": "bug: web UI crashes", "body": "web tier",
 "labels": ["Acceptance Failure"], "milestone": "Phase 5"}
EOF
_write_board <<'EOF'
{"item_id": "PVTI_3814",
 "fields": {"Status": "Acceptance Failed", "Priority": "P0 - Critical",
            "Effort": "L", "Module": "sre", "Sprint": "Sprint 7"}}
EOF

out="$(bash "$HYGIENE" 3814 2>&1)"
rc=$?
_assert_eq "populated issue: exits 0" "$rc" "0"
_assert_eq "NO-CLOBBER: Status untouched" "$(_field Status)" "Acceptance Failed"
_assert_eq "NO-CLOBBER: Priority untouched" "$(_field Priority)" "P0 - Critical"
_assert_eq "NO-CLOBBER: Effort untouched" "$(_field Effort)" "L"
_assert_eq "NO-CLOBBER: Module untouched" "$(_field Module)" "sre"
_assert_eq "NO-CLOBBER: Sprint untouched" "$(_field Sprint)" "Sprint 7"
_assert_eq "NO-CLOBBER: Milestone untouched" "$(_milestone)" "Phase 5"
_assert_eq "NO-CLOBBER: label untouched" "$(_labels)" "Acceptance Failure"
_assert_eq "NO-CLOBBER: not a single field write was issued" "$(_mutations)" ""
_assert_eq "no issue edit was issued" \
  "$([[ -f "$GH_STUB_DIR/issue_edit.log" ]] && echo dirty || echo clean)" "clean"
_assert_eq "no redundant hygiene comment" \
  "$([[ -f "$GH_STUB_DIR/comments.log" ]] && echo dirty || echo clean)" "clean"
_assert_contains "reports the existing Status" "$out" "Status: already 'Acceptance Failed'"

# ---- case 2: a genuinely empty issue is still fully populated ----------
_reset_stub case2
_write_issue <<'EOF'
{"number": 3900, "title": "feat: add a REST endpoint", "body": "api work",
 "labels": [], "milestone": null}
EOF
_write_board <<'EOF'
{"item_id": null, "fields": {}}
EOF

out="$(bash "$HYGIENE" 3900 2>&1)"
rc=$?
_assert_eq "empty issue: exits 0" "$rc" "0"
_assert_contains "board state: reported as not-yet-on-the-board" \
  "$out" "not on the project board yet"
_assert_eq "empty issue is added to the board" \
  "$(grep -c . "$GH_STUB_DIR/added.log" 2>/dev/null || echo 0)" "1"
_assert_eq "Status filled" "$(_field Status)" "Backlog"
_assert_eq "Priority filled" "$(_field Priority)" "P1 - High"
_assert_eq "Effort filled" "$(_field Effort)" "XS"
_assert_eq "Module filled from keywords" "$(_field Module)" "api"
_assert_eq "Sprint filled with the active iteration" "$(_field Sprint)" "Sprint 8"
_assert_eq "Milestone filled with the open milestone" \
  "$(_milestone)" "Phase 6 — Enterprise Deployment Hardening (v3.0.0)"
_assert_eq "Feature label applied" "$(_labels)" "Feature"
_assert_contains "hygiene comments what it filled" \
  "$(cat "$GH_STUB_DIR/comments.log")" "filled missing fields"

# ---- case 3: on the board with no Status is not the same as brand new --
_reset_stub case3
_write_issue <<'EOF'
{"number": 3901, "title": "chore: tidy the CLI", "body": "command line",
 "labels": ["Feature"], "milestone": null}
EOF
_write_board <<'EOF'
{"item_id": "PVTI_3901", "fields": {"Priority": "P2 - Medium"}}
EOF

out="$(bash "$HYGIENE" 3901 2>&1)"
_assert_contains "board state: on the board, no Status, reported distinctly" \
  "$out" "already on the board with no Status"
_assert_not_contains "board state: not confused with a brand-new card" \
  "$out" "not on the project board yet"
_assert_eq "existing card is not re-added" \
  "$([[ -f "$GH_STUB_DIR/added.log" ]] && echo dirty || echo clean)" "clean"
_assert_eq "empty Status on an existing card is still filled" "$(_field Status)" "Backlog"
_assert_eq "NO-CLOBBER: the populated Priority survives" "$(_field Priority)" "P2 - Medium"
_assert_not_contains "no Priority write was issued" "$(_mutations)" "Priority"

# ---- case R0 (fault injection): without the guard, the race clobbers ---
# Proves the injection reproduces the real defect, so cases R1-R3 below are
# not passing vacuously.
_reset_stub caseR0
_write_issue <<'EOF'
{"number": 3814, "title": "bug: hygiene overwrites fields", "body": "board",
 "labels": ["Acceptance Failure"], "milestone": "Phase 5"}
EOF
_write_board <<'EOF'
{"item_id": null, "fields": {}}
EOF
cat > "$GH_STUB_DIR/race.json" <<'EOF'
{"after_reads": 1, "fields": {"Status": "Acceptance Failed"}}
EOF

bash "$UNGUARDED" 3814 >/dev/null 2>&1
_assert_eq "REPRODUCED: the unguarded write clobbers the deliberate Status" \
  "$(_field Status)" "Backlog"

# ---- case R1: the guard keeps a Status that lands mid-run --------------
_reset_stub caseR1
_write_issue <<'EOF'
{"number": 3814, "title": "bug: hygiene overwrites fields", "body": "board",
 "labels": ["Acceptance Failure"], "milestone": "Phase 5"}
EOF
_write_board <<'EOF'
{"item_id": null, "fields": {}}
EOF
cat > "$GH_STUB_DIR/race.json" <<'EOF'
{"after_reads": 1, "fields": {"Status": "Acceptance Failed"}}
EOF

out="$(bash "$HYGIENE" 3814 2>&1)"
rc=$?
_assert_eq "race on Status: exits 0" "$rc" "0"
_assert_eq "the injected write really did land" \
  "$([[ -f "$GH_STUB_DIR/race.log" ]] && echo yes || echo no)" "yes"
_assert_eq "NO-CLOBBER: the deliberate Status survives the race" \
  "$(_field Status)" "Acceptance Failed"
_assert_not_contains "no Status write was issued" "$(_mutations)" "Status"
_assert_contains "the re-read guard is what skipped it" \
  "$out" "Status: a value appeared while hygiene was running"
_assert_not_contains "Status is not reported as filled" \
  "$(cat "$GH_STUB_DIR/comments.log" 2>/dev/null || true)" "Status: Backlog"

# ---- case R2: same for a non-Status field (Priority) -------------------
_reset_stub caseR2
_write_issue <<'EOF'
{"number": 3815, "title": "feat: api endpoint", "body": "api",
 "labels": ["Feature"], "milestone": "Phase 5"}
EOF
_write_board <<'EOF'
{"item_id": null, "fields": {}}
EOF
# Reads 1-2 are Status (report, guard); the deliberate Priority lands after
# read 3 (Priority's report read) and before read 4 (its guard read).
cat > "$GH_STUB_DIR/race.json" <<'EOF'
{"after_reads": 3, "fields": {"Priority": "P0 - Critical"}}
EOF

out="$(bash "$HYGIENE" 3815 2>&1)"
_assert_eq "NO-CLOBBER: the deliberate Priority survives the race" \
  "$(_field Priority)" "P0 - Critical"
_assert_not_contains "no Priority write was issued" "$(_mutations)" "Priority"
_assert_contains "the re-read guard is what skipped it" \
  "$out" "Priority: a value appeared while hygiene was running"
_assert_eq "the rest of the issue is still populated" "$(_field Status)" "Backlog"

# ---- case R3: same for Milestone, which is not a project field ---------
_reset_stub caseR3
_write_issue <<'EOF'
{"number": 3816, "title": "bug: project hygiene overwrites fields", "body": "board",
 "labels": ["Acceptance Failure"], "milestone": null}
EOF
# Already on the board, so no node_id lookup: the issue reads are then
# 1 = title/body, 2 = labels, 3 = the milestone check, 4 = the re-read taken
# immediately before `gh issue edit --milestone`.
_write_board <<'EOF'
{"item_id": "PVTI_3816", "fields": {}}
EOF
cat > "$GH_STUB_DIR/race.json" <<'EOF'
{"after_issue_reads": 3, "milestone": "Phase 5"}
EOF

out="$(bash "$HYGIENE" 3816 2>&1)"
_assert_eq "NO-CLOBBER: the deliberate Milestone survives the race" \
  "$(_milestone)" "Phase 5"
_assert_eq "no milestone edit was issued" \
  "$(grep -c -- '--milestone' "$GH_STUB_DIR/issue_edit.log" 2>/dev/null || echo 0)" "0"
_assert_contains "the re-read guard is what skipped it" \
  "$out" "appeared while hygiene was running"

# ---- case 4: hygiene is idempotent — a second run writes nothing -------
_reset_stub case4
_write_issue <<'EOF'
{"number": 3902, "title": "docs: update the README", "body": "documentation",
 "labels": [], "milestone": null}
EOF
_write_board <<'EOF'
{"item_id": null, "fields": {}}
EOF

bash "$HYGIENE" 3902 >/dev/null 2>&1
before="$(_mutations | wc -l | tr -d ' ')"
out="$(bash "$HYGIENE" 3902 2>&1)"
after="$(_mutations | wc -l | tr -d ' ')"
_assert_eq "second run writes no fields" "$after" "$before"
_assert_contains "second run reports everything as already set" "$out" "leaving as-is"

# ---- summary ------------------------------------------------------------
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All issue-hygiene checks passed."
  exit 0
fi
echo "${FAILURES} check(s) failed." >&2
exit 1
