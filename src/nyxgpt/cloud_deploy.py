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
   this repository, and nothing is copied from the operator's checkout
   (there may not be one). This mirrors, step for step, the
   `artifact-install-smoke` job in `.github/workflows/release-artifacts.yml`,
   which proves that exact sequence on a checkout-free Linux runner.
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
`ssh`, `terraform`, or `docker`. The same functions back the admin
dashboard's Cloud Infrastructure page, so the deploy is operable from the
SRE surface as well as the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nyxgpt import cloud_infra
from nyxgpt.cloud import CloudCommandError

# The same `~/.nyxGPT/cloud` directory `cloud_infra` owns -- one place for
# everything about a cloud deployment. `cloud_infra.CLOUD_STATE_FILE` (the
# substrate's instance/region handoff) is read through the module attribute
# rather than aliased here, so it stays a single source of truth.
CLOUD_DIR = cloud_infra.CLOUD_DIR

# What the last successful deploy installed, so `status` can answer without
# touching AWS or the instance, and a re-run can default to the same version.
DEPLOY_STATE_FILE = CLOUD_DIR / "deploy.json"

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
# resolves by default (terraform/aws/modules/compute/main.tf).
DEFAULT_SSH_USER = "ec2-user"

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


def resolve_plan(args: argparse.Namespace) -> DeployPlan:
    """Merge flags over the last deploy's recorded choices.

    The choices that carry over are the ones `deploy.json` records: the
    release, the SSH user and the identity file. Everything else (profiles,
    timeouts, whether to open a tunnel) is per-run and comes from the flags
    or their defaults.
    """
    previous = load_deploy_state()
    version = str(
        getattr(args, "version", None) or previous.get("version") or installed_version() or ""
    )
    if not version:
        raise CloudCommandError(
            "Could not determine which nyxGPT release to install on the instance. "
            "Pass --version <release> (e.g. --version 3.0.0) -- the instance installs a "
            "published artifact, never a copy of your working tree."
        )
    # The version is spliced into the remote provisioning script, so a stray
    # quote or `$` in it would surface as a confusing shell error ten minutes
    # into a remote run. Fail here instead, where the operator can see why.
    if not _VERSION_RE.fullmatch(version):
        raise CloudCommandError(
            f"{version!r} is not a valid release to install. Pass a published version such as "
            "--version 3.0.0 (digits, letters, dot, dash and underscore only)."
        )
    if getattr(args, "skip_observability", False):
        profiles: list[str] = []
    else:
        from nyxgpt import ops

        profiles = list(ops.OBSERVABILITY_PROFILES)
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
    )


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

echo "==> nyxGPT ${NYXGPT_VERSION}: provisioning $(hostname) from published artifacts"

# --- OS packages -------------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3.11 python3.11-pip docker tar gzip >/dev/null
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
    python3 python3-venv python3-pip docker.io tar gzip >/dev/null
else
  echo "unsupported package manager: need dnf (Amazon Linux/Fedora) or apt-get (Debian/Ubuntu)" >&2
  exit 1
fi

PY=python3.11
command -v "$PY" >/dev/null 2>&1 || PY=python3

# --- Node.js 20 (the web bundle's build and run toolchain) -------------
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

# --- Ollama ------------------------------------------------------------
command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh

# --- nyxGPT itself, from the published PyPI artifact -------------------
# No checkout is created on this machine, by design (CLAUDE.md, repo-less
# portability 2026-08-01): the instance installs the same published release
# an operator would install on a laptop.
"$PY" -m venv "$HOME/.nyxGPT/venv" >/dev/null
"$HOME/.nyxGPT/venv/bin/pip" install --quiet --upgrade pip
"$HOME/.nyxGPT/venv/bin/pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"
NYXGPT="$HOME/.nyxGPT/venv/bin/nyxgpt"

# --- Seed config.ini from the installed package ------------------------
mkdir -p "$HOME/.nyxGPT"
if [ ! -f "$HOME/.nyxGPT/config.ini" ]; then
  EXAMPLE_CONFIG=$("$HOME/.nyxGPT/venv/bin/python" -c \\
    "from nyxgpt import config_wizard; print(config_wizard._EXAMPLE_CONFIG_PATH)")
  cp "$EXAMPLE_CONFIG" "$HOME/.nyxGPT/config.ini"
  chmod 600 "$HOME/.nyxGPT/config.ini"
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
  run_nyxgpt ops install
else
  run_nyxgpt ops install --skip-observability
fi

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


def render_provision_script(plan: DeployPlan) -> str:
    """Render the instance provisioning script for `plan`.

    Kept a pure function so the repo-less guarantee (no clone, no checkout
    copy) and the artifact version pin are unit-testable without an EC2
    instance.
    """
    return PROVISION_SCRIPT_TEMPLATE.replace("__VERSION__", plan.version).replace(
        "__PROFILES__", ",".join(plan.profiles)
    )


# How many trailing lines of the provisioning output a failure summary quotes
# when the run produced no `[FAIL]` line to name (an OS-package or shell
# error before `ops install` ever started, say). Whole lines, never a
# character slice -- see `_provision_failure_detail`.
_PROVISION_TAIL_LINES = 25

# The prefix `nyxgpt ops` puts on every failed check, including the per-step
# verdict line `_emit_step_verdict` adds (#3762).
_OPS_FAIL_PREFIX = "[FAIL]"


def _run_provision_script(target: DeployTarget, script: str) -> tuple[int, list[str]]:
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
    argv = [*ssh_argv(target), "bash -s"]
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
    returncode, output = _run_provision_script(target, script)
    if returncode != 0:
        raise CloudCommandError(_provision_failure_detail(output))
    # `self_heal_enabled` is safe to assert rather than re-probe: the script
    # runs under `set -euo pipefail`, so a failed `self-heal enable` would
    # have made this a non-zero exit and raised above.
    return {"version": plan.version, "profiles": list(plan.profiles), "self_heal_enabled": True}


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

    infra = cloud_infra.apply_infra(args)
    steps.append({"step": "infra", "outputs": infra.get("outputs", {})})

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

    waited = wait_for_ssh(target, plan.ssh_timeout)
    steps.append({"step": "ssh", "host": target.host, "waited_seconds": round(waited, 1)})

    steps.append({"step": "provision", **provision_instance(target, plan)})

    record = {
        "version": plan.version,
        "profiles": list(plan.profiles),
        "ssh_user": plan.ssh_user,
        # Recorded so a re-run that needed a non-default key doesn't have to
        # pass --identity-file again, the same way --version and --ssh-user
        # carry over.
        "identity_file": plan.identity_file,
        "host": target.host,
        "instance_id": target.instance_id,
        "region": target.region,
    }
    _write_json(DEPLOY_STATE_FILE, record)

    health: dict[str, Any] = {"healthy": False, "skipped": True}
    tunnel: dict[str, Any] = {"running": False, "skipped": True}
    if plan.open_tunnel:
        tunnel = start_tunnel(target, plan.profiles)
        steps.append({"step": "tunnel", "pid": tunnel.get("pid", 0)})
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
                host=target.host,
                instance_id=target.instance_id,
                region=target.region,
                profiles=list(plan.profiles),
                detail=(
                    f"stack installed but {health['url']} never returned 200 within "
                    f"{plan.health_timeout:.0f}s"
                ),
            )
            raise CloudCommandError(
                f"The stack was installed but {health['url']} never returned 200 within "
                f"{plan.health_timeout:.0f}s (last status: {health['status'] or 'unreachable'}).\n"
                "The tunnel is still open -- `nyxgpt cloud deploy --status` and the instance's "
                "own `nyxgpt ops doctor` will say more."
            )

    record_history(
        "deploy",
        "succeeded",
        version=plan.version,
        host=target.host,
        instance_id=target.instance_id,
        region=target.region,
        profiles=list(plan.profiles),
        detail=(
            "healthy over the access tunnel"
            if health.get("healthy")
            else "installed; health not checked (no tunnel opened)"
        ),
    )

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
    """Tear the whole deployment down: tunnel first, then the substrate."""
    # Read before the teardown deletes it -- the history entry names what was
    # torn down, which is unrecoverable once `deploy.json` is gone.
    previous = load_deploy_state()
    tunnel = stop_tunnel()
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
    settings = result.get("settings", {})
    record_history(
        "destroy",
        "succeeded",
        version=str(previous.get("version") or ""),
        host=str(previous.get("host") or ""),
        instance_id=str(previous.get("instance_id") or ""),
        region=str(previous.get("region") or settings.get("aws_region") or ""),
        detail="tunnel closed, substrate torn down",
    )
    return {"action": "destroy", "tunnel": tunnel, "settings": settings}


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
    "status": "nyxgpt cloud deploy --status",
    "allow_ip": "nyxgpt cloud allow-ip",
}


def deploy_status(probe_health: bool = False) -> dict[str, Any]:
    """Report the deployment's state without touching AWS or the instance.

    Cheap enough for the dashboard to poll, and still answers on a machine
    whose AWS credentials have expired.

    `probe_health=True` additionally makes one short request to the tunneled
    API health endpoint. Opt-in rather than always-on so the polled default
    stays free of network calls; the dashboard asks for it on an explicit
    load or refresh, where waiting a moment for a real answer is the point.
    A probe with no tunnel open would only ever time out, so it is skipped.
    """
    record = load_deploy_state()
    infra = cloud_infra.infra_status()
    profiles = [str(p) for p in (record.get("profiles") or [])]
    tunnel = tunnel_status()

    health: dict[str, Any] = {"checked": False, "healthy": False, "status": 0, "reason": ""}
    if probe_health and not tunnel["running"]:
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
        "deployed": bool(record.get("host")),
        "version": str(record.get("version") or ""),
        "host": str(record.get("host") or ""),
        "instance_id": str(record.get("instance_id") or infra.get("instance_id") or ""),
        "region": str(record.get("region") or infra.get("region") or ""),
        "profiles": profiles,
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
    target = resolve_target(args)
    creds = remote_credentials(target, service=getattr(args, "service", "all") or "all")
    if getattr(args, "json", False):
        print(json.dumps(creds, indent=2))
    else:
        _print_credentials(creds)
        print("\nReach these URLs with `nyxgpt cloud tunnel`.")
    return 0 if all(c.get("available") for c in creds) else 2


def _tunnel_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud tunnel` entry point."""
    if getattr(args, "stop", False):
        result = stop_tunnel()
        print("Tunnel stopped." if result["stopped"] else "No tunnel is running.")
        return 0
    if getattr(args, "status", False):
        print(json.dumps(tunnel_status(), indent=2))
        return 0

    target = resolve_target(args)
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


def deploy_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud {deploy,destroy,tunnel,credentials}` entry point."""
    subcommand = getattr(args, "cloud_cmd", "")
    try:
        if subcommand == "deploy":
            if getattr(args, "status", False):
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
            destroy(args)
            print("Cloud deployment destroyed (tunnel closed, substrate torn down).")
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
    flows) -- `nyxgpt cloud tunnel` is what operators are told to run. This
    exists so `--status`/support output can show what is actually executing.
    """
    return " ".join(shlex.quote(part) for part in tunnel_argv(target, profiles))
