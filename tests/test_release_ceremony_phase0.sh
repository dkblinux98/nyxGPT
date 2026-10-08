#!/usr/bin/env bash
set -uo pipefail

# tests/test_release_ceremony_phase0.sh
# Behavioural guards on the ceremony's Phase 0 (#4166), run against a fake
# `gh` and `git` so every mutation it would make is RECORDED instead of made.
#
# The derivations are unit-tested in tests/unit/test_release_prereqs.py and the
# project mutation is proven in scripts/sprint-iteration-preservation-proof.sh.
# What neither of those can show is the ORDER, which is the owner's actual
# requirement:
#
#   1. every prerequisite is inventoried in one pass, and EVERY gap is
#      reported -- not just the first;
#   2. the provisionable ones are created BEFORE the gate decision, so a gate
#      failure leaves them in place and the re-dispatch starts from them;
#   3. a gate failure stops the run before Phase 1 -- no master push, no tag,
#      no publish;
#   4. --dry-run reports what it would provision and mutates NOTHING.
#
# (2) is the one a refactor silently inverts: moving the `fail` above the
# provisioning block still passes every unit test and still produces a
# sensible-looking log.
#
# Usage: bash tests/test_release_ceremony_phase0.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/release_ceremony.sh"

FAILURES=0
_ok() { echo "[ok] $1"; }
_fail() { echo "[FAIL] $1" >&2; FAILURES=$((FAILURES + 1)); }
_assert_contains() { # desc haystack needle
  [[ "$2" == *"$3"* ]] && _ok "$1" || { _fail "$1: '$3' not found"; echo "--- output:" >&2; echo "$2" >&2; }
}
_assert_not_contains() { # desc haystack needle
  [[ "$2" != *"$3"* ]] && _ok "$1" || _fail "$1: '$3' WAS present"
}
_assert_eq() { # desc expected actual
  [[ "$2" == "$3" ]] && _ok "$1" || _fail "$1: expected '$2', got '$3'"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export MUTATIONS="$WORK/mutations"
export FAKE_VARS="$WORK/vars"
export PATH="$WORK/bin:$PATH"

# --- fake git: the three reads Phase 0 makes -------------------------------
cat >"$WORK/bin/git" <<'FAKEGIT'
#!/usr/bin/env bash
args="$*"
case "$args" in
  fetch*) exit 0 ;;
  *"rev-parse --verify -q origin/"*) echo "deadbeefcafe"; exit 0 ;;
  *"rev-parse -q --verify refs/tags/"*) exit 1 ;;   # the tag does not exist
  *show*pyproject.toml*) echo "version = \"${FAKE_PY_VERSION:-3.0.1}\""; exit 0 ;;
esac
echo "[fake-git] unexpected: $args" >&2
exit 1
FAKEGIT
chmod +x "$WORK/bin/git"

# --- fake gh: every read Phase 0 makes, and a recorder for every write -----
#
# Defaults are exported below rather than written as `${VAR:-{...}}` inside the
# fake: an unescaped `}` in a parameter-expansion default closes the expansion,
# which silently truncated every JSON fixture in the first draft of this test.
cat >"$WORK/bin/gh" <<'FAKEGH'
#!/usr/bin/env bash
args="$*"
record() { echo "$1" >>"$MUTATIONS"; }
case "$args" in
  *"auth status"*) exit 0 ;;

  # ---- writes: recorded, never performed ----
  "variable set"*)
    # `gh variable set NAME -R repo --body VALUE`. pause_agent_flags verifies
    # every write by reading it back, so the fake has to remember them.
    name="$3"; val=""
    while [[ $# -gt 0 ]]; do [[ "$1" == "--body" ]] && { val="${2:-}"; break; }; shift; done
    mkdir -p "$FAKE_VARS"; printf '%s' "$val" >"$FAKE_VARS/$name"
    record "variable-set:${name}=${val}"; exit 0 ;;
  *"-X POST"*"/milestones"*)       record "create-milestone"; exit 0 ;;
  *"-X POST"*"/releases"*)         record "create-draft"; echo "999001"; exit 0 ;;
  "issue edit"*"--add-label"*)     record "add-label"; exit 0 ;;
  "issue edit"*"--milestone"*)     record "set-issue-milestone"; exit 0 ;;
  "issue comment"*)                record "comment"; exit 0 ;;

  # ---- reads ----
  "variable get"*)
    name="$3"
    [[ -f "$FAKE_VARS/$name" ]] && { cat "$FAKE_VARS/$name"; exit 0; }
    case "$name" in
      RELEASE_ISSUE_NUMBER) echo "4164" ;;
      PROJECT_OWNER) echo "test-owner" ;;
      PROJECT_NUMBER) echo "2" ;;
      AGENTS_ENABLED|SPRINT_AUTOPILOT|CLAUDE_REVIEW_ENABLED) echo "true" ;;
      *) exit 1 ;;              # CEREMONY_PAUSED_FLAGS: nothing saved yet
    esac
    exit 0 ;;
  *graphql*)
    # The query/mutation arrives on stdin (`--input -`), so reading it is also
    # how the two are told apart.
    if grep -q updateProjectV2Field; then record "update-sprint-field"; echo "{}"; exit 0; fi
    # The post-create read: Phase 0 re-verifies the new iteration by query, so
    # the fake has to answer as the project would after the mutation.
    if [[ -n "${FAKE_SPRINT_FIELD_AFTER:-}" ]] && grep -q update-sprint-field "$MUTATIONS" 2>/dev/null; then
      echo "$FAKE_SPRINT_FIELD_AFTER"; exit 0
    fi
    echo "$FAKE_SPRINT_FIELD"; exit 0 ;;
  *"milestones?state=all"*) echo "$FAKE_RELEASE_MILESTONE"; exit 0 ;;
  *"milestones?state=open"*)
    # before the create, the owner's open milestones; after it, the verify read
    if grep -q create-milestone "$MUTATIONS" 2>/dev/null; then echo "1"; else echo "$FAKE_OPEN_MILESTONES"; fi
    exit 0 ;;
  *"issues?milestone="*) echo "${FAKE_MILESTONE_OPEN_ISSUES:-}"; exit 0 ;;
  *"issues/4164"*) echo "$FAKE_RELEASE_ISSUE_JSON"; exit 0 ;;
  *"code-scanning/alerts"*) echo "0"; exit 0 ;;
  *"releases?per_page"*) echo "$FAKE_DRAFT"; exit 0 ;;
  *"releases/999001"*) echo "true"; exit 0 ;;
  *"git/ref/tags/"*) exit 1 ;;
  # the --phase4-only resume's proof that Phases 0-3 completed
  *"/commits/3.0.1"*) [[ -n "${FAKE_RESUME:-}" ]] || exit 1; echo "deadbeefcafe"; exit 0 ;;
  *"releases/tags/3.0.1"*) [[ -n "${FAKE_RESUME:-}" ]] || exit 1; echo "false"; exit 0 ;;
  *"/compare/3.0.1...master"*) echo "ahead"; exit 0 ;;
  *"contents/.github/workflows/"*) echo "{}"; exit 0 ;;
  "issue view"*labels*) echo "true"; exit 0 ;;
  "issue view"*milestone*) echo "Phase 6.5 (v3.0.1)"; exit 0 ;;
esac
echo "[fake-gh] unexpected call: $args" >&2
exit 1
FAKEGH
chmod +x "$WORK/bin/gh"

# The happy-path world: a scoped and drained release milestone, a clean release
# issue, one active sprint iteration -- and the two gaps Phase 0 must fill (no
# open "(vX.Y.Z)" milestone above 3.0.1, and no draft release).
export FAKE_RELEASE_MILESTONE='{"number":18,"title":"Phase 6.5 (v3.0.1)"}'
export FAKE_RELEASE_ISSUE_JSON='{"body":"clean","labels":[{"name":"Release Management"}],"milestone":{"title":"Phase 6.5 (v3.0.1)"}}'
export FAKE_SPRINT_FIELD='{"id":"PVTIF_x","configuration":{"duration":18,"startDay":1,"iterations":[{"id":"a1","title":"Sprint 10","startDate":"2026-10-06","duration":18}],"completedIterations":[]}}'
export FAKE_OPEN_MILESTONES='[]'
export FAKE_DRAFT='null'
export FAKE_MILESTONE_OPEN_ISSUES=''

_run() { # _run [extra args...]
  : >"$MUTATIONS"; rm -rf "$FAKE_VARS"; mkdir -p "$FAKE_VARS"
  NYXGPT_CONFIG_FILE="$WORK/none.ini" NYXGPT_CEREMONY_PAT="fake" \
    bash "$SCRIPT" 3.0.1 --unattended --stop-after-phase 0 --skip-scan-gate "$@" 2>&1
}

# The automated path gates on the wiring it needs after the publish.
export TAP_REPO="t/tap" TAP_TOKEN="x" SLACK_BOT_TOKEN="x" SLACK_USER_ID="x"

echo "=== Case 1: only provisionable gaps -> created, gate PASSES, Phase 1 reachable"
# no open "(vX.Y.Z)" milestone above 3.0.1, and no draft release
out="$(_run)"; rc=$?
_assert_eq "the run succeeds" "0" "$rc"
_assert_contains "the placeholder milestone is reported" "$out" "Placeholder — next line (v3.0.2)"
_assert_contains "the missing draft is reported" "$out" "no draft release matching v3.0.1"
_assert_contains "the gate passes" "$out" "Phase 0 gate: PASS"
mutations="$(sort "$MUTATIONS" | tr '\n' ' ')"
_assert_contains "the draft release was created" "$mutations" "create-draft"
_assert_contains "the placeholder milestone was created" "$mutations" "create-milestone"
_assert_contains "the provisioning note was posted on the release issue" "$mutations" "comment"
_assert_contains "the agent flags were paused" "$mutations" "variable-set:AGENTS_ENABLED=false"
_assert_not_contains "an existing sprint iteration is left alone" "$mutations" "update-sprint-field"

echo
echo "=== Case 2: a gate failure -> provisioning STILL happens, then the run stops"
# This is the ordering requirement. Unchecked release-issue tasks are
# gate-only; the next-line milestone is still missing and must still be made.
out="$(FAKE_RELEASE_ISSUE_JSON='{"body":"- [ ] #1 not done","labels":[{"name":"Release Management"}],"milestone":{"title":"Phase 6.5 (v3.0.1)"}}' _run)"; rc=$?
_assert_eq "the run fails" "1" "$rc"
_assert_contains "the gate failure is named" "$out" "GATE FAIL [release-issue-tasks]"
_assert_contains "the stop says nothing irreversible ran" "$out" "no master push, no tag, no publish"
_assert_not_contains "Phase 1 is not reached" "$out" "Phase 1:"
mutations="$(sort "$MUTATIONS" | tr '\n' ' ')"
_assert_contains "the placeholder milestone was created anyway" "$mutations" "create-milestone"
_assert_contains "the draft release was created anyway" "$mutations" "create-draft"
_assert_not_contains "the agent flags were NOT paused on a failed gate" "$mutations" "variable-set"

echo
echo "=== Case 3: every gap is reported in one pass, not just the first"
out="$(FAKE_PY_VERSION="2.9.9" \
      FAKE_RELEASE_ISSUE_JSON='{"body":"- [ ] #1 a\n- [ ] #2 b","labels":[],"milestone":null}' \
      FAKE_MILESTONE_OPEN_ISSUES="#77 still open" _run)"; rc=$?
_assert_eq "the run fails" "1" "$rc"
for key in pyproject-version release-milestone-drained release-issue-tasks; do
  _assert_contains "gap reported: ${key}" "$out" "GATE FAIL [${key}]"
done
_assert_contains "the count is reported" "$out" "3 gate failure(s)"
_assert_contains "the missing label is provisionable, not a gate" "$out" "PROVISION [release-issue-label]"

echo
echo "=== Case 4: a missing sprint iteration is provisioned, with every id resubmitted"
SPRINT_NONE='{"id":"PVTIF_x","configuration":{"duration":18,"startDay":1,"iterations":[],"completedIterations":[{"id":"c1","title":"Sprint 9","startDate":"2026-08-01","duration":18}]}}'
SPRINT_AFTER='{"id":"PVTIF_x","configuration":{"duration":18,"startDay":1,"iterations":[{"id":"n1","title":"Sprint 10","startDate":"2026-10-08","duration":18}],"completedIterations":[{"id":"c1","title":"Sprint 9","startDate":"2026-08-01","duration":18}]}}'
out="$(FAKE_SPRINT_FIELD="$SPRINT_NONE" FAKE_SPRINT_FIELD_AFTER="$SPRINT_AFTER" _run)"
_assert_contains "the missing sprint is reported" "$out" "no active or upcoming Sprint iteration"
_assert_contains "the new sprint continues the numbering" "$out" "Sprint 10"
_assert_contains "the resubmit carries the existing iteration" "$out" "2 iteration(s) resubmitted with their ids"
_assert_contains "the sprint field was updated" "$(cat "$MUTATIONS")" "update-sprint-field"
# The marker release_ceremony_watch.sh greps for, on the path that really did
# provision. Asserted here as well as negated in Case 5 so the tense fix
# cannot quietly take the real one away too.
_assert_contains "a real run emits the marker the watcher greps" "$out" "] PROVISIONED "

echo
echo "=== Case 4b: the sprint create is re-verified by query, and the verify bites"
# Fault injection: the mutation 'succeeds' but the iteration is not there.
# Without the re-verify, Phase 0 would report a sprint it never created and
# Phase 4 would fail at its line gate -- the shape #4166 removed.
out="$(FAKE_SPRINT_FIELD="$SPRINT_NONE" FAKE_SPRINT_FIELD_AFTER="$SPRINT_NONE" _run)"; rc=$?
_assert_eq "an unverifiable sprint create fails the run" "1" "$rc"
_assert_contains "the failed verify is named" "$out" "verify failed: sprint iteration 'Sprint 10'"

echo
echo "=== Case 5: --dry-run mutates nothing"
out="$(_run --dry-run)"; rc=$?
_assert_eq "the dry run succeeds" "0" "$rc"
_assert_contains "it says what it would provision" "$out" "DRY-RUN: would provision"
_assert_eq "nothing was mutated" "" "$(cat "$MUTATIONS")"
# The summary marker is tense-correct, because the watcher greps `PROVISIONED`
# to report in its completion comment what Phase 0 put in place -- and because
# a dry run claiming it provisioned contradicts its own lines above.
_assert_contains "the dry run's summary is in the conditional" "$out" "WOULD-PROVISION "
_assert_not_contains "the dry run does not claim it provisioned" "$out" "] PROVISIONED "

echo
echo "=== Case 6: the --phase4-only resume inventories the line prerequisites too"
# Before #4166 a resume skipped Phase 0 entirely and met the missing
# milestone/sprint at Phase 4's line gate -- after the release was public.
# Driven with --dry-run so Phase 4 itself mutates nothing.
out="$(FAKE_SPRINT_FIELD="$SPRINT_NONE" FAKE_RESUME=1   NYXGPT_CONFIG_FILE="$WORK/none.ini" NYXGPT_CEREMONY_PAT="fake"   bash "$SCRIPT" 3.0.1 --unattended --phase4-only --dry-run 2>&1)"
_assert_contains "the resume runs the line inventory" "$out" "Phase 0 (line prerequisites only, --phase4-only)"
_assert_contains "the resume sees the missing sprint" "$out" "no active or upcoming Sprint iteration"
_assert_contains "the resume sees the missing next-line milestone" "$out" "Placeholder — next line (v3.0.2)"
_assert_contains "the resume reports what it would provision" "$out" "DRY-RUN: would provision"

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
fi
echo "$FAILURES test(s) failed." >&2
exit 1
