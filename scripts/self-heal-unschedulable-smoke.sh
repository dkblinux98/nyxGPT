#!/usr/bin/env bash
# Executed verification for #3832 (#3775): what does self-heal DO to a Pod the
# scheduler refused?
#
# The question inspection cannot answer. #3832 was found by watching a real
# cluster, not by reading the code: seven Pods in 4.5 minutes, one roughly
# every 15 seconds, each `FailedScheduling: Insufficient memory`, each deletion
# resetting the Pod's age so the operator never saw one stuck long enough to
# diagnose. Nothing in a unit test's mocked `kubectl` proves the scheduler
# really refuses a Pod, that the ReplicaSet really recreates it under a new
# name, or that self-heal really leaves it alone across repeated passes.
#
# Three halves, per the fault-injection rule (CLAUDE.md, #3753) -- a job that
# only ran the fixed code would pass on a build that never had the bug:
#
#   1. THE PRE-FIX REMEDY, REPRODUCED -- take the old action (`kubectl delete
#      pod`) against the unschedulable Pod three times and assert what #3832
#      reported: a new Pod name every time, Pending and unschedulable for the
#      identical reason every time, and its age reset to seconds. Deleting
#      does not converge; it destroys the evidence. If the replacement ever
#      came back schedulable, the injection did not reproduce the defect and
#      this job fails rather than passing vacuously.
#   2. THE FIX -- run real `self_heal.heal_now()` passes through a `kubectl`
#      shim that records every argv, and assert ZERO delete calls, the Pod's
#      UID unchanged, the scheduler's own message surfaced on the component
#      row, and an explicit operator "Heal now" refused with that reason.
#   3. THE BUDGET -- against a Pod that IS Running-but-not-ready (the one
#      state deletion repairs), assert healing still happens AND stops at the
#      consecutive-restart cap even though every heal gives the Pod a new
#      name. Pre-fix the budget was keyed by Pod name, so recreation bought a
#      fresh budget and the cap could never fire.
#
# Prerequisites: a reachable cluster (the workflow creates a kind one),
# `kubectl` on PATH, and a `nyxgpt` importable (`pip install -e .`).
set -euo pipefail

NAMESPACE="nyxgpt"
POOL_LABEL="app=nyxgpt-api-canary-pool"
UNSCHEDULABLE_DEPLOY="nyxgpt-api-unschedulable"
NOTREADY_DEPLOY="nyxgpt-api-notready"
WORKDIR="$(mktemp -d)"
KUBECTL_LOG="$WORKDIR/kubectl-calls.log"
SHIM_DIR="$WORKDIR/shim"

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK] $*"; }
step() { echo; echo "=== $* ==="; }

cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "--- diagnostics ---" >&2
        kubectl -n "$NAMESPACE" get pods -o wide >&2 2>/dev/null || true
        kubectl -n "$NAMESPACE" describe pods -l "$POOL_LABEL" >&2 2>/dev/null || true
        echo "--- kubectl calls self-heal made ---" >&2
        cat "$KUBECTL_LOG" >&2 2>/dev/null || true
    fi
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

command -v kubectl >/dev/null 2>&1 || fail "kubectl is not on PATH"
kubectl cluster-info >/dev/null 2>&1 || fail "no reachable cluster"
python3 -c "import nyxgpt.self_heal" >/dev/null 2>&1 || fail "nyxgpt is not importable"

REAL_KUBECTL="$(command -v kubectl)"

pool_pod() {
    kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=$1" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

pool_pod_uid() {
    kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=$1" \
        -o jsonpath='{.items[0].metadata.uid}' 2>/dev/null
}

wait_for_unschedulable() {
    local deadline=$((SECONDS + 180))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local reason
        reason=$(kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=unschedulable" -o \
            jsonpath='{.items[0].status.conditions[?(@.type=="PodScheduled")].reason}' 2>/dev/null || true)
        if [ "$reason" = "Unschedulable" ]; then
            return 0
        fi
        sleep 3
    done
    return 1
}

wait_for_running_not_ready() {
    local deadline=$((SECONDS + 240))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local phase ready
        phase=$(kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=notready" \
            -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
        ready=$(kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=notready" -o \
            jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
        if [ "$phase" = "Running" ] && [ "$ready" = "False" ]; then
            return 0
        fi
        sleep 3
    done
    return 1
}

step "Create the unschedulable workload self-heal watches"
# The label selector is self-heal's own (K8S_POD_LABEL_SELECTOR): these Pods
# are what a `--kubernetes --local` install's api pool looks like to the
# watchdog. `role=` distinguishes the two cases within it.
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
cat <<YAML | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $UNSCHEDULABLE_DEPLOY
  namespace: $NAMESPACE
spec:
  replicas: 1
  selector:
    matchLabels: {app: nyxgpt-api-canary-pool, role: unschedulable}
  template:
    metadata:
      labels: {app: nyxgpt-api-canary-pool, role: unschedulable}
    spec:
      containers:
        - name: app
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              # Genuinely unschedulable on any node this job can run on --
              # the real #3832 condition (`Insufficient memory`), not a
              # simulated one.
              memory: 4096Gi
YAML

wait_for_unschedulable || fail "the Pod never became Unschedulable -- the condition under test does not exist"
ok "Pod $(pool_pod unschedulable) is Pending/Unschedulable"

# ---------------------------------------------------------------------------
step "HALF 1 -- the pre-fix remedy, reproduced: delete does not converge"
# ---------------------------------------------------------------------------
# Raw `kubectl delete pod` is deliberate HERE and only here: this reconstructs
# the action self-heal used to take, to prove it was never a remedy. Every
# operator-facing path is a `nyxgpt` command.
seen_names=""
for i in 1 2 3; do
    name="$(pool_pod unschedulable)"
    [ -n "$name" ] || fail "no unschedulable Pod to delete on iteration $i"
    case " $seen_names " in
        *" $name "*) fail "the ReplicaSet reused the Pod name $name -- half 1 cannot show the age reset" ;;
    esac
    seen_names="$seen_names $name"
    "$REAL_KUBECTL" -n "$NAMESPACE" delete pod "$name" --wait=true >/dev/null
    wait_for_unschedulable || fail "fault injection failed: the replacement Pod is not unschedulable, so deleting APPEARED to work"
    age=$(kubectl -n "$NAMESPACE" get pods -l "$POOL_LABEL,role=unschedulable" \
        --no-headers | awk '{print $5}')
    echo "  deletion $i: $name -> $(pool_pod unschedulable), still Pending/Unschedulable, age reset to $age"
done
ok "3 deletions produced 3 new Pods, all unschedulable for the identical reason (#3832's loop)"

# ---------------------------------------------------------------------------
step "HALF 2 -- the fix: self-heal takes no action against the unschedulable Pod"
# ---------------------------------------------------------------------------
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/kubectl" <<SHIM
#!/usr/bin/env bash
# Records every kubectl argv self-heal runs, then execs the real one. "Zero
# delete calls" is a claim about commands issued, so the commands are what
# this job observes.
echo "\$*" >> "$KUBECTL_LOG"
exec "$REAL_KUBECTL" "\$@"
SHIM
chmod +x "$SHIM_DIR/kubectl"
: > "$KUBECTL_LOG"

before_uid="$(pool_pod_uid unschedulable)"
before_name="$(pool_pod unschedulable)"

PATH="$SHIM_DIR:$PATH" python3 - "$before_name" <<'PY'
import json
import sys

from nyxgpt import self_heal

pod_name = sys.argv[1]

# Ten automatic passes -- more than the watchdog managed in the 4.5 minutes
# that destroyed seven Pods.
for _ in range(10):
    self_heal.heal_now(backoff_seconds=0.0)

status = self_heal.status()
row = next(
    (c for c in status["components"] if c["service"] == pod_name),
    None,
)
if row is None:
    raise SystemExit(f"self-heal did not report the Pod at all: {json.dumps(status, indent=2)}")
print(json.dumps(row, indent=2))

assert row["healthy"] is False, row
assert row["healable"] is False, row
assert row["health"] == "unschedulable", row
assert "Insufficient memory" in row["note"], row
assert row["giving_up"] is False, row  # nothing was tried; nothing was given up on

# The operator's explicit request is refused with the same reason, rather
# than silently doing nothing or deleting the Pod.
manual = self_heal.heal_now(service=pod_name)
print(json.dumps(manual["healed"], indent=2))
assert len(manual["healed"]) == 1, manual
assert manual["healed"][0]["ok"] is False, manual
assert manual["healed"][0]["action"] == "refused", manual
assert "Insufficient memory" in manual["healed"][0]["message"], manual
PY

deletes=$(grep -c "delete pod" "$KUBECTL_LOG" || true)
[ "$deletes" = "0" ] || fail "self-heal issued $deletes delete call(s) against an unschedulable Pod"
[ "$(pool_pod_uid unschedulable)" = "$before_uid" ] || fail "the Pod was replaced despite no delete call"
ok "10 automatic passes + 1 manual heal: zero delete calls, Pod UID unchanged, reason surfaced"

# ---------------------------------------------------------------------------
step "HALF 3 -- the budget: healing a Running-but-not-ready Pod still stops"
# ---------------------------------------------------------------------------
# Created only now, so half 2's "zero delete calls" could only ever have been
# about the unschedulable Pod.
cat <<YAML | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $NOTREADY_DEPLOY
  namespace: $NAMESPACE
spec:
  replicas: 1
  selector:
    matchLabels: {app: nyxgpt-api-canary-pool, role: notready}
  template:
    metadata:
      labels: {app: nyxgpt-api-canary-pool, role: notready}
    spec:
      terminationGracePeriodSeconds: 1
      containers:
        - name: app
          # busybox rather than pause: the readiness probe needs a shell
          # binary to fail with. Running-but-never-Ready is the one state
          # deleting a Pod repairs, so this is the case that must KEEP working.
          image: busybox:1.36
          command: ["sleep", "3600"]
          readinessProbe:
            exec:
              command: ["/bin/false"]
            periodSeconds: 2
YAML

wait_for_running_not_ready || fail "the not-ready Pod never reached Running/not-Ready"
: > "$KUBECTL_LOG"

PATH="$SHIM_DIR:$PATH" python3 - <<'PY'
import time

from nyxgpt import self_heal

# max_consecutive_restarts=2 keeps the job short. The point is that the budget
# is spent across Pods with DIFFERENT names: every heal deletes the Pod and the
# ReplicaSet makes a new one, which pre-#3832 handed the next pass a fresh
# counter of its own.
for _ in range(10):
    self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)
    time.sleep(3)
PY

deletes=$(grep -c "delete pod" "$KUBECTL_LOG" || true)
[ "$deletes" -ge 1 ] || fail "self-heal never healed a Running-but-not-ready Pod -- the fix over-corrected"
[ "$deletes" -le 2 ] || fail "the heal budget did not hold across Pod recreation: $deletes deletions in 10 passes"
if grep "delete pod" "$KUBECTL_LOG" | grep -q "$UNSCHEDULABLE_DEPLOY"; then
    fail "a delete targeted the unschedulable Deployment's Pod"
fi
ok "healing happened and stopped at the cap ($deletes deletion(s) in 10 passes), never touching the unschedulable Pod"

echo
echo "[PASS] #3832: an unschedulable Pod is reported and left alone; deletion stays"
echo "       the remedy for Running-but-not-ready, and it is budgeted."
