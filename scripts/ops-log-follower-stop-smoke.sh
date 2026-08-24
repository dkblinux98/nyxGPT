#!/usr/bin/env bash
# Executed evidence (#3775) for #4033: `nyxgpt ops stop ollama-logs` really
# brings the Ollama log follower down on a real machine.
#
# The question this job answers: an operator who wants the
# `tail -n 0 -F .../ollama.log` process gone can now type a `nyxgpt` command
# to get it gone, rather than `launchctl bootout` / `systemctl --user stop`.
# Unit tests cannot answer it -- they mock `systemctl`, so they prove the
# right unit name is passed and nothing about whether the process actually
# dies.
#
# Both halves of the proof, in one job:
#   1. FAILS WITHOUT THE FIX -- the pre-fix CLI, run from a worktree of the
#      base ref, is handed `ops stop ollama-logs` and refuses it (argparse
#      `invalid choice`), leaving the follower running. That is the defect
#      the owner reported, reproduced by execution rather than asserted.
#   2. PASSES WITH IT -- the working tree's CLI stops the same live unit,
#      and the follower process is gone afterwards. Repeated for
#      `nyxgpt ops stop all`, which did not cover the follower either.
#
# Deliberately narrow: it installs the one systemd unit under test via the
# real installer (`_sync_packaged_resources` + `_install_ollama_log_follower_service`)
# instead of running a full `nyxgpt ops install`, so the run costs seconds
# rather than the ~1.4GB Ollama download that systemd-native-smoke.sh pays.
# The install path itself is that script's job, not this one's.
#
# Wired into CI as the `ops-log-follower-stop` job in
# .github/workflows/linux-native-smoke.yml.
#
# Usage:
#   ./scripts/ops-log-follower-stop-smoke.sh              # full run
#   ./scripts/ops-log-follower-stop-smoke.sh --skip-prefix-check
#       (skip half 1 -- for a shallow checkout with no base ref available)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
CHECKOUT="$PWD"

SKIP_PREFIX_CHECK=0
BASE_REF="${BASE_REF:-}"
for arg in "$@"; do
  case "$arg" in
    --skip-prefix-check) SKIP_PREFIX_CHECK=1 ;;
    -h|--help) echo "Usage: $0 [--skip-prefix-check]"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { echo "[ops-log-follower-stop-smoke] $*"; }
fail() { echo "[ops-log-follower-stop-smoke] ERROR: $*" >&2; exit 1; }

UNIT="nyxgpt-ollama-logs.service"
BASE_WORKTREE=""

cleanup() {
  systemctl --user stop "$UNIT" >/dev/null 2>&1 || true
  systemctl --user disable "$UNIT" >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/$UNIT" || true
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if [[ -n "$BASE_WORKTREE" ]]; then
    git -C "$CHECKOUT" worktree remove --force "$BASE_WORKTREE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(uname -s)" == "Linux" ]] || fail "this script exercises the Linux systemd path -- run it on Linux"
command -v systemctl >/dev/null 2>&1 || fail "required tool not found: systemctl"
# The console script, not `python -m nyxgpt.cli`: cli.py has no
# `__main__` guard, so `-m` imports it, runs nothing and exits 0 -- which
# would make every assertion below vacuously green.
command -v nyxgpt >/dev/null 2>&1 || fail "required tool not found: nyxgpt (pip install -e .)"

# `systemctl --user` needs a reachable D-Bus session bus -- same defensive
# fallback systemd-native-smoke.sh uses for a headless host.
if ! systemctl --user status >/dev/null 2>&1; then
  log "No systemd --user session detected; enabling lingering for $(whoami)"
  sudo loginctl enable-linger "$(whoami)" || true
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  systemctl --user status >/dev/null 2>&1 || fail "systemctl --user still unreachable after enabling lingering"
fi

unit_active() { [[ "$(systemctl --user is-active "$UNIT" 2>/dev/null || true)" == "active" ]]; }

require_active() {
  unit_active || {
    systemctl --user status "$UNIT" --no-pager || true
    fail "$1: expected $UNIT to be active, it is '$(systemctl --user is-active "$UNIT" 2>/dev/null || true)'"
  }
}

require_inactive() {
  # `stop` is not synchronous with the child dying; give systemd a moment.
  for _ in $(seq 1 20); do
    unit_active || break
    sleep 0.5
  done
  if unit_active; then
    systemctl --user status "$UNIT" --no-pager || true
    fail "$1: $UNIT is still active after the stop"
  fi
  if pgrep -f 'follow-ollama-logs\.sh' >/dev/null 2>&1; then
    pgrep -af 'follow-ollama-logs\.sh' || true
    fail "$1: the follow-ollama-logs.sh process outlived the stop"
  fi
}

install_follower() {
  log "Installing $UNIT via the real ops installer"
  python3 - <<'PY'
import sys
from pathlib import Path

from nyxgpt import ops

# systemd opens the unit's `StandardOutput=append:...` targets before
# ExecStart runs, so ~/.nyxGPT/logs must exist or the unit dies with
# 209/STDOUT. A full `nyxgpt ops install` creates it as a side effect of the
# native-service steps this narrow run deliberately skips -- same call it
# makes.
ops._ensure_dir(Path.home() / ".nyxGPT" / "logs")

results = ops._sync_packaged_resources() + ops._install_ollama_log_follower_service()
for r in results:
    print(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
if not all(r.ok for r in results):
    sys.exit(1)
PY
}

# --- Half 1: the defect, reproduced by execution ------------------------------
#
# Runs the BASE ref's CLI against a live follower. The editable install puts
# `<checkout>/src` on sys.path via a plain .pth file, so PYTHONPATH -- which
# is searched first -- is enough to run the pre-fix code with the same
# already-installed dependencies. No second venv, no second dependency
# resolution.
if [[ "$SKIP_PREFIX_CHECK" -eq 1 ]]; then
  log "SKIPPING the pre-fix reproduction (--skip-prefix-check)"
else
  if [[ -z "$BASE_REF" ]]; then
    BASE_REF="$(git -C "$CHECKOUT" rev-parse --verify HEAD~1 2>/dev/null || true)"
  fi
  [[ -n "$BASE_REF" ]] || fail "no BASE_REF to compare against (set BASE_REF=<ref> or pass --skip-prefix-check)"

  BASE_WORKTREE="$(mktemp -d)/base"
  log "Checking out the pre-fix code ($BASE_REF) to prove it fails"
  git -C "$CHECKOUT" worktree add --detach --quiet "$BASE_WORKTREE" "$BASE_REF"

  install_follower
  require_active "after install (pre-fix half)"

  set +e
  prefix_out="$(PYTHONPATH="$BASE_WORKTREE/src" nyxgpt ops stop ollama-logs 2>&1)"
  prefix_rc=$?
  set -e
  echo "$prefix_out"

  # Confirm we really ran the base code and not the working tree's.
  PYTHONPATH="$BASE_WORKTREE/src" python3 -c "
import nyxgpt, sys
assert nyxgpt.__file__.startswith('$BASE_WORKTREE'), f'ran {nyxgpt.__file__}, not the base worktree'
" || fail "the pre-fix invocation did not actually load the base worktree's nyxgpt"

  if [[ "$prefix_rc" -eq 0 ]]; then
    fail "the base ref accepted 'ops stop ollama-logs' -- this run proves nothing (is BASE_REF wrong?)"
  fi
  grep -q "invalid choice: 'ollama-logs'" <<<"$prefix_out" \
    || fail "expected the pre-fix CLI to reject 'ollama-logs' as an invalid choice, got: $prefix_out"
  require_active "pre-fix CLI refused the command, so the follower must still be running"
  log "CONFIRMED: without the fix, 'nyxgpt ops stop ollama-logs' is refused and the follower survives"
fi

# --- Half 2: the fix, demonstrated by execution -------------------------------
install_follower
require_active "after install"

log "Running 'nyxgpt ops stop ollama-logs' from the working tree"
nyxgpt ops stop ollama-logs --quiet || fail "'ops stop ollama-logs' exited non-zero"
require_inactive "after 'ops stop ollama-logs'"
log "CONFIRMED: 'nyxgpt ops stop ollama-logs' brought the follower down"

# `stop all` did not reach the follower either -- the owner's report names
# both ("nyxgpt ops stop accepts cassandra-logs but not ollama-logs ... and
# all doesn't cover it either").
install_follower
require_active "after reinstall for the 'stop all' check"

log "Running 'nyxgpt ops stop all'"
# rc is deliberately not asserted: this narrow run installs only the Ollama
# follower, so `all` also reaches a cassandra-logs unit that was never
# installed and reports that as a failure. What must hold is that `all`
# issued the stop for THIS follower and the follower is gone -- both checked
# below, so the tolerated rc cannot hide the claim under test.
stop_all_out="$(nyxgpt ops stop all --quiet 2>&1 || true)"
echo "$stop_all_out"
grep -q "Stopped systemd unit: nyxgpt-ollama-logs" <<<"$stop_all_out" \
  || fail "'ops stop all' never issued a stop for nyxgpt-ollama-logs"
require_inactive "after 'ops stop all'"
log "CONFIRMED: 'nyxgpt ops stop all' covers the follower"

log "PASS"
