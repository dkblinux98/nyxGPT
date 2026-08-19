#!/usr/bin/env bash
# Executed verification for `nyxgpt cloud deploy --os` (#3867).
#
# The question this answers is not "does the dispatch return the right
# string" -- unit tests cover that by importing the module. It is the one
# they structurally cannot reach: when an operator runs the installed
# `nyxgpt`, does the CLI itself put the target OS's bootstrap onto the
# instance over SSH, or does something still have to be carried there by
# hand? Before #3867 the answer for macOS was "by hand": `nyxgpt cloud
# user-data --os macos` printed a script and a human pasted it into an AWS
# console instance launch, which is the raw-operations flow CLAUDE.md's
# Operational Command Wrapping requirement forbids.
#
# So this runs a real sshd on the runner, points the real `nyxgpt cloud
# deploy` at it, and reads what arrived. The authorized-keys entry carries a
# forced command that captures stdin and $SSH_ORIGINAL_COMMAND instead of
# executing them -- so the ssh client, the sshd, the connection and the
# delivery are all real, while the bootstrap itself is inspected rather than
# run (running the EC2 Mac script on a Linux runner would only prove that
# `dscl` is missing).
#
# Four phases, so a pass cannot be vacuous (the #3753 fault-injection rule):
#
#   1. `--os macos` with no Mac  -> fails, naming the Dedicated Host and its
#                                   24-hour minimum, and having applied
#                                   nothing: no Terraform directory exists
#                                   afterwards, so the failure cost nothing
#   2. `--os macos --host ...`   -> the CLI delivers the EC2 Mac bootstrap
#                                   itself, elevated, and records the family
#   3. os_family=linux, same box -> the Linux bootstrap arrives instead, over
#                                   the same path. This is what makes phase 2
#                                   non-vacuous: a deploy that shipped one
#                                   hard-coded script regardless of --os would
#                                   fail exactly one of these two
#   4. `cloud status`            -> an operator who lost the scrollback can
#                                   still see which OS is on that box
#
# Expects `nyxgpt` on PATH (installed from the wheel by the caller), a
# writable $HOME for nyxGPT's own state, and permission to run sshd on
# localhost.

set -euo pipefail

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

contains() {
    # contains <file> <needle> -- fixed-string, so shell metacharacters in
    # command text match literally.
    grep -qF -- "$2" "$1" || fail "expected '$2' in:$(printf '\n')$(cat "$1")"
}

not_contains() {
    grep -qF -- "$2" "$1" && fail "did not expect '$2' in:$(printf '\n')$(cat "$1")"
    return 0
}

SSH_USER="$(id -un)"
REAL_HOME="$(getent passwd "$SSH_USER" | cut -d: -f6)"
CAPTURE_DIR="$(mktemp -d)"
WORK="$(mktemp -d)"
OUT="$WORK/out.txt"
KEY="$WORK/id_ed25519"
trap 'rm -rf "$WORK"' EXIT

echo "== Setup: a real sshd on localhost, with a capturing forced command =="

# The forced command. sshd runs this *instead of* whatever the client asked
# for and exposes the original request in $SSH_ORIGINAL_COMMAND, which is
# exactly the two things under test: what nyxGPT sent, and how it asked for
# it to be run.
cat >"$CAPTURE_DIR/capture.sh" <<CAPTURE
#!/usr/bin/env bash
printf '%s' "\${SSH_ORIGINAL_COMMAND:-}" > "$CAPTURE_DIR/cmd.txt"
cat > "$CAPTURE_DIR/script.sh"
CAPTURE
chmod 0755 "$CAPTURE_DIR/capture.sh"
chmod 0755 "$CAPTURE_DIR"

ssh-keygen -t ed25519 -N '' -q -f "$KEY"
mkdir -p "$REAL_HOME/.ssh"
chmod 700 "$REAL_HOME/.ssh"
printf 'command="%s",no-pty,no-x11-forwarding %s\n' \
    "$CAPTURE_DIR/capture.sh" "$(cat "$KEY.pub")" >>"$REAL_HOME/.ssh/authorized_keys"
chmod 600 "$REAL_HOME/.ssh/authorized_keys"

sudo systemctl start ssh 2>/dev/null || sudo service ssh start

# The client's own $HOME is nyxGPT's state directory below, not $REAL_HOME,
# so give it a .ssh to write known_hosts into.
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# Prove the capture path works before anything is under test, so a later
# empty capture means "nyxGPT sent nothing", not "sshd was never up".
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
    -o IdentitiesOnly=yes "$SSH_USER@127.0.0.1" 'a-probe-command' </dev/null
contains "$CAPTURE_DIR/cmd.txt" "a-probe-command"
rm -f "$CAPTURE_DIR/cmd.txt" "$CAPTURE_DIR/script.sh"

CLOUD_DIR="$HOME/.nyxGPT/cloud"

echo
echo "== Phase 1: --os macos with no Mac to run on =="
rm -rf "$CLOUD_DIR"
if nyxgpt cloud deploy --os macos --version 3.0.0 >"$OUT" 2>&1; then
    fail "deploy --os macos succeeded with no target; it must refuse"
fi
cat "$OUT"
# It names the constraint and what it costs, per the issue's requirement that
# the wrapped flow surface the price before anything is allocated.
contains "$OUT" "Dedicated Host"
contains "$OUT" "24-hour minimum"
# And the way out is another wrapped command...
contains "$OUT" "nyxgpt cloud deploy --os macos --host"
# ...never a script to carry through the AWS console, which is the whole
# defect (#3867).
not_contains "$OUT" "paste"
not_contains "$OUT" "console"
not_contains "$OUT" "user-data"
# Nothing was applied: no Terraform working directory was ever materialized,
# so the refusal cost the operator nothing.
if [ -d "$CLOUD_DIR/terraform" ]; then
    fail "the refusal still materialized $CLOUD_DIR/terraform"
fi
if [ -f "$CLOUD_DIR/deploy.json" ]; then
    fail "the refusal still wrote a deploy record"
fi

echo
echo "== Phase 2: --os macos against a supplied target =="
nyxgpt cloud deploy \
    --os macos \
    --host 127.0.0.1 \
    --ssh-user "$SSH_USER" \
    --identity-file "$KEY" \
    --version 3.0.0 \
    --no-tunnel >"$OUT" 2>&1 || { cat "$OUT"; fail "deploy --os macos --host exited non-zero"; }
cat "$OUT"

# The CLI, not a human, put the bootstrap on the box.
contains "$CAPTURE_DIR/script.sh" "tap dkblinux98/nyxgpt"
contains "$CAPTURE_DIR/script.sh" "install nyxgpt-api nyxgpt-web"
contains "$CAPTURE_DIR/script.sh" "services start nyxgpt-api"
# Repo-less (CLAUDE.md, 2026-08-01): the remote tap is the only source.
not_contains "$CAPTURE_DIR/script.sh" "git clone http"
# And it asked for it to be run the way ec2-macos-init would have: as root,
# non-interactively, told which login user to install Homebrew for.
contains "$CAPTURE_DIR/cmd.txt" "sudo -n NYXGPT_TARGET_USER=$SSH_USER bash -s"

# The substrate was left alone -- reconciling it would have billed for a
# Linux instance nothing then deploys to.
if [ -d "$CLOUD_DIR/terraform" ]; then
    fail "a macOS deploy materialized $CLOUD_DIR/terraform"
fi
contains "$CLOUD_DIR/deploy.json" '"os_family": "macos"'
# Nothing on the Mac provisions a Cassandra, so its sessions default to file.
contains "$CLOUD_DIR/deploy.json" '"session_backend": "file"'

rm -f "$CAPTURE_DIR/cmd.txt" "$CAPTURE_DIR/script.sh"

echo
echo "== Phase 3: the Linux bootstrap still arrives, over the same path =="
# Driven through the module rather than `cloud deploy` because a Linux deploy
# applies the substrate first, which needs Terraform and an AWS account. The
# delivery being tested -- render, elevate, pipe over ssh -- is the same code
# either way, and it runs here from the installed wheel against the same real
# sshd. This is the phase that makes phase 2 mean something: a deploy still
# shipping one hard-coded script would fail one of the two.
python - "$SSH_USER" "$KEY" <<'PY'
import sys

from nyxgpt import cloud_deploy

user, key = sys.argv[1], sys.argv[2]
plan = cloud_deploy.DeployPlan(
    version="3.0.0",
    profiles=["monitoring"],
    ssh_user=user,
    os_family="linux",
)
target = cloud_deploy.DeployTarget(host="127.0.0.1", user=user, identity_file=key)
print(cloud_deploy.provision_instance(target, plan))
PY

contains "$CAPTURE_DIR/script.sh" 'NYXGPT_VERSION="3.0.0"'
# shellcheck disable=SC2016  # the needle is literal script text, not a
# substitution: ${NYXGPT_VERSION} is what the delivered bootstrap must contain.
contains "$CAPTURE_DIR/script.sh" 'install --quiet "nyxgpt==${NYXGPT_VERSION}"'
contains "$CAPTURE_DIR/script.sh" "ops install"
not_contains "$CAPTURE_DIR/script.sh" "dkblinux98/nyxgpt"
contains "$CAPTURE_DIR/cmd.txt" "bash -s"
not_contains "$CAPTURE_DIR/cmd.txt" "sudo"

echo
echo "== Phase 4: the deployment's target OS survives the scrollback =="
nyxgpt cloud status --no-probe >"$OUT" 2>&1 || fail "cloud status exited non-zero"
cat "$OUT"
contains "$OUT" "Target OS"
contains "$OUT" "macos"

echo
echo "PASS: nyxgpt drives both target-OS bootstraps itself, over the wrapped SSH path."
