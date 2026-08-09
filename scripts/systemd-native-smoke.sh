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

# Both checks below wait with a bounded retry window instead of probing once
# immediately: a unit freshly (re)started by `nyxgpt ops install` -- and
# especially `npm run start`'s Next.js boot behind nyxgpt-web -- can still be
# coming up when this script reaches its verification step, so a single
# immediate probe races startup rather than testing actual health (#3632: a
# 464ms-old nyxgpt-web unit returned `000` before Next.js had bound its port).
WAIT_TIMEOUT_SECONDS=60
WAIT_INTERVAL_SECONDS=2

log "Verifying systemd --user units are active (up to ${WAIT_TIMEOUT_SECONDS}s each)"
fail_count=0
wait_for_unit_active() { # <unit>
  local unit="$1" elapsed=0 state
  while :; do
    state=$(systemctl --user is-active "$unit.service" 2>/dev/null || echo "inactive")
    [[ "$state" == "active" ]] && { echo "  $unit -> $state"; return 0; }
    elapsed=$((elapsed + WAIT_INTERVAL_SECONDS))
    [[ "$elapsed" -ge "$WAIT_TIMEOUT_SECONDS" ]] && { echo "  $unit -> $state (gave up after ${elapsed}s)"; return 1; }
    sleep "$WAIT_INTERVAL_SECONDS"
  done
}
# The official installer run above auto-enables a *system-wide*
# `ollama.service` bound to the same port `nyxgpt-ollama.service` needs --
# `nyxgpt ops install` now detects that and adopts the system unit instead of
# installing/starting its own (see `_reconcile_system_ollama_service` in
# src/nyxgpt/ops.py, #3632), so nyxgpt-ollama.service is only expected to be
# active when nothing else already claimed the port.
ollama_adopted=0
if systemctl is-active --quiet ollama.service 2>/dev/null; then
  ollama_adopted=1
  log "System-wide ollama.service is active -- nyxgpt adopts it (nyxgpt-ollama.service is not installed/started, see docs/systemd.md)"
fi

for unit in nyxgpt-api nyxgpt-web nyxgpt-cassandra-logs nyxgpt-ollama-logs; do
  wait_for_unit_active "$unit" || { echo "::error::$unit is not active"; fail_count=1; }
done
if [[ "$ollama_adopted" -eq 1 ]]; then
  echo "  nyxgpt-ollama -> skipped (system ollama.service adopted instead)"
else
  wait_for_unit_active nyxgpt-ollama || { echo "::error::nyxgpt-ollama is not active"; fail_count=1; }
fi

log "Verifying core services are actually serving (up to ${WAIT_TIMEOUT_SECONDS}s each)"
check() { # <label> <url> <expected>
  local label="$1" url="$2" expected="$3" elapsed=0 code
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url" || echo 000)
    [[ "$code" == "$expected" ]] && { echo "  $label -> $code (expect $expected)"; return 0; }
    elapsed=$((elapsed + WAIT_INTERVAL_SECONDS))
    [[ "$elapsed" -ge "$WAIT_TIMEOUT_SECONDS" ]] && { echo "  $label -> $code (expect $expected, gave up after ${elapsed}s)"; return 1; }
    sleep "$WAIT_INTERVAL_SECONDS"
  done
}
check "api    /health" http://127.0.0.1:8000/health 200 || { echo "::error::api    /health expected 200"; fail_count=1; }
check "web    /"       http://127.0.0.1:3000/ 200       || { echo "::error::web    / expected 200"; fail_count=1; }
check "ollama /"       http://127.0.0.1:11434/ 200      || { echo "::error::ollama / expected 200"; fail_count=1; }

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
