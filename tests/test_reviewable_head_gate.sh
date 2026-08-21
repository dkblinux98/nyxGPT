#!/usr/bin/env bash
set -uo pipefail

# tests/test_reviewable_head_gate.sh
# Executed evidence (D-006) for #3971: "a red or pending head is not
# reviewable".
#
# Everything here RUNS. The check-state decision is a shell function in
# scripts/agents/lib/gh_project.sh rather than inline workflow YAML for
# exactly this reason -- inline YAML can only be inspected, and inspection is
# what this project's executed-verification rule exists to stop relying on.
# developer_submit_for_review.sh is executed end to end against a real git
# fixture and a stub `gh` serving stub check-run state, so the refusal is
# proven by a run rather than read off a diff.
#
# What is pinned, and why each one would otherwise rot silently:
#
#   1. RED REFUSES, AND LEAVES NOTHING BEHIND. A refusal that has already
#      created the PR, requested the reviewer and moved the issue is not a
#      refusal -- it is a slower submission. The gate therefore runs before
#      any GitHub write, and the test asserts `gh pr create` was never called.
#
#   2. PENDING SUBMITS. The rule is "a pending head is nobody's problem yet".
#      A gate that refused pending heads would bounce every PR that submits
#      while its CI is still running -- i.e. all of them.
#
#   3. THE REQUIRED SET IS A NAMED LIST. A failing check that is NOT on the
#      list (the review's own run, project bookkeeping) must not refuse
#      anything. Gating on "every check attached to the head" deadlocks the
#      review against its own check run.
#
#   4. THE OVERRIDE IS A CLAIM, NOT A MUTE. Submitting over a red check with
#      `--ci-override` must put the reason in the PR body under the marker the
#      review workflow reads. An override that submitted silently would be
#      indistinguishable from the gate not existing.
#
#   5. THE REFUSAL SENTENCE IS AN API. classify_error() maps it to
#      retriable:ci_red so the developer round continues instead of handing
#      off. Two files, one sentence, and nothing but this test to notice when
#      they drift apart.
#
#   6. THE HAND-BACK IS BOUNDED PER HEAD SHA. First arrival hands back to the
#      developer; a second arrival on the SAME head escalates instead of
#      handing back again. Without that, the review trigger and the developer
#      round ping-pong forever over an unchanged commit.
#
# Usage: bash tests/test_reviewable_head_gate.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
    echo "[FAIL] $desc: expected to contain '$needle', got:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "[FAIL] $desc: unexpectedly contained '$needle':" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

# ----------------------------------------------------------------------
# A required-check list of the same shape as the real one: two sections,
# comments, and a check that is deliberately NOT required.
# ----------------------------------------------------------------------
cat > "$TMP/required-checks.txt" <<'EOF'
# a comment line
[required]
security-scan            # trailing comment
Install the working tree's formulas
k8s-artifact-smoke

[not-required]
claude-review
pr-hygiene
EOF

export NYXGPT_REQUIRED_CHECKS_FILE="$TMP/required-checks.txt"

# ======================================================================
# Part 1 -- the decision itself, sourced and called directly
# ======================================================================
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
# The library turns on `set -e` for whoever sources it. A test that aborts on
# the first non-zero exit cannot assert that a refusal exits 3.
set +e

REPO_OWNER="test-owner"
REPO_NAME="test-repo"
export REPO_OWNER REPO_NAME

NAMES="$(required_check_names)"
_assert_eq "required_check_names reads only the [required] section" \
  "security-scan
Install the working tree's formulas
k8s-artifact-smoke" "$NAMES"

# The list resolves from the LIBRARY's location, not the caller's directory.
# Checked before anything stubs it, and from a different cwd, because getting
# this wrong is silent: a required-check file the library cannot find makes
# every head `unknown`, which fails open -- so the gate would quietly stop
# gating and every test below would still pass on its own stub file.
DEFAULT_NAMES="$(cd "$TMP" && NYXGPT_REQUIRED_CHECKS_FILE="" \
  bash -c 'source "$1/scripts/agents/lib/gh_project.sh"; required_check_names' _ "$ROOT_DIR" 2>/dev/null)"
_assert_contains "the real required set resolves from the library's own path" \
  "$DEFAULT_NAMES" "security-scan"

# head_check_runs is the single point that talks to GitHub, so it is the
# single point stubbed here.
CHECK_RUNS=""
head_check_runs() { printf '%s\n' "$CHECK_RUNS"; }

_state_of() {
  CHECK_RUNS="$1"
  required_check_state "deadbeef" | tr '\n' ' ' | sed 's/ $//'
}

_assert_eq "a failing required check makes the head red" \
  "state=failed failed=k8s-artifact-smoke pending=" \
  "$(_state_of 'security-scan=success
k8s-artifact-smoke=failure')"

_assert_eq "a failing check that is NOT on the list changes nothing" \
  "state=clear failed= pending=" \
  "$(_state_of 'security-scan=success
claude-review=failure
pr-hygiene=failure')"

_assert_eq "an unconcluded required check is pending, not red" \
  "state=pending failed= pending=security-scan" \
  "$(_state_of 'security-scan=pending')"

_assert_eq "a red check wins over a pending one (no waiting to say so)" \
  "state=failed failed=k8s-artifact-smoke pending=security-scan" \
  "$(_state_of 'security-scan=pending
k8s-artifact-smoke=failure')"

_assert_eq "cancelled counts as failure -- the gate it stood for did not pass" \
  "state=failed failed=security-scan pending=" \
  "$(_state_of 'security-scan=cancelled')"

_assert_eq "a skipped required check is a path filter, not a failure" \
  "state=clear failed= pending=" \
  "$(_state_of 'security-scan=success
k8s-artifact-smoke=skipped')"

_assert_eq "a check name with spaces matches exactly" \
  "state=failed failed=Install the working tree's formulas pending=" \
  "$(_state_of "security-scan=success
Install the working tree's formulas=failure")"

_assert_eq "no required check present at all is 'absent', not 'clear'" \
  "state=absent failed= pending=" \
  "$(_state_of 'pr-hygiene=success')"

# Fail open: an unreadable API is `unknown`, and every caller then behaves
# exactly as it did before this gate existed. Failing closed here would jam
# every submission and every review in the pipeline on a GitHub blip.
head_check_runs() { return 1; }
_assert_eq "an unreadable check list fails OPEN, not closed" \
  "state=unknown failed= pending=" \
  "$(required_check_state deadbeef 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"

# ======================================================================
# Part 2 -- await_required_checks: pending waits, it never rejects
# ======================================================================
# Recorded to a file for the same subshell reason as the cursor above: the
# waits happen inside `$(await_required_checks ...)`.
sleep() { echo "$1" >> "$TMP/sleeps"; }
_sleeps() { tr '\n' ' ' < "$TMP/sleeps" 2>/dev/null | sed 's/ $//'; }

# The scripted sequence of answers, consumed one per call. The cursor lives in
# a file on purpose: await_required_checks reads this through `$(...)`, whose
# subshell would throw away a plain variable increment -- so a variable cursor
# hands out the FIRST answer forever, and every case here would silently
# become "it waits until the cap".
STATES=()
_reset_sleeps() { : > "$TMP/sleeps"; }
_state_cursor() { cat "$TMP/state-idx"; }
required_check_state() {
  local idx s
  idx="$(_state_cursor)"
  s="${STATES[$idx]:-clear}"
  echo "$((idx + 1))" > "$TMP/state-idx"
  case "$s" in
    pending) printf 'state=pending\nfailed=\npending=security-scan\n' ;;
    failed)  printf 'state=failed\nfailed=k8s-artifact-smoke\npending=\n' ;;
    absent)  printf 'state=absent\nfailed=\npending=\n' ;;
    *)       printf 'state=clear\nfailed=\npending=\n' ;;
  esac
}

_reset_sleeps; STATES=(pending pending clear); echo 0 > "$TMP/state-idx"
OUT="$(await_required_checks abc123 600 30 300 2>/dev/null | tr '\n' ' ')"
_assert_eq "a pending head is waited out, then reviewed" "state=clear failed= pending= " "$OUT"
_assert_eq "one wait per pending poll, at the interval it was given" "30 30" "$(_sleeps)"

_reset_sleeps; STATES=(pending failed); echo 0 > "$TMP/state-idx"
OUT="$(await_required_checks abc123 600 30 300 2>/dev/null | tr '\n' ' ')"
_assert_eq "a check that goes red while waiting stops the wait" \
  "state=failed failed=k8s-artifact-smoke pending= " "$OUT"

_reset_sleeps; STATES=(pending pending pending pending pending pending pending pending); echo 0 > "$TMP/state-idx"
OUT="$(await_required_checks abc123 60 30 300 2>/dev/null | tr '\n' ' ')"
_assert_eq "the wait is capped, and says so rather than reviewing anyway" \
  "state=timeout failed= pending=security-scan " "$OUT"

_reset_sleeps; STATES=(absent absent absent absent); echo 0 > "$TMP/state-idx"
OUT="$(await_required_checks abc123 3600 30 60 2>/dev/null | tr '\n' ' ')"
_assert_eq "checks that never appear are a bounded grace, not a wedge" \
  "state=clear failed= pending= " "$OUT"
_assert_eq "the grace period is the one it was given, not the timeout" "30 30" "$(_sleeps)"

# ======================================================================
# Part 3 -- the refusal sentence is the classifier's input (two files,
# one string)
# ======================================================================
unset -f required_check_state sleep head_check_runs
# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
set +e

REFUSAL="$(grep -h 'red head is not reviewable' "$ROOT_DIR/scripts/agents/developer_submit_for_review.sh" | head -1)"
_assert_contains "the submit script still prints the refusal sentence" "$REFUSAL" "red head is not reviewable"
_assert_eq "and classify_error routes it to a developer round, not a hand-off" \
  "retriable:ci_red" "$(classify_error "$REFUSAL")"

# ======================================================================
# Part 4 -- the real submit script, executed
# ======================================================================
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

# --- Stub `gh` --------------------------------------------------------
# Serves the check-run state from $TMP/check-runs.json, records every call in
# $TMP/gh.log, and copies whatever body `gh pr create`/`gh pr edit` is handed
# so the override section can be asserted on.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
TMP="$STUB_TMP"
printf '%s\n' "gh $*" >> "$TMP/gh.log"

case "${1:-}" in
  auth) exit 0 ;;
esac

if [[ "${1:-}" == "pr" && ( "${2:-}" == "create" || "${2:-}" == "edit" ) ]]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--body-file" ]]; then
      cp "${args[$((i + 1))]}" "$TMP/pr-body.md"
    fi
  done
  [[ "${2:-}" == "create" ]] && echo "https://github.com/test-owner/test-repo/pull/4242"
  exit 0
fi

if [[ "${1:-}" == "pr" || "${1:-}" == "issue" ]]; then
  exit 0
fi

if [[ "${1:-}" == "api" ]]; then
  path=""
  args=("$@")
  for ((i = 1; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --jq|--template|-X|-f|-F|--method|--input) i=$((i + 1)) ;;
      --paginate|-*) ;;
      *) [[ -z "$path" ]] && path="${args[$i]}" ;;
    esac
  done

  if [[ "$path" == *"/check-runs" ]]; then
    # The stub answers what the real `--jq` would have produced: the
    # library's own jq filter is exercised by Part 1 above; what is under
    # test here is the submit script's behaviour given a verdict.
    cat "$TMP/check-runs.txt"
    exit 0
  fi

  if [[ "$path" =~ ^repos/test-owner/test-repo/issues/[0-9]+$ ]]; then
    # Heredoc, not printf: printf would expand the \n inside the body string
    # into real newlines and hand the script invalid JSON.
    cat <<'ISSUE_JSON'
{"title":"feat: a thing - api","body":"Some motivation.","labels":[{"name":"Feature"}],"url":"https://github.com/test-owner/test-repo/issues/4321","html_url":"https://github.com/test-owner/test-repo/issues/4321","milestone":null,"state":"OPEN"}
ISSUE_JSON
    exit 0
  fi

  echo '{}'
  exit 0
fi

exit 0
STUB
chmod +x "$TMP/bin/gh"

export STUB_TMP="$TMP"
export PATH="$TMP/bin:$PATH"
export NYXGPT_CONFIG_FILE="$TMP/config.ini"

# --- Git fixture: a feature branch ahead of the release branch --------
git init -q --bare "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/work" 2>/dev/null
cd "$TMP/work" || exit 1
git config user.email "gate-test@example.invalid"
git config user.name "Gate Test"
git config commit.gpgsign false
echo base > file.txt
git add file.txt
git commit -q -m base
git branch -M v9.9.9
git push -q -u origin v9.9.9
git checkout -q -b feat/4321-a-thing
echo work > feature.txt
git add feature.txt
git commit -q -m "feat: a thing (#4321)"
git push -q -u origin feat/4321-a-thing

SUBMIT="$ROOT_DIR/scripts/agents/developer_submit_for_review.sh"

_run_submit() {
  : > "$TMP/gh.log"
  rm -f "$TMP/pr-body.md"
  printf '%s\n' "$1" > "$TMP/check-runs.txt"
  shift
  # The stub cannot answer Projects v2 GraphQL, so the bookkeeping tail
  # (project hygiene, status, assignment) is not what these cases assert --
  # what they assert is whether the submission itself happened, which is
  # decided before any of that.
  bash "$SUBMIT" "$@" 4321 2>&1
}

# --- Red head: refuses, and creates nothing ---
OUT="$(_run_submit 'security-scan=success
k8s-artifact-smoke=failure')"
RC=$?
_assert_eq "a red head exits 3, distinguishable from every other failure" "3" "$RC"
_assert_contains "the refusal names the failing check" "$OUT" "k8s-artifact-smoke"
_assert_contains "the refusal says whose problem it is" "$OUT" "red head is not reviewable"
_assert_contains "the refusal documents the override" "$OUT" "--ci-override"
_assert_not_contains "a refused submission opens no PR" "$(cat "$TMP/gh.log")" "gh pr create"
_assert_not_contains "a refused submission requests no reviewer" "$(cat "$TMP/gh.log")" "--add-reviewer"

# --- A failing check that is not required: nothing to refuse ---
OUT="$(_run_submit 'security-scan=success
claude-review=failure')"
_assert_contains "an optional red check does not refuse the submission" "$(cat "$TMP/gh.log")" "gh pr create"

# --- Pending head: submits, because pending is nobody's problem yet ---
OUT="$(_run_submit 'security-scan=pending
k8s-artifact-smoke=pending')"
_assert_contains "a pending head submits normally" "$(cat "$TMP/gh.log")" "gh pr create"
_assert_not_contains "and is not reported as a failure" "$OUT" "red head is not reviewable"

# --- Green head: submits ---
OUT="$(_run_submit 'security-scan=success
k8s-artifact-smoke=success')"
_assert_contains "a green head submits" "$(cat "$TMP/gh.log")" "gh pr create"

# --- Red head + override: submits, and the reason is in the PR body ---
OUT="$(_run_submit 'security-scan=failure' --ci-override 'security-scan fails identically on v9.9.9 (run 123)')"
_assert_contains "an override submits over the red check" "$(cat "$TMP/gh.log")" "gh pr create"
BODY="$(cat "$TMP/pr-body.md" 2>/dev/null || echo "")"
_assert_contains "the override is marked for the review workflow to find" "$BODY" "<!-- nyxgpt-ci-override -->"
_assert_contains "the stated reason is recorded verbatim" "$BODY" "security-scan fails identically on v9.9.9 (run 123)"
_assert_contains "and it is recorded as a claim to verify" "$BODY" "verify"
_assert_contains "naming the check it covers" "$BODY" "security-scan"

# ======================================================================
# Part 5 -- the hand-back is bounded per head SHA
# ======================================================================
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
TMP="$STUB_TMP"
printf '%s\n' "gh $*" >> "$TMP/gh.log"

case "${1:-}" in
  auth) exit 0 ;;
esac

if [[ "${1:-}" == "pr" && "${2:-}" == "comment" ]]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--body" ]]; then
      printf '%s\n' "${args[$((i + 1))]}" >> "$TMP/pr-comments.txt"
    fi
  done
  exit 0
fi

if [[ "${1:-}" == "api" && "${2:-}" == "graphql" ]]; then
  # The PR closes issue 4321.
  echo '{"data":{"repository":{"pullRequest":{"closingIssuesReferences":{"nodes":[{"number":4321}]}}}}}'
  exit 0
fi

if [[ "${1:-}" == "api" ]]; then
  args=("$@")
  path=""
  for ((i = 1; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --jq|--template|-X|-f|-F|--method|--input) i=$((i + 1)) ;;
      --paginate|-*) ;;
      *) [[ -z "$path" ]] && path="${args[$i]}" ;;
    esac
  done
  if [[ "$path" == *"/issues/4242/comments" ]]; then
    # Everything the gate has already said on this PR, one body per line.
    cat "$TMP/pr-comments.txt" 2>/dev/null || true
    exit 0
  fi
  if [[ "$path" == *"/issues/4321" ]]; then
    echo '{"state":"open"}'
    exit 0
  fi
  echo '{}'
  exit 0
fi

exit 0
STUB
chmod +x "$TMP/bin/gh"

GATE_ACTION="$ROOT_DIR/scripts/agents/review_head_gate_action.sh"

: > "$TMP/gh.log"
: > "$TMP/pr-comments.txt"

OUT="$(bash "$GATE_ACTION" 4242 sha-red failed "k8s-artifact-smoke" 2>&1)"
COMMENTS="$(cat "$TMP/pr-comments.txt")"
_assert_contains "the first red head is reported on the PR" "$COMMENTS" "required checks are failing on this head"
_assert_contains "naming the check" "$COMMENTS" "k8s-artifact-smoke"
_assert_contains "and marked with the head it was reported for" "$COMMENTS" "<!-- ci-red-handback: sha-red -->"
_assert_contains "the hand-back is an assignment, not a verdict" "$(cat "$TMP/gh.log")" "dev-agent"
_assert_not_contains "no review verdict is submitted" "$(cat "$TMP/gh.log")" "gh pr review"

: > "$TMP/gh.log"
OUT="$(bash "$GATE_ACTION" 4242 sha-red failed "k8s-artifact-smoke" 2>&1)"
COMMENTS="$(cat "$TMP/pr-comments.txt")"
_assert_contains "a second arrival on the SAME head escalates instead of repeating" \
  "$COMMENTS" "<!-- ci-red-escalated: sha-red -->"
_assert_contains "and hands it to the owner" "$(cat "$TMP/gh.log")" "owner"

: > "$TMP/gh.log"
BEFORE="$(wc -l < "$TMP/pr-comments.txt")"
OUT="$(bash "$GATE_ACTION" 4242 sha-red failed "k8s-artifact-smoke" 2>&1)"
AFTER="$(wc -l < "$TMP/pr-comments.txt")"
_assert_eq "a third arrival says nothing at all" "$BEFORE" "$AFTER"

# A NEW head gets a fresh budget -- the bound is per SHA, not per PR.
: > "$TMP/gh.log"
OUT="$(bash "$GATE_ACTION" 4242 sha-red-2 failed "linux-native-smoke" 2>&1)"
COMMENTS="$(cat "$TMP/pr-comments.txt")"
_assert_contains "a new head is handed back again" "$COMMENTS" "<!-- ci-red-handback: sha-red-2 -->"

# A pending timeout tells the PR once and hands nothing back.
: > "$TMP/gh.log"
OUT="$(bash "$GATE_ACTION" 4242 sha-slow timeout "macos-brew-smoke" 2>&1)"
COMMENTS="$(cat "$TMP/pr-comments.txt")"
_assert_contains "a timed-out wait is reported, not rejected" "$COMMENTS" "still pending"
_assert_contains "and marked per head" "$COMMENTS" "<!-- ci-pending-timeout: sha-slow -->"
_assert_not_contains "with no hand-back to the developer" "$(cat "$TMP/gh.log")" "dev-agent"

# ======================================================================
# Part 6 -- the WORKFLOW's own shell body, executed
# ======================================================================
# Everything above tests the library. This runs the `head-gate` job's real
# `run:` block, lifted out of claude-code-review.yml, against stub check state
# -- because the step that turns a state into a decision is workflow YAML, and
# workflow YAML is exactly the thing this project has learned it cannot verify
# by reading (the huddle_session_probe.py pattern, #3911).
ROOT_DIR="$ROOT_DIR" python3 - <<'EXTRACT' > "$TMP/gate-step.sh"
import os
import yaml

wf = yaml.safe_load(open(os.environ["ROOT_DIR"] + "/.github/workflows/claude-code-review.yml"))
for step in wf["jobs"]["head-gate"]["steps"]:
    if step.get("id") == "check_state":
        print(step["run"])
        break
else:
    raise SystemExit("the head-gate job has no check_state step any more")
EXTRACT
GATE_STEP_RC=$?
_assert_eq "the gate step can still be found in the workflow" "0" "$GATE_STEP_RC"

cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
if [[ "${1:-}" == "api" && "$*" == *"/pulls/"* && "$*" == *".head.sha"* ]]; then
  echo "sha-under-test"
  exit 0
fi
cat "$STUB_TMP/check-runs.txt" 2>/dev/null || true
exit 0
STUB
chmod +x "$TMP/bin/gh"

_run_gate_step() {
  printf '%s\n' "$1" > "$TMP/check-runs.txt"
  : > "$TMP/gh-output"
  # `sleep` is shadowed so a pending head does not really wait a minute per
  # poll; WAIT_MINUTES is 1, so the cap is reached after one interval.
  (
    export GITHUB_REPOSITORY="test-owner/test-repo"
    export GITHUB_OUTPUT="$TMP/gh-output"
    export PR=4242 WAIT_MINUTES=1
    export PATH="$TMP/bin:$PATH"
    cd "$ROOT_DIR" || exit 1
    sleep() { :; }
    # shellcheck source=/dev/null
    source "$TMP/gate-step.sh"
  ) >/dev/null 2>&1
  sed -n 's/^decision=//p' "$TMP/gh-output"
}

_assert_eq "the workflow step reviews a green head" "proceed" \
  "$(_run_gate_step 'security-scan=success')"
_assert_eq "the workflow step refuses to review a red head" "failed" \
  "$(_run_gate_step 'security-scan=failure')"
_assert_eq "the workflow step reports a wait that expired" "timeout" \
  "$(_run_gate_step 'security-scan=pending')"

# ======================================================================
# Part 7 -- the refusal reaches the CLASSIFIER, not just the log
# ======================================================================
# AC1 is "the developer round CONTINUES (fix, push, re-submit) rather than
# handing off". Exiting 3 with a good sentence on stderr is not enough to
# deliver that, and this project has the receipt: on run 32419181728 the
# refusal fired exactly as designed against this branch's own red
# `gate-is-delegated`, and the round still ended `unknown` -> "non-retriable"
# -> FATAL owner DM, because Phase 1 classifies whatever it managed to harvest
# and mid-run it harvests the failed STEP NAME. So the assertions below are on
# the handoff itself, executed rather than read.
export NYXGPT_AGENT_ERROR_FILE="$TMP/agent-error.txt"
rm -f "$NYXGPT_AGENT_ERROR_FILE"

OUT="$(_run_submit 'security-scan=success
k8s-artifact-smoke=failure')"
_assert_contains "the refusal records its reason where the classifier looks" \
  "$(cat "$NYXGPT_AGENT_ERROR_FILE" 2>/dev/null)" "red head is not reviewable"
_assert_contains "and the recorded reason names the check" \
  "$(cat "$NYXGPT_AGENT_ERROR_FILE" 2>/dev/null)" "k8s-artifact-smoke"

# The whole point: what Phase 1 would classify. Sourcing the library here is
# what the workflow step does.
# shellcheck source=/dev/null
CLASSIFY="$(
  source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
  classify_error "$(read_agent_error_detail)"
)"
_assert_eq "a refused red head classifies retriable, so the round continues" \
  "retriable:ci_red" "$CLASSIFY"

# Fault injection: without the recorded reason, Phase 1's fallback is the step
# NAME -- and that is the `unknown` that woke the owner. If this ever stops
# being `unknown`, the assertion above is passing for the wrong reason.
STEP_NAME_CLASSIFY="$(
  source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"
  classify_error "Submit PR for review"
)"
_assert_eq "the step name alone still classifies unknown (what this fixes)" \
  "unknown" "$STEP_NAME_CLASSIFY"

# A green submission leaves no stale reason behind for the next failure to
# inherit -- the file is written only by a refusal.
rm -f "$NYXGPT_AGENT_ERROR_FILE"
OUT="$(_run_submit 'security-scan=success')"
_assert_eq "a successful submission records no failure reason" "" \
  "$(cat "$NYXGPT_AGENT_ERROR_FILE" 2>/dev/null)"

# And the harvest order is the workflow's, executed: the recorded reason
# outranks the step-name fallback. Lifted from developer_auto_implement.yml
# for the same reason Part 6 lifts the gate step.
ROOT_DIR="$ROOT_DIR" python3 - <<'EXTRACT' > "$TMP/classify-step.sh"
import os
import yaml

wf = yaml.safe_load(
    open(os.environ["ROOT_DIR"] + "/.github/workflows/developer_auto_implement.yml")
)
for step in wf["jobs"]["implement"]["steps"]:
    if step.get("id") == "classify_error":
        print(step["run"])
        break
else:
    raise SystemExit("the implement job has no classify_error step any more")
EXTRACT
_assert_eq "the classify step can still be found in the workflow" "0" "$?"

# The assignment, not the mere mention: the step's own comments name
# read_agent_error_detail too, so matching the bare identifier would go on
# passing after the call itself was deleted.
_assert_contains "the classify step asks the failing script first" \
  "$(cat "$TMP/classify-step.sh")" 'ERROR_LOG="$(read_agent_error_detail)"'
# The order is what makes it load-bearing: the log harvest must be guarded on
# the recorded reason being empty, or it overwrites the direct answer.
_assert_contains "and only falls back to the job log when nothing was recorded" \
  "$(tr '\n' ' ' < "$TMP/classify-step.sh")" 'if [[ -z "$ERROR_LOG" && -n "$JOB_ID" ]]'

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All reviewable-head gate assertions passed."
