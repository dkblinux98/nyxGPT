#!/usr/bin/env bash
set -uo pipefail

# tests/test_retro_data_pipeline.sh
#
# Coverage for the retrospective data pipeline under a receiver that requires
# pull requests (#3815).
#
# The defect: every retro dump workflow ended with
# `git push origin HEAD:<dispatching branch>`, and a repository ruleset on
# that branch rejects direct pushes with GH013 ("Changes must be made through
# a pull request"). The dumps ran green for weeks while every refresh was
# discarded at the push — `relationships.json` had never landed at all.
#
# This exercises the real scripts against a real git remote whose pre-receive
# hook enforces the same rule, so the failure is reproduced here rather than
# reasoned about:
#
#   1. the OLD direct push is rejected by the receiver (fault injection — a
#      lab where the push succeeds would prove nothing);
#   2. publish_data_branch.sh lands the JSON on claude/retro-data instead,
#      touching the protected ref not at all;
#   3. merge_data_branch.sh moves it onto the protected branch through a pull
#      request (stubbed `gh`, merged server-side exactly as the API does) and
#      never attempts a push at the protected ref;
#   4. both guards refuse a branch carrying anything outside
#      scripts/retrospective/, which is what bounds this review-skipping path.
#
# Usage: bash tests/test_retro_data_pipeline.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLISH="$ROOT_DIR/scripts/retrospective/publish_data_branch.sh"
MERGE="$ROOT_DIR/scripts/retrospective/merge_data_branch.sh"

PROTECTED="v9.9.9"
DATA_BRANCH="claude/retro-data"
FAILURES=0

_ok()   { echo "[ok] $1"; }
_fail() { echo "[FAIL] $1" >&2; FAILURES=$((FAILURES + 1)); }

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  [[ "$expected" == "$actual" ]] && _ok "$desc" || \
    _fail "$desc: expected '$expected', got '$actual'"
}
_assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  [[ "$haystack" == *"$needle"* ]] && _ok "$desc" || \
    _fail "$desc: expected to find '$needle' in: $haystack"
}
_assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  [[ "$haystack" != *"$needle"* ]] && _ok "$desc" || \
    _fail "$desc: did not expect to find '$needle' in: $haystack"
}

# --------------------------------------------------------------------------
# The lab: a bare "GitHub" whose pre-receive hook enforces the same rule the
# v3.0.0 ruleset does — no direct push to the protected branch, everything
# else allowed — plus a work clone standing in for the workflow runner.
# --------------------------------------------------------------------------
_make_lab() {
  local lab
  lab="$(mktemp -d)"
  git init -q --bare --initial-branch="$PROTECTED" "$lab/origin.git"

  git clone -q "$lab/origin.git" "$lab/work" 2>/dev/null
  (
    cd "$lab/work"
    git config user.email test@example.com
    git config user.name "test"
    mkdir -p scripts/retrospective/data src
    echo "seed" > scripts/retrospective/data/.gitkeep
    echo "print('app')" > src/app.py
    git add -A
    git commit -q -m "base"
    git push -q origin "HEAD:refs/heads/$PROTECTED"
  )

  # Installed AFTER seeding, so the seed itself is not blocked.
  cat > "$lab/origin.git/hooks/pre-receive" <<EOF
#!/bin/sh
while read -r old new ref; do
  echo "\$ref" >> "$lab/push-attempts.log"
  case "\$ref" in
    refs/heads/$PROTECTED)
      echo "error: GH013: Repository rule violations found for \$ref." >&2
      echo "- Changes must be made through a pull request." >&2
      exit 1
      ;;
  esac
done
exit 0
EOF
  chmod +x "$lab/origin.git/hooks/pre-receive"
  : > "$lab/push-attempts.log"
  echo "$lab"
}

_bare_show() {  # _bare_show <lab> <ref> <path>
  git --git-dir="$1/origin.git" show "$2:$3" 2>&1
}
_bare_sha() {  # _bare_sha <lab> <ref>  -> sha, or MISSING
  if git --git-dir="$1/origin.git" show-ref --verify --quiet "$2"; then
    git --git-dir="$1/origin.git" rev-parse "$2"
  else
    echo "MISSING"
  fi
}
_bare_has_path() {  # _bare_has_path <lab> <ref> <path> -> yes|no
  if git --git-dir="$1/origin.git" cat-file -e "$2:$3" 2>/dev/null; then
    echo "yes"
  else
    echo "no"
  fi
}

# --------------------------------------------------------------------------
# A `gh` stub that behaves like the pull-request API: it records the commands
# it was given and performs the merge server-side (an object push to a temp
# ref, then a ref update on the bare repo) — never a client push at the
# protected ref, which is exactly how GitHub merges a PR.
# --------------------------------------------------------------------------
_make_gh_stub() {  # _make_gh_stub <lab> <first_state>
  local lab="$1" first_state="${2:-clean}"
  mkdir -p "$lab/bin" "$lab/gh"
  echo "0" > "$lab/gh/calls"
  echo "$first_state" > "$lab/gh/first_state"
  cat > "$lab/bin/gh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
LAB="$lab"
PROTECTED="$PROTECTED"
DATA_BRANCH="$DATA_BRANCH"
EOF
  cat >> "$lab/bin/gh" <<'EOF'
echo "$*" >> "$LAB/gh/log"

sub="${1:-}"; shift || true

_pr_number() { cat "$LAB/gh/pr" 2>/dev/null || true; }

case "$sub" in
  pr)
    action="${1:-}"; shift || true
    case "$action" in
      list)
        _pr_number
        ;;
      create)
        echo "42" > "$LAB/gh/pr"
        echo "false" > "$LAB/gh/merged"
        echo "https://github.com/o/r/pull/42"
        ;;
      merge)
        # Server-side merge: the objects reach the remote over a ref the rule
        # permits, then the protected ref is moved by the "API", not pushed.
        tmp="$(mktemp -d)"
        git clone -q "$LAB/origin.git" "$tmp/m" 2>/dev/null
        (
          cd "$tmp/m"
          git config user.email api@example.com
          git config user.name "api"
          git checkout -q "$PROTECTED"
          git merge -q --no-ff -m "chore(retro): merge daily data refresh" \
            "origin/$DATA_BRANCH" >/dev/null 2>&1 || exit 1
          git push -q origin "HEAD:refs/heads/_api_merge_tmp" 2>/dev/null
          sha="$(git rev-parse HEAD)"
          git --git-dir="$LAB/origin.git" update-ref "refs/heads/$PROTECTED" "$sha"
          git --git-dir="$LAB/origin.git" update-ref -d "refs/heads/_api_merge_tmp"
          git --git-dir="$LAB/origin.git" update-ref -d "refs/heads/$DATA_BRANCH"
        ) || { echo "merge failed" >&2; rm -rf "$tmp"; exit 1; }
        rm -rf "$tmp"
        echo "true" > "$LAB/gh/merged"
        if [[ -f "$LAB/gh/merge_loses_race" ]]; then
          # Another lander got there first: gh reports failure on an already
          # merged pull request.
          echo "not mergeable: pull request already merged" >&2
          exit 1
        fi
        echo "Merged pull request #$(_pr_number)"
        ;;
      *) echo "unstubbed: gh pr $action" >&2; exit 90 ;;
    esac
    ;;
  api)
    jqexpr=""
    prev=""
    for a in "$@"; do
      [[ "$prev" == "--jq" ]] && jqexpr="$a"
      prev="$a"
    done
    n="$(cat "$LAB/gh/calls")"
    echo "$((n + 1))" > "$LAB/gh/calls"
    state="clean"
    [[ "$n" -eq 0 ]] && state="$(cat "$LAB/gh/first_state")"
    merged="$(cat "$LAB/gh/merged" 2>/dev/null || echo false)"
    payload="{\"merged\": $merged, \"mergeable_state\": \"$state\"}"
    if [[ -n "$jqexpr" ]]; then
      echo "$payload" | jq -r "$jqexpr"
    else
      echo "$payload"
    fi
    ;;
  *) echo "unstubbed: gh $sub" >&2; exit 90 ;;
esac
EOF
  chmod +x "$lab/bin/gh"
}

_write_dump() {  # _write_dump <lab> <name> <content>
  mkdir -p "$1/work/scripts/retrospective/data"
  echo "$3" > "$1/work/scripts/retrospective/data/$2"
}

# ==========================================================================
# 1. Fault injection: the receiver really does reject the old direct push.
#    Without this, everything below could pass on a lab that never enforced
#    the rule the fix exists for.
# ==========================================================================
LAB="$(_make_lab)"
OUT="$(
  cd "$LAB/work"
  _write_dump "$LAB" relationships.json '{"issues": 1}'
  git add -A >/dev/null
  git commit -q -m "chore(retro): refresh native issue relationships snapshot"
  git push origin "HEAD:$PROTECTED" 2>&1
  echo "exit=$?"
)"
_assert_contains "old direct push is rejected by the PR-requiring receiver" "$OUT" "GH013"
_assert_contains "rejection names the pull-request rule" "$OUT" "Changes must be made through a pull request"
_assert_not_contains "old direct push did not succeed" "$OUT" "exit=0"
_assert_eq "protected branch has no relationships.json after the old push" \
  "no" "$(_bare_has_path "$LAB" "$PROTECTED" scripts/retrospective/data/relationships.json)"
rm -rf "$LAB"

# ==========================================================================
# 2. The fix: publish_data_branch.sh lands the JSON on the data branch and
#    leaves the protected ref alone.
# ==========================================================================
LAB="$(_make_lab)"
_write_dump "$LAB" relationships.json '{"issues": 290}'
OUT="$(cd "$LAB/work" && BASE_REF="$PROTECTED" DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh native issue relationships snapshot" \
  scripts/retrospective/data/relationships.json 2>&1)"
RC=$?
_assert_eq "publish succeeds under the PR-requiring receiver" "0" "$RC"
_assert_contains "publish reports it published" "$OUT" "published"
_assert_contains "JSON is present on the data branch" \
  "$(_bare_show "$LAB" "$DATA_BRANCH" scripts/retrospective/data/relationships.json)" '"issues": 290'
_assert_not_contains "publish never pushed at the protected ref" \
  "$(cat "$LAB/push-attempts.log")" "refs/heads/$PROTECTED"

# 2b. Re-publishing identical data is a no-op (no empty commit, no push).
SHA_BEFORE="$(_bare_sha "$LAB" "refs/heads/$DATA_BRANCH")"
OUT="$(cd "$LAB/work" && BASE_REF="$PROTECTED" DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh native issue relationships snapshot" \
  scripts/retrospective/data/relationships.json 2>&1)"
_assert_contains "unchanged data publishes nothing" "$OUT" "no changes"
_assert_eq "data branch tip unchanged when nothing changed" \
  "$SHA_BEFORE" "$(_bare_sha "$LAB" "refs/heads/$DATA_BRANCH")"

# 2c. A second dump published before the first has merged keeps both files —
#     force-resetting to the base tip would silently drop the first refresh.
_write_dump "$LAB" spend.json '{"spend": true}'
OUT="$(cd "$LAB/work" && BASE_REF="$PROTECTED" DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh spend telemetry snapshot" \
  scripts/retrospective/data/spend.json 2>&1)"
_assert_contains "second dump keeps the unmerged first refresh" \
  "$(_bare_show "$LAB" "$DATA_BRANCH" scripts/retrospective/data/relationships.json)" '"issues": 290'
_assert_contains "second dump adds its own file" \
  "$(_bare_show "$LAB" "$DATA_BRANCH" scripts/retrospective/data/spend.json)" '"spend": true'
rm -rf "$LAB"

# ==========================================================================
# 3. Publish guard: nothing outside scripts/retrospective/ may reach the
#    branch that gets merged without review.
# ==========================================================================
LAB="$(_make_lab)"
OUT="$(cd "$LAB/work" && BASE_REF="$PROTECTED" \
  bash "$PUBLISH" "chore: sneak" src/app.py 2>&1; echo "exit=$?")"
_assert_contains "publish refuses a file outside scripts/retrospective/" "$OUT" "refusing to publish"
_assert_not_contains "publish guard exits non-zero" "$OUT" "exit=0"
_assert_eq "no data branch was created by the refused publish" \
  "MISSING" "$(_bare_sha "$LAB" "refs/heads/$DATA_BRANCH")"

# 3b. A dump that produced no file fails loudly instead of pushing nothing.
OUT="$(cd "$LAB/work" && BASE_REF="$PROTECTED" \
  bash "$PUBLISH" "chore(retro): refresh" scripts/retrospective/data/missing.json 2>&1; echo "exit=$?")"
_assert_contains "missing dump output is reported" "$OUT" "no such file"
_assert_not_contains "missing dump output exits non-zero" "$OUT" "exit=0"
rm -rf "$LAB"

# ==========================================================================
# 4. merge_data_branch.sh lands the data branch through a pull request.
# ==========================================================================
LAB="$(_make_lab)"
_make_gh_stub "$LAB" blocked        # first poll: checks pending, then clean
_write_dump "$LAB" relationships.json '{"issues": 290}'
(cd "$LAB/work" && BASE_REF="$PROTECTED" DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh" scripts/retrospective/data/relationships.json) >/dev/null 2>&1
: > "$LAB/push-attempts.log"

OUT="$(cd "$LAB/work" && PATH="$LAB/bin:$PATH" REPO="o/r" BASE_REF="$PROTECTED" \
  DATA_BRANCH="$DATA_BRANCH" POLL_INTERVAL=0 MERGE_TIMEOUT=30 \
  bash "$MERGE" 2>&1; echo "exit=$?")"
_assert_contains "merge reports the branch merged" "$OUT" "merged"
_assert_contains "merge exited cleanly" "$OUT" "exit=0"
GH_LOG="$(cat "$LAB/gh/log")"
_assert_contains "a pull request was opened" "$GH_LOG" "pr create"
_assert_contains "the pull request was merged" "$GH_LOG" "pr merge"
_assert_contains "merge waited out the non-mergeable state" "$OUT" "not mergeable yet"
_assert_contains "the JSON is now on the protected branch" \
  "$(_bare_show "$LAB" "$PROTECTED" scripts/retrospective/data/relationships.json)" '"issues": 290'
_assert_not_contains "merge never pushed at the protected ref" \
  "$(cat "$LAB/push-attempts.log")" "refs/heads/$PROTECTED"

# 4b. Nothing left to merge once it has landed.
OUT="$(cd "$LAB/work" && PATH="$LAB/bin:$PATH" REPO="o/r" BASE_REF="$PROTECTED" \
  DATA_BRANCH="$DATA_BRANCH" POLL_INTERVAL=0 bash "$MERGE" 2>&1; echo "exit=$?")"
_assert_contains "an already-landed refresh is a no-op" "$OUT" "nothing to merge"
_assert_contains "the no-op exits cleanly" "$OUT" "exit=0"
rm -rf "$LAB"

# ==========================================================================
# 4c. Two landers (the dump's own step and a manual push that triggered
#     retro_data_merge.yml) must not turn into a failed run: losing the race
#     is success, because the data landed.
# ==========================================================================
LAB="$(_make_lab)"
_make_gh_stub "$LAB" clean
touch "$LAB/gh/merge_loses_race"
_write_dump "$LAB" relationships.json '{"issues": 7}'
(cd "$LAB/work" && BASE_REF="$PROTECTED" DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh" scripts/retrospective/data/relationships.json) >/dev/null 2>&1
OUT="$(cd "$LAB/work" && PATH="$LAB/bin:$PATH" REPO="o/r" BASE_REF="$PROTECTED" \
  DATA_BRANCH="$DATA_BRANCH" POLL_INTERVAL=0 MERGE_TIMEOUT=30 \
  bash "$MERGE" 2>&1; echo "exit=$?")"
_assert_contains "a lost merge race is reported as merged" "$OUT" "merged by another run"
_assert_contains "a lost merge race exits cleanly" "$OUT" "exit=0"
rm -rf "$LAB"

# ==========================================================================
# 5. Merge guard: the review-skipping path refuses anything but retro data,
#    and refuses it before a pull request is ever opened.
# ==========================================================================
LAB="$(_make_lab)"
_make_gh_stub "$LAB" clean
(
  cd "$LAB/work"
  git checkout -q -B "$DATA_BRANCH"
  echo '{"issues": 1}' > scripts/retrospective/data/relationships.json
  echo "print('sneaky')" > src/app.py
  git add -A >/dev/null
  git commit -q -m "chore(retro): refresh plus a code change"
  git push -q --force origin "HEAD:refs/heads/$DATA_BRANCH" 2>/dev/null
)
PROTECTED_BEFORE="$(_bare_sha "$LAB" "refs/heads/$PROTECTED")"
OUT="$(cd "$LAB/work" && PATH="$LAB/bin:$PATH" REPO="o/r" BASE_REF="$PROTECTED" \
  DATA_BRANCH="$DATA_BRANCH" POLL_INTERVAL=0 bash "$MERGE" 2>&1; echo "exit=$?")"
_assert_contains "merge refuses a branch touching files outside scripts/retrospective/" \
  "$OUT" "Refusing to merge"
_assert_contains "the refusal names the offending file" "$OUT" "src/app.py"
_assert_not_contains "the refusal exits non-zero" "$OUT" "exit=0"
_assert_not_contains "no pull request was opened for a refused branch" \
  "$(cat "$LAB/gh/log" 2>/dev/null || echo "")" "pr create"
_assert_eq "protected branch untouched by a refused merge" \
  "$PROTECTED_BEFORE" "$(_bare_sha "$LAB" "refs/heads/$PROTECTED")"
rm -rf "$LAB"

# ==========================================================================
# 6. BASE_REF defaults to the remote's default branch, so a dump that does
#    not pass one still targets the branch the data is merged into.
# ==========================================================================
LAB="$(_make_lab)"
_write_dump "$LAB" churn.json '{"churn": 1}'
OUT="$(cd "$LAB/work" && DATA_BRANCH="$DATA_BRANCH" \
  bash "$PUBLISH" "chore(retro): refresh churn-cost telemetry snapshot" \
  scripts/retrospective/data/churn.json 2>&1)"
_assert_contains "publish resolves the default branch when BASE_REF is unset" \
  "$OUT" "base: $PROTECTED"
_assert_contains "resolved-base publish still lands the JSON" \
  "$(_bare_show "$LAB" "$DATA_BRANCH" scripts/retrospective/data/churn.json)" '"churn": 1'
rm -rf "$LAB"

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAILED: $FAILURES assertion(s)" >&2
  exit 1
fi
echo "All retro data pipeline assertions passed."
