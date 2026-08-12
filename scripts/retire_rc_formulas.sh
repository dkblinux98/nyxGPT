#!/usr/bin/env bash
set -euo pipefail

# retire_rc_formulas.sh — retire a released line's `-rc` formulas from the
# remote Homebrew tap (#3730, part of the automated release ceremony).
#
# The rc channel (#3727) stamps `nyxgpt-api-rc.rb` / `nyxgpt-web-rc.rb` into
# the tap so an acceptance candidate is installable the same repo-less way a
# release is. Once the line ships, those candidates are dead: `brew install
# nyxgpt-api-rc` would keep installing a pre-release of a version that is now
# available as the stable formula. This removes them.
#
# It is deliberately version-scoped, not a blanket delete. Only formulas
# whose stamped version belongs to the RELEASED line (`X.Y.Zrc<N>`) are
# retired; if the tap already carries candidates for a LATER line (the next
# RC series can start before the previous line's ceremony completes), they
# are left exactly as they are.
#
# The stable formulas (`nyxgpt-api.rb` / `nyxgpt-web.rb`) are never touched —
# verified before pushing, the same structural guard the rc publish uses.
#
# Usage:
#   scripts/retire_rc_formulas.sh VERSION
#     VERSION   the released version, e.g. 3.0.0 (retires 3.0.0rc* formulas)
#
# Environment:
#   TAP_REPO       owner/repo of the remote tap (e.g. dkblinux98/homebrew-nyxgpt)
#   TAP_TOKEN      push token for that repo
#   TAP_CLONE_URL  full clone URL, overriding TAP_REPO/TAP_TOKEN (tests)
#   DRY_RUN=1      report what would be retired; clone but never push
#
# An unconfigured tap is a warning, not a failure: the tap is optional
# infrastructure (see docs/homebrew.md#remote-tap) and the release itself
# has already happened by the time this runs.

VERSION="${1:-}"
[[ -n "$VERSION" ]] || { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "[retire-rc] FATAL: VERSION must be x.y.z, got: $VERSION" >&2; exit 2; }

log() { echo "[retire-rc] $*"; }

RC_FORMULAS=(Formula/nyxgpt-api-rc.rb Formula/nyxgpt-web-rc.rb)
STABLE_FORMULAS=(Formula/nyxgpt-api.rb Formula/nyxgpt-web.rb)

CLONE_URL="${TAP_CLONE_URL:-}"
if [[ -z "$CLONE_URL" ]]; then
  if [[ -z "${TAP_REPO:-}" ]]; then
    log "TAP_REPO is not set — no remote tap to retire rc formulas from (skipping)."
    exit 0
  fi
  if [[ -z "${TAP_TOKEN:-}" ]]; then
    log "WARNING: TAP_REPO is set but TAP_TOKEN is missing — cannot retire the rc formulas."
    exit 0
  fi
  CLONE_URL="https://x-access-token:${TAP_TOKEN}@github.com/${TAP_REPO}.git"
fi

TAP_DIR="$(mktemp -d)"
trap 'rm -rf "$TAP_DIR"' EXIT
git clone --depth 1 "$CLONE_URL" "$TAP_DIR" --quiet
cd "$TAP_DIR"

# `version "3.0.0rc4"` in the formula is what ties it to a line. Anything
# else (no version line, a version from another line) is left alone.
formula_version() {
  awk -F'"' '/^[[:space:]]*version[[:space:]]*"/{print $2; exit}' "$1"
}

RETIRED=()
for formula in "${RC_FORMULAS[@]}"; do
  if [[ ! -e "$formula" ]]; then
    log "  $formula: not present (nothing to retire)"
    continue
  fi
  fver="$(formula_version "$formula")"
  if [[ "$fver" == "${VERSION}rc"* ]]; then
    log "  $formula: version '${fver}' belongs to the released ${VERSION} line — retiring"
    git rm --quiet "$formula"
    RETIRED+=("$formula")
  else
    log "  $formula: version '${fver:-<none>}' is not a ${VERSION} candidate — leaving in place"
  fi
done

if [[ ${#RETIRED[@]} -eq 0 ]]; then
  log "No ${VERSION} release candidates in the tap — nothing to retire."
  exit 0
fi

# Never let a retirement touch the stable formulas — `brew install
# nyxgpt-api` must keep resolving to the release that just shipped.
for stable in "${STABLE_FORMULAS[@]}"; do
  if git status --porcelain -- "$stable" | grep -q .; then
    echo "[retire-rc] FATAL: this retirement modified ${stable} — refusing to push" >&2
    exit 1
  fi
done
log "stable formulas untouched (verified)"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log "DRY_RUN: would retire ${RETIRED[*]} and push to the tap"
  exit 0
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git config user.name "nyxgpt-release-bot"
git config user.email "actions@github.com"
git commit --quiet -m "retire nyxgpt ${VERSION} release candidates (stable ${VERSION} shipped)"
git push --quiet origin "HEAD:${BRANCH}"

# Verify the mutation by re-reading the remote rather than trusting the
# push's exit code (house rule). The shallow clone's own refs are stale
# after the push, so fetch the branch again first.
git fetch --quiet --depth 1 origin "$BRANCH"
for formula in "${RETIRED[@]}"; do
  if git ls-tree -r --name-only FETCH_HEAD 2>/dev/null | grep -qx "$formula"; then
    echo "[retire-rc] FATAL: verify failed — ${formula} is still in the tap" >&2
    exit 1
  fi
done
log "verified: retired ${RETIRED[*]} from the tap"
