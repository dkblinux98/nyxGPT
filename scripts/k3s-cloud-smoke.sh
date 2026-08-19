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
# Six steps, and two of them are fault injections -- a job that only runs the
# happy path passes on every machine that fails to reproduce the bug (#3753):
#
#   1  Execute the deploy's own k3s bootstrap.
#   2  Assert the access surface: nothing on 0.0.0.0, no ingress controller,
#      no LoadBalancer implementation, `local-path` still the default class.
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
step "1/7  Execute the deploy's own k3s bootstrap"
# ---------------------------------------------------------------------------
python3 - > "$WORK/k3s-bootstrap.sh" <<'PY'
from nyxgpt.cloud_deploy import render_k3s_bootstrap

print(render_k3s_bootstrap())
PY

log "Bootstrap text (as a --kubernetes deploy sends it):"
sed 's/^/    | /' "$WORK/k3s-bootstrap.sh"

# `set -euo pipefail` and the IMDS fallback are both properties of the text
# itself, so it is run as-is under bash rather than sourced into this shell.
bash "$WORK/k3s-bootstrap.sh"

export KUBECONFIG="$HOME/.kube/config"
[[ -f "$KUBECONFIG" ]] || fail "the bootstrap did not write $KUBECONFIG"
NODE_IP="$(awk -F'[/:]' '/server:/ {print $4}' "$KUBECONFIG")"
[[ -n "$NODE_IP" ]] || fail "could not read the API server address out of $KUBECONFIG"
log "MEASURED: the kubeconfig points at https://${NODE_IP}:6443"

# ---------------------------------------------------------------------------
step "2/7  The access surface: #3503 says nothing but TCP 22"
# ---------------------------------------------------------------------------
log "MEASURED: listeners on 6443:"
ss -ltnH 'sport = :6443' | sed 's/^/    | /'

if ss -ltnH 'sport = :6443' | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):6443$'; then
  fail "the k3s apiserver is listening on every interface -- on an EC2 instance that is the
        public NIC, and #3503's access model is that nothing but TCP 22 is reachable"
fi
ss -ltnH 'sport = :6443' | awk '{print $4}' | grep -q "${NODE_IP}:6443" \
  || fail "nothing is listening on ${NODE_IP}:6443, which is what the kubeconfig points at"
log "PASS: the apiserver is bound to the node's private address only"

# Traefik binds host ports 80/443 and is k3s's default ingress controller;
# servicelb is what makes a `Service: LoadBalancer` provision anything. #3506's
# premise is that the manifests need neither.
for unwanted in traefik svclb; do
  if kubectl get pods -A --no-headers 2>/dev/null | grep -q "$unwanted"; then
    kubectl get pods -A | sed 's/^/    | /'
    fail "$unwanted is running -- the bootstrap's --disable flags did not take"
  fi
done
log "PASS: no ingress controller and no LoadBalancer implementation are installed"

# ...and local-storage IS still enabled, because the Cassandra and Ollama
# StatefulSets bind through whatever the default StorageClass is.
kubectl get storageclass local-path >/dev/null 2>&1 \
  || fail "the local-path StorageClass is gone -- every volumeClaimTemplate in k8s/ would
           sit Pending on an unbound PVC"
kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/default-class=="true")].metadata.name}' \
  | grep -q local-path \
  || fail "local-path is not the default StorageClass"
log "PASS: local-path is present and is the default StorageClass"

# ---------------------------------------------------------------------------
step "3/7  k8s/*.yaml applies to k3s UNCHANGED"
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

# A server-side dry run is the strong, cheap form of "these manifests apply":
# the real API server validates, defaults and admits every object, and nothing
# is created -- so the job does not spend ten minutes pulling the Cassandra and
# Ollama images to learn what admission already answered. Whether those Pods
# then become Ready is k8s-local-smoke.yml's question, on a real cluster with
# real builds; this job's question is the k3s delta.
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
kubectl apply -f "$K8S_DIR/namespace.yaml" >/dev/null
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
step "4/7  FAULT INJECTION: a docker-built image is invisible to k3s"
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
step "5/7  The access bridge, end to end"
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
step "6/7  FAULT INJECTION: the bridge is what was measured"
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
step "7/7  The --no-kubernetes transition actually moves the box"
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
log "manifests apply to it unchanged, nothing listens on the public interface, the"
log "--no-kubernetes transition really retires it, and both fault injections"
log "reproduced the failures they guard against."
log "NOT covered here, by construction: a real EC2 instance, a real AWS security"
log "group, and IMDSv2 -- see docs/live-verification-ci.md."
