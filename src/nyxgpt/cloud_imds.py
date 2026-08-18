"""EC2 instance metadata (IMDSv2) reads for the AWS substrate panel (#3804).

The Infrastructure page's AWS section used to derive every substrate fact
from Terraform state. That is correct on the workstation that ran
`nyxgpt cloud deploy` and wrong everywhere else: a dashboard served *from*
the provisioned instance has no Terraform state, so it read "not
provisioned" while running on the very machine Terraform had just created
(owner observation, 2026-08-16, rc12).

The way out is to make the data source depend on where the UI is running.
This module answers the "am I on EC2, and if so which instance am I?" half:
it describes the *actual running machine* rather than a state file's intent,
and it works on a box that has never seen a checkout, a tfstate or an AWS
credential.

Three properties matter to the callers:

* **IMDSv2 only.** The Terraform compute module sets
  `http_tokens = "required"` (`terraform/aws/modules/compute/main.tf`), so a
  token-less read is refused. Every request here carries a session token
  obtained by the `PUT /latest/api/token` handshake.
* **Unreachable IMDS is "not on EC2", never an error.** A workstation, a
  container, a CI runner and a hop-limited pod all fail the same way, and
  none of them is a fault to report; the caller falls back to Terraform
  state (and, failing that, reports "unknown").
* **Answers are cached.** The link-local address black-holes rather than
  refusing on most non-EC2 machines, so an uncached miss costs the full
  timeout. Negative answers are cached exactly like positive ones -- the
  polled status endpoints must not pay that timeout on every request.
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any

# The link-local metadata endpoint. Not configurable: it is fixed by EC2, and
# a "metadata host" setting would only ever be a way to point this at
# something that is not the instance we are running on.
IMDS_BASE = "http://169.254.169.254/latest"
TOKEN_URL = f"{IMDS_BASE}/api/token"

# Short enough that a hung link-local route cannot stall a status request for
# long, generous enough for the real thing (IMDS answers in single-digit
# milliseconds on the instance itself).
DEFAULT_TIMEOUT = 1.0

# The token only has to outlive the handful of reads below.
TOKEN_TTL_SECONDS = 60

# Substrate facts do not change while an instance runs -- an instance cannot
# change its id, type, VPC or key pair -- so this can be long. It bounds how
# often a workstation pays the miss, which is the cost that matters.
CACHE_TTL_SECONDS = 300.0

# `(expires_at, facts_or_None)`. Module-level rather than per-call so the two
# status endpoints that both ask (substrate and deployment) share one answer.
_cache: tuple[float, dict[str, str] | None] | None = None


def reset_cache() -> None:
    """Forget any cached answer. Used by tests and by explicit re-reads."""
    global _cache
    _cache = None


def _read(path: str, token: str, timeout: float) -> str:
    """GET one metadata path with the session token, or "" when unavailable.

    A 404 is a normal answer here, not a failure: an instance with no public
    IP has no `public-ipv4` node at all.
    """
    request = urllib.request.Request(
        f"{IMDS_BASE}/{path}",
        headers={"X-aws-ec2-metadata-token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body: bytes = response.read()
    except Exception:
        return ""
    return body.decode("utf-8", errors="replace").strip()


def _session_token(timeout: float) -> str:
    """Perform the IMDSv2 handshake, returning "" when IMDS is not reachable."""
    request = urllib.request.Request(
        TOKEN_URL,
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": str(TOKEN_TTL_SECONDS)},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body: bytes = response.read()
    except Exception:
        return ""
    return body.decode("utf-8", errors="replace").strip()


def _ssh_key_name(token: str, timeout: float) -> str:
    """Return the attached key pair's name, from the `0=name` public-key index."""
    listing = _read("meta-data/public-keys/", token, timeout)
    if not listing:
        return ""
    first = listing.splitlines()[0]
    _, separator, name = first.partition("=")
    return (name if separator else "").strip()


def read_metadata(timeout: float = DEFAULT_TIMEOUT) -> dict[str, str] | None:
    """Read this machine's substrate facts from IMDS, or None when not on EC2.

    Uncached -- `instance_facts` is what callers should use.
    """
    token = _session_token(timeout)
    if not token:
        return None
    instance_id = _read("meta-data/instance-id", token, timeout)
    if not instance_id:
        # A token but no instance id means something is answering on the
        # link-local address that is not EC2 metadata. Treat it as not-EC2
        # rather than reporting half an instance.
        return None

    facts = {
        "instance_id": instance_id,
        "region": _read("meta-data/placement/region", token, timeout),
        "instance_type": _read("meta-data/instance-type", token, timeout),
        "public_ip": _read("meta-data/public-ipv4", token, timeout),
        "private_ip": _read("meta-data/local-ipv4", token, timeout),
        "availability_zone": _read("meta-data/placement/availability-zone", token, timeout),
        "vpc_id": "",
        "subnet_id": "",
        "security_group_id": "",
        "ssh_key_name": _ssh_key_name(token, timeout),
    }

    # VPC, subnet and security groups hang off the primary interface's MAC.
    mac = _read("meta-data/mac", token, timeout)
    if mac:
        interface = f"meta-data/network/interfaces/macs/{mac}"
        facts["vpc_id"] = _read(f"{interface}/vpc-id", token, timeout)
        facts["subnet_id"] = _read(f"{interface}/subnet-id", token, timeout)
        # Newline-separated when an instance has several; the substrate has
        # exactly one, but never silently drop the others.
        groups = _read(f"{interface}/security-group-ids", token, timeout)
        facts["security_group_id"] = ", ".join(groups.split())

    return facts


def instance_facts(
    *, timeout: float = DEFAULT_TIMEOUT, force: bool = False
) -> dict[str, str] | None:
    """Cached `read_metadata`. None means "this machine is not an EC2 instance"."""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and _cache[0] > now:
        return _cache[1]
    facts = read_metadata(timeout=timeout)
    _cache = (now + CACHE_TTL_SECONDS, facts)
    return facts


def on_ec2(**kwargs: Any) -> bool:
    """True when this process is running on an EC2 instance."""
    return instance_facts(**kwargs) is not None
