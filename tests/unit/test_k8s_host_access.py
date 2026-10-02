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

from nyxgpt import canary, install_mode, ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "k8s"


def _doc(name: str) -> dict:
    return yaml.safe_load((K8S_DIR / name).read_text())


def _node_ports_as_published(service):
    """`_k8s_service_node_ports` for a cluster where every patch stuck.

    Reads the product's own table so a test cannot disagree with it about
    which node port belongs to which Service.
    """
    return {
        entry.node_port
        for entry in ops.K8S_HOST_PUBLISHED_SERVICES.values()
        if entry.service == service
    }


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
    for host_port, entry in ops.K8S_HOST_PUBLISHED_SERVICES.items():
        assert (host_port, entry.node_port) in ops.KIND_HOST_PORT_MAPPINGS
        # Node ports have to be inside the range Kubernetes reserves for them,
        # which is why they cannot simply repeat the host port (16686 is not).
        assert 30000 <= entry.node_port <= 32767

    # ...and the app tier's host side is the port every other local mode binds,
    # so `WEB_URL` means one thing across all of them.
    assert set(ops.K8S_APP_TIER_PUBLISHED_SERVICES) == {
        ops.COMPOSE_COMPONENT_PORTS["web"],
        ops.COMPOSE_COMPONENT_PORTS["api"],
    }
    assert str(ops.COMPOSE_COMPONENT_PORTS["web"]) in ops.WEB_URL

    # The Services named here are the ones the manifests actually define.
    assert (
        ops.K8S_HOST_PUBLISHED_SERVICES[3000].service
        == _doc("service-web.yaml")["metadata"]["name"]
    )
    assert ops.K8S_HOST_PUBLISHED_SERVICES[8000].service == _doc("service.yaml")["metadata"]["name"]


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
    monkeypatch.setattr(ops, "_host_ports_in_use", lambda _ports: [])
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


def test_create_kind_cluster_refuses_when_a_mapped_host_port_is_taken(monkeypatch, tmp_path):
    """Docker's own error for this names a container and a port range, not the cause.

    Six mapped host ports is enough surface that a native or Compose stack on
    the same workstation is a likely owner of one of them, and an operator who
    reads `failed to create cluster: ... port is already allocated` has no way
    to know which of their deployments to stop.
    """
    monkeypatch.setattr(ops, "KIND_CLUSTER_CONFIG_FILE", tmp_path / "kind.yaml")
    monkeypatch.setattr(ops, "_host_ports_in_use", lambda _ports: [3001, 9090])
    monkeypatch.setattr(
        ops, "_run", lambda *_a, **_k: pytest.fail("must not call kind with a port already taken")
    )

    results = ops._create_kind_cluster()

    assert not all(r.ok for r in results)
    assert "3001, 9090" in results[0].message
    assert "nyxgpt ops down" in (results[0].details or "")


# --- #3986 (owner re-test, 2026-08-26): the SRE tier's own host surface ------


def _obs_doc(name: str, kind: str, metadata_name: str) -> dict:
    """One document out of a multi-document `k8s/observability/` manifest."""
    return next(
        doc
        for doc in yaml.safe_load_all((K8S_DIR / "observability" / name).read_text())
        if doc and doc.get("kind") == kind and doc["metadata"]["name"] == metadata_name
    )


def test_the_sre_tier_mapping_matches_the_ports_the_dashboard_links_to():
    """The SRE host ports are the ones the admin dashboard's links already use.

    Not a free choice: `[monitoring] grafana_ui_url` and friends default to
    localhost:3001/9090/16686/8080, `K8S_PORT_FORWARD_TARGETS` publishes those
    same numbers, and the panels are built from them (#3787). A mapping on any
    other host port would be reachable and still leave every tile broken.
    """
    mapped = {host for host, _node in ops.K8S_OBSERVABILITY_HOST_PORT_MAPPINGS}
    forwarded = {
        ops.K8S_PORT_FORWARD_TARGETS[name][1] for name in ops.K8S_OBSERVABILITY_PORT_FORWARD_TARGETS
    }
    assert mapped == forwarded
    assert set(ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES) == mapped
    # The two tiers are disjoint and together are what the cluster publishes.
    assert not mapped & {host for host, _node in ops.K8S_APP_TIER_HOST_PORT_MAPPINGS}
    assert set(ops.KIND_HOST_PORT_MAPPINGS) == set(ops.K8S_APP_TIER_HOST_PORT_MAPPINGS) | set(
        ops.K8S_OBSERVABILITY_HOST_PORT_MAPPINGS
    )


def test_the_sre_patch_matches_what_the_observability_manifests_declare():
    """A patch naming the wrong port or port name rewrites the Service instead of publishing it.

    `kubectl patch` merges `spec.ports` by port NUMBER, so the entry has to
    carry the Service port the manifest declares and repeat its name --
    Jaeger's UI port is `ui`, not `http`, and getting that wrong would rename
    the port its own Deployment's `targetPort` resolves through.
    """
    declared = {
        "grafana": _obs_doc("grafana.yaml", "Service", "grafana"),
        "prometheus": _obs_doc("prometheus.yaml", "Service", "prometheus"),
        "jaeger": _obs_doc("jaeger.yaml", "Service", "jaeger"),
        "glitchtip": _obs_doc("glitchtip.yaml", "Service", "glitchtip"),
    }
    for entry in ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES.values():
        spec = declared[entry.service]["spec"]
        # Still ClusterIP in the manifest, per #3503 -- the AWS k3s deployment
        # applies these too, and a NodePort there would open a port on an
        # instance whose invariant is that only 22 exists.
        assert spec["type"] == "ClusterIP", entry.service
        assert all("nodePort" not in port for port in spec["ports"]), entry.service
        port = next(p for p in spec["ports"] if p["port"] == entry.port)
        assert port["name"] == entry.port_name
        assert port["targetPort"] == entry.port_name


def test_publishing_jaeger_leaves_its_otlp_ports_alone():
    """Jaeger's Service also carries otlp-grpc/otlp-http, which the collector exports to.

    A patch that replaced the port list (`--type=merge`/`json`) would delete
    them and silently cut tracing in the cluster while making the UI
    reachable. Strategic merge -- kubectl's default, which is what the absence
    of a `--type` flag selects -- merges by port number instead.
    """
    patched: list[list[str]] = []

    def fake_run(cmd, check=True, **_kw):
        patched.append(cmd)
        return SimpleNamespace(returncode=0, stdout="patched", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ops, "_run", fake_run)
        results = ops._publish_k8s_nodeports(ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES)

    assert all(r.ok for r in results)
    assert len(patched) == 4
    jaeger = next(cmd for cmd in patched if "jaeger" in cmd)
    assert "--type" not in jaeger
    body = json.loads(jaeger[jaeger.index("-p") + 1])
    assert [p["port"] for p in body["spec"]["ports"]] == [16686]
    assert body["spec"]["ports"][0]["name"] == "ui"


def _sre_host_access(monkeypatch, *, context, publishes, present=None, probe=None):
    """`_ensure_k8s_observability_host_access` with the cluster stubbed out."""
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: context)
    monkeypatch.setattr(ops, "_kind_cluster_publishes_host_ports", lambda *_a, **_k: publishes)
    monkeypatch.setattr(
        ops,
        "_k8s_services_present",
        lambda names: set(names) if present is None else set(present),
    )
    monkeypatch.setattr(ops, "_probe_host_urls", lambda urls, **_kw: probe or [])
    return ops._ensure_k8s_observability_host_access()


def test_the_sre_tier_is_published_on_a_provisioned_cluster(monkeypatch):
    """The failure this round fixes: a healthy install with four dark SRE panels.

    All ten observability workloads `1/1 Running`, every Service ClusterIP,
    `KIND_HOST_PORT_MAPPINGS` carrying only the app tier -- so Grafana,
    Prometheus, Jaeger and GlitchTip all gave ERR_CONNECTION_REFUSED on a
    17-hour-old install, and the dashboard the Definition of Done requires to
    be usable without a terminal needed one.
    """
    monkeypatch.setattr(
        ops,
        "_publish_k8s_nodeports",
        lambda published: [
            ops.OpsResult(True, f"{e.service} published") for e in published.values()
        ],
    )
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda *_a, **_k: pytest.fail("a published cluster needs no forward"),
    )

    results = _sre_host_access(monkeypatch, context=ops.KIND_CONTEXT, publishes=True)

    assert all(r.ok for r in results)
    reachable = next(r for r in results if "SRE UIs reachable" in r.message)
    for port in (3001, 8080, 9090, 16686):
        assert f"http://127.0.0.1:{port}" in reachable.message
    assert "no port-forward needed" in reachable.message
    assert "survive Pod replacement" in reachable.message


def test_the_sre_tier_fails_loudly_when_a_published_ui_does_not_answer(monkeypatch):
    """Same standard as the web UI: a URL that does not answer is a failure, not a note."""
    monkeypatch.setattr(
        ops, "_publish_k8s_nodeports", lambda published: [ops.OpsResult(True, "published")]
    )

    results = _sre_host_access(
        monkeypatch,
        context=ops.KIND_CONTEXT,
        publishes=True,
        probe=[("http://127.0.0.1:3001", "ConnectError: refused")],
    )

    assert not all(r.ok for r in results)
    assert any("did not answer" in r.message for r in results)


def test_the_sre_tier_step_is_a_skip_inside_a_pod(monkeypatch):
    """The SRE dashboard can trigger this deploy, and it is served BY the api Pod.

    A Pod has no host ports to publish and its `127.0.0.1` is its own
    container, so publishing and probing are both meaningless there -- and a
    forward started inside a Pod would be a child nobody can reach. Reporting
    on the wrong machine is #3988's lesson.
    """
    monkeypatch.setattr(ops, "_in_cluster", lambda: True)
    monkeypatch.setattr(
        ops, "_publish_k8s_nodeports", lambda _p: pytest.fail("a Pod publishes nothing")
    )
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda *_a, **_k: pytest.fail("a Pod must not start a forward"),
    )

    results = _sre_host_access(monkeypatch, context="", publishes=False)

    assert all(r.ok for r in results)
    assert "in-cluster" in results[0].message


def test_no_observability_layer_is_a_skip_not_a_failure(monkeypatch):
    """`--skip-observability` deployed no tier; an absent tier is not an unreachable one."""
    monkeypatch.setattr(
        ops,
        "_publish_k8s_nodeports",
        lambda _published: pytest.fail("nothing to publish"),
    )

    results = _sre_host_access(monkeypatch, context=ops.KIND_CONTEXT, publishes=True, present=set())

    assert all(r.ok for r in results)
    assert "No observability layer" in results[0].message


def test_the_sre_forward_claims_only_the_ports_the_cluster_does_not_publish(monkeypatch):
    """The upgrade path, and the trap in it (first principle 2).

    A `nyxgpt-local` created by #3986's FIRST round publishes 3000/8000 and
    nothing else -- its published ports cannot be added to, because a kind node
    is a container. Forwarding the group `app,observability` there would try to
    bind two host ports the node already holds, so the supervisor would spin on
    a bind failure and the fix for the SRE tier would have cost the web UI.
    """
    started: list[str] = []
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda target: (started.append(target), [ops.OpsResult(True, "started")])[1],
    )
    monkeypatch.setattr(ops, "_k8s_access_bridge_owns_host_ports", lambda: False)

    results = _sre_host_access(monkeypatch, context=ops.KIND_CONTEXT, publishes=False)

    assert all(r.ok for r in results)
    assert started == [",".join(ops.K8S_OBSERVABILITY_PORT_FORWARD_TARGETS)]
    assert "web" not in started[0].split(",")
    assert "api" not in started[0].split(",")


def test_the_sre_forward_covers_only_services_that_exist(monkeypatch):
    """A half-deployed tier gets a forward for what is there, not four that fail."""
    started: list[str] = []
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda target: (started.append(target), [ops.OpsResult(True, "started")])[1],
    )
    monkeypatch.setattr(ops, "_k8s_access_bridge_owns_host_ports", lambda: False)

    results = _sre_host_access(
        monkeypatch, context="minikube", publishes=False, present={"grafana", "jaeger"}
    )

    assert all(r.ok for r in results)
    assert started == ["grafana,jaeger"]


def test_the_sre_tier_defers_to_the_k3s_access_bridge(monkeypatch):
    """`nyxgpt-k8s-bridge@observability` already owns these ports on a cloud instance.

    Same race as the app tier's (#3986): the install runs before the deploy
    script enables those units, so a forward started here would win the bind
    and leave a `Restart=always` unit failing forever.
    """
    monkeypatch.setattr(ops, "_k8s_access_bridge_owns_host_ports", lambda: True)
    monkeypatch.setattr(
        ops,
        "start_port_forward_background",
        lambda *_a, **_k: pytest.fail("must not compete with the access bridge"),
    )

    results = _sre_host_access(monkeypatch, context="default", publishes=False)

    assert all(r.ok for r in results)
    assert any("access bridge" in r.message for r in results)


def test_the_background_forward_extends_instead_of_claiming_to_be_covered(monkeypatch, tmp_path):
    """Two establish-access steps, one supervisor: the second must not be a no-op.

    On a bring-your-own cluster the app tier's step starts a forward for
    `web,api`, and the SRE tier's step then arrives with one already running.
    Reporting "already running (pid N)" and returning would leave the SRE tier
    unreachable on exactly the deployment that cannot publish node ports.
    """
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_STATE_FILE", tmp_path / "port-forward.json")
    monkeypatch.setattr(ops, "K8S_PORT_FORWARD_LOG_FILE", tmp_path / "port-forward.log")
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops,
        "port_forward_status",
        lambda: {"running": True, "pid": 11, "targets": ["web", "api"], "urls": []},
    )
    stopped: list[bool] = []
    monkeypatch.setattr(
        ops,
        "stop_port_forward",
        lambda: (stopped.append(True), [ops.OpsResult(True, "stopped")])[1],
    )
    spawned: dict = {}
    monkeypatch.setattr(
        ops.subprocess,
        "Popen",
        lambda argv, **_kw: (spawned.update(argv=argv), SimpleNamespace(pid=77))[1],
    )

    results = ops.start_port_forward_background("grafana,jaeger")

    assert all(r.ok for r in results)
    assert stopped == [True]
    target = spawned["argv"][spawned["argv"].index("--target") + 1]
    assert target.split(",") == ["web", "api", "grafana", "jaeger"]
    record = json.loads((tmp_path / "port-forward.json").read_text())
    assert record["targets"] == ["web", "api", "grafana", "jaeger"]


def test_the_background_forward_is_still_idempotent_when_it_already_covers_the_target(
    monkeypatch, tmp_path
):
    """The #3986 behavior that must survive the extension above."""
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops,
        "port_forward_status",
        lambda: {"running": True, "pid": 11, "targets": ["web", "api"], "urls": ["u"]},
    )
    monkeypatch.setattr(
        ops.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not start a second supervisor")
    )

    results = ops.start_port_forward_background("app")

    assert all(r.ok for r in results)
    assert "already running" in results[0].message


def test_port_forward_accepts_a_comma_separated_target_list():
    """What lets the install forward exactly the unpublished Services (#3986)."""
    plan = ops._port_forward_plan(_port_forward_args(target="grafana,jaeger"))
    assert [name for name, _s, _l, _r in plan] == ["grafana", "jaeger"]
    # Groups expand inside a list, and duplicates collapse.
    plan = ops._port_forward_plan(_port_forward_args(target="app,web,glitchtip"))
    assert [name for name, _s, _l, _r in plan] == ["web", "api", "glitchtip"]
    # One bad element invalidates the whole list rather than being dropped.
    assert ops._port_forward_plan(_port_forward_args(target="grafana,nope")) is None
    # ...and a multi-target list still refuses a single --port override.
    assert ops._port_forward_plan(_port_forward_args(target="grafana,jaeger", port=1)) is None
    assert ops._port_forward_plan(_port_forward_args(target="grafana", port=1))[0][2] == 1


def test_kind_published_host_ports_reads_the_nodes_real_mapping(monkeypatch):
    """Asked of the node container, because a config file is not what is running."""
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/bin/docker")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout="30300/tcp -> 127.0.0.1:3000\n30301/tcp -> 127.0.0.1:3001\n",
            stderr="",
        ),
    )

    assert ops._kind_published_host_ports() == {3000, 3001}
    assert ops._kind_cluster_publishes_host_ports(mappings=((3000, 30300),)) is True
    assert (
        ops._kind_cluster_publishes_host_ports(mappings=ops.K8S_OBSERVABILITY_HOST_PORT_MAPPINGS)
        is False
    )

    # No docker, no container, no answer: "not published" is the conservative
    # reading, and it is what routes the install to a forward.
    monkeypatch.setattr(ops, "_which", lambda _p: None)
    assert ops._kind_published_host_ports() == set()


def test_port_forward_does_not_fight_the_cluster_for_a_port_it_publishes(monkeypatch, capsys):
    """An operator whose notes still say `--target observability` must not see a bind error.

    `kubectl port-forward` cannot bind a host port the kind node holds, so
    running the documented command against a cluster that publishes these UIs
    would print `address already in use` about four UIs that are working.
    """
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(
        ops, "_kind_published_host_ports", lambda *_a, **_k: {3000, 8000, 3001, 8080, 9090, 16686}
    )
    # Every Service really carries the node port its mapping points at, so the
    # mapped host ports are being served and there is nothing to repair.
    monkeypatch.setattr(ops, "_k8s_service_node_ports", _node_ports_as_published)
    monkeypatch.setattr(
        ops.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not forward a published port")
    )

    assert ops.port_forward(_port_forward_args(target="observability")) == 0
    out = capsys.readouterr().out
    assert "already published at http://127.0.0.1:3001" in out
    assert "Nothing left to forward." in out

    # ...and a target it does NOT publish is still forwarded: the partition is
    # per-port, not a blanket refusal.
    monkeypatch.setattr(ops, "_kind_published_host_ports", lambda *_a, **_k: {3000, 8000})
    forwarded: list[list[str]] = []
    monkeypatch.setattr(
        ops.subprocess,
        "Popen",
        lambda argv, **_kw: (
            forwarded.append(argv),
            SimpleNamespace(wait=lambda timeout=None: 0, terminate=lambda: None),
        )[1],
    )

    assert ops.port_forward(_port_forward_args(target="app,grafana")) == 0
    assert len(forwarded) == 1
    assert "svc/grafana" in forwarded[0]


def _published_but_clusterip(monkeypatch, probe=lambda _url, **_kw: None):
    """A provisioned cluster that maps all six host ports and serves none of them.

    The state a `kubectl apply -k k8s/` leaves behind: the node container still
    publishes every mapping (they are fixed at cluster creation and cannot be
    changed on a running node), while the shipped `type: ClusterIP` has
    stripped every node port back off the Services. Returns the recorded
    `kubectl` argv list.
    """
    monkeypatch.setattr(ops, "_which", lambda _p: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(
        ops, "_kind_published_host_ports", lambda *_a, **_k: {3000, 8000, 3001, 8080, 9090, 16686}
    )
    monkeypatch.setattr(ops, "_k8s_service_node_ports", lambda _svc: set())
    monkeypatch.setattr(ops, "_probe_web_url", probe)
    monkeypatch.setattr(
        ops.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("cannot bind a host port the kind node holds"),
    )
    ran: list[list[str]] = []

    def fake_run(cmd, check=True, **_kw):
        ran.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="patched", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    return ran


def test_port_forward_republishes_a_mapped_port_the_service_stopped_serving(monkeypatch, capsys):
    """A mapped host port with no node port behind it is dark, not "already published".

    The owner's re-test failure in miniature, and the defect this guards: the
    partition used to read the node's mapping alone, so after anything that
    re-asserts the shipped ClusterIP Services (`kubectl apply -k k8s/`, a
    re-run of the observability apply) `nyxgpt ops port-forward` answered
    "already published ... no forward needed" about four UIs that were
    answering ERR_CONNECTION_REFUSED -- a command reporting success over an
    unreachable UI, which is the complaint #3986 opened with.

    A forward cannot fix it either: those host ports are held by the kind node
    container, so the bind would fail. Republishing the node port is the only
    recovery that works there, and it is the one that survives Pod replacement.
    """
    patched = _published_but_clusterip(monkeypatch)

    assert ops.port_forward(_port_forward_args(target="observability")) == 0

    out = capsys.readouterr().out
    assert "already published at http://127.0.0.1:3001" not in out
    assert "lost their node port" in out
    assert "http://127.0.0.1:3001 is served by the cluster again" in out

    # One patch per SRE Service, onto the node port the cluster maps -- read
    # from the product's table so the test cannot drift from the mapping.
    assert [cmd[cmd.index("svc") + 1] for cmd in patched if "patch" in cmd] == [
        entry.service for _host, entry in sorted(ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES.items())
    ]
    for host, entry in sorted(ops.K8S_OBSERVABILITY_PUBLISHED_SERVICES.items()):
        assert any(
            entry.service in cmd and f'"nodePort": {entry.node_port}' in cmd[-1] for cmd in patched
        ), f"{host} was not republished on node port {entry.node_port}"


def test_port_forward_fails_when_a_republished_url_stays_silent(monkeypatch, capsys):
    """Verified, not asserted -- the same standard every other access path here meets.

    `ops port-forward`'s promise is that the UI is reachable when it returns.
    A republished node port that never answers means something else is wrong
    (the Pods, kube-proxy), and saying so beats a green line over a dark UI.
    """
    _published_but_clusterip(monkeypatch, probe=lambda _url, **_kw: "ConnectError: refused")

    assert ops.port_forward(_port_forward_args(target="grafana")) == 2

    out = capsys.readouterr().out
    assert "[FAIL] Republished http://127.0.0.1:3001 but it did not answer" in out


def test_no_mapped_host_port_is_ever_routed_to_a_forward(monkeypatch):
    """Whatever the Services look like, a mapped port is never handed to kubectl.

    The structural fact behind the partition: `kubectl port-forward` cannot
    bind a host port the kind node container publishes, so routing one to a
    forward is a guaranteed `address already in use`. Holds in both
    directions -- Services serving their node ports, and Services back to
    ClusterIP.
    """
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    mapped = {host for host, _node in ops.KIND_HOST_PORT_MAPPINGS}
    monkeypatch.setattr(ops, "_kind_published_host_ports", lambda *_a, **_k: mapped)

    for node_ports in (_node_ports_as_published, lambda _svc: set()):
        monkeypatch.setattr(ops, "_k8s_service_node_ports", node_ports)
        plan = ops._port_forward_plan(ops._PortForwardArgs(target="app,observability"))
        assert plan is not None
        partition = ops._partition_published_targets(plan)
        assert not [row for row in partition.forward if row[2] in mapped]
        # ...and nothing is dropped on the floor: every row is accounted for.
        assert len(partition.forward) + len(partition.served) + len(partition.republish) == len(
            plan
        )

    # A bring-your-own cluster publishes nothing, so the plan passes through
    # untouched and there is nothing for nyxGPT to republish on it.
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")
    plan = ops._port_forward_plan(ops._PortForwardArgs(target="observability"))
    assert ops._partition_published_targets(plan) == ops._PortForwardPartition(plan, [], [])


def test_service_node_ports_reads_the_live_service(monkeypatch):
    """Empty for ClusterIP, a missing Service, and any kubectl failure."""
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **_kw):
        calls.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="31668 4317 4318\n", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    assert ops._k8s_service_node_ports("jaeger") == {31668, 4317, 4318}
    assert calls[0][-1] == "jsonpath={.spec.ports[*].nodePort}"
    assert "jaeger" in calls[0]

    # ClusterIP: the jsonpath selects nothing and kubectl prints an empty string.
    monkeypatch.setattr(
        ops, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    assert ops._k8s_service_node_ports("grafana") == set()

    # No such Service / no cluster: "not serving" is the conservative answer,
    # and acting on it re-establishes the access path.
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="NotFound"),
    )
    assert ops._k8s_service_node_ports("grafana") == set()


def test_the_sre_tier_is_published_after_glitchtip_provisioning():
    """Ordering: `_k8s_provision_glitchtip` rolls the DSN consumers.

    Probing the SRE UIs while that restart is in flight would report a false
    negative about the one thing this step exists to prove.
    """
    import inspect

    source = inspect.getsource(ops._install_kubernetes_steps)
    assert source.index("_wait_for_k8s_observability") < source.index(
        "_ensure_k8s_observability_host_access"
    )
    assert source.index("_k8s_provision_glitchtip") < source.index(
        "_ensure_k8s_observability_host_access"
    )

    # ...and deploying the tier on its own owes the same promise: the
    # dashboard's observability links point at these exact ports.
    assert "_ensure_k8s_observability_host_access" in inspect.getsource(
        ops.observability_kubernetes
    )


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


def _infra_status_in_cluster(
    monkeypatch,
    *,
    in_cluster: bool,
    pods: list[str],
    record: dict[str, str] | None = None,
    version: str = "3.0.0rc13",
    context: str = "",
):
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
    # The gate the issue is about: a Pod's `kubectl config current-context` is
    # empty. An off-cluster caller passes a `context` to stand in for a host
    # with a kubeconfig pointed at the deployment.
    monkeypatch.setattr(ops, "_kubectl_context", lambda: context)
    monkeypatch.setattr(
        ops,
        "_k8s_pod_states",
        lambda *a, **k: (
            [ops.K8sWorkloadState(name, ops.K8S_STATE_READY, "1/1 Running") for name in pods],
            None,
        ),
    )
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})
    # The deployment's own install record (#3988, second round) -- the cluster
    # ConfigMap `ops install --kubernetes` writes. `{}` is a cluster that
    # carries none.
    monkeypatch.setattr(ops, "_read_k8s_install_record", lambda: dict(record or {}))
    monkeypatch.setattr(ops, "running_version", lambda: version)
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


# --- #3988, second round: version and install mode from inside the cluster ---
#
# The re-test (2026-08-26) passed detection and the scope statements, and
# failed AC1's other two subjects: the `kubernetes` section carried no
# `version` field at all, and reported `install_mode.mode: "artifact"` for a
# deployment the owner had installed with `--dev` -- beside a `label` that
# said "unrecorded". Both have the same cause: the only record consulted was a
# marker file in the installing machine's `~/.nyxGPT`, which inside a Pod is
# the container's own empty home.


def test_infra_status_reports_the_version_it_is_serving_from_inside_the_cluster(monkeypatch):
    """The Definition of Done's "what version", which the card did not answer.

    Never a vantage-point limit: in-cluster the api process serving this page
    IS this deployment's api, so its own version is the version serving now.
    """
    status = _infra_status_in_cluster(
        monkeypatch, in_cluster=True, pods=["nyxgpt-api-stable-1"], version="3.0.0rc13"
    )

    assert status["kubernetes"]["version"]["known"] is True
    assert status["kubernetes"]["version"]["version"] == "3.0.0rc13"
    assert status["kubernetes"]["version"]["channel"] == "rc"
    assert "this api process" in status["kubernetes"]["version"]["source"]


def test_infra_status_reports_the_dev_install_mode_recorded_in_the_cluster(monkeypatch):
    """The owner's `--dev` cluster, read from the cluster rather than from a host."""
    status = _infra_status_in_cluster(
        monkeypatch,
        in_cluster=True,
        pods=["nyxgpt-api-stable-1"],
        record={"mode": "dev", "checkout": "/Users/o/src/nyxGPT", "version": "3.0.0rc13"},
    )

    install = status["kubernetes"]["install_mode"]
    assert install["recorded"] is True
    assert install["mode"] == "dev"
    assert install["checkout"] == "/Users/o/src/nyxGPT"
    assert "dev (images built from the working tree" in install["label"]
    assert f"configmap/{ops.K8S_INSTALL_RECORD_CONFIGMAP}" in install["source"]


def test_infra_status_prefers_the_recorded_channel_over_a_derived_one(monkeypatch):
    """A `--dev` deployment runs a working tree, whatever its version parses as.

    The checkout's version string is a perfectly good `rc`, and reporting that
    would send an operator hunting a published candidate that does not exist
    (#3982) -- so the install's own answer wins.
    """
    status = _infra_status_in_cluster(
        monkeypatch,
        in_cluster=False,
        pods=["nyxgpt-api-stable-1"],
        context="kind-nyxgpt-local",
        record={"mode": "dev", "version": "3.0.0rc13", "channel": "dev"},
    )

    assert status["kubernetes"]["version"]["version"] == "3.0.0rc13"
    assert status["kubernetes"]["version"]["channel"] == "dev"


def test_infra_status_never_calls_an_unrecorded_kubernetes_mode_artifact(monkeypatch):
    """`mode` must not answer `artifact` while `recorded` is false (#3988, D-032).

    Non-vacuous by construction: a marker IS written here, in the home this
    process can see -- which from inside a Pod is the container's own, i.e. a
    record of a deployment somewhere else. The honest answers are `unrecorded`
    and "no version".
    """
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )

    status = _infra_status_in_cluster(
        monkeypatch, in_cluster=True, pods=["nyxgpt-api-stable-1"], version=""
    )

    install = status["kubernetes"]["install_mode"]
    assert install["recorded"] is False
    assert install["mode"] == "unrecorded"
    assert install["source"] == ""
    assert "unrecorded" in install["label"]
    # And no version invented from the container's own absent metadata.
    assert status["kubernetes"]["version"] == {
        "known": False,
        "version": "",
        "channel": "unknown",
        "source": "",
    }


def test_infra_status_falls_back_to_the_local_marker_off_cluster(monkeypatch):
    """On a host, the marker is still the record -- a pre-#3988 deployment answers."""
    marker = install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, "/co", substrate=install_mode.SUBSTRATE_KUBERNETES
    )

    status = _infra_status_in_cluster(monkeypatch, in_cluster=False, pods=["nyxgpt-api-stable-1"])

    install = status["kubernetes"]["install_mode"]
    assert install["recorded"] is True
    assert install["mode"] == "dev"
    assert str(marker) in install["source"]


def test_write_k8s_install_record_applies_a_configmap_the_cluster_keeps(monkeypatch):
    """The write half: the record lands in the deployment's own namespace."""
    applied: dict[str, object] = {}

    def fake_run(cmd, check=True, input=None, **_kwargs):
        applied["cmd"] = cmd
        applied["manifest"] = json.loads(input)
        return SimpleNamespace(returncode=0, stdout="configmap/nyxgpt-install-mode configured")

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "running_version", lambda: "3.0.0rc13")

    result = ops._write_k8s_install_record("dev", "/Users/o/src/nyxGPT")

    assert result.ok is True
    assert applied["cmd"] == ["kubectl", "-n", ops.K8S_NAMESPACE, "apply", "-f", "-"]
    manifest = applied["manifest"]
    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["name"] == ops.K8S_INSTALL_RECORD_CONFIGMAP
    assert manifest["metadata"]["namespace"] == ops.K8S_NAMESPACE
    assert manifest["data"] == {
        "mode": "dev",
        "checkout": "/Users/o/src/nyxGPT",
        "version": "3.0.0rc13",
        # Not `rc`: a working-tree build is no published channel (#3982).
        "channel": "dev",
    }


def test_install_records_the_mode_in_the_cluster_as_well_as_on_this_machine(monkeypatch, tmp_path):
    """Two readers, two records (#3988): this machine's CLI, and the deployment."""
    monkeypatch.setattr(ops, "_read_k8s_install_record", lambda: {})
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: tmp_path / "checkout")
    written: list[tuple[str, object]] = []
    monkeypatch.setattr(
        ops,
        "_write_k8s_install_record",
        lambda mode, checkout: (
            written.append((mode, checkout)),
            ops.OpsResult(True, "Recorded the install mode in the cluster"),
        )[1],
    )

    results = ops._record_k8s_install_mode(dev=True)

    assert all(r.ok for r in results)
    assert written == [("dev", tmp_path / "checkout")]
    assert install_mode.install_mode_file(install_mode.SUBSTRATE_KUBERNETES).exists()
    assert any("in the cluster" in r.message for r in results)


def test_install_rolls_the_app_tier_on_a_mode_change_recorded_only_in_the_cluster(monkeypatch):
    """The record the install must not ignore (#3988).

    `kubectl apply` on an unchanged `:local` image tag does not replace the
    Pods, so without consulting the cluster's own record an install from a
    second machine -- which has no marker -- would leave the app tier serving
    the previous mode's images while both records claimed the new one.
    """
    monkeypatch.setattr(ops, "_read_k8s_install_record", lambda: {"mode": "dev", "checkout": "/co"})
    monkeypatch.setattr(ops, "_write_k8s_install_record", lambda *_a: ops.OpsResult(True, "ok"))
    rolled: list[bool] = []
    monkeypatch.setattr(
        ops,
        "_restart_k8s_app_tier",
        lambda: (rolled.append(True), [ops.OpsResult(True, "rolled")])[1],
    )

    results = ops._record_k8s_install_mode(dev=False)

    assert rolled == [True]
    assert any("install mode changing: dev -> artifact" in r.message for r in results)


def test_read_k8s_install_record_treats_an_unreadable_record_as_no_record(monkeypatch):
    """An RBAC refusal, a deleted namespace and a pre-#3988 deployment are one answer.

    They are all "nothing recorded here", which the card reports as unknown --
    the one thing it must not do is turn any of them into a mode.
    """
    monkeypatch.setattr(
        ops, "_run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="forbidden")
    )
    assert ops._read_k8s_install_record() == {}

    monkeypatch.setattr(
        ops, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not json", stderr="")
    )
    assert ops._read_k8s_install_record() == {}

    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=json.dumps({"data": {"mode": "dev"}}), stderr=""
        ),
    )
    assert ops._read_k8s_install_record() == {"mode": "dev"}


def test_rbac_grants_the_install_record_read_by_name_only():
    """The read AC2 asks for, declared in `k8s/` and no wider (#3988).

    Scoped with `resourceNames` rather than opening the namespace's ConfigMaps:
    `k8s/configmap.yaml` and the deployment's Secret must stay unreadable
    through this Role, which is what its own comment promises.
    """
    role = next(
        doc
        for doc in yaml.safe_load_all((K8S_DIR / "rbac.yaml").read_text())
        if doc and doc.get("kind") == "Role"
    )
    rule = next(r for r in role["rules"] if "configmaps" in r["resources"])

    assert rule["verbs"] == ["get"]
    assert rule["resourceNames"] == [ops.K8S_INSTALL_RECORD_CONFIGMAP]


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
    """The install asserts the contract it applied, rather than assuming it (#3991).

    "Both" is the claim, and it is the right one: only components that HAVE a
    canary track can be off-contract. This used to assert
    `calls == list(canary.COMPONENTS)` -- all three, `ollama` included -- so
    the test's own name and its assertion disagreed, and the assertion won.
    That is what let the loop ask `ollama` to reset, collect its documented
    refusal, and redden every Kubernetes smoke.
    """
    calls: list[str] = []

    def fake_reset(namespace, *, component):
        calls.append(component)
        return ops.OpsResult(True, f"Reset {component} to its resting 0")

    monkeypatch.setattr(canary, "reset", fake_reset)

    results = ops._reconcile_k8s_canary_resting()

    assert calls == [k for k, v in canary.COMPONENTS.items() if v.supported]
    assert all(r.ok for r in results)


def test_install_never_asks_an_unsupported_component_to_reset(monkeypatch):
    """A component with no canary track cannot be off-contract (#4016 regression).

    `ollama` declares `supported=False` because a stable/canary split has no
    sound implementation for it (canary.OLLAMA_UNSUPPORTED_REASON). Asking it
    to reset returns that refusal, which the loop scored as an install
    failure -- so `nyxgpt ops install --kubernetes` exited non-zero on a
    perfectly healthy stack and k8s-local-smoke, k8s-artifact-smoke and
    k8s-observability-smoke all went red on v3.0.0.

    Keyed on the `supported` flag rather than the name `ollama`, so this holds
    for any future component that declares itself unsupported.
    """
    unsupported = [k for k, v in canary.COMPONENTS.items() if not v.supported]
    assert unsupported, "this test is vacuous unless some component is unsupported"

    def fake_reset(namespace, *, component):
        spec = canary.COMPONENTS[component]
        if not spec.supported:
            return ops.OpsResult(False, spec.unsupported_reason)
        return ops.OpsResult(True, f"Reset {component} to its resting 0")

    monkeypatch.setattr(canary, "reset", fake_reset)

    results = ops._reconcile_k8s_canary_resting()

    assert all(r.ok for r in results), [r.message for r in results if not r.ok]
    for name in unsupported:
        assert not any(name in r.message for r in results)


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
