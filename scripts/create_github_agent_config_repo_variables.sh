#!/usr/bin/env bash
set -euo pipefail

# bootstrap_actions_vars.sh (Bash 3.2 compatible)
# Create/update GitHub Actions repository variables from ~/.myGPT/config.ini
# Safe to commit (does not embed secrets). Requires: gh authenticated.

CONFIG_FILE="${MYGPT_CONFIG_FILE:-$HOME/.myGPT/config.ini}"
API_VER="2022-11-28"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [--config <path>] [--repo owner/repo]

Reads key=value pairs from config.ini and pushes them as GitHub Actions repository variables.

Notes:
- Supports KEY=VALUE lines (ignores blank lines, comments, and [section] headers).
- Bash 3.2 compatible (macOS default).
EOF
}

REPO_OVERRIDE=""
CONFIG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_OVERRIDE="$2"; shift 2;;
    --repo) REPO_OVERRIDE="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "[error] unknown arg: $1" >&2; usage; exit 2;;
  esac
done

[[ -n "$CONFIG_OVERRIDE" ]] && CONFIG_FILE="$CONFIG_OVERRIDE"
[[ -f "$CONFIG_FILE" ]] || { echo "[error] config not found: $CONFIG_FILE" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || { echo "[error] Missing gh CLI" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "[error] gh not authenticated. Run: gh auth login" >&2; exit 1; }

# Normalize config -> temp KV file: KEY<TAB>VALUE
KV_FILE="$(mktemp)"
trap 'rm -f "$KV_FILE"' EXIT

awk '
  BEGIN{ FS="=" }
  {
    line=$0
    sub(/#.*/, "", line)
    sub(/;.*/, "", line)
    gsub(/^[ \t]+|[ \t]+$/, "", line)
    if (line=="" ) next
    if (line ~ /^\[.*\]$/) next
    # split on first "="
    pos = index(line, "=")
    if (pos==0) next
    key = substr(line, 1, pos-1)
    val = substr(line, pos+1)
    gsub(/^[ \t]+|[ \t]+$/, "", key)
    gsub(/^[ \t]+|[ \t]+$/, "", val)
    # strip surrounding quotes
    if (val ~ /^".*"$/) { sub(/^"/,"",val); sub(/"$/,"",val) }
    if (key ~ /^[A-Za-z0-9_]+$/) print key "\t" val
  }
' "$CONFIG_FILE" > "$KV_FILE"

get_kv() {
  local key="$1"
  awk -F'\t' -v k="$key" '$1==k {print $2; exit}' "$KV_FILE"
}

# Determine repo owner/repo from --repo or REPO_OWNER/REPO_NAME
if [[ -n "$REPO_OVERRIDE" ]]; then
  OWNER="${REPO_OVERRIDE%%/*}"
  REPO="${REPO_OVERRIDE#*/}"
else
  OWNER="$(get_kv REPO_OWNER)"
  REPO="$(get_kv REPO_NAME)"
fi

[[ -n "${OWNER:-}" && -n "${REPO:-}" && "$OWNER" != "$REPO" ]] || {
  echo "[error] Need repo. Provide --repo owner/repo or set REPO_OWNER and REPO_NAME in config." >&2
  exit 1
}

set_var() {
  local name="$1"
  local value="$2"

  # Create
  if gh api -X POST \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: $API_VER" \
      "repos/$OWNER/$REPO/actions/variables" \
      -f name="$name" \
      -f value="$value" >/dev/null 2>&1; then
    echo "created $name"
    return 0
  fi

  # Update (covers 409 exists)
  gh api -X PATCH \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: $API_VER" \
    "repos/$OWNER/$REPO/actions/variables/$name" \
    -f value="$value" >/dev/null

  echo "updated $name"
}

# Only push keys we expect (prevents accidentally uploading secrets)
VARS=(
  AGENTS_ENABLED
  REPO_OWNER
  REPO_NAME
  PROJECT_OWNER
  PROJECT_NUMBER
  DEV_AGENT
  REVIEW_AGENT
  SCRUM_AGENT
  HUMAN_OWNER
  STATUS_FIELD
  STATUS_BACKLOG
  STATUS_IN_PROGRESS
  STATUS_IN_REVIEW
  STATUS_FOR_RELEASE
  RELEASE_BRANCH
  RELEASE_ISSUE_NUMBER
)

echo "[info] repo=$OWNER/$REPO config=$CONFIG_FILE"

missing=0
for k in "${VARS[@]}"; do
  v="$(get_kv "$k")"
  if [[ -z "$v" ]]; then
    echo "[warn] missing in config: $k (skipping)"
    missing=$((missing+1))
    continue
  fi
  set_var "$k" "$v"
done

echo "✅ done (missing/skipped: $missing)"