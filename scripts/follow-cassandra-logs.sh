#!/usr/bin/env bash
set -euo pipefail

# nyxGPT Cassandra log follower
# Continuously appends Docker container logs to ~/.nyxGPT/logs/cassandra.log
# Rotates at MAX_BYTES, keeping KEEP_BACKUPS numbered backups.

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not available in PATH; cannot follow Cassandra logs" >&2
  exit 1
fi

LOG_DIR="$HOME/.nyxGPT/logs"
LOG_FILE="$LOG_DIR/cassandra.log"
CONTAINER_NAME="nyxgpt-cassandra"
MAX_BYTES=$((50 * 1024 * 1024))   # 50 MB per file
KEEP_BACKUPS=3                      # cassandra.log.1 .. cassandra.log.3
CHECK_INTERVAL=10                   # seconds between size checks

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

rotate_log() {
  local i
  for i in $(seq $((KEEP_BACKUPS - 1)) -1 1); do
    [ -f "${LOG_FILE}.${i}" ] && mv "${LOG_FILE}.${i}" "${LOG_FILE}.$((i + 1))"
  done
  [ -f "${LOG_FILE}.${KEEP_BACKUPS}" ] && rm -f "${LOG_FILE}.${KEEP_BACKUPS}"
  mv "$LOG_FILE" "${LOG_FILE}.1"
  touch "$LOG_FILE"
}

# Wait for Docker to be available (Docker Desktop may start after login)
until docker info >/dev/null 2>&1; do
  sleep 2
done

# Wait until the container exists (and optionally is running)
until docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; do
  sleep 2
done

# Follow logs with rotation: restart docker logs after each rotation so the new
# file descriptor points at the fresh log file.
#
# IMPORTANT: use `--tail 0` so we only capture NEW lines from this point forward.
# Without it, every (re)start re-dumps the container's entire history, which (a)
# defeats rotation and (b) compounds every time launchd respawns the follower.
while true; do
  docker logs --tail 0 -f "$CONTAINER_NAME" >>"$LOG_FILE" 2>&1 &
  DOCKER_PID=$!

  while kill -0 "$DOCKER_PID" 2>/dev/null; do
    sleep "$CHECK_INTERVAL"
    SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt "$MAX_BYTES" ]; then
      kill "$DOCKER_PID" 2>/dev/null
      wait "$DOCKER_PID" 2>/dev/null || true
      rotate_log
      break
    fi
  done

  # Brief pause before restarting (container may have stopped too)
  sleep 5
done
