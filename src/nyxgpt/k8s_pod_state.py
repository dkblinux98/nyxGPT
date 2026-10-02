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

Two further shared readings live at the bottom of this module, and they answer
the same question from different ends: *which* Pods are the deployment's state
at all.

- A Pod owned by a ReplicaSet scaled to zero is the residue of a finished
  rollout, and every reader of a Pod list has to drop it (#3956) -- `ops.py`
  failed an install on one, and self-heal rendered the same Pod as a
  permanently Failed component, because that rule had one copy instead of none.
- A terminal Pod whose own workload already has a ready Pod of a newer revision
  has been rolled past (#3990). This one needs no second question of the
  cluster, so it still answers for the populations the first cannot see.

Neither subsumes the other; see the comment above `POD_TERMINAL_PHASES`.

No nyxgpt imports: `ops.py` already imports `self_heal.py`, so anything the
two share has to sit below both.
"""

from __future__ import annotations

import json
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# `.status.conditions[type=PodScheduled].reason` values the scheduler sets
# when it has not placed a Pod. Recorded for the operator-facing message only
# -- the decision keys off the condition being False, so a reason this set has
# never heard of is still read as "not scheduled" rather than as schedulable.
UNSCHEDULABLE_REASONS = frozenset({"Unschedulable", "SchedulingGated"})

# Phases whose Pods have a reading of their own already: `Pending`/`Running`
# are answered by the waiting-container and readiness checks, and `Succeeded`
# is its own answer. `_terminated_reason` is consulted only outside this set --
# `Failed`, `Unknown`, and whatever a future Kubernetes adds.
_PHASES_WITH_A_LIVE_READING = frozenset({"Pending", "Running", "Succeeded"})

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


def _terminated_reason(status: Mapping[str, Any]) -> tuple[str, str]:
    """Why a Pod that is no longer running stopped: `(reason, detail)` (#3956).

    `_waiting_reason` covers every Pod that has not started yet; this covers the
    other end, which had no reading at all. A `Failed` Pod reported as the bare
    word `Failed` -- no reason, no container state, no exit code -- is what the
    owner's 2026-08-26 acceptance round had to SSH in and run kubectl by hand to
    diagnose.

    The Pod's own `.status.reason`/`.message` first (`Evicted`, `NodeShutdown`,
    `DeadlineExceeded` -- set by the component that ended the Pod, and the whole
    answer when no container ever ran), then the first terminated container's
    reason and exit code, which is where `OOMKilled` and `Error` live.
    """
    reason = status.get("reason")
    if isinstance(reason, str) and reason:
        return reason, _one_line(status.get("message"))
    for key in ("initContainerStatuses", "containerStatuses"):
        entries = status.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            state = entry.get("state")
            terminated = state.get("terminated") if isinstance(state, Mapping) else None
            if not isinstance(terminated, Mapping):
                continue
            container_reason = terminated.get("reason")
            if not (isinstance(container_reason, str) and container_reason):
                continue
            exit_code = terminated.get("exitCode")
            detail = _one_line(terminated.get("message"))
            if isinstance(exit_code, int):
                prefix = f"container {entry.get('name') or '?'} exited {exit_code}"
                detail = f"{prefix}: {detail}" if detail else prefix
            return container_reason, detail
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
        if not reason and phase not in _PHASES_WITH_A_LIVE_READING:
            # A phase with nothing waiting and nothing running has already
            # stopped, so ask the other end (#3956). Restricted to those
            # phases on purpose: a `Running` Pod whose init container
            # terminated `Completed` is a Pod that started normally, and
            # reporting "Completed" as the reason it is not ready would be a
            # worse answer than the generic one its caller already prints.
            reason, detail = _terminated_reason(status)

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


# --- Pods no live ReplicaSet owns (#3956) ----------------------------------
#
# `nyxgpt ops install --kubernetes` applies `k8s/` (whose ConfigMap carries the
# placeholder error-tracking DSN), waits for the stack, then provisions
# GlitchTip -- which writes the real DSN and rolls api/web onto a new pod
# template. The superseded ReplicaSet is scaled to zero and its Pod is left
# behind terminated, and the owner's 2026-08-26 acceptance round watched that
# Pod fail the whole install (`pod nyxgpt-web-stable-77c7d9c6f4-gz62g: Failed`)
# three lines above `nyxgpt-web-stable 1/1`, which said the opposite.
#
# The rule, stated once for every reader of a Pod list: a Pod whose ReplicaSet
# has zero desired replicas is the residue of a finished rollout, not the
# deployment's state. It lives here rather than in either caller because
# `ops.py` imports `self_heal.py`, so nothing they share can sit in either
# (#3832's placement rule) -- and because the alternative, a copy per reader,
# is exactly how `ops._k8s_pod_states` came to drop the corpse while
# `self_heal._list_kubernetes_component_status` still rendered it as a
# permanently Failed component of a healthy deployment.
#
# Deliberately NOT "ignore Pods whose phase is Failed": a Failed Pod of the
# *current* ReplicaSet is a real failure, and phase-filtering would hide it
# while leaving the actual defect -- consulting Pods no live controller owns --
# in place for every other terminal state to walk back through.
#
# The kubectl read is each caller's own (they have different `_run` wrappers,
# bounds and namespaces), but the argv and the parse are shared so the two
# cannot end up asking the cluster different questions.
RETIRED_REPLICASET_JSONPATH = "jsonpath={range .items[*]}{.metadata.name}={.spec.replicas};{end}"


def retired_replicaset_argv(namespace: str) -> list[str]:
    """The `kubectl` read whose stdout `parse_retired_replicasets` expects."""
    return ["kubectl", "-n", namespace, "get", "rs", "-o", RETIRED_REPLICASET_JSONPATH]


def parse_retired_replicasets(stdout: str) -> frozenset[str]:
    """ReplicaSet names with zero desired replicas, from that read's stdout.

    Tolerant like everything else here: an entry this cannot parse is simply
    not in the set, which keeps the Pod it owns in the report.
    """
    retired = set()
    for entry in (e for e in (stdout or "").split(";") if e):
        name, _, replicas = entry.partition("=")
        if name and replicas.strip() == "0":
            retired.add(name)
    return frozenset(retired)


def pod_owner_replicaset(pod: Mapping[str, Any]) -> str:
    """The name of the ReplicaSet that owns `pod`, or "" if none does.

    "" for a StatefulSet Pod (Cassandra, Ollama), a bare Pod, or a Pod whose
    ownerReferences are unreadable -- none of which this rule has anything to
    say about, so they are always kept.
    """
    metadata = pod.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    owners = metadata.get("ownerReferences")
    if not isinstance(owners, list):
        return ""
    for owner in owners:
        if isinstance(owner, Mapping) and str(owner.get("kind") or "").lower() == "replicaset":
            return str(owner.get("name") or "")
    return ""


def pod_is_retired(pod: Mapping[str, Any], retired: Container[str]) -> bool:
    """Whether `pod` belongs to a ReplicaSet in `retired` and may be dropped.

    A Pod may only ever be dropped on *positive* evidence that its owner is
    finished, so an unreadable ReplicaSet read (an empty `retired`) removes
    nothing and the report is the one it has always been.
    """
    owner = pod_owner_replicaset(pod)
    return bool(owner) and owner in retired


# --- Pods the rollout has already replaced (#3990) --------------------------
#
# The second of the two rules that keep a finished rollout's residue out of a
# verdict, and NOT a substitute for the first. `pod_is_retired` asks the
# ReplicaSets which of them have zero desired replicas, which costs an extra
# `kubectl` and says nothing about a Pod no ReplicaSet owns. This one asks only
# the Pods already in hand, so it still answers for the populations that one
# cannot see: a StatefulSet's rolled Pod, and any residue at all on a run where
# the ReplicaSet read timed out and the retired set came back empty -- which on
# a node sized for one rollout's surge is exactly the run that leaves residue
# behind.
#
# It lives here, beside `pod_is_retired`, for the reason that rule learned the
# hard way: `ops.py`'s first cut of this reading was `ops.py`'s alone, so the
# Infrastructure page could badge a Pod `SUPERSEDED` while the Self-Heal page
# rendered the same Pod as a permanently `Failed`, unhealable component of a
# deployment whose Deployments were both `1/1`. Two dashboards giving two
# verdicts about one Pod is the D-022/D-052 class of defect, and one copy of
# the reading is what prevents it.
#
# What each caller does with the reading is still its own: `ops.py` RE-LABELS
# the Pod (printed, not counted -- an operator looking for why a Pod died needs
# to see it is there), self-heal DROPS it from the component list (the same
# thing it does with a retired Pod, because there is nothing for the watchdog
# to heal and no component the operator must act on).

# Phases no container comes back from: the Pod is only still in the API because
# nothing has collected it yet. `Succeeded` is deliberately absent -- a one-shot
# Pod that completed is a success, not residue to be explained away.
POD_TERMINAL_PHASES = frozenset({"Failed", "Unknown"})

# Labels that identify the *revision* that minted a Pod rather than the
# workload it belongs to. Stripping them is what lets two Pods of one Deployment
# be recognised as the same workload across a rollout; comparing them is what
# tells "replaced by a newer revision" apart from "one replica of the current
# revision died", which is a real failure.
REVISION_LABELS = ("pod-template-hash", "controller-revision-hash")

# Also stripped from the identity: the label a StatefulSet stamps with the Pod's
# own name. See `pod_workload_identity` for what that costs and why it is paid.
_REPLICA_IDENTITY_LABELS = ("statefulset.kubernetes.io/pod-name",)


def pod_name(pod: Mapping[str, Any]) -> str:
    """The Pod's `metadata.name`, or "" when it is missing or not a string."""
    metadata = pod.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    name = metadata.get("name")
    return name if isinstance(name, str) else ""


def _pod_labels(pod: Mapping[str, Any]) -> dict[str, str]:
    """`metadata.labels` as a `str -> str` dict, skipping anything malformed."""
    metadata = pod.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw = metadata.get("labels")
    if not isinstance(raw, Mapping):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def pod_workload_identity(pod: Mapping[str, Any]) -> str:
    """The workload a Pod belongs to, independent of which revision minted it.

    The Pod's labels with the revision labels removed: two Pods of the same
    Deployment agree on this across a rollout, and two Pods of different
    workloads never do (every workload in `k8s/` carries an `app` label). "" for
    a Pod carrying nothing but its revision -- a bare Pod nothing owns, which
    has no workload to be superseded by -- and "" is never treated as a match.

    `statefulset.kubernetes.io/pod-name` is stripped too, and that is the one
    imprecision in this reading: it makes every replica of a StatefulSet share
    one identity, so mid-rollout a terminal `cassandra-0` of the old revision
    can be excused by a Ready `cassandra-1` of the new one. Keying the identity
    on the ordinal instead would be stricter and would also retire the rule for
    StatefulSets entirely -- a StatefulSet recreates a replica under the *same*
    Pod name, so no two Pods ever share that label and no replacement could ever
    be found. The looser identity is chosen because the residue it exists to
    explain is real, the Pod is still printed either way, and that controller
    replaces its own terminal replica under the same name rather than leaving it
    indefinitely.
    """
    labels = _pod_labels(pod)
    for label in REVISION_LABELS + _REPLICA_IDENTITY_LABELS:
        labels.pop(label, None)
    return json.dumps(sorted(labels.items())) if labels else ""


def pod_revision(pod: Mapping[str, Any]) -> str:
    """The revision hash that minted a Pod, or "" when its controller stamps none."""
    labels = _pod_labels(pod)
    for label in REVISION_LABELS:
        value = labels.get(label) or ""
        if value:
            return value
    return ""


def pod_is_terminal(pod: Mapping[str, Any]) -> bool:
    """Whether the Pod's phase is one no container comes back from."""
    status = pod.get("status")
    status = status if isinstance(status, Mapping) else {}
    phase = status.get("phase")
    return (phase if isinstance(phase, str) else "") in POD_TERMINAL_PHASES


def superseded_pods(pods: Sequence[Mapping[str, Any]], ready: Sequence[bool]) -> dict[int, str]:
    """Which Pods their own workload has already rolled past: `{index: replacement}`.

    `ready[i]` is the caller's own verdict on whether `pods[i]` is serving --
    `ops.py` counts a `Succeeded` one-shot Pod, self-heal does not -- so the
    policy stays with the caller and only the reading is shared. Keyed by index
    rather than by name because the caller already holds its own per-Pod state
    in the same order, and because two Pods with an unreadable name would
    otherwise collide on "".

    A Pod is superseded when all three hold: its phase is terminal
    (`POD_TERMINAL_PHASES`), it carries a revision hash, and another Pod of the
    same workload (`pod_workload_identity`) from a DIFFERENT revision is ready.
    The last clause is what keeps this from swallowing real failures -- one
    replica of the *current* revision dying still reports as itself, because its
    replacement carries the same hash. A workload with no ready Pod at all is
    the whole workload being down, and nothing here excuses it.
    """
    if len(pods) != len(ready):  # pragma: no cover - caller contract
        raise ValueError("superseded_pods: `ready` must carry one verdict per Pod")

    ready_by_identity: dict[str, list[tuple[str, str]]] = {}
    for pod, is_ready in zip(pods, ready, strict=True):
        if is_ready:
            ready_by_identity.setdefault(pod_workload_identity(pod), []).append(
                (pod_revision(pod), pod_name(pod))
            )

    superseded: dict[int, str] = {}
    for index, pod in enumerate(pods):
        if not pod_is_terminal(pod):
            continue
        revision = pod_revision(pod)
        identity = pod_workload_identity(pod)
        if not (revision and identity):
            continue
        replacement = next(
            (name for rev, name in ready_by_identity.get(identity, ()) if rev and rev != revision),
            "",
        )
        if replacement:
            superseded[index] = replacement
    return superseded


__all__ = [
    "POD_TERMINAL_PHASES",
    "RETIRED_REPLICASET_JSONPATH",
    "REVISION_LABELS",
    "UNSCHEDULABLE_REASONS",
    "PodState",
    "classify_pod",
    "classify_pods",
    "parse_retired_replicasets",
    "pod_is_retired",
    "pod_is_terminal",
    "pod_name",
    "pod_owner_replicaset",
    "pod_revision",
    "pod_workload_identity",
    "retired_replicaset_argv",
    "superseded_pods",
]
