"""The Kubernetes deployment's host-reachable surface, and the two honesty
defects around it (#3986, #3988, #3991).

Three claims, each of which the tree failed before this file existed:

* **#3986** -- a completed `nyxgpt ops install --kubernetes` leaves the web UI
  reachable from the browser with no follow-up command. Previously the
  provisioned `kind` cluster published nothing and every Service was
  ClusterIP, so the install reported a healthy stack over a UI nobody could
  open, and the workaround (`kubectl port-forward`) died with the next Pod
  replacement.
* **#3988** -- the Infrastructure page, served BY the api Pod, reported the
  cluster it was running in as NOT DEPLOYED, because detection asked
  `kubectl config current-context` and a Pod has none.
* **#3991** -- an idle canary Deployment carrying replicas had no wrapped way
  back: `rollback` refuses (correctly) when no rollout is in progress, and the
  install never checked the manifests' resting contract it had just applied.

The manifest guards here are the load-bearing ones: the kind port mapping and
the Service NodePort are declared in two different files and are worthless
apart, so they are asserted against each other rather than each against a
literal.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from nyxgpt import canary, ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "k8s"


def _doc(name: str) -> dict:
    return yaml.safe_load((K8S_DIR / name).read_text())


def _port_forward_args(**overrides):
    """An argparse Namespace shaped like the `ops port-forward` parser."""
    args = argparse.Namespace(
        target="web",
        port=None,
        background=False,
        status=False,
        stop=False,
        supervise=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


# --- #3986: the host-reachable surface -------------------------------------


def test_base_manifests_stay_clusterip_so_the_cloud_posture_is_unchanged():
    """The NodePort is applied by ops, never declared in `k8s/` (#3986, #3503).

    These manifests are applied by the AWS k3s deployment too, whose invariant
    is that nothing but port 22 exists on the instance
    (docs/security.md; `scripts/k3s-cloud-smoke.sh` asserts it). A NodePort in
    the base manifest would bind on that node's interfaces as well -- so the
    reachability fix is a patch applied only where nyxGPT created the cluster
    AND mapped the ports to loopback.
    """
    for name in (
        "service-web.yaml",
        "service.yaml",
        "service-canary.yaml",
        "service-web-canary.yaml",
        "service-cassandra.yaml",
        "service-ollama.yaml",
    ):
        spec = _doc(name)["spec"]
        assert spec["type"] == "ClusterIP", name
        assert all("nodePort" not in port for port in spec["ports"]), name


def test_published_services_and_the_kind_mapping_cannot_drift():
    """The Service patch and the cluster's port mapping must agree, or neither works.

    #3986's fix is a constraint between two places: `_create_kind_cluster`
    maps a *node* port to a *host* port, and the Service patch is what makes
    that node port answer. Changing either alone silently restores the
    unreachable install, and nothing else in the tree would notice.
    """
    assert set(ops.K8S_HOST_PUBLISHED_SERVICES) == {
        host for host, _node in ops.KIND_HOST_PORT_MAPPINGS
    }
    for host_port, (_service, _port, node_port) in ops.K8S_HOST_PUBLISHED_SERVICES.items():
        assert (host_port, node_port) in ops.KIND_HOST_PORT_MAPPINGS

    # ...and the host side is the port every other local mode binds, so
    # `WEB_URL` means one thing across all of them.
    assert set(ops.K8S_HOST_PUBLISHED_SERVICES) == {
        ops.COMPOSE_COMPONENT_PORTS["web"],
        ops.COMPOSE_COMPONENT_PORTS["api"],
    }
    assert str(ops.COMPOSE_COMPONENT_PORTS["web"]) in ops.WEB_URL

    # The Services named here are the ones the manifests actually define.
    assert ops.K8S_HOST_PUBLISHED_SERVICES[3000][0] == _doc("service-web.yaml")["metadata"]["name"]
    assert ops.K8S_HOST_PUBLISHED_SERVICES[8000][0] == _doc("service.yaml")["metadata"]["name"]


def test_publish_nodeports_patches_both_services_idempotently(monkeypatch):
    """`kubectl apply -k` re-asserts ClusterIP on every install, so this re-publishes."""
    patched: list[list[str]] = []

    def fake_run(cmd, check=True, **_kw):
        patched.append(cmd)
        return SimpleNamespace(returncode=0, stdout="patched", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._publish_k8s_app_tier_nodeports()

    assert all(r.ok for r in results)
    assert len(patched) == 2
    for cmd in patched:
        assert cmd[:2] == ["kubectl", "-n"]
        assert "patch" in cmd
        body = json.loads(cmd[cmd.index("-p") + 1])
        assert body["spec"]["type"] == "NodePort"
        node_port = body["spec"]["ports"][0]["nodePort"]
        assert node_port in {node for _host, node in ops.KIND_HOST_PORT_MAPPINGS}


def test_host_access_publishes_before_it_probes(monkeypatch):
    """The probe is meaningless until the Services carry the node ports."""
    order: list[str] = []
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: True)
    monkeypatch.setattr(
        ops,
        "_publish_k8s_app_tier_nodeports",
        lambda: (order.append("publish"), [ops.OpsResult(True, "published")])[1],
    )
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: (order.append("probe"), None)[1])

    assert all(r.ok for r in ops._ensure_k8s_host_access())
    assert order == ["publish", "probe"]


def test_host_access_does_not_open_node_ports_on_a_cluster_it_did_not_create(monkeypatch):
    """A bring-your-own cluster keeps its ClusterIP posture (#3503's reasoning)."""
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: False)
    monkeypatch.setattr(
        ops,
        "_publish_k8s_app_tier_nodeports",
        lambda: pytest.fail("must not patch Services on a cluster nyxGPT did not create"),
    )
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: None)
    monkeypatch.setattr(
        ops, "start_port_forward_background", lambda *_a, **_k: [ops.OpsResult(True, "started")]
    )

    assert all(r.ok for r in ops._ensure_k8s_host_access())


def test_kind_cluster_config_publishes_every_mapping_on_loopback():
    """The generated kind config carries one extraPortMapping per pair, bound to 127.0.0.1."""
    config = yaml.safe_load(ops._kind_cluster_config())

    assert config["kind"] == "Cluster"
    mappings = config["nodes"][0]["extraPortMappings"]
    assert [(m["hostPort"], m["containerPort"]) for m in mappings] == list(
        ops.KIND_HOST_PORT_MAPPINGS
    )
    # Loopback only, per #3195: this is a workstation cluster holding an api key.
    assert {m["listenAddress"] for m in mappings} == {"127.0.0.1"}


def test_create_kind_cluster_writes_and_passes_the_config(monkeypatch, tmp_path):
    """The config is written where an operator can read it, and actually passed.

    Fault-injection value: with `--config` dropped, the cluster comes up fine
    and publishes nothing -- exactly the state #3986 reports -- so asserting
    the flag is what catches a regression that otherwise looks like success.
    """
    config_file = tmp_path / "k8s" / "kind-cluster.yaml"
    monkeypatch.setattr(ops, "KIND_CLUSTER_CONFIG_FILE", config_file)
    seen: list[list[str]] = []

    def fake_run(cmd, check=True, **_kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout="created", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._create_kind_cluster()

    assert all(r.ok for r in results)
    assert "--config" in seen[0]
    assert seen[0][seen[0].index("--config") + 1] == str(config_file)
    assert "extraPortMappings" in config_file.read_text()


def test_host_access_reports_the_url_when_the_cluster_publishes_it(monkeypatch):
    """A provisioned cluster with mapped ports needs no forward -- and is verified."""
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: True)
    monkeypatch.setattr(
        ops, "_publish_k8s_app_tier_nodeports", lambda: [ops.OpsResult(True, "published")]
    )
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: None)
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda *_a, **_k: pytest.fail("must not start a forward for a cluster that publishes"),
    )

    results = ops._ensure_k8s_host_access()

    assert all(r.ok for r in results)
    assert any(ops.WEB_URL in r.message for r in results)
    assert any("survives Pod replacement" in r.message for r in results)


def test_host_access_fails_loudly_when_the_published_url_does_not_answer(monkeypatch):
    """An install must not report success over a UI that cannot be opened.

    This is the defect #3986 is: every Pod Ready, `ops status` healthy, and
    nothing listening. A URL that does not answer is a failure here, not a
    note.
    """
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: True)
    monkeypatch.setattr(
        ops, "_publish_k8s_app_tier_nodeports", lambda: [ops.OpsResult(True, "published")]
    )
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: "ConnectError: refused")

    results = ops._ensure_k8s_host_access()

    assert not all(r.ok for r in results)
    assert any("did not answer" in r.message for r in results)


def test_host_access_establishes_a_managed_forward_on_a_byo_cluster(monkeypatch):
    """Where nyxGPT cannot map host ports, the install still establishes the path.

    Not a printed instruction: #3986's acceptance criterion is that the access
    path exists when the command returns, in the shape
    `nyxgpt cloud tunnel --background` already uses.
    """
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: None)
    started: list[str] = []

    def fake_start(target="app"):
        started.append(target)
        return [ops.OpsResult(True, "Background port-forward started (pid 1)")]

    monkeypatch.setattr(ops, "start_port_forward_background", fake_start)

    results = ops._ensure_k8s_host_access()

    assert all(r.ok for r in results)
    # web AND api, which is the combination docs/kubernetes.md used to ask for
    # while showing a command that forwarded one.
    assert started == ["app"]
    assert set(ops.K8S_APP_PORT_FORWARD_TARGETS) == {"web", "api"}


def test_host_access_defers_to_the_k3s_access_bridge(monkeypatch):
    """A cloud k3s instance must not get a second forward on the same two ports.

    Its systemd `--user` access bridge (docs/cloud.md) binds 127.0.0.1:3000
    and :8000, and this step runs BEFORE the provisioning script installs
    those units -- so a forward started here would win the bind race and leave
    every bridge unit restarting forever against a port it can never have.
    That is the shape of trap first principle 2 exists to catch: a fix for the
    local install breaking the cloud one.
    """
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "default")
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/k3s" if prog == "k3s" else None)
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda *_a, **_k: pytest.fail("must not compete with the access bridge for 3000/8000"),
    )

    results = ops._ensure_k8s_host_access()

    assert all(r.ok for r in results)
    assert any("access bridge" in r.message for r in results)


def test_a_linux_workstation_without_k3s_still_gets_the_managed_forward(monkeypatch, tmp_path):
    """The bridge guard is scoped to the substrate it exists for, not to Linux."""
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "minikube")
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda _prog: None)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: tmp_path)
    monkeypatch.setattr(ops, "_probe_web_url", lambda url, **_kw: None)
    started: list[str] = []
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda target="app": (started.append(target), [ops.OpsResult(True, "started")])[1],
    )

    assert all(r.ok for r in ops._ensure_k8s_host_access())
    assert started == ["app"]


def test_port_forward_app_target_expands_to_web_and_api():
    plan = ops._port_forward_plan(_port_forward_args(target="app"))
    assert [name for name, _svc, _local, _remote in plan] == ["web", "api"]
    assert [local for _n, _s, local, _r in plan] == [3000, 8000]


def test_background_port_forward_records_a_detached_supervisor(monkeypatch, tmp_path):
    """`--background` detaches a supervisor and records its pid for `--status`/`--stop`."""
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_STATE_FILE", tmp_path / "port-forward.json")
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_LOG_FILE", tmp_path / "port-forward.log")
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    spawned: dict = {}

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)

    results = ops.start_port_forward_background("app")

    assert all(r.ok for r in results)
    # Its own process group, so `--stop` can signal the kubectl children too.
    assert spawned["kwargs"]["start_new_session"] is True
    assert "--supervise" in spawned["argv"]
    record = json.loads((tmp_path / "port-forward.json").read_text())
    assert record["pid"] == 4242
    assert record["targets"] == ["web", "api"]

    monkeypatch.setattr(ops, "_process_alive", lambda pid: pid == 4242)
    status = ops.port_forward_status()
    assert status["running"] is True
    assert status["urls"] == ["http://127.0.0.1:3000", "http://127.0.0.1:8000"]


def test_port_forward_status_self_heals_a_dead_pid(monkeypatch, tmp_path):
    """A recorded pid that is gone (reboot, external kill) reads as not running."""
    state = tmp_path / "port-forward.json"
    state.write_text(json.dumps({"pid": 999999, "targets": ["web"], "urls": ["u"]}))
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_STATE_FILE", state)
    monkeypatch.setattr(ops, "_process_alive", lambda _pid: False)

    assert ops.port_forward_status()["running"] is False


def test_stop_port_forward_signals_the_group_and_clears_the_record(monkeypatch, tmp_path):
    state = tmp_path / "port-forward.json"
    state.write_text(json.dumps({"pid": 4242, "targets": ["web"], "urls": ["u"]}))
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_STATE_FILE", state)
    monkeypatch.setattr(ops, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(ops.os, "getpgid", lambda pid: pid)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(ops.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    results = ops.stop_port_forward()

    assert all(r.ok for r in results)
    assert killed and killed[0][0] == 4242
    assert not state.exists()


def test_supervisor_restarts_a_forward_that_died(monkeypatch):
    """The property a plain `kubectl port-forward` does not have (#3986).

    `kubectl port-forward` attaches to ONE Pod and exits when that Pod is
    replaced -- which a canary rollout and a self-heal restart both do by
    design. Without this loop the background forward would be exactly as
    fragile as the manual workaround the issue rejects.
    """
    plan = [("web", "nyxgpt-web", 3000, 3000)]
    spawns: list[list[str]] = []
    exit_codes = iter([1, None, None, None, None])

    class FakeProc:
        def __init__(self):
            self.returncode = 1

        def poll(self):
            try:
                return next(exit_codes)
            except StopIteration:
                return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **_kw):
        spawns.append(argv)
        return FakeProc()

    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ops.signal, "signal", lambda *_a, **_k: None)

    class Stopper:
        """Ends the loop after two passes -- long enough to observe the restart."""

        def __init__(self):
            self.passes = 0

        def is_set(self):
            return self.passes > 2

        def set(self):
            self.passes = 99

        def wait(self, _timeout):
            self.passes += 1

    monkeypatch.setattr(ops.threading, "Event", Stopper)

    assert ops._supervise_port_forward(plan) == 0
    # Spawned once, found dead, spawned again -- the restart is the assertion.
    assert len(spawns) >= 2
    assert spawns[0][:2] == ["kubectl", "-n"]


def test_probe_web_url_treats_any_http_response_as_reachable(monkeypatch):
    """A 404 from Next.js proves the path end to end; only a transport error doesn't."""
    monkeypatch.setattr(ops.httpx, "get", lambda *_a, **_k: SimpleNamespace(status_code=404))
    assert ops._probe_web_url("http://127.0.0.1:3000", budget_s=0.0) is None

    def boom(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ops.httpx, "get", boom)
    assert ops._probe_web_url("http://127.0.0.1:3000", budget_s=0.0) is not None


# --- #3988: reporting from inside the cluster -------------------------------


def test_in_cluster_requires_both_signals(monkeypatch, tmp_path):
    """Env var alone can be inherited by a shell; a token dir alone can be stale."""
    monkeypatch.setattr(ops, "K8S_SERVICEACCOUNT_DIR", tmp_path)
    monkeypatch.delenv(ops.K8S_IN_CLUSTER_ENV, raising=False)
    assert ops._in_cluster() is False

    monkeypatch.setenv(ops.K8S_IN_CLUSTER_ENV, "10.96.0.1")
    assert ops._in_cluster() is False  # no token yet

    (tmp_path / "token").write_text("t")
    assert ops._in_cluster() is True


def _infra_status_in_cluster(monkeypatch, *, in_cluster: bool, pods: list[str]):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops.self_heal,
        "compose_probe",
        lambda: ops.self_heal.ComposeProbe(
            available=False,
            reason="`docker compose ps` exited 14: stat /root/.nyxGPT/docker-compose.yml",
        ),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_in_cluster", lambda: in_cluster)
    # The gate the issue is about: a Pod's `kubectl config current-context` is empty.
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "")
    monkeypatch.setattr(
        ops,
        "_k8s_pod_states",
        lambda *a, **k: (
            [ops.K8sWorkloadState(name, ops.K8S_STATE_READY, "1/1 Running") for name in pods],
            None,
        ),
    )
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})
    return ops.infra_status()


def test_infra_status_detects_the_cluster_it_is_served_from(monkeypatch):
    """The #3988 headline: a Pod must not report its own cluster NOT DEPLOYED.

    Non-vacuous by construction -- `_kubectl_context` returns "" here, which is
    exactly what a Pod sees, and is what the old `bool(current-context)` gate
    read as "no cluster was ever configured".
    """
    status = _infra_status_in_cluster(monkeypatch, in_cluster=True, pods=["nyxgpt-api-stable-1"])

    assert status["kubernetes"]["configured"] is True
    assert status["kubernetes"]["deployed"] is True
    assert status["kubernetes"]["in_cluster"] is True
    assert status["kubernetes"]["context"] == ops.K8S_IN_CLUSTER_CONTEXT_LABEL
    assert status["mode"] == "kubernetes"
    # Never claimed: a Pod cannot see whether its nodes belong to a kind
    # cluster nyxGPT provisioned.
    assert status["kubernetes"]["provisioned"] is False


def test_infra_status_without_in_cluster_credentials_still_reports_not_deployed(monkeypatch):
    """The #3468 behavior is preserved for a machine with genuinely no cluster."""
    status = _infra_status_in_cluster(monkeypatch, in_cluster=False, pods=[])

    assert status["kubernetes"]["configured"] is False
    assert status["kubernetes"]["deployed"] is False
    assert status["in_cluster"] is False


def test_infra_status_scopes_compose_and_native_out_from_inside_a_pod(monkeypatch):
    """Rows a Pod cannot answer say so, and never leak the container's own paths."""
    status = _infra_status_in_cluster(monkeypatch, in_cluster=True, pods=["nyxgpt-api-stable-1"])

    assert status["in_cluster"] is True
    assert status["compose_probe_available"] is False
    reason = status["compose_probe_reason"]
    assert "Not in scope" in reason
    # The leaked container path is the evidence in #3988's report; it must not
    # reach the operator as the reason for a verdict about their machine.
    assert "/root/.nyxGPT" not in reason

    assert status["install_mode"]["in_scope"] is False
    assert "Not in scope" in status["install_mode"]["out_of_scope_reason"]


def test_infra_status_keeps_the_compose_probe_reason_on_a_host(monkeypatch):
    """Off-cluster, a probe failure's cause is still reported (the #3812 behavior)."""
    status = _infra_status_in_cluster(monkeypatch, in_cluster=False, pods=[])

    assert "docker compose ps" in status["compose_probe_reason"]
    assert status["install_mode"]["in_scope"] is True


def test_rbac_grants_the_list_the_in_cluster_read_needs():
    """The read is declared in `k8s/`, not left to a cluster's defaults (#3988 AC).

    `_k8s_observability_workload_state` runs `kubectl get deploy` / `get
    daemonset` for the whole namespace -- a LIST, which the Role's `get` alone
    does not cover. Without these the page would detect the cluster and then
    call every observability workload absent.
    """
    role = next(
        doc
        for doc in yaml.safe_load_all((K8S_DIR / "rbac.yaml").read_text())
        if doc and doc.get("kind") == "Role"
    )
    verbs = {}
    for rule in role["rules"]:
        for resource in rule["resources"]:
            verbs.setdefault(resource, set()).update(rule["verbs"])

    assert "list" in verbs["deployments"]
    assert "list" in verbs["daemonsets"]
    assert {"get", "list"} <= verbs["pods"]


# --- #3991: the canary resting contract -------------------------------------


def test_canary_manifests_declare_a_zero_replica_resting_state():
    """The contract `reset` and the install reconcile enforce."""
    assert _doc("deployment-canary.yaml")["spec"]["replicas"] == 0
    assert _doc("deployment-web-canary.yaml")["spec"]["replicas"] == 0
    assert _doc("deployment-stable.yaml")["spec"]["replicas"] == canary.DEFAULT_RESTING_REPLICAS


def test_canary_reset_scales_an_idle_canary_back_to_zero(monkeypatch, tmp_path):
    """The wrapped stand-down that did not exist (#3991).

    In this state `rollback` answers "No canary rollout in progress" and
    refuses -- correctly, it ends rollouts -- which left raw `kubectl scale` as
    the only recovery.
    """
    monkeypatch.setattr(canary, "_state_path", lambda: tmp_path / "canary_state.json")
    monkeypatch.setattr(canary, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        canary, "_desired_replicas", lambda name, ns=None: 1 if "canary" in name else 1
    )
    scaled: list[tuple[str, int]] = []

    def fake_scale(name, replicas, namespace=canary.DEFAULT_NAMESPACE):
        scaled.append((name, replicas))
        return canary.CanaryResult(True, f"Scaled {name} to {replicas} replicas")

    monkeypatch.setattr(canary, "_scale", fake_scale)
    monkeypatch.setattr(canary.ops_module, "record_canary_action", lambda *a, **k: None)

    # The proof that `rollback` cannot do this job.
    assert canary.rollback(component="api").message == "No canary rollout in progress"

    result = canary.reset(component="api")

    assert result.ok
    assert scaled == [(canary.CANARY_DEPLOYMENT, 0)]
    assert "resting 0" in result.message


def test_canary_reset_is_a_no_op_when_already_at_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(canary, "_state_path", lambda: tmp_path / "canary_state.json")
    monkeypatch.setattr(canary, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 0)
    monkeypatch.setattr(
        canary, "_scale", lambda *a, **k: pytest.fail("must not scale a Deployment already at rest")
    )

    result = canary.reset(component="api")

    assert result.ok
    assert "already at its resting 0" in result.message


def test_canary_reset_refuses_to_end_a_live_rollout(monkeypatch, tmp_path):
    """Ending a rollout is `rollback`'s job; a command named "reset" must not
    quietly take traffic away from one an operator deliberately started."""
    state = tmp_path / "canary_state.json"
    state.write_text(json.dumps({"active": True, "weight_percent": 25, "history": []}))
    monkeypatch.setattr(canary, "_state_path", lambda: state)
    monkeypatch.setattr(canary, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        canary, "_scale", lambda *a, **k: pytest.fail("must not scale during a rollout")
    )

    result = canary.reset(component="api")

    assert not result.ok
    assert "rollout is in progress" in result.message
    assert "rollback" in result.message


def test_install_reconciles_both_components_to_their_resting_state(monkeypatch):
    """The install asserts the contract it applied, rather than assuming it (#3991)."""
    calls: list[str] = []

    def fake_reset(namespace, *, component):
        calls.append(component)
        return ops.OpsResult(True, f"Reset {component} to its resting 0")

    monkeypatch.setattr(canary, "reset", fake_reset)

    results = ops._reconcile_k8s_canary_resting()

    assert calls == list(canary.COMPONENTS)
    assert all(r.ok for r in results)


def test_install_does_not_end_a_rollout_an_operator_started(monkeypatch):
    """A refusal because a rollout IS in progress is not an install failure."""
    monkeypatch.setattr(
        canary,
        "reset",
        lambda namespace, *, component: ops.OpsResult(
            False, "A canary rollout is in progress at 25% -- use `nyxgpt canary rollback`"
        ),
    )

    results = ops._reconcile_k8s_canary_resting()

    assert all(r.ok for r in results)


def test_canary_reset_step_runs_after_the_app_tier_is_up():
    """Ordering: reading replica counts mid-rollout races the rollout itself."""
    import inspect

    source = inspect.getsource(ops._install_kubernetes_steps)
    assert source.index("_wait_for_k8s_app_tier") < source.index("_reconcile_k8s_canary_resting")
    assert source.index("_reconcile_k8s_canary_resting") < source.index("_ensure_k8s_host_access")


def test_port_forward_dispatches_status_and_stop_without_touching_kubectl(monkeypatch, capsys):
    """`--status` answers from the recorded state; neither needs a cluster."""
    monkeypatch.setattr(
        ops, "port_forward_status", lambda: {"running": False, "pid": 0, "targets": [], "urls": []}
    )
    monkeypatch.setattr(
        ops, "_which", lambda _p: pytest.fail("--status must not require kubectl on PATH")
    )

    assert ops.port_forward(_port_forward_args(status=True)) == 0
    assert "No managed background port-forward is running." in capsys.readouterr().out

    stopped: list[bool] = []
    monkeypatch.setattr(
        ops,
        "stop_port_forward",
        lambda: (stopped.append(True), [ops.OpsResult(True, "stopped")])[1],
    )
    assert ops.port_forward(_port_forward_args(stop=True)) == 0
    assert stopped == [True]


def test_subprocess_import_is_used_for_the_supervisor_argv():
    """The supervisor re-enters this CLI through the running interpreter.

    Not through a `nyxgpt` on PATH: the api process that may start it, and a
    venv install whose bin directory is not exported, can both be relied on to
    have the package importable and neither to have the console script
    findable.
    """
    argv = ops._supervisor_argv("app")
    assert "python" in Path(argv[0]).name
    assert argv[1] == "-c"
    assert "nyxgpt.cli" in argv[2]
    assert argv[-4:] == ["port-forward", "--target", "app", "--supervise"]


def test_canary_page_points_at_the_cli_instead_of_acting():
    """#3991's added scope, asserted against the shipped page.

    The removed control ran `docker build` inside the in-cluster api Pod. The
    page must name `nyxgpt canary deploy` and must not POST to the deploy
    endpoint -- which is the thing a future edit would silently re-add.
    """
    page = (REPO_ROOT / "web" / "src" / "app" / "admin" / "canary" / "page.tsx").read_text()

    assert "nyxgpt canary deploy" in page
    assert "/api/v1/canary/deploy" not in page
    assert "Deploy current version to canary" not in page
    # The traffic controls #3409/#3829 deliberately keep on this page stay.
    assert "/api/v1/canary/start" in page
    assert "/api/v1/canary/rollback" in page
