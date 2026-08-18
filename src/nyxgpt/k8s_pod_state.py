"""One shared reading of what a Kubernetes Pod's state means (#3832).

`self_heal.py` and `ops.py` both look at Pods, and both used to reduce them to
the same two-valued question -- "is the phase `Running`?" -- which conflates a
Pod that is still pulling its image (transient, resolves itself) with one the
scheduler could not place at all (permanent: no amount of waiting, restarting
or recreating fixes it). `ops.py` only *reported* that conflation; self-heal
acted on it, deleting an unschedulable Pod every 15 seconds forever -- #3832
observed seven Pods in 4.5 minutes, each `FailedScheduling: Insufficient
memory`, each deletion resetting the Pod's age so no operator ever saw a Pod
stuck long enough to diagnose. The loop erased its own evidence.

So the reading lives here, once, and both callers use it -- `self_heal.py`
directly, `ops._classify_k8s_pod` to build its own three-state install
vocabulary on top (#3827). What each does *with* the reading is still its
own: a `CrashLoopBackOff` Pod fails an install and is healable by the
watchdog. Sharing the reading is what stops them disagreeing about the facts;
sharing the policy was never the goal. The distinctions that matter:

- **healthy** -- `Running` and `Ready`. Nothing to do.
- **unschedulable** -- `Pending` with `PodScheduled=False`: the scheduler has
  said it cannot place this Pod (`Unschedulable` -- insufficient memory/CPU,
  no matching node -- or `SchedulingGated`). Deleting it cannot create
  capacity; the ReplicaSet recreates it and the new Pod is Pending for the
  identical reason. Report the scheduler's own message and take no action.
- **starting** -- `Pending` without that condition failing: still being
  scheduled, pulling an image, running init containers. It converges on its
  own, and acting on it only restarts the clock.
- **deletion may recover** -- `Running` but not `Ready`. The only state in
  which deleting a Pod is a repair rather than churn: a container came up and
  is not serving, and a fresh Pod plausibly does better.

`Failed`/`Succeeded`/`Unknown` are reported as they are and are never deleted
from here -- a ReplicaSet replaces its own failed Pods, and a Pod on a lost
node is the node controller's business.

`workload` is the stable identity a Pod keeps across recreation: its owner
(the ReplicaSet/StatefulSet), not its own name, which changes on every
recreate. Anything that budgets repair attempts has to count against that --
counting against the Pod name is why #3832's per-service restart cap never
fired even once across seven deletions.

No nyxgpt imports: `ops.py` already imports `self_heal.py`, so anything the
two share has to sit below both.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# `.status.conditions[type=PodScheduled].reason` values the scheduler sets
# when it has not placed a Pod. Recorded for the operator-facing message only
# -- the decision keys off the condition being False, so a reason this set has
# never heard of is still read as "not scheduled" rather than as schedulable.
UNSCHEDULABLE_REASONS = frozenset({"Unschedulable", "SchedulingGated"})

# Longest scheduler/kubelet message rendered into a status line. These strings
# reach the SRE dashboard's component list, not a log pane.
_MAX_DETAIL_CHARS = 240


@dataclass(frozen=True)
class PodState:
    """What one Pod's `.status` means, in the terms callers actually decide on.

    `reason`/`detail` carry the cluster's own words for why the Pod is not
    serving -- the scheduler's `FailedScheduling` message, or the waiting
    container's `ImagePullBackOff`/`CrashLoopBackOff` -- so the operator reads
    the cause instead of inferring it from a phase.
    """

    name: str
    phase: str
    ready: bool
    unschedulable: bool = False
    reason: str = ""
    detail: str = ""
    workload: str = ""

    @property
    def healthy(self) -> bool:
        """`Running` and `Ready` -- the only state that needs nothing."""
        return self.phase == "Running" and self.ready

    @property
    def running(self) -> bool:
        """Phase is `Running`, whether or not the Pod is Ready."""
        return self.phase == "Running"

    @property
    def pending(self) -> bool:
        """Phase is `Pending` -- scheduled-but-starting, or not scheduled at all."""
        return self.phase == "Pending"

    @property
    def starting(self) -> bool:
        """`Pending` and converging on its own (being scheduled, pulling, initializing)."""
        return self.pending and not self.unschedulable

    @property
    def deletion_may_recover(self) -> bool:
        """True only for `Running`-but-not-`Ready`, where deleting is a repair.

        Every other unhealthy state is either self-resolving (`starting`),
        beyond deletion's reach (`unschedulable`), or already the controller's
        business (`Failed`/`Unknown`). This is the predicate #3832's acceptance
        criteria name: the delete remedy is restricted to Running-but-not-ready.
        """
        return self.running and not self.ready

    @property
    def health_label(self) -> str:
        """Short health word for a status row (`ready`/`starting`/`unschedulable`/`not-ready`)."""
        if self.healthy:
            return "ready"
        if self.unschedulable:
            return "unschedulable"
        if self.starting:
            return "starting"
        return "not-ready"

    def summary(self) -> str:
        """One operator-facing line: phase, the cluster's reason, its message."""
        text = self.phase or "unknown phase"
        if self.reason:
            text = f"{text} ({self.reason})"
        if self.detail:
            text = f"{text}: {self.detail}"
        return text


def _one_line(text: Any) -> str:
    """Collapse a multi-line cluster message to one trimmed, bounded line."""
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_DETAIL_CHARS:
        collapsed = collapsed[: _MAX_DETAIL_CHARS - 3] + "..."
    return collapsed


def _conditions(status: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Index `.status.conditions` by type, ignoring anything malformed."""
    indexed: dict[str, Mapping[str, Any]] = {}
    raw = status.get("conditions")
    if not isinstance(raw, list):
        return indexed
    for condition in raw:
        if isinstance(condition, Mapping):
            key = condition.get("type")
            if isinstance(key, str):
                indexed[key] = condition
    return indexed


def _waiting_reason(status: Mapping[str, Any]) -> tuple[str, str]:
    """First waiting container's `reason`/`message`, init containers first.

    This is where `ImagePullBackOff`, `ErrImagePull`, `ContainerCreating` and
    `CrashLoopBackOff` live. It answers "why is this Pod not serving?" for
    every case the scheduler condition does not.
    """
    for key in ("initContainerStatuses", "containerStatuses"):
        entries = status.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            state = entry.get("state")
            waiting = state.get("waiting") if isinstance(state, Mapping) else None
            if isinstance(waiting, Mapping):
                reason = waiting.get("reason")
                if isinstance(reason, str) and reason:
                    return reason, _one_line(waiting.get("message"))
    return "", ""


def _workload_key(metadata: Mapping[str, Any], fallback: str) -> str:
    """The Pod's owner (`<kind>/<name>`), or its own name when it has none.

    A Pod's name dies with the Pod; its owner survives the recreate. Repair
    budgets have to be counted against something that does (#3832).
    """
    owners = metadata.get("ownerReferences")
    if isinstance(owners, list):
        for owner in owners:
            if not isinstance(owner, Mapping):
                continue
            name = owner.get("name")
            if isinstance(name, str) and name:
                kind = owner.get("kind")
                kind_text = kind.lower() if isinstance(kind, str) and kind else "owner"
                return f"{kind_text}/{name}"
    return fallback


def classify_pod(pod: Mapping[str, Any]) -> PodState:
    """Read one Pod object (as `kubectl get pod -o json` returns it) into a `PodState`.

    Tolerant by construction: a field that is missing or the wrong type reads
    as absent, never as an exception. A Pod this cannot make sense of comes
    back with an empty phase, which is not `Running` and therefore never
    deletable -- the safe direction.
    """
    metadata = pod.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    status = pod.get("status")
    status = status if isinstance(status, Mapping) else {}

    name = metadata.get("name")
    name = name if isinstance(name, str) else ""
    phase = status.get("phase")
    phase = phase if isinstance(phase, str) else ""

    conditions = _conditions(status)
    ready = str((conditions.get("Ready") or {}).get("status", "")) == "True"

    scheduled = conditions.get("PodScheduled") or {}
    scheduled_status = scheduled.get("status")
    # A `PodScheduled` condition that is present and not `True` means the
    # scheduler has not placed this Pod -- `False` (`Unschedulable`), and
    # equally `SchedulingGated` or any status a future Kubernetes reports, so
    # an unrecognised answer errs toward "not scheduled" rather than silently
    # toward "fine". A Pod so new that the condition is not there *at all* is
    # simply starting: absence of the answer is not a negative answer, the
    # same rule #3812 established for the Compose probe.
    unschedulable = (
        phase == "Pending" and isinstance(scheduled_status, str) and scheduled_status != "True"
    )

    if unschedulable:
        reason = scheduled.get("reason")
        reason = reason if isinstance(reason, str) else ""
        detail = _one_line(scheduled.get("message"))
    else:
        reason, detail = _waiting_reason(status)

    return PodState(
        name=name,
        phase=phase,
        ready=ready,
        unschedulable=unschedulable,
        reason=reason,
        detail=detail,
        workload=_workload_key(metadata, name),
    )


def classify_pods(payload: Mapping[str, Any]) -> list[PodState]:
    """Read a `kubectl get pods -o json` list body into one `PodState` per Pod."""
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [classify_pod(item) for item in items if isinstance(item, Mapping)]


__all__ = [
    "UNSCHEDULABLE_REASONS",
    "PodState",
    "classify_pod",
    "classify_pods",
]
