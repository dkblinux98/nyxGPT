#!/usr/bin/env bash
set -uo pipefail

# release_ceremony_watch.sh — automated release ceremony trigger (#3730).
#
# Owner decision 2026-08-12: the owner moving the RELEASE TRACKING ISSUE to
# `For Release` is the human sign-off for the release. From that signal the
# ceremony runs end-to-end unattended — master fast-forward, tag, GitHub
# Release, `stable` publish via the #3727 pipeline, stable tap stamp (via
# release-artifacts.yml, which triggers on the published release) and
# retirement of that line's `-rc` formulas. This supersedes the old
# "master/main merges are human-controlled" rule in CLAUDE.md: the move to
# For Release IS the human control point.
#
# Guardrails (decision logic in lib/ceremony_trigger.py, unit-tested):
#   * only the release tracking issue triggers it;
#   * only the TRANSITION into For Release does — a version-scoped marker
#     comment makes every later dispatch a no-op;
#   * only with a parseable vX.Y.Z version in the issue title.
#
# Phase scope: all five phases (owner decision 2026-10-07): the Phase 0
# prerequisite inventory (which provisions what is missing, then pauses the
# agent flags), master+tag+release, stable publish, close-out, and Phase 4 --
# the next line named by its open "(vX.Y.Z)" milestone is created with its
# release issue and draft release, the repo is repointed to it, and the agent
# flags are restored. No owner step follows the dispatch.
#
# Prerequisites are no longer the owner's to prepare (#4166): Phase 0 creates
# the next line's milestone (as a self-named placeholder), the next sprint
# iteration and this release's draft release if they are missing, and reports
# every gap it CANNOT fill in one pass before anything irreversible runs. The
# tap and Slack wiring this script needs AFTER the publish is part of that
# inventory, so a missing tap token stops the run at Phase 0 instead of at the
# rc retirement with the release already out.
#
# Resume: if the release tag already exists, Phases 0-3 are done and only
# Phase 4 is outstanding, so the ceremony runs with --phase4-only. That makes a
# plain re-dispatch pick up where a failed run stopped, without `force`.
#
# Any failure alerts the owner on the existing Slack DM channel (#3695) in
# addition to a loud comment on the release issue.
#
# Those alerts are NOTIFICATIONS, not escalations (#4134 reviewed every
# owner-facing path and classified each one). All three are about the ceremony
# rather than about a piece of work, and all three are attached to the RELEASE
# TRACKING ISSUE -- owner-assigned by design for the whole life of a release,
# and carrying a `Release Management` label the ceremony, the drain gate and
# the promotion sweep all read. Replacing that label with `Escalation` would
# break the release machinery in order to report that the release machinery is
# broken. Nothing changes hands here: the ceremony stops and the owner
# re-dispatches it (`gh workflow run release_ceremony.yml`; there is no
# schedule since 2026-10-05), so `escalate_to_owner` is not the right verb.
#
# Usage:
#   scripts/agents/release_ceremony_watch.sh [--check-only]
#
# Environment:
#   NYXGPT_CEREMONY_PAT   owner-level token for the ceremony (master push)
#   TAP_REPO / TAP_TOKEN  remote Homebrew tap, for the rc retirement
#   SLACK_BOT_TOKEN / SLACK_USER_ID   owner DM channel (#3695)

# Who any Slack escalation from this script is from (#3911): the ceremony
# watch runs as SCRUMMASTER_AGENT_TOKEN and reports on the release tracking
# issue.
export AGENT_ROLE="${AGENT_ROLE:-scrum}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
# shellcheck source=lib/gh_project.sh
source "$DIR/lib/gh_project.sh"

require_cmd jq
require_cmd python3

CHECK_ONLY=0
case "${1:-}" in
  --check-only) CHECK_ONLY=1 ;;
  -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "[error] unknown argument: $1" >&2; exit 2 ;;
esac

load_config
require_gh_auth

RELEASE_ISSUE="${RELEASE_ISSUE_NUMBER:-}"
if [[ -z "$RELEASE_ISSUE" ]]; then
  echo "[ceremony-watch] RELEASE_ISSUE_NUMBER is not configured — nothing to watch." >&2
  jq -n -c '{fire: false, reason: "no release issue configured"}'
  exit 0
fi

TITLE="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${RELEASE_ISSUE}" --jq '.title' 2>/dev/null || echo "")"
STATUS="$(issue_status "$RELEASE_ISSUE" 2>/dev/null || echo "")"

# "Has a ceremony already started for this version?" — the marker is
# version-scoped, so a NEW line's release issue is never suppressed by the
# previous line's marker.
# `|| true`: sourcing gh_project.sh turns on `set -e`, and a title with no
# version makes grep exit 1 -- which must be a conservative "no ceremony"
# decision below, not an abort with no output.
VERSION_GUESS="$(grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' <<<"$TITLE" | head -1 | tr -d 'v' || true)"
# FORCE_CEREMONY=1 (the workflow's `force` input) ignores an existing
# marker so a ceremony that failed part-way can be re-run deliberately. It
# does NOT relax the other guardrails: the release issue must still be in
# For Release with a parseable version.
ALREADY=false
if [[ -n "$VERSION_GUESS" && "${FORCE_CEREMONY:-0}" != "1" ]]; then
  MARKER="$(python3 "${DIR}/lib/ceremony_trigger.py" marker "$VERSION_GUESS")"
  if gh api "repos/${REPO_OWNER}/${REPO_NAME}/issues/${RELEASE_ISSUE}/comments" --paginate 2>/dev/null \
    | jq -s --arg m "$MARKER" '[.[][] | select(.body | contains($m))] | length > 0' 2>/dev/null \
    | grep -q true; then
    ALREADY=true
  fi
fi

# Phases 0-3 already done? The tag is the proof (Phase 1 creates it). Then only
# Phase 4 is left; it is idempotent, so the version marker -- which exists to
# stop Phases 0-3 running twice -- must not suppress it.
PHASE4_ONLY=0
if [[ -n "$VERSION_GUESS" ]] \
   && [[ "$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/releases/tags/${VERSION_GUESS}" --jq .draft 2>/dev/null)" == "false" ]]; then
  PHASE4_ONLY=1
  ALREADY=false
  echo "[ceremony-watch] ${VERSION_GUESS} is already published -- only Phase 4 (next line + repoint) remains." >&2
fi

DECISION="$(jq -n -c \
  --argjson issue "$RELEASE_ISSUE" \
  --argjson release_issue "$RELEASE_ISSUE" \
  --arg status "$STATUS" \
  --arg for_release "${STATUS_FOR_RELEASE:-For Release}" \
  --arg title "$TITLE" \
  --argjson already "$ALREADY" \
  '{issue: $issue, release_issue: $release_issue, status: $status,
    for_release_status: $for_release, title: $title, already_fired: $already}' \
  | python3 "${DIR}/lib/ceremony_trigger.py" decide)"

echo "$DECISION"

FIRE="$(jq -r '.fire' <<<"$DECISION")"
REASON="$(jq -r '.reason' <<<"$DECISION")"
VERSION="$(jq -r '.version // empty' <<<"$DECISION")"

if [[ "$FIRE" != "true" ]]; then
  echo "[ceremony-watch] No ceremony: ${REASON}" >&2
  exit 0
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "[ceremony-watch] --check-only: would run the ceremony for ${VERSION} (${REASON})" >&2
  if [[ -z "${NYXGPT_CEREMONY_PAT:-}" ]]; then
    echo "[ceremony-watch] --check-only: NYXGPT_CEREMONY_PAT is not set — the ceremony would stop before starting." >&2
  fi
  exit 0
fi

# Fail fast on a missing ceremony token, BEFORE the marker is claimed: the
# scrummaster token cannot fast-forward master, so without this the
# ceremony would post its start comment, run the Phase 0 inventory (which
# since #4166 also PROVISIONS the milestone/sprint/draft it can) and
# only then die at the Phase 1 push — with the marker already stamped.
if [[ -z "${NYXGPT_CEREMONY_PAT:-}" ]]; then
  echo "[ceremony-watch] Ceremony token not configured (RELEASE_CEREMONY_TOKEN / NYXGPT_CEREMONY_PAT) — refusing to start." >&2
  issue_comment "$RELEASE_ISSUE" "🚨 **Release ceremony (${VERSION}) did not start**: the ceremony token is not configured (repository secret \`RELEASE_CEREMONY_TOKEN\`).

Nothing was changed — no tag, no master merge, no publish. Configure the secret (an owner-level token that may push to \`master\`) then re-run it: \`gh workflow run release_ceremony.yml\` (there is no schedule; nothing retries on its own)." \
    || _warn "ceremony-watch: could not post the missing-token report."
  notify_human_escalation "$RELEASE_ISSUE" "release-ceremony-no-token" \
    "Automated release ceremony for ${VERSION} could not start: RELEASE_CEREMONY_TOKEN is not configured" \
    "Add the RELEASE_CEREMONY_TOKEN repository secret (owner-level token with push access to master), then re-run: gh workflow run release_ceremony.yml" \
    "${RELEASE_ISSUE}:ceremony-token:${VERSION}" 1440 || true
  exit 1
fi

# Claim the ceremony BEFORE doing anything irreversible: the marker is what
# stops a repeat or concurrent dispatch from starting a second one.
MARKER="$(python3 "${DIR}/lib/ceremony_trigger.py" marker "$VERSION")"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-${REPO_OWNER}/${REPO_NAME}}/actions/runs/${GITHUB_RUN_ID:-0}"
issue_comment "$RELEASE_ISSUE" "🚀 **Release ceremony (automated, #3730)**: release issue moved to **${STATUS_FOR_RELEASE:-For Release}** — starting the ceremony for \`${VERSION}\` unattended.

Scope: Phase 0 prerequisite inventory (#4166 — every prerequisite for all five phases, with the provisionable ones created) → agents paused → master fast-forward → tag + GitHub Release → \`stable\` publish (#3727) → stable tap stamp → retirement of the \`${VERSION}rc*\` formulas → next line (branch, release issue, draft release) → repoint → agents restored.

Anything Phase 0 has to put in place (next-line milestone, sprint iteration, draft release, release-issue label/milestone) is listed in its own comment below. A prerequisite automation cannot create stops the run **before** Phase 1, with every gap listed at once.

[Ceremony run](${RUN_URL})

${MARKER}" || {
  # The marker IS the claim. Without it a later dispatch would start a second
  # ceremony, hit the Phase 0 tag gate and DM the owner a false alarm.
  # Nothing irreversible has happened yet, so stopping here is free and the
  # a re-dispatch retries the whole thing cleanly.
  _warn "ceremony-watch: could not post the ceremony marker — refusing to start the ceremony unclaimed. Re-run: gh workflow run release_ceremony.yml"
  notify_human_escalation "$RELEASE_ISSUE" "release-ceremony-unclaimed" \
    "Automated release ceremony for ${VERSION} could not claim the release issue (marker comment failed) — it did NOT start" \
    "Check GitHub API availability and the ceremony token, then re-run: gh workflow run release_ceremony.yml" \
    "${RELEASE_ISSUE}:ceremony-claim:${VERSION}" 60 || true
  exit 1
}

ceremony_failed() {
  local step="$1" detail="$2"
  issue_comment "$RELEASE_ISSUE" "🚨 **Release ceremony FAILED (${VERSION})** at: ${step}

${detail}

The ceremony stopped here — nothing further ran. Re-run it after fixing the cause: dispatch **Release Ceremony (Automated)** with \`force=true\`, or run \`scripts/release_ceremony.sh ${VERSION}\` locally.

[Ceremony run](${RUN_URL})" \
    || _warn "ceremony-watch: could not post the failure report."
  notify_human_escalation "$RELEASE_ISSUE" "release-ceremony-failed" \
    "Automated release ceremony for ${VERSION} failed at: ${step}" \
    "Inspect ${RUN_URL}, fix the cause, then re-dispatch the ceremony (force=true)" \
    "${RELEASE_ISSUE}:ceremony:${VERSION}" 60
  exit 1
}

CEREMONY_ARGS=(--unattended)
[[ "$PHASE4_ONLY" == "1" ]] && CEREMONY_ARGS+=(--phase4-only)
CEREMONY_LOG="$(mktemp)"
echo "[ceremony-watch] Running the ceremony for ${VERSION} (${CEREMONY_ARGS[*]}, all phases)." >&2
if ! NYXGPT_CEREMONY_PAT="${NYXGPT_CEREMONY_PAT}" \
  "$ROOT/scripts/release_ceremony.sh" "$VERSION" "${CEREMONY_ARGS[@]}" 2>&1 | tee "$CEREMONY_LOG"; then
  ceremony_failed "the ceremony itself" \
    "See the run log for which phase stopped it. **The agent flags (AGENTS_ENABLED, SPRINT_AUTOPILOT, CLAUDE_REVIEW_ENABLED) stay paused** until a run completes -- that is deliberate, so nothing merges into a half-released line. Re-dispatching resumes: Phases 0-3 are skipped once the release tag exists, and the saved flags are restored at the end of Phase 4."
fi
# `|| true` on BOTH greps below, for the same reason as line 102: sourcing
# gh_project.sh turns on `set -e`, and `grep` exits 1 when the pattern is
# absent -- which `pipefail` propagates out of the command substitution. These
# two lines read OPTIONAL detail out of the ceremony log, so a miss has to mean
# "nothing to report", not an abort. Without it the watcher dies here after a
# SUCCESSFUL ceremony: the rc formulas are never retired, the completion
# comment is never posted, and nothing says so (no failure comment, no DM,
# since `ceremony_failed` is never reached either).
NEXT_LINE="$(grep -oE 'NEXT_LINE v[0-9.]+ #[0-9]+' "$CEREMONY_LOG" | tail -1 | cut -d' ' -f2- || true)"
# What Phase 0 had to put in place (#4166). The ceremony posts its own note on
# the release issue at the time; this carries it into the completion summary so
# the owner sees "a placeholder milestone is waiting to be renamed" without
# scrolling back. Provisioning nothing is the normal case once the owner has
# prepared the line themselves, so this grep misses more often than it hits.
PROVISIONED="$(grep -oE '^\[ceremony\] PROVISIONED .*' "$CEREMONY_LOG" | tail -1 | sed 's/^\[ceremony\] PROVISIONED //' || true)"
# Built outside the comment body on purpose: inside a double-quoted string,
# `${VAR:+...}` re-parses quotes in its word, so an apostrophe in the prose
# would open a quote and leave the script unparseable.
PROVISIONED_NOTE=""
if [[ -n "$PROVISIONED" ]]; then
  PROVISIONED_NOTE="
Phase 0 provisioned: ${PROVISIONED}. A placeholder milestone is the ceremony's stand-in — rename and re-scope it for the real next line.
"
fi

if [[ "$PHASE4_ONLY" != "1" ]]; then
  echo "[ceremony-watch] Retiring the ${VERSION}rc* formulas from the tap." >&2
  if ! "$ROOT/scripts/retire_rc_formulas.sh" "$VERSION"; then
    ceremony_failed "rc formula retirement" \
      "The release is published and the repo is repointed; only the tap cleanup failed. Re-run \`scripts/retire_rc_formulas.sh ${VERSION}\` once the tap is reachable."
  fi
fi

issue_comment "$RELEASE_ISSUE" "✅ **Release ceremony complete (${VERSION})** — master fast-forwarded, tag and GitHub Release published, \`stable\` published to PyPI, tap stamped, \`${VERSION}rc*\` formulas retired, next line ready and repointed${NEXT_LINE:+ (**${NEXT_LINE}**)}, agent flags restored.
${PROVISIONED_NOTE}
Nothing is left for the owner. The session that dispatched this reconciles the local ~/.nyxGPT/config.ini mirror (\`[github] RELEASE_BRANCH\` / \`RELEASE_ISSUE_NUMBER\`).

[Ceremony run](${RUN_URL})" \
  || _warn "ceremony-watch: could not post the completion note."

echo "[ceremony-watch] Ceremony complete for ${VERSION}." >&2
