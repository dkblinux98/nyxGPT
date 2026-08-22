"""Unit tests for the EC2 Mac Dedicated Host lifecycle (#3995).

Nothing here talks to AWS or runs Terraform: boto3 clients and the Terraform
runner are replaced with recorders, so the tests assert on the parts that can
actually be wrong -- the pricing parse, the release arithmetic, the consent
gate, what is written to cloud state, and the teardown's refusal to let a
stuck host take the rest of the destroy with it.

**What is deliberately NOT asserted here.** No unit test can prove a
Dedicated Host allocates, that `ReleaseHosts` behaves as documented inside the
24-hour window, or that a Slack message arrives. `docs/live-verification-ci.md`
already lists EC2 Mac hardware among the things no CI job can run; under the
executed-verification gate (#3775 / D-006) that is the named exception, not a
missing test. The Terraform-shape tests at the bottom are the closest thing
available: they pin the four properties of the deferred-release design that a
well-meaning edit would silently break.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nyxgpt import cloud_infra, cloud_mac
from nyxgpt.cloud import CloudCommandError

REPO_ROOT = Path(__file__).resolve().parents[2]
MAC_TF = REPO_ROOT / "terraform" / "aws" / "mac"
MAC_RELEASE_TF = REPO_ROOT / "terraform" / "aws" / "mac-release"


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the module reads or writes at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_infra, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    monkeypatch.setattr(cloud_infra, "TERRAFORM_DIR", cloud_dir / "terraform")
    monkeypatch.setattr(cloud_infra, "TFSTATE_FILE", cloud_dir / "terraform.tfstate")
    monkeypatch.setattr(cloud_mac, "MAC_TFSTATE_FILE", cloud_dir / "mac.tfstate")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFSTATE_FILE", cloud_dir / "mac-release.tfstate")
    monkeypatch.setattr(cloud_mac, "MAC_TFVARS_FILE", cloud_dir / "mac.tfvars")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFVARS_FILE", cloud_dir / "mac-release.tfvars")
    return cloud_dir


def _args(**overrides) -> argparse.Namespace:
    base = {"region": None, "profile": None, "host": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _price_document(rate: str) -> str:
    """One AWS Price List document with a single on-demand dimension."""
    return json.dumps(
        {
            "product": {"productFamily": "Dedicated Host"},
            "terms": {
                "OnDemand": {
                    "TERM.SKU": {
                        "priceDimensions": {
                            "TERM.SKU.DIM": {"pricePerUnit": {"USD": rate}, "unit": "Hrs"}
                        }
                    }
                }
            },
        }
    )


class _StubPricing:
    def __init__(self, price_list):
        self._price_list = price_list
        self.filters = None

    def get_products(self, **kwargs):
        self.filters = kwargs.get("Filters")
        return {"PriceList": self._price_list}


# --- Host family + pricing ------------------------------------------------


@pytest.mark.parametrize(
    ("instance_type", "family"),
    [
        ("mac1.metal", "mac1"),
        ("mac2.metal", "mac2"),
        ("mac2-m2.metal", "mac2-m2"),
        ("mac2-m2pro.metal", "mac2-m2pro"),
        ("MAC2-M1ULTRA.METAL", "mac2-m1ultra"),
    ],
)
def test_the_host_family_is_the_instance_type_without_its_metal_suffix(instance_type, family):
    """The Pricing API prices the *host*, named by the family; EC2 places the
    *instance*, named by the type. Getting the two confused returns no prices
    at all, which the consent prompt then has to report as unknown."""
    assert cloud_mac.host_family(instance_type) == family


def test_the_hourly_rate_is_read_from_the_pricing_api_not_a_constant(monkeypatch):
    stub = _StubPricing([_price_document("0.6500000000")])
    monkeypatch.setattr(cloud_mac, "_client", lambda *a, **k: stub)

    pricing = cloud_mac.lookup_host_pricing("mac2.metal", "us-east-1")

    assert pricing.hourly_rate == pytest.approx(0.65)
    assert pricing.minimum_cost == pytest.approx(15.60)
    assert pricing.error == ""
    # Queried per family AND per region -- the spread across families is 2.4x,
    # so a query that dropped either filter would price the wrong hardware.
    fields = {f["Field"]: f["Value"] for f in stub.filters}
    assert fields["instanceType"] == "mac2"
    assert fields["regionCode"] == "us-east-1"
    assert fields["productFamily"] == "Dedicated Host"


def test_a_zero_rated_dimension_is_not_mistaken_for_a_free_host(monkeypatch):
    """The *instance* on a Dedicated Host genuinely costs $0.00/hour -- you pay
    for the host. A naive minimum over every dimension would therefore report
    the most expensive resource nyxGPT can create as free."""
    monkeypatch.setattr(
        cloud_mac,
        "_client",
        lambda *a, **k: _StubPricing([_price_document("0.0000000000"), _price_document("1.5600")]),
    )

    assert cloud_mac.lookup_host_pricing("mac2-m2pro.metal", "us-east-1").hourly_rate == (
        pytest.approx(1.56)
    )


def test_an_unanswerable_pricing_lookup_reports_unknown_rather_than_guessing(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr(cloud_mac, "_client", _boom)

    pricing = cloud_mac.lookup_host_pricing("mac2.metal", "us-east-1")

    assert pricing.hourly_rate is None
    assert pricing.minimum_cost is None
    assert "AccessDeniedException" in pricing.error


def test_an_empty_price_list_is_an_error_not_a_zero_rate(monkeypatch):
    monkeypatch.setattr(cloud_mac, "_client", lambda *a, **k: _StubPricing([]))

    pricing = cloud_mac.lookup_host_pricing("mac2.metal", "eu-west-3")

    assert pricing.hourly_rate is None
    assert "no on-demand rate" in pricing.error


# --- Availability zones ---------------------------------------------------


class _StubEC2:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, _name):
        return self

    def paginate(self, **_kwargs):
        return self._pages


def test_mac_capable_zones_are_queried_from_ec2_and_sorted(monkeypatch):
    """Mac capacity is per-AZ and differs by family. Assuming a zone fails at
    AllocateHosts, which is cheap only if the message is one an operator can
    act on."""
    monkeypatch.setattr(
        cloud_mac,
        "_client",
        lambda *a, **k: _StubEC2(
            [
                {"InstanceTypeOfferings": [{"Location": "us-east-1d"}]},
                {"InstanceTypeOfferings": [{"Location": "us-east-1c"}]},
            ]
        ),
    )

    assert cloud_mac.mac_capable_azs("mac2-m2.metal", "us-east-1") == [
        "us-east-1c",
        "us-east-1d",
    ]


# --- Release arithmetic ---------------------------------------------------


def test_the_release_time_is_the_24_hour_minimum_plus_a_scrub_buffer():
    allocated = datetime(2026, 8, 22, 18, 0, 0, tzinfo=UTC)

    assert cloud_mac.release_time(allocated) == allocated + timedelta(hours=24, minutes=30)


def test_the_scheduler_timestamp_carries_no_timezone_suffix():
    """EventBridge Scheduler's `at()` takes a naive timestamp and rejects a
    trailing Z or +00:00; `schedule_expression_timezone` is what says UTC."""
    stamp = cloud_mac.scheduler_timestamp(datetime(2026, 8, 23, 18, 30, 0, tzinfo=UTC))

    assert stamp == "2026-08-23T18:30:00"
    assert not stamp.endswith("Z")
    assert "+" not in stamp


def test_a_release_time_survives_a_round_trip_through_cloud_state():
    allocated = datetime(2026, 8, 22, 18, 0, 0, tzinfo=UTC)
    stored = cloud_mac.release_time(allocated).isoformat()

    assert cloud_mac.parse_timestamp(stored) == cloud_mac.release_time(allocated)


def test_accrued_cost_is_floored_at_the_24_hour_minimum():
    """AWS charges the full day even for a host released the instant its window
    closes, so counting up from zero would understate what is already owed."""
    allocated = datetime(2026, 8, 22, 18, 0, 0, tzinfo=UTC)
    one_hour_later = allocated + timedelta(hours=1)

    assert cloud_mac.accrued_cost(allocated, 0.65, one_hour_later) == pytest.approx(15.60)


def test_accrued_cost_keeps_counting_past_the_minimum():
    allocated = datetime(2026, 8, 22, 18, 0, 0, tzinfo=UTC)
    two_days_later = allocated + timedelta(hours=48)

    assert cloud_mac.accrued_cost(allocated, 0.65, two_days_later) == pytest.approx(31.20)


# --- Consent --------------------------------------------------------------


def _plan(**overrides):
    base = {
        "instance_type": "mac2.metal",
        "region": "us-east-1",
        "availability_zone": "us-east-1c",
        "pricing": cloud_mac.MacHostPricing(
            instance_type="mac2.metal",
            host_family="mac2",
            region="us-east-1",
            hourly_rate=0.65,
        ),
    }
    base.update(overrides)
    return cloud_mac.MacAllocationPlan(**base)


def test_the_disclosure_names_the_live_rate_the_minimum_and_when_it_can_be_released():
    releasable = datetime(2026, 8, 23, 18, 30, 0, tzinfo=UTC)

    text = cloud_mac.format_allocation_disclosure(_plan(), releasable)

    assert "mac2" in text
    assert "us-east-1 / us-east-1c" in text
    assert "$0.6500/hour" in text
    assert "$15.60" in text
    assert "2026-08-23T18:30:00 UTC" in text
    assert "Pricing API" in text


def test_the_disclosure_says_unknown_rather_than_printing_a_number_nothing_checked():
    plan = _plan(
        pricing=cloud_mac.MacHostPricing(
            instance_type="mac2.metal",
            host_family="mac2",
            region="us-east-1",
            error="the AWS Pricing API could not be queried: AccessDenied",
        )
    )

    text = cloud_mac.format_allocation_disclosure(plan, datetime.now(UTC))

    assert "UNKNOWN" in text
    assert "AccessDenied" in text
    # The constraint still holds even when the price does not.
    assert "24-hour minimum" in text


def test_allocation_needs_the_typed_word(capsys):
    cloud_mac.confirm_allocation("disclosure", reader=lambda _prompt: "allocate")

    assert "disclosure" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["y", "yes", "ALLOCATE HOST", "", "n"])
def test_anything_but_the_word_stops_before_anything_is_billed(answer):
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_mac.confirm_allocation("disclosure", reader=lambda _prompt: answer)

    assert "nothing is billed" in str(excinfo.value)


def test_the_word_is_case_insensitive_because_the_gate_is_intent_not_typing():
    cloud_mac.confirm_allocation("disclosure", reader=lambda _prompt: " Allocate\n")


def test_yes_skips_the_typing_but_never_the_disclosure(capsys):
    """`--yes` keeps the path scriptable. It must not make the cost invisible:
    a scripted run's log is where the numbers are read afterwards."""
    called = []

    cloud_mac.confirm_allocation(
        "RATE $0.6500/hour", assume_yes=True, reader=lambda _p: called.append(1)
    )

    assert called == []
    assert "RATE $0.6500/hour" in capsys.readouterr().out


def test_a_non_interactive_run_without_yes_stops_rather_than_hanging():
    def _no_tty(_prompt):
        raise EOFError

    with pytest.raises(CloudCommandError, match="--yes"):
        cloud_mac.confirm_allocation("disclosure", reader=_no_tty)


# --- Cloud-state record ---------------------------------------------------


def _record_host(**overrides):
    values = {
        "mac_host_id": "h-0abc",
        "mac_instance_id": "i-0mac",
        "mac_instance_type": "mac2.metal",
        "mac_region": "us-east-1",
        "mac_availability_zone": "us-east-1c",
        "mac_allocated_at": "2026-08-22T18:00:00+00:00",
        "mac_release_at": "2026-08-23T18:30:00+00:00",
        "mac_hourly_rate": 0.65,
        "mac_release_scheduled": False,
    }
    values.update(overrides)
    return cloud_mac.record_mac_host(values)


def test_the_host_id_allocation_time_and_release_time_round_trip_through_state():
    _record_host()

    record = cloud_mac.load_mac_record()

    assert record["mac_host_id"] == "h-0abc"
    assert record["mac_instance_id"] == "i-0mac"
    assert record["mac_allocated_at"] == "2026-08-22T18:00:00+00:00"
    assert record["mac_release_at"] == "2026-08-23T18:30:00+00:00"


def test_recording_the_host_preserves_the_substrates_own_keys(_isolated_cloud_home):
    cloud_infra.write_cloud_state({"instance_id": "i-linux", "region": "us-east-1"})

    _record_host()

    state = json.loads((_isolated_cloud_home / "state.json").read_text())
    assert state["instance_id"] == "i-linux"
    assert state["mac_host_id"] == "h-0abc"


def test_tearing_the_substrate_down_does_not_erase_the_still_billing_host(
    _isolated_cloud_home,
):
    """The cross-module invariant this whole design rests on: `cloud destroy`
    clears the substrate's keys from the same file, and if that took the Mac
    host's record with it, the one resource still costing money would become
    invisible to `nyxgpt cloud status` at the exact moment it is all that is
    left."""
    cloud_infra.write_cloud_state({"instance_id": "i-linux", "region": "us-east-1"})
    _record_host()

    cloud_infra.clear_cloud_state()

    assert cloud_mac.load_mac_record()["mac_host_id"] == "h-0abc"


def test_no_host_means_no_pending_release():
    assert cloud_mac.pending_release() == {}


def test_the_pending_release_reports_the_id_the_time_and_the_accrued_cost(monkeypatch):
    _record_host()
    monkeypatch.setattr(cloud_mac, "utc_now", lambda: datetime(2026, 8, 23, 6, 0, 0, tzinfo=UTC))

    pending = cloud_mac.pending_release()

    assert pending["host_id"] == "h-0abc"
    assert pending["release_at"] == "2026-08-23T18:30:00+00:00"
    assert pending["accrued_cost"] == pytest.approx(15.60)
    assert pending["releasable_now"] is False
    assert pending["billing"] is True


def test_a_host_past_its_window_is_reported_as_releasable(monkeypatch):
    _record_host()
    monkeypatch.setattr(cloud_mac, "utc_now", lambda: datetime(2026, 8, 24, 6, 0, 0, tzinfo=UTC))

    assert cloud_mac.pending_release()["releasable_now"] is True


def test_clearing_the_record_leaves_the_substrates_keys_alone(_isolated_cloud_home):
    cloud_infra.write_cloud_state({"instance_id": "i-linux"})
    _record_host()

    cloud_mac.clear_mac_record()

    state = json.loads((_isolated_cloud_home / "state.json").read_text())
    assert state == {"instance_id": "i-linux"}


# --- Instance type resolution ---------------------------------------------


def test_a_non_mac_type_is_refused_rather_than_asked_of_ec2():
    with pytest.raises(CloudCommandError, match="not an EC2 Mac instance type"):
        cloud_mac.resolve_mac_instance_type(_args(mac_instance_type="m5.large"))


def test_the_remembered_substrate_type_is_only_honoured_when_it_names_a_mac(monkeypatch):
    """`infra.json` remembers the substrate's own type for every Linux deploy.
    Reading it here would ask EC2 for a Dedicated Host of a family that cannot
    boot macOS. Pinned to the live default, not a literal, so a change like
    #3992 (m5.large -> m5.xlarge) cannot leave this asserting the old size."""
    monkeypatch.setattr(
        cloud_infra,
        "load_settings",
        lambda: {"instance_type": cloud_infra.DEFAULT_INSTANCE_TYPE},
    )

    resolved = cloud_mac.resolve_mac_instance_type(
        _args(mac_instance_type=None, instance_type=None)
    )

    assert resolved == cloud_mac.DEFAULT_MAC_INSTANCE_TYPE


def test_an_instance_type_flag_naming_a_mac_is_enough(monkeypatch):
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {})

    resolved = cloud_mac.resolve_mac_instance_type(
        _args(mac_instance_type=None, instance_type="mac2-m2.metal")
    )

    assert resolved == "mac2-m2.metal"


# --- Teardown -------------------------------------------------------------


@pytest.fixture
def _stub_teardown(monkeypatch):
    """Record what the teardown drives, without Terraform or AWS."""
    calls: list[str] = []
    monkeypatch.setattr(cloud_mac, "_slack_settings", lambda: ("C0ABH478QC8", "xoxb-test"))
    # The recorded host is still there (that is why we are tearing it down);
    # only a *previous* stack's host is treated as gone.
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: True)
    monkeypatch.setattr(
        cloud_mac,
        "apply_release_schedule",
        lambda **kwargs: calls.append("schedule")
        or {"host_id": kwargs["host_id"], "slack_channel": kwargs["slack_channel"]},
    )
    monkeypatch.setattr(
        cloud_mac,
        "destroy_mac_instance",
        lambda values: calls.append("destroy")
        or {"instance_terminated": True, "host_forgotten": True},
    )
    return calls


def test_teardown_schedules_the_release_before_it_destroys_anything(_stub_teardown):
    """Order is a money decision, not a style one: the schedule is the only
    step that stops the bill, and a destroy that failed first would leave a
    host allocated with nothing arranged to release it."""
    _record_host()

    result = cloud_mac.teardown(_args())

    assert _stub_teardown == ["schedule", "destroy"]
    assert result["release_scheduled"] is True
    assert result["instance_terminated"] is True
    assert result["host_id"] == "h-0abc"
    assert result["release_at"] == "2026-08-23T18:30:00+00:00"
    assert result["errors"] == []


def test_the_teardown_records_that_the_release_is_scheduled(_stub_teardown):
    _record_host()

    cloud_mac.teardown(_args())

    assert cloud_mac.pending_release()["release_scheduled"] is True


def test_a_schedule_that_cannot_be_created_does_not_stop_the_mac_coming_down(
    monkeypatch, _stub_teardown
):
    """The acceptance criterion in so many words: a host-release failure must
    not block or half-fail the rest of the teardown."""
    _record_host()

    def _boom(**_kwargs):
        raise CloudCommandError("no Slack bot token is configured")

    monkeypatch.setattr(cloud_mac, "apply_release_schedule", _boom)

    result = cloud_mac.teardown(_args())

    assert result["release_scheduled"] is False
    assert result["instance_terminated"] is True
    assert "no Slack bot token is configured" in result["errors"][0]


def test_a_mac_that_will_not_come_down_still_reports_the_scheduled_release(
    monkeypatch, _stub_teardown
):
    _record_host()

    def _boom(_values):
        raise CloudCommandError("terraform destroy failed")

    monkeypatch.setattr(cloud_mac, "destroy_mac_instance", _boom)

    result = cloud_mac.teardown(_args())

    assert result["release_scheduled"] is True
    assert result["instance_terminated"] is False
    assert "terraform destroy failed" in result["errors"][0]


def test_nothing_to_tear_down_is_reported_as_unmanaged_not_as_a_failure():
    assert cloud_mac.teardown(_args()) == {"managed": False}


# --- Reconciling a host AWS has already released --------------------------


def test_a_host_aws_says_is_gone_stops_being_reported_as_still_billing(monkeypatch):
    """The deferred release fires with nobody watching -- that is the design.
    Nothing on this machine learns the host is gone, so without this reconcile
    the "still billing" row would outlive the charge it describes."""
    _record_host()
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: False)
    monkeypatch.setattr(cloud_mac, "destroy_release_stack", lambda: True)

    assert cloud_mac.reconcile_released_host(_args()) is True
    assert cloud_mac.pending_release() == {}


def test_a_host_that_could_not_be_looked_up_is_kept_not_forgotten(monkeypatch):
    """`host_still_allocated` answers None when it could not ask. Deleting the
    record on the strength of expired credentials would hide a resource that
    is still costing money -- the one failure this row exists to prevent."""
    _record_host()
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: None)

    assert cloud_mac.reconcile_released_host(_args()) is False
    assert cloud_mac.pending_release()["host_id"] == "h-0abc"


def test_a_host_still_allocated_is_kept(monkeypatch):
    _record_host()
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: True)

    assert cloud_mac.reconcile_released_host(_args()) is False
    assert cloud_mac.pending_release()["host_id"] == "h-0abc"


def test_tearing_down_a_host_already_released_is_a_no_op(monkeypatch):
    _record_host()
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: False)
    monkeypatch.setattr(cloud_mac, "destroy_release_stack", lambda: True)

    assert cloud_mac.teardown(_args()) == {"managed": False, "already_released": True}


def test_reconciling_with_no_recorded_host_asks_aws_nothing(monkeypatch):
    def _never(*_args, **_kwargs):
        raise AssertionError("no host is recorded; there is nothing to ask about")

    monkeypatch.setattr(cloud_mac, "host_still_allocated", _never)

    assert cloud_mac.reconcile_released_host(_args()) is False


def test_scheduling_without_a_slack_token_names_the_host_that_is_still_billing(monkeypatch):
    """The message is the only thing standing between the operator and a
    silently unreleased host, so it has to carry the id."""
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_mac.apply_release_schedule(
            host_id="h-0abc",
            release_at=datetime.now(UTC),
            region="us-east-1",
            profile="",
            slack_channel="C0ABH478QC8",
            slack_bot_token="   ",
        )

    message = str(excinfo.value)
    assert "h-0abc" in message
    assert "slack_bot_token" in message


def test_forgetting_the_host_that_is_already_gone_is_not_an_error(monkeypatch):
    """`terraform state rm` on a resource that is not there fails, and the
    postcondition -- Terraform will not try to release the host -- already
    holds. A re-run of destroy must not trip over it."""

    def _boom(*_args, **_kwargs):
        raise CloudCommandError("no matching objects found")

    monkeypatch.setattr(cloud_infra, "run_terraform", _boom)

    assert cloud_mac.forget_host() is False


def test_a_host_that_cannot_be_looked_up_is_unknown_not_gone(monkeypatch):
    """Expired credentials reported as "released" is the one wrong answer that
    stops an operator looking for a resource that is still billing."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ExpiredToken")

    monkeypatch.setattr(cloud_mac, "_client", _boom)

    assert cloud_mac.host_still_allocated("h-0abc", "us-east-1") is None


# --- The Terraform shape the design depends on ----------------------------
#
# These read the HCL rather than plan it. `terraform validate` (CI) proves the
# configuration parses; what it cannot prove is that the four properties the
# deferred release actually rests on are still present, and each of them is one
# careless edit away from a host that never gets released or a failure nobody
# hears about.


def test_the_schedule_is_one_shot_and_deletes_itself():
    hcl = (MAC_RELEASE_TF / "main.tf").read_text(encoding="utf-8")

    assert 'action_after_completion = "DELETE"' in hcl
    assert 'mode = "OFF"' in hcl
    assert 'schedule_expression          = "at(${var.release_at})"' in hcl


def test_the_slack_branch_treats_http_200_with_ok_false_as_a_failure():
    """Slack answers `invalid_auth` and `channel_not_found` with HTTP 200 and
    `"ok": false`, so a state machine that branched on status codes would
    report success for a message that never arrived."""
    hcl = (MAC_RELEASE_TF / "main.tf").read_text(encoding="utf-8")

    assert hcl.count('"$.ResponseBody.ok"') == 2
    assert "SlackUndelivered" in hcl


def test_the_scrub_window_is_absorbed_by_a_wait_loop_not_by_a_retry_block():
    """ReleaseHosts returns 200 with the host in `Unsuccessful` while the host
    is being scrubbed -- not an exception, so a `Retry` would never fire."""
    hcl = (MAC_RELEASE_TF / "main.tf").read_text(encoding="utf-8")

    assert "WaitForScrub" in hcl
    assert "States.MathAdd($.attempts, 1)" in hcl
    assert '"$.release.Successful[0]"' in hcl


def test_the_state_machine_role_can_release_hosts_and_call_slack_and_nothing_else():
    hcl = (MAC_RELEASE_TF / "main.tf").read_text(encoding="utf-8")

    for action in (
        "ec2:ReleaseHosts",
        "states:InvokeHTTPEndpoint",
        "events:RetrieveConnectionCredentials",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    ):
        assert action in hcl
    # The HTTP permission is fenced to Slack rather than left open.
    assert '"https://slack.com/*"' in hcl


def test_the_dedicated_host_is_isolated_in_its_own_root_module():
    """Its own state file is what lets `destroy` forget the host and tear
    everything else down; a child module of the substrate could not."""
    assert (MAC_TF / "versions.tf").read_text(encoding="utf-8").count('backend "local"') == 1
    hcl = (MAC_TF / "main.tf").read_text(encoding="utf-8")
    assert 'resource "aws_ec2_host" "this"' in hcl
    assert 'tenancy = "host"' in hcl
    # The Mac never joins the substrate's VPC: the two are torn down on
    # different schedules and must share nothing whose deletion could block.
    assert 'resource "aws_vpc" "this"' in hcl


def test_the_mac_security_group_opens_ssh_only_and_never_to_the_world():
    hcl = (MAC_TF / "main.tf").read_text(encoding="utf-8")
    variables = (MAC_TF / "variables.tf").read_text(encoding="utf-8")

    assert "from_port   = 22" in hcl
    assert "cidr_blocks = [var.owner_ip_cidr]" in hcl
    assert 'var.owner_ip_cidr != "0.0.0.0/0"' in variables
