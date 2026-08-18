"""Unit tests for the in-cluster Kubernetes observability layer (#3787).

Two halves:

* the manifests -- `k8s/observability/` must deploy the same observability
  tier the Compose profiles do, under Service names the SHARED Grafana
  provisioning already points at (that reuse is the design; a divergent
  name silently gives Grafana a datasource that resolves to nothing);
* the ops wiring -- `nyxgpt ops install/observability/down/status/
  port-forward --kubernetes` apply, report and tear it down, with no raw
  kubectl for the operator.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nyxgpt import ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_DIR = REPO_ROOT / "k8s" / "observability"


def _manifest_docs() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(OBSERVABILITY_DIR.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        docs += [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
    return docs


def _by_kind(kind: str) -> dict[str, dict]:
    return {doc["metadata"]["name"]: doc for doc in _manifest_docs() if doc["kind"] == kind}


# --- manifests -------------------------------------------------------------


def test_overlay_ships_every_compose_observability_component() -> None:
    """Kubernetes mode must not be a reduced observability tier: the same
    metrics/logs/traces/errors components the Compose profiles start."""
    workloads = set(_by_kind("Deployment")) | set(_by_kind("DaemonSet"))
    assert workloads == set(ops.K8S_OBSERVABILITY_DEPLOYMENTS) | set(
        ops.K8S_OBSERVABILITY_DAEMONSETS
    )


def test_service_names_match_grafana_datasource_urls() -> None:
    """The overlay reuses `docker/grafana/provisioning` verbatim, so each
    datasource URL's host must be an in-cluster Service of that exact name."""
    services = set(_by_kind("Service"))
    datasource_dir = REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources"
    for path in datasource_dir.glob("*.yml"):
        for datasource in yaml.safe_load(path.read_text())["datasources"]:
            host = datasource["url"].removeprefix("http://").split(":")[0]
            assert host in services, (
                f"Grafana datasource {datasource['name']} points at http://{host} but "
                "k8s/observability/ ships no Service by that name -- it would resolve "
                "to nothing in Kubernetes mode"
            )


def test_prometheus_scrapes_the_in_cluster_api_service() -> None:
    """The one config that CANNOT be shared with Compose: there is no host
    gateway in a cluster, so the scrape target is the api Service."""
    config = yaml.safe_load(_by_kind("ConfigMap")["prometheus-config"]["data"]["prometheus.yml"])
    targets = [t for job in config["scrape_configs"] for t in job["static_configs"][0]["targets"]]
    assert "nyxgpt-api:8000" in targets
    assert not any("host.docker.internal" in t for t in targets)


def test_promtail_keeps_the_nyxgpt_log_label_contract() -> None:
    """Pod discovery replaces file tailing, but the labels the dashboards and
    curated Explore links query on (`job`, `service_name`) must survive."""
    config = yaml.safe_load(_by_kind("ConfigMap")["promtail-config"]["data"]["promtail-config.yml"])
    scrape = config["scrape_configs"][0]
    assert config["clients"][0]["url"] == "http://loki:3100/loki/api/v1/push"
    assert scrape["kubernetes_sd_configs"] == [{"role": "pod"}]
    # CRI unwrapping must come first or nyxGPT's own log format never matches.
    assert scrape["pipeline_stages"][0] == {"cri": {}}
    targets = {rule.get("target_label") for rule in scrape["relabel_configs"]}
    assert {"job", "service_name", "__path__"} <= targets
    assert any(
        rule.get("action") == "keep" and rule.get("regex") == "nyxgpt"
        for rule in scrape["relabel_configs"]
    ), "promtail must only ship the nyxgpt namespace's logs"


def test_grafana_mounts_generated_provisioning_configmaps() -> None:
    """Grafana's provisioning comes from ConfigMaps `ops` generates out of
    docker/grafana -- the mount names and the generator table must agree."""
    grafana = _by_kind("Deployment")["grafana"]
    mounted = {
        volume["configMap"]["name"]
        for volume in grafana["spec"]["template"]["spec"]["volumes"]
        if "configMap" in volume
    }
    assert set(ops.K8S_GRAFANA_CONFIGMAPS) == mounted


def test_grafana_reads_its_file_secrets_from_the_bootstrapped_secret() -> None:
    """The `$__file{}` targets must exist and be non-empty or Grafana's
    alerting validator crash-loops the Pod (#3538)."""
    example = yaml.safe_load((OBSERVABILITY_DIR / "secret.example.yaml").read_text())
    assert example["stringData"]["slack-webhook-url"].strip()
    assert example["stringData"]["glitchtip-grafana-token"].strip()

    grafana = _by_kind("Deployment")["grafana"]
    secret_volume = next(
        volume
        for volume in grafana["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "nyxgpt-secrets"
    )
    paths = {item["path"] for item in secret_volume["secret"]["items"]}
    assert paths == {"slack-webhook-url", "glitchtip-grafana-token"}


def test_kustomization_lists_every_manifest() -> None:
    kustomization = yaml.safe_load((OBSERVABILITY_DIR / "kustomization.yaml").read_text())
    on_disk = {path.name for path in OBSERVABILITY_DIR.glob("*.yaml")} - {
        "kustomization.yaml",
        "secret.example.yaml",
    }
    # secret.yaml is bootstrapped by ops from the example, never committed.
    assert set(kustomization["resources"]) == on_disk | {"secret.yaml"}


# --- ops wiring ------------------------------------------------------------


def test_install_kubernetes_applies_the_observability_layer() -> None:
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_build_and_load_k8s_image", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=[ops.OpsResult(True, "ok")]),
        # #3786's in-cluster Cassandra/Ollama wait sits between the app tier
        # and the observability layer, and really polls a cluster.
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_k8s_stack_health", return_value=[]),
        patch.object(ops, "_k8s_observability_health", return_value=[]),
        patch.object(
            ops, "_apply_k8s_observability", return_value=[ops.OpsResult(True, "observability")]
        ) as apply_observability,
        # #3826's rollout wait, for the same reason as the data-tier one above.
        patch.object(ops, "_wait_for_k8s_observability", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None)

    apply_observability.assert_called_once()
    assert any(r.message == "observability" for r in results)


def test_install_kubernetes_honours_skip_observability() -> None:
    """`--skip-observability` used to be silently ignored in Kubernetes mode."""
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_build_and_load_k8s_image", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=[ops.OpsResult(True, "ok")]),
        # Patched for the same reason as above: otherwise the install stops at
        # the data-tier wait and this would assert nothing.
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_k8s_stack_health", return_value=[]),
        patch.object(ops, "_apply_k8s_observability") as apply_observability,
        patch.object(ops, "_wait_for_k8s_observability") as wait_observability,
        patch.object(ops, "_k8s_observability_health") as observability_health,
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None, skip_observability=True)

    assert all(r.ok for r in results)

    apply_observability.assert_not_called()
    wait_observability.assert_not_called()
    observability_health.assert_not_called()


def test_apply_observability_bootstraps_secret_then_applies(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ops, "K8S_OBSERVABILITY_DIR", tmp_path)
    (tmp_path / "secret.example.yaml").write_text(
        'stringData:\n  grafana-admin-password: "change-me"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        ops, "_k8s_observability_secret_values", lambda: {"grafana-admin-password": "s3cret"}
    )
    monkeypatch.setattr(
        ops, "_apply_k8s_grafana_provisioning", lambda: [ops.OpsResult(True, "configmaps")]
    )
    applied: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        applied.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._apply_k8s_observability()

    assert all(r.ok for r in results)
    assert 'grafana-admin-password: "s3cret"' in (tmp_path / "secret.yaml").read_text()
    assert applied == [["kubectl", "apply", "-k", str(tmp_path)]]


def test_observability_secret_is_not_rewritten_once_bootstrapped(tmp_path, monkeypatch) -> None:
    """Re-running install must not rotate GlitchTip's SECRET_KEY out from
    under the data already encrypted with it."""
    monkeypatch.setattr(ops, "K8S_OBSERVABILITY_DIR", tmp_path)
    (tmp_path / "secret.yaml").write_text("existing", encoding="utf-8")

    results = ops._ensure_k8s_observability_secret()

    assert all(r.ok for r in results)
    assert (tmp_path / "secret.yaml").read_text() == "existing"


def test_grafana_provisioning_restarts_grafana_only_when_changed(tmp_path, monkeypatch) -> None:
    """Grafana reads provisioning at startup only -- but bouncing it on every
    install would be gratuitous, so the restart follows kubectl's own verdict."""
    grafana_dir = tmp_path / "grafana"
    for parts in ops.K8S_GRAFANA_CONFIGMAPS.values():
        grafana_dir.joinpath(*parts).mkdir(parents=True)
    monkeypatch.setattr(ops, "OPS_DOCKER_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        if "create" in cmd:
            return MagicMock(returncode=0, stdout="rendered", stderr="")
        return MagicMock(returncode=0, stdout="configmap/x unchanged", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    with patch.object(ops, "_restart_k8s_grafana") as restart:
        ops._apply_k8s_grafana_provisioning()
    restart.assert_not_called()

    def changed_run(cmd, **kwargs):
        if "create" in cmd:
            return MagicMock(returncode=0, stdout="rendered", stderr="")
        return MagicMock(returncode=0, stdout="configmap/x configured", stderr="")

    monkeypatch.setattr(ops, "_run", changed_run)
    with patch.object(
        ops, "_restart_k8s_grafana", return_value=ops.OpsResult(True, "restarted")
    ) as restart:
        ops._apply_k8s_grafana_provisioning()
    restart.assert_called_once()


def test_delete_observability_is_a_noop_when_never_bootstrapped(tmp_path, monkeypatch) -> None:
    """The kustomization references secret.yaml, so a `kubectl delete -k` on a
    cluster that never had it fails on the missing FILE, not on the cluster."""
    monkeypatch.setattr(ops, "K8S_OBSERVABILITY_DIR", tmp_path)
    with patch.object(ops, "_run") as run:
        results = ops._delete_k8s_observability()
    run.assert_not_called()
    assert all(r.ok for r in results)


def test_down_kubernetes_deletes_the_observability_layer(monkeypatch) -> None:
    monkeypatch.setattr(ops, "_ensure_nyxgpt_bin_on_path", lambda: None)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")
    monkeypatch.setattr(ops, "_run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    with patch.object(
        ops, "_delete_k8s_observability", return_value=[ops.OpsResult(True, "deleted")]
    ) as delete:
        ops._down_kubernetes_steps()
    delete.assert_called_once()


def test_down_kubernetes_tears_down_an_observability_only_cluster(tmp_path, monkeypatch) -> None:
    """`ops observability --kubernetes` can deploy the layer with no app tier
    at all -- and then there is no k8s/secret.yaml, which the app-tier
    kustomization references, so `kubectl delete -k k8s/` would fail on the
    missing FILE and never reach the observability delete."""
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path)  # no secret.yaml in it
    monkeypatch.setattr(ops, "_ensure_nyxgpt_bin_on_path", lambda: None)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "docker-desktop")
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: ran.append(cmd) or MagicMock(returncode=0, stdout="", stderr=""),
    )
    with patch.object(
        ops, "_delete_k8s_observability", return_value=[ops.OpsResult(True, "deleted")]
    ) as delete:
        results = ops._down_kubernetes_steps()

    assert all(r.ok for r in results)
    assert ran == [], "no app tier means no app-tier delete to run"
    delete.assert_called_once()


def test_workload_state_reports_absent_and_partial_readiness(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        if "deploy" in cmd:
            # grafana has no ready replica yet; loki is absent from the output.
            return MagicMock(returncode=0, stdout="prometheus=1/1;grafana=/1;", stderr="")
        return MagicMock(returncode=0, stdout="promtail=1/1;", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    state = ops._k8s_observability_workload_state()

    assert state["prometheus"] == "1/1 ready"
    assert state["grafana"] == "0/1 ready"
    assert state["loki"] == "absent"
    assert state["promtail"] == "1/1 ready"


def test_infra_status_reports_the_observability_layer(monkeypatch) -> None:
    """The admin dashboard's Infrastructure page renders straight off this."""
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: SimpleNamespace(native={}, compose={}, conflicts=set()),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", dict)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: ops.KIND_CONTEXT)
    monkeypatch.setattr(ops, "_run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(ops.self_heal, "compose_probe_available", lambda: True)
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {"grafana": "1/1 ready"})

    observability = ops.infra_status()["kubernetes"]["observability"]

    assert observability["deployed"] is True
    assert observability["workloads"] == {"grafana": "1/1 ready"}
    # Command wrapping: the dashboard tells the operator a `nyxgpt` command.
    assert observability["port_forward_command"].startswith("nyxgpt ops port-forward")


# --- port-forward ----------------------------------------------------------


def test_port_forward_defaults_to_web_unchanged() -> None:
    plan = ops._port_forward_plan(SimpleNamespace(target="web", port=None))
    assert plan == [("web", "nyxgpt-web", 3000, 3000)]


def test_port_forward_observability_uses_the_dashboard_ports() -> None:
    """The whole point: Grafana lands on 3001, Jaeger on 16686 and GlitchTip
    on 8080 -- the ports [monitoring] grafana_ui_url and the dashboard's
    observability links already default to, so they work unchanged here."""
    plan = ops._port_forward_plan(SimpleNamespace(target="observability", port=None))
    assert plan == [
        ("grafana", "grafana", 3001, 3000),
        ("prometheus", "prometheus", 9090, 9090),
        ("jaeger", "jaeger", 16686, 16686),
        ("glitchtip", "glitchtip", 8080, 8080),
    ]


def test_port_forward_rejects_port_override_for_multiple_targets(capsys) -> None:
    assert ops._port_forward_plan(SimpleNamespace(target="observability", port=1234)) is None
    assert "--port cannot be combined" in capsys.readouterr().err


def test_port_forward_rejects_unknown_target(capsys) -> None:
    assert ops._port_forward_plan(SimpleNamespace(target="nope", port=None)) is None
    assert "unknown --target" in capsys.readouterr().err


def test_observability_command_routes_kubernetes_to_the_cluster() -> None:
    with (
        patch.object(
            ops, "observability_kubernetes", return_value=[ops.OpsResult(True, "applied")]
        ) as apply_k8s,
        patch.object(ops, "_reconcile_grafana_provisioning") as compose_path,
        patch.object(ops, "_record_ops_action"),
    ):
        rc = ops.observability(
            SimpleNamespace(kubernetes=True, local=True, cloud=False, quiet=True)
        )

    assert rc == 0
    apply_k8s.assert_called_once()
    compose_path.assert_not_called()


def test_observability_kubernetes_requires_local() -> None:
    with (
        patch.object(ops, "observability_kubernetes") as apply_k8s,
        patch.object(ops, "_record_ops_action"),
    ):
        rc = ops.observability(
            SimpleNamespace(kubernetes=True, local=False, cloud=False, quiet=True)
        )

    assert rc == 2
    apply_k8s.assert_not_called()


# --- the rollout wait the default install depends on (#3826) ---------------


def _rollout_refs(calls: list[list[str]]) -> list[str]:
    """The workload refs `kubectl rollout status` was asked about, in order."""
    return [cmd[cmd.index("status") + 1] for cmd in calls if "rollout" in cmd]


def test_wait_for_k8s_observability_waits_for_every_workload(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="rolled out", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability()

    assert all(r.ok for r in results)
    assert _rollout_refs(calls) == [
        *(f"deploy/{name}" for name in ops.K8S_OBSERVABILITY_DEPLOYMENTS),
        *(f"daemonset/{name}" for name in ops.K8S_OBSERVABILITY_DAEMONSETS),
    ]
    # One shared deadline, not a per-workload timeout: the budget left shrinks
    # as the wait proceeds, so ten workloads cannot cost ten budgets.
    timeouts = [int(arg.split("=")[1][:-1]) for cmd in calls for arg in cmd if "--timeout=" in arg]
    assert timeouts and all(0 < t <= ops.K8S_OBSERVABILITY_ROLLOUT_BUDGET_S for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)


def _advancing_clock(monkeypatch, step: float = 30.0) -> None:
    """Make `time.monotonic` advance by `step` on every read.

    The rollout wait polls in slices, so a fixed clock would spin forever;
    an advancing one drains any budget in a bounded number of iterations.
    """
    ticks = {"t": 0.0}

    def now() -> float:
        ticks["t"] += step
        return ticks["t"]

    monkeypatch.setattr(ops.time, "monotonic", now)


def test_wait_for_k8s_observability_fails_naming_the_workload(monkeypatch) -> None:
    """A workload that never rolls out is a failure, not a warning -- an
    operator told "installed" by a command that left Prometheus Pending has
    been told the wrong thing."""
    calls: list[list[str]] = []
    _advancing_clock(monkeypatch)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "deploy/grafana" in cmd:
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        return MagicMock(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability()

    assert not results[-1].ok
    assert "deploy/grafana did not become ready in time" in results[-1].message
    # Stops at the first failure: the refs after grafana are never waited on.
    assert _rollout_refs(calls)[-1] == "deploy/grafana"


def test_wait_for_k8s_observability_reports_a_rollout_it_could_not_check(monkeypatch) -> None:
    """`rollout status` failing for a reason waiting does not fix (no such
    object, unreachable cluster) is reported now, not after the budget."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        rc = 1 if "deploy/grafana" in cmd else 0
        return MagicMock(returncode=rc, stdout="", stderr='Error from server (NotFound): "grafana"')

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability()

    assert not results[-1].ok
    assert "deploy/grafana: could not check rollout" in results[-1].message
    assert "NotFound" in results[-1].details
    # One attempt, not a poll loop: this failure does not resolve by waiting.
    assert _rollout_refs(calls).count("deploy/grafana") == 1


def test_wait_for_k8s_observability_stops_when_the_budget_is_spent(monkeypatch) -> None:
    _advancing_clock(monkeypatch, step=500.0)
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: MagicMock(
            returncode=1, stdout="", stderr="error: timed out waiting for the condition"
        ),
    )

    results = ops._wait_for_k8s_observability(budget_s=900)

    assert not results[-1].ok
    assert "did not become ready in time" in results[-1].message
    assert "900s in total" in results[-1].details


def test_wait_for_k8s_observability_fails_fast_on_an_unschedulable_pod(monkeypatch) -> None:
    """#3827's real failure: prometheus could not be scheduled (`Insufficient
    memory`). The wait must say so within a slice or two rather than spend the
    whole 900s budget and then blame whichever workload it happened to be on."""
    _advancing_clock(monkeypatch)
    rollout_attempts: list[str] = []

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            rollout_attempts.append(cmd[cmd.index("status") + 1])
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        return MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "prometheus-abc"},
                            "status": {
                                "phase": "Pending",
                                "conditions": [
                                    {
                                        "type": "PodScheduled",
                                        "status": "False",
                                        "reason": "Unschedulable",
                                        "message": "0/1 nodes are available: Insufficient memory.",
                                    }
                                ],
                            },
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability()

    assert not results[-1].ok
    assert "pod prometheus-abc: Pending: unschedulable" in results[-1].message
    assert "Insufficient memory" in results[-1].details
    # Confirmed over two slices, then abandoned -- nowhere near the budget.
    assert len(rollout_attempts) == ops.K8S_BLOCKED_CONFIRMATIONS


def test_wait_for_k8s_observability_tolerates_a_one_off_blocked_reading(monkeypatch) -> None:
    """A single `ImagePullBackOff` reading can be a registry hiccup the next
    kubelet retry clears; one confirmation slice keeps the wait from aborting
    a rollout that was about to succeed."""
    _advancing_clock(monkeypatch)
    seen = {"polls": 0}

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            if seen["polls"] == 0:
                seen["polls"] += 1
                return MagicMock(
                    returncode=1, stdout="", stderr="error: timed out waiting for the condition"
                )
            return MagicMock(returncode=0, stdout="rolled out", stderr="")
        return MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "prometheus-abc"},
                            "status": {
                                "phase": "Pending",
                                "containerStatuses": [
                                    {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
                                ],
                            },
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability()

    assert all(r.ok for r in results)


def test_install_waits_for_observability_before_reading_pod_phases() -> None:
    """The ordering is the whole point (#3826).

    `_k8s_stack_health` reads Pod *phase* for every Pod in the namespace, and
    the observability Pods land in that same namespace -- so reading it while
    they are still pulling images reports a healthy stack as failed. That is
    why the smoke used to pass `--skip-observability` and test a configuration
    no user runs.
    """
    order: list[str] = []
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=ok),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
        patch.object(
            ops,
            "_wait_for_k8s_observability",
            side_effect=lambda: (order.append("wait"), ok)[1],
        ),
        patch.object(ops, "_k8s_stack_health", side_effect=lambda: (order.append("health"), ok)[1]),
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None)

    assert all(r.ok for r in results)
    assert order == ["wait", "health"]


def _pods_run(pods: list[dict]):
    """A `_run` stand-in answering `kubectl get pods -o json` with `pods`."""

    def fake_run(cmd, **kwargs):
        if "pods" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"items": pods}), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return fake_run


def _pod(name: str, phase: str, **status) -> dict:
    return {"metadata": {"name": name}, "status": {"phase": phase, **status}}


def test_k8s_stack_health_reports_a_pod_still_pulling_its_image_as_pending(monkeypatch) -> None:
    """#3827: `Pending` is pending, not `[FAIL]`.

    The install run that produced the issue printed ten `[FAIL] pod ...:
    Pending` lines for Pods that were all Running three minutes later, and
    the one genuinely broken Pod was lost among them."""
    monkeypatch.setattr(
        ops,
        "_run",
        _pods_run(
            [
                _pod(
                    "nyxgpt-api-stable-1",
                    "Running",
                    conditions=[{"type": "Ready", "status": "True"}],
                ),
                _pod(
                    "prometheus-abc",
                    "Pending",
                    containerStatuses=[{"state": {"waiting": {"reason": "ContainerCreating"}}}],
                ),
            ]
        ),
    )

    results = ops._k8s_stack_health()
    pending = [r for r in results if "prometheus-abc" in r.message]

    assert pending and pending[0].ok, "a Pod pulling its image is not an install failure"
    assert ops._result_status_label(pending[0]) == "PENDING"
    assert "Pending: ContainerCreating" in pending[0].message


def test_k8s_stack_health_fails_a_pod_that_cannot_be_scheduled(monkeypatch) -> None:
    """...and the non-transient one still fails, distinctly (#3827)."""
    monkeypatch.setattr(
        ops,
        "_run",
        _pods_run(
            [
                _pod(
                    "prometheus-abc",
                    "Pending",
                    conditions=[
                        {
                            "type": "PodScheduled",
                            "status": "False",
                            "reason": "Unschedulable",
                            "message": "0/1 nodes are available: Insufficient memory.",
                        }
                    ],
                ),
                _pod(
                    "loki-def",
                    "Pending",
                    containerStatuses=[{"state": {"waiting": {"reason": "ContainerCreating"}}}],
                ),
            ]
        ),
    )

    results = ops._k8s_stack_health()
    by_pod = {r.message.split(":")[0]: r for r in results if r.message.startswith("pod ")}

    assert not by_pod["pod prometheus-abc"].ok
    assert "unschedulable" in by_pod["pod prometheus-abc"].message
    assert "Insufficient memory" in by_pod["pod prometheus-abc"].details
    # The distinction the issue asks for: the other Pending Pod is untouched.
    assert by_pod["pod loki-def"].ok


def test_k8s_stack_health_fails_a_crashlooping_pod(monkeypatch) -> None:
    """`Running` is not a synonym for healthy: a container in CrashLoopBackOff
    keeps its Pod in the Running phase forever."""
    monkeypatch.setattr(
        ops,
        "_run",
        _pods_run(
            [
                _pod(
                    "nyxgpt-api-stable-1",
                    "Running",
                    conditions=[{"type": "Ready", "status": "False"}],
                    containerStatuses=[
                        {
                            "state": {
                                "waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}
                            }
                        }
                    ],
                )
            ]
        ),
    )

    results = ops._k8s_stack_health()
    pod = next(r for r in results if "nyxgpt-api-stable-1" in r.message)

    assert not pod.ok
    assert "CrashLoopBackOff" in pod.message


def test_k8s_stack_health_and_observability_health_agree_on_zero_ready(monkeypatch) -> None:
    """The contradiction #3827 was filed for: one command printed `[FAIL] pod
    grafana-x: Pending` and `[OK] observability grafana: 0/1 ready` about the
    same condition. Both halves now call it PENDING."""
    monkeypatch.setattr(
        ops,
        "_run",
        _pods_run(
            [
                _pod(
                    "grafana-x",
                    "Pending",
                    containerStatuses=[{"state": {"waiting": {"reason": "ContainerCreating"}}}],
                )
            ]
        ),
    )
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {"grafana": "0/1 ready"})

    pod = next(r for r in ops._k8s_stack_health() if "grafana-x" in r.message)
    workload = next(r for r in ops._k8s_observability_health() if "grafana" in r.message)

    assert ops._result_status_label(pod) == ops._result_status_label(workload) == "PENDING"
    assert pod.ok and workload.ok


def test_observability_health_fails_an_absent_workload(monkeypatch) -> None:
    monkeypatch.setattr(
        ops,
        "_k8s_observability_workload_state",
        lambda: {"grafana": "1/1 ready", "loki": "absent"},
    )

    results = ops._k8s_observability_health()
    by_name = {r.message: r for r in results}

    assert by_name["observability grafana: 1/1 ready"].ok
    assert not by_name["observability loki: absent"].ok
    assert any("missing from the cluster" in r.message and not r.ok for r in results)


def test_observability_command_waits_for_the_rollout_before_reporting_health() -> None:
    """`nyxgpt ops observability --kubernetes --local` has the same contract as
    the install: it returns when the layer works, not when the objects were
    accepted."""
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok) as wait,
        patch.object(ops, "_k8s_observability_health", return_value=ok) as health,
    ):
        results = ops.observability_kubernetes()

    assert all(r.ok for r in results)
    wait.assert_called_once()
    health.assert_called_once()


def test_observability_command_reports_a_failed_rollout_without_claiming_health() -> None:
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_apply_k8s_observability", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops,
            "_wait_for_k8s_observability",
            return_value=[ops.OpsResult(False, "deploy/loki never became ready")],
        ),
        patch.object(ops, "_k8s_observability_health") as health,
    ):
        results = ops.observability_kubernetes()

    assert not all(r.ok for r in results)
    health.assert_not_called()
