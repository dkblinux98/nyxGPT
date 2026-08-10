"""Terraform remote state for the AWS substrate (P6-9, #3510).

`nyxgpt cloud infra` (P6-8, #3509) keeps the substrate's Terraform state in a
single local file, `~/.nyxGPT/cloud/terraform.tfstate`. That is correct for
one operator on one machine and wrong for everything else: a second operator
(or a CI runner) applying the same substrate has no way to see the first
one's state, and two concurrent applies can interleave into a corrupted
state file with no warning. This module moves that state to an S3 bucket with
a DynamoDB lock table -- shared, versioned, and mutually exclusive.

**The bootstrap problem.** The bucket and lock table cannot themselves be
managed by the state they hold: Terraform needs the backend to exist before
it can initialize, so a configuration that created its own backend could
never run the first time. They are therefore created out-of-band with boto3
(already a `nyxgpt[cloud]` dependency), idempotently, before the backend is
switched. `bootstrap_backend` is safe to re-run: it adopts resources that
already exist rather than failing on them.

**How the switch happens.** `terraform/aws/backend.tf` is a file of its own
precisely so this module can replace it. `nyxgpt cloud state migrate` writes
the S3 flavor into the synced, ops-managed copy under
`~/.nyxGPT/cloud/terraform/` and re-runs init with `-migrate-state
-force-copy`, which copies the existing local state up to S3. Nothing is
text-edited and the provider/version pins are never touched.

**Recovery.** Bucket versioning is enabled at creation -- not as a nicety but
because it is the entire recovery story for a corrupted or truncated state
object: every write keeps its predecessor, `state versions` lists them, and
`state restore` pushes one back. A crashed apply that leaves the DynamoDB
lock held is cleared with `state unlock`. See docs/cloud.md ("Remote state").

Per CLAUDE.md's wrapper requirement, no user flow here prints or expects a
raw `terraform` command: `nyxgpt cloud state
{status,bootstrap,migrate,unlock,backup,versions,restore,local}` owns the
whole lifecycle, and the admin dashboard drives the same functions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import cloud_infra
from nyxgpt.cloud import CloudCommandError
from nyxgpt.optional_imports import try_import

# Records which backend the substrate is configured for. Read by `status` and
# by `cloud_infra.terraform_init`, which must not pass a local `-backend-config`
# path once state lives in S3.
BACKEND_FILE = cloud_infra.CLOUD_DIR / "backend.json"

# Name of the file inside the synced Terraform configuration that carries the
# `backend` block, and nothing else. See terraform/aws/backend.tf.
BACKEND_TF_NAME = "backend.tf"

# The object key the substrate's state occupies inside the bucket. Fixed
# rather than configurable: one nyxGPT deployment has exactly one substrate,
# and a per-install key would make "which object do I restore?" a question
# the operator has to answer during an outage.
DEFAULT_STATE_KEY = "nyxgpt/aws/terraform.tfstate"

# DynamoDB's lock table has a fixed shape -- Terraform writes an item whose
# partition key is `LockID` and expects exactly that attribute name.
LOCK_TABLE_HASH_KEY = "LockID"

DEFAULT_TABLE_NAME = "nyxgpt-tfstate-locks"

# S3 bucket names are globally unique across every AWS account, so the
# account id is part of the default -- otherwise the first nyxGPT install
# anywhere would take `nyxgpt-tfstate` and every later one would fail with a
# confusing "bucket already exists" from a stranger's account.
BUCKET_PREFIX = "nyxgpt-tfstate"


@dataclass
class BackendSettings:
    """Where remote state lives, once it does. Persisted to `backend.json`."""

    bucket: str
    table: str
    region: str
    key: str = DEFAULT_STATE_KEY
    profile: str = ""
    # False while the bucket/table exist but the configuration still points at
    # local state -- the window between `state bootstrap` and `state migrate`,
    # and where a failed migration leaves things.
    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, used for `backend.json`."""
        return {
            "bucket": self.bucket,
            "table": self.table,
            "region": self.region,
            "key": self.key,
            "profile": self.profile,
            "enabled": self.enabled,
        }


def load_backend_settings() -> BackendSettings | None:
    """Read `backend.json`, or `None` when remote state was never set up."""
    if not BACKEND_FILE.exists():
        return None
    try:
        loaded = json.loads(BACKEND_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not loaded.get("bucket") or not loaded.get("region"):
        return None
    return BackendSettings(
        bucket=str(loaded["bucket"]),
        table=str(loaded.get("table") or DEFAULT_TABLE_NAME),
        region=str(loaded["region"]),
        key=str(loaded.get("key") or DEFAULT_STATE_KEY),
        profile=str(loaded.get("profile", "")),
        enabled=bool(loaded.get("enabled", False)),
    )


def save_backend_settings(settings: BackendSettings) -> None:
    """Persist `settings` (0600 -- it names the account's state bucket)."""
    cloud_infra.CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND_FILE.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.chmod(BACKEND_FILE, 0o600)


def remote_state_enabled() -> bool:
    """True when the substrate's state is configured to live in S3."""
    settings = load_backend_settings()
    return settings is not None and settings.enabled


# --- AWS clients ---


def _client(service: str, region: str, profile: str = "") -> Any:
    """Build a boto3 client for `service`, with a clean error if boto3 is absent."""
    boto3 = try_import("boto3")
    if boto3 is None:
        raise CloudCommandError(
            "boto3 is required to provision the Terraform state backend. "
            "Install with `pip install nyxgpt[cloud]`."
        )
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return session.client(service, region_name=region)
    except Exception as exc:
        raise CloudCommandError(f"Failed to create an AWS {service} client: {exc}") from exc


def _error_code(exc: Exception) -> str:
    """Extract botocore's `Error.Code` from a ClientError, or '' for anything else.

    Read off the response dict rather than by catching typed botocore
    exceptions so this module keeps working when boto3 isn't installed --
    importing `botocore.exceptions` at module scope would break every other
    `nyxgpt cloud state` subcommand on a non-cloud install.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def aws_account_id(region: str, profile: str = "") -> str:
    """Return the account id the current credentials belong to."""
    client = _client("sts", region, profile)
    try:
        identity = client.get_caller_identity()
    except Exception as exc:
        raise CloudCommandError(
            "Could not identify the AWS account for the Terraform state backend "
            f"({exc}). Run `nyxgpt cloud credentials-setup` first."
        ) from exc
    account = str(identity.get("Account", ""))
    if not account:
        raise CloudCommandError("AWS returned no account id for the current credentials.")
    return account


def default_bucket_name(account_id: str, region: str) -> str:
    """The default state bucket name: unique per account and region."""
    return f"{BUCKET_PREFIX}-{account_id}-{region}"


# --- Backend provisioning (the out-of-band bootstrap) ---


def ensure_state_bucket(bucket: str, region: str, profile: str = "") -> list[str]:
    """Create (or adopt) the state bucket and enforce its required settings.

    Returns a human-readable list of what was done. Versioning, encryption,
    and the public-access block are applied unconditionally, including to a
    bucket that already existed: they are the difference between "we can
    recover the state" and "we cannot", and an adopted bucket someone created
    by hand is exactly the one likely to be missing them.
    """
    client = _client("s3", region, profile)
    actions: list[str] = []

    try:
        # us-east-1 is the API's default and rejects an explicit
        # LocationConstraint naming it -- every other region requires one.
        if region == "us-east-1":
            client.create_bucket(Bucket=bucket)
        else:
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        actions.append(f"created S3 bucket {bucket}")
    except Exception as exc:
        code = _error_code(exc)
        if code == "BucketAlreadyOwnedByYou":
            actions.append(f"S3 bucket {bucket} already exists")
        elif code == "BucketAlreadyExists":
            raise CloudCommandError(
                f"S3 bucket name {bucket!r} is already taken by another AWS account "
                "(bucket names are globally unique). Re-run with --bucket <unique-name>."
            ) from exc
        else:
            raise CloudCommandError(f"Failed to create S3 bucket {bucket}: {exc}") from exc

    try:
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        actions.append("enabled bucket versioning (state history for recovery)")
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to enable versioning on {bucket}: {exc}. Versioning is required -- "
            "without it a corrupted state write is unrecoverable."
        ) from exc

    try:
        client.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
                ]
            },
        )
        actions.append("enabled default server-side encryption (AES256)")
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to enable default encryption on {bucket}: {exc}. Terraform state "
            "carries resource ids and any sensitive outputs, so it is never left unencrypted."
        ) from exc

    try:
        client.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        actions.append("blocked all public access")
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to block public access on {bucket}: {exc}. A publicly readable state "
            "bucket is an immediate disclosure of the deployment's topology."
        ) from exc

    return actions


def ensure_lock_table(table: str, region: str, profile: str = "") -> list[str]:
    """Create (or adopt) the DynamoDB lock table and wait for it to be usable."""
    client = _client("dynamodb", region, profile)
    actions: list[str] = []

    try:
        client.create_table(
            TableName=table,
            AttributeDefinitions=[{"AttributeName": LOCK_TABLE_HASH_KEY, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": LOCK_TABLE_HASH_KEY, "KeyType": "HASH"}],
            # On-demand: the table sees a couple of writes per apply, so
            # provisioned capacity would bill for idle throughput all month.
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Project", "Value": "nyxGPT"},
                {"Key": "ManagedBy", "Value": "nyxgpt cloud state"},
            ],
        )
        actions.append(f"created DynamoDB lock table {table}")
    except Exception as exc:
        if _error_code(exc) == "ResourceInUseException":
            actions.append(f"DynamoDB lock table {table} already exists")
        else:
            raise CloudCommandError(f"Failed to create DynamoDB table {table}: {exc}") from exc

    # A table is CREATING for a few seconds after the call returns; an apply
    # that raced it would fail to take its lock.
    try:
        client.get_waiter("table_exists").wait(TableName=table)
    except Exception as exc:
        raise CloudCommandError(f"DynamoDB table {table} did not become active: {exc}") from exc

    return actions


def resolve_backend_settings(args: argparse.Namespace) -> BackendSettings:
    """Merge flags over saved backend settings, then over the substrate's own region.

    The region defaults to whatever `nyxgpt cloud infra` is provisioning into:
    keeping state in a different region than the substrate adds a second
    regional dependency to every apply for no benefit.
    """
    saved = load_backend_settings()
    infra = cloud_infra.load_settings()

    region = (
        getattr(args, "region", None)
        or (saved.region if saved else "")
        or infra.get("aws_region")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    profile = (
        getattr(args, "profile", None)
        or (saved.profile if saved else "")
        or infra.get("aws_profile")
        or os.environ.get("AWS_PROFILE")
        or ""
    )

    bucket = getattr(args, "bucket", None) or (saved.bucket if saved else "")
    if not bucket:
        bucket = default_bucket_name(aws_account_id(str(region), str(profile)), str(region))

    return BackendSettings(
        bucket=str(bucket),
        table=str(
            getattr(args, "table", None) or (saved.table if saved else "") or DEFAULT_TABLE_NAME
        ),
        region=str(region),
        key=str(getattr(args, "key", None) or (saved.key if saved else "") or DEFAULT_STATE_KEY),
        profile=str(profile),
        enabled=bool(saved.enabled) if saved else False,
    )


# --- Rendering the backend block ---


LOCAL_BACKEND_TF = """# Local state (the default). Rewritten by `nyxgpt cloud state migrate`.
terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
"""


def render_backend_tf(settings: BackendSettings) -> str:
    """Render the S3 `backend` block for the synced configuration.

    `dynamodb_table` is the lock mechanism this issue specifies and the one
    supported across the whole `required_version >= 1.9.0` range. Terraform
    1.11 introduced `use_lockfile` (an S3-native conditional-write lock) as
    its successor and warns on this argument; moving to it is a separate
    decision, since it changes which AWS permissions an operator needs.
    """
    lines = [
        "# Generated by `nyxgpt cloud state migrate` -- do not edit.",
        "# Regenerated whenever the configuration is re-synced; hand edits are lost.",
        "terraform {",
        '  backend "s3" {',
        f'    bucket         = "{settings.bucket}"',
        f'    key            = "{settings.key}"',
        f'    region         = "{settings.region}"',
        f'    dynamodb_table = "{settings.table}"',
        "    encrypt        = true",
    ]
    if settings.profile:
        lines.append(f'    profile        = "{settings.profile}"')
    lines.extend(["  }", "}", ""])
    return "\n".join(lines)


def write_backend_tf(settings: BackendSettings | None) -> Path:
    """Write `backend.tf` into the synced configuration and return its path.

    `None` restores the packaged local-state default. Called after every
    `sync_terraform_config`, which re-copies the packaged (local) file --
    without this the next init would silently drop back to local state.
    """
    target = cloud_infra.TERRAFORM_DIR / BACKEND_TF_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    content = LOCAL_BACKEND_TF if settings is None else render_backend_tf(settings)
    target.write_text(content, encoding="utf-8")
    return target


def apply_configured_backend() -> None:
    """Point the synced configuration at whichever backend is currently configured."""
    settings = load_backend_settings()
    write_backend_tf(settings if settings and settings.enabled else None)


# --- Operations ---


def bootstrap_backend(args: argparse.Namespace) -> dict[str, Any]:
    """Create the state bucket and lock table without switching the backend.

    Split out from `migrate` so the AWS-side resources can be created by
    someone with the permissions to create them, and the migration run later
    (or by CI) once they exist.
    """
    settings = resolve_backend_settings(args)
    actions = ensure_state_bucket(settings.bucket, settings.region, settings.profile)
    actions += ensure_lock_table(settings.table, settings.region, settings.profile)
    save_backend_settings(settings)
    return {
        "action": "bootstrap",
        "backend": settings.to_dict(),
        "actions": actions,
        "migrated": settings.enabled,
    }


def migrate_to_remote(args: argparse.Namespace) -> dict[str, Any]:
    """Bootstrap the backend if needed, then move existing state into S3.

    `-migrate-state -force-copy` is what copies the local state file up
    rather than starting from an empty remote state -- without `-force-copy`
    Terraform stops to ask interactively, which no wrapped command can answer.
    """
    settings = resolve_backend_settings(args)
    actions = ensure_state_bucket(settings.bucket, settings.region, settings.profile)
    actions += ensure_lock_table(settings.table, settings.region, settings.profile)

    # Record the pre-migration state file so the summary can tell the operator
    # whether anything was actually copied or the remote started empty.
    had_local_state = cloud_infra.TFSTATE_FILE.exists()

    # `backend.json` is written *before* init, not after: it is what
    # `sync_terraform_config` renders backend.tf from and what
    # `terraform_init` reads to decide whether a local state path applies.
    # If init then fails, the record is rolled back so it keeps describing
    # where the state actually is -- and the next command's sync repairs
    # backend.tf to match.
    previous = load_backend_settings()
    migrated = BackendSettings(**{**settings.to_dict(), "enabled": True})
    save_backend_settings(migrated)
    try:
        cloud_infra.sync_terraform_config()
        cloud_infra.terraform_init(migrate_state=True)
    except Exception:
        save_backend_settings(
            previous or BackendSettings(**{**settings.to_dict(), "enabled": False})
        )
        raise

    if had_local_state:
        actions.append(
            f"copied {cloud_infra.TFSTATE_FILE} into s3://{settings.bucket}/{settings.key}"
        )
    else:
        actions.append("no local state existed; the remote backend starts empty")

    return {
        "action": "migrate",
        "backend": migrated.to_dict(),
        "actions": actions,
        "migrated_local_state": had_local_state,
    }


def migrate_to_local() -> dict[str, Any]:
    """Move state back out of S3 into the local file (the reverse migration).

    The escape hatch for the case where the backend itself is the problem --
    an account lockout, a deleted bucket policy, a region outage. The bucket
    and lock table are deliberately left in place; this changes where
    Terraform reads state, not what exists in AWS.
    """
    settings = load_backend_settings()
    if settings is None or not settings.enabled:
        raise CloudCommandError(
            "Remote state is not in use -- the substrate's state is already the local file "
            f"{cloud_infra.TFSTATE_FILE}."
        )
    demoted = BackendSettings(**{**settings.to_dict(), "enabled": False})
    save_backend_settings(demoted)
    try:
        cloud_infra.sync_terraform_config()
        cloud_infra.terraform_init(migrate_state=True)
    except Exception:
        save_backend_settings(settings)
        raise
    return {
        "action": "local",
        "backend": demoted.to_dict(),
        "state_file": str(cloud_infra.TFSTATE_FILE),
    }


def _require_remote() -> BackendSettings:
    """Return the active remote backend, or explain that there isn't one."""
    settings = load_backend_settings()
    if settings is None or not settings.enabled:
        raise CloudCommandError(
            "Remote state is not configured. Run `nyxgpt cloud state migrate` first."
        )
    return settings


def unlock_state(lock_id: str) -> dict[str, Any]:
    """Release a DynamoDB lock left held by a crashed or killed run.

    Terraform holds the lock for the duration of a plan/apply and releases it
    on exit; a runner that is killed mid-apply never gets to, and every later
    run then fails with the lock's id. `-force` skips the interactive
    confirmation, which a wrapped command cannot answer -- the operator's
    confirmation is having passed the specific lock id.
    """
    _require_remote()
    if not lock_id.strip():
        raise CloudCommandError(
            "A lock id is required. Terraform prints it in the error that reports the lock "
            "(`Lock Info: ID: ...`)."
        )
    cloud_infra.sync_terraform_config()
    cloud_infra.terraform_init()
    cloud_infra.run_terraform(["force-unlock", "-force", lock_id.strip()], capture=True)
    return {"action": "unlock", "lock_id": lock_id.strip(), "unlocked": True}


def backup_state(destination: Path | None = None) -> dict[str, Any]:
    """Write the current state to a local file, whichever backend holds it.

    `state pull` rather than an S3 download so this works identically before
    and after migration, and so the output is the state Terraform itself
    would read.
    """
    cloud_infra.sync_terraform_config()
    cloud_infra.terraform_init()
    completed = cloud_infra.run_terraform(["state", "pull"], capture=True)
    payload = completed.stdout or ""
    if not payload.strip():
        raise CloudCommandError("Terraform returned empty state -- nothing was backed up.")

    target = (
        Path(destination) if destination else cloud_infra.CLOUD_DIR / "terraform.tfstate.backup"
    )
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    os.chmod(target, 0o600)
    return {"action": "backup", "path": str(target), "bytes": len(payload)}


def list_state_versions(limit: int = 20) -> dict[str, Any]:
    """List the state object's S3 versions, newest first.

    This is the recovery inventory: each entry is a complete state file as it
    stood after one apply, and `state restore --version-id` puts one back.
    """
    settings = _require_remote()
    client = _client("s3", settings.region, settings.profile)
    try:
        response = client.list_object_versions(Bucket=settings.bucket, Prefix=settings.key)
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to list versions of s3://{settings.bucket}/{settings.key}: {exc}"
        ) from exc

    versions: list[dict[str, Any]] = [
        {
            "version_id": str(entry.get("VersionId", "")),
            "last_modified": str(entry.get("LastModified", "")),
            "size": int(entry.get("Size", 0) or 0),
            "latest": bool(entry.get("IsLatest", False)),
        }
        for entry in response.get("Versions", [])
        if entry.get("Key") == settings.key
    ]
    # S3 returns versions newest-first already, but the ordering is only
    # documented per-key -- sorting on the timestamp makes it explicit.
    versions.sort(key=lambda v: str(v["last_modified"]), reverse=True)
    return {
        "action": "versions",
        "bucket": settings.bucket,
        "key": settings.key,
        "versions": versions[:limit],
        "truncated": len(versions) > limit,
    }


def restore_state_version(version_id: str) -> dict[str, Any]:
    """Restore a previous state version, becoming the new current state.

    The old version is downloaded and pushed back through Terraform rather
    than copied over the object in S3 directly: the backend keeps a checksum
    of the state in DynamoDB, and an out-of-band overwrite leaves that
    checksum describing the version that was replaced, so every later command
    fails the integrity check. `state push -force` updates both together.
    Nothing is lost either way -- the version being replaced remains in the
    bucket's history as its own version.
    """
    settings = _require_remote()
    if not version_id.strip():
        raise CloudCommandError("A version id is required -- see `nyxgpt cloud state versions`.")

    client = _client("s3", settings.region, settings.profile)
    try:
        response = client.get_object(
            Bucket=settings.bucket, Key=settings.key, VersionId=version_id.strip()
        )
        payload = response["Body"].read()
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to download version {version_id} of "
            f"s3://{settings.bucket}/{settings.key}: {exc}"
        ) from exc

    cloud_infra.sync_terraform_config()
    cloud_infra.terraform_init()

    with tempfile.NamedTemporaryFile("wb", suffix=".tfstate", delete=False) as handle:
        handle.write(payload)
        staged = Path(handle.name)
    try:
        cloud_infra.run_terraform(["state", "push", "-force", str(staged)], capture=True)
    finally:
        staged.unlink(missing_ok=True)

    return {
        "action": "restore",
        "bucket": settings.bucket,
        "key": settings.key,
        "version_id": version_id.strip(),
        "bytes": len(payload),
    }


def state_status(verify: bool = False) -> dict[str, Any]:
    """Report where the substrate's state lives and how recoverable it is.

    Offline by default -- it reads the recorded configuration only -- so the
    admin dashboard can poll it and so it still answers while AWS credentials
    are expired. `verify=True` additionally confirms the bucket and table
    really exist and that versioning is on, which is the check worth running
    before trusting the recovery story.
    """
    settings = load_backend_settings()
    enabled = bool(settings and settings.enabled)
    status: dict[str, Any] = {
        "backend": "s3" if enabled else "local",
        "remote_enabled": enabled,
        "bootstrapped": settings is not None,
        "bucket": settings.bucket if settings else "",
        "table": settings.table if settings else "",
        "key": settings.key if settings else "",
        "region": settings.region if settings else "",
        "profile": settings.profile if settings else "",
        "locking": "dynamodb" if enabled else "none (single local state file)",
        "local_state_file": str(cloud_infra.TFSTATE_FILE),
        "local_state_exists": cloud_infra.TFSTATE_FILE.exists(),
        "verified": False,
    }
    if not verify or settings is None:
        return status

    status["verified"] = True
    s3 = _client("s3", settings.region, settings.profile)
    try:
        s3.head_bucket(Bucket=settings.bucket)
        status["bucket_exists"] = True
    except Exception:
        status["bucket_exists"] = False
    try:
        versioning = s3.get_bucket_versioning(Bucket=settings.bucket)
        status["versioning_enabled"] = versioning.get("Status") == "Enabled"
    except Exception:
        status["versioning_enabled"] = False
    dynamodb = _client("dynamodb", settings.region, settings.profile)
    try:
        described = dynamodb.describe_table(TableName=settings.table)
        status["table_status"] = str(described.get("Table", {}).get("TableStatus", ""))
        status["table_exists"] = True
    except Exception:
        status["table_exists"] = False
        status["table_status"] = ""
    return status


# --- CLI entry point ---


def _print_actions(result: dict[str, Any]) -> None:
    """Print the step list a bootstrap/migrate produced."""
    for action in result.get("actions", []):
        print(f"  - {action}")


def state_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud state <subcommand>` entry point."""
    subcommand = getattr(args, "state_cmd", "")
    try:
        if subcommand == "status":
            print(json.dumps(state_status(verify=getattr(args, "verify", False)), indent=2))
        elif subcommand == "bootstrap":
            result = bootstrap_backend(args)
            backend = result["backend"]
            print(f"State backend ready in {backend['region']}:")
            _print_actions(result)
            print(
                "\nNothing has moved yet -- run `nyxgpt cloud state migrate` to point the "
                "substrate at it."
            )
        elif subcommand == "migrate":
            result = migrate_to_remote(args)
            backend = result["backend"]
            print(f"Remote state active: s3://{backend['bucket']}/{backend['key']}")
            _print_actions(result)
            print(
                f"\nLocking is via DynamoDB table {backend['table']}. Concurrent applies now "
                "block instead of racing.\nIf a run is killed mid-apply and leaves the lock "
                "held, clear it with `nyxgpt cloud state unlock --lock-id <id>`."
            )
        elif subcommand == "local":
            result = migrate_to_local()
            print(f"State moved back to the local file {result['state_file']}.")
            print(
                "The bucket and lock table were left in place -- re-run "
                "`nyxgpt cloud state migrate` to go back."
            )
        elif subcommand == "unlock":
            result = unlock_state(getattr(args, "lock_id", "") or "")
            print(f"Released lock {result['lock_id']}.")
        elif subcommand == "backup":
            destination = getattr(args, "output", None)
            result = backup_state(Path(destination) if destination else None)
            print(f"State backed up to {result['path']} ({result['bytes']} bytes).")
        elif subcommand == "versions":
            result = list_state_versions(limit=int(getattr(args, "limit", 20) or 20))
            if not result["versions"]:
                print(f"No stored versions of s3://{result['bucket']}/{result['key']} yet.")
            else:
                print(f"Versions of s3://{result['bucket']}/{result['key']} (newest first):")
                for version in result["versions"]:
                    marker = " (current)" if version["latest"] else ""
                    print(
                        f"  {version['version_id']}  {version['last_modified']}  "
                        f"{version['size']} bytes{marker}"
                    )
                print("\nRestore one with `nyxgpt cloud state restore --version-id <id>`.")
        elif subcommand == "restore":
            result = restore_state_version(getattr(args, "version_id", "") or "")
            print(
                f"Restored version {result['version_id']} ({result['bytes']} bytes) as the "
                "current state.\nRun `nyxgpt cloud infra plan` to see what Terraform now "
                "believes differs from AWS."
            )
        else:  # pragma: no cover - argparse enforces the choices
            raise CloudCommandError(f"Unknown `nyxgpt cloud state` subcommand {subcommand!r}")
    except CloudCommandError as exc:
        print(f"nyxgpt cloud state {subcommand}: {exc}", file=sys.stderr)
        return 1
    return 0
