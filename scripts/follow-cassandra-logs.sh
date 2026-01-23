#!/usr/bin/env bash
set -euo pipefail

# nyxGPT Cassandra log follower
# Continuously appends Docker container logs to ~/.nyxGPT/logs/cassandra.log

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not available in PATH; cannot follow Cassandra logs" >&2
  exit 1
fi

LOG_DIR="$HOME/.nyxGPT/logs"
LOG_FILE="$LOG_DIR/cassandra.log"
CONTAINER_NAME="nyxgpt-cassandra"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

# Wait for Docker to be available (Docker Desktop may start after login)
until docker info >/dev/null 2>&1; do
  sleep 2
done

# Wait until the container exists (and optionally is running)
until docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; do
  sleep 2
done

# Follow logs forever; append to file
exec docker logs -f "$CONTAINER_NAME" >>"$LOG_FILE" 2>&1
