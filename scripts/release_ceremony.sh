#!/usr/bin/env bash
# release_ceremony.sh — owner-run release ceremony for nyxGPT.
#
# Encodes the ceremony agreed 2026-08-06 (owner + assistant walkthrough):
#   Phase 0  Entry gate (read-only checks)            -> STOP: "ship it"
#   Phase 1  master fast-forward, then publish the draft release
#            (master is always the authoritative latest release; the
#            release tag is created on master AFTER the fast-forward)
#   Phase 2  PyPI publish — DELEGATED to the single publish pipeline
#            (.github/workflows/release-publish-pypi.yml, channel `stable`).
#            The ceremony no longer builds or uploads anything itself: there
#            is one build/publish core for rc and stable (#3727, owner
#            decision 2026-08-11; channels revised by #3735), it
#            authenticates with PyPI Trusted
#            Publishing (OIDC), and no PyPI token is stored anywhere.
#   Phase 3  Project close-out (statuses -> Done, milestone + issue close)
#   Phase 4  Next line + repoint
#            the next line is named by the open "(vX.Y.Z)" milestone; it is
#            created from the release tag (or, if it already exists, the
#            release is forward-ported into it), its release issue and draft
#            release are created, and the default branch / RELEASE_BRANCH /
#            RELEASE_ISSUE_NUMBER are repointed to it.
#
# Agent pause (owner requirement, 2026-10-07): Phase 0 sets AGENTS_ENABLED,
# SPRINT_AUTOPILOT and CLAUDE_REVIEW_ENABLED to false so nothing merges into
# the line being released, saving their prior values in the repo variable
# CEREMONY_PAUSED_FLAGS. Phase 4 restores them once the repoint is done, so the
# agents resume on the NEW line. A ceremony that stops part-way leaves them
# paused on purpose; re-running resumes and restores them.
#
# Normally run by .github/workflows/release_ceremony.yml (dispatched by a
# session on the owner's instruction) with --unattended and the ceremony PAT.
# Run locally, credentials come from ~/.nyxGPT/config.ini:
#   [github] PAT          — owner PAT (ruleset bypass: master push, repoint)
# No PyPI credential is needed here: Phase 2 dispatches the publish workflow,
# which uploads with Trusted Publishing (OIDC) from GitHub Actions.
#
# Usage:
#   scripts/release_ceremony.sh VERSION [options]
#     VERSION                e.g. 2.1.0  (release branch is v<VERSION>)
#   Options:
#     --next-branch BRANCH   next development line. Default: derived from
#                            the lowest open milestone titled "(vX.Y.Z)" with
#                            X.Y.Z above VERSION. Created from the release tag
#                            if missing; forward-ported into if it exists.
#     --next-release-issue N the next line's release issue. Default: an open
#                            `Release Management` issue naming the branch, or
#                            a new one created by the ceremony.
#     --next-title TEXT      suffix for the next release issue / draft name.
#                            Default: the milestone title minus "Phase N —"
#                            and "(vX.Y.Z)".
#     --phase4-only          run only Phase 4, after verifying Phases 0-3
#                            completed (tag published, master contains it).
#                            release_ceremony_watch.sh selects this itself
#                            when the release tag already exists.
#     --skip-scan-gate       skip the code-scanning gate (v2.1.0 decision;
#                            keep the gate for lines with the full CI suite)
#     --skip-pypi            skip Phase 2
#     --unattended           no interactive stop points: every `confirm`
#                            passes automatically. Used by the automated
#                            ceremony (#3730), where the owner's move of the
#                            release issue to `For Release` IS the sign-off.
#                            Credentials come from NYXGPT_CEREMONY_PAT (or
#                            GH_TOKEN) instead of ~/.nyxGPT/config.ini.
#     --stop-after-phase N   stop cleanly after phase N (0-4, default 4).
#                            The automated ceremony runs all five phases
#                            (owner decision 2026-10-07; it used to stop
#                            after Phase 3 and leave Phase 4 to the owner).
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
UNATTENDED=0; STOP_AFTER_PHASE=4
while [[ $# -gt 0 ]]; do
  case "$1" in
    --next-branch)        NEXT_BRANCH="$2"; shift 2 ;;
    --next-release-issue) NEXT_RELEASE_ISSUE="$2"; shift 2 ;;
    --next-title)         NEXT_TITLE="$2"; shift 2 ;;
    --phase4-only)        PHASE4_ONLY=1; shift ;;
    --skip-scan-gate)     SKIP_SCAN=1; shift ;;
    --skip-pypi)          SKIP_PYPI=1; shift ;;
    --unattended)         UNATTENDED=1; shift ;;
    --stop-after-phase)   STOP_AFTER_PHASE="$2"; shift 2 ;;
    --dry-run)            DRY=1; shift ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ "$STOP_AFTER_PHASE" =~ ^[0-4]$ ]] || fail "--stop-after-phase takes 0-4, got: $STOP_AFTER_PHASE"

REL_BRANCH="v${VERSION}"
if [[ "$VERSION" =~ ^[0-9]+\.0\.0$ ]]; then REL_TYPE="major"; else REL_TYPE="point"; fi
# The next line is resolved in Phase 4 (from the open milestones) unless
# given explicitly, so an unattended run needs no next-line arguments.
NEXT_VERSION="${NEXT_BRANCH#v}"

# --- credentials ---
ini_get() { # ini_get SECTION KEY
  awk -F'=' -v sec="[$1]" -v key="$2" '
    $0==sec {s=1; next} /^\[/{s=0}
    s && $1 ~ "^"key"[ \t]*$" {v=$2; gsub(/[ \t]/,"",v); print v; exit}' "$CONFIG_FILE"
}
# Unattended runs (the automated ceremony, #3730) have no config.ini on the
# runner: the credential arrives as NYXGPT_CEREMONY_PAT (or an already-
# exported GH_TOKEN). It still has to be an owner-level token -- Phase 1
# pushes master, which the ruleset only lets the owner bypass.
if [[ -f "$CONFIG_FILE" ]]; then
  PAT="$(ini_get github PAT)"
else
  PAT=""
fi
PAT="${NYXGPT_CEREMONY_PAT:-${PAT:-${GH_TOKEN:-}}}"
[[ -n "$PAT" ]] || fail "no credential: set NYXGPT_CEREMONY_PAT, or add [github] PAT to $CONFIG_FILE"
# Phase 2 delegates the upload to the publish workflow (Trusted Publishing),
# so the ceremony holds no PyPI credential at all.
PUBLISH_WORKFLOW="release-publish-pypi.yml"
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
  # Unattended: the owner's move of the release issue to `For Release` is
  # the sign-off (owner decision 2026-08-12, #3730), so there is no second
  # human confirmation to collect -- and no TTY to collect it on.
  [[ $UNATTENDED -eq 1 ]] && { log "UNATTENDED: stop point '$1' auto-confirmed (release-issue sign-off)"; return 0; }
  echo
  read -r -p "[ceremony] STOP POINT — type '$1' to continue: " ans
  [[ "$ans" == "$1" ]] || fail "aborted at stop point (expected '$1')"
}

# Project owner/number: the owner's config.ini ([github] section), else the
# repo variable. The runner's ephemeral config has no [github] section, so
# without the fallback Phase 4's sprint check queried an empty owner.
project_setting() { # project_setting KEY
  local v=""
  [[ -f "$CONFIG_FILE" ]] && v="$(ini_get github "$1")"
  [[ -n "$v" ]] || v="$(gh variable get "$1" -R "$REPO" 2>/dev/null || true)"
  printf '%s' "$v"
}

AGENT_FLAGS=(AGENTS_ENABLED SPRINT_AUTOPILOT CLAUDE_REVIEW_ENABLED)
PAUSED_FLAGS_VAR="CEREMONY_PAUSED_FLAGS"

# Pause the agent loop for the length of the ceremony. The prior values are
# saved ONCE: if a previous run stopped part-way the saved values are the
# owner's real settings and the current `false`s are ours, so they must not
# overwrite them.
pause_agent_flags() {
  if [[ $DRY -eq 1 ]]; then log "DRY-RUN: would pause ${AGENT_FLAGS[*]} (saving prior values in ${PAUSED_FLAGS_VAR})"; return 0; fi
  local saved f cur pairs=()
  saved="$(gh variable get "$PAUSED_FLAGS_VAR" -R "$REPO" 2>/dev/null || true)"
  if [[ -z "$saved" ]]; then
    for f in "${AGENT_FLAGS[@]}"; do
      cur="$(gh variable get "$f" -R "$REPO" 2>/dev/null || true)"
      [[ -n "$cur" ]] && pairs+=("${f}=${cur}")
    done
    saved="$(IFS=,; echo "${pairs[*]}")"
    [[ -n "$saved" ]] || { log "  agents: none of ${AGENT_FLAGS[*]} is set -- nothing to pause"; return 0; }
    gh variable set "$PAUSED_FLAGS_VAR" -R "$REPO" --body "$saved"
    [[ "$(gh variable get "$PAUSED_FLAGS_VAR" -R "$REPO")" == "$saved" ]] || fail "verify failed: could not save agent flags"
  else
    log "  agents: a previous run already saved the owner's flags (${saved}) -- keeping them"
  fi
  local pair
  IFS=, read -r -a pairs <<<"$saved"
  for pair in "${pairs[@]}"; do
    f="${pair%%=*}"
    gh variable set "$f" -R "$REPO" --body false
    [[ "$(gh variable get "$f" -R "$REPO")" == "false" ]] || fail "verify failed: ${f} not paused"
  done
  log "  agents paused for the ceremony: ${saved} -> false (restored after Phase 4)"
}

resume_agent_flags() {
  if [[ $DRY -eq 1 ]]; then log "DRY-RUN: would restore the agent flags saved in ${PAUSED_FLAGS_VAR}"; return 0; fi
  local saved pair f v pairs=()
  saved="$(gh variable get "$PAUSED_FLAGS_VAR" -R "$REPO" 2>/dev/null || true)"
  [[ -n "$saved" ]] || { log "  agents: no paused flags to restore"; return 0; }
  IFS=, read -r -a pairs <<<"$saved"
  for pair in "${pairs[@]}"; do
    f="${pair%%=*}"; v="${pair#*=}"
    gh variable set "$f" -R "$REPO" --body "$v"
    [[ "$(gh variable get "$f" -R "$REPO")" == "$v" ]] || fail "verify failed: ${f} not restored to ${v}"
  done
  gh variable delete "$PAUSED_FLAGS_VAR" -R "$REPO"
  log "  verified: agent flags restored (${saved})"
}

# Ends the run cleanly after the last phase the caller asked for.
phase_boundary() { # phase_boundary N
  if [[ $STOP_AFTER_PHASE -le $1 ]]; then
    log "Stopping after Phase $1 (--stop-after-phase ${STOP_AFTER_PHASE})."
    exit 0
  fi
}

# =====================================================================
log "Release $VERSION ($REL_TYPE release) — branch $REL_BRANCH"
git fetch origin --tags --quiet

if [[ $PHASE4_ONLY -eq 1 ]]; then
# --- Phases 0-3 already ran: verify that, then go straight to Phase 4 ---
log "Phases 0-3: skipped (--phase4-only) -- verifying they completed"
TIP="$(gh api "repos/${REPO}/commits/${VERSION}" --jq .sha 2>/dev/null)" \
  || fail "tag ${VERSION} does not exist -- Phases 0-3 have not run; drop --phase4-only"
log "  ok: tag ${VERSION} -> ${TIP}"
[[ "$(gh api "repos/${REPO}/releases/tags/${VERSION}" --jq .draft 2>/dev/null)" == "false" ]] \
  || fail "the ${VERSION} GitHub Release is not published -- Phase 1 has not completed"
log "  ok: GitHub Release ${VERSION} is published"
case "$(gh api "repos/${REPO}/compare/${VERSION}...master" --jq .status 2>/dev/null)" in
  identical|ahead) log "  ok: master contains ${VERSION}" ;;
  *) fail "master does not contain tag ${VERSION} -- Phase 1 has not completed" ;;
esac
pause_agent_flags
else

# --- Phase 0: entry gate (read-only apart from pausing the agents) ---
log "Phase 0: entry gate"
pause_agent_flags
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
phase_boundary 0

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
phase_boundary 1

# --- Phase 2: PyPI (delegated to the single publish pipeline) ---
#
# One build/publish core serves rc and stable (#3727). The ceremony's
# only job here is to ask for the `stable` channel and then verify the
# result the same way it verifies every other mutation. The build, the
# twine check, the clean-venv smoke and the upload all happen inside
# release-publish-pypi.yml, with Trusted Publishing instead of a token —
# and the workflow refuses a stable build unless the release tag Phase 1
# just created is at the ref's tip and this confirmation is passed.
if [[ $SKIP_PYPI -eq 1 ]]; then
  log "Phase 2: skipped (--skip-pypi)"
elif [[ $DRY -eq 1 ]]; then
  log "Phase 2 DRY-RUN: would dispatch ${PUBLISH_WORKFLOW} (channel=stable) on ${REL_BRANCH} and wait for pypi.org to serve ${VERSION}"
else
  log "Phase 2: PyPI publish — delegated to ${PUBLISH_WORKFLOW} (channel=stable)"
  DISPATCH_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  gh workflow run "$PUBLISH_WORKFLOW" -R "$REPO" --ref "$REL_BRANCH" \
    -f channel=stable -f confirm=ceremony -f number='' -f dry_run=false \
    || fail "could not dispatch ${PUBLISH_WORKFLOW}"

  # Locate the run we just asked for (dispatch returns nothing identifying
  # it), then wait for it — never assume success from a non-error response.
  RUN_ID=""
  for i in $(seq 1 12); do
    sleep 5
    RUN_ID=$(gh run list -R "$REPO" --workflow "$PUBLISH_WORKFLOW" --event workflow_dispatch \
      --limit 10 --json databaseId,headSha,createdAt \
      --jq "[.[] | select(.headSha==\"${TIP}\") | select(.createdAt >= \"${DISPATCH_AT}\")][0].databaseId" 2>/dev/null || true)
    [[ -n "$RUN_ID" && "$RUN_ID" != "null" ]] && break
  done
  [[ -n "$RUN_ID" && "$RUN_ID" != "null" ]] || fail "dispatched ${PUBLISH_WORKFLOW} but could not find its run — check the Actions tab"
  log "  run: https://github.com/${REPO}/actions/runs/${RUN_ID}"

  CONCLUSION=""
  for i in $(seq 1 120); do   # up to ~30 minutes
    STATUS=$(gh run view "$RUN_ID" -R "$REPO" --json status --jq .status)
    [[ "$STATUS" == "completed" ]] && { CONCLUSION=$(gh run view "$RUN_ID" -R "$REPO" --json conclusion --jq .conclusion); break; }
    sleep 15
  done
  [[ "$CONCLUSION" == "success" ]] \
    || fail "publish run ${RUN_ID} ended '${CONCLUSION:-timed out}' — see https://github.com/${REPO}/actions/runs/${RUN_ID}"

  for i in $(seq 1 12); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "https://pypi.org/pypi/nyxgpt/${VERSION}/json")
    [[ "$code" == "200" ]] && break; sleep 10
  done
  [[ "$code" == "200" ]] || fail "verify failed: pypi.org does not serve nyxgpt ${VERSION}"
  log "  verified live: https://pypi.org/project/nyxgpt/${VERSION}/"
fi
phase_boundary 2

# --- Phase 3: project close-out ---
log "Phase 3: project close-out"
if [[ $DRY -eq 1 ]]; then
  log "  DRY-RUN: would set milestone issues -> Done, close milestone $MS_NUM, close issue #$RELEASE_ISSUE"
else
  # statuses -> Done via the project lib (agent scripts' own path; run as owner)
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # The config file is required too: load_config aborts without one, and an
  # unattended run (#3730) that got this far has already published the
  # release — it must degrade to the WARN below, not die at the close-out.
  if [[ -f "$DIR/agents/lib/gh_project.sh" && -f "$CONFIG_FILE" ]]; then
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
phase_boundary 3

fi  # end: Phases 0-3 (skipped under --phase4-only)

# --- Phase 4: next-line preparation + repoint ---
log "Phase 4: next-line preparation"

# 4.0 name the next line. Nothing to type in an unattended run: the owner
# prepares the next line's milestone ("... (vX.Y.Z)"), and that IS the
# decision. Several open lines -> the lowest version above this release.
NEXT_MS_TITLE=""
if [[ -z "$NEXT_BRANCH" ]]; then
  NEXT_PICK="$(gh api "repos/${REPO}/milestones?state=open&per_page=100" --jq '[.[].title]' \
    | python3 -c '
import json, re, sys
cur = tuple(int(x) for x in sys.argv[1].split("."))
best = None
for t in json.load(sys.stdin):
    m = re.search(r"\(v(\d+)\.(\d+)\.(\d+)\)", t)
    if not m:
        continue
    v = tuple(int(x) for x in m.groups())
    if v > cur and (best is None or v < best[0]):
        best = (v, t)
if best:
    print("v" + ".".join(map(str, best[0])) + "\t" + best[1])
' "$VERSION")"
  [[ -n "$NEXT_PICK" ]] || fail "no open milestone names a version above ${VERSION} as \"(vX.Y.Z)\" -- create the next line's milestone (it names the next branch), then re-run"
  NEXT_BRANCH="${NEXT_PICK%%$'\t'*}"; NEXT_MS_TITLE="${NEXT_PICK#*$'\t'}"
  log "  next line: ${NEXT_BRANCH} (from milestone '${NEXT_MS_TITLE}')"
fi
NEXT_VERSION="${NEXT_BRANCH#v}"
if [[ -z "$NEXT_MS_TITLE" ]]; then
  NEXT_MS_TITLE="$(gh api "repos/${REPO}/milestones?state=open&per_page=100" \
    --jq "[.[] | select(.title | test(\"v${NEXT_VERSION}\"))][0].title // empty")"
fi
if [[ -z "$NEXT_TITLE" && -n "$NEXT_MS_TITLE" ]]; then
  NEXT_TITLE="$(sed -E -e 's/[[:space:]]*\(v[0-9]+\.[0-9]+\.[0-9]+\)[[:space:]]*$//' \
    -e 's/^Phase[[:space:]]+[0-9.]+[[:space:]]*[—-][[:space:]]*//' <<<"$NEXT_MS_TITLE")"
fi

# 4-pre: readiness gates for the next line -- checked BEFORE any change.
LINE_GATE_FAIL=0
if [[ -z "$NEXT_MS_TITLE" ]]; then
  log "  LINE GATE FAIL: no open milestone mentions v${NEXT_VERSION} -- prepare the phase milestone first"
  LINE_GATE_FAIL=1
else
  log "  ok: open milestone for v${NEXT_VERSION}: ${NEXT_MS_TITLE}"
fi
PROJ_OWNER="$(project_setting PROJECT_OWNER)"; PROJ_NUM="$(project_setting PROJECT_NUMBER)"
SPRINTS_RAW=$(gh api graphql -f owner="$PROJ_OWNER" -F num="${PROJ_NUM:-0}" -f query='
  query($owner:String!,$num:Int!){ user(login:$owner){ projectV2(number:$num){
    field(name:"Sprint"){ ... on ProjectV2IterationField {
      configuration { iterations { title startDate } } } } } } }' 2>&1) || SPRINTS_RAW="QUERY_ERROR: $SPRINTS_RAW"
if grep -qE 'QUERY_ERROR|"errors"|RATE_LIMIT' <<<"$SPRINTS_RAW"; then
  log "  LINE GATE FAIL: could not verify Sprint iterations (GraphQL error/rate limit) -- retry when the limit resets:"
  head -2 <<<"$SPRINTS_RAW" | sed 's/^/    /'
  LINE_GATE_FAIL=1
else
  SPRINTS=$(jq -r '.data.user.projectV2.field.configuration.iterations[].title' <<<"$SPRINTS_RAW" 2>/dev/null || true)
  if [[ -z "$SPRINTS" ]]; then
    log "  LINE GATE FAIL: no active/upcoming Sprint iteration on the project -- prepare the sprint first"
    LINE_GATE_FAIL=1
  else
    log "  ok: active/upcoming sprint iteration(s): $(echo "$SPRINTS" | tr '\n' ' ')"
  fi
fi
[[ $LINE_GATE_FAIL -eq 0 || $DRY -eq 1 ]] || fail "next-line readiness gate failed -- prepare the milestone/sprint, then re-run (Phases 0-3 are not repeated)"

if [[ $DRY -eq 1 ]]; then
  log "  DRY-RUN: would create ${NEXT_BRANCH} at ${VERSION} (or forward-port into it if it exists), find/create its release issue and draft release, and bump its pyproject to ${NEXT_VERSION}"
else
  # (1) the next line's branch: born from the release tag, or -- if it
  # already exists -- forward-ported so it carries the release content.
  if gh api "repos/${REPO}/branches/${NEXT_BRANCH}" >/dev/null 2>&1; then
    MERGE_CODE=$(gh api -X POST "repos/${REPO}/merges" -f base="$NEXT_BRANCH" -f head="$TIP" \
      -f commit_message="merge: absorb release ${VERSION} into ${NEXT_BRANCH} (forward-port of release content)" \
      -i 2>&1 | awk 'NR==1{print $2}') || true
    case "$MERGE_CODE" in
      201) log "  verified: ${VERSION} forward-ported into existing ${NEXT_BRANCH}" ;;
      204) log "  ok: ${NEXT_BRANCH} already contains ${VERSION}" ;;
      *)   fail "could not forward-port ${VERSION} into ${NEXT_BRANCH} (HTTP ${MERGE_CODE:-?}; 409 = conflict) -- resolve with a local merge + push, then re-run" ;;
    esac
  else
    mutate "create branch ${NEXT_BRANCH} at tag ${VERSION}" \
      git push "$AUTH_URL" "${TIP}:refs/heads/${NEXT_BRANCH}"
    [[ "$(gh api "repos/${REPO}/branches/${NEXT_BRANCH}" --jq .commit.sha)" == "$TIP" ]] \
      || fail "verify failed: ${NEXT_BRANCH} not at release tip"
    log "  verified: ${NEXT_BRANCH} created at ${VERSION}"
  fi
  # Anything committed to the release branch AFTER its tag -- a fix to the
  # ceremony itself, made while running it -- must carry forward, or the next
  # line (and its ceremony) silently lacks it. 204 = nothing past the tag.
  POST_TAG_CODE=$(gh api -X POST "repos/${REPO}/merges" -f base="$NEXT_BRANCH" -f head="$REL_BRANCH" \
    -f commit_message="merge: carry ${REL_BRANCH} (post-${VERSION} commits) into ${NEXT_BRANCH}" \
    -i 2>&1 | awk 'NR==1{print $2}') || true
  case "$POST_TAG_CODE" in
    201) log "  verified: post-${VERSION} commits on ${REL_BRANCH} carried into ${NEXT_BRANCH}" ;;
    204) log "  ok: ${NEXT_BRANCH} already has everything on ${REL_BRANCH}" ;;
    *)   fail "could not carry ${REL_BRANCH} into ${NEXT_BRANCH} (HTTP ${POST_TAG_CODE:-?}; 409 = conflict) -- resolve with a local merge + push, then re-run" ;;
  esac

  # (2) the next line's release issue -- found if it exists (re-runs are
  # idempotent), else created carrying what the release machinery reads:
  # the `Release Management` label (drain gate, promotion sweep), the
  # milestone, and a place on the project board.
  if [[ -z "$NEXT_RELEASE_ISSUE" ]]; then
    # The plain list, NOT --search: the search index lags, so a resume right
    # after a create would miss the issue it just made and file a duplicate.
    NEXT_RELEASE_ISSUE="$(gh issue list -R "$REPO" --state open --label "Release Management" \
      --limit 100 --json number,title \
      --jq "[.[] | select(.title | startswith(\"Release ${NEXT_BRANCH}\"))][0].number // empty")"
  fi
  if [[ -n "$NEXT_RELEASE_ISSUE" ]]; then
    log "  ok: next release issue #${NEXT_RELEASE_ISSUE}"
  else
    ISSUE_TITLE="Release ${NEXT_BRANCH}${NEXT_TITLE:+ — ${NEXT_TITLE}}"
    ISSUE_URL="$(gh issue create -R "$REPO" --title "$ISSUE_TITLE" --label "Release Management" \
      ${NEXT_MS_TITLE:+--milestone "$NEXT_MS_TITLE"} --body "## ${ISSUE_TITLE}

**Release branch:** \`${NEXT_BRANCH}\` (cut from tag \`${VERSION}\`)
**Milestone:** ${NEXT_MS_TITLE:-see open v${NEXT_VERSION} milestones}

## Included Work
_Populated as work merges (add-to-release-issue automation + scrummaster)._

## Ceremony
Move this issue to \`For Release\` and ask a session to run the release ceremony
(\`gh workflow run release_ceremony.yml\`). It runs all five phases unattended.")"
    NEXT_RELEASE_ISSUE="${ISSUE_URL##*/}"
    [[ "$NEXT_RELEASE_ISSUE" =~ ^[0-9]+$ ]] || fail "could not create/parse the next release issue (${ISSUE_URL})"
    [[ "$(gh issue view "$NEXT_RELEASE_ISSUE" -R "$REPO" --json labels --jq '[.labels[].name] | index("Release Management") != null')" == "true" ]] \
      || fail "verify failed: #${NEXT_RELEASE_ISSUE} is missing the Release Management label"
    if [[ -n "$PROJ_OWNER" && -n "$PROJ_NUM" ]]; then
      gh project item-add "$PROJ_NUM" --owner "$PROJ_OWNER" --url "$ISSUE_URL" >/dev/null \
        || log "  WARN: could not add #${NEXT_RELEASE_ISSUE} to project ${PROJ_NUM} -- add it by hand"
    fi
    log "  verified: release issue #${NEXT_RELEASE_ISSUE} created (${ISSUE_TITLE})"
  fi

  # (3) draft release for the next line (draft => no tag yet; tag_name and
  # target are pre-set for its ceremony's Phase 1)
  if [[ "$(gh api "repos/${REPO}/releases?per_page=30" \
        --jq "[.[] | select(.draft==true) | select(.name | test(\"${NEXT_BRANCH}\"))] | length")" != "0" ]]; then
    log "  ok: draft release for ${NEXT_BRANCH} already exists"
  else
    DRAFT_NEW_ID="$(gh api -X POST "repos/${REPO}/releases" \
      -f tag_name="${NEXT_VERSION}" -f target_commitish="${NEXT_BRANCH}" \
      -f name="nyxGPT Release ${NEXT_BRANCH}${NEXT_TITLE:+ — ${NEXT_TITLE}}" \
      -f body="Draft — populated at ceremony time from the release issue." \
      -F draft=true --jq .id)"
    # Verify by the id the create returned. Re-listing releases lags the
    # write: 3.0.0's Phase 4 (run 37569589877) created this draft and then
    # failed to find it in the list a second later.
    [[ "$(gh api "repos/${REPO}/releases/${DRAFT_NEW_ID}" --jq .draft 2>/dev/null)" == "true" ]] \
      || fail "verify failed: draft release ${DRAFT_NEW_ID:-?} for ${NEXT_BRANCH} not readable after create"
    log "  verified: draft release created for ${NEXT_BRANCH}"
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
  log "DRY-RUN: would repoint default branch + RELEASE_BRANCH -> ${NEXT_BRANCH}, RELEASE_ISSUE_NUMBER -> ${NEXT_RELEASE_ISSUE:-<created issue>}"
else
  gh api -X PATCH "repos/${REPO}" -f default_branch="$NEXT_BRANCH" --silent
  [[ "$(gh api "repos/${REPO}" --jq .default_branch)" == "$NEXT_BRANCH" ]] || fail "verify failed: default branch"
  gh variable set RELEASE_BRANCH -R "$REPO" --body "$NEXT_BRANCH"
  gh variable set RELEASE_ISSUE_NUMBER -R "$REPO" --body "$NEXT_RELEASE_ISSUE"
  [[ "$(gh variable get RELEASE_BRANCH -R "$REPO")" == "$NEXT_BRANCH" \
     && "$(gh variable get RELEASE_ISSUE_NUMBER -R "$REPO")" == "$NEXT_RELEASE_ISSUE" ]] \
    || fail "verify failed: RELEASE_BRANCH / RELEASE_ISSUE_NUMBER"
  log "  verified: default branch, RELEASE_BRANCH, RELEASE_ISSUE_NUMBER -> ${NEXT_BRANCH} / #${NEXT_RELEASE_ISSUE}"
  # The owner's config.ini mirrors these ([github] keys are synced TO the repo
  # variables, so a stale mirror would push the old line back). Update it
  # when this run has it; a runner's ephemeral config is not the mirror.
  if [[ -f "$CONFIG_FILE" ]] && grep -q '^\[github\]' "$CONFIG_FILE"; then
    sed -i.cerbak -E \
      -e "s|^(RELEASE_BRANCH[[:space:]]*=).*|\1${NEXT_BRANCH}|" \
      -e "s|^(RELEASE_ISSUE_NUMBER[[:space:]]*=).*|\1${NEXT_RELEASE_ISSUE}|" \
      "$CONFIG_FILE"
    grep -qE "^RELEASE_BRANCH[[:space:]]*=${NEXT_BRANCH}$" "$CONFIG_FILE" \
      && grep -qE "^RELEASE_ISSUE_NUMBER[[:space:]]*=${NEXT_RELEASE_ISSUE}$" "$CONFIG_FILE" \
      || fail "verify failed: config.ini repoint (backup at ${CONFIG_FILE}.cerbak)"
    rm -f "${CONFIG_FILE}.cerbak"
    log "  verified: config.ini RELEASE_BRANCH/RELEASE_ISSUE_NUMBER updated"
  else
    log "  NOTE: no owner config.ini on this host -- the session that dispatched the ceremony reconciles ~/.nyxGPT/config.ini [github] RELEASE_BRANCH=${NEXT_BRANCH} RELEASE_ISSUE_NUMBER=${NEXT_RELEASE_ISSUE}"
  fi
fi

resume_agent_flags
log "NEXT_LINE ${NEXT_BRANCH} #${NEXT_RELEASE_ISSUE}"
log "Ceremony complete for ${VERSION}."
