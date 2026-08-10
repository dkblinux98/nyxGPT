"""AWS cloud lifecycle helpers for `nyxgpt cloud` (P6-11-class CLI surface).

Currently implements `nyxgpt cloud allow-ip` (#3630): refreshing the
owner-IP-scoped SSH (port 22) security-group ingress rule described in
`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md` -- the private-
access mechanism's one open port is pinned to the owner's current public IP,
and that IP can churn (ISP renewal, travel, tethering), locking the owner
out of the instance (including SSH itself) until the rule is refreshed. This
command talks only to the AWS EC2 API (never the instance), so it works even
while fully locked out.

AWS provisioning itself (`nyxgpt cloud deploy`/`destroy`, P6-11/#3513, and
the AWS Terraform module, P6-8) is separate, not-yet-implemented scope. This
module identifies the target security group via `--security-group-id`/
`--region`, or by reading `CLOUD_STATE_FILE` -- the contract future
`nyxgpt cloud deploy` work should write to (`{"security_group_id": ...,
"region": ...}`) so `allow-ip` keeps working unmodified once that lands.

`nyxgpt cloud user-data` (P6-12/#3511, `nyxgpt.cloud_provision`) is a
separate module: it renders the EC2 user-data bootstrap script the
not-yet-implemented deploy/Terraform work above will eventually embed as an
instance's `user_data`.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from nyxgpt.optional_imports import try_import

NYXGPT_HOME = Path.home() / ".nyxGPT"

# Written by future `nyxgpt cloud deploy` work; read here as a fallback when
# --security-group-id/--region aren't passed explicitly.
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
    """Resolve the AWS region from `--region`, else the cloud state file, else `None`."""
    explicit = getattr(args, "region", None)
    if explicit:
        return str(explicit)
    region = _load_cloud_state().get("region")
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


def _get_ec2_client(region: str | None) -> Any:
    """Build a boto3 EC2 client, raising a clean error if boto3 isn't installed."""
    boto3 = try_import("boto3")
    if boto3 is None:
        raise CloudCommandError(
            "boto3 is required for `nyxgpt cloud` commands. Install with `pip install nyxgpt[cloud]`."
        )
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    try:
        return boto3.client("ec2", **kwargs)
    except Exception as exc:
        raise CloudCommandError(f"Failed to create an AWS EC2 client: {exc}") from exc


def _describe_ssh_ingress_cidrs(ec2_client: Any, security_group_id: str) -> list[str]:
    """Return every IPv4 CIDR currently allowed by `security_group_id`'s port-22 TCP rule(s)."""
    try:
        response = ec2_client.describe_security_groups(GroupIds=[security_group_id])
    except Exception as exc:
        raise CloudCommandError(
            f"Failed to describe security group {security_group_id}: {exc}"
        ) from exc
    groups = response.get("SecurityGroups", [])
    if not groups:
        raise CloudCommandError(f"Security group {security_group_id} not found")

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
    ec2_client: Any, security_group_id: str, new_cidr: str
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
    """
    old_cidrs = _describe_ssh_ingress_cidrs(ec2_client, security_group_id)
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
        explicit_ip = getattr(args, "ip", None)
        new_cidr = (
            normalize_cidr(explicit_ip)
            if explicit_ip
            else normalize_cidr(detect_current_public_ip())
        )
        ec2_client = _get_ec2_client(region)
        old_cidrs, changed = refresh_ssh_ingress_rule(ec2_client, security_group_id, new_cidr)
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
