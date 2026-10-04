#!/usr/bin/env bash
set -uo pipefail

# tests/test_stranded_residue.sh
# Executed evidence (#3775) for #4151: work pushed to a branch AFTER its pull
# request merged must be surfaced, not stranded.
#
# THE ORDERING THAT PRODUCED THE DEFECT, which every case below plants for real
# against a bare `origin` and a stub `gh` (no network):
#
#   04:24:54Z   PR #4126 merged onto the release branch
#   04:43:46Z   b7b3b096 pushed to the SAME branch, 19 minutes later
#
# `developer_ensure_pr_exists.sh` asked "does this branch have a pull
# request?". It had one; it was merged. So the backstop skipped, the review
# path had nothing to review, the issue was already closed, and 579 insertions
# implementing #3986's untested acceptance criterion sat on origin in a form
# `git cherry origin/v3.0.0` marked `+` -- present on the release branch in no
# form at all.
#
# What is pinned here:
#
#   1. A merged PR plus later commits gets a DRAFT residue PR carrying its own
#      marker, `Refs` (never a closing keyword), the residue shas -- and a loud
#      comment on the originating issue naming the branch and the shas, because
#      that issue is closed and its lane reads as accepted.
#   2. FAULT INJECTION. The retired "any PR exists -> skip" form, restored from
#      the real script by tests/inject_retired_pr_guard.py, opens nothing on the
#      identical fixture. Without this half every assertion above would also
#      pass against an implementation that is right by luck (#3775).
#   3. NO FALSE ALARMS. A squash-merged branch reports unmerged commits forever
#      (git compares shas, the squash rewrote them); its content IS on the base,
#      so it must stay silent. A guard that fires on every squash-merged branch
#      is one nobody reads.
#   4. The four quiet dispositions stay quiet: open PR, closed-unmerged PR,
#      merged-with-nothing-left, and an unreadable PR list (which must fail
#      CLOSED -- do nothing, say why, exit 0).
#   5. #3862's case still works and is still DISTINCT: a branch with NO pull
#      request gets the rescue draft with the rescue marker, not the residue
#      one. Neither case masks the other (#4151 criterion 3).
#   6. The repo-wide sweep reports exactly the stranded branch, reports by
#      default, and opens the draft PR when asked.
#
# Usage: bash tests/test_stranded_residue.sh

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

_assert_not_matches() {
  local desc="$1" haystack="$2" pattern="$3"
  if printf '%s' "$haystack" | grep -Eiq "$pattern"; then
    echo "[FAIL] $desc: /$pattern/ unexpectedly matched in:" >&2
    echo "$haystack" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

# Every closing keyword GitHub honours immediately before a reference, in any
# case. The residue draft must not carry one: the issue it refs is already
# CLOSED, and a closing reference on an unverified draft would make merging it
# look like the acceptance this residue proves never happened.
CLOSING_KEYWORD_RE='(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)):?[[:space:]]+#9301'

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

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
STATUS_ACCEPTANCE_TESTING=Acceptance Testing
STATUS_ACCEPTANCE_FAILED=Acceptance Failed
RELEASE_BRANCH=v2.0.0
EOF

# --- Stub `gh` --------------------------------------------------------
# Per-branch PR lists: $TMP/pulls-<branch with / -> _>.json, defaulting to `[]`
# (no PR) so an unconfigured branch exercises the #3862 path. $TMP/pulls-fail
# injects the transient REST failure. `gh pr create` copies the body it was
# handed; POSTed issue comments are copied too, so the loud half can be
# asserted on rather than assumed.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
TMP="$STUB_TMP"
printf '%s\n' "gh $*" >> "$TMP/gh.log"

case "${1:-}" in
  auth) exit 0 ;;
esac

if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--body-file" ]]; then
      cp "${args[$((i + 1))]}" "$TMP/pr-body.md"
    fi
  done
  echo "https://github.com/test-owner/test-repo/pull/8888"
  exit 0
fi

if [[ "${1:-}" == "pr" || "${1:-}" == "issue" ]]; then
  exit 0
fi

if [[ "${1:-}" == "api" ]]; then
  path=""
  jq_expr=""
  body=""
  args=("$@")
  for ((i = 1; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --jq) jq_expr="${args[$((i + 1))]}"; i=$((i + 1)) ;;
      -f|-F)
        val="${args[$((i + 1))]}"
        [[ "$val" == body=* ]] && body="${val#body=}"
        i=$((i + 1))
        ;;
      --template|-X) i=$((i + 1)) ;;
      --paginate) ;;
      -*) ;;
      *) [[ -z "$path" ]] && path="${args[$i]}" ;;
    esac
  done

  _emit() {
    if [[ -n "$jq_expr" ]]; then
      jq -r "$jq_expr" <<<"$1"
    else
      printf '%s\n' "$1"
    fi
  }

  if [[ "$path" == *"/pulls?"* ]]; then
    [[ -f "$TMP/pulls-fail" ]] && exit 1
    head="${path#*head=}"
    head="${head%%&*}"
    head="${head#*:}"
    file="$TMP/pulls-${head//\//_}.json"
    if [[ -f "$file" ]]; then
      _emit "$(cat "$file")"
    else
      _emit '[]'
    fi
    exit 0
  fi

  if [[ "$path" == *"/comments" ]]; then
    printf '%s\n' "$body" >> "$TMP/issue-comments.md"
    _emit '{}'
    exit 0
  fi

  if [[ "$path" =~ ^repos/test-owner/test-repo/issues/([0-9]+)$ ]]; then
    _emit '{"title":"bug: k8s install leaves the web UI unreachable","labels":[{"name":"Acceptance Failure"}]}'
    exit 0
  fi

  _emit '{}'
  exit 0
fi

exit 0
STUB
chmod +x "$TMP/bin/gh"

export STUB_TMP="$TMP"
export PATH="$TMP/bin:$PATH"
export NYXGPT_CONFIG_FILE="$TMP/config.ini"

_reset_log() {
  : > "$TMP/gh.log"
  rm -f "$TMP/pr-body.md" "$TMP/issue-comments.md" "$TMP/pulls-fail"
}

_merged_pr_json() {  # <branch> <number>
  cat <<JSON
[{"number": $2, "state": "closed", "merged_at": "2026-10-04T04:24:54Z",
  "html_url": "https://github.com/test-owner/test-repo/pull/$2",
  "head": {"ref": "$1"}, "base": {"ref": "v2.0.0"}}]
JSON
}

# --- Git fixture ------------------------------------------------------
git init -q --bare "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/work" 2>/dev/null
cd "$TMP/work" || exit 1
git config user.email "residue-test@example.invalid"
git config user.name "Residue Test"
git config commit.gpgsign false

echo base > file.txt
git add file.txt
git commit -q -m base
git branch -M v2.0.0
git push -q -u origin v2.0.0

# THE DEFECT'S SHAPE: work merged via a PR, then more work pushed to the same
# branch after that merge.
STRANDED="fix/9301-k8s-install-leaves-the-web-ui-unreachable"
git checkout -q -b "$STRANDED"
echo "criteria 1-3" > implemented.py
git add implemented.py
git commit -q -m "fix: the work that was reviewed and merged"
git push -q -u origin "$STRANDED"

git checkout -q v2.0.0
git merge -q --no-ff -m "Merge pull request #4126 from test-owner/${STRANDED}" "$STRANDED"
git push -q origin v2.0.0

git checkout -q "$STRANDED"
echo "acceptance criterion 4 -- the one the owner could not test" > k8s_sre_access_claims_test.py
git add k8s_sre_access_claims_test.py
git commit -q -m "test: criterion 4, pushed 19 minutes after the PR merged"
git push -q origin "$STRANDED"
RESIDUE_SHA="$(git rev-parse HEAD)"

# A branch that merged and received nothing afterwards.
CLEAN="fix/9302-nothing-left-over"
git checkout -q v2.0.0
git checkout -q -b "$CLEAN"
echo "all of it" > clean.py
git add clean.py
git commit -q -m "fix: all of this landed"
git push -q -u origin "$CLEAN"
git checkout -q v2.0.0
git merge -q --no-ff -m "Merge pull request #4127" "$CLEAN"
git push -q origin v2.0.0

# A SQUASH-merged branch: its commit is not an ancestor of the base and never
# will be, so it reports unmerged commits forever -- but every byte it carries
# is on the base. This is the false alarm the content check must prevent.
SQUASHED="fix/9303-squash-merged"
git checkout -q v2.0.0
git checkout -q -b "$SQUASHED"
echo "squashed content" > squashed.py
git add squashed.py
git commit -q -m "fix: work that was squash-merged"
git push -q -u origin "$SQUASHED"
git checkout -q v2.0.0
echo "squashed content" > squashed.py
git add squashed.py
git commit -q -m "fix: work that was squash-merged (#9303) [squashed]"
git push -q origin v2.0.0

# Residue, but an OPEN pull request already carries it.
OPEN_HEAD="fix/9304-open-pr"
git checkout -q v2.0.0
git checkout -q -b "$OPEN_HEAD"
echo "under review" > open_pr.py
git add open_pr.py
git commit -q -m "fix: work with an open PR on it"
git push -q -u origin "$OPEN_HEAD"

# Residue behind a PR that was closed WITHOUT merging: an explicit decision.
ABANDONED="fix/9305-closed-unmerged"
git checkout -q v2.0.0
git checkout -q -b "$ABANDONED"
echo "abandoned" > abandoned.py
git add abandoned.py
git commit -q -m "fix: work whose PR was closed unmerged"
git push -q -u origin "$ABANDONED"

# #3862's case, unchanged: on origin, carrying work, with no PR at all.
NO_PR="fix/9306-no-pr-at-all"
git checkout -q v2.0.0
git checkout -q -b "$NO_PR"
echo "never routed" > no_pr.py
git add no_pr.py
git commit -q -m "fix: work that never got a PR"
git push -q -u origin "$NO_PR"

git checkout -q v2.0.0
git pull -q origin v2.0.0 2>/dev/null || true

# PR lists per branch.
_merged_pr_json "$STRANDED" 4126 > "$TMP/pulls-${STRANDED//\//_}.json"
_merged_pr_json "$CLEAN" 4127 > "$TMP/pulls-${CLEAN//\//_}.json"
_merged_pr_json "$SQUASHED" 4128 > "$TMP/pulls-${SQUASHED//\//_}.json"
cat > "$TMP/pulls-${OPEN_HEAD//\//_}.json" <<JSON
[{"number": 4129, "state": "open", "merged_at": null,
  "head": {"ref": "${OPEN_HEAD}"}, "base": {"ref": "v2.0.0"}}]
JSON
cat > "$TMP/pulls-${ABANDONED//\//_}.json" <<JSON
[{"number": 4130, "state": "closed", "merged_at": null,
  "head": {"ref": "${ABANDONED}"}, "base": {"ref": "v2.0.0"}}]
JSON

ENSURE="$ROOT_DIR/scripts/agents/developer_ensure_pr_exists.sh"
SWEEP="$ROOT_DIR/scripts/agents/sweep_stranded_residue.sh"

# ======================================================================
# 1. The defect: a merged PR, then a later push. Residue PR + loud comment.
# ======================================================================
_reset_log
OUT="$(bash "$ENSURE" 9301 "$STRANDED" 2>&1)"
RC=$?
LOG="$(cat "$TMP/gh.log")"
BODY="$(cat "$TMP/pr-body.md" 2>/dev/null || echo "")"
COMMENTS="$(cat "$TMP/issue-comments.md" 2>/dev/null || echo "")"

_assert_contains "the guard exits 0 (it runs if: always())" "rc=$RC" "rc=0"
_assert_contains "the residue is named in the log with its commit count" \
  "$OUT" "received 1 commit(s) AFTER its pull request (#4126) merged"
_assert_contains "a PR is opened for the residue" "$LOG" "--head ${STRANDED}"
_assert_contains "and it is a draft, not a submission" "$LOG" "--draft"
_assert_contains "the body carries the RESIDUE marker" "$BODY" "<!-- residue-pr: issue-9301 -->"
_assert_not_contains "and not #3862's rescue marker (different situation)" \
  "$BODY" "<!-- rescue-pr:"
_assert_contains "the body names the residue sha" "$BODY" "${RESIDUE_SHA:0:12}"
_assert_contains "the body names the merged PR the work arrived after" "$BODY" "#4126"
_assert_contains "the body refs the issue" "$BODY" "Refs #9301"
_assert_not_matches "and never spells a closing keyword GitHub would honour on merge" \
  "$BODY" "$CLOSING_KEYWORD_RE"
_assert_contains "the originating issue is told, loudly" "$COMMENTS" "was pushed after"
_assert_contains "and the comment names the branch" "$COMMENTS" "$STRANDED"
_assert_contains "and the comment names the sha" "$COMMENTS" "${RESIDUE_SHA:0:12}"
_assert_contains "and warns that the closed issue may read as accepted" \
  "$COMMENTS" "may read as accepted"

# 1b. The keyword guard above must be able to fail.
for _violation in "Closes #9301" "this CLOSED #9301" "fixes #9301" "Closes: #9301"; do
  if printf '%s' "$_violation" | grep -Eiq "$CLOSING_KEYWORD_RE"; then
    echo "[ok] the keyword guard still catches: ${_violation}"
  else
    echo "[FAIL] the keyword guard no longer catches: ${_violation}" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

# ======================================================================
# 2. FAULT INJECTION: the shipped form -- "any PR exists -> skip" -- strands
#    the identical fixture, so case 1 can fail.
# ======================================================================
ln -sfn "$ROOT_DIR/scripts/agents/lib" "$TMP/lib"
python3 "$ROOT_DIR/tests/inject_retired_pr_guard.py" "$ENSURE" "$TMP/retired_any_pr.sh"

_reset_log
RETIRED_OUT="$(bash "$TMP/retired_any_pr.sh" 9301 "$STRANDED" 2>&1)"
RETIRED_LOG="$(cat "$TMP/gh.log")"

_assert_not_contains "the retired form opens nothing (so case 1 can fail)" \
  "$RETIRED_LOG" "gh pr create"
_assert_contains "and it says exactly what it concluded: a PR exists, so skip" \
  "$RETIRED_OUT" "already has 1 pull request(s); nothing to do"
_assert_not_contains "it never mentions the residue it is walking away from" \
  "$RETIRED_OUT" "$RESIDUE_SHA"

# ======================================================================
# 3. NO FALSE ALARMS: a squash-merged branch is silent, and so are the other
#    quiet dispositions.
# ======================================================================
# Non-vacuity: the squash case only tests the content exemption if the branch
# really does look unmerged to git. If this ever reads 0 the assertions below
# are passing for the wrong reason.
git fetch -q origin "$SQUASHED" v2.0.0 2>/dev/null || true
SQUASH_AHEAD="$(git rev-list --count "origin/v2.0.0..origin/${SQUASHED}" 2>/dev/null || echo 0)"
_assert_contains "the squash-merged branch DOES carry commits git calls unmerged" \
  "ahead=${SQUASH_AHEAD}" "ahead=1"

_reset_log
OUT="$(bash "$ENSURE" 9303 "$SQUASHED" 2>&1)"
LOG="$(cat "$TMP/gh.log")"
_assert_contains "a squash-merged branch is reported clean, not stranded" \
  "$OUT" "carries nothing of its own"
_assert_not_contains "and gets no PR (the content is already on the base)" "$LOG" "gh pr create"

_reset_log
OUT="$(bash "$ENSURE" 9302 "$CLEAN" 2>&1)"
LOG="$(cat "$TMP/gh.log")"
_assert_contains "a merged branch with nothing after it is clean" "$OUT" "carries nothing of its own"
_assert_not_contains "and gets no PR" "$LOG" "gh pr create"

_reset_log
OUT="$(bash "$ENSURE" 9304 "$OPEN_HEAD" 2>&1)"
LOG="$(cat "$TMP/gh.log")"
_assert_contains "an open PR means the work is routed" "$OUT" "head of an open pull request"
_assert_not_contains "and no second PR is opened for the same head" "$LOG" "gh pr create"

_reset_log
OUT="$(bash "$ENSURE" 9305 "$ABANDONED" 2>&1)"
LOG="$(cat "$TMP/gh.log")"
_assert_contains "a closed-unmerged PR is an explicit abandonment" "$OUT" "closed without merging"
_assert_not_contains "and is not reopened as a question here" "$LOG" "gh pr create"

# An unreadable PR list must fail CLOSED: do nothing, say why, exit 0.
_reset_log
touch "$TMP/pulls-fail"
OUT="$(bash "$ENSURE" 9301 "$STRANDED" 2>&1)"
RC=$?
LOG="$(cat "$TMP/gh.log")"
rm -f "$TMP/pulls-fail"
_assert_contains "an unreadable PR list still exits 0" "rc=$RC" "rc=0"
_assert_contains "and says why it is leaving the branch alone" \
  "$OUT" "leaving it alone rather than opening a duplicate"
_assert_not_contains "and opens nothing on a knowledge failure" "$LOG" "gh pr create"

# ======================================================================
# 4. #3862's case still works, and is still a DIFFERENT case (criterion 3).
# ======================================================================
_reset_log
OUT="$(bash "$ENSURE" 9306 "$NO_PR" 2>&1)"
LOG="$(cat "$TMP/gh.log")"
BODY="$(cat "$TMP/pr-body.md" 2>/dev/null || echo "")"
_assert_contains "a branch with no PR at all still gets the #3862 rescue" "$LOG" "--head ${NO_PR}"
_assert_contains "carrying the RESCUE marker" "$BODY" "<!-- rescue-pr: issue-9306 -->"
_assert_not_contains "and not the residue marker" "$BODY" "<!-- residue-pr:"

# ======================================================================
# 5. The repo-wide sweep (criterion 5): reports the stranded branch, only it,
#    and acts only when asked.
# ======================================================================
_reset_log
SWEEP_OUT="$(bash "$SWEEP" 2>&1)"
RC=$?
LOG="$(cat "$TMP/gh.log")"
_assert_contains "the sweep exits 0" "rc=$RC" "rc=0"
_assert_contains "it reports the stranded branch" "$SWEEP_OUT" "STRANDED ${STRANDED}"
_assert_contains "naming the residue sha" "$SWEEP_OUT" "$RESIDUE_SHA"
_assert_contains "and the issue behind it" "$SWEEP_OUT" "issue #9301"
_assert_not_contains "the squash-merged branch is not reported stranded" \
  "$SWEEP_OUT" "STRANDED ${SQUASHED}"
_assert_not_contains "nor the branch with an open PR" "$SWEEP_OUT" "STRANDED ${OPEN_HEAD}"
_assert_not_contains "nor the abandoned one" "$SWEEP_OUT" "STRANDED ${ABANDONED}"
_assert_contains "it counts what it found" "$SWEEP_OUT" "stranded=1"
_assert_not_contains "and opens nothing by default (report-only)" "$LOG" "gh pr create"

_reset_log
SWEEP_OUT="$(bash "$SWEEP" --open-prs 2>&1)"
LOG="$(cat "$TMP/gh.log")"
_assert_contains "--open-prs opens the draft PR for the stranded branch" \
  "$LOG" "--head ${STRANDED}"
_assert_contains "as a draft" "$LOG" "--draft"
_assert_not_contains "and still opens nothing for the clean branches" \
  "$LOG" "--head ${SQUASHED}"

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "PASS: post-merge residue is detected, surfaced, and not confused with #3862"
