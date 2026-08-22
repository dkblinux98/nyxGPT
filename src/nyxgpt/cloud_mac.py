"""Allocate, run and eventually release an EC2 Mac Dedicated Host (#3995).

`nyxgpt cloud deploy --os macos` used to refuse unless the operator handed it
a `--host` that already existed. Producing that Mac was left entirely to them:
raw `aws ec2 allocate-hosts`, `run-instances --placement Tenancy=host`, and
later `release-hosts` -- none of which appear anywhere in this repository, and
all of which are exactly the raw-operations flow CLAUDE.md's Operational
Command Wrapping requirement forbids. #3867 removed the console step for the
*bootstrap*; this module removes it for the *machine*.

**Why the old refusal no longer holds.** Its stated rationale was economic:
allocating a host would spend the operator's money "on a resource this
configuration cannot then tear down", because AWS bills an allocated Dedicated
Host for a 24-hour minimum and rejects `ReleaseHosts` inside that window. Two
things answer it:

* **Consent.** Irreversible spend is authorized by disclosure plus
  confirmation everywhere else in this CLI. So the allocation prints the
  family, the region and AZ, the **live** per-hour rate and 24-hour minimum
  from the Pricing API, and the moment the host becomes releasable -- then
  requires a typed word. `--yes` bypasses the typing, not the disclosure.
* **A release that does happen.** `nyxgpt cloud destroy` terminates the Mac
  immediately and hands the host release to a one-shot EventBridge Scheduler
  schedule (`terraform/aws/mac-release`) that fires after the window closes.
  The configuration *can* tear the host down -- just not synchronously.

**The cost table is queried, never hardcoded.** The spread across Mac families
is 2.4x (mac2 vs mac2-m2pro), so a constant in the source would be wrong for
most operators and silently stale for the rest. `lookup_host_pricing` asks the
Pricing API per family and region at prompt time; when it cannot answer, the
prompt says so in those words rather than substituting a number nobody checked.

**What is deliberately untouched:** the no-Docker / no-observability
constraint on the macOS target. EC2 Macs have no nested virtualization, so no
Docker daemon can exist there. That is a platform limit, not a scoping choice,
and nothing here revisits it -- see docs/cloud.md, "EC2 Mac targets".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nyxgpt import cloud_infra
from nyxgpt.cloud import CloudCommandError
from nyxgpt.optional_imports import try_import

# The two extra root modules inside the synced Terraform tree. Root modules,
# not child modules of the substrate: see terraform/aws/mac/versions.tf for
# why the isolation is the point rather than a filing convention.
MAC_CONFIG_DIRNAME = "mac"
MAC_RELEASE_CONFIG_DIRNAME = "mac-release"

# Each root gets its own local state file, outside the (re-synced, disposable)
# configuration directory for the same reason the substrate's does: an nyxGPT
# upgrade re-materializes the .tf sources and must never be able to clobber
# state. Deliberately not the substrate's S3 backend either -- these are torn
# down on different schedules, and sharing one state file would put the
# deferred host back in the way of the substrate's destroy.
MAC_TFSTATE_FILE = cloud_infra.CLOUD_DIR / "mac.tfstate"
MAC_RELEASE_TFSTATE_FILE = cloud_infra.CLOUD_DIR / "mac-release.tfstate"

MAC_TFVARS_FILE = cloud_infra.CLOUD_DIR / "mac.tfvars"
MAC_RELEASE_TFVARS_FILE = cloud_infra.CLOUD_DIR / "mac-release.tfvars"

# Keys this module owns inside the shared `~/.nyxGPT/cloud/state.json`.
# `cloud_infra.write_cloud_state`/`clear_cloud_state` touch only their own
# `STATE_KEYS` and preserve everything else, so a substrate teardown cannot
# take the record of a still-billing host with it.
STATE_KEYS: tuple[str, ...] = (
    "mac_host_id",
    "mac_instance_id",
    "mac_instance_type",
    "mac_region",
    "mac_availability_zone",
    "mac_public_ip",
    "mac_security_group_id",
    "mac_allocated_at",
    "mac_release_at",
    "mac_hourly_rate",
    "mac_release_scheduled",
    "mac_release_scheduled_at",
)

# EC2 bills an allocated Dedicated Host for at least this long, and refuses
# ReleaseHosts until it has elapsed. Not configurable: it is AWS's number.
HOST_MINIMUM_HOURS = 24

# Added on top of the 24-hour minimum when computing the schedule's fire time.
# Two reasons, both about not burning a one-shot schedule: clock skew between
# whatever recorded the allocation timestamp and EC2's own idea of it, and the
# post-termination *host scrub*, during which a release is rejected. The scrub
# is also absorbed inside the state machine (which can retry); this buffer is
# what keeps the first attempt from landing in the obviously-too-early window.
RELEASE_BUFFER_MINUTES = 30

# The word `nyxgpt cloud deploy --os macos` asks for before allocating. A word
# rather than y/N because this is the one deploy action that starts a bill the
# operator cannot stop for a day.
CONFIRMATION_WORD = "allocate"

# The Price List service has regional endpoints in only a few regions; the
# prices it returns are for whatever region the *query* names, not for the
# endpoint. So this is fixed and is not the region being priced.
PRICING_API_REGION = "us-east-1"

# Cheapest Mac family, and the default when nothing else says otherwise. The
# rate is still looked up -- this constant chooses hardware, not a price.
DEFAULT_MAC_INSTANCE_TYPE = "mac2.metal"

# EC2 Mac host billing is per-hour with a per-second granularity above the
# minimum; hours are what the Pricing API quotes and what the disclosure says.
SECONDS_PER_HOUR = 3600.0


@dataclass
class MacHostPricing:
    """What one Dedicated Host family costs, as the Pricing API answered.

    `hourly_rate is None` is a first-class outcome, not an error to swallow:
    an operator who cannot be told the price must be told *that*, and the
    consent prompt says so instead of printing a number nothing checked.
    """

    instance_type: str
    host_family: str
    region: str
    hourly_rate: float | None = None
    currency: str = "USD"
    error: str = ""

    @property
    def minimum_cost(self) -> float | None:
        """What the 24-hour minimum comes to, or `None` when the rate is unknown."""
        if self.hourly_rate is None:
            return None
        return self.hourly_rate * HOST_MINIMUM_HOURS

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, carried in the deploy result and the status payload."""
        return {
            "instance_type": self.instance_type,
            "host_family": self.host_family,
            "region": self.region,
            "hourly_rate": self.hourly_rate,
            "currency": self.currency,
            "minimum_hours": HOST_MINIMUM_HOURS,
            "minimum_cost": self.minimum_cost,
            "error": self.error,
        }


@dataclass
class MacAllocationPlan:
    """Everything the allocation needs, resolved before anything is billed."""

    instance_type: str
    region: str
    availability_zone: str
    profile: str = ""
    owner_ip_cidr: str = ""
    ssh_key_name: str = ""
    ssh_public_key: str = ""
    root_volume_size: int = 200
    name_prefix: str = "nyxgpt-mac"
    ami_id: str = ""
    pricing: MacHostPricing | None = None
    candidate_azs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for the deploy result (never the SSH key material)."""
        return {
            "instance_type": self.instance_type,
            "region": self.region,
            "availability_zone": self.availability_zone,
            "candidate_azs": list(self.candidate_azs),
            "root_volume_size": self.root_volume_size,
            "name_prefix": self.name_prefix,
            "ami_id": self.ami_id,
            "pricing": self.pricing.to_dict() if self.pricing else {},
        }


# --- AWS plumbing ------------------------------------------------------


def _client(service: str, region: str, profile: str = "") -> Any:
    """Build a boto3 client for `service`, with a clean error if boto3 is absent."""
    boto3 = try_import("boto3")
    if boto3 is None:
        raise CloudCommandError(
            "boto3 is required to price and place an EC2 Mac Dedicated Host. "
            "Install with `pip install nyxgpt[cloud]`."
        )
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return session.client(service, region_name=region)
    except Exception as exc:
        raise CloudCommandError(f"Failed to create an AWS {service} client: {exc}") from exc


def host_family(instance_type: str) -> str:
    """Return the Dedicated Host family a Mac instance type belongs to.

    `mac2.metal` -> `mac2`, `mac2-m2pro.metal` -> `mac2-m2pro`. The Pricing
    API prices the *host*, which is named by the family, while EC2 places the
    *instance*, which is named by the type -- so both spellings are needed and
    they are never interchangeable.
    """
    return instance_type.strip().lower().split(".", 1)[0]


def lookup_host_pricing(instance_type: str, region: str, profile: str = "") -> MacHostPricing:
    """Ask the Pricing API what one hour of this Dedicated Host family costs.

    Never raises: a pricing lookup that fails must not stop an operator who
    knows what they are doing, and it must not be silently replaced with a
    guess either. The failure is carried in `error` and the consent prompt
    prints it verbatim.

    The lowest non-zero on-demand rate is taken when several price dimensions
    come back. Zero-rated dimensions are dropped deliberately: the *instance*
    on a Dedicated Host genuinely costs $0.00/hr (you pay for the host), so a
    naive minimum would confidently report the host as free.
    """
    family = host_family(instance_type)
    pricing = MacHostPricing(instance_type=instance_type, host_family=family, region=region)
    try:
        client = _client("pricing", PRICING_API_REGION, profile)
        response = client.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Dedicated Host"},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": family},
                # `regionCode` rather than `location`: the latter wants the
                # region's marketing long name ("US East (N. Virginia)"),
                # which would put a region-name table in this file for the
                # Pricing API to disagree with later.
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            ],
            MaxResults=100,
        )
    except Exception as exc:
        pricing.error = f"the AWS Pricing API could not be queried: {exc}"
        return pricing

    rates: list[float] = []
    for blob in response.get("PriceList", []):
        try:
            document = json.loads(blob) if isinstance(blob, str) else blob
        except (TypeError, json.JSONDecodeError):
            continue
        terms = (document or {}).get("terms", {}).get("OnDemand", {})
        for term in terms.values():
            for dimension in (term or {}).get("priceDimensions", {}).values():
                raw = (dimension or {}).get("pricePerUnit", {}).get("USD")
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    rates.append(value)

    if not rates:
        pricing.error = (
            f"the AWS Pricing API returned no on-demand rate for Dedicated Host family "
            f"{family!r} in {region}"
        )
        return pricing
    pricing.hourly_rate = min(rates)
    return pricing


def mac_capable_azs(instance_type: str, region: str, profile: str = "") -> list[str]:
    """Return the AZs in `region` that offer `instance_type`, sorted.

    Queried rather than assumed: Mac capacity is per-AZ and differs by family
    (in us-east-1 on 2026-08-22, `mac2.metal` was offered in 1a/1b/1c/1d while
    `mac2-m2.metal` was 1c/1d only). Allocating into an AZ that does not offer
    the family fails at `AllocateHosts` -- cheap, but only if the failure is
    the one an operator can read.
    """
    client = _client("ec2", region, profile)
    zones: set[str] = set()
    try:
        paginator = client.get_paginator("describe_instance_type_offerings")
        pages = paginator.paginate(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )
        for page in pages:
            for offering in page.get("InstanceTypeOfferings", []):
                location = str(offering.get("Location") or "")
                if location:
                    zones.add(location)
    except Exception as exc:
        raise CloudCommandError(
            f"Could not ask EC2 which availability zones in {region} offer {instance_type}: "
            f"{exc}. Run `nyxgpt cloud credentials-setup` first, or pass --mac-az to name one."
        ) from exc
    return sorted(zones)


# --- Timing ------------------------------------------------------------


def utc_now() -> datetime:
    """Current UTC time, as an aware datetime. A seam for the tests."""
    return datetime.now(UTC)


def release_time(allocated_at: datetime) -> datetime:
    """When the deferred release should be attempted for a host allocated then.

    Allocation + AWS's 24-hour minimum + `RELEASE_BUFFER_MINUTES`. Truncated
    to whole seconds because EventBridge Scheduler's `at()` expression takes
    `YYYY-MM-DDTHH:MM:SS` and rejects a fractional part.
    """
    fires = allocated_at.astimezone(UTC) + timedelta(
        hours=HOST_MINIMUM_HOURS, minutes=RELEASE_BUFFER_MINUTES
    )
    return fires.replace(microsecond=0)


def scheduler_timestamp(moment: datetime) -> str:
    """Render `moment` as an EventBridge Scheduler `at()` timestamp (UTC, naive).

    The expression carries no timezone suffix -- `schedule_expression_timezone
    = "UTC"` on the schedule is what says which zone it is in -- so a trailing
    `Z` or `+00:00` here is rejected by the API.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from cloud state, or `None` if unusable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def accrued_cost(allocated_at: datetime, hourly_rate: float, now: datetime | None = None) -> float:
    """Bill accrued on a host allocated at `allocated_at`, floored at the minimum.

    Floored rather than pro-rated from zero because that is how AWS bills it:
    a host released the moment its window closes still costs 24 hours, so a
    status line that counted up from zero would understate what is already
    owed for the first day.
    """
    elapsed_hours = (
        (now or utc_now()) - allocated_at.astimezone(UTC)
    ).total_seconds() / SECONDS_PER_HOUR
    return hourly_rate * max(float(HOST_MINIMUM_HOURS), elapsed_hours)


# --- Consent -----------------------------------------------------------


def format_allocation_disclosure(plan: MacAllocationPlan, releasable_at: datetime) -> str:
    """The block printed before anything is allocated.

    Everything an operator needs to decide, in the order they need it: what is
    being created, where, what it costs, and -- the part nothing else in AWS
    tells you until it is too late -- that they cannot stop paying for it for
    a day.
    """
    pricing = plan.pricing or MacHostPricing(
        instance_type=plan.instance_type, host_family=host_family(plan.instance_type), region=""
    )
    if pricing.hourly_rate is not None:
        rate_line = (
            f"${pricing.hourly_rate:.4f}/hour ({pricing.currency}, live from the AWS Pricing API)"
        )
        minimum = pricing.minimum_cost or 0.0
        minimum_line = f"${minimum:.2f} for the {HOST_MINIMUM_HOURS}-hour minimum, charged even if you destroy in a minute"
    else:
        rate_line = f"UNKNOWN -- {pricing.error or 'the rate could not be looked up'}"
        minimum_line = (
            f"UNKNOWN -- with no rate there is no estimate. The {HOST_MINIMUM_HOURS}-hour "
            "minimum applies regardless."
        )

    lines = [
        "`--os macos` needs an EC2 Mac, and an EC2 Mac needs a Dedicated Host.",
        "nyxGPT can allocate one now. Read this first -- it is a real, non-refundable charge:",
        "",
        f"  Host family        {pricing.host_family} (instance type {plan.instance_type})",
        f"  Region / AZ        {plan.region} / {plan.availability_zone}",
        f"  Rate               {rate_line}",
        f"  Minimum charge     {minimum_line}",
        f"  Releasable at      {scheduler_timestamp(releasable_at)} UTC",
        "",
        "The instance itself is $0.00/hour -- on a Dedicated Host you pay for the host.",
        "AWS refuses to release a host before that timestamp, so `nyxgpt cloud destroy` will",
        "terminate the Mac immediately and schedule the host release for then, reporting the",
        "outcome to Slack. `nyxgpt cloud status` shows the pending release until it fires.",
    ]
    if plan.candidate_azs and len(plan.candidate_azs) > 1:
        others = ", ".join(az for az in plan.candidate_azs if az != plan.availability_zone)
        lines.append(f"Other zones offering {plan.instance_type}: {others} (--mac-az to choose).")
    return "\n".join(lines)


def confirm_allocation(
    disclosure: str,
    *,
    assume_yes: bool = False,
    reader: Any = None,
) -> None:
    """Print the disclosure and require the confirmation word. Raises if declined.

    `assume_yes` (the CLI's `--yes`) skips the typing, not the disclosure:
    a scripted run still leaves the numbers in its log, which is the whole
    value of printing them. Consistent with the rest of the CLI, so this path
    stays scriptable.
    """
    print(disclosure)
    if assume_yes:
        print("\nProceeding without confirmation (--yes).")
        return
    prompt = (
        f"\nType `{CONFIRMATION_WORD}` to allocate the Dedicated Host, or anything else to stop: "
    )
    try:
        answer = (reader or input)(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise CloudCommandError(
            "No confirmation was given, so nothing was allocated and nothing is billed. "
            "Pass --yes to allocate from a non-interactive run."
        ) from exc
    if str(answer).strip().lower() != CONFIRMATION_WORD:
        raise CloudCommandError(
            "Not confirmed -- nothing was allocated and nothing is billed.\n"
            f"Re-run and type `{CONFIRMATION_WORD}`, pass --yes to skip the prompt, or point "
            "the deploy at a Mac you already have with `--host <address>`."
        )


# --- Cloud-state record ------------------------------------------------


def _load_cloud_state() -> dict[str, Any]:
    """Read the shared `~/.nyxGPT/cloud/state.json`, returning `{}` when absent."""
    path = cloud_infra.CLOUD_STATE_FILE
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_cloud_state(state: dict[str, Any]) -> None:
    """Write the shared cloud state file back (0600 -- it names the deployment)."""
    path = cloud_infra.CLOUD_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def record_mac_host(values: dict[str, Any]) -> dict[str, Any]:
    """Merge this module's keys into the shared cloud state and return the record.

    Only `STATE_KEYS` are touched, mirroring `cloud_infra.write_cloud_state`:
    the substrate and the Mac write the same file and neither may erase the
    other's entries -- least of all the entry that says money is still being
    spent.
    """
    state = _load_cloud_state()
    for key in STATE_KEYS:
        if key in values and values[key] is not None:
            state[key] = values[key]
    _save_cloud_state(state)
    return load_mac_record()


def load_mac_record() -> dict[str, Any]:
    """Return what is recorded about the Mac host, or `{}` when there is none."""
    state = _load_cloud_state()
    record = {key: state[key] for key in STATE_KEYS if key in state}
    return record if record.get("mac_host_id") else {}


def clear_mac_record() -> None:
    """Drop this module's keys from the shared cloud state.

    Called only once the host is *gone* -- not when the instance is
    terminated. A record deleted while the host still bills is the exact
    failure `nyxgpt cloud status`'s pending-release row exists to prevent.
    """
    state = _load_cloud_state()
    if not state:
        return
    remaining = {k: v for k, v in state.items() if k not in STATE_KEYS}
    if remaining:
        _save_cloud_state(remaining)
    else:
        cloud_infra.CLOUD_STATE_FILE.unlink(missing_ok=True)


def pending_release() -> dict[str, Any]:
    """What `nyxgpt cloud status` reports about a host that is still billing.

    Empty dict when nothing is outstanding. A resource that still costs money
    must not be the one thing nothing observes (Definition of Done, the
    observability rule) -- and the host outlives both the instance and the
    substrate by construction, so no other status source can see it.
    """
    record = load_mac_record()
    if not record:
        return {}
    allocated_at = parse_timestamp(str(record.get("mac_allocated_at") or ""))
    release_at = parse_timestamp(str(record.get("mac_release_at") or ""))
    now = utc_now()
    try:
        rate = float(record.get("mac_hourly_rate") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0

    accrued: float | None = None
    if allocated_at is not None and rate > 0:
        accrued = accrued_cost(allocated_at, rate, now)

    releasable = bool(release_at and now >= release_at)
    return {
        "host_id": str(record.get("mac_host_id") or ""),
        "instance_id": str(record.get("mac_instance_id") or ""),
        "instance_type": str(record.get("mac_instance_type") or ""),
        "region": str(record.get("mac_region") or ""),
        "availability_zone": str(record.get("mac_availability_zone") or ""),
        "allocated_at": str(record.get("mac_allocated_at") or ""),
        "release_at": str(record.get("mac_release_at") or ""),
        "release_scheduled": bool(record.get("mac_release_scheduled")),
        "hourly_rate": rate or None,
        "accrued_cost": accrued,
        "currency": "USD",
        "releasable_now": releasable,
        "billing": True,
    }


# --- Terraform drivers -------------------------------------------------


def _config_dir(name: str) -> Path:
    """Path to one of the Mac root modules inside the synced Terraform tree."""
    return cloud_infra.TERRAFORM_DIR / name


def _write_tfvars(path: Path, values: dict[str, Any]) -> Path:
    """Render `values` as a tfvars file at `path` (0600) and return it.

    0600 because the release stack's vars carry the Slack bot token as an
    `Authorization` header value.
    """
    lines = [
        "# Generated by `nyxgpt cloud deploy --os macos` / `nyxgpt cloud destroy`.",
        "# Regenerated on every run; hand edits are lost.",
        "",
    ]
    for key, value in values.items():
        if value == "":
            continue
        lines.append(f"{key} = {cloud_infra._hcl_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _init(config_dir: Path, state_file: Path) -> None:
    """`terraform init` one of the Mac roots against its own local state file."""
    cloud_infra.run_terraform(
        ["init", "-input=false", f"-backend-config=path={state_file}"],
        capture=True,
        chdir=config_dir,
    )


def _outputs(config_dir: Path) -> dict[str, Any]:
    """`terraform output -json` for one of the Mac roots, decoded to `{name: value}`."""
    try:
        completed = cloud_infra.run_terraform(["output", "-json"], capture=True, chdir=config_dir)
    except CloudCommandError:
        return {}
    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {name: entry.get("value") for name, entry in raw.items() if isinstance(entry, dict)}


def apply_mac_host(plan: MacAllocationPlan) -> dict[str, Any]:
    """Allocate the Dedicated Host and launch the Mac on it. Idempotent.

    Terraform, so a re-run of `nyxgpt cloud deploy --os macos` reconciles the
    same host and instance rather than allocating a second one -- which on
    this resource would be a second 24-hour minimum.
    """
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_CONFIG_DIRNAME)
    _write_tfvars(
        MAC_TFVARS_FILE,
        {
            "aws_region": plan.region,
            "aws_profile": plan.profile,
            "name_prefix": plan.name_prefix,
            "availability_zone": plan.availability_zone,
            "mac_instance_type": plan.instance_type,
            "mac_ami_id": plan.ami_id,
            "owner_ip_cidr": plan.owner_ip_cidr,
            "ssh_key_name": plan.ssh_key_name,
            "ssh_public_key": plan.ssh_public_key,
            "root_volume_size": plan.root_volume_size,
        },
    )
    _init(config_dir, MAC_TFSTATE_FILE)
    cloud_infra.run_terraform(
        ["apply", "-input=false", "-auto-approve", f"-var-file={MAC_TFVARS_FILE}"],
        chdir=config_dir,
    )
    return _outputs(config_dir)


def mac_state_exists() -> bool:
    """True when a Mac root state file is present (a host was applied from here)."""
    return MAC_TFSTATE_FILE.exists()


def forget_host() -> bool:
    """Drop `aws_ec2_host.this` from the Mac root's state. Returns False if absent.

    This is what makes the deferred release possible at all: with the resource
    still in state, `terraform destroy` would call ReleaseHosts inside the
    24-hour window, AWS would reject it, and the destroy would half-fail --
    leaving the instance terminated, the network half-deleted, and the
    operator believing the teardown failed rather than that it deliberately
    left one resource behind.
    """
    config_dir = _config_dir(MAC_CONFIG_DIRNAME)
    try:
        cloud_infra.run_terraform(
            ["state", "rm", "aws_ec2_host.this"], capture=True, chdir=config_dir
        )
    except CloudCommandError:
        # Already gone -- a re-run of destroy, or a state file that never had
        # it. Not an error: the postcondition ("Terraform will not try to
        # release the host") already holds.
        return False
    return True


def destroy_mac_instance(plan_values: dict[str, Any]) -> dict[str, Any]:
    """Terminate the Mac and delete its network, leaving the host allocated.

    The tfvars the *apply* wrote are reused when they are still there, and
    only rebuilt from the record when they are not. Same reasoning as
    `cloud_infra.destroy_infra`'s saved settings, one step further: those
    values are exactly what created the resources being destroyed, whereas a
    rebuild has to re-derive an availability zone that has no default and
    would fail the run if the record never captured one.
    """
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_CONFIG_DIRNAME)
    if not MAC_TFVARS_FILE.exists():
        _write_tfvars(MAC_TFVARS_FILE, plan_values)
    _init(config_dir, MAC_TFSTATE_FILE)
    forgot = forget_host()
    cloud_infra.run_terraform(
        ["destroy", "-input=false", "-auto-approve", f"-var-file={MAC_TFVARS_FILE}"],
        chdir=config_dir,
    )
    return {"instance_terminated": True, "host_forgotten": forgot}


def apply_release_schedule(
    *,
    host_id: str,
    release_at: datetime,
    region: str,
    profile: str,
    slack_channel: str,
    slack_bot_token: str,
    name_prefix: str = "nyxgpt-mac",
) -> dict[str, Any]:
    """Create the one-shot schedule that releases `host_id` once it can be released."""
    if not slack_bot_token.strip():
        raise CloudCommandError(
            "No Slack bot token is configured, and the deferred host release reports its "
            "outcome over Slack -- hours after this teardown, when nothing else is watching.\n"
            "Set `[monitoring] slack_bot_token` in ~/.nyxGPT/config.ini (`nyxgpt config wizard`) "
            "and re-run `nyxgpt cloud destroy --yes`.\n"
            f"The host {host_id} is still allocated and still billing until then."
        )
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_RELEASE_CONFIG_DIRNAME)
    _write_tfvars(
        MAC_RELEASE_TFVARS_FILE,
        {
            "aws_region": region,
            "aws_profile": profile,
            "name_prefix": name_prefix,
            "host_id": host_id,
            "release_at": scheduler_timestamp(release_at),
            "slack_channel": slack_channel,
            "slack_authorization_header": f"Bearer {slack_bot_token.strip()}",
        },
    )
    _init(config_dir, MAC_RELEASE_TFSTATE_FILE)
    cloud_infra.run_terraform(
        ["apply", "-input=false", "-auto-approve", f"-var-file={MAC_RELEASE_TFVARS_FILE}"],
        chdir=config_dir,
    )
    outputs = _outputs(config_dir)
    return {
        "host_id": str(outputs.get("host_id") or host_id),
        "release_at": str(outputs.get("release_at") or scheduler_timestamp(release_at)),
        "schedule_name": str(outputs.get("schedule_name") or ""),
        "state_machine_arn": str(outputs.get("state_machine_arn") or ""),
        "slack_channel": str(outputs.get("slack_channel") or slack_channel),
    }


def destroy_release_stack() -> bool:
    """Tear down a completed release stack. Returns False when there is none.

    The schedule deletes itself when it fires, but the state machine, the
    EventBridge connection and the Secrets Manager secret EventBridge creates
    for it do not -- and that secret is the one piece of this design that
    keeps costing (cents per month) after the host is gone. Called at the top
    of the next teardown rather than exposed as a command of its own: the only
    moment nyxGPT can be sure the previous release finished is the next time
    it is asked to schedule one.
    """
    if not MAC_RELEASE_TFSTATE_FILE.exists():
        return False
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_RELEASE_CONFIG_DIRNAME)
    _init(config_dir, MAC_RELEASE_TFSTATE_FILE)
    cloud_infra.run_terraform(
        ["destroy", "-input=false", "-auto-approve", f"-var-file={MAC_RELEASE_TFVARS_FILE}"],
        capture=True,
        chdir=config_dir,
    )
    return True


def host_still_allocated(host_id: str, region: str, profile: str = "") -> bool | None:
    """Is `host_id` still allocated? `None` when AWS could not be asked.

    Three answers, not two, and the third matters: expired credentials must
    not be reported as "the host is gone", which would be the one wrong answer
    that stops an operator looking for a resource that is still billing.
    """
    if not host_id:
        return False
    try:
        client = _client("ec2", region, profile)
        response = client.describe_hosts(HostIds=[host_id])
    except Exception:
        return None
    for host in response.get("Hosts", []):
        if str(host.get("HostId") or "") != host_id:
            continue
        # `released` and `released-permanent-failure` are terminal; anything
        # else (available, under-assessment, pending) is still allocated.
        return not str(host.get("State") or "").startswith("released")
    return False


# --- Resolution + the two lifecycle entry points -----------------------


def resolve_mac_instance_type(args: argparse.Namespace) -> str:
    """Decide which EC2 Mac type to allocate a host for.

    `--mac-instance-type` wins, then `--instance-type` when it names a Mac
    (that flag is already how `--os auto` detects a macOS deploy at all), then
    whatever `~/.nyxGPT/cloud/infra.json` remembers if *it* names a Mac, then
    the cheapest family. The saved value is only honoured when it is a Mac
    type: the substrate's default is a general-purpose Linux size
    (`cloud_infra.DEFAULT_INSTANCE_TYPE`, `m5.xlarge` since #3992) and reading
    it here would ask EC2 for a Dedicated Host of a family that cannot boot
    macOS.
    """
    from nyxgpt import cloud_deploy

    explicit = str(getattr(args, "mac_instance_type", None) or "").strip().lower()
    if explicit:
        if cloud_deploy.instance_type_os_family(explicit) != cloud_deploy.OS_FAMILY_MACOS:
            raise CloudCommandError(
                f"{explicit!r} is not an EC2 Mac instance type. macOS runs only on "
                "mac1.metal (Intel) and the mac2* Apple Silicon types, all of which are "
                "`.metal` -- EC2 Macs are always bare metal."
            )
        return explicit
    for candidate in (
        str(getattr(args, "instance_type", None) or ""),
        str(cloud_infra.load_settings().get("instance_type") or ""),
    ):
        normalized = candidate.strip().lower()
        if normalized and cloud_deploy.instance_type_os_family(normalized) == (
            cloud_deploy.OS_FAMILY_MACOS
        ):
            return normalized
    return DEFAULT_MAC_INSTANCE_TYPE


def resolve_allocation_plan(args: argparse.Namespace) -> MacAllocationPlan:
    """Resolve everything the allocation needs, without allocating anything.

    Deliberately network-heavy and side-effect-free: it prices the host, asks
    which zones offer the family, and detects the operator's address, so the
    consent prompt is built from what AWS says now rather than from constants.
    Nothing here bills.
    """
    settings = cloud_infra.resolve_settings(args)
    instance_type = resolve_mac_instance_type(args)
    region = settings.aws_region
    profile = settings.aws_profile

    zones = mac_capable_azs(instance_type, region, profile)
    if not zones:
        raise CloudCommandError(
            f"No availability zone in {region} offers {instance_type}. EC2 Mac capacity is "
            "per-region and per-family -- pick another region with `--region`, another family "
            "with `--mac-instance-type`, or point the deploy at a Mac you already have with "
            "`--host <address>`."
        )
    requested = str(getattr(args, "mac_az", None) or "").strip()
    if requested and requested not in zones:
        raise CloudCommandError(
            f"{requested!r} does not offer {instance_type}. Zones that do, in {region}: "
            f"{', '.join(zones)}."
        )

    # Explicit --root-volume-size only: the substrate's 100 GiB default is
    # sized for Amazon Linux, and a macOS AMI's own snapshot is larger than
    # that on some families -- a launch that fails on volume size would fail
    # after the host is allocated and billing.
    requested_volume = getattr(args, "root_volume_size", None)
    return MacAllocationPlan(
        instance_type=instance_type,
        region=region,
        availability_zone=requested or zones[0],
        profile=profile,
        owner_ip_cidr=settings.owner_ip_cidr,
        ssh_key_name=settings.ssh_key_name,
        ssh_public_key=settings.ssh_public_key,
        root_volume_size=int(requested_volume) if requested_volume else 200,
        name_prefix=f"{settings.name_prefix}-mac",
        ami_id=str(getattr(args, "mac_ami_id", None) or ""),
        pricing=lookup_host_pricing(instance_type, region, profile),
        candidate_azs=zones,
    )


def _plan_tfvars(plan: MacAllocationPlan) -> dict[str, Any]:
    """The tfvars the Mac root takes, from a resolved plan."""
    return {
        "aws_region": plan.region,
        "aws_profile": plan.profile,
        "name_prefix": plan.name_prefix,
        "availability_zone": plan.availability_zone,
        "mac_instance_type": plan.instance_type,
        "mac_ami_id": plan.ami_id,
        "owner_ip_cidr": plan.owner_ip_cidr,
        "ssh_key_name": plan.ssh_key_name,
        "ssh_public_key": plan.ssh_public_key,
        "root_volume_size": plan.root_volume_size,
    }


def allocate(args: argparse.Namespace, *, assume_yes: bool = False) -> dict[str, Any]:
    """Price, confirm, allocate and launch. The `--os macos` half of `cloud deploy`.

    Returns the step record `cloud_deploy.deploy` folds into its own -- the
    host id, the instance, the address to SSH to, and the release timestamp
    the teardown will schedule against.

    Re-entrant: an existing recorded host with live Terraform state is
    reconciled (a re-deploy of the same Mac) rather than re-priced and
    re-confirmed. Asking an operator to re-consent to a charge they already
    made would train them to type the word without reading it.
    """
    reconcile_released_host(args)
    existing = load_mac_record()
    if existing and mac_state_exists():
        return _reconcile_existing(args, existing)

    plan = resolve_allocation_plan(args)
    allocated_at = utc_now().replace(microsecond=0)
    releasable_at = release_time(allocated_at)
    confirm_allocation(format_allocation_disclosure(plan, releasable_at), assume_yes=assume_yes)

    print(
        f"\nAllocating a {plan.instance_type} Dedicated Host in {plan.region}/"
        f"{plan.availability_zone} (region resolved from your nyxGPT cloud configuration, "
        "not from the AWS CLI default)."
    )
    outputs = apply_mac_host(plan)
    host_id = str(outputs.get("host_id") or "")
    if not host_id:
        raise CloudCommandError(
            "Terraform applied but reported no Dedicated Host id. Re-run "
            "`nyxgpt cloud deploy --os macos`; it reconciles rather than allocating again."
        )

    record = record_mac_host(
        {
            "mac_host_id": host_id,
            "mac_instance_id": str(outputs.get("instance_id") or ""),
            "mac_instance_type": str(outputs.get("instance_type") or plan.instance_type),
            "mac_region": str(outputs.get("region") or plan.region),
            "mac_availability_zone": str(
                outputs.get("availability_zone") or plan.availability_zone
            ),
            "mac_public_ip": str(outputs.get("public_ip") or ""),
            "mac_security_group_id": str(outputs.get("security_group_id") or ""),
            "mac_allocated_at": allocated_at.isoformat(),
            "mac_release_at": releasable_at.isoformat(),
            "mac_hourly_rate": (plan.pricing.hourly_rate if plan.pricing else None),
            "mac_release_scheduled": False,
        }
    )
    return {
        "allocated": True,
        "host_id": host_id,
        "instance_id": str(outputs.get("instance_id") or ""),
        "public_ip": str(outputs.get("public_ip") or ""),
        "security_group_id": str(outputs.get("security_group_id") or ""),
        "region": str(outputs.get("region") or plan.region),
        "availability_zone": str(outputs.get("availability_zone") or plan.availability_zone),
        "instance_type": str(outputs.get("instance_type") or plan.instance_type),
        "owner_ip_cidr": plan.owner_ip_cidr,
        "allocated_at": allocated_at.isoformat(),
        "release_at": releasable_at.isoformat(),
        "pricing": plan.pricing.to_dict() if plan.pricing else {},
        "record": record,
    }


def _reconcile_existing(args: argparse.Namespace, existing: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the Mac root for a host this machine already allocated."""
    plan = MacAllocationPlan(
        instance_type=str(existing.get("mac_instance_type") or DEFAULT_MAC_INSTANCE_TYPE),
        region=str(existing.get("mac_region") or ""),
        availability_zone=str(existing.get("mac_availability_zone") or ""),
    )
    settings = cloud_infra.resolve_settings(args)
    plan.profile = settings.aws_profile
    plan.owner_ip_cidr = settings.owner_ip_cidr
    plan.ssh_key_name = settings.ssh_key_name
    plan.ssh_public_key = settings.ssh_public_key
    plan.name_prefix = f"{settings.name_prefix}-mac"
    plan.root_volume_size = 200
    print(
        f"Reconciling the EC2 Mac already allocated from this machine "
        f"(host {existing.get('mac_host_id')}) -- no new host, no new 24-hour minimum."
    )
    outputs = apply_mac_host(plan)
    record = record_mac_host(
        {
            "mac_public_ip": str(outputs.get("public_ip") or ""),
            "mac_instance_id": str(outputs.get("instance_id") or ""),
            "mac_security_group_id": str(outputs.get("security_group_id") or ""),
        }
    )
    return {
        "allocated": False,
        "reconciled": True,
        "host_id": str(record.get("mac_host_id") or ""),
        "instance_id": str(outputs.get("instance_id") or ""),
        "public_ip": str(outputs.get("public_ip") or ""),
        "security_group_id": str(outputs.get("security_group_id") or ""),
        "region": plan.region,
        "availability_zone": plan.availability_zone,
        "instance_type": plan.instance_type,
        "owner_ip_cidr": plan.owner_ip_cidr,
        "allocated_at": str(record.get("mac_allocated_at") or ""),
        "release_at": str(record.get("mac_release_at") or ""),
        "pricing": {},
        "record": record,
    }


def _slack_settings() -> tuple[str, str]:
    """Return `(channel, bot_token)` from config.ini, or empty strings on failure."""
    try:
        from nyxgpt import config as config_mod

        cfg = config_mod.load_config()
        return (
            config_mod.get_monitoring_slack_channel(cfg),
            config_mod.get_monitoring_slack_bot_token(cfg),
        )
    except Exception:
        return ("", "")


def reconcile_released_host(args: argparse.Namespace) -> bool:
    """Forget a recorded host AWS says is already gone. Returns True if it cleared one.

    The deferred release fires with nobody watching -- that is the whole
    design, and it is why Slack carries the outcome. But it means nothing on
    this machine learns that the host is gone, so the record (and with it the
    "still billing" row on `nyxgpt cloud status`) would otherwise outlive the
    charge it describes.

    This is the reconcile, and both lifecycle commands run it first. It asks
    AWS exactly one question and only acts on a definite *no*:
    `host_still_allocated` answers `None` when it could not ask, and a record
    deleted on the strength of expired credentials would hide a resource that
    is still costing money. Never raises -- a reconcile that cannot run must
    not stop the command it runs ahead of.
    """
    record = load_mac_record()
    host_id = str(record.get("mac_host_id") or "")
    if not host_id:
        return False
    saved = cloud_infra.load_settings()
    region = (
        str(record.get("mac_region") or "")
        or str(getattr(args, "region", None) or "")
        or str(saved.get("aws_region") or "")
    )
    profile = str(getattr(args, "profile", None) or "") or str(saved.get("aws_profile") or "")
    try:
        if host_still_allocated(host_id, region, profile) is not False:
            return False
        print(
            f"Dedicated Host {host_id} has been released -- AWS no longer has it. "
            "Clearing it from this machine's cloud state."
        )
        destroy_release_stack()
    except Exception as exc:  # pragma: no cover - best effort by design
        print(f"note: could not clean up the released host's schedule: {exc}", file=sys.stderr)
    clear_mac_record()
    MAC_TFSTATE_FILE.unlink(missing_ok=True)
    MAC_TFVARS_FILE.unlink(missing_ok=True)
    return True


def teardown(args: argparse.Namespace) -> dict[str, Any]:
    """Terminate the Mac now and schedule the host release for when AWS allows it.

    Never raises. Every failure is collected into `errors` and returned,
    because this runs inside `nyxgpt cloud destroy` and a Dedicated Host that
    cannot be scheduled for release must not take the rest of the teardown
    with it -- the substrate, the tunnel and the deploy record all still have
    to come down, and an operator with a stuck host needs the *other* things
    gone so the one that is left is unambiguous.
    """
    if reconcile_released_host(args):
        return {"managed": False, "already_released": True}
    record = load_mac_record()
    if not record and not mac_state_exists():
        return {"managed": False}

    saved = cloud_infra.load_settings()
    host_id = str(record.get("mac_host_id") or "")
    # The record first: a host lives in exactly one region and that is the one
    # it was allocated in, whatever the flags or the AWS CLI default say now.
    # Flags are only a fallback for a record written before the region was
    # captured, and for the profile, which is a credential choice rather than
    # a property of the resource.
    region = (
        str(record.get("mac_region") or "")
        or str(getattr(args, "region", None) or "")
        or str(saved.get("aws_region") or "")
    )
    profile = str(getattr(args, "profile", None) or "") or str(saved.get("aws_profile") or "")
    allocated_at = parse_timestamp(str(record.get("mac_allocated_at") or ""))
    release_at = parse_timestamp(str(record.get("mac_release_at") or "")) or release_time(
        allocated_at or utc_now()
    )

    result: dict[str, Any] = {
        "managed": True,
        "host_id": host_id,
        "region": region,
        "release_at": release_at.isoformat(),
        "instance_terminated": False,
        "release_scheduled": False,
        "schedule": {},
        "errors": [],
    }

    # A release stack from a *previous* host, whose schedule has already fired
    # and deleted itself, still holds a state machine, a connection and the
    # Secrets Manager secret EventBridge made for it. This is the moment we can
    # be sure it is finished, so it is the moment it gets cleaned up.
    previous_host = _recorded_release_host()
    if previous_host and previous_host != host_id:
        try:
            if host_still_allocated(previous_host, region, profile) is False:
                destroy_release_stack()
        except Exception as exc:  # pragma: no cover - best effort by design
            result["errors"].append(f"could not clean up the previous release stack: {exc}")

    if host_id:
        channel, token = _slack_settings()
        try:
            result["schedule"] = apply_release_schedule(
                host_id=host_id,
                release_at=release_at,
                region=region,
                profile=profile,
                slack_channel=channel,
                slack_bot_token=token,
                name_prefix=f"{cloud_infra.load_settings().get('name_prefix') or 'nyxgpt-tf'}-mac",
            )
            result["release_scheduled"] = True
            record_mac_host(
                {
                    "mac_release_scheduled": True,
                    "mac_release_scheduled_at": utc_now().isoformat(),
                }
            )
        except Exception as exc:
            result["errors"].append(str(exc))

    try:
        destroyed = destroy_mac_instance(_teardown_tfvars(record, region, profile))
        result["instance_terminated"] = bool(destroyed.get("instance_terminated"))
        result["host_forgotten"] = bool(destroyed.get("host_forgotten"))
    except Exception as exc:
        result["errors"].append(f"the Mac instance could not be torn down: {exc}")

    return result


def _recorded_release_host() -> str:
    """Host id the existing release stack's tfvars names, or "" when there is none."""
    if not MAC_RELEASE_TFVARS_FILE.exists():
        return ""
    try:
        for line in MAC_RELEASE_TFVARS_FILE.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "host_id":
                return value.strip().strip('"')
    except OSError:
        return ""
    return ""


def _teardown_tfvars(record: dict[str, Any], region: str, profile: str) -> dict[str, Any]:
    """tfvars for the destroy of the Mac root, from the recorded deployment.

    Rebuilt from the record rather than re-resolved, for the same reason
    `cloud_infra.destroy_infra` uses saved settings: a teardown must not need
    the operator's current public IP (the network may be exactly what broke)
    or an SSH key that has since been deleted.
    """
    saved = cloud_infra.load_settings()
    return {
        "aws_region": region,
        "aws_profile": profile,
        "name_prefix": f"{saved.get('name_prefix') or 'nyxgpt-tf'}-mac",
        "availability_zone": str(record.get("mac_availability_zone") or ""),
        "mac_instance_type": str(record.get("mac_instance_type") or DEFAULT_MAC_INSTANCE_TYPE),
        # Any non-world CIDR satisfies the variable's validation; the security
        # group is being deleted, so its rule's value is immaterial.
        "owner_ip_cidr": str(saved.get("owner_ip_cidr") or "127.0.0.1/32"),
        "ssh_key_name": str(saved.get("ssh_key_name") or ""),
        "ssh_public_key": str(saved.get("ssh_public_key") or ""),
    }
