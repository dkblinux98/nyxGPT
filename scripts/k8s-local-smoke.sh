#!/usr/bin/env bash
# Executed verification for the local Kubernetes deployment (#3786, #3775).
#
# The question this answers: after `nyxgpt ops install --kubernetes --local`,
# can a user actually chat? Not "are the Pods Running" -- #3786 was filed
# against a stack where every api and web Pod ran and the web UI still
# showed "Failed to load sessions" and could not answer a single message,
# because the deployment had no data tier and no LLM tier at all. Inspection
# cannot see that; only running it can.
#
# THE COMMAND UNDER TEST IS THE DEFAULT ONE (#3826). This script used to pass
# `--skip-observability`, so it exercised a configuration no user runs: the
# real command brings the in-cluster observability layer up with the app tier,
# which is ~2.4Gi more of requests and ten more Pods competing for the node.
# A smoke that opts out cannot answer "does the install a user actually types
# work", and it structurally could not supply #3787's executed evidence,
# because it excluded the very layer #3787 added. If a reduced-footprint run
# is ever wanted, it belongs in an ADDITIONAL job, never as a replacement for
# this one.
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
# The observability layer's own behaviour (UIs answering, Grafana datasources,
# promtail shipping into Loki) is k8s-observability-smoke.yml's job and is not
# duplicated here -- what this script adds is that the layer comes up *with*
# the app tier, on one node, in the default install, with nothing left Pending.
#
# Prerequisites: Docker, and a `nyxgpt` on PATH (`pip install -e .`). kubectl
# and kind are installed by `nyxgpt ops install --kubernetes --local` itself
# when missing (#3724), so this script does not install them.
set -euo pipefail

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

step "1/8 Bring the stack up: nyxgpt ops install --kubernetes --local"
# No --skip-observability: this is the command as a user types it (#3826).
nyxgpt ops install --kubernetes --local --api-key "$API_KEY"
ok "install --kubernetes --local completed"

step "2/8 The data/LLM tier exists and is Ready"
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

step "3/8 The observability layer came up with the app tier"
# Every workload k8s/observability/ ships, prometheus first: it is the one the
# SRE dashboard's metrics tiles and every Grafana panel read from, and it is
# the workload #3787 found missing. `install` already waits for these
# (ops._wait_for_k8s_observability) -- asserting again here is what makes a
# regression to an app-tier-only install a test failure rather than a quietly
# blind stack.
for deploy in prometheus grafana loki otel-collector jaeger \
              glitchtip-postgres glitchtip-redis glitchtip glitchtip-worker; do
    kubectl -n "$NAMESPACE" get "deploy/${deploy}" >/dev/null 2>&1 ||
        fail "no ${deploy} Deployment in the default install -- observability is absent (#3787)"
    kubectl -n "$NAMESPACE" rollout status "deploy/${deploy}" --timeout=600s ||
        fail "${deploy} never became Ready in the default install"
done
kubectl -n "$NAMESPACE" rollout status ds/promtail --timeout=300s ||
    fail "promtail never became Ready in the default install"
ok "all ten observability workloads are Ready alongside the app tier"

step "4/8 Nothing is left Pending -- the whole default stack fits on the node"
# The failure mode this exists for: the node cannot fit the default stack's
# requests, so Pods sit Pending forever and every other assertion below either
# hangs or passes on a partial stack. Report the arithmetic either way, so a
# future footprint increase shows up as a number in the log rather than as a
# mysterious timeout.
echo "--- node allocatable ---"
kubectl get nodes -o custom-columns=\
'NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'
echo "--- requests by Pod ---"
kubectl -n "$NAMESPACE" get pods -o custom-columns=\
'NAME:.metadata.name,PHASE:.status.phase,REQ_MEM:.spec.containers[*].resources.requests.memory'
pending=$(kubectl -n "$NAMESPACE" get pods \
    --field-selector=status.phase=Pending -o name 2>/dev/null | tr '\n' ' ')
if [ -n "${pending// /}" ]; then
    kubectl -n "$NAMESPACE" describe pods --field-selector=status.phase=Pending | tail -60 >&2
    fail "Pods still Pending after the default install: ${pending}-- the node cannot fit the \
default stack (size the runner or the kind node, do not drop observability)"
fi
ok "no Pending Pods: the default stack (app + data/LLM + observability) fits"

step "5/8 The user path works: sessions list, via the web Service"
start_port_forward
curl -fsS "${BASE}/api/sessions" >/dev/null ||
    fail "GET /api/sessions failed -- this is the UI's 'Failed to load sessions'"
ok "session list loads through the web UI's own proxy route"

step "6/8 A real chat round-trip"
curl -fsS -X POST "${BASE}/api/sessions/init" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${SESSION}\"}" >/dev/null || fail "could not create a chat session"
chat_round_trip "$SESSION" || fail "chat round-trip produced no answer -- no chat is possible"
ok "chat answered through web -> api -> in-cluster Ollama"

step "7/8 Sessions are shared by every api replica (Cassandra-backed)"
# With the file backend each api replica keeps its own session list, so
# consecutive requests from one browser see different sessions. The stable
# Deployment rests at 1 replica since #3833, so the poll below no longer
# spreads across a standing pool by itself -- scale up for the duration of
# this check, exactly as a canary rollout would, so the assertion still has
# more than one replica to disagree.
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-stable --replicas=3 >/dev/null
kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-api-stable --timeout=300s >/dev/null ||
    fail "nyxgpt-api-stable did not reach 3 replicas for the shared-session check"
for _ in $(seq 1 12); do
    curl -fsS "${BASE}/api/sessions" | grep -q "$SESSION" ||
        fail "session ${SESSION} missing from a replica's session list -- sessions are not shared"
done
kubectl -n "$NAMESPACE" exec cassandra-0 -- \
    cqlsh -e "SELECT name FROM ${NAMESPACE}.chat_sessions;" | grep -q "$SESSION" ||
    fail "session ${SESSION} is not in Cassandra -- the session store is not the shared one"
ok "session is stored in the in-cluster Cassandra and visible from every replica"
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-stable --replicas=1 >/dev/null

step "8/8 Fault injection: the pre-#3786 topology must FAIL this same check"
stop_port_forward
kubectl -n "$NAMESPACE" delete statefulset cassandra ollama --wait=true >/dev/null
kubectl -n "$NAMESPACE" wait --for=delete pod/ollama-0 --timeout=180s >/dev/null 2>&1 || true
start_port_forward
if curl -fsS -o /dev/null "${BASE}/api/sessions" 2>/dev/null; then
    fail "the session list still loaded with no Cassandra in the cluster -- \
step 5 cannot detect the #3786 regression"
fi
ok "without Cassandra the session list fails (the UI's 'Failed to load sessions')"
if chat_round_trip "${SESSION}-nofix" >/tmp/k8s-smoke-nofix.log 2>&1; then
    cat /tmp/k8s-smoke-nofix.log >&2
    fail "chat still answered with no Ollama and no Cassandra in the cluster -- \
step 6 cannot detect the #3786 regression and is worthless as a gate"
fi
ok "without the data/LLM tier the chat round-trip fails, as it must"

echo
echo "[PASS] k8s --local deploys a stack that can actually chat (#3786)"
