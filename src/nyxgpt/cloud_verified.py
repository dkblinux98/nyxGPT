"""One way to read a cloud record: a fact is what AWS confirmed in THIS run (#4181).

Every `nyxgpt cloud` surface that describes AWS reads a local record --
`~/.nyxGPT/cloud/state.json` -- because that is the only thing that still
answers when credentials have expired, when the dashboard is polling, and when
the resource has outlived the deploy that made it. The record is useful. It is
not evidence.

Until this module, each reader decided for itself how much to believe it, and
they disagreed. `cloud_mac.pending_release()` returned the raw fields plus a
hard-coded `"billing": True`; `cloud_deploy._print_incomplete_summary`
concluded "an instance exists and is being billed" from the presence of a host
id; the dashboard's Dedicated Host card keyed its heading off `verified_at`
alone. In the owner's 2026-10-09 acceptance round all three spoke about an
EC2 Mac that AWS no longer had, from a record whose own fields contradicted
each other, and `nyxgpt cloud status` printed the contradiction and the
conclusion drawn from it three rows apart.

So the decision -- *may this value be stated as a fact about AWS?* -- lives
here, once, and the answer is reached the same way for every substrate and
every surface:

* **`confirmed`** -- AWS answered about this exact resource, within the last
  few minutes, under the credentials this run resolved. Anything older is a
  memory of a previous run; anything from another account is an answer about a
  different resource with the same name (the default account returns
  `InvalidHostID.NotFound` for every host it does not own, so "not found" from
  the wrong account is indistinguishable from "released").
* **`coherent`** -- the record's own fields can describe a single moment. These
  checks cost nothing: no credentials, no network. A block that fails them was
  written by more than one run about more than one resource, so no two of its
  fields may be read together even if AWS confirms the resource exists.
* **`usable`** -- both. **This is the only gate a claim may be made through.**

And the two accessors are what make "reported rather than used" structural
rather than a convention each reader has to remember:

* **`fact(key)`** returns the value only when `usable`, and the empty default
  otherwise. A claim site that calls it cannot accidentally assert a recorded
  field.
* **`reported(key)`** returns the value unconditionally, for a row that is
  labelled as what the record holds. That is still worth printing -- an
  operator with a machine they cannot otherwise see needs the ids -- but it
  carries no assertion.

Deliberately dependency-free (stdlib only): `cloud_infra`, `cloud_mac` and
`cloud_identity` all import each other in some direction already, and the one
place the believing/not-believing decision lives must be importable from any
of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: How recent an answer from AWS has to be to count as "this run". Generous
#: enough to cover a slow Terraform sync between a reconcile and the decision
#: that reads it, short enough that it can only ever mean the current command.
CONFIRMATION_MAX_AGE_SECONDS = 300.0

#: The reason reported when no run has asked AWS at all -- distinct from a run
#: that asked and could not get an answer, which carries its own reason.
NOT_ASKED = "nothing on this machine has asked AWS about it in this run"


def utc_now() -> datetime:
    """Current UTC time, as an aware datetime. A seam for the tests."""
    return datetime.now(UTC)


def parse_timestamp(value: str) -> datetime | None:
    """Parse a recorded ISO-8601 instant, or `None` when it is not one.

    `None` rather than an exception, and naive values are rejected rather than
    assumed to be UTC: a timestamp nobody can place is exactly the kind of
    field this module exists to stop being read as a fact. Callers treat it as
    "unknown", which is the honest reading of an unparseable record.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        # `Z` is legal ISO-8601 and is what AWS returns; `fromisoformat` only
        # learned it in 3.11, and this project still supports 3.10.
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_fresh(confirmed_at: str, *, now: datetime | None = None) -> bool:
    """Was `confirmed_at` written within `CONFIRMATION_MAX_AGE_SECONDS`?"""
    moment = parse_timestamp(confirmed_at)
    if moment is None:
        return False
    return ((now or utc_now()) - moment).total_seconds() <= CONFIRMATION_MAX_AGE_SECONDS


@dataclass(frozen=True)
class Observation:
    """What this machine may say about one cloud resource, and on whose authority.

    Built by `observe`, never by hand: the `confirmed` flag is a conclusion
    about freshness and credentials, and a caller that could set it directly
    would be back to each reader deciding for itself.
    """

    #: The record's own fields, verbatim. Reportable; never a claim on its own.
    recorded: dict[str, Any]
    #: AWS answered about this resource, in this run, under these credentials.
    confirmed: bool
    #: When it answered (ISO-8601), or "" when nothing has asked.
    confirmed_at: str
    #: What it said: True/False, or None when it could not be asked.
    present: bool | None
    #: The credential profile the answer was obtained with ("" = default chain).
    profile: str
    #: The AWS account id that profile resolved to, when one was recorded.
    account_id: str
    #: The rendered "profile (account id)" label, from `cloud_identity`.
    account_label: str
    #: Internal contradictions in the record. Non-empty => nothing is usable.
    findings: tuple[str, ...]
    #: Why `confirmed` is False, in words an operator can act on.
    reason: str

    @property
    def coherent(self) -> bool:
        """Can the record's fields describe a single moment in AWS?"""
        return not self.findings

    @property
    def usable(self) -> bool:
        """May a surface state presence, billing or release from this record?

        The one gate. Confirmed *and* coherent: a confirmation proves the
        resource exists, and coherence is what makes the rest of the block
        describe the resource that was confirmed rather than some mixture of
        that one and an earlier one.
        """
        return self.confirmed and self.coherent

    def fact(self, key: str, default: Any = "") -> Any:
        """The recorded value, but only when it may be stated as a fact.

        `default` otherwise -- so a claim site reads "unknown" rather than a
        remembered value it would go on to assert.
        """
        if not self.usable:
            return default
        value = self.recorded.get(key)
        return default if value is None else value

    def reported(self, key: str, default: Any = "") -> Any:
        """The recorded value, for a row labelled as what the record holds.

        No assertion attached, and deliberately available whatever `usable`
        says: an operator whose only view of a machine is this record needs the
        instance id in it. The caller's job is to say where it came from --
        `provenance` is that sentence.
        """
        value = self.recorded.get(key)
        return default if value is None else value

    @property
    def provenance(self) -> str:
        """One clause naming what the rows around it are, and what asked.

        The heading sentence for any surface rendering this observation. It is
        the reason finding 6 of #4181 existed: `nyxgpt cloud status` said "NOT
        confirmed at AWS in this run -- `nyxgpt cloud status` asks" *while being
        that command*, because the message named the remedy for "nobody asked"
        when the real reason was that boto3 was not installed.
        """
        if self.usable:
            where = f" in {self.account_label}" if self.account_label else ""
            return f"confirmed at AWS{where} at {self.confirmed_at}"
        if self.confirmed and not self.coherent:
            return (
                f"confirmed at AWS at {self.confirmed_at}, but this record contradicts itself "
                "-- its fields were written by different runs about different resources, so "
                "none of them may be read together"
            )
        return f"recorded on this machine; NOT confirmed at AWS -- {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        """The serialized form every status payload and the dashboard read.

        Flat and explicit: `usable` is the flag a UI gates a claim on, and
        `provenance` is the sentence it prints when it cannot make one. Nothing
        here is derived twice -- a second copy of the four branches in
        TypeScript is how D-066 happened.
        """
        return {
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at,
            "present": self.present,
            "coherent": self.coherent,
            "usable": self.usable,
            "findings": list(self.findings),
            "reason": self.reason,
            "profile": self.profile,
            "account_id": self.account_id,
            "account_label": self.account_label,
            "provenance": self.provenance,
        }


def observe(
    recorded: dict[str, Any],
    *,
    present: bool | None = None,
    confirmed_at: str = "",
    profile: str = "",
    account_id: str = "",
    account_label: str = "",
    findings: tuple[str, ...] | list[str] = (),
    reason: str = "",
    expect_profile: str | None = None,
    now: datetime | None = None,
) -> Observation:
    """Build the one observation of `recorded`, deciding `confirmed` here.

    `present` / `confirmed_at` / `profile` are what the last AWS answer about
    this resource recorded. `expect_profile` is the credential profile *this*
    run resolved: when it differs from the one the answer was obtained with,
    the answer is about a resource in another account and is not a confirmation
    here -- the wrong-account read is the mechanism of #4181 finding 1, where
    the default account's `InvalidHostID.NotFound` for a host it does not own
    was taken as "released".

    `reason` is only used when the result is unconfirmed; the three ways that
    happens (never asked, asked and could not get an answer, answered about
    another account) each get their own words, because the operator's next
    action differs for each.
    """
    fresh = is_fresh(confirmed_at, now=now)
    wrong_account = expect_profile is not None and str(expect_profile or "") != str(profile or "")
    confirmed = bool(present) and fresh and not wrong_account

    if confirmed:
        detail = ""
    elif wrong_account:
        asked = account_label or (f"profile {profile!r}" if profile else "the default credentials")
        wanted = expect_profile or "the default credentials"
        detail = (
            f"the last answer came from {asked}, and this run resolved {wanted!r}. An AWS "
            "account answers 'not found' for every resource it does not own, so an answer "
            "from the wrong account cannot tell a released resource from someone else's"
        )
    elif present is False:
        detail = "AWS reported it as gone"
    elif reason:
        detail = reason
    elif confirmed_at and not fresh:
        detail = (
            f"the last answer is from {confirmed_at}, which is older than this run -- it "
            "describes what AWS said then, not now"
        )
    else:
        detail = NOT_ASKED

    return Observation(
        recorded=dict(recorded),
        confirmed=confirmed,
        confirmed_at=str(confirmed_at or ""),
        present=present,
        profile=str(profile or ""),
        account_id=str(account_id or ""),
        account_label=str(account_label or ""),
        findings=tuple(findings),
        reason=detail,
    )
