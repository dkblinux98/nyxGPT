#!/usr/bin/env python3
"""Executed proof that the AWS substrate panel reads a real EC2 instance (#3804, #3775).

The question this answers is a runtime one that unit tests structurally
cannot: **does `nyxgpt.cloud_imds` actually talk IMDSv2 to
`169.254.169.254`, and does the substrate/deployment status then describe
the machine it is running on rather than a Terraform state file that lives
somewhere else?** Unit tests mock `urlopen`; this drives the real
`urllib.request` code path over a real socket to the real link-local address.

The runner is not an EC2 instance, so the condition is injected: a `/32`
alias for the metadata address is added to `lo` and a stand-in IMDS is
served on it. Three phases, in order, so a pass cannot be vacuous:

1. **Before the alias exists** -- no metadata service answers, so the status
   must report *unknown* (`source: none`), never "not provisioned". This is
   the rc12 defect's general form and the half that would silently pass on
   any machine if it were not asserted.
2. **With a token-gated IMDSv2 stand-in** -- every substrate fact comes back
   from the instance: id, type, region, public IP, VPC, subnet, security
   group and key pair, with `source: imds`. The deployment reports itself
   (`source: local-instance`) even though no deploy record exists here, and
   the tunnel health probe is skipped rather than run against itself. The
   stand-in refuses any read that arrives without a session token, so a
   regression to token-less IMDSv1 reads fails this phase instead of
   quietly working.
3. **With an IMDSv1-only stand-in** (the token handshake refused) -- back to
   *unknown*. The substrate mandates `http_tokens = "required"`, so a box
   that cannot complete the handshake is not one we can describe.

Run it as `python3 scripts/cloud-imds-smoke.py`; it re-executes itself under
`sudo` for the two privileged parts (the address alias and binding port 80)
and removes the alias again on the way out, including on failure.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMDS_IP = "169.254.169.254"
IMDS_PORT = 80

# What the stand-in reports. Deliberately unlike anything the surrounding
# tests use, so a fact that is echoed rather than read is obvious.
FACTS = {
    "meta-data/instance-id": "i-0smoke1234567890",
    "meta-data/instance-type": "m5.large",
    "meta-data/placement/region": "eu-west-2",
    "meta-data/placement/availability-zone": "eu-west-2b",
    "meta-data/public-ipv4": "203.0.113.77",
    "meta-data/local-ipv4": "10.7.0.33",
    "meta-data/mac": "0a:aa:bb:cc:dd:ee",
    "meta-data/network/interfaces/macs/0a:aa:bb:cc:dd:ee/vpc-id": "vpc-0smokevpc",
    "meta-data/network/interfaces/macs/0a:aa:bb:cc:dd:ee/subnet-id": "subnet-0smokesub",
    "meta-data/network/interfaces/macs/0a:aa:bb:cc:dd:ee/security-group-ids": "sg-0smokesg",
    "meta-data/public-keys/": "0=nyxgpt-smoke-key",
}

TOKEN = "smoke-session-token"


# --- the stand-in metadata service (privileged half) ----------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """A minimal EC2 metadata service, token-gated exactly as IMDSv2 is."""

    imdsv1_only = False

    def log_message(self, *_args: object) -> None:  # noqa: D102 - quiet
        return

    def _send(self, status: int, body: str = "") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path != "/latest/api/token" or self.imdsv1_only:
            self._send(400)
            return
        if not self.headers.get("X-aws-ec2-metadata-token-ttl-seconds"):
            # Real IMDS refuses a handshake with no TTL header.
            self._send(400)
            return
        self._send(200, TOKEN)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.headers.get("X-aws-ec2-metadata-token") != TOKEN:
            self._send(401)
            return
        path = self.path.removeprefix("/latest/")
        if path not in FACTS:
            self._send(404)
            return
        self._send(200, FACTS[path])


def _serve(imdsv1_only: bool) -> None:
    handler = type("Handler", (_Handler,), {"imdsv1_only": imdsv1_only})
    server = http.server.ThreadingHTTPServer((IMDS_IP, IMDS_PORT), handler)
    server.serve_forever()


# --- the assertions (unprivileged half) -----------------------------------


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")
    sys.exit(1)


def _check(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)
    print(f"[ok] {message}")


def _fresh_status() -> tuple[dict, dict]:
    """Re-read substrate and deployment status with no cached IMDS answer."""
    from nyxgpt import cloud_deploy, cloud_imds, cloud_infra

    cloud_imds.reset_cache()
    infra = cloud_infra.infra_status()
    cloud_imds.reset_cache()
    deploy = cloud_deploy.deploy_status(probe_health=True)
    return infra, deploy


def _assert_not_on_ec2(phase: str) -> None:
    infra, deploy = _fresh_status()
    _check(infra["on_ec2"] is False, f"{phase}: not identified as an EC2 instance")
    _check(
        infra["source"] != "imds",
        f"{phase}: substrate source is {infra['source']!r}, not instance metadata",
    )
    _check(
        infra["known"] is False and infra["provisioned"] is False,
        f"{phase}: reported as unknown, not as 'not provisioned'",
    )
    _check(deploy["known"] is False, f"{phase}: deployment reported as unknown")


def _wait_for_imds(timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        f"http://{IMDS_IP}/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:  # noqa: S310
                if response.status == 200:
                    return
        except urllib.error.HTTPError:
            # An answer, just not a token: an IMDSv1-only stand-in is up.
            return
        except Exception:
            time.sleep(0.3)
    _fail(f"the stand-in metadata service never answered on {IMDS_IP}")


def _assert_reads_the_instance() -> None:
    infra, deploy = _fresh_status()

    _check(infra["source"] == "imds", "substrate read from instance metadata")
    _check(infra["on_ec2"] is True and infra["known"] is True, "identified as an EC2 instance")
    _check(infra["provisioned"] is True, "reported as provisioned")

    expected = {
        "instance_id": "i-0smoke1234567890",
        "instance_type": "m5.large",
        "region": "eu-west-2",
        "public_ip": "203.0.113.77",
        "private_ip": "10.7.0.33",
        "vpc_id": "vpc-0smokevpc",
        "subnet_id": "subnet-0smokesub",
        "security_group_id": "sg-0smokesg",
        "ssh_key_name": "nyxgpt-smoke-key",
    }
    for field, value in expected.items():
        _check(infra[field] == value, f"{field} read from the instance: {infra[field]!r}")

    _check(infra["access_model"]["open_ports"] == [22], "access model reported for the substrate")
    _check(
        infra["owner_ip_cidr"] == "",
        "the SSH source CIDR is left empty -- it is a rule, not metadata",
    )

    _check(deploy["source"] == "local-instance", "deployment read first-hand from the instance")
    _check(deploy["known"] is True and deploy["deployed"] is True, "deployment reported as present")
    _check(
        deploy["health"]["checked"] is False
        and "served from the instance" in deploy["health"]["reason"],
        "health probe skipped rather than run against itself",
    )


def _seed_config_if_missing() -> None:
    """Give the API a config to load, as the other smoke scripts do.

    `example.config.ini` is the template a from-scratch bring-up copies, so
    it is what a clean machine would have. Only written when there is nothing
    there -- a real machine's config is never overwritten.
    """
    config = pathlib.Path.home() / ".nyxGPT" / "config.ini"
    if config.exists():
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(pathlib.Path(__file__).resolve().parents[1] / "example.config.ini", config)
    print(f"[ok] seeded {config} from example.config.ini")


def _assert_endpoint_agrees() -> None:
    """The page consumes the HTTP endpoint, so prove that, not just the module."""
    from fastapi.testclient import TestClient

    from nyxgpt import cloud_imds
    from nyxgpt.app import app

    cloud_imds.reset_cache()
    response = TestClient(app).get("/api/v1/cloud/infra")
    _check(response.status_code == 200, "GET /api/v1/cloud/infra answered 200")
    body = response.json()
    _check(body["source"] == "imds", "the endpoint reports the instance-metadata source")
    _check(body["instance_id"] == "i-0smoke1234567890", "the endpoint reports the real instance id")


# --- orchestration --------------------------------------------------------


def _sudo(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True)


def _add_alias() -> None:
    result = _sudo("ip", "addr", "add", f"{IMDS_IP}/32", "dev", "lo")
    if result.returncode != 0 and "File exists" not in result.stderr:
        _fail(f"could not alias {IMDS_IP} onto lo: {result.stderr.strip()}")


def _remove_alias() -> None:
    _sudo("ip", "addr", "del", f"{IMDS_IP}/32", "dev", "lo")


def _start_server(*, imdsv1_only: bool) -> subprocess.Popen[bytes]:
    argv = ["sudo", "-n", sys.executable, os.path.abspath(__file__), "--serve"]
    if imdsv1_only:
        argv.append("--imdsv1-only")
    process = subprocess.Popen(argv)
    _wait_for_imds()
    return process


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    # Started under sudo, so the child is root-owned: ask sudo to kill it.
    _sudo("kill", str(process.pid))
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _sudo("kill", "-9", str(process.pid))
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run the stand-in metadata service")
    parser.add_argument("--imdsv1-only", action="store_true", help="refuse the token handshake")
    args = parser.parse_args()

    if args.serve:
        _serve(args.imdsv1_only)
        return 0

    if shutil.which("sudo") is None:
        _fail("sudo is required to alias the metadata address and bind port 80")

    print("== Phase 1: no metadata service (the machine this actually runs on)")
    _assert_not_on_ec2("with no IMDS reachable")

    server: subprocess.Popen[bytes] | None = None
    try:
        _add_alias()

        print("== Phase 2: a token-gated IMDSv2 stand-in on the real address")
        server = _start_server(imdsv1_only=False)
        _assert_reads_the_instance()
        _seed_config_if_missing()
        _assert_endpoint_agrees()
        _stop_server(server)
        server = None

        print("== Phase 3: the same address, IMDSv1 only (the handshake refused)")
        server = _start_server(imdsv1_only=True)
        _assert_not_on_ec2("with only IMDSv1 available")
    finally:
        if server is not None:
            _stop_server(server)
        _remove_alias()

    print("\nAll phases passed: the substrate panel reads the instance it runs on.")
    print(json.dumps({"smoke": "cloud-imds", "phases": 3, "outcome": "passed"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
