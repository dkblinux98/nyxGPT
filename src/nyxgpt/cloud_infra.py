"""AWS substrate provisioning for `nyxgpt cloud infra` (P6-8, #3509).

The Terraform configuration in `terraform/aws/` provisions the cloud
substrate -- a VPC, public subnet(s), one owner-IP-scoped SSH-only security
group, and the single EC2 instance approved in
`product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md`. This module is the
only supported way to drive it: per CLAUDE.md's wrapper requirement no user
flow may call `terraform` directly, so `nyxgpt cloud infra
{plan,apply,destroy,status,test}` owns Terraform's whole lifecycle --
installing the binary, materializing the configuration, generating tfvars,
pinning state, and recording the result.

Three details are load-bearing for the surrounding system:

* **Repo-less portability** (CLAUDE.md, 2026-08-01): the configuration is
  read from the packaged `nyxgpt.resources` tree (a symlink back to
  `terraform/` in a dev checkout, real files in a built wheel -- see #3621)
  and synced into `~/.nyxGPT/cloud/terraform/`, so provisioning works on a
  machine with no checkout.
* **The `state.json` handoff**: after a successful apply the Terraform
  outputs are written to `~/.nyxGPT/cloud/state.json` -- exactly the
  contract `nyxgpt cloud allow-ip` (`nyxgpt.cloud`) already reads to find
  the security group whose port-22 rule it retargets when the owner's IP
  changes.
* **Owner-IP scoping**: the SSH source CIDR defaults to this machine's
  current public IP, normalized through `nyxgpt.cloud.normalize_cidr`, which
  refuses `0.0.0.0/0`. The Terraform configuration refuses it a second time
  at plan time.

Deploying the nyxGPT stack *onto* the provisioned instance, and the
`nyxgpt cloud tunnel` access path, are P6-11/#3513 scope -- this module
stops at the substrate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import cloud_imds
from nyxgpt import config as config_mod
from nyxgpt.cloud import (
    CLOUD_STATE_FILE,
    CloudCommandError,
    detect_current_public_ip,
    normalize_cidr,
)

NYXGPT_HOME = Path.home() / ".nyxGPT"
CLOUD_DIR = NYXGPT_HOME / "cloud"

# Where the packaged terraform/aws configuration is materialized. Terraform
# needs a real, writable directory (provider plugins, .terraform/), and an
# installed wheel's package data may live somewhere read-only.
TERRAFORM_DIR = CLOUD_DIR / "terraform"

# Generated from `infra.json`; 0600 because it carries the owner's IP and,
# potentially, an SSH public key.
TFVARS_FILE = CLOUD_DIR / "terraform.tfvars"

# Local state lives outside the (re-synced, disposable) configuration
# directory so an upgrade that re-materializes terraform/aws can never clobber
# it. This is the default and remains the fallback; `nyxgpt cloud state
# migrate` (P6-9, #3510) moves state to an S3 backend with DynamoDB locking,
# after which this file is only the pre-migration copy.
TFSTATE_FILE = CLOUD_DIR / "terraform.tfstate"

# Persisted answers (region, key pair, instance type, ...) so a later
# `nyxgpt cloud infra apply` doesn't need every flag repeated.
SETTINGS_FILE = CLOUD_DIR / "infra.json"

PLAN_FILE = CLOUD_DIR / "tfplan"

# Keys `nyxgpt cloud infra` owns inside the shared cloud state file. Anything
# else in there (written by other `nyxgpt cloud` work) is preserved on write
# and left alone on destroy.
STATE_KEYS = (
    "region",
    "vpc_id",
    "security_group_id",
    "instance_id",
    "instance_type",
    "public_ip",
    "private_ip",
    "ssh_key_name",
)

# Terraform was pulled from homebrew-core after HashiCorp's 2023 BUSL
# relicense, so `brew install terraform` fails -- the official tap is the
# supported install path (mirrors `nyxgpt.ops`'s local Terraform bootstrap).
HASHICORP_TAP = "hashicorp/tap"

# Dummy credentials for `nyxgpt cloud infra test`, which runs the plan-level
# suite in terraform/aws/tests/. Those run blocks set the provider's skip_*
# escape hatches and pin an AMI, so nothing reaches AWS -- but the provider
# still refuses to configure itself with no credentials at all.
_OFFLINE_TEST_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_EC2_METADATA_DISABLED": "true",
}


@dataclass
class InfraSettings:
    """Resolved inputs for one `nyxgpt cloud infra` run, rendered into tfvars."""

    aws_region: str
    owner_ip_cidr: str
    ssh_key_name: str = ""
    ssh_public_key: str = ""
    instance_type: str = "m5.large"
    root_volume_size: int = 100
    aws_profile: str = ""
    name_prefix: str = "nyxgpt-tf"

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, used for both `infra.json` and tfvars rendering."""
        return {
            "aws_region": self.aws_region,
            "aws_profile": self.aws_profile,
            "name_prefix": self.name_prefix,
            "owner_ip_cidr": self.owner_ip_cidr,
            "ssh_key_name": self.ssh_key_name,
            "ssh_public_key": self.ssh_public_key,
            "instance_type": self.instance_type,
            "root_volume_size": self.root_volume_size,
        }


# --- Terraform binary + configuration materialization ---


def ensure_terraform_binary() -> str:
    """Return the path to `terraform`, installing it from the HashiCorp tap if missing.

    CLAUDE.md forbids telling an operator to run raw `terraform`, which
    extends to telling them to install it by hand when a wrapped command can
    do it. Homebrew is the supported install path (as it is for the local
    Terraform stack); without it, the error names the manual download.
    """
    found = shutil.which("terraform")
    if found:
        return found
    if shutil.which("brew") is None:
        raise CloudCommandError(
            "terraform is required to provision the AWS substrate and Homebrew is "
            "unavailable to install it. Install Terraform >= 1.9.0 from "
            "https://developer.hashicorp.com/terraform/install and re-run."
        )
    for command in (
        ["brew", "tap", HASHICORP_TAP],
        ["brew", "install", f"{HASHICORP_TAP}/terraform"],
    ):
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            raise CloudCommandError(
                f"`{' '.join(command)}` failed while installing Terraform: "
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )
    found = shutil.which("terraform")
    if found is None:
        raise CloudCommandError(
            "Terraform install reported success but the binary is still not on PATH."
        )
    return found


def packaged_terraform_dir() -> Path:
    """Path to the packaged `terraform/aws` configuration inside `nyxgpt.resources`."""
    return Path(str(importlib.resources.files("nyxgpt.resources"))) / "terraform" / "aws"


def sync_terraform_config() -> Path:
    """Copy the packaged AWS configuration into `TERRAFORM_DIR` and return it.

    Idempotent: overwrites the `.tf` sources (so an upgraded nyxGPT always
    provisions with its own configuration) while leaving the working
    directory's `.terraform/` plugin cache in place, and leaving state alone
    entirely -- local state lives one directory up, outside this tree.

    The copy includes the packaged `backend.tf`, which selects *local* state,
    so the last step re-applies whichever backend is actually configured
    (P6-9, #3510). Without it, every sync would silently demote a migrated
    installation back to local state on its next init.
    """
    source = packaged_terraform_dir()
    if not source.is_dir():
        raise CloudCommandError(
            f"Packaged Terraform configuration not found at {source}. "
            "This nyxGPT installation is incomplete -- reinstall the package."
        )
    TERRAFORM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(
            source,
            TERRAFORM_DIR,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfvars"),
        )
    except OSError as exc:
        raise CloudCommandError(
            f"Failed to materialize the Terraform configuration in {TERRAFORM_DIR}: {exc}"
        ) from exc

    # Imported here rather than at module scope: `nyxgpt.cloud_state` imports
    # this module for the shared paths and the Terraform runner, so a
    # top-level import either way would be circular.
    from nyxgpt import cloud_state

    cloud_state.apply_configured_backend()
    return TERRAFORM_DIR


# --- Settings + tfvars ---


def load_settings() -> dict[str, Any]:
    """Read previously-saved infra settings, returning `{}` when absent or unreadable."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_settings(settings: InfraSettings) -> None:
    """Persist `settings` so later runs don't need every flag repeated."""
    CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.chmod(SETTINGS_FILE, 0o600)


def saved_settings() -> InfraSettings | None:
    """Rebuild `InfraSettings` from `infra.json`, or `None` if it isn't usable.

    Used by teardown, which must work from what the last apply recorded
    rather than re-deriving inputs that need the network (the owner's public
    IP) or an SSH key that may since have been removed.
    """
    saved = load_settings()
    if not saved.get("aws_region") or not saved.get("owner_ip_cidr"):
        return None
    return InfraSettings(
        aws_region=str(saved["aws_region"]),
        owner_ip_cidr=str(saved["owner_ip_cidr"]),
        ssh_key_name=str(saved.get("ssh_key_name", "")),
        ssh_public_key=str(saved.get("ssh_public_key", "")),
        instance_type=str(saved.get("instance_type") or "m5.large"),
        root_volume_size=int(saved.get("root_volume_size") or 100),
        aws_profile=str(saved.get("aws_profile", "")),
        name_prefix=str(saved.get("name_prefix") or "nyxgpt-tf"),
    )


def _configured_cloud_reference() -> dict[str, str]:
    """Return config.ini's `[cloud]` profile/region reference, or empty strings on failure."""
    try:
        from nyxgpt import aws_credentials_setup

        return aws_credentials_setup.cloud_reference_status(config_mod.load_config())
    except Exception:
        # A missing//invalid config.ini must not block provisioning -- every
        # value it would supply also has a flag and an environment fallback.
        return {"profile": "", "region": ""}


def _read_ssh_public_key(path_or_material: str) -> str:
    """Return OpenSSH public key material, reading it from disk when given a path."""
    value = path_or_material.strip()
    if not value:
        return ""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        value = candidate.read_text(encoding="utf-8").strip()
    # Checked before the format check so pointing at `id_ed25519` instead of
    # `id_ed25519.pub` -- the easy mistake -- gets the specific warning rather
    # than a generic "that isn't a public key".
    if "PRIVATE KEY" in value:
        raise CloudCommandError(
            "That looks like a PRIVATE key. Pass the matching .pub file -- nyxGPT never "
            "reads or uploads private keys."
        )
    if not value.startswith(("ssh-", "ecdsa-", "sk-")):
        raise CloudCommandError(
            f"{path_or_material!r} is neither a readable file nor OpenSSH public key material "
            "(expected something starting with `ssh-ed25519`/`ssh-rsa`)."
        )
    return value


def resolve_settings(args: argparse.Namespace) -> InfraSettings:
    """Merge explicit flags over saved settings, config.ini, and the environment.

    The owner IP is re-detected on every run unless `--owner-ip` pins it: the
    security group is only useful while it points at where the owner actually
    is, and a plan that quietly reuses a stale IP would look like a no-op
    while leaving the operator locked out.
    """
    saved = load_settings()
    reference = _configured_cloud_reference()

    region = (
        getattr(args, "region", None)
        or saved.get("aws_region")
        or reference.get("region")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    profile = (
        getattr(args, "profile", None)
        or saved.get("aws_profile")
        or reference.get("profile")
        or os.environ.get("AWS_PROFILE")
        or ""
    )

    explicit_ip = getattr(args, "owner_ip", None)
    owner_ip_cidr = normalize_cidr(explicit_ip if explicit_ip else detect_current_public_ip())

    ssh_key_name = getattr(args, "ssh_key_name", None) or ""
    ssh_public_key_arg = getattr(args, "ssh_public_key", None) or ""
    if ssh_key_name and ssh_public_key_arg:
        raise CloudCommandError(
            "Pass either --ssh-key-name (an EC2 key pair that already exists) or "
            "--ssh-public-key (a .pub file to register), not both."
        )
    if ssh_public_key_arg:
        ssh_public_key = _read_ssh_public_key(ssh_public_key_arg)
    elif ssh_key_name:
        ssh_public_key = ""
    else:
        ssh_key_name = str(saved.get("ssh_key_name", ""))
        ssh_public_key = str(saved.get("ssh_public_key", ""))

    if not ssh_key_name and not ssh_public_key:
        raise CloudCommandError(
            "No SSH key configured, and SSH is the only way into a nyxGPT instance "
            "(see product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md). Re-run with "
            "--ssh-public-key ~/.ssh/id_ed25519.pub (registers a new EC2 key pair) or "
            "--ssh-key-name <existing-pair>."
        )

    return InfraSettings(
        aws_region=str(region),
        aws_profile=str(profile),
        owner_ip_cidr=owner_ip_cidr,
        ssh_key_name=str(ssh_key_name),
        ssh_public_key=str(ssh_public_key),
        instance_type=str(
            getattr(args, "instance_type", None) or saved.get("instance_type") or "m5.large"
        ),
        root_volume_size=int(
            getattr(args, "root_volume_size", None) or saved.get("root_volume_size") or 100
        ),
        name_prefix=str(saved.get("name_prefix") or "nyxgpt-tf"),
    )


def _hcl_value(value: Any) -> str:
    """Render a Python value as an HCL literal for the generated tfvars file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_hcl_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_tfvars(settings: InfraSettings) -> str:
    """Render `settings` as the contents of a Terraform `.tfvars` file."""
    lines = [
        "# Generated by `nyxgpt cloud infra` -- edit the command's flags, not this file.",
        "# Regenerated on every run; hand edits are lost.",
        "",
    ]
    for key, value in settings.to_dict().items():
        # Empty strings mean "unset" for every variable here, and the
        # Terraform defaults (or the mutually-exclusive sibling) handle them.
        if value == "":
            continue
        lines.append(f"{key} = {_hcl_value(value)}")
    return "\n".join(lines) + "\n"


def write_tfvars(settings: InfraSettings) -> Path:
    """Write the generated tfvars file (0600) and return its path."""
    CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    TFVARS_FILE.write_text(render_tfvars(settings), encoding="utf-8")
    os.chmod(TFVARS_FILE, 0o600)
    return TFVARS_FILE


# --- Terraform invocation ---


def _run_terraform(
    arguments: list[str],
    *,
    capture: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `terraform -chdir=<synced config> <arguments>`, raising on failure.

    stderr is always captured so a failure carries Terraform's own diagnostic
    into the raised error -- the dashboard only ever sees that message, and
    "terraform apply failed" with no reason is useless to an operator. stdout
    is left attached to the terminal unless `capture` is set (it is for
    `output -json`, which is parsed), so a CLI `plan`/`apply` still streams
    Terraform's progress live rather than going silent for minutes.
    """
    binary = ensure_terraform_binary()
    command = [binary, f"-chdir={TERRAFORM_DIR}", *arguments]
    env = {**os.environ, **(extra_env or {})}
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        detail = f": {diagnostic}" if diagnostic else ""
        raise CloudCommandError(f"`terraform {' '.join(arguments)}` failed{detail}")
    if not capture and completed.stderr:
        # Warnings on a successful run still belong in front of the operator.
        print(completed.stderr, end="", file=sys.stderr)
    return completed


def run_terraform(
    arguments: list[str],
    *,
    capture: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Public entry point to `_run_terraform` for `nyxgpt.cloud_state`.

    That module drives Terraform's state subcommands (`state pull`, `state
    push`, `force-unlock`) against this same synced configuration and must go
    through the same runner, so failures carry Terraform's own diagnostic. A
    delegating function rather than an alias: an alias would bind at import
    and quietly bypass anything that replaces `_run_terraform` later.
    """
    return _run_terraform(arguments, capture=capture, extra_env=extra_env)


def _synced_config_fingerprint() -> str:
    """SHA-256 over the synced Terraform sources, used to detect config changes."""
    digest = hashlib.sha256()
    for path in sorted(TERRAFORM_DIR.rglob("*")):
        if path.is_file() and path.suffix in {".tf", ".hcl", ".tftest.hcl"}:
            digest.update(str(path.relative_to(TERRAFORM_DIR)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _active_backend_mode() -> str:
    """`"s3"` when remote state is configured and migrated, else `"local"`."""
    from nyxgpt import cloud_state

    return "s3" if cloud_state.remote_state_enabled() else "local"


def terraform_init(*, migrate_state: bool = False) -> None:
    """Initialize the synced configuration against its currently configured backend.

    Providers are only re-resolved (`-upgrade`) when the synced configuration
    actually changed -- an nyxGPT upgrade, or a first run. Passing it on every
    init made each plan/apply re-check the registry for nothing.

    Three backend cases, and Terraform needs a different flag for each:

    * **Unchanged.** A plain init. Local state additionally needs
      `-backend-config=path=`, since the packaged `backend.tf` cannot spell
      `~/.nyxGPT` (no `~` expansion inside a backend block); the S3 backend
      is fully specified by the generated file and takes no extra config.
    * **Changing on purpose** (`migrate_state=True`, i.e. `nyxgpt cloud state
      migrate`/`local`): `-migrate-state -force-copy` copies the existing
      state across. `-force-copy` matters because the bare flag stops to ask
      for confirmation, and a wrapped command has no one to ask.
    * **Changed behind our back**: the recorded mode disagrees with the
      configured one outside a migration, which is what a migration that
      failed part-way leaves. `-reconfigure` adopts the current backend
      without copying -- the right move here, because the state that matters
      is whatever the backend we are dropping back to already holds.
    """
    fingerprint = _synced_config_fingerprint()
    mode = _active_backend_mode()
    # Both stamps live under `.terraform/`, which init creates and
    # `sync_terraform_config` deliberately leaves alone, so they track the
    # plugin cache's own lifetime: delete that directory and the next init
    # upgrades and reconfigures again, as it must.
    stamp = TERRAFORM_DIR / ".terraform" / "nyxgpt-config.sha256"
    mode_stamp = TERRAFORM_DIR / ".terraform" / "nyxgpt-backend.mode"
    try:
        upgrade = stamp.read_text(encoding="utf-8").strip() != fingerprint
    except OSError:
        upgrade = True
    try:
        previous_mode = mode_stamp.read_text(encoding="utf-8").strip()
    except OSError:
        previous_mode = ""

    arguments = ["init", "-input=false"]
    if upgrade:
        arguments.append("-upgrade")
    if migrate_state:
        arguments.extend(["-migrate-state", "-force-copy"])
    elif previous_mode and previous_mode != mode:
        arguments.append("-reconfigure")
    if mode == "local":
        arguments.append(f"-backend-config=path={TFSTATE_FILE}")
    _run_terraform(arguments, capture=True)

    # Recorded only after a successful init, so a failed one repeats the work.
    for path, value in ((stamp, fingerprint), (mode_stamp, mode)):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n", encoding="utf-8")
        except OSError:
            # These are caches, not state -- losing one costs an extra
            # `-upgrade`/`-reconfigure`, never correctness.
            pass


def terraform_outputs() -> dict[str, Any]:
    """Return `terraform output -json`, decoded to `{name: value}` (empty before first apply)."""
    try:
        completed = _run_terraform(["output", "-json"], capture=True)
    except CloudCommandError:
        return {}
    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {name: entry.get("value") for name, entry in raw.items() if isinstance(entry, dict)}


# --- Cloud state handoff (shared with `nyxgpt cloud allow-ip`) ---


def _load_cloud_state() -> dict[str, Any]:
    """Read the shared `~/.nyxGPT/cloud/state.json`, returning `{}` when absent/unreadable."""
    if not CLOUD_STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_cloud_state(outputs: dict[str, Any]) -> dict[str, Any]:
    """Merge the substrate's Terraform outputs into the shared cloud state file.

    Only `STATE_KEYS` are touched; anything else another `nyxgpt cloud`
    command wrote is preserved. This is what makes `nyxgpt cloud allow-ip`
    work with no arguments right after provisioning.
    """
    state = _load_cloud_state()
    for key in STATE_KEYS:
        if key in outputs and outputs[key] is not None:
            state[key] = outputs[key]
    CLOUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLOUD_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(CLOUD_STATE_FILE, 0o600)
    return state


def clear_cloud_state() -> None:
    """Drop this module's keys from the shared cloud state after a destroy."""
    state = _load_cloud_state()
    if not state:
        CLOUD_STATE_FILE.unlink(missing_ok=True)
        return
    remaining = {k: v for k, v in state.items() if k not in STATE_KEYS}
    if remaining:
        CLOUD_STATE_FILE.write_text(json.dumps(remaining, indent=2) + "\n", encoding="utf-8")
        os.chmod(CLOUD_STATE_FILE, 0o600)
    else:
        CLOUD_STATE_FILE.unlink(missing_ok=True)


# --- Operations (shared by the CLI and the admin dashboard API) ---


def _prepare(args: argparse.Namespace) -> InfraSettings:
    """Sync the configuration, resolve+persist settings, write tfvars, and init Terraform."""
    sync_terraform_config()
    settings = resolve_settings(args)
    save_settings(settings)
    write_tfvars(settings)
    terraform_init()
    return settings


def plan_infra(args: argparse.Namespace) -> dict[str, Any]:
    """Produce a saved Terraform plan for the substrate. Creates nothing."""
    settings = _prepare(args)
    _run_terraform(
        [
            "plan",
            "-input=false",
            f"-var-file={TFVARS_FILE}",
            f"-out={PLAN_FILE}",
        ]
    )
    return {"action": "plan", "settings": settings.to_dict(), "plan_file": str(PLAN_FILE)}


def apply_infra(args: argparse.Namespace) -> dict[str, Any]:
    """Provision (or reconcile) the substrate and record its outputs.

    Idempotent by construction -- Terraform reconciles against state, so a
    re-run with unchanged settings is a no-op apart from refreshing the
    owner-IP-scoped SSH rule.
    """
    settings = _prepare(args)
    _run_terraform(
        [
            "apply",
            "-input=false",
            "-auto-approve",
            f"-var-file={TFVARS_FILE}",
        ]
    )
    outputs = terraform_outputs()
    state = write_cloud_state(outputs)
    return {"action": "apply", "settings": settings.to_dict(), "outputs": outputs, "state": state}


def destroy_infra(args: argparse.Namespace) -> dict[str, Any]:
    """Tear the substrate down and drop its entries from the shared cloud state."""
    sync_terraform_config()
    if not TFSTATE_FILE.exists():
        raise CloudCommandError(
            f"No substrate state at {TFSTATE_FILE} -- nothing to destroy. "
            "(`nyxgpt cloud infra apply` creates it.)"
        )
    # Deliberately the saved settings rather than `resolve_settings`: a
    # teardown must not depend on detecting the owner's current public IP (the
    # network may be exactly what's broken) or on an SSH key being configured.
    settings = saved_settings() or resolve_settings(args)
    write_tfvars(settings)
    terraform_init()
    _run_terraform(
        [
            "destroy",
            "-input=false",
            "-auto-approve",
            f"-var-file={TFVARS_FILE}",
        ]
    )
    clear_cloud_state()
    return {"action": "destroy", "settings": settings.to_dict()}


def test_infra() -> dict[str, Any]:
    """Run the plan-level test suite (terraform/aws/tests) offline.

    The same gate CI runs, wrapped so an operator never types `terraform
    test`. Nothing is created and no AWS account is used.
    """
    sync_terraform_config()
    _run_terraform(["init", "-input=false", "-backend=false"], capture=True)
    _run_terraform(["test"], extra_env=_OFFLINE_TEST_ENV)
    return {"action": "test", "passed": True}


# Where a substrate answer came from. The distinction is the whole point of
# #3804: a dashboard served *from* the instance has no Terraform state, so
# deriving facts from state alone made it report "not provisioned" while
# running on the provisioned machine.
SOURCE_IMDS = "imds"
SOURCE_TERRAFORM_STATE = "terraform-state"
SOURCE_UNKNOWN = "none"

SOURCE_LABELS = {
    SOURCE_IMDS: "instance metadata (this dashboard is running on the instance)",
    SOURCE_TERRAFORM_STATE: "Terraform state on this machine",
    SOURCE_UNKNOWN: "no source available on this machine",
}


def infra_status() -> dict[str, Any]:
    """Report what is provisioned, without touching AWS or requiring Terraform.

    Answers from whichever source can actually see the substrate from here:

    * **On an EC2 instance** -- instance metadata (IMDSv2), which describes
      the machine this process is running on rather than a state file's
      intent, and is available with no checkout, tfstate or credential.
    * **On the workstation that provisioned it** -- the Terraform outputs
      recorded in `~/.nyxGPT/cloud/state.json`. This is the only source that
      knows about an instance the local machine is not itself.
    * **Neither** -- `known` is False and the caller must say *unknown*. A
      machine that has never provisioned anything and is not an instance has
      no answer to give, and reporting "not provisioned" there would be an
      assertion about AWS that nothing here checked.

    Both sources are local reads, so this stays cheap enough for the
    dashboard to poll and still answers when credentials have expired.
    """
    settings = load_settings()
    facts = cloud_imds.instance_facts()

    if facts is not None:
        source = SOURCE_IMDS
        known = True
        provisioned = True
        # The instance can see everything about itself except which CIDR its
        # security group admits -- that is a rule, not metadata. Left empty
        # and reported as not-visible rather than guessed at.
        owner_ip_cidr = ""
        values: dict[str, Any] = dict(facts)
    else:
        state = _load_cloud_state()
        source = SOURCE_TERRAFORM_STATE
        provisioned = bool(state.get("instance_id"))
        # "Not provisioned" is only an answer when this machine has actually
        # run Terraform for the substrate; otherwise there is nothing here
        # that could know either way.
        known = provisioned or TFSTATE_FILE.exists() or bool(settings)
        if not known:
            source = SOURCE_UNKNOWN
        owner_ip_cidr = settings.get("owner_ip_cidr") or ""
        values = {
            "region": state.get("region") or settings.get("aws_region") or "",
            "instance_id": state.get("instance_id") or "",
            "instance_type": state.get("instance_type") or settings.get("instance_type") or "",
            "public_ip": state.get("public_ip") or "",
            "private_ip": state.get("private_ip") or "",
            "vpc_id": state.get("vpc_id") or "",
            "subnet_id": state.get("subnet_id") or "",
            "security_group_id": state.get("security_group_id") or "",
            "ssh_key_name": state.get("ssh_key_name") or "",
        }

    return {
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "on_ec2": source == SOURCE_IMDS,
        "known": known,
        "provisioned": provisioned,
        "config_synced": TERRAFORM_DIR.is_dir(),
        "state_file": str(TFSTATE_FILE),
        "state_file_exists": TFSTATE_FILE.exists(),
        "region": values.get("region") or "",
        "instance_id": values.get("instance_id") or "",
        "instance_type": values.get("instance_type") or "",
        "public_ip": values.get("public_ip") or "",
        "private_ip": values.get("private_ip") or "",
        "vpc_id": values.get("vpc_id") or "",
        "subnet_id": values.get("subnet_id") or "",
        "security_group_id": values.get("security_group_id") or "",
        "ssh_key_name": values.get("ssh_key_name") or "",
        "owner_ip_cidr": owner_ip_cidr,
        # The access model is a property of the configuration, not of a live
        # lookup: the security group has exactly one inbound rule and the
        # Terraform config refuses to represent any other shape. It is
        # therefore reported for a provisioned substrate whichever source
        # identified it -- and withheld when there is no substrate to
        # describe.
        "access_model": {
            "open_ports": [22] if provisioned else [],
            "ssh_only": True,
            "world_open_ingress": False,
            "reachability": (
                "SSH tunnel to loopback-bound services "
                "(product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md)"
            ),
        },
    }


# --- CLI entry points ---


def _print_summary(result: dict[str, Any]) -> None:
    """Print the human-readable result of an apply."""
    outputs = result.get("outputs") or {}
    print("AWS substrate applied.")
    for label, key in (
        ("region", "region"),
        ("vpc", "vpc_id"),
        ("security group", "security_group_id"),
        ("instance", "instance_id"),
        ("public IP", "public_ip"),
    ):
        if outputs.get(key):
            print(f"  {label}: {outputs[key]}")
    print(
        "\nThe instance exposes SSH (port 22) to your IP and nothing else -- the app and "
        "observability UIs bind 127.0.0.1 and are reached over an SSH tunnel.\n"
        "If your public IP changes later, run `nyxgpt cloud allow-ip`."
    )


def infra_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud infra <subcommand>` entry point."""
    subcommand = getattr(args, "infra_cmd", "")
    try:
        if subcommand == "plan":
            plan_infra(args)
            print(
                "\nPlan complete. Nothing has been created -- run "
                "`nyxgpt cloud infra apply` to provision it."
            )
        elif subcommand == "apply":
            _print_summary(apply_infra(args))
        elif subcommand == "destroy":
            if not getattr(args, "yes", False):
                print(
                    "Refusing to destroy the AWS substrate without --yes. This deletes the "
                    "instance and its root volume; any data only on that box is lost.",
                    file=sys.stderr,
                )
                return 1
            destroy_infra(args)
            print("AWS substrate destroyed.")
        elif subcommand == "test":
            test_infra()
            print("\nPlan-level substrate tests passed. Nothing was created.")
        elif subcommand == "status":
            print(json.dumps(infra_status(), indent=2))
        else:  # pragma: no cover - argparse enforces the choices
            raise CloudCommandError(f"Unknown `nyxgpt cloud infra` subcommand {subcommand!r}")
    except CloudCommandError as exc:
        print(f"nyxgpt cloud infra {subcommand}: {exc}", file=sys.stderr)
        return 1
    return 0
