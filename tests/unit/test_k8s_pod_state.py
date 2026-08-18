"""Unit tests for the shared Pod-state reading (src/nyxgpt/k8s_pod_state.py).

#3832: self_heal.py and ops.py both reduced a Pod to "is the phase Running?",
which cannot tell a Pod pulling its image from one the scheduler refused.
ops.py misreported that; self-heal acted on it, deleting an unschedulable Pod
every 15 seconds forever. These tests pin the distinctions both now share.
"""

from __future__ import annotations

import pytest

from nyxgpt.k8s_pod_state import PodState, classify_pod, classify_pods


def _pod(name="nyxgpt-api-stable-r56wb", *, phase="Running", conditions=None, **rest):
    status = {"phase": phase, "conditions": conditions if conditions is not None else []}
    status.update(rest)
    return {"metadata": {"name": name}, "status": status}


_READY = {"type": "Ready", "status": "True"}
_NOT_READY = {"type": "Ready", "status": "False"}
_UNSCHEDULABLE = {
    "type": "PodScheduled",
    "status": "False",
    "reason": "Unschedulable",
    "message": "0/1 nodes are available: 1 Insufficient memory.",
}


@pytest.mark.unit
def test_running_and_ready_is_healthy():
    state = classify_pod(_pod(conditions=[_READY]))

    assert state.healthy is True
    assert state.deletion_may_recover is False
    assert state.health_label == "ready"


@pytest.mark.unit
def test_running_but_not_ready_is_the_only_state_deletion_repairs():
    state = classify_pod(_pod(conditions=[_NOT_READY]))

    assert state.healthy is False
    assert state.deletion_may_recover is True
    assert state.health_label == "not-ready"


@pytest.mark.unit
def test_pending_with_podscheduled_false_is_unschedulable():
    """The #3832 shape: `FailedScheduling: Insufficient memory`, forever."""
    state = classify_pod(_pod(phase="Pending", conditions=[_NOT_READY, _UNSCHEDULABLE]))

    assert state.unschedulable is True
    assert state.starting is False
    assert state.deletion_may_recover is False
    assert state.reason == "Unschedulable"
    assert "Insufficient memory" in state.detail
    assert "Insufficient memory" in state.summary()
    assert state.health_label == "unschedulable"


@pytest.mark.unit
def test_pending_while_pulling_its_image_is_starting_not_unschedulable():
    """Transient and self-resolving -- the other half of the conflation."""
    state = classify_pod(
        _pod(
            phase="Pending",
            conditions=[_NOT_READY, {"type": "PodScheduled", "status": "True"}],
            containerStatuses=[{"state": {"waiting": {"reason": "ContainerCreating"}}}],
        )
    )

    assert state.unschedulable is False
    assert state.starting is True
    assert state.deletion_may_recover is False
    assert state.reason == "ContainerCreating"
    assert state.health_label == "starting"


@pytest.mark.unit
def test_a_brand_new_pending_pod_without_the_condition_is_not_called_unschedulable():
    """Absence of the answer is not a negative answer (#3812's rule)."""
    state = classify_pod(_pod(phase="Pending", conditions=[_NOT_READY]))

    assert state.unschedulable is False
    assert state.starting is True


@pytest.mark.unit
def test_scheduling_gated_counts_as_not_scheduled():
    state = classify_pod(
        _pod(
            phase="Pending",
            conditions=[
                _NOT_READY,
                {"type": "PodScheduled", "status": "False", "reason": "SchedulingGated"},
            ],
        )
    )

    assert state.unschedulable is True
    assert state.reason == "SchedulingGated"


@pytest.mark.unit
def test_init_container_reason_wins_over_the_app_container():
    state = classify_pod(
        _pod(
            phase="Pending",
            conditions=[_NOT_READY, {"type": "PodScheduled", "status": "True"}],
            initContainerStatuses=[{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
            containerStatuses=[{"state": {"waiting": {"reason": "PodInitializing"}}}],
        )
    )

    assert state.reason == "ImagePullBackOff"


@pytest.mark.unit
def test_workload_is_the_owner_so_it_survives_the_pods_own_recreation():
    pod = _pod(conditions=[_READY])
    pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "nyxgpt-api-stable-7d9c8f"}
    ]

    assert classify_pod(pod).workload == "replicaset/nyxgpt-api-stable-7d9c8f"


@pytest.mark.unit
def test_workload_falls_back_to_the_pod_name_when_nothing_owns_it():
    assert classify_pod(_pod("solo-pod", conditions=[_READY])).workload == "solo-pod"


@pytest.mark.unit
@pytest.mark.parametrize(
    "pod",
    [
        {},
        {"metadata": None, "status": None},
        {"status": {"phase": 7, "conditions": "not-a-list"}},
        {"status": {"conditions": [None, {"type": None}]}},
        {"status": {"containerStatuses": [{"state": {"waiting": None}}]}},
    ],
)
def test_malformed_pod_objects_read_as_absent_never_as_deletable(pod):
    """A Pod this cannot make sense of must fail towards *not* acting on it."""
    state = classify_pod(pod)

    assert isinstance(state, PodState)
    assert state.running is False
    assert state.deletion_may_recover is False
    assert state.healthy is False


@pytest.mark.unit
def test_long_scheduler_messages_are_collapsed_to_one_bounded_line():
    """These strings land in the SRE dashboard's component list, not a log pane."""
    long_message = "0/9 nodes are available:\n" + " ".join(
        f"node-{i} too small." for i in range(60)
    )
    state = classify_pod(
        _pod(
            phase="Pending",
            conditions=[
                _NOT_READY,
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": long_message,
                },
            ],
        )
    )

    assert "\n" not in state.detail
    assert len(state.detail) <= 240
    assert state.detail.endswith("...")


@pytest.mark.unit
def test_classify_pods_reads_a_list_body_and_ignores_junk():
    payload = {"items": [_pod("a", conditions=[_READY]), "not-a-pod", None]}

    states = classify_pods(payload)

    assert [s.name for s in states] == ["a"]
    assert classify_pods({}) == []
    assert classify_pods({"items": "nope"}) == []
