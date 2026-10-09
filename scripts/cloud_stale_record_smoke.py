#!/usr/bin/env python3
"""Executed evidence for #4136 and #4181: nothing is claimed that was not established.

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

**#4181: the class, not that instance.** The owner's 2026-10-09 round confirmed
the original incident was fixed and failed acceptance anyway, because rc1 still
acted on, and reported, cloud facts it had not established with the right
credentials. Six more scenarios drive the three channels that allowed it, and
two of them are negative controls:

* `wrong-account`      -- two live credential profiles and a host only one of
  them owns, with `infra.json` carrying no profile (the fresh-machine state the
  scrummaster's note insists on). The command must authenticate as the account
  `config.ini [cloud] profile` names and must never ask the other one, because
  an AWS account answers `InvalidHostID.NotFound` for every host it does not
  own -- indistinguishable from a release.
* `wrong-account-answer` -- the record already carries a confirmation obtained
  in *another* account. Resolving the right account is necessary and not
  sufficient; nothing may be claimed from that answer.
* `incoherent`         -- a record whose own fields contradict each other is
  reported and nothing is concluded from it. rc1 printed both INCOHERENT rows
  and then, from the same fields, "the scheduled release has fired", the
  October 4 instance id, and "an instance exists and is being billed".
* `status-changes-nothing` -- `nyxgpt cloud status` ran `terraform destroy` on
  the release schedule. A recording `terraform` shim on PATH proves the whole
  command invokes no Terraform mutation at all.
* `pre-fix-terraform`  -- the negative control for the credential channel. With
  `credential_env` and `_restamp_tfvars` removed at the boundary, the
  `terraform destroy` on the mac-release root is started with no AWS_PROFILE,
  which is the owner's AccessDenied.
* `terraform-credentials` -- the same command with both channels in place: the
  shim's recorded environment must name this run's profile, and the stored
  tfvars an earlier run wrote must have been re-stamped with it.
* `declined`           -- typing `no` at the priced disclosure records
  `"status": "declined"`, and the next `nyxgpt cloud status` must not report an
  unfinished deploy over a run that created nothing.

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
    #: #4181. When set, only this access key id owns the host: every other one
    #: gets `InvalidHostID.NotFound`, which is exactly what a real AWS account
    #: answers for a host it does not own. That equivalence is the whole of
    #: finding 1 -- the default account's "not found" for a host billing in
    #: another account is indistinguishable from a release.
    owner_access_key = ""
    #: Access key ids seen, in order, so a scenario can prove WHICH account the
    #: command authenticated as rather than only that it got the right answer.
    #: The owner's rc1 run got the right answer by coincidence.
    access_keys: list[str] = []

    def log_message(self, *_args):  # noqa: A003 - BaseHTTPRequestHandler's hook
        """Silence the default per-request logging; the driver prints a summary."""

    def _access_key(self) -> str:
        """The access key id botocore signed this request with, or ""."""
        authorization = str(self.headers.get("Authorization") or "")
        for part in authorization.split():
            if part.startswith("Credential="):
                return part[len("Credential=") :].split("/")[0]
        return ""

    def _owns_the_host(self) -> bool:
        """Does the caller's account own the recorded host?

        With no `owner_access_key` configured every caller does, which is the
        single-account behaviour the original scenarios were written against.
        """
        if not self.owner_access_key:
            return True
        return self._access_key() == self.owner_access_key

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's hook
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        target = str(self.headers.get("X-Amz-Target") or "")
        type(self).access_keys.append(self._access_key())

        if target:
            action = target.split(".")[-1]
            self._record(action)
            self._json(_fixture_text(f"{_snake(action)}.json"))
            return

        fields = urllib.parse.parse_qs(body)
        action = (fields.get("Action") or [""])[0]
        self._record(action)
        if action == "DescribeHosts":
            if self.host_present and self._owns_the_host():
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


def serve(host_present: bool, *, owner_access_key: str = "") -> tuple[HTTPServer, str]:
    """Start the replay server and return it with its base URL."""
    _RecordedAws.host_present = host_present
    _RecordedAws.owner_access_key = owner_access_key
    _RecordedAws.calls = []
    _RecordedAws.access_keys = []
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
# `reconcile_released_host` reads `host_presence`, which carries the reason an
# answer could not be had alongside the verdict (#4181). The pre-fix behaviour
# is the verdict-only one above, so the control replaces both -- patching the
# wrapper alone would leave the fixed reader in place and the control would
# silently stop reproducing anything.
cloud_mac.host_presence = lambda host_id, region, profile="": cloud_mac.HostPresence(
    present=host_still_allocated(host_id, region, profile)
)
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
                "Local ESTIMATE only" not in output,
                "a local estimate was shown where AWS's figure was available",
            )
            # The incoherent field the stale record carried is reported, not used.
            _require(
                "INCOHERENT" in output,
                "the impossible release_scheduled_at was not reported",
            )
            # And #4181: AWS confirming the HOST does not make a block whose own
            # fields contradict each other describe that host. rc1 printed
            # "still billing -- AWS confirmed ..." directly above these rows.
            # The `incoherent` scenario is the full statement of this; here it
            # guards the combination the owner actually had.
            _require(
                "still billing" not in output,
                "a charge was asserted over a record whose own fields contradict each other",
            )
            print(
                "--- PASS: the spend came from Cost Explorer, the host was confirmed, and the "
                "incoherent block was reported rather than believed"
            )
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


# --- #4181: two accounts, a read that changes nothing, and a declined no ---
#
# The owner's 2026-10-09 acceptance round failed #4136 not because the original
# incident reproduced -- it did not -- but because the CLASS survived: rc1 still
# acted on, and reported, cloud facts it had not established with the right
# credentials. These scenarios drive the three channels that made that possible.

# The two accounts. The nyxgpt one owns the host; the default one is the account
# the owner's `nyxgpt cloud status` actually asked (551292530955), which answers
# `InvalidHostID.NotFound` for every host it does not own.
NYXGPT_ACCESS_KEY = "AKIANYXGPTACCOUNT01"
DEFAULT_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"

# A `terraform` that runs nothing and records everything: argv, cwd, and the
# environment it was handed. Finding 2 is a question about that environment --
# "did the run's `--profile` reach the subprocess?" -- and no amount of reading
# the Python answers it. A stub on PATH does, by being a real subprocess the
# real CLI really starts.
TERRAFORM_SHIM = r"""#!/usr/bin/env python3
"""  # the shebang alone; the body follows so no escape is ambiguous
TERRAFORM_SHIM += "\n".join(
    [
        "import json, os, sys",
        'record = {"argv": sys.argv[1:],',
        '          "env": {k: v for k, v in os.environ.items() if "AWS" in k}}',
        'with open(os.environ["TERRAFORM_SHIM_LOG"], "a", encoding="utf-8") as handle:',
        "    handle.write(json.dumps(record) + chr(10))",
        "# Nothing parses this; printed so a `version` call looks like one.",
        "# Deliberately without the `v` prefix: it is Terraform's version, and",
        "# tests/unit/test_no_hardcoded_release_version.py reads `vN.N.N` in any",
        "# source file as a nyxGPT release line named in live code.",
        'if "version" in sys.argv:',
        '    print("Terraform 1.9.5")',
        'elif "output" in sys.argv:',
        '    print("{}")',
        "sys.exit(0)",
        "",
    ]
)


def _install_terraform_shim(root: Path) -> tuple[Path, Path]:
    """Put the recording `terraform` on a PATH directory; return (bindir, log)."""
    bindir = root / "shim-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "terraform"
    shim.write_text(TERRAFORM_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return bindir, root / "terraform-calls.jsonl"


def _shim_calls(log: Path) -> list[dict]:
    """Every invocation the shim recorded (empty when it was never called)."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _write_aws_profiles(home: Path) -> None:
    """Two real credential profiles, so "which account" is a live question.

    The default chain (the environment) is the account that does NOT own the
    host; `[nyxgpt]` is the one that does. That is the owner's machine: a
    `nyxgpt` profile for 066835328281 beside a default identity of
    `arn:aws:iam::551292530955:root`.
    """
    aws = home / ".aws"
    aws.mkdir(parents=True, exist_ok=True)
    (aws / "credentials").write_text(
        "[nyxgpt]\n"
        f"aws_access_key_id = {NYXGPT_ACCESS_KEY}\n"
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYNYXGPTKEY\n",
        encoding="utf-8",
    )
    (aws / "config").write_text("[profile nyxgpt]\nregion = us-east-1\n", encoding="utf-8")


def _write_config_ini(home: Path, profile: str) -> None:
    """`[cloud] profile` in ~/.nyxGPT/config.ini -- the step the chain used to miss.

    `cloud_mac._record_profile` read only `--profile` and `infra.json` before
    #4186, so an operator who had run `nyxgpt cloud credentials-setup` and
    nothing else had their host looked up in the default account. This is the
    state that makes that visible, and it is the state the scrummaster's note
    on #4181 insists on: finding 1 only bites when `infra.json` carries no
    profile -- a fresh machine, or the first command before any deploy saved one.
    """
    (home / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        f"[cloud]\nprofile = {profile}\nregion = us-east-1\n", encoding="utf-8"
    )


def run_cli(
    home: Path,
    endpoint: str,
    arguments: list[str],
    *,
    answer: str = "",
    path_prefix: Path | None = None,
    shim_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real `nyxgpt` with `arguments`, optionally with the terraform shim."""
    env = _env(home, endpoint)
    if path_prefix is not None:
        env["PATH"] = os.pathsep.join([str(path_prefix), env["PATH"]])
    if shim_log is not None:
        env["TERRAFORM_SHIM_LOG"] = str(shim_log)
    return subprocess.run(
        [sys.executable, "-m", "nyxgpt", *arguments],
        input=answer,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=300,
    )


def scenario_wrong_account() -> None:
    """Finding 1: the account is resolved from the documented chain, and recorded.

    Two live credential profiles and a host only one of them owns. `infra.json`
    carries no profile -- the fresh-machine state -- so the only thing that can
    name the right account is `config.ini [cloud] profile`. If the command asks
    the default account it gets `InvalidHostID.NotFound` and clears the record
    of a host that is still billing, which is the failure this proves cannot
    happen.
    """
    server, endpoint = serve(host_present=True, owner_access_key=NYXGPT_ACCESS_KEY)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            _write_aws_profiles(home)
            _write_config_ini(home, "nyxgpt")
            # The precondition the scrummaster named: no saved profile.
            cloud = home / ".nyxGPT" / "cloud"
            infra = json.loads((cloud / "infra.json").read_text(encoding="utf-8"))
            infra.pop("aws_profile", None)
            (cloud / "infra.json").write_text(json.dumps(infra, indent=2), encoding="utf-8")
            # A COHERENT record, so the only thing that can stop the billing row
            # being printed is the account the lookup went to. The default
            # `STALE_STATE` also claims a release schedule that does not exist
            # on this machine, which is its own (reported) incoherence -- and if
            # it were left in place this scenario would pass for that reason
            # instead of for the one it is named after.
            state = {
                key: value
                for key, value in STALE_STATE.items()
                if key not in ("mac_release_scheduled", "mac_release_scheduled_at")
            }
            (cloud / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

            completed = run_cli(home, endpoint, ["cloud", "status"])
            output = _report("wrong-account", completed)

            _require(completed.returncode == 0, "`nyxgpt cloud status` failed")
            _require(
                NYXGPT_ACCESS_KEY in _RecordedAws.access_keys,
                "the command never authenticated as the account that owns the host -- it "
                f"used {sorted(set(_RecordedAws.access_keys))}",
            )
            _require(
                DEFAULT_ACCESS_KEY not in _RecordedAws.access_keys,
                "the command asked the DEFAULT account about a host it does not own; that "
                "account answers InvalidHostID.NotFound for every such host, so this is "
                "indistinguishable from a release",
            )
            _require(
                f"{STALE_HOST_ID} has been released" not in output,
                "a host the owning account confirms was treated as released",
            )
            _require(
                "still billing" in output,
                "a host confirmed in the resolved account was not reported as billing",
            )
            print("--- PASS: the host was looked up in the account config.ini names")
    finally:
        server.shutdown()


def scenario_wrong_account_answer_is_refused() -> None:
    """The other half of finding 1: an answer from the wrong account is not evidence.

    Resolving the right account is necessary and not sufficient -- a record can
    already carry an answer some earlier run obtained elsewhere. The run asks
    as `nyxgpt`, the record says the last confirmation came from the default
    account, and nothing may be claimed from it.
    """
    server, endpoint = serve(host_present=True, owner_access_key=NYXGPT_ACCESS_KEY)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            _write_aws_profiles(home)
            cloud = home / ".nyxGPT" / "cloud"
            state = dict(STALE_STATE)
            state.update(
                {
                    "mac_host_present": True,
                    # Fresh enough to count as "this run" -- the only thing
                    # wrong with it is whose account it came from.
                    "mac_verified_at": "2099-01-01T00:00:00+00:00",
                    "mac_verified_profile": "some-other-account",
                }
            )
            (cloud / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

            completed = run_cli(
                home,
                endpoint,
                ["cloud", "status", "--json", "--no-probe", "--profile", "nyxgpt"],
            )
            _report("wrong-account-answer", completed)

            _require(completed.returncode == 0, "`nyxgpt cloud status --json` failed")
            payload = json.loads(completed.stdout)
            mac_host = payload.get("mac_host") or {}
            _require(bool(mac_host), "the payload carried no mac_host block to judge")
            _require(
                mac_host.get("usable") is False and mac_host.get("billing") is False,
                "an answer from another account was treated as a confirmation",
            )
            print("--- PASS: a confirmation from another account is not a confirmation")
    finally:
        server.shutdown()


def scenario_incoherent_record() -> None:
    """Finding 3: an incoherent record is reported, and nothing is concluded from it.

    rc1 printed both INCOHERENT rows and then, from the same fields, "the
    scheduled release has fired", the October 4 instance id, and "an instance
    exists and is being billed" -- with AWS holding no instances at all.
    """
    server, endpoint = serve(host_present=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            completed = run_cli(home, endpoint, ["cloud", "status"])
            output = _report("incoherent", completed)

            _require(completed.returncode == 0, "`nyxgpt cloud status` failed")
            _require("INCOHERENT" in output, "the impossible record was not reported")
            _require(
                "still billing" not in output,
                "a charge was asserted over a record whose own fields contradict each other",
            )
            _require(
                "the scheduled release has fired" not in output,
                "a release outcome was asserted from the fields the INCOHERENT rows "
                "had just disqualified",
            )
            _require(
                "an instance exists and is being billed" not in output,
                "presence and billing were concluded from an incoherent record",
            )
            _require(
                "this record contradicts itself" in output,
                "the heading did not say why its rows may not be read together",
            )
            _require(
                "Every row below is what THIS MACHINE RECORDED" in output,
                "the report did not say that its rows are recorded rather than confirmed",
            )
            print("--- PASS: the incoherent record was reported and nothing was concluded")
    finally:
        server.shutdown()


def scenario_status_changes_nothing() -> None:
    """Finding 4: `nyxgpt cloud status` is a read.

    It ran `terraform destroy` on the EC2 Mac release schedule, through
    `verify_mac_record` -> `reconcile_released_host` -> `destroy_release_stack`.
    The recording shim proves the whole command invokes Terraform not at all --
    which is a stronger claim than "it does not destroy", and the right one for
    a command whose contract is to observe.
    """
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _prepare_home(root)
            # A release stack on disk, so there is something to destroy.
            (home / ".nyxGPT" / "cloud" / "mac-release.tfstate").write_text("{}", encoding="utf-8")
            (home / ".nyxGPT" / "cloud" / "mac-release.tfvars").write_text(
                f'aws_region = "us-east-1"\nhost_id = "{STALE_HOST_ID}"\n', encoding="utf-8"
            )
            bindir, log = _install_terraform_shim(root)

            completed = run_cli(
                home, endpoint, ["cloud", "status"], path_prefix=bindir, shim_log=log
            )
            output = _report("status-changes-nothing", completed)

            _require(completed.returncode == 0, "`nyxgpt cloud status` failed")
            _require(
                f"{STALE_HOST_ID} has been released" in output,
                "the read did not reconcile the released host at all, so this scenario "
                "would pass for the wrong reason",
            )
            calls = _shim_calls(log)
            destructive = [c for c in calls if {"destroy", "apply"} & set(c["argv"])]
            _require(
                not destructive,
                f"a read-only command ran terraform {destructive}",
            )
            _require(
                (home / ".nyxGPT" / "cloud" / "mac-release.tfstate").exists(),
                "a read-only command deleted Terraform state",
            )
            print("--- PASS: status observed, and invoked no Terraform mutation")
    finally:
        server.shutdown()


def scenario_terraform_credentials() -> None:
    """Finding 2: the run's `--profile` reaches the `terraform` subprocess.

    `terraform destroy -var-file=~/.nyxGPT/cloud/mac-release.tfvars` ran as the
    default account because that file -- rendered by the run that created the
    schedule on 2026-10-04 -- carried `aws_region` and no `aws_profile`, and
    nothing put the current run's choice in the subprocess environment either.
    Both channels are asserted: the environment the shim was handed, and the
    tfvars it was pointed at.
    """
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _prepare_home(root)
            _write_aws_profiles(home)
            cloud = home / ".nyxGPT" / "cloud"
            # The stored tfvars exactly as the owner's machine held it.
            (cloud / "mac-release.tfvars").write_text(
                f'aws_region = "us-east-1"\nhost_id = "{STALE_HOST_ID}"\n', encoding="utf-8"
            )
            (cloud / "mac-release.tfstate").write_text("{}", encoding="utf-8")
            bindir, log = _install_terraform_shim(root)

            completed = run_cli(
                home,
                endpoint,
                ["cloud", "destroy", "--yes", "--profile", "nyxgpt"],
                path_prefix=bindir,
                shim_log=log,
            )
            _report("terraform-credentials", completed)

            calls = _shim_calls(log)
            # The transcript IS the evidence for this one: what a reader needs
            # to check by hand is the environment each invocation was handed.
            for call in calls:
                print(f"--- terraform {' '.join(call['argv'])}")
                print(f"      AWS_PROFILE={call['env'].get('AWS_PROFILE')!r}")
            _require(calls, "terraform was never invoked, so this proves nothing")
            _require(
                any("mac-release" in " ".join(c["argv"]) for c in calls),
                "terraform was never pointed at the mac-release root, which is the one the "
                "owner's AccessDenied came from",
            )
            wrong = [c for c in calls if c["env"].get("AWS_PROFILE") != "nyxgpt"]
            _require(
                not wrong,
                "terraform was started without this run's credential choice: "
                f"{[c['env'].get('AWS_PROFILE') for c in wrong]}",
            )
            restamped = (cloud / "mac-release.tfvars").read_text(encoding="utf-8")
            _require(
                'aws_profile = "nyxgpt"' in restamped,
                "the stored tfvars an earlier run wrote was reused with its own credential "
                f"choice: {restamped!r}",
            )
            print("--- PASS: every terraform invocation carried the run's own account")
    finally:
        server.shutdown()


# The pre-#4181 state of the two credential channels, restored at the boundary
# rather than by editing the product source -- the #3753 fault-injection
# template. A job that only runs the fixed path passes on any build, including
# one where nothing was fixed.
PRE_FIX_TERRAFORM_DRIVER = '''\
"""Drive the CLI with both #4181 credential channels removed, to prove they matter."""

import sys

from nyxgpt import cloud_identity, cloud_mac

# Channel 1 gone: `_run_terraform` merged nothing into the subprocess
# environment, so Terraform resolved credentials from its own provider block
# and whatever was already in the environment.
cloud_identity.credential_env = lambda choice=None: {}
# Channel 2 gone: a stored tfvars an earlier run rendered was reused verbatim,
# including (or, as on the owner's machine, omitting) its credential choice.
cloud_mac._restamp_tfvars = lambda path, *, region="", profile="": None

from nyxgpt.cli import cli  # noqa: E402 - after the patches, deliberately

sys.exit(cli())
'''


def scenario_pre_fix_terraform() -> None:
    """The negative control for finding 2: without both channels, it reproduces.

    Same stored tfvars, same `--profile nyxgpt`, same command -- and the
    `terraform destroy` on the mac-release root is started with no AWS_PROFILE
    at all, which on the owner's machine meant account 551292530955 and an
    AccessDenied on `nyxgpt-tf-mac-release`. Without this, `terraform-credentials`
    passing would prove only that this build sets the variable, not that
    anything depended on it.
    """
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _prepare_home(root)
            _write_aws_profiles(home)
            cloud = home / ".nyxGPT" / "cloud"
            (cloud / "mac-release.tfvars").write_text(
                f'aws_region = "us-east-1"\nhost_id = "{STALE_HOST_ID}"\n', encoding="utf-8"
            )
            (cloud / "mac-release.tfstate").write_text("{}", encoding="utf-8")
            bindir, log = _install_terraform_shim(root)
            driver = root / "pre_fix_terraform_driver.py"
            driver.write_text(PRE_FIX_TERRAFORM_DRIVER, encoding="utf-8")

            env = _env(home, endpoint)
            env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
            env["TERRAFORM_SHIM_LOG"] = str(log)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    "cloud",
                    "destroy",
                    "--yes",
                    "--profile",
                    "nyxgpt",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=300,
            )
            _report("pre-fix terraform (negative control)", completed)

            calls = _shim_calls(log)
            for call in calls:
                print(f"--- terraform {' '.join(call['argv'])}")
                print(f"      AWS_PROFILE={call['env'].get('AWS_PROFILE')!r}")
            _require(
                calls,
                "terraform was never invoked, so this control proves nothing and the "
                "positive scenario proves nothing either",
            )
            _require(
                all(not c["env"].get("AWS_PROFILE") for c in calls),
                "the pre-fix behaviour unexpectedly handed Terraform a profile",
            )
            _require(
                "aws_profile" not in (cloud / "mac-release.tfvars").read_text(encoding="utf-8"),
                "the pre-fix behaviour unexpectedly re-stamped the stored tfvars",
            )
            print(
                "--- PASS (as a FAILURE of the old code): terraform destroyed the release "
                "stack with no account named by the run"
            )
    finally:
        server.shutdown()


def scenario_declined() -> None:
    """Finding 5: a declined consent is its own outcome, not a failed deploy.

    Typing `no` at the priced disclosure wrote `"status": "failed"`, and the
    next `nyxgpt cloud status` reported "a deploy started here and did not
    finish ... re-run it" over a run whose own recorded error read "nothing was
    allocated and nothing is billed".
    """
    server, endpoint = serve(host_present=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = _prepare_home(Path(tmp))
            declined = run_deploy(home, endpoint, answer="no\n")
            _report("declined (the deploy)", declined)

            attempt_file = home / ".nyxGPT" / "cloud" / "deploy-attempt.json"
            _require(attempt_file.exists(), "the declined run recorded no attempt at all")
            attempt = json.loads(attempt_file.read_text(encoding="utf-8"))
            _require(
                attempt["status"] == "declined",
                f"a declined consent was recorded as {attempt['status']!r}",
            )

            status = run_cli(home, endpoint, ["cloud", "status"])
            output = _report("declined (the status)", status)
            _require(status.returncode == 0, "`nyxgpt cloud status` failed")
            _require(
                "did not finish" not in output,
                "a declined consent was reported as an unfinished deploy",
            )
            _require(
                "DECLINED" in output,
                "the declined attempt was not reported at all -- it is still the answer to "
                "'what happened when I ran that command'",
            )
            _require(
                "nothing was created and nothing is billed" in output,
                "the report did not say that declining created nothing",
            )
            print("--- PASS: declining is an answer, not an incident")
    finally:
        server.shutdown()


#: The modules every scenario's subprocess needs before it can test anything.
#: `nyxgpt` is the product under test; `boto3` is how it asks AWS, and without
#: it the deploy legitimately reports that AWS could not be asked.
REQUIRED_IN_SUBPROCESS = ("nyxgpt", "boto3")


def _preflight_imports() -> str | None:
    """The first of `REQUIRED_IN_SUBPROCESS` a scenario subprocess cannot import.

    `None` means the environment is sound. The check runs under the same `_env`
    the scenarios use -- same `$HOME` override, same stripped `PATH` -- so it
    answers for the environment that actually matters rather than this one.
    """
    with tempfile.TemporaryDirectory() as probe_home:
        env = _env(Path(probe_home), "http://127.0.0.1:1")
        for module in REQUIRED_IN_SUBPROCESS:
            probe = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode != 0:
                return module
    return None


def main() -> int:
    """Run the requested scenarios, failing the job on the first broken claim."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=[
            "released",
            "allocated",
            "status",
            "pre-fix",
            # #4181: the class, not the original instance.
            "wrong-account",
            "wrong-account-answer",
            "incoherent",
            "status-changes-nothing",
            "pre-fix-terraform",
            "terraform-credentials",
            "declined",
            "all",
        ],
        default="all",
        help="Which recorded AWS answer to drive the deploy with (default: all)",
    )
    args = parser.parse_args()

    # Preflight, before any scenario runs -- and run in the SUBPROCESS
    # environment, not this process's. The decision under test is made by a
    # boto3 call inside a `nyxgpt` subprocess, so an import that fails there
    # makes the deploy take its "AWS could not be asked" branch and every
    # scenario then fails with a message about the deploy's behaviour --
    # `the deploy never asked AWS about the recorded host`, or the control's
    # `the pre-fix behaviour did not reproduce the defect` -- which sends the
    # reader into cloud_mac.py after a defect that is not there.
    #
    # Checking `import boto3` here would miss half of it: `_env` overrides
    # `HOME`, which is what makes the subprocess environment differ from this
    # one. Python derives the per-user site directory from `$HOME`, so a
    # `pip install --user` / no-virtualenv checkout imports fine in this
    # process and not in the child. Ask the child.
    if (failure := _preflight_imports()) is not None:
        print(
            f"cloud-stale-record-smoke: {failure} is not importable by the subprocess "
            "the scenarios drive, so nyxGPT cannot ask AWS anything there and no "
            "scenario below would be testing what it claims to test.\n"
            "Install into an environment the subprocess can see -- the wheel-into-a-venv "
            "step this job uses, or `pip install -e '.[cloud]'` inside a virtualenv. "
            "A `--user` install is NOT enough: this harness overrides $HOME, which "
            "moves the per-user site directory out from under it.",
            file=sys.stderr,
        )
        return 2

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
        # #4181. #4136's original incident is fixed and stays fixed above; these
        # cover the CLASS it belongs to -- acting on, or reporting, cloud facts
        # this run did not establish with the right credentials.
        "wrong-account": scenario_wrong_account,
        "wrong-account-answer": scenario_wrong_account_answer_is_refused,
        "incoherent": scenario_incoherent_record,
        "status-changes-nothing": scenario_status_changes_nothing,
        # The negative control for finding 2, before the positive scenario.
        "pre-fix-terraform": scenario_pre_fix_terraform,
        "terraform-credentials": scenario_terraform_credentials,
        "declined": scenario_declined,
    }
    for name in scenarios if args.scenario == "all" else [args.scenario]:
        scenarios[name]()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
