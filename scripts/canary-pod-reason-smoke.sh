#!/usr/bin/env bash
# Executed verification for #3831 (#3775 gate).
#
# The question this answers: when a canary Pod cannot run, does the reason
# Kubernetes gives actually reach the operator?
#
# #3831 was an acceptance failure where the canary dashboard showed
# "[object Object]" over a rollout that had really failed with
# "FailedScheduling: Insufficient memory". The rendering half is unit-tested
# (web/tests/lib/apiError.test.ts). This half cannot be: the reason is parsed
# out of a live `kubectl get pods -o json`, and a hand-written fixture proves
# only that the parser agrees with the fixture's author. A real scheduler
# refusing a real Pod on a real cluster is the only evidence that the field
# names, the condition shape and the message text are what the code expects.
#
# Two halves, per the fault-injection rule (CLAUDE.md, #3753):
#
#   1. UNSCHEDULABLE  -- a Deployment asking for more memory than the node has.
#      `deployment_health()` must report unhealthy AND name the scheduler's own
#      sentence ("Insufficient memory"), and `_wait_rollout()` must do the same
#      instead of only "did not become healthy within Ns". The script asserts
#      the message is strictly longer than the pre-fix message it still
#      contains -- the pre-fix code never queried the Pods, so it could not
#      produce this output no matter what the cluster said.
#   2. SCHEDULABLE    -- the same Deployment with sane requests must come back
#      healthy with NO reason appended and no extra Pod query, so the job fails
#      if the enrichment starts firing (or fabricating) unconditionally.
#
# Deliberately tiny: one kind node and two `registry.k8s.io/pause` Pods, no
# nyxGPT image build (agentic first principle 1 -- cost). It is not a canary
# rollout end to end; k8s-local-smoke.yml owns the full stack.
#
# Prerequisites: docker, kind, kubectl, and `pip install -e .`.
set -euo pipefail

NAMESPACE="${NYXGPT_CANARY_SMOKE_NS:-nyxgpt-canary-reason-smoke}"
CLUSTER="${NYXGPT_CANARY_SMOKE_CLUSTER:-canary-reason-smoke}"
KEEP_CLUSTER="${NYXGPT_CANARY_SMOKE_KEEP:-0}"

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "--- diagnostics ---" >&2
        kubectl -n "$NAMESPACE" get pods -o wide >&2 2>/dev/null || true
        kubectl -n "$NAMESPACE" describe pods >&2 2>/dev/null || true
    fi
    if [ "$KEEP_CLUSTER" != "1" ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap cleanup EXIT

step "Provision a throwaway kind cluster"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 90s
kubectl cluster-info --context "kind-${CLUSTER}" >/dev/null
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
ok "cluster ${CLUSTER} is up"

# The canary Deployments' real label shape (k8s/deployment-canary.yaml): the
# code reads the selector off the Deployment rather than guessing it, so the
# fixture has to carry a multi-label selector for that read to mean anything.
render_deployment() {
    local name="$1" memory="$2" track="$3"
    cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
  labels:
    app: ${name}-pool
    track: ${track}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${name}-pool
      track: ${track}
  template:
    metadata:
      labels:
        app: ${name}-pool
        track: ${track}
    spec:
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              memory: "${memory}"
YAML
}

step "Half 1: a canary Pod the scheduler cannot place"
render_deployment "nyxgpt-api-canary" "900Gi" "canary" | kubectl apply -f -
# Wait for the scheduler to have actually rendered its verdict; without this
# the read can race the very condition it is asserting on.
for _ in $(seq 1 30); do
    if kubectl -n "$NAMESPACE" get pods -l app=nyxgpt-api-canary-pool \
        -o jsonpath='{.items[*].status.conditions[?(@.type=="PodScheduled")].reason}' \
        2>/dev/null | grep -q Unschedulable; then
        break
    fi
    sleep 2
done
kubectl -n "$NAMESPACE" get pods -l app=nyxgpt-api-canary-pool -o wide || true

NYXGPT_CANARY_SMOKE_NS="$NAMESPACE" python3 - <<'PY'
import os
import sys

from nyxgpt import canary

ns = os.environ["NYXGPT_CANARY_SMOKE_NS"]
name = "nyxgpt-api-canary"

health = canary.deployment_health(name, ns)
print(f"deployment_health -> state={health.state!r} message={health.message!r}")
if health.state != "unhealthy":
    sys.exit(f"[FAIL] expected state 'unhealthy', got {health.state!r}")

# The pre-fix message, verbatim: the whole defect was that this was ALL the
# operator ever saw. It must still be there (no regression in the summary) and
# it must no longer be the entire message.
pre_fix = f"{name} not healthy (0/1 ready)"
if pre_fix not in health.message:
    sys.exit(f"[FAIL] summary changed shape: {health.message!r}")
if health.message == pre_fix:
    sys.exit("[FAIL] no reason appended -- this is exactly the pre-fix output")
if "Insufficient memory" not in health.message:
    sys.exit(f"[FAIL] the scheduler's own reason is missing: {health.message!r}")
if "Unschedulable" not in health.message:
    sys.exit(f"[FAIL] the scheduler's reason code is missing: {health.message!r}")
print("[OK] deployment_health names the live scheduler verdict")

# The rollout path an operator hits from the dashboard's Deploy button.
result = canary._wait_rollout(name, ns, timeout_seconds=15)
print(f"_wait_rollout -> ok={result.ok} message={result.message!r} details={result.details!r}")
if result.ok:
    sys.exit("[FAIL] an unschedulable rollout must not report success")
if "Insufficient memory" not in result.message:
    sys.exit(f"[FAIL] the rollout failure hides the reason: {result.message!r}")
print("[OK] _wait_rollout names the live scheduler verdict")

# What the dashboard is handed for that failure: one line, reason included.
from nyxgpt.app import _canary_failure_detail  # noqa: E402

detail = _canary_failure_detail(result)
if "Insufficient memory" not in detail or "\n" in detail:
    sys.exit(f"[FAIL] the API detail is not a single line naming the reason: {detail!r}")
print("[OK] the 409 the dashboard renders carries the reason")
PY
ok "the Kubernetes reason survives all the way to the API's error detail"

step "Half 2: the same Deployment, schedulable"
kubectl delete deployment nyxgpt-api-canary -n "$NAMESPACE" --wait=true
render_deployment "nyxgpt-api-canary" "16Mi" "canary" | kubectl apply -f -
kubectl -n "$NAMESPACE" rollout status deployment/nyxgpt-api-canary --timeout=120s

NYXGPT_CANARY_SMOKE_NS="$NAMESPACE" python3 - <<'PY'
import os
import sys

from nyxgpt import canary

ns = os.environ["NYXGPT_CANARY_SMOKE_NS"]
health = canary.deployment_health("nyxgpt-api-canary", ns)
print(f"deployment_health -> state={health.state!r} message={health.message!r}")
if health.state != "healthy":
    sys.exit(f"[FAIL] expected 'healthy', got {health.state!r}: {health.message!r}")
if "--" in health.message:
    sys.exit(f"[FAIL] a healthy track must carry no failure reason: {health.message!r}")
print("[OK] a healthy track reports no reason")
PY
ok "the enrichment fires only when there is something to explain"

echo
echo "=== canary pod-reason smoke PASSED ==="
