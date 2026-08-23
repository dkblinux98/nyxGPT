"""One-command AWS deploy for `nyxgpt cloud deploy` (P6-11, #3513).

`nyxgpt cloud infra apply` (P6-8, `nyxgpt.cloud_infra`) stops at the
substrate: a VPC, an owner-IP-scoped SSH-only security group, and one bare
EC2 instance. This module is the layer above it -- the single command that
takes an operator from "nothing" to "a running, monitored nyxGPT reachable
from my workstation":

1. **Apply the infrastructure** (`cloud_infra.apply_infra`) -- idempotent by
   construction, so a re-run reconciles rather than duplicates.
2. **Wire the P6-4 access path** -- the apply re-detects the operator's
   current public IP on every run, so the security group's single port-22
   rule already points at wherever the deploy is running from; this module
   records that CIDR as its own step and then waits for SSH to answer on the
   freshly booted box.
3. **Provision the instance from published artifacts** -- see
   `render_provision_script`. The instance `pip install`s a published
   `nyxgpt` release from PyPI and runs `nyxgpt ops install`; it never clones
   this repository, and on this path nothing is copied from the operator's
   checkout (there may not be one). This mirrors, step for step, the
   `artifact-install-smoke` job in `.github/workflows/release-artifacts.yml`,
   which proves that exact sequence on a checkout-free Linux runner.

   `--dev` (#3950) is the one deliberate exception, and it is opt-in and
   checkout-only exactly like `nyxgpt up --dev` (D-009): the operator's
   *working tree* is shipped to the instance by `ship_working_tree` and
   installed editable there, so `ops install --dev` on the box builds from
   that tree instead of a release. The instance still never clones anything
   -- the tree crosses the same SSH connection everything else does -- and
   the default path is untouched, so the repo-less guarantee (#3504) holds
   for every deploy that does not ask for this.
4. **Open the tunnel and wait for health** -- the app, web UI and every
   observability UI bind `127.0.0.1` on the instance and are never exposed
   in the security group
   (`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`), so the only
   URL that can be printed is a `localhost` one reached through
   `nyxgpt cloud tunnel`. Deploy opens that tunnel, confirms health through
   it, and leaves it up so the printed URLs work immediately.

`nyxgpt cloud destroy` is the counterpart: stop the tunnel, then tear the
substrate down through `cloud_infra.destroy_infra`.

Everything here is wrapped (CLAUDE.md, 2026-07-15): the operator never types
`ssh`, `terraform`, or `docker`. Deploying is a CLI operation and only a CLI
operation (owner decision, 2026-08-16, #3804): the admin dashboard's
Infrastructure page reads `deploy_status` to *report* what is deployed and
names the commands below, because a UI served by the instance cannot safely
change the substrate it is running on.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nyxgpt import cloud_infra, cloud_mac
from nyxgpt.cloud import CloudCommandError
from nyxgpt.config import VALID_SESSION_BACKENDS

# The same `~/.nyxGPT/cloud` directory `cloud_infra` owns -- one place for
# everything about a cloud deployment. `cloud_infra.CLOUD_STATE_FILE` (the
# substrate's instance/region handoff) is read through the module attribute
# rather than aliased here, so it stays a single source of truth.
CLOUD_DIR = cloud_infra.CLOUD_DIR

# What the last successful deploy installed, so `status` can answer without
# touching AWS or the instance, and a re-run can default to the same version.
DEPLOY_STATE_FILE = CLOUD_DIR / "deploy.json"

# The deploy *currently or last* attempted, written before anything is
# provisioned and updated as each phase completes (#3993). Deliberately a
# separate file from `deploy.json`: that one is the record of a deployment
# that exists, and writing a half-finished attempt into it would make
# `nyxgpt cloud status` report DEPLOYED for a stack that was never installed
# -- trading one lie for a worse one.
#
# It exists because a provision that dies partway is exactly the state an
# operator most needs status to describe, and it was the one state nothing
# could see: `deploy.json` is written only on success, so after a failed
# deploy `cloud status` said "UNKNOWN from this machine" while a live,
# billing instance ran and `state.json` on the same disk named it.
DEPLOY_ATTEMPT_FILE = CLOUD_DIR / "deploy-attempt.json"

# `status` values for `DEPLOY_ATTEMPT_FILE`. Three, not two: a deploy that is
# still running is not a failure, and treating it as one would send an
# operator to debug a provision that is merely slow.
ATTEMPT_RUNNING = "running"
ATTEMPT_FAILED = "failed"
ATTEMPT_SUCCEEDED = "succeeded"

# Append-only record of every deploy and teardown, so the admin dashboard can
# answer "what happened to this deployment" and not just "what is it now"
# (P6-15, #3514). JSONL rather than one rewritten JSON document because two
# processes write it -- the operator's CLI and the API server -- and appending
# a single short line is the closest thing to an atomic write available
# without introducing a lock file. Entries are small and a deploy takes
# minutes, so the file does not meaningfully grow; readers take the tail.
DEPLOY_HISTORY_FILE = CLOUD_DIR / "history.jsonl"

# How many history entries `deploy_status` carries. Enough to cover the
# redeploy-until-it-works sequence an operator is usually trying to
# reconstruct, small enough that the status payload stays pollable.
HISTORY_LIMIT = 20

# The background tunnel's pid + forwarded ports, so `--stop`/`status` can find
# a tunnel started by an earlier process (including one started by the admin
# dashboard and stopped from the CLI, or the reverse).
TUNNEL_STATE_FILE = CLOUD_DIR / "tunnel.json"

# Where the detached background tunnel's ssh stderr goes. A long-lived `ssh -N`
# child outlives the CLI process that started it, so its stderr cannot stay on
# a pipe nobody will ever read -- a later write (a ServerAlive notice, say)
# would hit a closed pipe once the parent exits.
TUNNEL_LOG_FILE = CLOUD_DIR / "tunnel.log"

# A release is spliced into the remote provisioning script; keep it to what a
# published artifact version can actually look like.
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Amazon Linux 2023's default login user -- the AMI the compute module
# resolves by default (terraform/aws/modules/compute/main.tf). EC2 Mac's own
# AMIs use the same name, so it is the right default for both target OSes.
DEFAULT_SSH_USER = "ec2-user"

# Where `--dev` lands the operator's working tree on the instance (#3950).
# Under `~/.nyxGPT` beside the venv and config the same provisioning script
# creates, so a teardown that removes the nyxGPT home removes the copy with
# it and nothing is left in the login user's home directory. Written as a
# shell expansion rather than an absolute path because the login user is
# `--ssh-user`'s to choose.
REMOTE_SOURCE_DIR = "$HOME/.nyxGPT/src"

# How long `ship_working_tree`'s two steps may take. The archive is built
# locally from files git already knows about (fast, and bounded by the tree
# rather than the network); the transfer is a few tens of MB over one SSH
# connection to an instance that has just booted, which is the slow half.
ARCHIVE_TIMEOUT = 300.0
TRANSFER_TIMEOUT = 1800.0
# --- Target OS (#3867) -------------------------------------------------
#
# A deploy provisions one of two target OS families, and until #3867 it
# provisioned exactly one of them: the Linux SSH script below was hard-coded,
# and the only way to bootstrap an EC2 Mac was to render
# `nyxgpt cloud user-data --os macos` and paste it into an AWS console launch
# by hand -- the raw-operations flow CLAUDE.md's Operational Command Wrapping
# requirement forbids. `deploy` now dispatches on the family and drives either
# bootstrap itself, over the same wrapped SSH path.
OS_FAMILY_LINUX = "linux"
OS_FAMILY_MACOS = "macos"

# `--os auto`, the default: derive the family from the instance type rather
# than making every Linux operator type a flag they never had to before.
OS_FAMILY_AUTO = "auto"

DEPLOY_OS_CHOICES: tuple[str, ...] = (OS_FAMILY_AUTO, OS_FAMILY_LINUX, OS_FAMILY_MACOS)

# EC2's Mac instance types, the only ones that boot macOS: `mac1.metal`
# (Intel) and the `mac2*`. Apple Silicon family (`mac2.metal`,
# `mac2-m2.metal`, `mac2-m2pro.metal`, `mac2-m1ultra.metal`, ...). Every one
# is `.metal` -- macOS is only ever bare metal on EC2 -- so the shape is
# stable enough to detect on, and anything unrecognized falls to Linux, which
# is the pre-#3867 behavior.
_MAC_INSTANCE_TYPE_RE = re.compile(r"^mac\d+(-[a-z0-9]+)?\.metal$")

# `--os macos` with no `--host` used to stop here with a refusal, on the
# grounds that allocating a Dedicated Host would spend the operator's money on
# "a resource this configuration cannot then tear down". #3995 retired both
# halves of that:
#
#  * the configuration *can* tear it down -- `destroy` terminates the Mac at
#    once and defers only the host release, which AWS refuses inside the
#    24-hour minimum, to a one-shot EventBridge schedule; and
#  * spending money without asking is a consent problem, and consent is how
#    every other irreversible spend in this CLI is authorized. So the deploy
#    prices the host live, prints what it will cost and when it can be
#    released, and requires a typed word (`--yes` skips the typing, never the
#    disclosure).
#
# What the path must never do is send the operator to the AWS console or to a
# raw `aws` shell (#3867, and #3995's finding that #3867 moved that seam
# rather than removing it). Allocation, launch and bootstrap are one command.
# See `nyxgpt.cloud_mac` and docs/cloud.md, "EC2 Mac targets".

# The session backend a cloud deploy selects unless told otherwise (#3865).
#
# `cassandra`, not the `file` back-compat default `example.config.ini` ships:
# the whole point of the DB backend (#3590) is that every mode pointed at the
# same Cassandra sees one session list, and the Kubernetes overlay already
# asserts that declaratively (k8s/configmap.yaml). A cloud instance that
# quietly ran the file backend broke the guarantee where it matters most --
# chats saved as JSON on ephemeral instance disk, invisible to every other
# mode, and lost with the instance. `ops install` provisions
# `nyxgpt-cassandra` on the instance as a core service either way, so this
# needs no infrastructure the deploy was not already creating.
DEFAULT_SESSION_BACKEND = "cassandra"

# Core services, always tunneled. Ports match docker-compose.yml and the
# native systemd units; both bind 127.0.0.1 on the instance.
CORE_TUNNEL_PORTS: tuple[tuple[str, int], ...] = (
    ("api", 8000),
    ("web", 3000),
)

# Observability UIs, tunneled per enabled profile. Keys are
# `ops.OBSERVABILITY_PROFILES` entries; the deploy records which profiles it
# enabled so `tunnel` forwards exactly those.
OBSERVABILITY_TUNNEL_PORTS: dict[str, tuple[tuple[str, int], ...]] = {
    "monitoring": (("grafana", 3001), ("prometheus", 9090)),
    "logging": (),  # Loki has no UI of its own -- it is read through Grafana.
    "tracing": (("jaeger", 16686),),
    "errors": (("glitchtip", 8080),),
}

# The endpoint deploy polls through the tunnel to decide the stack is up.
HEALTH_PATH = "/health"

# What runs the stack on the instance. `native` is the default -- systemd
# --user services plus the Cassandra/observability containers, the layout
# every cloud deploy has used since P6-11. `kubernetes` is #3506's decision
# implemented (#3956): a single-node k3s cluster on the same box, running the
# existing `k8s/*.yaml` manifests, which is what makes `nyxgpt canary` -- the
# capability that decision was choosing a substrate *for* -- available on the
# cloud target at all.
SUBSTRATE_NATIVE = "native"
SUBSTRATE_KUBERNETES = "kubernetes"

# SSH options applied to every connection. `StrictHostKeyChecking=accept-new`
# trusts the key on first contact but still fails loudly if it changes later
# (a plain `no` would silently accept a swapped host); the instance is brand
# new on a first deploy, so there is no prior key to compare against and a
# strict setting would just hang on a prompt no wrapped command can answer.
SSH_COMMON_OPTIONS: tuple[str, ...] = (
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
)


@dataclass
class DeployTarget:
    """Where and how to reach the provisioned instance."""

    host: str
    user: str = DEFAULT_SSH_USER
    identity_file: str = ""
    region: str = ""
    instance_id: str = ""
    security_group_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, recorded in `deploy.json` and returned by the API."""
        return {
            "host": self.host,
            "user": self.user,
            "identity_file": self.identity_file,
            "region": self.region,
            "instance_id": self.instance_id,
            "security_group_id": self.security_group_id,
        }


@dataclass
class DeployPlan:
    """Resolved inputs for one `nyxgpt cloud deploy` run."""

    version: str
    profiles: list[str] = field(default_factory=list)
    ssh_user: str = DEFAULT_SSH_USER
    identity_file: str = ""
    open_tunnel: bool = True
    health_timeout: float = 900.0
    ssh_timeout: float = 300.0
    session_backend: str = DEFAULT_SESSION_BACKEND
    kubernetes: bool = False
    # `--dev` (#3950): ship and install the working tree at `source_dir`
    # instead of the published release `version` names. `source_dir` is only
    # ever set together with `dev`, and `resolve_plan` refuses `dev` without
    # a resolvable checkout -- so the two can never disagree.
    dev: bool = False
    source_dir: str = ""
    # Which target OS's bootstrap this deploy drives (#3867). Defaults to
    # Linux so a plan built without the field behaves exactly as it did.
    os_family: str = OS_FAMILY_LINUX

    @property
    def substrate(self) -> str:
        """What runs the stack on the instance: `kubernetes` or `native`."""
        return SUBSTRATE_KUBERNETES if self.kubernetes else SUBSTRATE_NATIVE

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, recorded in `deploy.json`."""
        return {
            "version": self.version,
            "profiles": list(self.profiles),
            "ssh_user": self.ssh_user,
            "identity_file": self.identity_file,
            "open_tunnel": self.open_tunnel,
            "health_timeout": self.health_timeout,
            "ssh_timeout": self.ssh_timeout,
            "session_backend": self.session_backend,
            "kubernetes": self.kubernetes,
            "substrate": self.substrate,
            "dev": self.dev,
            "source_dir": self.source_dir,
            "os_family": self.os_family,
        }


# --- Small state files -------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    """Return `path` parsed as a JSON object, or `{}` if missing/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` to `path` as pretty JSON, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_deploy_state() -> dict[str, Any]:
    """Return what the last successful deploy recorded (or `{}`)."""
    return _read_json(DEPLOY_STATE_FILE)


def _cloud_state() -> dict[str, Any]:
    """Return the substrate outputs `cloud_infra.apply_infra` recorded."""
    return _read_json(cloud_infra.CLOUD_STATE_FILE)


def load_deploy_attempt() -> dict[str, Any]:
    """Return the last deploy attempt this machine started (or `{}`)."""
    return _read_json(DEPLOY_ATTEMPT_FILE)


def begin_deploy_attempt(plan: DeployPlan, host: str = "") -> dict[str, Any]:
    """Record that a deploy has *started*, before anything is provisioned (#3993).

    Written first so that whatever happens next -- a Terraform failure, an SSH
    timeout, an `ops install` that dies halfway, the operator's laptop closing
    -- leaves behind a record naming the target, the version and the phase it
    got to. Best-effort, like `record_history`: an unwritable state directory
    must not be the reason a deploy does not happen.
    """
    attempt = {
        "status": ATTEMPT_RUNNING,
        "phase": "start",
        "started_at": time.time(),
        "updated_at": time.time(),
        "version": plan.version,
        "host": host,
        "instance_id": "",
        "region": "",
        "os_family": plan.os_family,
        "substrate": plan.substrate,
        "dev": plan.dev,
        "profiles": list(plan.profiles),
        "error": "",
    }
    _write_attempt(attempt)
    return attempt


def update_deploy_attempt(phase: str, **fields: Any) -> dict[str, Any]:
    """Advance the recorded attempt to `phase`, merging `fields` into it.

    A no-op when no attempt is on file -- `deploy` is the only writer, and a
    caller that never began one has nothing to advance.
    """
    attempt = load_deploy_attempt()
    if not attempt:
        return {}
    attempt.update(fields)
    attempt["phase"] = phase
    attempt["updated_at"] = time.time()
    _write_attempt(attempt)
    return attempt


def finish_deploy_attempt(status: str, phase: str = "", error: str = "") -> dict[str, Any]:
    """Close out the recorded attempt as `status` (succeeded or failed).

    A succeeded attempt is kept rather than deleted: "the last deploy finished
    cleanly" is a useful answer, and keeping the file means the *absence* of
    one always means "no deploy has been attempted from this machine" rather
    than being ambiguous between that and "it went fine".
    """
    attempt = load_deploy_attempt()
    if not attempt:
        return {}
    attempt["status"] = status
    attempt["error"] = error
    attempt["updated_at"] = time.time()
    if phase:
        attempt["phase"] = phase
    _write_attempt(attempt)
    return attempt


def _write_attempt(attempt: dict[str, Any]) -> None:
    """Persist the attempt record, swallowing write errors (see `begin_deploy_attempt`)."""
    with contextlib.suppress(OSError):
        _write_json(DEPLOY_ATTEMPT_FILE, attempt)


def record_history(action: str, outcome: str, **fields: Any) -> dict[str, Any]:
    """Append one lifecycle event to the deploy history and return it.

    Called from `deploy`/`destroy` themselves rather than from the CLI or the
    API handler, so a deploy run either way lands in the same history -- the
    dashboard's job here is to report what an operator did at the terminal,
    which it could not do if only its own requests were recorded.

    Best-effort: an unwritable history file never fails a deploy that
    otherwise succeeded.
    """
    event: dict[str, Any] = {"ts": time.time(), "action": action, "outcome": outcome, **fields}
    try:
        DEPLOY_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with DEPLOY_HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
    except OSError:
        pass
    return event


def deploy_history(limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent lifecycle events, newest first.

    Unparseable lines are skipped rather than raising: a truncated final line
    (a process killed mid-append) must not take the whole history with it.
    """
    if limit <= 0:
        return []
    try:
        lines = DEPLOY_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    events.reverse()
    return events


# --- Resolution --------------------------------------------------------


def installed_version() -> str:
    """Return the version of `nyxgpt` running on *this* workstation.

    The default the instance is deployed at, so the operator's CLI and the
    box run the same release without having to name it. Read from installed
    package metadata (works from a wheel with no checkout, per the repo-less
    requirement), falling back to the checkout's pyproject when running from
    a `pip install -e .` dev tree whose metadata is stale.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nyxgpt")
    except PackageNotFoundError:  # pragma: no cover - source tree, never installed
        pass
    try:  # pragma: no cover - only reached without package metadata
        from nyxgpt import ops

        return ops.project_version()
    except Exception:
        return ""


def resolve_target(args: argparse.Namespace) -> DeployTarget:
    """Locate the provisioned instance and how to SSH to it.

    Reads `~/.nyxGPT/cloud/state.json` -- the handoff `cloud infra apply`
    writes -- so no flag is needed on the happy path.
    """
    state = _cloud_state()
    host = str(getattr(args, "host", None) or state.get("public_ip") or "")
    if not host:
        raise CloudCommandError(
            "No provisioned instance found in "
            f"{cloud_infra.CLOUD_STATE_FILE} -- run `nyxgpt cloud deploy` (which provisions "
            "first) or `nyxgpt cloud infra apply`, or pass --host to target an existing box."
        )
    saved = cloud_infra.load_settings()
    # No identity file means "let ssh use its own defaults and agent", which
    # is the right behaviour both for an `--ssh-key-name` setup and for the
    # common case where the registered public key's private half is one of
    # ssh's standard `~/.ssh/id_*` candidates.
    identity = str(getattr(args, "identity_file", None) or "")
    if identity:
        identity = str(Path(identity).expanduser())
    return DeployTarget(
        host=host,
        user=str(getattr(args, "ssh_user", None) or DEFAULT_SSH_USER),
        identity_file=identity,
        region=str(state.get("region") or saved.get("aws_region") or ""),
        instance_id=str(state.get("instance_id") or ""),
        security_group_id=str(state.get("security_group_id") or ""),
    )


def resolve_access_target(args: argparse.Namespace) -> DeployTarget:
    """Resolve the instance *and* the SSH credentials the last deploy used.

    `resolve_target` takes the host from the substrate handoff but the login
    user and identity file from flags alone, which is right for `deploy` --
    `resolve_plan` restores those from the deploy record separately. For the
    read-only commands that reach the instance afterwards there was no such
    restore, so a deployment made with a non-default key could only be
    inspected by re-typing `--identity-file` every time. Flags still win;
    this only fills in what was not given (#3813).
    """
    target = resolve_target(args)
    record = load_deploy_state()
    if not getattr(args, "ssh_user", None) and record.get("ssh_user"):
        target.user = str(record["ssh_user"])
    if not getattr(args, "identity_file", None) and record.get("identity_file"):
        target.identity_file = str(Path(str(record["identity_file"])).expanduser())
    return target


def instance_type_os_family(instance_type: str) -> str:
    """Return the OS family an EC2 instance type boots (#3867).

    Only the Mac instance types boot macOS, and every one of them is a
    `.metal` type; everything else -- including an unset or unrecognized type
    -- is Linux, which is what every deploy before #3867 assumed.
    """
    return (
        OS_FAMILY_MACOS
        if _MAC_INSTANCE_TYPE_RE.match(instance_type.strip().lower())
        else OS_FAMILY_LINUX
    )


def resolve_os_family(args: argparse.Namespace) -> str:
    """Decide which target OS's bootstrap this deploy drives (#3867).

    An explicit `--os linux|macos` wins outright. `--os auto` (the default)
    answers from what is already known, in order:

    1. **The deploy record for this exact host**, when `--host` names a
       machine a previous run provisioned. Re-deploying a Mac must stay a Mac
       deploy, and an operator-supplied Mac has no instance type on this
       machine to read -- the substrate does not manage it.
    2. **The instance type the substrate is configured for** -- this run's
       `--instance-type` if given, otherwise the one remembered in
       `~/.nyxGPT/cloud/infra.json` -- so an operator who already asked for a
       `mac2.metal` does not also have to say `--os macos`.

    A Linux operator types nothing new and lands on `linux` either way.
    """
    requested = str(getattr(args, "os_family", None) or OS_FAMILY_AUTO).strip().lower()
    if requested and requested != OS_FAMILY_AUTO:
        if requested not in (OS_FAMILY_LINUX, OS_FAMILY_MACOS):
            raise CloudCommandError(
                f"{requested!r} is not a supported target OS. Choose one of: "
                f"{', '.join(DEPLOY_OS_CHOICES)}."
            )
        return requested
    host = str(getattr(args, "host", None) or "")
    previous = load_deploy_state()
    recorded = str(previous.get("os_family") or "")
    if (
        host
        and host == str(previous.get("host") or "")
        and recorded in (OS_FAMILY_LINUX, OS_FAMILY_MACOS)
    ):
        return recorded
    saved = cloud_infra.load_settings()
    instance_type = str(getattr(args, "instance_type", None) or saved.get("instance_type") or "")
    return instance_type_os_family(instance_type)


def resolve_plan(args: argparse.Namespace) -> DeployPlan:
    """Merge flags over the last deploy's recorded choices.

    The choices that carry over are the ones `deploy.json` records: the
    release, the SSH user, the identity file, the session backend and the
    substrate. Everything else (profiles, timeouts, whether to open a tunnel)
    is per-run and comes from the flags or their defaults.

    The session backend carries over for the same reason the SSH user does:
    a re-deploy of an instance whose sessions live in Cassandra must not
    silently move them back to files because the operator omitted a flag.
    The substrate carries over for a stronger version of the same reason
    (#3956): a bare `nyxgpt cloud deploy` against a box already running k3s
    must not quietly install a second, native copy of the stack beside the
    cluster and fight it for ports 8000/3000. It is a *tri-state* flag
    (`--kubernetes` / `--no-kubernetes` / neither) rather than a `store_true`
    precisely so the carry-forward is reversible: a sticky boolean with no
    way to say "off" is a trap, not a convenience.

    `dev` (#3950) deliberately does **not** carry over. Every other recorded
    choice describes the instance's configuration, which a re-deploy should
    preserve; `--dev` describes where this *particular run* gets its code, and
    a plain `nyxgpt cloud deploy` has meant "install a published release"
    since the command existed. Inheriting it would silently re-ship whatever
    tree happened to be checked out -- a different commit, a half-finished
    edit -- under a command the operator read as the artifact path. The
    recorded `dev` flag is still written, so `cloud status` can say the box is
    running a tree rather than a release.
    """
    previous = load_deploy_state()
    dev = bool(getattr(args, "dev", False))
    source_dir = ""
    if dev:
        source_dir = str(_require_dev_source())
    if dev:
        # The tree being shipped is the answer, so this run's own version
        # comes before the last deploy's: `version` here is a label for what
        # is about to be on the box, and nothing installs from it.
        version = str(getattr(args, "version", None) or installed_version() or "0.0.0")
    else:
        version = str(
            getattr(args, "version", None) or previous.get("version") or installed_version() or ""
        )
    if not version:
        raise CloudCommandError(
            "Could not determine which nyxGPT release to install on the instance. "
            "Pass --version <release> (e.g. --version 3.0.0) -- without --dev the instance "
            "installs a published artifact, never a copy of your working tree."
        )
    # The version is spliced into the remote provisioning script, so a stray
    # quote or `$` in it would surface as a confusing shell error ten minutes
    # into a remote run. Fail here instead, where the operator can see why.
    if not _VERSION_RE.fullmatch(version):
        raise CloudCommandError(
            f"{version!r} is not a valid release to install. Pass a published version such as "
            "--version 3.0.0 (digits, letters, dot, dash and underscore only)."
        )
    os_family = resolve_os_family(args)
    if dev and os_family == OS_FAMILY_MACOS:
        # Refused rather than ignored, and refused here -- before the substrate
        # is applied -- for the same reason `--dev` refuses without a checkout
        # (#3950): the macOS bootstrap installs published Homebrew formulas and
        # has no working-tree source, so honouring the flag by rendering it
        # anyway would hand an operator a published release while they believe
        # they are testing their tree. That silent wrong answer is the defect
        # this whole feature exists to prevent, so it must not be reachable by
        # combining two flags that are each fine on their own.
        raise CloudCommandError(
            "--dev is not available for a macOS target. The EC2 Mac bootstrap installs "
            "published Homebrew formulas from the remote tap and has no working-tree "
            "source, so it cannot run your checkout.\n"
            "\n"
            "Deploy your tree to a Linux target (`nyxgpt cloud deploy --dev`, the default "
            "target OS), or drop --dev to install a published release on the Mac."
        )
    if getattr(args, "skip_observability", False) or os_family == OS_FAMILY_MACOS:
        # No observability stack on an EC2 Mac: its bootstrap installs the two
        # Homebrew formulas and starts them (scripts/cloud/ec2-user-data-macos
        # .sh.tmpl), and never runs `ops install`'s observability profiles --
        # so recording profiles here would only make `tunnel` forward ports
        # nothing is listening on and the summary promise URLs that 404.
        profiles: list[str] = []
    else:
        from nyxgpt import ops

        profiles = list(ops.OBSERVABILITY_PROFILES)

    # The per-target-OS session-backend default (#3865, and #3867 for the
    # dispatch): `cassandra` on Linux, where the bootstrap provisions the
    # `nyxgpt-cassandra` container as a core service, and `file` on macOS,
    # where nothing does -- defaulting a Mac to `cassandra` would point its
    # API at a database that is not on the machine. Imported here rather than
    # at module scope because `cloud_provision` imports this module.
    from nyxgpt import cloud_provision

    default_backend = cloud_provision.DEFAULT_SESSION_BACKEND_BY_OS.get(
        os_family, DEFAULT_SESSION_BACKEND
    )
    # The recorded choice carries over only within one target OS. A re-deploy
    # that switches families must not inherit the other one's backend -- the
    # whole reason the default differs is that the two bootstraps provision
    # different stacks.
    carried = (
        str(previous.get("session_backend") or "")
        if str(previous.get("os_family") or OS_FAMILY_LINUX) == os_family
        else ""
    )
    backend = str(getattr(args, "session_backend", None) or carried or default_backend)
    if backend not in VALID_SESSION_BACKENDS:
        raise CloudCommandError(
            f"{backend!r} is not a valid session backend. "
            f"Choose one of: {', '.join(VALID_SESSION_BACKENDS)} "
            "(see docs/session-storage.md)."
        )
    requested_k8s = getattr(args, "kubernetes", None)
    # The substrate carries over only within one target OS, for the same
    # reason the session backend above does and one more: k3s is Linux-only,
    # so a Linux deploy's recorded `kubernetes: true` must not follow the
    # operator onto a Mac and get refused there for a choice they did not
    # make on this run.
    carried_k8s = (
        bool(previous.get("kubernetes", False))
        if str(previous.get("os_family") or OS_FAMILY_LINUX) == os_family
        else False
    )
    kubernetes = carried_k8s if requested_k8s is None else bool(requested_k8s)
    if kubernetes and os_family == OS_FAMILY_MACOS:
        # Refused rather than accepted-and-ignored, the same standard the
        # session-backend check below applies (#3956 + #3867). The macOS
        # branch of `render_provision_script` returns the Homebrew bootstrap
        # and never reaches the substrate sections, so without this the flag
        # would be accepted, dropped on the floor, and then contradicted by a
        # deploy record claiming a cluster that was never installed.
        #
        # There is no macOS k3s path to offer instead: the k3s installer is
        # Linux-only (`get.k3s.io` builds a systemd/openrc unit), and an EC2
        # Mac's bootstrap installs Homebrew formulas under launchd.
        raise CloudCommandError(
            "--kubernetes is not available on a macOS target: k3s is Linux-only, and an "
            "EC2 Mac is provisioned from the remote Homebrew tap under launchd "
            "(see docs/cloud.md, 'EC2 Mac targets').\n"
            "Deploy the cluster on a Linux target -- `nyxgpt cloud deploy --os linux "
            "--kubernetes` -- or drop --kubernetes to provision this Mac natively."
        )
    if kubernetes and backend != "cassandra":
        # Refused rather than accepted-and-ignored (#3956). In Kubernetes mode
        # the Pods read `k8s/configmap.yaml`, which asserts
        # `session_backend = cassandra` declaratively -- `ops session-backend`
        # writes the *host's* config.ini, which nothing in the cluster reads.
        # So the flag cannot take effect here, and the deploy summary that
        # reported it would have been describing a setting no Pod was using.
        # This also fires on a value merely carried forward from an earlier
        # native deploy, which is exactly the case where the flag silently
        # changes meaning under the operator.
        raise CloudCommandError(
            f"--session-backend {backend} cannot take effect on a Kubernetes deployment: the "
            "Pods read k8s/configmap.yaml, which sets `session_backend = cassandra` so that "
            "every api replica shares one session list (see docs/session-storage.md). "
            f"Pass --session-backend cassandra, or drop --kubernetes to deploy natively with "
            f"the {backend} backend."
            + (
                "\n(That value came from the last deploy on this machine, not from a flag on "
                "this one -- `deploy.json` carries it forward.)"
                if not getattr(args, "session_backend", None)
                else ""
            )
        )
    return DeployPlan(
        version=version,
        profiles=profiles,
        ssh_user=str(
            getattr(args, "ssh_user", None) or previous.get("ssh_user") or DEFAULT_SSH_USER
        ),
        identity_file=str(
            getattr(args, "identity_file", None) or previous.get("identity_file") or ""
        ),
        open_tunnel=not getattr(args, "no_tunnel", False),
        health_timeout=float(getattr(args, "health_timeout", None) or 900.0),
        ssh_timeout=float(getattr(args, "ssh_timeout", None) or 300.0),
        session_backend=backend,
        kubernetes=kubernetes,
        dev=dev,
        source_dir=source_dir,
        os_family=os_family,
    )


# --- Dev mode: the working tree as the build source (#3950) ------------


def dev_source_root() -> Path | None:
    """Return the checkout `nyxgpt cloud deploy --dev` would ship, or None.

    Delegates to `ops.dev_checkout_root` rather than testing for a
    `pyproject.toml` here, so the cloud path and the local install path can
    never disagree about what counts as a source tree. On an
    artifact-installed CLI there is no tree above the package and this
    answers None -- which is the whole of the refusal below.
    """
    from nyxgpt import ops

    return ops.dev_checkout_root()


def _require_dev_source() -> Path:
    """Return the checkout `--dev` will ship, or raise saying why there is none.

    The loud refusal the local install paths already make (`ops.install` and
    `ops._install_terraform`), moved to the front of the cloud path for the
    same reason: an operator who believes they are testing their working tree
    must never be handed a published release instead. Failing here also means
    failing *before* AWS is touched, so a mistaken `--dev` costs nothing.
    """
    source = dev_source_root()
    if source is None:
        raise CloudCommandError(
            "--dev deploys your working tree, and this nyxgpt is running from an installed "
            "package -- there is no checkout above it to ship (it needs pyproject.toml, "
            "src/nyxgpt/ and web/).\n"
            "Run `nyxgpt cloud deploy --dev` from a clone of the repository, or drop --dev "
            "to deploy a published release."
        )
    return source


def working_tree_files(source: Path) -> list[str]:
    """Return every path under `source` that `--dev` ships, relative to it.

    Asks git rather than walking the tree: `--cached --others
    --exclude-standard` is exactly "everything tracked, plus everything new
    that is not ignored", which is the working tree as the operator sees it
    -- uncommitted edits included, `node_modules`/`.venv`/`.next` excluded by
    the ignore rules already in the repository. Committed state (`git
    archive HEAD`) would be the wrong answer: dev mode exists to run the code
    in front of you, and half of that is usually not committed yet.

    `.git` itself is never listed, so the instance receives a source tree and
    not a repository -- there is nothing on the box to pull, push or clone
    from, and the transfer stays small.

    Tracked paths deleted in the working tree are dropped: git still lists
    them and tar would fail on the first one. The test is `lexists`, not
    `is_file`, and the difference is load-bearing rather than pedantic --
    `src/nyxgpt/resources/` is a directory of *symlinks* back to the
    top-level `docker/`, `ops/`, `k8s/`, `terraform/` and `scripts/cloud/`
    trees (#3621), each one tracked by git as a single entry that `is_file()`
    answers False for. Shipping the tree without them would put a checkout on
    the instance whose `ops install` could not find its own runtime data.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            timeout=ARCHIVE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # Named rather than left to surface as a traceback: every other way
        # this step can fail already reads as one sentence, and a timeout is
        # the one an operator is most likely to hit on a huge or
        # network-mounted tree -- where the answer is a fixable fact about
        # their tree, not a stack trace about `subprocess`.
        raise CloudCommandError(
            f"Listing the working tree at {source} took longer than "
            f"{ARCHIVE_TIMEOUT:.0f}s and was stopped. A very large or "
            "network-mounted checkout can do this; deploy from a local tree."
        ) from exc
    if completed.returncode != 0:
        raise CloudCommandError(
            f"Could not list the working tree at {source} to ship it: "
            f"{(completed.stderr or '').strip() or 'git ls-files failed'}"
        )
    names = [name for name in completed.stdout.split("\0") if name]
    return sorted({name for name in names if os.path.lexists(source / name)})


def build_working_tree_archive(source: Path, dest: Path) -> int:
    """Write a gzipped tar of `source`'s working tree to `dest`; return the file count.

    Staged to a file rather than streamed straight into ssh's stdin: the two
    halves then fail independently and legibly (a tar problem is a tar
    problem, not a mysterious broken pipe), and there is no producer/consumer
    deadlock to get wrong on a tree that is tens of megabytes.
    """
    names = working_tree_files(source)
    if not names:
        raise CloudCommandError(
            f"The working tree at {source} lists no files to ship -- is it a git checkout?"
        )
    listing = "\0".join(names)
    try:
        completed = subprocess.run(
            ["tar", "-czf", str(dest), "-C", str(source), "--null", "-T", "-"],
            input=listing,
            capture_output=True,
            text=True,
            timeout=ARCHIVE_TIMEOUT,
            # Belt-and-braces against AppleDouble `._*` metadata reaching the
            # instance (observed 2026-08-22: 1,182 `._*` files shipped in one
            # --dev deploy; Grafana's provisioner then crash-loops parsing
            # `._dashboards.yml` as YAML). The primary vector was Dropbox
            # materializing `._*` companions as real files during sync
            # activity, which `git ls-files --others` then listed -- closed by
            # the `._*` entry in .gitignore. COPYFILE_DISABLE additionally
            # stops any macOS bsdtar configuration from synthesizing its own
            # `._` metadata entries at archive time. A no-op for GNU tar.
            env={**os.environ, "COPYFILE_DISABLE": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise CloudCommandError(
            f"Archiving the working tree at {source} ({len(names)} files) took longer than "
            f"{ARCHIVE_TIMEOUT:.0f}s and was stopped. Nothing was shipped."
        ) from exc
    if completed.returncode != 0:
        raise CloudCommandError(
            f"Could not archive the working tree at {source}: "
            f"{(completed.stderr or '').strip() or 'tar failed'}"
        )
    return len(names)


def ship_working_tree(target: DeployTarget, source: Path) -> dict[str, Any]:
    """Copy the working tree at `source` to `REMOTE_SOURCE_DIR` on the instance.

    The remote directory is replaced, not merged: a file deleted or renamed in
    the tree since the last `--dev` deploy has to disappear from the box too,
    and an install that half-matches the operator's tree is worse than one
    that plainly does not. The tree is small enough (git's own file list, no
    `node_modules`, no history) that re-shipping it is cheaper than teaching
    this to reconcile.

    Returns the step record `deploy` reports: how many files crossed and how
    big the archive was.
    """
    with tempfile.TemporaryDirectory(prefix="nyxgpt-dev-deploy-") as staging:
        archive = Path(staging) / "worktree.tar.gz"
        files = build_working_tree_archive(source, archive)
        size = archive.stat().st_size
        remote = (
            f'rm -rf "{REMOTE_SOURCE_DIR}" && mkdir -p "{REMOTE_SOURCE_DIR}" '
            f'&& tar -xzf - -C "{REMOTE_SOURCE_DIR}"'
        )
        try:
            with archive.open("rb") as handle:
                completed = subprocess.run(
                    [*ssh_argv(target), remote],
                    stdin=handle,
                    capture_output=True,
                    text=True,
                    timeout=TRANSFER_TIMEOUT,
                )
        except subprocess.TimeoutExpired as exc:
            # Says what the box is left holding, which the traceback did not:
            # the remote command removes the old tree before extracting, so a
            # transfer stopped part-way leaves an incomplete one behind. The
            # next `--dev` deploy replaces it wholesale, so the fix is to
            # re-run -- but an operator who reconnects meanwhile must not
            # trust what is there.
            raise CloudCommandError(
                f"Copying the working tree to {target.user}@{target.host} took longer than "
                f"{TRANSFER_TIMEOUT:.0f}s and was stopped. The instance is left with an "
                f"incomplete tree at {REMOTE_SOURCE_DIR}; re-run `nyxgpt cloud deploy --dev`, "
                "which replaces it, once the link to the instance is healthy."
            ) from exc
    if completed.returncode != 0:
        raise CloudCommandError(
            f"Could not copy the working tree to {target.user}@{target.host}: "
            f"{(completed.stderr or '').strip() or 'ssh failed'}"
        )
    return {
        "step": "source",
        "source_dir": str(source),
        "remote_dir": REMOTE_SOURCE_DIR,
        "files": files,
        "archive_bytes": size,
    }


# --- SSH ---------------------------------------------------------------


def ssh_argv(target: DeployTarget, *, options: list[str] | None = None) -> list[str]:
    """Build the `ssh` argv prefix for `target` (no remote command appended)."""
    argv = ["ssh", *SSH_COMMON_OPTIONS]
    if target.identity_file:
        # IdentitiesOnly stops ssh from offering every key in the agent first
        # and tripping the server's MaxAuthTries before reaching this one.
        argv += ["-i", target.identity_file, "-o", "IdentitiesOnly=yes"]
    argv += [*(options or []), f"{target.user}@{target.host}"]
    return argv


def run_remote(
    target: DeployTarget,
    command: str,
    *,
    stream: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `command` on the instance over SSH and return the completed process.

    Never raises on a non-zero exit -- callers decide what a failure means
    (a health probe failing is a retry, a provisioning step failing is
    fatal). stdout is streamed to the terminal when `stream` is set so a
    long `nyxgpt ops install` isn't a silent ten-minute wait.
    """
    argv = [*ssh_argv(target), command]
    return subprocess.run(
        argv,
        stdout=None if stream else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def wait_for_ssh(target: DeployTarget, timeout: float, *, interval: float = 5.0) -> float:
    """Block until the instance answers SSH, returning how long it took.

    A freshly applied instance is reachable in the API long before sshd is
    listening, so every remote step would otherwise fail on the first deploy.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_error = ""
    while time.monotonic() < deadline:
        completed = run_remote(target, "true")
        if completed.returncode == 0:
            return time.monotonic() - started
        last_error = (completed.stderr or "").strip()
        time.sleep(interval)
    detail = f" Last error: {last_error}" if last_error else ""
    raise CloudCommandError(
        f"{target.user}@{target.host} did not accept SSH within {timeout:.0f}s.{detail}\n"
        "If your public IP changed since the substrate was applied, run `nyxgpt cloud allow-ip`."
    )


# --- Instance provisioning (repo-less) ---------------------------------

# The provisioning script. Deliberately a literal rather than a file copied
# from the checkout: `nyxgpt cloud deploy` must work from an
# artifact-installed CLI on a workstation with no repo, so the only thing
# that crosses the wire is this text plus the operator's config values.
#
# Every install step here matches the `artifact-install-smoke` job in
# .github/workflows/release-artifacts.yml, which runs the same sequence on a
# checkout-free runner on every release. `tests/unit/test_cloud_deploy.py`
# asserts the rendered script contains no `git clone` / `git://` /
# `github.com/...git` -- the repo-less requirement, enforced rather than
# documented.
PROVISION_SCRIPT_TEMPLATE = """set -euo pipefail

NYXGPT_VERSION="__VERSION__"
NYXGPT_PROFILES="__PROFILES__"
NYXGPT_SESSION_BACKEND_CHOICE="__SESSION_BACKEND__"

echo "==> nyxGPT ${NYXGPT_VERSION}: provisioning $(hostname) from published artifacts"

# --- OS packages -------------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y docker tar gzip >/dev/null
  # Newest first, stopping at the first that installs: Amazon Linux 2023
  # ships 3.9 as `python3` and offers 3.11+ only as separate packages.
  for pkg in python3.13 python3.12 python3.11; do
    if sudo dnf install -y "$pkg" "$pkg-pip" >/dev/null 2>&1; then
      break
    fi
  done
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
    python3 python3-venv python3-pip docker.io tar gzip >/dev/null
  # Ubuntu 22.04 LTS's `python3` is 3.10 -- below the floor; add an explicit
  # 3.11 when the distro's own is too old. Best-effort: the resolution below
  # is what decides, and it names the problem if nothing qualifies.
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
      python3.11 python3.11-venv >/dev/null 2>&1 || true
  fi
else
  echo "unsupported package manager: need dnf (Amazon Linux/Fedora) or apt-get (Debian/Ubuntu)" >&2
  exit 1
fi

# --- Resolve a Python that satisfies nyxGPT's requires-python (>= 3.11) -
# The distro's bare `python3` is never assumed sufficient (#3782): on Amazon
# Linux 2023 it is 3.9, and `pip install nyxgpt` into a venv built from it is
# refused with "requires a different Python: 3.9.x not in '>=3.11'". Ask each
# candidate its own version rather than trusting its name.
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "no Python >= 3.11 is installed or installable on this instance; nyxGPT's requires-python is >=3.11 (system python3: $(python3 -V 2>&1 || echo 'none'))" >&2
  exit 1
fi
echo "==> using $PY ($("$PY" -V 2>&1)) for the nyxGPT venv"

__NODE_SECTION__
# --- Docker (Cassandra + the observability stack run as containers) ----
# The Compose plugin is deliberately NOT installed here: Amazon Linux 2023
# packages no compose at all, so `nyxgpt ops install` owns that decision (it
# tries the distro package, then Docker's own release binary) rather than this
# script guessing per-distro package names.
sudo systemctl enable --now docker
sudo usermod -aG docker "$(id -un)" || true

# `usermod -aG` cannot reach an already-open login session, so every nyxGPT
# command that talks to the Docker socket runs under `sg docker`, which grants
# the group immediately and without a re-login. Resolved once, up front: the
# `sg ... || <bare command>` form this replaced re-ran the whole command
# *without* the group whenever the first attempt failed for an unrelated
# reason, turning one failed step into a "permission denied ...
# /var/run/docker.sock" cascade on the retry (#3760).
if command -v sg >/dev/null 2>&1 && sg docker -c true >/dev/null 2>&1; then
  run_nyxgpt() { sg docker -c "$NYXGPT $*"; }
else
  run_nyxgpt() { "$NYXGPT" "$@"; }
fi

# --- A systemd --user session that survives logout ---------------------
# `nyxgpt ops install` installs systemd --user units; without lingering they
# would stop the moment this SSH session closes.
sudo loginctl enable-linger "$(id -un)" || true
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

__LLM_RUNTIME_SECTION__
__NYXGPT_INSTALL__

# --- Put `nyxgpt` on the login user's PATH (#3993) ----------------------
# The venv above is never activated by a login shell, so an operator who SSHes
# in to diagnose a failed deploy got `nyxgpt: command not found` from every
# wrapped command the docs, the dashboard and this script's own output tell
# them to run -- while the binary sat in a directory nothing named. That turns
# "read the instance's own doctor" into a scavenger hunt at exactly the moment
# it matters. A profile.d drop-in rather than an edit to ~/.bashrc: it is
# idempotent (one file, rewritten), it applies to every login user rather than
# whichever one this deploy happened to use, and it leaves the operator's own
# dotfiles alone.
# The delimiter is quoted so the heredoc is written verbatim: $HOME has to be
# expanded by the *login shell that sources it*, not by this provisioning run,
# or every user would get the deploying user's home directory.
sudo tee /etc/profile.d/nyxgpt.sh >/dev/null <<'PROFILE_EOF'
# Managed by nyxgpt cloud deploy (#3993) -- rewritten on every deploy.
if [ -d "$HOME/.nyxGPT/venv/bin" ]; then
  PATH="$HOME/.nyxGPT/venv/bin:$PATH"
  export PATH
fi
PROFILE_EOF
sudo chmod 0644 /etc/profile.d/nyxgpt.sh
echo "==> nyxgpt is on the PATH of any login shell on this instance (/etc/profile.d/nyxgpt.sh); the binary itself is \\$HOME/.nyxGPT/venv/bin/nyxgpt"

# --- Seed config.ini from the installed package ------------------------
mkdir -p "$HOME/.nyxGPT"
if [ ! -f "$HOME/.nyxGPT/config.ini" ]; then
  EXAMPLE_CONFIG=$("$HOME/.nyxGPT/venv/bin/python" -c \\
    "from nyxgpt import config_wizard; print(config_wizard._EXAMPLE_CONFIG_PATH)")
  cp "$EXAMPLE_CONFIG" "$HOME/.nyxGPT/config.ini"
  chmod 600 "$HOME/.nyxGPT/config.ini"
fi

# --- Session storage backend (#3865) -----------------------------------
# `example.config.ini` ships the back-compat `session_backend = file`, which
# on a cloud instance means chats are JSON files on ephemeral instance disk:
# invisible to every other deployment mode pointed at the same Cassandra, and
# gone with the instance. Nothing in this path used to change it, so the
# cross-mode session guarantee (docs/session-storage.md, #3590) silently did
# not hold for cloud deploys and the only fix was to SSH in and hand-edit
# config.ini -- exactly the raw-operations flow the wrapped-command
# requirement forbids. Set it here instead, before `ops install`, so the
# derived containerized config (`_generate_compose_config`) and the API's
# first start both see the operator's choice.
#
# Idempotent (`ops session-backend` writes only on a change), so a re-deploy
# is a no-op here, and it never overrides an operator who deployed with
# `--session-backend file`: the choice is recorded in deploy.json and
# carried forward by `resolve_plan`.
"$NYXGPT" ops session-backend "$NYXGPT_SESSION_BACKEND_CHOICE"

__STACK_BRINGUP_SECTION__
# --- Self-healing ------------------------------------------------------
# A cloud deployment is unattended by definition: nobody is watching the
# instance to restart a component that dies, and the tunnel makes the stack
# reachable only while the operator is at their workstation. The watchdog
# ships disabled (example.config.ini's `[self_heal] enabled = false`, which
# seeds the runtime flag on first run), so a cloud deploy turns it on
# explicitly -- P6-16's acceptance criterion is a *self-healing* deployment,
# not merely a running one. Idempotent: it writes one flag in
# ~/.nyxGPT/self_heal_state.json, and the watchdog thread the API server
# already runs re-reads that flag every interval, so this takes effect
# without a restart on a re-deploy too.
"$NYXGPT" self-heal enable

echo "==> nyxGPT ${NYXGPT_VERSION} provisioned"
"""


# --- The native substrate's blocks (the default deploy, unchanged) -----

NATIVE_NODE_SECTION = """# --- Node.js 20 (the web bundle's build and run toolchain) -------------
# `nyxgpt ops install`'s "native web service" step runs `npm ci`/`npm run
# build`, and the wrapper it installs execs `npm run start`, so npm has to
# be here before `ops install` runs. Without it that step fails with "npm
# not found; cannot install nyxgpt-web" and the deploy leaves an API with no
# web surface, which is not a working nyxGPT (#3761).
#
# Node 20 from NodeSource rather than the distro repos -- the same source
# scripts/cloud/ec2-user-data-linux.sh.tmpl uses, and for the same reason:
# the AMIs in the target-OS support matrix ship a Node older than the one
# the web bundle builds against. Where NodeSource has no repo for the
# release, fall back to the distro's own packages, then verify.
#
# Every install is `|| true`: under `set -euo pipefail` a failed one would
# abort here with the package manager's own message, and the named
# diagnostic below -- the whole point of checking -- would never print.
# Nothing is swallowed; the installers' stderr still reaches the deploy
# output, and the verification immediately after is what decides.
NODE_MAJOR=0
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -v | sed 's/^v//' | cut -d. -f1)
fi
if ! command -v npm >/dev/null 2>&1 || [ "${NODE_MAJOR:-0}" -lt 20 ]; then
  if command -v dnf >/dev/null 2>&1; then
    if curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash - >/dev/null; then
      sudo dnf install -y nodejs >/dev/null || true
    else
      sudo dnf install -y nodejs20 nodejs20-npm >/dev/null \\
        || sudo dnf install -y nodejs npm >/dev/null || true
    fi
  else
    if curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null; then
      sudo apt-get install -y -qq nodejs >/dev/null || true
    else
      sudo apt-get install -y -qq nodejs npm >/dev/null || true
    fi
  fi
fi

# Stop here, naming the actual problem, rather than most of the way through
# `ops install` with a bare "npm not found".
if ! command -v npm >/dev/null 2>&1; then
  echo "node/npm could not be installed -- 'nyxgpt ops install' cannot build the nyxgpt-web bundle without npm" >&2
  exit 1
fi
"""

NATIVE_LLM_RUNTIME_SECTION = """# --- Ollama ------------------------------------------------------------
command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh
"""

NATIVE_STACK_BRINGUP_SECTION = """# --- Retire a Kubernetes substrate, if this box was running one --------
# `--no-kubernetes` is a documented transition, so it has to actually move
# the instance rather than install a second stack beside the first. Without
# this, the k3s cluster and the `Restart=always` access bridge keep holding
# 127.0.0.1:8000/3000; the freshly installed native services never bind; and
# every probe that would notice -- `ops install`'s health wait, the deploy's
# own check, the tunnel -- is answered by the cluster the operator just asked
# to leave. Nothing reports a failure. The box simply keeps serving from the
# substrate `deploy.json` now records it as no longer running, on an instance
# sized for one stack and running two.
#
# Both halves are guarded on existence and idempotent, so a first deploy and
# an ordinary native re-deploy pass straight through. `k3s-uninstall.sh` is
# the uninstaller the k3s installer itself writes; it takes the cluster, its
# containerd image store and its `local-path` volumes with it, which is the
# same data `cloud destroy` would have taken and the reason the docs say a
# substrate switch moves the instance rather than migrating it.
for bridge in api web observability; do
  systemctl --user disable --now "nyxgpt-k8s-bridge@${bridge}.service" >/dev/null 2>&1 || true
done
rm -f "$HOME/.config/systemd/user/nyxgpt-k8s-bridge@.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true
if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
  echo "==> removing the k3s cluster this instance was running (--no-kubernetes)"
  sudo /usr/local/bin/k3s-uninstall.sh
fi

# --- Bring the stack up ------------------------------------------------
# Services bind 127.0.0.1 (P6-1 loopback default); nothing is published to a
# non-loopback address, which is what makes the SSH tunnel the only path in.
#
# Exactly one install pass per deploy (#3762). `ops install` without
# `--skip-observability` already runs the observability volume/secret/stack
# steps, so the `ops observability` call that used to follow it re-ran the
# same Grafana provisioning reconcile a second time -- work the operator
# reads as the deploy repeating itself, on top of the `|| "$NYXGPT" ...`
# retry #3760 removed. A re-deploy still converges: `ops install` is a
# reconcile, not a first-run-only bootstrap.
if [ -n "$NYXGPT_PROFILES" ]; then
  run_nyxgpt ops install__OPS_INSTALL_FLAGS__
else
  run_nyxgpt ops install --skip-observability__OPS_INSTALL_FLAGS__
fi
"""


# --- The Kubernetes substrate's blocks (#3506's decision, #3956) -------
#
# #3506, approved by the owner 2026-08-04, decided **EC2 single-box with the
# existing `k8s/*.yaml` manifests optionally layered on a single-node k3s
# cluster for canary** -- rejecting a managed EKS control plane, not
# Kubernetes. Its downstream section is explicit that this is "no new
# deployment code path, just the existing `--kubernetes` install mode
# pointed at the remote box over the P6-4 SSH path instead of a local
# cluster", and that is exactly what these blocks do: they put a cluster on
# the instance and then run the *existing* `nyxgpt ops install --kubernetes
# --local` there. `_ensure_kubectl_and_cluster` already takes the
# bring-your-own-cluster path whenever `kubectl cluster-info` answers, so the
# install mode needs no notion of "remote" at all -- the box it installs on
# is simply not the operator's workstation.
#
# Two things are deliberately NOT installed in this mode, because the
# cluster supplies them and a second host-level copy would be worse than
# useless:
#
#   * **Ollama.** `k8s/statefulset-ollama.yaml` runs it in-cluster. A host
#     Ollama would be a second model server on the same box, holding a
#     second copy of every pulled model in RAM, that nothing points at.
#   * **Node/npm.** The native web service builds the Next bundle on the
#     host with `npm ci`; in Kubernetes mode `nyxgpt-web:local` is a
#     *container* built by docker from the published `nyxgpt-web` artifact
#     (`_build_and_load_k8s_web_image`), so the host toolchain is never
#     used. Installing it would add a NodeSource repo and several minutes to
#     every deploy for nothing.
#
# Docker IS still installed: the k8s install path builds both images with it
# before importing them into k3s's containerd (`_k3s_import_image`).

# The k3s server flags, as one list so the CI proxy that executes this text
# and the tests that assert on it read the same source (#3956).
#
#   --bind-address / --advertise-address / --node-ip
#       Pin the apiserver and the node to the instance's PRIVATE address.
#       k3s's default is 0.0.0.0, which would leave the apiserver listening
#       on the public interface -- refused by the security group, but
#       listening, and #3503's access model is that nothing but TCP 22 is
#       reachable. The private address rather than 127.0.0.1 because the
#       in-cluster `kubernetes` Service is built from the advertise address:
#       pinned to loopback, every Pod that talks to the API server would
#       dial its own loopback instead. "Loopback or private" is what the
#       requirement asks for, and only one of the two is correct here.
#   --tls-san
#       So the serving certificate is valid for the address the kubeconfig
#       below is rewritten to.
#   --disable=traefik
#       No ingress controller. k3s ships Traefik on by default and it binds
#       host ports 80/443.
#   --disable=servicelb
#       No `Service: LoadBalancer` implementation. `k8s/*.yaml` declares no
#       LoadBalancer Service (that is #3506's cluster-flavor-agnostic
#       premise), so removing the controller costs nothing and makes the
#       absence structural rather than a convention a later manifest could
#       break silently.
#
# `local-storage` is deliberately LEFT ENABLED: the Cassandra and Ollama
# StatefulSets declare `volumeClaimTemplates` with no `storageClassName`, so
# they bind through whatever the cluster's default StorageClass is, and on
# k3s that is `local-path`. Disabling it would leave both Pods Pending on
# unbound PVCs.

# The pod and Service networks, pinned OFF k3s's own defaults.
#
# k3s defaults to `--cluster-cidr=10.42.0.0/16` and `--service-cidr=10.43.0.0/16`,
# and terraform/aws/variables.tf defaults the substrate VPC to 10.42.0.0/16 --
# byte-identical. That collision is not a near miss, it is fatal and silent
# (#3956 acceptance failure, 2026-08-22, owner-diagnosed on a real instance):
#
#   1. AWS puts the VPC's AmazonProvidedDNS resolver at VPC-base+2, i.e.
#      10.42.0.2, and hands it to the host over DHCP.
#   2. k3s starts, and its CNI claims the on-node route for 10.42.0.0/16 --
#      shadowing the resolver. Host queries to 10.42.0.2 now land on whatever
#      Pod holds that address, which is CoreDNS itself.
#   3. CoreDNS forwards to the node's resolv.conf -> 10.42.0.2 -> itself. Its
#      loop guard fires (`[FATAL] plugin/loop: Loop ... detected for zone "."`)
#      and it CrashLoopBackOffs. Cluster DNS is never up for one second.
#   4. Every symptom surfaces layers away from the cause: the deploy fails on
#      "Ollama did not become ready in time", because the ollama Pod's model
#      pulls cannot resolve registry.ollama.ai.
#
# Fixed on the k3s side rather than by moving the VPC default. Changing
# `vpc_cidr` forces Terraform to REPLACE the VPC -- and with it the subnet, the
# instance and its root volume -- on the next apply against an existing
# substrate, which is a data-loss migration to fix a defect the cluster side
# can fix with two flags.
#
# 100.64.0.0/10 is RFC 6598 (carrier-grade NAT) space: reserved, routable
# nowhere on the public internet, and outside every RFC 1918 range a VPC is
# normally cut from (10/8, 172.16/12, 192.168/16). An operator CAN still cut a
# VPC from it -- AWS allows it -- which is why the pin is paired with the
# runtime overlap guard in the bootstrap below rather than trusted on its own.
K3S_CLUSTER_CIDR = "100.96.0.0/16"
K3S_SERVICE_CIDR = "100.97.0.0/16"

K3S_SERVER_FLAGS: tuple[str, ...] = (
    "--bind-address=$NYXGPT_NODE_IP",
    "--advertise-address=$NYXGPT_NODE_IP",
    "--node-ip=$NYXGPT_NODE_IP",
    "--tls-san=$NYXGPT_NODE_IP",
    "--disable=traefik",
    "--disable=servicelb",
    f"--cluster-cidr={K3S_CLUSTER_CIDR}",
    f"--service-cidr={K3S_SERVICE_CIDR}",
    # Empty off EC2 -- see the resolv.conf block in the bootstrap. `k3s`'s
    # cluster-dns address is derived from --service-cidr, so it moves with it
    # and is not pinned separately.
    "$NYXGPT_K3S_RESOLV_FLAG",
)

KUBERNETES_LLM_RUNTIME_SECTION = """# --- Single-node Kubernetes (k3s) --------------------------------------
# #3506's decision, implemented (#3956). Ollama is NOT installed on the host
# in this mode -- k8s/statefulset-ollama.yaml runs it in the cluster.

# The address k3s binds to. IMDSv2 first (this is an EC2 instance by
# construction), falling back to the first address the kernel reports, which
# is what makes this same text executable on a plain Linux machine -- the
# k3s-cloud-smoke CI job runs exactly this block.
NYXGPT_IMDS_TOKEN=$(curl -sf -m 5 -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null || true)
NYXGPT_NODE_IP=""
if [ -n "$NYXGPT_IMDS_TOKEN" ]; then
  NYXGPT_NODE_IP=$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: $NYXGPT_IMDS_TOKEN" \\
    "http://169.254.169.254/latest/meta-data/local-ipv4" 2>/dev/null || true)
fi
if [ -z "$NYXGPT_NODE_IP" ]; then
  NYXGPT_NODE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$NYXGPT_NODE_IP" ]; then
  echo "could not determine this instance's private IPv4 address; k3s needs one to bind to" >&2
  exit 1
fi
echo "==> k3s will bind to $NYXGPT_NODE_IP (private) -- nothing listens on the public interface"

# --- Refuse a VPC network that overlaps the pod/Service networks -------
# The pin above moves the cluster off the substrate's default VPC range; this
# refuses the case the pin cannot cover -- an operator-chosen `vpc_cidr` that
# overlaps anyway. Without it the failure mode is a CrashLoopBackOff CoreDNS
# and a deploy that reports "Ollama did not become ready in time" 95 minutes
# later, three layers from the cause (#3956 acceptance failure).
#
# Arithmetic, not bit operations: `and()`/`lshift()` are gawk extensions and
# Debian/Ubuntu's default awk is mawk, which has neither. Two CIDRs overlap
# exactly when they share a network under the SHORTER of the two prefixes.
nyxgpt_cidrs_overlap() {
  awk -v a="$1" -v b="$2" '
    function ipnum(ip,   p) {
      split(ip, p, ".")
      return ((p[1] * 256 + p[2]) * 256 + p[3]) * 256 + p[4]
    }
    BEGIN {
      split(a, x, "/"); split(b, y, "/")
      pa = (x[2] == "" ? 32 : x[2] + 0); pb = (y[2] == "" ? 32 : y[2] + 0)
      p = (pa < pb ? pa : pb)
      size = 2 ^ (32 - p)
      exit (int(ipnum(x[1]) / size) == int(ipnum(y[1]) / size)) ? 0 : 1
    }'
}

# NYXGPT_VPC_CIDRS is read from IMDS on an EC2 instance. It is overridable so
# the executed-verification job can inject a colliding VPC and prove the
# refusal fires (there is no hosted runner inside a VPC), and so an operator
# running this bootstrap on a non-EC2 Linux box can state their own network.
NYXGPT_VPC_CIDRS="${NYXGPT_VPC_CIDRS:-}"
if [ -z "$NYXGPT_VPC_CIDRS" ] && [ -n "$NYXGPT_IMDS_TOKEN" ]; then
  NYXGPT_IMDS_MAC=$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: $NYXGPT_IMDS_TOKEN" \\
    "http://169.254.169.254/latest/meta-data/mac" 2>/dev/null || true)
  if [ -n "$NYXGPT_IMDS_MAC" ]; then
    NYXGPT_VPC_CIDRS=$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: $NYXGPT_IMDS_TOKEN" \\
      "http://169.254.169.254/latest/meta-data/network/interfaces/macs/${NYXGPT_IMDS_MAC}/vpc-ipv4-cidr-blocks" \\
      2>/dev/null || true)
  fi
fi
if [ -n "$NYXGPT_VPC_CIDRS" ]; then
  echo "==> VPC IPv4 network(s): $(echo $NYXGPT_VPC_CIDRS)"
  for nyxgpt_vpc_cidr in $NYXGPT_VPC_CIDRS; do
    for nyxgpt_k3s_cidr in __K3S_CLUSTER_CIDR__ __K3S_SERVICE_CIDR__; do
      if nyxgpt_cidrs_overlap "$nyxgpt_vpc_cidr" "$nyxgpt_k3s_cidr"; then
        echo "the VPC network $nyxgpt_vpc_cidr overlaps the k3s network $nyxgpt_k3s_cidr." >&2
        echo "Refusing to install the cluster: the CNI would claim on-node routes for that" >&2
        echo "range and shadow the VPC's DNS resolver, which sends CoreDNS into a query loop" >&2
        echo "it kills itself over -- cluster AND host DNS dead, surfacing minutes later as an" >&2
        echo "unrelated readiness timeout. Give the substrate a VPC network that does not" >&2
        echo "overlap __K3S_CLUSTER_CIDR__ or __K3S_SERVICE_CIDR__ (the vpc_cidr variable in the" >&2
        echo "substrate's Terraform configuration), then deploy again." >&2
        exit 1
      fi
    done
  done
  echo "==> VPC network and k3s pod/Service networks do not overlap"
else
  echo "==> no VPC network reported (not an EC2 instance, or IMDS unreachable); overlap check skipped"
fi

# --- CoreDNS's upstream resolver, on AWS -------------------------------
# Defence in depth for the same class of failure. k3s's bundled CoreDNS
# forwards to the node's resolv.conf, so a resolver address that a pod network
# can ever shadow puts CoreDNS one routing change away from resolving itself.
# 169.254.169.253 is the SAME AmazonProvidedDNS resolver on its link-local
# alias: identical answers, and no pod CIDR can shadow 169.254.0.0/16 because
# no cluster is cut from link-local space.
#
# A k3s SERVER FLAG rather than a patch to the CoreDNS ConfigMap: k3s
# re-applies its bundled CoreDNS manifest on every service restart, so a
# patched ConfigMap silently reverts. --resolv-conf survives that, because it
# is what the manifest is rendered against.
#
# Empty off EC2: 169.254.169.253 exists only inside a VPC, and pointing a
# non-AWS machine's cluster at it would break DNS to fix a problem it does not
# have. That is the path the CI proxy takes, and k3s's own default applies.
NYXGPT_K3S_RESOLV_FLAG=""
if [ -n "$NYXGPT_IMDS_TOKEN" ]; then
  # The VPC's search domain (`ec2.internal`, `<region>.compute.internal`) is
  # carried over rather than dropped: this file replaces the node's resolv.conf
  # for every Pod's search list too, and silently losing the domain would break
  # short-name resolution on a box where it used to work.
  NYXGPT_DNS_SEARCH=$(awk '/^search /{ $1=""; sub(/^ /, ""); print; exit }' \\
    /etc/resolv.conf 2>/dev/null || true)
  sudo mkdir -p /etc/rancher/k3s
  {
    echo "nameserver 169.254.169.253"
    if [ -n "$NYXGPT_DNS_SEARCH" ]; then echo "search $NYXGPT_DNS_SEARCH"; fi
    echo "options timeout:2 attempts:3"
  } | sudo tee /etc/rancher/k3s/resolv.conf >/dev/null
  NYXGPT_K3S_RESOLV_FLAG="--resolv-conf=/etc/rancher/k3s/resolv.conf"
  echo "==> CoreDNS will forward to 169.254.169.253 (the VPC resolver's link-local alias)"
fi

# Skipped when k3s is already here, which makes a re-deploy fast and keeps
# the running cluster's Pods up. The assumption that makes that safe: the
# existing server was installed by *this* text, so its flags are these
# flags. It holds today -- NYXGPT_NODE_IP is derived the same way on every
# run and K3S_SERVER_FLAGS is a constant -- but it means a future change to
# K3S_SERVER_FLAGS would not reach an instance whose k3s predates it. If you
# change those flags in a way that has to take effect on an existing box,
# re-install the server here (or say so in the release notes); the operator's
# other route is `nyxgpt cloud destroy` and a fresh deploy.
#
# The one case that MUST reach an existing box is the flag change this fix is:
# a cluster whose pod or Service network overlaps the VPC has DNS that cannot
# work, so leaving it in place would make a re-deploy a silent no-op on top of
# a broken cluster. It is replaced rather than left, and only when the overlap
# is measured -- a pre-fix cluster in a VPC that never collided is working, and
# rebuilding it would destroy its volumes to fix nothing.
if command -v k3s >/dev/null 2>&1 && [ -n "$NYXGPT_VPC_CIDRS" ]; then
  # No flag in the unit means k3s's own defaults, which are the colliding pair.
  # The `|| true` is load-bearing under `set -euo pipefail`: a grep that
  # matches nothing exits 1, and `VAR=$(...)` propagates that status -- so the
  # unflagged case (the very one being detected) would abort the deploy here.
  NYXGPT_OLD_CLUSTER_CIDR=$(grep -oE -- '--cluster-cidr=[0-9./]+' \\
    /etc/systemd/system/k3s.service 2>/dev/null | head -n1 | cut -d= -f2 || true)
  NYXGPT_OLD_SERVICE_CIDR=$(grep -oE -- '--service-cidr=[0-9./]+' \\
    /etc/systemd/system/k3s.service 2>/dev/null | head -n1 | cut -d= -f2 || true)
  [ -n "$NYXGPT_OLD_CLUSTER_CIDR" ] || NYXGPT_OLD_CLUSTER_CIDR="10.42.0.0/16"
  [ -n "$NYXGPT_OLD_SERVICE_CIDR" ] || NYXGPT_OLD_SERVICE_CIDR="10.43.0.0/16"
  for nyxgpt_vpc_cidr in $NYXGPT_VPC_CIDRS; do
    for nyxgpt_k3s_cidr in "$NYXGPT_OLD_CLUSTER_CIDR" "$NYXGPT_OLD_SERVICE_CIDR"; do
      if nyxgpt_cidrs_overlap "$nyxgpt_vpc_cidr" "$nyxgpt_k3s_cidr"; then
        echo "==> the cluster already on this instance uses $nyxgpt_k3s_cidr, which overlaps"
        echo "==> the VPC network $nyxgpt_vpc_cidr -- its DNS cannot work. Replacing it."
        sudo /usr/local/bin/k3s-uninstall.sh
        # Then a check, not a `|| true`: a half-completed uninstall that left
        # the binary behind would fall through to the "k3s is already here"
        # fast path below and reuse the broken cluster it just tried to
        # remove -- the exact silence this block exists to end. `hash -r`
        # first so the check reads the filesystem rather than a cached lookup.
        hash -r 2>/dev/null || true
        if command -v k3s >/dev/null 2>&1; then
          echo "k3s-uninstall.sh ran but k3s is still installed; the cluster on this" >&2
          echo "instance cannot resolve names and cannot be replaced in place. Run" >&2
          echo "'nyxgpt cloud destroy --yes' and deploy again onto a fresh instance." >&2
          exit 1
        fi
        break 2
      fi
    done
  done
fi
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io \\
    | INSTALL_K3S_EXEC="server __K3S_SERVER_FLAGS__" sh -
fi
sudo systemctl enable --now k3s

# A kubeconfig this login user owns, so `nyxgpt ops install --kubernetes
# --local`, `nyxgpt canary` and `nyxgpt ops port-forward` all find the
# cluster with no environment variable and no `sudo`. Read through `sudo
# cat` and redirected as the user rather than loosening
# /etc/rancher/k3s/k3s.yaml with --write-kubeconfig-mode: that file holds
# cluster-admin credentials and world-readable is the wrong trade on a box
# whose whole access model is "one port, one user".
mkdir -p "$HOME/.kube"
sudo cat /etc/rancher/k3s/k3s.yaml > "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"
# k3s writes `server: https://127.0.0.1:6443` regardless of --bind-address,
# so a kubeconfig left as written points at an address nothing listens on.
sed -i "s#https://127.0.0.1:6443#https://${NYXGPT_NODE_IP}:6443#" "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"

echo "==> waiting for the k3s node to report Ready"
NYXGPT_NODE_READY=0
for _ in $(seq 1 60); do
  if kubectl wait --for=condition=Ready node --all --timeout=10s >/dev/null 2>&1; then
    NYXGPT_NODE_READY=1
    break
  fi
  sleep 5
done
if [ "$NYXGPT_NODE_READY" -ne 1 ]; then
  echo "the k3s node never reported Ready; 'kubectl get nodes' on the instance will say why" >&2
  exit 1
fi
kubectl get nodes -o wide
"""

KUBERNETES_STACK_BRINGUP_SECTION = """# --- Bring the stack up, on the cluster --------------------------------
# The EXISTING install mode, pointed at the box the cluster is on (#3506):
# no cloud-specific deployment code path, and `k8s/*.yaml` is applied
# unchanged by `_kubectl_apply_kustomization`.
#
# `__OPS_INSTALL_FLAGS__` carries `--dev` (#3950) through unchanged, for
# the same reason the native section does: `ops install --kubernetes
# --local --dev` builds the api and web images from the checkout, which
# on this box is the tree the deploy shipped to `$HOME/.nyxGPT/src`.
# Nothing about the substrate makes that a different question.
export KUBECONFIG="$HOME/.kube/config"

# --- Retire a native substrate, if this box was running one ------------
# The mirror image of the native section's k3s teardown, and the direction
# that used to fail loudly instead of silently: `install_kubernetes_local`'s
# `_refuse_port_collision` aborts under `set -euo pipefail` and tells the
# operator to run `nyxgpt ops down` on the instance -- which no wrapped
# `nyxgpt cloud` command can do, the `cloud ops` allowlist being read-only,
# so the only way forward was `cloud destroy`. Run the wrapped teardown here
# and the transition completes on its own.
#
# Guarded on a native unit actually being installed, so a first deploy does
# not run a teardown against an empty box. `ops down` preserves volumes, so
# the native Cassandra's data is still there if the operator switches back --
# but the cluster runs its own Cassandra, so a switch moves the instance, it
# does not migrate its sessions.
if [ -f "$HOME/.config/systemd/user/nyxgpt-api.service" ]; then
  echo "==> retiring the native stack this instance was running (--kubernetes)"
  run_nyxgpt ops down
fi

if [ -n "$NYXGPT_PROFILES" ]; then
  run_nyxgpt ops install --kubernetes --local__OPS_INSTALL_FLAGS__
else
  run_nyxgpt ops install --kubernetes --local --skip-observability__OPS_INSTALL_FLAGS__
fi

# --- The access bridge -------------------------------------------------
# `k8s/`'s Services are ClusterIP-only -- no Ingress, no LoadBalancer, which
# is #3506's premise and #3503's requirement -- so nothing binds
# 127.0.0.1:8000/3000 on the instance the way the native services do. The
# SSH tunnel forwards to the instance's loopback, so without a bridge from
# loopback into the cluster a `--kubernetes` deploy would install a perfectly
# healthy stack and then fail its own health check.
#
# Locally an operator runs `nyxgpt ops port-forward` in a terminal and leaves
# it there. A cloud deployment is unattended by definition (the same reason
# `self-heal enable` below exists), so the same wrapped command runs as a
# systemd --user service instead: no raw `kubectl` in any operator-facing
# flow (CLAUDE.md's wrapper requirement), and `Restart=always` reconnects the
# forward when a Pod is replaced -- which a canary rollout does by design.
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/nyxgpt-k8s-bridge@.service" <<'UNIT'
[Unit]
Description=nyxGPT Kubernetes access bridge (%i)
After=network-online.target

[Service]
Environment=KUBECONFIG=%h/.kube/config
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.nyxGPT/venv/bin/nyxgpt ops port-forward --target %i
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now nyxgpt-k8s-bridge@api.service
systemctl --user enable --now nyxgpt-k8s-bridge@web.service
if [ -n "$NYXGPT_PROFILES" ]; then
  systemctl --user enable --now nyxgpt-k8s-bridge@observability.service
fi
"""

# The one block that differs between the two build sources, spliced into
# `__NYXGPT_INSTALL__` above. Everything else the instance needs -- the OS
# packages, the Python floor, Node, Docker, lingering, Ollama, the seeded
# config, the session backend, `ops install`, self-heal -- is identical, and
# is deliberately *one* template rather than two: a second copy of a
# 200-line bootstrap is a copy that drifts, and the defects this script has
# collected (#3759, #3760, #3761, #3762, #3782) would each have had to be
# found and fixed twice.
ARTIFACT_INSTALL_BLOCK = """# --- nyxGPT itself, from the published PyPI artifact -------------------
# No checkout is created on this machine, by design (CLAUDE.md, repo-less
# portability 2026-08-01): the instance installs the same published release
# an operator would install on a laptop.
"$PY" -m venv "$HOME/.nyxGPT/venv" >/dev/null
"$HOME/.nyxGPT/venv/bin/pip" install --quiet --upgrade pip
"$HOME/.nyxGPT/venv/bin/pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"
NYXGPT="$HOME/.nyxGPT/venv/bin/nyxgpt\""""

# `--dev` (#3950). Still no clone and no download of source: the tree was
# copied here over this deploy's own SSH connection by `ship_working_tree`
# before this script ran.
#
# The guard earns its place twice, both measured on a bare AL2023 box
# (`scripts/cloud-dev-deploy-smoke.sh` phase 1). With the directory *absent*
# and the guard removed, the run builds the venv and then dies inside pip with
# "is not a valid editable requirement ... or a VCS URL (beginning with
# bzr+http, ...)" -- a message about version control, ninety seconds in, for
# an operator whose actual problem is that a file copy did not happen. With a
# *stale* tree left by an earlier `--dev` deploy there is no error at all:
# the box installs the old code and comes up healthy, and the operator finds
# out when their change appears to have done nothing.
DEV_INSTALL_BLOCK = """# --- nyxGPT itself, from the working tree this deploy shipped ----------
if [ ! -f "__REMOTE_SOURCE__/pyproject.toml" ]; then
  echo "no working tree at __REMOTE_SOURCE__ -- --dev ships one before provisioning, and this instance has none" >&2
  exit 1
fi
"$PY" -m venv "$HOME/.nyxGPT/venv" >/dev/null
"$HOME/.nyxGPT/venv/bin/pip" install --quiet --upgrade pip
# Editable, so the api service on the box runs the shipped tree rather than a
# copy of it -- the same property `nyxgpt up --dev` gives a laptop, and what
# makes `ops install --dev` below resolve its checkout to this directory.
"$HOME/.nyxGPT/venv/bin/pip" install --quiet -e "__REMOTE_SOURCE__"
NYXGPT="$HOME/.nyxGPT/venv/bin/nyxgpt\""""


def render_provision_script(plan: DeployPlan) -> str:
    """Render the instance provisioning script for `plan`'s target OS.

    Kept a pure function so the repo-less guarantee (no clone, no checkout
    copy on the default path), the artifact version pin, `--dev`'s
    working-tree source (#3950) and the k3s server flags (#3956) are all
    unit-testable without an EC2 instance.

    The macOS branch renders the *same* bootstrap `nyxgpt cloud user-data
    --os macos` prints (`cloud_provision.render_user_data`, P6-12/#3511)
    rather than a second copy of it. That script was already the only thing
    that knows how to bring nyxGPT up on an EC2 Mac -- remote Homebrew tap,
    `brew services`, no clone -- and until #3867 the only way to get it onto
    an instance was for a human to paste it into an AWS console launch.
    Nothing about it changes here; what changes is that `deploy` delivers it.

    It carries neither a substrate section nor a `--dev` variant, and
    `resolve_plan` refuses both combinations rather than letting them reach
    here. Same reasoning in both cases: a bootstrap that quietly ignored the
    flag would install something the operator did not ask for -- a published
    release to someone testing their tree (#3950), or a native stack to
    someone who asked for a cluster (#3956) -- and then record a deploy that
    claims otherwise.

    On Linux the substrate blocks are substituted rather than guarded at
    runtime on a shell variable, so a `--kubernetes` script contains no host
    Ollama or NodeSource install and a native one installs no cluster. Each
    script does contain the *teardown* of the substrate it replaces, which is
    what makes `--kubernetes`/`--no-kubernetes` a transition rather than a
    second stack installed beside the first; both teardowns are guarded on
    existence, so a first deploy runs neither.
    """
    if plan.os_family == OS_FAMILY_MACOS:
        from nyxgpt import cloud_provision

        return cloud_provision.render_user_data(OS_FAMILY_MACOS, plan.version, plan.session_backend)
    install_block = DEV_INSTALL_BLOCK if plan.dev else ARTIFACT_INSTALL_BLOCK
    if plan.kubernetes:
        node_section = ""
        llm_section = render_k3s_bootstrap()
        bringup_section = KUBERNETES_STACK_BRINGUP_SECTION
    else:
        node_section = NATIVE_NODE_SECTION
        llm_section = NATIVE_LLM_RUNTIME_SECTION
        bringup_section = NATIVE_STACK_BRINGUP_SECTION
    return (
        PROVISION_SCRIPT_TEMPLATE.replace("__NYXGPT_INSTALL__", install_block)
        .replace("__REMOTE_SOURCE__", REMOTE_SOURCE_DIR)
        .replace("__VERSION__", plan.version)
        .replace("__PROFILES__", ",".join(plan.profiles))
        .replace("__SESSION_BACKEND__", plan.session_backend)
        .replace("__NODE_SECTION__", node_section)
        .replace("__LLM_RUNTIME_SECTION__", llm_section)
        .replace("__STACK_BRINGUP_SECTION__", bringup_section)
        # Last: `__OPS_INSTALL_FLAGS__` is inside the bringup section
        # that line just spliced in, not in the template itself.
        .replace("__OPS_INSTALL_FLAGS__", " --dev" if plan.dev else "")
    )


def render_k3s_bootstrap() -> str:
    """Return the k3s bootstrap block exactly as a `--kubernetes` deploy sends it.

    Public and standalone so the executed-verification job
    (`.github/workflows/k3s-cloud-smoke.yml`) can run *this* text on a real
    Linux machine rather than a hand-copied approximation of it -- the
    difference between proving the deploy's own bootstrap works and proving
    that something like it does (D-006).
    """
    return (
        KUBERNETES_LLM_RUNTIME_SECTION.replace("__K3S_SERVER_FLAGS__", " ".join(K3S_SERVER_FLAGS))
        .replace("__K3S_CLUSTER_CIDR__", K3S_CLUSTER_CIDR)
        .replace("__K3S_SERVICE_CIDR__", K3S_SERVICE_CIDR)
    )


def provision_remote_command(plan: DeployPlan) -> str:
    """The shell command the rendered bootstrap is piped into on the instance.

    Linux's script is written to run as the login user and elevates per step
    with `sudo`, so it is fed to a plain `bash -s`.

    The macOS script refuses to run as anyone but root -- `ec2-macos-init`
    hands user-data to root, and the script drops to the login user with
    `sudo -u` for every Homebrew call, which is not a thing it can do from
    inside that user's own session. Over SSH there is no `ec2-macos-init`, so
    the deploy elevates in its place. `sudo -n` (non-interactive) so a Mac
    whose login user needs a password fails immediately with sudo's own
    message rather than hanging a deploy on a prompt no wrapped command can
    answer, and `NYXGPT_TARGET_USER` so the script installs Homebrew for the
    user the operator actually logged in as instead of assuming `ec2-user`.
    """
    if plan.os_family == OS_FAMILY_MACOS:
        return f"sudo -n NYXGPT_TARGET_USER={shlex.quote(plan.ssh_user)} bash -s"
    return "bash -s"


# How many trailing lines of the provisioning output a failure summary quotes
# when the run produced no `[FAIL]` line to name (an OS-package or shell
# error before `ops install` ever started, say). Whole lines, never a
# character slice -- see `_provision_failure_detail`.
_PROVISION_TAIL_LINES = 25

# The prefix `nyxgpt ops` puts on every failed check, including the per-step
# verdict line `_emit_step_verdict` adds (#3762).
_OPS_FAIL_PREFIX = "[FAIL]"


def _run_provision_script(
    target: DeployTarget, script: str, remote_command: str = "bash -s"
) -> tuple[int, list[str]]:
    """Run `script` on the instance, echoing its output live and keeping a copy.

    stdout was streamed straight to the terminal before and stderr was
    captured but never shown until the end, so the two interleaved wrongly
    for the operator *and* the only text a failure could quote was stderr --
    which is precisely where `ops install`'s `[FAIL]` lines are not (they go
    to stdout). Merging the streams fixes both: errors appear in place, and
    the failure summary can name the steps that actually failed (#3762).

    The script is written to stdin in one go before reading: it is a few KB,
    comfortably inside the pipe buffer, so this cannot deadlock against a
    remote that hasn't started draining yet.
    """
    argv = [*ssh_argv(target), remote_command]
    output: list[str] = []
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None  # nosec B101 - pipes requested
        proc.stdin.write(script)
        proc.stdin.close()
        for line in proc.stdout:
            print(line, end="")
            output.append(line.rstrip("\n"))
    return proc.returncode, output


def _provision_failure_detail(output: list[str]) -> str:
    """Summarize why provisioning failed, from the run's own output.

    Lists every failed step in full rather than splicing a fixed number of
    characters off the end of the log: the owner's 2026-08-14 acceptance run
    ended on `Provisioning the instance failed: 2-user/.nyxGPT/volumes/...`,
    where `2-user` is the tail of `ec2-user` left by a mid-word slice of the
    captured stderr (#3762). Failed steps are quoted untruncated; the tail is
    only a fallback, and is cut on line boundaries.
    """
    failures = [line.strip() for line in output if line.strip().startswith(_OPS_FAIL_PREFIX)]
    if failures:
        listed = "\n".join(f"  {line}" for line in failures)
        return f"Provisioning the instance failed. Failed steps:\n{listed}"
    tail = [line for line in output if line.strip()][-_PROVISION_TAIL_LINES:]
    if not tail:
        return "Provisioning the instance failed (no diagnostic returned)."
    quoted = "\n".join(f"  {line}" for line in tail)
    return (
        "Provisioning the instance failed with no step-level diagnostic. "
        f"Last {len(tail)} line(s) of its output:\n{quoted}"
    )


def provision_instance(target: DeployTarget, plan: DeployPlan) -> dict[str, Any]:
    """Install and start the nyxGPT stack on the instance.

    Idempotent: the script skips work that is already done (Ollama, the
    seeded config) and `nyxgpt ops install` is itself a reconcile, so a
    re-deploy converges rather than duplicating.
    """
    script = render_provision_script(plan)
    # `bash -s` over stdin rather than a quoted argv: the script is long and
    # multi-line, and piping it keeps quoting out of the picture entirely.
    returncode, output = _run_provision_script(target, script, provision_remote_command(plan))
    if returncode != 0:
        raise CloudCommandError(_provision_failure_detail(output))
    # `self_heal_enabled` is safe to assert rather than re-probe on Linux: the
    # script runs under `set -euo pipefail`, so a failed `self-heal enable`
    # would have made this a non-zero exit and raised above. The macOS
    # bootstrap has no such step, so this reports False rather than claiming a
    # watchdog that is not running (#3867). The same "it exited 0, so it
    # happened" reasoning covers `substrate`: in Kubernetes mode the script
    # does not reach this point unless k3s came up, the node went Ready and
    # `ops install --kubernetes --local` succeeded.
    return {
        "version": plan.version,
        "os_family": plan.os_family,
        "profiles": list(plan.profiles),
        "self_heal_enabled": plan.os_family == OS_FAMILY_LINUX,
        "substrate": plan.substrate,
        "dev": plan.dev,
    }


# --- The tunnel (the P6-4 access path) ---------------------------------


def tunnel_ports(profiles: list[str] | None = None) -> list[tuple[str, int]]:
    """Return the `(service, port)` pairs to forward for `profiles`.

    Core services always; observability UIs only for profiles the deploy
    actually enabled, so `--skip-observability` doesn't forward four dead
    ports.
    """
    ports = list(CORE_TUNNEL_PORTS)
    for profile in profiles or []:
        ports.extend(OBSERVABILITY_TUNNEL_PORTS.get(profile, ()))
    return ports


def tunnel_urls(profiles: list[str] | None = None) -> dict[str, str]:
    """Return `{service: http://localhost:<port>}` for the forwarded services."""
    return {name: f"http://localhost:{port}" for name, port in tunnel_ports(profiles)}


def tunnel_argv(target: DeployTarget, profiles: list[str] | None = None) -> list[str]:
    """Build the full `ssh -N -L ...` argv for the access tunnel."""
    options = ["-N"]
    for _name, port in tunnel_ports(profiles):
        options += ["-L", f"{port}:127.0.0.1:{port}"]
    return ssh_argv(target, options=options)


def tunnel_state() -> dict[str, Any]:
    """Return the recorded background tunnel (or `{}`)."""
    return _read_json(TUNNEL_STATE_FILE)


def _process_alive(pid: int) -> bool:
    """True if `pid` names a live process this user can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours
        return True
    return True


def tunnel_status() -> dict[str, Any]:
    """Report whether a background tunnel is up, and what it forwards.

    Self-healing: a recorded pid that is no longer alive (reboot, `kill`)
    clears the state file rather than being reported as running forever.
    """
    recorded = tunnel_state()
    pid = int(recorded.get("pid") or 0)
    running = _process_alive(pid)
    if recorded and not running:
        TUNNEL_STATE_FILE.unlink(missing_ok=True)
    profiles = [str(p) for p in (recorded.get("profiles") or [])]
    return {
        "running": running,
        "pid": pid if running else 0,
        "host": str(recorded.get("host") or "") if running else "",
        "profiles": profiles if running else [],
        "urls": tunnel_urls(profiles) if running else {},
    }


def start_tunnel(
    target: DeployTarget, profiles: list[str] | None = None, *, background: bool = True
) -> dict[str, Any]:
    """Open the SSH tunnel to the instance.

    In background mode the child is detached into its own process group (so
    a Ctrl-C aimed at the CLI doesn't take the tunnel with it) and its pid
    recorded, which is what lets `--stop` and the dashboard find a tunnel
    another process started. In foreground mode this blocks until the
    operator interrupts it.
    """
    existing = tunnel_status()
    if existing["running"]:
        return {"action": "tunnel", "already_running": True, **existing}

    argv = tunnel_argv(target, profiles)
    if not background:
        subprocess.run(argv)
        return {
            "action": "tunnel",
            "running": False,
            "pid": 0,
            "host": target.host,
            "profiles": list(profiles or []),
            "urls": tunnel_urls(profiles),
        }

    # stderr goes to a log file rather than a pipe: this child is detached and
    # outlives the process that started it, so a pipe would either be left
    # unread (under the API server) or closed under the tunnel's feet when the
    # CLI exits. The file keeps the early-exit diagnostic readable either way.
    TUNNEL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TUNNEL_LOG_FILE, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
            text=True,
        )
    # ssh exits within a moment when the port bind or the auth fails; a short
    # settle avoids reporting "tunnel open" for a process that is already gone.
    time.sleep(1.0)
    if process.poll() is not None:
        try:
            detail = TUNNEL_LOG_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            detail = ""
        raise CloudCommandError(
            "Could not open the SSH tunnel"
            + (f": {detail}" if detail else ".")
            + "\nA local port may already be in use -- `nyxgpt ops status` shows whether a "
            "local stack is holding 8000/3000."
        )
    record = {
        "pid": process.pid,
        "host": target.host,
        "user": target.user,
        "profiles": list(profiles or []),
    }
    _write_json(TUNNEL_STATE_FILE, record)
    return {
        "action": "tunnel",
        "already_running": False,
        "running": True,
        "pid": process.pid,
        "host": target.host,
        "profiles": list(profiles or []),
        "urls": tunnel_urls(profiles),
    }


def stop_tunnel() -> dict[str, Any]:
    """Tear down the background tunnel, if one is running."""
    status = tunnel_status()
    if not status["running"]:
        return {"action": "tunnel-stop", "stopped": False, "reason": "no tunnel running"}
    pid = int(status["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:  # pragma: no cover - racing an external kill
        raise CloudCommandError(f"Could not stop the tunnel (pid {pid}): {exc}") from exc
    TUNNEL_STATE_FILE.unlink(missing_ok=True)
    return {"action": "tunnel-stop", "stopped": True, "pid": pid}


# --- Health ------------------------------------------------------------


def _probe(url: str, timeout: float) -> int:
    """Return the HTTP status for `url`, or 0 when it could not be reached."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - localhost
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def wait_for_health(timeout: float, *, port: int = 8000, interval: float = 5.0) -> dict[str, Any]:
    """Poll the tunneled API health endpoint until it answers 200.

    Deliberately probed through the tunnel rather than over a second SSH
    call: this proves the exact path the operator is about to be handed a
    URL for, which is what
    `product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md` means by
    "confirms health over the tunnel it opens for that purpose".
    """
    url = f"http://localhost:{port}{HEALTH_PATH}"
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    status = 0
    while time.monotonic() < deadline:
        status = _probe(url, timeout=10.0)
        if status == 200:
            return {
                "healthy": True,
                "url": url,
                "status": status,
                "waited": time.monotonic() - started,
            }
        time.sleep(interval)
    return {"healthy": False, "url": url, "status": status, "waited": time.monotonic() - started}


# --- Operations (shared by the CLI and the admin dashboard API) --------


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    """Provision AWS and deploy the full stack onto it. Idempotent.

    Returns a step-by-step record of what happened -- the same payload the
    dashboard renders and the CLI prints a summary of.
    """
    plan = resolve_plan(args)
    steps: list[dict[str, Any]] = []
    host_flag = str(getattr(args, "host", None) or "")
    # Before the substrate is applied, not after the stack is up (#3993): a
    # deploy that dies partway is the state `cloud status` most needs to be
    # able to describe, and until this existed it was the one state it could
    # not see at all. Everything below runs inside the try/except that closes
    # this record out, so no exit path -- exception or not -- leaves it
    # claiming to still be running.
    begin_deploy_attempt(plan, host=host_flag)
    try:
        return _deploy(args, plan, steps, host_flag)
    except BaseException as exc:
        finish_deploy_attempt(ATTEMPT_FAILED, error=str(exc) or exc.__class__.__name__)
        raise


def _deploy(
    args: argparse.Namespace,
    plan: DeployPlan,
    steps: list[dict[str, Any]],
    host_flag: str,
) -> dict[str, Any]:
    """The body of `deploy`, split out so the attempt record wraps every exit path (#3993)."""
    # Empty for a `--host` box: that machine is not ours, so there are no
    # applied settings to read an instance type out of (#3867 + #3956).
    applied_settings: dict[str, Any] = {}
    # Empty unless this run allocated (or reconciled) an EC2 Mac Dedicated
    # Host of its own (#3995).
    mac_allocation: dict[str, Any] = {}

    if plan.os_family == OS_FAMILY_MACOS:
        # Never `apply_infra` for a Mac: that reconciles (or creates) the
        # Linux single-box substrate, which is a different machine from the
        # Mac being provisioned -- it would bill for an instance nothing then
        # deploys to. The Mac's own network, security group, Dedicated Host
        # and instance live in `cloud_mac`'s isolated Terraform root instead.
        if host_flag:
            steps.append(
                {
                    "step": "infra",
                    "skipped": True,
                    "reason": (
                        "the EC2 Mac was supplied with --host; nyxGPT's substrate provisions "
                        "default-tenancy Linux instances and does not manage this machine"
                    ),
                    "outputs": {},
                }
            )
            steps.append(
                {
                    "step": "access",
                    "owner_ip_cidr": "",
                    "open_ports": [22],
                    "mechanism": "ssh-tunnel-to-loopback",
                    "managed": False,
                }
            )
            target = DeployTarget(
                host=host_flag,
                user=plan.ssh_user,
                identity_file=(
                    str(Path(plan.identity_file).expanduser()) if plan.identity_file else ""
                ),
                region=str(getattr(args, "region", None) or ""),
            )
        else:
            # #3995: no `--host` is now an offer, not a refusal. `allocate`
            # prices the host, requires consent, allocates it and launches the
            # Mac on it -- and records the host id and its release time before
            # anything else can fail, because a host whose id is lost is a
            # charge nothing can stop.
            allocation = cloud_mac.allocate(args, assume_yes=bool(getattr(args, "yes", False)))
            mac_allocation = allocation
            steps.append({"step": "mac-host", **allocation})
            steps.append(
                {
                    "step": "access",
                    "owner_ip_cidr": str(allocation.get("owner_ip_cidr") or ""),
                    "open_ports": [22],
                    "mechanism": "ssh-tunnel-to-loopback",
                    "managed": True,
                }
            )
            mac_host = str(allocation.get("public_ip") or "")
            if not mac_host:
                raise CloudCommandError(
                    "The Dedicated Host and Mac instance were created but AWS reported no "
                    f"public address for instance {allocation.get('instance_id') or 'unknown'}. "
                    "Nothing was lost -- `nyxgpt cloud status` shows the host, and re-running "
                    "`nyxgpt cloud deploy --os macos` reconciles it without allocating again."
                )
            target = DeployTarget(
                host=mac_host,
                user=plan.ssh_user,
                identity_file=(
                    str(Path(plan.identity_file).expanduser()) if plan.identity_file else ""
                ),
                region=str(allocation.get("region") or ""),
                instance_id=str(allocation.get("instance_id") or ""),
                security_group_id=str(allocation.get("security_group_id") or ""),
            )
    else:
        update_deploy_attempt("infra")
        infra = cloud_infra.apply_infra(args)
        steps.append({"step": "infra", "outputs": infra.get("outputs", {})})
        # `cloud_infra.apply_infra` reports this rather than raising: the
        # substrate is up and billing, but its outputs could not be read, so
        # `state.json` still describes an earlier one (#3993). Carried onto the
        # attempt record so a `cloud status` run afterwards repeats the warning
        # instead of quietly presenting stale ids as this deploy's.
        if not infra.get("state_refreshed", True):
            update_deploy_attempt(
                "infra",
                state_stale=True,
                error=(
                    "the substrate applied but its Terraform outputs were unreadable, so "
                    f"{cloud_infra.CLOUD_STATE_FILE} still describes an earlier substrate"
                ),
            )

        # The P6-4 access path is wired by the apply itself: `resolve_settings`
        # re-detects the operator's current public IP on every run, so the
        # security group's single port-22 rule already points at wherever this
        # deploy is running from. Recorded as its own step because "which CIDR
        # can reach this box" is the thing an operator most needs to see.
        applied_settings = infra.get("settings", {})
        steps.append(
            {
                "step": "access",
                "owner_ip_cidr": applied_settings.get("owner_ip_cidr", ""),
                "open_ports": [22],
                "mechanism": "ssh-tunnel-to-loopback",
            }
        )

        target = resolve_target(args)
        if plan.identity_file:
            target.identity_file = str(Path(plan.identity_file).expanduser())
        target.user = plan.ssh_user

    if plan.kubernetes:
        # Said before the ~20 minutes of provisioning, not after it (#3956).
        # In Kubernetes mode the instance carries the whole stack as Pods --
        # api and web on both tracks, Cassandra, Ollama and the observability
        # layer -- so it needs what a local cluster node needs, and the
        # default instance type was chosen for the native layout.
        #
        # `ops install --kubernetes`'s own capacity preflight is the authority
        # on whether a given node fits, and refuses *before* building anything
        # (#3825). So this is a pointer to the flag that fixes it, not a size
        # table this file would have to keep correct as instance families
        # change. Read from the applied settings rather than re-resolving
        # them: `resolve_settings` re-detects the operator's public IP over
        # the network, and calling it twice would pay for that twice and could
        # answer differently the second time. A `--host` box has none -- it is
        # not ours to resize -- so that case names no instance type and drops
        # the --instance-type pointer rather than offering a flag that would
        # do nothing (#3867).
        instance_type = str(applied_settings.get("instance_type") or "")
        print(
            "Kubernetes substrate: the whole stack runs as Pods on this one "
            f"{instance_type or 'machine'}. See the node-capacity section of "
            "docs/kubernetes.md for what it reserves"
            + (", and pass --instance-type to size up" if instance_type else "")
            + " -- the install refuses a node that cannot hold the stack before it "
            "builds anything.",
            file=sys.stderr,
        )

    # The target is only known once the substrate has been applied (or the
    # --host box named), so this is the first point the attempt record can say
    # *which machine* a failure from here on refers to (#3993).
    update_deploy_attempt(
        "ssh", host=target.host, instance_id=target.instance_id, region=target.region
    )
    waited = wait_for_ssh(target, plan.ssh_timeout)
    steps.append({"step": "ssh", "host": target.host, "waited_seconds": round(waited, 1)})

    # Before provisioning, not during: the provisioning script installs from
    # the tree, so the tree has to already be there -- and a transfer that
    # fails should fail with nothing installed rather than half-way through a
    # bootstrap. Only under `--dev`; the artifact path copies nothing.
    if plan.dev:
        update_deploy_attempt("ship")
        steps.append(ship_working_tree(target, Path(plan.source_dir)))

    update_deploy_attempt("provision")
    steps.append({"step": "provision", **provision_instance(target, plan)})

    record = {
        "version": plan.version,
        "profiles": list(plan.profiles),
        "ssh_user": plan.ssh_user,
        # Recorded so a re-run that needed a non-default key doesn't have to
        # pass --identity-file again, the same way --version and --ssh-user
        # carry over.
        "identity_file": plan.identity_file,
        # Same carry-forward reason (#3865): a re-deploy must not silently
        # move an instance's sessions back to files because the flag was
        # omitted -- `resolve_plan` reads this back.
        "session_backend": plan.session_backend,
        # #3956. Read back by `resolve_plan` so a bare re-deploy does not
        # install a native stack beside the cluster, and reported by
        # `deploy_status` so the dashboard can say which substrate is running
        # rather than assuming the native one.
        "kubernetes": plan.kubernetes,
        "substrate": plan.substrate,
        # Recorded but never carried forward by `resolve_plan` (see there):
        # this is how `cloud status` and the dashboard can say the instance is
        # running a working tree rather than the release `version` names,
        # which is otherwise indistinguishable from the outside.
        "dev": plan.dev,
        "source_dir": plan.source_dir,
        # Which bootstrap this deployment was built with (#3867). Carried
        # forward by `resolve_plan` (a re-deploy of a Mac stays a Mac deploy)
        # and reported by `cloud status` and the Infrastructure page, because
        # "which OS is that box running" is otherwise unanswerable from here.
        "os_family": plan.os_family,
        # #3995. Set only when nyxGPT allocated the Dedicated Host itself, so
        # `destroy` can tell "a Mac we created and must schedule the release
        # of" from "a Mac the operator pointed us at and we must not touch".
        # The authoritative record is `~/.nyxGPT/cloud/state.json` (which
        # outlives this file by design); this is the deploy-side breadcrumb.
        "mac_host_id": str(mac_allocation.get("host_id") or ""),
        "host": target.host,
        "instance_id": target.instance_id,
        "region": target.region,
    }
    _write_json(DEPLOY_STATE_FILE, record)

    health: dict[str, Any] = {"healthy": False, "skipped": True}
    tunnel: dict[str, Any] = {"running": False, "skipped": True}
    if plan.open_tunnel:
        update_deploy_attempt("tunnel")
        tunnel = start_tunnel(target, plan.profiles)
        steps.append({"step": "tunnel", "pid": tunnel.get("pid", 0)})
        update_deploy_attempt("health")
        health = wait_for_health(plan.health_timeout)
        steps.append({"step": "health", **health})
        if not health["healthy"]:
            # Recorded before raising: a deploy that installed the stack but
            # never went healthy is exactly the event the dashboard's history
            # exists to show, and it is the one an exception would otherwise
            # leave no trace of.
            record_history(
                "deploy",
                "failed",
                version=plan.version,
                dev=plan.dev,
                host=target.host,
                instance_id=target.instance_id,
                region=target.region,
                profiles=list(plan.profiles),
                substrate=plan.substrate,
                detail=(
                    f"stack installed but {health['url']} never returned 200 within "
                    f"{plan.health_timeout:.0f}s"
                ),
            )
            # In Kubernetes mode the loopback address the tunnel forwards to
            # is held by the access bridge, not by the API process, so the
            # bridge is a distinct thing that can be down while every Pod is
            # Running -- and an operator told only "the API did not answer"
            # would go looking in the wrong place (#3956).
            bridge_hint = (
                "\nThis is a Kubernetes deployment: `127.0.0.1:8000` on the instance is held "
                "by the access bridge, not by the API process, so `nyxgpt cloud canary status` "
                "may well report healthy Pods while this probe fails. "
                "`nyxgpt cloud ops doctor` reports the bridge units by name alongside the "
                "cluster's own state."
                if plan.kubernetes
                else ""
            )
            raise CloudCommandError(
                f"The stack was installed but {health['url']} never returned 200 within "
                f"{plan.health_timeout:.0f}s (last status: {health['status'] or 'unreachable'}).\n"
                "The tunnel is still open -- `nyxgpt cloud status` and "
                "`nyxgpt cloud ops doctor` (which runs the instance's own doctor over the "
                f"same SSH path) will say more.{bridge_hint}"
            )

    record_history(
        "deploy",
        "succeeded",
        version=plan.version,
        # So the dashboard's history can tell a tree deploy from a release
        # deploy after the fact, which the version alone cannot (#3950).
        dev=plan.dev,
        host=target.host,
        instance_id=target.instance_id,
        region=target.region,
        profiles=list(plan.profiles),
        substrate=plan.substrate,
        detail=(
            "healthy over the access tunnel"
            if health.get("healthy")
            else "installed; health not checked (no tunnel opened)"
        ),
    )
    finish_deploy_attempt(ATTEMPT_SUCCEEDED, phase="done")

    return {
        "action": "deploy",
        "plan": plan.to_dict(),
        "target": target.to_dict(),
        "steps": steps,
        "tunnel": tunnel,
        "health": health,
        "urls": tunnel_urls(plan.profiles),
    }


def destroy(args: argparse.Namespace) -> dict[str, Any]:
    """Tear the whole deployment down: tunnel first, then the substrate.

    A `--kubernetes` deployment needs no extra teardown step, and adding one
    would be worse than useless (#3956). The k3s cluster is *on* the
    instance: its control plane, its containerd image store, its
    `local-path` PersistentVolumes and every Pod live on the root volume
    Terraform is about to delete. Running `ops down --kubernetes` first would
    spend minutes gracefully draining workloads off a machine that is being
    terminated seconds later, and would add a way for the teardown to hang or
    fail on a cluster that is already unreachable. The deploy record --
    including the `kubernetes` substrate marker `resolve_plan` reads back --
    is deleted with it, so the next deploy starts from a clean statement of
    what is deployed rather than from a stale one.
    """
    # Read before the teardown deletes it -- the history entry names what was
    # torn down, which is unrecoverable once `deploy.json` is gone.
    previous = load_deploy_state()
    tunnel = stop_tunnel()

    # #3995: the EC2 Mac comes down first and on its own terms. `cloud_mac.
    # teardown` terminates the instance immediately and schedules the host
    # release for when AWS will accept it, and it never raises -- a host that
    # cannot be scheduled must not stop the substrate, the tunnel and the
    # deploy record from coming down, because the operator's next question is
    # "what is still running?" and the answer has to be short.
    mac: dict[str, Any] = {"managed": False}
    if cloud_mac.load_mac_record() or cloud_mac.mac_state_exists():
        mac = cloud_mac.teardown(args)

    # A Mac-only deployment never applied the substrate (see `deploy`), so
    # there is no Terraform state for `destroy_infra` to work from and its
    # "nothing to destroy" error would be the whole teardown's exit code --
    # after the Mac had already been torn down successfully.
    if mac.get("managed") and not cloud_infra.TFSTATE_FILE.exists():
        DEPLOY_STATE_FILE.unlink(missing_ok=True)
        record_history(
            "destroy",
            "succeeded",
            version=str(previous.get("version") or ""),
            host=str(previous.get("host") or ""),
            instance_id=str(previous.get("instance_id") or ""),
            region=str(previous.get("region") or mac.get("region") or ""),
            detail=_mac_teardown_detail(mac),
        )
        return {
            "action": "destroy",
            "tunnel": tunnel,
            "settings": {},
            "unmanaged_target": "",
            "mac": mac,
        }

    try:
        result = cloud_infra.destroy_infra(args)
    except Exception as exc:
        # Symmetric with the failed-deploy path above: a teardown that was
        # attempted and did not finish is exactly the event an operator comes
        # to the history panel to reconstruct, and a half-destroyed substrate
        # is the state most worth leaving a trace of.
        record_history(
            "destroy",
            "failed",
            version=str(previous.get("version") or ""),
            host=str(previous.get("host") or ""),
            instance_id=str(previous.get("instance_id") or ""),
            region=str(previous.get("region") or ""),
            detail=f"tunnel closed, but the substrate teardown failed: {exc}",
        )
        raise
    DEPLOY_STATE_FILE.unlink(missing_ok=True)
    # And the attempt record with it (#3993): once the substrate is gone,
    # "a deploy stopped at `provision`" describes an instance that no longer
    # exists, and `cloud status` would report NOT COMPLETED for a machine that
    # was deliberately destroyed. The history keeps the event.
    DEPLOY_ATTEMPT_FILE.unlink(missing_ok=True)
    settings = result.get("settings", {})
    # An EC2 Mac the *operator* supplied with `--host` is not part of the
    # substrate this just tore down -- nyxGPT never created it and cannot
    # terminate it (#3867). Say so rather than let "Cloud deployment
    # destroyed" imply a Mac that is still running, and still billing, was
    # included. A Mac nyxGPT allocated itself (#3995) is the opposite case and
    # is reported by `mac` above.
    macos_target = str(previous.get("os_family") or "") == OS_FAMILY_MACOS and not mac.get(
        "managed"
    )
    record_history(
        "destroy",
        "succeeded",
        version=str(previous.get("version") or ""),
        host=str(previous.get("host") or ""),
        instance_id=str(previous.get("instance_id") or ""),
        region=str(previous.get("region") or settings.get("aws_region") or ""),
        detail=(
            "tunnel closed, substrate torn down; the EC2 Mac target was left running "
            "(nyxGPT does not manage it)"
            if macos_target
            else (
                f"tunnel closed, substrate torn down; {_mac_teardown_detail(mac)}"
                if mac.get("managed")
                else "tunnel closed, substrate torn down"
            )
        ),
    )
    return {
        "action": "destroy",
        "tunnel": tunnel,
        "settings": settings,
        "unmanaged_target": previous.get("host", "") if macos_target else "",
        "mac": mac,
    }


def _mac_teardown_detail(mac: dict[str, Any]) -> str:
    """One sentence describing what happened to the Mac and its Dedicated Host."""
    host_id = str(mac.get("host_id") or "unknown")
    release_at = str(mac.get("release_at") or "unknown")
    terminated = (
        "Mac instance terminated"
        if mac.get("instance_terminated")
        else ("the Mac instance did NOT come down")
    )
    if mac.get("release_scheduled"):
        return (
            f"{terminated}; Dedicated Host {host_id} stays allocated until {release_at} "
            "(AWS's 24-hour minimum) and its release is scheduled"
        )
    return (
        f"{terminated}; Dedicated Host {host_id} is STILL ALLOCATED and its release could "
        f"not be scheduled: {'; '.join(str(e) for e in mac.get('errors') or []) or 'no reason recorded'}"
    )


# The wrapped commands that own each cloud lifecycle action, returned with
# every status so the dashboard renders exactly what the CLI documents rather
# than its own hand-typed copy. The owner's 2026-08-09 decision on #3514 makes
# the cloud page status-plus-pointers -- these are the pointers, and per
# CLAUDE.md's wrapper requirement every one of them is a `nyxgpt` command.
LIFECYCLE_COMMANDS: dict[str, str] = {
    "deploy": "nyxgpt cloud deploy",
    "redeploy": "nyxgpt cloud deploy",
    "destroy": "nyxgpt cloud destroy --yes",
    # The cloud end-to-end test (P6-17, #3515). Listed here so the dashboard
    # offers it as a pointer like every other lifecycle action -- it deploys
    # and destroys real billed infrastructure, which is exactly why it is a
    # deliberate terminal command and not a button.
    "smoke": "nyxgpt cloud smoke",
    "tunnel": "nyxgpt cloud tunnel",
    "tunnel_stop": "nyxgpt cloud tunnel --stop",
    "status": "nyxgpt cloud status",
    "ops_status": "nyxgpt cloud ops status",
    "doctor": "nyxgpt cloud ops doctor",
    "self_heal": "nyxgpt cloud ops self-heal",
    "credentials": "nyxgpt cloud credentials",
    "allow_ip": "nyxgpt cloud allow-ip",
    # #3956. Only meaningful on a `--kubernetes` deployment, and named
    # unconditionally anyway: the dashboard reports the substrate beside it,
    # and a pointer an operator can read is how they find out the capability
    # exists at all.
    "deploy_kubernetes": "nyxgpt cloud deploy --kubernetes",
    "canary": "nyxgpt cloud canary status",
}


# The read-only inspections `nyxgpt cloud ops <name>` runs *on* the instance
# over the same wrapped SSH path `nyxgpt cloud credentials` uses (#3813), so
# checking container state never needs a hand-rolled `ssh` followed by a raw
# `docker compose ps` (CLAUDE.md's wrapper requirement). Deliberately an
# allowlist of reads: changing what runs on the instance is `nyxgpt cloud
# deploy`, which is idempotent and records what it did.
REMOTE_OPS_COMMANDS: dict[str, str] = {
    "status": "ops status",
    "doctor": "ops doctor",
    "self-heal": "self-heal status",
    # #3865. A read by construction: `ops session-backend` with no backend
    # argument reports the value in force and writes nothing. It answers the
    # question the deploy record cannot for an instance deployed before the
    # flag existed -- what the machine is *actually* running, rather than
    # what was last requested of it.
    "session-backend": "ops session-backend",
}


# `nyxgpt cloud canary <subcommand>` -- the capability #3506 was choosing a
# substrate FOR, reachable on the cloud target (#3956).
#
# It runs the instance's own `nyxgpt canary`, over the same wrapped SSH path
# `nyxgpt cloud ops` uses, for the reason that path exists at all: the
# cluster's API server binds the instance's private address and is reachable
# from nothing but that box, so the alternative would be tunnelling a
# cluster-admin kubeconfig back to the workstation and pointing the
# operator's own kubectl context at their cloud deployment -- more exposure,
# and a context that then collides with any local cluster they have.
#
# Unlike `REMOTE_OPS_COMMANDS` this is deliberately NOT read-only, and the
# distinction is worth stating because that allowlist's docstring says the
# opposite about itself. `nyxgpt cloud ops` is an *inspection* surface, and
# changing what runs on the instance is `nyxgpt cloud deploy`. Traffic
# weighting is neither: `start`/`promote`/`rollback` move traffic between two
# Deployments that a deploy already created, inside the cluster, without
# touching the substrate -- the same class of action as `nyxgpt cloud deploy`
# itself and explicitly not the class D-017 keeps in the CLI's hands for
# self-hosting reasons, since a CLI on the operator's workstation is where
# this runs.
#
# `deploy` is absent, and its absence is the point: `nyxgpt canary deploy`
# builds a versioned image from the current checkout, and the instance has no
# checkout by construction (CLAUDE.md's repo-less requirement). Rolling a new
# release out to the cloud is `nyxgpt cloud deploy --version <release>`.
REMOTE_CANARY_COMMANDS: tuple[str, ...] = (
    "status",
    "start",
    "evaluate",
    "promote",
    "rollback",
)

# Optional pass-through flags per canary subcommand. An allowlist rather than
# `*argv`: everything here is spliced into a remote shell command, and the
# argument values are validated by type before they get there.
CANARY_FLAGS: dict[str, tuple[str, ...]] = {
    "status": ("component",),
    "start": ("component", "weight"),
    "evaluate": ("component",),
    "promote": ("component", "step", "force"),
    "rollback": ("component",),
}


# Where a deployment answer came from -- the same three-way distinction
# `cloud_infra.infra_status` makes about the substrate, for the same reason
# (#3804): the deploy record lives on the workstation that ran the deploy, so
# a dashboard served from the instance has to answer from what it *is*.
SOURCE_DEPLOY_RECORD = "deploy-record"
SOURCE_LOCAL_INSTANCE = "local-instance"
# No deploy has ever completed here, but this machine started one and it did
# not finish (#3993). A real, first-hand source -- this workstation wrote the
# record -- and a state distinct from both "deployed" and "unknown": something
# was provisioned, it is probably billing, and it is not a working deployment.
SOURCE_DEPLOY_ATTEMPT = "deploy-attempt"
# No deploy record and no attempt, but this machine's substrate handoff
# (`state.json`, written by `cloud infra apply`) names a provisioned instance
# (#3993). Weaker than a deploy record -- it says a box exists, not that nyxGPT
# was ever installed on it -- but categorically stronger than UNKNOWN, which
# was what an operator got while a live instance's id sat on their own disk.
SOURCE_SUBSTRATE_RECORD = "substrate-record"
SOURCE_UNKNOWN = "none"


def recorded_target() -> DeployTarget | None:
    """Rebuild the `DeployTarget` of the last deploy, or `None` if none is recorded.

    `resolve_target` answers the same question from command-line flags plus
    the substrate handoff, for commands that are about to *act* on the
    instance. This one answers it from `deploy.json` alone -- the SSH user
    and identity file a deploy actually used -- which is what a status report
    has to show: the connection target as it is, not as a fresh set of flags
    would resolve it.
    """
    record = load_deploy_state()
    host = str(record.get("host") or "")
    if not host:
        return None
    return DeployTarget(
        host=host,
        user=str(record.get("ssh_user") or DEFAULT_SSH_USER),
        identity_file=str(record.get("identity_file") or ""),
        region=str(record.get("region") or ""),
        instance_id=str(record.get("instance_id") or ""),
    )


def connection_status(on_instance: bool = False) -> dict[str, Any]:
    """Report how this workstation reaches the deployment (#3813).

    The `host` a deployment reports is only half an address: reaching it also
    takes the login user and, when the key is not one of ssh's own defaults,
    the identity file. All three are recorded by `nyxgpt cloud deploy` and,
    until this existed, none of them was ever printed -- an operator who had
    lost the deploy's scrollback had no wrapped command that would say where
    their own instance was.

    `tunnel_invocation` is the raw `ssh` the wrapped tunnel executes. It is
    reported as *diagnostics* -- what is running, for a support conversation
    -- and never as the instruction: `nyxgpt cloud tunnel` is what an
    operator runs (CLAUDE.md's wrapper requirement).
    """
    target = recorded_target()
    if target is None:
        return {
            "known": False,
            "host": "",
            "user": "",
            "identity_file": "",
            "target": "",
            "tunnel_invocation": "",
            "command": LIFECYCLE_COMMANDS["tunnel"],
            "reason": (
                "this dashboard is served by the instance itself -- the SSH user and "
                "identity file are the operator workstation's, and are recorded there"
                if on_instance
                else "no deploy has been recorded on this machine, so there is no "
                "connection target to report"
            ),
        }
    profiles = [str(p) for p in (load_deploy_state().get("profiles") or [])]
    return {
        "known": True,
        "host": target.host,
        "user": target.user,
        # Empty means "ssh's own ~/.ssh defaults and agent" -- a real answer,
        # not a missing one, so it is reported as such rather than blanked.
        "identity_file": target.identity_file,
        "target": f"{target.user}@{target.host}",
        "tunnel_invocation": tunnel_invocation(target, profiles),
        "command": LIFECYCLE_COMMANDS["tunnel"],
        "reason": "",
    }


def deploy_status(probe_health: bool = False) -> dict[str, Any]:
    """Report the deployment's state without touching AWS or the instance.

    Cheap enough for the dashboard to poll, and still answers on a machine
    whose AWS credentials have expired.

    Like the substrate status it wraps, this answers from whichever source
    can see the deployment from here:

    * the deploy record `nyxgpt cloud deploy` wrote, on the workstation that
      ran it (`source: deploy-record`);
    * this process itself, when running *on* the instance -- the stack
      serving the request is the deployment, so its version and address are
      known first-hand even though no deploy record exists there
      (`source: local-instance`);
    * the record of a deploy this machine *started* and did not finish
      (`source: deploy-attempt`, `deployed: False`) -- a provision that dies
      partway is the state an operator most needs described, and it was the
      one state nothing could see (#3993);
    * the substrate handoff alone (`source: substrate-record`,
      `deployed: False`) -- `state.json` names a provisioned instance, so a
      box exists and is billing, but nothing here says nyxGPT was installed
      on it;
    * none of those, on a machine that is neither the operator's nor the
      instance (`source: none`, `known: False` -- the caller must say
      *unknown*).

    The last three keep D-018's rule intact: each names a source that actually
    exists on *this* machine, and *unknown* is still what a machine with no
    source reports. What changed is that "a live instance's id is sitting in
    my own state file" stopped counting as no source at all.

    `probe_health=True` additionally makes one short request to the tunneled
    API health endpoint. Opt-in rather than always-on so the polled default
    stays free of network calls; the dashboard asks for it on an explicit
    load or refresh, where waiting a moment for a real answer is the point.
    A probe with no tunnel open would only ever time out, so it is skipped --
    as is a probe from the instance, where the tunnel is not the access path
    and the answering process is the one being asked about.
    """
    record = load_deploy_state()
    attempt = load_deploy_attempt()
    infra = cloud_infra.infra_status()
    on_instance = bool(infra.get("on_ec2"))
    profiles = [str(p) for p in (record.get("profiles") or [])]
    tunnel = tunnel_status()

    # An attempt only *answers* the question when no deploy ever completed
    # here; a finished deployment plus a later failed attempt is still a
    # deployment, and the attempt is reported alongside it rather than
    # replacing it.
    unfinished_attempt = bool(attempt) and attempt.get("status") != ATTEMPT_SUCCEEDED

    if record.get("host"):
        source = SOURCE_DEPLOY_RECORD
        deployed = True
        version = str(record.get("version") or "")
        host = str(record.get("host") or "")
    elif on_instance:
        source = SOURCE_LOCAL_INSTANCE
        deployed = True
        # First-hand, not recorded: this is the release answering the request.
        version = installed_version()
        host = str(infra.get("public_ip") or "")
    elif unfinished_attempt:
        # #3993. `deployed` stays False -- nothing here saw a working stack --
        # but the answer is no longer "unknown": this machine started a deploy
        # and can say what it was aiming at and how far it got.
        source = SOURCE_DEPLOY_ATTEMPT
        deployed = False
        version = str(attempt.get("version") or "")
        host = str(attempt.get("host") or infra.get("public_ip") or "")
    elif infra.get("provisioned"):
        # #3993. No deploy and no attempt, but the substrate handoff on this
        # disk names an instance. Reporting UNKNOWN here -- as this did while a
        # live, billing instance's id sat in `state.json` on the same machine
        # -- is the blindness this branch exists to remove.
        source = SOURCE_SUBSTRATE_RECORD
        deployed = False
        version = ""
        host = str(infra.get("public_ip") or "")
    else:
        source = SOURCE_UNKNOWN
        deployed = False
        version = ""
        host = ""

    health: dict[str, Any] = {"checked": False, "healthy": False, "status": 0, "reason": ""}
    if probe_health and source == SOURCE_LOCAL_INSTANCE:
        health["reason"] = (
            "this dashboard is served from the instance -- the stack answering this request "
            "is the deployment, so there is nothing to probe through a tunnel"
        )
    elif probe_health and not tunnel["running"]:
        health["reason"] = "no access tunnel is open, so the instance is not reachable from here"
    elif probe_health:
        url = f"http://localhost:8000{HEALTH_PATH}"
        status = _probe(url, timeout=5.0)
        health = {
            "checked": True,
            "healthy": status == 200,
            "status": status,
            "url": url,
            "reason": "" if status == 200 else "the tunneled API did not answer with 200",
        }

    return {
        "source": source,
        "known": source != SOURCE_UNKNOWN,
        "on_instance": on_instance,
        "deployed": deployed,
        # The last deploy this machine started, whatever became of it (#3993).
        # Always present -- `{}` means none has ever been started here -- and
        # reported even alongside a completed deployment, because "the deploy
        # you ran an hour ago failed at `provision`" is the answer an operator
        # is looking for and no other field carries it.
        "attempt": dict(attempt),
        "version": version,
        "host": host,
        "instance_id": str(
            record.get("instance_id")
            or attempt.get("instance_id")
            or infra.get("instance_id")
            or ""
        ),
        "instance_type": str(infra.get("instance_type") or ""),
        "region": str(record.get("region") or attempt.get("region") or infra.get("region") or ""),
        "profiles": profiles,
        # Where this deployment's chat sessions live (#3865). Observable
        # rather than operable, per the Definition of Done: the dashboard
        # reports it and names the `nyxgpt cloud deploy --session-backend`
        # command that changes it, and never drives the change itself.
        # Empty when nothing was recorded -- a deploy from before #3865, or
        # no deploy at all -- which is not the same claim as "file".
        "session_backend": str(record.get("session_backend") or ""),
        # What runs the stack on the instance (#3956). Observable, never
        # operable, per the Definition of Done: the dashboard reports it and
        # names `nyxgpt cloud deploy --kubernetes`, and never drives the
        # change itself -- switching substrates is exactly the class of
        # action D-017 keeps out of a UI the substrate is serving.
        #
        # Empty rather than "native" when nothing was recorded: a deploy from
        # before this flag existed, or no deploy at all, is not the same
        # claim as "this box runs the native stack".
        "substrate": str(record.get("substrate") or ""),
        # Whether the instance is running a shipped working tree rather than
        # the published release `version` names (#3950). Reported because it
        # is otherwise invisible: a dev deploy and an artifact deploy of the
        # same version look identical in every other field, and an operator
        # debugging one needs to know which they are looking at. Only the
        # deploy record can answer -- an instance asked about itself
        # (`local-instance`) reads its own package metadata, which says the
        # version and not where it came from -- so `source` says how much
        # weight this carries.
        "dev": bool(record.get("dev")),
        "source_dir": str(record.get("source_dir") or ""),
        # Which target OS's bootstrap this deployment was built with (#3867).
        # Observable rather than operable, like the session backend above: the
        # dashboard reports it and names `nyxgpt cloud deploy --os`, and never
        # drives a re-provision itself. Empty when nothing was recorded -- a
        # deploy from before #3867, or no deploy at all -- which is not the
        # same claim as "linux".
        "os_family": str(record.get("os_family") or ""),
        # #3995. The EC2 Mac Dedicated Host outlives both the instance and the
        # deploy record by construction: `destroy` terminates the Mac at once
        # but AWS refuses to release the host for 24 hours, so between those
        # two moments the only thing that still costs money is the one thing
        # every other field here has stopped describing. Empty dict when
        # nothing is outstanding. Read from `~/.nyxGPT/cloud/state.json`, so
        # it still answers after `deploy.json` is gone -- and, like everything
        # else on this surface, with no AWS call.
        "mac_host": cloud_mac.pending_release(),
        "connection": connection_status(on_instance),
        "infra": infra,
        "tunnel": tunnel,
        "health": health,
        "history": deploy_history(),
        "urls": tunnel_urls(profiles),
        "access_command": "nyxgpt cloud tunnel",
        "commands": dict(LIFECYCLE_COMMANDS),
    }


# --- CLI entry points --------------------------------------------------


def _print_urls(urls: dict[str, str]) -> None:
    """Print the localhost URLs the tunnel makes reachable."""
    for name, url in urls.items():
        print(f"  {name:<11} {url}")


def _print_deploy_summary(result: dict[str, Any]) -> None:
    """Print the operator-facing result of a deploy."""
    target = result["target"]
    plan = result["plan"]
    print(f"\nnyxGPT {plan['version']} deployed to {target['instance_id'] or target['host']}.")
    if plan.get("dev"):
        # Said plainly and every time: the version above is the tree's own
        # declared version, which on a working tree usually names a release
        # that does not exist yet. An operator reading only that line would
        # conclude they had deployed a published build.
        print(
            f"Built from your working tree at {plan.get('source_dir')} (--dev), not from a "
            f"published {plan['version']} release. Re-run `nyxgpt cloud deploy --dev` to ship "
            "your latest edits; a plain `nyxgpt cloud deploy` puts the published release back."
        )
    if str(plan.get("os_family") or OS_FAMILY_LINUX) == OS_FAMILY_MACOS:
        # Say plainly what a Mac deploy did and did not do, rather than
        # letting the Linux wording below imply parity it does not have
        # (#3867): its bootstrap installs the two Homebrew formulas and starts
        # them, and runs neither the observability profiles nor the self-heal
        # watchdog that `ops install` brings with it on Linux.
        print(
            "Target OS: macOS (EC2 Mac) -- installed from the remote Homebrew tap and "
            "started with `brew services`.\n"
            "No observability stack and no self-heal watchdog: that bootstrap does not run "
            "`nyxgpt ops install`. See docs/cloud.md, 'EC2 Mac targets'."
        )
        # The single most expensive thing about this deploy, said at the end
        # where the operator is actually looking (#3995). A Dedicated Host is
        # not something to discover on next month's bill.
        mac_step: dict[str, Any] = next(
            (step for step in result.get("steps", []) if step.get("step") == "mac-host"), {}
        )
        if mac_step.get("host_id"):
            print(
                f"Dedicated Host {mac_step['host_id']} is allocated and billing. AWS will not "
                f"release it before {mac_step.get('release_at') or 'its 24-hour minimum closes'}"
                " -- `nyxgpt cloud destroy --yes` terminates the Mac immediately and schedules "
                "the host release for then. `nyxgpt cloud status` shows it until it is gone."
            )
    elif plan.get("kubernetes"):
        # elif, not a second if: `resolve_plan` refuses macOS + --kubernetes,
        # so these two are mutually exclusive by construction (#3956).
        print(
            "Substrate: a single-node k3s cluster on the instance, running the same "
            "k8s/*.yaml manifests as a local Kubernetes install.\n"
            "Canary rollout is available here: `nyxgpt cloud canary status` (and start / "
            "evaluate / promote / rollback) run the instance's own `nyxgpt canary`."
        )
    backend = str(plan.get("session_backend") or DEFAULT_SESSION_BACKEND)
    if plan.get("kubernetes"):
        # Always cassandra here, and said as the cluster's own statement
        # rather than as a choice this deploy made: `k8s/configmap.yaml` is
        # what the Pods read, and `resolve_plan` refuses any other value.
        print(
            "Chat sessions: the in-cluster Cassandra (`nyxgpt.chat_sessions`), as "
            "k8s/configmap.yaml asserts -- every api replica shares one session list."
        )
    elif backend == "cassandra":
        print(
            "Chat sessions: the instance's Cassandra (`nyxgpt.chat_sessions`) -- shared with "
            "every mode pointed at the same Cassandra."
        )
    else:
        print(
            "Chat sessions: JSON files on the instance's own disk -- not shared with any "
            "other deployment mode, and lost with the instance. Switch with "
            "`nyxgpt cloud deploy --session-backend cassandra`."
        )
    if result["tunnel"].get("running"):
        print("\nThe access tunnel is open. Reachable now:")
        _print_urls(result["urls"])
        print("\nClose it with `nyxgpt cloud tunnel --stop`; reopen it with `nyxgpt cloud tunnel`.")
    else:
        print("\nNothing is reachable until you open the access tunnel:\n")
        print("  nyxgpt cloud tunnel\n")
        print("which will make these available:")
        _print_urls(result["urls"])
    print(
        "\nNo application port is open in the security group -- the instance's services bind "
        "127.0.0.1 and are reached only through that tunnel "
        "(product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md).\n"
        "If your public IP changes, run `nyxgpt cloud allow-ip`."
    )
    # Everything above scrolls away. This is the command that says it all
    # again -- the address, the SSH target, the tunnel state -- so an
    # operator never has to reconstruct it from a terminal's scrollback
    # (#3813).
    print(
        f"\nAsk for all of this again at any time with `{LIFECYCLE_COMMANDS['status']}`, and "
        f"check the instance's own containers with `{LIFECYCLE_COMMANDS['ops_status']}`."
    )


def _print_row(label: str, value: str) -> None:
    """Print one aligned `label  value` line of the status summary."""
    print(f"  {label:<16}{value}")


def _health_label(health: dict[str, Any]) -> str:
    """Describe the health probe in one line, including why it was skipped."""
    if not health.get("checked"):
        return f"not checked -- {health['reason']}" if health.get("reason") else "not checked"
    if health.get("healthy"):
        return "healthy (the tunneled API answered 200)"
    status = health.get("status") or 0
    return f"unhealthy (the tunneled API answered {status or 'nothing'})"


def _attempt_label(attempt: dict[str, Any]) -> str:
    """Describe a deploy attempt in one line: what it was, how far it got, and why it stopped."""
    if not attempt:
        return ""
    phase = str(attempt.get("phase") or "unknown")
    version = str(attempt.get("version") or "an unrecorded version")
    status = str(attempt.get("status") or ATTEMPT_RUNNING)
    if status == ATTEMPT_SUCCEEDED:
        return f"{version} completed"
    verb = "still running" if status == ATTEMPT_RUNNING else "stopped"
    error = str(attempt.get("error") or "")
    detail = f" -- {error}" if error else ""
    return f"{version}, {verb} at the `{phase}` phase{detail}"


def _print_incomplete_summary(status: dict[str, Any], commands: dict[str, str]) -> None:
    """Print the two verdicts between DEPLOYED and UNKNOWN (#3993).

    Kept apart from the DEPLOYED report rather than folded into it with
    conditionals: almost every row there ("Version", "Build source", "Chat
    sessions") describes a stack that is installed, and printing them for a
    deploy that never finished would assert exactly the things this state
    exists to say are not known. What an operator needs here is narrower --
    what exists, what it cost them, and which command moves it forward.
    """
    infra = status.get("infra") or {}
    attempt = status.get("attempt") or {}
    substrate_only = status["source"] == SOURCE_SUBSTRATE_RECORD

    if substrate_only:
        print("nyxGPT cloud deployment: SUBSTRATE ONLY -- provisioned, but nothing deployed.\n")
        print(
            "  (from the substrate handoff on this machine, "
            f"{cloud_infra.CLOUD_STATE_FILE} -- it names an instance, and no deploy has been "
            "recorded against it)\n"
        )
    else:
        print(
            "nyxGPT cloud deployment: NOT COMPLETED -- a deploy started here and did not "
            "finish.\n"
        )
        print(f"  (from the deploy attempt this machine recorded, {DEPLOY_ATTEMPT_FILE})\n")
        _print_row("Attempt", _attempt_label(attempt))

    _print_row("Instance", status.get("instance_id") or "not recorded")
    _print_row("Instance type", infra.get("instance_type") or "not recorded")
    _print_row("Public IP", status.get("host") or "not recorded")
    _print_row("Region", status.get("region") or "not recorded")
    _print_row("Security group", infra.get("security_group_id") or "not recorded")

    # D-018 -- never imply an answer nothing checked. This sentence used to
    # print unconditionally, and two ordinary flows reach here with nothing
    # provisioned: (a) a deploy that failed at `start`/`infra` before Terraform
    # created anything (no terraform binary, no AWS credentials, a failed
    # `terraform init`), which records FAILED with no instance_id and no
    # substrate record; (b) an operator who declined the EC2 Mac allocation,
    # whose own recorded error reads "nothing was allocated and nothing is
    # billed" -- sat inside a frame asserting the opposite. Asserting billing
    # over either is the same class of lie this verdict was written to end.
    provisioned = bool(
        substrate_only
        or status.get("instance_id")
        or status.get("host")
        or infra.get("instance_type")
        or infra.get("security_group_id")
    )
    if provisioned:
        print(
            "\nThis is NOT the same as nothing being deployed, and it is not the same as "
            "unknown: an instance exists and is being billed."
        )
    else:
        print(
            "\nNothing is recorded as provisioned by this attempt: it failed at or before "
            "the substrate step, so no instance was created here and nothing from it is "
            "being billed."
        )
    if attempt.get("state_stale"):
        # The one case where the ids above must not be trusted -- say so here
        # rather than printing them as though they described this attempt.
        print(
            "\nWARNING: the substrate applied but its Terraform outputs could not be read, so "
            f"the ids above come from an EARLIER substrate. Re-run `{commands['deploy']}` (or "
            "`nyxgpt cloud infra apply`) to refresh them before acting on them."
        )
    print("\nWhat to do next:")
    print(
        f"  {commands['deploy']}  -- re-run it; the deploy is idempotent and reconciles "
        "from here"
    )
    print(f"  {commands['allow_ip']}  -- refresh SSH access if your public IP has changed")
    if provisioned:
        # Only offered when something is recorded. Against a pre-substrate
        # failure `cloud destroy` raises "nothing to destroy", so prescribing
        # it there sends the operator to a dead end to disprove a claim this
        # function should not have made.
        print(f"  {commands['destroy']}  -- tear the instance down if you do not want it")


def _print_pending_mac_host(mac_host: dict[str, Any]) -> None:
    """Print the EC2 Mac Dedicated Host block, when one is outstanding (#3995).

    Nothing at all when there is no host -- a permanent "no Dedicated Host"
    row on every Linux deployment would be noise. When there *is* one this is
    deliberately loud: it is the only resource nyxGPT creates that keeps
    costing money after everything else is gone, and the whole reason the
    observability rule applies to it.
    """
    if not mac_host or not mac_host.get("host_id"):
        return
    print("\nEC2 Mac Dedicated Host (still billing)")
    _print_row("Host", f"{mac_host['host_id']} ({mac_host.get('instance_type') or 'unknown type'})")
    _print_row(
        "Location",
        f"{mac_host.get('region') or 'unknown'} / {mac_host.get('availability_zone') or 'unknown'}",
    )
    _print_row("Allocated", mac_host.get("allocated_at") or "unknown")
    if mac_host.get("releasable_now"):
        release_note = f"{mac_host.get('release_at') or 'unknown'} -- that moment has passed"
    else:
        release_note = f"{mac_host.get('release_at') or 'unknown'} (AWS's 24-hour minimum)"
    _print_row("Releasable at", release_note)
    if mac_host.get("release_scheduled") and mac_host.get("releasable_now"):
        # Deliberately not "released": nothing on this machine watched the
        # schedule fire, so claiming the charge has stopped would be an
        # assertion nothing checked. Slack has the outcome; the next
        # lifecycle command asks AWS and clears this row if the host is gone.
        _print_row(
            "Release",
            "the scheduled release has fired -- Slack has the outcome. This row clears on the "
            "next `nyxgpt cloud deploy --os macos` or `nyxgpt cloud destroy --yes`, which asks "
            "AWS whether the host is really gone",
        )
    elif mac_host.get("release_scheduled"):
        _print_row(
            "Release",
            "scheduled -- a one-shot AWS schedule releases it and reports the outcome to Slack",
        )
    else:
        _print_row(
            "Release",
            "NOT scheduled yet -- `nyxgpt cloud destroy --yes` terminates the Mac and "
            "schedules it",
        )
    accrued = mac_host.get("accrued_cost")
    rate = mac_host.get("hourly_rate")
    if accrued is not None and rate:
        _print_row(
            "Accrued",
            f"${float(accrued):.2f} at ${float(rate):.4f}/hour "
            f"(the {cloud_mac.HOST_MINIMUM_HOURS}-hour minimum is charged either way)",
        )
    else:
        _print_row("Accrued", "unknown -- no rate was recorded for this host")


def _print_status_summary(status: dict[str, Any]) -> None:
    """Print `nyxgpt cloud status` in the form an operator reads (#3813).

    The machine form is still available behind `--json`; this is the default
    because the question being asked -- "where is my instance and how do I
    reach it?" -- is one a human is asking, and answering it with a nested
    JSON blob made the operator scroll for the public IP either way.

    Four headline verdicts, not two (#3993): DEPLOYED, NOT COMPLETED (a deploy
    this machine started and did not finish), SUBSTRATE ONLY (a provisioned
    instance with no deploy recorded against it), and UNKNOWN. A failed deploy
    printed as UNKNOWN sent the operator looking for another workstation while
    the instance they had just created was named on their own disk.
    """
    commands = status.get("commands") or LIFECYCLE_COMMANDS
    if not status["known"]:
        print(
            "nyxGPT cloud deployment: UNKNOWN from this machine.\n\n"
            "No deploy has been recorded here and this is not the instance. That is not the "
            "same as nothing being deployed -- another operator's workstation would know. Run "
            f"`{commands['status']}` where `{commands['deploy']}` was run, or deploy from here "
            f"with `{commands['deploy']}`."
        )
        # Printed even here, and especially here: the ordinary end state of a
        # macOS teardown is "no deployment, one Dedicated Host still billing
        # until tomorrow". Reporting only the deployment would make the single
        # remaining charge invisible at exactly the moment it is the only
        # thing left (#3995).
        _print_pending_mac_host(status.get("mac_host") or {})
        return

    if status["source"] in (SOURCE_DEPLOY_ATTEMPT, SOURCE_SUBSTRATE_RECORD):
        _print_incomplete_summary(status, commands)
        return

    infra = status.get("infra") or {}
    connection = status.get("connection") or {}
    tunnel = status.get("tunnel") or {}

    print("nyxGPT cloud deployment: DEPLOYED")
    if status["on_instance"]:
        print(
            "  (read first-hand: this process is running on the instance, so the release "
            "below is the one answering)\n"
        )
    else:
        print(f"  (from the deploy record on this machine, {DEPLOY_STATE_FILE})\n")

    _print_row("Version", status["version"] or "unknown")
    # #3950. The version line alone cannot answer "is this a published build?":
    # a working tree declares a release that usually does not exist yet, so a
    # dev deploy and an artifact deploy of the same version print an identical
    # `Version` row. Named on every deployment rather than only on dev ones --
    # "published release" is a claim worth stating, and a row that appears in
    # one state only is a row an operator does not know to look for. Same three
    # answers the dashboard gives (`web/src/app/admin/infrastructure/page.tsx`),
    # including the honest "not recorded here" when the question is asked from
    # the instance, where no deploy record exists to answer it.
    if status.get("dev"):
        _print_row(
            "Build source",
            f"working tree shipped from {status.get('source_dir') or 'an unrecorded checkout'} "
            f"(--dev) -- not a published {status['version']} release",
        )
    elif status.get("source") == SOURCE_LOCAL_INSTANCE:
        _print_row(
            "Build source",
            "not recorded here -- the deploy record lives on the workstation that ran the deploy",
        )
    else:
        _print_row("Build source", "published release, installed from PyPI on the instance")
    _print_row(
        "Target OS",
        # Not defaulted to "linux": a record written before #3867 does not say,
        # and guessing would be an assertion about a machine nothing checked.
        status.get("os_family") or "not recorded (deploy predates `--os`)",
    )
    instance = status["instance_id"] or "unknown"
    if status.get("instance_type"):
        instance = f"{instance} ({status['instance_type']})"
    _print_row("Instance", instance)
    _print_row("Region", status["region"] or "unknown")
    _print_row("Public IP", status["host"] or "unknown")
    _print_row("Profiles", ", ".join(status["profiles"]) or "none (core stack only)")
    # #3956. "unknown" rather than "native" when nothing was recorded: a
    # deploy from before the flag existed is not a claim about what the box is
    # running, and D-018's rule is that a cloud status surface says *unknown*
    # rather than an answer nothing checked.
    substrate = status.get("substrate") or ""
    if substrate == SUBSTRATE_KUBERNETES:
        _print_row("Substrate", "single-node k3s cluster on the instance (k8s/*.yaml)")
        _print_row("Canary", commands["canary"])
    elif substrate == SUBSTRATE_NATIVE:
        _print_row(
            "Substrate",
            f"native services on the instance -- {commands['deploy_kubernetes']} for a "
            "cluster (enables canary rollout)",
        )
    else:
        _print_row("Substrate", "unknown -- this deploy predates the record of it")

    _print_pending_mac_host(status.get("mac_host") or {})

    access = infra.get("access_model") or {}
    if access.get("open_ports"):
        allowed = infra.get("owner_ip_cidr") or "your workstation's IP"
        ports = ", ".join(str(port) for port in access["open_ports"])
        _print_row("Security group", f"port {ports} from {allowed}, and nothing else")

    print("\nConnection target")
    if connection.get("known"):
        _print_row("SSH target", connection["target"])
        _print_row(
            "Identity file",
            connection["identity_file"] or "(ssh's own ~/.ssh defaults and agent)",
        )
    else:
        _print_row("SSH target", f"unknown -- {connection.get('reason', '')}")

    print("\nAccess tunnel")
    if status["on_instance"]:
        _print_row("State", "not applicable -- the tunnel is opened from your workstation")
    elif tunnel.get("running"):
        _print_row("State", f"open (pid {tunnel['pid']})")
    else:
        _print_row("State", f"closed -- open it with `{commands['tunnel']}`")
    _print_row("Stack health", _health_label(status.get("health") or {}))
    # #3993. A deployment that exists plus a *later* deploy that failed is a
    # real and confusing state -- the box is up on the previous release while
    # the operator believes they just shipped a new one. The record above
    # cannot say it, because it is only ever written on success.
    attempt = status.get("attempt") or {}
    if attempt and attempt.get("status") != ATTEMPT_SUCCEEDED:
        _print_row("Last deploy attempt", _attempt_label(attempt))
    if status.get("urls") and not status["on_instance"]:
        print("\n  Reachable while the tunnel is open:")
        _print_urls(status["urls"])

    print("\nCommands")
    labelled = [
        (label, commands[key])
        for label, key in (
            ("Open the tunnel", "tunnel"),
            ("Close the tunnel", "tunnel_stop"),
            ("Containers on the instance", "ops_status"),
            ("Diagnose the instance", "doctor"),
            ("Observability logins", "credentials"),
            ("Redeploy (idempotent)", "redeploy"),
            ("Re-allow SSH after an IP change", "allow_ip"),
            ("Tear it all down", "destroy"),
        )
        if commands.get(key)
    ]
    # Widened to the longest label rather than the fixed column the fields
    # above use: these labels are sentences, and a fixed column ran them into
    # the commands they name.
    width = max((len(label) for label, _ in labelled), default=0) + 2
    for label, command in labelled:
        print(f"  {label:<{width}}{command}")

    # Diagnostics, not an instruction: `nyxgpt cloud tunnel` is the command an
    # operator runs (CLAUDE.md's wrapper requirement). Printed because a
    # tunnel that will not open is a support conversation about ssh options,
    # and nothing used to show what was actually being executed.
    if connection.get("tunnel_invocation"):
        print(
            f"\nDiagnostics -- what `{commands['tunnel']}` executes on your behalf "
            "(run the wrapped command, not this):"
        )
        print(f"  {connection['tunnel_invocation']}")


def remote_credentials(target: DeployTarget, service: str = "all") -> list[dict[str, Any]]:
    """Read the deployment's Grafana/GlitchTip admin logins over wrapped SSH.

    Runs the instance's own `nyxgpt ops credentials --json` (#3718) through
    the same `run_remote` path every other deploy step uses, so an operator
    never hand-rolls `ssh` + `cat` to sign into the observability UIs behind
    the tunnel. `nyxgpt ops credentials` exits 2 when a service has no
    provisioned password yet, which is a perfectly reportable answer -- the
    per-service `remediation` text says what to run -- so only an unparseable
    or transport-level failure raises here.
    """
    remote = (
        f'"$HOME/.nyxGPT/venv/bin/nyxgpt" ops credentials --json --service {shlex.quote(service)}'
    )
    completed = run_remote(target, remote, timeout=120)
    stdout = completed.stdout or ""
    try:
        parsed = json.loads(stdout[stdout.index("[") :])
    except (ValueError, json.JSONDecodeError) as exc:
        detail = (completed.stderr or "").strip() or stdout.strip() or "no output"
        raise CloudCommandError(
            f"Could not read credentials from {target.host}: {detail}. The instance may "
            "predate `nyxgpt ops credentials` -- `nyxgpt cloud deploy` upgrades it."
        ) from exc
    return [dict(item) for item in parsed]


def _print_credentials(creds: list[dict[str, Any]]) -> None:
    """Print remote credential records in the same shape as the local command."""
    blocks: list[str] = []
    for cred in creds:
        lines = [str(cred.get("service", ""))]
        if cred.get("url"):
            lines.append(f"  URL:      {cred['url']}")
        lines.append(f"  Username: {cred.get('username', '')}")
        if cred.get("available"):
            lines.append(f"  Password: {cred.get('password', '')}")
            lines.append(f"  Source:   {cred.get('source', '')} (on the instance)")
        else:
            lines.append("  Password: (not provisioned)")
            lines.append(f"  Fix:      {cred.get('remediation', '')}")
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))


def _credentials_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud credentials` entry point.

    Note the URLs printed are the instance's own loopback URLs; they are
    reachable from the workstation once `nyxgpt cloud tunnel` is open, which
    is the same wrapped access path the dashboard links assume.
    """
    target = resolve_access_target(args)
    creds = remote_credentials(target, service=getattr(args, "service", "all") or "all")
    if getattr(args, "json", False):
        print(json.dumps(creds, indent=2))
    else:
        _print_credentials(creds)
        print("\nReach these URLs with `nyxgpt cloud tunnel`.")
    return 0 if all(c.get("available") for c in creds) else 2


def remote_ops(target: DeployTarget, inspection: str) -> int:
    """Run one read-only `nyxgpt` inspection *on* the instance, over wrapped SSH.

    The same access path `remote_credentials` uses, for the same reason:
    checking what containers the deployment is running should not require an
    operator to hand-roll `ssh` and then a raw `docker compose ps` (#3813).
    The instance's own output is streamed through unchanged -- this is the
    remote command's answer, not a re-formatting of it.

    A non-zero exit from the *inspection* is a reportable answer (`ops
    doctor` says so when the stack is unhealthy) and is returned. Only a
    failure of the transport itself raises: ssh's own 255, or a shell that
    could not find the instance's `nyxgpt` at all.
    """
    remote = f'"$HOME/.nyxGPT/venv/bin/nyxgpt" {REMOTE_OPS_COMMANDS[inspection]}'
    completed = run_remote(target, remote, stream=True, timeout=300)
    stderr = (completed.stderr or "").strip()
    if completed.returncode == 255:
        raise CloudCommandError(
            f"Could not reach {target.user}@{target.host} over SSH: {stderr or 'no detail'}. "
            f"If your public IP has changed, run `{LIFECYCLE_COMMANDS['allow_ip']}`."
        )
    if completed.returncode == 127:
        raise CloudCommandError(
            f"{target.host} has no nyxGPT installed at ~/.nyxGPT/venv/bin/nyxgpt. "
            f"`{LIFECYCLE_COMMANDS['deploy']}` installs it."
        )
    if stderr:
        print(stderr, file=sys.stderr)
    return completed.returncode


def canary_argv(args: argparse.Namespace) -> list[str]:
    """Build the `canary ...` argument list to run on the instance (#3956).

    Every value is validated here rather than trusted from the namespace:
    this is spliced into a remote shell command, and the numeric flags are
    the only free-form input in it. `argparse` already types `--weight` and
    `--step` as ints, so this is the second of two checks, not the only one.
    """
    subcommand = str(getattr(args, "canary_cmd", "") or "")
    if subcommand not in REMOTE_CANARY_COMMANDS:
        raise CloudCommandError(
            f"{subcommand!r} is not a canary subcommand that can run against a cloud "
            f"deployment. Available: {', '.join(REMOTE_CANARY_COMMANDS)}. "
            "(`canary deploy` builds an image from a checkout, and the instance has none "
            "by design -- roll a release out with `nyxgpt cloud deploy --version <release>`.)"
        )
    allowed = CANARY_FLAGS[subcommand]
    argv = ["canary", subcommand]
    component = getattr(args, "component", None)
    if "component" in allowed and component:
        argv += ["--component", str(component)]
    for flag in ("weight", "step"):
        value = getattr(args, flag, None)
        if flag in allowed and value is not None:
            argv += [f"--{flag}", str(int(value))]
    if "force" in allowed and getattr(args, "force", False):
        argv.append("--force")
    return argv


def remote_canary(target: DeployTarget, argv: list[str]) -> int:
    """Run one `nyxgpt canary ...` invocation on the instance, over wrapped SSH.

    Same transport contract as `remote_ops`: the command's own exit status is
    the answer (`canary evaluate` exits 2 on a failing canary, which is a
    result, not an error), and only a transport failure raises.
    """
    remote = " ".join(['"$HOME/.nyxGPT/venv/bin/nyxgpt"', *(shlex.quote(part) for part in argv)])
    completed = run_remote(target, remote, stream=True, timeout=600)
    stderr = (completed.stderr or "").strip()
    if completed.returncode == 255:
        raise CloudCommandError(
            f"Could not reach {target.user}@{target.host} over SSH: {stderr or 'no detail'}. "
            f"If your public IP has changed, run `{LIFECYCLE_COMMANDS['allow_ip']}`."
        )
    if completed.returncode == 127:
        raise CloudCommandError(
            f"{target.host} has no nyxGPT installed at ~/.nyxGPT/venv/bin/nyxgpt. "
            f"`{LIFECYCLE_COMMANDS['deploy']}` installs it."
        )
    if stderr:
        print(stderr, file=sys.stderr)
    return completed.returncode


def _canary_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud canary <subcommand>` entry point (#3956)."""
    record = load_deploy_state()
    if record and not record.get("kubernetes"):
        raise CloudCommandError(
            "The recorded deployment does not run Kubernetes, and canary rollout needs a "
            "cluster to weight traffic in. Re-deploy with `nyxgpt cloud deploy --kubernetes` "
            "(see docs/cloud.md#kubernetes-on-the-cloud-target)."
        )
    argv = canary_argv(args)
    target = resolve_access_target(args)
    print(f"# {target.user}@{target.host}: nyxgpt {' '.join(argv)}\n")
    return remote_canary(target, argv)


def _ops_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud ops <inspection>` entry point."""
    inspection = str(getattr(args, "inspection", "") or "status")
    target = resolve_access_target(args)
    print(f"# {target.user}@{target.host}: nyxgpt {REMOTE_OPS_COMMANDS[inspection]}\n")
    return remote_ops(target, inspection)


def _status_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud status` entry point (#3813).

    Human-readable by default and JSON on request -- the reverse of the
    `nyxgpt cloud deploy --status` flag it replaces, which only ever emitted
    the machine form. Both forms answer from the same `deploy_status` call,
    so the summary can never describe a different deployment than `--json`.

    The health probe is opt-out rather than opt-in here: this is a one-shot
    command an operator ran to find out whether their deployment works, and
    `deploy_status` already skips the probe entirely when there is no tunnel
    to probe through (or when it is the instance answering), so the default
    costs nothing in exactly the cases where it would have been useless.
    """
    status = deploy_status(probe_health=not getattr(args, "no_probe", False))
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
    else:
        _print_status_summary(status)
    return 0


def _tunnel_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud tunnel` entry point."""
    if getattr(args, "stop", False):
        result = stop_tunnel()
        print("Tunnel stopped." if result["stopped"] else "No tunnel is running.")
        return 0
    if getattr(args, "status", False):
        print(json.dumps(tunnel_status(), indent=2))
        return 0

    # The same recorded credentials `nyxgpt cloud status` reports as the
    # connection target -- otherwise the tunnel this opens could differ from
    # the invocation that command prints as what it executes.
    target = resolve_access_target(args)
    profiles = [str(p) for p in (load_deploy_state().get("profiles") or [])]
    background = bool(getattr(args, "background", False))
    result = start_tunnel(target, profiles, background=background)
    if background:
        if result.get("already_running"):
            print("A tunnel is already open.")
        else:
            print(f"Tunnel open in the background (pid {result['pid']}).")
        _print_urls(result["urls"])
        print("\nClose it with `nyxgpt cloud tunnel --stop`.")
    return 0


def _print_mac_teardown(mac: dict[str, Any]) -> None:
    """Report what happened to the Mac and its Dedicated Host (#3995).

    Silent when nyxGPT did not manage a Mac. When it did, this is the part of
    the teardown an operator must not miss: the instance is gone but the host
    is not, deliberately, and something has to say when it will be and who
    will be told.
    """
    if not mac.get("managed"):
        return
    host_id = str(mac.get("host_id") or "unknown")
    if mac.get("instance_terminated"):
        print("The EC2 Mac instance was terminated.")
    else:
        print(
            "WARNING: the EC2 Mac instance did NOT come down. It costs $0.00/hour on a "
            "Dedicated Host, but it also keeps the host from being scrubbed and released."
        )
    if mac.get("release_scheduled"):
        channel = str((mac.get("schedule") or {}).get("slack_channel") or "")
        where = f" ({channel})" if channel else ""
        print(
            f"Dedicated Host {host_id} stays allocated until "
            f"{mac.get('release_at') or 'its 24-hour minimum closes'} -- AWS refuses to release "
            "one before that. A one-shot AWS schedule releases it then and posts the outcome "
            f"to Slack{where}."
            "\n`nyxgpt cloud status` reports the host, its release time and the accrued cost "
            "until it is gone."
        )
    else:
        print(
            f"WARNING: Dedicated Host {host_id} is STILL ALLOCATED and its release is NOT "
            "scheduled. It bills until it is released."
        )
        for error in mac.get("errors") or []:
            print(f"  - {error}")
        print(
            "Fix the cause and re-run `nyxgpt cloud destroy --yes` -- it is idempotent and "
            "will schedule the release without touching anything that is already gone."
        )


def deploy_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud {deploy,destroy,tunnel,credentials,status,ops}` entry point."""
    subcommand = getattr(args, "cloud_cmd", "")
    try:
        if subcommand == "status":
            return _status_command(args)
        if subcommand == "ops":
            return _ops_command(args)
        if subcommand == "canary":
            return _canary_command(args)
        if subcommand == "deploy":
            if getattr(args, "status", False):
                # Kept working for anything already scripted against it, and
                # kept emitting JSON so that output does not change shape
                # under a script's feet. `nyxgpt cloud status` is the
                # first-class command now (#3813) and says so here, because
                # a flag on `deploy` is not where an operator looks for it.
                print(
                    f"note: `{LIFECYCLE_COMMANDS['status']}` is the status command -- it prints "
                    "an operator-readable summary, and `--json` for this same payload.",
                    file=sys.stderr,
                )
                print(json.dumps(deploy_status(), indent=2))
                return 0
            _print_deploy_summary(deploy(args))
        elif subcommand == "destroy":
            if not getattr(args, "yes", False):
                print(
                    "Refusing to destroy the cloud deployment without --yes. This deletes the "
                    "instance and its root volume; any data only on that box is lost.",
                    file=sys.stderr,
                )
                return 1
            destroyed = destroy(args)
            print("Cloud deployment destroyed (tunnel closed, substrate torn down).")
            if destroyed.get("unmanaged_target"):
                print(
                    f"The EC2 Mac at {destroyed['unmanaged_target']} is still running -- nyxGPT "
                    "did not create it and cannot terminate it. Release it (and its Dedicated "
                    "Host) yourself if you are done with it; the host bills until you do."
                )
            _print_mac_teardown(destroyed.get("mac") or {})
        elif subcommand == "tunnel":
            return _tunnel_command(args)
        elif subcommand == "credentials":
            return _credentials_command(args)
        else:  # pragma: no cover - argparse enforces the choices
            raise CloudCommandError(f"Unknown `nyxgpt cloud` subcommand {subcommand!r}")
    except CloudCommandError as exc:
        print(f"nyxgpt cloud {subcommand}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - foreground tunnel
        print("\nTunnel closed.")
        return 0
    return 0


def tunnel_invocation(target: DeployTarget, profiles: list[str] | None = None) -> str:
    """Return the raw `ssh` command the tunnel wraps, for docs and diagnostics.

    Never printed as an *instruction* (CLAUDE.md forbids raw commands in user
    flows) -- `nyxgpt cloud tunnel` is what operators are told to run. It is
    carried by `connection_status`, which is how both `nyxgpt cloud status`
    and the dashboard's cloud page show what is actually executing under a
    "diagnostics" heading (#3813).
    """
    return " ".join(shlex.quote(part) for part in tunnel_argv(target, profiles))
