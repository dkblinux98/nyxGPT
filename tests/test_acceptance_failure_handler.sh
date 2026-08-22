#!/usr/bin/env bash
set -uo pipefail

# tests/test_acceptance_failure_handler.sh
# Executed evidence (ledger D-006) for the acceptance-command handlers after
# #3999 implemented owner standard D-042.
#
# The step body is EXTRACTED FROM THE WORKFLOW YAML and run end to end
# against a stub `gh` and a stub create_issue.sh, so the code that runs here
# is the code GitHub runs -- not a copy that can drift. Only the writes are
# shimmed (set_issue_status, issue_comment, assign_issue_verified,
# assign_and_trigger_developer); every decision under test -- the Path A/B
# discriminator, the unique-failure count, the reopen -- is the real code
# from scripts/agents/lib/gh_project.sh.
#
# Four things are pinned:
#
#   1. THE SILENT-LOSS BUG (runs 32547268563 and 32583495118, 2026-08-22).
#      The unique-failure count ran `... | grep -E '^[0-9]+$' | sort -u |
#      wc -l`, and `grep` exits 1 when it matches NOTHING. Under
#      `set -o pipefail` that is the pipeline's status, so `set -e` killed
#      the step -- silently, with no output at all -- for every FIRST
#      failure filed against an issue. No derived issue, no blocking edge,
#      the owner's report acted on by nothing. Case 1 files the first
#      failure against a feature and requires the handler to survive it;
#      case 1b runs the retired form and shows it dying with empty output,
#      because a check that cannot fail proves nothing (#3775).
#
#   2. REOPEN ON AN ORIGINAL (D-042 (a)). The commented issue is reopened,
#      assigned to the scrummaster and parked in Acceptance Failed, and the
#      derived issue is the only thing dispatched.
#
#   3. NO REOPEN-AS-ORIGINAL ON A FAILURE ISSUE. `@acceptance-failure` on a
#      handler-filed failure issue keeps its documented behavior: that same
#      issue is reopened for REWORK and no derived issue is created. #3850
#      and #3862 both depend on this path.
#
#   4. THE SAME TREATMENT ON `@improvement`. The two commands differ only in
#      label and copy, so a fix to one that skips the other re-creates the
#      divergence.
#
# Usage: bash tests/test_acceptance_failure_handler.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAILURES=0

_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' unexpectedly found in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "[FAIL] $desc: expected '$expected', got '$actual'" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/repo/scripts/agents/lib"

# --- Extract a workflow step's `run:` body -----------------------------
# Deliberately not a YAML library: the point is to run the exact text the
# workflow carries, and a dependency-free extractor keeps this test runnable
# wherever bash and python3 are.
cat > "$TMP/extract.py" <<'PY'
import sys

path, step = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().splitlines()
i = next(n for n, ln in enumerate(lines) if ln.strip() == f"- name: {step}")
j = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
out = []
for ln in lines[j + 1:]:
    if ln.strip() and not ln.startswith(" " * 10):
        break
    out.append(ln[10:])
body = "\n".join(out)
# The one workflow-context interpolation in these steps.
body = body.replace("${{ github.event.issue.number }}", '${COMMENTED_INPUT}')
sys.stdout.write(body + "\n")
PY

# --- Fixture repo ------------------------------------------------------
# Real scripts, one shimmed lib. The shim sources the real library and then
# replaces only the calls that would write to GitHub, so the handler's own
# decisions run untouched.
cat > "$TMP/repo/scripts/agents/lib/gh_project.sh" <<SHIM
# shellcheck disable=SC1090
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

require_gh_auth() { :; }
iteration_active_title() { echo ""; }
set_issue_status() { echo "STATUS \$1 -> \$2" >>"\$ACTION_LOG"; }
issue_comment() { echo "COMMENT \$1" >>"\$ACTION_LOG"; }
assign_issue_verified() { echo "ASSIGN \$1 -> \$2" >>"\$ACTION_LOG"; return 0; }
assign_and_trigger_developer() { echo "DISPATCH \$1" >>"\$ACTION_LOG"; }
SHIM

cat > "$TMP/repo/scripts/agents/create_issue.sh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
{
  echo "CREATE_ISSUE $*"
} >>"$ACTION_LOG"
echo "9001"
STUB
chmod +x "$TMP/repo/scripts/agents/create_issue.sh"

cat > "$TMP/repo/config.ini" <<'EOF'
REPO_OWNER=test-owner
REPO_NAME=test-repo
PROJECT_OWNER=test-owner
PROJECT_NUMBER=1
DEV_AGENT=dev-agent
REVIEW_AGENT=review-agent
SCRUM_AGENT=scrum-agent
HUMAN_OWNER=owner
STATUS_FIELD=Status
STATUS_BACKLOG=Backlog
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_FOR_RELEASE=For Release
STATUS_ACCEPTANCE_TESTING=Acceptance Testing
STATUS_ACCEPTANCE_FAILED=Acceptance Failed
RELEASE_BRANCH=v3.0.0
EOF

# --- Stub `gh` ---------------------------------------------------------
# $TMP/issues.json  number -> {state, title, labels, body}
# $TMP/deps.json    {"blocking": {...}, "blocked_by": {...}}
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
TMP="$STUB_TMP"

sub="${1:-}"
if [[ "$sub" == "issue" ]]; then
  echo "GH_ISSUE $*" >>"$ACTION_LOG"
  exit 0
fi

path=""
jq_filter=""
method=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i + 1))]}"; ((i++)) ;;
    --jq) jq_filter="${args[$((i + 1))]}"; ((i++)) ;;
    -f | -F) ((i++)) ;;
    repos/*) path="${args[$i]}" ;;
  esac
done

_emit() { if [[ -n "$jq_filter" ]]; then jq -r "$jq_filter"; else cat; fi; }

if [[ "$method" == "POST" ]]; then
  echo "GH_POST $path" >>"$ACTION_LOG"
  echo '{}'
  exit 0
fi

case "$path" in
  *"/dependencies/blocking")
    n="${path#*issues/}"; n="${n%%/*}"
    jq -c --arg n "$n" '[(.blocking[$n] // [])[] | {number: ., state: "open"}]' "$TMP/deps.json" | _emit
    ;;
  *"/dependencies/blocked_by")
    n="${path#*issues/}"; n="${n%%/*}"
    jq -c --arg n "$n" '[(.blocked_by[$n] // [])[] | {number: ., state: "open"}]' "$TMP/deps.json" | _emit
    ;;
  *"/issues?labels="*)
    echo '[]' | _emit
    ;;
  *"/issues/"*)
    n="${path#*issues/}"; n="${n%%/*}"; n="${n%%\?*}"
    jq -c --arg n "$n" '.[$n] // {state: "open", title: "unknown", labels: [], body: ""}' "$TMP/issues.json" \
      | jq -c --arg n "$n" '. + {number: ($n | tonumber), id: ($n | tonumber), labels: [.labels[] | {name: .}]}' \
      | _emit
    ;;
  *)
    echo '{}' | _emit
    ;;
esac
STUB
chmod +x "$TMP/bin/gh"

_run_handler() {
  # $1 = workflow file, $2 = step name, $3 = commented issue, $4 = comment body
  local wf="$1" step="$2" commented="$3" body="$4"
  python3 "$TMP/extract.py" "$ROOT_DIR/$wf" "$step" > "$TMP/step.sh"
  : >"$TMP/actions.log"
  : >"$TMP/gh_output"
  (
    cd "$TMP/repo" || exit 1
    STUB_TMP="$TMP" \
    ACTION_LOG="$TMP/actions.log" \
    PATH="$TMP/bin:$PATH" \
    NYXGPT_CONFIG_FILE="$TMP/repo/config.ini" \
    RUNNER_TEMP="$TMP" \
    GITHUB_OUTPUT="$TMP/gh_output" \
    COMMENTED_INPUT="$commented" \
    COMMENT_BODY="$body" \
    COMMENT_URL="https://example.invalid/c/1" \
      bash "$TMP/step.sh" 2>&1
  )
}

# =====================================================================
# Case 1: the FIRST acceptance failure against a feature
# =====================================================================
cat > "$TMP/issues.json" <<'EOF'
{"3835": {"state": "closed", "title": "feat: dev mode and artifact path", "labels": ["Agent"], "body": ""}}
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {}, "blocked_by": {}}
EOF

out="$(_run_handler .github/workflows/handle_acceptance_failure.yml "Handle acceptance failure" 3835 "@acceptance-failure the install mode is wrong")"
rc=$?
log="$(cat "$TMP/actions.log")"

_assert_eq "the first failure against an issue does not kill the handler" "0" "$rc"
_assert_contains "it counts zero existing failures and files number 1" \
  "$out" "Existing acceptance-failure issues against #3835: 0 -> filing unique failure 1"
_assert_contains "the derived issue is created" "$log" "CREATE_ISSUE"
_assert_contains "mode=filed is reported" "$(cat "$TMP/gh_output")" "mode=filed"

# --- D-042 (a): the ORIGINAL is reopened, assigned and parked ---------
_assert_contains "the original is reopened" "$log" "GH_ISSUE issue reopen 3835"
_assert_contains "the original is assigned to the scrummaster" "$log" "ASSIGN 3835 -> scrum-agent"
_assert_contains "the original is parked in the holding lane" "$log" "STATUS 3835 -> Acceptance Failed"
_assert_not_contains "nothing is dispatched against the original" "$log" "DISPATCH 3835"

# =====================================================================
# Case 1b: the retired count pipeline, to show the bug was real
# =====================================================================
# Same fixture, the pre-#3999 expression. It must die -- and die SILENTLY,
# which is what made two owner reports vanish on 2026-08-22.
cat > "$TMP/retired.sh" <<'RETIRED'
set -euo pipefail
NATIVE_AFS=""
PROSE_AFS=""
EXISTING="$(printf '%s\n%s\n' "$NATIVE_AFS" "$PROSE_AFS" \
  | grep -E '^[0-9]+$' | sort -u | wc -l | tr -d ' ')"
echo "reached the line after the count: EXISTING=$EXISTING"
RETIRED
retired_out="$(bash "$TMP/retired.sh" 2>&1)"
retired_rc=$?
_assert_eq "the retired count pipeline exits non-zero on an empty match" "1" "$retired_rc"
_assert_eq "and prints nothing at all -- the silent loss" "" "$retired_out"

# =====================================================================
# Case 2: @acceptance-failure ON a handler-filed FAILURE issue
# =====================================================================
# #3850's path: a rework label AND a native blocking edge -> reopen THAT
# issue for rework, no derived issue, no reopen-as-original.
cat > "$TMP/issues.json" <<'EOF'
{"3850": {"state": "closed", "title": "bug: acceptance failure 1 for #3854", "labels": ["Acceptance Failure"], "body": ""},
 "3854": {"state": "closed", "title": "feat: brew install", "labels": ["Agent"], "body": ""}}
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3850": [3854]}, "blocked_by": {"3854": [3850]}}
EOF

out="$(_run_handler .github/workflows/handle_acceptance_failure.yml "Handle acceptance failure" 3850 "@acceptance-failure the fix did not hold")"
rc=$?
log="$(cat "$TMP/actions.log")"

_assert_eq "the rework path still succeeds" "0" "$rc"
_assert_contains "the failure issue itself is reopened" "$log" "GH_ISSUE issue reopen 3850"
_assert_contains "mode=reopened is reported" "$(cat "$TMP/gh_output")" "mode=reopened"
_assert_not_contains "no derived issue is created for a re-tested failure" "$log" "CREATE_ISSUE"
_assert_not_contains "and the issue it blocks is never touched" "$log" "3854"

# =====================================================================
# Case 3: @improvement gets the same D-042 treatment
# =====================================================================
cat > "$TMP/issues.json" <<'EOF'
{"3835": {"state": "closed", "title": "feat: dev mode and artifact path", "labels": ["Agent"], "body": ""}}
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {}, "blocked_by": {}}
EOF

out="$(_run_handler .github/workflows/handle_improvement.yml "File the improvement" 3835 "@improvement default the instance type")"
rc=$?
log="$(cat "$TMP/actions.log")"

_assert_eq "the improvement handler succeeds" "0" "$rc"
_assert_contains "the improvement issue is created" "$log" "CREATE_ISSUE"
_assert_contains "@improvement reopens the original too" "$log" "GH_ISSUE issue reopen 3835"
_assert_contains "and assigns it to the scrummaster" "$log" "ASSIGN 3835 -> scrum-agent"
_assert_contains "and parks it in the holding lane" "$log" "STATUS 3835 -> Acceptance Failed"
_assert_contains "the reopen is reported to the acknowledgement step" \
  "$(cat "$TMP/gh_output")" "reopened=1"

# =====================================================================
# Case 4: @improvement ON a failure issue reopens nothing
# =====================================================================
# The improvement belongs to the issue THAT one blocks; reopening that would
# be reopening something the owner never marked.
cat > "$TMP/issues.json" <<'EOF'
{"3850": {"state": "closed", "title": "bug: acceptance failure 1 for #3854", "labels": ["Acceptance Failure"], "body": ""},
 "3854": {"state": "closed", "title": "feat: brew install", "labels": ["Agent"], "body": ""}}
EOF
cat > "$TMP/deps.json" <<'EOF'
{"blocking": {"3850": [3854]}, "blocked_by": {"3854": [3850]}}
EOF

out="$(_run_handler .github/workflows/handle_improvement.yml "File the improvement" 3850 "@improvement widen the check")"
log="$(cat "$TMP/actions.log")"
_assert_not_contains "no issue is reopened when the command lands on a failure issue" \
  "$log" "issue reopen"
_assert_contains "and the acknowledgement says so" "$(cat "$TMP/gh_output")" "reopened=0"

# =====================================================================
# Case 5: both handlers carry a loud failure path
# =====================================================================
# Static, deliberately: the step only runs `if: failure()`, so the thing
# worth pinning is that it exists, fires on failure, and reaches the owner
# on the #3695 DM channel rather than only a red run nobody watches.
for wf in handle_acceptance_failure handle_improvement; do
  wf_text="$(cat "$ROOT_DIR/.github/workflows/${wf}.yml")"
  _assert_contains "${wf}: has a failure-triggered alert step" "$wf_text" "if: failure()"
  _assert_contains "${wf}: the alert uses the #3695 owner DM path" \
    "$wf_text" "notify_human_escalation"
  _assert_contains "${wf}: the alert names the reporting comment" "$wf_text" "COMMENT_URL"
  _assert_contains "${wf}: the alert names the failing run" "$wf_text" "RUN_URL"
  _assert_contains "${wf}: the job can reach Slack" "$wf_text" "SLACK_BOT_TOKEN"
done

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
