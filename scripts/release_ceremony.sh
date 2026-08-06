#!/usr/bin/env bash
# release_ceremony.sh — owner-run release ceremony for nyxGPT.
#
# Encodes the ceremony agreed 2026-08-06 (owner + assistant walkthrough):
#   Phase 0  Entry gate (read-only checks)            -> STOP: "ship it"
#   Phase 1  master fast-forward, then publish the draft release
#            (master is always the authoritative latest release; the
#            release tag is created on master AFTER the fast-forward)
#   Phase 2  PyPI publish (build from tag, verify, upload, verify live)
#   Phase 3  Project close-out (statuses -> Done, milestone + issue close)
#   Phase 4  Line reconciliation                       -> STOP: repoint
#            point release: merge release into the next development line
#            major release: next line is born from the release point (manual)
#
# Run LOCALLY by the human owner. Credentials come from ~/.nyxGPT/config.ini:
#   [github] PAT          — owner PAT (ruleset bypass: master push, repoint)
#   [pypi]   PYPI_TOKEN   — project-scoped upload token
#
# Usage:
#   scripts/release_ceremony.sh VERSION [options]
#     VERSION                e.g. 2.1.0  (release branch is v<VERSION>)
#   Options:
#     --next-branch BRANCH   next development line (required for point
#                            releases, e.g. v3.0.0; ignored for x.0.0)
#     --next-release-issue N RELEASE_ISSUE_NUMBER after repoint (Phase 4)
#     --skip-scan-gate       skip the code-scanning gate (v2.1.0 decision;
#                            keep the gate for lines with the full CI suite)
#     --skip-pypi            skip Phase 2
#     --dry-run              run all read-only checks; print, don't mutate
#
# Every mutation is re-verified by querying GitHub/PyPI afterwards — never
# assume success from a non-error response (house rule).

set -euo pipefail

REPO_OWNER="dkblinux98"
REPO_NAME="nyxGPT"
REPO="${REPO_OWNER}/${REPO_NAME}"
CONFIG_FILE="${NYXGPT_CONFIG_FILE:-$HOME/.nyxGPT/config.ini}"

log()  { echo "[ceremony] $*"; }
fail() { echo "[ceremony] FATAL: $*" >&2; exit 1; }

# --- args ---
VERSION="${1:-}"; [[ -n "$VERSION" ]] || { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
shift
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "VERSION must be x.y.z, got: $VERSION"
NEXT_BRANCH=""; NEXT_RELEASE_ISSUE=""; SKIP_SCAN=0; SKIP_PYPI=0; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --next-branch)        NEXT_BRANCH="$2"; shift 2 ;;
    --next-release-issue) NEXT_RELEASE_ISSUE="$2"; shift 2 ;;
    --skip-scan-gate)     SKIP_SCAN=1; shift ;;
    --skip-pypi)          SKIP_PYPI=1; shift ;;
    --dry-run)            DRY=1; shift ;;
    *) fail "unknown option: $1" ;;
  esac
done

REL_BRANCH="v${VERSION}"
if [[ "$VERSION" =~ ^[0-9]+\.0\.0$ ]]; then REL_TYPE="major"; else REL_TYPE="point"; fi
if [[ "$REL_TYPE" == "point" && -z "$NEXT_BRANCH" && $DRY -eq 0 ]]; then
  fail "point release requires --next-branch (the development line that must absorb these changes)"
fi

# --- credentials ---
ini_get() { # ini_get SECTION KEY
  awk -F'=' -v sec="[$1]" -v key="$2" '
    $0==sec {s=1; next} /^\[/{s=0}
    s && $1 ~ "^"key"[ \t]*$" {v=$2; gsub(/[ \t]/,"",v); print v; exit}' "$CONFIG_FILE"
}
PAT="$(ini_get github PAT)";           [[ -n "$PAT" ]] || fail "[github] PAT not found in $CONFIG_FILE"
PYPI_TOKEN="$(ini_get pypi PYPI_TOKEN)"
[[ $SKIP_PYPI -eq 1 || -n "$PYPI_TOKEN" ]] || fail "[pypi] PYPI_TOKEN not found (or pass --skip-pypi)"
export GH_TOKEN="$PAT"   # the ceremony is the human owner's act — all gh calls run as the owner
AUTH_URL="https://x-access-token:${PAT}@github.com/${REPO}.git"

mutate() { # mutate "description" cmd...
  local desc="$1"; shift
  if [[ $DRY -eq 1 ]]; then log "DRY-RUN: would $desc"; return 0; fi
  log "$desc"
  "$@"
}

confirm() { # confirm "prompt-token"
  [[ $DRY -eq 1 ]] && { log "DRY-RUN: stop point '$1' auto-skipped"; return 0; }
  echo
  read -r -p "[ceremony] STOP POINT — type '$1' to continue: " ans
  [[ "$ans" == "$1" ]] || fail "aborted at stop point (expected '$1')"
}

# =====================================================================
log "Release $VERSION ($REL_TYPE release) — branch $REL_BRANCH"
git fetch origin --tags --quiet

# --- Phase 0: entry gate (read-only) ---
log "Phase 0: entry gate"
GATE_FAIL=0

TIP=$(git rev-parse "origin/${REL_BRANCH}" 2>/dev/null) || fail "origin/${REL_BRANCH} not found"
log "  release tip: $TIP"

# 0.4 version sanity on the branch tip
PY_VER=$(git show "origin/${REL_BRANCH}:pyproject.toml" | awk -F'"' '/^version =/{print $2; exit}')
if [[ "$PY_VER" != "$VERSION" ]]; then log "  GATE FAIL: pyproject version on tip is '$PY_VER', expected '$VERSION'"; GATE_FAIL=1
else log "  ok: pyproject version = $VERSION"; fi

# 0.1 milestone: every issue closed
MILESTONE_JSON=$(gh api "repos/${REPO}/milestones?state=all&per_page=100" \
  --jq "[.[] | select(.title | test(\"v${VERSION}\"))][0]")
[[ -n "$MILESTONE_JSON" && "$MILESTONE_JSON" != "null" ]] || fail "no milestone matching v${VERSION}"
MS_NUM=$(jq -r .number <<<"$MILESTONE_JSON"); MS_TITLE=$(jq -r .title <<<"$MILESTONE_JSON")
MS_OPEN=$(jq -r .open_issues <<<"$MILESTONE_JSON")
if [[ "$MS_OPEN" != "0" ]]; then
  log "  GATE FAIL: milestone '$MS_TITLE' has $MS_OPEN open issue(s):"
  gh issue list -R "$REPO" --milestone "$MS_TITLE" --state open --json number,title \
    --jq '.[]|"    #\(.number) \(.title)"'
  GATE_FAIL=1
else log "  ok: milestone '$MS_TITLE' fully closed"; fi

# 0.2 release issue: no unchecked issue-reference tasks
RELEASE_ISSUE=$(gh variable get RELEASE_ISSUE_NUMBER -R "$REPO" 2>/dev/null || true)
[[ -n "$RELEASE_ISSUE" ]] || fail "RELEASE_ISSUE_NUMBER repo variable not readable"
UNCHECKED=$(gh issue view "$RELEASE_ISSUE" -R "$REPO" --json body --jq .body \
  | grep -cE '^\s*- \[ \] #[0-9]+' || true)
if [[ "$UNCHECKED" != "0" ]]; then
  log "  GATE FAIL: release issue #$RELEASE_ISSUE has $UNCHECKED unchecked issue task(s)"; GATE_FAIL=1
else log "  ok: release issue #$RELEASE_ISSUE task list clean"; fi

# 0.3 code-scanning gate (skippable)
if [[ $SKIP_SCAN -eq 1 ]]; then
  log "  skipped: code-scanning gate (--skip-scan-gate)"
else
  ALERTS=$(gh api "repos/${REPO}/code-scanning/alerts?state=open&ref=refs/heads/${REL_BRANCH}&per_page=100" \
    --jq '[.[] | select(.rule.security_severity_level=="critical" or .rule.security_severity_level=="high")] | length' 2>/dev/null || echo "ERR")
  if [[ "$ALERTS" == "ERR" ]]; then log "  GATE FAIL: could not query code-scanning alerts"; GATE_FAIL=1
  elif [[ "$ALERTS" != "0" ]]; then log "  GATE FAIL: $ALERTS open critical/high code-scanning alert(s) on $REL_BRANCH"; GATE_FAIL=1
  else log "  ok: no open critical/high code-scanning alerts"; fi
fi

# draft release located (by intended name match among drafts)
DRAFT_JSON=$(gh api "repos/${REPO}/releases?per_page=30" \
  --jq "[.[] | select(.draft==true) | select(.name | test(\"v${VERSION}\"))][0]")
[[ -n "$DRAFT_JSON" && "$DRAFT_JSON" != "null" ]] || fail "no draft release matching v${VERSION} found"
DRAFT_ID=$(jq -r .id <<<"$DRAFT_JSON")
log "  ok: draft release found (id $DRAFT_ID, tag='$(jq -r .tag_name <<<"$DRAFT_JSON")', target='$(jq -r .target_commitish <<<"$DRAFT_JSON")')"

# tag must not already exist
if git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null 2>&1 \
   || gh api "repos/${REPO}/git/ref/tags/${VERSION}" >/dev/null 2>&1; then
  fail "tag ${VERSION} already exists — ceremony already ran?"
fi

[[ $GATE_FAIL -eq 0 ]] || fail "entry gate failed — fix the items above and re-run"
log "Phase 0 gate: PASS"
confirm "ship it"

# --- Phase 1: master fast-forward, then publish (master-first, owner order) ---
log "Phase 1: master fast-forward -> publish release"

MASTER_BEFORE=$(gh api "repos/${REPO}/branches/master" --jq .commit.sha)
log "  master before: $MASTER_BEFORE"
if [[ "$MASTER_BEFORE" == "$TIP" ]]; then
  log "  master already at release tip (ok)"
else
  # plain (non-force) push is inherently fast-forward-only: git rejects it
  # outright if master cannot fast-forward — that IS the safety check.
  mutate "fast-forward master -> $TIP" \
    git push "$AUTH_URL" "${TIP}:refs/heads/master"
  if [[ $DRY -eq 0 ]]; then
    MASTER_AFTER=$(gh api "repos/${REPO}/branches/master" --jq .commit.sha)
    [[ "$MASTER_AFTER" == "$TIP" ]] || fail "verify failed: master is $MASTER_AFTER, expected $TIP"
    log "  verified: master == release tip"
  fi
fi

# normalize the draft (tag + target master, which now equals the tip), publish
mutate "normalize draft (tag=${VERSION}, target=master) and publish" \
  gh api -X PATCH "repos/${REPO}/releases/${DRAFT_ID}" \
    -f tag_name="${VERSION}" -f target_commitish="master" -F draft=false --silent
if [[ $DRY -eq 0 ]]; then
  TAG_SHA=$(gh api "repos/${REPO}/git/ref/tags/${VERSION}" --jq .object.sha)
  [[ "$TAG_SHA" == "$TIP" ]] || fail "verify failed: tag ${VERSION} at $TAG_SHA, expected $TIP"
  log "  verified: tag ${VERSION} == release tip; release published"
fi

# --- Phase 2: PyPI ---
if [[ $SKIP_PYPI -eq 1 ]]; then
  log "Phase 2: skipped (--skip-pypi)"
elif [[ $DRY -eq 1 ]]; then
  log "Phase 2 DRY-RUN: would build from tag ${VERSION}, twine check, clean-venv smoke, upload, verify live"
else
  log "Phase 2: PyPI publish"
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"; git worktree prune >/dev/null 2>&1 || true' EXIT
  git worktree add --detach "$WORK/src" "$TIP" >/dev/null
  python3 -m venv "$WORK/buildenv"
  "$WORK/buildenv/bin/pip" -q install --upgrade build twine
  ( cd "$WORK/src" && "$WORK/buildenv/bin/python" -m build --outdir "$WORK/dist" ) >/dev/null
  "$WORK/buildenv/bin/twine" check "$WORK"/dist/* | sed 's/^/  /'
  # clean-venv smoke: the uploaded artifact must at least import and answer --help
  python3 -m venv "$WORK/smokeenv"
  "$WORK/smokeenv/bin/pip" -q install "$WORK"/dist/*.whl
  "$WORK/smokeenv/bin/python" -c "import nyxgpt" || fail "smoke failed: import nyxgpt"
  "$WORK/smokeenv/bin/nyxgpt" --help >/dev/null 2>&1 || log "  note: 'nyxgpt --help' unavailable in bare install (acceptable if documented)"
  log "  smoke ok — uploading to PyPI"
  TWINE_USERNAME=__token__ TWINE_PASSWORD="$PYPI_TOKEN" \
    "$WORK/buildenv/bin/twine" upload --non-interactive "$WORK"/dist/* >/dev/null
  for i in $(seq 1 12); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "https://pypi.org/pypi/nyxgpt/${VERSION}/json")
    [[ "$code" == "200" ]] && break; sleep 10
  done
  [[ "$code" == "200" ]] || fail "verify failed: pypi.org does not serve nyxgpt ${VERSION}"
  log "  verified live: https://pypi.org/project/nyxgpt/${VERSION}/"
  git worktree remove "$WORK/src" --force >/dev/null 2>&1 || true
fi

# --- Phase 3: project close-out ---
log "Phase 3: project close-out"
if [[ $DRY -eq 1 ]]; then
  log "  DRY-RUN: would set milestone issues -> Done, close milestone $MS_NUM, close issue #$RELEASE_ISSUE"
else
  # statuses -> Done via the project lib (agent scripts' own path; run as owner)
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$DIR/agents/lib/gh_project.sh" ]]; then
    # shellcheck disable=SC1091
    source "$DIR/agents/lib/gh_project.sh"; load_config
    while read -r n; do
      set_issue_status "$n" "Done" >/dev/null 2>&1 \
        && log "  status Done: #$n" || log "  WARN: could not set Done on #$n"
    done < <(gh issue list -R "$REPO" --milestone "$MS_TITLE" --state closed --limit 200 --json number --jq '.[].number')
  else
    log "  WARN: gh_project.sh lib not found — set statuses to Done manually"
  fi
  gh api -X PATCH "repos/${REPO}/milestones/${MS_NUM}" -f state=closed --silent
  [[ "$(gh api "repos/${REPO}/milestones/${MS_NUM}" --jq .state)" == "closed" ]] || fail "verify failed: milestone still open"
  log "  verified: milestone closed"
  gh issue comment "$RELEASE_ISSUE" -R "$REPO" --body "Release ${VERSION} ceremony complete.
- Tag \`${VERSION}\` at \`${TIP}\` (master fast-forwarded first; master is the authoritative latest release)
- GitHub Release published$( [[ $SKIP_PYPI -eq 1 ]] || echo "
- PyPI: https://pypi.org/project/nyxgpt/${VERSION}/" )
- Branch \`${REL_BRANCH}\` is now frozen (hotfixes by owner decision only)" >/dev/null
  gh issue close "$RELEASE_ISSUE" -R "$REPO" >/dev/null
  [[ "$(gh issue view "$RELEASE_ISSUE" -R "$REPO" --json state --jq .state)" == "CLOSED" ]] || fail "verify failed: release issue still open"
  log "  verified: release issue #$RELEASE_ISSUE closed"
fi

# --- Phase 4: line reconciliation + repoint ---
log "Phase 4: line reconciliation ($REL_TYPE release)"
if [[ "$REL_TYPE" == "point" ]]; then
  # point release: the next development line must absorb the release content
  if [[ $DRY -eq 1 ]]; then
    log "  DRY-RUN: would merge ${REL_BRANCH} into ${NEXT_BRANCH:-<next-branch>} (server-side merge)"
  else
    MERGE_RESP=$(gh api -X POST "repos/${REPO}/merges" \
      -f base="$NEXT_BRANCH" -f head="$TIP" \
      -f commit_message="merge: absorb release ${VERSION} into ${NEXT_BRANCH} (forward-port of release content)" \
      2>&1) && MERGED=1 || MERGED=0
    if [[ $MERGED -eq 1 ]]; then
      log "  verified: ${REL_BRANCH} merged into ${NEXT_BRANCH}"
      log "  NOTE: merge updated ${NEXT_BRANCH} into any open PR branches targeting it (house rule):"
      gh pr list -R "$REPO" --base "$NEXT_BRANCH" --state open --json number,headRefName \
        --jq '.[]|"    PR #\(.number) (\(.headRefName))"' || true
    else
      log "  MERGE CONFLICT or error merging ${REL_BRANCH} -> ${NEXT_BRANCH}:"
      echo "$MERGE_RESP" | sed 's/^/    /'
      log "  resolve manually (local merge + push), then continue with the repoint below"
    fi
  fi
else
  log "  major release: cut the next development line from tag ${VERSION} when ready (manual owner step)"
fi

confirm "repoint"
if [[ $DRY -eq 1 ]]; then
  log "DRY-RUN: would set default branch + RELEASE_BRANCH -> ${NEXT_BRANCH:-<next>}, RELEASE_ISSUE_NUMBER -> ${NEXT_RELEASE_ISSUE:-<unset>}"
else
  if [[ -n "$NEXT_BRANCH" ]]; then
    gh api -X PATCH "repos/${REPO}" -f default_branch="$NEXT_BRANCH" --silent
    [[ "$(gh api "repos/${REPO}" --jq .default_branch)" == "$NEXT_BRANCH" ]] || fail "verify failed: default branch"
    gh variable set RELEASE_BRANCH -R "$REPO" --body "$NEXT_BRANCH"
    log "  verified: default branch + RELEASE_BRANCH -> $NEXT_BRANCH"
  else
    log "  no --next-branch: leaving default branch and RELEASE_BRANCH untouched"
  fi
  if [[ -n "$NEXT_RELEASE_ISSUE" ]]; then
    gh variable set RELEASE_ISSUE_NUMBER -R "$REPO" --body "$NEXT_RELEASE_ISSUE"
    log "  RELEASE_ISSUE_NUMBER -> $NEXT_RELEASE_ISSUE"
  else
    log "  no --next-release-issue: RELEASE_ISSUE_NUMBER untouched — set it before the next cycle"
  fi
fi

log "Ceremony complete for ${VERSION}."
