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

from nyxgpt import cloud_infra, cloud_record
from nyxgpt.cloud import CloudCommandError
from nyxgpt.optional_imports import CLOUD_EXTRA_REMEDY, try_import

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

# This substrate's *block* inside the shared `~/.nyxGPT/cloud/state.json`.
# Defined in `cloud_record`, which owns the file: each substrate's block is
# replaced whole and never merged into (#4136), and the other substrate's block
# is preserved -- so a substrate teardown cannot take the record of a
# still-billing host with it.
#
# Among the keys there: `mac_ami_id` and `mac_root_volume_size` record what the
# instance actually booted and how big its root disk is, so a reconcile
# re-applies the *same* values rather than re-resolving them (`most_recent =
# true` on the AMI data source and a differing volume size both force instance
# replacement, and replacing an EC2 Mac means a terminated instance, a lost
# disk and a host that then scrubs for an hour before anything can be placed on
# it again).
STATE_KEYS: tuple[str, ...] = cloud_record.MAC_BLOCK_KEYS

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
            "boto3 is required to price and place an EC2 Mac Dedicated Host. " + CLOUD_EXTRA_REMEDY
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


# --- What AWS actually billed ------------------------------------------


@dataclass
class MacHostSpend:
    """What Cost Explorer says the Dedicated Host family has cost.

    `amount is None` is a first-class outcome for the same reason it is on
    `MacHostPricing`: an operator who cannot be told the figure must be told
    *that*, not shown a locally computed number dressed up as AWS's.
    """

    host_family: str
    region: str
    amount: float | None = None
    currency: str = "USD"
    # Last day included, `YYYY-MM-DD`. Cost Explorer lags by up to a day, so a
    # figure with no "through" date reads as more current than it is.
    through: str = ""
    daily: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, carried in the status payload."""
        return {
            "host_family": self.host_family,
            "region": self.region,
            "amount": self.amount,
            "currency": self.currency,
            "through": self.through,
            "daily": list(self.daily),
            "error": self.error,
        }


# Cost Explorer is billed per request, so the figure is cached in the record
# and refreshed no more than this often. The number it reports moves at most
# once a day (AWS's own granularity), so a shorter interval would buy nothing
# and a dashboard poll would buy it repeatedly.
SPEND_REFRESH_SECONDS = 3600.0

# Cost Explorer has one endpoint, in us-east-1, whatever region is being
# queried -- same shape as the Pricing API above.
COST_EXPLORER_REGION = "us-east-1"

# Dedicated Host line items carry a usage type of `HostUsage:<family>`,
# optionally prefixed with a region code (`EUC1-HostUsage:mac2`). The family
# after the colon is matched exactly: `mac2` and `mac2-m2` are different
# hardware at different prices, and a prefix match would add one to the other.
HOST_USAGE_MARKER = "HostUsage:"

# What Cost Explorer calls EC2 compute. Dedicated Host charges land here, not
# under a service of their own.
EC2_COMPUTE_SERVICE = "Amazon Elastic Compute Cloud - Compute"


def lookup_host_spend(
    instance_type: str,
    region: str,
    profile: str = "",
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> MacHostSpend:
    """Ask Cost Explorer what the Dedicated Host family has actually cost.

    This is the answer to "how much has this host cost me?", and it replaces
    `hourly_rate * (now - allocated_at)` (#4136), which was wrong in both
    directions and most wrong exactly when it mattered: it read $48.44 for a
    host AWS had billed $12.02 for and had stopped charging for two days
    earlier, because a local clock multiplied by a local rate keeps counting
    after the resource is gone. The figure here stops moving when the charges
    do, because it is the charges.

    Never raises: a cost lookup that fails must not stop a lifecycle command,
    and must not be replaced by a guess either. The failure lands in `error`
    and every surface prints it instead of a number.

    Scoped to the host *family* in one region rather than to the host id.
    Resource-level attribution is a separate, opt-in Cost Explorer feature with
    its own 14-day window, and nyxGPT allocates at most one Mac host per region
    by construction, so the family total in that region is the host's bill.
    """
    family = host_family(instance_type)
    spend = MacHostSpend(host_family=family, region=region)
    moment = (now or utc_now()).astimezone(UTC)
    # Cost Explorer's `End` is exclusive, so tomorrow is what includes today.
    end = (moment + timedelta(days=1)).date()
    start = (since.astimezone(UTC).date() if since else None) or (moment - timedelta(days=30)).date()
    if start >= end:
        start = end - timedelta(days=1)

    try:
        client = _client("ce", COST_EXPLORER_REGION, profile)
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE", "Values": [EC2_COMPUTE_SERVICE]}},
                    {"Dimensions": {"Key": "REGION", "Values": [region]}},
                ]
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    except Exception as exc:
        spend.error = f"the AWS Cost Explorer API could not be queried: {exc}"
        return spend

    total = 0.0
    matched = False
    for period in response.get("ResultsByTime", []):
        day = str((period or {}).get("TimePeriod", {}).get("Start") or "")
        day_total = 0.0
        day_matched = False
        for group in (period or {}).get("Groups", []):
            keys = [str(key) for key in (group or {}).get("Keys", [])]
            if not any(
                key.split(HOST_USAGE_MARKER, 1)[1] == family
                for key in keys
                if HOST_USAGE_MARKER in key
            ):
                continue
            metric = (group or {}).get("Metrics", {}).get("UnblendedCost", {})
            try:
                day_total += float(metric.get("Amount"))
            except (TypeError, ValueError):
                continue
            day_matched = True
            currency = str(metric.get("Unit") or "")
            if currency:
                spend.currency = currency
        if day_matched:
            matched = True
            total += day_total
            spend.daily.append({"date": day, "amount": round(day_total, 4)})
        if day:
            spend.through = day

    if not matched:
        spend.error = (
            f"Cost Explorer reported no Dedicated Host charges for family {family!r} in "
            f"{region} between {start.isoformat()} and {end.isoformat()}. A host allocated in "
            "the last few hours may not have been billed yet -- Cost Explorer lags by up to a day."
        )
        return spend
    spend.amount = round(total, 2)
    return spend


def record_findings(record: dict[str, Any]) -> list[str]:
    """Internal inconsistencies in the Mac block, each detectable with no API call.

    An incoherent block is reported rather than used (#4136). All three of
    these were true of the record the 2026-10-03 deploy read, and every one is
    free: no credentials, no network, no latency. A block that fails them
    describes no state AWS could ever have been in, so the only safe reading is
    that its fields came from more than one run.
    """
    findings: list[str] = []
    if not record.get("mac_host_id"):
        return findings

    allocated_at = parse_timestamp(str(record.get("mac_allocated_at") or ""))
    release_at = parse_timestamp(str(record.get("mac_release_at") or ""))
    scheduled_at = parse_timestamp(str(record.get("mac_release_scheduled_at") or ""))

    if record.get("mac_allocated_at") and allocated_at is None:
        findings.append(
            f"mac_allocated_at ({record.get('mac_allocated_at')!r}) is not a timestamp"
        )
    if scheduled_at is not None and allocated_at is not None and scheduled_at < allocated_at:
        findings.append(
            f"mac_release_scheduled_at ({scheduled_at.isoformat()}) is EARLIER than "
            f"mac_allocated_at ({allocated_at.isoformat()}) -- the release of this host cannot "
            "have been scheduled before the host existed, so these two fields were written by "
            "different runs about different hosts"
        )
    if allocated_at is not None and release_at is not None:
        expected = release_time(allocated_at)
        # Whole seconds; the record stores both to second precision, so any
        # real difference is minutes or days, never rounding.
        if abs((release_at - expected).total_seconds()) > 1:
            findings.append(
                f"mac_release_at ({release_at.isoformat()}) is not mac_allocated_at + "
                f"{HOST_MINIMUM_HOURS}h{RELEASE_BUFFER_MINUTES}m "
                f"(expected {expected.isoformat()})"
            )
    if record.get("mac_release_scheduled"):
        scheduled_host = _recorded_release_host()
        host_id = str(record.get("mac_host_id") or "")
        if not scheduled_host:
            findings.append(
                "mac_release_scheduled is true but no release schedule exists on this machine "
                "-- nothing will release this host"
            )
        elif scheduled_host != host_id:
            findings.append(
                f"mac_release_scheduled is true but the release schedule names {scheduled_host} "
                f"rather than {host_id}"
            )
    return findings


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


def record_mac_host(values: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    """Replace this substrate's whole block with `values` and return the record.

    **Whole, not merged** (#4136). Every key this substrate owns is decided by
    this call; one `values` does not carry is dropped. The merge this replaced
    wrote only the fields the caller happened to touch, so a deploy that
    provisioned a *new* Mac updated the instance fields and left the host
    fields naming the previous, released host -- a new instance stapled to a
    dead host, a combination that never existed. Everything downstream then
    read the whole file as equally fresh: the consent gate skipped a priced
    disclosure, `cloud status` reported a release window that had "passed" for
    a host allocated hours later, and `destroy` would have tried to release a
    deleted host while leaving the live, billing one untracked.

    Use `amend_mac_record` for an update to fields of a host that is already
    recorded -- it proves the block still describes the same host first. Only
    the other substrate's block is preserved here.
    """
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, values, reason=reason)
    return load_mac_record()


def amend_mac_record(
    updates: dict[str, Any], *, host_id: str, reason: str = ""
) -> dict[str, Any]:
    """Update fields of the recorded host, proving it is still that host.

    The gated merge. `host_id` is what the caller believes the block describes;
    if the record now names a different host the write is refused
    (`cloud_record.StaleRecordError`) rather than applied to whichever host
    happens to be recorded. Without that proof, "the release is scheduled"
    lands on the wrong host -- which is how a `release_scheduled_at` 78 seconds
    *earlier* than the `allocated_at` of the host it described got written.
    """
    cloud_record.amend_block(
        cloud_record.SUBSTRATE_MAC,
        updates,
        expect={"mac_host_id": host_id},
        reason=reason,
    )
    return load_mac_record()


def load_mac_record() -> dict[str, Any]:
    """Return what is recorded about the Mac host, or `{}` when there is none."""
    record = cloud_record.load_block(cloud_record.SUBSTRATE_MAC)
    return record if record.get("mac_host_id") else {}


def clear_mac_record(*, reason: str = "") -> None:
    """Drop this substrate's block from the shared cloud state.

    Called only once the host is *gone* -- not when the instance is
    terminated. A record deleted while the host still bills is the exact
    failure `nyxgpt cloud status`'s pending-release row exists to prevent.
    """
    cloud_record.clear_block(cloud_record.SUBSTRATE_MAC, reason=reason)


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

    # The local figure is an *estimate* and is reported under that name
    # (#4136). It used to be the headline "Accrued" number, computed as
    # `rate * (now - allocated_at)`, which kept climbing after AWS stopped
    # charging -- $48.44 displayed for a $12.02 bill that had ended two days
    # earlier. What AWS billed comes from Cost Explorer and is recorded by
    # `verify_mac_record`; this one is the fallback for a record that has not
    # been verified yet, never a substitute for it.
    estimate: float | None = None
    if allocated_at is not None and rate > 0:
        estimate = accrued_cost(allocated_at, rate, now)

    spend_amount = record.get("mac_spend_amount")
    try:
        spend = float(spend_amount) if spend_amount is not None else None
    except (TypeError, ValueError):
        spend = None

    releasable = bool(release_at and now >= release_at)
    return {
        "host_id": str(record.get("mac_host_id") or ""),
        "instance_id": str(record.get("mac_instance_id") or ""),
        "instance_type": str(record.get("mac_instance_type") or ""),
        "region": str(record.get("mac_region") or ""),
        "availability_zone": str(record.get("mac_availability_zone") or ""),
        # #4122. Recorded since #3995 and reported by nothing, so `cloud status`
        # read the *Linux* substrate's fields for a Mac and printed
        # "Security group: not recorded" over a security group this file names,
        # and `m5.xlarge` for a `mac2.metal`. Surfaced here rather than fixed at
        # each reader: one source, so no two surfaces can disagree.
        "security_group_id": str(record.get("mac_security_group_id") or ""),
        "public_ip": str(record.get("mac_public_ip") or ""),
        "allocated_at": str(record.get("mac_allocated_at") or ""),
        "release_at": str(record.get("mac_release_at") or ""),
        "release_scheduled": bool(record.get("mac_release_scheduled")),
        "release_scheduled_at": str(record.get("mac_release_scheduled_at") or ""),
        "hourly_rate": rate or None,
        # What AWS billed, and only that. `None` means nobody has asked AWS
        # yet (or it could not answer) -- it never silently becomes the local
        # estimate, which is carried separately so no surface can print one
        # under the other's name.
        "accrued_cost": spend,
        "accrued_source": "aws-cost-explorer" if spend is not None else "",
        "estimated_cost": estimate,
        "spend_through": str(record.get("mac_spend_through") or ""),
        "spend_as_of": str(record.get("mac_spend_as_of") or ""),
        "spend_error": str(record.get("mac_spend_error") or ""),
        "currency": str(record.get("mac_spend_currency") or "USD"),
        "releasable_now": releasable,
        # When AWS last confirmed this host exists, and what it said. Empty /
        # `None` means no run has confirmed it, which is a different claim from
        # "it is gone" -- and the reason no surface here says "still billing"
        # on the strength of the record alone.
        "verified_at": str(record.get("mac_verified_at") or ""),
        "host_present": (
            bool(record.get("mac_host_present"))
            if record.get("mac_host_present") is not None
            else None
        ),
        # Free internal-consistency checks. Non-empty means the block cannot
        # describe any state AWS was ever in, so its fields came from more than
        # one run and must not be acted on.
        "incoherent": record_findings(record),
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


#: The Mac root's Dedicated Host resource address, as `terraform/aws/mac` names
#: it. Also the address `forget_host` removes.
HOST_RESOURCE_TYPE = "aws_ec2_host"
HOST_RESOURCE_NAME = "this"


def allocated_host_from_state() -> str:
    """The Dedicated Host id in the Mac root's Terraform state, or `""`.

    Read straight out of the local state file rather than through
    `terraform output` (#4122). Two reasons, and the second is the whole point:

    * it costs nothing -- no `terraform init`, no process, no AWS call; and
    * **it still answers after an apply that failed.** That is the case this
      function exists for. `allocate` records the host id only once the whole
      apply succeeds, so an apply that allocated the host and then failed on a
      later resource left a billed Dedicated Host with *no* record of it:
      `load_mac_record()` answered `{}`, the next run took the fresh-allocation
      path, and the operator was shown a non-refundable-charge disclosure for a
      charge they had already made. Only Terraform's own state (`0 added, 0
      changed`) stopped it being a second real one. State is what knows; so
      state is what is asked.

    Unreadable or malformed state answers `""` -- the caller then treats the
    host as not-yet-allocated, which is the pre-existing behaviour.
    """
    if not MAC_TFSTATE_FILE.exists():
        return ""
    try:
        state = json.loads(MAC_TFSTATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(state, dict):
        return ""
    for resource in state.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        if resource.get("type") != HOST_RESOURCE_TYPE or resource.get("name") != HOST_RESOURCE_NAME:
            continue
        for instance in resource.get("instances") or []:
            if not isinstance(instance, dict):
                continue
            attributes = instance.get("attributes")
            host_id = (
                str((attributes or {}).get("id") or "") if isinstance(attributes, dict) else ""
            )
            if host_id:
                return host_id
    return ""


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


#: The error EC2 returns for a host id it has no record of -- a host released
#: long enough ago that it has aged out of `DescribeHosts` entirely. Both
#: spellings are matched because botocore has shipped each: the EC2 API
#: documents `InvalidHostID.NotFound`, and some paths report `InvalidHostId`.
HOST_NOT_FOUND_CODES = ("invalidhostid.notfound", "invalidhostidnotfound")


def _aws_error_code(exc: Exception) -> str:
    """botocore's `Error.Code` off a ClientError, or `''` for anything else.

    Read off the response dict rather than by catching typed botocore
    exceptions, for the same reason `cloud_state._error_code` does: importing
    `botocore.exceptions` at module scope would break this module on an install
    without the cloud extra.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def host_still_allocated(host_id: str, region: str, profile: str = "") -> bool | None:
    """Is `host_id` still allocated? `None` when AWS could not be asked.

    Three answers, not two, and both of the definite ones matter.

    * Expired credentials must not be reported as "the host is gone", which
      would be the one wrong answer that stops an operator looking for a
      resource that is still billing. That is the `None`.
    * **`InvalidHostID.NotFound` is an answer, not a failure** (#4136). EC2
      returns it for a host it has no record of, which is precisely "the host
      is gone" -- but it arrives as a `ClientError`, and catching every
      exception as `None` turned the clearest possible *no* into "could not
      ask". That is the whole 2026-10-03 incident: `reconcile_released_host`
      made exactly this call, got `InvalidHostID.NotFound` for a host released
      three days earlier, read it as unknown, left the stale record in place,
      and the deploy went on to announce "no new host, no new 24-hour minimum"
      before allocating one.
    """
    if not host_id:
        return False
    try:
        client = _client("ec2", region, profile)
        response = client.describe_hosts(HostIds=[host_id])
    except Exception as exc:
        code = _aws_error_code(exc).replace("-", "").lower()
        if code in HOST_NOT_FOUND_CODES:
            return False
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
        root_volume_size=(
            int(requested_volume) if requested_volume else MacAllocationPlan.root_volume_size
        ),
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

    Re-entrant: a host this machine has already allocated is reconciled (a
    re-deploy of the same Mac) rather than re-priced and re-confirmed. Asking an
    operator to re-consent to a charge they already made would train them to
    type the word without reading it.

    **What "already allocated" means (#4122).** It used to mean "the record in
    `state.json` names a host, and a Terraform state file exists". Both halves
    of that are written *after* a successful apply, so an apply that allocated
    the host and then failed -- the owner's 2026-09-30 run failed on an
    apostrophe in a security-group rule description, after the host existed --
    satisfied neither, and the next run disclosed a fresh non-refundable charge
    for a host that was already billing. It now also means "Terraform's state
    holds the host", which is true from the moment the allocation succeeds,
    whatever fails afterwards. `allocated_host_from_state` is the read, and the
    record is healed from it on the way through.

    **And "already allocated" is never decided by the record alone (#4136).**
    Every reconcile path below requires AWS to have confirmed the host *in this
    run*. The 2026-10-03 deploy skipped the disclosure and the prompt because
    the record named a host released three days earlier and nothing here
    insisted on asking; one `DescribeHosts` answered `InvalidHostID.NotFound`
    and was read as "could not ask". A billable allocation now cannot happen
    without the disclosure and the prompt whatever the local record says,
    because the only paths that bypass them are the ones holding a positive
    answer from AWS.
    """
    reconcile_released_host(args)
    existing = load_mac_record()
    if existing and mac_state_exists():
        host_id = str(existing.get("mac_host_id") or "")
        if not host_confirmed_this_run(existing):
            raise CloudCommandError(_unconfirmed_host_message(host_id))
        return _reconcile_existing(args, existing)
    # No usable record, but Terraform already holds a host: a previous run
    # allocated it and then failed before recording it. Heal the record and
    # reconcile -- never re-disclose (#4122). Still only on a confirmed host:
    # Terraform state outlives the resource it names just as the record does.
    orphaned_host = allocated_host_from_state()
    if orphaned_host:
        present = host_still_allocated(
            orphaned_host, _record_region(existing, args), _record_profile(args)
        )
        if present is None:
            raise CloudCommandError(_unconfirmed_host_message(orphaned_host))
        if present:
            _heal_orphaned_record(args, orphaned_host, existing)
            _record_verification(orphaned_host, True)
            # Re-read rather than reuse what `_heal_orphaned_record` returned:
            # the verification landed after it, and the reconcile's block is
            # built from this record.
            return _reconcile_existing(args, load_mac_record())
        print(
            f"Terraform's state names Dedicated Host {orphaned_host}, but AWS has released it. "
            "Discarding that state so this deploy allocates a host with the full disclosure "
            "rather than reconciling one that no longer exists.",
            file=sys.stderr,
        )
        MAC_TFSTATE_FILE.unlink(missing_ok=True)
        MAC_TFVARS_FILE.unlink(missing_ok=True)

    plan = resolve_allocation_plan(args)
    allocated_at = utc_now().replace(microsecond=0)
    releasable_at = release_time(allocated_at)
    confirm_allocation(format_allocation_disclosure(plan, releasable_at), assume_yes=assume_yes)

    print(
        f"\nAllocating a {plan.instance_type} Dedicated Host in {plan.region}/"
        f"{plan.availability_zone} (region resolved from your nyxGPT cloud configuration, "
        "not from the AWS CLI default)."
    )
    try:
        outputs = apply_mac_host(plan)
    except BaseException:
        # The apply may have allocated the host before failing, and a host whose
        # id is lost is a charge nothing can stop (#4122). Record what Terraform
        # state knows *before* the exception leaves this function, so
        # `nyxgpt cloud status` names the host and `nyxgpt cloud destroy --yes`
        # can schedule its release even though this deploy never finished.
        stranded = allocated_host_from_state()
        if stranded:
            record_mac_host(
                {
                    "mac_host_id": stranded,
                    "mac_instance_type": plan.instance_type,
                    "mac_region": plan.region,
                    "mac_availability_zone": plan.availability_zone,
                    "mac_ami_id": plan.ami_id,
                    "mac_root_volume_size": plan.root_volume_size,
                    "mac_allocated_at": allocated_at.isoformat(),
                    "mac_release_at": releasable_at.isoformat(),
                    "mac_hourly_rate": (plan.pricing.hourly_rate if plan.pricing else None),
                    "mac_release_scheduled": False,
                },
                reason=f"recorded Dedicated Host {stranded} stranded by a failed apply",
            )
            print(
                f"\nWARNING: Dedicated Host {stranded} WAS allocated before this failure and is "
                "billing. It has been recorded, so `nyxgpt cloud status` names it, its release "
                "time and its accrued cost, and re-running the deploy reconciles it rather than "
                "allocating a second one.",
                file=sys.stderr,
            )
        raise
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
            "mac_ami_id": str(outputs.get("ami_id") or plan.ami_id or ""),
            "mac_root_volume_size": plan.root_volume_size,
            "mac_allocated_at": allocated_at.isoformat(),
            "mac_release_at": releasable_at.isoformat(),
            "mac_hourly_rate": (plan.pricing.hourly_rate if plan.pricing else None),
            "mac_release_scheduled": False,
            # Confirmed by the allocation itself -- the host exists because this
            # run just created it, which is the one case that needs no
            # `DescribeHosts` to prove.
            "mac_verified_at": utc_now().isoformat(),
            "mac_host_present": True,
        },
        reason=f"allocated Dedicated Host {host_id}",
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


def _heal_orphaned_record(
    args: argparse.Namespace, host_id: str, existing: dict[str, Any]
) -> dict[str, Any]:
    """Write a record for a host Terraform holds but `state.json` never captured (#4122).

    The allocation timestamp is the one field that cannot be recovered from
    Terraform state -- `aws_ec2_host` has no allocation-time attribute -- so
    this records *now*, which is deliberately conservative in the only
    direction that is safe: a release time later than the true one never asks
    AWS to release a host inside its 24-hour minimum, whereas an earlier one
    burns the one-shot schedule on a rejection. The accrued cost printed from
    it is a lower bound for the same reason, and the disclosure that follows
    says so.

    The instance type and zone come from the plan this machine would allocate,
    which is where the tfvars that created the host came from, so a reconcile
    re-applies the same values rather than resolving new ones.
    """
    instance_type = str(existing.get("mac_instance_type") or resolve_mac_instance_type(args))
    settings = cloud_infra.resolve_settings(args)
    allocated_at = utc_now().replace(microsecond=0)
    pricing = lookup_host_pricing(instance_type, settings.aws_region, settings.aws_profile)
    print(
        f"Dedicated Host {host_id} is already allocated from this machine -- Terraform's state "
        "holds it, but an earlier run failed before recording it. Adopting it: no new host, no "
        "new 24-hour minimum, and no second charge.\n"
        "Its allocation time could not be recovered, so the release time and accrued cost "
        f"reported from here are measured from now ({allocated_at.isoformat()}) and are a lower "
        "bound on the age of the host.",
        file=sys.stderr,
    )
    # Both timestamps come from the *same* moment, so the record cannot end up
    # with a release time that is not its allocation time plus AWS's minimum --
    # one of the incoherences `record_findings` reports.
    recorded_allocation = parse_timestamp(str(existing.get("mac_allocated_at") or "")) or (
        allocated_at
    )
    return record_mac_host(
        {
            "mac_host_id": host_id,
            "mac_instance_type": instance_type,
            "mac_region": str(existing.get("mac_region") or settings.aws_region),
            "mac_availability_zone": str(existing.get("mac_availability_zone") or ""),
            "mac_allocated_at": recorded_allocation.isoformat(),
            "mac_release_at": release_time(recorded_allocation).isoformat(),
            "mac_hourly_rate": existing.get("mac_hourly_rate") or pricing.hourly_rate,
            "mac_release_scheduled": False,
        },
        reason=f"adopted Dedicated Host {host_id} from Terraform state",
    )


def _recorded_root_volume_size(existing: dict[str, Any]) -> int:
    """Root volume size from the record, or the size the allocation would use.

    Separate from a plain `int(...)` because the record can hold `""`, `None`
    or a string from an older write, and a bad value here does not fail loudly
    -- it plans a differently-sized root volume, which forces replacement.
    """
    raw = existing.get("mac_root_volume_size")
    default = MacAllocationPlan.root_volume_size
    if raw in (None, ""):
        return int(default)
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return size if size > 0 else int(default)


def _reconciled_block(
    existing: dict[str, Any],
    outputs: dict[str, Any],
    plan: MacAllocationPlan,
) -> dict[str, Any]:
    """The complete block a reconcile records, built field by field (#4136).

    Every key this substrate owns is decided here, because `record_mac_host`
    replaces the block whole and a key this function omits is a key that
    disappears. The reconcile used to write five of them and leave the rest
    alone, which is the defect: the host fields kept describing the *previous*
    host while the instance fields described the new one.

    The host-identity fields come from the apply's own outputs, so a host id
    that changed under a reconcile is recorded as what it is rather than
    papered over -- and when it changes, the timing fields that described the
    old host (`mac_allocated_at`, `mac_release_at`, both release-schedule
    fields) are recomputed or dropped rather than carried across -- including
    the verification fields, which are a claim about the host AWS confirmed and
    say nothing about a different one.
    """
    applied_host = str(outputs.get("host_id") or "")
    expected_host = str(existing.get("mac_host_id") or "")
    host_id = applied_host or expected_host
    host_changed = bool(applied_host and expected_host and applied_host != expected_host)

    if host_changed:
        # Not reachable through `allocate`, which confirms the host at AWS and
        # requires Terraform's state to hold it before it ever gets here -- so
        # the apply has nothing to create. Reported rather than trusted not to
        # happen: if it ever does, a non-refundable 24-hour minimum has started
        # without a disclosure, and the record saying so is the only way the
        # operator finds out before the bill.
        print(
            f"WARNING: this reconcile was for Dedicated Host {expected_host}, but the apply "
            f"reports {applied_host}. A NEW host has been allocated and a new 24-hour minimum "
            "has started. The record now describes the new host; the previous one is in "
            f"{cloud_record.archive_file()}. Check `nyxgpt cloud status` and release whichever "
            "host you do not want.",
            file=sys.stderr,
        )
        allocated = utc_now().replace(microsecond=0)
        allocated_at: str = allocated.isoformat()
        release_at: str = release_time(allocated).isoformat()
    else:
        allocated_at = str(existing.get("mac_allocated_at") or "")
        release_at = str(existing.get("mac_release_at") or "")

    block: dict[str, Any] = {
        "mac_host_id": host_id,
        "mac_instance_id": str(outputs.get("instance_id") or ""),
        "mac_instance_type": str(outputs.get("instance_type") or plan.instance_type),
        "mac_region": str(outputs.get("region") or plan.region),
        "mac_availability_zone": str(outputs.get("availability_zone") or plan.availability_zone),
        "mac_public_ip": str(outputs.get("public_ip") or ""),
        "mac_security_group_id": str(outputs.get("security_group_id") or ""),
        # Heals a record written before these keys existed: the output is the
        # instance's own `ami`, so what lands here is what the Mac is running,
        # and the next reconcile pins to it instead of resolving `most_recent`
        # all over again.
        "mac_ami_id": str(outputs.get("ami_id") or plan.ami_id or ""),
        "mac_root_volume_size": plan.root_volume_size,
        "mac_allocated_at": allocated_at,
        "mac_release_at": release_at,
        "mac_hourly_rate": existing.get("mac_hourly_rate"),
    }
    if not host_changed:
        # A pending release belongs to the host it was created for. Carried only
        # when the host is the same one, and dropped outright when it is not --
        # `mac_release_scheduled = true` against a host whose release nothing
        # scheduled is one of the incoherences `record_findings` reports.
        block["mac_release_scheduled"] = bool(existing.get("mac_release_scheduled"))
        if existing.get("mac_release_scheduled_at"):
            block["mac_release_scheduled_at"] = existing["mac_release_scheduled_at"]
        # The recorded spend is about this host and this family, so it survives
        # a same-host reconcile; `verify_mac_record` refreshes it on its own
        # schedule. So does the confirmation -- `allocate` has just had AWS
        # confirm this exact host, and dropping that would make the record read
        # as unverified immediately after the one run that verified it.
        for key in (
            "mac_spend_amount",
            "mac_spend_currency",
            "mac_spend_through",
            "mac_verified_at",
            "mac_host_present",
        ):
            if existing.get(key) is not None:
                block[key] = existing[key]
    return {key: value for key, value in block.items() if value not in (None, "")}


def _reconcile_existing(args: argparse.Namespace, existing: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the Mac root for a host this machine already allocated.

    Re-applies the machine the operator *has*, never a newer one. Two inputs
    decide that and both are read back from the record rather than re-resolved:

    * **The AMI.** `data.aws_ami.macos` is `most_recent = true`, so leaving
      `ami_id` empty here would resolve whatever Amazon published since the
      allocation. `ami` forces replacement, and replacing an EC2 Mac is not a
      brief outage -- the instance is terminated, its disk goes with it, and
      the host enters an hour-long scrub during which the replacement cannot
      launch. `terraform apply` runs `-auto-approve` on this path, so nothing
      would have stopped it either.
    * **The root volume size.** This used to be hardcoded to 200 GiB, which
      silently disagreed with any deploy that passed `--root-volume-size`;
      shrinking a root volume forces replacement the same way.

    A record written before those keys existed has neither, so each falls back
    to what the original allocation would have used -- and `ignore_changes =
    [ami]` in the module is the backstop that keeps even that case from
    replacing a running Mac.
    """
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
    plan.ami_id = str(existing.get("mac_ami_id") or "")
    plan.root_volume_size = _recorded_root_volume_size(existing)
    expected_host = str(existing.get("mac_host_id") or "")
    print(
        f"Reconciling the EC2 Mac already allocated from this machine "
        f"(host {expected_host}, confirmed at AWS in this run) -- no new host, no new "
        "24-hour minimum."
    )
    outputs = apply_mac_host(plan)
    record = record_mac_host(
        _reconciled_block(existing, outputs, plan),
        reason=f"reconciled Dedicated Host {expected_host}",
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

    A definite *yes* is written down too (#4136). `mac_verified_at` /
    `mac_host_present` are what let every later surface say "AWS confirmed this
    host at <time>" instead of repeating the record back as fact, and they are
    what `allocate` requires before it is allowed to claim "no new host, no new
    24-hour minimum".
    """
    record = load_mac_record()
    host_id = str(record.get("mac_host_id") or "")
    if not host_id:
        return False
    region = _record_region(record, args)
    profile = _record_profile(args)
    try:
        present = host_still_allocated(host_id, region, profile)
    except Exception:  # pragma: no cover - host_still_allocated swallows its own
        return False
    if present is not False:
        if present is True:
            _record_verification(host_id, True)
        return False
    print(
        f"Dedicated Host {host_id} has been released -- AWS no longer has it. "
        "Clearing it from this machine's cloud state."
    )
    try:
        destroy_release_stack()
    except Exception as exc:  # pragma: no cover - best effort by design
        print(f"note: could not clean up the released host's schedule: {exc}", file=sys.stderr)
    clear_mac_record(reason=f"AWS reports Dedicated Host {host_id} as released")
    MAC_TFSTATE_FILE.unlink(missing_ok=True)
    MAC_TFVARS_FILE.unlink(missing_ok=True)
    return True


def _record_region(record: dict[str, Any], args: argparse.Namespace) -> str:
    """Region for the recorded host: the record first, then flags, then settings.

    A host lives in exactly one region and that is the one it was allocated in,
    whatever the flags or the AWS CLI default say now. Flags are only a
    fallback for a record written before the region was captured.
    """
    saved = cloud_infra.load_settings()
    return (
        str(record.get("mac_region") or "")
        or str(getattr(args, "region", None) or "")
        or str(saved.get("aws_region") or "")
    )


def _record_profile(args: argparse.Namespace) -> str:
    """Credential profile for an AWS call about the recorded host.

    Unlike the region this is *not* a property of the resource -- it is which
    credentials to use now -- so the flag wins over the saved setting.
    """
    return str(getattr(args, "profile", None) or "") or str(
        cloud_infra.load_settings().get("aws_profile") or ""
    )


def _record_verification(host_id: str, present: bool) -> None:
    """Write down that AWS confirmed `host_id`, and when. Best effort.

    Amended rather than written whole, and gated on the host id: the point of
    the field is that it describes *this* host, so landing it on a block that
    has since been replaced would make the record less trustworthy than having
    no verification at all.
    """
    try:
        amend_mac_record(
            {"mac_verified_at": utc_now().isoformat(), "mac_host_present": present},
            host_id=host_id,
            reason="AWS confirmed the host",
        )
    except Exception:  # pragma: no cover - a record that moved under us
        return


# How recent a `DescribeHosts` confirmation has to be to count as "this run".
# Generous enough to cover a slow Terraform sync between the reconcile and the
# decision, short enough that it can only ever mean the current command.
VERIFICATION_MAX_AGE_SECONDS = 300.0


def host_confirmed_this_run(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Did AWS confirm this record's host within the last few minutes?

    The gate on every path that skips the priced disclosure and the `allocate`
    prompt (#4136). "The record names a host" is not evidence the host exists;
    `mac_host_present` plus a fresh `mac_verified_at` is, and nothing writes
    those except an actual answer from `DescribeHosts`.
    """
    if not record.get("mac_host_id") or not record.get("mac_host_present"):
        return False
    verified = parse_timestamp(str(record.get("mac_verified_at") or ""))
    if verified is None:
        return False
    return ((now or utc_now()) - verified).total_seconds() <= VERIFICATION_MAX_AGE_SECONDS


def _unconfirmed_host_message(host_id: str) -> str:
    """Why a reconcile cannot proceed when AWS could not be asked about `host_id`."""
    return (
        f"Dedicated Host {host_id} is recorded on this machine, but AWS could not be asked "
        "whether it still exists in this run -- so nyxGPT cannot tell a host you are already "
        "paying for from one that was released.\n"
        "Reconciling on the record alone is how a deploy came to announce 'no new host, no new "
        "24-hour minimum' and then allocate one (#4136), so nothing is applied and nothing is "
        "billed here.\n"
        "Fix the credentials (`nyxgpt cloud credentials-setup`, or check the profile/region) and "
        "re-run. `nyxgpt cloud status` shows what is recorded in the meantime."
    )


def verify_mac_record(args: argparse.Namespace | None = None) -> dict[str, Any]:
    """Confirm the recorded host against AWS and refresh what it has cost.

    The read-side counterpart to `reconcile_released_host`: same single
    `DescribeHosts` call (so a released host still clears the record), plus the
    Cost Explorer figure that replaces the local `rate * elapsed` estimate.

    The cost query is cached in the record and refreshed at most every
    `SPEND_REFRESH_SECONDS`, because Cost Explorer bills per request and its own
    granularity is a day -- a dashboard poll must not be able to turn an
    observability surface into a line item. Never raises.

    `args` is optional so a status surface with no parsed flags can call it; the
    region then comes from the record (which is where it belongs anyway) and the
    profile from the saved settings.
    """
    args = args if args is not None else argparse.Namespace()
    if reconcile_released_host(args):
        return {"host_present": False, "cleared": True}
    record = load_mac_record()
    host_id = str(record.get("mac_host_id") or "")
    if not host_id:
        return {}

    as_of = parse_timestamp(str(record.get("mac_spend_as_of") or ""))
    if as_of is not None and (utc_now() - as_of).total_seconds() < SPEND_REFRESH_SECONDS:
        return {"host_present": record.get("mac_host_present"), "spend_cached": True}

    spend = lookup_host_spend(
        str(record.get("mac_instance_type") or DEFAULT_MAC_INSTANCE_TYPE),
        _record_region(record, args),
        _record_profile(args),
        since=parse_timestamp(str(record.get("mac_allocated_at") or "")),
    )
    try:
        amend_mac_record(
            {
                "mac_spend_amount": spend.amount,
                "mac_spend_currency": spend.currency,
                "mac_spend_through": spend.through or None,
                "mac_spend_as_of": utc_now().isoformat(),
                "mac_spend_error": spend.error or None,
            },
            host_id=host_id,
            reason="refreshed the Cost Explorer figure",
        )
    except Exception:  # pragma: no cover - a record that moved under us
        pass
    return {"host_present": record.get("mac_host_present"), "spend": spend.to_dict()}


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
            # Amended, not written whole, and gated on the host id (#4136): this
            # says "the release of *this* host is scheduled", and landing it on
            # a block that has since been replaced is how a
            # `mac_release_scheduled_at` 78 seconds earlier than the
            # `mac_allocated_at` of the host it described got written.
            amend_mac_record(
                {
                    "mac_release_scheduled": True,
                    "mac_release_scheduled_at": utc_now().isoformat(),
                },
                host_id=host_id,
                reason="deferred release scheduled",
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
