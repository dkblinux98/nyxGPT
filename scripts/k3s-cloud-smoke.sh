#!/usr/bin/env bash
# Executed verification for `nyxgpt cloud deploy --kubernetes` (#3956, D-006).
#
# The question this job answers: **does the k3s bootstrap a `--kubernetes`
# cloud deploy sends actually produce a cluster the existing `k8s/*.yaml`
# manifests run on, with nothing listening on the public interface?**
#
# What it does NOT answer, and says so rather than implying otherwise: whether
# a real EC2 instance comes up. There is no hosted runner that is an EC2
# instance (`docs/live-verification-ci.md`), so the honest proxy is to execute
# the deploy's own bootstrap text on a real Linux machine and check every
# property that does not depend on being in AWS. The one property that does --
# reading the private IPv4 from IMDSv2 -- is exercised through its documented
# fallback here, and the fallback is the code path a non-EC2 machine takes by
# design.
#
# It runs the REAL text, not a copy: `cloud_deploy.render_k3s_bootstrap()` is
# what a deploy pipes to the instance, and it is what step 1 executes. A
# hand-maintained approximation of a bootstrap is evidence about the
# approximation (the #3860 lesson).
#
# Fourteen steps, and nine of them carry fault injections -- a job that only
# runs the happy path passes on every machine that fails to reproduce the bug
# (#3753):
#
#   1  FAULT INJECTION: a VPC network that overlaps the k3s pod or Service
#      network must be REFUSED, with nothing installed. This is the
#      2026-08-22 acceptance failure (#3956): k3s's default pod network and
#      the substrate's default VPC were the same /16, the CNI shadowed the VPC
#      resolver, CoreDNS forwarded to itself and its loop guard killed it, and
#      the deploy failed 95 minutes later on "Ollama did not become ready in
#      time". This runner is not inside a VPC, so the condition is injected
#      through the bootstrap's own NYXGPT_VPC_CIDRS override -- without that,
#      a green run here is structurally blind to the defect (the V-032 class).
#      Then execute the deploy's own k3s bootstrap for real.
#   2  Assert the access surface: nothing on 0.0.0.0, no ingress controller,
#      no LoadBalancer implementation, `local-path` still the default class --
#      and the networks the RUNNING cluster cut, with CoreDNS Available at 0
#      restarts and no `plugin/loop` in its log.
#   3  Apply `k8s/` UNCHANGED against the real k3s API server, and prove the
#      files were not edited to get there (#3506's cluster-flavor-agnostic
#      premise).
#   4  FAULT INJECTION: a locally-built image is invisible to k3s until it is
#      imported. Prove the Pod fails without `_k3s_import_image`, and runs
#      with it. This is the defect that would otherwise have shipped as a
#      green install and a stack of ImagePullBackOff Pods.
#   5  The access bridge, end to end: the systemd --user unit the deploy
#      installs -> `nyxgpt ops port-forward` -> the ClusterIP Service ->
#      a Pod -> 127.0.0.1:8000 on the host, which is what the SSH tunnel
#      forwards to.
#   6  FAULT INJECTION: stop the bridge and prove 127.0.0.1:8000 goes dead --
#      i.e. that step 5 measured the bridge and not something else.
#   7  FAULT INJECTION: a real rollout's leftover Pod. This is the 2026-08-26
#      acceptance blocker (#3956): the GlitchTip DSN write rolls api/web, and
#      the retired ReplicaSet's terminated Pod failed the whole install three
#      lines above `nyxgpt-web-stable 1/1` -- before the access bridge was
#      installed, so the feature could not produce a reachable deployment. Both
#      halves: the unfiltered reading must fail on the corpse, the product's
#      must not, and a Failed Pod of the CURRENT ReplicaSet must still fail
#      (the narrow fix the owner ruled out). All THREE readers of a Pod list
#      are measured against the same live corpse -- the install, self-heal's
#      component list (where it rendered as a permanently Failed component of a
#      healthy deployment) and canary's per-track reason enrichment.
#   8  FAULT INJECTION: `kubectl` on a k3s node is a symlink to `k3s`, whose
#      shim defaults KUBECONFIG to a root-only file -- which is why `cloud
#      canary status` reported "native mode" on a live cluster. The shim is
#      injected against the real root-only kubeconfig, and the product must
#      still read `kubernetes`; a probe that cannot reach an API server must
#      report `unknown` rather than a confident `native`.
#   9  The applied image tags: per-build-path and versioned, through a generated
#      kustomize overlay that kubectl's embedded kustomize has to accept and the
#      API server has to admit -- with `k8s/` still byte-identical.
#  10  FAULT INJECTION: host-side probes must not answer FOR the cluster
#      (#4137). A k3s host is neither in-cluster nor Compose, so a gate written
#      as "am I inside a Pod?" cannot see it.
#  11  FAULT INJECTION: a Pod must be able to report its own cloud deployment
#      (#4138) -- the inverse vantage point. With nothing recorded, an
#      in-cluster read of this live cluster must say UNKNOWN (which is both the
#      defect the owner photographed and #3804's case, so neither can regress
#      unnoticed); after the product's own writer records the instance into the
#      cluster, both status surfaces must report it, the real API server must
#      agree that the `nyxgpt-api` ServiceAccount may read that one ConfigMap
#      read-only and no other, an off-cluster read of the same cluster must
#      still say UNKNOWN, and deleting the record must take the answer with it.
#  12  FAULT INJECTION: a DECLARED substrate, before anything answers (#4184).
#      Step 10's host answers for a cluster that is UP; this is the other half
#      of the class and the half the rc1 report is about -- during the deploy
#      nothing answers yet, so inference falls back to the host and the output
#      carried a Compose file, a native install mode and a restart command for
#      an api that did not exist. Asserts their ABSENCE, which is the criterion.
#  13  FAULT INJECTION: the instance's own sshd must offer a post-quantum key
#      exchange (#4184). This runner already does by default, so a classical-
#      only drop-in is injected first -- both to reproduce the owner's
#      condition and to prove the product REPORTS a drop-in that is parsed and
#      overridden rather than announcing success it did not get.
#  14  The `--no-kubernetes` transition, against the live cluster and bridge
#      the steps above built: the native section really stops and removes the
#      bridge, frees 8000, uninstalls k3s and frees 6443 -- and a second pass
#      on a box with none of them is a no-op, which every first deploy runs.
#
# Usage:
#   ./scripts/k3s-cloud-smoke.sh              # full run, tears the cluster down
#   ./scripts/k3s-cloud-smoke.sh --keep-up    # leave k3s installed afterwards

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
CHECKOUT="$PWD"

KEEP_UP=0
for arg in "$@"; do
  case "$arg" in
    --keep-up) KEEP_UP=1 ;;
    -h|--help) echo "Usage: $0 [--keep-up]"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { echo "[k3s-cloud-smoke] $*"; }
step() { echo; echo "[k3s-cloud-smoke] ===== $* ====="; }
fail() { echo "[k3s-cloud-smoke] ERROR: $*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || fail "required tool not found: $1"; }
require curl
require docker
require ss
require systemctl

[[ "$(uname -s)" == "Linux" ]] || fail "k3s is Linux-only -- run this on Linux"

NAMESPACE=nyxgpt
PROBE_IMAGE="nyxgpt-k3s-import-probe:local"
WORK="$(mktemp -d)"

cleanup() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo
    log "--- diagnostics ---"
    sudo systemctl status k3s --no-pager -l 2>&1 | tail -40 || true
    kubectl get nodes -o wide 2>&1 || true
    kubectl get pods -A -o wide 2>&1 || true
    kubectl -n "$NAMESPACE" describe pods 2>&1 | tail -80 || true
    systemctl --user status 'nyxgpt-k8s-bridge@*' --no-pager -l 2>&1 | tail -40 || true
    journalctl --user -u 'nyxgpt-k8s-bridge@api.service' --no-pager -n 50 2>&1 || true
  fi
  systemctl --user stop 'nyxgpt-k8s-bridge@api.service' >/dev/null 2>&1 || true
  systemctl --user disable 'nyxgpt-k8s-bridge@api.service' >/dev/null 2>&1 || true
  if [[ $KEEP_UP -eq 0 ]]; then
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=60s >/dev/null 2>&1 || true
    if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
      log "Uninstalling k3s"
      sudo /usr/local/bin/k3s-uninstall.sh >/dev/null 2>&1 || true
    fi
  fi
  rm -rf "$WORK"
  exit $rc
}
trap cleanup EXIT

# `systemctl --user` needs a reachable D-Bus session bus. Same bootstrap as
# scripts/systemd-native-smoke.sh, and the same one the provisioning script
# performs on the instance.
if ! systemctl --user status >/dev/null 2>&1; then
  log "No systemd --user session detected; enabling lingering for $(whoami)"
  sudo loginctl enable-linger "$(whoami)" || true
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  systemctl --user status >/dev/null 2>&1 \
    || fail "systemctl --user still unreachable after enabling lingering"
fi

# ---------------------------------------------------------------------------
step "1/14  Execute the deploy's own k3s bootstrap"
# ---------------------------------------------------------------------------
python3 - > "$WORK/k3s-bootstrap.sh" <<'PY'
from nyxgpt.cloud_deploy import render_k3s_bootstrap

print(render_k3s_bootstrap())
PY

log "Bootstrap text (as a --kubernetes deploy sends it):"
sed 's/^/    | /' "$WORK/k3s-bootstrap.sh"

# --- FAULT INJECTION: the VPC/pod-network collision ------------------------
# The defect that failed owner acceptance on 2026-08-22 (#3956). k3s's default
# pod network is 10.42.0.0/16 and the substrate VPC default WAS the same
# block, so the CNI shadowed the VPC resolver at 10.42.0.2, CoreDNS forwarded
# to itself, its loop guard killed it, and the deploy failed 95 minutes later
# on "Ollama did not become ready in time".
#
# This runner is not inside a VPC, so the collision cannot arise here on its
# own -- which is exactly why the merged job was structurally blind to it (the
# V-032 class). The condition is therefore INJECTED: NYXGPT_VPC_CIDRS is the
# override the bootstrap reads in place of the IMDS lookup, and the bootstrap
# must refuse a colliding value outright and install nothing.
K3S_CLUSTER_CIDR="$(python3 -c 'from nyxgpt import cloud_deploy; print(cloud_deploy.K3S_CLUSTER_CIDR)')"
K3S_SERVICE_CIDR="$(python3 -c 'from nyxgpt import cloud_deploy; print(cloud_deploy.K3S_SERVICE_CIDR)')"
log "MEASURED: the shipped k3s networks are $K3S_CLUSTER_CIDR (pods) and $K3S_SERVICE_CIDR (Services)"

for colliding in "$K3S_CLUSTER_CIDR" "$K3S_SERVICE_CIDR" "100.64.0.0/10"; do
  log "FAULT INJECTION: a VPC on $colliding must be refused"
  set +e
  NYXGPT_VPC_CIDRS="$colliding" bash "$WORK/k3s-bootstrap.sh" > "$WORK/refusal.log" 2>&1
  rc=$?
  set -e
  sed 's/^/    | /' "$WORK/refusal.log"
  [[ $rc -ne 0 ]] \
    || fail "the bootstrap accepted a VPC network of $colliding -- the cluster it would
             install has DNS that cannot work, and the deploy fails minutes later on an
             unrelated readiness timeout (#3956)"
  grep -q "Refusing to install the cluster" "$WORK/refusal.log" \
    || fail "the bootstrap exited non-zero on a colliding VPC but did not say why"
  if command -v k3s >/dev/null 2>&1; then
    fail "the bootstrap installed k3s despite refusing the network overlap"
  fi
done
log "PASS: an overlapping VPC network is refused, and nothing is installed"

# `set -euo pipefail` and the IMDS fallback are both properties of the text
# itself, so it is run as-is under bash rather than sourced into this shell.
#
# NYXGPT_VPC_CIDRS is set to the SUBSTRATE DEFAULT (terraform/aws/variables.tf)
# rather than left unset: on a real instance IMDS always answers, so the
# unset path is the runner's fallback and not a deploy's. Setting it proves
# the shipped pair -- the substrate's VPC and the pinned k3s networks -- passes
# the guard it now has to pass, which is the combination owner acceptance runs.
VPC_CIDR="$(awk '/variable "vpc_cidr"/,/^}/' terraform/aws/variables.tf \
  | awk -F'"' '/default/ {print $2; exit}')"
[[ -n "$VPC_CIDR" ]] || fail "could not read vpc_cidr's default out of terraform/aws/variables.tf"
log "MEASURED: the substrate VPC default is $VPC_CIDR"
NYXGPT_VPC_CIDRS="$VPC_CIDR" bash "$WORK/k3s-bootstrap.sh"

export KUBECONFIG="$HOME/.kube/config"
[[ -f "$KUBECONFIG" ]] || fail "the bootstrap did not write $KUBECONFIG"
# `server: https://10.1.1.137:6443` -- the `//` in the scheme separator yields
# two EMPTY fields under a `[/:]` split, so the address is not the field the
# naive count says it is. `+` collapses each run of separators into one, which
# makes the field index say what it means: scheme, host, port.
NODE_IP="$(awk -F'[/:]+' '/server:/ {print $3; exit}' "$KUBECONFIG")"
[[ -n "$NODE_IP" ]] || fail "could not read the API server address out of $KUBECONFIG"
log "MEASURED: the kubeconfig points at https://${NODE_IP}:6443"

# ---------------------------------------------------------------------------
step "2/14  The access surface: #3503 says nothing but TCP 22"
# ---------------------------------------------------------------------------
log "MEASURED: listeners on 6443:"
ss -ltnH 'sport = :6443' | sed 's/^/    | /'

if ss -ltnH 'sport = :6443' | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):6443$'; then
  fail "the k3s apiserver is listening on every interface -- on an EC2 instance that is the
        public NIC, and #3503's access model is that nothing but TCP 22 is reachable"
fi
# -F: the address is an IPv4 literal, so its dots are data, not regex.
ss -ltnH 'sport = :6443' | awk '{print $4}' | grep -qF "${NODE_IP}:6443" \
  || fail "nothing is listening on ${NODE_IP}:6443, which is what the kubeconfig points at"
log "PASS: the apiserver is bound to the node's private address only"

# Traefik binds host ports 80/443 and is k3s's default ingress controller;
# servicelb is what makes a `Service: LoadBalancer` provision anything. #3506's
# premise is that the manifests need neither.
# The bootstrap returns as soon as the NODE reports Ready, and k3s's deploy
# controller applies the bundled addons after that -- measured at ~3s on this
# runner. Every assertion below is about what that controller did or did not
# apply, so waiting for its evidence is what makes them mean anything: check
# them at node-Ready and "traefik is not running" is true because *nothing* is
# running yet. `local-path` is the addon that must survive, so its arrival is
# both the property under test and the barrier for the negative ones.
log "Waiting up to 90s for k3s's addon deployer (the local-path StorageClass)"
default_sc=""
for _ in $(seq 1 30); do
  # The single-object form, not a filter expression over `.items[?(...)]`:
  # kubectl's jsonpath does not reliably escape a dotted, slashed annotation
  # key inside a filter, and it answers "" for the missing and the mistyped
  # alike -- which is how the previous spelling of this key (`default-class`,
  # for the real `is-default-class`) read as "not the default" on a cluster
  # where it plainly was.
  default_sc="$(kubectl get storageclass local-path \
    -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' \
    2>/dev/null || true)"
  [[ "$default_sc" == "true" ]] && break
  sleep 3
done
log "MEASURED: StorageClasses after the addon deployer ran:"
kubectl get storageclass 2>&1 | sed 's/^/    | /'

# local-storage IS still enabled, because the Cassandra and Ollama
# StatefulSets bind through whatever the default StorageClass is.
kubectl get storageclass local-path >/dev/null 2>&1 \
  || fail "the local-path StorageClass is gone -- every volumeClaimTemplate in k8s/ would
           sit Pending on an unbound PVC"
[[ "$default_sc" == "true" ]] \
  || fail "local-path is not the default StorageClass (is-default-class=${default_sc:-unset})"
log "PASS: local-path is present and is the default StorageClass"

# ...and now that the deploy controller has demonstrably run, the absence of
# these two is evidence rather than a head start.
for unwanted in traefik svclb; do
  if kubectl get pods -A --no-headers 2>/dev/null | grep -q "$unwanted"; then
    kubectl get pods -A | sed 's/^/    | /'
    fail "$unwanted is running -- the bootstrap's --disable flags did not take"
  fi
done
log "PASS: no ingress controller and no LoadBalancer implementation are installed"

# The networks the RUNNING cluster actually cut, not the flags it was asked
# for: `--cluster-cidr` accepted and silently overridden would read identical
# in the unit tests and be the same outage (#3956).
#
# Bounded wait, not an instant read: `node.spec.podCIDR` is filled in by the
# controller-manager's node-ipam controller AFTER the node registers, so a read
# taken the moment the node reports Ready is a race the bootstrap cannot be
# blamed for (measured unset on k3s v1.36.5 here, seconds after Ready).
POD_CIDR=""
for _ in $(seq 1 30); do
  POD_CIDR="$(kubectl get nodes -o jsonpath='{.items[0].spec.podCIDR}')"
  [[ -n "$POD_CIDR" ]] && break
  sleep 2
done
log "MEASURED: the node's pod CIDR is ${POD_CIDR:-<unset>}"
python3 - "$POD_CIDR" "$K3S_CLUSTER_CIDR" <<'PY'
import ipaddress
import sys

pod, pinned = sys.argv[1], sys.argv[2]
assert pod, "the node reports no podCIDR"
assert ipaddress.ip_network(pinned).supernet_of(ipaddress.ip_network(pod)), (
    f"the cluster cut its pod network from {pod}, not from the pinned {pinned}"
)
print(f"    | PASS: {pod} is inside the pinned {pinned}")
PY

# kube-dns's ClusterIP is allocated out of --service-cidr, so it is the
# cheapest live measurement of where the Service network really is.
DNS_IP="$(kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')"
log "MEASURED: the cluster DNS Service address is ${DNS_IP:-<unset>}"
python3 - "$DNS_IP" "$K3S_SERVICE_CIDR" "$VPC_CIDR" <<'PY'
import ipaddress
import sys

dns, pinned, vpc = sys.argv[1], sys.argv[2], sys.argv[3]
assert dns, "kube-dns has no ClusterIP"
assert ipaddress.ip_address(dns) in ipaddress.ip_network(pinned), (
    f"cluster DNS landed on {dns}, outside the pinned Service network {pinned}"
)
assert not ipaddress.ip_network(pinned).overlaps(ipaddress.ip_network(vpc)), (
    f"the Service network {pinned} overlaps the substrate VPC {vpc}"
)
print(f"    | PASS: {dns} is inside the pinned {pinned}, which does not overlap the VPC {vpc}")
PY

# The failure signature itself. On the owner's instance CoreDNS logged
# `[FATAL] plugin/loop: Loop (10.42.0.2:40206 -> :53) detected for zone "."`
# and CrashLoopBackOffed 23 times in 95 minutes. Asserting on Ready alone
# would pass on a Pod that has been killed and restarted twenty times.
log "Waiting up to 120s for CoreDNS to become Available"
# The addon deployer applies coredns and local-path independently, so the
# StorageClass arriving is not proof the Deployment object exists yet;
# `rollout status` on a missing object fails immediately rather than waiting.
for _ in $(seq 1 30); do
  kubectl -n kube-system get deploy coredns >/dev/null 2>&1 && break
  sleep 2
done
kubectl -n kube-system rollout status deploy/coredns --timeout=120s \
  || fail "CoreDNS never became available -- the exact shape of the #3956 acceptance failure"
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide | sed 's/^/    | /'
COREDNS_RESTARTS="$(kubectl -n kube-system get pods -l k8s-app=kube-dns \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
  | awk '{ total += $1 } END { print total + 0 }')"
log "MEASURED: CoreDNS restart count is $COREDNS_RESTARTS"
[[ "$COREDNS_RESTARTS" -eq 0 ]] \
  || fail "CoreDNS has restarted $COREDNS_RESTARTS time(s) -- it is Available but not healthy"
if kubectl -n kube-system logs -l k8s-app=kube-dns --tail=200 2>/dev/null | grep -q "plugin/loop"; then
  kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50 | sed 's/^/    | /'
  fail "CoreDNS logged a resolver loop -- its upstream resolves back to itself"
fi
log "PASS: CoreDNS is Available with 0 restarts and no resolver loop"

# ---------------------------------------------------------------------------
step "3/14  k8s/*.yaml applies to k3s UNCHANGED"
# ---------------------------------------------------------------------------
# Through the product's own resource sync and secret bootstrap, not a
# hand-rolled copy: what a deploy applies is the PACKAGED manifests under
# ~/.nyxGPT/k8s (#3834), so that is what has to be proved applicable.
python3 - <<'PY'
from nyxgpt import ops

for label, results in (
    ("sync packaged resources", ops._sync_packaged_resources()),
    ("secret bootstrap", ops._ensure_k8s_secret("smoke-api-key")),
):
    for r in results:
        print(f"    | [{'OK' if r.ok else 'FAIL'}] {label}: {r.message}")
    assert all(r.ok for r in results), label
print(f"    | K8S_DIR={ops.K8S_DIR}")
PY

K8S_DIR="$HOME/.nyxGPT/k8s"

# The namespace, for real and FIRST -- a property of the DRY RUN, not of the
# manifests. What a deploy runs is `kubectl apply -k` for real
# (`ops._kubectl_apply_kustomization`), where kubectl creates the Namespace
# before the objects that declare themselves into it, so one pass suffices. A
# server-side dry run creates nothing, so that namespace never comes into
# existence and every namespaced object is rejected with `namespaces "nyxgpt"
# not found` -- an error that reads as "the manifests do not apply to k3s" and
# is really "nothing can be validated against a namespace that was not made".
kubectl apply -f "$K8S_DIR/namespace.yaml" | sed 's/^/    | /'

# A server-side dry run is then the strong, cheap form of "these manifests
# apply": the real API server validates, defaults and admits every object, and
# nothing is created -- so the job does not spend ten minutes pulling the
# Cassandra and Ollama images to learn what admission already answered. Whether
# those Pods then become Ready is k8s-local-smoke.yml's question, on a real
# cluster with real builds; this job's question is the k3s delta.
kubectl apply -k "$K8S_DIR" --dry-run=server -o name | sed 's/^/    | /'
log "PASS: every object in k8s/ is accepted by the k3s API server as written"

# "Unchanged" is a claim about the FILES, so check the files. If making the
# cloud target work had needed a manifest edit, that is a finding about
# #3506's premise and belongs in the issue, not in a quiet diff.
git -C "$CHECKOUT" diff --exit-code -- k8s/ \
  || fail "k8s/ was modified -- #3506's rationale rests on the manifests being
           cluster-flavor-agnostic, so a required edit is a finding about the decision"
# secret.yaml is generated per-machine and never committed; everything else
# the deploy applies must be byte-identical to the repository's copy.
diff -r --exclude=secret.yaml "$CHECKOUT/k8s" "$K8S_DIR" \
  || fail "the manifests the deploy applies differ from the repository's k8s/"
log "PASS: the applied manifests are byte-identical to k8s/ (secret.yaml aside)"

# The Services, really created this time -- they are free (no Pods, no pulls)
# and they are what the LoadBalancer assertion and the bridge below need.
for svc in service.yaml service-canary.yaml service-web.yaml service-web-canary.yaml \
           service-cassandra.yaml service-ollama.yaml; do
  kubectl apply -n "$NAMESPACE" -f "$K8S_DIR/$svc" >/dev/null
done
log "MEASURED: Service types in the nyxgpt namespace:"
kubectl -n "$NAMESPACE" get svc -o custom-columns=NAME:.metadata.name,TYPE:.spec.type --no-headers \
  | sed 's/^/    | /'
if kubectl -n "$NAMESPACE" get svc -o jsonpath='{.items[*].spec.type}' | grep -Eq 'LoadBalancer|NodePort'; then
  fail "a Service asks for a LoadBalancer or NodePort -- #3503 allows no port but 22"
fi
log "PASS: every Service is ClusterIP"

# ---------------------------------------------------------------------------
step "4/14  FAULT INJECTION: a docker-built image is invisible to k3s"
# ---------------------------------------------------------------------------
# k3s runs its own containerd with its own image store, and every Deployment in
# k8s/ pins `imagePullPolicy: IfNotPresent` against a `:local` tag that exists
# in no registry. Before #3956, `_build_and_load_k8s_image` reported
# "unrecognized cluster context -- skipped image load" as a SUCCESS here, so
# the install went green and every Pod sat in ImagePullBackOff.
mkdir -p "$WORK/probe/www"
cat > "$WORK/probe/www/health" <<'JSON'
{"status":"ok","source":"k3s-cloud-smoke probe"}
JSON
cat > "$WORK/probe/Dockerfile" <<'DOCKERFILE'
FROM busybox:1.36
COPY www /www
EXPOSE 8000
CMD ["httpd", "-f", "-p", "8000", "-h", "/www"]
DOCKERFILE
docker build -q -t "$PROBE_IMAGE" "$WORK/probe" >/dev/null
log "Built $PROBE_IMAGE -- it exists in docker's store and in no registry"

probe_pod() {
  # $1: pod name. Carries the nyxgpt-api Service's own selector and port name,
  # so step 5 can reach it through the unmodified Service.
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $NAMESPACE
  labels:
    app: nyxgpt-api-canary-pool
spec:
  containers:
    - name: probe
      image: $PROBE_IMAGE
      imagePullPolicy: IfNotPresent
      ports:
        - name: http
          containerPort: 8000
YAML
}

probe_pod import-probe-before | kubectl apply -f - >/dev/null
log "Waiting up to 90s for the pre-import Pod to fail (it must not start)"
before_state=""
for _ in $(seq 1 18); do
  before_state="$(kubectl -n "$NAMESPACE" get pod import-probe-before \
    -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)"
  case "$before_state" in
    ErrImagePull|ImagePullBackOff) break ;;
  esac
  if [[ "$(kubectl -n "$NAMESPACE" get pod import-probe-before \
        -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]]; then
    fail "the Pod started WITHOUT the image being imported -- this fault injection proves
          nothing, and the import step it justifies cannot be trusted"
  fi
  sleep 5
done
[[ "$before_state" == "ErrImagePull" || "$before_state" == "ImagePullBackOff" ]] \
  || fail "expected ErrImagePull/ImagePullBackOff without the import, got '${before_state:-none}'"
log "PASS (defect reproduced): without the import the Pod is $before_state"
kubectl -n "$NAMESPACE" delete pod import-probe-before --now >/dev/null

# Now the fix -- the product's own code path, not a hand-typed `ctr import`.
python3 - <<PY
from nyxgpt import ops

results = ops._k3s_import_image("$PROBE_IMAGE")
for r in results:
    print(f"    | [{'OK' if r.ok else 'FAIL'}] {r.message}")
assert all(r.ok for r in results), "the import step failed"
PY

probe_pod import-probe-after | kubectl apply -f - >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/import-probe-after --timeout=120s \
  || fail "the Pod still did not start after _k3s_import_image -- the import did not take"
log "PASS (fix proven): after _k3s_import_image the same Pod runs"

# ---------------------------------------------------------------------------
step "5/14  The access bridge, end to end"
# ---------------------------------------------------------------------------
# `k8s/`'s Services are ClusterIP-only, so nothing binds 127.0.0.1:8000 on the
# instance the way the native services do -- and the SSH tunnel forwards to
# the instance's loopback. Without this bridge a --kubernetes deploy installs a
# perfectly healthy stack and then fails its own health check.
#
# The unit text is not retyped here: it is lifted out of the rendered
# provisioning script, so what runs is what a deploy installs.
python3 - > "$WORK/bridge.sh" <<'PY'
from nyxgpt.cloud_deploy import DeployPlan, render_provision_script

script = render_provision_script(DeployPlan(version="0.0.0", kubernetes=True))
start = script.index("mkdir -p \"$HOME/.config/systemd/user\"")
end = script.index("systemctl --user enable --now nyxgpt-k8s-bridge@web.service")
print(script[start:end])
PY

# The unit's ExecStart is the instance's venv path. On a runner nyxgpt lives on
# PATH instead, so the venv path is pointed at it -- which keeps the UNIT text
# under test rather than rewriting the thing being verified.
mkdir -p "$HOME/.nyxGPT/venv/bin"
ln -sf "$(command -v nyxgpt)" "$HOME/.nyxGPT/venv/bin/nyxgpt"
# The extracted block writes the unit, reloads, and enables the api instance --
# the web and observability instances are outside the slice, since this cluster
# has no web Pods to forward to.
bash "$WORK/bridge.sh"

log "Waiting up to 60s for 127.0.0.1:8000 to answer through the bridge"
bridged=""
for _ in $(seq 1 20); do
  if bridged="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null)"; then
    break
  fi
  sleep 3
done
[[ -n "$bridged" ]] \
  || fail "127.0.0.1:8000 never answered -- the bridge did not connect the tunnel's
           loopback endpoint to the ClusterIP Service, so a --kubernetes deploy would
           fail its own health check with every Pod healthy"
log "MEASURED: 127.0.0.1:8000/health -> $bridged"
log "PASS: systemd --user unit -> nyxgpt ops port-forward -> ClusterIP Service -> Pod"

# ---------------------------------------------------------------------------
step "6/14  FAULT INJECTION: the bridge is what was measured"
# ---------------------------------------------------------------------------
# Without this, step 5 would pass on any runner where something else happened
# to be listening on 8000.
systemctl --user stop nyxgpt-k8s-bridge@api.service
sleep 3
if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  fail "127.0.0.1:8000 still answers with the bridge stopped -- step 5 measured something
        other than the bridge"
fi
log "PASS: with the bridge stopped, 127.0.0.1:8000 is dead"

# ---------------------------------------------------------------------------
step "7/14  FAULT INJECTION: a corpse from a finished rollout fails the install"
# ---------------------------------------------------------------------------
# The 2026-08-26 acceptance blocker (#3956). A `--kubernetes` deploy applies
# `k8s/` (whose ConfigMap carries the placeholder error-tracking DSN), brings
# the stack up, then provisions GlitchTip -- which writes the real DSN and rolls
# api/web onto a new pod template. The pre-DSN ReplicaSet is scaled to zero and
# a terminated Pod of its is left in the namespace, and `_k8s_stack_health`
# failed the whole install on it, three lines above `nyxgpt-web-stable 1/1`.
# The deploy then exited before installing the access bridge, so the feature
# could not produce a reachable deployment at all.
#
# A unit test can assert the reading; only a real cluster can show that the
# state exists and that Kubernetes leaves it there. So this builds it for real:
# a Deployment, a rollout that supersedes its ReplicaSet, and a Failed Pod
# adopted by the retired one.
#
# The Pod label is a REAL core one (`self_heal.K8S_CORE_POD_APPS`) rather than
# the Deployment's own name, because three readers have to be shown dropping
# this Pod and one of them selects by that label: the first cut of this fix
# covered the install alone, and the same corpse went on rendering on the
# Self-Heal dashboard as a Failed, unhealable component of a healthy
# deployment. A stand-in label would have exercised the filter while skipping
# the tier classification that puts the Pod on that page at all.
ROLLOUT=smoke-rollout
ROLLOUT_LABEL=nyxgpt-web-canary-pool
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $ROLLOUT
  namespace: $NAMESPACE
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $ROLLOUT_LABEL
  template:
    metadata:
      labels:
        app: $ROLLOUT_LABEL
    spec:
      containers:
        - name: probe
          image: $PROBE_IMAGE
          imagePullPolicy: IfNotPresent
YAML
kubectl -n "$NAMESPACE" rollout status "deploy/$ROLLOUT" --timeout=120s | sed 's/^/    | /'
OLD_RS="$(kubectl -n "$NAMESPACE" get rs -l "app=$ROLLOUT_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"
OLD_RS_UID="$(kubectl -n "$NAMESPACE" get "rs/$OLD_RS" -o jsonpath='{.metadata.uid}')"

# The rollout the DSN write performs, in the one way that matters here: a new
# pod template, so a new ReplicaSet, so the old one is scaled to zero.
kubectl -n "$NAMESPACE" set env "deploy/$ROLLOUT" ROLLED=1 >/dev/null
kubectl -n "$NAMESPACE" rollout status "deploy/$ROLLOUT" --timeout=120s | sed 's/^/    | /'
retired="$(kubectl -n "$NAMESPACE" get "rs/$OLD_RS" -o jsonpath='{.spec.replicas}')"
[[ "$retired" == "0" ]] \
  || fail "the superseded ReplicaSet $OLD_RS still wants $retired replica(s) -- this step
           is not reproducing a finished rollout"
log "MEASURED: $OLD_RS is retired (0 desired) after the rollout"

# The corpse. Created WITHOUT the owner reference and patched once it is already
# terminal, deliberately: a ReplicaSet scaled to zero deletes any *active* Pod
# it owns, and only ignores the terminal ones -- which is exactly why these
# survive on a real deployment, and exactly what would make this step flaky if
# the Pod were adopted while still starting.
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $ROLLOUT-corpse
  namespace: $NAMESPACE
  labels:
    app: $ROLLOUT_LABEL
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $PROBE_IMAGE
      imagePullPolicy: IfNotPresent
      command: ["false"]
YAML
corpse_phase=""
for _ in $(seq 1 24); do
  corpse_phase="$(kubectl -n "$NAMESPACE" get "pod/$ROLLOUT-corpse" \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "$corpse_phase" == "Failed" ]] && break
  sleep 5
done
[[ "$corpse_phase" == "Failed" ]] \
  || fail "the corpse Pod never reached phase Failed (got '${corpse_phase:-none}') -- without
           it this step proves nothing"
kubectl -n "$NAMESPACE" patch "pod/$ROLLOUT-corpse" --type=merge -p "$(cat <<JSON
{"metadata":{"ownerReferences":[{"apiVersion":"apps/v1","kind":"ReplicaSet",
"name":"$OLD_RS","uid":"$OLD_RS_UID"}]}}
JSON
)" >/dev/null
log "MEASURED: a Failed Pod owned by the retired $OLD_RS, as the owner found it"

python3 - <<PY
import json
import sys

from nyxgpt import ops

namespace = "$NAMESPACE"
corpse = "$ROLLOUT-corpse"
raw = json.loads(
    ops._run(["kubectl", "-n", namespace, "get", "pods", "-o", "json"], check=False).stdout
)

# --- the pre-#3956 reading: every Pod in the namespace, owner ignored. This is
# --- literally the old body of _k8s_pod_states.
before = [ops._classify_k8s_pod(p) for p in raw["items"]]
failed_before = [s.name for s in before if not s.ok]
print(f"    | without the fix, FAILED pods: {failed_before}")
if corpse not in failed_before:
    sys.exit(
        "FAULT INJECTION FAILED: the unfiltered reading does not fail on the corpse, so "
        "this step cannot prove the filter does anything"
    )

# --- the product's reading.
states, read_failure = ops._k8s_pod_states(namespace)
assert read_failure is None, read_failure
names = [s.name for s in states]
print(f"    | with the fix, pods considered: {names}")
if corpse in names:
    sys.exit(f"{corpse} is still part of the deployment's state")
if any(s.name == corpse for s in ops._k8s_blocked_pods(namespace, selector="app=$ROLLOUT_LABEL")):
    sys.exit("the rollout wait would still fast-fail on the corpse")

# ...and the Pod that IS current is still reported, so the filter did not just
# empty the report.
if not any(n.startswith("$ROLLOUT-") and n != corpse for n in names):
    sys.exit("the current ReplicaSet's Pod went missing too -- the filter is too wide")

print("    | the retired ReplicaSet's Pod is out, the current one's Pod is in")
PY

# The same corpse, read by the OTHER two readers of a Pod list. The install was
# the only one fixed in the first cut of this, and this Pod then rendered on the
# Self-Heal dashboard as a Failed, `healable=False` component of a deployment
# whose Deployments are both 1/1 -- so each reader is measured here, against the
# cluster state the step above built, rather than trusted to the shared helper.
python3 - <<PY
import sys

from nyxgpt import canary, self_heal

corpse = "$ROLLOUT-corpse"

# The injection: the rule switched off, which is exactly the reading self-heal
# had before this round (the filter is the only difference in that function).
real_rule = self_heal.pod_is_retired
self_heal.pod_is_retired = lambda *_a, **_k: False
before = self_heal._list_kubernetes_component_status(set())
print(f"    | without the rule, self-heal reports: {[(c.service, c.state, c.healable) for c in before]}")
if not any(c.service == corpse and not c.healthy for c in before):
    sys.exit(
        "FAULT INJECTION FAILED: the unfiltered self-heal reading does not report the corpse, "
        "so this step cannot prove the filter does anything there"
    )
self_heal.pod_is_retired = real_rule

after = self_heal._list_kubernetes_component_status(set())
names = [c.service for c in after]
print(f"    | with the rule, self-heal reports: {names}")
if corpse in names:
    sys.exit(
        "the Self-Heal dashboard still shows the corpse as a component of a healthy deployment"
    )
if not names:
    sys.exit("every component vanished -- the filter is too wide")

# canary's per-track reason enrichment selects by the same label the corpse
# carries, so an unhealthy track could be 'explained' by the rollout before it.
# Injected the same way, so an empty list below is evidence rather than a Pod
# that simply had nothing to say.
real_rule = canary.pod_is_retired
canary.pod_is_retired = lambda *_a, **_k: False
unfiltered = canary.pod_failure_reasons("app=$ROLLOUT_LABEL", "$NAMESPACE")
print(f"    | without the rule, canary's reasons: {unfiltered}")
if not any(corpse in r for r in unfiltered):
    sys.exit(
        "FAULT INJECTION FAILED: the unfiltered canary reading does not blame the corpse, so "
        "this step cannot prove the filter does anything there"
    )
canary.pod_is_retired = real_rule

reasons = canary.pod_failure_reasons("app=$ROLLOUT_LABEL", "$NAMESPACE")
print(f"    | with the rule, canary's reasons: {reasons}")
if any(corpse in r for r in reasons):
    sys.exit("the corpse is still offered as the reason the track is unhealthy")
PY

# Same question, asked of the live cluster through the real patch rather than in
# Python: adopt the corpse onto the CURRENT ReplicaSet and the reading must fail
# again. Nothing about the phase changed; only its owner did.
CURRENT_RS="$(kubectl -n "$NAMESPACE" get rs -l "app=$ROLLOUT_LABEL" \
  -o jsonpath='{range .items[*]}{.metadata.name}={.spec.replicas}{"\n"}{end}' \
  | awk -F= '$2 != "0" {print $1; exit}')"
CURRENT_RS_UID="$(kubectl -n "$NAMESPACE" get "rs/$CURRENT_RS" -o jsonpath='{.metadata.uid}')"
kubectl -n "$NAMESPACE" patch "pod/$ROLLOUT-corpse" --type=merge -p "$(cat <<JSON
{"metadata":{"ownerReferences":[{"apiVersion":"apps/v1","kind":"ReplicaSet",
"name":"$CURRENT_RS","uid":"$CURRENT_RS_UID"}]}}
JSON
)" >/dev/null
python3 - <<PY
import sys

from nyxgpt import ops

states, _ = ops._k8s_pod_states("$NAMESPACE")
failing = [s.name for s in states if not s.ok]
print(f"    | owned by the CURRENT ReplicaSet, FAILED pods: {failing}")
if "$ROLLOUT-corpse" not in failing:
    sys.exit(
        "a Failed Pod of the current ReplicaSet was filtered out -- the fix would be "
        "hiding real failures, which is the narrow fix #3956 ruled out"
    )
# ...and it says WHY, which a bare 'pod NAME: Failed' line did not (the
# owner's second non-blocking note).
state = next(s for s in states if s.name == "$ROLLOUT-corpse")
print(f"    | and it reports: {state.summary}")
if state.summary.strip() == "Failed":
    sys.exit("the Failed line still carries no reason at all")
PY
kubectl -n "$NAMESPACE" delete "pod/$ROLLOUT-corpse" --now >/dev/null
kubectl -n "$NAMESPACE" delete "deploy/$ROLLOUT" --now >/dev/null
log "PASS: a Pod no live ReplicaSet owns is not the deployment's state, for the install,"
log "      self-heal and canary alike -- and one the current ReplicaSet owns still fails,"
log "      with its reason"

# ---------------------------------------------------------------------------
step "8/14  FAULT INJECTION: kubectl on a k3s node is not kubectl"
# ---------------------------------------------------------------------------
# The second 2026-08-26 blocker. `/usr/local/bin/kubectl` on a k3s node is a
# symlink to the `k3s` binary, whose shim defaults KUBECONFIG to the root-only
# /etc/rancher/k3s/k3s.yaml -- so the user-owned ~/.kube/config the deploy
# writes was never read, and `nyxgpt cloud canary status` reported "running in
# native mode" on a live cluster, telling the operator to install the thing that
# was already running.
#
# A hosted runner ships its own kubectl, so k3s's installer leaves it alone and
# the condition does not arise here by itself. It is INJECTED with a shim that
# does exactly what k3s's does -- against the real root-only kubeconfig this
# cluster really has.
[[ -f /etc/rancher/k3s/k3s.yaml ]] || fail "k3s wrote no kubeconfig to default to"
[[ -f "$HOME/.kube/config" ]] \
  || fail "the bootstrap did not write \$HOME/.kube/config -- the fix has nothing to find"

REAL_KUBECTL="$(command -v kubectl)"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/kubectl" <<SHIM
#!/bin/sh
# k3s's own kubectl shim, in one line: default KUBECONFIG to k3s's file.
KUBECONFIG="\${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}" export KUBECONFIG
exec "$REAL_KUBECTL" "\$@"
SHIM
chmod +x "$WORK/bin/kubectl"

if [[ $EUID -eq 0 ]]; then
  log "SKIPPED (running as root): /etc/rancher/k3s/k3s.yaml is readable here, so the"
  log "        permission-denied half cannot be reproduced. The fix is still asserted."
else
  if [[ -r /etc/rancher/k3s/k3s.yaml ]]; then
    fail "/etc/rancher/k3s/k3s.yaml is readable by $(whoami) -- the injection cannot
          reproduce the owner's condition"
  fi
  set +e
  shim_out="$(PATH="$WORK/bin:$PATH" env -u KUBECONFIG kubectl -n "$NAMESPACE" get pods 2>&1)"
  shim_rc=$?
  set -e
  [[ $shim_rc -ne 0 ]] \
    || fail "the k3s-style shim reached the cluster without KUBECONFIG -- the injection
             proves nothing"
  log "MEASURED (defect reproduced): kubectl's own default fails -- ${shim_out##*$'\n'}"
fi

# The fix: nyxGPT's own kubectl calls name the kubeconfig kubectl *would* have
# used, so they no longer depend on an environment variable nothing exports.
mode="$(PATH="$WORK/bin:$PATH" env -u KUBECONFIG python3 -c \
  'from nyxgpt import canary; print(canary.current_mode())')"
log "MEASURED: canary.current_mode() with no KUBECONFIG exported -> $mode"
[[ "$mode" == "kubernetes" ]] \
  || fail "the deployment mode reads as '$mode' on a box running k3s -- canary rollout,
           the capability #3506 chose this substrate for, would report itself absent"

# ...and the other half: a probe that genuinely cannot reach an API server must
# say so, not fall back to a confident "native".
cat > "$WORK/broken-kubeconfig.yaml" <<'KUBECONFIG'
apiVersion: v1
kind: Config
clusters:
  - name: nowhere
    cluster:
      server: https://127.0.0.1:1
contexts:
  - name: nowhere
    context:
      cluster: nowhere
      user: nobody
current-context: nowhere
users:
  - name: nobody
    user:
      token: smoke-not-a-real-token  # pragma: allowlist secret
KUBECONFIG
broken="$(KUBECONFIG="$WORK/broken-kubeconfig.yaml" python3 -c \
  'from nyxgpt import canary; print("|".join(canary._current_mode_with_reason()))')"
log "MEASURED: with an unreachable cluster configured -> $broken"
[[ "${broken%%|*}" == "unknown" ]] \
  || fail "a failed probe still reports '${broken%%|*}' -- an assertion about the substrate
           that nothing checked"
log "PASS: the product finds the kubeconfig kubectl would have, and a probe that could"
log "      not ask never answers 'native'"

# ---------------------------------------------------------------------------
step "9/14  The applied image tags name the build and the version"
# ---------------------------------------------------------------------------
# The third 2026-08-26 blocker: four build paths shared `nyxgpt-api:local` /
# `nyxgpt-web:local`, so an instance running published 3.0.0rc14 reported its
# images as `local` and `nyxgpt canary status` -- which reads the version
# straight off the Pod's image tag -- could not name the release. The tags are
# now per-path and versioned, applied through a generated kustomize overlay.
#
# Only a real cluster can answer the part that matters here: that kubectl's
# EMBEDDED kustomize accepts the overlay and that the API server admits the
# whole set through it.
OVERLAY="$(python3 -c \
  'from nyxgpt import ops; print(ops._write_k8s_image_overlay(dev=False))')"
log "MEASURED: generated overlay at $OVERLAY"
sed 's/^/    | /' "$OVERLAY/kustomization.yaml"

EXPECTED_API_TAG="$(python3 -c \
  'from nyxgpt import ops; print(ops.k8s_image_refs(dev=False)["api"])')"
EXPECTED_WEB_TAG="$(python3 -c \
  'from nyxgpt import ops; print(ops.k8s_image_refs(dev=False)["web"])')"
case "$EXPECTED_API_TAG" in
  *:local) fail "the api image tag is still the mutable ':local' this step exists to retire" ;;
esac

kubectl kustomize "$OVERLAY" > "$WORK/rendered.yaml"
grep -q "image: $EXPECTED_API_TAG" "$WORK/rendered.yaml" \
  || fail "the rendered manifests do not carry $EXPECTED_API_TAG"
grep -q "image: $EXPECTED_WEB_TAG" "$WORK/rendered.yaml" \
  || fail "the rendered manifests do not carry $EXPECTED_WEB_TAG"
if grep -qE 'image: nyxgpt-(api|web):local' "$WORK/rendered.yaml"; then
  fail "a ':local' image survived the overlay -- canary status would report 'local' again"
fi
log "MEASURED: rendered image tags:"
grep -E '^ *image: nyxgpt-' "$WORK/rendered.yaml" | sort -u | sed 's/^/    | /'

# The real apply path, server-side validated: the overlay is what a deploy
# applies, so it has to be admissible, not merely renderable.
kubectl apply -k "$OVERLAY" --dry-run=server -o name | sed 's/^/    | /'
log "PASS: every object is admitted through the overlay, carrying the versioned tags"

# And the overlay must not have been bought by editing the manifests: #3506's
# rationale rests on `k8s/` being the repository's copy, byte for byte.
[[ "$OVERLAY" != "$K8S_DIR" && "$OVERLAY" != "$K8S_DIR"/* ]] \
  || fail "the generated overlay was written inside $K8S_DIR"
git -C "$CHECKOUT" diff --exit-code -- k8s/ \
  || fail "k8s/ was modified to make the image tags work -- that is a finding about
           #3506's premise, not a quiet diff"
diff -r --exclude=secret.yaml "$CHECKOUT/k8s" "$K8S_DIR" \
  || fail "the manifests the deploy applies differ from the repository's k8s/"
log "PASS: the manifests are still byte-identical to k8s/ (secret.yaml aside)"

# ---------------------------------------------------------------------------
step "10/14  FAULT INJECTION: host-side probes must not answer for the cluster"
# ---------------------------------------------------------------------------
# #4137, and it needs a live k3s host rather than unit fixtures because the
# whole defect is a property of this vantage point: a k3s host is NEITHER
# in-cluster NOR Compose. `kubectl` here reaches the cluster perfectly well
# while `docker compose ps` has nothing to answer about, so every gate written
# as "am I inside a Pod?" falls through to Compose and reports a Compose
# failure as the deployment's state. On the owner's instance:
#
#   Observability survey: CANNOT DETERMINE from here -- `docker compose ps`
#   exited 125 ... querying /home/ec2-user/.nyxGPT/docker-compose.yml
#    [OK] grafana-8686574f7b-d7vjf: state=Running health=ready
#    ... 14 Pods, all Running and ready ...
#
#   nyxGPT ops doctor: FAIL
#   - Missing local Cassandra container: nyxgpt-cassandra (run: nyxgpt ops install)
#
# with `cassandra-0: Running` three lines above the last one.
#
# The injection has two halves, and both are real conditions on this box:
#   (a) real Pods carrying the labels the product keys on -- the cheap probe
#       image imported in step 4, named and labelled as the cluster's own
#       workloads. What is being proved is a READING, so the Cassandra process
#       itself is not part of the claim; the Pod's identity in a real
#       namespace, read through the product's own kubectl path, is.
#   (b) an unanswerable Compose survey and no `nyxgpt-cassandra` container.
#       NYXGPT_COMPOSE_FILE points at a path that does not exist, so every
#       `docker compose ps` from these processes fails the way it failed on
#       that instance -- and the host Cassandra check is asserted to produce
#       its finding, so a regression to the host reading reinstates the FAIL.
cluster_pod() {
  # $1: pod name. $2..: `key: value` label lines.
  local name="$1"
  shift
  {
    cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $name
  namespace: $NAMESPACE
  labels:
YAML
    printf '    %s\n' "$@"
    cat <<YAML
spec:
  containers:
    - name: probe
      image: $PROBE_IMAGE
      imagePullPolicy: IfNotPresent
YAML
  } | kubectl apply -f - >/dev/null
}

# `cassandra-0` is the name Kubernetes gives a `statefulset/cassandra` replica,
# and `app: cassandra` is the label k8s/statefulset-cassandra.yaml carries --
# both are what the product matches on, so both are what this creates.
cluster_pod cassandra-0 'app: cassandra'
cluster_pod grafana-smoke 'tier: observability' 'app: grafana'
for pod in cassandra-0 grafana-smoke; do
  kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$pod" --timeout=120s \
    || fail "$pod never became Ready -- without a cluster that answers, this step
             cannot tell a routing fix from a silenced check"
done
log "MEASURED: the Pods the product will be asked about:"
kubectl -n "$NAMESPACE" get pods -o custom-columns=NAME:.metadata.name,PHASE:.status.phase \
  --no-headers | sed 's/^/    | /'

export NYXGPT_COMPOSE_FILE="$WORK/there-is-no-compose-stack-here/docker-compose.yml"
set +e
docker compose -f "$NYXGPT_COMPOSE_FILE" ps >/dev/null 2>&1
compose_rc=$?
set -e
[[ $compose_rc -ne 0 ]] \
  || fail "the Compose survey still answers from here -- the injection proves nothing"
log "MEASURED (defect condition reproduced): \`docker compose ps\` exits $compose_rc here"
docker ps -a --format '{{.Names}}' | grep -qx nyxgpt-cassandra \
  && fail "this host really has a nyxgpt-cassandra container -- the doctor half of this
           injection cannot reproduce the owner's condition"

# --- the self-heal survey ---------------------------------------------------
python3 - <<'PY'
import sys

from nyxgpt import self_heal

payload = self_heal.status()
source = payload["observability_source"]
print(f"    | observability_source={source}")
print(f"    | compose_probe_available={payload['compose_probe_available']}")
print(f"    | compose_probe_applicable={payload['compose_probe_applicable']}")
print(f"    | compose_probe_undetermined={payload['compose_probe_undetermined']}")
print(f"    | compose_probe_reason={payload['compose_probe_reason']}")

# The pre-#4137 reading, written out: this is literally the condition the CLI
# used. It must still be True, or the injection is not live and nothing below
# is evidence of anything.
if payload["compose_probe_available"]:
    sys.exit(
        "FAULT INJECTION FAILED: the Compose probe reports available on a host with no "
        "reachable Compose file, so the old reading would not have printed its banner"
    )

if source != "kubernetes":
    sys.exit(f"the observability tier was surveyed as '{source}' on a live cluster")
if payload["compose_probe_applicable"] or payload["compose_probe_undetermined"]:
    sys.exit("a Compose answer is still being claimed as owed on this deployment")
reason = payload["compose_probe_reason"]
for leak in ("docker-compose.yml", "docker compose ps"):
    if leak in reason:
        sys.exit(f"the reason still names {leak!r} on a host with no Compose stack")
pods = {c["service"]: c for c in payload["components"]}
for name in ("cassandra-0", "grafana-smoke"):
    row = pods.get(name)
    if row is None:
        sys.exit(f"{name} is not in the survey at all")
    if not (row["known"] and row["healthy"]):
        sys.exit(f"{name} read as known={row['known']} healthy={row['healthy']}")
if payload["unknown_count"]:
    sys.exit(f"{payload['unknown_count']} component(s) still report an undetermined state")
print("    | every Pod read from the cluster; nothing reported unknown")
PY
log "PASS (fix proven): the survey routes to the cluster and names no Compose file"

# The CLI's own text, because that is the surface the owner read.
self_heal_out="$(python3 -c \
  'import sys; from nyxgpt import cli; sys.exit(cli.cmd_self_heal_status(None))')"
echo "$self_heal_out" | sed 's/^/    | /'
if grep -q "CANNOT DETERMINE" <<<"$self_heal_out"; then
  fail "\`nyxgpt self-heal status\` still prints CANNOT DETERMINE on a live cluster"
fi
if grep -q "docker-compose.yml" <<<"$self_heal_out"; then
  fail "the CLI still names a docker-compose.yml on a host that has no Compose stack"
fi
grep -q "read from the cluster" <<<"$self_heal_out" \
  || fail "the CLI does not say where the observability rows came from"
grep -q "\[OK\] cassandra-0" <<<"$self_heal_out" \
  || fail "the CLI does not report the cluster's Cassandra Pod as OK"
log "PASS: no screen states both 'cannot determine the tier' and 'every component ready'"

# --- ops doctor -------------------------------------------------------------
# The marker is what turns doctor's cluster read on (it pays nothing for the
# question on a machine that never deployed to Kubernetes), and a
# `cloud deploy --kubernetes` writes it.
python3 - <<'PY'
from nyxgpt import install_mode, ops

install_mode.write_install_mode(
    install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
)

# Half (b), asserted rather than assumed: the host check really would fire here.
host = ops._cassandra_deployment_issues()
print(f"    | the host-side check on this box says: {host}")
assert any(
    "Missing local Cassandra container" in i for i in host
), "FAULT INJECTION FAILED: the host check produces no finding, so routing past it proves nothing"

probe = ops._k8s_deployment_probe()
print(f"    | _k8s_deployment_probe -> {probe.summary}")
assert probe.deployed, "doctor would not take the cluster branch on this box"
cluster = ops._k8s_cassandra_deployment_issues(probe.pods)
print(f"    | the cluster-side check says: {cluster}")
assert not cluster, "the cluster-side check reports a problem with a Ready cassandra-0"
PY

set +e
doctor_out="$(python3 -c \
  'from types import SimpleNamespace; from nyxgpt import ops; raise SystemExit(ops.doctor(SimpleNamespace()))' 2>&1)"
doctor_rc=$?
set -e
echo "$doctor_out" | sed 's/^/    | /'
log "MEASURED: ops doctor exited $doctor_rc"
if grep -q "Missing local Cassandra container" <<<"$doctor_out"; then
  fail "doctor still reports a missing local Cassandra container on a cluster whose
        cassandra-0 it lists as Running -- the #4137 FAIL"
fi
grep -q "Cassandra, model readiness, tracing wiring" <<<"$doctor_out" \
  || fail "doctor does not name Cassandra among the cluster-scoped checks, so an operator
           cannot tell which machine the finding below it is about"
log "PASS (fix proven): the Cassandra check answers for the substrate that is serving"
# Deliberately NOT asserted: doctor's exit status. A hosted runner is a
# half-installed host -- no config.ini, no brew/systemd services, no venv for a
# native api -- so it has real host-scoped findings of its own, and demanding
# rc 0 here would be asserting something this box cannot be. The claim is
# about which substrate each check answered for, and that is what is checked.

# --- the Infrastructure page's payload --------------------------------------
# The screen the owner actually read the CANNOT DETERMINE badge on, answered
# here from the same live cluster. The core/observability split this asserts is
# the reason it is worth a third reading: the gate is "does the cluster hold
# the CORE tier", not "are there Pods", so both of the Pods above have to be
# classified correctly for the two answers below to come out as they do.
python3 - <<'PY'
from nyxgpt import ops

status = ops.infra_status()
print(f"    | in_cluster={status['in_cluster']}  compose_in_scope={status['compose_in_scope']}")
print(f"    | compose_probe_reason={status['compose_probe_reason']}")

assert status["in_cluster"] is False, (
    "this host is being read as in-cluster, so the #3988 gate would cover it and this step "
    "is no longer exercising the vantage point #4137 is about"
)
assert status["compose_in_scope"] is False, (
    "the Infrastructure page still owes a Compose answer on a host whose core tier is in "
    "the cluster -- this is the CANNOT DETERMINE badge the owner saw"
)
for leak in ("docker-compose.yml", "docker compose ps"):
    assert leak not in status["compose_probe_reason"], (
        f"the card still names {leak!r} on a host with no Compose stack"
    )
# A separate question, and it must NOT have been swept along: this process is
# the native install, so the page can speak for it.
assert status["install_mode"]["in_scope"] is True, (
    "the native card went out of scope on a host that is not in a Pod -- the page just "
    "hid the one install it can speak for"
)
# The observability Pod alone must never be what decides the above: classified
# as its own tier, it is not core.
assert ops._k8s_core_pods_present(
    [s for s in ops._k8s_pod_states()[0] if s.name == "grafana-smoke"]
) is False, "an observability Pod is being counted as the core tier"
print("    | the core/observability split is read from the live cluster")
PY
log "PASS (fix proven): the Infrastructure page's Compose card is scoped, not undetermined"

unset NYXGPT_COMPOSE_FILE
kubectl -n "$NAMESPACE" delete pod cassandra-0 grafana-smoke --now >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
step "11/14  FAULT INJECTION: a Pod must be able to report its own deployment"
# ---------------------------------------------------------------------------
# #4138, and like step 10 it is a property of a VANTAGE POINT no unit fixture
# reproduces -- here the opposite one. On the owner's EC2 k3s instance the
# dashboard that deployment was itself serving read:
#
#   AWS substrate                                          [UNKNOWN]
#     Unknown from this machine -- it is neither an EC2 instance nor one that
#     has provisioned the substrate, so nothing here can answer.
#   Cloud deployment                                       [UNKNOWN]
#     Unknown from this machine -- no deploy has been recorded here and this
#     is not the instance.
#
# while `nyxgpt cloud status` ON that instance printed i-081ec19e9a96fa4ff
# (m5.xlarge), us-east-1, 184.193.4.58 and 3.0.0rc17 one process away. The api
# Pod reaches neither IMDS (169.254.169.254 is link-local and not routed into
# the Pod network) nor the host's ~/.nyxGPT/cloud, so it had nothing to answer
# with. The fix is #3988's: the install RECORDS the facts in the cluster.
#
# Both halves are proved against this live cluster:
#   (a) in-cluster with no record, `infra_status` must still report UNKNOWN --
#       the defect condition, and simultaneously the #3804 case AC4 says must
#       not regress;
#   (b) after the product's own writer applies the record to the real API
#       server, both status surfaces must report the instance, and the
#       off-cluster read of the SAME cluster must still report UNKNOWN.
#
# Two things this step does NOT pretend to be:
#   * a real EC2 instance. There is no hosted runner that is one
#     (docs/live-verification-ci.md), so the IMDS facts are injected into the
#     product's writer -- which is exactly where the real install gets them,
#     and the write, the manifest, the API server, the RBAC scope and both
#     reads are all real.
#   * a real Pod. In a Pod kubectl authenticates with the mounted
#     ServiceAccount; here the in-cluster READS are simulated by setting
#     KUBERNETES_SERVICE_HOST, which makes `subprocess_bounds.kubectl_env`
#     stand aside, so KUBECONFIG has to be named explicitly for kubectl to
#     reach this node's cluster at all (step 8's finding). The real in-Pod
#     authorization is covered below by asking the API server directly whether
#     the `nyxgpt-api` ServiceAccount may read this ConfigMap -- and whether it
#     may read any other, which it must not.

# rbac.yaml for real this time: step 3 validated it server-side only (a dry
# run creates nothing), and `kubectl auth can-i --as=serviceaccount` needs the
# Role and RoleBinding to exist. Free -- no Pods, no image pulls.
kubectl apply -n "$NAMESPACE" -f "$K8S_DIR/rbac.yaml" | sed 's/^/    | /'

# The precondition that makes the whole step evidence: the facts below cannot
# have come from IMDS or from a deploy record, because this box has neither.
python3 - <<'PY'
import sys
from pathlib import Path

from nyxgpt import cloud_deploy, cloud_imds, cloud_infra

if cloud_imds.instance_facts(force=True) is not None:
    sys.exit(
        "this runner answers on the IMDS address, so a populated card here would prove "
        "nothing about the cluster record"
    )
print("    | MEASURED: cloud_imds.instance_facts() -> None (this box is not an EC2 instance)")
for label, path in (
    ("deploy record", cloud_deploy.DEPLOY_STATE_FILE),
    ("deploy attempt", cloud_deploy.DEPLOY_ATTEMPT_FILE),
    ("substrate handoff", cloud_infra.CLOUD_STATE_FILE),
    # Saved substrate settings also make "not provisioned" an answer this
    # machine is entitled to give, which is not the UNKNOWN the (a) half
    # below asserts. Named here so that failure reads as a dirty runner
    # rather than as a broken product.
    ("saved substrate settings", cloud_infra.SETTINGS_FILE),
):
    if Path(path).exists():
        sys.exit(f"this box has a {label} at {path} -- it would answer instead of the cluster")
    print(f"    | MEASURED: no {label} at {path}")
PY

# --- (a) the defect condition, on a live cluster ---------------------------
# In-cluster, cluster reachable, namespace present, no record: UNKNOWN.
export KUBECONFIG="$HOME/.kube/config"
export KUBERNETES_SERVICE_HOST=127.0.0.1
export KUBERNETES_SERVICE_PORT=6443
kubectl -n "$NAMESPACE" delete configmap nyxgpt-cloud-deploy --ignore-not-found >/dev/null

python3 - <<'PY'
import sys

from nyxgpt import cloud_cluster_record, cloud_deploy, cloud_infra

cloud_cluster_record.reset_cache()
if not cloud_cluster_record.in_cluster():
    sys.exit("the in-cluster simulation did not take -- the reads below are the host's")

infra = cloud_infra.infra_status()
deploy = cloud_deploy.deploy_status()
print(f"    | infra.source={infra['source']}  known={infra['known']}")
print(f"    | deploy.source={deploy['source']}  known={deploy['known']}")

# This is the owner's screenshot, reproduced: a Pod of a live cluster, with
# nothing recorded, saying it cannot answer. It is BOTH the defect condition
# and AC4's requirement, which is why it is asserted rather than assumed.
if infra["known"] or infra["source"] != cloud_infra.SOURCE_UNKNOWN:
    sys.exit(
        "an in-cluster read answered about AWS with no record in the cluster -- the card "
        "would be asserting an instance nothing named"
    )
if deploy["known"]:
    sys.exit("the deployment card claims to know a deployment with no record of one")
print("    | FAULT INJECTION LIVE: both cards read UNKNOWN from inside this cluster")
PY
log "PASS (defect condition reproduced): a Pod of a live cluster cannot answer without a record"

# --- the write, by the product, on the host --------------------------------
# Not in-cluster: the real write happens during `ops install --kubernetes` ON
# the instance, so the env is put back to the host's for it.
env -u KUBERNETES_SERVICE_HOST -u KUBERNETES_SERVICE_PORT python3 - <<'PY'
import sys

from nyxgpt import cloud_imds, ops

FACTS = {
    "instance_id": "i-081ec19e9a96fa4ff",
    "instance_type": "m5.xlarge",
    "region": "us-east-1",
    "availability_zone": "us-east-1a",
    "public_ip": "184.193.4.58",
    "private_ip": "10.0.1.20",
    "vpc_id": "vpc-0smoke0",
    "subnet_id": "subnet-0smoke0",
    "security_group_id": "sg-0smoke0",
    "ssh_key_name": "nyxgpt-smoke",
}
# The one injection: this runner is not an EC2 instance, so the IMDS read the
# install makes on the real instance is substituted. Everything downstream --
# the record, the manifest, `kubectl apply`, the API server -- is the product's.
cloud_imds.instance_facts = lambda **_kw: dict(FACTS)

results = ops._record_k8s_cloud_deploy()
for r in results:
    print(f"    | [{'OK' if r.ok else 'FAIL'}] {r.message}")
    print(f"    |     {r.details}")
if not results:
    sys.exit("the install wrote no record even with instance facts available")
if not all(r.ok for r in results):
    sys.exit("the install could not write the cloud-deploy record into the cluster")
PY

log "MEASURED: the ConfigMap the install created, as the cluster holds it:"
kubectl -n "$NAMESPACE" get configmap nyxgpt-cloud-deploy \
  -o go-template='{{range $k, $v := .data}}{{$k}}={{$v}}{{"\n"}}{{end}}' | sed 's/^/    | /'

# The authorization a real Pod depends on, asked of the real API server --
# which is the half the simulated reads above cannot cover. Both directions:
# the record readable, and the namespace's other ConfigMaps still not.
API_SA="system:serviceaccount:${NAMESPACE}:nyxgpt-api"
kubectl auth can-i get configmap/nyxgpt-cloud-deploy -n "$NAMESPACE" --as="$API_SA" \
  | sed 's/^/    | can-i get configmap\/nyxgpt-cloud-deploy: /'
kubectl auth can-i get configmap/nyxgpt-cloud-deploy -n "$NAMESPACE" --as="$API_SA" -q \
  || fail "the api ServiceAccount may not read the cloud-deploy record -- in a real Pod the
           cards would read UNKNOWN however correctly the install wrote it"
if kubectl auth can-i get configmap/nyxgpt-config -n "$NAMESPACE" --as="$API_SA" -q; then
  fail "the Role grants the namespace's other ConfigMaps too -- resourceNames is supposed to
        keep configmap.yaml and the Secret unreadable through it"
fi
if kubectl auth can-i update configmap/nyxgpt-cloud-deploy -n "$NAMESPACE" --as="$API_SA" -q; then
  fail "the api ServiceAccount may WRITE the cloud-deploy record -- the page observes the
        deployment, it does not act on it (D-017)"
fi
log "PASS: the Role grants that one ConfigMap, read-only, and no other"

# --- (b) the fix, from inside the cluster ----------------------------------
python3 - <<'PY'
import sys

from nyxgpt import cloud_cluster_record, cloud_deploy, cloud_infra

cloud_cluster_record.reset_cache()
infra = cloud_infra.infra_status()
deploy = cloud_deploy.deploy_status()

print(f"    | infra.source={infra['source']}  on_ec2={infra['on_ec2']}")
print(f"    | infra.source_label={infra['source_label'][:110]}...")
print(f"    | deploy.source={deploy['source']}  deployed={deploy['deployed']}")

if infra["source"] != cloud_infra.SOURCE_CLUSTER_RECORD or not infra["provisioned"]:
    sys.exit("the substrate card still cannot report the instance this cluster runs on")
if deploy["source"] != cloud_deploy.SOURCE_CLUSTER_RECORD or not deploy["deployed"]:
    sys.exit("the deployment card still cannot report the deployment serving the page")

# AC2: the values must be the ones `nyxgpt cloud status` prints on the box.
expected = {
    "instance_id": "i-081ec19e9a96fa4ff",
    "instance_type": "m5.xlarge",
    "region": "us-east-1",
    "public_ip": "184.193.4.58",
}
for key, want in expected.items():
    if infra[key] != want:
        sys.exit(f"substrate card: {key}={infra[key]!r}, expected {want!r}")
for key, want in (
    ("instance_id", expected["instance_id"]),
    ("instance_type", expected["instance_type"]),
    ("region", expected["region"]),
    ("host", expected["public_ip"]),
    ("substrate", "kubernetes"),
):
    if deploy[key] != want:
        sys.exit(f"deployment card: {key}={deploy[key]!r}, expected {want!r}")
if not deploy["version"]:
    sys.exit("the deployment card reports no version from inside the deployment")
print(f"    | deploy.version={deploy['version']} (the process answering, read first-hand)")

# AC3: the card names the vantage point it answered from.
if cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP not in infra["source_label"]:
    sys.exit("the card does not name the record it answered from")

# AC6: nothing on this path reads the host filesystem for these facts. The
# record is the only source, so removing it must remove the answer.
PY
log "PASS (fix proven): both cards report the deployment from inside the cluster serving them"

# --- AC5: one endpoint, so the CLI prints what the page renders ------------
# `deploy_status()` is the single function behind both `GET /cloud/deploy` and
# `nyxgpt cloud status`, so this is not two code paths being compared -- it is
# the one answer, rendered twice. Printed in full because the owner's report
# was a side-by-side of these two surfaces disagreeing.
log "MEASURED: \`nyxgpt cloud status\` from inside the deployment:"
python3 - <<'PY' | sed 's/^/    | /'
from nyxgpt import cloud_cluster_record, cloud_deploy

cloud_cluster_record.reset_cache()
cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
PY
python3 - <<'PY'
import contextlib
import io
import sys

from nyxgpt import cloud_cluster_record, cloud_deploy

cloud_cluster_record.reset_cache()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
out = buf.getvalue()
for needle in (
    "DEPLOYED",
    "i-081ec19e9a96fa4ff",
    "m5.xlarge",
    "184.193.4.58",
    cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP,
):
    if needle not in out:
        sys.exit(f"`nyxgpt cloud status` does not print {needle!r} -- the CLI and the page "
                 f"would be disagreeing about the same endpoint's answer")
print("    | the CLI prints the same instance, type, IP and vantage point the page renders")
PY
log "PASS: the CLI and the page agree because they read the same function"

# --- the gate: off-cluster, the same cluster answers nothing ---------------
env -u KUBERNETES_SERVICE_HOST -u KUBERNETES_SERVICE_PORT python3 - <<'PY'
import sys

from nyxgpt import cloud_cluster_record, cloud_infra

cloud_cluster_record.reset_cache()
if cloud_cluster_record.read_cloud_deploy_record():
    sys.exit(
        "an off-cluster process read the cluster's cloud-deploy record -- a workstation "
        "pointed at a local cluster would start answering questions about AWS"
    )
status = cloud_infra.infra_status()
print(f"    | off-cluster: source={status['source']}  known={status['known']}")
if status["known"]:
    sys.exit("this host answers about AWS from a ConfigMap in a cluster it merely runs")
PY
log "PASS: the record is read only from inside the cluster, never by the host beside it"

# --- and the record is what did it -----------------------------------------
kubectl -n "$NAMESPACE" delete configmap nyxgpt-cloud-deploy >/dev/null
python3 - <<'PY'
import sys

from nyxgpt import cloud_cluster_record, cloud_deploy, cloud_infra

cloud_cluster_record.reset_cache()
if cloud_infra.infra_status()["known"] or cloud_deploy.deploy_status()["known"]:
    sys.exit(
        "the cards still answer with the ConfigMap deleted, so something other than the "
        "record was the source and the two assertions above proved nothing"
    )
print("    | with the record gone, both cards are UNKNOWN again")
PY
log "PASS (attribution): the record is the source -- delete it and the answer goes with it"

unset KUBERNETES_SERVICE_HOST KUBERNETES_SERVICE_PORT

# ---------------------------------------------------------------------------
step "12/14  FAULT INJECTION: a DECLARED substrate, before anything answers"
# ---------------------------------------------------------------------------
# #4184, and the reason step 10 above could not cover it. Step 10 proves the
# routing on a host whose cluster ANSWERS. This box, with the Pods deleted
# again, is the other half of the same class and the half the owner's rc1
# report is about: during the deploy itself nothing answers yet, so inference
# has nothing to work from and every check falls back to the host. On that
# instance it produced, mid-deploy:
#
#   WARNING self-heal: `docker compose ps` exited 125 ... querying
#   /home/ec2-user/.nyxGPT/docker-compose.yml -- observability survey
#   unavailable this pass
#
# plus `Install mode (native api/web): artifact (published/vendored build)` on
# a box whose api and web exist only as Pods, and "Restart the API to pick this
# up (`nyxgpt ops restart api`)" before any api was installed.
#
# What is asserted here is the ABSENCE of those three, which is deliberate:
# this issue's acceptance criterion is that the output carries no Compose
# reference and no native-install claim, and a presence-only assertion passes
# happily on output that also contains the wrong answer.
#
# The injection is the same box the earlier steps built, reduced to the
# deploy's own starting conditions: no core Pods, a `NYXGPT_COMPOSE_FILE`
# pointing at nothing, no `nyxgpt-cassandra` container -- and the declaration
# the deploy exports. Both pre-fix readings are asserted to still produce their
# false answers here, so a silenced check cannot pass as a routed one.
export NYXGPT_COMPOSE_FILE="$WORK/there-is-no-compose-stack-here/docker-compose.yml"
# EVERY core-tier Pod, and "every" is the word this step's first cut got wrong:
# it deleted step 10's `cassandra-0`/`grafana-smoke` and left step 4's
# `import-probe-after` Ready. That Pod carries `app: nyxgpt-api-canary-pool`
# (it has to -- step 5 reaches it through the unmodified api Service), which
# `_kubernetes_pod_tier` classifies as the CORE tier, so this box was still a
# cluster that ANSWERS. That is step 10's condition, not this one's: inference
# said "kubernetes" correctly, there was no fall-through to the host to
# reproduce, and the step failed its own fault-injection check rather than
# quietly proving nothing.
kubectl -n "$NAMESPACE" delete pod --all --now >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" wait --for=delete pod --all --timeout=120s >/dev/null 2>&1 || true
log "MEASURED (deploy-time conditions): core Pods in the namespace:"
kubectl -n "$NAMESPACE" get pods -o name --no-headers 2>/dev/null | sed 's/^/    | /' || true

python3 - <<'PY'
import sys

from nyxgpt import ops, self_heal, substrate

# --- half (a): the fault injection, asserted rather than assumed ------------
# First the precondition itself, read through the product's own kubectl path
# rather than inferred from the deletes above: this step's whole subject is a
# reachable cluster that answers NOTHING for the core tier yet, and with one
# core-tier Pod still up inference finds a substrate and the fall-through this
# injects cannot occur. Checked here, and separately from the probe count
# below, so the failure names the box rather than the symptom.
core_pods = [
    s.service
    for s in self_heal._list_kubernetes_component_status(set())
    if s.tier == "core"
]
print(f"    | cluster core tier from this host: {core_pods or 'nothing answers'}")
if core_pods:
    sys.exit(
        "FAULT INJECTION FAILED: the cluster still answers for the core tier "
        f"({', '.join(core_pods)}), so this box is a SERVING cluster -- which is step 10's "
        "condition and not the deploy's own moment this step exists to cover"
    )

# With nothing declared, this box is exactly the box that logged the Compose
# failure: the survey has no cluster to infer from and runs `docker compose ps`.
# "Nothing declared" is made true rather than assumed -- an inherited
# `NYXGPT_SUBSTRATE` from the surrounding job would route this half the way
# half (b) is routed and the two would prove the same thing twice.
substrate.clear_declaration()
probed = []
real_probe = self_heal.compose_probe


def _counting_probe():
    probed.append(True)
    return real_probe()


self_heal.compose_probe = _counting_probe
try:
    survey = self_heal.component_survey()
finally:
    self_heal.compose_probe = real_probe
print(f"    | with no declaration: compose probed={len(probed)}  "
      f"undetermined={survey.compose_probe.undetermined}")
if not probed:
    sys.exit(
        "FAULT INJECTION FAILED: the survey did not probe Compose on a box with no cluster, "
        "so the declared run below proves nothing"
    )

# --- half (b): the product's reading ---------------------------------------
with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
    probed.clear()
    self_heal.compose_probe = _counting_probe
    try:
        declared_survey = self_heal.component_survey()
        payload = self_heal.status()
    finally:
        self_heal.compose_probe = real_probe
    print(f"    | declared kubernetes: compose probed={len(probed)}  "
          f"substrate={payload['substrate']}  source={payload['substrate_source']}")
    if probed:
        sys.exit("a declared Kubernetes run still ran `docker compose ps`")
    if payload["observability_source"] != "kubernetes":
        sys.exit(f"the tier was surveyed as {payload['observability_source']!r} on a declared run")
    reason = declared_survey.compose_probe.reason
    for leak in ("docker-compose.yml", "docker compose ps", "125"):
        if leak in reason:
            sys.exit(f"the scope statement still names {leak!r}")
    # The sentence must be true when it is printed: there are no rows to read.
    if "rows below" in reason:
        sys.exit("the scope statement points at Pod rows that do not exist yet")
print("    | the declaration routed the survey; the host was never asked about Compose")

# --- the session-backend guidance, mid-deploy ------------------------------
# Printed BEFORE `ops install` by the provisioning script, so "Restart the API
# to pick this up" names a command that would have failed.
import pathlib
cfg = pathlib.Path.home() / ".nyxGPT" / "config.ini"
cfg.parent.mkdir(parents=True, exist_ok=True)
if not cfg.exists():
    cfg.write_text("[nyxgpt]\nsession_backend = file\n", encoding="utf-8")
with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
    results = ops.set_session_backend("cassandra", cfg_path=cfg)
detail = results[0].details
print(f"    | session-backend guidance: {detail}")
if "nyxgpt ops restart api" in detail:
    sys.exit("the guidance still names a restart of an api this box does not have")
# Withdrawing the wrong sentence is half the fix; the other half is saying what
# IS true where this runs, or the operator is left with a write and no idea
# what picks it up.
if ops.K8S_CONFIG_CONFIGMAP not in detail:
    sys.exit(
        f"the guidance does not name the {ops.K8S_CONFIG_CONFIGMAP} ConfigMap the api Pods "
        "actually read, so the restart line was deleted rather than replaced"
    )
PY
log "PASS (fix proven): the declared substrate routes the surveys a deploy triggers"

# --- the same guidance with NOTHING declared and no api of any kind --------
# The arm the first fix for finding 4 got wrong (review of PR #4191). This box
# now has no api registered with its service manager -- which is also the
# permanent state of every Compose- and Terraform-served host, because their
# api is a container. Answering that with "No api service is installed on this
# host yet ... whatever starts it next reads this value then" is false twice
# over beside a serving container, and it is this issue's own class one arm
# along: a sentence answering for a substrate the check never looked at.
#
# Run as its own block, and before the native unit is injected below, because
# both conditions this needs are destroyed by the next thing this step does.
python3 - <<'PY'
import pathlib
import sys

from nyxgpt import install_mode, ops, substrate

# The conditions, made true rather than assumed. The Kubernetes marker step 10
# wrote is cleared HERE (the block below used to do it) -- while it stands,
# `deployment_substrate` can still answer "kubernetes" from the access bridge
# holding this host's ports, and the host arm under test is unreachable.
substrate.clear_declaration()
install_mode.clear_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
snapshot = ops._native_services_snapshot()
if snapshot.get("api", "none") != "none":
    sys.exit(
        f"FAULT INJECTION FAILED: this box already has a native api ({snapshot['api']}), so "
        "the arm under test is not the one that would be printed"
    )

cfg = pathlib.Path.home() / ".nyxGPT" / "config.ini"
undeclared = ops.set_session_backend("file", cfg_path=cfg)[0].details
print(f"    | session-backend guidance, nothing declared, no native api: {undeclared}")
if "No api service is installed on this host yet" in undeclared:
    sys.exit("the guidance still claims no api is installed from a check that never looked")
if "No native api service is registered with this host's service manager" not in undeclared:
    sys.exit("the guidance does not scope itself to the one question it asked")
if str(ops.COMPOSE_CONFIG_FILE) not in undeclared or "nyxgpt ops env-sync" not in undeclared:
    sys.exit(
        "the guidance does not name what a containerised api actually reads, so the false "
        "sentence was withdrawn rather than replaced"
    )
# Put the value back: an unchanged write returns "already `file`" with no
# guidance at all, and the native-arm measurement below writes `file` too --
# it has to be a real change for there to be a sentence to read.
ops.set_session_backend("cassandra", cfg_path=cfg)
PY
log "PASS (review fix proven): a host with no native api is told what was checked"

# --- doctor's own text, on this box ----------------------------------------
# The surface the owner read. Asserted on the OUTPUT rather than on a helper,
# because the finding was a printed record and not a return value.
#
# Injected first, and this is the half that makes the absence assertion below
# mean something: a bare runner has no native unit at all, so `Install mode
# (native api/web):` would be missing whether the fix is present or not. A real
# `systemd --user` unit under the name the product reads, actually started,
# plus a native marker on disk, is exactly the state the owner's instance was
# in -- and the pre-fix gate ("is a native unit registered here") prints the
# line on it. The unit binds nothing: what is under test is a READING.
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/nyxgpt-api.service" <<'UNIT'
[Unit]
Description=k3s-cloud-smoke stand-in for a native nyxGPT api (#4184 fault injection)

[Service]
ExecStart=/bin/sleep infinity
UNIT
systemctl --user daemon-reload
systemctl --user start nyxgpt-api.service
systemctl --user is-active nyxgpt-api.service >/dev/null \
  || fail "the injected native api unit did not start -- the absence assertion below would
           pass on a box that simply has no native install, which proves nothing"
python3 - <<'PY'
from nyxgpt import install_mode, ops

# A native marker, so the record EXISTS and the question is where it is
# printed rather than whether there is one.
install_mode.write_install_mode(
    install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_NATIVE
)
# ...and no Kubernetes marker, which is the mid-deploy truth:
# `_record_k8s_install_mode` is a later step of the install this run is in, so
# the marker-gated block cannot speak for it. (Step 10 wrote one; the
# no-native-api block just above removed it, and this reasserts the condition
# rather than inheriting it -- this block's reading must not depend on what
# another block happened to leave behind.) The declaration is what has to carry
# the routing here.
install_mode.clear_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)

snapshot = ops._native_services_snapshot()
print(f"    | native services as the product reads them: {snapshot}")
assert snapshot.get("api") == "started", (
    "FAULT INJECTION FAILED: the product does not see the injected unit as started, so "
    "the pre-fix gate would not have printed its install-mode line"
)

# The other side of the session-backend guidance, now that this box HAS a
# native api: with no declaration it is told to restart it. Asserted because
# without it the check above is satisfied by a sentence someone deleted, and a
# deleted sentence is not a routed one -- on a native host the restart is the
# right answer and must survive.
import pathlib

cfg = pathlib.Path.home() / ".nyxGPT" / "config.ini"
detail = ops.set_session_backend("file", cfg_path=cfg)[0].details
print(f"    | session-backend guidance, native api registered: {detail}")
assert "nyxgpt ops restart api" in detail, (
    "FAULT INJECTION FAILED: a host with a started native api is not told to restart it "
    "either, so the declared run's silence about it proves nothing about routing"
)
PY

set +e
doctor_out="$(NYXGPT_SUBSTRATE=kubernetes python3 -c \
  'from types import SimpleNamespace; from nyxgpt import ops; raise SystemExit(ops.doctor(SimpleNamespace()))' 2>&1)"
set -e
echo "$doctor_out" | sed 's/^/    | /'
if grep -q "^Install mode (native api/web):" <<<"$doctor_out"; then
  fail "doctor still prints a native install mode as the install mode on a box whose
        deployment is the cluster's -- the #4184 finding 2 record"
fi
# Not hidden, either: the record is real and is repositioned as dated history.
grep -q "Install history" <<<"$doctor_out" \
  || fail "the native record was dropped rather than repositioned -- an operator with a
           real past install can no longer see it at all"
# And the one case where silence would be wrong: this unit really is started.
grep -q "native api still running on a host whose deployment is Kubernetes" <<<"$doctor_out" \
  || fail "a started native api beside a serving cluster is not reported -- withdrawing the
           claim must not mean going quiet about two stacks on one host's ports"
if grep -q "Missing local Cassandra container" <<<"$doctor_out"; then
  fail "the host Cassandra check still answers for a Kubernetes run"
fi
grep -q "Substrate: kubernetes" <<<"$doctor_out" \
  || fail "doctor does not say which substrate its checks are about on a declared run"
log "PASS (fix proven): no record is printed as a claim about a substrate that is not there,"
log "                   the record survives as history, and the live conflict is reported"

# Leave the box as step 14 expects to find it: that step's teardown is the
# NATIVE section's, and a stand-in unit under a real product name is not
# something to hand to it.
systemctl --user stop nyxgpt-api.service >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/nyxgpt-api.service"
systemctl --user daemon-reload
unset NYXGPT_COMPOSE_FILE

# And put back the one Pod the deletes above took that a LATER step needs: step
# 14's precondition is a bridge that answers on 127.0.0.1:8000, which it can
# only do while svc/nyxgpt-api has an endpoint. This Pod is that endpoint (step
# 4 built it with the Service's own selector and port name), so emptying the
# namespace for this step's condition has to be undone before step 14 measures
# anything -- otherwise that step would fail on a missing endpoint and read as
# "the teardown could not be proven".
probe_pod import-probe-after | kubectl apply -f - >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/import-probe-after --timeout=120s \
  || fail "the api Service's endpoint Pod did not come back after this step emptied the
           namespace -- step 14's bridge precondition cannot be measured without it"

# ---------------------------------------------------------------------------
step "13/14  FAULT INJECTION: the instance's sshd must offer a post-quantum KEX"
# ---------------------------------------------------------------------------
# #4184 finding 5. Every `nyxgpt cloud ops ...` on the owner's instance carried
#
#   ** WARNING: connection is not using a post-quantum key exchange algorithm.
#   ** This session may be vulnerable to "store now, decrypt later" attacks.
#
# which is the CLIENT reporting what the SERVER offered. The fix is on the
# instance nyxGPT provisions, and this runs the deploy's own text
# (`SSHD_PQ_KEX_SECTION`) against this machine's real sshd.
#
# The injection matters here more than usual: this runner's OpenSSH already
# offers a PQ exchange by default, so a bare run of the block would pass on a
# box that never had the defect. So the owner's condition -- a server offering
# only classical KEX -- is written as a drop-in in BOTH positions, and the
# position is what each half is about:
#
#   (a) sorting BEFORE the product's file, where
#       `KexAlgorithms`-takes-its-first-value leaves the product's drop-in
#       parsed and ignored. What is proven is the read-back: the block reports
#       that rather than announcing a key exchange it did not obtain.
#   (b) sorting AFTER it, where the product's file legitimately wins. The box
#       is measured classical before the drop-in, post-quantum with it, and
#       classical again once it is removed -- so the PQ offer is attributable
#       to this file and not to the runner's default.
if ! ssh -Q kex 2>/dev/null | grep -qE '^(mlkem768x25519-sha256|sntrup761x25519-sha512@openssh.com)$'; then
  log "SKIP: this runner's OpenSSH implements no post-quantum KEX, so there is nothing"
  log "      here to prove the drop-in against (the block's own no-op branch covers it)"
else
  python3 -c \
    'from nyxgpt.cloud_deploy import SSHD_PQ_KEX_SECTION; print("set -euo pipefail"); print(SSHD_PQ_KEX_SECTION)' \
    > "$WORK/sshd-pq.sh"
  log "The sshd text (as a deploy sends it):"
  sed 's/^/    | /' "$WORK/sshd-pq.sh"
  # `sshd -t` needs the privilege-separation directory; on a real instance sshd
  # is running and it exists. Created here so the block's own baseline check
  # measures the config and not this runner's missing /run/sshd.
  sudo mkdir -p /run/sshd
  sudo rm -f /etc/ssh/sshd_config.d/50-nyxgpt-pq-kex.conf

  # --- half (a): the defect condition, on this box -------------------------
  sudo tee /etc/ssh/sshd_config.d/10-nyxgpt-smoke-classical-only.conf >/dev/null <<'CLASSICAL'
# Injected by k3s-cloud-smoke (#4184): a server that offers only classical key
# exchange, which is what Amazon Linux 2023's sshd does by default and what put
# OpenSSH's "store now, decrypt later" warning on every `cloud ops` command.
KexAlgorithms curve25519-sha256,ecdh-sha2-nistp256
CLASSICAL
  classical="$(sudo sshd -T 2>/dev/null | grep -i '^kexalgorithms ' || true)"
  log "MEASURED (defect condition reproduced): $classical"
  if grep -qE 'mlkem|sntrup' <<<"$classical"; then
    fail "the injected classical-only config did not take effect -- nothing below is evidence"
  fi

  # The product's block must REPORT this rather than claim success: its file is
  # read second, and KexAlgorithms takes its first value.
  ignored_out="$(bash "$WORK/sshd-pq.sh" 2>&1 || true)"
  echo "$ignored_out" | sed 's/^/    | /'
  grep -q "not in effect" <<<"$ignored_out" \
    || fail "the block announced a post-quantum key exchange it did not obtain -- the
             read-back is what keeps this from being a claim about a connection that
             is still classical"
  log "PASS: a parsed-but-overridden drop-in is reported, not announced as success"

  # --- half (b): the product's reading, and what it is ATTRIBUTABLE to -----
  # The classical-only config does not go away here, it MOVES: into a drop-in
  # that sorts AFTER the product's, which the first-value rule makes the
  # product's file the winner of rather than the loser. That keeps this
  # runner's PQ-by-default out of the answer -- deleting the injection instead
  # would leave `sntrup761x25519-sha512@openssh.com` effective on a box that
  # never had the defect, and the assertion below would pass with or without
  # the fix. Measured without the drop-in, with it, and after removing it, so
  # the PQ offer is attributable to this file.
  sudo rm -f /etc/ssh/sshd_config.d/10-nyxgpt-smoke-classical-only.conf \
             /etc/ssh/sshd_config.d/50-nyxgpt-pq-kex.conf
  sudo tee /etc/ssh/sshd_config.d/90-nyxgpt-smoke-classical-after.conf >/dev/null <<'CLASSICAL'
# Injected by k3s-cloud-smoke (#4184): the same classical-only server as above,
# in a file the product's drop-in precedes. Sorting after it is the point --
# this is the defect condition, kept on the box, in a position that cannot
# override the fix under test.
KexAlgorithms curve25519-sha256,ecdh-sha2-nistp256
CLASSICAL
  without="$(sudo sshd -T 2>/dev/null | grep -i '^kexalgorithms ' || true)"
  log "MEASURED (defect condition, before the drop-in): $without"
  if grep -qE 'mlkem|sntrup' <<<"$without"; then
    fail "this box already offers a post-quantum key exchange with no drop-in written, so
          the assertions below would pass whether the fix is present or not"
  fi
  applied_out="$(bash "$WORK/sshd-pq.sh" 2>&1)"
  echo "$applied_out" | sed 's/^/    | /'
  grep -q "sshd offers a post-quantum key exchange" <<<"$applied_out" \
    || fail "the deploy's sshd block did not put a post-quantum key exchange in place"
  effective="$(sudo sshd -T 2>/dev/null | grep -i '^kexalgorithms ' || true)"
  log "MEASURED (effective): $effective"
  grep -qE '^kexalgorithms (mlkem768x25519-sha256|sntrup761x25519-sha512@openssh\.com)' \
    <<<"$effective" \
    || fail "sshd does not PREFER a post-quantum key exchange after the drop-in"
  # `^` prepends: an older client's algorithms must still be offered, or this
  # fix would lock out the very operators it is for.
  grep -q 'curve25519-sha256' <<<"$effective" \
    || fail "the drop-in dropped the classical algorithms instead of prepending to them"
  sudo sshd -t || fail "sshd cannot validate its own config after the drop-in"
  log "PASS (fix proven): PQ first, every classical algorithm still offered, config valid"

  # Attribution: remove the one file and the answer goes with it.
  sudo rm -f /etc/ssh/sshd_config.d/50-nyxgpt-pq-kex.conf
  after="$(sudo sshd -T 2>/dev/null | grep -i '^kexalgorithms ' || true)"
  log "MEASURED (drop-in removed again): $after"
  if grep -qE 'mlkem|sntrup' <<<"$after"; then
    fail "the post-quantum offer outlived the drop-in's removal, so something other than
          the product's file is what the assertions above measured"
  fi
  log "PASS (attribution): the drop-in is the source -- removed, this sshd is classical again"
  sudo rm -f /etc/ssh/sshd_config.d/90-nyxgpt-smoke-classical-after.conf
fi

# ---------------------------------------------------------------------------
step "14/14  The --no-kubernetes transition actually moves the box"
# ---------------------------------------------------------------------------
# `--no-kubernetes` is documented as moving a deployment back to the native
# substrate. The failure this proves against is silent in the worst available
# way: without the teardown, k3s and the `Restart=always` bridge keep holding
# 127.0.0.1:8000, the freshly installed native services never bind, and the
# install's health wait, the deploy's own check and the tunnel are all answered
# by the cluster the operator just asked to leave -- so the deploy reports
# success and records "native" about a box still serving from the cluster.
#
# Inspection cannot see that. It needs a running cluster and a running bridge,
# which is exactly what the preceding steps have built, so the teardown is run
# here against the real thing. As everywhere else in this script the text is
# LIFTED from the rendered native section rather than retyped.
python3 - > "$WORK/teardown.sh" <<'TEARDOWN_PY'
from nyxgpt.cloud_deploy import NATIVE_STACK_BRINGUP_SECTION

end = NATIVE_STACK_BRINGUP_SECTION.index("# --- Bring the stack up")
print("set -euo pipefail")
print(NATIVE_STACK_BRINGUP_SECTION[:end])
TEARDOWN_PY

log "Teardown text (as a --no-kubernetes deploy sends it):"
sed 's/^/    | /' "$WORK/teardown.sh"

# Step 6 stopped the bridge to prove it was the thing being measured. Bring it
# back first, so what kills it below is the teardown and not that.
systemctl --user start nyxgpt-k8s-bridge@api.service
restored=""
for _ in $(seq 1 20); do
  if restored="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null)"; then
    break
  fi
  sleep 3
done
[[ -n "$restored" ]] \
  || fail "could not restore the bridge before the teardown -- step 7 would prove nothing"
log "MEASURED (precondition): bridge up again, 127.0.0.1:8000/health -> $restored"
command -v k3s >/dev/null 2>&1 || fail "k3s is already gone before the teardown ran"

bash "$WORK/teardown.sh"

# The bridge: gone as a unit, and gone off the port.
if systemctl --user is-active nyxgpt-k8s-bridge@api.service >/dev/null 2>&1; then
  fail "the access bridge is still active after the --no-kubernetes teardown -- the
        native services would fail to bind 8000 and every probe would be answered by
        the cluster the operator asked to leave"
fi
if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  fail "127.0.0.1:8000 still answers after the --no-kubernetes teardown"
fi
[[ ! -f "$HOME/.config/systemd/user/nyxgpt-k8s-bridge@.service" ]] \
  || fail "the bridge unit template survived the teardown"
log "PASS: the bridge is stopped, disabled, removed, and 8000 is free for the native stack"

# The cluster: actually uninstalled, not merely stopped.
if command -v k3s >/dev/null 2>&1; then
  fail "k3s is still installed after the --no-kubernetes teardown -- the cluster would
        keep running the stack the deploy record now says is native"
fi
if ss -ltnH 'sport = :6443' | grep -q .; then
  fail "something still listens on 6443 after the k3s uninstall"
fi
log "PASS: k3s is uninstalled and 6443 is free"

# And the half that makes it safe to run on every native deploy: a second pass,
# on a box that now has neither, must be a no-op rather than an abort. The
# teardown runs under `set -euo pipefail` on the instance, where `disable --now`
# on an absent unit and an absent uninstaller are both non-zero.
bash "$WORK/teardown.sh" \
  || fail "the teardown is not idempotent -- it aborts on a box that never had k3s,
           which is every first deploy and every ordinary native re-deploy"
log "PASS (idempotence): a second teardown on a box with neither is a no-op"

echo
log "ALL PASS -- the k3s substrate a --kubernetes cloud deploy creates works, the"
log "manifests apply to it unchanged (with the image tags pinned from outside them),"
log "nothing listens on the public interface, the product finds the cluster with no"
log "KUBECONFIG exported, a finished rollout's leftover Pod no longer fails the"
log "install, no host-side probe answers for the cluster from this host, a Pod of"
log "the cluster can report the cloud deployment it is part of (and reports UNKNOWN"
log "when nothing recorded one), a DECLARED substrate routes every survey before"
log "anything answers -- with no Compose file, no native install mode and no"
log "restart-the-api guidance in the output -- this instance's sshd offers a"
log "post-quantum key exchange, the --no-kubernetes transition really retires the"
log "cluster, and all nine fault injections reproduced the failures they guard"
log "against."
log "NOT covered here, by construction: a real EC2 instance, a real AWS security"
log "group, and IMDSv2 -- see docs/live-verification-ci.md."
