#!/usr/bin/env bash
# Executed evidence for the Docker Compose half of #3824.
#
# The claim: the Compose `ollama` service pulls the configured chat and
# embedding models on start, and does not report healthy until both are there
# -- which is what stops `api` (whose `depends_on` waits on that healthcheck)
# from coming up on a stack that cannot answer a chat message. Nothing about
# that is visible to a unit test or to YAML review; it is a property of the
# running container.
#
# Both halves, per the fault-injection rule (#3753):
#
#   1. Start `ollama` with the shipped model names. It must become healthy,
#      and both models must be in its store afterwards, with the embedding
#      model actually serving /api/embed.
#   2. Restart it pointed at a model that does not exist. It must NOT become
#      healthy -- if it does, the healthcheck is not gating on the model and
#      half 1 proved nothing.
#
# Only the `ollama` service is started: the api/web images take minutes to
# build and add nothing to this question.
#
# Usage: ./scripts/compose-model-prepull-smoke.sh

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

HEALTHY_TIMEOUT="${NYXGPT_SMOKE_HEALTHY_TIMEOUT:-900}"
# How long to give the deliberately-broken configuration before concluding it
# really is not going to report healthy. The server itself is up within
# seconds, so this only has to outlast that plus a probe interval.
UNHEALTHY_WINDOW="${NYXGPT_SMOKE_UNHEALTHY_WINDOW:-120}"
CONTAINER="nyxgpt-ollama"

log() { echo "[compose-model-prepull-smoke] $*"; }
fail() { echo "[compose-model-prepull-smoke] ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is required"

cleanup() {
  log "Tearing down the ollama service"
  docker compose down ollama >/dev/null 2>&1 || true
}
trap cleanup EXIT

health_of() {
  docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "absent"
}

wait_for_health() { # <timeout-seconds> -> 0 when healthy
  local timeout="$1" waited=0 state
  while [[ "$waited" -lt "$timeout" ]]; do
    state=$(health_of)
    [[ "$state" == "healthy" ]] && { log "  healthy after ${waited}s"; return 0; }
    sleep 5
    waited=$((waited + 5))
  done
  log "  still '$(health_of)' after ${waited}s"
  return 1
}

# --- 1. The shipped configuration -------------------------------------------

# Not exported: the compose file's own defaults are what a user gets, and
# those defaults are what this asserts on.
log "Starting the ollama service with the shipped model names"
docker compose up -d ollama

log "Waiting for it to report healthy (it pulls both models first)"
wait_for_health "$HEALTHY_TIMEOUT" \
  || fail "the ollama service never became healthy -- it did not finish pulling the configured models"

chat_model=$(docker compose exec -T ollama printenv NYXGPT_DEFAULT_MODEL | tr -d '\r')
embedding_model=$(docker compose exec -T ollama printenv NYXGPT_EMBEDDING_MODEL | tr -d '\r')
log "Configured models: chat=$chat_model embedding=$embedding_model"

installed=$(docker compose exec -T ollama ollama list)
echo "$installed"
grep -q "${chat_model%%:*}" <<<"$installed" \
  || fail "the chat model $chat_model is not in the store -- the first chat message would fail"
grep -q "${embedding_model%%:*}" <<<"$installed" \
  || fail "the embedding model $embedding_model is not in the store -- the first RAG-enabled message would block on downloading it"
log "Both configured models are in the container's store"

docker compose exec -T ollama \
  curl -fsS http://127.0.0.1:11434/api/embed \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$embedding_model\",\"input\":\"compose smoke\"}" >/dev/null \
  || fail "the embedding model is listed but cannot serve /api/embed"
log "The embedding model answers /api/embed"

# --- 2. The inverse proof ----------------------------------------------------
#
# Point the same service at a model that cannot be pulled. The server still
# starts (the entrypoint tolerates a failed pull rather than crash-looping the
# container), so a healthcheck that only asked "is the server answering" would
# still report healthy -- and `api` would start against a stack with no usable
# model, which is the defect. It must stay unhealthy instead.

log "Fault injection: restarting ollama pointed at a model that does not exist"
docker compose down ollama >/dev/null 2>&1 || true
NYXGPT_DEFAULT_MODEL="nyxgpt-nonexistent-model:0b" docker compose up -d ollama

if wait_for_health "$UNHEALTHY_WINDOW"; then
  fail "the ollama service reported healthy with an unpullable chat model -- its healthcheck is not gating on the model, so the check above proves nothing"
fi
log "It correctly refused to report healthy without its configured chat model"

log "PASS: the Compose ollama service pre-pulls both models and gates health on them"
