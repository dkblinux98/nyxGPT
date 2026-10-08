"""Unit tests for `~/.nyxGPT/cloud/state.json` as a record of CURRENT state (#4136).

The defect these pin down cost real money on 2026-10-03, and it had already cost
it on 2026-08-22 (#3993). The file was merged into field by field, in one flat
namespace shared by both substrates, with no record of which run wrote any
value. So a deploy that provisioned a *new* EC2 Mac updated the instance fields
and left the host fields naming the previous, released host -- a new instance
stapled to a dead host, a combination that never existed. The consent gate then
read that record as fact, skipped a priced disclosure, announced "no new
24-hour minimum", and allocated one.

So the assertions here are mostly about what is *absent*. The interesting
question is never "did the write land" -- that was never the failure -- it is
"did everything the write did not mention go away". Several tests walk the whole
key set rather than the keys the caller passed, because a test that only checks
the fields a deploy writes is exactly the test that passed through all three
occurrences.
"""

import json
import os

import pytest

from nyxgpt import cloud_infra, cloud_record


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point the record (and its archive) at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_infra, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    return cloud_dir


#: The record the owner's machine actually held after the 2026-10-03 deploy,
#: before correction. Two fields describe the new substrate; five describe a
#: host AWS released three days earlier; the last is detectably impossible --
#: 78 seconds BEFORE the host it describes was allocated.
STALE_2026_10_03 = {
    "mac_instance_id": "i-00e566c3560462cd4",
    "mac_public_ip": "34.201.63.175",
    "mac_host_id": "h-06c438d25077be888",
    "mac_allocated_at": "2026-09-30T15:49:43+00:00",
    "mac_release_at": "2026-10-01T16:19:43+00:00",
    "mac_release_scheduled": True,
    "mac_release_scheduled_at": "2026-10-03T19:25:47.725000+00:00",
}


def _written() -> dict:
    return json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))


def _seed(values: dict) -> None:
    cloud_infra.CLOUD_STATE_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")


# --- A block is replaced whole --------------------------------------------


def test_a_write_decides_every_field_in_the_block_not_only_the_ones_it_carries():
    """The #3993 criterion, this time asserted over the WHOLE key set.

    "No stale security-group or other prior-substrate ids survive" was written
    in 2026-08-22 and was still unmet on 2026-10-03, because the tests that
    guarded it checked the fields the write mentioned. Five prior-substrate
    values survived. This walks all of `MAC_BLOCK_KEYS` instead: every one is
    either what this write said, or absent.
    """
    _seed(dict(STALE_2026_10_03))

    written = cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC,
        {
            "mac_host_id": "h-0c8f9957132fb0794",
            "mac_instance_id": "i-0newinstance",
            "mac_region": "us-east-1",
        },
    )

    assert written == {
        "mac_host_id": "h-0c8f9957132fb0794",
        "mac_instance_id": "i-0newinstance",
        "mac_region": "us-east-1",
    }
    state = _written()
    for key in cloud_record.MAC_BLOCK_KEYS:
        if key in written:
            assert state[key] == written[key]
        else:
            assert key not in state, f"{key} survived a write that did not mention it"
    # Named explicitly as well as covered by the loop: these are the five that
    # actually survived, and the one that was impossible.
    for stale in (
        "mac_allocated_at",
        "mac_release_at",
        "mac_release_scheduled",
        "mac_release_scheduled_at",
    ):
        assert stale not in state


def test_a_none_valued_field_is_absent_rather_than_recorded_as_null():
    """ "Unknown" and "absent" are both honest; `null` in a file whose name means
    current is a third thing every consumer has to special-case."""
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC,
        {"mac_host_id": "h-0abc", "mac_hourly_rate": None, "mac_public_ip": ""},
    )

    state = _written()
    assert "mac_hourly_rate" not in state
    # An empty string is a value the caller chose to record; only `None` means
    # "there is no answer". Kept deliberately -- `cloud_mac` filters its own.
    assert state["mac_public_ip"] == ""


def test_keys_the_substrate_does_not_own_are_ignored_rather_than_recorded():
    """So a caller can hand over a Terraform output dict wholesale."""
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC,
        {"mac_host_id": "h-0abc", "security_group_id": "sg-1", "whatever": 1},
    )

    assert _written() == {"mac_host_id": "h-0abc"}


def test_writing_one_substrate_leaves_the_other_substrates_block_alone():
    """Both substrates are live at once while a Mac and a Linux box coexist, and
    the Mac's block is the one that records a resource still being billed."""
    _seed({"mac_host_id": "h-0abc", "mac_region": "us-east-1"})

    cloud_record.write_block(
        cloud_record.SUBSTRATE_AWS, {"instance_id": "i-1", "security_group_id": "sg-1"}
    )

    state = _written()
    assert state["mac_host_id"] == "h-0abc"
    assert state["instance_id"] == "i-1"


def test_an_unknown_substrate_name_is_a_clean_error():
    with pytest.raises(cloud_record.CloudCommandError, match="not a recorded substrate"):
        cloud_record.write_block("gcp", {})


# --- The superseded record leaves the file --------------------------------


def test_the_superseded_block_is_archived_under_a_different_name():
    """There may be a reason to keep the previous run -- diagnosis -- but it is
    not kept in the file that answers "what is deployed now"."""
    _seed(dict(STALE_2026_10_03))

    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0c8f9957132fb0794"}, reason="re-provisioned"
    )

    archive = cloud_record.load_archive()
    assert len(archive) == 1
    entry = archive[0]
    assert entry["substrate"] == cloud_record.SUBSTRATE_MAC
    assert entry["reason"] == "re-provisioned"
    assert entry["block"] == STALE_2026_10_03
    assert entry["archived_at"]
    # A different file, deliberately: nothing reading `state.json` should ever
    # have to ask which run a value came from.
    assert cloud_record.archive_file() != cloud_record.state_file()
    assert cloud_record.archive_file().name == "state-archive.jsonl"


def test_rewriting_the_same_values_does_not_archive_a_copy_of_itself():
    """A reconcile of an unchanged substrate is the common case. Archiving it
    every time would bury the one entry that matters under duplicates."""
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc"})
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc"})

    assert cloud_record.load_archive() == []


def test_the_archive_is_capped_so_it_cannot_grow_without_bound():
    for index in range(cloud_record.ARCHIVE_LIMIT + 10):
        cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": f"h-{index}"})

    archive = cloud_record.load_archive(limit=1000)
    assert len(archive) == cloud_record.ARCHIVE_LIMIT
    # Newest first, and the oldest entries are the ones dropped.
    assert archive[0]["block"]["mac_host_id"] == f"h-{cloud_record.ARCHIVE_LIMIT + 8}"


def test_an_unwritable_archive_does_not_stop_the_record_being_corrected(monkeypatch):
    """Losing a diagnostic copy is a smaller harm than leaving `state.json`
    describing a substrate that is gone."""
    _seed({"mac_host_id": "h-old"})
    monkeypatch.setattr(
        cloud_record.Path, "write_text", _raise_oserror_for(cloud_record.archive_file())
    )

    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-new"})

    assert _written()["mac_host_id"] == "h-new"


def _raise_oserror_for(blocked):
    """A `Path.write_text` that fails for one path and works for the rest."""
    original = cloud_record.Path.write_text

    def write_text(self, *args, **kwargs):
        if self == blocked:
            raise OSError("read-only")
        return original(self, *args, **kwargs)

    return write_text


def test_a_key_belonging_to_no_substrate_is_archived_and_dropped():
    """Nothing can refresh it, so by construction it can only get staler --
    which is the ambiguity the whole issue is about. The archive is where an
    operator looks if one mattered."""
    _seed({"mac_host_id": "h-0abc", "left_over_from_some_older_version": "value"})

    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc"})

    assert "left_over_from_some_older_version" not in _written()
    assert any(
        entry["block"] == {"left_over_from_some_older_version": "value"}
        for entry in cloud_record.load_archive()
    )


def test_foreign_keys_are_reportable_before_they_are_dropped():
    _seed({"mac_host_id": "h-0abc", "instance_id": "i-1", "mystery": 1})

    assert cloud_record.foreign_keys() == ["mystery"]


# --- The gated merge ------------------------------------------------------


def test_an_amend_updates_its_fields_and_leaves_the_rest_of_the_block_standing():
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC,
        {"mac_host_id": "h-0abc", "mac_region": "us-east-1", "mac_release_scheduled": False},
    )

    cloud_record.amend_block(
        cloud_record.SUBSTRATE_MAC,
        {"mac_release_scheduled": True, "mac_release_scheduled_at": "2026-10-04T00:00:00+00:00"},
        expect={"mac_host_id": "h-0abc"},
    )

    state = _written()
    assert state["mac_region"] == "us-east-1"
    assert state["mac_release_scheduled"] is True
    assert state["mac_release_scheduled_at"] == "2026-10-04T00:00:00+00:00"


def test_an_amend_against_a_record_that_has_been_replaced_is_refused():
    """The merge IS the defect. An update resolved against one host must not
    land on another: `mac_release_scheduled = true` written against whatever
    host happened to be recorded is how a `release_scheduled_at` 78 seconds
    earlier than its host's `allocated_at` got into the file.
    """
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0c8f9957132fb0794"})

    with pytest.raises(cloud_record.StaleRecordError, match="no longer describes"):
        cloud_record.amend_block(
            cloud_record.SUBSTRATE_MAC,
            {"mac_release_scheduled": True},
            expect={"mac_host_id": "h-06c438d25077be888"},
        )

    assert "mac_release_scheduled" not in _written()


def test_an_amend_to_none_removes_the_field():
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc", "mac_spend_error": "boom"}
    )

    cloud_record.amend_block(
        cloud_record.SUBSTRATE_MAC,
        {"mac_spend_error": None, "mac_spend_amount": 12.02},
        expect={"mac_host_id": "h-0abc"},
    )

    state = _written()
    assert "mac_spend_error" not in state
    assert state["mac_spend_amount"] == 12.02


# --- Clearing -------------------------------------------------------------


def test_clearing_a_block_removes_it_entirely_rather_than_emptying_it():
    """A reader must never have to decide whether `""` means "no security group"
    or "we stopped tracking it"."""
    cloud_record.write_block(
        cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc", "mac_region": "us-east-1"}
    )
    cloud_record.write_block(cloud_record.SUBSTRATE_AWS, {"instance_id": "i-1"})

    cloud_record.clear_block(cloud_record.SUBSTRATE_MAC, reason="AWS reports it released")

    assert _written() == {"instance_id": "i-1"}
    assert cloud_record.load_archive()[0]["reason"] == "AWS reports it released"


def test_clearing_the_last_block_removes_the_file():
    """ "Nothing is deployed" is better said by the file's absence than by an
    empty object a reader has to interpret."""
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc"})

    cloud_record.clear_block(cloud_record.SUBSTRATE_MAC)

    assert not cloud_infra.CLOUD_STATE_FILE.exists()
    assert cloud_record.load_state() == {}
    assert cloud_record.load_block(cloud_record.SUBSTRATE_MAC) == {}


# --- The write itself -----------------------------------------------------


def test_the_record_is_written_atomically_and_left_private(_isolated_cloud_home):
    """Temp file plus `os.replace`, so a killed writer cannot leave a
    half-written record for the next command to parse as current state -- and
    0600, because the file names the operator's deployment."""
    cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-0abc"})

    assert oct(os.stat(cloud_infra.CLOUD_STATE_FILE).st_mode)[-3:] == "600"
    # No staging file left behind, under any name.
    assert sorted(p.name for p in _isolated_cloud_home.iterdir()) == ["state.json"]


def test_a_write_that_fails_mid_flight_leaves_no_debris(monkeypatch, _isolated_cloud_home):
    _seed({"mac_host_id": "h-old"})
    monkeypatch.setattr(cloud_record.os, "replace", _boom)

    with pytest.raises(RuntimeError):
        cloud_record.write_block(cloud_record.SUBSTRATE_MAC, {"mac_host_id": "h-new"})

    # The previous record is intact (the replace never happened) and nothing
    # partial is lying around next to it.
    assert _written()["mac_host_id"] == "h-old"
    assert sorted(p.name for p in _isolated_cloud_home.iterdir()) == [
        "state-archive.jsonl",
        "state.json",
    ]


def _boom(*args, **kwargs):
    raise RuntimeError("replace failed")


def test_an_unreadable_record_reads_as_empty_rather_than_raising():
    """Every consumer treats `{}` as "nothing recorded", which is the safe
    reading of a corrupted file -- the unsafe one is a traceback out of a
    lockout-recovery command."""
    cloud_infra.CLOUD_STATE_FILE.write_text("{not json", encoding="utf-8")

    assert cloud_record.load_state() == {}
    assert cloud_record.load_block(cloud_record.SUBSTRATE_MAC) == {}


def test_a_json_document_that_is_not_an_object_reads_as_empty():
    cloud_infra.CLOUD_STATE_FILE.write_text("[1, 2, 3]", encoding="utf-8")

    assert cloud_record.load_state() == {}


def test_the_two_substrates_key_sets_do_not_overlap():
    """They share one flat namespace, so an overlapping key would make "replace
    this substrate's block" ambiguous -- and the ambiguity is the bug."""
    assert not set(cloud_record.AWS_BLOCK_KEYS) & set(cloud_record.MAC_BLOCK_KEYS)
