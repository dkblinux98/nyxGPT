#!/usr/bin/env bash
# Executed verification for `nyxgpt cloud status` / `nyxgpt cloud ops` (#3813).
#
# Runs the commands as an operator runs them -- through the installed console
# script, against real files under $HOME/.nyxGPT -- rather than by importing
# the module. That is the half unit tests structurally cannot reach: whether
# the subcommands are wired into the CLI's argparse tree at all, whether the
# entry point resolves them, and whether they read the state files at the
# paths the deploy actually writes.
#
# Ten phases, so a pass cannot be vacuous (the #3753 fault-injection rule):
#
#   1.  No deploy record         -> UNKNOWN, and explicitly not "not deployed"
#   1b. A failed deploy attempt  -> NOT COMPLETED, naming the phase and the
#                                   real failure -- never UNKNOWN (#3993)
#   1c. Substrate, no deploy     -> SUBSTRATE ONLY, naming the live instance
#                                   this machine's own state file records
#   2.  A deploy record present  -> the connection target is printed
#   3.  An unreachable instance  -> `cloud ops` fails with the wrapped fix,
#                                   never a raw ssh/docker instruction
#   4a. Nothing recorded at all  -> `cloud ops` still refuses, which is what
#                                   makes 4b-4d mean something (#4161)
#   4b. A macOS state.json       -> every `cloud ops` inspection resolves the
#                                   Mac from its `mac_` keys with no --host
#   4c. Both substrate blocks    -> the deploy record decides, so a Mac
#                                   deployed after a Linux box is the target
#   4d. The same Mac, `tunnel`   -> the shared resolver fixed it there too
#   4e. A Linux deploy after it  -> the deploy's own family wins over that
#                                   stale record, so the install cannot land
#                                   on the Mac
#
# 4a-4e are the #4161 half: on a live `mac2.metal` during v3.0.0 acceptance
# testing, every `nyxgpt cloud ops` subcommand answered "No provisioned
# instance found" about a Mac the same CLI had just deployed, because the
# resolver read `public_ip` and a macOS deploy writes `mac_public_ip`.
#
# 1b/1c are the #3993 half: after three failed deploys, `cloud status` on the
# deploying workstation said "UNKNOWN from this machine" while a live, billing
# EC2 instance ran and `state.json` on the same disk named its instance id.
# Phase 1 immediately before them is what makes those two non-vacuous -- the
# same binary, the same $HOME, and UNKNOWN is still what it prints when there
# genuinely is no source.
#
# 1b/1c are also the #4181 half, and this script was the defect's last holdout:
# they asserted "an instance exists and is being billed" over a record nothing
# had verified, on a runner with no AWS credentials at all. Reporting an id is
# the #3993 fix and it stands; claiming the resource exists and is costing
# money from that id is what #4181 removed. Both phases now pin the honest
# sentence and the absence of the old claim, so a regression toward either
# defect fails here rather than at the owner's acceptance round.
#
# Expects `nyxgpt` on PATH (installed from the wheel by the caller) and a
# writable $HOME.

set -euo pipefail

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

contains() {
    # contains <haystack-file> <needle> -- fixed-string, so command text with
    # regex metacharacters (`--yes`, `127.0.0.1`) matches literally.
    grep -qF -- "$2" "$1" || fail "expected '$2' in:$(printf '\n')$(cat "$1")"
}

not_contains() {
    grep -qF -- "$2" "$1" && fail "did not expect '$2' in:$(printf '\n')$(cat "$1")"
    return 0
}

CLOUD_DIR="$HOME/.nyxGPT/cloud"
OUT="$(mktemp -d)/out.txt"
trap 'rm -rf "$(dirname "$OUT")"' EXIT

echo "== Phase 1: no deploy record on this machine =="
rm -rf "$CLOUD_DIR"
nyxgpt cloud status >"$OUT" 2>&1 || fail "cloud status exited non-zero with no record"
cat "$OUT"
contains "$OUT" "UNKNOWN"
# The distinction #3804 established: nothing here has checked AWS, so this
# machine must not assert that nothing is deployed.
contains "$OUT" "not the same as nothing being deployed"

echo
echo "== Phase 1b: a deploy that started here and did not finish (#3993) =="
mkdir -p "$CLOUD_DIR"
# Exactly what `nyxgpt cloud deploy` now writes before it provisions anything,
# left as a failure would leave it. No deploy.json: the deploy never got that
# far, which is the whole point.
cat >"$CLOUD_DIR/deploy-attempt.json" <<'JSON'
{
  "status": "failed",
  "phase": "provision",
  "started_at": 1.0,
  "updated_at": 2.0,
  "version": "3.0.0",
  "host": "203.0.113.10",
  "instance_id": "i-0abc123def",
  "region": "us-east-1",
  "error": "[FAIL] Could not reconcile Grafana admin credential"
}
JSON
cat >"$CLOUD_DIR/state.json" <<'JSON'
{
  "region": "us-east-1",
  "instance_id": "i-0abc123def",
  "instance_type": "t3.large",
  "public_ip": "203.0.113.10",
  "security_group_id": "sg-0abc"
}
JSON

nyxgpt cloud status >"$OUT" 2>&1 || fail "cloud status exited non-zero with a failed attempt"
cat "$OUT"
contains "$OUT" "NOT COMPLETED"
# The regression this phase exists to catch: reporting UNKNOWN while a live,
# billing instance is named on this machine's own disk.
not_contains "$OUT" "UNKNOWN"
contains "$OUT" "provision"
contains "$OUT" "Could not reconcile Grafana admin credential"
contains "$OUT" "i-0abc123def"
# #4181. This asserted "an instance exists and is being billed" over exactly
# this state -- a state.json with no verification fields, on a runner with no
# AWS credentials. That is the claim #4181 removed: nothing in the run asked
# AWS, so the ids above are a record, not a resource. The assertion now pins
# the honest sentence AND the absence of the old claim, so this phase guards
# the fix instead of encoding the defect.
contains "$OUT" "nothing confirmed it at AWS in this run"
not_contains "$OUT" "an instance exists and is being billed"
# ...and the reason given is the one that actually applies (finding 6), which
# this job is in an unusually good position to prove: it installs the wheel
# WITHOUT the `cloud` extra, so there is no boto3 here -- the owner's exact
# live condition when `cloud status` answered "NOT confirmed at AWS in this run
# -- `nyxgpt cloud status` asks" from inside `nyxgpt cloud status`. The reason
# must name the missing boto3, and must never be the wording for "nobody has
# asked", which is the one cause that cannot apply inside the command that asks.
contains "$OUT" "Why nothing confirmed them:"
contains "$OUT" "boto3 is not installed"
not_contains "$OUT" "nothing on this machine has asked AWS about it"
# Still wrapped commands only, in the state where an operator is most likely
# to reach for a raw one.
contains "$OUT" "nyxgpt cloud allow-ip"
not_contains "$OUT" "docker compose"

nyxgpt cloud status --json >"$OUT" 2>&1 || fail "cloud status --json exited non-zero"
python3 - "$OUT" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
# Describable is not the same as deployed: the three outcomes stay apart.
assert payload["source"] == "deploy-attempt", payload["source"]
assert payload["known"] is True, payload["known"]
assert payload["deployed"] is False, payload["deployed"]
assert payload["attempt"]["phase"] == "provision", payload["attempt"]
print("failed-attempt payload OK")
PY

echo
echo "== Phase 1c: a provisioned substrate with no deploy recorded against it =="
rm -f "$CLOUD_DIR/deploy-attempt.json"
nyxgpt cloud status >"$OUT" 2>&1 || fail "cloud status exited non-zero with substrate only"
cat "$OUT"
contains "$OUT" "SUBSTRATE ONLY"
not_contains "$OUT" "UNKNOWN"
contains "$OUT" "i-0abc123def"
# The same #4181 rule on the other verdict that reads the same record. This
# phase went unasserted on it, which is how one sibling of the Phase 1b
# assertion could have been corrected while this one kept the old claim.
contains "$OUT" "nothing confirmed it at AWS in this run"
not_contains "$OUT" "an instance exists and is being billed"

echo
# shellcheck disable=SC2016  # the backticks are literal: this is a banner, not
# a substitution -- quoting it with double quotes would run `nyxgpt cloud deploy`.
echo '== Phase 2: a deploy record, as `nyxgpt cloud deploy` writes it =='
mkdir -p "$CLOUD_DIR"
cat >"$CLOUD_DIR/deploy.json" <<'JSON'
{
  "version": "3.0.0",
  "profiles": ["monitoring", "tracing"],
  "ssh_user": "ec2-user",
  "identity_file": "/keys/nyxgpt.pem",
  "host": "203.0.113.10",
  "instance_id": "i-0abc123def",
  "region": "us-east-1"
}
JSON
cat >"$CLOUD_DIR/state.json" <<'JSON'
{
  "region": "us-east-1",
  "instance_id": "i-0abc123def",
  "instance_type": "t3.large",
  "public_ip": "203.0.113.10",
  "security_group_id": "sg-0abc"
}
JSON
cat >"$CLOUD_DIR/infra.json" <<'JSON'
{"aws_region": "us-east-1", "owner_ip_cidr": "198.51.100.7/32", "instance_type": "t3.large"}
JSON

nyxgpt cloud status >"$OUT" 2>&1 || fail "cloud status exited non-zero with a record"
cat "$OUT"
contains "$OUT" "DEPLOYED"
contains "$OUT" "3.0.0"
contains "$OUT" "i-0abc123def (t3.large)"
contains "$OUT" "203.0.113.10"
# The gap the issue was filed for: the SSH target and identity file.
contains "$OUT" "ec2-user@203.0.113.10"
contains "$OUT" "/keys/nyxgpt.pem"
contains "$OUT" "http://localhost:3000"
# The wrapped route to container state, and no raw one anywhere in the output.
contains "$OUT" "nyxgpt cloud ops status"
not_contains "$OUT" "docker compose"
# The raw ssh appears only as labelled diagnostics.
contains "$OUT" "Diagnostics"
contains "$OUT" "run the wrapped command, not this"

nyxgpt cloud status --json >"$OUT" 2>&1 || fail "cloud status --json exited non-zero"
python3 - "$OUT" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
assert payload["known"] is True, payload["known"]
assert payload["version"] == "3.0.0", payload["version"]
assert payload["instance_type"] == "t3.large", payload["instance_type"]
connection = payload["connection"]
assert connection["target"] == "ec2-user@203.0.113.10", connection
assert connection["identity_file"] == "/keys/nyxgpt.pem", connection
assert connection["tunnel_invocation"].endswith("ec2-user@203.0.113.10"), connection
assert "-L 8000:127.0.0.1:8000" in connection["tunnel_invocation"], connection
assert payload["commands"]["status"] == "nyxgpt cloud status", payload["commands"]
assert all(c.startswith("nyxgpt ") for c in payload["commands"].values()), payload["commands"]
print("json payload OK")
PY

echo
echo "== Phase 3: the instance cannot be reached =="
# 203.0.113.0/24 is TEST-NET-3: it is guaranteed not to route anywhere, so
# this exercises the real ssh failure path rather than a stub of it.
set +e
nyxgpt cloud ops status >"$OUT" 2>&1
code=$?
set -e
cat "$OUT"
[ "$code" -eq 1 ] || fail "expected exit 1 from an unreachable instance, got $code"
contains "$OUT" "ec2-user@203.0.113.10"
contains "$OUT" "nyxgpt cloud allow-ip"
# A failure to reach the box must not fall back to telling the operator to
# type ssh or docker themselves (CLAUDE.md's wrapper requirement).
not_contains "$OUT" "docker compose"

echo
echo "== Phase 4a: nothing recorded at all -- the refusal must still happen =="
# What makes phase 4b non-vacuous (#3753's rule): the resolver must still
# refuse when there genuinely is no instance, or "it resolved the Mac" would
# only mean "it resolves anything".
rm -rf "$CLOUD_DIR"
set +e
nyxgpt cloud ops status >"$OUT" 2>&1
code=$?
set -e
cat "$OUT"
[ "$code" -ne 0 ] || fail "cloud ops exited 0 with no instance recorded at all"
contains "$OUT" "No provisioned instance found"

echo
echo "== Phase 4b: a macOS deploy's state.json -- mac_ keys, no public_ip (#4161) =="
# The exact shape a successful `nyxgpt cloud deploy --os macos` leaves behind:
# that deploy never applies the Linux substrate, so there is no bare
# `public_ip` key anywhere in the file. `cloud ops` read only that key, so
# every wrapped inspection refused a Mac it had just deployed -- the whole
# `cloud ops` surface unreachable without re-typing `--host`.
#
# The address is TEST-NET-3 again: reaching the resolver is what is under
# test, so the right pass is the *ssh* failure against the Mac's own address,
# not "No provisioned instance found".
mkdir -p "$CLOUD_DIR"
cat >"$CLOUD_DIR/state.json" <<'JSON'
{
  "mac_host_id": "h-0a34ad0272012a987",
  "mac_instance_id": "i-0a1bd7690f11507d6",
  "mac_public_ip": "203.0.113.21",
  "mac_region": "us-east-1",
  "mac_security_group_id": "sg-0mac",
  "mac_instance_type": "mac2.metal"
}
JSON
set +e
nyxgpt cloud ops status >"$OUT" 2>&1
code=$?
set -e
cat "$OUT"
[ "$code" -eq 1 ] || fail "expected exit 1 from an unreachable Mac, got $code"
not_contains "$OUT" "No provisioned instance found"
contains "$OUT" "ec2-user@203.0.113.21"
contains "$OUT" "nyxgpt cloud allow-ip"
# Every other inspection goes through the same resolver, so none of them may
# fail on resolution either.
for inspection in doctor self-heal session-backend; do
    set +e
    nyxgpt cloud ops "$inspection" >"$OUT" 2>&1
    set -e
    not_contains "$OUT" "No provisioned instance found"
    contains "$OUT" "203.0.113.21"
done

echo
echo "== Phase 4c: a Mac deployed after a Linux one -- the record breaks the tie =="
# One state.json can hold both blocks. With the old Linux `public_ip` still
# present, reading the bare key first would point every inspection at a
# machine the operator is not running.
cat >"$CLOUD_DIR/state.json" <<'JSON'
{
  "region": "us-east-1",
  "instance_id": "i-0abc123def",
  "public_ip": "203.0.113.10",
  "security_group_id": "sg-0abc",
  "mac_host_id": "h-0a34ad0272012a987",
  "mac_instance_id": "i-0a1bd7690f11507d6",
  "mac_public_ip": "203.0.113.21",
  "mac_region": "us-east-1",
  "mac_security_group_id": "sg-0mac"
}
JSON
cat >"$CLOUD_DIR/deploy.json" <<'JSON'
{
  "version": "3.0.0",
  "os_family": "macos",
  "ssh_user": "ec2-user",
  "host": "203.0.113.21",
  "instance_id": "i-0a1bd7690f11507d6",
  "region": "us-east-1"
}
JSON
set +e
nyxgpt cloud ops status >"$OUT" 2>&1
set -e
cat "$OUT"
contains "$OUT" "ec2-user@203.0.113.21"
not_contains "$OUT" "203.0.113.10"

echo
echo '== Phase 4d: `nyxgpt cloud tunnel` resolves the same Mac (#4161) =='
# The tunnel shares the resolver, and it failed the same way on a Mac. Its
# own record is the evidence: the host it opened a tunnel *to*.
set +e
nyxgpt cloud tunnel --background >"$OUT" 2>&1
set -e
cat "$OUT"
not_contains "$OUT" "No provisioned instance found"
if [ -f "$CLOUD_DIR/tunnel.json" ]; then
    contains "$CLOUD_DIR/tunnel.json" "203.0.113.21"
    nyxgpt cloud tunnel --stop >/dev/null 2>&1 || true
else
    # ssh died before the record was written (it is connecting to an
    # unroutable address). The resolution still has to have happened -- the
    # failure must be about the tunnel, not about finding the instance.
    contains "$OUT" "Could not open the SSH tunnel"
fi

echo
echo '== Phase 4e: a Linux deploy after a Mac one must not target the Mac (#4161) =='
# The other half of the same resolution, and the dangerous one. `cloud deploy`
# resolves its install target through this resolver immediately after applying
# the substrate -- at which point deploy.json still describes the PREVIOUS
# deploy. Phase 4c is the inspection case, where that stale record is exactly
# right. Here it is exactly wrong: a plain `nyxgpt cloud deploy` after a macOS
# one is a Linux deploy (the family comes from the instance type), so taking
# `os_family: macos` off the record would send the Linux install over SSH onto
# a working EC2 Mac -- `ec2-user` on both substrates, so it connects -- while
# the instance this run just paid for sits empty.
#
# Driven through the installed package rather than `nyxgpt cloud deploy`,
# which would need AWS and a real apply. The resolver is still the shipped
# one, read from the wheel in the venv, against the same real files on disk.
#
# The state.json below is what phase 4c left: both blocks, Mac deployed last.
# 203.0.113.10 is the Linux box this run would have just applied.
python - <<'PY' >"$OUT" 2>&1
import argparse

from nyxgpt import cloud_deploy

args = argparse.Namespace(host=None, ssh_user=None, identity_file=None)
print("deploying-linux:", cloud_deploy.resolve_target(args, os_family="linux").host)
print("inspecting:", cloud_deploy.resolve_target(args).host)
PY
cat "$OUT"
contains "$OUT" "deploying-linux: 203.0.113.10"
# ...and with nobody pinning a family, the Mac is still what an inspection
# resolves, so the fix above did not simply disable the record.
contains "$OUT" "inspecting: 203.0.113.21"

echo
echo "All phases passed."
