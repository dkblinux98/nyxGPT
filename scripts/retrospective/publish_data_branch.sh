#!/usr/bin/env bash
set -euo pipefail

# scripts/retrospective/publish_data_branch.sh
#
# Publishes a retrospective dump's JSON to the retro data branch
# (claude/retro-data) instead of pushing it straight at the branch the dump
# was dispatched on (#3815).
#
# Why: a repository ruleset requires changes to the release/default branch to
# arrive through a pull request, so the dump workflows' old
# `git push origin HEAD:<dispatching branch>` was rejected server-side:
#
#     remote: error: GH013: Repository rule violations found for refs/heads/v3.0.0.
#     remote: - Changes must be made through a pull request.
#
# The dump itself succeeded every time; only the push failed, so every refresh
# was silently discarded. The data branch is not ruleset-protected, and
# scripts/retrospective/merge_data_branch.sh lands it on the default branch
# through a pull request — the mechanism the rule asks for.
#
# Usage:
#   publish_data_branch.sh <commit-message> <path>...
#
# Env:
#   DATA_BRANCH  branch to publish to             (default: claude/retro-data)
#   BASE_REF     branch the data is destined for  (default: remote HEAD)
#   REMOTE       git remote                       (default: origin)
#
# Exits 0 printing "no changes" when the dump produced nothing new (no commit,
# no push). Exits non-zero if asked to publish anything outside
# scripts/retrospective/ — the merge guard refuses such a branch, so this
# fails at dump time rather than leaving an unmergeable branch behind.

_die() { echo "[publish-retro-data] ERROR: $*" >&2; exit 1; }
_log() { echo "[publish-retro-data] $*" >&2; }

usage() {
  # Print the whole leading comment block, however long it grows — a fixed
  # line range silently truncates the env list when the header changes.
  awk 'NR <= 2 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
    "${BASH_SOURCE[0]}"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

MSG="${1:-}"
shift || true
FILES=("$@")

[[ -n "$MSG" ]] || { usage >&2; exit 2; }
[[ ${#FILES[@]} -gt 0 ]] || { usage >&2; exit 2; }

REMOTE="${REMOTE:-origin}"
DATA_BRANCH="${DATA_BRANCH:-claude/retro-data}"

if [[ -z "${BASE_REF:-}" ]]; then
  BASE_REF="$(git ls-remote --symref "$REMOTE" HEAD 2>/dev/null \
    | awk '$1 == "ref:" { sub("refs/heads/", "", $2); print $2; exit }')"
fi
[[ -n "$BASE_REF" ]] || _die "cannot resolve the base branch (set BASE_REF)"

# Guard, mirroring merge_data_branch.sh: this branch may only ever carry
# retrospective data, because merging it skips code review.
for f in "${FILES[@]}"; do
  case "$f" in
    scripts/retrospective/*) ;;
    *) _die "refusing to publish '$f': only files under scripts/retrospective/ may reach $DATA_BRANCH" ;;
  esac
  [[ -f "$f" ]] || _die "no such file: $f (did the dump step run?)"
done

git config user.name  >/dev/null 2>&1 || git config user.name "github-actions[bot]"
git config user.email >/dev/null 2>&1 || \
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Stash the generated files outside the work tree: switching branches below
# overwrites them.
TMP="$(mktemp -d)"
for f in "${FILES[@]}"; do
  mkdir -p "$TMP/$(dirname "$f")"
  cp "$f" "$TMP/$f"
done

# Publishing switches the work tree to the data branch, which is built on the
# DEFAULT branch — so every later step in the job would run the default
# branch's code, not the code of the ref the workflow was dispatched on. That
# is how a dump dispatched on a feature branch failed with "No such file or
# directory" on a script that plainly exists there. Put the checkout back
# before returning, whatever the outcome.
ORIG_REF="$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)"
_restore_checkout() {
  git checkout -f -q "$ORIG_REF" 2>/dev/null || \
    _log "WARNING: could not return the work tree to $ORIG_REF"
  # checkout -f drops files tracked on the data branch but not here; the
  # dump's own output is expected to still be sitting in the tree.
  for f in "${FILES[@]}"; do
    if [[ -f "$TMP/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$TMP/$f" "$f"
    fi
  done
}
trap '_restore_checkout; rm -rf "$TMP"' EXIT

git fetch --quiet "$REMOTE" "$BASE_REF"
base="$(git rev-parse FETCH_HEAD)"

# Normally the data branch is reset to the base tip, so the pull request it
# produces contains exactly one refresh. If a previous refresh is still
# waiting to merge, build on top of it instead — force-pushing over it would
# throw away a dump that has not landed yet.
data_head=""
if git ls-remote --exit-code --heads "$REMOTE" "$DATA_BRANCH" >/dev/null 2>&1; then
  git fetch --quiet "$REMOTE" "$DATA_BRANCH"
  data_head="$(git rev-parse FETCH_HEAD)"
  if ! git merge-base --is-ancestor "$data_head" "$base"; then
    _log "$DATA_BRANCH has an unmerged refresh ($(git rev-parse --short "$data_head")); building on it"
    base="$data_head"
  fi
fi

git checkout -f -q -B "$DATA_BRANCH" "$base"

for f in "${FILES[@]}"; do
  mkdir -p "$(dirname "$f")"
  cp "$TMP/$f" "$f"
done

git add -- "${FILES[@]}"
if git diff --cached --quiet; then
  _log "no changes — $DATA_BRANCH already carries this data"
  echo "no changes"
  exit 0
fi

git commit -q -m "$MSG"

# A blind --force would drop a refresh another dump published between the
# fetch above and this push. The lease pins the tip we actually read, so a
# concurrent publish makes this fail loudly with the refresh still in hand
# rather than disappearing someone else's. (The dump workflows also share one
# `concurrency:` group, so this is the backstop, not the first line.)
if [[ -n "$data_head" ]]; then
  push_args=(--force-with-lease="refs/heads/$DATA_BRANCH:$data_head")
else
  # The branch does not exist: a plain push creates it and is rejected if
  # another dump created it first.
  push_args=()
fi

if ! git push "${push_args[@]}" "$REMOTE" "HEAD:refs/heads/$DATA_BRANCH"; then
  _die "publishing to $DATA_BRANCH lost a race with another dump (its tip moved${data_head:+ since ${data_head:0:7}}); re-run this dump"
fi

_log "published $(git rev-parse --short HEAD) to $DATA_BRANCH (base: $BASE_REF)"
echo "published"
