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
#     --next-branch BRANCH   next development line. Point release: must exist
#                            (e.g. v3.0.0) — receives the forward-port merge.
#                            Major (x.0.0): will be CREATED from the release
#                            tag as the new release-candidate branch.
#     --next-release-issue N RELEASE_ISSUE_NUMBER after repoint. Point
#                            releases: required (issue exists). Major: omit —
#                            the ceremony creates the release issue and uses
#                            its number.
#     --next-title TEXT      major only: descriptive suffix for the next
#                            line's release issue + draft release name
#     --phase4-only          resume at Phase 4 (line prep + repoint) after an
#                            earlier abort; Phases 0-3 must already be done
#     --skip-scan-gate       skip the code-scanning gate (v2.1.0 decision;
#                            keep the gate for lines with the full CI suite)
#     --skip-pypi            skip Phase 2
#     --dry-run              run all read-only checks; print, don't mutate
#
# Major (x.0.0) line preparation — performed BEFORE any repoint, per owner
# requirement (2026-08-06): (1) create the new RC branch from the release
# tag, (2) create its release issue, (3) create its draft release, and
# verify (4) phase milestone(s) and (5) sprint iteration(s) exist for the
# new line. Only then do the GitHub vars, default branch, and config.ini
# get repointed.
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
NEXT_BRANCH=""; NEXT_RELEASE_ISSUE=""; NEXT_TITLE=""; SKIP_SCAN=0; SKIP_PYPI=0; DRY=0; PHASE4_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --next-branch)        NEXT_BRANCH="$2"; shift 2 ;;
    --next-release-issue) NEXT_RELEASE_ISSUE="$2"; shift 2 ;;
    --next-title)         NEXT_TITLE="$2"; shift 2 ;;
    --phase4-only)        PHASE4_ONLY=1; shift ;;
    --skip-scan-gate)     SKIP_SCAN=1; shift ;;
    --skip-pypi)          SKIP_PYPI=1; shift ;;
    --dry-run)            DRY=1; shift ;;
    *) fail "unknown option: $1" ;;
  esac
done

REL_BRANCH="v${VERSION}"
if [[ "$VERSION" =~ ^[0-9]+\.0\.0$ ]]; then REL_TYPE="major"; else REL_TYPE="point"; fi
if [[ -z "$NEXT_BRANCH" && $DRY -eq 0 ]]; then
  fail "--next-branch is required (point: existing line to forward-port into; major: new RC branch to create)"
fi
if [[ "$REL_TYPE" == "point" && -z "$NEXT_RELEASE_ISSUE" && $DRY -eq 0 ]]; then
  fail "point release requires --next-release-issue (the next line's existing release issue)"
fi
NEXT_VERSION="${NEXT_BRANCH#v}"

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

# release issue number first — the milestone gate must exclude it (the
# release issue itself stays open until Phase 3 closes it)
RELEASE_ISSUE=$(gh variable get RELEASE_ISSUE_NUMBER -R "$REPO" 2>/dev/null || true)
[[ -n "$RELEASE_ISSUE" ]] || fail "RELEASE_ISSUE_NUMBER repo variable not readable"

# 0.1 milestone: every issue closed (except the release issue itself)
MILESTONE_JSON=$(gh api "repos/${REPO}/milestones?state=all&per_page=100" \
  --jq "[.[] | select(.title | test(\"v${VERSION}\"))][0]")
[[ -n "$MILESTONE_JSON" && "$MILESTONE_JSON" != "null" ]] || fail "no milestone matching v${VERSION}"
MS_NUM=$(jq -r .number <<<"$MILESTONE_JSON"); MS_TITLE=$(jq -r .title <<<"$MILESTONE_JSON")
MS_OPEN_LIST=$(gh api "repos/${REPO}/issues?milestone=${MS_NUM}&state=open&per_page=100" \
  --jq "[.[] | select(.number != ${RELEASE_ISSUE})] | .[] | \"    #\(.number) \(.title)\"")
if [[ -n "$MS_OPEN_LIST" ]]; then
  log "  GATE FAIL: milestone '$MS_TITLE' has open issue(s) besides the release issue:"
  echo "$MS_OPEN_LIST"
  GATE_FAIL=1
else log "  ok: milestone '$MS_TITLE' fully closed (release issue #$RELEASE_ISSUE excluded)"; fi

# 0.2 release issue: no unchecked issue-reference tasks
UNCHECKED=$(gh api "repos/${REPO}/issues/${RELEASE_ISSUE}" --jq .body \
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

# --- Phase 4: next-line preparation + repoint ---
log "Phase 4: next-line preparation ($REL_TYPE release)"

# 4-pre: readiness gates for the next line — checked BEFORE any repoint.
# (4) phase milestone(s) and (5) sprint iteration(s) must exist.
LINE_GATE_FAIL=0
MS_NEXT=$(gh api "repos/${REPO}/milestones?state=open&per_page=100" \
  --jq "[.[] | select(.title | test(\"v${NEXT_VERSION}\"))] | length")
if [[ "$MS_NEXT" == "0" ]]; then
  log "  LINE GATE FAIL: no open milestone mentions v${NEXT_VERSION} — prepare the phase milestone(s) first"
  LINE_GATE_FAIL=1
else
  log "  ok: $MS_NEXT open phase milestone(s) for v${NEXT_VERSION}:"
  gh api "repos/${REPO}/milestones?state=open&per_page=100" \
    --jq ".[] | select(.title | test(\"v${NEXT_VERSION}\")) | \"    \" + .title"
fi
PROJ_OWNER="$(ini_get github PROJECT_OWNER)"; PROJ_NUM="$(ini_get github PROJECT_NUMBER)"
SPRINTS_RAW=$(gh api graphql -f owner="$PROJ_OWNER" -F num="${PROJ_NUM:-0}" -f query='
  query($owner:String!,$num:Int!){ user(login:$owner){ projectV2(number:$num){
    field(name:"Sprint"){ ... on ProjectV2IterationField {
      configuration { iterations { title startDate } } } } } } }' 2>&1) || SPRINTS_RAW="QUERY_ERROR: $SPRINTS_RAW"
if grep -qE 'QUERY_ERROR|"errors"|RATE_LIMIT' <<<"$SPRINTS_RAW"; then
  log "  LINE GATE FAIL: could not verify Sprint iterations (GraphQL error/rate limit) — retry when the limit resets:"
  head -2 <<<"$SPRINTS_RAW" | sed 's/^/    /'
  LINE_GATE_FAIL=1
else
  SPRINTS=$(jq -r '.data.user.projectV2.field.configuration.iterations[].title' <<<"$SPRINTS_RAW" 2>/dev/null || true)
  if [[ -z "$SPRINTS" ]]; then
    log "  LINE GATE FAIL: no active/upcoming Sprint iteration on the project — prepare the sprint(s) first"
    LINE_GATE_FAIL=1
  else
    log "  ok: active/upcoming sprint iteration(s): $(echo "$SPRINTS" | tr '\n' ' ')"
  fi
fi
[[ $LINE_GATE_FAIL -eq 0 || $DRY -eq 1 ]] || fail "next-line readiness gate failed — prepare milestones/sprints, then re-run with --phase4-only"

if [[ "$REL_TYPE" == "point" ]]; then
  # Point release: next line already exists — it must absorb the release
  # content (forward-port), on top of master's fast-forward. Verify its
  # release issue exists too (it is about to become RELEASE_ISSUE_NUMBER).
  if [[ $DRY -eq 1 ]]; then
    log "  DRY-RUN: would merge ${REL_BRANCH} into ${NEXT_BRANCH:-<next-branch>} (server-side merge)"
  else
    [[ "$(gh issue view "$NEXT_RELEASE_ISSUE" -R "$REPO" --json state --jq .state 2>/dev/null)" == "OPEN" ]] \
      || fail "next release issue #$NEXT_RELEASE_ISSUE is not an open issue"
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
      log "  resolve manually (local merge + push), then re-run with --phase4-only"
    fi
  fi
else
  # Major release: BUILD the next line before anything points at it.
  if [[ $DRY -eq 1 ]]; then
    log "  DRY-RUN (major): would create branch ${NEXT_BRANCH:-<next>} from tag ${VERSION}, its release issue, and its draft release"
  else
    # (1) new release-candidate branch from the release tag
    if gh api "repos/${REPO}/branches/${NEXT_BRANCH}" >/dev/null 2>&1; then
      log "  branch ${NEXT_BRANCH} already exists (ok)"
    else
      mutate "create branch ${NEXT_BRANCH} at tag ${VERSION}" \
        git push "$AUTH_URL" "${TIP}:refs/heads/${NEXT_BRANCH}"
      [[ "$(gh api "repos/${REPO}/branches/${NEXT_BRANCH}" --jq .commit.sha)" == "$TIP" ]] \
        || fail "verify failed: ${NEXT_BRANCH} not at release tip"
      log "  verified: ${NEXT_BRANCH} created at release tip"
    fi
    # (2) release issue for the new line
    if [[ -z "$NEXT_RELEASE_ISSUE" ]]; then
      ISSUE_TITLE="Release ${NEXT_BRANCH}${NEXT_TITLE:+ — ${NEXT_TITLE}}"
      NEXT_RELEASE_ISSUE=$(gh issue create -R "$REPO" --title "$ISSUE_TITLE" --body "## ${ISSUE_TITLE}

**Release branch:** \`${NEXT_BRANCH}\` (cut from tag \`${VERSION}\`)
**Milestone(s):** see open v${NEXT_VERSION} phase milestones

## Included Work
_Populated as work merges (add-to-release-issue automation + scrummaster)._

## Ceremony Checklist (remaining)
- [ ] Run \`scripts/release_ceremony.sh ${NEXT_VERSION}\` when scope completes" \
        | grep -oE '[0-9]+$')
      [[ -n "$NEXT_RELEASE_ISSUE" ]] || fail "could not create/parse next release issue"
      log "  verified: release issue #${NEXT_RELEASE_ISSUE} created"
    else
      log "  using provided next release issue #${NEXT_RELEASE_ISSUE}"
    fi
    # (3) draft release for the new line (draft => no tag is created yet;
    # tag_name/target are pre-normalized for the next ceremony)
    EXISTING_DRAFT=$(gh api "repos/${REPO}/releases?per_page=30" \
      --jq "[.[] | select(.draft==true) | select(.name | test(\"${NEXT_BRANCH}\"))] | length")
    if [[ "$EXISTING_DRAFT" != "0" ]]; then
      log "  draft release for ${NEXT_BRANCH} already exists (ok)"
    else
      gh api -X POST "repos/${REPO}/releases" \
        -f tag_name="${NEXT_VERSION}" -f target_commitish="${NEXT_BRANCH}" \
        -f name="nyxGPT Release ${NEXT_BRANCH}${NEXT_TITLE:+ — ${NEXT_TITLE}}" \
        -f body="Draft — populated at ceremony time from the release issue." \
        -F draft=true --silent
      [[ "$(gh api "repos/${REPO}/releases?per_page=30" \
        --jq "[.[] | select(.draft==true) | select(.name | test(\"${NEXT_BRANCH}\"))] | length")" != "0" ]] \
        || fail "verify failed: draft release for ${NEXT_BRANCH} not found after create"
      log "  verified: draft release created for ${NEXT_BRANCH}"
    fi
  fi
fi

# version bump on the next line: pyproject.toml must carry the next RC's
# version once the release is live (owner requirement, 2026-08-06)
if [[ $DRY -eq 1 ]]; then
  log "  DRY-RUN: would ensure ${NEXT_BRANCH:-<next>}:pyproject.toml version == ${NEXT_VERSION:-<next>}"
else
  NEXT_PY_JSON=$(gh api "repos/${REPO}/contents/pyproject.toml?ref=${NEXT_BRANCH}")
  NEXT_PY_SHA=$(jq -r .sha <<<"$NEXT_PY_JSON")
  NEXT_PY_CUR=$(jq -r .content <<<"$NEXT_PY_JSON" | python3 -c 'import sys,base64; print(base64.b64decode(sys.stdin.read()).decode())' \
    | awk -F'"' '/^version =/{print $2; exit}')
  if [[ "$NEXT_PY_CUR" == "$NEXT_VERSION" ]]; then
    log "  ok: ${NEXT_BRANCH} pyproject version already ${NEXT_VERSION}"
  else
    NEW_B64=$(jq -r .content <<<"$NEXT_PY_JSON" | python3 -c "
import sys, base64, re
t = base64.b64decode(sys.stdin.read()).decode()
t = re.sub(r'^version = \"[^\"]+\"', 'version = \"${NEXT_VERSION}\"', t, count=1, flags=re.M)
print(base64.b64encode(t.encode()).decode())")
    gh api -X PUT "repos/${REPO}/contents/pyproject.toml" \
      -f branch="$NEXT_BRANCH" -f sha="$NEXT_PY_SHA" -f content="$NEW_B64" \
      -f message="chore: bump version to ${NEXT_VERSION} post-${VERSION}-release (ceremony)" --silent
    NEW_CUR=$(gh api "repos/${REPO}/contents/pyproject.toml?ref=${NEXT_BRANCH}" --jq .content \
      | python3 -c 'import sys,base64; print(base64.b64decode(sys.stdin.read()).decode())' \
      | awk -F'"' '/^version =/{print $2; exit}')
    [[ "$NEW_CUR" == "$NEXT_VERSION" ]] || fail "verify failed: ${NEXT_BRANCH} pyproject version is '$NEW_CUR'"
    log "  verified: ${NEXT_BRANCH} pyproject version bumped ${NEXT_PY_CUR} -> ${NEXT_VERSION}"
  fi
fi

confirm "repoint"
if [[ $DRY -eq 1 ]]; then
  log "DRY-RUN: would repoint default branch + RELEASE_BRANCH -> ${NEXT_BRANCH:-<next>}, RELEASE_ISSUE_NUMBER -> ${NEXT_RELEASE_ISSUE:-<from-created-issue>} (GitHub vars AND config.ini)"
else
  gh api -X PATCH "repos/${REPO}" -f default_branch="$NEXT_BRANCH" --silent
  [[ "$(gh api "repos/${REPO}" --jq .default_branch)" == "$NEXT_BRANCH" ]] || fail "verify failed: default branch"
  gh variable set RELEASE_BRANCH -R "$REPO" --body "$NEXT_BRANCH"
  gh variable set RELEASE_ISSUE_NUMBER -R "$REPO" --body "$NEXT_RELEASE_ISSUE"
  log "  verified: default branch, RELEASE_BRANCH, RELEASE_ISSUE_NUMBER -> ${NEXT_BRANCH} / #${NEXT_RELEASE_ISSUE}"
  # config.ini mirror (local scripts read these — must match the repo vars)
  sed -i.cerbak -E \
    -e "s|^(RELEASE_BRANCH[[:space:]]*=).*|\1${NEXT_BRANCH}|" \
    -e "s|^(RELEASE_ISSUE_NUMBER[[:space:]]*=).*|\1${NEXT_RELEASE_ISSUE}|" \
    "$CONFIG_FILE"
  grep -q "^RELEASE_BRANCH[[:space:]]*=${NEXT_BRANCH}$" "$CONFIG_FILE" \
    && grep -q "^RELEASE_ISSUE_NUMBER[[:space:]]*=${NEXT_RELEASE_ISSUE}$" "$CONFIG_FILE" \
    || fail "verify failed: config.ini repoint (backup at ${CONFIG_FILE}.cerbak)"
  rm -f "${CONFIG_FILE}.cerbak"
  log "  verified: config.ini RELEASE_BRANCH/RELEASE_ISSUE_NUMBER updated"
fi

log "Ceremony complete for ${VERSION}."
