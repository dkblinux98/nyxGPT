#!/usr/bin/env bash
set -uo pipefail

# tests/test_create_issue_blocks.sh
# Proves that the issue-to-issue relationship written by the agent scripts is
# the NATIVE blocked-by edge and nothing else (owner decision 2026-08-12,
# #3731; ledger D-002), and that the retired prose path is gone (#3836).
#
# Two scripts are exercised end to end against a stub `gh` on PATH, so the
# real code -- argument parsing, mark_issue_blocked_by, the PR body
# generator -- runs with no network:
#
#   1. scripts/agents/create_issue.sh --blocks N
#      must POST .../issues/N/dependencies/blocked_by with the new issue's
#      id, and must NOT reopen N, label anything `blocks`, or post
#      "Blocks #N" / "Blocked by #N" comments. N's state is asserted
#      unchanged (it starts CLOSED -- an acceptance failure is filed against
#      a feature parked closed in Acceptance Testing, which
#      handle_acceptance_failure.yml says is never reopened).
#
#   2. scripts/agents/developer_submit_for_review.sh --dry-run
#      must build its PR body without scanning comments for the retired
#      "Blocks #N" marker, and must not emit a second `Closes #` for the
#      blocked issue (that would close a feature the acceptance sweep is
#      supposed to promote).
#
# Usage: bash tests/test_create_issue_blocks.sh

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
RELEASE_BRANCH=v3.0.0
EOF

# --- Stub `gh` --------------------------------------------------------
# Every invocation is appended to $TMP/gh.log (one line per call) so the
# tests can assert on what was and was not called. Fixtures:
#   $TMP/state.txt     the target issue's state, rewritten only by a real
#                      `gh issue reopen` -- which must never happen
#   $TMP/deps.txt      one "<issue> blocked_by <id>" line per native write
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail

TMP="$STUB_TMP"
printf '%s\n' "gh $*" >> "$TMP/gh.log"

case "${1:-}" in
  auth) exit 0 ;;
esac

if [[ "${1:-}" == "issue" ]]; then
  case "${2:-}" in
    create)
      echo "https://github.com/test-owner/test-repo/issues/4242"
      exit 0
      ;;
    reopen)
      # The retired behavior. Recorded, and it flips the fixture state so a
      # regression is visible as changed state and not only as a log line.
      echo "REOPENED" > "$TMP/state.txt"
      exit 0
      ;;
    comment|edit)
      exit 0
      ;;
  esac
  exit 0
fi

if [[ "${1:-}" == "api" ]]; then
  path=""
  method="GET"
  issue_id=""
  args=("$@")
  for ((i = 1; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      # Flags that take a value: consume both, so the value is never
      # mistaken for the API path.
      -X)
        method="${args[$((i + 1))]}"
        i=$((i + 1))
        ;;
      -F|-f)
        case "${args[$((i + 1))]}" in
          issue_id=*) issue_id="${args[$((i + 1))]#issue_id=}" ;;
        esac
        i=$((i + 1))
        ;;
      --jq|--template)
        i=$((i + 1))
        ;;
      --paginate) ;;
      graphql) path="graphql" ;;
      -*) ;;
      *) [[ -z "$path" ]] && path="${args[$i]}" ;;
    esac
  done

  # Native dependency endpoints
  if [[ "$path" == *"/dependencies/blocked_by" ]]; then
    if [[ "$method" == "POST" ]]; then
      target="${path#repos/test-owner/test-repo/issues/}"
      target="${target%/dependencies/blocked_by}"
      printf '%s blocked_by %s\n' "$target" "$issue_id" >> "$TMP/deps.txt"
      echo '{}'
    else
      # No pre-existing blockers (the --jq filter would yield nothing).
      :
    fi
    exit 0
  fi

  if [[ "$path" == *"/dependencies/blocking" ]]; then
    # What issue 4242 blocks, for the PR-body test.
    if [[ -f "$TMP/blocking.txt" ]]; then cat "$TMP/blocking.txt"; fi
    exit 0
  fi

  # Single issue read: the whole object is served, and the caller's --jq
  # picks from it, so this stub must answer both shapes it is asked for.
  if [[ "$path" =~ ^repos/test-owner/test-repo/issues/([0-9]+)$ ]]; then
    num="${BASH_REMATCH[1]}"
    if [[ "$num" == "3787" ]]; then
      state="$(cat "$TMP/state.txt")"
    else
      state="OPEN"
    fi
    for ((i = 1; i < ${#args[@]}; i++)); do
      if [[ "${args[$i]}" == "--jq" ]]; then
        case "${args[$((i + 1))]}" in
          *.id*) echo "$((900000 + num))"; exit 0 ;;
          # `.state | ascii_upcase` -- the retired path's state probe, which
          # gets the bare string real gh would print.
          .state*) echo "$state" | tr '[:lower:]' '[:upper:]'; exit 0 ;;
          *state*)
            printf '{"title":"feat: something (#%s)","body":"## Problem\\n\\nA thing.\\n\\n## Acceptance Criteria\\n- [ ] done","labels":[{"name":"Improvement"}],"url":"https://github.com/test-owner/test-repo/issues/%s","milestone":null,"state":"%s"}\n' \
              "$num" "$num" "$(echo "$state" | tr '[:lower:]' '[:upper:]')"
            exit 0
            ;;
        esac
      fi
    done
    echo '{}'
    exit 0
  fi

  if [[ "$path" == *"/comments"* ]]; then
    # The retired reader's source. Serving a live "Blocks #3787" marker here
    # means any surviving comment scan shows up as a second Closes line.
    echo "Blocks #3787"
    exit 0
  fi

  # Project GraphQL and everything else: no data. create_issue.sh degrades
  # to warnings on project-field writes, which is not what is under test.
  echo '{}'
  exit 0
fi

exit 0
STUB
chmod +x "$TMP/bin/gh"

export STUB_TMP="$TMP"
export PATH="$TMP/bin:$PATH"
export NYXGPT_CONFIG_FILE="$TMP/config.ini"

# ======================================================================
# 1. create_issue.sh --blocks writes the native edge and nothing else
# ======================================================================
echo "CLOSED" > "$TMP/state.txt"
: > "$TMP/gh.log"
: > "$TMP/deps.txt"

OUT="$(bash "$ROOT_DIR/scripts/agents/create_issue.sh" \
  --title "fix: something broke - api" \
  --body "Filed against #3787." \
  --label "Acceptance Failure" \
  --blocks 3787 \
  --no-auto-sprint --no-auto-milestone 2>&1)"
RC=$?
LOG="$(cat "$TMP/gh.log")"
DEPS="$(cat "$TMP/deps.txt")"

_assert_contains "create_issue.sh --blocks exits 0" "rc=$RC" "rc=0"
_assert_contains "native blocked_by edge written on the target issue" \
  "$DEPS" "3787 blocked_by 904242"
_assert_contains "the POST goes to the native dependency endpoint" \
  "$LOG" "repos/test-owner/test-repo/issues/3787/dependencies/blocked_by"
_assert_contains "the run reports the native relationship" \
  "$OUT" "#4242 blocks #3787"

_assert_not_contains "the target issue is not reopened" "$LOG" "issue reopen"
_assert_contains "the target issue's state is unchanged" \
  "$(cat "$TMP/state.txt")" "CLOSED"
_assert_not_contains "no 'blocks' label is applied" "$LOG" "--add-label blocks"
_assert_not_contains "no issue is edited at all" "$LOG" "gh issue edit"
_assert_not_contains "no relationship comment is posted" "$LOG" "gh issue comment"

# The source itself must not carry the retired shapes -- a regression could
# reintroduce them behind a branch this scenario does not take.
SRC="$(cat "$ROOT_DIR/scripts/agents/create_issue.sh")"
_assert_not_contains "no 'Blocks #' comment body in the source" "$SRC" '--body "Blocks #'
_assert_not_contains "no 'Blocked by #' comment body in the source" "$SRC" '--body "Blocked by #'
_assert_not_contains "no issue reopen in the source" "$SRC" "gh issue reopen"
_assert_not_contains "no 'blocks' label in the source" "$SRC" '--add-label "blocks"'
_assert_contains "the source uses the native primitive" "$SRC" "mark_issue_blocked_by"

# ======================================================================
# 1b. Fault injection: the same assertions must FAIL on the retired
#     implementation (#3775/D-006 -- a check that cannot fail proves
#     nothing). A copy of the script carrying the pre-#3836 relationship
#     block, embedded here as a fixture so it stays the retired code
#     forever, must reopen the target and post the prose markers.
# ======================================================================
cat > "$TMP/retired-step4.sh" <<'RETIRED'
if [[ -n "$BLOCKS" ]]; then
  BLOCKED_STATE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${BLOCKS}" --jq '.state | ascii_upcase')
  if [[ "$BLOCKED_STATE" == "CLOSED" ]]; then
    gh issue reopen "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}"
    gh issue comment "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}" --body "Reopened due to Acceptance Failure"
  fi
  gh issue edit "$ISSUE_NUMBER" --add-label "blocks" --repo "${REPO_OWNER}/${REPO_NAME}" 2>/dev/null || true
  gh issue comment "$ISSUE_NUMBER" --body "Blocks #${BLOCKS}" --repo "${REPO_OWNER}/${REPO_NAME}"
  gh issue comment "$BLOCKS" --repo "${REPO_OWNER}/${REPO_NAME}" --body "Blocked by #${ISSUE_NUMBER} (Acceptance Failure)"
fi
RETIRED

python3 - "$ROOT_DIR/scripts/agents/create_issue.sh" "$TMP/retired-step4.sh" \
         "$TMP/create_issue_retired.sh" <<'PY'
import re
import sys

src, retired, out = sys.argv[1:4]
text = open(src).read()
block = open(retired).read()
patched, n = re.subn(
    r"# --- Step 4: Set relationships ---.*?(?=# --- Step 5)",
    "# --- Step 4 (retired implementation, fault injection) ---\n" + block + "\n",
    text,
    flags=re.S,
)
if n != 1:
    sys.exit("could not locate the Step 4 relationship block to replace")
open(out, "w").write(patched)
PY

# The copy resolves its library relative to its own location, so the real
# lib/ travels with it.
cp -r "$ROOT_DIR/scripts/agents/lib" "$TMP/lib"

echo "CLOSED" > "$TMP/state.txt"
: > "$TMP/gh.log"
: > "$TMP/deps.txt"

bash "$TMP/create_issue_retired.sh" \
  --title "fix: something broke - api" \
  --body "Filed against #3787." \
  --label "Acceptance Failure" \
  --blocks 3787 \
  --no-auto-sprint --no-auto-milestone >/dev/null 2>&1
RETIRED_LOG="$(cat "$TMP/gh.log")"

_assert_contains "[injected] the retired path reopens the target issue" \
  "$RETIRED_LOG" "issue reopen"
_assert_contains "[injected] the retired path changes the target's state" \
  "$(cat "$TMP/state.txt")" "REOPENED"
_assert_contains "[injected] the retired path applies the 'blocks' label" \
  "$RETIRED_LOG" "--add-label blocks"
_assert_contains "[injected] the retired path posts the prose marker" \
  "$RETIRED_LOG" "Blocks #3787"
_assert_contains "[injected] the retired path writes no native edge" \
  "empty=$([[ -s "$TMP/deps.txt" ]] && echo no || echo yes)" "empty=yes"

# ======================================================================
# 2. developer_submit_for_review.sh builds the PR body from native data
# ======================================================================
# A repo whose branch is a feature branch, so the script's guards pass.
WORK="$TMP/work"
mkdir -p "$WORK"
(
  cd "$WORK" || exit 1
  git init -q -b v3.0.0 .
  git config user.email test@example.com
  git config user.name test
  git commit -q --allow-empty -m "base"
  git checkout -q -b "fix/4242-something"
) >/dev/null 2>&1

echo "3787" > "$TMP/blocking.txt"
: > "$TMP/gh.log"

BODY_OUT="$(cd "$WORK" && bash "$ROOT_DIR/scripts/agents/developer_submit_for_review.sh" \
  --dry-run 4242 2>&1)"

_assert_contains "the PR closes its own issue" "$BODY_OUT" "Closes #4242"
_assert_not_contains "the PR does not close the issue it blocks" \
  "$BODY_OUT" "Closes #3787"
_assert_contains "the blocked issue is reported as context" \
  "$BODY_OUT" "Blocks acceptance of: #3787"
_assert_contains "the blocked issue is read from the native endpoint" \
  "$(cat "$TMP/gh.log")" "issues/4242/dependencies/blocking"

SUBMIT_SRC="$(cat "$ROOT_DIR/scripts/agents/developer_submit_for_review.sh")"
_assert_not_contains "no comment scan for the retired 'Blocks #' marker" \
  "$SUBMIT_SRC" 'Blocks #\K'

# ======================================================================
echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All create_issue.sh --blocks relationship assertions passed."
