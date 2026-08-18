#!/usr/bin/env bash
# Executed verification for the checkout-free Kubernetes install (#3834, #3775).
#
# The question this answers: can `nyxgpt ops install --kubernetes --local`
# bring the stack up on a machine that has NO source checkout? That is the
# Repo-less Portability requirement (CLAUDE.md, 2026-08-01), which names
# Kubernetes as an in-scope target, and until #3834 the answer was no in three
# separate places at once -- the api image built from `REPO_ROOT`, the web
# image built from `REPO_ROOT/web`, and the kustomization read from
# `REPO_ROOT/k8s`. None of that is visible to inspection or to a unit test:
# both pass happily inside a checkout, which is the only place they were ever
# run.
#
# So this script runs the install from a wheel, in a venv, with the repository
# unreadable from the product's point of view, and asserts a user can chat.
#
# Two halves, per the fault-injection rule (CLAUDE.md, #3753):
#
#   1. PRE-FIX PATH MUST FAIL -- the build context and the manifest directory
#      the old code used are asserted absent here, and a `docker build` of the
#      old context is asserted to fail. Without this the job would pass on a
#      runner that happens to have a checkout in the right place, which is
#      exactly the condition that hid the defect.
#   2. FIXED PATH MUST WORK  -- the real command, then the real user path
#      (sessions and a chat round-trip through the web Service's own proxy
#      routes), then `ops status` reporting the deployment's own install mode.
#
# Observability is deliberately skipped (`--skip-observability`): whether the
# in-cluster layer comes up with the app tier is k8s-local-smoke.yml's
# question, asked there on the default command. Duplicating it here would
# double a 60-minute job to re-answer a question already covered, while this
# job's question -- "with no checkout anywhere" -- needs none of it.
#
# Prerequisites: Docker, a Python that can build a wheel, and this checkout
# (the harness runs FROM the repository; the product under test does not).
# kubectl and kind are installed by the install command itself (#3724).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="nyxgpt"
API_KEY="${NYXGPT_SMOKE_API_KEY:-k8s-artifact-smoke-key}"
WEB_PORT="${NYXGPT_SMOKE_WEB_PORT:-3000}"
SESSION="k8s-artifact-smoke-$$"
MODEL="${NYXGPT_SMOKE_MODEL:-qwen2.5:0.5b}"
BASE="http://127.0.0.1:${WEB_PORT}"
# Everything the product may see lives outside the checkout.
WORKDIR="${NYXGPT_SMOKE_WORKDIR:-/tmp/nyxgpt-artifact-smoke}"
VENV="${WORKDIR}/venv"
ARTIFACTS="${WORKDIR}/artifacts"
NOCHECKOUT="${WORKDIR}/no-checkout"
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
    if [ "${NYXGPT_SMOKE_KEEP_UP:-0}" != "1" ] && [ -x "${VENV}/bin/nyxgpt" ]; then
        (cd "$NOCHECKOUT" && "${VENV}/bin/nyxgpt" ops down --kubernetes) >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

nyx() {
    # Every product command runs from a directory with no repository in it,
    # out of a venv that has no checkout above it.
    (cd "$NOCHECKOUT" && "${VENV}/bin/nyxgpt" "$@")
}

step "1/9 Build the artifacts this install will consume"
rm -rf "$WORKDIR"
mkdir -p "$ARTIFACTS" "$NOCHECKOUT"
python3 -m venv "$VENV"
"${VENV}/bin/pip" install --quiet --upgrade pip build
"${VENV}/bin/python" -m build --wheel --outdir "${WORKDIR}/dist" "$REPO_ROOT" >/dev/null
shopt -s nullglob
wheels=("${WORKDIR}"/dist/nyxgpt-*.whl)
shopt -u nullglob
[ ${#wheels[@]} -gt 0 ] || fail "no wheel was built from ${REPO_ROOT}"
WHEEL="${wheels[0]}"
VERSION=$(basename "$WHEEL" | cut -d- -f2)
ok "built $(basename "$WHEEL") (version ${VERSION})"

# The two service tarballs a release publishes, built by the same builder the
# release publishes with (release_tarball._create_dist_tarball). The branch's
# in-development version has no GitHub Release, so they are staged for
# `NYXGPT_ARTIFACT_DIR` exactly as cloud-artifact-smoke.yml stages them -- the
# supported escape hatch for "this version is not published yet", and the only
# way to smoke the artifact path of the code under review rather than of some
# already-released version.
PYTHONPATH="${REPO_ROOT}/src" python3 - "$VERSION" "$ARTIFACTS" <<'PY'
import sys
from pathlib import Path

from nyxgpt import release_tarball

version, out = sys.argv[1], Path(sys.argv[2])
for name in ("nyxgpt-api", "nyxgpt-web"):
    tar = release_tarball._create_dist_tarball(out, name, version)
    print(f"staged {tar}")
PY
export NYXGPT_ARTIFACT_DIR="${ARTIFACTS}/dist"
ls "$NYXGPT_ARTIFACT_DIR"
ok "staged the published-shape service tarballs in ${NYXGPT_ARTIFACT_DIR}"

step "2/9 Install the wheel -- no checkout anywhere the product can see"
"${VENV}/bin/pip" install --quiet "$WHEEL"
PRODUCT_REPO_ROOT=$("${VENV}/bin/python" -c 'from nyxgpt import ops; print(ops.REPO_ROOT)')
echo "product REPO_ROOT: ${PRODUCT_REPO_ROOT}"
[ "$PRODUCT_REPO_ROOT" != "$REPO_ROOT" ] ||
    fail "the installed package resolves REPO_ROOT to this checkout -- the venv is editable, \
so nothing below tests the artifact path"
ok "nyxgpt $(nyx --version) installed from the wheel"

step "3/9 The pre-#3834 code path CANNOT work here (fault injection)"
# The old install built the api image from REPO_ROOT, the web image from
# REPO_ROOT/web, and applied REPO_ROOT/k8s. Prove all three are absent, and
# that the build the old code would have run actually fails -- otherwise a
# green run below proves nothing about checkout-freeness.
for missing in "Dockerfile" "web/Dockerfile" "k8s/kustomization.yaml"; do
    [ ! -e "${PRODUCT_REPO_ROOT}/${missing}" ] ||
        fail "${PRODUCT_REPO_ROOT}/${missing} exists -- this machine is not checkout-free \
and the assertions below are meaningless"
done
if docker build -t nyxgpt-prefix-check "$PRODUCT_REPO_ROOT" >/tmp/k8s-artifact-prefix.log 2>&1; then
    fail "docker build of the pre-fix context (${PRODUCT_REPO_ROOT}) SUCCEEDED -- the \
fault-injection half cannot detect a regression to it"
fi
ok "the pre-fix build context and manifests do not exist here, and building them fails"

step "4/9 Seed the config from packaged resources (no checkout involved)"
"${VENV}/bin/python" - <<'PY'
import shutil
from pathlib import Path

from nyxgpt.ops import _packaged_resources_root

home = Path.home() / ".nyxGPT"
home.mkdir(parents=True, exist_ok=True)
shutil.copy(_packaged_resources_root() / "example.config.ini", home / "config.ini")
print(f"seeded {home / 'config.ini'} from the packaged example config")
PY
ok "config seeded from package data"

step "5/9 --dev is refused on a machine with no checkout"
if nyx ops install --kubernetes --local --dev --api-key "$API_KEY" >/tmp/k8s-artifact-dev.log 2>&1; then
    cat /tmp/k8s-artifact-dev.log >&2
    fail "--dev was accepted with no checkout to build from"
fi
grep -q "needs a source checkout" /tmp/k8s-artifact-dev.log ||
    { cat /tmp/k8s-artifact-dev.log >&2; fail "--dev failed for the wrong reason"; }
ok "--dev refuses up front and names why"

step "6/9 The real command: nyxgpt ops install --kubernetes --local"
nyx ops install --kubernetes --local --api-key "$API_KEY" --skip-observability
ok "install --kubernetes --local completed with no checkout"

# The manifests came from package data, not from a repository.
[ -f "${HOME}/.nyxGPT/k8s/kustomization.yaml" ] ||
    fail "no kustomization under ~/.nyxGPT/k8s -- the manifests were not synced from package data"
docker image inspect nyxgpt-api:local >/dev/null ||
    fail "nyxgpt-api:local was never built"
docker image inspect nyxgpt-web:local >/dev/null ||
    fail "nyxgpt-web:local was never built"
ok "manifests synced from package data; both images built from the staged artifacts"

step "7/9 The data/LLM tier is Ready and the app tier is serving"
for workload in cassandra ollama; do
    kubectl -n "$NAMESPACE" rollout status "statefulset/${workload}" --timeout=900s ||
        fail "${workload} never became Ready"
done
kubectl -n "$NAMESPACE" rollout status deploy/nyxgpt-api-stable --timeout=600s ||
    fail "the api Deployment never became Ready"
kubectl -n "$NAMESPACE" rollout status deploy/nyxgpt-web-stable --timeout=600s ||
    fail "the web Deployment never became Ready"
ok "the whole app tier rolled out from images built without a checkout"

step "8/9 A user can actually chat"
kubectl -n "$NAMESPACE" port-forward "svc/nyxgpt-web" "${WEB_PORT}:3000" \
    >/tmp/k8s-artifact-portforward.log 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then break; fi
    sleep 2
done
curl -fsS "${BASE}/api/sessions" >/dev/null ||
    fail "GET /api/sessions failed -- the UI would show 'Failed to load sessions'"
curl -fsS -X POST "${BASE}/api/sessions/init" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${SESSION}\"}" >/dev/null || fail "could not create a chat session"
CHAT=$(curl -sS -N -X POST "${BASE}/api/chat/stream" \
    -H 'Content-Type: application/json' \
    -d "{\"session\":\"${SESSION}\",\"prompt\":\"Reply with exactly: PONG\",\"model\":\"${MODEL}\"}" \
    --max-time "${NYXGPT_SMOKE_CHAT_TIMEOUT:-300}" 2>&1) || fail "the chat request failed"
echo "$CHAT" | grep -q '"content"' ||
    { echo "$CHAT" >&2; fail "chat produced no answer -- the artifact-built stack cannot chat"; }
kill "$PF_PID" 2>/dev/null || true; PF_PID=""
ok "chat answered through web -> api -> in-cluster Ollama, all from published artifacts"

step "9/9 The deployment reports its own install mode, honestly"
STATUS=$(nyx ops status)
echo "$STATUS"
echo "$STATUS" | grep -q "Install mode: artifact (images built from the published" ||
    fail "ops status does not report the Kubernetes deployment's artifact install mode"
# The #3834 report: a `[dev]` stamp on native components that are not running.
if echo "$STATUS" | grep -E "native +(api|web): none" | grep -q "\[dev\]"; then
    fail "ops status stamped an install mode on a native component that is not running"
fi
nyx ops doctor >/tmp/k8s-artifact-doctor.log 2>&1 || true
grep -q "Install mode (kubernetes): artifact" /tmp/k8s-artifact-doctor.log ||
    { cat /tmp/k8s-artifact-doctor.log >&2; fail "doctor does not report the k8s install mode"; }
ok "status and doctor report the deployment that is actually running"

step "Teardown clears the record"
nyx ops down --kubernetes
[ ! -f "${HOME}/.nyxGPT/install-mode-kubernetes.json" ] ||
    fail "the install-mode record survived teardown -- status would describe a deployment \
that no longer exists"
ok "nyxgpt ops down --kubernetes removed the deployment and its install-mode record"

echo
echo "[PASS] --kubernetes installs, chats and reports honestly with no checkout (#3834)"
