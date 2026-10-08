"""What the reporting commands say about reaching the SRE UIs (#4135).

`nyxgpt ops status` told every operator to open a port-forward for the
observability UIs -- including the one whose `nyxgpt ops install --kubernetes
--dev` had, minutes earlier on the same cluster, reported:

    [OK] SRE UIs reachable at http://127.0.0.1:3001, http://127.0.0.1:8080,
         http://127.0.0.1:9090, http://127.0.0.1:16686 (NodePorts published by
         the cluster -- no port-forward needed, ...)

Two messages in one product contradicting each other about one cluster. The
UIs worked; the instruction was wrong, which costs the operator either a step
they do not need or a hunt for a fault that does not exist.

So these tests pin the discriminator, not the wording of one branch: the
answer comes from what the **live Service** carries (`_k8s_service_node_ports`,
added by #4126 for exactly this class of claim), never from the install mode
and never from `K8S_OBSERVABILITY_PUBLISHED_SERVICES` on its own -- that table
records what *would* be published, which is the claim under test rather than
evidence for it. A mapped host port whose Service lost its node port
(`kubectl apply -k k8s/` re-asserts the shipped ClusterIP) is mapped and dark,
and must read as neither "published" nor "forward it".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit

ALL_SRE_HOST_PORTS = set(ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES)
SRE_SERVICES = tuple(entry.service for entry in ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES.values())
FORWARD_POINTER = "`nyxgpt ops port-forward --target observability`"


class _CP:
    """The subset of `CompletedProcess` `ops._run`'s callers actually read."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _published_node_ports() -> dict[str, set[int]]:
    """Every SRE Service carrying the node port the provisioned cluster maps."""
    return {
        entry.service: {entry.node_port}
        for entry in ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES.values()
    }


def _fake_cluster(
    monkeypatch,
    *,
    mapped: set[int],
    node_ports: dict[str, set[int]],
    present: tuple[str, ...] = SRE_SERVICES,
    forward_running: bool = False,
    in_cluster: bool = False,
    bridge: bool = False,
) -> None:
    """A live cluster to ask: node port mappings, and what each Service carries.

    Driven through `ops._run` rather than by stubbing the Service read itself,
    so the test exercises the helper that answers the question instead of
    asserting against its stand-in.
    """

    def fake_run(cmd, **_kwargs):
        if list(cmd[:5]) == ["kubectl", "-n", ops.K8S_NAMESPACE, "get", "svc"]:
            if cmd[5] == "-o":  # the whole-namespace listing
                return _CP(stdout="".join(f"service/{name}\n" for name in present))
            return _CP(stdout=" ".join(str(port) for port in sorted(node_ports.get(cmd[5], ()))))
        return _CP()

    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_in_cluster", lambda: in_cluster)
    monkeypatch.setattr(ops, "_kind_published_host_ports", lambda *_a, **_k: set(mapped))
    monkeypatch.setattr(ops, "_k8s_access_bridge_owns_host_ports", lambda: bridge)
    monkeypatch.setattr(
        ops,
        "port_forward_status",
        lambda: {"running": forward_running, "pid": 42 if forward_running else 0},
    )


# --- 1. the two branches, keyed on what the live Service carries -------------


def test_a_published_cluster_is_reported_as_reachable_not_as_homework(monkeypatch) -> None:
    """AC1: the URLs, and no instruction to forward what already answers."""
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=_published_node_ports())

    access = ops._k8s_observability_host_access(context=ops.KIND_CONTEXT)

    assert access.served == sorted(ALL_SRE_HOST_PORTS)
    assert access.stripped == []
    for port in sorted(ALL_SRE_HOST_PORTS):
        assert f"http://127.0.0.1:{port}" in access.note
    assert "no port-forward needed" in access.note
    assert FORWARD_POINTER not in access.note


def test_a_bring_your_own_cluster_still_gets_the_forward(monkeypatch) -> None:
    """AC2: where the Services are left ClusterIP the pointer is the only way in."""
    _fake_cluster(monkeypatch, mapped=set(), node_ports={})

    access = ops._k8s_observability_host_access(context="docker-desktop")

    assert access.served == [] and access.stripped == []
    assert FORWARD_POINTER in access.note
    assert "published by the cluster" not in access.note


def test_the_answer_is_the_live_service_not_the_mapping(monkeypatch) -> None:
    """AC4's discriminator, in the state that produced #4126.

    The node maps all four host ports and every Service is ClusterIP -- the
    shape a `kubectl apply -k k8s/` leaves behind. Mapped is not served:
    claiming these are published is the defect #4126 fixed in
    `ops port-forward`, and asking for a forward cannot work either, because
    the node container already holds the port.
    """
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports={})

    access = ops._k8s_observability_host_access(context=ops.KIND_CONTEXT)

    assert access.served == []
    assert access.stripped == sorted(ALL_SRE_HOST_PORTS)
    assert "lost their node port" in access.note
    assert "`nyxgpt ops observability --kubernetes`" in access.note
    assert "no port-forward needed" not in access.note


def test_a_partly_stripped_cluster_is_reported_per_port(monkeypatch) -> None:
    """One re-applied overlay does not make the other three unreachable."""
    grafana = ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES[3001]
    node_ports = _published_node_ports()
    node_ports[grafana.service] = set()
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=node_ports)

    access = ops._k8s_observability_host_access(context=ops.KIND_CONTEXT)

    assert access.stripped == [3001]
    assert access.served == sorted(ALL_SRE_HOST_PORTS - {3001})
    assert "http://127.0.0.1:3001 but the Service(s)" in access.note
    assert "SRE UIs reachable at http://127.0.0.1:8080" in access.note


def test_a_service_that_was_never_deployed_is_not_called_stripped(monkeypatch) -> None:
    """An absent Service carries no node port either -- and saying it "lost"
    one would be a fresh wrong message of the kind this issue is about."""
    _fake_cluster(
        monkeypatch,
        mapped=ALL_SRE_HOST_PORTS,
        node_ports=_published_node_ports(),
        present=("grafana",),
    )

    access = ops._k8s_observability_host_access(context=ops.KIND_CONTEXT)

    assert access.served == [3001]
    assert access.stripped == []


def test_a_cluster_with_no_sre_mappings_gets_the_forward(monkeypatch) -> None:
    """A `nyxgpt-local` created before the SRE ports were mapped: the node
    publishes none of them, and a running cluster's mappings cannot change."""
    _fake_cluster(monkeypatch, mapped={3000, 8000}, node_ports=_published_node_ports())

    access = ops._k8s_observability_host_access(context=ops.KIND_CONTEXT)

    assert access.served == [] and access.stripped == []
    assert FORWARD_POINTER in access.note


# --- 2. the other two access paths the install establishes ------------------


def test_a_running_managed_forward_is_not_asked_for_again(monkeypatch) -> None:
    """The install starts one on a BYO cluster; status says so, and still
    names the command, which is how the forward is re-established."""
    _fake_cluster(monkeypatch, mapped=set(), node_ports={}, forward_running=True)

    access = ops._k8s_observability_host_access(context="docker-desktop")

    assert "managed background port-forward" in access.note
    assert "`nyxgpt ops port-forward --status`" in access.note
    assert FORWARD_POINTER in access.note


def test_the_cloud_instance_names_the_bridge_not_a_forward(monkeypatch) -> None:
    """On the k3s instance the supervised bridge owns these ports; a forward
    started here would win a bind race it has to lose."""
    _fake_cluster(monkeypatch, mapped=set(), node_ports={}, bridge=True)

    access = ops._k8s_observability_host_access(context="default")

    assert "access bridge" in access.note
    assert "`nyxgpt cloud tunnel`" in access.note
    assert FORWARD_POINTER not in access.note


def test_in_a_pod_both_paths_are_named_and_neither_is_claimed(monkeypatch) -> None:
    """#3988's lesson: a Pod can see neither the node's port mappings nor the
    Services, so it must not answer for the machine the operator browses
    from -- the same stance the dashboard's own card takes."""
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports={}, in_cluster=True)

    access = ops._k8s_observability_host_access()

    assert FORWARD_POINTER in access.note
    assert "provisioned the cluster" in access.note
    assert access.served == [] and access.stripped == []


# --- 3. the wiring: what `ops status` and `nyxgpt up` actually print ---------


def _pods(*specs: tuple[str, str]) -> list[ops.K8sWorkloadState]:
    return [ops.K8sWorkloadState(name=name, state=state, summary=state) for name, state in specs]


def _stub_status(monkeypatch, tmp_path) -> None:
    """Reduce `ops status` to the one section these tests are about."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda _name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops,
        "_k8s_deployment_probe",
        lambda: ops.K8sDeploymentProbe(_pods(("nyxgpt-api-stable-0", ops.K8S_STATE_READY))),
    )
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {"grafana": "1/1 ready"})
    monkeypatch.setattr(ops, "_k8s_observability_data_flow", lambda _state=None: [])
    monkeypatch.setattr(ops, "_serving_status", lambda _mode: {"supported": False})
    monkeypatch.setattr(ops, "_print_required_models_status", lambda **_k: None)


def test_status_on_a_published_cluster_names_the_urls(monkeypatch, tmp_path, capsys) -> None:
    """The reported defect, at the surface it was reported on."""
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=_published_node_ports())
    _stub_status(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)

    assert ops.status(SimpleNamespace()) == 0

    out = capsys.readouterr().out
    assert "Kubernetes observability (in-cluster):" in out
    assert "Access: SRE UIs reachable at http://127.0.0.1:3001" in out
    assert "no port-forward needed" in out
    assert "port-forward --target observability" not in out


def test_status_on_a_bring_your_own_cluster_names_the_forward(monkeypatch, tmp_path, capsys) -> None:
    _fake_cluster(monkeypatch, mapped=set(), node_ports={})
    _stub_status(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")

    assert ops.status(SimpleNamespace()) == 0

    out = capsys.readouterr().out
    assert f"Access: reach the UIs with {FORWARD_POINTER}" in out


def test_status_reads_the_context_the_command_already_paid_for(monkeypatch, tmp_path, capsys):
    """Cost (first principle 1): the Kubernetes section already read the
    context to label the cluster, and the access answer rides on it."""
    reads = 0

    def counted():
        nonlocal reads
        reads += 1
        return ops.KIND_CONTEXT

    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=_published_node_ports())
    _stub_status(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_kubectl_context", counted)

    assert ops.status(SimpleNamespace()) == 0
    capsys.readouterr()

    assert reads == 1


def test_up_kubernetes_does_not_contradict_its_own_install(monkeypatch, capsys) -> None:
    """AC3, on the command that runs the install and then reports on it.

    `nyxgpt up --kubernetes` printed the forward instruction two screens below
    the install's own "no port-forward needed" -- the same contradiction, in
    the one command that emits both halves of it.
    """
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=_published_node_ports())
    monkeypatch.setattr(ops, "install", lambda _args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **_k: True)
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)

    assert ops.up(SimpleNamespace(kubernetes=True, no_wait=False, skip_observability=False)) == 0

    out = capsys.readouterr().out
    assert "SRE UIs reachable at http://127.0.0.1:3001" in out
    assert "port-forward --target observability" not in out


def test_the_install_and_the_status_report_agree(monkeypatch, capsys) -> None:
    """AC3 proper: one fake cluster, both commands, no contradiction.

    The install's own wording is asserted here too, because "status stopped
    being wrong" is not the same fact as "the two commands say the same
    thing", and only the second one is what the operator experienced.
    """
    _fake_cluster(monkeypatch, mapped=ALL_SRE_HOST_PORTS, node_ports=_published_node_ports())
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda **_k: True)
    monkeypatch.setattr(
        ops,
        "_publish_k8s_nodeports",
        lambda published: [ops.OpsResult(True, f"Published {len(published)} Service(s)")],
    )
    monkeypatch.setattr(ops, "_probe_host_urls", lambda _urls: [])

    installed = ops._ensure_k8s_observability_host_access()
    reported = ops._k8s_observability_host_access()
    capsys.readouterr()

    install_message = installed[-1].message
    assert "SRE UIs reachable at" in install_message
    assert "no port-forward needed" in install_message
    for message in (install_message, reported.note):
        assert "port-forward --target observability" not in message
        for port in sorted(ALL_SRE_HOST_PORTS):
            assert f"http://127.0.0.1:{port}" in message
