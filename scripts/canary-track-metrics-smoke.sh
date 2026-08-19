#!/usr/bin/env bash
# Executed verification for the canary rollout gate's metric attribution
# (#3829, #3775).
#
# The question this answers: when `evaluate()` runs inside an nyxgpt-api Pod
# -- which is where the dashboard and the API endpoint run it -- does its
# verdict describe the CANARY track, or the Pod that happens to be serving the
# request?
#
# #3829 was an acceptance failure where `nyxgpt-api-canary` was 0/1, had zero
# Service endpoints and had served zero requests, and the Canary page still
# reported "Requests 459 / Error rate 0.00% / p95 6ms" and evaluate answered
# "safe to promote". The numbers were real -- they were the *stable* Pod's.
# No amount of manifest or code review sees that: the two readings are the
# same call, and only a cluster with two tracks in different states can tell
# them apart.
#
# Two halves, per the fault-injection rule (CLAUDE.md, #3753):
#
#   1. FIXED BEHAVIOUR -- with the canary at 0 replicas and stable driven
#      well past `min_requests`, evaluate must refuse to say "safe to
#      promote", promote must refuse a canary that has served nothing, and
#      once the canary itself is given traffic the verdict must reflect the
#      CANARY's request count, read from a *different* Pod than the one
#      answering the call.
#   2. PRE-FIX INPUT -- the same stable Pod's own /api/v1/metrics is asserted
#      to still report >= min_requests at a 0% error rate. That is exactly
#      the snapshot the pre-fix gate consumed, so it is proof that the input
#      which produced "safe to promote" is still present and still says
#      "healthy and busy" -- the verdict changed because the gate stopped
#      reading it, not because this cluster is quiet.
#
# It also proves, on a real cluster, that kubelet /health probes and /metrics
# scrapes are excluded from the judged traffic: by the time the canary Pod is
# Ready it has been probed several times, and the gate must still report 0
# canary-track requests.
#
# Deliberately NOT a full `nyxgpt ops install --kubernetes`: no Cassandra, no
# Ollama, no observability. Those are k8s-local-smoke.yml's subject, cost tens
# of minutes, and none of them participate in the attribution question. This
# brings up the two api Deployments the canary tool operates on, and nothing
# else.
#
# Prerequisites: docker, kind, kubectl, and a `nyxgpt` on PATH.
set -euo pipefail

CLUSTER="${NYXGPT_SMOKE_CLUSTER:-nyxgpt-canary-smoke}"
NAMESPACE="nyxgpt"
IMAGE="nyxgpt-api:canary-smoke"
API_KEY="${NYXGPT_SMOKE_API_KEY:-canary-smoke-key}"
MIN_REQUESTS=20
STABLE_REQUESTS=40
CANARY_REQUESTS=25
PORT="${NYXGPT_SMOKE_PORT:-18000}"
BASE="http://127.0.0.1:${PORT}"
PF_PID=""

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
    stop_port_forward
    if [ "$rc" -ne 0 ]; then
        echo "--- diagnostics ---" >&2
        kubectl -n "$NAMESPACE" get pods -o wide >&2 2>/dev/null || true
        kubectl -n "$NAMESPACE" describe pods >&2 2>/dev/null | tail -60 || true
    fi
    if [ "${NYXGPT_SMOKE_KEEP_UP:-0}" != "1" ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

stop_port_forward() {
    if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; PF_PID=""; fi
    # kubectl port-forward exits asynchronously; give the socket up before the
    # next forward binds the same local port.
    sleep 1
}

# Forwards to ONE named Pod, never to the Service: the whole point is which
# Pod a request reaches, and a Service forward would round-robin the tracks.
start_port_forward() {
    local pod="$1"
    kubectl -n "$NAMESPACE" port-forward "pod/${pod}" "${PORT}:8000" \
        >/tmp/canary-smoke-portforward.log 2>&1 &
    PF_PID=$!
    for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null "${BASE}/health" 2>/dev/null; then return 0; fi
        sleep 1
    done
    cat /tmp/canary-smoke-portforward.log >&2 || true
    fail "pod/${pod} never answered on ${BASE}"
}

# The first Ready, non-terminating Pod on the track -- the same selection
# canary.py makes. Taking `.items[0]` instead picks up Pods draining from a
# previous ReplicaSet, whose container is already refusing connections.
pod_for_track() {
    kubectl -n "$NAMESPACE" get pods -l "app=nyxgpt-api-canary-pool,track=$1" -o json | python -c '
import json, sys
for item in json.load(sys.stdin)["items"]:
    if item["metadata"].get("deletionTimestamp"):
        continue
    conditions = item.get("status", {}).get("conditions", []) or []
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
        print(item["metadata"]["name"])
        break
'
}

# Drives N requests at whatever the port-forward points to. /docs is
# unauthenticated and is NOT one of the paths the gate excludes, so these are
# "real" requests in exactly the sense the canary gate counts.
drive_requests() {
    local count="$1" i
    for i in $(seq 1 "$count"); do
        curl -fsS -o /dev/null "${BASE}/docs" || fail "request ${i}/${count} failed"
    done
}

evaluate_from_pod() {
    curl -sS -X POST "${BASE}/api/v1/canary/evaluate" \
        -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' -d '{}'
}

promote_from_pod() {
    curl -sS -X POST "${BASE}/api/v1/canary/promote" \
        -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' -d '{}'
}

step "1/8 Bring up a kind cluster with the two api tracks (no data/LLM/observability tier)"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER"
docker build -t "$IMAGE" .
kind load docker-image "$IMAGE" --name "$CLUSTER"

kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl -n "$NAMESPACE" create secret generic nyxgpt-secrets \
    --from-literal=api-key="$API_KEY" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/service.yaml -f k8s/service-canary.yaml
kubectl apply -f k8s/deployment-stable.yaml -f k8s/deployment-canary.yaml

# Image, pull policy and replica count in ONE patch each: separate edits mean
# separate rollouts, which leaves Pods from superseded ReplicaSets draining
# alongside the live ones. One stable replica is enough to answer requests,
# and the canary stays at 0 -- precisely the topology #3829 was reported
# against.
kubectl -n "$NAMESPACE" patch deployment/nyxgpt-api-stable -p "{\"spec\":{\"replicas\":1,\
\"template\":{\"spec\":{\"containers\":[{\"name\":\"nyxgpt-api\",\"image\":\"${IMAGE}\",\
\"imagePullPolicy\":\"Never\"}]}}}}"
kubectl -n "$NAMESPACE" patch deployment/nyxgpt-api-canary -p "{\"spec\":{\
\"template\":{\"spec\":{\"containers\":[{\"name\":\"nyxgpt-api\",\"image\":\"${IMAGE}\",\
\"imagePullPolicy\":\"Never\"}]}}}}"
kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-api-stable --timeout=300s
# The superseded ReplicaSet's Pods must be gone before anything is measured:
# while they drain they are still Running, and a track's metrics must not
# include Pods on their way out (canary.py skips them by deletionTimestamp).
for _ in $(seq 1 60); do
    running="$(kubectl -n "$NAMESPACE" get pods -l track=stable -o name | wc -l | tr -d ' ')"
    [ "$running" = "1" ] && break
    sleep 2
done
STABLE_POD="$(pod_for_track stable)"
[ -n "$STABLE_POD" ] || fail "no stable Pod came up"
ok "stable Pod ${STABLE_POD} Ready; canary at 0 replicas"

step "2/8 Reproduce the reported topology: canary 0/1 with zero endpoints"
kubectl -n "$NAMESPACE" get endpoints nyxgpt-api -o wide
canary_pods="$(kubectl -n "$NAMESPACE" get pods -l 'track=canary' -o name | wc -l | tr -d ' ')"
[ "$canary_pods" = "0" ] || fail "expected zero canary Pods, found ${canary_pods}"
# The rollout is marked active in the serving Pod's own state file, the same
# state `nyxgpt canary start` writes -- without it evaluate short-circuits on
# "no rollout in progress" and proves nothing.
kubectl -n "$NAMESPACE" exec "$STABLE_POD" -- python -c "
import json, pathlib
path = pathlib.Path.home() / '.nyxGPT' / 'canary_state.json'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({'active': True, 'weight_percent': 10, 'total_replicas': 4, 'history': []}))
print('canary state:', path.read_text())
"
ok "canary track has no Pods and no endpoints, with a rollout marked active"

step "3/8 Make the stable Pod busy -- ${STABLE_REQUESTS} requests, well past min_requests=${MIN_REQUESTS}"
start_port_forward "$STABLE_POD"
drive_requests "$STABLE_REQUESTS"
ok "${STABLE_REQUESTS} requests served by ${STABLE_POD}"

step "4/8 PRE-FIX INPUT: the serving Pod still reports a busy, error-free process"
legacy="$(curl -fsS "${BASE}/api/v1/metrics" -H "X-API-Key: ${API_KEY}")"
echo "$legacy"
legacy_requests="$(printf '%s' "$legacy" | python -c 'import json,sys; print(json.load(sys.stdin)["queue"]["total_requests"])')"
legacy_errors="$(printf '%s' "$legacy" | python -c 'import json,sys; print(json.load(sys.stdin)["errors"]["rate_percent"])')"
[ "$legacy_requests" -ge "$MIN_REQUESTS" ] ||
    fail "the serving process reports only ${legacy_requests} requests -- this run cannot \
distinguish the fix from a quiet cluster; the pre-fix gate would have held on insufficient data"
ok "the pre-fix input is intact: ${legacy_requests} requests at ${legacy_errors}% errors in the \
serving process. That snapshot is what produced #3829's \"safe to promote\" for a canary with \
no Pods -- the verdict below must change anyway"

step "5/8 FIXED: evaluate refuses to green-light a canary with no Pods"
verdict="$(evaluate_from_pod)"
echo "$verdict"
case "$verdict" in
    *"safe to promote"*)
        fail "evaluate still answered \"safe to promote\" for a canary with zero Pods -- \
it is reading the serving Pod's counters (#3829)" ;;
esac
case "$verdict" in
    *"no ready Pods"*) ;;
    *) fail "evaluate held, but not for the honest reason; expected the canary track's \
\"no ready Pods\", got: ${verdict}" ;;
esac
ok "evaluate reports the canary track has no ready Pods, on the same Pod that reports \
${legacy_requests} of its own requests"

step "6/8 FIXED: promote refuses a canary track that has served no traffic"
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-canary --replicas=1
kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-api-canary --timeout=300s
CANARY_POD="$(pod_for_track canary)"
[ -n "$CANARY_POD" ] || fail "no canary Pod came up"
# The canary Pod is now Ready, which means the kubelet has already probed
# /health several times and this gate has scraped its /metrics. Neither is
# canary traffic, and the count below must say so.
verdict="$(evaluate_from_pod)"
echo "$verdict"
case "$verdict" in
    *"0/${MIN_REQUESTS} canary-track requests"*) ;;
    *) fail "expected 0 canary-track requests for a freshly scheduled canary -- health probes \
or metrics scrapes are being counted as traffic: ${verdict}" ;;
esac
refusal="$(promote_from_pod)"
echo "$refusal"
case "$refusal" in
    *"served no traffic"*) ;;
    *) fail "promote did not refuse a canary that has served nothing: ${refusal}" ;;
esac
ok "probes and scrapes are excluded (0 canary-track requests on a Ready Pod), and promote refuses"

step "7/8 FIXED: with the canary given traffic, the verdict describes the CANARY"
stop_port_forward
start_port_forward "$CANARY_POD"
drive_requests "$CANARY_REQUESTS"
stop_port_forward
# Back to the STABLE Pod: evaluate is answered by a process whose own counters
# now stand at hundreds, about a track it never served. That cross-Pod reading
# is the whole fix.
start_port_forward "$STABLE_POD"
verdict="$(evaluate_from_pod)"
echo "$verdict"
case "$verdict" in
    *"safe to promote"*) ;;
    *) fail "evaluate did not pass a healthy canary that served ${CANARY_REQUESTS} requests: \
${verdict}" ;;
esac
case "$verdict" in
    *"${CANARY_REQUESTS} requests"*) ;;
    *) fail "evaluate passed, but on the wrong count -- expected the canary's \
${CANARY_REQUESTS} requests, got: ${verdict}" ;;
esac
ok "evaluate, served by ${STABLE_POD}, judged ${CANARY_POD}'s ${CANARY_REQUESTS} requests"

step "8/8 The CLI reports the same per-track split"
# Printed so the per-track Pod counts in the status output below can be read
# against the Pods that actually exist, rather than taken on trust.
kubectl -n "$NAMESPACE" get pods -l app=nyxgpt-api-canary-pool \
    -o custom-columns='NAME:.metadata.name,TRACK:.metadata.labels.track,PHASE:.status.phase,DELETING:.metadata.deletionTimestamp'
kubectl -n "$NAMESPACE" exec "$STABLE_POD" -- nyxgpt canary status | tee /tmp/canary-smoke-status.log
grep -q "canary-track metrics: ${CANARY_REQUESTS} requests" /tmp/canary-smoke-status.log ||
    fail "nyxgpt canary status did not report the canary track's own request count"
ok "nyxgpt canary status reports canary- and stable-track metrics separately"

echo
echo "[PASS] canary evaluation is attributed to the canary track (#3829)"
