#!/usr/bin/env bash
set -euo pipefail

# myGPT web runner
# Starts the Next.js dev server using ~/.myGPT/config.ini

CONFIG="$HOME/.myGPT/config.ini"
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG" >&2
  exit 1
fi

# Minimal INI reader: get_ini section key
get_ini() {
  local section="$1"
  local key="$2"
  awk -F '=' -v s="[$section]" -v k="$key" '
    $0==s { found=1; next }
    /^\[/ { found=0 }
    found && $1 ~ k {
      gsub(/^[ \t]+|[ \t]+$/, "", $2)
      print $2
      exit
    }
  ' "$CONFIG"
}

WEB_HOST="$(get_ini web host)"
WEB_PORT="$(get_ini web port)"
API_OVERRIDE="$(get_ini web api_base_url)"
NODE_BIN="$(get_ini paths node_bin)"
NPM_BIN="$(get_ini paths npm_bin)"
REPO_DIR="$(get_ini paths repo_dir)"


if [[ -z "$NODE_BIN" || -z "$NPM_BIN" || -z "$REPO_DIR" ]]; then
  echo "Missing required [paths] configuration (node_bin, npm_bin, repo_dir)" >&2
  exit 1
fi

# Ensure node is discoverable for tools that use `#!/usr/bin/env node` (e.g., npm)
NODE_DIR="$(dirname "$NODE_BIN")"
NPM_DIR="$(dirname "$NPM_BIN")"
export PATH="$NODE_DIR:$NPM_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if ! command -v node >/dev/null 2>&1; then
  echo "node not found on PATH (PATH=$PATH). Check [paths] node_bin and npm_bin in $CONFIG" >&2
  exit 1
fi

export HOST="${WEB_HOST:-127.0.0.1}"
export PORT="${WEB_PORT:-3000}"

if [[ -n "${API_OVERRIDE:-}" ]]; then
  export NEXT_PUBLIC_API_BASE="$API_OVERRIDE"
fi

cd "$REPO_DIR/web"

# Clear Next.js cache to ensure fresh routes are recognized
# This prevents 404 errors when new routes are added
if [[ -d ".next" ]]; then
  echo "Clearing Next.js cache (.next directory)..."
  rm -rf .next
fi

exec "$NPM_BIN" run dev
