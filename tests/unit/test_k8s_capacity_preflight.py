"""Node-capacity sizing and preflight for the local Kubernetes install (#3825).

Two halves, matching the two halves of the defect:

* the MANIFESTS -- `nyxgpt ops install --kubernetes --local` provisions a
  single-node kind cluster and then applied a stack whose memory *requests*
  exceeded it, so prometheus (and later the canary Pod) sat Pending forever
  with `Insufficient memory`. These tests do the arithmetic the manifests
  imply and assert it fits the node an operator actually has, with room for
  a canary rollout, and that nothing is requested below its observed
  steady state (a request under real usage makes that Pod the kubelet's
  first eviction candidate);
* the PREFLIGHT -- the sizing above is right for an 8Gi VM and wrong for a
  4Gi one, so the install measures the node before applying anything and
  refuses instead of leaving Pods Pending.

Both resources are covered, not just the memory the issue named: with the
memory right-sized, the canary Pod on a 4-core VM failed with `Insufficient
cpu` instead -- the same defect one resource over.

The arithmetic here mirrors `ops._workload_resource_requests`; it is spelled
out again from the YAML rather than reusing it, so a bug in the production
summation cannot make these assertions vacuously pass.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from nyxgpt import ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "k8s"

MIB = 1024**2

# The node `nyxgpt ops install --kubernetes --local` gets on a stock Docker
# Desktop: an 8GiB VM reports 7936Mi allocatable once the kubelet's reserved
# slice is taken out (measured on the acceptance run this issue was filed
# from). This is the number the stack has to fit inside, not 8192.
DOCKER_DESKTOP_ALLOCATABLE_MI = 7936

# kube-system on a kind node (etcd, two CoreDNS replicas, kindnet, ...)
# reserves this much of that allocatable pool before nyxGPT gets any. Rounded
# up from the ~290Mi observed so the headroom assertions stay honest.
KUBE_SYSTEM_RESERVED_MI = 320


def _manifest_docs(directory: Path) -> list[dict]:
    docs: list[dict] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        docs += [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
    return docs


def _stack_docs() -> list[dict]:
    """Every workload a default `install --kubernetes --local` applies."""
    return _manifest_docs(K8S_DIR) + _manifest_docs(K8S_DIR / "observability")


def _mem_mi(quantity: str) -> int:
    parsed = ops._parse_k8s_quantity(quantity)
    assert parsed is not None, f"unparseable memory quantity in a manifest: {quantity!r}"
    return parsed // MIB


def _pod_request_mi(doc: dict) -> int:
    """Effective per-Pod memory request: max(sum of containers, largest init)."""
    spec = doc["spec"]["template"]["spec"]
    containers = sum(
        _mem_mi(c["resources"]["requests"]["memory"])
        for c in spec.get("containers", [])
        if c.get("resources", {}).get("requests", {}).get("memory")
    )
    inits = [
        _mem_mi(c["resources"]["requests"]["memory"])
        for c in spec.get("initContainers", [])
        if c.get("resources", {}).get("requests", {}).get("memory")
    ]
    return max([containers, *inits])


def _workloads() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for doc in _stack_docs()
        if doc.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")
    }


def _totals_mi() -> tuple[int, int]:
    """`(scheduled, standby)` MiB the shipped manifests reserve on one node."""
    scheduled = 0
    standby = 0
    for doc in _workloads().values():
        per_pod = _pod_request_mi(doc)
        replicas = 1 if doc["kind"] == "DaemonSet" else doc["spec"].get("replicas", 1)
        if replicas == 0:
            standby += per_pod
        else:
            scheduled += per_pod * replicas
    return scheduled, standby


# --- the manifests fit the node -------------------------------------------


def test_default_install_fits_a_default_docker_desktop_node() -> None:
    """The whole default stack -- observability included -- must schedule.

    #3825: the applied stack requested 8162Mi against 7936Mi allocatable, so
    `install --kubernetes --local` reported success with prometheus Pending.
    """
    scheduled, _ = _totals_mi()
    budget = DOCKER_DESKTOP_ALLOCATABLE_MI - KUBE_SYSTEM_RESERVED_MI
    assert scheduled <= budget, (
        f"the default k8s stack requests {scheduled}Mi but only {budget}Mi is available on a "
        f"stock 8GiB Docker Desktop node -- some Pod will sit Pending (#3825)"
    )


def test_a_canary_rollout_still_has_somewhere_to_land() -> None:
    """`nyxgpt canary start` scales a parked Deployment to 1 on the same node.

    The acceptance report saw this as "canary is broken": the rollout's Pod
    was Pending with Insufficient memory, because the steady-state stack had
    already reserved 99% of the node.
    """
    scheduled, standby = _totals_mi()
    assert standby > 0, "no parked canary workload found -- this test is measuring nothing"
    budget = DOCKER_DESKTOP_ALLOCATABLE_MI - KUBE_SYSTEM_RESERVED_MI
    assert scheduled + standby <= budget, (
        f"steady state {scheduled}Mi + a canary rollout {standby}Mi exceeds the {budget}Mi "
        "available -- the canary Pod would be Pending"
    )


def test_the_pre_fix_sizing_would_still_be_caught() -> None:
    """Fault injection for the two tests above.

    They only mean something if the numbers they check could fail. Restore
    the requests #3825 was filed against and assert the same budget rejects
    them -- otherwise a future headroom change could quietly make the
    assertions unfalsifiable.
    """
    pre_fix = {"nyxgpt-api-stable": 512, "nyxgpt-web-stable": 256, "glitchtip": 512}
    scheduled, _ = _totals_mi()
    for name, old_request in pre_fix.items():
        doc = _workloads()[name]
        replicas = doc["spec"].get("replicas", 1)
        scheduled += (old_request - _pod_request_mi(doc)) * replicas
    budget = DOCKER_DESKTOP_ALLOCATABLE_MI - KUBE_SYSTEM_RESERVED_MI
    assert scheduled > budget, (
        "the pre-#3825 requests now fit the budget, so these tests no longer prove the "
        "oversubscription is fixed"
    )


@pytest.mark.parametrize(
    ("workload", "observed_mi"),
    [
        # Measured on the acceptance run (issue #3825's table). A request
        # below actual usage is not a saving: the Pod runs over its
        # reservation and is evicted first when the node comes under
        # pressure.
        ("cassandra", 1675),
        ("grafana", 334),
        ("ollama", 893),
    ],
)
def test_requests_cover_observed_steady_state(workload: str, observed_mi: int) -> None:
    doc = _workloads()[workload]
    assert _pod_request_mi(doc) >= observed_mi, (
        f"{workload} requests less than the {observed_mi}Mi it actually holds -- it would run "
        "above its reservation and be evicted first (#3825)"
    )


def test_every_request_stays_under_its_limit() -> None:
    """A request above the limit is rejected by the API server outright."""
    for name, doc in _workloads().items():
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            resources = container.get("resources", {})
            request = resources.get("requests", {}).get("memory")
            limit = resources.get("limits", {}).get("memory")
            if request and limit:
                assert _mem_mi(request) <= _mem_mi(limit), f"{name}: request exceeds limit"


@pytest.mark.parametrize(
    ("stable", "canary"),
    [
        ("nyxgpt-api-stable", "nyxgpt-api-canary"),
        ("nyxgpt-web-stable", "nyxgpt-web-canary"),
    ],
)
def test_canary_requests_track_their_stable_pool(stable: str, canary: str) -> None:
    """A canary Pod that reserves more than the pool it shadows cannot land."""
    workloads = _workloads()
    assert _pod_request_mi(workloads[canary]) == _pod_request_mi(workloads[stable])


def test_api_keeps_its_burst_ceiling() -> None:
    """Right-sizing is a REQUEST change; the limit must not move with it.

    Requests reserve at schedule time, limits contain the peak -- the whole
    premise of the fix is that the api can still burst to 1Gi under RAG or
    concurrent chat.
    """
    api = _workloads()["nyxgpt-api-stable"]
    limits = api["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert _mem_mi(limits["memory"]) == 1024


# --- the manifests fit the node's CPUs too ---------------------------------

# A stock Docker Desktop VM gets 4 CPUs, and kube-system on a kind node holds
# ~950m of that before nyxGPT asks for any.
DOCKER_DESKTOP_ALLOCATABLE_CPU_M = 4000
KUBE_SYSTEM_RESERVED_CPU_M = 950


def _cpu_m(quantity: str) -> int:
    parsed = ops._parse_k8s_cpu(quantity)
    assert parsed is not None, f"unparseable cpu quantity in a manifest: {quantity!r}"
    return parsed


def _pod_request_cpu_m(doc: dict) -> int:
    spec = doc["spec"]["template"]["spec"]
    containers = sum(
        _cpu_m(c["resources"]["requests"]["cpu"])
        for c in spec.get("containers", [])
        if c.get("resources", {}).get("requests", {}).get("cpu")
    )
    inits = [
        _cpu_m(c["resources"]["requests"]["cpu"])
        for c in spec.get("initContainers", [])
        if c.get("resources", {}).get("requests", {}).get("cpu")
    ]
    return max([containers, *inits])


def _totals_cpu_m() -> tuple[int, int]:
    scheduled = 0
    standby = 0
    for doc in _workloads().values():
        per_pod = _pod_request_cpu_m(doc)
        replicas = 1 if doc["kind"] == "DaemonSet" else doc["spec"].get("replicas", 1)
        if replicas == 0:
            standby += per_pod
        else:
            scheduled += per_pod * replicas
    return scheduled, standby


def test_the_default_stack_and_a_canary_fit_four_cpus() -> None:
    """Memory was the reported symptom; CPU was the next wall behind it.

    With the memory right-sized, `nyxgpt-api-canary` still would not schedule
    on a 4-core node -- `0/1 nodes are available: 1 Insufficient cpu`. Fixing
    only the resource the issue named would have left the canary broken and
    the next session diagnosing it from scratch.
    """
    scheduled, standby = _totals_cpu_m()
    budget = DOCKER_DESKTOP_ALLOCATABLE_CPU_M - KUBE_SYSTEM_RESERVED_CPU_M
    assert scheduled + standby <= budget, (
        f"steady state {scheduled}m + a canary rollout {standby}m exceeds the {budget}m a "
        "4-CPU Docker Desktop VM leaves -- the canary Pod would be Pending on cpu"
    )


def test_the_pre_fix_cpu_sizing_would_still_be_caught() -> None:
    """Fault injection for the test above, in the cpu dimension."""
    pre_fix = {"nyxgpt-api-stable": 250, "nyxgpt-web-stable": 100}
    scheduled, standby = _totals_cpu_m()
    for name, old_request in pre_fix.items():
        doc = _workloads()[name]
        scheduled += (old_request - _pod_request_cpu_m(doc)) * doc["spec"].get("replicas", 1)
    # The canary pair tracked stable, so its request moved with it.
    standby += (250 - _pod_request_cpu_m(_workloads()["nyxgpt-api-canary"])) + (
        100 - _pod_request_cpu_m(_workloads()["nyxgpt-web-canary"])
    )
    budget = DOCKER_DESKTOP_ALLOCATABLE_CPU_M - KUBE_SYSTEM_RESERVED_CPU_M
    assert scheduled + standby > budget, (
        "the pre-fix cpu requests now fit, so this pair of tests no longer proves the "
        "canary's Insufficient cpu failure is fixed"
    )


@pytest.mark.parametrize(
    ("stable", "canary"),
    [
        ("nyxgpt-api-stable", "nyxgpt-api-canary"),
        ("nyxgpt-web-stable", "nyxgpt-web-canary"),
    ],
)
def test_canary_cpu_requests_track_their_stable_pool(stable: str, canary: str) -> None:
    workloads = _workloads()
    assert _pod_request_cpu_m(workloads[canary]) == _pod_request_cpu_m(workloads[stable])


# --- the preflight arithmetic ---------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        ("512Mi", 512 * MIB),
        ("2Gi", 2 * 1024**3),
        ("1000M", 1000 * 1000**2),
        ("1024", 1024),
        ("7936Mi", 7936 * MIB),
        ("1.5Gi", int(1.5 * 1024**3)),
        # What a node actually reports its allocatable memory in -- capital
        # K, which the first cut of the regex did not match, so the preflight
        # skipped itself on every real cluster.
        ("16373452Ki", 16373452 * 1024),
    ],
)
def test_parse_k8s_quantity(quantity: str, expected: int) -> None:
    assert ops._parse_k8s_quantity(quantity) == expected


@pytest.mark.parametrize("quantity", ["", "lots", None, "512MB", "-1Mi", "512K"])
def test_parse_k8s_quantity_rejects_what_it_cannot_read(quantity: object) -> None:
    """Unreadable means skip the preflight, never guess a number and block."""
    assert ops._parse_k8s_quantity(quantity) is None


def _container(memory: str, cpu: str = "0", **extra: Any) -> dict[str, Any]:
    return {"resources": {"requests": {"memory": memory, "cpu": cpu}}, **extra}


def test_pod_request_sums_containers_and_floors_on_init_containers() -> None:
    """The scheduler charges max(sum of containers, largest initContainer)."""
    spec = {
        "containers": [_container("100Mi"), _container("150Mi")],
        "initContainers": [_container("256Mi")],
    }
    assert ops._pod_resource_request(spec) == 256 * MIB

    spec["initContainers"] = [_container("64Mi")]
    assert ops._pod_resource_request(spec) == 250 * MIB


def test_pod_request_adds_native_sidecars() -> None:
    """A restartPolicy: Always initContainer runs alongside, so it adds."""
    spec = {
        "containers": [_container("100Mi")],
        "initContainers": [_container("50Mi", restartPolicy="Always")],
    }
    assert ops._pod_resource_request(spec) == 150 * MIB


def test_pod_request_tolerates_containers_with_no_requests() -> None:
    spec = {"containers": [{"name": "no-resources"}, _container("64Mi")]}
    assert ops._pod_resource_request(spec) == 64 * MIB


def _workload(
    kind: str, name: str, memory: str, replicas: int | None = None, cpu: str = "0"
) -> dict[str, Any]:
    spec: dict[str, Any] = {"template": {"spec": {"containers": [_container(memory, cpu)]}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return {"kind": kind, "metadata": {"name": name}, "spec": spec}


def test_workload_totals_multiply_by_replicas_and_park_zero_replica_workloads() -> None:
    objects = [
        _workload("Deployment", "api", "256Mi", replicas=4),
        _workload("Deployment", "api-canary", "256Mi", replicas=0),
        _workload("StatefulSet", "cassandra", "2Gi", replicas=1),
        _workload("DaemonSet", "promtail", "128Mi"),
        {"kind": "Service", "metadata": {"name": "api"}, "spec": {}},
    ]
    scheduled, standby, breakdown = ops._workload_resource_requests(objects, node_count=3)

    # 4x256 + 2048 + (128 x 3 nodes) = 3456Mi; the canary is standby, and the
    # Service contributes nothing.
    assert scheduled == 3456 * MIB
    assert standby == 256 * MIB
    assert [name for name, _ in breakdown] == ["cassandra x1", "api x4", "promtail x3"]


def test_committed_memory_excludes_our_namespace_and_unscheduled_pods() -> None:
    pods = {
        "items": [
            {
                "metadata": {"namespace": "kube-system"},
                "spec": {"nodeName": "node", "containers": [_container("100Mi", "250m")]},
                "status": {"phase": "Running"},
            },
            {
                # A re-install replaces these; counting them double-charges.
                "metadata": {"namespace": ops.K8S_NAMESPACE},
                "spec": {"nodeName": "node", "containers": [_container("512Mi")]},
                "status": {"phase": "Running"},
            },
            {
                # Pending, so holding nothing.
                "metadata": {"namespace": "other"},
                "spec": {"containers": [_container("256Mi")]},
                "status": {"phase": "Pending"},
            },
            {
                "metadata": {"namespace": "other"},
                "spec": {"nodeName": "node", "containers": [_container("64Mi")]},
                "status": {"phase": "Succeeded"},
            },
        ]
    }
    with patch.object(
        ops, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(pods), "")
    ):
        committed, error = ops._k8s_committed_requests(ops.K8S_NAMESPACE)

    assert error is None
    assert committed == {"memory": 100 * MIB, "cpu": 250}


def test_node_allocatable_ignores_cordoned_nodes() -> None:
    nodes = {
        "items": [
            {"spec": {}, "status": {"allocatable": {"memory": "7936Mi", "cpu": "4"}}},
            {
                "spec": {"unschedulable": True},
                "status": {"allocatable": {"memory": "8Gi", "cpu": "8"}},
            },
        ]
    }
    with patch.object(
        ops, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(nodes), "")
    ):
        allocatable, count, error = ops._k8s_node_allocatable()

    assert error is None
    assert count == 1
    assert allocatable == {"memory": 7936 * MIB, "cpu": 4000}


# --- the preflight decision ------------------------------------------------


def _preflight(
    *,
    allocatable_mi: int,
    node_count: int = 1,
    requests_mi: list[int],
    standby_mi: int = 0,
    committed_mi: int = 0,
    allocatable_cpu_m: int = 100_000,
    requests_cpu_m: int = 0,
    standby_cpu_m: int = 0,
    committed_cpu_m: int = 0,
) -> list[ops.OpsResult]:
    """Run the preflight against a synthetic cluster and manifest set.

    CPU defaults to a node with far more than anything asked of it, so a test
    about memory stays a test about memory; the cpu-dimension tests set it
    explicitly.

    `K8S_DIR` is pointed at a path with no bootstrapped Secret so exactly one
    kustomization (the observability one) is rendered, whatever a previous
    real install left in the checkout.
    """
    objects = [
        _workload("Deployment", f"w{i}", f"{mi}Mi", 1, cpu=f"{requests_cpu_m if i == 0 else 0}m")
        for i, mi in enumerate(requests_mi)
    ]
    if standby_mi or standby_cpu_m:
        objects.append(
            _workload("Deployment", "canary", f"{standby_mi}Mi", 0, cpu=f"{standby_cpu_m}m")
        )
    with (
        patch.object(ops, "K8S_DIR", Path("/nonexistent-k8s-dir")),
        patch.object(
            ops,
            "_k8s_node_allocatable",
            return_value=(
                {"memory": allocatable_mi * MIB, "cpu": allocatable_cpu_m},
                node_count,
                None,
            ),
        ),
        patch.object(ops, "_k8s_render_kustomization", return_value=(objects, None)),
        patch.object(
            ops,
            "_k8s_committed_requests",
            return_value=({"memory": committed_mi * MIB, "cpu": committed_cpu_m}, None),
        ),
        patch.object(
            ops, "_ensure_k8s_observability_secret", return_value=[ops.OpsResult(True, "ok")]
        ),
    ):
        return ops._preflight_k8s_capacity()


def _for(results: list[ops.OpsResult], resource: str) -> ops.OpsResult:
    """The result the preflight produced for one resource."""
    matches = [r for r in results if resource in r.message.lower()]
    assert matches, f"no {resource} result in {[r.message for r in results]}"
    return matches[0]


def test_preflight_refuses_a_node_the_stack_does_not_fit() -> None:
    """The #3825 arithmetic: refuse before applying, not Pending afterwards."""
    results = _preflight(allocatable_mi=7936, requests_mi=[7872], committed_mi=290)

    assert not all(r.ok for r in results)
    memory = _for(results, "memory")
    assert "Not enough node memory" in memory.message
    assert "226Mi more memory" in memory.details  # 7872 - (7936 - 290)
    assert "Nothing was applied." in memory.details


def test_preflight_refuses_a_node_short_of_cpu() -> None:
    """Right-sizing memory alone just moves the wall.

    On a 4-core VM the canary Pod failed with `Insufficient cpu` once the
    memory fitted -- the same defect, a different word in the event.
    """
    results = _preflight(
        allocatable_mi=7936,
        requests_mi=[1024],
        allocatable_cpu_m=4000,
        requests_cpu_m=3500,
        committed_cpu_m=950,
    )

    assert not all(r.ok for r in results)
    cpu = _for(results, "cpu")
    assert "Not enough node cpu" in cpu.message
    assert "450m more cpu" in cpu.details  # 3500 - (4000 - 950)
    assert "Settings -> Resources -> CPUs" in cpu.details


def test_preflight_reports_both_resources() -> None:
    """One result per resource, so a pass says what it actually checked."""
    results = _preflight(allocatable_mi=7936, requests_mi=[1024], requests_cpu_m=500)

    assert len(results) == 2
    assert {"memory", "cpu"} == {
        word for r in results for word in ("memory", "cpu") if word in r.message.lower()
    }


def test_preflight_refusal_names_the_skip_observability_escape_hatch() -> None:
    """A refusal an operator cannot act on is just a different dead end."""
    with (
        patch.object(
            ops,
            "_k8s_node_allocatable",
            return_value=({"memory": 2 * 1024**3, "cpu": 8000}, 1, None),
        ),
        patch.object(
            ops,
            "_k8s_render_kustomization",
            return_value=([_workload("Deployment", "api", "4Gi", 1)], None),
        ),
        patch.object(ops, "_k8s_committed_requests", return_value=({"memory": 0, "cpu": 0}, None)),
        patch.object(
            ops, "_ensure_k8s_observability_secret", return_value=[ops.OpsResult(True, "ok")]
        ),
    ):
        results = ops._preflight_k8s_capacity(skip_observability=False)

    assert not all(r.ok for r in results)
    memory = _for(results, "memory")
    assert "--skip-observability" in memory.details
    assert "Docker Desktop" in memory.details


def test_preflight_passes_with_canary_headroom() -> None:
    results = _preflight(
        allocatable_mi=7936,
        requests_mi=[6976],
        standby_mi=448,
        committed_mi=290,
        allocatable_cpu_m=4000,
        requests_cpu_m=2075,
        standby_cpu_m=150,
        committed_cpu_m=950,
    )

    assert all(r.ok for r in results)
    assert "Node memory is sufficient" in _for(results, "memory").message
    assert "Node cpu is sufficient" in _for(results, "cpu").message


def test_preflight_warns_when_only_the_canary_headroom_is_missing() -> None:
    """Fits now, will not fit a rollout: say so rather than fail the install."""
    results = _preflight(allocatable_mi=7936, requests_mi=[7600], standby_mi=448)

    assert all(r.ok for r in results)
    memory = _for(results, "memory")
    assert "Memory is tight" in memory.message
    assert "canary" in memory.message


def test_preflight_on_a_multi_node_cluster_warns_instead_of_refusing() -> None:
    """Summed allocatable cannot prove a per-node placement either way.

    It can only ever be an upper bound, so an apparent shortfall across
    several nodes is reported, not turned into a refusal that a real cluster
    might not deserve.
    """
    results = _preflight(allocatable_mi=4096, node_count=3, requests_mi=[6000])

    assert all(r.ok for r in results)
    assert _for(results, "memory").message.startswith("Warning: Not enough node memory")


def test_preflight_skips_rather_than_blocks_when_it_cannot_measure() -> None:
    with (
        patch.object(ops, "_k8s_node_allocatable", return_value=({}, 0, "no nodes")),
        patch.object(ops, "_ensure_k8s_observability_secret") as bootstrap_secret,
    ):
        results = ops._preflight_k8s_capacity()

    assert all(r.ok for r in results)
    assert results[0].message == "Skipped capacity preflight"
    # A preflight that skipped must leave no trace behind it.
    bootstrap_secret.assert_not_called()


def test_preflight_skips_when_the_manifests_cannot_be_rendered() -> None:
    with (
        patch.object(
            ops,
            "_k8s_node_allocatable",
            return_value=({"memory": 8 * 1024**3, "cpu": 8000}, 1, None),
        ),
        patch.object(ops, "_k8s_render_kustomization", return_value=([], "kustomize blew up")),
        patch.object(
            ops, "_ensure_k8s_observability_secret", return_value=[ops.OpsResult(True, "ok")]
        ),
    ):
        results = ops._preflight_k8s_capacity()

    assert all(r.ok for r in results)
    assert results[0].message == "Skipped capacity preflight"


def test_render_never_touches_the_cluster() -> None:
    """`--dry-run=client`: a preflight that applied things would not be one."""
    with patch.object(
        ops,
        "_run",
        return_value=subprocess.CompletedProcess([], 0, '{"kind":"List","items":[]}', ""),
    ) as run:
        objects, error = ops._k8s_render_kustomization(K8S_DIR)

    assert (objects, error) == ([], None)
    assert "--dry-run=client" in run.call_args[0][0]


def test_install_runs_the_preflight_before_applying_anything() -> None:
    """Order is the whole point: a refusal after `kubectl apply` is too late."""
    order: list[str] = []

    def _record(label: str):
        def _step(*_args: object, **_kwargs: object) -> list[ops.OpsResult]:
            order.append(label)
            return [ops.OpsResult(True, label)]

        return _step

    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_build_and_load_k8s_image", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_preflight_k8s_capacity", side_effect=_record("preflight")),
        patch.object(ops, "_kubectl_apply_kustomization", side_effect=_record("apply")),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_apply_k8s_observability", side_effect=_record("apply-observability")),
        patch.object(ops, "_k8s_stack_health", return_value=[]),
        patch.object(ops, "_k8s_observability_health", return_value=[]),
        patch.object(ops, "_record_ops_action"),
    ):
        ops._install_kubernetes_steps(None)

    assert order == ["preflight", "apply", "apply-observability"]


def test_a_failing_preflight_stops_the_install() -> None:
    """The step loop must not carry on and apply the stack anyway."""
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_build_and_load_k8s_image", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops,
            "_preflight_k8s_capacity",
            return_value=[ops.OpsResult(False, "Not enough node memory")],
        ),
        patch.object(ops, "_kubectl_apply_kustomization") as apply_stack,
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None)

    apply_stack.assert_not_called()
    assert not all(r.ok for r in results)


def test_standalone_observability_install_is_preflighted_too() -> None:
    """`ops observability --kubernetes --local` adds the layer that broke it."""
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops,
            "_preflight_k8s_capacity",
            return_value=[ops.OpsResult(False, "Not enough node memory")],
        ) as preflight,
        patch.object(ops, "_apply_k8s_observability") as apply_observability,
        patch.object(ops, "_sync_packaged_resources") as sync,
    ):
        results = ops.observability_kubernetes()

    preflight.assert_called_once()
    sync.assert_not_called()
    apply_observability.assert_not_called()
    assert not all(r.ok for r in results)
