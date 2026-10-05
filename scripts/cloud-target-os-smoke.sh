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
# Phases, so a pass cannot be vacuous (the #3753 fault-injection rule):
#
#   1. `--os macos` with no Mac  -> reaches the allocation path (#3995) and
#                                   stops on AWS, not on the policy refusal it
#                                   used to make; the old wording and its
#                                   `--host` workaround are gone, and nothing
#                                   was billed or recorded
#   2. `--os macos --host ...`   -> the CLI delivers the EC2 Mac bootstrap
#                                   itself, elevated, and records the family.
#                                   `--version 3.0.0` is a release, so the
#                                   STABLE formulas are what must arrive
#   2b. the same with an rc      -> `--version 3.0.0rc14` must deliver the
#                                   `@3.0.0rc` formulas instead (#4122). Phase 2
#                                   and phase 2b are the pair that makes either
#                                   one mean something: a bootstrap with a
#                                   hardcoded channel fails exactly one
#   2c. the assertion, injected  -> the delivered bootstrap's own version check,
#                                   run here against a CLI reporting the wrong
#                                   version, must exit non-zero -- and zero
#                                   against the right one
#   3. os_family=linux, same box -> the Linux bootstrap arrives instead, over
#                                   the same path. This is what makes phase 2
#                                   non-vacuous for the --os dispatch: a deploy
#                                   that shipped one hard-coded script
#                                   regardless of --os would fail one of them
#   4. `cloud status`            -> an operator who lost the scrollback can
#                                   still see which OS is on that box
#   5a. `cloud screen`, unmanaged -> REFUSES on a Mac nyxGPT did not configure,
#                                   and sends nothing and generates no
#                                   credential on the way out (#4121)
#   5b. `cloud screen`, managed  -> the CLI delivers the Screen Sharing
#                                   configuration itself, loads the
#                                   loopback-only pf rule BEFORE activating the
#                                   agent, opens no security-group port, writes
#                                   ONE credential (account + VNC) before
#                                   anything listens and restarts the job that
#                                   authenticates, keeps the credential out of
#                                   every argv and off the terminal, and
#                                   `cloud status` reports the open path
#   5d. `cloud screen --local-port` -> a request for a different local port than
#                                   the open one replaces the path instead of
#                                   reporting the old port as satisfying it
#                                   (#4121, owner acceptance 2026-10-04)
#   5c. `cloud screen`, Linux    -> refuses, and the Linux status surface does
#                                   not advertise a screen that box has not got
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
# No AWS credentials on the runner, deliberately: the allocation path's first
# AWS call is the availability-zone query, so this run reaches the new code
# and stops there. That is the assertion -- it stops on *AWS*, not on a policy
# refusal nyxGPT used to make on principle.
#
# `--owner-ip` and `--ssh-public-key` are supplied so the run gets *past* the
# input resolution that precedes the AWS call: without them it would stop on
# "no SSH key configured" or on a public-IP echo service being unreachable,
# and this phase would pass for the wrong reason.
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE AWS_EC2_METADATA_DISABLED=true \
    nyxgpt cloud deploy --os macos --version 3.0.0 \
    --ssh-public-key "$KEY.pub" --owner-ip 198.51.100.5 >"$OUT" 2>&1 && \
    fail "deploy --os macos succeeded with no AWS credentials"
cat "$OUT"
# Diagnose the environment before diagnosing the CLI. Without the `[cloud]`
# extra the run stops on the boto3 requirement *before* the AWS call, and every
# assertion below would then fail with a message about the CLI's wording when
# the actual fault is how this smoke's venv was built.
if grep -qF "boto3 is required" "$OUT"; then
    fail "the venv running this smoke has no boto3, so the allocation path was
never reached. Install the wheel with the extra the CLI names:
    pip install \"\$(ls dist/*.whl)[cloud]\""
fi
# The retired refusal (#3995): nyxGPT no longer declines to allocate on
# principle, so neither its wording nor the workaround it used to offer may
# come back.
not_contains "$OUT" "cannot allocate one"
not_contains "$OUT" "nyxgpt cloud deploy --os macos --host"
# What it says instead is an AWS problem the operator can act on, and it names
# the wrapped flag that picks a zone -- never a raw `aws` command, a console
# step, or a script to paste (#3867's defect, one level down).
contains "$OUT" "availability zones"
contains "$OUT" "--mac-az"
not_contains "$OUT" "paste"
not_contains "$OUT" "console"
not_contains "$OUT" "user-data"
not_contains "$OUT" "aws ec2 allocate-hosts"
# And nothing was billed or recorded: the failure happened before any
# Terraform ran, so the operator paid nothing for it.
if [ -f "$CLOUD_DIR/mac.tfstate" ]; then
    fail "a failed allocation still wrote $CLOUD_DIR/mac.tfstate"
fi
if [ -f "$CLOUD_DIR/deploy.json" ]; then
    fail "a failed allocation still wrote a deploy record"
fi
if [ -f "$CLOUD_DIR/state.json" ] && grep -qF 'mac_host_id' "$CLOUD_DIR/state.json"; then
    fail "a failed allocation still recorded a Dedicated Host"
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
# The formulas are the ones that carry the version this deploy declared, and
# the bootstrap verifies the version it got rather than trusting the command
# (#4122). `--version 3.0.0` is a release, so these are the stable names --
# phase 2b below is the half that proves the selection is derived and not
# hardcoded, which is what this assertion USED to get wrong: it asserted the
# literal string `install nyxgpt-api nyxgpt-web`, so executed verification
# existed for this path and certified the defect.
contains "$CAPTURE_DIR/script.sh" 'NYXGPT_BREW_FORMULAS="nyxgpt-api nyxgpt-web"'
contains "$CAPTURE_DIR/script.sh" 'NYXGPT_VERSION="3.0.0"'
contains "$CAPTURE_DIR/script.sh" 'if [ "$INSTALLED_VERSION" != "$NYXGPT_VERSION" ]; then'
contains "$CAPTURE_DIR/script.sh" 'services start "$NYXGPT_BREW_API_FORMULA"'
# Repo-less (CLAUDE.md, 2026-08-01): the remote tap is the only source.
not_contains "$CAPTURE_DIR/script.sh" "git clone http"
# And it asked for it to be run the way ec2-macos-init would have: as root,
# non-interactively, told which login user to install Homebrew for.
# Staged to a file, not fed on stdin (#4122): `bash -s` reads the script from
# stdin and so does anything it runs, so a stdin-reading command consumes the
# rest of the script -- bash then exits 0 with steps silently skipped. The
# interpreter is handed a path instead, so assert the elevation AND that no
# `-s` survives.
contains "$CAPTURE_DIR/cmd.txt" "sudo -n NYXGPT_TARGET_USER=$SSH_USER bash \"\$_nyxgpt"
not_contains "$CAPTURE_DIR/cmd.txt" "bash -s"

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
echo "== Phase 2b: a release CANDIDATE deploys the candidate's own formulas =="
# The defect this phase exists for (#4122). A candidate is published as a
# separately named formula so that `brew install nyxgpt-api` keeps resolving to
# the latest *stable* -- which means the unversioned install the old bootstrap
# ran could not deploy a candidate at all. The owner's 2026-09-30 acceptance run
# declared 3.0.0rc14 and got stable 2.1.0 on the box.
#
# Non-vacuous by construction: phase 2 above asserts the STABLE names for
# `--version 3.0.0` and this one asserts the `@3.0.0rc` names for
# `--version 3.0.0rc14`, so a bootstrap that hardcoded either set would fail
# exactly one of the two. That is the property the retired assertion lacked.
nyxgpt cloud deploy \
    --os macos \
    --host 127.0.0.1 \
    --ssh-user "$SSH_USER" \
    --identity-file "$KEY" \
    --version 3.0.0rc14 \
    --no-tunnel >"$OUT" 2>&1 || { cat "$OUT"; fail "deploy --os macos rc exited non-zero"; }
cat "$OUT"

contains "$CAPTURE_DIR/script.sh" 'NYXGPT_VERSION="3.0.0rc14"'
contains "$CAPTURE_DIR/script.sh" 'NYXGPT_BREW_FORMULAS="nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc"'
contains "$CAPTURE_DIR/script.sh" 'NYXGPT_BREW_API_FORMULA="nyxgpt-api@3.0.0rc"'
# The unversioned names must not survive anywhere in a candidate bootstrap: a
# single leftover `opt/nyxgpt-api` path would read a keg that is not there.
not_contains "$CAPTURE_DIR/script.sh" "install nyxgpt-api nyxgpt-web"
not_contains "$CAPTURE_DIR/script.sh" "opt/nyxgpt-api/libexec"
not_contains "$CAPTURE_DIR/script.sh" "services start nyxgpt-api"

echo
echo "== Phase 2c: the version assertion actually fails on the wrong version =="
# Fault injection (#3753's rule): a verification step that is never made to fail
# is indistinguishable from no verification step. Run the rendered bootstrap's
# own assertion against a CLI that reports a different version, on this runner,
# and require a non-zero exit -- then against the right one, and require zero.
ASSERT_DIR="$WORK/assert"
mkdir -p "$ASSERT_DIR"
# The four lines under test, lifted from the rendered script rather than
# retyped, so this cannot drift from what is delivered.
sed -n '/^if \[ -n "\$NYXGPT_VERSION" \]; then$/,/^fi$/p' "$CAPTURE_DIR/script.sh" \
    >"$ASSERT_DIR/assert.sh"
if [ ! -s "$ASSERT_DIR/assert.sh" ]; then
    fail "could not lift the version assertion out of the delivered bootstrap"
fi
# The version is read from a file, not an environment variable: the lifted
# lines invoke the CLI through `sudo -u`, and sudo resets the environment.
cat >"$ASSERT_DIR/fake-nyxgpt" <<FAKE
#!/usr/bin/env bash
echo "nyxgpt \$(cat "$ASSERT_DIR/version.txt")"
FAKE
chmod 0755 "$ASSERT_DIR/fake-nyxgpt"
chmod 0755 "$ASSERT_DIR"
run_assertion() {
    printf '%s\n' "$1" >"$ASSERT_DIR/version.txt"
    # `sudo -u <me>` is a no-op elevation on the runner, which keeps the lifted
    # lines byte-identical to the delivered ones.
    bash -c '
        set -euo pipefail
        NYXGPT_VERSION="3.0.0rc14"
        NYXGPT_BREW_FORMULAS="nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc"
        NYXGPT_TARGET_USER="'"$SSH_USER"'"
        NYXGPT_CLI="'"$ASSERT_DIR/fake-nyxgpt"'"
        # shellcheck disable=SC1091
        source "'"$ASSERT_DIR/assert.sh"'"
    '
}
if run_assertion 2.1.0 >"$OUT" 2>&1; then
    cat "$OUT"
    fail "the bootstrap accepted 2.1.0 when 3.0.0rc14 was asked for -- the version assertion does not work"
fi
cat "$OUT"
contains "$OUT" "but this machine has 2.1.0"
echo "  -> the wrong version is refused, non-zero, naming both versions"
run_assertion 3.0.0rc14 >"$OUT" 2>&1 || { cat "$OUT"; fail "the bootstrap rejected the version it asked for"; }
cat "$OUT"
contains "$OUT" "verified nyxGPT 3.0.0rc14"
echo "  -> the right version is accepted"

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
# Also staged rather than piped (#4122) -- see the macOS assertion above.
contains "$CAPTURE_DIR/cmd.txt" 'bash "$_nyxgpt_bootstrap"'
not_contains "$CAPTURE_DIR/cmd.txt" "bash -s"
not_contains "$CAPTURE_DIR/cmd.txt" "sudo"

echo
echo "== Phase 4: the deployment's target OS survives the scrollback =="
nyxgpt cloud status --no-probe >"$OUT" 2>&1 || fail "cloud status exited non-zero"
cat "$OUT"
contains "$OUT" "Target OS"
contains "$OUT" "macos"

echo
echo "== Phase 5: nyxgpt cloud screen -- the wrapped screen path (#4121) =="
# The same question as phase 2, for the second thing that has to reach the Mac
# over SSH and used to be hand-rolled: `kickstart`, an account password and an
# `ssh -L`, typed by the operator. Unit tests prove what the module renders;
# only this proves the CLI puts it on the wire, keeps the credential off every
# argv, and opens the forward.
#
# Non-vacuous by construction, the #3753 rule: 5a asserts the command REFUSES
# on a Mac nyxGPT did not configure and sends nothing, and 5b asserts it works
# once nyxGPT's own Mac record names that host. A build that skipped the
# scoping check would pass 5b and fail 5a.
rm -f "$CAPTURE_DIR/cmd.txt" "$CAPTURE_DIR/script.sh"
SECRETS_DIR="$HOME/.nyxGPT/secrets"
VNC_SECRET="$SECRETS_DIR/cloud-mac-vnc-password"
rm -f "$VNC_SECRET"

echo "-- 5a: a Mac nyxGPT did not configure is refused, and nothing is sent"
# Phase 2 left a macOS deploy record for 127.0.0.1 and no Dedicated Host
# record, which is exactly the `--host` case: nyxGPT cannot know what that
# machine's security group exposes, so it must not enable a listener on it.
nyxgpt cloud screen --host 127.0.0.1 --ssh-user "$SSH_USER" --identity-file "$KEY" \
    >"$OUT" 2>&1 && fail "cloud screen configured a Mac nyxGPT does not manage"
cat "$OUT"
contains "$OUT" "did not configure the Mac"
contains "$OUT" "loopback"
# A refusal must be a refusal: nothing crossed the connection...
if [ -f "$CAPTURE_DIR/script.sh" ]; then
    fail 'a refused cloud screen still delivered a script to the Mac'
fi
# ...and no credential was generated for a machine it declined to touch.
if [ -f "$VNC_SECRET" ]; then
    fail 'a refused cloud screen still generated a VNC credential'
fi

echo "-- 5b: the Mac nyxGPT manages gets the loopback-only listener"
# The Dedicated Host record `nyxgpt cloud deploy --os macos` writes at
# allocation (cloud_mac.record_mac_host). Seeded rather than allocated because
# allocating one is a real 24-hour charge on real Mac hardware -- the thing
# docs/live-verification-ci.md records as unreachable from CI. Everything under
# test below (the render, the delivery, the credential, the forward) is
# unaffected by how the record got there.
python - "$CLOUD_DIR/state.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text()) if path.exists() else {}
state.update(
    {
        "mac_host_id": "h-smoke",
        "mac_instance_id": "i-smoke",
        "mac_instance_type": "mac2.metal",
        "mac_public_ip": "127.0.0.1",
        "mac_region": "us-east-1",
        "mac_security_group_id": "sg-smoke",
        "mac_allocated_at": "2026-01-01T00:00:00Z",
        "mac_release_at": "2026-01-02T00:30:00Z",
    }
)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(state))
PY

nyxgpt cloud screen --ssh-user "$SSH_USER" --identity-file "$KEY" >"$OUT" 2>&1 || {
    cat "$OUT"
    fail "cloud screen exited non-zero against the Mac nyxGPT manages"
}
cat "$OUT"

# The CLI, not a human, put the configuration script on the box -- and asked
# for it elevated the way `kickstart` and `pfctl` need.
# Staged to a file, not fed on stdin (#4122): `bash -s` reads the script from
# stdin and so does anything it runs, so a stdin-reading command consumes the
# rest of the script -- bash then exits 0 with steps silently skipped. The
# interpreter is handed a path instead, so assert the elevation AND that no
# `-s` survives.
contains "$CAPTURE_DIR/cmd.txt" "sudo -n NYXGPT_TARGET_USER=$SSH_USER bash \"\$_nyxgpt"
not_contains "$CAPTURE_DIR/cmd.txt" "bash -s"
contains "$CAPTURE_DIR/script.sh" "ARDAgent.app/Contents/Resources/kickstart"
contains "$CAPTURE_DIR/script.sh" "-activate -configure -access -on"

# The constraint that may not be traded away
# (product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md): loopback only,
# and the rule is loaded and read back BEFORE the agent is activated.
contains "$CAPTURE_DIR/script.sh" "block drop in quick proto tcp from any to any port 5900"
contains "$CAPTURE_DIR/script.sh" "pass in quick on lo0 proto tcp from any to any port 5900"
python - "$CAPTURE_DIR/script.sh" <<'PY'
import sys

script = open(sys.argv[1]).read()
block = script.index("block drop in quick proto tcp")
readback = script.index("-s rules | grep -q")
# The credential -- account password AND VNC password -- is written and
# verified before anything listens, and the job that actually authenticates on
# 5900 is restarted after it (#4121, owner acceptance 2026-10-04). Before this
# fix the VNC password was set AFTER the agent restart and `screensharingd`
# never loaded it: on the owner's host the listener started 4m21s earlier.
account = script.index("dscl . -passwd")
authonly = script.index("dscl . -authonly")
vnc_password = script.index("-setvncpw -vncpw")
activate = script.index("-activate -configure -access -on")
restart = script.index("launchctl kickstart -k system/com.apple.screensharing")
if not block < readback < account < authonly < vnc_password < activate < restart:
    raise SystemExit(
        "the delivered script's ordering is wrong. It must load and read back the "
        "loopback-only pf rule, then write and VERIFY the credential, then activate "
        "the agent, then restart com.apple.screensharing -- any other order either "
        "exposes 5900 or leaves the listener holding a credential it never loaded"
    )
print("  -> delivered ordering: pf rule, read it back, credential, activate, restart")
PY
# Absence assertions below are made against what the Mac will RUN, not against
# the script's comments. The delivered text names the APIs that were tried and
# rejected -- `sysadminctl`, `kickstart -restart -agent` -- so a grep over the
# whole file would fail on the paragraph explaining why they are not used.
grep -v '^[[:space:]]*#' "$CAPTURE_DIR/script.sh" > "$WORK/screen-commands.sh"

# No security-group port is opened, not even one scoped to the operator's /32
# -- that is the alternative the decision rejected.
not_contains "$CAPTURE_DIR/script.sh" "authorize-security-group-ingress"
not_contains "$CAPTURE_DIR/script.sh" "0.0.0.0"
# ONE credential, serving both clients: Apple's Screen Sharing.app offers
# security types 30/33 first and both authenticate against the ACCOUNT
# password, so a VNC-only credential is one the client this command's own
# output names structurally cannot use (#4121, owner acceptance 2026-10-04).
contains "$CAPTURE_DIR/script.sh" "-setvncpw -vncpw"
# On a DEDICATED account, never the login user. `ec2-user` on the EC2 macOS
# AMI holds a SecureToken, and macOS then refuses to change its password
# without the existing one -- for root too (owner acceptance 2026-10-05: the
# command aborted with "Permission denied. Please enter user's old password").
# A freshly created account carries no token, so its password is settable.
contains "$CAPTURE_DIR/script.sh" 'dscl . -passwd "/Users/$SCREEN_USER"'
not_contains "$CAPTURE_DIR/script.sh" 'dscl . -passwd "/Users/$TARGET_USER"'
contains "$CAPTURE_DIR/script.sh" "sysadminctl -addUser"
# Verified in the same step, not assumed -- and with dscl, because
# `sysadminctl -resetPasswordFor` fails on an EC2 Mac without a secure token.
# Creating an account is not resetting one, so only the reset verb is barred.
contains "$CAPTURE_DIR/script.sh" 'dscl . -authonly "$SCREEN_USER"'
not_contains "$WORK/screen-commands.sh" "-resetPasswordFor"
not_contains "$WORK/screen-commands.sh" "-secureTokenOn"
# Authenticating to Screen Sharing only reaches the console's LOGIN WINDOW,
# and that window caches its user list at launch -- so an account created
# seconds earlier is not offered and there is no "switch user" affordance.
# Without this the feature authenticates perfectly and delivers nothing an
# operator can log into (owner verified: usable only after the restart).
contains "$CAPTURE_DIR/script.sh" "killall loginwindow"
# Still nothing interactive: `sudo passwd ec2-user` is the hand-rolled step
# this command replaces, and no wrapped command can answer its prompt.
not_contains "$WORK/screen-commands.sh" "sudo passwd"
# The restart targets the process that authenticates, not ARDAgent: measured
# across a `kickstart -restart -agent`, the pid on 5900 did not change.
contains "$CAPTURE_DIR/script.sh" "launchctl kickstart -k system/com.apple.screensharing"
not_contains "$WORK/screen-commands.sh" "-restart -agent"
# And the ordering is measured on the Mac rather than trusted: the script
# reads the listener's start time back and fails if it predates the credential.
contains "$CAPTURE_DIR/script.sh" 'CREDENTIAL_WRITTEN_AT="$(date +%s)"'
contains "$CAPTURE_DIR/script.sh" "ps -o lstart= -p"
contains "$CAPTURE_DIR/script.sh" '[ "$LISTENER_STARTED_AT" -lt "$CREDENTIAL_WRITTEN_AT" ]'
# Nothing is installed on the host beyond enabling what macOS ships.
not_contains "$CAPTURE_DIR/script.sh" "brew install"
not_contains "$CAPTURE_DIR/script.sh" "git clone"

# The credential: generated, in ~/.nyxGPT/secrets, 0600, and NOT in the
# operator's scrollback or in any argv sshd was asked to run.
[ -f "$VNC_SECRET" ] || fail "no VNC credential was generated in $SECRETS_DIR"
VNC_MODE="$(stat -c '%a' "$VNC_SECRET")"
[ "$VNC_MODE" = "600" ] || fail "the VNC credential is mode $VNC_MODE, expected 600"
VNC_PASSWORD="$(cat "$VNC_SECRET")"
[ -n "$VNC_PASSWORD" ] || fail "the VNC credential file is empty"
not_contains "$OUT" "$VNC_PASSWORD"
not_contains "$CAPTURE_DIR/cmd.txt" "$VNC_PASSWORD"
# It did reach the Mac -- on the connection's stdin, which is the whole point
# of delivering a script rather than a command line.
contains "$CAPTURE_DIR/script.sh" "$VNC_PASSWORD"
# What the operator is told instead of the secret. The LOCAL port is 5901, not
# 5900: on a macOS workstation `vnc://localhost:5900` is the operator's own
# screen, and Apple's client refuses it before the forward is ever consulted
# (#4121, owner acceptance 2026-10-04).
contains "$OUT" "vnc://localhost:5901"
not_contains "$OUT" "vnc://localhost:5900"
contains "$OUT" "cloud-mac-vnc-password"
# And the account to sign in as, which is the other half of connecting now
# that the credential is the login password.
contains "$OUT" "Sign in as"
contains "$OUT" "$SSH_USER"
# Wrapped end to end: no raw command is ever presented as an instruction.
not_contains "$OUT" "ssh -L"
not_contains "$OUT" "kickstart"

echo "-- 5b: cloud status reports the screen path (observable, not operable)"
nyxgpt cloud status --no-probe >"$OUT" 2>&1 || fail "cloud status exited non-zero"
cat "$OUT"
contains "$OUT" "Screen path"
contains "$OUT" "open at vnc://localhost:5901"
not_contains "$OUT" "$VNC_PASSWORD"
nyxgpt cloud screen --status --json >"$OUT" 2>&1 || fail "cloud screen --status exited non-zero"
cat "$OUT"
contains "$OUT" '"running": true'
contains "$OUT" '"local_port": 5901'
not_contains "$OUT" "$VNC_PASSWORD"

echo "-- 5d: --local-port against an open path on another port replaces it"
# The defect this covers: `start_screen_tunnel` returned the open path without
# comparing its port to the requested one, so with a forward alive on 5901 the
# command reported 5901 for `--local-port 5902` and opened nothing -- the flag
# looked inert. Run twice on purpose; the first run above left a path open.
SCREEN_PID_BEFORE="$(python -c "
import json, pathlib
print(json.loads(pathlib.Path('$CLOUD_DIR/screen.json').read_text()).get('pid', 0))
")"
nyxgpt cloud screen --ssh-user "$SSH_USER" --identity-file "$KEY" --local-port 5902 \
    >"$OUT" 2>&1 || { cat "$OUT"; fail "cloud screen --local-port 5902 exited non-zero"; }
cat "$OUT"
contains "$OUT" "vnc://localhost:5902"
not_contains "$OUT" "vnc://localhost:5901"
contains "$OUT" "which is what was asked for"
# The replaced forward is gone, not leaked: a path reported closed has to BE
# closed, and the recorded pid has to be the new one.
# SIGTERM is not synchronous, so give the replaced child a moment to reap
# rather than racing it -- the claim under test is that it is gone, not that it
# was gone within one scheduler tick.
for _ in 1 2 3 4 5; do
    kill -0 "$SCREEN_PID_BEFORE" 2>/dev/null || break
    sleep 1
done
if kill -0 "$SCREEN_PID_BEFORE" 2>/dev/null; then
    fail "the forward on 5901 is still alive after being replaced (pid $SCREEN_PID_BEFORE)"
fi
nyxgpt cloud screen --status --json >"$OUT" 2>&1 || fail "cloud screen --status exited non-zero"
contains "$OUT" '"local_port": 5902'
# And asking again for the port that IS open is a no-op, not a third process.
nyxgpt cloud screen --ssh-user "$SSH_USER" --identity-file "$KEY" --local-port 5902 \
    >"$OUT" 2>&1 || { cat "$OUT"; fail "cloud screen on an already-open port exited non-zero"; }
contains "$OUT" "already open"

echo "-- 5b: and it closes again, leaving the listener enabled but unreachable"
nyxgpt cloud screen --stop >"$OUT" 2>&1 || fail "cloud screen --stop exited non-zero"
cat "$OUT"
contains "$OUT" "Screen path closed"
nyxgpt cloud screen --status >"$OUT" 2>&1 || fail "cloud screen --status exited non-zero"
cat "$OUT"
contains "$OUT" "closed"
# Closing the tunnel is not the same claim as turning Screen Sharing off, and
# the command must not make the stronger one.
contains "$OUT" "Screen Sharing enabled by nyxGPT"

echo "-- 5c: a Linux deployment has no screen, and the command says so"
# The other half of the scoping criterion. Driven by rewriting the deploy
# record's target OS, which is the state a Linux operator is actually in.
python - "$CLOUD_DIR/deploy.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
record = json.loads(path.read_text())
record["os_family"] = "linux"
path.write_text(json.dumps(record))
PY
nyxgpt cloud screen >"$OUT" 2>&1 && fail "cloud screen ran against a Linux deployment"
cat "$OUT"
contains "$OUT" "macOS capability"
contains "$OUT" "linux"
# And the Linux status surface does not advertise a capability that box lacks.
nyxgpt cloud status --no-probe >"$OUT" 2>&1 || fail "cloud status exited non-zero"
not_contains "$OUT" "Screen path"

echo
echo "PASS: nyxgpt drives both target-OS bootstraps and the Mac screen path itself,"
echo "      over the wrapped SSH path, with no non-loopback listener and no secret"
echo "      in any argv -- and with one credential, written and verified before"
echo "      anything listens, for the client the command's own output names."
