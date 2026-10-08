"""`~/.nyxGPT/cloud/state.json` -- current state, per substrate, written whole (#4136).

This file answers one question: **what is deployed now?** Until #4136 it also
answered, accidentally, "what was deployed at some point in the past", because
every command merged the handful of fields it happened to touch into one flat
namespace shared by both substrates. A deploy that provisioned a new EC2 Mac
wrote the instance fields and left the *host* fields pointing at the previous,
released host -- a combination that never existed in reality. `nyxgpt cloud
deploy --os macos` then believed a released Dedicated Host still existed,
skipped the priced disclosure and the `allocate` consent prompt, announced "no
new 24-hour minimum", and allocated a new host anyway. That was the third
occurrence of the same mechanism (#3993 was the first).

Three properties fix it, and all three live here rather than in each caller --
a rule enforced in one place cannot be forgotten by the next command added:

* **A block is the unit of writing.** Each substrate owns a fixed set of keys
  (`BLOCK_KEYS`). `write_block` replaces *every* one of them: a key the new
  values do not carry is **dropped**, not left holding the previous
  substrate's answer. "This substrate has no such id" is an honest answer; a
  three-day-old host id presented as current is not.
* **An in-place field update must prove the resource is unchanged.**
  `amend_block` is the only merge there is, and it takes an `expect` mapping it
  verifies first. Recording "the release is scheduled" against whatever host
  the block happens to name now is how an impossible record
  (`release_scheduled_at` 78 seconds *before* `allocated_at`) got written.
* **The write is atomic, and what it supersedes leaves this file.** Blocks are
  written through a temporary file and `os.replace`, so a killed process cannot
  leave a half-written record; and the block being replaced is appended to
  `state-archive.jsonl` first. Diagnosis keeps its history under a name that
  does not claim to be current -- nothing reading `state.json` has to ask which
  run a value came from.

Keys belonging to no registered block are archived and dropped on the next
write. Nothing can refresh them, so by construction they can only become
staler; keeping them would reintroduce exactly the ambiguity above, and the
archive is where an operator looks if one mattered.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyxgpt.cloud import CloudCommandError

# --- Substrates and the keys each one owns ------------------------------

#: The Linux substrate `nyxgpt cloud infra` provisions.
SUBSTRATE_AWS = "aws"

#: The EC2 Mac Dedicated Host plus the instance on it (`cloud_mac`).
SUBSTRATE_MAC = "mac"

#: Keys the Linux substrate owns. Re-exported as `cloud_infra.STATE_KEYS`.
AWS_BLOCK_KEYS: tuple[str, ...] = (
    "region",
    "vpc_id",
    "security_group_id",
    "instance_id",
    "instance_type",
    "public_ip",
    "private_ip",
    "ssh_key_name",
)

#: Keys the EC2 Mac owns. Re-exported as `cloud_mac.STATE_KEYS`.
MAC_BLOCK_KEYS: tuple[str, ...] = (
    "mac_host_id",
    "mac_instance_id",
    "mac_instance_type",
    "mac_region",
    "mac_availability_zone",
    "mac_public_ip",
    "mac_security_group_id",
    "mac_ami_id",
    "mac_root_volume_size",
    "mac_allocated_at",
    "mac_release_at",
    "mac_hourly_rate",
    "mac_release_scheduled",
    "mac_release_scheduled_at",
    # What AWS said, and when it said it (#4136). Every other field above is a
    # claim about AWS with no expiry; these are what let a reader tell a
    # confirmed host from a remembered one, and they are why no surface has to
    # explain that its data might be stale.
    "mac_verified_at",
    "mac_host_present",
    # The spend figure, as Cost Explorer reported it. Recorded rather than
    # recomputed from `now - allocated_at`, which kept counting after AWS
    # stopped charging: the display read $48.44 for a host AWS billed $12.02
    # for and had not charged for in two days.
    "mac_spend_amount",
    "mac_spend_currency",
    "mac_spend_through",
    "mac_spend_as_of",
    "mac_spend_error",
)

#: Every substrate's key set, by substrate name.
BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    SUBSTRATE_AWS: AWS_BLOCK_KEYS,
    SUBSTRATE_MAC: MAC_BLOCK_KEYS,
}

#: How many superseded blocks `state-archive.jsonl` keeps. Bounded because it
#: is a diagnostic aid, not a ledger: the question it answers ("what did the
#: previous run record?") is always about the last few runs.
ARCHIVE_LIMIT = 50


class StaleRecordError(CloudCommandError):
    """An `amend_block` whose `expect` did not match what the file now holds.

    Raised rather than merged, because the merge is the defect: the caller
    resolved its values against a record that has since been replaced, and
    writing them over the current block is how a new substrate ends up
    wearing a previous one's fields.
    """


# --- Paths -------------------------------------------------------------
#
# Resolved through functions, not module constants, because the tests (and
# `nyxgpt ops`) relocate `cloud_infra.CLOUD_DIR` wholesale. Imported lazily
# inside each one: `cloud_infra` imports this module's consumers, so a
# module-scope import here would close a cycle.


def state_file() -> Path:
    """Path to `state.json` -- the record of what is deployed now."""
    from nyxgpt import cloud_infra

    return cloud_infra.CLOUD_STATE_FILE


def archive_file() -> Path:
    """Path to `state-archive.jsonl` -- superseded blocks, newest last.

    A different file under a different name, deliberately. A superseded record
    may be worth keeping for diagnosis; it may not be kept in the file whose
    name means *current*.
    """
    from nyxgpt import cloud_infra

    return cloud_infra.CLOUD_DIR / "state-archive.jsonl"


# --- Reading -----------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Read the whole record, returning `{}` when absent or unreadable."""
    path = state_file()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _keys(substrate: str) -> tuple[str, ...]:
    """The key set `substrate` owns, or a clean error for an unknown name."""
    try:
        return BLOCK_KEYS[substrate]
    except KeyError:
        raise CloudCommandError(
            f"{substrate!r} is not a recorded substrate. Known: "
            f"{', '.join(sorted(BLOCK_KEYS))}."
        ) from None


def load_block(substrate: str) -> dict[str, Any]:
    """Return `substrate`'s block: only its own keys, only those present."""
    state = load_state()
    return {key: state[key] for key in _keys(substrate) if key in state}


def foreign_keys(state: dict[str, Any] | None = None) -> list[str]:
    """Keys in the record that belong to no substrate, sorted.

    These are what `write_block` archives and drops. Exposed so a surface can
    report having found them rather than silently discarding them.
    """
    owned = {key for keys in BLOCK_KEYS.values() for key in keys}
    return sorted(key for key in (state if state is not None else load_state()) if key not in owned)


# --- Writing -----------------------------------------------------------


def _utc_now_iso() -> str:
    """Current UTC instant, ISO-8601. A seam for the tests."""
    return datetime.now(UTC).isoformat()


def _write_state(state: dict[str, Any]) -> None:
    """Replace the record with `state`, atomically, 0600.

    Temp file in the same directory plus `os.replace`, so a reader never sees
    a half-written record and a killed writer never leaves one. An empty
    record removes the file: "nothing is deployed" is better said by the file's
    absence than by an empty object a reader has to interpret.
    """
    path = state_file()
    if not state:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    staged = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _archive(entries: list[dict[str, Any]]) -> None:
    """Append superseded blocks to `state-archive.jsonl`, trimmed to the cap.

    Best effort: an unwritable archive must never stop the record itself being
    corrected. Losing a diagnostic copy is a smaller harm than leaving
    `state.json` describing a substrate that is gone.
    """
    entries = [entry for entry in entries if entry.get("block")]
    if not entries:
        return
    path = archive_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = [line for line in existing if line.strip()]
        lines.extend(json.dumps(entry) for entry in entries)
        path.write_text("\n".join(lines[-ARCHIVE_LIMIT:]) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return


def load_archive(limit: int = ARCHIVE_LIMIT) -> list[dict[str, Any]]:
    """Return archived blocks, newest first (`[]` when there are none)."""
    path = archive_file()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
        if len(entries) >= limit:
            break
    return entries


def _rewrite(
    substrate: str,
    block: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Replace `substrate`'s block with `block` and drop unowned keys.

    The one function that writes. Everything superseded -- the previous block,
    and any key belonging to no substrate -- is archived before it goes.
    """
    keys = _keys(substrate)
    state = load_state()

    previous = {key: state[key] for key in keys if key in state}
    stray = {key: state[key] for key in foreign_keys(state)}

    archived: list[dict[str, Any]] = []
    # Equality, not identity: re-recording the same values is the common case
    # (a reconcile of an unchanged substrate) and archiving it every time would
    # bury the one entry that matters under copies of the current state.
    if previous and previous != block:
        archived.append(
            {
                "archived_at": _utc_now_iso(),
                "substrate": substrate,
                "reason": reason or "superseded",
                "block": previous,
            }
        )
    if stray:
        archived.append(
            {
                "archived_at": _utc_now_iso(),
                "substrate": "",
                "reason": "key belongs to no substrate and nothing can refresh it",
                "block": stray,
            }
        )
    _archive(archived)

    updated = {
        key: value
        for key, value in state.items()
        if key not in keys and key not in stray
    }
    updated.update(block)
    _write_state(updated)
    return dict(block)


def write_block(
    substrate: str,
    values: dict[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Replace `substrate`'s whole block with `values`. Returns what was written.

    Every key the substrate owns is decided by this call: one present in
    `values` with a non-`None` value is recorded, and **every other one is
    dropped**. That is the point -- a merge that wrote only the keys the new
    outputs carried is what left a new instance stapled to a released host.

    `values` may carry keys the substrate does not own; they are ignored, so a
    caller can hand over a Terraform output dict wholesale. An empty result
    clears the block, which is the honest record of a substrate that is gone.
    """
    block = {
        key: values[key] for key in _keys(substrate) if key in values and values[key] is not None
    }
    return _rewrite(substrate, block, reason=reason or "replaced by a newer substrate")


def amend_block(
    substrate: str,
    updates: dict[str, Any],
    *,
    expect: dict[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    """Update fields of `substrate`'s block in place, proving it is the same resource.

    The only merge in this module, and it is gated. `expect` names the fields
    that identify the resource (for the Mac, `mac_host_id`); each is compared
    against what the record holds *now*, and a mismatch raises
    `StaleRecordError` instead of writing. Without that check an update
    resolved against one host lands on another -- which is how
    `mac_release_scheduled = true` came to describe a host allocated 78 seconds
    *after* the schedule was supposedly created.

    An update whose value is `None` removes that field: a field with no current
    answer is absent, not stale.
    """
    keys = _keys(substrate)
    current = load_block(substrate)
    for key, wanted in expect.items():
        if str(current.get(key) or "") != str(wanted or ""):
            raise StaleRecordError(
                f"{state_file()} no longer describes {key}={wanted!r} "
                f"(it now holds {current.get(key)!r}), so this update was resolved against a "
                "record that has been replaced. Re-read the record and retry."
            )
    block = dict(current)
    for key, value in updates.items():
        if key not in keys:
            continue
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return _rewrite(substrate, block, reason=reason or "fields updated in place")


def clear_block(substrate: str, *, reason: str = "") -> None:
    """Drop `substrate`'s block entirely, archiving what it held.

    Called when the substrate is *gone*. The block does not become empty
    strings or nulls -- it ceases to exist, because a reader must never have to
    decide whether `""` means "no security group" or "we stopped tracking it".
    """
    _rewrite(substrate, {}, reason=reason or "substrate destroyed")
