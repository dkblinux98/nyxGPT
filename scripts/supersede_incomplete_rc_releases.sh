#!/usr/bin/env bash
# scripts/supersede_incomplete_rc_releases.sh
#
# Clear (or, failing that, label) release-candidate prereleases whose assets
# are incomplete, so the Homebrew candidate formulas can only ever point at a
# release that actually carries both service tarballs.
#
# Why this exists (#3747): GitHub releases in this repository are IMMUTABLE.
# Once a release is published it can never gain or change an asset -- the rc
# tap job used to create the prerelease first and upload the tarballs
# afterwards, and run 31663088319 died on
# `HTTP 422: Cannot upload assets to an immutable release`, leaving a
# published `3.0.0rc3` prerelease with no tarballs on it. The tap job now
# attaches its assets AT CREATION, and runs this sweep first so a leftover
# half-release is retired rather than left looking installable.
#
# For every `<line>rcN` release that is missing a tarball it:
#   1. deletes it (with its tag) -- the clean outcome; the candidate number is
#      burned either way, since PyPI never re-serves a version; or
#   2. if the platform refuses the delete, rewrites its notes with a
#      "superseded" banner naming what is missing, so nobody installs from it.
#
# It is deliberately best-effort: a leftover that can be neither deleted nor
# annotated is reported as a warning, not a failure, because it must not block
# the next candidate from publishing. The tap job separately refuses to point
# a formula at an incomplete release, which is the check that actually gates.
#
# Usage:
#   scripts/supersede_incomplete_rc_releases.sh <release-line> [options]
#
#   <release-line>      X.Y.Z -- the line whose candidates to sweep, e.g. 3.0.0
#   --repo owner/name   Repository (default: $GITHUB_REPOSITORY)
#   --limit N           Releases to consider (default: 100)
#   --dry-run           Report what would happen; mutate nothing
#
# Exits non-zero only on a usage error or when the release list cannot be
# read -- never because a leftover could not be retired.

set -uo pipefail

RELEASE_LINE=""
REPO="${GITHUB_REPOSITORY:-}"
LIMIT=100
DRY=0

usage() {
  sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --limit) LIMIT="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *)
      [[ -z "$RELEASE_LINE" ]] || { echo "unexpected argument: $1" >&2; usage; }
      RELEASE_LINE="$1"; shift ;;
  esac
done

[[ -n "$RELEASE_LINE" ]] || { echo "a release line (X.Y.Z) is required" >&2; usage; }
[[ "$RELEASE_LINE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "'$RELEASE_LINE' is not a release line -- expected X.Y.Z, e.g. 3.0.0" >&2
  exit 2
}
[[ -n "$REPO" ]] || { echo "--repo (or GITHUB_REPOSITORY) is required" >&2; exit 2; }

# The two assets every candidate release must carry. Same names
# scripts/build_homebrew_artifacts.py writes, and the same names the stamped
# formulas' `url` points at -- if either is absent the formula cannot install.
SERVICES=(nyxgpt-api nyxgpt-web)

# `3.0.0rc4` and nothing else: the dots are escaped so the line cannot match
# another version by accident.
LINE_RE="^${RELEASE_LINE//./\\.}rc[0-9]+$"

log() { echo "supersede-rc: $*"; }

TAGS="$(gh release list -R "$REPO" --limit "$LIMIT" \
  --json tagName,isPrerelease --jq '.[] | select(.isPrerelease) | .tagName' 2>/dev/null)"
# shellcheck disable=SC2181
if [[ $? -ne 0 ]]; then
  echo "supersede-rc: could not list releases for $REPO" >&2
  exit 1
fi

SWEPT=0
for TAG in $TAGS; do
  # Only this line's candidates. `3.0.0` (the release itself) and another
  # line's `3.1.0rc1` are somebody else's business and are never touched.
  [[ "$TAG" =~ $LINE_RE ]] || continue

  PRESENT="$(gh release view "$TAG" -R "$REPO" --json assets \
    --jq '[.assets[].name] | join(" ")' 2>/dev/null)"
  MISSING=()
  for service in "${SERVICES[@]}"; do
    NAME="${service}-${TAG}.tar.gz"
    case " $PRESENT " in
      *" $NAME "*) ;;
      *) MISSING+=("$NAME") ;;
    esac
  done
  if [[ ${#MISSING[@]} -eq 0 ]]; then
    log "$TAG carries both tarballs -- left alone"
    continue
  fi

  SWEPT=$((SWEPT + 1))
  MISSING_LIST="${MISSING[*]}"
  if [[ $DRY -eq 1 ]]; then
    log "DRY-RUN: $TAG is missing ${MISSING_LIST} -- would delete it, or mark it superseded"
    continue
  fi

  log "$TAG is missing ${MISSING_LIST} -- retiring it"
  if gh release delete "$TAG" -R "$REPO" --yes --cleanup-tag >/dev/null 2>&1; then
    log "$TAG deleted (tag removed)"
    continue
  fi

  # Immutable releases cannot be deleted either. Say so in the release notes
  # so the leftover cannot be mistaken for an installable candidate.
  BODY="$(gh release view "$TAG" -R "$REPO" --json body --jq .body 2>/dev/null)"
  NOTES_FILE="$(mktemp)"
  {
    echo "> [!WARNING]"
    echo "> **Superseded -- do not install.** This candidate's release assets are"
    echo "> incomplete (missing: ${MISSING_LIST// /, }). Releases in this repository"
    echo "> are immutable, so it can never gain them (#3747). Use the next candidate."
    echo ""
    echo "$BODY"
  } >"$NOTES_FILE"
  if gh release edit "$TAG" -R "$REPO" --notes-file "$NOTES_FILE" >/dev/null 2>&1; then
    log "$TAG marked superseded in its notes"
  else
    echo "::warning::${TAG} is incomplete and could be neither deleted nor annotated" >&2
    echo "::warning::-- it stays as-is; no formula points at it." >&2
  fi
  rm -f "$NOTES_FILE"
done

if [[ $SWEPT -eq 0 ]]; then
  log "no incomplete ${RELEASE_LINE} candidate releases found"
fi
exit 0
