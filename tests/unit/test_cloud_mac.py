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
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
    # #4136: the headline figure is AWS's, and nothing has asked AWS here, so
    # there is no figure. The local `rate * elapsed` number is still computed
    # but is reported under its own name -- it is an estimate, and a surface
    # that printed it as "accrued" told the owner $48.44 for a $12.02 bill.
    assert pending["accrued_cost"] is None
    assert pending["accrued_source"] == ""
    assert pending["estimated_cost"] == pytest.approx(15.60)
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


# --- Reconcile must re-apply the Mac you have, not a newer one -------------


def test_an_existing_mac_is_never_replaced_for_a_newer_ami():
    """`ami` forces replacement, and the AMI data source is `most_recent`.
    Replacing an EC2 Mac terminates it, takes its disk with it and starts an
    hour-long host scrub the replacement cannot then launch onto -- on the
    path the docs call safe to re-run, under `apply -auto-approve`."""
    hcl = (MAC_TF / "main.tf").read_text(encoding="utf-8")

    assert "ignore_changes = [ami]" in hcl
    # The output has to be the instance's own attribute: with the drift
    # ignored, `local.ami_id` is the *newer* image, not the booted one, and
    # cloud_mac feeds this value back on the next reconcile.
    outputs = (MAC_TF / "outputs.tf").read_text(encoding="utf-8")
    assert re.search(r"^\s*value\s*=\s*aws_instance\.mac\.ami\s*$", outputs, re.MULTILINE)
    assert not re.search(r"^\s*value\s*=\s*local\.ami_id\s*$", outputs, re.MULTILINE)


def test_reconcile_re_applies_the_recorded_ami_and_volume_not_freshly_resolved_ones(monkeypatch):
    """The record is the pin. Leaving `mac_ami_id` empty would re-resolve
    `most_recent`, and the root volume size was hardcoded to 200 -- which
    silently shrinks the disk of anyone who deployed with a larger
    `--root-volume-size`, forcing replacement the same way an AMI change does.
    """
    _record_host(mac_ami_id="ami-booted", mac_root_volume_size=400)
    applied: dict[str, object] = {}

    def _capture(plan):
        applied["ami_id"] = plan.ami_id
        applied["root_volume_size"] = plan.root_volume_size
        return {"public_ip": "203.0.113.9", "instance_id": "i-0mac", "ami_id": "ami-booted"}

    monkeypatch.setattr(cloud_mac, "apply_mac_host", _capture)
    monkeypatch.setattr(
        cloud_mac.cloud_infra,
        "resolve_settings",
        lambda _args: SimpleNamespace(
            aws_profile="",
            owner_ip_cidr="198.51.100.5/32",
            ssh_key_name="",
            ssh_public_key="",
            name_prefix="nyxgpt-tf",
        ),
    )
    monkeypatch.setattr(cloud_mac, "resolve_allocation_plan", _never_priced)

    result = cloud_mac._reconcile_existing(_args(), cloud_mac.load_mac_record())

    assert applied["ami_id"] == "ami-booted"
    assert applied["root_volume_size"] == 400
    assert result["reconciled"] is True


def test_a_record_written_before_the_ami_was_pinned_falls_back_to_the_defaults(monkeypatch):
    """A host allocated before those keys existed has neither. It must still
    reconcile -- with the module's `ignore_changes` as the thing that keeps it
    from being replaced -- rather than crash or plan a 0 GiB root volume."""
    _record_host()
    applied: dict[str, object] = {}

    def _capture(plan):
        applied["ami_id"] = plan.ami_id
        applied["root_volume_size"] = plan.root_volume_size
        return {"public_ip": "203.0.113.9", "instance_id": "i-0mac", "ami_id": "ami-booted"}

    monkeypatch.setattr(cloud_mac, "apply_mac_host", _capture)
    monkeypatch.setattr(
        cloud_mac.cloud_infra,
        "resolve_settings",
        lambda _args: SimpleNamespace(
            aws_profile="",
            owner_ip_cidr="198.51.100.5/32",
            ssh_key_name="",
            ssh_public_key="",
            name_prefix="nyxgpt-tf",
        ),
    )

    cloud_mac._reconcile_existing(_args(), cloud_mac.load_mac_record())

    assert applied["ami_id"] == ""
    assert applied["root_volume_size"] == cloud_mac.MacAllocationPlan.root_volume_size
    # And the record is healed from what the instance reports, so the *next*
    # reconcile is pinned even though this one could not be.
    assert cloud_mac.load_mac_record()["mac_ami_id"] == "ami-booted"


@pytest.mark.parametrize("raw", [None, "", "not-a-number", 0, -1])
def test_an_unusable_recorded_volume_size_falls_back_rather_than_shrinking_the_disk(raw):
    """A bad value here does not fail loudly -- it plans a differently-sized
    root volume, and a smaller one forces replacement."""
    assert (
        cloud_mac._recorded_root_volume_size({"mac_root_volume_size": raw})
        == cloud_mac.MacAllocationPlan.root_volume_size
    )


def _never_priced(*_args, **_kwargs):
    raise AssertionError("a reconcile must not re-price or re-resolve the allocation")


# --- #4122: a host Terraform holds is never re-disclosed -----------------


def _mac_state(host_id: str = "h-06c438d25077be888") -> None:
    """Write a Mac-root Terraform state file holding an allocated host.

    The shape Terraform's local backend actually writes, because that is what
    `allocated_host_from_state` parses -- a hand-rolled shape would let the
    parser pass a test and fail on a real state file.
    """
    cloud_mac.MAC_TFSTATE_FILE.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.9.5",
                "resources": [
                    {
                        "mode": "managed",
                        "type": "aws_ec2_host",
                        "name": "this",
                        "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                        "instances": [
                            {
                                "schema_version": 0,
                                "attributes": {
                                    "id": host_id,
                                    "availability_zone": "us-east-1a",
                                    "instance_type": "mac2.metal",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_an_allocated_host_is_read_out_of_terraform_state():
    _mac_state()
    assert cloud_mac.allocated_host_from_state() == "h-06c438d25077be888"


@pytest.mark.parametrize(
    "contents",
    ["", "not json", "[]", json.dumps({"resources": []}), json.dumps({"resources": [{}]})],
)
def test_unreadable_or_hostless_state_answers_empty(contents):
    """Fails closed to "no host recorded", which is the pre-#4122 behaviour --
    never to a crash on a state file this parser did not expect."""
    cloud_mac.MAC_TFSTATE_FILE.write_text(contents, encoding="utf-8")
    assert cloud_mac.allocated_host_from_state() == ""


def test_no_state_file_at_all_answers_empty():
    assert cloud_mac.allocated_host_from_state() == ""


def test_a_host_terraform_already_holds_is_adopted_not_re_disclosed(monkeypatch):
    """#4122. `allocate` recorded the host id only after the WHOLE apply
    succeeded, so an apply that allocated the host and then failed -- the owner's
    2026-09-30 run failed on an apostrophe in a security-group rule description,
    after the host existed -- left a billed host with no record. The next run then
    printed a fresh non-refundable-charge disclosure for a charge already made,
    and only Terraform's `0 added, 0 changed` stopped it being a second real one.
    """
    _mac_state()
    disclosed: list[str] = []
    monkeypatch.setattr(
        cloud_mac,
        "confirm_allocation",
        lambda text, assume_yes=False: disclosed.append(text),
    )
    monkeypatch.setattr(cloud_mac, "resolve_allocation_plan", _never_priced)
    monkeypatch.setattr(cloud_mac, "reconcile_released_host", lambda args: False)
    # #4136: adoption requires AWS to confirm the host in this run. Terraform's
    # state naming it is not evidence it exists -- that is how the 2026-10-03
    # deploy reconciled a host released three days earlier.
    monkeypatch.setattr(cloud_mac, "host_still_allocated", lambda *a, **k: True)
    monkeypatch.setattr(
        cloud_mac,
        "apply_mac_host",
        lambda plan: {
            "host_id": "h-06c438d25077be888",
            "public_ip": "98.93.96.217",
            "instance_id": "i-05289782c39bdc827",
        },
    )
    monkeypatch.setattr(
        cloud_mac,
        "lookup_host_pricing",
        lambda *a, **k: cloud_mac.MacHostPricing(
            instance_type="mac2.metal", host_family="mac2", region="us-east-1", hourly_rate=0.65
        ),
    )
    monkeypatch.setattr(
        cloud_mac.cloud_infra,
        "resolve_settings",
        lambda _args: SimpleNamespace(
            aws_region="us-east-1",
            aws_profile="",
            owner_ip_cidr="198.51.100.5/32",
            ssh_key_name="",
            ssh_public_key="",
            name_prefix="nyxgpt-tf",
        ),
    )

    result = cloud_mac.allocate(_args(mac_instance_type=None, instance_type=None), assume_yes=True)

    assert disclosed == [], "a host already allocated from this machine was re-disclosed"
    assert result["allocated"] is False
    assert result["reconciled"] is True
    assert result["host_id"] == "h-06c438d25077be888"
    # And the record is healed, so `cloud status` can name the host from now on.
    assert cloud_mac.load_mac_record()["mac_host_id"] == "h-06c438d25077be888"


def test_a_host_allocated_by_an_apply_that_then_failed_is_recorded_before_raising(monkeypatch):
    """A host whose id is lost is a charge nothing can stop. So the id Terraform
    state knows is written down on the way out of the failure, not after it."""
    monkeypatch.setattr(cloud_mac, "confirm_allocation", lambda text, assume_yes=False: None)
    monkeypatch.setattr(cloud_mac, "reconcile_released_host", lambda args: False)
    monkeypatch.setattr(
        cloud_mac,
        "resolve_allocation_plan",
        lambda args: cloud_mac.MacAllocationPlan(
            instance_type="mac2.metal",
            region="us-east-1",
            availability_zone="us-east-1a",
            pricing=cloud_mac.MacHostPricing(
                instance_type="mac2.metal",
                host_family="mac2",
                region="us-east-1",
                hourly_rate=0.65,
            ),
        ),
    )

    def _apply_that_allocates_then_fails(plan):
        # Exactly the owner's failure: the host is created, then a later
        # resource is rejected by EC2.
        _mac_state()
        raise CloudCommandError("InvalidParameterValue: description contains an invalid character")

    monkeypatch.setattr(cloud_mac, "apply_mac_host", _apply_that_allocates_then_fails)

    with pytest.raises(CloudCommandError, match="invalid character"):
        cloud_mac.allocate(_args(), assume_yes=True)

    record = cloud_mac.load_mac_record()
    assert record["mac_host_id"] == "h-06c438d25077be888"
    assert record["mac_instance_type"] == "mac2.metal"
    assert record["mac_hourly_rate"] == 0.65
    # The whole point: `cloud status` can now see it and `destroy` can schedule
    # its release.
    assert cloud_mac.pending_release()["host_id"] == "h-06c438d25077be888"


def test_an_apply_that_failed_before_allocating_records_nothing(monkeypatch):
    """Symmetric, and the half that must not over-report: a failure with no host
    in state must not invent a record that would make `cloud status` claim a
    charge nothing made."""
    monkeypatch.setattr(cloud_mac, "confirm_allocation", lambda text, assume_yes=False: None)
    monkeypatch.setattr(cloud_mac, "reconcile_released_host", lambda args: False)
    monkeypatch.setattr(
        cloud_mac,
        "resolve_allocation_plan",
        lambda args: cloud_mac.MacAllocationPlan(
            instance_type="mac2.metal", region="us-east-1", availability_zone="us-east-1a"
        ),
    )

    def _apply_that_never_allocates(plan):
        raise CloudCommandError("InsufficientHostCapacity")

    monkeypatch.setattr(cloud_mac, "apply_mac_host", _apply_that_never_allocates)

    with pytest.raises(CloudCommandError, match="InsufficientHostCapacity"):
        cloud_mac.allocate(_args(), assume_yes=True)

    assert cloud_mac.load_mac_record() == {}
    assert cloud_mac.pending_release() == {}


def test_pending_release_reports_the_security_group_and_address_it_records(monkeypatch):
    """Recorded since #3995 and reported by nothing, so `cloud status` read the
    LINUX substrate's fields for a Mac: "Security group: not recorded" over a
    security group state.json was holding, and `m5.xlarge` for a `mac2.metal`."""
    _record_host(
        mac_security_group_id="sg-0e3cde668e9c66292",
        mac_public_ip="98.93.96.217",
    )

    pending = cloud_mac.pending_release()

    assert pending["security_group_id"] == "sg-0e3cde668e9c66292"
    assert pending["public_ip"] == "98.93.96.217"
    assert pending["instance_type"] == "mac2.metal"


# --- #4136: the record is never the sole basis for a billable decision ----
#
# 2026-10-03. `state.json` held a RELEASED host's id, its release time and its
# "release scheduled" flag beside the NEW instance's id and IP -- a new instance
# stapled to a dead host. `nyxgpt cloud deploy --os macos` believed the host
# still existed, skipped the priced disclosure and the `allocate` consent
# prompt, announced "no new 24-hour minimum", and allocated a new host.
#
# It was not that nothing asked AWS. `reconcile_released_host` made exactly the
# right call and AWS gave exactly the right answer -- `InvalidHostID.NotFound`
# -- and `host_still_allocated` caught it as "could not ask".

#: The record the owner's machine actually held after that deploy, verbatim.
#: Two fields describe the new substrate; four describe a host AWS released
#: three days earlier; the last is detectably impossible with no API call --
#: 78 seconds BEFORE the host it describes was allocated (19:27:05Z).
STALE_2026_10_03 = {
    "mac_instance_id": "i-00e566c3560462cd4",
    "mac_public_ip": "34.201.63.175",
    "mac_host_id": "h-06c438d25077be888",
    "mac_allocated_at": "2026-09-30T15:49:43+00:00",
    "mac_release_at": "2026-10-01T16:19:43+00:00",
    "mac_release_scheduled": True,
    "mac_release_scheduled_at": "2026-10-03T19:25:47.725000+00:00",
}


class _StubEc2:
    """An EC2 client that answers DescribeHosts however the test needs.

    `describe_hosts` either returns a payload or raises a botocore-shaped
    `ClientError` -- a plain exception carrying a `response` dict, which is what
    `_aws_error_code` reads, because importing `botocore.exceptions` would break
    this module on an install without the cloud extra.
    """

    def __init__(self, *, hosts=None, error_code="", zones=("us-east-1a", "us-east-1c")):
        self.hosts = hosts
        self.error_code = error_code
        self.zones = zones
        self.describe_hosts_calls: list[list[str]] = []

    def describe_hosts(self, HostIds):  # noqa: N803 - boto3's parameter name
        self.describe_hosts_calls.append(list(HostIds))
        if self.error_code:
            raise _client_error(self.error_code)
        return {"Hosts": self.hosts or []}

    def get_paginator(self, _name):
        zones = self.zones

        class _Paginator:
            def paginate(self, **_kwargs):
                return [
                    {"InstanceTypeOfferings": [{"Location": zone} for zone in zones]},
                ]

        return _Paginator()


def _client_error(code: str) -> Exception:
    exc = Exception(f"An error occurred ({code})")
    exc.response = {"Error": {"Code": code, "Message": "no such host"}}
    return exc


def _stub_clients(monkeypatch, ec2, *, price="0.6500000000", cost=None):
    """Route `cloud_mac._client` to a per-service stub."""
    pricing = _StubPricing([_price_document(price)] if price else [])
    explorer = _StubCostExplorer(cost if cost is not None else {})

    def _dispatch(service, *_a, **_k):
        if service == "ec2":
            return ec2
        if service == "pricing":
            return pricing
        if service == "ce":
            return explorer
        raise AssertionError(f"unexpected AWS client {service!r}")

    monkeypatch.setattr(cloud_mac, "_client", _dispatch)
    return pricing, explorer


class _StubCostExplorer:
    def __init__(self, response):
        self.response = response
        self.requests: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _settings(monkeypatch):
    monkeypatch.setattr(
        cloud_mac.cloud_infra,
        "resolve_settings",
        lambda _args: SimpleNamespace(
            aws_region="us-east-1",
            aws_profile="",
            owner_ip_cidr="198.51.100.5/32",
            ssh_key_name="",
            ssh_public_key="",
            name_prefix="nyxgpt-tf",
        ),
    )


def test_a_host_aws_has_no_record_of_is_gone_not_unknown(monkeypatch):
    """The single line that cost the money. `InvalidHostID.NotFound` is EC2
    saying the host does not exist -- the clearest possible no -- and it arrives
    as a ClientError, so catching every exception as "could not ask" turned it
    into the one answer that leaves a stale record in place."""
    ec2 = _StubEc2(error_code="InvalidHostID.NotFound")
    _stub_clients(monkeypatch, ec2)

    assert cloud_mac.host_still_allocated("h-06c438d25077be888", "us-east-1") is False


@pytest.mark.parametrize("code", ["InvalidHostId.NotFound", "INVALIDHOSTID.NOTFOUND"])
def test_the_not_found_code_is_matched_however_aws_spells_it(monkeypatch, code):
    _stub_clients(monkeypatch, _StubEc2(error_code=code))

    assert cloud_mac.host_still_allocated("h-0abc", "us-east-1") is False


def test_expired_credentials_are_still_unknown_rather_than_gone(monkeypatch):
    """The other half, unchanged and load-bearing: a record deleted on the
    strength of an auth failure would hide a host that is still billing."""
    _stub_clients(monkeypatch, _StubEc2(error_code="AuthFailure"))

    assert cloud_mac.host_still_allocated("h-0abc", "us-east-1") is None


def test_a_deploy_whose_recorded_host_aws_released_takes_the_full_consent_path(monkeypatch, capsys):
    """The 2026-10-03 run, end to end, with AWS's real answer.

    Asserted through the real `confirm_allocation` and a real refusal at the
    prompt, not a stub: the claim is that a billable allocation cannot happen
    without the disclosure AND the typed word, so the test has to be able to
    fail by the prompt not appearing.
    """
    cloud_mac.record_mac_host(STALE_2026_10_03)
    _mac_state()
    _settings(monkeypatch)
    ec2 = _StubEc2(error_code="InvalidHostID.NotFound")
    _stub_clients(monkeypatch, ec2)
    monkeypatch.setattr(
        cloud_mac, "apply_mac_host", lambda plan: pytest.fail("nothing may be applied")
    )
    # Declines at the prompt. The prompt existing at all is the thing under
    # test -- on 2026-10-03 it never appeared.
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(CloudCommandError, match="nothing was allocated and nothing is billed"):
        cloud_mac.allocate(_args(mac_instance_type=None, instance_type=None), assume_yes=False)

    out = capsys.readouterr().out
    # AWS was asked about the recorded host, and its answer was acted on.
    assert ec2.describe_hosts_calls == [["h-06c438d25077be888"]]
    assert "h-06c438d25077be888 has been released" in out
    # The priced disclosure, with the live rate and the minimum charge.
    assert "$0.6500/hour" in out
    assert "$15.60 for the 24-hour minimum" in out
    # And NOT the sentence that was the whole defect.
    assert "no new host, no new 24-hour minimum" not in out
    # The stale block is gone from the file that means "current", not relabelled.
    assert cloud_mac.load_mac_record() == {}
    assert cloud_mac.pending_release() == {}


def test_a_reconcile_is_refused_when_aws_cannot_be_asked_in_this_run(monkeypatch):
    """ "No command claims 'no new host, no new 24-hour minimum' without having
    confirmed the host exists at AWS in that run." With no confirmation there is
    no claim and no apply -- an apply that cannot verify its own premise is how
    an unannounced 24-hour minimum started."""
    _record_host()
    _mac_state("h-0abc")
    _settings(monkeypatch)
    _stub_clients(monkeypatch, _StubEc2(error_code="AuthFailure"))
    monkeypatch.setattr(
        cloud_mac, "apply_mac_host", lambda plan: pytest.fail("nothing may be applied")
    )

    with pytest.raises(CloudCommandError, match="could not be asked"):
        cloud_mac.allocate(_args(), assume_yes=True)

    # The record is left alone: "we could not ask" is not "it is gone".
    assert cloud_mac.load_mac_record()["mac_host_id"] == "h-0abc"


def test_terraform_state_naming_a_released_host_does_not_suppress_the_disclosure(
    monkeypatch, capsys
):
    """#4122 adopts a host Terraform's state holds without re-disclosing it.
    That is still right -- but only for a host that EXISTS. State outlives the
    resource it names just as the record does."""
    _mac_state("h-06c438d25077be888")
    _settings(monkeypatch)
    _stub_clients(monkeypatch, _StubEc2(error_code="InvalidHostID.NotFound"))
    monkeypatch.setattr(
        cloud_mac, "apply_mac_host", lambda plan: pytest.fail("nothing may be applied")
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(CloudCommandError, match="nothing was allocated"):
        cloud_mac.allocate(_args(mac_instance_type=None, instance_type=None), assume_yes=False)

    out = capsys.readouterr()
    assert "AWS has released it" in out.err
    assert "Minimum charge" in out.out
    assert not cloud_mac.MAC_TFSTATE_FILE.exists()


def test_a_confirmed_host_is_reconciled_and_its_whole_block_is_rewritten(monkeypatch):
    """The second half of the defect: the reconcile wrote five of the block's
    fields and left the rest naming the previous host. Every key is asserted
    here, not only the ones the apply produced."""
    cloud_mac.record_mac_host(
        {
            **STALE_2026_10_03,
            "mac_host_id": "h-0abc",
            "mac_region": "us-east-1",
            "mac_availability_zone": "us-east-1c",
            "mac_instance_type": "mac2.metal",
            "mac_allocated_at": "2026-10-03T19:27:05+00:00",
            "mac_release_at": "2026-10-04T19:57:05+00:00",
            "mac_release_scheduled": False,
            "mac_release_scheduled_at": None,
            "mac_hourly_rate": 0.65,
        }
    )
    _mac_state("h-0abc")
    _settings(monkeypatch)
    _stub_clients(
        monkeypatch,
        _StubEc2(hosts=[{"HostId": "h-0abc", "State": "available"}]),
    )
    monkeypatch.setattr(
        cloud_mac,
        "apply_mac_host",
        lambda plan: {
            "host_id": "h-0abc",
            "instance_id": "i-0new",
            "public_ip": "34.201.63.175",
            "security_group_id": "sg-0new",
            "ami_id": "ami-booted",
            "instance_type": "mac2.metal",
            "region": "us-east-1",
            "availability_zone": "us-east-1c",
        },
    )

    result = cloud_mac.allocate(_args(), assume_yes=True)

    assert result["reconciled"] is True
    record = cloud_mac.load_mac_record()
    # Nothing from the previous run survives unexamined: every key is either a
    # value this reconcile decided or absent.
    assert record == {
        "mac_host_id": "h-0abc",
        "mac_instance_id": "i-0new",
        "mac_instance_type": "mac2.metal",
        "mac_region": "us-east-1",
        "mac_availability_zone": "us-east-1c",
        "mac_public_ip": "34.201.63.175",
        "mac_security_group_id": "sg-0new",
        "mac_ami_id": "ami-booted",
        "mac_root_volume_size": 200,
        "mac_allocated_at": "2026-10-03T19:27:05+00:00",
        "mac_release_at": "2026-10-04T19:57:05+00:00",
        "mac_hourly_rate": 0.65,
        "mac_release_scheduled": False,
        "mac_verified_at": record["mac_verified_at"],
        "mac_host_present": True,
    }
    # The impossible field is gone, and nothing re-derived it.
    assert "mac_release_scheduled_at" not in record
    assert cloud_mac.record_findings(record) == []


def test_a_reconcile_that_somehow_allocated_a_new_host_records_it_loudly(monkeypatch, capsys):
    """Unreachable through `allocate`, which proves the host exists at AWS and
    in Terraform's state before it gets here. Reported rather than trusted not
    to happen: if it ever does, a non-refundable minimum has started with no
    disclosure, and the record saying so is how the operator finds out."""
    _record_host()
    monkeypatch.setattr(
        cloud_mac,
        "apply_mac_host",
        lambda plan: {"host_id": "h-0c8f9957132fb0794", "instance_id": "i-0new"},
    )
    _settings(monkeypatch)

    cloud_mac._reconcile_existing(_args(), cloud_mac.load_mac_record())

    err = capsys.readouterr().err
    assert "A NEW host has been allocated" in err
    record = cloud_mac.load_mac_record()
    assert record["mac_host_id"] == "h-0c8f9957132fb0794"
    # The previous host's timing fields describe the previous host, so they are
    # recomputed rather than carried across -- and the record stays coherent.
    assert record["mac_allocated_at"] != "2026-08-22T18:00:00+00:00"
    assert cloud_mac.record_findings(record) == []
    # And the superseded block is retrievable, under its own name.
    assert cloud_mac.cloud_record.load_archive()[0]["block"]["mac_host_id"] == "h-0abc"


# --- Free internal-consistency checks ------------------------------------


def test_a_release_scheduled_before_its_host_was_allocated_is_reported():
    """Detectable with no API call at all, and true of the 2026-10-03 record:
    `release_scheduled_at` was 78 seconds BEFORE the host it described was
    allocated."""
    record = {
        "mac_host_id": "h-0c8f9957132fb0794",
        "mac_allocated_at": "2026-10-03T19:27:05+00:00",
        "mac_release_at": "2026-10-04T19:57:05+00:00",
        "mac_release_scheduled_at": "2026-10-03T19:25:47.725000+00:00",
    }

    findings = cloud_mac.record_findings(record)

    assert any("EARLIER than" in finding for finding in findings)


def test_a_release_time_that_is_not_the_allocation_plus_the_minimum_is_reported():
    record = {
        "mac_host_id": "h-0abc",
        "mac_allocated_at": "2026-10-03T19:27:05+00:00",
        # The previous host's window, three days stale.
        "mac_release_at": "2026-10-01T16:19:43+00:00",
    }

    findings = cloud_mac.record_findings(record)

    assert any("is not mac_allocated_at +" in finding for finding in findings)


def test_a_scheduled_release_with_no_schedule_is_reported():
    """`aws scheduler list-schedules` was empty while the record claimed the
    release was scheduled. The local half of that is free to check."""
    findings = cloud_mac.record_findings({"mac_host_id": "h-0abc", "mac_release_scheduled": True})

    assert any("no release schedule exists" in finding for finding in findings)


def test_a_schedule_for_a_different_host_is_reported():
    cloud_mac.MAC_RELEASE_TFVARS_FILE.write_text(
        'host_id = "h-06c438d25077be888"\n', encoding="utf-8"
    )

    findings = cloud_mac.record_findings(
        {"mac_host_id": "h-0c8f9957132fb0794", "mac_release_scheduled": True}
    )

    assert any("rather than h-0c8f9957132fb0794" in finding for finding in findings)


def test_a_coherent_record_reports_nothing():
    allocated = datetime(2026, 10, 3, 19, 27, 5, tzinfo=UTC)
    cloud_mac.MAC_RELEASE_TFVARS_FILE.write_text('host_id = "h-0abc"\n', encoding="utf-8")

    assert (
        cloud_mac.record_findings(
            {
                "mac_host_id": "h-0abc",
                "mac_allocated_at": allocated.isoformat(),
                "mac_release_at": cloud_mac.release_time(allocated).isoformat(),
                "mac_release_scheduled": True,
                "mac_release_scheduled_at": "2026-10-03T19:30:00+00:00",
            }
        )
        == []
    )


def test_the_incoherence_is_reported_on_the_status_surface():
    """Reported rather than used -- a surface that printed those rows as if they
    agreed is what told the operator a release window had "passed" for a host
    allocated hours later."""
    _record_host(
        mac_allocated_at="2026-10-03T19:27:05+00:00",
        mac_release_at="2026-10-01T16:19:43+00:00",
    )

    assert cloud_mac.pending_release()["incoherent"]


# --- The spend figure comes from AWS -------------------------------------


def _ce_response(days):
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": day, "End": day},
                "Groups": [
                    {
                        "Keys": ["HostUsage:mac2"],
                        "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}},
                    }
                ],
            }
            for day, amount in days
        ]
    }


def test_the_spend_is_what_cost_explorer_billed_not_rate_times_elapsed(monkeypatch):
    """Observed: the display read $48.44 for a host AWS billed $12.02 for and
    had stopped charging for two days earlier. `rate * (now - allocated_at)`
    cannot stop counting, because neither of its inputs knows the host is gone.
    """
    _, explorer = _stub_clients(
        monkeypatch,
        _StubEc2(),
        cost=_ce_response(
            [
                ("2026-09-30", "3.90"),
                ("2026-10-01", "8.12"),
                ("2026-10-02", "0"),
                ("2026-10-03", "0"),
            ]
        ),
    )

    spend = cloud_mac.lookup_host_spend(
        "mac2.metal",
        "us-east-1",
        since=datetime(2026, 9, 30, tzinfo=UTC),
        now=datetime(2026, 10, 3, 19, 0, tzinfo=UTC),
    )

    assert spend.amount == pytest.approx(12.02)
    assert spend.currency == "USD"
    assert spend.through == "2026-10-03"
    assert spend.error == ""
    # Scoped to the family and the region, and asked for daily so the figure can
    # be seen to have stopped moving.
    request = explorer.requests[0]
    assert request["Granularity"] == "DAILY"
    assert request["TimePeriod"] == {"Start": "2026-09-30", "End": "2026-10-04"}


def test_a_later_query_returns_the_same_figure_once_the_charges_stop(monkeypatch):
    """ "Stops changing once the host is released" -- the property the local
    estimate could not have."""
    days = [("2026-09-30", "3.90"), ("2026-10-01", "8.12"), ("2026-10-02", "0")]
    _stub_clients(monkeypatch, _StubEc2(), cost=_ce_response(days))
    first = cloud_mac.lookup_host_spend(
        "mac2.metal",
        "us-east-1",
        since=datetime(2026, 9, 30, tzinfo=UTC),
        now=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
    )

    _stub_clients(monkeypatch, _StubEc2(), cost=_ce_response(days + [("2026-10-03", "0")]))
    later = cloud_mac.lookup_host_spend(
        "mac2.metal",
        "us-east-1",
        since=datetime(2026, 9, 30, tzinfo=UTC),
        now=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    )

    assert first.amount == later.amount == pytest.approx(12.02)


def test_another_host_familys_charges_are_not_counted_as_this_ones(monkeypatch):
    """`mac2` and `mac2-m2` are different hardware at different prices, so a
    prefix match would add one bill to the other."""
    _stub_clients(
        monkeypatch,
        _StubEc2(),
        cost={
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                    "Groups": [
                        {
                            "Keys": ["USE1-HostUsage:mac2"],
                            "Metrics": {"UnblendedCost": {"Amount": "8.12", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["USE1-HostUsage:mac2-m2pro"],
                            "Metrics": {"UnblendedCost": {"Amount": "40.00", "Unit": "USD"}},
                        },
                    ],
                }
            ]
        },
    )

    spend = cloud_mac.lookup_host_spend("mac2.metal", "us-east-1")

    assert spend.amount == pytest.approx(8.12)


def test_a_cost_explorer_failure_is_reported_not_replaced_with_a_guess(monkeypatch):
    _stub_clients(monkeypatch, _StubEc2(), cost=RuntimeError("AccessDeniedException"))

    spend = cloud_mac.lookup_host_spend("mac2.metal", "us-east-1")

    assert spend.amount is None
    assert "AccessDeniedException" in spend.error


def test_no_matching_charges_is_an_explained_absence_not_zero(monkeypatch):
    """Zero and "AWS has not billed this yet" are different answers, and
    reporting the second as the first would say a host allocated an hour ago is
    free."""
    _stub_clients(monkeypatch, _StubEc2(), cost={"ResultsByTime": []})

    spend = cloud_mac.lookup_host_spend("mac2.metal", "us-east-1")

    assert spend.amount is None
    assert "no Dedicated Host charges" in spend.error


# --- Verification is recorded, and cached ---------------------------------


def test_verifying_the_record_writes_down_what_aws_said_and_what_it_cost(monkeypatch):
    _record_host()
    _settings(monkeypatch)
    _stub_clients(
        monkeypatch,
        _StubEc2(hosts=[{"HostId": "h-0abc", "State": "available"}]),
        cost=_ce_response([("2026-10-01", "8.12")]),
    )

    cloud_mac.verify_mac_record(_args())

    record = cloud_mac.load_mac_record()
    assert record["mac_host_present"] is True
    assert record["mac_verified_at"]
    assert record["mac_spend_amount"] == pytest.approx(8.12)
    pending = cloud_mac.pending_release()
    assert pending["accrued_cost"] == pytest.approx(8.12)
    assert pending["accrued_source"] == "aws-cost-explorer"


def test_verifying_a_released_host_clears_the_block_rather_than_labelling_it(monkeypatch):
    """ "A host AWS reports as absent is not described as 'still billing'" -- and
    the way to guarantee that is for there to be no block to describe."""
    _record_host()
    _settings(monkeypatch)
    _stub_clients(monkeypatch, _StubEc2(error_code="InvalidHostID.NotFound"))

    result = cloud_mac.verify_mac_record(_args())

    assert result == {"host_present": False, "cleared": True}
    assert cloud_mac.pending_release() == {}


def test_the_cost_query_is_not_repeated_on_every_poll(monkeypatch):
    """Cost Explorer bills per request and its granularity is a day, so a
    dashboard poll must not be able to turn an observability surface into a line
    item."""
    _record_host()
    _settings(monkeypatch)
    _, explorer = _stub_clients(
        monkeypatch,
        _StubEc2(hosts=[{"HostId": "h-0abc", "State": "available"}]),
        cost=_ce_response([("2026-10-01", "8.12")]),
    )

    cloud_mac.verify_mac_record(_args())
    cloud_mac.verify_mac_record(_args())

    assert len(explorer.requests) == 1


def test_a_record_the_run_did_not_confirm_is_not_treated_as_confirmed():
    _record_host(mac_host_present=True, mac_verified_at="2026-08-22T18:00:00+00:00")

    assert (
        cloud_mac.host_confirmed_this_run(
            cloud_mac.load_mac_record(), now=datetime(2026, 10, 3, tzinfo=UTC)
        )
        is False
    )


def test_a_fresh_confirmation_counts():
    now = datetime(2026, 10, 3, 19, 30, tzinfo=UTC)
    _record_host(mac_host_present=True, mac_verified_at=now.isoformat())

    assert cloud_mac.host_confirmed_this_run(cloud_mac.load_mac_record(), now=now) is True
