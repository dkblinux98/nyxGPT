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
# Since #3825 it also asserts every Pod was SCHEDULED before it goes on to ask
# whether chat works, on a node ballasted down to the 7936Mi a stock 8GiB
# Docker Desktop VM offers. That defect shipped a stack whose memory requests
# exceeded the node: chat worked, `install` reported success, and prometheus
# was Pending forever. A gate that installs with --skip-observability, or that
# only asks "can I chat?", passes on exactly that stack.
#
# It also runs scripts/k8s-self-heal-coverage-smoke.py against the same live
# cluster (#3828): whether self-heal names this deployment, watches all four
# core tiers plus the in-cluster observability tier rather than the api pool
# alone, and can heal a non-api Pod. That script carries its own
# fault-injection half -- it reconstructs the pre-#3828 (api-only) survey from
# the same cluster and asserts its checks fail against it.
#
# The observability layer's own behaviour (UIs answering, Grafana datasources,
# promtail shipping into Loki) is k8s-observability-smoke.yml's job and is not
# duplicated here -- what this script adds is that the layer comes up *with*
# the app tier, on one node, in the default install, with nothing left Pending.
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
# Must match k8s/configmap.yaml's `[nyxgpt] default_model` / `[rag]
# embedding_model` -- the StatefulSet pulls both and its readiness probe gates
# on both (#3824), so a mismatch here would assert on a model nothing pulls.
MODEL="${NYXGPT_SMOKE_MODEL:-qwen3:0.6b}"
EMBEDDING_MODEL="${NYXGPT_SMOKE_EMBEDDING_MODEL:-nomic-embed-text}"
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
        # Ollama's own log, which the describe does not carry: the model pulls
        # this smoke asserts on happen in the postStart hook, so when a model
        # assertion fails this is the only record of what the pull did. The
        # workflow-level "Diagnostics on failure" step cannot supply it -- the
        # cleanup below has already torn the cluster down by then.
        kubectl -n "$NAMESPACE" logs ollama-0 --tail=100 >&2 2>/dev/null || true
    fi
    if [ "${NYXGPT_SMOKE_KEEP_UP:-0}" != "1" ]; then
        nyxgpt ops down --kubernetes >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ClusterIP-only Services (see docs/kubernetes.md) -- the operator reaches the
# UI the same way, through `nyxgpt ops port-forward`. Driven here with kubectl
# directly so the script can background it and get its PID. Appends, so a
# rebuilt tunnel does not erase the previous one's error from the diagnostics.
_open_tunnel() {
    kubectl -n "$NAMESPACE" port-forward "svc/nyxgpt-web" "${WEB_PORT}:3000" \
        >>/tmp/k8s-smoke-portforward.log 2>&1 &
    PF_PID=$!
}

start_port_forward() {
    : >/tmp/k8s-smoke-portforward.log
    # Let the web Deployment report available BEFORE forwarding to it. Step 8
    # heals a web Pod for real and returns as soon as the replacement Pod
    # EXISTS, not when its server is listening, so step 9 used to open the
    # tunnel against a Pod seconds old. This is a settle, not an assertion --
    # the probe loop below is still the only thing that decides pass/fail.
    kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-web-stable \
        --timeout=180s >/dev/null 2>&1 ||
        echo "[warn] nyxgpt-web-stable is not reporting available; forwarding anyway" >&2
    _open_tunnel
    # Readiness of the TUNNEL only -- the UI's own root page, which is served
    # by the web Pod without touching the api. Probing an api-backed route
    # here would conflate "the tunnel is up" with "the backend works", and
    # the fault-injection phase below deliberately breaks the latter.
    local attempt
    for attempt in $(seq 1 30); do
        if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then return 0; fi
        # `kubectl port-forward` treats the first refused in-pod connection as
        # fatal ("error: lost connection to pod") and exits. Without this the
        # loop spends its whole 60s budget curling a process that died in the
        # first two seconds, turning a startup window into a hard failure
        # (run 32214550593, #3825). Rebuilding costs nothing and cannot mask a
        # real outage: the probe above is unchanged and still decides.
        if [ $((attempt % 3)) -eq 0 ]; then
            kill "$PF_PID" 2>/dev/null || true
            _open_tunnel
        fi
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

step "1/9 Bring the stack up: nyxgpt ops install --kubernetes --local"
# No --skip-observability: this is the command as a user types it (#3826).
# The layer that flag used to hide is also the one that did not fit the node
# (#3825), so a gate that installs less than the default cannot see either.
nyxgpt ops install --kubernetes --local --api-key "$API_KEY"
ok "install --kubernetes --local completed"

step "2/9 Every Pod of the default stack was scheduled"
# #3825: `install` reported success on a node whose memory was 99% reserved,
# with prometheus left Pending / FailedScheduling for good. Nothing in the
# steps below would have noticed -- chat worked fine. An unscheduled Pod has
# an empty .spec.nodeName, which is what this checks; "Pending" on its own is
# also what a Pod that IS scheduled and pulling its image looks like.
#
# Checked here, before the rollout waits below, so an unschedulable Pod reads
# as its own failure rather than as one of those waits timing out. Report the
# arithmetic either way (#3826), so a future footprint increase shows up as a
# number in the log rather than as a mysterious timeout.
echo "--- node allocatable ---"
kubectl get nodes -o custom-columns=\
'NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'
echo "--- requests by Pod ---"
kubectl -n "$NAMESPACE" get pods -o custom-columns=\
'NAME:.metadata.name,PHASE:.status.phase,REQ_MEM:.spec.containers[*].resources.requests.memory'
unscheduled=$("${SCRIPT_DIR}/k8s-unscheduled-pods.sh" "$NAMESPACE")
if [ -n "$unscheduled" ]; then
    kubectl -n "$NAMESPACE" get pods -o wide >&2
    kubectl -n "$NAMESPACE" get events --field-selector reason=FailedScheduling | tail -20 >&2
    fail "these Pods could not be scheduled: $(echo "$unscheduled" | tr '\n' ' ')-- the node \
cannot fit the default stack (size the cluster VM, do not drop observability)"
fi
ok "every Pod in the default stack has a node"

step "3/9 The data/LLM tier exists and is Ready"
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
# Read the store ONCE and match against the captured text. Piping `kubectl
# exec` straight into `grep -q` is a race under `set -o pipefail`: grep exits
# on its first match, kubectl takes EPIPE writing the rows it has not streamed
# yet, and a *successful* match becomes a failed pipeline. `ollama list` is
# newest-first, so the embedding model (pulled last) is the first data row and
# lost that race deterministically; the chat model, last in the list, never
# did. One capture removes the race for both -- and halves the execs.
OLLAMA_MODELS=$(kubectl -n "$NAMESPACE" exec ollama-0 -- ollama list)
grep -q "$MODEL" <<<"$OLLAMA_MODELS" ||
    fail "Ollama is Ready but the default model ${MODEL} was never pulled -- chat would 404"
ok "default model ${MODEL} present in the in-cluster Ollama"
# The embedding model too (#3824): RAG is a per-session toggle, so a user can
# turn it on at any moment, and a Ready Ollama without it would stall that
# first RAG-enabled message on a ~275 MB download inside the request.
grep -q "$EMBEDDING_MODEL" <<<"$OLLAMA_MODELS" ||
    fail "Ollama is Ready but the embedding model ${EMBEDDING_MODEL} was never pulled -- \
the first RAG-enabled message would block on downloading it"
ok "embedding model ${EMBEDDING_MODEL} present in the in-cluster Ollama"
# The readiness probe in k8s/statefulset-ollama.yaml gates on both models, so
# the rollout-status wait above only returned because both were there -- this
# assertion names which model, so a probe regression fails with the reason.

step "4/9 The observability layer came up with the app tier"
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

# Nothing is left Pending once every rollout above has landed either -- a Pod
# that was scheduled but never became Ready fails its own rollout wait, and
# step 2 already ruled out the unschedulable case with the node arithmetic
# printed alongside it (#3826, #3825).

step "5/9 The user path works: sessions list, via the web Service"
start_port_forward
curl -fsS "${BASE}/api/sessions" >/dev/null ||
    fail "GET /api/sessions failed -- this is the UI's 'Failed to load sessions'"
ok "session list loads through the web UI's own proxy route"

step "6/9 A real chat round-trip"
curl -fsS -X POST "${BASE}/api/sessions/init" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${SESSION}\"}" >/dev/null || fail "could not create a chat session"
chat_round_trip "$SESSION" || fail "chat round-trip produced no answer -- no chat is possible"
ok "chat answered through web -> api -> in-cluster Ollama"

step "7/9 Sessions are shared by every api replica (Cassandra-backed)"
# With the file backend each api replica keeps its own session list, so
# consecutive requests from one browser see different sessions; the poll below
# runs enough times to land on every replica. The stable Deployment rests at 1
# replica since #3833, so the poll no longer spreads across a standing pool by
# itself -- scale up for the duration of this check, exactly as a canary
# rollout would, so the assertion still has more than one replica to disagree.
# Two extra api Pods is 200m/512Mi against the ballasted node (#3825), which
# step 2 has already shown has room for a rollout's worth of borrowing.
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

step "8/9 Self-heal sees the whole cluster, not just the api pool (#3828)"
# Deletes a web Pod for real (the heal action), which is why it runs after the
# user-path steps and why the tunnel is dropped first -- step 9 reopens it.
stop_port_forward
python3 scripts/k8s-self-heal-coverage-smoke.py ||
    fail "self-heal does not cover this deployment -- see the output above (#3828)"
ok "self-heal names the mode, watches every tier, and heals a non-api Pod"

step "9/9 Fault injection: the pre-#3786 topology must FAIL this same check"
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
