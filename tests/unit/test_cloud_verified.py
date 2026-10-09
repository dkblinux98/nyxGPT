"""The one decision: may a recorded value be stated as a fact about AWS? (#4181)

`cloud_verified` exists because every surface that describes the cloud reads a
local record and, until #4181, each decided for itself how much to believe it.
They disagreed inside a single command's output: `nyxgpt cloud status` printed
two INCOHERENT rows about a record and then, three rows later, concluded "an
instance exists and is being billed" from the same fields.

These tests pin the decision itself -- the four ways a confirmation can fail,
and the two accessors that make "reported rather than used" structural rather
than a convention each reader has to remember.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nyxgpt import cloud_verified

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

RECORD = {
    "mac_host_id": "h-0abc",
    "mac_instance_id": "i-0mac",
    "mac_instance_type": "mac2.metal",
}


def _observe(**over):
    """An observation of RECORD, confirmed in this run unless told otherwise."""
    kwargs = {
        "present": True,
        "confirmed_at": (NOW - timedelta(seconds=10)).isoformat(),
        "profile": "nyxgpt",
        "account_id": "066835328281",
        "account_label": "nyxgpt (066835328281)",
        "expect_profile": "nyxgpt",
        "now": NOW,
    }
    kwargs.update(over)
    return cloud_verified.observe(RECORD, **kwargs)


# --- The gate --------------------------------------------------------------


def test_a_fresh_answer_from_the_resolved_account_is_a_confirmation():
    observation = _observe()

    assert observation.confirmed is True
    assert observation.coherent is True
    assert observation.usable is True
    assert "confirmed at AWS in nyxgpt (066835328281)" in observation.provenance


def test_nothing_having_asked_is_not_a_confirmation():
    observation = _observe(present=None, confirmed_at="")

    assert observation.usable is False
    assert cloud_verified.NOT_ASKED in observation.reason


def test_an_answer_older_than_this_run_is_not_a_confirmation():
    """A `mac_verified_at` from an hour ago describes what AWS said then.

    The whole point of the field is to distinguish a confirmed resource from a
    remembered one; a confirmation with no expiry is a memory with a timestamp.
    """
    stale = (NOW - timedelta(seconds=cloud_verified.CONFIRMATION_MAX_AGE_SECONDS + 1)).isoformat()

    observation = _observe(confirmed_at=stale)

    assert observation.usable is False
    assert "older than this run" in observation.reason


def test_an_answer_from_another_account_is_not_a_confirmation():
    """#4181 finding 1, stated as a rule.

    An AWS account answers `InvalidHostID.NotFound` for every host it does not
    own, so "not found" from the wrong account is indistinguishable from
    "released" -- and a *positive* answer from the wrong account describes some
    other account's resource with the same id. Live, `nyxgpt cloud status`
    asked `arn:aws:iam::551292530955:root` about a host in 066835328281 and got
    the right answer only by coincidence.
    """
    observation = _observe(expect_profile="other-account")

    assert observation.usable is False
    # Both accounts named, not just "the wrong account": the operator's next
    # action is to re-run with one of the two, and a label does not say which.
    assert "nyxgpt (066835328281)" in observation.reason
    assert "this run resolved 'other-account'" in observation.reason
    assert "every resource it does not own" in observation.reason


def test_a_record_that_contradicts_itself_is_never_usable_however_confirmed():
    """#4181 finding 3. A confirmation proves the RESOURCE exists.

    It does not make a block whose fields were written by different runs
    describe the resource that was confirmed. `nyxgpt cloud status` printed
    "still billing -- AWS confirmed ..." directly above its own INCOHERENT
    rows, and then filled the instance rows from the disqualified fields.
    """
    observation = _observe(findings=("release_scheduled_at precedes allocated_at",))

    assert observation.confirmed is True
    assert observation.coherent is False
    assert observation.usable is False
    assert "contradicts itself" in observation.provenance


def test_a_definite_no_is_reported_as_gone_rather_than_as_unknown():
    observation = _observe(present=False)

    assert observation.usable is False
    assert observation.reason == "AWS reported it as gone"


def test_the_recorded_reason_is_preferred_over_a_generic_one():
    """#4181 finding 6: say what is missing, not the remedy for something else.

    `nyxgpt cloud status` printed "NOT confirmed at AWS in this run -- `nyxgpt
    cloud status` asks" from inside that very command, because the only wording
    available was the one for "nobody has asked".
    """
    observation = _observe(present=None, reason="boto3 is not installed")

    assert "boto3 is not installed" in observation.reason
    assert "boto3 is not installed" in observation.provenance


# --- fact() vs reported() --------------------------------------------------


def test_fact_withholds_a_value_no_run_confirmed():
    observation = _observe(present=None, confirmed_at="")

    assert observation.fact("mac_instance_id") == ""
    assert observation.fact("mac_instance_id", default=None) is None


def test_reported_yields_the_value_whatever_the_gate_says():
    """An operator whose only view of the machine is this record needs the ids.

    Withholding them would trade one failure for another: the #4122 report
    printed the Linux substrate's default `m5.xlarge` for a `mac2.metal`
    because it had no Mac value to read. The value is shown; the assertion is
    not made.
    """
    observation = _observe(present=None, confirmed_at="")

    assert observation.reported("mac_instance_id") == "i-0mac"


def test_fact_yields_the_value_once_the_gate_opens():
    assert _observe().fact("mac_instance_id") == "i-0mac"


def test_a_key_the_record_does_not_hold_is_the_default_either_way():
    observation = _observe()

    assert observation.fact("mac_public_ip") == ""
    assert observation.reported("mac_public_ip", default="not recorded") == "not recorded"


# --- Serialization ---------------------------------------------------------


def test_the_payload_carries_the_gate_and_the_sentence_for_a_closed_one():
    """The dashboard reads these two and re-derives nothing (D-066).

    A second copy of "verified_at is set, so say still billing" in TypeScript
    is exactly how the card went on asserting a charge over a record whose own
    fields contradicted each other.
    """
    payload = _observe(findings=("a contradiction",)).to_dict()

    assert payload["usable"] is False
    assert payload["confirmed"] is True
    assert payload["coherent"] is False
    assert payload["findings"] == ["a contradiction"]
    assert payload["account_label"] == "nyxgpt (066835328281)"
    assert payload["provenance"] == _observe(findings=("a contradiction",)).provenance


def test_the_recorded_block_is_copied_rather_than_aliased():
    """An observation is a reading, so mutating it must not edit the record."""
    observation = _observe()

    observation.recorded["mac_host_id"] = "h-tampered"

    assert RECORD["mac_host_id"] == "h-0abc"


# --- Timestamp parsing (shared with `cloud_mac.parse_timestamp`) -----------


def test_a_naive_timestamp_is_read_as_utc_rather_than_rejected():
    assert cloud_verified.parse_timestamp("2026-10-09T12:00:00") == NOW


def test_a_trailing_z_is_accepted():
    assert cloud_verified.parse_timestamp("2026-10-09T12:00:00Z") == NOW


def test_an_unparseable_timestamp_is_unknown_rather_than_an_exception():
    assert cloud_verified.parse_timestamp("yesterday") is None
    assert cloud_verified.parse_timestamp("") is None
    assert cloud_verified.is_fresh("yesterday", now=NOW) is False


def test_a_never_asked_record_is_not_called_a_wrong_account_read():
    """ "Whose account was that answer from?" only applies when there was one.

    Asking it of a record nothing has checked reported a wrong-account
    mismatch, and sent the operator at `--profile` over a record whose problem
    is that nobody has looked.
    """
    observation = _observe(
        present=None, confirmed_at="", profile="", account_label="", expect_profile="nyxgpt"
    )

    assert observation.usable is False
    assert observation.reason == cloud_verified.NOT_ASKED
