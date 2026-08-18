#!/usr/bin/env bash
# Shrink a local cluster's schedulable memory to a target, for CI (#3825).
#
# A GitHub runner has ~16GiB; the stock Docker Desktop VM `nyxgpt ops install
# --kubernetes --local` lands on has 8GiB, and reports 7936Mi allocatable once
# the kubelet's reserved slice is out. A capacity gate run on the runner's own
# node is therefore green by luck -- exactly the failure mode the
# fault-injection rule exists to prevent (CLAUDE.md, #3753).
#
# So: place one `pause` Pod, in its own namespace, that REQUESTS the surplus
# and uses none of it. The scheduler then has the operator's node, and so does
# `nyxgpt ops install`'s capacity preflight (it counts every other namespace's
# requests against allocatable). The runner keeps all its real memory, so
# nothing in the stack gets OOM-killed for the sake of the simulation.
#
# Usage: scripts/k8s-node-ballast.sh [target-allocatable-Mi]   (default 7936)
set -euo pipefail

TARGET_MI="${1:-7936}"
BALLAST_NAMESPACE="capacity-ballast"

allocatable=$(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.memory}')
alloc_mi=$(python3 -c '
import sys

raw = sys.argv[1]
for suffix, mult in (("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3)):
    if raw.endswith(suffix):
        print(int(raw[: -len(suffix)]) * mult // 1024**2)
        break
else:
    print(int(raw) // 1024**2)
' "$allocatable")

ballast=$((alloc_mi - TARGET_MI))
echo "node allocatable ${alloc_mi}Mi; reserving ${ballast}Mi so ${TARGET_MI}Mi is schedulable"
if [ "$ballast" -le 0 ]; then
    echo "[FAIL] node is already at or below ${TARGET_MI}Mi -- nothing to ballast, and a" \
        "gate that ballasts nothing is not testing the constrained node it claims to" >&2
    exit 1
fi

kubectl create namespace "$BALLAST_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$BALLAST_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ballast
spec:
  containers:
    - name: pause
      image: registry.k8s.io/pause:3.9
      resources:
        requests:
          memory: ${ballast}Mi
EOF

# The reservation only exists once the Pod is placed; the image pull that
# follows is irrelevant to it.
kubectl -n "$BALLAST_NAMESPACE" wait --for=jsonpath='{.spec.nodeName}' pod/ballast --timeout=120s
kubectl -n "$BALLAST_NAMESPACE" get pod ballast -o wide
