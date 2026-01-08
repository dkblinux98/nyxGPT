#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
require_gh_auth
require_cmd git

ISSUE="${1:?issue number required}"
KIND="${2:-feat}"
SLUG="${3:-issue-${ISSUE}}"

base_branch="$(get_release_branch)"
branch="${KIND}/${ISSUE}-${SLUG}"
branch="$(echo "$branch" | tr ' ' '-' | tr -cd '[:alnum:]/._-')"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || _die "Run inside a git repo"
git fetch origin "$base_branch" >/dev/null
git checkout -b "$branch" "origin/${base_branch}"
echo "$branch"
