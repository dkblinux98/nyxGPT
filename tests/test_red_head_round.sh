#!/usr/bin/env bash
set -uo pipefail

# tests/test_red_head_round.sh
#
# The executed half of #4179: Phase 2 (`developer_analyze_failure.sh`) run for
# real, on the inputs it actually has mid-run.
#
# The defect it pins is not "ci_red had no arm" -- that is the instance. It is
# that Phase 2 classified the failure a SECOND time, from a source that cannot
# answer while the run is in progress, and reported the empty answer as FATAL:
#
#   * `gh run view <id> --log` downloads a zip that only exists after the run
#     completes; mid-run it returns 403. The stub `gh` below FAILS that call
#     with 403 and logs every invocation, so a script that still reached for it
#     would be caught here rather than on the next escalation.
#   * The old `case`'s default arm wrote `STATUS=FATAL` and exited 1, which the
#     workflow maps to `fix_status=FATAL` -> `escalate_fatal`. On #4138 (run
#     37722699004) that woke the owner for `retriable:ci_red`.
#
# FAULT INJECTION, both directions (the macos-brew-smoke.yml template, and
# #3753's lesson that a job exercising only the fixed path passes on every
# machine that fails to reproduce the bug):
#
#   * Phase 2 is run WITH the recorded refusal and with NOTHING recorded at
#     all. Both must continue the round; the second is the #4138 harvest.
#   * It is run over EVERY class `classify_error` can emit: no retriable class
#     may exit 1, and `fatal:auth_failure` must still exit 1 -- a script that
#     never exits fatal would pass half of this.
#   * An unrecognised class must DEFER (exit 3), which is the arm that used to
#     be FATAL.
#
# Usage: bash tests/test_red_head_round.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Resolved to an ABSOLUTE path before anything is put on PATH: the stub
# directory below shadows `python3`, and a shim that re-execs the bare name
# would exec itself forever.
PYTHON="$(command -v "${NYXGPT_TEST_PYTHON:-python3}" || echo "${NYXGPT_TEST_PYTHON:-python3}")"

FAILURES=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

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
    echo "[FAIL] $desc: expected output to contain '$needle', got: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[ok] $desc"
  else
    echo "[FAIL] $desc: expected output NOT to contain '$needle', got: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# --- Fixtures ---------------------------------------------------------------

cat > "$TMP/config.ini" <<'EOF'
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
RELEASE_BRANCH=v9.9.9
EOF

# A `gh` that behaves like the real one does MID-RUN: the run-log archive is a
# 403, and every call is recorded so the assertions can be about what the
# script asked for, not only about what it concluded.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "gh $*" >> "$STUB_TMP/gh.log"
case "${1:-}" in
  auth) exit 0 ;;
  run)
    echo "HTTP 403: Must have admin rights to Repository. (https://api.github.com/...)" >&2
    exit 1
    ;;
esac
echo '{}'
exit 0
STUB
chmod +x "$TMP/bin/gh"

# `python3` resolved to the interpreter the suite was handed, so a machine
# whose PATH `python3` is some other build still exercises the same table.
cat > "$TMP/bin/python3" <<STUB
#!/usr/bin/env bash
exec "$PYTHON" "\$@"
STUB
chmod +x "$TMP/bin/python3"

export STUB_TMP="$TMP"
export PATH="$TMP/bin:$PATH"
export NYXGPT_CONFIG_FILE="$TMP/config.ini"
export NYXGPT_AGENT_ERROR_FILE="$TMP/agent-error.txt"
export NYXGPT_PHASE1_ERROR_FILE="$TMP/phase1-error.txt"
export NYXGPT_ANALYSIS_RESULT_FILE="$TMP/analysis_result.txt"

ANALYZE="$ROOT_DIR/scripts/agents/developer_analyze_failure.sh"

# The refusal developer_submit_for_review.sh records, structured lines and all.
REFUSAL="$(
  echo "[error] Required checks FAILED on head abc1234: k3s-cloud-smoke"
  echo "[error] Refusing to submit: a red head is not reviewable (#3971)."
  echo "red-head-check: k3s-cloud-smoke https://github.com/o/r/actions/runs/37722699004/job/1"
  echo "[error] This is the developer round's work, not the reviewer's -- fix the"
  echo "[error] failing check(s), push, and submit again."
)"

_run_phase2() {
  : > "$TMP/gh.log"
  rm -f "$TMP/analysis_result.txt"
  bash "$ANALYZE" 4138 37722699004 "$@" >"$TMP/out.txt" 2>&1
  echo $?
}

_result() { cat "$TMP/analysis_result.txt" 2>/dev/null; }

# ===========================================================================
# 1. A red head continues the round -- the #4138 case, with the refusal
# ===========================================================================
echo "--- a red head continues the round ---"

printf '%s\n' "$REFUSAL" > "$NYXGPT_AGENT_ERROR_FILE"
: > "$NYXGPT_PHASE1_ERROR_FILE"

RC="$(_run_phase2 "retriable:ci_red")"
_assert_eq "exit 2 (continue the round), NOT 1 (fatal)" "2" "$RC"
_assert_contains "the result is TRANSIENT, which the workflow retries" "$(_result)" "STATUS=TRANSIENT"
_assert_not_contains "and is never FATAL" "$(_result)" "STATUS=FATAL"
_assert_contains "the failing check is handed to the next round" "$(_result)" "CI_RED_CHECKS=k3s-cloud-smoke"
_assert_contains "with its run URL" "$(_result)" "runs/37722699004/job/1"
_assert_contains "and the action says what the round will do" "$(_result)" "Continuing the developer round"

# THE HARVEST. The old script's first act was `gh run view "$RUN_ID" --log`.
_assert_not_contains "Phase 2 never calls the run-log API (it cannot answer mid-run)" \
  "$(cat "$TMP/gh.log")" "run view"
_assert_not_contains "and the 403 the stub would have returned is nowhere in its output" \
  "$(cat "$TMP/out.txt")" "HTTP 403"

# ===========================================================================
# 2. The #4138 harvest: nothing recorded at all
# ===========================================================================
# This is the half the narrow fix (adding a `ci_red)` arm) does not deliver: on
# the observed run the harvest came back EMPTY, so the arm would never have
# been reached. Phase 1's class is the input, so an empty harvest changes
# nothing.
echo "--- an empty harvest still continues the round ---"

rm -f "$NYXGPT_AGENT_ERROR_FILE"
: > "$NYXGPT_PHASE1_ERROR_FILE"

RC="$(_run_phase2 "retriable:ci_red")"
_assert_eq "no recorded text, and the round still continues" "2" "$RC"
_assert_contains "still TRANSIENT" "$(_result)" "STATUS=TRANSIENT"
_assert_contains "the check list is empty rather than wrong" "$(_result)" "CI_RED_CHECKS="

# ===========================================================================
# 3. Nothing at all: no class, no text. Defers; never fatal.
# ===========================================================================
echo "--- a Phase 2 that found nothing defers to Phase 1 ---"

RC="$(_run_phase2)"
_assert_eq "exit 3 (defer), which the workflow does NOT escalate" "3" "$RC"
_assert_contains "the status says so" "$(_result)" "STATUS=DEFER"
_assert_not_contains "'could not harvest' is never fatal" "$(_result)" "STATUS=FATAL"

RC="$(_run_phase2 "retriable:a_class_invented_tomorrow")"
_assert_eq "an unrecognised class defers too (this arm used to be FATAL)" "3" "$RC"
_assert_contains "and says whose classification stands" "$(_result)" "Phase 1's classification"

# ===========================================================================
# 4. Phase 1's text is read from the file Phase 1 recorded it in
# ===========================================================================
echo "--- Phase 2 reads the text Phase 1 classified ---"

rm -f "$NYXGPT_AGENT_ERROR_FILE"
printf '%s\n' "$REFUSAL" > "$NYXGPT_PHASE1_ERROR_FILE"

# No class passed: the script classifies the recorded text itself, which must
# reach the same answer Phase 1 did.
RC="$(_run_phase2)"
_assert_eq "the refusal text alone continues the round" "2" "$RC"
_assert_contains "classified from the text Phase 1 used" "$(_result)" "ERROR_TYPE=ci_red"
_assert_contains "and the check is still named" "$(_result)" "CI_RED_CHECKS=k3s-cloud-smoke"

# ===========================================================================
# 5. Every class the classifier can emit, both directions
# ===========================================================================
echo "--- no retriable class exits fatal, and a fatal one still does ---"

rm -f "$NYXGPT_AGENT_ERROR_FILE"
: > "$NYXGPT_PHASE1_ERROR_FILE"

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

CLASSES="$("$PYTHON" - "$ROOT_DIR/scripts/agents/lib/gh_project.sh" <<'PY'
import sys
sys.path.insert(0, "scripts/agents/lib")
import error_classes as ec
for name in ec.shell_emitted_classes(sys.argv[1]):
    print("verification_failed:pytest" if name == ec.VERIFICATION_FAILED else name)
PY
)"
_assert_contains "the class list was readable" "$CLASSES" "retriable:ci_red"

while IFS= read -r CLASS; do
  [[ -n "$CLASS" ]] || continue
  DISPOSITION="$(error_class_disposition "$CLASS")"
  OUTCOME="$("$PYTHON" "$ROOT_DIR/scripts/agents/lib/error_classes.py" phase2 "$CLASS")"
  RC="$(_run_phase2 "$CLASS")"
  case "$DISPOSITION" in
    retriable)
      # A retriable class the table has an outcome for must NOT exit 1: that
      # exit is what the workflow maps to `fix_status=FATAL` -> escalation,
      # and the class says the pipeline knows how to continue. `rate_limit`
      # alone investigates live state (and against this stub concludes a
      # zero-second wait, i.e. retry), so it is asserted the same way.
      _assert_eq "$CLASS (retriable, $OUTCOME) continues rather than escalating" "2" "$RC"
      _assert_not_contains "$CLASS is never the unrecognised-type fatal" \
        "$(_result)" "Unrecognized error type"
      ;;
    fatal)
      _assert_contains "$CLASS is reported fatal with a diagnosis" "$(_result)" "ERROR_TYPE="
      ;;
    diagnose)
      _assert_eq "$CLASS defers to Phase 3" "3" "$RC"
      ;;
  esac
done <<< "$CLASSES"

# The other direction: a genuinely fatal class must still exit 1, or the
# assertions above would pass on a script that never escalates anything.
RC="$(_run_phase2 "fatal:auth_failure")"
_assert_eq "fatal:auth_failure still exits 1" "1" "$RC"
_assert_contains "with a diagnosis the owner can act on" "$(_result)" "DIAGNOSIS=Authentication failed"

# ===========================================================================
# 6. The refusal's structured lines, written by the shell
# ===========================================================================
echo "--- red_head_check_lines writes what the parser reads ---"

head_check_links() {
  printf '%s\n' \
    "k3s-cloud-smoke=https://github.com/o/r/actions/runs/9/job/8" \
    "security-scan="
}
LINES="$(red_head_check_lines abc1234 "k3s-cloud-smoke,security-scan")"
_assert_contains "the failing check carries its URL" "$LINES" \
  "red-head-check: k3s-cloud-smoke https://github.com/o/r/actions/runs/9/job/8"
_assert_contains "a check with no URL still gets its line" "$LINES" "red-head-check: security-scan"

PARSED="$(printf '%s\n%s\n' "a red head is not reviewable" "$LINES" \
  | "$PYTHON" "$ROOT_DIR/scripts/agents/lib/escalation_evidence.py" red-head-checks)"
_assert_contains "and the Python reader gets the name back" "$PARSED" "k3s-cloud-smoke"
_assert_contains "and the URL" "$PARSED" "runs/9/job/8"
_assert_contains "and the second check" "$PARSED" "security-scan"

_assert_eq "no checks named means no lines, not an error" "" "$(red_head_check_lines abc1234 "")"

# ===========================================================================
# 7. The WORKFLOW's own shell bodies, executed
# ===========================================================================
# Everything above tests the scripts. The two decisions #4138 actually died in
# are workflow YAML -- "what does exit 3 mean?" and "what is the continued
# round told?" -- and workflow YAML is the thing this project has learned it
# cannot verify by reading (the huddle_session_probe.py pattern, #3911). So the
# real `run:` blocks are lifted out and run.
echo "--- the workflow's Phase 2 and brief steps, executed ---"

_extract_step() {
  # <workflow> <job> <step name> -> the run block with ${{ }} resolved
  ROOT_DIR="$ROOT_DIR" "$PYTHON" - "$1" "$2" "$3" <<'EXTRACT'
import os
import re
import sys

import yaml

wf_path, job, name = sys.argv[1], sys.argv[2], sys.argv[3]
wf = yaml.safe_load(open(os.environ["ROOT_DIR"] + "/.github/workflows/" + wf_path))
for step in wf["jobs"][job]["steps"]:
    if step.get("name") == name:
        break
else:
    raise SystemExit(f"no step named {name!r} in {wf_path}")

# The run block is shell with GitHub expressions embedded; resolve the ones
# this fixture stands in for and refuse the rest rather than emitting
# something that silently evaluates to the empty string.
SUBSTITUTIONS = {
    "github.event.issue.number": "4138",
    "github.run_id": "37722699004",
    "github.repository": "test-owner/test-repo",
}
def resolve(match):
    key = match.group(1).strip()
    if key not in SUBSTITUTIONS:
        raise SystemExit(f"the fixture has no value for ${{{{ {key} }}}}")
    return SUBSTITUTIONS[key]

print(re.sub(r"\$\{\{([^}]*)\}\}", resolve, step["run"]))
EXTRACT
}

PHASE2_STEP="$TMP/phase2-step.sh"
_extract_step developer_auto_implement.yml implement "Attempt intelligent fix (Phase 2)" \
  > "$PHASE2_STEP" 2>"$TMP/extract.err"
_assert_eq "the Phase 2 step can still be found in the workflow" "0" "$?"
[[ -s "$PHASE2_STEP" ]] || cat "$TMP/extract.err" >&2

_run_phase2_step() {
  : > "$TMP/gh-output"
  rm -f /tmp/analysis_result.txt
  (
    # The workflow step reads the script's DEFAULT result file, so the
    # override this suite uses elsewhere is dropped here -- running the real
    # step means running it over the real path.
    unset NYXGPT_ANALYSIS_RESULT_FILE
    export ERROR_CLASS="$1"
    export NYXGPT_PHASE1_ERROR_FILE="$TMP/phase1-error.txt"
    export GITHUB_OUTPUT="$TMP/gh-output"
    cd "$ROOT_DIR" || exit 1
    # shellcheck source=/dev/null
    source "$PHASE2_STEP"
  ) >"$TMP/step-out.txt" 2>&1
  sed -n 's/^fix_status=//p' "$TMP/gh-output"
}

printf '%s\n' "$REFUSAL" > "$TMP/phase1-error.txt"
rm -f "$NYXGPT_AGENT_ERROR_FILE"
_assert_eq "the workflow reports a red head as TRANSIENT, so the round continues" \
  "TRANSIENT" "$(_run_phase2_step 'retriable:ci_red')"
_assert_contains "and hands the failing check to the retry comment" \
  "$(cat "$TMP/gh-output")" "ci_red_checks=k3s-cloud-smoke"

: > "$TMP/phase1-error.txt"
_assert_eq "an unrecognised retriable class is DEFER, not FATAL" \
  "DEFER" "$(_run_phase2_step 'retriable:a_class_invented_tomorrow')"
_assert_eq "and a genuinely fatal class is still FATAL" \
  "FATAL" "$(_run_phase2_step 'fatal:auth_failure')"

# --- The brief the continued round is handed ------------------------------
BRIEF_STEP="$TMP/brief-step.sh"
_extract_step developer_auto_implement.yml implement \
  "Save review comments to file (if review issues found)" > "$BRIEF_STEP" 2>"$TMP/extract.err"
_assert_eq "the brief step can still be found in the workflow" "0" "$?"
[[ -s "$BRIEF_STEP" ]] || cat "$TMP/extract.err" >&2

cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "gh $*" >> "$STUB_TMP/gh.log"
if [[ "$*" == *"/pulls/"*"head.sha"* ]]; then
  echo "feedface"
  exit 0
fi
if [[ "$*" == *"/check-runs"* ]]; then
  if [[ "$*" == *html_url* ]]; then
    echo "k3s-cloud-smoke=https://github.com/test-owner/test-repo/actions/runs/5/job/6"
  else
    echo "security-scan=success"
    echo "k3s-cloud-smoke=failure"
  fi
  exit 0
fi
case "${1:-}" in
  auth) exit 0 ;;
esac
echo ''
exit 0
STUB
chmod +x "$TMP/bin/gh"

cat > "$TMP/required-checks.txt" <<'EOF'
[required]
security-scan
k3s-cloud-smoke
EOF

: > "$TMP/gh.log"
rm -f /tmp/review-comments.txt
(
  export RESCUE=true PR=4242 ISSUE=4138
  export NYXGPT_REQUIRED_CHECKS_FILE="$TMP/required-checks.txt"
  cd "$ROOT_DIR" || exit 1
  # shellcheck source=/dev/null
  source "$BRIEF_STEP"
) > "$TMP/brief-out.txt" 2>&1
BRIEF="$(cat /tmp/review-comments.txt 2>/dev/null)"

_assert_contains "the rescue brief still says to finish the work on that branch" \
  "$BRIEF" "Do not"
_assert_contains "AND it now names the red check the round has to fix" \
  "$BRIEF" "A required check is RED on this branch's head"
_assert_contains "naming the check" "$BRIEF" "k3s-cloud-smoke"
_assert_contains "and where to read it" "$BRIEF" "actions/runs/5/job/6"
_assert_not_contains "and never tells the round to override the gate" "$BRIEF" "--ci-override"

# Fault injection the other way: a GREEN head must add no red-head section, or
# every continued round would be sent chasing a check that already passed.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
if [[ "$*" == *"/pulls/"*"head.sha"* ]]; then echo "feedface"; exit 0; fi
if [[ "$*" == *"/check-runs"* ]]; then
  echo "security-scan=success"
  echo "k3s-cloud-smoke=success"
  exit 0
fi
case "${1:-}" in auth) exit 0 ;; esac
echo ''
exit 0
STUB
chmod +x "$TMP/bin/gh"

rm -f /tmp/review-comments.txt
(
  export RESCUE=true PR=4242 ISSUE=4138
  export NYXGPT_REQUIRED_CHECKS_FILE="$TMP/required-checks.txt"
  cd "$ROOT_DIR" || exit 1
  # shellcheck source=/dev/null
  source "$BRIEF_STEP"
) > "$TMP/brief-out2.txt" 2>&1
_assert_not_contains "a green head adds no red-head section" \
  "$(cat /tmp/review-comments.txt 2>/dev/null)" "A required check is RED"
rm -f /tmp/review-comments.txt

# ===========================================================================
echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All red-head round assertions passed."
