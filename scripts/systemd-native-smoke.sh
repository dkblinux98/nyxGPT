#!/usr/bin/env bash
# CI smoke test for the Linux native (systemd --user) install path (#3508).
#
# Mirrors terraform-local-smoke.yml's shape (install -> verify -> diagnostics
# -> teardown) but exercises `nyxgpt ops install` (no --terraform/--kubernetes
# flag), the Homebrew-services-equivalent native path, on Linux.
#
# Wired into CI as .github/workflows/linux-native-smoke.yml (runs on
# `ubuntu-latest`, scoped to changes under src/nyxgpt/ops.py,
# src/nyxgpt/self_heal.py, ops/systemd/**, and this script).
#
# `--dev` runs the same shape against the dev install mode (#3789): install
# with `--dev`, prove the running services are the *checkout* (editable venv,
# Next dev server, a file added to the working tree after install is already
# importable), then reinstall WITHOUT `--dev` and prove the machine switched
# back to the artifact path cleanly -- which is also the inverse proof that
# the dev assertions above are not vacuously true of any install.
#
# Usage:
#   ./scripts/systemd-native-smoke.sh                # full run (artifact path)
#   ./scripts/systemd-native-smoke.sh --dev          # dev-mode run + switch back
#   ./scripts/systemd-native-smoke.sh --keep-up      # leave services running afterwards

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CHECKOUT="$PWD"
KEEP_UP=0
DEV_MODE=0
INSTALL_FLAGS=()
for arg in "$@"; do
  case "$arg" in
    --keep-up) KEEP_UP=1 ;;
    --dev) DEV_MODE=1; INSTALL_FLAGS+=(--dev) ;;
    -h|--help)
      echo "Usage: $0 [--dev] [--keep-up]"
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

# Deliberately NOT pre-installing Ollama (#3508 acceptance). This script used
# to run the official installer here, before `nyxgpt ops install` -- which
# pre-satisfied the one prerequisite a real clean Linux machine does not have,
# so CI could never see that the install step simply stopped and told the
# operator to run that installer themselves. Installing Ollama is now ops's
# job (`_ensure_ollama_installed`), the same way `brew install ollama` is on
# macOS, and this script asserts it happened rather than doing it.
#
# On a CI runner this is a hard requirement: a hosted runner that shipped
# Ollama would turn the assertion below into a tautology, and a green run
# would stop being evidence. On a developer's own machine it is only a
# warning -- they legitimately may have Ollama already, and the rest of the
# script (units, health, status/doctor, teardown) is still worth running.
OLLAMA_PREINSTALLED=0
if command -v ollama >/dev/null 2>&1; then
  if [[ -n "${CI:-}" ]]; then
    fail "ollama is already on PATH -- in CI this smoke test must start from a machine without it, since what it verifies is that \`nyxgpt up\` installs it"
  fi
  OLLAMA_PREINSTALLED=1
  log "WARNING: ollama is already on PATH, so this run cannot verify that
  \`nyxgpt up\` installs it. Every other check still applies."
fi

if [[ ! -f "$HOME/.nyxGPT/config.ini" ]]; then
  mkdir -p "$HOME/.nyxGPT"
  # example.config.ini is the human template / schema source of truth; it's
  # a valid native config for a from-scratch bring-up.
  cp example.config.ini "$HOME/.nyxGPT/config.ini"
  log "Seeded ~/.nyxGPT/config.ini from example.config.ini"
fi

# `nyxgpt up`, not `nyxgpt ops install`: the four commands the owner named in
# #3508's acceptance are `up`, `down`, `ops status` and `ops doctor`, and this
# script previously exercised none of them -- it drove `ops install`/`ops
# down`, so the aliases a Linux operator actually types were untested. `up` is
# a strict superset (it runs the same install, then waits for health).
# INSTALL_FLAGS (--dev, #3789) passes straight through: `up` forwards every
# mode flag to the same install.
log "nyxgpt up ${INSTALL_FLAGS[*]-} --skip-observability"
nyxgpt up ${INSTALL_FLAGS[@]+"${INSTALL_FLAGS[@]}"} --skip-observability --timeout 300

# The install had to have installed Ollama itself -- the assertion the removed
# pre-install above used to make impossible.
if [[ "$OLLAMA_PREINSTALLED" -eq 0 ]]; then
  command -v ollama >/dev/null 2>&1 || fail "nyxgpt up did not install Ollama"
  log "ollama installed by nyxgpt up -> $(command -v ollama)"
fi

# Both checks below wait with a bounded retry window instead of probing once
# immediately: a unit freshly (re)started by `nyxgpt ops install` -- and
# especially `npm run start`'s Next.js boot behind nyxgpt-web -- can still be
# coming up when this script reaches its verification step, so a single
# immediate probe races startup rather than testing actual health (#3632: a
# 464ms-old nyxgpt-web unit returned `000` before Next.js had bound its port).
WAIT_TIMEOUT_SECONDS=60
WAIT_INTERVAL_SECONDS=2
# Dev mode serves through `next dev`, which compiles the requested route on
# the first request rather than serving a prebuilt bundle -- a cold first
# GET / legitimately takes longer than an artifact-mode `next start` one.
[[ "$DEV_MODE" -eq 1 ]] && WAIT_TIMEOUT_SECONDS=240

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
# `ollama.service` bound to the same port `nyxgpt-ollama.service` needs.
# `nyxgpt ops install` stops and disables that system unit so nyxgpt owns
# Ollama itself (`_takeover_system_ollama_service` in src/nyxgpt/ops.py,
# #3632), so after install the system unit must be gone and nyxgpt-ollama
# must be the one serving -- assert both, since an "adopted" system unit
# would otherwise pass the HTTP check below while leaving Ollama pointed at
# the wrong model store.
if systemctl is-active --quiet ollama.service 2>/dev/null; then
  echo "::error::system-wide ollama.service is still active after install -- nyxgpt ops install should have disabled it"
  fail_count=1
fi

for unit in nyxgpt-api nyxgpt-web nyxgpt-ollama nyxgpt-cassandra-logs nyxgpt-ollama-logs; do
  wait_for_unit_active "$unit" || { echo "::error::$unit is not active"; fail_count=1; }
done

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

# The other two commands #3508's acceptance names. Both are read-only, so
# they are asserted on a stack that is up: `ops status` has to report the
# native components as running (a Linux `status` that printed macOS's brew
# view, or nothing, would still exit 0 -- so grep the output, don't just
# trust the exit code), and `ops doctor` has to find a healthy install clean.
log "nyxgpt ops status"
status_out=$(nyxgpt ops status 2>&1) || { echo "::error::nyxgpt ops status exited non-zero"; fail_count=1; }
echo "$status_out"
for line in "native  api: started" "native  web: started" "native  ollama: started"; do
  grep -qF "$line" <<<"$status_out" || {
    echo "::error::nyxgpt ops status did not report '$line'"; fail_count=1;
  }
done
grep -qF "systemd --user services:" <<<"$status_out" || {
  echo "::error::nyxgpt ops status printed no systemd section -- OS dispatch is wrong"; fail_count=1;
}

log "nyxgpt ops doctor"
if ! doctor_out=$(nyxgpt ops doctor 2>&1); then
  echo "$doctor_out"
  echo "::error::nyxgpt ops doctor reported issues on a freshly-installed stack"
  fail_count=1
else
  echo "$doctor_out"
fi

# --- Dev mode: prove the running stack IS the checkout, then prove the
# --- machine switches back to the artifact path cleanly (#3789).
API_VENV_PY="$HOME/.nyxGPT/opt/nyxgpt-api/venv/bin/python3"
WORKTREE_MARKER="$CHECKOUT/src/nyxgpt/_dev_mode_smoke_marker.py"

venv_imports_the_checkout() { # -> 0 if the venv's nyxgpt resolves inside $CHECKOUT
  local resolved
  resolved=$("$API_VENV_PY" -c 'import nyxgpt, pathlib; print(pathlib.Path(nyxgpt.__file__).resolve())' 2>/dev/null || echo "")
  echo "  venv nyxgpt resolves to: ${resolved:-<import failed>}"
  [[ -n "$resolved" && "$resolved" == "$CHECKOUT"/* ]]
}

if [[ "$DEV_MODE" -eq 1 ]]; then
  log "Verifying the dev-mode install is actually running the checkout"

  # 1. `ops status` must SAY dev mode -- a dev pass must not be mistakable
  #    for an artifact-path pass (acceptance criterion 3).
  if nyxgpt ops status | grep -q "Install mode: dev (editable checkout at $CHECKOUT)"; then
    echo "  ops status reports dev mode at $CHECKOUT"
  else
    echo "::error::ops status does not report dev mode for $CHECKOUT"
    nyxgpt ops status || true
    fail_count=1
  fi

  # 2. The api venv must import nyxgpt out of the checkout, not out of a
  #    tarball/keg build copied into the venv.
  venv_imports_the_checkout || { echo "::error::api venv does not import nyxgpt from $CHECKOUT"; fail_count=1; }

  # 3. Nothing built an artifact: no dist tarball anywhere under the
  #    ops-managed install roots.
  if find "$HOME/.nyxGPT/opt" -name 'nyxgpt-*.tar.gz' -print -quit | grep -q .; then
    echo "::error::a dist tarball was built in dev mode -- the keg/tarball path was taken"
    fail_count=1
  else
    echo "  no dist tarball under ~/.nyxGPT/opt (no keg/tarball build)"
  fi

  # 4. The web service runs the checkout's dev server, not a built bundle.
  if grep -q "cd \"$CHECKOUT/web\"" "$HOME/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web" \
     && grep -q "npm run dev" "$HOME/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web"; then
    echo "  web wrapper runs 'npm run dev' in $CHECKOUT/web"
  else
    echo "::error::web wrapper is not pointed at the checkout's dev server"
    cat "$HOME/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web" || true
    fail_count=1
  fi

  # 5. The whole point of the mode: a change made to the working tree AFTER
  #    the install is already live for the installed service, with nothing
  #    rebuilt or reinstalled in between.
  echo "MARKER = 'dev-mode picks up the working tree'" > "$WORKTREE_MARKER"
  if "$API_VENV_PY" -c 'from nyxgpt._dev_mode_smoke_marker import MARKER; print("  " + MARKER)'; then
    :
  else
    echo "::error::a file added to the checkout after install is NOT visible to the installed api"
    fail_count=1
  fi

  # Remove it BEFORE the switch-back: the artifact install vendors
  # `src/nyxgpt/` from this same checkout into its tarball, so a marker left
  # in the tree here would be built INTO the artifact venv and the inverse
  # check below would be testing vendoring, not working-tree pickup.
  rm -f "$WORKTREE_MARKER"

  # 6. Switching back: reinstall without --dev and prove the machine is on
  #    the artifact path again -- services still serving, mode relabelled,
  #    and the venv no longer importing the checkout.
  log "Switching back to the artifact path: nyxgpt ops install --skip-observability"
  nyxgpt ops install --skip-observability

  for unit in nyxgpt-api nyxgpt-web; do
    wait_for_unit_active "$unit" || { echo "::error::$unit is not active after switching back"; fail_count=1; }
  done
  check "api    /health" http://127.0.0.1:8000/health 200 || { echo "::error::api /health expected 200 after mode switch"; fail_count=1; }
  check "web    /"       http://127.0.0.1:3000/ 200       || { echo "::error::web / expected 200 after mode switch"; fail_count=1; }

  if nyxgpt ops status | grep -q "Install mode: artifact"; then
    echo "  ops status reports artifact mode after the switch"
  else
    echo "::error::ops status still reports dev mode after reinstalling without --dev"
    fail_count=1
  fi

  if venv_imports_the_checkout; then
    echo "::error::the api venv still imports the checkout after switching to the artifact path"
    fail_count=1
  else
    echo "  artifact-mode venv no longer imports the checkout (dev checks are not vacuous)"
  fi

  # 7. The inverse proof for step 5 (#3753's fault-injection rule): the same
  #    "add a file to the working tree, then import it from the service's
  #    venv" check must FAIL on an artifact install. Written only now, after
  #    that install has vendored the tree, so nothing could have built it in.
  echo "MARKER = 'artifact mode must NOT pick this up'" > "$WORKTREE_MARKER"
  if "$API_VENV_PY" -c 'import nyxgpt._dev_mode_smoke_marker' 2>/dev/null; then
    echo "::error::the working-tree marker is importable from an ARTIFACT install -- step 5 proves nothing"
    fail_count=1
  else
    echo "  working-tree marker is not visible to the artifact install, as expected"
  fi
  rm -f "$WORKTREE_MARKER"
fi

if [[ "$fail_count" -ne 0 ]]; then
  log "Dumping diagnostics"
  systemctl --user status nyxgpt-api nyxgpt-web nyxgpt-ollama --no-pager -l || true
  tail -n 80 "$HOME/.nyxGPT/logs/nyxgpt-api.err.log" 2>/dev/null || true
  tail -n 80 "$HOME/.nyxGPT/logs/nyxgpt-web.err.log" 2>/dev/null || true
fi

if [[ "$KEEP_UP" -eq 0 ]]; then
  # `nyxgpt down`, the alias, for the same reason `nyxgpt up` is used above.
  log "Tearing down: nyxgpt down"
  nyxgpt down || true
  for unit in nyxgpt-api nyxgpt-web nyxgpt-ollama; do
    state=$(systemctl --user is-active "$unit.service" 2>/dev/null || echo "inactive")
    [[ "$state" == "active" ]] && {
      echo "::error::$unit is still active after nyxgpt down"; fail_count=1;
    }
  done
else
  log "--keep-up set: leaving services running"
fi

[[ "$fail_count" -eq 0 ]] || fail "one or more checks failed"
if [[ "$DEV_MODE" -eq 1 ]]; then
  log "Linux native dev-mode install smoke test passed (dev install + switch back to artifact)."
else
  log "Linux native (systemd) install smoke test passed."
fi
