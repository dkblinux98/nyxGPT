#!/usr/bin/env bash
set -euo pipefail

# scripts/agents/snapshot_safe_add.sh
#
# Stages uncommitted work for the developer workflow's "Snapshot uncommitted
# implementation work" step without a blanket `git add -A`. A blanket add
# sweeps in any untracked artifact .gitignore doesn't yet cover -- see #3370,
# where an uncovered .venv-agent/ produced a 7,032-file / ~1.8M-line commit
# on a feature branch that then made verification crawl for 30+ minutes
# against a ~75s baseline.
#
# Strategy:
#   - `git add -u` stages modifications/deletions of already-tracked files.
#   - New (untracked) files/dirs are staged ONLY if their path starts under
#     an explicit allowlist of source/test/doc directories, AND don't match
#     a junk pattern (venv, node_modules, caches, build output, ...) that
#     .gitignore might not cover -- belt-and-suspenders, since .gitignore
#     alone already failed to catch this once.
#   - Untracked paths outside the allowlist are left untouched and reported
#     via ::notice:: so they're visible instead of silently dropped.
#   - A size guard aborts (unstaging everything) if the result is
#     implausibly large for an agent-authored diff, instead of letting a
#     pathological tree reach `git commit` and hang verification.
#
# Usage: scripts/agents/snapshot_safe_add.sh   (run from repo root)
# Exit codes: 0 = staged (possibly nothing), 1 = size guard tripped

MAX_FILES="${SNAPSHOT_MAX_FILES:-200}"
MAX_LINES="${SNAPSHOT_MAX_LINES:-50000}"

ALLOWED_PREFIXES=(
  src/
  tests/
  docs/
  web/
  scripts/
  agents/
  product_management/
  k8s/
  terraform/
  .github/ISSUE_TEMPLATE/
  .github/actions/
)

ROOT_ALLOWED_FILES=(
  README.md
  AGENTS.md
  CLAUDE.md
  CONTRIBUTING.md
  pyproject.toml
  example.config.ini
  .gitignore
)

# Junk patterns excluded even under an allowed prefix or the repo root.
JUNK_PATTERN='(^|/)(\.?venv[^/]*|node_modules|\.next|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|htmlcov|\.coverage[^/]*|coverage|dist|build|[^/]*\.egg-info)(/|$)'

git add -u

mapfile -t UNTRACKED < <(git status --porcelain | awk '$1 == "??" {print substr($0, 4)}')

SKIPPED=()
for path in "${UNTRACKED[@]}"; do
  [[ -z "$path" ]] && continue

  if [[ "$path" =~ $JUNK_PATTERN ]]; then
    SKIPPED+=("$path")
    continue
  fi

  matched=0
  for prefix in "${ALLOWED_PREFIXES[@]}"; do
    if [[ "$path" == "$prefix"* ]]; then
      matched=1
      break
    fi
  done
  if [[ "$matched" == "0" ]]; then
    for f in "${ROOT_ALLOWED_FILES[@]}"; do
      if [[ "$path" == "$f" ]]; then
        matched=1
        break
      fi
    done
  fi

  if [[ "$matched" == "1" ]]; then
    git add -- "$path"
  else
    SKIPPED+=("$path")
  fi
done

if [[ "${#SKIPPED[@]}" -gt 0 ]]; then
  echo "::notice::Snapshot skipped ${#SKIPPED[@]} untracked path(s) outside the source/test/doc allowlist (not staged):"
  printf '  %s\n' "${SKIPPED[@]}"
fi

STAGED_FILES="$(git diff --cached --numstat | wc -l | tr -d ' ')"
STAGED_LINES="$(git diff --cached --numstat | awk '{a=$1; d=$2; if (a=="-") a=0; if (d=="-") d=0; sum+=a+d} END {print sum+0}')"

if [[ "$STAGED_FILES" -gt "$MAX_FILES" || "$STAGED_LINES" -gt "$MAX_LINES" ]]; then
  echo "::error::Snapshot guard tripped: staged $STAGED_FILES file(s) / $STAGED_LINES line(s) exceeds the limit ($MAX_FILES files / $MAX_LINES lines)."
  echo "::error::Offending paths (largest first):"
  git diff --cached --numstat | sort -t "$(printf '\t')" -k1,1nr | head -20 >&2
  git reset >/dev/null
  exit 1
fi

echo "Staged $STAGED_FILES file(s) / $STAGED_LINES line(s) for snapshot."
