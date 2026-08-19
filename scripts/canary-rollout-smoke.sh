#!/usr/bin/env bash
# Executed verification for the elastic canary pool (#3833, #3775).
#
# The question this answers: on a REAL cluster, does a canary rollout borrow
# the replicas it needs and give them back -- and does the stable track always
# keep at least one Pod serving?
#
# Inspection cannot answer either half. `_split_replicas` returning `stable=0`
# reads as ordinary arithmetic in a diff; on a cluster it is a full cutover
# with the previous version gone from the Service's endpoints. And "the pool
# deflates on rollback" is a claim about what `kubectl scale` was actually
# asked for, three commands later, against whatever the Deployment really
# rested at.
#
# Two halves, per the fault-injection rule (CLAUDE.md, #3753):
#
#   1. THE FIX      -- install the shipped manifests, assert stable really
#      rests at 1 replica on the cluster, then drive `nyxgpt canary
#      start --weight 25` / `promote` / `rollback` and assert, from the
#      Service's own EndpointSlices, which Pods are serving at each step and
#      that the pool is back to 1 at the end.
#   2. PRE-FIX POOL -- apply the split the pre-#3833 code produced from a
#      1-replica pool (canary=1, stable=0) and assert the Service is left
#      serving canary Pods ONLY. That is the failure this issue exists to
#      prevent, and it makes "stable never hits 0" a check that can fail
#      rather than a sentence in a docstring.
#
# The app image is irrelevant to replica arithmetic, and building it costs
# minutes, so both Deployments run a stand-in that answers the manifests' own
# readiness probe. Everything else -- the manifests, the Services, the
# selectors, `nyxgpt canary`'s real kubectl calls -- is the shipped thing.
# The full-stack install is covered by scripts/k8s-local-smoke.sh.
#
# Prerequisites: Docker, kind, kubectl, and a `nyxgpt` on PATH.
set -euo pipefail

CLUSTER="${NYXGPT_CANARY_SMOKE_CLUSTER:-nyxgpt-canary-smoke}"
NAMESPACE="nyxgpt"
# Answers 200 on any path, so the manifests' `GET /health` probe passes
# unmodified; `--port` puts it on the containerPort the manifests declare.
STAND_IN_IMAGE="${NYXGPT_CANARY_SMOKE_IMAGE:-traefik/whoami:v1.10.1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="$HOME/.nyxGPT/canary_state.json"
STATE_BACKUP="${TMPDIR:-/tmp}/nyxgpt-canary-smoke-state-backup.$$"
STATE_BACKED_UP=0
CONFIG_FILE="$HOME/.nyxGPT/config.ini"
CONFIG_SEEDED=0

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "--- diagnostics ---" >&2
        kubectl -n "$NAMESPACE" get deploy,pods -o wide >&2 2>/dev/null || true
        kubectl -n "$NAMESPACE" describe pods >&2 2>/dev/null | tail -60 || true
    fi
    if [ "${NYXGPT_CANARY_SMOKE_KEEP_UP:-0}" != "1" ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    fi
    # Leave the invoking machine's own nyxGPT state as it was found. This
    # script drives the REAL `nyxgpt canary`, whose state file is a fixed path
    # under $HOME, and each phase has to start from a clean one -- but on a
    # developer machine that file may describe a rollout actually in flight,
    # and deleting it would strand a real cluster mid-rollout (the V-015/V-037
    # pattern of a test eating live state).
    rm -f "$STATE_FILE"
    if [ "$STATE_BACKED_UP" = "1" ]; then
        mkdir -p "$(dirname "$STATE_FILE")"
        mv -f "$STATE_BACKUP" "$STATE_FILE" &&
            echo "[INFO] restored your $STATE_FILE"
    fi
    if [ "$CONFIG_SEEDED" = "1" ]; then
        rm -f "$CONFIG_FILE"
    fi
}
trap cleanup EXIT

# Moved aside up front, before any phase clears it -- see cleanup() above.
if [ -f "$STATE_FILE" ]; then
    mv "$STATE_FILE" "$STATE_BACKUP"
    STATE_BACKED_UP=1
    echo "[INFO] moved your existing $STATE_FILE aside; restored on exit"
fi

# Desired (not ready) replica count of a Deployment -- what `nyxgpt canary`
# asked the cluster for, which is the thing under test.
desired() {
    kubectl -n "$NAMESPACE" get "deployment/$1" -o jsonpath='{.spec.replicas}'
}

# The Pods actually serving a Service, from its EndpointSlices: the cluster's
# own answer to "who gets the traffic", not our arithmetic repeated back.
serving_pods() {
    kubectl -n "$NAMESPACE" get endpointslices -l "kubernetes.io/service-name=$1" \
        -o jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)]}{.targetRef.name}{"\n"}{end}' |
        grep -c . || true
}

serving_pods_matching() {
    kubectl -n "$NAMESPACE" get endpointslices -l "kubernetes.io/service-name=$1" \
        -o jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)]}{.targetRef.name}{"\n"}{end}' |
        grep -c -- "$2" || true
}

assert_replicas() {
    local deployment="$1" expected="$2" actual
    actual="$(desired "$deployment")"
    [ "$actual" = "$expected" ] ||
        fail "$deployment has ${actual} desired replicas, expected ${expected}"
    ok "$deployment desired replicas = ${expected}"
}

wait_ready() {
    kubectl -n "$NAMESPACE" rollout status "deployment/$1" --timeout=180s >/dev/null ||
        fail "$1 never became Ready"
}

step "1/7 A cluster, and the api half of the shipped manifests"
# `nyxgpt canary` reads [canary] namespace/total_replicas from the config the
# same way every other command does, so seed the shipped example if the
# machine running this has none.
mkdir -p "$HOME/.nyxGPT"
if [ ! -f "$CONFIG_FILE" ]; then
    cp "${REPO_ROOT}/example.config.ini" "$CONFIG_FILE"
    # Ours, so cleanup() takes it away again; an existing config is never
    # touched.
    CONFIG_SEEDED=1
fi
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/namespace.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/rbac.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/configmap.yaml" >/dev/null
# The real secret.yaml is gitignored (it holds a live key); the example is
# what the repo ships and nothing here reads the value.
kubectl apply -f "${REPO_ROOT}/k8s/secret.example.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/deployment-stable.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/deployment-canary.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/service.yaml" >/dev/null
kubectl apply -f "${REPO_ROOT}/k8s/service-canary.yaml" >/dev/null
ok "cluster ${CLUSTER} up with the api stable/canary pair applied"

step "2/7 Stable rests at 1 replica, as installed (#3833)"
if [ "${NYXGPT_CANARY_SMOKE_INJECT_STANDING_POOL:-0}" = "1" ]; then
    # Fault injection (CLAUDE.md, #3753): put back the standing pool the
    # manifests shipped before #3833. The workflow runs this variant and
    # REQUIRES the script to fail here -- an assertion that passes either way
    # is not a gate. Injected on the cluster rather than by editing the
    # manifest, so the injection cannot drift away from what is applied.
    kubectl -n "$NAMESPACE" patch deployment/nyxgpt-api-stable --type=merge \
        -p '{"spec":{"replicas":4}}' >/dev/null
    echo "[INJECTED] pre-#3833 standing pool: nyxgpt-api-stable scaled to 4"
fi
# The whole point of the issue: an install must not carry a standing pool.
# Asserted against the applied Deployment, not against the YAML text.
assert_replicas nyxgpt-api-stable 1
assert_replicas nyxgpt-api-canary 0

# Swap in the stand-in image (see the header) and give it the port the
# manifests' containerPort/probe declare.
for deployment in nyxgpt-api-stable nyxgpt-api-canary; do
    kubectl -n "$NAMESPACE" set image "deployment/${deployment}" \
        "nyxgpt-api=${STAND_IN_IMAGE}" >/dev/null
    kubectl -n "$NAMESPACE" patch "deployment/${deployment}" --type=json -p \
        '[{"op":"add","path":"/spec/template/spec/containers/0/args","value":["--port","8000"]}]' \
        >/dev/null
done
wait_ready nyxgpt-api-stable
[ "$(serving_pods nyxgpt-api)" = "1" ] ||
    fail "the resting pool serves $(serving_pods nyxgpt-api) Pods, expected 1"
ok "the resting pool serves exactly 1 Pod through the Service"

step "3/7 A real rollout at 25%: the pool GROWS for it"
rm -f "$STATE_FILE"
start_output="$(nyxgpt canary start --weight 25)"
echo "$start_output"
echo "$start_output" | grep -q '^\[OK\]' || fail "canary start failed"
echo "$start_output" | grep -q '25% (1/4 replicas)' ||
    fail "canary start did not report the 25% split it planned"
echo "$start_output" | grep -q 'returns to 1 replica' ||
    fail "canary start did not name the resting count it will hand back"
assert_replicas nyxgpt-api-stable 3
assert_replicas nyxgpt-api-canary 1
wait_ready nyxgpt-api-stable
wait_ready nyxgpt-api-canary
[ "$(serving_pods nyxgpt-api)" = "4" ] ||
    fail "the Service serves $(serving_pods nyxgpt-api) Pods at 25%, expected 4"
[ "$(serving_pods_matching nyxgpt-api nyxgpt-api-canary)" = "1" ] ||
    fail "exactly 1 of the 4 serving Pods must be a canary Pod"
ok "25% is served as 1 canary + 3 stable Pods, all of them in the Service"

step "4/7 Promote to 50%: the pool is RE-PLANNED, not re-sliced"
# `--force` is required here, and is not a way around the #3829 no-traffic
# gate: this smoke's subject is replica arithmetic, so both Deployments run
# the stand-in image (see the header), which exports none of the nyxgpt HTTP
# metric families. `track_metrics` therefore scrapes its Pod successfully and
# reads an attributable ZERO -- indistinguishable, by design, from a canary
# nothing can reach -- and `promote` refuses. Driving requests at the stand-in
# cannot move that count; only re-platforming this smoke onto a real
# nyxgpt-api image could, which would trade minutes of image build for
# coverage that already exists. The gate has its own end-to-end proof on a
# real cluster in scripts/canary-track-metrics-smoke.sh, including the
# refuse-at-zero-traffic step this line is forcing past.
promote_output="$(nyxgpt canary promote --step 25 --force)"
echo "$promote_output"
echo "$promote_output" | grep -q '50% (1/2 replicas)' ||
    fail "promote did not re-plan the pool down to the 2 replicas 50% needs"
# Keeps the `--force` above honest: if this stops matching, the promote no
# longer needed forcing and the flag must come off rather than sit there
# waving a real no-traffic canary through in some future edit of this script.
echo "$promote_output" | grep -q 'forced: the canary track has served no traffic' ||
    fail "the promote was forced but did not report forcing past the no-traffic \
gate -- drop --force from this step (see the comment above it)"
assert_replicas nyxgpt-api-canary 1
assert_replicas nyxgpt-api-stable 1
ok "50% costs 2 Pods, not the 4 the previous step borrowed"

step "5/7 Rollback DEFLATES the pool back to its resting count"
rollback_output="$(nyxgpt canary rollback)"
echo "$rollback_output"
echo "$rollback_output" | grep -q 'restored to its resting 1 replica' ||
    fail "rollback did not report restoring the resting count"
assert_replicas nyxgpt-api-canary 0
assert_replicas nyxgpt-api-stable 1
wait_ready nyxgpt-api-stable
[ "$(serving_pods nyxgpt-api)" = "1" ] ||
    fail "after rollback the Service serves $(serving_pods nyxgpt-api) Pods, expected 1"
ok "the rollout gave every borrowed replica back"

step "6/7 An operator-scaled stable is not re-inflated by the next rollout"
# The other half of "no hardcoded pool": a rollout must return the pool to
# what it FOUND, not to a constant.
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-stable --replicas=2 >/dev/null
wait_ready nyxgpt-api-stable
rm -f "$STATE_FILE"
nyxgpt canary start --weight 50 | tee /tmp/canary-smoke-start2.log
grep -q 'returns to 2 replicas' /tmp/canary-smoke-start2.log ||
    fail "the rollout did not read the operator's live replica count"
nyxgpt canary rollback >/dev/null
assert_replicas nyxgpt-api-stable 2
ok "stable came back to the 2 replicas the operator set, not to a constant"

step "7/7 Fault injection: the pre-#3833 split must FAIL this same check"
# `_split_replicas(total=1, weight=25)` used to return (1, 0) -- apply that
# and watch the Service lose the stable version entirely. Without this half
# "stable never hits 0" is untested: every assertion above would pass on code
# that could still produce it.
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-stable --replicas=0 >/dev/null
kubectl -n "$NAMESPACE" scale deployment/nyxgpt-api-canary --replicas=1 >/dev/null
wait_ready nyxgpt-api-canary
kubectl -n "$NAMESPACE" wait --for=delete pod -l track=stable --timeout=120s >/dev/null 2>&1 || true
[ "$(serving_pods_matching nyxgpt-api nyxgpt-api-stable)" = "0" ] ||
    fail "stable Pods still served traffic at 0 replicas -- this check cannot \
detect the pre-#3833 cutover and is worthless as a gate"
[ "$(serving_pods_matching nyxgpt-api nyxgpt-api-canary)" = "1" ] ||
    fail "expected the canary Pod to be the only endpoint left"
ok "a 0-replica stable is a full cutover, exactly as #3833 says -- which is why \
_split_replicas can no longer produce one"

echo
echo "[PASS] the canary pool is elastic: it grows for a rollout, deflates after \
one, and never starves the stable track (#3833)"
