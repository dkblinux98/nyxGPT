#!/usr/bin/env bash
# Executed verification for `nyxgpt cloud deploy --dev` (#3950, D-006).
#
# The question: **does an operator who asks to deploy their working tree to a
# cloud target actually get their working tree on the target?**
#
# Inspection cannot answer it. The claim is about two machines and the link
# between them -- what git selects, what tar packs, how the remote command is
# quoted, where the tree lands under a login user this script does not choose,
# and what the instance's own venv imports afterwards. A unit test can assert
# the strings; only a run can assert the outcome. So this drives the real
# functions (`cloud_deploy.ship_working_tree`, `cloud_deploy.DEV_INSTALL_BLOCK`
# as rendered, never a copy) over a real SSH connection to a bare Amazon Linux
# 2023 container.
#
# Five phases, and phase 1 is a fault injection so a green run cannot be luck:
#
#   0. refusal     The built wheel, in a venv of its own, with no checkout
#                  above it: `nyxgpt cloud deploy --dev` must refuse, name the
#                  missing tree, and do it before the substrate is applied.
#                  Checked here rather than only in a unit test because the
#                  unit test monkeypatches the very function whose real answer
#                  is the point, and because it cannot see whether the flag
#                  reached argparse at all.
#   1. no-tree     Run the dev install block on a box that was never shipped a
#                  tree. It MUST fail, and fail *at the guard*, naming the
#                  missing directory. Measured with the guard stripped out
#                  (the "without the change" shape): the run gets as far as
#                  building the venv and then dies inside pip with "is not a
#                  valid editable requirement. It should either be a path to a
#                  local project or a VCS URL (beginning with bzr+http, ...)"
#                  -- a message about version-control URLs, ninety seconds in,
#                  for an operator whose actual problem is that a file copy
#                  did not happen. And that is the *loud* case: a stale tree
#                  left by an earlier `--dev` deploy has no such error at all
#                  and installs silently. Cheap (it dies immediately), so it
#                  is the first signal.
#   2. ship        The real transfer. Asserts the tree arrived, that an
#                  UNCOMMITTED sentinel file came with it -- which is the whole
#                  difference between "your working tree" and "the last commit"
#                  -- and that `.git` did not.
#   3. install     Execute the rendered dev install block on the box, then ask
#                  the instance's own venv two questions: does `import nyxgpt`
#                  resolve inside the shipped tree, and does
#                  `ops.dev_checkout_root()` -- the exact predicate
#                  `ops install --dev` refuses on -- answer with it. A pass
#                  here is `ops install --dev` proven not to refuse on that
#                  machine.
#   4. artifact    The unchanged default, rendered by the same function: it
#                  must still install `nyxgpt==<version>` from PyPI and must
#                  not mention the shipped tree at all. This is the "without
#                  the change, the same invocation does not" half -- the
#                  artifact script is what `cloud deploy` rendered before
#                  #3950, and it is still what a plain `cloud deploy` renders.
#
# What this does NOT cover, stated rather than left implicit: EC2, Terraform,
# cloud-init, the security group and the tunnel are all outside the container
# (`cloud-artifact-smoke.yml`'s COVERAGE_GAPS lists the same boundary), and the
# full `ops install --dev` bring-up on the instance -- npm ci, the Next dev
# server, Cassandra, Ollama -- is not run here. `linux-native-smoke.yml` covers
# the native install on Linux and `cloud-artifact-smoke.yml` covers the full
# AL2023 bring-up; what is new in #3950, and therefore what this proves, is
# that the build source on a cloud target can be a working tree.
set -euo pipefail

IMAGE_TAG="nyxgpt-cloud-dev-deploy-smoke:al2023"
CONTAINER_NAME="nyxgpt-cloud-dev-deploy-smoke"
REMOTE_DIR="/home/ec2-user/.nyxGPT/src"
# Uncommitted on purpose: it is the discriminator. A `--dev` built on
# `git archive HEAD` (the obvious wrong implementation) would ship a tree
# without this file and phase 2 would fail.
SENTINEL_REL="src/nyxgpt/_dev_deploy_smoke_sentinel.py"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORK_DIR="$(mktemp -d)"
SENTINEL_TOKEN="working-tree-$$-$(date +%s)"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -f "$REPO_ROOT/$SENTINEL_REL"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

pass() {
  echo "[OK] $*"
}

# --- The target box ------------------------------------------------------

echo "==> building the AL2023 sshd image"
docker build -q -t "$IMAGE_TAG" -f "$REPO_ROOT/scripts/cloud/al2023-sshd.Dockerfile" \
  "$REPO_ROOT/scripts/cloud" >/dev/null

ssh-keygen -q -t ed25519 -N "" -f "$WORK_DIR/id_smoke" -C nyxgpt-dev-deploy-smoke

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER_NAME" "$IMAGE_TAG" >/dev/null
docker cp "$WORK_DIR/id_smoke.pub" "$CONTAINER_NAME:/home/ec2-user/.ssh/authorized_keys"
docker exec "$CONTAINER_NAME" chown ec2-user:ec2-user /home/ec2-user/.ssh/authorized_keys
docker exec "$CONTAINER_NAME" chmod 600 /home/ec2-user/.ssh/authorized_keys

HOST_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTAINER_NAME")"
[ -n "$HOST_IP" ] || fail "could not resolve the container's address"

for _ in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=2 \
      -i "$WORK_DIR/id_smoke" "ec2-user@$HOST_IP" true >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i "$WORK_DIR/id_smoke" \
  "ec2-user@$HOST_IP" true || fail "the target box never accepted SSH"
pass "bare AL2023 box reachable at $HOST_IP"

# The box must not already have what the deploy is supposed to bring. A target
# that came with a checkout would let every phase below pass for the wrong
# reason -- the same "green by luck" condition the artifact smoke's preflight
# guards against.
docker exec "$CONTAINER_NAME" sh -c '! command -v git' >/dev/null 2>&1 \
  || fail "the target image has git -- it must not be able to fetch source itself"
docker exec "$CONTAINER_NAME" sh -c "[ ! -d $REMOTE_DIR ]" \
  || fail "the target image already has $REMOTE_DIR"
pass "preflight: no git and no source tree on the target"

remote() {
  ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i "$WORK_DIR/id_smoke" \
    "ec2-user@$HOST_IP" "$@"
}

# --- Phase 0: the refusal, from an artifact-installed CLI ----------------
#
# The acceptance criterion is about what an operator *sees*, so it is checked
# the way they meet it: the built wheel in a venv of its own, with no checkout
# above it, running the real console script. A unit test cannot answer this --
# it monkeypatches the very function whose real answer is the whole point, and
# it never finds out whether the flag is wired into argparse at all.

echo "==> phase 0: --dev from an installed package must refuse"
python3 -m pip install --quiet --upgrade build
python3 -m build --wheel --outdir "$WORK_DIR/dist" "$REPO_ROOT" >/dev/null
python3 -m venv "$WORK_DIR/artifact-venv"
"$WORK_DIR/artifact-venv/bin/pip" install --quiet --upgrade pip
"$WORK_DIR/artifact-venv/bin/pip" install --quiet "$WORK_DIR"/dist/*.whl

# From a directory that is not the checkout, so nothing above the installed
# package could be mistaken for one.
if (cd "$WORK_DIR" && "$WORK_DIR/artifact-venv/bin/nyxgpt" cloud deploy --dev) \
    >"$WORK_DIR/phase0.log" 2>&1; then
  cat "$WORK_DIR/phase0.log" >&2
  fail "an artifact-installed nyxgpt accepted --dev -- it has no working tree to deploy"
fi
grep -q -- "--dev deploys your working tree" "$WORK_DIR/phase0.log" \
  || { cat "$WORK_DIR/phase0.log" >&2; fail "it refused, but not with the --dev diagnostic"; }
# It has to refuse *before* the substrate: an operator who mistypes this must
# not be billed for an EC2 instance to find out.
if grep -qi "terraform\|instance i-" "$WORK_DIR/phase0.log"; then
  cat "$WORK_DIR/phase0.log" >&2
  fail "it reached the substrate before refusing"
fi
pass "phase 0: refused before touching AWS, naming the missing checkout"

# --- The install block, rendered by the product, never retyped -----------

python3 - "$WORK_DIR/dev-install.sh" <<'PY'
"""Write the dev install block exactly as `render_provision_script` splices it.

Taken from the module rather than retyped here: a copy in this script would
pass forever after the product's own block changed, which is the failure mode
a smoke exists to prevent.
"""
import sys
from pathlib import Path

from nyxgpt import cloud_deploy

block = cloud_deploy.DEV_INSTALL_BLOCK.replace("__REMOTE_SOURCE__", cloud_deploy.REMOTE_SOURCE_DIR)
# The two things the surrounding template establishes before this block runs:
# `set -euo pipefail`, and a `$PY` that meets nyxGPT's >=3.11 floor.
Path(sys.argv[1]).write_text(f"set -euo pipefail\nPY=python3.11\n{block}\n", encoding="utf-8")
PY

# --- Phase 1: the guard bites (fault injection) --------------------------

echo "==> phase 1: the dev install block on a box with no tree must fail"
if remote "bash -s" < "$WORK_DIR/dev-install.sh" >"$WORK_DIR/phase1.log" 2>&1; then
  cat "$WORK_DIR/phase1.log" >&2
  fail "the dev install block succeeded with no working tree on the box -- the guard is not load-bearing"
fi
grep -q "no working tree at" "$WORK_DIR/phase1.log" \
  || { cat "$WORK_DIR/phase1.log" >&2; fail "it failed, but not at the working-tree guard"; }
pass "phase 1: refused, naming the missing tree"

# --- Phase 2: ship the working tree --------------------------------------

echo "==> phase 2: ship the working tree"
printf 'TOKEN = "%s"\n' "$SENTINEL_TOKEN" > "$REPO_ROOT/$SENTINEL_REL"

python3 - "$HOST_IP" "$WORK_DIR/id_smoke" <<'PY'
"""Run the product's own transfer, not a hand-rolled scp."""
import sys
from pathlib import Path

from nyxgpt import cloud_deploy

target = cloud_deploy.DeployTarget(host=sys.argv[1], user="ec2-user", identity_file=sys.argv[2])
source = cloud_deploy.dev_source_root()
if source is None:
    raise SystemExit("this smoke must run from a source checkout")
record = cloud_deploy.ship_working_tree(target, Path(source))
print(f"shipped {record['files']} files ({record['archive_bytes']} bytes) to {record['remote_dir']}")
PY

remote "test -f $REMOTE_DIR/pyproject.toml" || fail "pyproject.toml did not arrive"
remote "test -d $REMOTE_DIR/src/nyxgpt" || fail "src/nyxgpt did not arrive"
remote "test -f $REMOTE_DIR/web/package.json" || fail "web/ did not arrive"
remote "test ! -e $REMOTE_DIR/.git" || fail "the git repository was shipped -- it must not be"
remote "grep -q '$SENTINEL_TOKEN' $REMOTE_DIR/$SENTINEL_REL" \
  || fail "the uncommitted sentinel did not arrive -- this shipped a commit, not the working tree"
pass "phase 2: the working tree arrived, uncommitted edits included, without .git"

# --- Phase 3: install it, and prove the box runs it -----------------------

echo "==> phase 3: install the shipped tree on the box"
remote "bash -s" < "$WORK_DIR/dev-install.sh" || fail "the dev install block failed on the box"

RESOLVED="$(remote '$HOME/.nyxGPT/venv/bin/python -c "import nyxgpt, pathlib; print(pathlib.Path(nyxgpt.__file__).resolve())"')"
case "$RESOLVED" in
  "$REMOTE_DIR"/*) pass "phase 3: the instance imports nyxgpt from $RESOLVED" ;;
  *) fail "the instance's nyxgpt resolved to $RESOLVED, not the shipped tree" ;;
esac

SENTINEL_ON_BOX="$(remote '$HOME/.nyxGPT/venv/bin/python -c "from nyxgpt import _dev_deploy_smoke_sentinel as s; print(s.TOKEN)"')"
[ "$SENTINEL_ON_BOX" = "$SENTINEL_TOKEN" ] \
  || fail "the installed package does not carry the working tree's edit ($SENTINEL_ON_BOX)"
pass "phase 3: the code the instance would serve is this working tree"

# The exact predicate `ops install --dev` refuses on, executed on the target.
# A pass here is that refusal proven not to fire on this machine -- which is
# what makes the `--dev` flag mean anything once `ops install` runs.
CHECKOUT_ON_BOX="$(remote '$HOME/.nyxGPT/venv/bin/python -c "from nyxgpt import ops; print(ops.dev_checkout_root())"')"
[ "$CHECKOUT_ON_BOX" = "$REMOTE_DIR" ] \
  || fail "ops.dev_checkout_root() on the instance answered '$CHECKOUT_ON_BOX' -- ops install --dev would refuse"
pass "phase 3: ops.dev_checkout_root() on the instance is $CHECKOUT_ON_BOX"

# --- Phase 4: the default path is still the artifact path ----------------

echo "==> phase 4: a plain deploy still installs a published release"
python3 - <<'PY'
"""The other half of the injection: what this invocation did before #3950.

Rendered by the same function the command calls, so this cannot drift from
what a plain `nyxgpt cloud deploy` actually runs.
"""
import argparse

from nyxgpt import cloud_deploy

args = argparse.Namespace(version="3.0.0", dev=False, skip_observability=True)
script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(args))
assert 'pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"' in script, "the artifact path changed"
assert cloud_deploy.REMOTE_SOURCE_DIR not in script, "the artifact path now mentions a working tree"
assert "--dev" not in script, "the artifact path now passes --dev"
print("the artifact path installs a published release and never mentions the tree")
PY
pass "phase 4: the repo-less default is unchanged"

echo
echo "cloud dev-deploy smoke: PASS"
