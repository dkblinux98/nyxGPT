"""AWS cloud lifecycle helpers for `nyxgpt cloud` (P6-11-class CLI surface).

Currently implements `nyxgpt cloud allow-ip` (#3630): refreshing the
owner-IP-scoped SSH (port 22) security-group ingress rule described in
`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md` -- the private-
access mechanism's one open port is pinned to the owner's current public IP,
and that IP can churn (ISP renewal, travel, tethering), locking the owner
out of the instance (including SSH itself) until the rule is refreshed. This
command talks only to the AWS EC2 API (never the instance), so it works even
while fully locked out.

AWS provisioning itself lives in sibling modules: the Terraform substrate in
`nyxgpt.cloud_infra` (P6-8/#3509) and the one-command deploy in
`nyxgpt.cloud_deploy` (P6-11/#3513). This module identifies the target
security group via `--security-group-id`/`--region`, or by reading
`CLOUD_STATE_FILE` -- the handoff `nyxgpt cloud infra apply` writes
(`{"security_group_id": ..., "region": ...}`) -- so `allow-ip` needs no
arguments after a deploy.

**Credentials (#3993).** Every AWS client built here resolves its profile the
way the substrate commands do -- `--profile`, then the profile the last
`cloud infra apply` recorded in `infra.json`, then config.ini `[cloud]
profile`, then `AWS_PROFILE`, then boto3's own default chain (`_resolve_profile`).
Building a bare `boto3.client` instead meant `[cloud] profile` was ignored and
the query ran in whatever account the workstation's *default* profile names, so
a security group that plainly existed came back `InvalidGroup.NotFound` -- to
an operator who, being locked out, could not check. That is why a not-found
here names the account and profile it queried rather than reporting a bare
absence.

`nyxgpt cloud user-data` (P6-12/#3511, `nyxgpt.cloud_provision`) renders the
per-target-OS bootstrap script. `nyxgpt cloud deploy --os` is what delivers
it to an instance (#3867); the substrate still attaches no Terraform
`user_data`.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from nyxgpt.optional_imports import try_import

NYXGPT_HOME = Path.home() / ".nyxGPT"

# Written by `nyxgpt cloud infra apply` (and so by `nyxgpt cloud deploy`);
# read here as a fallback when --security-group-id/--region aren't passed.
CLOUD_STATE_FILE = NYXGPT_HOME / "cloud" / "state.json"

# AWS's own plain-text IP echo service -- no third-party dependency, and a
# reasonable choice given this command only ever runs against AWS.
IP_ECHO_URL = "https://checkip.amazonaws.com"

SSH_PORT = 22

# Stamped on the rule this command manages so it's identifiable in the AWS
# console/CLI, separate from any other port-22 rule an operator might add.
SSH_RULE_DESCRIPTION = "nyxgpt-cloud-allow-ip: owner SSH access"

# Refused outright, regardless of --ip -- the one CIDR that would defeat the
# owner-IP-scoping this command exists to maintain.
_OPEN_CIDR = "0.0.0.0/0"


class CloudCommandError(RuntimeError):
    """Raised for an `allow-ip` failure that should print a clean CLI message, not a traceback."""


def _load_cloud_state() -> dict[str, Any]:
    """Read `CLOUD_STATE_FILE`, returning `{}` if it's missing or unparseable."""
    if not CLOUD_STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(CLOUD_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_security_group_id(args: argparse.Namespace) -> str:
    """Resolve the target security group id from `--security-group-id`, else the cloud state file."""
    explicit = getattr(args, "security_group_id", None)
    if explicit:
        return str(explicit)
    sg_id = _load_cloud_state().get("security_group_id")
    if sg_id:
        return str(sg_id)
    raise CloudCommandError(
        "No security group id given and none found in "
        f"{CLOUD_STATE_FILE} (written by `nyxgpt cloud deploy`). "
        "Pass --security-group-id explicitly."
    )


def _resolve_region(args: argparse.Namespace) -> str | None:
    """Resolve the AWS region from `--region`, the cloud state file, config.ini, else `None`.

    The config.ini step matches `cloud_infra.resolve_settings` (#3993): a
    region reachable by the substrate commands but not by this one is the same
    class of gap as the profile, and produces the same wrong-place lookup.
    `None` still means "let boto3 resolve it", which is the honest answer when
    nothing here knows.
    """
    explicit = getattr(args, "region", None)
    if explicit:
        return str(explicit)
    region = (
        _load_cloud_state().get("region")
        or _saved_infra_settings().get("aws_region")
        or _configured_cloud_reference().get("region")
    )
    return str(region) if region else None


def detect_current_public_ip(timeout: float = 5.0) -> str:
    """Detect the caller's current public IPv4 address via AWS's checkip service."""
    try:
        response = httpx.get(IP_ECHO_URL, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CloudCommandError(
            f"Failed to detect current public IP via {IP_ECHO_URL}: {exc}"
        ) from exc
    ip = response.text.strip()
    try:
        ipaddress.IPv4Address(ip)
    except ValueError as exc:
        raise CloudCommandError(f"Unexpected response from {IP_ECHO_URL}: {ip!r}") from exc
    return ip


def normalize_cidr(ip_or_cidr: str) -> str:
    """Normalize a bare IPv4 address or CIDR to a CIDR string.

    A bare address (no `/`) is scoped to `/32`. An explicit CIDR is kept
    as passed, except `0.0.0.0/0` -- which is always refused, since it would
    defeat the owner-IP-scoping this command exists to maintain.
    """
    value = ip_or_cidr.strip()
    if "/" not in value:
        value = f"{value}/32"
    try:
        network = ipaddress.IPv4Network(value, strict=False)
    except ValueError as exc:
        raise CloudCommandError(f"Invalid IPv4 address/CIDR {ip_or_cidr!r}: {exc}") from exc
    cidr = str(network)
    if cidr == _OPEN_CIDR:
        raise CloudCommandError(
            f"Refusing to open the SSH rule to {_OPEN_CIDR} -- pass a specific address or CIDR."
        )
    return cidr


def _configured_cloud_reference() -> dict[str, str]:
    """Return config.ini's `[cloud]` profile/region reference, or empty strings on failure.

    Mirrors `cloud_infra._configured_cloud_reference`. Imported lazily because
    this module is imported *by* `cloud_infra` (and through it by most of the
    `nyxgpt cloud` surface): a module-scope import of the config stack would
    close an import cycle. A missing or invalid config.ini must never block the
    lockout-recovery command -- every value it supplies also has a flag and an
    environment fallback.
    """
    try:
        from nyxgpt import aws_credentials_setup
        from nyxgpt import config as config_mod

        return aws_credentials_setup.cloud_reference_status(config_mod.load_config())
    except Exception:
        return {"profile": "", "region": ""}


def _saved_infra_settings() -> dict[str, Any]:
    """Return what the last `cloud infra apply` recorded (`infra.json`), or `{}`."""
    try:
        from nyxgpt import cloud_infra

        return cloud_infra.load_settings()
    except Exception:
        return {}


def _resolve_profile(args: argparse.Namespace) -> str:
    """Resolve the AWS profile by the same documented order the substrate commands use.

    `--profile` > the profile the last `cloud infra apply` recorded
    (`infra.json`) > config.ini `[cloud] profile` > `AWS_PROFILE` > `""`
    (boto3's own default chain). That is exactly
    `cloud_infra.resolve_settings`'s order, and matching it is the whole point
    (#3993): until this existed every client built here authenticated through
    boto3's default chain alone, so an operator whose *default* profile names a
    different account got `InvalidGroup.NotFound` for a security group that
    plainly exists -- reported, at that moment, to someone locked out of the
    instance and unable to check.
    """
    return str(
        getattr(args, "profile", None)
        or _saved_infra_settings().get("aws_profile")
        or _configured_cloud_reference().get("profile")
        or os.environ.get("AWS_PROFILE")
        or ""
    )


def _get_ec2_client(region: str | None, profile: str = "") -> Any:
    """Build a boto3 EC2 client for `profile`, raising a clean error if boto3 isn't installed.

    A named profile goes through `boto3.Session(profile_name=...)` (#3993):
    the bare `boto3.client` this used unconditionally consults only the
    process environment, so a configured `[cloud] profile` was silently
    dropped and every call landed in whichever account the workstation's
    default profile names. With no profile resolved, the bare form is kept --
    it *is* boto3's default session, and going through an explicit one would
    change nothing except which code path a credential bug hides in.
    """
    boto3 = try_import("boto3")
    if boto3 is None:
        raise CloudCommandError(
            "boto3 is required for `nyxgpt cloud` commands. Install with `pip install nyxgpt[cloud]`."
        )
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    try:
        if profile:
            return boto3.Session(profile_name=profile).client("ec2", **kwargs)
        return boto3.client("ec2", **kwargs)
    except Exception as exc:
        suffix = f" for profile {profile!r}" if profile else ""
        raise CloudCommandError(f"Failed to create an AWS EC2 client{suffix}: {exc}") from exc


def describe_credential_context(region: str | None, profile: str = "") -> str:
    """Return a short "AWS account 1234, profile 'x'" description of who the caller is.

    Best-effort and never fatal: this only ever *annotates* another error, and
    an STS call that fails (expired credentials, no network) must not replace
    the failure the operator is actually being told about. The profile is named
    even when the account lookup fails, because "which profile did I just use"
    is the half of the answer that identifies a credential-resolution mistake.
    """
    profile_label = f"profile {profile!r}" if profile else "no profile (boto3's default chain)"
    boto3 = try_import("boto3")
    if boto3 is None:
        return profile_label
    try:
        kwargs: dict[str, Any] = {"region_name": region} if region else {}
        client = (
            boto3.Session(profile_name=profile).client("sts", **kwargs)
            if profile
            else boto3.client("sts", **kwargs)
        )
        account = str(client.get_caller_identity().get("Account", ""))
    except Exception:
        return f"{profile_label}; the account it resolves to could not be determined"
    if not account:
        return profile_label
    return f"AWS account {account}, {profile_label}"


def _error_code(exc: Exception) -> str:
    """Extract botocore's `Error.Code` from a ClientError, or `''` for anything else.

    Read off the response dict rather than by catching typed botocore
    exceptions, so this module keeps working on an install without boto3 --
    the same reason `cloud_state._error_code` does it this way.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def _not_found_detail(describe_credentials: Any) -> str:
    """Return the "which account did I even ask?" sentence appended to a NotFound error.

    `InvalidGroup.NotFound` has two very different causes and, until #3993,
    one message: the group is gone, or the call went to the wrong account.
    Told the first when the truth was the second -- while locked out of the
    instance, which is the only time this command runs -- an operator reads it
    as "my infrastructure has been destroyed" and goes looking for a disaster
    that did not happen. Naming the account and profile the query actually used
    makes a credential-resolution mistake self-evident and costs one STS call
    on the failure path only.
    """
    context = describe_credentials() if callable(describe_credentials) else ""
    if not context:
        return ""
    return (
        f" -- that lookup ran against {context}. If the group exists in a different "
        "account, this is a credential-resolution problem and not a destroyed "
        "substrate: set `[cloud] profile` in ~/.nyxGPT/config.ini (or pass "
        "`nyxgpt cloud allow-ip --profile <name>`), then re-run. "
        "`nyxgpt cloud status` reports the substrate this machine has recorded."
    )


def _describe_ssh_ingress_cidrs(
    ec2_client: Any, security_group_id: str, describe_credentials: Any = None
) -> list[str]:
    """Return every IPv4 CIDR currently allowed by `security_group_id`'s port-22 TCP rule(s).

    `describe_credentials` is an optional zero-argument callable returning a
    description of the credentials in use. It is invoked only when the lookup
    fails with a not-found -- see `_not_found_detail` for why, and so that the
    happy path never pays for an STS round trip.
    """
    try:
        response = ec2_client.describe_security_groups(GroupIds=[security_group_id])
    except Exception as exc:
        detail = (
            _not_found_detail(describe_credentials)
            if _error_code(exc) == "InvalidGroup.NotFound"
            else ""
        )
        raise CloudCommandError(
            f"Failed to describe security group {security_group_id}: {exc}{detail}"
        ) from exc
    groups = response.get("SecurityGroups", [])
    if not groups:
        raise CloudCommandError(
            f"Security group {security_group_id} not found"
            + _not_found_detail(describe_credentials)
        )

    cidrs: list[str] = []
    for permission in groups[0].get("IpPermissions", []):
        if permission.get("IpProtocol") != "tcp":
            continue
        if permission.get("FromPort") != SSH_PORT or permission.get("ToPort") != SSH_PORT:
            continue
        for ip_range in permission.get("IpRanges", []):
            cidr = ip_range.get("CidrIp")
            if cidr:
                cidrs.append(cidr)
    return cidrs


def refresh_ssh_ingress_rule(
    ec2_client: Any,
    security_group_id: str,
    new_cidr: str,
    describe_credentials: Any = None,
) -> tuple[list[str], bool]:
    """Point `security_group_id`'s port-22 ingress rule at `new_cidr`.

    Authorizes `new_cidr` (if not already present) *before* revoking any
    other port-22 CIDR currently allowed, so the security group is never
    left without a valid SSH source between the two calls -- if the
    authorize call fails, the stale rule(s) are left untouched instead of
    the group being left with zero SSH ingress. Returns
    `(old_cidrs, changed)` -- `changed` is `False` when `new_cidr` was
    already the only allowed source (idempotent no-op, no AWS API mutation
    calls made).

    `describe_credentials` is forwarded to the describe call and used only to
    annotate a not-found failure (#3993).
    """
    old_cidrs = _describe_ssh_ingress_cidrs(ec2_client, security_group_id, describe_credentials)
    if old_cidrs == [new_cidr]:
        return old_cidrs, False

    if new_cidr not in old_cidrs:
        try:
            ec2_client.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": SSH_PORT,
                        "ToPort": SSH_PORT,
                        "IpRanges": [{"CidrIp": new_cidr, "Description": SSH_RULE_DESCRIPTION}],
                    }
                ],
            )
        except Exception as exc:
            raise CloudCommandError(
                f"Failed to authorize new SSH ingress rule {new_cidr}: {exc}"
            ) from exc

    stale = [cidr for cidr in old_cidrs if cidr != new_cidr]
    if stale:
        try:
            ec2_client.revoke_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": SSH_PORT,
                        "ToPort": SSH_PORT,
                        "IpRanges": [{"CidrIp": cidr} for cidr in stale],
                    }
                ],
            )
        except Exception as exc:
            raise CloudCommandError(
                f"New SSH ingress rule {new_cidr} was authorized, but failed to revoke "
                f"stale rule(s) {stale}: {exc}. Both the new and stale CIDRs are now "
                "allowed -- re-run `nyxgpt cloud allow-ip` to retry the cleanup."
            ) from exc

    return old_cidrs, True


def allow_ip(args: argparse.Namespace) -> int:
    """`nyxgpt cloud allow-ip` entry point: refresh the SSH ingress rule to the current owner IP."""
    try:
        security_group_id = _resolve_security_group_id(args)
        region = _resolve_region(args)
        # Resolved once and reused for both the EC2 client and the failure
        # annotation, so the two can never disagree about which identity was
        # used (#3993).
        profile = _resolve_profile(args)
        explicit_ip = getattr(args, "ip", None)
        new_cidr = (
            normalize_cidr(explicit_ip)
            if explicit_ip
            else normalize_cidr(detect_current_public_ip())
        )
        ec2_client = _get_ec2_client(region, profile)
        old_cidrs, changed = refresh_ssh_ingress_rule(
            ec2_client,
            security_group_id,
            new_cidr,
            lambda: describe_credential_context(region, profile),
        )
    except CloudCommandError as exc:
        print(f"nyxgpt cloud allow-ip: {exc}", file=sys.stderr)
        return 1

    old_display = ", ".join(old_cidrs) if old_cidrs else "(none)"
    if not changed:
        print(
            f"Security group {security_group_id}: SSH already allowed from {new_cidr} "
            "-- no change."
        )
        return 0

    print(f"Security group {security_group_id}: SSH ingress rule updated.")
    print(f"  old: {old_display}")
    print(f"  new: {new_cidr}")
    return 0
