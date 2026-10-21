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
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nyxgpt import cloud_identity, cloud_infra, cloud_record, cloud_verified
from nyxgpt.cloud import CloudCommandError, ConsentDeclined
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
MAC_TFVARS_FILE = cloud_infra.CLOUD_DIR / "mac.tfvars"

# The release root's state and vars are **per host** (#4200), and the legacy
# single-slot pair below is only still named so a stack created before that
# change can be found and torn down. One slot could only ever hold one host:
# scheduling the release of host B rewrote these two files and `terraform
# apply` updated the one schedule in place, so host A -- still allocated,
# still inside its 24-hour minimum, still billing about $15.60 a day -- was
# left with no release scheduled at all and no Slack message to say so,
# because the schedule that would have sent it now named B.
#
# `release_paths` is the one resolver; nothing else composes these names.
MAC_RELEASE_TFSTATE_FILE = cloud_infra.CLOUD_DIR / "mac-release.tfstate"
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
    """Parse an ISO-8601 timestamp from cloud state, or `None` if unusable.

    Delegated to `cloud_verified` since #4181: deciding whether a recorded
    instant is usable is part of deciding whether a recorded *fact* is, and the
    substrate module and the verified reader disagreeing about one timestamp
    would be a disagreement about whether AWS confirmed something.
    """
    return cloud_verified.parse_timestamp(value)


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
    start = (since.astimezone(UTC).date() if since else None) or (
        moment - timedelta(days=30)
    ).date()
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
        findings.append(f"mac_allocated_at ({record.get('mac_allocated_at')!r}) is not a timestamp")
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
        # Asked of THIS host's own stack since #4200. It used to be asked of the
        # one global stack, which is why scheduling a second host's release
        # silently turned the first host's "scheduled" flag into a lie that this
        # check then reported against the *second* host instead of the first.
        host_id = str(record.get("mac_host_id") or "")
        if not release_stack_exists(host_id):
            # Structural only, and deliberately says nothing about billing
            # (#4181): a finding is reported over a record that has NOT been
            # confirmed at AWS, and "it is still billing" is a claim about AWS
            # that only `observation.usable` may license. What is free to state
            # here is what this machine holds -- the flag, and the absence of
            # the stack the flag refers to.
            findings.append(
                "mac_release_scheduled is true but no release schedule for this host exists on "
                "this machine, so nothing here will release it"
            )
    return findings


# --- Consent -----------------------------------------------------------


def format_allocation_disclosure(
    plan: MacAllocationPlan,
    releasable_at: datetime,
    *,
    already_billing: list[dict[str, Any]] | None = None,
) -> str:
    """The block printed before anything is allocated.

    Everything an operator needs to decide, in the order they need it: what is
    being created, where, what it costs, and -- the part nothing else in AWS
    tells you until it is too late -- that they cannot stop paying for it for
    a day.

    `already_billing` is `pending_releases()` (#4200). A host this machine has
    already allocated and not seen released is named here, with its own release
    time, because "this is a non-refundable charge" and "this is your SECOND
    non-refundable charge, and the first one is still running" are different
    decisions. The disclosure is what the operator is given to make it with.
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
    outstanding = already_billing or []
    if outstanding:
        lines.extend(
            [
                "",
                f"WARNING: {len(outstanding)} Dedicated Host(s) allocated from this machine have "
                "NOT been released. This allocation is IN ADDITION to them, and each one carries "
                f"its own {HOST_MINIMUM_HOURS}-hour minimum:",
            ]
        )
        for host in outstanding:
            if host.get("release_schedule_present"):
                fate = f"release scheduled for {host.get('release_at') or 'an unknown time'} UTC"
            else:
                fate = "NOT scheduled to be released -- nothing here will release it"
            rate = host.get("hourly_rate")
            cost = f", ${float(rate):.4f}/hour" if rate else ""
            lines.append(
                f"  {host.get('host_id')} ({host.get('instance_type') or 'unknown type'} in "
                f"{host.get('region') or 'unknown'}{cost}) -- {fate}"
            )
        lines.append(
            "If you meant to reuse one of those, answer no and run `nyxgpt cloud status` -- a "
            "host that AWS confirms is still allocated is reused by this command rather than "
            "re-allocated."
        )
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
        # `ConsentDeclined`, not a failure (#4181). No answer is not consent,
        # and the outcome is identical to typing anything else: nothing exists,
        # nothing is billed, and nothing needs re-running unless the operator
        # wants the host after all.
        raise ConsentDeclined(
            "No confirmation was given, so nothing was allocated and nothing is billed. "
            "Pass --yes to allocate from a non-interactive run."
        ) from exc
    if str(answer).strip().lower() != CONFIRMATION_WORD:
        raise ConsentDeclined(
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


def amend_mac_record(updates: dict[str, Any], *, host_id: str, reason: str = "") -> dict[str, Any]:
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


# --- The per-host release stack: one host, one stack (#4200) -----------


def host_slug(host_id: str) -> str:
    """The filename- and AWS-name-safe part of a Dedicated Host id.

    `h-0c8f9957132fb0794` -> `0c8f9957132fb0794`. The `h-` prefix is dropped
    because every one of these carries it, and anything outside `[0-9a-z]` is
    replaced so a malformed id recorded by an older build still produces a
    usable path instead of escaping the directory.
    """
    cleaned = "".join(character if character.isalnum() else "-" for character in host_id.lower())
    return cleaned[2:] if cleaned.startswith("h-") else cleaned


def release_paths(host_id: str) -> tuple[Path, Path]:
    """`(tfstate, tfvars)` for the release stack of exactly `host_id` (#4200).

    Per host, so scheduling the release of a second host cannot rewrite the
    first host's stack. The legacy single-slot pair is returned instead when it
    is the stack that *already* describes this host: those files are the only
    record of resources that exist in AWS, and renaming them underneath a live
    stack would orphan a schedule rather than adopt it. A host with no legacy
    claim gets its own pair, so one upgrade converges the layout without a
    migration step.
    """
    slug = host_slug(host_id)
    if not slug:
        return (MAC_RELEASE_TFSTATE_FILE, MAC_RELEASE_TFVARS_FILE)
    if MAC_RELEASE_TFVARS_FILE.exists() and _tfvars_host(MAC_RELEASE_TFVARS_FILE) == host_id:
        return (MAC_RELEASE_TFSTATE_FILE, MAC_RELEASE_TFVARS_FILE)
    return (
        cloud_infra.CLOUD_DIR / f"mac-release-{slug}.tfstate",
        cloud_infra.CLOUD_DIR / f"mac-release-{slug}.tfvars",
    )


def release_stack_exists(host_id: str) -> bool:
    """Is there a release stack on this machine for `host_id`?

    The honest form of "is this host scheduled to be released". The record's
    `mac_release_scheduled` flag is a claim a previous run wrote down; this is
    the Terraform state that claim depends on, and the two disagreeing is one of
    the incoherences `record_findings` reports.
    """
    state_file, tfvars = release_paths(host_id)
    return state_file.exists() and _tfvars_host(tfvars) == host_id


def release_stack_hosts() -> list[str]:
    """Every host id a release stack on this machine names, sorted.

    Read from the tfvars rather than from the record, so a host whose block was
    lost is still found -- which is the situation #4200 is about.
    """
    found: set[str] = set()
    try:
        candidates = sorted(cloud_infra.CLOUD_DIR.glob("mac-release*.tfvars"))
    except OSError:  # pragma: no cover - an unreadable cloud dir
        return []
    for tfvars in candidates:
        host_id = _tfvars_host(tfvars)
        if host_id and tfvars.with_suffix(".tfstate").exists():
            found.add(host_id)
    return sorted(found)


def _tfvars_host(path: Path) -> str:
    """The `host_id` a release-stack tfvars names, or `""`."""
    if not path.exists():
        return ""
    return str(_read_tfvars(path).get("host_id") or "")


# --- The host ledger: every host that still bills (#4200) --------------
#
# `state.json`'s Mac block describes the host the *current* deployment sits on,
# and a deploy replaces it whole. A Dedicated Host outlives that deployment by
# construction -- it is still allocated after the instance is terminated, and
# AWS refuses to release it for 24 hours -- so a single-slot record cannot track
# two of them, and the one it drops is the one nothing then reports or releases.
#
# This ledger is keyed by host id and holds an entry for every host from the
# moment it is allocated until AWS confirms it is gone. It is the one source for
# "which hosts still cost money": `pending_releases`, `nyxgpt cloud status`, the
# API payload, both dashboard cards and `teardown`'s scheduling loop all read it
# rather than deciding for themselves.

#: Fields a ledger entry carries. Deliberately a subset of the Mac block's: the
#: ledger exists to see and release a host, not to redescribe the deployment
#: that used to be on it. Mapped to and from `mac_`-prefixed block keys by
#: `_ledger_from_block` / `_block_from_ledger`, so `observe_host` and
#: `record_findings` read a ledger entry with no second code path.
LEDGER_FIELDS: tuple[str, ...] = (
    "host_id",
    "instance_type",
    "region",
    "availability_zone",
    "allocated_at",
    "release_at",
    "hourly_rate",
    "release_scheduled",
    "release_scheduled_at",
    "verified_at",
    "host_present",
    "verify_error",
    "verified_profile",
    "verified_account_id",
)


def load_host_ledger() -> list[dict[str, Any]]:
    """Every host still on the ledger, oldest allocation first.

    Entries with no `host_id` are dropped on the way out: a row that cannot name
    its host cannot be acted on, and leaving it in would make every caller
    guard for it.
    """
    block = cloud_record.load_block(cloud_record.SUBSTRATE_MAC_HOSTS)
    raw = block.get("mac_hosts")
    if not isinstance(raw, list):
        return []
    entries = [
        {key: entry[key] for key in LEDGER_FIELDS if key in entry}
        for entry in raw
        if isinstance(entry, dict) and entry.get("host_id")
    ]
    return sorted(entries, key=lambda entry: str(entry.get("allocated_at") or ""))


def _write_host_ledger(entries: list[dict[str, Any]], *, reason: str) -> list[dict[str, Any]]:
    """Replace the ledger with `entries`. An empty list clears the block."""
    if not entries:
        cloud_record.clear_block(cloud_record.SUBSTRATE_MAC_HOSTS, reason=reason)
        return []
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC_HOSTS,
        {"mac_hosts": entries},
        reason=reason,
    )
    return load_host_ledger()


def ledger_entry(host_id: str) -> dict[str, Any]:
    """The ledger's entry for `host_id`, or `{}`."""
    for entry in load_host_ledger():
        if str(entry.get("host_id") or "") == host_id:
            return entry
    return {}


def remember_host(values: dict[str, Any], *, reason: str = "") -> list[dict[str, Any]]:
    """Add or update `values["host_id"]`'s ledger entry. Returns the whole ledger.

    Merged per entry rather than replaced, because the callers each know a
    different part of the truth: the allocation knows the price and the zone,
    the teardown knows the release is scheduled, the reconcile knows what AWS
    last said. Merging is safe *here* -- unlike the Mac block, where it was the
    #4136 defect -- because the merge is keyed by the host id, so a field can
    only ever land on the host it was resolved for.
    """
    host_id = str(values.get("host_id") or "")
    if not host_id:
        return load_host_ledger()
    updates = {key: values[key] for key in LEDGER_FIELDS if key in values}
    entries = load_host_ledger()
    for index, entry in enumerate(entries):
        if str(entry.get("host_id") or "") == host_id:
            merged = dict(entry)
            for key, value in updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            entries[index] = merged
            break
    else:
        entries.append({key: value for key, value in updates.items() if value is not None})
    return _write_host_ledger(entries, reason=reason or f"recorded Dedicated Host {host_id}")


def forget_ledger_host(host_id: str, *, reason: str = "") -> bool:
    """Drop `host_id` from the ledger. Returns False when it was not on it.

    Called only once AWS has said the host is gone. A host dropped while it
    still bills is the exact failure the ledger exists to prevent, so there is
    deliberately no "tidy up the ledger" path that does not ask AWS first.
    """
    entries = load_host_ledger()
    kept = [entry for entry in entries if str(entry.get("host_id") or "") != host_id]
    if len(kept) == len(entries):
        return False
    _write_host_ledger(kept, reason=reason or f"Dedicated Host {host_id} has been released")
    return True


def _ledger_from_block(block: dict[str, Any]) -> dict[str, Any]:
    """A ledger entry from a Mac block -- the `mac_` prefix dropped."""
    entry = {field: block.get(f"mac_{field}") for field in LEDGER_FIELDS}
    entry["host_id"] = str(block.get("mac_host_id") or "")
    return {key: value for key, value in entry.items() if value is not None}


def _block_from_ledger(entry: dict[str, Any]) -> dict[str, Any]:
    """A Mac-block-shaped dict from a ledger entry -- the `mac_` prefix restored.

    So `observe_host` and `record_findings` are the SAME functions for a ledger
    host as for the current deployment's host. A second copy of "is this host
    confirmed" is exactly the divergence that let one surface assert billing
    over a host another surface had already given up on.
    """
    return {f"mac_{field}": entry[field] for field in LEDGER_FIELDS if field in entry}


def pending_release(args: argparse.Namespace | None = None) -> dict[str, Any]:
    """What `nyxgpt cloud status` reports about a host that may still be billing.

    Empty dict when nothing is outstanding. A resource that still costs money
    must not be the one thing nothing observes (Definition of Done, the
    observability rule) -- and the host outlives both the instance and the
    substrate by construction, so no other status source can see it.

    **The payload separates what is recorded from what may be claimed (#4181).**
    The descriptive fields are the record's, and they are here so an operator
    whose only view of that machine is this file still gets the ids. The
    `observation` block beside them is `observe_host().to_dict()`, and its
    `usable` flag is the ONLY thing any surface -- this CLI, the API, both
    dashboard cards -- may gate a claim of presence, billing or release on.
    `billing` is that flag rather than the `True` it used to be hard-coded to:
    in the owner's acceptance round this payload asserted billing over a host
    AWS no longer had, from a record that contradicted itself, and every reader
    believed it because the payload said so.

    **One host -- the deployment's (#4200).** `pending_releases` is what reports
    every host that still bills, and it is what every surface should list;
    this remains because a single Mac deployment is still the common case and
    the API's `mac_host` key is it.
    """
    return _host_payload(load_mac_record(), args)


def pending_releases(args: argparse.Namespace | None = None) -> list[dict[str, Any]]:
    """Every Dedicated Host that is allocated or pending release (#4200).

    The ONE source for "what still costs money" -- `nyxgpt cloud status`, the
    API payload and both dashboard cards list what this returns rather than
    reading the record themselves. Oldest allocation first, so the host that has
    been billing longest is the one read first.

    Built from the union of three places, because each of them can know about a
    host the other two do not:

    * the **ledger**, which is the record of every host from allocation until
      AWS confirms the release;
    * the **Mac block**, which describes the current deployment's host (and is
      the only one of the three for a record written before the ledger existed);
    * the **release stacks on disk**, which name a host even when both records
      above have been replaced -- the #4200 case exactly. A host found only
      there is reported with what little is known of it rather than omitted,
      because "nothing here describes it" is not a reason to stop billing for
      it.

    A host with no release stack is reported with `release_schedule_present`
    false, which is what the surfaces turn into "NOT scheduled to be released --
    still billing".
    """
    blocks: dict[str, dict[str, Any]] = {}
    for entry in load_host_ledger():
        blocks[str(entry["host_id"])] = _block_from_ledger(entry)
    record = load_mac_record()
    current = str(record.get("mac_host_id") or "")
    if current:
        # The block wins for its own host: it carries the spend figure, the
        # instance and the security group, none of which the ledger holds.
        blocks[current] = record
    for host_id in release_stack_hosts():
        blocks.setdefault(host_id, {"mac_host_id": host_id})
    payloads = [_host_payload(block, args) for block in blocks.values()]
    return sorted(
        (payload for payload in payloads if payload),
        key=lambda payload: (str(payload.get("allocated_at") or ""), str(payload["host_id"])),
    )


def _host_payload(record: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, Any]:
    """The payload every surface reads about one Dedicated Host.

    One builder for the current deployment's host and for a host that only the
    ledger or a leftover release stack still names (#4200) -- so no surface can
    describe the two differently, and the "still billing" wording is decided
    once.
    """
    if not record.get("mac_host_id"):
        return {}
    observation = observe_host(args, record=record)
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
    host_id = str(record.get("mac_host_id") or "")
    # #4200. The record's `mac_release_scheduled` is what some previous run
    # wrote down; this is whether the Terraform stack that would do the
    # releasing is actually on this machine, for THIS host. The two are
    # reported separately and never collapsed, because the defect was precisely
    # a flag that stayed true while the schedule it referred to had been
    # rewritten to name a different host.
    schedule_present = release_stack_exists(host_id)
    return {
        "host_id": host_id,
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
        # Whether *this host's* release stack exists here, and the sentence to
        # print when it does not (#4200). Decided once; every surface prints it
        # rather than inferring "scheduled" from the flag above.
        "release_schedule_present": schedule_present,
        "release_state_file": str(release_paths(host_id)[0]) if host_id else "",
        # Gated on `usable`, like every other claim in this payload (#4181):
        # "still billing" is about AWS, and a reader handed that sentence over
        # an unconfirmed record would state it. Empty when the host is
        # scheduled, and empty when nothing has confirmed the host -- the
        # unconfirmed case has its own wording in `provenance`.
        "unscheduled_warning": (
            ""
            if schedule_present or not observation.usable
            else (
                f"NOT scheduled to be released -- still billing. Nothing on this machine will "
                f"release {host_id}. Run `nyxgpt cloud destroy --yes` to schedule it."
            )
        ),
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
        "verified_at": observation.confirmed_at,
        "host_present": observation.present,
        # Free internal-consistency checks. Non-empty means the block cannot
        # describe any state AWS was ever in, so its fields came from more than
        # one run and must not be acted on.
        "incoherent": list(observation.findings),
        # #4181. Was `True`, unconditionally, in a payload whose whole job is to
        # say whether a charge is running. It is now the one gate: AWS
        # confirmed this host, in this run, under the credentials this run
        # resolved, and the block is internally coherent.
        "billing": observation.usable,
        # The gate itself, plus the sentence to print when it is closed. Every
        # reader reads these two rather than re-deriving the decision -- the
        # dashboard card had its own copy of "verified_at is set, so say still
        # billing", which is how it went on asserting a charge over a record
        # whose own fields contradicted each other.
        "observation": observation.to_dict(),
        "usable": observation.usable,
        "provenance": observation.provenance,
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


def _read_tfvars(path: Path) -> dict[str, Any]:
    """Parse a tfvars file this module wrote back into `{name: value}`.

    Only the shapes `_write_tfvars` emits: `key = "string"`, `key = 123`,
    `key = true`. Comments and blank lines are skipped; anything it cannot
    parse is skipped rather than guessed at, because the caller is about to
    hand the result to `terraform destroy`.
    """
    values: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw = stripped.partition("=")
        if not separator:
            continue
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            values[key] = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            continue
        if raw in ("true", "false"):
            values[key] = raw == "true"
            continue
        try:
            values[key] = int(raw)
        except ValueError:
            continue
    return values


def _restamp_tfvars(path: Path, *, region: str, profile: str) -> None:
    """Rewrite `path`'s credential choice to this run's, keeping everything else (#4181).

    A stored tfvars file is the right thing to reuse for *what* a destroy
    operates on -- those values are exactly what created the resources, and
    rebuilding them would re-derive an availability zone the record may never
    have captured. It is the wrong thing to reuse for *whose credentials* do
    it: that is a property of the current invocation, not of the resource.

    The owner's 2026-10-09 round proved both halves at once. `nyxgpt cloud
    status --profile nyxgpt` ran the release-schedule cleanup against
    `mac-release.tfvars` as rendered by the run that created the schedule on
    2026-10-04 -- which carried `aws_region` and no `aws_profile` line at all --
    so the provider fell through to the default credential chain and
    `terraform destroy` failed AccessDenied on `nyxgpt-tf-mac-release` while
    every boto3 call in the same command authenticated correctly.

    Done in addition to `cloud_identity.credential_env()`, not instead of it:
    the environment covers the roots whose provider has no `aws_profile` to
    read, and this covers the case where a *stale* one would outrank it
    (`profile = var.aws_profile != "" ? var.aws_profile : null`). A missing
    file is a no-op -- the caller renders a fresh one.
    """
    if not path.exists():
        return
    values = _read_tfvars(path)
    if not values:
        return
    values["aws_region"] = region or str(values.get("aws_region") or "")
    # Deliberately assigned even when empty: "" is how the provider is told to
    # use the ambient chain (`_write_tfvars` drops the line), and a run that
    # resolved no named profile must not inherit an earlier run's.
    values["aws_profile"] = profile
    _write_tfvars(path, values)


def _init(config_dir: Path, state_file: Path, *, reconfigure: bool = False) -> None:
    """`terraform init` one of the Mac roots against its own local state file.

    `reconfigure=True` for the release root, which since #4200 is initialized
    against a *different* state file per host from the same working directory.
    Terraform records the backend configuration it last initialized under
    `.terraform/`, and a changed `path` otherwise makes it stop and ask whether
    to migrate the previous host's state into the new file -- a question that
    fails outright under `-input=false`, and whose "yes" would copy host A's
    resources into host B's state and then have Terraform update them to point
    at B. `-reconfigure` adopts the named backend and copies nothing, which is
    the only correct answer: these are two independent stacks that happen to
    share a root module.
    """
    arguments = ["init", "-input=false"]
    if reconfigure:
        arguments.append("-reconfigure")
    arguments.append(f"-backend-config=path={state_file}")
    cloud_infra.run_terraform(arguments, capture=True, chdir=config_dir)


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


def _adopt_allocated_host(plan: MacAllocationPlan, host_id: str) -> bool:
    """Put a still-held Dedicated Host back into the Mac root's state (#4200).

    `terraform import`, and the alternative is allocating a second host. The
    sequence that needs it is the ordinary one: `cloud destroy` calls
    `forget_host` so the destroy cannot try to release the host inside AWS's
    24-hour minimum, which leaves Terraform's state with no `aws_ec2_host.this`
    while the host is very much still allocated and still billing. The next
    `cloud deploy --os macos` then reconciles -- announcing "no new host, no new
    24-hour minimum" -- and `terraform apply` creates the missing resource,
    which on this resource type means `AllocateHosts`.

    A no-op when state already holds the host, which is the common case (a
    re-deploy with no teardown in between).

    **A failure here stops the deploy.** Falling through to the apply would
    spend the operator's money on a second host they did not ask for, so the
    error says what happened and names the host that is already billing. That
    is the inverse of the usual "best effort" treatment, deliberately: the
    cheap, reversible outcome is a deploy that does not run.
    """
    if not host_id or allocated_host_from_state() == host_id:
        return False
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_CONFIG_DIRNAME)
    _write_tfvars(MAC_TFVARS_FILE, _plan_tfvars(plan))
    _init(config_dir, MAC_TFSTATE_FILE)
    print(
        f"Terraform's state does not hold Dedicated Host {host_id} (a previous "
        "`nyxgpt cloud destroy` dropped it so the teardown could not release it early). "
        "Importing it so this deploy reuses the host you are already paying for."
    )
    try:
        cloud_infra.run_terraform(
            [
                "import",
                "-input=false",
                f"-var-file={MAC_TFVARS_FILE}",
                f"{HOST_RESOURCE_TYPE}.{HOST_RESOURCE_NAME}",
                host_id,
            ],
            capture=True,
            chdir=config_dir,
        )
    except CloudCommandError as exc:
        raise CloudCommandError(
            f"Dedicated Host {host_id} is allocated and billing, but it could not be imported "
            f"into Terraform's state: {exc}\n"
            "Nothing was applied, because applying would have allocated a SECOND host and "
            "started a second non-refundable 24-hour minimum.\n"
            "`nyxgpt cloud status` shows the host and its scheduled release; fix what the error "
            "above names and re-run, or run `nyxgpt cloud destroy --yes` to schedule this host's "
            "release and deploy again afterwards."
        ) from exc
    return True


def destroy_mac_instance(plan_values: dict[str, Any]) -> dict[str, Any]:
    """Terminate the Mac and delete its network, leaving the host allocated.

    The tfvars the *apply* wrote are reused when they are still there, and
    only rebuilt from the record when they are not. Same reasoning as
    `cloud_infra.destroy_infra`'s saved settings, one step further: those
    values are exactly what created the resources being destroyed, whereas a
    rebuild has to re-derive an availability zone that has no default and
    would fail the run if the record never captured one.

    What is *not* reused is the credential choice in it: `_restamp_tfvars`
    replaces that with this run's before Terraform reads the file (#4181).
    """
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_CONFIG_DIRNAME)
    if not MAC_TFVARS_FILE.exists():
        _write_tfvars(MAC_TFVARS_FILE, plan_values)
    else:
        _restamp_tfvars(
            MAC_TFVARS_FILE,
            region=str(plan_values.get("aws_region") or ""),
            profile=str(plan_values.get("aws_profile") or ""),
        )
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
    """Create the one-shot schedule that releases `host_id` once it can be released.

    **One stack per host (#4200).** The state file, the tfvars and every AWS
    resource name are derived from `host_id`, so applying a schedule for a
    second host creates a second, independent stack instead of updating the one
    schedule to name the new host -- which left the first host, still inside its
    24-hour minimum and still billing, with nothing scheduled to release it and
    no Slack message to say so.

    Idempotent per host: re-applying for the same host reconciles that host's
    own stack. A stack applied before #4200 keeps the resource names it was
    created with (read back from its tfvars) rather than being renamed, because
    renaming an EventBridge schedule is a destroy-and-create and this one may be
    the only thing that will ever release the host.
    """
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
    state_file, tfvars_file = release_paths(host_id)
    _write_tfvars(
        tfvars_file,
        {
            "aws_region": region,
            "aws_profile": profile,
            # The host id is in every resource name, so two hosts cannot collide
            # on a schedule, a state machine, an IAM role or the EventBridge
            # connection. An existing stack keeps the prefix it was created with.
            "name_prefix": _release_name_prefix(tfvars_file, name_prefix, host_id),
            "host_id": host_id,
            "release_at": scheduler_timestamp(release_at),
            "slack_channel": slack_channel,
            "slack_authorization_header": f"Bearer {slack_bot_token.strip()}",
        },
    )
    _init(config_dir, state_file, reconfigure=True)
    cloud_infra.run_terraform(
        ["apply", "-input=false", "-auto-approve", f"-var-file={tfvars_file}"],
        chdir=config_dir,
    )
    outputs = _outputs(config_dir)
    return {
        "host_id": str(outputs.get("host_id") or host_id),
        "release_at": str(outputs.get("release_at") or scheduler_timestamp(release_at)),
        "schedule_name": str(outputs.get("schedule_name") or ""),
        "state_machine_arn": str(outputs.get("state_machine_arn") or ""),
        "slack_channel": str(outputs.get("slack_channel") or slack_channel),
        "state_file": str(state_file),
    }


def _release_name_prefix(tfvars_file: Path, name_prefix: str, host_id: str) -> str:
    """AWS resource-name prefix for `host_id`'s release stack.

    The host slug is appended so the stacks of two hosts name no resource in
    common -- without it, `aws_scheduler_schedule.release` is called
    `nyxgpt-tf-mac-release` for both and the second apply simply retargets the
    first host's schedule.

    An already-applied stack keeps what its own tfvars says, whatever this run
    would have composed. Changing `name_prefix` renames every resource, and
    renaming is destroy-then-create: for the one-shot schedule that is the only
    thing that will ever release the host, a failed create after a successful
    destroy leaves the host billing with nothing to stop it.
    """
    existing = str(_read_tfvars(tfvars_file).get("name_prefix") or "")
    if existing:
        return existing
    slug = host_slug(host_id)
    return f"{name_prefix}-{slug}" if slug else name_prefix


def destroy_release_stack(*, host_id: str, region: str = "", profile: str = "") -> bool:
    """Tear down one host's completed release stack. Returns False when there is none.

    The schedule deletes itself when it fires, but the state machine, the
    EventBridge connection and the Secrets Manager secret EventBridge creates
    for it do not -- and that secret is the one piece of this design that
    keeps costing (cents per month) after the host is gone. Called at the top
    of the next teardown rather than exposed as a command of its own: the only
    moment nyxGPT can be sure the previous release finished is the next time
    it is asked to schedule one.

    **Never from a read command** (#4181): this is a `terraform destroy`, and
    `nyxgpt cloud status` used to reach it through `reconcile_released_host`.
    `verify_mac_record` now passes `cleanup=False` there, so the only callers
    are `cloud destroy` and `cloud deploy`.

    `region`/`profile` are this run's resolved credential choice, written over
    whatever the stored tfvars said -- see `_restamp_tfvars`. Defaulted rather
    than required so a caller that genuinely has no resolution (a test, a
    teardown with no record) gets the environment's, which is what it had
    before.

    **`host_id` is required, and it names the only stack this touches (#4200).**
    It used to destroy "the release stack", of which there was one -- so
    cleaning up after a finished host would have torn down a *pending* host's
    schedule had the two ever shared the slot. Now the cleanup of one host
    cannot reach another's, which is the acceptance criterion in both
    directions: scheduling B leaves A's schedule alone, and cleaning up A
    leaves B's alone.
    """
    state_file, tfvars_file = release_paths(host_id)
    if not state_file.exists():
        return False
    cloud_infra.sync_terraform_config()
    config_dir = _config_dir(MAC_RELEASE_CONFIG_DIRNAME)
    _restamp_tfvars(tfvars_file, region=region, profile=profile)
    _init(config_dir, state_file, reconfigure=True)
    cloud_infra.run_terraform(
        ["destroy", "-input=false", "-auto-approve", f"-var-file={tfvars_file}"],
        capture=True,
        chdir=config_dir,
    )
    # The stack is gone, so the files that described it must go too: a leftover
    # tfvars naming a released host makes `release_stack_hosts` report it as
    # still pending, which is the same class of defect in the other direction.
    state_file.unlink(missing_ok=True)
    tfvars_file.unlink(missing_ok=True)
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


@dataclass(frozen=True)
class HostPresence:
    """AWS's answer about one Dedicated Host, with the reason when there is none.

    `present` is the three-valued answer `host_still_allocated` has always
    returned; `reason` is what #4181 finding 6 was missing. Every surface that
    reports an unconfirmed host used to have to word the cause itself, and the
    only wording available was "nobody has asked yet" -- which `nyxgpt cloud
    status` printed about itself over a run whose real problem was that boto3
    was not installed.
    """

    present: bool | None
    reason: str = ""


def host_presence(host_id: str, region: str, profile: str = "") -> HostPresence:
    """Ask AWS whether `host_id` is still allocated, carrying why if it cannot.

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

    **`InvalidHostID.NotFound` is only that answer from the account that owns
    the host** (#4181). An AWS account reports every host it does not own as
    not-found, so the caller records the profile this answer came from
    alongside it and `cloud_verified.observe` refuses to treat an answer from
    another account as a confirmation. Live, `nyxgpt cloud status` asked
    `arn:aws:iam::551292530955:root` about a host in 066835328281 and would
    have cleared a still-billing record on the strength of it.
    """
    if not host_id:
        return HostPresence(present=False)
    try:
        client = _client("ec2", region, profile)
        response = client.describe_hosts(HostIds=[host_id])
    except Exception as exc:
        code = _aws_error_code(exc).replace("-", "").lower()
        if code in HOST_NOT_FOUND_CODES:
            return HostPresence(present=False)
        return HostPresence(present=None, reason=f"AWS could not be asked: {exc}")
    for host in response.get("Hosts", []):
        if str(host.get("HostId") or "") != host_id:
            continue
        # `released` and `released-permanent-failure` are terminal; anything
        # else (available, under-assessment, pending) is still allocated.
        return HostPresence(present=not str(host.get("State") or "").startswith("released"))
    return HostPresence(present=False)


def host_still_allocated(host_id: str, region: str, profile: str = "") -> bool | None:
    """`host_presence(...).present` -- the three-valued answer on its own.

    Kept as the name the callers that only need the verdict use, and as the
    seam `scripts/cloud_stale_record_smoke.py`'s negative control replaces.
    """
    return host_presence(host_id, region, profile).present


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
        # The one reader, so the gate that may skip a priced disclosure and the
        # row that reports a charge cannot disagree about this record -- and the
        # refusal carries that reader's own reason rather than a generic one
        # (#4181).
        observation = observe_host(args, record=existing)
        if not observation.confirmed:
            raise CloudCommandError(_unconfirmed_host_message(host_id, observation.reason))
        return _reconcile_existing(args, existing)
    # No usable record, but Terraform already holds a host: a previous run
    # allocated it and then failed before recording it. Heal the record and
    # reconcile -- never re-disclose (#4122). Still only on a confirmed host:
    # Terraform state outlives the resource it names just as the record does.
    orphaned_host = allocated_host_from_state()
    if orphaned_host:
        presence = host_presence(
            orphaned_host, _record_region(existing, args), _record_profile(args)
        )
        present = presence.present
        if present is None:
            raise CloudCommandError(_unconfirmed_host_message(orphaned_host, presence.reason))
        if present:
            _heal_orphaned_record(args, orphaned_host, existing)
            _record_verification(orphaned_host, True, profile=_record_profile(args), args=args)
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
    # Every host this machine has allocated and not seen released, named in the
    # disclosure (#4200). The reconcile above has just asked AWS about each of
    # them, so what is left here is what is really still outstanding.
    confirm_allocation(
        format_allocation_disclosure(plan, releasable_at, already_billing=pending_releases(args)),
        assume_yes=assume_yes,
    )

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
            remember_host(
                {
                    "host_id": stranded,
                    "instance_type": plan.instance_type,
                    "region": plan.region,
                    "availability_zone": plan.availability_zone,
                    "allocated_at": allocated_at.isoformat(),
                    "release_at": releasable_at.isoformat(),
                    "hourly_rate": (plan.pricing.hourly_rate if plan.pricing else None),
                    "release_scheduled": False,
                },
                reason=f"Dedicated Host {stranded} stranded by a failed apply",
            )
            print(
                f"\nWARNING: Dedicated Host {stranded} WAS allocated before this failure and is "
                "billing. It has been recorded, so `nyxgpt cloud status` names it, its release "
                "time and what it has cost, and re-running the deploy reconciles it rather than "
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
            # `DescribeHosts` to prove. The credentials are recorded with it
            # (#4181): a confirmation that cannot say which account it was made
            # in is not one `observe_host` will honour, and without this the
            # very next command would report the host it had just allocated as
            # unconfirmed.
            "mac_verified_at": utc_now().isoformat(),
            "mac_host_present": True,
            "mac_verified_profile": plan.profile or None,
            "mac_verified_account_id": cloud_identity.account_default(args).account_id or None,
        },
        reason=f"allocated Dedicated Host {host_id}",
    )
    # And onto the ledger, which is what keeps this host visible and releasable
    # after a later deploy replaces the block above (#4200).
    remember_host(_ledger_from_block(record), reason=f"allocated Dedicated Host {host_id}")
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
    burns the one-shot schedule on a rejection. The disclosure that follows says
    the release time is a lower bound; what the host has *cost* does not inherit
    that uncertainty, because since #4136 it comes from Cost Explorer rather than
    from this timestamp.

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
        "Its allocation time could not be recovered, so the release time reported from here is "
        f"measured from now ({allocated_at.isoformat()}) and is a lower bound on the age of the "
        "host. What it has cost comes from AWS Cost Explorer and does not depend on that.",
        file=sys.stderr,
    )
    # Both timestamps come from the *same* moment, so the record cannot end up
    # with a release time that is not its allocation time plus AWS's minimum --
    # one of the incoherences `record_findings` reports.
    recorded_allocation = parse_timestamp(str(existing.get("mac_allocated_at") or "")) or (
        allocated_at
    )
    record = record_mac_host(
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
    remember_host(
        _ledger_from_block(record),
        reason=f"adopted Dedicated Host {host_id} from Terraform state",
    )
    return record


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
            # And which credentials got that confirmation (#4181). Dropping it
            # while keeping `mac_verified_at` would leave a record that claims
            # an answer and cannot say whose account it came from -- which
            # `observe_host` correctly refuses to treat as a confirmation, so
            # the reconcile would un-verify the host it just verified.
            "mac_verified_profile",
            "mac_verified_account_id",
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
    # The host is still held and AWS has confirmed it in this run, so this
    # deploy reuses it. That requires Terraform to be holding it too (#4200):
    # `cloud destroy` removes `aws_ec2_host.this` from state on purpose, so that
    # the destroy does not call ReleaseHosts inside the 24-hour window -- and a
    # re-apply against that state does not reconcile the host, it ALLOCATES A
    # SECOND ONE, starting a second non-refundable 24-hour minimum with no
    # disclosure. Importing it first is what makes "deploy, destroy, deploy"
    # reuse the Mac the operator is already paying for.
    _adopt_allocated_host(plan, expected_host)
    outputs = apply_mac_host(plan)
    record = record_mac_host(
        _reconciled_block(existing, outputs, plan),
        reason=f"reconciled Dedicated Host {expected_host}",
    )
    remember_host(
        _ledger_from_block(record),
        reason=f"reconciled Dedicated Host {record.get('mac_host_id')}",
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


def reconcile_released_host(args: argparse.Namespace, *, cleanup: bool = True) -> bool:
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

    **`cleanup=False` makes this safe for a read command (#4181).** With it,
    the one `DescribeHosts` still happens and the record is still corrected --
    both are local bookkeeping, and correcting the record only ever *removes* a
    claim this machine could no longer support -- but nothing in AWS is
    touched, and the Terraform state and tfvars are left alone. That is the
    whole of finding 4: `nyxgpt cloud status` reached `destroy_release_stack`
    through here, so a read-only command ran `terraform destroy` on the release
    schedule. Cleanup belongs to `cloud destroy` and `cloud deploy`, which pass
    the default.

    **It asks about every host, not only the deployment's (#4200).** Delegated
    to `reconcile_released_hosts`; the boolean it returns is still specifically
    about the current deployment's host, because that is what the callers
    branch on -- `teardown` has nothing left to tear down, `allocate` has no
    host to reconcile.
    """
    record = load_mac_record()
    host_id = str(record.get("mac_host_id") or "")
    cleared = reconcile_released_hosts(args, cleanup=cleanup)
    return bool(host_id) and host_id in cleared


def tracked_hosts() -> list[str]:
    """Every Dedicated Host this machine has any trace of, oldest first (#4200).

    The union of the three places a host can be named -- the ledger, the Mac
    block, and a release stack's tfvars -- and the one answer to "which hosts
    must we ask AWS about". A host named by only one of them is still a host
    that may be billing, and the reason the union is taken rather than the
    intersection is that each of the three has, at some point, been the only one
    that knew.
    """
    ordered = [str(entry["host_id"]) for entry in load_host_ledger()]
    current = str(load_mac_record().get("mac_host_id") or "")
    for host_id in ([current] if current else []) + release_stack_hosts():
        if host_id and host_id not in ordered:
            ordered.append(host_id)
    return ordered


def reconcile_released_hosts(args: argparse.Namespace, *, cleanup: bool = True) -> list[str]:
    """Forget every tracked host AWS says is gone. Returns the host ids cleared.

    One `DescribeHosts` per tracked host, and only a definite *no* acts: a host
    whose answer could not be obtained stays on the books, because a record
    deleted on the strength of expired credentials hides a resource that is
    still costing money. Never raises -- a reconcile that cannot run must not
    stop the command it runs ahead of.
    """
    record = load_mac_record()
    current = str(record.get("mac_host_id") or "")
    profile = _record_profile(args)
    cleared: list[str] = []
    for host_id in tracked_hosts():
        entry = ledger_entry(host_id)
        block = record if host_id == current else _block_from_ledger(entry)
        region = _record_region(block, args)
        try:
            presence = host_presence(host_id, region, profile)
        except Exception as exc:  # pragma: no cover - host_presence swallows its own
            presence = HostPresence(present=None, reason=f"AWS could not be asked: {exc}")
        # Written down whatever the answer was, including "could not ask" and the
        # reason for it (#4181). An unrecorded failure is why every reader had to
        # word the unconfirmed case as "nobody has asked yet".
        if host_id == current:
            _record_verification(host_id, presence, profile=profile, args=args)
        _remember_verification(host_id, presence, profile=profile, args=args)
        if presence.present is not False:
            continue
        print(
            f"Dedicated Host {host_id} has been released -- AWS no longer has it. "
            "Clearing it from this machine's cloud state."
        )
        if cleanup:
            try:
                destroy_release_stack(host_id=host_id, region=region, profile=profile)
            except Exception as exc:  # pragma: no cover - best effort by design
                print(
                    f"note: could not clean up the released host's schedule: {exc}",
                    file=sys.stderr,
                )
        forget_ledger_host(host_id, reason=f"AWS reports Dedicated Host {host_id} as released")
        if host_id == current:
            clear_mac_record(reason=f"AWS reports Dedicated Host {host_id} as released")
            if cleanup:
                MAC_TFSTATE_FILE.unlink(missing_ok=True)
                MAC_TFVARS_FILE.unlink(missing_ok=True)
        if not cleanup and release_stack_exists(host_id):
            print(
                f"note: the released host {host_id}'s EventBridge schedule stack is still on "
                "this machine. `nyxgpt cloud status` observes and changes nothing, so it does "
                "not tear it down; `nyxgpt cloud destroy --yes` and the next `nyxgpt cloud "
                "deploy --os macos` do.",
                file=sys.stderr,
            )
        cleared.append(host_id)
    return cleared


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


def _record_profile(args: argparse.Namespace | None) -> str:
    """Credential profile for an AWS call about the recorded host.

    Unlike the region this is *not* a property of the resource -- it is which
    credentials to use now -- so the flag wins over the saved setting.

    Delegated to the one resolver since #4186: this was the second hand-written
    copy of the chain (`cloud._resolve_profile` was the first), and it stopped
    two steps short of it -- no `config.ini [cloud] profile`, no `AWS_PROFILE`
    -- so an operator who had set either one, and never run `cloud infra
    apply`, had their EC2 Mac host looked up in whatever account boto3's
    default profile names.

    `None` is accepted since #4181 so a status surface with no parsed flags
    reaches the same resolver -- it then reads the flags `bind_run_args`
    recorded for this invocation, rather than starting the chain one step down.
    """
    return cloud_identity.account_default(args).profile


def _record_verification(
    host_id: str,
    presence: HostPresence | bool,
    *,
    profile: str = "",
    args: argparse.Namespace | None = None,
) -> None:
    """Write down what AWS said about `host_id`, when, and under whose credentials.

    Amended rather than written whole, and gated on the host id: the point of
    the field is that it describes *this* host, so landing it on a block that
    has since been replaced would make the record less trustworthy than having
    no verification at all. Best effort.

    Three fields beyond the answer, all added by #4181, and each closes a way a
    reader could state something nothing established:

    * `mac_verify_error` -- why AWS could not be asked. Without it the only
      wording available for an unconfirmed host was "nothing has asked yet",
      which `nyxgpt cloud status` printed about itself when the real reason was
      a missing boto3.
    * `mac_verified_profile` / `mac_verified_account_id` -- which credentials
      got the answer. An AWS account reports every host it does not own as
      not-found, so without these an `InvalidHostID.NotFound` from the default
      account reads exactly like a release.

    A `bool` is accepted for the one caller that *created* the resource and so
    needs no AWS round trip to know it exists.
    """
    answer = presence if isinstance(presence, HostPresence) else HostPresence(present=presence)
    # A local read: the id is recorded by the resolver at provision time
    # precisely so reporting the account later costs no STS call.
    account = cloud_identity.account_default(args)
    try:
        amend_mac_record(
            {
                "mac_verified_at": utc_now().isoformat(),
                "mac_host_present": answer.present,
                # `None` removes the field -- an error from a previous run must
                # not outlive the answer that replaced it.
                "mac_verify_error": answer.reason or None,
                "mac_verified_profile": profile or None,
                "mac_verified_account_id": (
                    account.account_id if account.profile == profile else None
                ),
            },
            host_id=host_id,
            reason="recorded what AWS said about the host",
        )
    except Exception:  # pragma: no cover - a record that moved under us
        return


def _remember_verification(
    host_id: str,
    presence: HostPresence | bool,
    *,
    profile: str = "",
    args: argparse.Namespace | None = None,
) -> None:
    """The same answer, written onto `host_id`'s ledger entry (#4200).

    The ledger needs its own copy because it outlives the Mac block: a host that
    is no longer the deployment's has no block to carry `mac_verified_at`, and
    without it every surface would have to word that host as "nobody has asked"
    forever -- over a host that is billing. Keyed by host id, so unlike the
    block's version it needs no staleness gate: the fields cannot land on
    another host.
    """
    answer = presence if isinstance(presence, HostPresence) else HostPresence(present=presence)
    account = cloud_identity.account_default(args)
    if not ledger_entry(host_id) and answer.present is False:
        # Nothing to remember about a host AWS says is gone and the ledger does
        # not list: `reconcile_released_hosts` forgets a released host on the
        # very next statement, so creating the entry here would write the ledger
        # twice to end where it started -- and would briefly publish an entry
        # asserting a host that no longer exists.
        return
    # Otherwise the entry is CREATED when it is missing (`remember_host` appends
    # keyed by host id). A host can be tracked by a leftover release stack alone
    # -- a pre-#4200 record, or a run that died between the allocation and the
    # ledger write -- and `tracked_hosts()` therefore asks AWS about it. Before
    # this, that answer had nowhere to land: the early return discarded it, so
    # every surface went on saying "nothing has asked AWS about this host"
    # immediately after a run had asked, over a host that is billing. Which is
    # the same class as #4200 itself -- a fact about a billing resource with no
    # per-resource place to live is a fact that gets dropped.
    remember_host(
        {
            "host_id": host_id,
            "verified_at": utc_now().isoformat(),
            "host_present": answer.present,
            "verify_error": answer.reason or None,
            "verified_profile": profile or None,
            "verified_account_id": (account.account_id if account.profile == profile else None),
        },
        reason=f"recorded what AWS said about {host_id}",
    )


# How recent a `DescribeHosts` confirmation has to be to count as "this run".
# One definition, shared with the Linux substrate's identical question (#4181):
# two modules disagreeing about how old an answer may be is two modules
# disagreeing about whether AWS confirmed something.
VERIFICATION_MAX_AGE_SECONDS = cloud_verified.CONFIRMATION_MAX_AGE_SECONDS


def observe_host(
    args: argparse.Namespace | None = None,
    *,
    record: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> cloud_verified.Observation:
    """The ONE reader of the Mac block: what may be stated, and on whose authority.

    Every surface that describes the Dedicated Host goes through this --
    `pending_release` (and so `nyxgpt cloud status`, the API payload and the
    dashboard card), `allocate`'s reconcile gate, and the remedies. Before
    #4181 each of them read the raw record and decided for itself: `allocate`
    required a fresh confirmation, `pending_release` shipped a hard-coded
    `"billing": True`, and the deploy-attempt report concluded "an instance
    exists and is being billed" from the presence of a host id. All three were
    looking at the same block, and in the owner's acceptance round they
    disagreed about it inside one command's output.

    The observation it returns answers in two registers, and the difference is
    the whole point: `fact(key)` yields a value only when AWS confirmed this
    host in this run under these credentials *and* the block is internally
    coherent, while `reported(key)` always yields it for a row labelled as
    what the record holds. A claim site that calls `fact` cannot assert a
    remembered value; one that calls `reported` is not asserting anything.
    """
    block = load_mac_record() if record is None else record
    profile = _record_profile(args)
    account = cloud_identity.account_default(args)
    if not block.get("mac_host_id"):
        # No host recorded at all. An observation of nothing, so that a caller
        # never has to special-case the absence before it can ask `usable`.
        return cloud_verified.observe(
            {},
            reason=cloud_verified.NOT_ASKED,
            account_label=cloud_identity.recorded_account_label(profile, account.account_id),
            now=now,
        )
    recorded_profile = str(block.get("mac_verified_profile") or "")
    return cloud_verified.observe(
        block,
        present=(
            bool(block.get("mac_host_present"))
            if block.get("mac_host_present") is not None
            else None
        ),
        confirmed_at=str(block.get("mac_verified_at") or ""),
        profile=recorded_profile,
        account_id=str(block.get("mac_verified_account_id") or ""),
        account_label=cloud_identity.recorded_account_label(
            recorded_profile, str(block.get("mac_verified_account_id") or "")
        ),
        findings=tuple(record_findings(block)),
        reason=str(block.get("mac_verify_error") or ""),
        # The account this run resolved. An answer obtained with any other one
        # is about a host in another account, and "not found" there is not a
        # release here (#4181 finding 1).
        expect_profile=profile,
        now=now,
    )


def host_confirmed_this_run(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Did AWS confirm this record's host within the last few minutes?

    The gate on every path that skips the priced disclosure and the `allocate`
    prompt (#4136). "The record names a host" is not evidence the host exists;
    `mac_host_present` plus a fresh `mac_verified_at` is, and nothing writes
    those except an actual answer from `DescribeHosts`.

    Answered by `observe_host` since #4181 -- one definition of "confirmed",
    shared with every reporting surface, so the gate that lets a deploy skip a
    priced disclosure and the row that tells an operator the host is billing
    can never disagree about the same record.
    """
    if not record.get("mac_host_id"):
        return False
    return observe_host(record=record, now=now).confirmed


def _unconfirmed_host_message(host_id: str, reason: str = "") -> str:
    """Why a reconcile cannot proceed when AWS could not be asked about `host_id`.

    `reason` is the observation's own, so the remedy matches the cause (#4181).
    This used to say only "AWS could not be asked", leaving an operator whose
    boto3 was missing, or whose profile pointed at the wrong account, to run
    `credentials-setup` against a problem it does not fix -- the same defect as
    finding 6, in a remedy rather than a status row.
    """
    detail = f" -- {reason}" if reason else ""
    return (
        f"Dedicated Host {host_id} is recorded on this machine, but AWS could not be asked "
        f"whether it still exists in this run{detail}. So nyxGPT cannot tell a host you are "
        "already paying for from one that was released.\n"
        "Reconciling on the record alone is how a deploy came to announce 'no new host, no new "
        "24-hour minimum' and then allocate one (#4136), so nothing is applied and nothing is "
        "billed here.\n"
        "Fix what that reason names -- `nyxgpt cloud credentials-setup` for credentials, "
        "`--profile`/`--region` for the wrong account, the cloud extra for a missing boto3 -- "
        "and re-run. `nyxgpt cloud status` shows what is recorded in the meantime."
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
    profile from the one resolver, which since #4181 can see the flags this run
    was given even here.

    **This is a read, and it changes nothing in AWS** (#4181). It used to reach
    `destroy_release_stack` -- a `terraform destroy` -- through the reconcile,
    so `nyxgpt cloud status` mutated the account it was reporting on. The
    `cleanup=False` below is that fix: the `DescribeHosts` still happens, the
    record is still corrected (local, and it only ever withdraws a claim), and
    tearing the finished schedule stack down is left to `cloud destroy` and
    `cloud deploy`, which are the commands an operator ran to change something.
    """
    args = args if args is not None else argparse.Namespace()
    if reconcile_released_host(args, cleanup=False):
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
    # Suppressed: the only way this raises is `StaleRecordError`, meaning the
    # block was replaced while the cost query was in flight. The figure then
    # describes the previous host and must not be written over the new one --
    # which is the whole point of the gate.
    with contextlib.suppress(Exception):  # pragma: no cover - a record that moved under us
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
    return {"host_present": record.get("mac_host_present"), "spend": spend.to_dict()}


def teardown(args: argparse.Namespace) -> dict[str, Any]:
    """Terminate the Mac now and schedule the host release for when AWS allows it.

    Never raises. Every failure is collected into `errors` and returned,
    because this runs inside `nyxgpt cloud destroy` and a Dedicated Host that
    cannot be scheduled for release must not take the rest of the teardown
    with it -- the substrate, the tunnel and the deploy record all still have
    to come down, and an operator with a stuck host needs the *other* things
    gone so the one that is left is unambiguous.

    **It covers every host, not only this deployment's (#4200).** Once the
    current host is dealt with, any other host still on the ledger without a
    release schedule gets one -- and the early returns below are only taken when
    there is no such backlog, because "this deployment is already gone" was
    never a reason to leave another host billing with nothing to release it.
    """
    # Read before the reconcile, because the reconcile is what clears it: the
    # question "was THIS deployment's host the one that turned out to be
    # released" cannot be asked of a record that no longer names it.
    deployed_host = str(load_mac_record().get("mac_host_id") or "")
    released = reconcile_released_hosts(args)
    record = load_mac_record()
    host_id = str(record.get("mac_host_id") or "")
    outstanding = [host for host in tracked_hosts() if not release_stack_exists(host)]
    if deployed_host and deployed_host in released and not outstanding:
        return {"managed": False, "already_released": True}
    if not record and not mac_state_exists() and not outstanding:
        return {"managed": False}
    if not record and not mac_state_exists():
        # No deployment left here, but hosts are. Schedule their releases and
        # report that as the whole of this teardown.
        saved_region = str(getattr(args, "region", None) or "") or str(
            cloud_infra.load_settings().get("aws_region") or ""
        )
        profile = _record_profile(args)
        errors: list[str] = []
        schedules = _schedule_outstanding_hosts(saved_region, profile, skip="", errors=errors)
        return {
            "managed": False,
            "already_released": bool(released),
            "other_schedules": schedules,
            "errors": errors,
        }

    saved = cloud_infra.load_settings()
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
    # The one resolver, not a third copy of the chain (#4186).
    profile = _record_profile(args)
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

    # A release stack whose schedule has already fired and deleted itself still
    # holds a state machine, a connection and the Secrets Manager secret
    # EventBridge made for it. This is the moment we can be sure it is finished,
    # so it is the moment it gets cleaned up -- and since #4200 it is cleaned up
    # **per host**, only for hosts AWS has confirmed are gone, which
    # `reconcile_released_hosts` has just done at the top of this function. A
    # stack belonging to a host that is still allocated is never touched here:
    # tearing it down would remove the only thing that will release that host.
    result["errors"].extend(_cleanup_finished_release_stacks(region, profile, keep=host_id))

    if host_id:
        result["schedule"] = _schedule_one(
            host_id, release_at, region, profile, errors=result["errors"]
        )
        result["release_scheduled"] = bool(result["schedule"])

    # Every OTHER host still on the ledger with no schedule of its own (#4200).
    # Before this, a host that lost its slot to a later one was never looked at
    # again by anything: it had no schedule, no Slack message, and no row on
    # `cloud status`. A teardown is the one command that is already resolving
    # credentials and Terraform, so it is where the backlog gets cleared.
    result["other_schedules"] = _schedule_outstanding_hosts(
        region, profile, skip=host_id, errors=result["errors"]
    )

    try:
        destroyed = destroy_mac_instance(_teardown_tfvars(record, region, profile))
        result["instance_terminated"] = bool(destroyed.get("instance_terminated"))
        result["host_forgotten"] = bool(destroyed.get("host_forgotten"))
    except Exception as exc:
        result["errors"].append(f"the Mac instance could not be torn down: {exc}")

    return result


def _schedule_one(
    host_id: str,
    release_at: datetime,
    region: str,
    profile: str,
    *,
    errors: list[str],
) -> dict[str, Any]:
    """Apply `host_id`'s own release stack and record that it exists.

    The one place a release gets scheduled, so the Slack settings, the name
    prefix and the two records that say "this host's release is scheduled" are
    decided once however many hosts a teardown has to cover.
    """
    channel, token = _slack_settings()
    try:
        schedule = apply_release_schedule(
            host_id=host_id,
            release_at=release_at,
            region=region,
            profile=profile,
            slack_channel=channel,
            slack_bot_token=token,
            name_prefix=f"{cloud_infra.load_settings().get('name_prefix') or 'nyxgpt-tf'}-mac",
        )
    except Exception as exc:
        errors.append(str(exc))
        return {}
    scheduled_at = utc_now().isoformat()
    # The ledger first, and unconditionally: it is keyed by host id, so it
    # cannot be stale, and it is the record that outlives the deployment.
    remember_host(
        {
            "host_id": host_id,
            # The record's own ISO form, not the scheduler's naive `at()`
            # timestamp: every reader here parses offset-aware timestamps, and
            # a ledger row carrying one of each is a row whose two time fields
            # cannot be compared.
            "release_at": release_at.isoformat(),
            "release_scheduled": True,
            "release_scheduled_at": scheduled_at,
        },
        reason=f"deferred release scheduled for {host_id}",
    )
    # Amended, not written whole, and gated on the host id (#4136): this says
    # "the release of *this* host is scheduled", and landing it on a block that
    # has since been replaced is how a `mac_release_scheduled_at` 78 seconds
    # earlier than the `mac_allocated_at` of the host it described got written.
    # A mismatch is no longer an error -- the ledger above has already recorded
    # it, and this host simply is not the one the block describes.
    with contextlib.suppress(cloud_record.StaleRecordError):
        amend_mac_record(
            {"mac_release_scheduled": True, "mac_release_scheduled_at": scheduled_at},
            host_id=host_id,
            reason="deferred release scheduled",
        )
    return schedule


def _schedule_outstanding_hosts(
    region: str,
    profile: str,
    *,
    skip: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Schedule a release for every other tracked host that has none (#4200).

    The repair half of the fix. The defect left hosts with no schedule at all,
    and a fix that only stops *new* ones being orphaned would leave the ones
    already orphaned billing forever -- including, on the owner's machine, any
    host stranded by the rounds that found this.
    """
    scheduled: list[dict[str, Any]] = []
    for host_id in tracked_hosts():
        if host_id == skip or release_stack_exists(host_id):
            continue
        entry = ledger_entry(host_id)
        allocated_at = parse_timestamp(str(entry.get("allocated_at") or ""))
        release_at = parse_timestamp(str(entry.get("release_at") or "")) or release_time(
            allocated_at or utc_now()
        )
        host_region = str(entry.get("region") or region)
        print(
            f"Dedicated Host {host_id} is still allocated and has no release scheduled. "
            f"Scheduling its release for {scheduler_timestamp(release_at)} UTC."
        )
        schedule = _schedule_one(host_id, release_at, host_region, profile, errors=errors)
        if schedule:
            scheduled.append(schedule)
    return scheduled


def _cleanup_finished_release_stacks(region: str, profile: str, *, keep: str) -> list[str]:
    """Destroy the release stacks of hosts AWS no longer has. Returns error strings.

    Per host (#4200), and only on a definite "the host is gone": a stack torn
    down while its host is still allocated removes the one thing that was going
    to release it. `keep` is the host this teardown is about to schedule, which
    is never a candidate.
    """
    errors: list[str] = []
    for host_id in release_stack_hosts():
        if host_id == keep:
            continue
        try:
            if host_still_allocated(host_id, region, profile) is False:
                destroy_release_stack(host_id=host_id, region=region, profile=profile)
        except Exception as exc:  # pragma: no cover - best effort by design
            errors.append(f"could not clean up the release stack for {host_id}: {exc}")
    return errors


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
