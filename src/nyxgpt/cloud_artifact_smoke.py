"""Containerized artifact-install smoke for `nyxgpt cloud smoke --container` (#3784).

`nyxgpt cloud smoke` (src/nyxgpt/cloud_smoke.py) verifies a *live AWS
deployment*: it costs money, needs credentials, and takes an EC2 round-trip
per attempt. This module is its cheap, hermetic sibling and answers a
different question -- **does the artifact install path work on a bare Amazon
Linux 2023 machine?** -- by running the real EC2 bootstrap inside a container
built to be that machine.

Why it exists: the rc9 cloud round found five serial defects in the EC2
install path (#3759 artifact-relative paths, #3760 docker, #3761 npm, #3762
subprocess output, #3782 the 3.9 venv), each one masking the next because the
bootstrap aborts at its first failure. `linux-native-smoke.yml` structurally
cannot see any of them: it runs on ubuntu-latest with Python 3.11 and Node 20
already installed by setup actions, and installs nyxGPT *editable from the
checkout*. That is the D-006 "green by luck" condition -- a machine groomed to
never hit the failures the smoke is supposed to catch.

The three properties that make this smoke able to see them:

1. **The machine is bare.** The image (`scripts/cloud/al2023-ami-parity.Dockerfile`)
   adds only what the AL2023 AMI already has -- systemd, sudo, an `ec2-user`
   account. No modern Python, no node, no docker, no git. `preflight()`
   asserts each absence *before* the bootstrap runs, so an image that quietly
   gained one fails the smoke instead of hiding a defect.
2. **The install is from an artifact, and the bootstrap is the real one.**
   The script executed in the container is `nyxgpt cloud user-data --os linux`
   rendered by `nyxgpt.cloud_provision` -- the same text a real instance runs
   as cloud-init user-data, not a copy that can drift. It installs a published
   PyPI release (`--version`) or a locally built wheel (`--wheel`); nothing
   reaches the repository at runtime, and `git` is not even present.
3. **Failure is provable, not assumed.** `--inject` rewrites the rendered
   bootstrap to reintroduce a known defect class and inverts the verdict: the
   run passes only if the smoke *fails*. That is the #3753 fault-injection
   pattern -- a job that only runs the happy path passes on every machine that
   fails to reproduce the bug.

What it cannot cover is `COVERAGE_GAPS`, printed by the command itself and
rendered on the dashboard: everything between an AWS API call and a booted
instance (Terraform, cloud-init proper, SSH, instance lifecycle) is not in
this loop. Green here does not mean the cloud path is verified end to end; it
means the part that has been failing for five rounds is verified.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyxgpt import cloud_provision, release_tarball
from nyxgpt.cloud import NYXGPT_HOME, CloudCommandError
from nyxgpt.subprocess_bounds import timeout_result

# The distro the owner's cloud rounds actually run on. Overridable
# (`--image`) so the same smoke can be pointed at another AMI family's base
# image, but the default is deliberately not "some Linux".
DEFAULT_BASE_IMAGE = "amazonlinux:2023"

# Built locally from the packaged Dockerfile; tagged rather than anonymous so
# a re-run reuses Docker's layer cache instead of re-installing systemd.
IMAGE_TAG = "nyxgpt-cloud-artifact-smoke:al2023"
CONTAINER_NAME = "nyxgpt-cloud-artifact-smoke"

# The AMI's default login account (see the Dockerfile). The native install
# path is per-user, so everything after the prerequisites runs as this user.
TARGET_USER = "ec2-user"

# Where the last run is recorded for `--status` and the dashboard panel. Under
# ~/.nyxGPT/cloud/ beside the deploy state, so it survives a re-install of the
# CLI and is readable without a checkout.
RESULT_FILE = NYXGPT_HOME / "cloud" / "artifact-smoke.json"

# Staged outside /tmp on purpose: AL2023's systemd mounts a fresh tmpfs over
# /tmp (`tmp.mount`) as it finishes booting, which silently ate a wheel copied
# there a second earlier. The image creates this directory so nothing the
# smoke stages can be wiped mid-run.
STAGING_DIR = "/opt/nyxgpt-smoke"
BOOTSTRAP_PATH = f"{STAGING_DIR}/nyxgpt-bootstrap.sh"
# Where `--wheel` stages the two service tarballs `ops install` would
# otherwise download from the wheel's (unpublished) GitHub Release. Separate
# from the wheel itself because `NYXGPT_ARTIFACT_DIR` names a directory whose
# every `<name>-<version>.tar.gz` is an install source.
ARTIFACT_DIR = f"{STAGING_DIR}/artifacts"

# Default budgets, in seconds. The bootstrap is the long pole by an order of
# magnitude: dnf, a Node 20 install, an Ollama download, `npm ci` + a Next.js
# build, and a Cassandra container all happen inside it.
DEFAULT_BOOTSTRAP_TIMEOUT = 2400.0
DEFAULT_BUILD_TIMEOUT = 900.0
DEFAULT_HEALTH_TIMEOUT = 300.0
_SYSTEMD_TIMEOUT = 120.0
_SHORT_EXEC_TIMEOUT = 120.0

# The services the install must leave serving, and what each must answer.
HEALTH_CHECKS: tuple[tuple[str, str, int], ...] = (
    ("api", "http://127.0.0.1:8000/health", 200),
    ("web", "http://127.0.0.1:3000/", 200),
    ("ollama", "http://127.0.0.1:11434/", 200),
)

# Stated by the command and the dashboard panel, never left implicit: a green
# run here is not a verified cloud deployment. Everything in this list is
# outside the container by construction.
COVERAGE_GAPS: tuple[str, ...] = (
    "EC2 provisioning: Terraform, the AMI itself, instance/EBS lifecycle and "
    "security groups are not exercised -- `nyxgpt cloud infra plan/apply` and "
    "terraform-local-smoke.yml cover that layer.",
    "cloud-init: the bootstrap is executed directly as root, not by cloud-init "
    "from instance user-data, so user-data size limits, cloud-init ordering and "
    "its logging are not covered.",
    "The private access path: SSH, the `nyxgpt cloud tunnel` port forwards and "
    "the owner-IP security-group rule are not involved -- services are probed "
    "on the container's own loopback.",
    "Instance metadata (IMDS), instance profiles/IAM and cloud-sourced secrets "
    "are absent, so anything that reads them is not verified here.",
    "Real AWS timing and hardware: instance boot, EBS throughput and network "
    "egress differ from a container on a developer machine or CI runner.",
    "The live end-to-end behaviour of a deployment (model answers, RAG "
    "retrieval, the observability UIs) is `nyxgpt cloud smoke` without "
    "--container, against real AWS. Session *storage* is the exception: the "
    "`session-backend` step drives the running API and reads the row back out "
    "of the instance's Cassandra (#3865).",
)


class ArtifactSmokeFailure(CloudCommandError):
    """A phase of the containerized artifact-install smoke failed."""


# --- Fault injection (#3753 pattern) ------------------------------------
#
# Each fault is a textual transform of the *rendered* bootstrap that
# reintroduces one of the rc9 defect classes. Transforms are written against
# the defect, not against a particular line: `old-python` rewrites every
# versioned interpreter reference (`python3.11`, `python3.12`, ...) back to the
# bare `python3`, so it keeps reproducing #3782 whatever shape the fix takes,
# and `no-node` drops the Node provisioning regardless of which package
# manager branch installs it.

_OLD_PYTHON_RE = re.compile(r"python3\.\d+")
_NO_NODE_RE = re.compile(r"nodesource\.com|install\s+-y\s+nodejs|setup_\d+\.x")
# Written against the defect, not the line: any invocation of the wrapped
# setter, however the bootstrap comes to spell it, so `file-sessions` keeps
# reproducing #3865 if the call is later moved or renamed.
_NO_SESSION_BACKEND_RE = re.compile(r"ops\s+session-backend")


@dataclass(frozen=True)
class Fault:
    """A fault the smoke can inject, and the failure it must produce.

    `expects` names the defect class (a `_FAILURE_SIGNATURES` key) the injected
    fault reintroduces. It is what makes `--inject` a real proof: without it
    *any* failure -- a docker outage, an image-pull flake, a refused preflight
    -- would satisfy the inverted verdict, and the job would be green while the
    smoke was as blind to the defect class as before.
    """

    description: str
    expects: str


FAULTS: dict[str, Fault] = {
    "old-python": Fault(
        description=(
            "Rewrite every versioned interpreter reference back to the system "
            "`python3` (3.9 on AL2023), reintroducing the #3782 class: the CLI venv "
            "is built on an interpreter the wheel's requires-python refuses."
        ),
        expects="interpreter-selection",
    ),
    "no-node": Fault(
        description=(
            "Drop the Node 20 provisioning, reintroducing the #3761 class: "
            "`ops install` reaches the web build with no `npm` on the machine."
        ),
        expects="node-provisioning",
    ),
    "file-sessions": Fault(
        description=(
            "Drop the session-backend selection, reintroducing the #3865 class: the "
            "instance keeps `example.config.ini`'s back-compat `session_backend = "
            "file`, so chats are written as JSON on the instance's own disk and never "
            "reach the Cassandra every other deployment mode reads."
        ),
        expects="session-backend",
    ),
}


def apply_faults(script: str, faults: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """Return `script` with each named fault injected, plus a record of each edit.

    An unknown fault name is an error rather than a silent no-op, and a fault
    that changed nothing is reported with `changed: False` -- the run still
    expects a failure, but the record says the injection did not bite, which is
    the difference between "the smoke caught the fault" and "the tree was
    already broken in that way".
    """
    unknown = [name for name in faults if name not in FAULTS]
    if unknown:
        raise ArtifactSmokeFailure(
            f"Unknown --inject fault(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(FAULTS))}"
        )

    records: list[dict[str, Any]] = []
    for name in faults:
        before = script
        if name == "old-python":
            script = _OLD_PYTHON_RE.sub("python3", script)
        elif name == "no-node":
            script = "\n".join(line for line in script.splitlines() if not _NO_NODE_RE.search(line))
        elif name == "file-sessions":
            script = "\n".join(
                line
                for line in script.splitlines()
                # Only the invocation, never the comment block explaining it:
                # a `#`-prefixed mention is documentation, and removing it
                # would make the injected script differ from the real one in
                # ways the fault is not about.
                if line.lstrip().startswith("#") or not _NO_SESSION_BACKEND_RE.search(line)
            )
        records.append(
            {
                "fault": name,
                "description": FAULTS[name].description,
                "expects": FAULTS[name].expects,
                "changed": script != before,
            }
        )
    return script, records


# --- Docker plumbing ----------------------------------------------------


def _run(
    command: list[str], *, timeout: float, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `command`, capturing both streams as text and never raising on exit status.

    A timeout is turned into a synthetic non-zero result rather than an
    exception so every phase reports the same shape -- a bootstrap that hangs
    reads as a failed step with a message, not as a traceback (#3762: an ops
    subprocess failure that does not carry its own output is undiagnosable).
    """
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Shared with every other bounded helper (#3858) so "timed out" means
        # the same returncode and reads the same way everywhere.
        return timeout_result(command, exc, timeout)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _combined(completed: subprocess.CompletedProcess[str]) -> str:
    """Return the process's stdout and stderr as one blob, stripped."""
    return f"{completed.stdout or ''}{completed.stderr or ''}".strip()


def _tail(text: str, lines: int = 40) -> str:
    """Return the last `lines` lines of `text` -- enough to name a failure, short enough to print."""
    kept = text.strip().splitlines()[-lines:]
    return "\n".join(kept)


def docker_available() -> tuple[bool, str]:
    """Report whether a usable Docker engine is reachable, and why not if it is not."""
    completed = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60.0)
    if completed.returncode == 127:
        return False, "the `docker` command is not installed on this machine"
    if completed.returncode != 0:
        return False, _tail(_combined(completed), 6) or "the Docker daemon is not reachable"
    return True, (completed.stdout or "").strip()


def _exec(
    script: str, *, user: str | None = None, timeout: float = _SHORT_EXEC_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run `script` with bash inside the smoke container, optionally as `user`."""
    command = ["docker", "exec"]
    if user:
        command += ["-u", user]
    command += [CONTAINER_NAME, "bash", "-lc", script]
    return _run(command, timeout=timeout)


def _exec_as_target(script: str, *, timeout: float = _SHORT_EXEC_TIMEOUT) -> str:
    """Run `script` as the target user with a login-like environment, returning its output.

    `docker exec -u` starts a bare process, exactly like the `sudo -u` calls in
    the bootstrap: without XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS nothing can
    reach the user's service manager, so verification would report every unit
    as missing on a perfectly healthy install.
    """
    uid_result = _exec(f"id -u {shlex.quote(TARGET_USER)}", timeout=30.0)
    uid = (uid_result.stdout or "").strip() or "1000"
    # HOME too: `docker exec -u` does not re-derive it from the account, so
    # without this every `$HOME/...` path below would resolve under /root.
    prelude = (
        f"export HOME=$(getent passwd {shlex.quote(TARGET_USER)} | cut -d: -f6); "
        f"export XDG_RUNTIME_DIR=/run/user/{uid}; "
        f"export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus; "
        f'export PATH="$HOME/.nyxGPT/opt/nyxgpt-cli/bin:$PATH"; '
    )
    return _combined(_exec(prelude + script, user=TARGET_USER, timeout=timeout))


# --- Phases -------------------------------------------------------------


def build_image(base_image: str, *, timeout: float) -> dict[str, Any]:
    """Build the AMI-parity image from the packaged Dockerfile."""
    dockerfile = cloud_provision.packaged_cloud_file("al2023-ami-parity.Dockerfile")
    if not dockerfile.is_file():
        raise ArtifactSmokeFailure(f"Missing packaged Dockerfile: {dockerfile}")
    completed = _run(
        [
            "docker",
            "build",
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "-t",
            IMAGE_TAG,
            "-f",
            str(dockerfile),
            str(dockerfile.parent),
        ],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise ArtifactSmokeFailure(
            f"Failed to build the AMI-parity image from {base_image}:\n"
            f"{_tail(_combined(completed))}"
        )
    return {"image": IMAGE_TAG, "base_image": base_image}


def start_container() -> dict[str, Any]:
    """Start a privileged systemd container from the built image, and wait for systemd.

    Privileged with a writable cgroup mount because the thing under test is a
    *systemd* install: `ops install` writes `systemd --user` units, the
    bootstrap enables lingering, and the Docker engine it installs is started
    as a system unit. A container without systemd would exercise a path no
    instance ever runs.
    """
    _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=120.0)
    completed = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--privileged",
            "--cgroupns=host",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            IMAGE_TAG,
        ],
        timeout=300.0,
    )
    if completed.returncode != 0:
        raise ArtifactSmokeFailure(
            "Failed to start the smoke container. A privileged container with a "
            "writable cgroup mount is required, because the install under test is "
            f"a systemd install:\n{_tail(_combined(completed))}"
        )

    deadline = time.monotonic() + _SYSTEMD_TIMEOUT
    state = ""
    while time.monotonic() < deadline:
        state = _combined(_exec("systemctl is-system-running", timeout=30.0))
        # `degraded` is normal in a container (units the image never needs), and
        # only means some unit failed -- the bus is up, which is what matters.
        if state.startswith(("running", "degraded")):
            return {"container": CONTAINER_NAME, "systemd_state": state}
        time.sleep(2.0)
    raise ArtifactSmokeFailure(
        f"systemd never finished booting inside the container (last state: {state or 'unknown'})"
    )


def preflight() -> dict[str, Any]:
    """Assert the container is a genuinely bare AL2023 machine before anything installs.

    This is the anti-"green by luck" gate (D-006/#3753). Each absence below is
    a defect class the smoke exists to catch; if the base image ever ships one,
    the bootstrap would be handed a machine that cannot reproduce the failure,
    and the smoke would pass while the real instance broke. Failing here says
    "this run proves nothing", which is the honest outcome.
    """
    checks: list[dict[str, Any]] = []

    version_text = _combined(_exec("python3 -V 2>&1 || true", timeout=60.0))
    match = re.search(r"(\d+)\.(\d+)", version_text)
    system_python = ".".join(match.groups()) if match else ""
    old_python = match is not None and (int(match.group(1)), int(match.group(2))) < (3, 11)
    checks.append(
        {
            "check": "system python3 is present and older than the 3.11 nyxGPT requires",
            "passed": old_python,
            "detail": (
                f"python3 is {system_python or 'absent'} -- selecting a newer interpreter "
                "is the bootstrap's job (#3782)"
            ),
        }
    )

    for tool, defect in (
        ("node", "#3761"),
        ("npm", "#3761"),
        ("docker", "#3760"),
        ("git", "repo-less portability"),
        ("nyxgpt", "the artifact under test"),
    ):
        absent = _exec(f"command -v {tool}", timeout=30.0).returncode != 0
        checks.append(
            {
                "check": f"{tool} is absent from the bare machine",
                "passed": absent,
                "detail": (
                    f"present -- the machine is not bare, so this run cannot see {defect}"
                    if not absent
                    else f"absent, as on the AMI ({defect})"
                ),
            }
        )

    failed = [check["check"] for check in checks if not check["passed"]]
    if failed:
        raise ArtifactSmokeFailure(
            "The container is not a bare Amazon Linux 2023 machine, so a green run "
            "would prove nothing (D-006 'green by luck'). Failed: " + "; ".join(failed)
        )
    return {"checks": checks, "system_python": system_python}


def _wheel_version(wheel: Path) -> str:
    """The version encoded in a wheel filename (`nyxgpt-3.0.0-py3-none-any.whl` -> `3.0.0`)."""
    parts = wheel.name.split("-")
    if len(parts) < 3 or not parts[1]:
        raise ArtifactSmokeFailure(
            f"--wheel does not name a PEP 427 wheel ({wheel.name}): its version cannot be "
            "read, and the service tarballs staged alongside it must carry the same one."
        )
    return parts[1]


def _stage_service_tarballs(version: str) -> dict[str, Any]:
    """Build `nyxgpt-{api,web}-<version>.tar.gz` from this checkout and copy them in.

    `ops install` on the artifact path downloads these two assets from the
    `<version>` GitHub Release. A wheel built from a branch declares the
    checkout's in-development version (e.g. `3.0.0`), which has no release
    until the ceremony cuts one -- so without this the container installs the
    branch's CLI and then dies on a 404 for its `nyxgpt-api-<version>.tar.gz`,
    having proved nothing about the install sequence past step 33.

    They are built by the *same* builder the release publishes with
    (`release_tarball._create_dist_tarball`, what release-artifacts.yml
    attaches to a release), so this stages the identical bytes a published
    version would serve, from the tree under test. `ops` picks them up via
    `NYXGPT_ARTIFACT_DIR` (`ops._staged_service_tarball`).
    """
    if not (release_tarball.REPO_ROOT / "pyproject.toml").is_file():
        raise ArtifactSmokeFailure(
            "--wheel needs the checkout that built the wheel, to build the matching "
            f"service tarballs from; {release_tarball.REPO_ROOT} is not one. Use "
            "--version <published rc> instead."
        )
    staged: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nyxgpt-smoke-artifacts-") as tmp:
        for name in ("nyxgpt-api", "nyxgpt-web"):
            tar = release_tarball._create_dist_tarball(Path(tmp), name, version)
            completed = _run(
                ["docker", "cp", str(tar), f"{CONTAINER_NAME}:{ARTIFACT_DIR}/{tar.name}"],
                timeout=600.0,
            )
            if completed.returncode != 0:
                raise ArtifactSmokeFailure(
                    f"Failed to copy {tar.name} into the container:"
                    f"\n{_tail(_combined(completed))}"
                )
            staged.append(tar.name)
    # The bootstrap reads them as the unprivileged target user; `docker cp`
    # lands them root-owned with the host's mode.
    _exec(f"chmod -R a+rX {shlex.quote(ARTIFACT_DIR)}", timeout=60.0)
    return {"artifact_dir": ARTIFACT_DIR, "service_tarballs": staged}


def stage_artifact(wheel: str | None) -> dict[str, Any]:
    """Copy a locally built wheel, and its matching service tarballs, into the container.

    Returns the pip spec the bootstrap should install and the directory the
    service tarballs were staged in. Without `--wheel` the bootstrap installs
    from PyPI and `ops install` downloads the published service tarballs,
    which is what an instance does; with it, the smoke tests the packaging of
    the tree in front of you, which is what CI needs on a branch whose version
    is not published yet. Neither path uses a checkout at runtime -- the
    tarballs are built on the host and land in the container as opaque
    artifacts, exactly as a download would.
    """
    if not wheel:
        return {"pip_spec": "", "wheel": "", "artifact_dir": "", "service_tarballs": []}
    source = Path(wheel).expanduser()
    if not source.is_file():
        raise ArtifactSmokeFailure(f"--wheel does not name a file: {source}")
    version = _wheel_version(source)
    destination = f"{STAGING_DIR}/{source.name}"
    completed = _run(
        ["docker", "cp", str(source), f"{CONTAINER_NAME}:{destination}"], timeout=300.0
    )
    if completed.returncode != 0:
        raise ArtifactSmokeFailure(
            f"Failed to copy {source} into the container:\n{_tail(_combined(completed))}"
        )
    _exec(f"chmod 0644 {shlex.quote(destination)}", timeout=60.0)
    return {
        "pip_spec": destination,
        "wheel": str(source),
        **_stage_service_tarballs(version),
    }


# What the bootstrap's own failure output means, most specific first. Turning
# a wall of dnf/pip output into "this is the #3782 class" is the difference
# between one EC2 round-trip per defect and a diagnosis (#3762).
#
# Each entry is (class key, pattern, operator-facing meaning). The key is the
# stable name -- `FAULTS[...].expects` and the injected-run verdict are written
# against it, so rewording a meaning cannot silently change what a
# fault-injected run accepts.
#
# Order is precedence, and it is load-bearing: a failed install prints
# *everything* it did, so a single blob routinely carries more than one
# signature. The artifact-obtain class sits above the systemd one because that
# is the exact misdiagnosis this table produced in CI (run 31905652586: a 404
# for `nyxgpt-api-3.0.0.tar.gz` reported as "systemd --user session", because
# the ops log tail mentions `systemctl --user` on the way past).
_FAILURE_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    (
        "interpreter-selection",
        r"requires-python|Requires-Python|requires a different Python|python_requires",
        "interpreter selection: the venv was built on a Python too old for the nyxGPT "
        "artifact (the #3782 class -- the AMI's system python3 is 3.9)",
    ),
    (
        "node-provisioning",
        r"npm: command not found|node: command not found|npm ci|Node\.js version",
        "node provisioning: the web build ran without a usable Node 20 (the #3761 class)",
    ),
    (
        "docker-handling",
        r"Cannot connect to the Docker daemon|docker: command not found|"
        r"permission denied while trying to connect to the Docker daemon",
        "docker handling: the engine was missing, not running, or not reachable by the "
        "target user (the #3760 class)",
    ),
    (
        "artifact-obtain",
        # The ops wording, or a 404 within a few lines of the asset it was for
        # -- urllib reports the status and the URL on separate lines, so a
        # single-line pattern would miss the shape this actually arrives in.
        r"Could not obtain the nyxgpt-(?:api|web) artifact|"
        r"(?:releases/download/|nyxgpt-(?:api|web)-\S*\.tar\.gz)[\s\S]{0,300}?(?:404|Not Found)|"
        r"(?:404|Not Found)[\s\S]{0,300}?"
        r"(?:releases/download/|nyxgpt-(?:api|web)-\S*\.tar\.gz)",
        "artifact download: the service tarball for this version could not be fetched from "
        "its GitHub Release -- the version is unpublished, or the asset is missing (this is "
        "what a branch build without staged artifacts looks like)",
    ),
    (
        "artifact-relative-path",
        r"(nyxGPT|docker/|ops/|scripts/)[^\s']*'?: No such file or directory|REPO_ROOT",
        "artifact-relative path resolution: something resolved a runtime path against a "
        "repository that is not on this machine (the #3759 class)",
    ),
    (
        # Narrowed to failure phrasing: a bare `systemctl --user` mention is in
        # the normal output of every successful install, so matching it alone
        # made this class the catch-all that swallowed unrelated failures.
        "systemd-user-session",
        r"Failed to connect to bus|Interactive authentication required|"
        r"(?:[Ff]ailed|[Ee]rror|[Uu]nable)[^\n]*systemctl --user|"
        r"systemctl --user[^\n]*(?:[Ff]ailed|[Ee]rror|not found|No such file)|"
        r"(?:lingering|user service manager)[^\n]*(?:not |un|refus|fail)",
        "systemd --user session: lingering or the user bus was not usable for the target user",
    ),
    (
        # Matched on this smoke's own wording rather than on anything the
        # install prints: the defect is silent by construction (#3865 -- the
        # instance came up healthy and served chat, it just stored it in the
        # wrong place), so the only thing that can name it is the check that
        # went looking.
        "session-backend",
        r"session backend|chat_sessions",
        "session storage backend: the instance did not end up on the Cassandra session "
        "store, so its chats are host-local JSON that no other deployment mode can see "
        "(the #3865 class)",
    ),
)


def _match_signature(output: str) -> tuple[str, str]:
    """Return the (class key, meaning) of the first signature `output` carries."""
    for name, pattern, meaning in _FAILURE_SIGNATURES:
        if re.search(pattern, output):
            return name, meaning
    return "", ""


def classify_failure(output: str) -> str:
    """Name the defect class visible in bootstrap/verification output, or `""`."""
    return _match_signature(output)[1]


def failure_class(output: str) -> str:
    """Return the stable class key for `output` (`interpreter-selection`, ...), or `""`."""
    return _match_signature(output)[0]


def run_bootstrap(
    script: str, *, timeout: float, pip_spec: str, artifact_dir: str = ""
) -> dict[str, Any]:
    """Copy the rendered bootstrap into the container and run it as root.

    Root because that is what cloud-init does; the bootstrap drops to the
    target user itself for everything after the prerequisites, so running it
    any other way would test a path no instance takes.

    `pip_spec` and `artifact_dir` are the two `--wheel` escape hatches, both
    unset on a real instance: the first points the CLI install at the staged
    wheel, the second points `ops install` at the staged service tarballs
    (`ops.LOCAL_ARTIFACT_DIR_ENV`) instead of the wheel version's GitHub
    Release, which does not exist for an unpublished branch version.
    """
    completed = _run(
        ["docker", "exec", "-i", CONTAINER_NAME, "tee", BOOTSTRAP_PATH],
        timeout=120.0,
        input_text=script,
    )
    if completed.returncode != 0:
        raise ArtifactSmokeFailure(
            f"Failed to write the bootstrap into the container:\n{_tail(_combined(completed))}"
        )
    _exec(f"chmod +x {BOOTSTRAP_PATH}", timeout=60.0)

    started = time.monotonic()
    environment = f"NYXGPT_PIP_SPEC={shlex.quote(pip_spec)} " if pip_spec else ""
    if artifact_dir:
        environment += f"NYXGPT_ARTIFACT_DIR={shlex.quote(artifact_dir)} "
    result = _exec(f"{environment}bash {BOOTSTRAP_PATH}", timeout=timeout)
    output = _combined(result)
    if result.returncode != 0:
        # The tail, not the whole blob: a 35-step install prints everything it
        # did on the way to the failure, and classifying that carries every
        # earlier step's incidental phrasing into the diagnosis. The lines that
        # say why it stopped are the last ones.
        tail = _tail(output)
        classification = classify_failure(tail) or classify_failure(output)
        raise ArtifactSmokeFailure(
            f"The EC2 bootstrap failed inside the container (exit {result.returncode})"
            + (f" -- {classification}" if classification else "")
            + f".\nLast lines:\n{_tail(output)}"
        )
    return {
        "waited_seconds": round(time.monotonic() - started, 1),
        "pip_spec": pip_spec or "nyxgpt (PyPI)",
        "log_tail": _tail(output, 15),
    }


def verify_repo_less() -> dict[str, Any]:
    """Assert nyxGPT is running from an installed artifact, not from a checkout.

    `git` is absent from the image, so a clone is impossible by construction;
    what remains to prove is that the *installed* package is what answers --
    a path resolved relative to a repository that is not there is exactly the
    #3759 class, and it fails as a missing file rather than as a wrong import.
    """
    module_path = _exec_as_target(
        "python3 -c 'import nyxgpt,sys; sys.stdout.write(nyxgpt.__file__)' "
        '2>/dev/null || "$HOME/.nyxGPT/opt/nyxgpt-cli/bin/python" -c '
        "'import nyxgpt,sys; sys.stdout.write(nyxgpt.__file__)'",
        timeout=120.0,
    ).strip()
    if "site-packages" not in module_path:
        raise ArtifactSmokeFailure(
            "nyxGPT is not being served from an installed artifact "
            f"(nyxgpt.__file__ = {module_path or 'unresolvable'}) -- the repo-less "
            "portability requirement is not met on this machine."
        )
    checkout = _exec("ls -d /home/*/nyxGPT /root/nyxGPT 2>/dev/null || true", timeout=60.0)
    stray = _combined(checkout)
    if stray:
        raise ArtifactSmokeFailure(f"A repository checkout is present in the container: {stray}")
    return {"module_path": module_path, "checkout_present": False}


def verify_services(timeout: float) -> dict[str, Any]:
    """Wait for every installed service to answer, then report what each returned.

    Bounded retries rather than one probe: a unit `ops install` has just
    started -- especially the Next.js boot behind nyxgpt-web -- can still be
    binding its port when this runs (#3632).
    """
    deadline = time.monotonic() + timeout
    reached: dict[str, str] = {}
    pending = list(HEALTH_CHECKS)
    while pending:
        still_pending = []
        for label, url, expected in pending:
            code = _exec_as_target(
                f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 {shlex.quote(url)} "
                "|| echo 000",
                timeout=60.0,
            ).strip()[-3:]
            if code == str(expected):
                reached[label] = code
            else:
                still_pending.append((label, url, expected))
        pending = still_pending
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(5.0)

    if pending:
        unreachable = ", ".join(
            f"{label} ({url}, expected {expected})" for label, url, expected in pending
        )
        raise ArtifactSmokeFailure(
            f"Installed services did not answer within {timeout:.0f}s: {unreachable}"
        )

    units = _exec_as_target(
        "systemctl --user list-units 'nyxgpt-*' --all --no-pager --no-legend || true",
        timeout=120.0,
    )
    return {"services": reached, "units": _tail(units, 12)}


# The session this phase asks the running API to create. A valid session name
# (see `sessions.validate_session_name`) and obviously synthetic, so a kept
# container (`--keep`) shows plainly where the row came from.
SMOKE_SESSION_NAME = "cloud-artifact-smoke"

# The Docker container `nyxgpt ops install` creates for the one Docker-managed
# core service (`_ensure_cassandra_container`), and the keyspace/table
# `nyxgpt.session_db` writes into (`[rag] cassandra_keyspace`, default
# `nyxgpt`, and `SESSIONS_TABLE`).
CASSANDRA_CONTAINER = "nyxgpt-cassandra"
CHAT_SESSIONS_TABLE = "nyxgpt.chat_sessions"


def verify_session_backend(timeout: float = 180.0) -> dict[str, Any]:
    """Assert a fresh artifact install stores chat sessions in Cassandra (#3865).

    Three checks, in the order that makes a failure name its own cause:

    1. The wrapped reader (`nyxgpt ops session-backend`) reports `cassandra`
       as the backend in force. This is the configuration claim.
    2. The *running API* -- not this process, and not the CLI -- creates a
       session over HTTP. `POST /api/v1/sessions/init` goes through the same
       `nyxgpt.sessions` dispatch layer every chat write uses and needs no
       model, so it proves the serving process's behaviour without depending
       on Ollama having a model pulled.
    3. That session is a row in `nyxgpt.chat_sessions`, read back through
       `cqlsh` in the instance's own Cassandra container. This is the check
       that cannot be satisfied by a well-formed config alone, and it is the
       issue's acceptance criterion stated literally: a chat produces a row,
       with no manual configuration step anywhere above it.

    Checking (1) alone would be the same mistake #3865 was: config.ini said
    nothing, nobody looked, and the deployment served happily while writing
    sessions somewhere no other mode could read them.
    """
    reported = _exec_as_target("nyxgpt ops session-backend", timeout=timeout)
    if "session_backend = cassandra" not in reported:
        raise ArtifactSmokeFailure(
            "The instance did not come up on the Cassandra session backend -- "
            f"`nyxgpt ops session-backend` reported: {_tail(reported, 5) or '(no output)'}. "
            "A fresh cloud install must not fall back to host-local JSON sessions (#3865)."
        )

    payload = json.dumps({"name": SMOKE_SESSION_NAME})
    created = _exec_as_target(
        "curl -sS -X POST --max-time 60 -H 'Content-Type: application/json' "
        f"-d {shlex.quote(payload)} http://127.0.0.1:8000/api/v1/sessions/init",
        timeout=timeout,
    )
    if '"ok"' not in created or "true" not in created.lower():
        raise ArtifactSmokeFailure(
            "The running API refused to create a session through the Cassandra session "
            f"backend: {_tail(created, 5) or '(no output)'}"
        )

    # Read the row back out of Cassandra itself, not out of the API: asking the
    # API would just re-report whatever store it used, which is the thing under
    # test. `cqlsh` ships inside the Cassandra image, so this needs nothing
    # installed on the instance.
    #
    # As root rather than through `_exec_as_target`: this is the smoke
    # inspecting the machine, not the machine operating itself, and running it
    # as the target user would make the check's verdict depend on that
    # account's docker-group membership -- a different question, already
    # answered by the bootstrap's own preflight.
    cql = (
        f"SELECT name FROM {CHAT_SESSIONS_TABLE} "
        f"WHERE name = '{SMOKE_SESSION_NAME}';"  # noqa: S608 - a fixed literal, not input
    )
    rows = _combined(
        _exec(
            f"docker exec {shlex.quote(CASSANDRA_CONTAINER)} cqlsh -e {shlex.quote(cql)}",
            timeout=timeout,
        )
    )
    if SMOKE_SESSION_NAME not in rows:
        raise ArtifactSmokeFailure(
            f"The API created session '{SMOKE_SESSION_NAME}' but it is not a row in "
            f"{CHAT_SESSIONS_TABLE} -- the session went to this host's disk instead of the "
            f"shared store (#3865). cqlsh said: {_tail(rows, 8) or '(no output)'}"
        )

    return {
        "backend": "cassandra",
        "session": SMOKE_SESSION_NAME,
        "table": CHAT_SESSIONS_TABLE,
        "reported": _tail(reported, 3),
        "row": _tail(rows, 6),
    }


def collect_diagnostics() -> dict[str, str]:
    """Gather what an operator would ask for after a failed run. Never raises."""
    return {
        "ops_status": _tail(_exec_as_target("nyxgpt ops status || true", timeout=180.0), 40),
        # #3865: on a session-backend failure the first question is which
        # backend the instance actually resolved, and what the config says --
        # `session_backend` is the whole diagnosis, and it is one grep.
        "session_backend": _tail(
            _exec_as_target(
                "nyxgpt ops session-backend 2>&1 || true; "
                'grep -n "^session_backend" "$HOME/.nyxGPT/config.ini" 2>/dev/null || true',
                timeout=120.0,
            ),
            10,
        ),
        "units": _tail(
            _exec_as_target(
                "systemctl --user list-units 'nyxgpt-*' --all --no-pager || true", timeout=120.0
            ),
            30,
        ),
        "api_log": _tail(
            _exec_as_target(
                'tail -n 60 "$HOME/.nyxGPT/logs/nyxgpt-api.err.log" 2>/dev/null || true',
                timeout=60.0,
            ),
            60,
        ),
        "web_log": _tail(
            _exec_as_target(
                'tail -n 60 "$HOME/.nyxGPT/logs/nyxgpt-web.err.log" 2>/dev/null || true',
                timeout=60.0,
            ),
            60,
        ),
    }


def teardown(*, keep: bool) -> dict[str, Any]:
    """Remove the smoke container unless `--keep` was passed. Never raises."""
    if keep:
        return {
            "removed": False,
            "kept": True,
            "reason": (
                f"--keep was passed: the container is still running. Inspect it with "
                f"`docker exec -it -u {TARGET_USER} {CONTAINER_NAME} bash`, and remove it "
                f"with `docker rm -f {CONTAINER_NAME}`."
            ),
        }
    completed = _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=180.0)
    return {
        "removed": completed.returncode == 0,
        "kept": False,
        "reason": "" if completed.returncode == 0 else _tail(_combined(completed), 5),
    }


# --- Result recording ---------------------------------------------------


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_result(result: dict[str, Any]) -> None:
    """Persist `result` as the last run, for `--status` and the dashboard panel."""
    try:
        RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        # A run that cannot write its record is still a run whose verdict the
        # caller needs; losing the history is not worth failing it.
        pass


def last_result() -> dict[str, Any]:
    """Return the last recorded run, or `{}` if there is none (or it is unreadable)."""
    if not RESULT_FILE.exists():
        return {}
    try:
        loaded = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# --- The run ------------------------------------------------------------

# Every phase that has to complete before the bootstrap -- the thing an
# injected fault lives in -- is even reached. A run that stops earlier failed
# at the smoke's own scaffolding, not at the defect.
_PRE_BOOTSTRAP_STEPS: tuple[str, ...] = (
    "docker",
    "render",
    "build",
    "boot",
    "preflight",
    "artifact",
)


def _reached_bootstrap(steps: list[dict[str, Any]]) -> bool:
    """Whether the run got as far as running the bootstrap in the container."""
    completed = {str(step.get("step", "")) for step in steps}
    return all(name in completed for name in _PRE_BOOTSTRAP_STEPS)


def injection_verdict(
    faults: list[str], *, failure: str, failure_class_key: str, reached_bootstrap: bool
) -> tuple[bool, str]:
    """Decide whether a `--inject` run proved the smoke sees the injected defect.

    "The run failed" is not the pass condition, which is the trap this closes:
    a docker outage, an image-pull flake, a build failure or a refused
    preflight all make an injected run fail, and accepting any of them would
    make the fault-injection job green while proving nothing -- the same
    "green by luck" shape (D-006/#3753) the job exists to rule out. So a pass
    requires all three: a failure, at or after the bootstrap step, whose
    classification is the class the injected fault reintroduces.
    """
    names = ", ".join(faults)
    expected = [FAULTS[name].expects for name in faults if name in FAULTS]
    wanted = " or ".join(expected) or "a classified failure"

    if not failure:
        return False, (
            f"injected {names} but the run still passed -- this smoke cannot see that "
            "defect class, so a green run proves nothing"
        )
    if not reached_bootstrap:
        return False, (
            f"injected {names} but the run failed before the bootstrap it was injected into "
            f"ever ran, so the fault was never exercised: {failure}"
        )
    if failure_class_key not in expected:
        return False, (
            f"injected {names} and the run failed, but as "
            f"'{failure_class_key or 'an unclassified failure'}' rather than the expected "
            f"{wanted} -- something other than the injected defect broke, so this run does "
            f"not show the smoke sees it: {failure}"
        )
    return True, (
        f"injected {names} and the smoke failed with the expected {failure_class_key} "
        f"failure: {failure}"
    )


def run_container_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Build, boot, bootstrap, verify and tear down; return the full record.

    Returns rather than raises, for the same reason the AWS smoke does: the
    caller has to report both the verdict and what happened to the container,
    and an exception would hide one behind the other.
    """
    version = str(getattr(args, "version", None) or "").strip()
    wheel = str(getattr(args, "wheel", None) or "").strip()
    base_image = str(getattr(args, "image", None) or DEFAULT_BASE_IMAGE)
    faults = [str(f) for f in (getattr(args, "inject", None) or [])]
    keep = bool(getattr(args, "keep", False))
    bootstrap_timeout = float(getattr(args, "bootstrap_timeout", None) or DEFAULT_BOOTSTRAP_TIMEOUT)
    health_timeout = float(getattr(args, "health_timeout", None) or DEFAULT_HEALTH_TIMEOUT)

    started_wall = _now()
    started = time.monotonic()
    steps: list[dict[str, Any]] = []
    failure = ""
    classification = ""
    fault_records: list[dict[str, Any]] = []
    diagnostics: dict[str, str] = {}
    container_started = False

    try:
        available, detail = docker_available()
        if not available:
            raise ArtifactSmokeFailure(
                f"A Docker engine is required to run the containerized smoke -- {detail}. "
                "Start Docker (or run this on a machine that has it) and try again."
            )
        steps.append({"step": "docker", "server_version": detail})

        script = cloud_provision.render_user_data("linux", version or None)
        script, fault_records = apply_faults(script, faults)
        steps.append(
            {
                "step": "render",
                "version": version or "latest",
                "bytes": len(script),
                "faults": fault_records,
            }
        )

        steps.append(
            {
                "step": "build",
                **build_image(
                    base_image,
                    timeout=float(getattr(args, "build_timeout", None) or DEFAULT_BUILD_TIMEOUT),
                ),
            }
        )
        steps.append({"step": "boot", **start_container()})
        container_started = True
        steps.append({"step": "preflight", **preflight()})
        artifact = stage_artifact(wheel or None)
        steps.append({"step": "artifact", **artifact})
        steps.append(
            {
                "step": "bootstrap",
                **run_bootstrap(
                    script,
                    timeout=bootstrap_timeout,
                    pip_spec=artifact["pip_spec"],
                    artifact_dir=artifact.get("artifact_dir", ""),
                ),
            }
        )
        steps.append({"step": "repo-less", **verify_repo_less()})
        steps.append({"step": "services", **verify_services(health_timeout)})
        # After services: this one drives the running API, so it needs the
        # stack the step above just proved is answering (#3865).
        steps.append({"step": "session-backend", **verify_session_backend()})
    except CloudCommandError as exc:
        failure = str(exc)
    except KeyboardInterrupt:
        failure = "interrupted (Ctrl-C) -- removing the container before exiting"
    # Broad on purpose: anything escaping a phase must still reach the teardown
    # below, or a bug here leaves a privileged container running.
    except Exception as exc:  # pragma: no cover - defensive
        failure = f"unexpected error: {exc.__class__.__name__}: {exc}"

    classification_key = ""
    if failure and container_started:
        classification_key, classification = _match_signature(failure)
        diagnostics = collect_diagnostics()

    teardown_result = (
        teardown(keep=keep)
        if container_started
        else {
            "removed": False,
            "kept": False,
            "reason": "no container was started",
        }
    )
    steps.append({"step": "teardown", **teardown_result})

    # With --inject the verdict is inverted: the point of the run is to prove
    # the smoke SEES the injected defect, so a bootstrap that succeeds anyway
    # is the failure (#3753 -- both halves, or the job is green by luck). It is
    # inverted, not blind: the failure has to be the injected one
    # (`injection_verdict`), or any breakage at all would pass the job.
    if faults:
        passed, detail = injection_verdict(
            faults,
            failure=failure,
            failure_class_key=classification_key,
            reached_bootstrap=_reached_bootstrap(steps),
        )
    else:
        passed = not failure
        detail = failure or (
            "bare AL2023 -> artifact install -> services serving -> a session created "
            "through the API landed in nyxgpt.chat_sessions, verified"
        )

    return {
        "action": "cloud-artifact-smoke",
        "passed": passed,
        "failure": failure,
        "failure_class": classification,
        "detail": detail,
        "expected_failure": bool(faults),
        "injected_faults": fault_records,
        "version": version or "latest",
        "wheel": wheel,
        "base_image": base_image,
        "steps": steps,
        "diagnostics": diagnostics,
        "teardown": teardown_result,
        "started_at": started_wall,
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "coverage_gaps": list(COVERAGE_GAPS),
    }


# --- Background runs (dashboard surface) --------------------------------
#
# The panel has to be able to start a run, and a run takes tens of minutes --
# far longer than an HTTP request. So the endpoint starts a thread and returns
# immediately; the panel polls the same recorded result the CLI writes, so both
# surfaces report one run, not two implementations of it.

_RUN_LOCK = threading.Lock()
_RUNNING: dict[str, Any] = {}


def running_state() -> dict[str, Any]:
    """Return the in-flight run's descriptor, or `{}` when nothing is running."""
    with _RUN_LOCK:
        return dict(_RUNNING)


def start_background_run(args: argparse.Namespace) -> dict[str, Any]:
    """Start a smoke run in a background thread; refuse if one is already in flight."""
    with _RUN_LOCK:
        if _RUNNING:
            raise ArtifactSmokeFailure(
                f"A containerized cloud smoke started at {_RUNNING.get('started_at')} is "
                "still running -- wait for it to finish, or remove its container with "
                f"`docker rm -f {CONTAINER_NAME}`."
            )
        descriptor = {
            "started_at": _now(),
            "version": str(getattr(args, "version", None) or "latest"),
            "faults": [str(f) for f in (getattr(args, "inject", None) or [])],
        }
        _RUNNING.update(descriptor)

    def _worker() -> None:
        try:
            result = run_container_smoke(args)
        # A crash must clear the in-flight marker, or the panel is wedged until
        # the API restarts.
        except Exception as exc:  # pragma: no cover - defensive
            result = {
                "action": "cloud-artifact-smoke",
                "passed": False,
                "failure": f"unexpected error: {exc.__class__.__name__}: {exc}",
                "started_at": descriptor["started_at"],
                "finished_at": _now(),
                "steps": [],
                "coverage_gaps": list(COVERAGE_GAPS),
            }
        record_result(result)
        with _RUN_LOCK:
            _RUNNING.clear()

    threading.Thread(target=_worker, name="cloud-artifact-smoke", daemon=True).start()
    return {"started": True, **descriptor}


def smoke_status() -> dict[str, Any]:
    """Report the last run, whether one is in flight, and how to start one.

    Side-effect free apart from one `docker info` call, so the dashboard can
    poll it: knowing whether Docker is even available is what tells the panel
    to offer the button or explain why it cannot.
    """
    available, detail = docker_available()
    return {
        "running": bool(running_state()),
        "current": running_state(),
        "last_result": last_result(),
        "docker_available": available,
        "docker_detail": detail,
        "base_image": DEFAULT_BASE_IMAGE,
        "faults": [
            {"fault": name, "description": fault.description, "expects": fault.expects}
            for name, fault in sorted(FAULTS.items())
        ],
        "coverage_gaps": list(COVERAGE_GAPS),
        "commands": {
            "run": "nyxgpt cloud smoke --container",
            "pinned": "nyxgpt cloud smoke --container --version <rc>",
            "inject": "nyxgpt cloud smoke --container --inject old-python",
            "status": "nyxgpt cloud smoke --container --status",
        },
        "docs": "docs/cloud-artifact-smoke.md",
    }


# --- CLI entry point ----------------------------------------------------


def _print_summary(result: dict[str, Any]) -> None:
    """Print the operator-facing result of a containerized run."""
    for step in result["steps"]:
        name = step["step"]
        if name == "teardown":
            if step.get("kept"):
                print(f"[cloud-artifact-smoke] teardown: skipped -- {step['reason']}")
            elif step.get("removed"):
                print("[cloud-artifact-smoke] teardown: container removed")
            else:
                print(f"[cloud-artifact-smoke] teardown: {step.get('reason', 'nothing to remove')}")
        else:
            print(f"[cloud-artifact-smoke] {name}: ok")

    if result["passed"]:
        print(f"\nContainerized cloud artifact smoke PASSED in {result['duration_seconds']:.0f}s.")
        print(f"  {result['detail']}")
    else:
        print(
            f"\nContainerized cloud artifact smoke FAILED after "
            f"{result['duration_seconds']:.0f}s.",
            file=sys.stderr,
        )
        print(f"  {result['detail']}", file=sys.stderr)
        if result.get("failure_class"):
            print(f"  Defect class: {result['failure_class']}", file=sys.stderr)
        for label, text in (result.get("diagnostics") or {}).items():
            if text:
                print(f"\n--- {label} ---\n{text}", file=sys.stderr)

    print("\nWhat a green run here does NOT cover:")
    for gap in result.get("coverage_gaps") or COVERAGE_GAPS:
        print(f"  - {gap}")


def _print_status() -> None:
    """Print the last recorded run and whether one is in flight."""
    status = smoke_status()
    if status["running"]:
        print(f"A run started at {status['current'].get('started_at')} is still in flight.")
    result = status["last_result"]
    if not result:
        print("No containerized cloud artifact smoke has been recorded on this machine yet.")
        print(f"Run one with: {status['commands']['run']}")
        return
    verdict = "PASSED" if result.get("passed") else "FAILED"
    print(f"Last run: {verdict} at {result.get('finished_at', 'unknown time')}")
    print(f"  version: {result.get('version', 'unknown')}")
    print(f"  detail:  {result.get('detail') or result.get('failure') or ''}")
    if result.get("failure_class"):
        print(f"  class:   {result['failure_class']}")


def container_smoke_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud smoke --container` entry point."""
    if getattr(args, "status", False):
        if getattr(args, "json", False):
            print(json.dumps(smoke_status(), indent=2))
        else:
            _print_status()
        return 0

    try:
        result = run_container_smoke(args)
    except ArtifactSmokeFailure as exc:  # pragma: no cover - run_container_smoke catches its own
        print(f"nyxgpt cloud smoke --container: {exc}", file=sys.stderr)
        return 1
    record_result(result)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        _print_summary(result)
    return 0 if result["passed"] else 1
