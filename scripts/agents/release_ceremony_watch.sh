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
#     comment makes every later poll a no-op;
#   * only with a parseable vX.Y.Z version in the issue title.
#
# Phase scope: Phases 0-3 (entry gate, master+tag+release, stable publish,
# project close-out). Phase 4 (next-line preparation and the repoint) stays
# with the owner-run script: it needs the owner's local config.ini mirror
# and next-line decisions, and is not part of the automated scope.
#
# Any failure alerts the owner on the existing Slack DM channel (#3695) in
# addition to a loud comment on the release issue.
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
# ceremony would post its start comment, run the read-only entry gate and
# only then die at the Phase 1 push — with the marker already stamped.
if [[ -z "${NYXGPT_CEREMONY_PAT:-}" ]]; then
  echo "[ceremony-watch] Ceremony token not configured (RELEASE_CEREMONY_TOKEN / NYXGPT_CEREMONY_PAT) — refusing to start." >&2
  issue_comment "$RELEASE_ISSUE" "🚨 **Release ceremony (${VERSION}) did not start**: the ceremony token is not configured (repository secret \`RELEASE_CEREMONY_TOKEN\`).

Nothing was changed — no tag, no master merge, no publish. Configure the secret (an owner-level token that may push to \`master\`) and the next poll starts the ceremony automatically." \
    || _warn "ceremony-watch: could not post the missing-token report."
  notify_human_escalation "$RELEASE_ISSUE" "release-ceremony-no-token" \
    "Automated release ceremony for ${VERSION} could not start: RELEASE_CEREMONY_TOKEN is not configured" \
    "Add the RELEASE_CEREMONY_TOKEN repository secret (owner-level token with push access to master); the next poll retries" \
    "${RELEASE_ISSUE}:ceremony-token:${VERSION}" 1440 || true
  exit 1
fi

# Claim the ceremony BEFORE doing anything irreversible: the marker is what
# stops the next poll (and a concurrent run) from starting a second one.
MARKER="$(python3 "${DIR}/lib/ceremony_trigger.py" marker "$VERSION")"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-${REPO_OWNER}/${REPO_NAME}}/actions/runs/${GITHUB_RUN_ID:-0}"
issue_comment "$RELEASE_ISSUE" "🚀 **Release ceremony (automated, #3730)**: release issue moved to **${STATUS_FOR_RELEASE:-For Release}** — starting the ceremony for \`${VERSION}\` unattended.

Scope: master fast-forward → tag + GitHub Release → \`stable\` publish (#3727) → stable tap stamp → retirement of the \`${VERSION}rc*\` formulas. Phase 4 (next-line preparation and the repoint) remains owner-run.

[Ceremony run](${RUN_URL})

${MARKER}" || {
  # The marker IS the claim. Without it a later poll would start a second
  # ceremony, hit the Phase 0 tag gate and DM the owner a false alarm.
  # Nothing irreversible has happened yet, so stopping here is free and the
  # next poll retries the whole thing cleanly.
  _warn "ceremony-watch: could not post the ceremony marker — refusing to start the ceremony unclaimed. The next poll will retry."
  notify_human_escalation "$RELEASE_ISSUE" "release-ceremony-unclaimed" \
    "Automated release ceremony for ${VERSION} could not claim the release issue (marker comment failed) — it did NOT start" \
    "Check GitHub API availability and the ceremony token; the next poll retries automatically" \
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

echo "[ceremony-watch] Running the ceremony for ${VERSION} (Phases 0-3, unattended)." >&2
if ! NYXGPT_CEREMONY_PAT="${NYXGPT_CEREMONY_PAT}" \
  "$ROOT/scripts/release_ceremony.sh" "$VERSION" --unattended --stop-after-phase 3; then
  ceremony_failed "the ceremony itself (Phases 0-3: entry gate, master/tag/release, stable publish, close-out)" \
    "See the run log for which phase stopped it. The entry gate is read-only, so a gate failure changed nothing."
fi

echo "[ceremony-watch] Retiring the ${VERSION}rc* formulas from the tap." >&2
if ! "$ROOT/scripts/retire_rc_formulas.sh" "$VERSION"; then
  ceremony_failed "rc formula retirement" \
    "The release itself is published; only the tap cleanup failed. Re-run \`scripts/retire_rc_formulas.sh ${VERSION}\` once the tap is reachable."
fi

issue_comment "$RELEASE_ISSUE" "✅ **Release ceremony complete (${VERSION})** — master fast-forwarded, tag and GitHub Release published, \`stable\` published to PyPI, tap stamped and the \`${VERSION}rc*\` formulas retired.

Remaining owner step: Phase 4 (next-line preparation and the repoint) — \`scripts/release_ceremony.sh ${VERSION} --phase4-only --next-branch <next>\`.

[Ceremony run](${RUN_URL})" \
  || _warn "ceremony-watch: could not post the completion note."

echo "[ceremony-watch] Ceremony complete for ${VERSION}." >&2
