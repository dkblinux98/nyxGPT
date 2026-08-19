#!/usr/bin/env bash
set -uo pipefail

# tests/test_ensure_pr_exists.sh
# Executed evidence (ledger D-006) for the rescue backstop added in #3862:
# scripts/agents/developer_ensure_pr_exists.sh.
#
# The script is run end to end against a real bare `origin` and a stub `gh` on
# PATH, so the actual code path -- the PR-list guard, classify_mergeable, the
# body generator -- runs with no network. Three things are pinned:
#
#   1. FAIL CLOSED, AND SURVIVE. When the PR list cannot be read, the script
#      must leave the branch alone -- rather than open a PR that may be a
#      duplicate -- and still exit 0, because it runs `if: always()` as the
#      last step of the developer job and a rescue that fails the job it is
#      rescuing is worse than the orphan it was preventing.
#
#      Written as one `gh ... | jq ... || echo unknown` pipeline the guard is
#      DEAD CODE: jq succeeds on empty stdin and prints `0`, then `pipefail`
#      fails the pipeline and the fallback appends its own line, so the
#      variable holds $'0\nunknown' -- which is not `== unknown`, so the guard
#      never fires, and two lines later `[[ "$pr_count" -gt 0 ]]` evaluates it
#      as arithmetic, hits `unknown` as a bare word, and dies on `set -u`'s
#      unbound variable. Case 1b runs exactly that retired form and shows it
#      exiting non-zero with the guard's warning never printed: a check that
#      cannot fail proves nothing (#3775).
#
#   2. The rescue body carries the machine-readable marker, and `Refs #N`
#      rather than `Closes #N` (an unfinished draft must not close its issue).
#
#   3. The marker is the SAME string developer_auto_implement.yml matches on.
#      This is the coupling that makes the body's own recovery instructions
#      true: without it a reassignment starts a fresh timestamped branch,
#      duplicates the rescued work, and leaves the draft open forever with its
#      head branch shielded from every cleanup by that open PR -- a new
#      accumulation mode inside the change whose purpose is ending
#      accumulation. Two files, one string, and nothing but a test to notice
#      when they drift apart.
#
# Usage: bash tests/test_ensure_pr_exists.sh

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

# Case-insensitive ERE assertion. `_assert_not_contains` is a literal glob
# match, which is the wrong instrument for the closing-keyword property:
# GitHub matches `close|closes|closed|fix|fixes|fixed|resolve|resolves|
# resolved` immediately before an issue reference, ANYWHERE in the body and
# WITHOUT regard to case. Guarding that with the literal "Close" is a check
# that cannot fail on the real violation -- and it did not: the body used to
# read "would close #9201 if", lowercase and mid-sentence, a live closing
# reference, while that assertion passed (#3862, second review round).
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

# Every keyword GitHub honours, in every case, immediately before the
# reference. Kept next to its falsifiability check below -- the two are one
# instrument.
#
# The `:?` covers the colon form (`Closes: #N`), which the first cut of this
# pattern let through. Deliberately a superset of what GitHub is known to
# honour: the cost of matching one form GitHub might ignore is a rephrase, and
# the cost of missing one it does honour is the defect this guard exists to
# catch, silently, on every rescue draft.
CLOSING_KEYWORD_RE='(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)):?[[:space:]]+#9201'

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
# Every invocation is logged to $TMP/gh.log; `gh pr create` copies the body it
# was handed to $TMP/pr-body.md so the generated text can be asserted on.
# $TMP/pulls-fail toggles the transient failure case 1 injects.
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
  echo "https://github.com/test-owner/test-repo/pull/7777"
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
      --jq|--template|-X|-f|-F) i=$((i + 1)) ;;
      --paginate) ;;
      -*) ;;
      *) [[ -z "$path" ]] && path="${args[$i]}" ;;
    esac
  done

  if [[ "$path" == *"/pulls?"* ]]; then
    # The injected transient failure: gh exits non-zero having printed
    # nothing, exactly as a rate limit or auth hiccup does.
    [[ -f "$TMP/pulls-fail" ]] && exit 1
    cat "$TMP/pulls.json"
    exit 0
  fi

  if [[ "$path" =~ ^repos/test-owner/test-repo/issues/([0-9]+)$ ]]; then
    printf '{"title":"fix: something broke - api","labels":[{"name":"Agent"}]}\n'
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

# --- Git fixture: a real bare origin holding a base branch and a branch ---
# --- carrying a file that exists nowhere else (the #3789 shape) -----------
git init -q --bare "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/work" 2>/dev/null
cd "$TMP/work" || exit 1
git config user.email "ensure-pr-test@example.invalid"
git config user.name "Ensure PR Test"
git config commit.gpgsign false

echo base > file.txt
git add file.txt
git commit -q -m base
git branch -M v2.0.0
git push -q -u origin v2.0.0

git checkout -q -b claude/issue-9201-stranded
echo "the only copy of this work" > stranded_test.py
git add stranded_test.py
git commit -q -m "test: work that exists on no other ref"
git push -q -u origin claude/issue-9201-stranded
git checkout -q v2.0.0

ENSURE="$ROOT_DIR/scripts/agents/developer_ensure_pr_exists.sh"

# ======================================================================
# 1. A PR list that cannot be read leaves the branch alone
# ======================================================================
: > "$TMP/gh.log"
rm -f "$TMP/pr-body.md"
echo '[]' > "$TMP/pulls.json"
touch "$TMP/pulls-fail"

OUT="$(bash "$ENSURE" 9201 claude/issue-9201-stranded 2>&1)"
RC=$?
LOG="$(cat "$TMP/gh.log")"

_assert_contains "an unreadable PR list still exits 0 (never fails the caller)" "rc=$RC" "rc=0"
_assert_contains "and says why" "$OUT" "leaving it alone rather than opening a duplicate"
_assert_not_contains "no PR is opened on an unreadable PR list" "$LOG" "gh pr create"

# ======================================================================
# 1b. Fault injection: the retired one-line form of the same guard never
#     fires, and the script dies two lines later instead of exiting cleanly.
# ======================================================================
# Written beside a symlink to the real lib/ so the copy sources the same
# gh_project.sh the original does (the script resolves its library relative to
# its own path).
ln -sfn "$ROOT_DIR/scripts/agents/lib" "$TMP/lib"

python3 - "$ENSURE" "$TMP/retired_ensure.sh" <<'PY'
import re
import sys

src, out = sys.argv[1:3]
text = open(src).read()

fixed = re.search(
    r'if ! pr_pages=.*?\nfi\n(if ! pr_count=.*?\nfi\n)',
    text,
    re.S,
)
assert fixed, "could not locate the PR-count guard to replace"

retired = (
    'pr_count="$(gh api "repos/${REPO}/pulls?head=${REPO_OWNER}:${BRANCH}'
    '&state=all&per_page=100" \\\n'
    '    --paginate 2>/dev/null | jq -s \'[.[][]] | length\' || echo "unknown")"\n'
    'if [[ "$pr_count" == "unknown" ]]; then\n'
    '  _warn "Could not list PRs for ${BRANCH}; leaving it alone rather than '
    'opening a duplicate."\n'
    '  exit 0\n'
    'fi\n'
)
open(out, "w").write(text[: fixed.start()] + retired + text[fixed.end() :])
PY

: > "$TMP/gh.log"
RETIRED_OUT="$(bash "$TMP/retired_ensure.sh" 9201 claude/issue-9201-stranded 2>&1)"
# shellcheck disable=SC2181
RETIRED_RC=$?
_assert_contains "the retired one-line guard dies instead of exiting cleanly (so case 1 can fail)" \
  "rc=$RETIRED_RC" "rc=1"
_assert_contains "and it dies exactly where the dead guard let it through" \
  "$RETIRED_OUT" "unbound variable"
_assert_not_contains "the retired guard never fires at all" \
  "$RETIRED_OUT" "leaving it alone rather than opening a duplicate"

# ======================================================================
# 2. A stranded branch gets a draft rescue PR carrying the marker
# ======================================================================
: > "$TMP/gh.log"
rm -f "$TMP/pr-body.md" "$TMP/pulls-fail"
echo '[]' > "$TMP/pulls.json"

OUT="$(bash "$ENSURE" 9201 claude/issue-9201-stranded 2>&1)"
LOG="$(cat "$TMP/gh.log")"
BODY="$(cat "$TMP/pr-body.md" 2>/dev/null || echo "")"

_assert_contains "a stranded branch gets a PR" "$LOG" "gh pr create"
_assert_contains "and it is a draft, not a submission" "$LOG" "--draft"
_assert_contains "the body carries the rescue marker" "$BODY" "<!-- rescue-pr: issue-9201 -->"
_assert_contains "the body refs the issue" "$BODY" "Refs #9201"
_assert_not_contains "the body does NOT close the issue (the draft is unfinished)" \
  "$BODY" "Closes #9201"
_assert_contains "the body describes continuation on this branch" \
  "$BODY" "checks \`claude/issue-9201-stranded\` out and"
_assert_not_matches "and never spells a closing keyword GitHub would honour on merge" \
  "$BODY" "$CLOSING_KEYWORD_RE"

# 2b. The assertion above must be able to fail. The exact string the body
# carried before this fix, plus one of each remaining keyword form, are fed
# to the same pattern: if any of these stops matching, the guard has been
# weakened and case 2 is decoration again.
for _violation in \
  "so spelling it out would close #9201 if anyone merged" \
  "Closes #9201" \
  "this CLOSED #9201" \
  "fixes #9201" \
  "Fix #9201" \
  "resolved  #9201" \
  "Closes: #9201"
do
  if printf '%s' "$_violation" | grep -Eiq "$CLOSING_KEYWORD_RE"; then
    echo "[ok] the keyword guard still catches: ${_violation}"
  else
    echo "[FAIL] the keyword guard no longer catches: ${_violation}" >&2
    FAILURES=$((FAILURES + 1))
  fi
done
# ...and it must not fire on the phrasing the body legitimately uses to
# explain the rule, or the fix would be untestable in the other direction.
_assert_not_matches "the explanatory phrasing itself is not a closing reference" \
  "No closing keyword appears anywhere above that reference for #9201" \
  "$CLOSING_KEYWORD_RE"

# ======================================================================
# 3. The marker the body writes is the marker the workflow matches
# ======================================================================
WF="$(cat "$ROOT_DIR/.github/workflows/developer_auto_implement.yml")"
_assert_contains "developer_auto_implement.yml matches the same marker string" \
  "$WF" '<!-- rescue-pr: issue-${ISSUE} -->'
_assert_contains "the rescue lookup is restricted to OPEN PRs (closing one is the discard signal)" \
  "$WF" 'pulls?state=open&per_page=100'
_assert_contains "a rescue draft routes to the continue-the-work path" \
  "$WF" '"$RESCUE" == "true"'
_assert_contains "a promoted rescue PR gets the Closes line the PR rules require" \
  "$WF" 'body//Refs #${ISSUE}/Closes #${ISSUE}'
_assert_contains "and is taken out of draft before review is requested" \
  "$WF" 'gh pr ready "$PR"'
_assert_contains "developer_submit_for_review.sh adopts an existing open PR instead of dying" \
  "$(cat "$ROOT_DIR/scripts/agents/developer_submit_for_review.sh")" \
  'gh pr ready "$existing_pr"'

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "PASS: developer_ensure_pr_exists.sh rescue contract holds"
