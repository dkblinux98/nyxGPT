#!/usr/bin/env python3
"""Executed evidence for #4136: the stale-record case, driven end to end.

**The question this answers.** On 2026-10-03 `nyxgpt cloud deploy --os macos`
read a `~/.nyxGPT/cloud/state.json` naming a Dedicated Host AWS had released
three days earlier, announced "no new host, no new 24-hour minimum", skipped the
priced disclosure and the `allocate` consent prompt, and allocated a new host.
The acceptance criterion (D-006) is a run against real or recorded AWS responses
covering that case end to end -- not unit fixtures, because the defect was not in
any one function's logic. Every unit test in `tests/unit/test_cloud_mac.py`
passed through all three occurrences of this mechanism (#3993, #4122, #4136).

**How it runs.** A real `nyxgpt cloud deploy --os macos` subprocess, against the
`~/.nyxGPT` layout the owner's machine actually held, with boto3 pointed at a
local server replaying **recorded** AWS responses (`tests/fixtures/aws/`,
captured from the API shapes EC2 / the Pricing API / Cost Explorer return). The
CLI, argparse, the deploy flow, boto3's own error parsing and the consent prompt
are all the real thing; only the AWS endpoint is substituted.

**Both halves are proved** (the #3753 fault-injection template: a job that only
runs the happy path passes on every machine that fails to reproduce the bug).
The discriminator under test is AWS's answer, so the scenarios differ only in
the recorded response:

* `pre-fix`   -- the negative control. The pre-#4136 behaviour of the two layers
  the fix added, restored at the boundary (`PRE_FIX_DRIVER`), against the same
  recorded response and the same stale record. It must reproduce the defect:
  "no new host, no new 24-hour minimum", no disclosure, no prompt. If it does
  not, `released` passing proves nothing.
* `released`  -- DescribeHosts answers `InvalidHostID.NotFound`. The deploy must
  take the FULL allocation path: priced disclosure, the `allocate` prompt, and
  nothing applied when it is declined. It must NOT say "no new host".
* `allocated` -- DescribeHosts answers with the host. The deploy must reconcile
  it, must NOT re-disclose a charge already made (#4122), and must say it
  confirmed the host at AWS in this run.
* `status`    -- `nyxgpt cloud status` must report the spend Cost Explorer
  returns ($12.02 for the window the issue quotes, not the $48.44 the local
  `rate * elapsed` estimate produced) and must only say "still billing" of a
  host it confirmed.

Usage:
    python scripts/cloud_stale_record_smoke.py              # every scenario
    python scripts/cloud_stale_record_smoke.py --scenario released
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "aws"

# The record the owner's machine held after the 2026-10-03 deploy, verbatim from
# the issue. Two fields describe the new substrate; four describe the released
# host; `mac_release_scheduled_at` is 78 seconds BEFORE the host it claims to
# describe was allocated.
STALE_STATE = {
    "mac_instance_id": "i-00e566c3560462cd4",
    "mac_public_ip": "34.201.63.175",
    "mac_host_id": "h-06c438d25077be888",
    "mac_instance_type": "mac2.metal",
    "mac_region": "us-east-1",
    "mac_availability_zone": "us-east-1a",
    "mac_allocated_at": "2026-09-30T15:49:43+00:00",
    "mac_release_at": "2026-10-01T16:19:43+00:00",
    "mac_hourly_rate": 0.65,
    "mac_release_scheduled": True,
    "mac_release_scheduled_at": "2026-10-03T19:25:47.725000+00:00",
}

STALE_HOST_ID = STALE_STATE["mac_host_id"]

# The Mac root's Terraform state, in the shape Terraform's local backend
# actually writes -- `allocated_host_from_state` parses this file, and on
# 2026-10-03 it held the released host, which is why "Terraform's state names it"
# is not evidence the host exists.
MAC_TFSTATE = {
    "version": 4,
    "terraform_version": "1.9.5",
    "resources": [
        {
            "mode": "managed",
            "type": "aws_ec2_host",
            "name": "this",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 0,
                    "attributes": {
                        "id": STALE_HOST_ID,
                        "availability_zone": "us-east-1a",
                        "instance_type": "mac2.metal",
                    },
                }
            ],
        }
    ],
}


# --- The recorded-response server --------------------------------------


class _RecordedAws(BaseHTTPRequestHandler):
    """Replay recorded AWS responses, dispatching on the action the SDK asks for.

    EC2 speaks the query protocol (form-encoded POST, XML reply, errors as HTTP
    400 with an XML body); the Pricing API and Cost Explorer speak JSON with the
    action in `X-Amz-Target`. Both shapes are reproduced rather than simplified,
    because botocore's own parsing is part of what is under test -- turning an
    `InvalidHostID.NotFound` into a `ClientError` with that code on
    `response["Error"]["Code"]` is precisely the step the fix depends on.
    """

    #: Set per-run by `serve`.
    host_present = False
    calls: list[str] = []

    def log_message(self, *_args):  # noqa: A003 - BaseHTTPRequestHandler's hook
        """Silence the default per-request logging; the driver prints a summary."""

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's hook
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        target = str(self.headers.get("X-Amz-Target") or "")

        if target:
            action = target.split(".")[-1]
            self._record(action)
            self._json(_fixture_text(f"{_snake(action)}.json"))
            return

        fields = urllib.parse.parse_qs(body)
        action = (fields.get("Action") or [""])[0]
        self._record(action)
        if action == "DescribeHosts":
            if self.host_present:
                self._xml(_fixture_text("describe_hosts_available.xml"))
            else:
                # HTTP 400 with the error document -- the status code matters:
                # botocore raises ClientError off it, and the whole 2026-10-03
                # incident was that exception being read as "could not ask".
                self._xml(_fixture_text("describe_hosts_not_found.xml"), status=400)
            return
        if action == "DescribeInstanceTypeOfferings":
            self._xml(_fixture_text("describe_instance_type_offerings.xml"))
            return
        self._xml(_fixture_text("describe_hosts_not_found.xml"), status=400)

    def _record(self, action: str) -> None:
        type(self).calls.append(action)

    def _xml(self, payload: str, status: int = 200) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, payload: str, status: int = 200) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _snake(action: str) -> str:
    """`GetCostAndUsage` -> `get_cost_and_usage`, to name its fixture file."""
    out: list[str] = []
    for index, char in enumerate(action):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _fixture_text(name: str) -> str:
    """Read a recorded response, with a clear error when one is missing."""
    path = FIXTURES / name
    if not path.exists():
        raise AssertionError(f"no recorded AWS response at {path}")
    return path.read_text(encoding="utf-8")


def serve(host_present: bool) -> tuple[HTTPServer, str]:
    """Start the replay server and return it with its base URL."""
    _RecordedAws.host_present = host_present
    _RecordedAws.calls = []
    server = HTTPServer(("127.0.0.1", 0), _RecordedAws)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# --- The run -----------------------------------------------------------


def _prepare_home(root: Path) -> Path:
    """Write the `~/.nyxGPT` layout the owner's machine held, and return HOME."""
    cloud = root / ".nyxGPT" / "cloud"
    cloud.mkdir(parents=True)
    (cloud / "state.json").write_text(json.dumps(STALE_STATE, indent=2) + "\n", encoding="utf-8")
    (cloud / "mac.tfstate").write_text(json.dumps(MAC_TFSTATE, indent=2) + "\n", encoding="utf-8")
    # Saved substrate settings, so the run needs no public-IP detection and no
    # SSH key prompt -- neither is what is under test, and both would make this
    # depend on the internet rather than on the recorded responses.
    (cloud / "infra.json").write_text(
        json.dumps(
            {
                "aws_region": "us-east-1",
                "aws_profile": "",
                "owner_ip_cidr": "198.51.100.5/32",
                "ssh_key_name": "nyxgpt-smoke",
                "name_prefix": "nyxgpt-tf",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _env(home: Path, endpoint: str) -> dict[str, str]:
    """The subprocess environment: fake credentials, recorded endpoints, no brew."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AWS_ACCESS_KEY_ID"] = "AKIAIOSFODNN7EXAMPLE"
    env["AWS_SECRET_ACCESS_KEY"] = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    env["AWS_REGION"] = "us-east-1"
    env.pop("AWS_PROFILE", None)
    # botocore's per-service endpoint overrides (service id, uppercased). The
    # global one is set too, so this does not depend on which spelling a given
    # botocore release honours.
    env["AWS_ENDPOINT_URL"] = endpoint
    env["AWS_ENDPOINT_URL_EC2"] = endpoint
    env["AWS_ENDPOINT_URL_PRICING"] = endpoint
    env["AWS_ENDPOINT_URL_COST_EXPLORER"] = endpoint
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    # GitHub's Ubuntu images carry Homebrew, and `ensure_terraform_binary`
    # installs Terraform through it. Nothing here should reach Terraform at all;
    # removing brew from PATH makes that a fast, legible failure instead of a
    # several-minute install.
    env["PATH"] = os.pathsep.join(
        part for part in env.get("PATH", "").split(os.pathsep) if "linuxbrew" not in part
    )
    return env


# The pre-#4136 behaviour of the two layers the fix added, reconstructed at the
# boundary rather than by editing the product source -- the same shape as
# macos-brew-smoke.yml's `mac_ver()` fault injection. A job that only runs the
# fixed path passes on any build, including one where nothing was fixed.
PRE_FIX_DRIVER = '''\
"""Drive the CLI with the pre-#4136 behaviour restored, to prove it reproduces."""

import sys

from nyxgpt import cloud_mac

_client = cloud_mac._client


def host_still_allocated(host_id, region, profile=""):
    """The old version: EVERY exception becomes "could not ask AWS".

    `InvalidHostID.NotFound` -- EC2 saying the host does not exist -- arrives as
    a ClientError, so this turned the clearest possible *no* into "unknown", and
    `reconcile_released_host` only acts on a definite no.
    """
    if not host_id:
        return False
    try:
        response = _client("ec2", region, profile).describe_hosts(HostIds=[host_id])
    except Exception:
        return None
    for host in response.get("Hosts", []):
        if str(host.get("HostId") or "") != host_id:
            continue
        return not str(host.get("State") or "").startswith("released")
    return False


cloud_mac.host_still_allocated = host_still_allocated
# And the old premise for skipping the disclosure: "the record names a host".
cloud_mac.host_confirmed_this_run = lambda record, now=None: bool(record.get("mac_host_id"))

from nyxgpt.cli import cli  # noqa: E402 - after the patches, deliberately

sys.exit(cli())
'''


def run_deploy(
    home: Path, endpoint: str, *, answer: str, driver: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real `nyxgpt cloud deploy --os macos`, answering the prompt with `answer`."""
    entry = [str(driver)] if driver else ["-m", "nyxgpt"]
    return subprocess.run(
        [
            sys.executable,
            *entry,
            "cloud",
            "deploy",
            "--os",
            "macos",
            "--region",
            "us-east-1",
            "--owner-ip",
            "198.51.100.5",
            "--ssh-key-name",
            "nyxgpt-smoke",
            "--version",
            "3.0.1",
            "--no-tunnel",
        ],
        input=answer,
        capture_output=True,
        text=True,
        env=_env(home, endpoint),
        cwd=str(REPO_ROOT),
        timeout=300,
    )


def _report(name: str, completed: subprocess.CompletedProcess[str]) -> str:
    """Print the transcript -- this log IS the evidence -- and return it."""
    print(f"\n=== scenario: {name} ===")
    print(f"--- exit code: {completed.returncode}")
    print("--- stdout ---")
    print(completed.stdout)
    print("--- stderr ---")
    print(completed.stderr)
    return (completed.stdout or "") + (completed.stderr or "")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def scenario_released() -> None:
    """A recorded host AWS does not know: the full disclosure-and-consent path."""
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            # "no" at the prompt. A declined prompt is what makes the assertions
            # below meaningful: nothing can be allocated, so a pass cannot come
            # from the disclosure being printed after the fact.
            completed = run_deploy(home, endpoint, answer="no\n")
            output = _report("released", completed)

            _require(
                "DescribeHosts" in _RecordedAws.calls,
                "the deploy never asked AWS about the recorded host",
            )
            _require(
                f"{STALE_HOST_ID} has been released" in output,
                "InvalidHostID.NotFound was not acted on as 'the host is gone'",
            )
            _require(
                "no new host, no new 24-hour minimum" not in output,
                "the deploy claimed 'no new host' for a host AWS has released",
            )
            _require(
                "Minimum charge" in output and "24-hour minimum" in output,
                "the priced disclosure was not printed",
            )
            _require(
                "Type `allocate`" in output,
                "the `allocate` consent prompt was not required",
            )
            _require(
                "nothing was allocated and nothing is billed" in output,
                "declining the prompt did not stop the allocation",
            )
            _require(completed.returncode != 0, "a declined deploy exited 0")

            # The record now contains only current state: the stale block is
            # gone, not relabelled, and the superseded copy is under a different
            # name.
            state_file = home / ".nyxGPT" / "cloud" / "state.json"
            state = (
                json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            )
            for key in STALE_STATE:
                _require(key not in state, f"{key} survived in state.json")
            archive = home / ".nyxGPT" / "cloud" / "state-archive.jsonl"
            _require(archive.exists(), "the superseded block was dropped without being archived")
            _require(
                STALE_HOST_ID in archive.read_text(encoding="utf-8"),
                "the archive does not hold the superseded block",
            )
            print("--- PASS: released host took the full disclosure-and-consent path")
    finally:
        server.shutdown()


def scenario_allocated() -> None:
    """A recorded host AWS confirms: reconciled, and never re-disclosed (#4122)."""
    server, endpoint = serve(host_present=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            completed = run_deploy(home, endpoint, answer="")
            output = _report("allocated", completed)

            _require(
                "DescribeHosts" in _RecordedAws.calls,
                "the deploy never asked AWS about the recorded host",
            )
            _require(
                "confirmed at AWS in this run" in output,
                "the reconcile did not state that AWS confirmed the host",
            )
            _require(
                "Type `allocate`" not in output,
                "a charge already made was re-disclosed (#4122)",
            )
            _require(
                f"{STALE_HOST_ID} has been released" not in output,
                "a host AWS confirmed was treated as released",
            )
            # It then needs Terraform, which this environment deliberately does
            # not have. Reaching that failure is the proof the reconcile path was
            # taken; what matters is which path, not that it completed.
            _require(
                "terraform" in output.lower(),
                "the reconcile did not reach the apply it is supposed to reach",
            )
            print("--- PASS: confirmed host was reconciled without a second disclosure")
    finally:
        server.shutdown()


def run_status(home: Path, endpoint: str) -> subprocess.CompletedProcess[str]:
    """Run the real `nyxgpt cloud status` against the recorded responses."""
    return subprocess.run(
        [sys.executable, "-m", "nyxgpt", "cloud", "status"],
        capture_output=True,
        text=True,
        env=_env(home, endpoint),
        cwd=str(REPO_ROOT),
        timeout=300,
    )


def scenario_status() -> None:
    """The spend figure is AWS's, and "still billing" is only said of a confirmed host.

    The owner's display read $48.44 for a host AWS billed $12.02 for and had
    stopped charging for two days earlier, because `rate * (now - allocated_at)`
    cannot stop counting. The recorded Cost Explorer response is the real
    breakdown from the issue (09-30 $3.90, 10-01 $8.12, 10-02 $0.00, 10-03
    $0.00), so what this prints is checkable against it by hand.
    """
    server, endpoint = serve(host_present=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            completed = run_status(home, endpoint)
            output = _report("status", completed)

            _require(completed.returncode == 0, "`nyxgpt cloud status` failed")
            _require(
                "GetCostAndUsage" in _RecordedAws.calls,
                "the spend figure was not read from Cost Explorer",
            )
            _require(
                "USD 12.02 from AWS Cost Explorer" in output,
                "the spend row did not report AWS's figure",
            )
            _require(
                "still billing -- AWS confirmed" in output,
                "'still billing' was not backed by a confirmation from this run",
            )
            _require(
                "Local ESTIMATE only" not in output,
                "a local estimate was shown where AWS's figure was available",
            )
            # The incoherent field the stale record carried is reported, not used.
            _require(
                "INCOHERENT" in output,
                "the impossible release_scheduled_at was not reported",
            )
            print("--- PASS: the spend came from Cost Explorer and the host was confirmed")
    finally:
        server.shutdown()


def scenario_pre_fix() -> None:
    """The negative control: with the pre-#4136 behaviour, the defect reproduces.

    Same recorded `InvalidHostID.NotFound`, same stale record, same command --
    and it announces "no new host, no new 24-hour minimum" with no disclosure and
    no prompt, which is what happened on 2026-10-03. Without this, `released`
    passing would prove only that *this* build discloses, not that the fix is
    what makes it.
    """
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _prepare_home(root)
            driver = root / "pre_fix_driver.py"
            driver.write_text(PRE_FIX_DRIVER, encoding="utf-8")

            completed = run_deploy(home, endpoint, answer="no\n", driver=driver)
            output = _report("pre-fix (negative control)", completed)

            _require(
                "no new host, no new 24-hour minimum" in output,
                "the pre-fix behaviour did not reproduce the defect -- this control "
                "proves nothing, so the positive scenario proves nothing either",
            )
            _require(
                "Type `allocate`" not in output,
                "the pre-fix behaviour unexpectedly asked for consent",
            )
            _require(
                f"{STALE_HOST_ID} has been released" not in output,
                "the pre-fix behaviour unexpectedly acted on InvalidHostID.NotFound",
            )
            print(
                "--- PASS (as a FAILURE of the old code): the released host was reconciled "
                "silently, with no disclosure and no prompt"
            )
    finally:
        server.shutdown()


def main() -> int:
    """Run the requested scenarios, failing the job on the first broken claim."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["released", "allocated", "status", "pre-fix", "all"],
        default="all",
        help="Which recorded AWS answer to drive the deploy with (default: all)",
    )
    args = parser.parse_args()

    if shutil.which("terraform"):
        print(
            "note: terraform is on PATH; the `allocated` scenario will attempt a real "
            "`terraform init` against the recorded endpoint.",
            file=sys.stderr,
        )

    scenarios = {
        # The negative control first: if the old behaviour does not reproduce the
        # defect, nothing below is evidence of anything.
        "pre-fix": scenario_pre_fix,
        "released": scenario_released,
        "allocated": scenario_allocated,
        "status": scenario_status,
    }
    for name in scenarios if args.scenario == "all" else [args.scenario]:
        scenarios[name]()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
