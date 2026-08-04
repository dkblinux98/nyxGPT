#!/usr/bin/env bash
set -euo pipefail

# nyxGPT Ollama log follower
# Continuously appends Ollama's logs to ~/.nyxGPT/logs/ollama.log, in
# whichever mode Ollama is actually running:
#   - Compose mode (`nyxgpt-ollama` Docker container): follows `docker logs -f`.
#   - Native mode (Ollama as a Homebrew service): tails Homebrew's own
#     ollama.log directly. A *symlink* into that path doesn't work here --
#     promtail reads ~/.nyxGPT/logs from inside its own container via a
#     read-only bind mount, and a symlink whose target lives outside that
#     mount (Homebrew's var/log dir) is unreachable from inside the
#     container, so the symlink this script used to rely on
#     (`_ensure_log_symlinks` in src/nyxgpt/ops.py) never actually shipped
#     any lines to Loki (#3441). This script always writes a real file
#     instead, mirroring follow-cassandra-logs.sh.
# Rotates ~/.nyxGPT/logs/ollama.log at MAX_BYTES, keeping KEEP_BACKUPS
# numbered backups.
#
# Re-checks which mode applies every CHECK_INTERVAL seconds so a deployment
# mode switch (or Ollama simply not having started yet) is picked up without
# needing to restart this LaunchAgent by hand.

LOG_DIR="$HOME/.nyxGPT/logs"
LOG_FILE="$LOG_DIR/ollama.log"
CONTAINER_NAME="nyxgpt-ollama"
MAX_BYTES=$((50 * 1024 * 1024))   # 50 MB per file
KEEP_BACKUPS=3                      # ollama.log.1 .. ollama.log.3
CHECK_INTERVAL=10                   # seconds between size/mode checks

mkdir -p "$LOG_DIR"

rotate_log() {
  local i
  for i in $(seq $((KEEP_BACKUPS - 1)) -1 1); do
    [ -f "${LOG_FILE}.${i}" ] && mv "${LOG_FILE}.${i}" "${LOG_FILE}.$((i + 1))"
  done
  [ -f "${LOG_FILE}.${KEEP_BACKUPS}" ] && rm -f "${LOG_FILE}.${KEEP_BACKUPS}"
  mv "$LOG_FILE" "${LOG_FILE}.1"
  touch "$LOG_FILE"
}

# ~/.nyxGPT/logs/ollama.log may be a stale symlink left over from a prior
# nyxgpt version's `_ensure_log_symlinks` (#3441) -- replace it with a plain
# file this script owns so append/tail redirection below writes here, not
# through the (unreachable-from-promtail) symlink target.
ensure_real_file() {
  if [ -L "$LOG_FILE" ]; then
    rm -f "$LOG_FILE"
  fi
  touch "$LOG_FILE"
}

compose_container_present() {
  command -v docker >/dev/null 2>&1 && docker inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

# Resolve the native Ollama log path. On macOS this is Homebrew's own
# ollama.log -- tries `brew --prefix` first, then falls back to the two
# conventional prefixes (Apple Silicon/Intel), mirroring docs/api.md's manual
# fallback instructions. On Linux, native Ollama runs as the
# nyxgpt-ollama.service systemd --user unit (see src/nyxgpt/ops.py's
# _install_native_ollama_systemd), which appends its own stdout/stderr
# straight to ~/.nyxGPT/logs/ollama-native.log -- included as a candidate so
# this script's rotation applies there too, uniformly with macOS.
native_ollama_log() {
  local prefix candidate
  if command -v brew >/dev/null 2>&1; then
    prefix="$(brew --prefix 2>/dev/null || true)"
  fi
  for candidate in "${prefix:-}/var/log/ollama.log" /opt/homebrew/var/log/ollama.log /usr/local/var/log/ollama.log "$HOME/.nyxGPT/logs/ollama-native.log"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Watches $SOURCE_PID, rotating on size, and bailing out early if `$1`
# ("compose"/"native") stops being the currently-active mode -- e.g. a
# Compose container appears while we're tailing the native Homebrew log --
# so control returns to the outer loop to switch sources instead of
# double-writing from both at once.
follow_source() {
  local mode="$1"
  while kill -0 "$SOURCE_PID" 2>/dev/null; do
    sleep "$CHECK_INTERVAL"
    if [ "$mode" = "native" ] && compose_container_present; then
      kill "$SOURCE_PID" 2>/dev/null
      wait "$SOURCE_PID" 2>/dev/null || true
      return 0
    fi
    SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt "$MAX_BYTES" ]; then
      kill "$SOURCE_PID" 2>/dev/null
      wait "$SOURCE_PID" 2>/dev/null || true
      rotate_log
    fi
  done
}

while true; do
  if compose_container_present; then
    ensure_real_file
    # IMPORTANT: use `--tail 0` so we only capture NEW lines from this point
    # forward. Without it, every (re)start re-dumps the container's entire
    # history, which (a) defeats rotation and (b) compounds every time
    # launchd respawns the follower.
    docker logs --tail 0 -f "$CONTAINER_NAME" >>"$LOG_FILE" 2>&1 &
    SOURCE_PID=$!
    follow_source "compose"
  elif NATIVE_LOG="$(native_ollama_log)"; then
    ensure_real_file
    # `-n 0`: only capture NEW lines from this point forward, same reasoning
    # as `--tail 0` above.
    tail -n 0 -F "$NATIVE_LOG" >>"$LOG_FILE" 2>&1 &
    SOURCE_PID=$!
    follow_source "native"
  else
    # Neither a Compose container nor a native Homebrew log exists yet
    # (Ollama hasn't been started at all) -- idle and retry.
    sleep "$CHECK_INTERVAL"
  fi

  # Brief pause before restarting (the source may have stopped too).
  sleep 5
done
