#!/usr/bin/env bash
# Executed verification for the local Kubernetes deployment (#3786, #3775).
#
# The question this answers: after `nyxgpt ops install --kubernetes --local`,
# can a user actually chat? Not "are the Pods Running" -- #3786 was filed
# against a stack where 4/4 api and 4/4 web Pods ran and the web UI still
# showed "Failed to load sessions" and could not answer a single message,
# because the deployment had no data tier and no LLM tier at all. Inspection
# cannot see that; only running it can.
#
# Two halves, deliberately, per the fault-injection rule (CLAUDE.md, #3753):
#
#   1. FIXED TOPOLOGY  -- install, then exercise the real user path (the web
#      Service's own proxy routes, the same ones the browser calls): list
#      sessions, create one, chat, assert an answer came back, and assert the
#      session is in Cassandra rather than on one pod's filesystem.
#   2. PRE-FIX TOPOLOGY -- delete the in-cluster Cassandra and Ollama, which
#      reproduces exactly the deployment #3786 reported, and assert the SAME
#      chat request now fails. Without this half the job would pass on a
#      build that never shipped the data tier, which is how a green CI run
#      and a broken stack coexist.
#
# Since #3825 this runs the DEFAULT install -- observability included -- and
# asserts every Pod was scheduled before it goes on to ask whether chat works.
# That defect shipped a stack whose memory requests exceeded the node: chat
# worked, `install` reported success, and prometheus was Pending forever. A
# gate that installs with --skip-observability, or that only asks "can I
# chat?", passes on exactly that stack.
#
# Prerequisites: Docker, and a `nyxgpt` on PATH (`pip install -e .`). kubectl
# and kind are installed by `nyxgpt ops install --kubernetes --local` itself
# when missing (#3724), so this script does not install them. To reproduce the
# capacity claim on a machine larger than a stock 8GiB Docker Desktop VM,
# create the cluster first and run `scripts/k8s-node-ballast.sh` against it --
# which is what .github/workflows/k8s-local-smoke.yml does.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="nyxgpt"
API_KEY="${NYXGPT_SMOKE_API_KEY:-k8s-smoke-key}"
WEB_PORT="${NYXGPT_SMOKE_WEB_PORT:-3000}"
SESSION="k8s-smoke-$$"
MODEL="${NYXGPT_SMOKE_MODEL:-qwen2.5:0.5b}"
BASE="http://127.0.0.1:${WEB_PORT}"
PF_PID=""

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
    if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; fi
    if [ "$rc" -ne 0 ]; then
        echo "--- diagnostics ---" >&2
        kubectl -n "$NAMESPACE" get pods -o wide >&2 2>/dev/null || true
        kubectl -n "$NAMESPACE" describe pods >&2 2>/dev/null | tail -80 || true
    fi
    if [ "${NYXGPT_SMOKE_KEEP_UP:-0}" != "1" ]; then
        nyxgpt ops down --kubernetes >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

start_port_forward() {
    # ClusterIP-only Services (see docs/kubernetes.md) -- the operator reaches
    # the UI the same way, through `nyxgpt ops port-forward`. Driven here with
    # kubectl directly so the script can background it and get its PID.
    kubectl -n "$NAMESPACE" port-forward "svc/nyxgpt-web" "${WEB_PORT}:3000" \
        >/tmp/k8s-smoke-portforward.log 2>&1 &
    PF_PID=$!
    # Readiness of the TUNNEL only -- the UI's own root page, which is served
    # by the web Pod without touching the api. Probing an api-backed route
    # here would conflate "the tunnel is up" with "the backend works", and
    # the fault-injection phase below deliberately breaks the latter.
    for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then return 0; fi
        sleep 2
    done
    cat /tmp/k8s-smoke-portforward.log >&2 || true
    fail "web Service never answered on ${BASE} -- the UI itself is unreachable"
}

stop_port_forward() {
    if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; PF_PID=""; fi
}

# Runs one chat round-trip through the web proxy. Prints the SSE stream;
# returns non-zero if the request failed or no assistant text came back.
chat_round_trip() {
    local session="$1" out
    out=$(curl -sS -N -X POST "${BASE}/api/chat/stream" \
        -H 'Content-Type: application/json' \
        -d "{\"session\":\"${session}\",\"prompt\":\"Reply with exactly: PONG\",\"model\":\"${MODEL}\"}" \
        --max-time "${NYXGPT_SMOKE_CHAT_TIMEOUT:-300}" 2>&1) || return 1
    echo "$out"
    echo "$out" | grep -q '"content"' || return 1
}

step "1/7 Bring the stack up: nyxgpt ops install --kubernetes --local"
# The DEFAULT install, observability included -- no --skip-observability
# (#3825). The layer that flag used to hide is the one that did not fit the
# node, and a gate that installs less than the default cannot see that.
nyxgpt ops install --kubernetes --local --api-key "$API_KEY"
ok "install --kubernetes --local completed"

step "2/7 Every Pod of the default stack was scheduled"
# #3825: `install` reported success on a node whose memory was 99% reserved,
# with prometheus left Pending / FailedScheduling for good. Nothing in the
# steps below would have noticed -- chat worked fine. An unscheduled Pod has
# an empty .spec.nodeName, which is what this checks; "Pending" on its own is
# also what a Pod that IS scheduled and pulling its image looks like.
unscheduled=$("${SCRIPT_DIR}/k8s-unscheduled-pods.sh" "$NAMESPACE")
if [ -n "$unscheduled" ]; then
    kubectl -n "$NAMESPACE" get pods -o wide >&2
    kubectl -n "$NAMESPACE" get events --field-selector reason=FailedScheduling >&2 | tail -20
    fail "these Pods could not be scheduled: $(echo "$unscheduled" | tr '\n' ' ')"
fi
ok "every Pod in the default stack has a node"
for workload in prometheus grafana loki jaeger glitchtip; do
    kubectl -n "$NAMESPACE" get "deploy/${workload}" >/dev/null 2>&1 ||
        fail "no ${workload} Deployment -- the default install shipped no observability layer"
done
ok "the observability layer is deployed, prometheus included"

step "3/7 The data/LLM tier exists and is Ready"
# `install` already waits for these (ops._wait_for_k8s_data_tier); asserting
# again here is what makes the *absence* of the tier a test failure rather
# than a silently degraded stack.
for workload in cassandra ollama; do
    kubectl -n "$NAMESPACE" get "statefulset/${workload}" >/dev/null 2>&1 ||
        fail "no ${workload} StatefulSet in the deployment -- this is the #3786 regression"
    kubectl -n "$NAMESPACE" rollout status "statefulset/${workload}" --timeout=900s ||
        fail "${workload} never became Ready"
    ok "${workload} StatefulSet Ready"
done
kubectl -n "$NAMESPACE" exec ollama-0 -- ollama list | grep -q "$MODEL" ||
    fail "Ollama is Ready but the default model ${MODEL} was never pulled -- chat would 404"
ok "default model ${MODEL} present in the in-cluster Ollama"

step "4/7 The user path works: sessions list, via the web Service"
start_port_forward
curl -fsS "${BASE}/api/sessions" >/dev/null ||
    fail "GET /api/sessions failed -- this is the UI's 'Failed to load sessions'"
ok "session list loads through the web UI's own proxy route"

step "5/7 A real chat round-trip"
curl -fsS -X POST "${BASE}/api/sessions/init" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${SESSION}\"}" >/dev/null || fail "could not create a chat session"
chat_round_trip "$SESSION" || fail "chat round-trip produced no answer -- no chat is possible"
ok "chat answered through web -> api -> in-cluster Ollama"

step "6/7 Sessions are shared by every api replica (Cassandra-backed)"
# With the file backend each of the 4 api replicas keeps its own session list,
# so consecutive requests from one browser see different sessions. Poll enough
# times to land on every replica.
for _ in $(seq 1 12); do
    curl -fsS "${BASE}/api/sessions" | grep -q "$SESSION" ||
        fail "session ${SESSION} missing from a replica's session list -- sessions are not shared"
done
kubectl -n "$NAMESPACE" exec cassandra-0 -- \
    cqlsh -e "SELECT name FROM ${NAMESPACE}.chat_sessions;" | grep -q "$SESSION" ||
    fail "session ${SESSION} is not in Cassandra -- the session store is not the shared one"
ok "session is stored in the in-cluster Cassandra and visible from every replica"

step "7/7 Fault injection: the pre-#3786 topology must FAIL this same check"
stop_port_forward
kubectl -n "$NAMESPACE" delete statefulset cassandra ollama --wait=true >/dev/null
kubectl -n "$NAMESPACE" wait --for=delete pod/ollama-0 --timeout=180s >/dev/null 2>&1 || true
start_port_forward
if curl -fsS -o /dev/null "${BASE}/api/sessions" 2>/dev/null; then
    fail "the session list still loaded with no Cassandra in the cluster -- \
step 4 cannot detect the #3786 regression"
fi
ok "without Cassandra the session list fails (the UI's 'Failed to load sessions')"
if chat_round_trip "${SESSION}-nofix" >/tmp/k8s-smoke-nofix.log 2>&1; then
    cat /tmp/k8s-smoke-nofix.log >&2
    fail "chat still answered with no Ollama and no Cassandra in the cluster -- \
step 5 cannot detect the #3786 regression and is worthless as a gate"
fi
ok "without the data/LLM tier the chat round-trip fails, as it must"

echo
echo "[PASS] k8s --local deploys a stack that can actually chat (#3786)"
