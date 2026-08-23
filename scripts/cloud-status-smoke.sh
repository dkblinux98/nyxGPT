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
# Five phases, so a pass cannot be vacuous (the #3753 fault-injection rule):
#
#   1.  No deploy record         -> UNKNOWN, and explicitly not "not deployed"
#   1b. A failed deploy attempt  -> NOT COMPLETED, naming the phase and the
#                                   real failure -- never UNKNOWN (#3993)
#   1c. Substrate, no deploy     -> SUBSTRATE ONLY, naming the live instance
#                                   this machine's own state file records
#   2.  A deploy record present  -> the connection target is printed
#   3.  An unreachable instance  -> `cloud ops` fails with the wrapped fix,
#                                   never a raw ssh/docker instruction
#
# 1b/1c are the #3993 half: after three failed deploys, `cloud status` on the
# deploying workstation said "UNKNOWN from this machine" while a live, billing
# EC2 instance ran and `state.json` on the same disk named its instance id.
# Phase 1 immediately before them is what makes those two non-vacuous -- the
# same binary, the same $HOME, and UNKNOWN is still what it prints when there
# genuinely is no source.
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
contains "$OUT" "an instance exists and is being billed"
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
echo "All phases passed."
