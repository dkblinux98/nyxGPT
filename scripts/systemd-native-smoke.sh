#!/usr/bin/env bash
# CI smoke test for the Linux native (systemd --user) install path (#3508).
#
# Mirrors terraform-local-smoke.yml's shape (install -> verify -> diagnostics
# -> teardown) but exercises `nyxgpt ops install` (no --terraform/--kubernetes
# flag), the Homebrew-services-equivalent native path, on Linux.
#
# NOTE: this script is intentionally NOT wired into a GitHub Actions workflow
# yet -- .github/workflows/* changes are outside this change's scope (see
# the PR description). A maintainer should add a workflow that runs this on
# `ubuntu-latest`, mirroring terraform-local-smoke.yml's structure, scoped to
# changes under src/nyxgpt/ops.py, src/nyxgpt/self_heal.py, ops/systemd/**,
# and this script.
#
# Usage:
#   ./scripts/systemd-native-smoke.sh                # full run
#   ./scripts/systemd-native-smoke.sh --keep-up       # leave services running afterwards

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEEP_UP=0
for arg in "$@"; do
  case "$arg" in
    --keep-up) KEEP_UP=1 ;;
    -h|--help)
      echo "Usage: $0 [--keep-up]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

log() { echo "[systemd-native-smoke] $*"; }
fail() { echo "[systemd-native-smoke] ERROR: $*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || fail "required tool not found: $1"
}

require systemctl
require docker
require curl
require npm

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "this script only exercises the Linux native path -- run it on Linux"
fi

# `systemctl --user` needs a reachable D-Bus session bus. GitHub Actions'
# ubuntu-latest runners already have one for the default `runner` user (no
# special lingering/dbus-run-session setup observed to be needed) -- this is
# a defensive fallback for a minimal/headless host that doesn't.
if ! systemctl --user status >/dev/null 2>&1; then
  log "No systemd --user session detected; enabling lingering for $(whoami)"
  sudo loginctl enable-linger "$(whoami)" || true
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  systemctl --user status >/dev/null 2>&1 || fail "systemctl --user still unreachable after enabling lingering"
fi

if ! command -v ollama >/dev/null 2>&1; then
  log "Installing Ollama (official Linux installer)"
  curl -fsSL https://ollama.com/install.sh | sh
fi

if [[ ! -f "$HOME/.nyxGPT/config.ini" ]]; then
  mkdir -p "$HOME/.nyxGPT"
  # example.config.ini is the human template / schema source of truth; it's
  # a valid native config for a from-scratch bring-up.
  cp example.config.ini "$HOME/.nyxGPT/config.ini"
  log "Seeded ~/.nyxGPT/config.ini from example.config.ini"
fi

log "nyxgpt ops install --skip-observability"
nyxgpt ops install --skip-observability

log "Verifying systemd --user units are active"
fail_count=0
for unit in nyxgpt-api nyxgpt-web nyxgpt-ollama nyxgpt-cassandra-logs nyxgpt-ollama-logs; do
  state=$(systemctl --user is-active "$unit.service" 2>/dev/null || echo "inactive")
  echo "  $unit -> $state"
  [[ "$state" == "active" ]] || { echo "::error::$unit is not active"; fail_count=1; }
done

log "Verifying core services are actually serving"
check() { # <label> <url> <expected>
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$2" || echo 000)
  echo "  $1 -> $code (expect $3)"
  [[ "$code" == "$3" ]] || { echo "::error::$1 expected $3, got $code"; fail_count=1; }
}
check "api  /health" http://127.0.0.1:8000/health 200
check "web  /"       http://127.0.0.1:3000/ 200

if [[ "$fail_count" -ne 0 ]]; then
  log "Dumping diagnostics"
  systemctl --user status nyxgpt-api nyxgpt-web nyxgpt-ollama --no-pager -l || true
  tail -n 80 "$HOME/.nyxGPT/logs/nyxgpt-api.err.log" 2>/dev/null || true
  tail -n 80 "$HOME/.nyxGPT/logs/nyxgpt-web.err.log" 2>/dev/null || true
fi

if [[ "$KEEP_UP" -eq 0 ]]; then
  log "Tearing down: nyxgpt ops down"
  nyxgpt ops down || true
else
  log "--keep-up set: leaving services running"
fi

[[ "$fail_count" -eq 0 ]] || fail "one or more checks failed"
log "Linux native (systemd) install smoke test passed."
