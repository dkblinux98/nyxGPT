#!/usr/bin/env bash
set -uo pipefail

# tests/test_release_ceremony_watch.sh
# Guardrail tests for scripts/agents/release_ceremony_watch.sh (#3730), the
# automated release ceremony's trigger. The ceremony is irreversible
# (master fast-forward, tag, GitHub Release, PyPI publish), so what matters
# is that it fires ONLY for the release tracking issue, ONLY on the
# transition into `For Release`, and never twice for the same version.
#
# Runs the real script with a fake `gh` on PATH and `--check-only`, so no
# mutation can happen even if a guardrail were to regress.
#
# Usage: bash tests/test_release_ceremony_watch.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/agents/release_ceremony_watch.sh"

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
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "[FAIL] $desc: '$needle' not found in: $haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- fake gh: answers the three calls the watcher makes -------------------
mkdir -p "$WORK/bin"
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"auth status"*)
    exit 0 ;;
  *graphql*)
    # issue_status()'s project-item query
    printf '{"data":{"repository":{"issue":{"projectItems":{"nodes":[{"fieldValues":{"nodes":[{"field":{"name":"Status"},"name":"%s"}]}}]}}}}}\n' "$FAKE_STATUS"
    exit 0 ;;
  *comments*)
    echo "${FAKE_COMMENTS:-[]}"
    exit 0 ;;
  *releases/tags/*)
    # `--jq .draft` -- unset means "no such release" (gh exits non-zero)
    [[ -n "${FAKE_RELEASE_DRAFT:-}" ]] || exit 1
    echo "$FAKE_RELEASE_DRAFT"
    exit 0 ;;
  *issues/*)
    # `--jq '.title'` -- gh applies the filter itself, so print the title
    echo "$FAKE_TITLE"
    exit 0 ;;
esac
echo "[fake-gh] unexpected call: $args" >&2
exit 1
FAKE
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

_write_config() { # _write_config [release_issue_number]
  local release_issue="${1-}"
  cat >"$WORK/config.ini" <<EOF
REPO_OWNER=test-owner
REPO_NAME=test-repo
PROJECT_OWNER=test-owner
PROJECT_NUMBER=1
DEV_AGENT=dev
REVIEW_AGENT=rev
SCRUM_AGENT=scrum
HUMAN_OWNER=owner
STATUS_FIELD=Status
STATUS_BACKLOG=Backlog
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_FOR_RELEASE=For Release
RELEASE_BRANCH=v3.0.0
EOF
  [[ -n "$release_issue" ]] && echo "RELEASE_ISSUE_NUMBER=$release_issue" >>"$WORK/config.ini"
  export NYXGPT_CONFIG_FILE="$WORK/config.ini"
}

_run() { NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SCRIPT" --check-only 2>/dev/null | tail -1; }

export FAKE_TITLE="Release v3.0.0 — Phase 6"
export FAKE_COMMENTS='[]'

# --- Test 1: the release issue is still under acceptance -> no ceremony ---
_write_config 3521
export FAKE_STATUS="Acceptance Testing"
out="$(_run)"
_assert_eq "no ceremony while the release issue is still in Acceptance Testing" \
  "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the reason names the status" "$out" "Acceptance Testing"

# --- Test 2: the owner moves it to For Release -> the ceremony fires ---
export FAKE_STATUS="For Release"
out="$(_run)"
_assert_eq "moving the release issue to For Release fires the ceremony" \
  "true" "$(jq -r '.fire' <<<"$out")"
_assert_eq "the version comes from the release issue title" \
  "3.0.0" "$(jq -r '.version' <<<"$out")"

# --- Test 3: a marker for this version means it already ran -> no re-fire ---
# (without this a repeat dispatch would re-run Phases 0-3 of a release)
export FAKE_COMMENTS='[{"body":"starting\n<!-- nyxgpt-release-ceremony:3.0.0 -->"}]'
out="$(_run)"
_assert_eq "an existing marker for this version suppresses a second ceremony" \
  "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the reason says it already started" "$out" "already started"

# --- Test 3a: release already PUBLISHED -> Phase 4 is resumable despite the marker ---
# Phases 0-3 are done once the tag's release is published; only Phase 4
# (idempotent) remains, and a plain re-dispatch must pick it up without force.
export FAKE_RELEASE_DRAFT="false"
out="$(_run)"
_assert_eq "a published release resumes Phase 4 even though a marker exists" \
  "true" "$(jq -r '.fire' <<<"$out")"
err="$(NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SCRIPT" --check-only 2>&1 >/dev/null)"
_assert_contains "the resume is announced" "$err" "only Phase 4"
unset FAKE_RELEASE_DRAFT

# --- Test 3b: a marker for a DIFFERENT version does not suppress it ---
export FAKE_COMMENTS='[{"body":"previous line\n<!-- nyxgpt-release-ceremony:2.1.0 -->"}]'
out="$(_run)"
_assert_eq "the previous line's marker does not suppress this line's ceremony" \
  "true" "$(jq -r '.fire' <<<"$out")"

# --- Test 4: FORCE_CEREMONY re-arms a marked version deliberately ---
export FAKE_COMMENTS='[{"body":"<!-- nyxgpt-release-ceremony:3.0.0 -->"}]'
out="$(FORCE_CEREMONY=1 NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SCRIPT" --check-only 2>/dev/null | tail -1)"
_assert_eq "force re-arms the trigger for a re-run" "true" "$(jq -r '.fire' <<<"$out")"

# --- Test 5: an unparseable release title is a conservative stop ---
export FAKE_COMMENTS='[]'
export FAKE_TITLE="Release tracking issue"
out="$(_run)"
_assert_eq "no version in the title -> no ceremony" "false" "$(jq -r '.fire' <<<"$out")"
_assert_contains "the stop is explained" "$out" "conservative stop"

# --- Test 6: no release issue configured -> nothing to watch ---
_write_config ""
export FAKE_TITLE="Release v3.0.0"
export FAKE_STATUS="For Release"
out="$(_run)"
_assert_eq "no RELEASE_ISSUE_NUMBER -> no ceremony" "false" "$(jq -r '.fire' <<<"$out")"

# --- Tests 7/8: the two pre-flight guards, exercised for real ------------
# These run the watcher WITHOUT --check-only, so it takes the firing path.
# It runs from a sandbox copy of scripts/ whose release_ceremony.sh and
# retire_rc_formulas.sh are recorders: if a guard ever regresses the test
# fails loudly instead of starting a real ceremony.
SANDBOX="$WORK/sandbox"
mkdir -p "$SANDBOX/scripts"
cp -R "$ROOT_DIR/scripts/agents" "$SANDBOX/scripts/agents"
RAN_FILE="$WORK/ceremony-ran"
for stub in release_ceremony.sh retire_rc_formulas.sh; do
  cat >"$SANDBOX/scripts/$stub" <<EOF
#!/usr/bin/env bash
echo "$stub \$*" >>"$RAN_FILE"
EOF
  chmod +x "$SANDBOX/scripts/$stub"
done
SANDBOX_SCRIPT="$SANDBOX/scripts/agents/release_ceremony_watch.sh"

_write_config 3521
export FAKE_TITLE="Release v3.0.0 — Phase 6"
export FAKE_COMMENTS='[]'
export FAKE_STATUS="For Release"

# Test 7: no ceremony token -> refuse before claiming anything.
: >"$RAN_FILE"
err="$(NYXGPT_CEREMONY_PAT="" NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SANDBOX_SCRIPT" 2>&1 >/dev/null)"
rc=$?
_assert_eq "a missing ceremony token fails the run" "1" "$rc"
_assert_contains "the missing token is named" "$err" "Ceremony token not configured"
_assert_eq "no ceremony runs without a token" "" "$(cat "$RAN_FILE")"

# Test 8: the marker comment cannot be posted -> refuse to start unclaimed.
# (starting anyway would let the next poll fire a second ceremony)
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"auth status"*)
    exit 0 ;;
  *"-X POST"*comments*)
    echo "[fake-gh] comment POST refused" >&2
    exit 1 ;;
  *graphql*)
    printf '{"data":{"repository":{"issue":{"projectItems":{"nodes":[{"fieldValues":{"nodes":[{"field":{"name":"Status"},"name":"%s"}]}}]}}}}}\n' "$FAKE_STATUS"
    exit 0 ;;
  *comments*)
    echo "${FAKE_COMMENTS:-[]}"
    exit 0 ;;
  *releases/tags/*)
    # `--jq .draft` -- unset means "no such release" (gh exits non-zero)
    [[ -n "${FAKE_RELEASE_DRAFT:-}" ]] || exit 1
    echo "$FAKE_RELEASE_DRAFT"
    exit 0 ;;
  *issues/*)
    echo "$FAKE_TITLE"
    exit 0 ;;
esac
echo "[fake-gh] unexpected call: $args" >&2
exit 1
FAKE
chmod +x "$WORK/bin/gh"

: >"$RAN_FILE"
err="$(NYXGPT_CEREMONY_PAT="fake-token" NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SANDBOX_SCRIPT" 2>&1 >/dev/null)"
rc=$?
_assert_eq "an unclaimable ceremony fails the run" "1" "$rc"
_assert_contains "the refusal explains the unposted claim" "$err" "refusing to start the ceremony unclaimed"
_assert_eq "no ceremony runs unclaimed" "" "$(cat "$RAN_FILE")"

# Test 9: resume -- with the release published, the ceremony is run with
# --phase4-only and the rc retirement (a Phase 0-3 follow-up) is not repeated.
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"auth status"*) exit 0 ;;
  *"-X POST"*comments*) exit 0 ;;
  *graphql*)
    printf '{"data":{"repository":{"issue":{"projectItems":{"nodes":[{"fieldValues":{"nodes":[{"field":{"name":"Status"},"name":"%s"}]}}]}}}}}\n' "$FAKE_STATUS"
    exit 0 ;;
  *comments*) echo "${FAKE_COMMENTS:-[]}"; exit 0 ;;
  *releases/tags/*) echo "false"; exit 0 ;;
  *issues/*) echo "$FAKE_TITLE"; exit 0 ;;
esac
exit 0
FAKE
chmod +x "$WORK/bin/gh"
export FAKE_COMMENTS='[{"body":"<!-- nyxgpt-release-ceremony:3.0.0 -->"}]'
: >"$RAN_FILE"
NYXGPT_CEREMONY_PAT="fake-token" NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SANDBOX_SCRIPT" >/dev/null 2>&1
_assert_contains "a published release runs only Phase 4" "$(cat "$RAN_FILE")" "release_ceremony.sh 3.0.0 --unattended --phase4-only"
_assert_eq "rc retirement is not repeated on a Phase 4 resume" "" "$(grep retire_rc_formulas "$RAN_FILE" || true)"

# --- Tests 10/11: the PROVISIONED marker contract, consumer side (#4166) ---
# `scripts/release_ceremony.sh` emits `[ceremony] PROVISIONED <what>` for the
# objects Phase 0 had to create; the watcher greps that out of the ceremony log
# and carries it into the owner's completion comment. The producer side is
# pinned in tests/test_release_ceremony_phase0.sh (Cases 4/5); without this the
# consumer could stop reading the marker and nobody would notice until a real
# release left a placeholder milestone nobody was told to rename.
COMMENTS_FILE="$WORK/comments"
cat >"$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"auth status"*) exit 0 ;;
  *"-X POST"*comments*) printf '%s\n' "$args" >>"$COMMENTS_FILE"; exit 0 ;;
  *graphql*)
    printf '{"data":{"repository":{"issue":{"projectItems":{"nodes":[{"fieldValues":{"nodes":[{"field":{"name":"Status"},"name":"%s"}]}}]}}}}}\n' "$FAKE_STATUS"
    exit 0 ;;
  *comments*) echo "${FAKE_COMMENTS:-[]}"; exit 0 ;;
  # No release for this tag yet -> a full run, not a Phase 4 resume.
  *releases/tags/*) exit 1 ;;
  *issues/*) echo "$FAKE_TITLE"; exit 0 ;;
esac
exit 0
FAKE
chmod +x "$WORK/bin/gh"
export COMMENTS_FILE
export FAKE_COMMENTS='[]'

_stub_ceremony() { # _stub_ceremony <line to print on stdout>
  cat >"$SANDBOX/scripts/release_ceremony.sh" <<EOF
#!/usr/bin/env bash
echo "release_ceremony.sh \$*" >>"$RAN_FILE"
printf '%s\n' "$1"
EOF
  chmod +x "$SANDBOX/scripts/release_ceremony.sh"
}

# Test 10: the marker's content reaches the completion comment.
: >"$RAN_FILE"; : >"$COMMENTS_FILE"
_stub_ceremony "[ceremony] PROVISIONED Release Management label on #3521; milestone 'Placeholder — next line (v3.0.1)'"
NYXGPT_CEREMONY_PAT="fake-token" NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SANDBOX_SCRIPT" >/dev/null 2>&1
# The whole recorded body, not just the matching line: the note is appended
# several lines below "Release ceremony complete".
posted="$(cat "$COMMENTS_FILE")"
_assert_contains "the completion comment is posted at all" "$posted" "Release ceremony complete"
_assert_contains "the completion comment carries what Phase 0 provisioned" \
  "$posted" "Phase 0 provisioned: Release Management label on #3521"
_assert_contains "the provisioned milestone is named for the owner to rename" \
  "$posted" "Placeholder — next line (v3.0.1)"
_assert_contains "the note says the placeholder is the owner's to re-scope" \
  "$posted" "rename and re-scope it"

# Test 11: nothing provisioned -> no note at all, rather than an empty one.
# (the normal case once the owner has prepared the line themselves)
: >"$RAN_FILE"; : >"$COMMENTS_FILE"
_stub_ceremony "[ceremony] Phase 0: prerequisite inventory"
NYXGPT_CEREMONY_PAT="fake-token" NYXGPT_CONFIG_FILE="$WORK/config.ini" bash "$SANDBOX_SCRIPT" >/dev/null 2>&1
posted="$(cat "$COMMENTS_FILE")"
_assert_eq "a run that provisioned nothing claims nothing" \
  "" "$(grep -o 'Phase 0 provisioned' <<<"$posted" || true)"
# The real bite: `grep` finding no marker exits 1, and sourcing gh_project.sh
# turns on `set -e`, so without the `|| true` the watcher died HERE -- after a
# successful ceremony, with the rc formulas unretired and no comment at all.
_assert_contains "the completion comment is still posted" "$posted" "agent flags restored"
_assert_contains "the rc formulas are still retired" "$(cat "$RAN_FILE")" "retire_rc_formulas.sh 3.0.0"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
