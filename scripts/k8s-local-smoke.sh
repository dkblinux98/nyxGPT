#!/usr/bin/env bash
# Executed verification for the local Kubernetes deployment (#3786, #3775).
#
# The question this answers: after `nyxgpt ops install --kubernetes`,
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
# and kind are installed by `nyxgpt ops install --kubernetes` itself
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
# Read, never restated. These used to be literals with a comment saying "must
# match k8s/configmap.yaml" -- and they diverged anyway when the shipped model
# moved and only some sites followed (owner, 2026-08-23). Asking the same
# `get_default_model` the install itself reads means this smoke cannot assert
# on a model nothing pulled. The env overrides stay, for driving the smoke at
# a deliberately different model.
_shipped_models() {
    python3 - <<'PYEOF' 2>/dev/null
from nyxgpt.config import get_default_model, load_config
cfg = load_config()
chat = get_default_model(cfg)
emb = cfg.get("rag", "embedding_model", fallback="").strip() or chat
print(f"{chat}\t{emb}")
PYEOF
}
IFS=$'\t' read -r _SHIPPED_CHAT _SHIPPED_EMB <<<"$(_shipped_models)"
[ -n "${_SHIPPED_CHAT:-}" ] || {
    echo "[FAIL] could not read the shipped chat model from nyxgpt.config -- the smoke" >&2
    echo "[FAIL] refuses to fall back to a literal, which is the defect it guards." >&2
    exit 1
}
MODEL="${NYXGPT_SMOKE_MODEL:-$_SHIPPED_CHAT}"
EMBEDDING_MODEL="${NYXGPT_SMOKE_EMBEDDING_MODEL:-$_SHIPPED_EMB}"
BASE="http://127.0.0.1:${WEB_PORT}"

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
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

# NO TUNNEL OF ITS OWN (#3986). This script used to background a
# `kubectl port-forward svc/nyxgpt-web 3000:3000` here, which quietly made it
# blind to the defect #3986 reports: the smoke reached the UI because IT had
# opened a forward, while a real operator finishing the same install had
# nothing listening on the host at all. Every step below now drives
# `http://127.0.0.1:3000` exactly as a browser would, and reaching it is an
# assertion rather than a setup step.
#
# The install is what establishes that address -- a NodePort published by the
# provisioned kind cluster's `extraPortMappings`, or a managed background
# forward on a cluster whose host ports nyxGPT cannot map. Either way nothing
# here opens one, and `nyxgpt ops down --kubernetes` releases whatever it was.
wait_for_web() {
    # The UI's own root page, served by the web Pod without touching the api:
    # probing an api-backed route here would conflate "the address answers"
    # with "the backend works", and the fault-injection phase below
    # deliberately breaks the latter.
    local _attempt
    for _attempt in $(seq 1 45); do
        if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then return 0; fi
        sleep 2
    done
    kubectl -n "$NAMESPACE" get svc nyxgpt-web -o wide >&2 || true
    nyxgpt ops port-forward --status >&2 || true
    fail "web UI never answered on ${BASE} with no port-forward running -- \
the install did not leave a reachable UI (#3986)"
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

step "1/17 Bring the stack up: nyxgpt ops install --kubernetes"
# No --skip-observability: this is the command as a user types it (#3826).
# The layer that flag used to hide is also the one that did not fit the node
# (#3825), so a gate that installs less than the default cannot see either.
#
# And no --local either (#3948): local is the default locality now, and this
# is the executed evidence for it -- a real deploy driven by the command with
# no locality flag at all. `--local` stays accepted as a no-op, which
# k8s-artifact-smoke.sh still passes.
nyxgpt ops install --kubernetes --api-key "$API_KEY"
ok "install --kubernetes completed with no locality flag"

step "2/17 Every Pod of the default stack was scheduled"
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

step "3/17 The data/LLM tier exists and is Ready"
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

step "4/17 ops status/doctor report on THIS deployment, not the host (#3987)"
# Executed evidence for #3987 (#3775). The defect it fixes is invisible to
# inspection and to unit tests, because it is about which machine the command
# asks: on the owner's acceptance run `ops status` reported the two models
# above -- the ones step 3 has just proved are in the cluster -- as `UNKNOWN
# (Ollama unreachable)`, because it probed the host's `127.0.0.1:11434`, and
# printed a ~50-line urllib traceback above that verdict. Only a run against a
# live deployment can show the difference, and this is the one job that has
# one. It goes here, immediately after the models are verified present, so a
# PRESENT here is checked against ground truth established two steps up.
#
# No fault injection needed (the rule's usual companion, CLAUDE.md/#3753): the
# pre-fix failure is what this step's own assertions describe, and it happens
# *naturally* on any live Kubernetes deployment -- the host Ollama is absent
# by design in this mode. `127.0.0.1:11434` in the Required models header IS
# the pre-fix output, so asserting against it fails on a revert.
STATUS_OUT=$(nyxgpt ops status 2>&1) || fail "nyxgpt ops status exited non-zero"
echo "$STATUS_OUT"
if grep -q "Traceback" <<<"$STATUS_OUT"; then
    fail "ops status printed a Python traceback -- an unreachable dependency is one line"
fi
grep -qE "^  kubernetes: ${NAMESPACE} namespace: [0-9]+/[0-9]+ pod\(s\) ready" <<<"$STATUS_OUT" ||
    fail "the 'Deployment mode' block does not name Kubernetes above a running cluster"
ok "the Deployment mode block names the running Kubernetes deployment"
# The block header, sliced out on its own: `127.0.0.1:11434` legitimately
# appears elsewhere in a status report (the Ollama env agent), so the
# assertion has to be about the line that says which Ollama was asked.
MODELS_HEADER=$(grep -E "^Required models \(Ollama at " <<<"$STATUS_OUT") ||
    fail "ops status printed no Required models block at all"
grep -q "(in-cluster)" <<<"$MODELS_HEADER" ||
    fail "the required-model check did not read the in-cluster Ollama: ${MODELS_HEADER}"
if grep -q "127.0.0.1" <<<"$MODELS_HEADER"; then
    fail "the required-model check probed the host -- this is the #3987 defect: ${MODELS_HEADER}"
fi
for role_model in "chat: ${MODEL}" "embedding: ${EMBEDDING_MODEL}"; do
    grep -qE "^  ${role_model}.* -- PRESENT$" <<<"$STATUS_OUT" ||
        fail "ops status does not report ${role_model} as PRESENT, though step 3 read it \
straight out of the cluster's Ollama"
done
ok "required models resolve PRESENT from the in-cluster Ollama, with no traceback"
# doctor, per the issue's fourth acceptance criterion. Its exit code is not
# the assertion: a CI runner with a Kubernetes-only install legitimately has
# native-side findings, so the check is on what it SAYS about models.
DOCTOR_OUT=$(nyxgpt ops doctor 2>&1) || true
echo "$DOCTOR_OUT"
if grep -q "Traceback" <<<"$DOCTOR_OUT"; then
    fail "ops doctor printed a Python traceback"
fi
grep -qE "^Kubernetes deployment: ${NAMESPACE} namespace: [0-9]+/[0-9]+ pod\(s\) ready" \
    <<<"$DOCTOR_OUT" ||
    fail "ops doctor does not name the Kubernetes deployment its checks are reported against"
grep -q "reported against the cluster" <<<"$DOCTOR_OUT" ||
    fail "ops doctor does not say model readiness is read from the cluster"
if grep -q "pull into the cluster" <<<"$DOCTOR_OUT"; then
    fail "ops doctor reports a missing required model against a cluster that has both"
fi
ok "ops doctor reports model readiness against the cluster, and finds nothing missing"

step "5/17 The observability layer came up with the app tier"
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

step "6/17 The web UI is reachable from the host with NO port-forward (#3986)"
# THE assertion #3986 asks for: an HTTP request to the web UI from the host,
# with no forward running. Before the fix a completed install left nothing
# listening -- every Pod Ready, `ops status` healthy, and `curl` refused --
# and the operator had to start a foreground `kubectl port-forward` in a spare
# terminal before the product could be used at all.
# `x && fail` would be an errexit trap of its own (a compound whose overall
# status is non-zero exits the script under `set -e`), so this is an `if`.
if nyxgpt ops port-forward --status | grep -qi 'running'; then
    echo "[info] this cluster uses the managed background forward (the bring-your-own path)."
    echo "       The install established it; the operator still ran no second command."
elif pgrep -f "kubectl.*port-forward" >/dev/null 2>&1; then
    pgrep -af "kubectl.*port-forward" >&2 || true
    fail "a stray port-forward is running -- this step must prove reachability WITHOUT one"
fi
wait_for_web
ok "the UI answers on ${BASE} with nothing forwarding to it"

echo "--- how the address is provided ---"
kubectl -n "$NAMESPACE" get svc nyxgpt-web nyxgpt-api \
    -o custom-columns='NAME:.metadata.name,TYPE:.spec.type,NODEPORT:.spec.ports[*].nodePort'
nyxgpt ops port-forward --status

step "7/17 Fault injection: the shipped ClusterIP Service must break that reachability"
# Without this half, step 6 passes on any build -- the runner would simply be
# reaching the UI some other way and nobody would know. `k8s/service-web.yaml`
# as committed is ClusterIP (the base posture the AWS deployment relies on,
# #3503), so re-applying it verbatim IS the pre-fix state: it strips the node
# port the install patched on. The address must stop answering, and the same
# wrapped install must then restore it.
INJECTED_CLUSTERIP=0
if nyxgpt ops port-forward --status | grep -qi 'running'; then
    ok "this cluster uses the managed background forward, not a NodePort -- \
the Service-type injection does not apply, skipping"
else
    kubectl -n "$NAMESPACE" apply -f "$HOME/.nyxGPT/k8s/service-web.yaml" >/dev/null
    kubectl -n "$NAMESPACE" get svc nyxgpt-web \
        -o custom-columns='NAME:.metadata.name,TYPE:.spec.type,NODEPORT:.spec.ports[*].nodePort'
    reachable_as_clusterip=0
    for _ in $(seq 1 10); do
        if curl -fsS -o /dev/null --max-time 3 "${BASE}/" 2>/dev/null; then
            reachable_as_clusterip=1
        fi
        sleep 2
    done
    [ "$reachable_as_clusterip" -eq 0 ] ||
        fail "the UI was still reachable with the shipped ClusterIP web Service -- step 6 is \
vacuous and cannot detect the #3986 regression"
    INJECTED_CLUSTERIP=1
    ok "the shipped ClusterIP Service leaves ${BASE} unreachable -- step 6 is load-bearing"
    # Put it back the way an operator would: re-run the wrapped install. The
    # rest of this script needs a reachable UI, and proving the install
    # RE-ESTABLISHES the access path is worth more than a kubectl patch here.
    nyxgpt ops install --kubernetes --api-key "$API_KEY" --skip-observability >/dev/null ||
        fail "the re-install did not complete after the ClusterIP injection"
    wait_for_web
    ok "re-running the install republished the Service and restored ${BASE}"
fi

step "8/17 Reachability survives Pod replacement (#3986)"
# The property a `kubectl port-forward` does NOT have: it attaches to one Pod
# and exits when that Pod is replaced, so a canary rollout or a self-heal
# restart silently took the UI down again -- which is why #3986 rejects the
# forward as the answer even as a workaround.
kubectl -n "$NAMESPACE" delete pod -l app=nyxgpt-web-canary-pool --wait=true >/dev/null
kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-web-stable --timeout=300s >/dev/null ||
    fail "the web Deployment did not replace its Pod"
wait_for_web
ok "the same URL answers after every web Pod was replaced"

step "9/17 The canary pair rests at 0, and there is a wrapped way back (#3991)"
for deployment in nyxgpt-api-canary nyxgpt-web-canary; do
    replicas=$(kubectl -n "$NAMESPACE" get "deploy/${deployment}" -o jsonpath='{.spec.replicas}')
    [ "$replicas" = "0" ] ||
        fail "${deployment} rests at ${replicas} replicas after a fresh install -- its manifest \
declares 0, and two idle canary Pods are carrying live Service endpoints outside any rollout"
done
ok "both canary Deployments rest at 0, matching their manifests"

# Now the state #3991 was filed from: an idle canary carrying replicas, with
# `canary status` reporting no rollout. `rollback` refuses -- correctly, it
# ends rollouts and there is none -- which used to leave raw `kubectl scale`
# as the only recovery.
kubectl -n "$NAMESPACE" scale deploy/nyxgpt-api-canary --replicas=1 >/dev/null
if nyxgpt canary rollback >/dev/null 2>&1; then
    fail "canary rollback claimed to handle an idle canary -- the premise of this step is wrong"
fi
ok "canary rollback refuses an off-contract idle canary, as its contract says"
nyxgpt canary reset || fail "canary reset could not stand the idle canary down"
replicas=$(kubectl -n "$NAMESPACE" get deploy/nyxgpt-api-canary -o jsonpath='{.spec.replicas}')
[ "$replicas" = "0" ] ||
    fail "canary reset returned success but nyxgpt-api-canary is still at ${replicas} replicas"
ok "nyxgpt canary reset returns an off-contract canary to 0 -- no raw kubectl scale"

step "10/17 The install reconciles a canary left off-contract"
# The other half of #3991: the install applies the manifests and must then
# ASSERT the resting state, not assume it. Scale the canary up and re-run the
# install; it must come back to rest. (`kubectl apply -k` alone already sets
# the manifests' `replicas: 0` -- what was missing is the install ever
# CHECKING, which is what a canary left carrying replicas by an interrupted
# rollout needs.)
kubectl -n "$NAMESPACE" scale deploy/nyxgpt-web-canary --replicas=1 >/dev/null
echo "[info] the ClusterIP injection ran in step 6: ${INJECTED_CLUSTERIP}"
nyxgpt ops install --kubernetes --api-key "$API_KEY" --skip-observability >/dev/null ||
    fail "the reconciling re-install failed"
replicas=$(kubectl -n "$NAMESPACE" get deploy/nyxgpt-web-canary -o jsonpath='{.spec.replicas}')
[ "$replicas" = "0" ] ||
    fail "a re-install left nyxgpt-web-canary at ${replicas} replicas -- the install still does \
not assert the resting contract it applied (#3991)"
ok "a re-install brings an off-contract canary back to its resting 0"

step "11/17 The Infrastructure page detects this cluster from inside it (#3988)"
# The api Pod answers about the cluster it is running in. The gate used to ask
# `kubectl config current-context`, which is EMPTY in a Pod -- print it, so the
# log carries the pre-fix input alongside the post-fix verdict.
api_pod=$(kubectl -n "$NAMESPACE" get pod -l app=nyxgpt-api-canary-pool,track=stable \
    -o jsonpath='{.items[0].metadata.name}')
echo "--- what the old gate saw inside ${api_pod} ---"
kubectl -n "$NAMESPACE" exec "$api_pod" -- \
    sh -c 'kubectl config current-context 2>&1; echo "(exit $?)"' || true
infra=$(kubectl -n "$NAMESPACE" exec "$api_pod" -- \
    curl -fsS -H "X-API-Key: ${API_KEY}" http://127.0.0.1:8000/api/v1/infra/status)
echo "$infra" | python3 -c '
import json, sys
data = json.load(sys.stdin)
k8s = data["kubernetes"]
assert data.get("in_cluster") is True, "the api Pod does not know it is in a cluster"
assert k8s["configured"] is True, "in-cluster credentials were not accepted as a configured cluster"
assert k8s["deployed"] is True, "the page reports NOT DEPLOYED from inside the deployment (#3988)"
assert k8s["pods"], "no Pods reported -- the in-cluster RBAC read failed"
reason = data.get("compose_probe_reason") or ""
assert "/root/.nyxGPT" not in reason, f"the container path is still leaked as a reason: {reason}"
assert data["install_mode"]["in_scope"] is False, "the native card is not scoped out in-cluster"
pod_count = len(k8s["pods"])
context = k8s["context"]
print(f"[OK] in-cluster: {pod_count} Pods, context={context!r}")
' || fail "the Infrastructure payload served from inside the cluster is wrong (#3988)"
ok "the page served by the api Pod reports the deployment it is running in"

step "12/17 The user path works: sessions list, via the web Service"
wait_for_web
curl -fsS "${BASE}/api/sessions" >/dev/null ||
    fail "GET /api/sessions failed -- this is the UI's 'Failed to load sessions'"
ok "session list loads through the web UI's own proxy route"

step "13/17 A real chat round-trip"
curl -fsS -X POST "${BASE}/api/sessions/init" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${SESSION}\"}" >/dev/null || fail "could not create a chat session"
chat_round_trip "$SESSION" || fail "chat round-trip produced no answer -- no chat is possible"
ok "chat answered through web -> api -> in-cluster Ollama"

step "14/17 The observability tier RECEIVES telemetry, not just runs (#3990)"
# The question step 4 cannot answer. #3990 was an install where all ten
# observability workloads reported `1/1 ready`, Grafana and Prometheus
# answered 200, and the tier received NOTHING from the application it exists
# to observe: the app Pods exported every span to `localhost:4318` -- their own
# Pods -- and reported no errors at all, while Grafana authenticated to
# GlitchTip with the placeholder token the manifest ships. Readiness cannot
# see that (a collector with no clients is as ready as one with a thousand),
# and neither can manifest review, so this step asks each backend what it has
# actually RECEIVED, after the chat above has given it something to receive.
#
# Each half carries its own fault injection, per the D-006 rule: the pre-fix
# endpoint and the pre-fix credential are exercised HERE, against this same
# cluster, and must fail -- otherwise a green line proves nothing.

# 7a. The api's EFFECTIVE config -- what the process is running with, not what
#     the ConfigMap says -- points at the collector Service.
# shellcheck disable=SC2016  # $HOME must expand inside the Pod, not out here
kubectl -n "$NAMESPACE" exec deploy/nyxgpt-api-stable -- \
    sh -c 'grep -h "^otlp_endpoint" "${HOME:-/root}/.nyxGPT/config.ini"' \
    >/tmp/k8s-smoke-otlp.txt ||
    fail "the api Pod has no [tracing] otlp_endpoint at all -- this is the #3990 regression"
grep -q "http://otel-collector:4318/v1/traces" /tmp/k8s-smoke-otlp.txt ||
    fail "the api exports spans to $(cat /tmp/k8s-smoke-otlp.txt) -- not to the in-cluster \
collector; every span is dropped (#3990)"
ok "the api Pod exports spans to http://otel-collector:4318/v1/traces"

# 7b. FAULT INJECTION for 7a: prove the endpoint it USED to carry is dead.
#     Posting an empty OTLP payload from inside the api Pod to both endpoints
#     shows the collector accepting and the Pod-local default refusing -- so
#     the assertion above is about a reachable endpoint, not a spelling.
kubectl -n "$NAMESPACE" exec deploy/nyxgpt-api-stable -- python -c '
import httpx

def post(url):
    try:
        return str(httpx.post(url, json={"resourceSpans": []}, timeout=5).status_code)
    except Exception as e:  # connection refused, DNS failure, ...
        return type(e).__name__

print("collector", post("http://otel-collector:4318/v1/traces"))
print("pod-local", post("http://localhost:4318/v1/traces"))
' >/tmp/k8s-smoke-otlp-probe.txt || fail "could not probe the OTLP endpoints from the api Pod"
cat /tmp/k8s-smoke-otlp-probe.txt
grep -q "^collector 200$" /tmp/k8s-smoke-otlp-probe.txt ||
    fail "otel-collector did not accept an OTLP payload from the api Pod"
if grep -q "^pod-local 200$" /tmp/k8s-smoke-otlp-probe.txt; then
    fail "something IS listening on the api Pod's own :4318 -- this probe cannot detect \
the #3990 misdirection and is worthless as a gate"
fi
ok "the collector accepts spans and the pre-fix pod-local endpoint refuses them"

# 7c. End to end: Jaeger must now know the nyxGPT services, not only itself.
#     `{"data":["jaeger-all-in-one"],"total":1}` is the literal payload the
#     acceptance run captured -- Jaeger's own self-traces, which read in the
#     UI as "tracing works" while Grafana's trace panels stayed empty.
services=""
for _ in $(seq 1 24); do
    services=$(kubectl -n "$NAMESPACE" exec deploy/grafana -- \
        wget -q -O - -T 10 http://jaeger:16686/api/services 2>/dev/null || true)
    case "$services" in *nyxgpt-api*) break ;; esac
    sleep 5
done
echo "jaeger services: $services"
case "$services" in
    *nyxgpt-api*) ok "Jaeger holds spans from nyxgpt-api after the chat round-trip" ;;
    *) fail "Jaeger knows only $services -- the chat above produced no spans the collector \
ever saw (#3990)" ;;
esac
case "$services" in
    *nyxgpt-web*) ok "Jaeger holds spans from nyxgpt-web (NYXGPT_OTLP_ENDPOINT is wired)" ;;
    *) fail "Jaeger has no nyxgpt-web spans -- the web Deployment is still tracing to its \
own Pod (#3990)" ;;
esac

# 7d. Grafana's GlitchTip credential: provisioned, and accepted. The
#     placeholder is what produced `401 Unauthorized` on every SRE Home
#     GlitchTip panel, so it is also this check's fault injection.
GLITCHTIP_TOKEN=$(kubectl -n "$NAMESPACE" exec deploy/grafana -- \
    cat /etc/nyxgpt-secrets/glitchtip-grafana-token 2>/dev/null || true)
[ -n "$GLITCHTIP_TOKEN" ] || fail "Grafana has no GlitchTip token mounted at all"
[ "$GLITCHTIP_TOKEN" != "UNCONFIGURED-glitchtip-token" ] ||
    fail "Grafana still holds the placeholder GlitchTip token -- the SRE Home panels will \
401 (#3990); the install did not provision one"
kubectl -n "$NAMESPACE" exec deploy/grafana -- sh -c \
    "wget -q -O - -T 10 --header=\"Authorization: Bearer \$(cat /etc/nyxgpt-secrets/\
glitchtip-grafana-token)\" http://glitchtip:8080/api/0/organizations/ >/dev/null" ||
    fail "GlitchTip rejected the provisioned token -- the SRE Home panels would 401"
if kubectl -n "$NAMESPACE" exec deploy/grafana -- sh -c \
    'wget -q -O - -T 10 --header="Authorization: Bearer UNCONFIGURED-glitchtip-token" \
http://glitchtip:8080/api/0/organizations/ >/dev/null' 2>/dev/null; then
    fail "GlitchTip accepted the PLACEHOLDER token -- this check cannot detect the 401 \
this issue is about"
fi
ok "GlitchTip accepts Grafana's provisioned token and refuses the placeholder"

# 7e. And the errors half end to end: an exception reported inside the cluster
#     has to reach the in-cluster GlitchTip. The api answers 503 when error
#     tracking is inactive or has no valid DSN -- i.e. the pre-fix state --
#     and 202 only once it is really reporting, so this assertion cannot pass
#     vacuously.
ERROR_MARKER="k8s-smoke-synthetic-error-$$"
kubectl -n "$NAMESPACE" exec deploy/nyxgpt-api-stable -- python -c "
import sys, httpx
r = httpx.post(
    'http://127.0.0.1:8000/api/v1/error-tracking/report',
    json={'message': '${ERROR_MARKER}'},
    headers={'X-API-Key': '${API_KEY}'},
    timeout=30,
)
print(r.status_code, r.text[:200])
sys.exit(0 if r.status_code == 202 else 1)
" || fail "the api would not report an error (503 means error tracking is inactive or has \
no valid DSN -- the #3990 state: the api had no [error_tracking] section at all)"

found=""
for _ in $(seq 1 24); do
    issues=$(kubectl -n "$NAMESPACE" exec deploy/grafana -- sh -c \
        "wget -q -O - -T 10 --header=\"Authorization: Bearer \$(cat /etc/nyxgpt-secrets/\
glitchtip-grafana-token)\" http://glitchtip:8080/api/0/organizations/nyxgpt/issues/" \
        2>/dev/null || true)
    case "$issues" in *"$ERROR_MARKER"*) found=1; break ;; esac
    sleep 5
done
[ -n "$found" ] ||
    fail "the api accepted the error but GlitchTip never received it -- the DSN does not \
resolve to the in-cluster GlitchTip (#3565's failure mode, in Kubernetes)"
ok "an error raised in the cluster arrived in the in-cluster GlitchTip"

step "15/17 Sessions are shared by every api replica (Cassandra-backed)"
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

step "16/17 Self-heal sees the whole cluster, not just the api pool (#3828)"
# Deletes a web Pod for real (the heal action), which is why it runs after the
# user-path steps. Nothing has to be torn down first any more (#3986): the
# address the steps above used is a NodePort (or a supervised forward), not a
# process attached to the Pod being deleted -- which is exactly the property
# step 7 asserts.
python3 scripts/k8s-self-heal-coverage-smoke.py ||
    fail "self-heal does not cover this deployment -- see the output above (#3828)"
ok "self-heal names the mode, watches every tier, and heals a non-api Pod"

step "17/17 Fault injection: the pre-#3786 topology must FAIL this same check"
kubectl -n "$NAMESPACE" delete statefulset cassandra ollama --wait=true >/dev/null
kubectl -n "$NAMESPACE" wait --for=delete pod/ollama-0 --timeout=180s >/dev/null 2>&1 || true
wait_for_web
if curl -fsS -o /dev/null "${BASE}/api/sessions" 2>/dev/null; then
    fail "the session list still loaded with no Cassandra in the cluster -- \
step 11 cannot detect the #3786 regression"
fi
ok "without Cassandra the session list fails (the UI's 'Failed to load sessions')"
if chat_round_trip "${SESSION}-nofix" >/tmp/k8s-smoke-nofix.log 2>&1; then
    cat /tmp/k8s-smoke-nofix.log >&2
    fail "chat still answered with no Ollama and no Cassandra in the cluster -- \
step 12 cannot detect the #3786 regression and is worthless as a gate"
fi
ok "without the data/LLM tier the chat round-trip fails, as it must"

echo
echo "[PASS] k8s local deploy produces a stack that can actually chat (#3786)"
