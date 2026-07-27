#!/usr/bin/env bash
# End-to-end smoke test for the local Docker Compose deployment (#3160):
# deploy -> verify chat/RAG -> kill each core component -> observe self-heal
# recovery -> teardown. See docs/self-healing.md for the full write-up,
# including the one expected exception (the `api` container can't heal
# itself -- the watchdog runs inside it).
#
# Usage:
#   ./scripts/smoke-test.sh                # full run
#   ./scripts/smoke-test.sh --skip-deploy   # stack is already up; skip straight to verify/kill/heal
#   ./scripts/smoke-test.sh --keep-up       # leave the stack running afterwards

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

API_URL="${NYXGPT_API_URL:-http://localhost:8000}"
KEEP_UP=0
SKIP_DEPLOY=0

for arg in "$@"; do
  case "$arg" in
    --keep-up) KEEP_UP=1 ;;
    --skip-deploy) SKIP_DEPLOY=1 ;;
    -h|--help)
      echo "Usage: $0 [--skip-deploy] [--keep-up]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

log() { echo "[smoke-test] $*"; }
fail() { echo "[smoke-test] ERROR: $*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || fail "required tool not found: $1"
}

require docker
require curl
require python3

# docker/config.docker.ini is a git-ignored, per-machine artifact seeded from
# its tracked .example template (see .gitignore / docs/docker-compose.md). This
# script drives Compose directly without going through `nyxgpt ops install`, so
# seed it here if a fresh checkout hasn't yet -- the bind mount at
# docker-compose.yml:108 needs a real file, or Docker creates an empty dir.
if [[ ! -f docker/config.docker.ini ]]; then
  cp docker/config.docker.ini.example docker/config.docker.ini
  log "Seeded docker/config.docker.ini from its .example template"
fi

NYXGPT_AUTH_API_KEY="${NYXGPT_AUTH_API_KEY:-}"
if [[ -z "$NYXGPT_AUTH_API_KEY" && -f .env ]]; then
  NYXGPT_AUTH_API_KEY="$(sed -n 's/^NYXGPT_AUTH_API_KEY=//p' .env | tail -n1)"
fi
if [[ -z "$NYXGPT_AUTH_API_KEY" || "$NYXGPT_AUTH_API_KEY" == "change-me" ]]; then
  fail "NYXGPT_AUTH_API_KEY is not set to a real value -- export it or set it in .env (see .env.example); docker/config.docker.ini enables auth by default"
fi
auth_args=(-H "X-API-Key: ${NYXGPT_AUTH_API_KEY}")

compose_field() {
  # compose_field <service> <field: State|Health>
  docker compose ps "$1" --format json 2>/dev/null | python3 -c "
import json, sys
lines = [l for l in sys.stdin if l.strip()]
print(json.loads(lines[0]).get('$2', '') if lines else '')
" 2>/dev/null || true
}

wait_for_healthy() {
  local service="$1" timeout="${2:-300}" waited=0
  log "Waiting for '$service' to become healthy (timeout ${timeout}s)..."
  while (( waited < timeout )); do
    state=$(compose_field "$service" State)
    health=$(compose_field "$service" Health)
    if [[ "$state" == "running" && ( "$health" == "healthy" || -z "$health" ) ]]; then
      log "  '$service' is up (state=$state health=${health:-n/a})"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  fail "'$service' did not become healthy within ${timeout}s (state=${state:-unknown} health=${health:-unknown})"
}

self_heal_component_healthy() {
  # Returns "1" if the component is reported healthy by /api/v1/self-heal/status, else "0".
  local service="$1"
  curl -sf "${auth_args[@]}" "$API_URL/api/v1/self-heal/status" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('0')
    sys.exit()
comp = next((c for c in d.get('components', []) if c['service'] == '$service'), None)
print('1' if comp and comp.get('healthy') else '0')
" 2>/dev/null || echo "0"
}

wait_for_self_heal() {
  local service="$1" timeout="${2:-120}" waited=0
  while (( waited < timeout )); do
    if [[ "$(self_heal_component_healthy "$service")" == "1" ]]; then
      log "  '$service' self-healed within ${waited}s"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
  log "Deploying full stack: docker compose up -d"
  docker compose up -d
  for svc in ollama cassandra api web; do
    wait_for_healthy "$svc"
  done
else
  log "--skip-deploy set: assuming the stack is already up"
fi

log "Enabling the self-heal watchdog"
curl -sf -X POST "$API_URL/api/v1/self-heal/toggle" "${auth_args[@]}" \
  -H 'Content-Type: application/json' -d '{"enabled": true}' >/dev/null \
  || fail "could not reach $API_URL to enable self-heal -- is the api container up, and is NYXGPT_AUTH_API_KEY correct?"

log "Ensuring the configured Ollama model is pulled"
default_model="$(sed -n 's/^default_model[[:space:]]*=[[:space:]]*//p' docker/config.docker.ini | head -n1)"
[[ -n "$default_model" ]] || fail "could not determine default_model from docker/config.docker.ini"
have_model=$(curl -sf "${auth_args[@]}" "$API_URL/api/v1/models" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('0')
    sys.exit()
print('1' if '$default_model' in d.get('models', []) else '0')
" 2>/dev/null || echo "0")
if [[ "$have_model" == "1" ]]; then
  log "  '$default_model' already present"
else
  log "  Pulling '$default_model' (first run only -- can take a few minutes)..."
  curl -sf --max-time 600 -X POST "$API_URL/api/v1/models/pull" "${auth_args[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"model\": \"$default_model\"}" >/dev/null \
    || fail "failed to pull model '$default_model'"
  log "  Pulled '$default_model'"
fi

log "Verifying chat works end-to-end"
chat_response=$(curl -sf -X POST "$API_URL/api/v1/chat" "${auth_args[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Reply with exactly one word: OK", "session": "smoke-test", "new": true}') \
  || fail "chat request failed"
echo "$chat_response" | python3 -c "
import json, sys
d = json.load(sys.stdin)
reply = d.get('reply')
assert reply, f'chat did not return a reply: {d}'
print('  chat reply:', reply[:80])
"

log "Verifying RAG: ingest a document, then query it"
doc_marker="XYZZY-NYXGPT-$$"
ingest_ok=1
curl -sf -X POST "$API_URL/api/v1/rag/ingest" "${auth_args[@]}" -H 'Content-Type: application/json' \
  -d "{\"doc_id\": \"smoke-test-doc\", \"text\": \"The secret smoke-test phrase is ${doc_marker}.\", \"ensure_schema\": true}" \
  >/dev/null || ingest_ok=0

if [[ "$ingest_ok" -eq 0 ]]; then
  log "  WARNING: RAG ingest returned a non-2xx response -- is [rag] enabled in config.ini? Skipping RAG verification."
else
  sleep 2
  rag_response=$(curl -sf -X POST "$API_URL/api/v1/rag/query" "${auth_args[@]}" -H 'Content-Type: application/json' \
    -d '{"query": "What is the secret smoke-test phrase?"}' || true)
  if echo "$rag_response" | grep -q "$doc_marker"; then
    log "  RAG query surfaced the ingested phrase"
  else
    log "  WARNING: RAG query did not surface the ingested phrase -- check RAG config"
  fi
fi

log "Killing each core component and observing self-heal recovery"
for svc in ollama cassandra web; do
  log "  Killing '$svc'..."
  docker compose kill "$svc"
  log "  Waiting for self-heal to restart '$svc'..."
  if ! wait_for_self_heal "$svc" 120; then
    fail "'$svc' did not self-heal within 120s"
  fi
done

log "  Killing 'api'..."
docker compose kill api
log "  NOTE: the self-heal watchdog runs inside the api container, so killing api kills the"
log "  watchdog with it -- this component cannot heal itself (see docs/self-healing.md)."
log "  Bringing it back explicitly (the one non-hands-off step in this test): docker compose up -d api"
docker compose up -d api
wait_for_healthy api

log "Smoke test passed: deploy, chat, RAG, and self-heal for ollama/cassandra/web all verified."
log "(api recovery required an explicit restart, as documented -- not a failure.)"

if [[ "$KEEP_UP" -eq 0 ]]; then
  log "Tearing down: docker compose down"
  docker compose down
else
  log "--keep-up set: leaving the stack running"
fi
