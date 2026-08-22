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

import contextlib
import json
import os
import re
import subprocess
import sys
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nyxgpt import ops
from nyxgpt.config import get_error_tracking_config, get_tracing_config

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
        patch.object(
            ops, "_build_and_load_k8s_api_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        # #3825's capacity preflight really reads a cluster and really
        # bootstraps the observability Secret to render it; neither belongs
        # in a unit test of the step ORDER.
        patch.object(ops, "_preflight_k8s_capacity", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=[ops.OpsResult(True, "ok")]),
        # #3786's in-cluster Cassandra/Ollama wait sits between the app tier
        # and the observability layer, and really polls a cluster.
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_k8s_stack_health", return_value=[]),
        # #3990's provisioning step execs into a Pod and opens a port-forward;
        # it is a step of this install like any other, so it is patched out of
        # a test about step ORDER.
        patch.object(ops, "_k8s_provision_glitchtip", return_value=[ops.OpsResult(True, "ok")]),
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
        patch.object(
            ops, "_build_and_load_k8s_api_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(
            ops, "_build_and_load_k8s_web_image", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_ensure_k8s_secret", return_value=[ops.OpsResult(True, "ok")]),
        # #3825's capacity preflight really reads a cluster and really
        # bootstraps the observability Secret to render it; neither belongs
        # in a unit test of the step ORDER.
        patch.object(ops, "_preflight_k8s_capacity", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=[ops.OpsResult(True, "ok")]),
        # Patched for the same reason as above: otherwise the install stops at
        # the data-tier wait and this would assert nothing.
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_k8s_stack_health", return_value=[]),
        patch.object(ops, "_apply_k8s_observability") as apply_observability,
        patch.object(ops, "_wait_for_k8s_observability") as wait_observability,
        patch.object(ops, "_k8s_provision_glitchtip") as provision_glitchtip,
        patch.object(ops, "_k8s_observability_health") as observability_health,
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None, skip_observability=True)

    assert all(r.ok for r in results)

    apply_observability.assert_not_called()
    wait_observability.assert_not_called()
    # There is no GlitchTip to provision when the layer was never deployed
    # (#3990) -- the step belongs to the observability tail, not the app tier.
    provision_glitchtip.assert_not_called()
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
    monkeypatch.setattr(
        ops,
        "_k8s_observability_workload_state",
        lambda: {"grafana": "1/1 ready", "prometheus": "0/1 ready", "loki": "absent"},
    )

    observability = ops.infra_status()["kubernetes"]["observability"]

    assert observability["deployed"] is True
    assert observability["workloads"]["grafana"] == "1/1 ready"
    # Command wrapping: the dashboard tells the operator a `nyxgpt` command.
    assert observability["port_forward_command"].startswith("nyxgpt ops port-forward")

    # ...and the same three states the Pod badges use (#3827). The raw
    # `workloads` map rendered as undifferentiated grey text, so a workload
    # that is up, one still rolling out and one that never deployed were
    # indistinguishable on a card that badges every Pod READY/PENDING/FAILED.
    by_name = {w["name"]: w for w in observability["workload_states"]}
    assert by_name["grafana"]["state"] == ops.K8S_STATE_READY
    assert by_name["prometheus"]["state"] == ops.K8S_STATE_PENDING
    assert by_name["loki"]["state"] == ops.K8S_STATE_FAILED
    assert by_name["prometheus"]["summary"] == "0/1 ready"


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


def test_observability_kubernetes_defaults_to_local() -> None:
    """No locality flag targets the local cluster rather than refusing (#3948)."""
    with (
        patch.object(
            ops, "observability_kubernetes", return_value=[ops.OpsResult(True, "applied")]
        ) as apply_k8s,
        patch.object(ops, "_record_ops_action"),
    ):
        rc = ops.observability(
            SimpleNamespace(kubernetes=True, local=False, cloud=False, quiet=True)
        )

    assert rc == 0
    apply_k8s.assert_called_once()


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
        if any("jsonpath" in arg for arg in cmd):
            # The workload's own label selector -- the wait only fast-fails on
            # Pods it can attribute to the workload it is waiting for.
            return MagicMock(returncode=0, stdout='{"app": "prometheus"}', stderr="")
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
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout='{"app": "prometheus"}', stderr="")
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


def test_wait_only_fast_fails_on_its_own_workloads_pods(monkeypatch) -> None:
    """Every tier shares the `nyxgpt` namespace, and the api Pods restart
    against their liveness probe while Cassandra is still bootstrapping. A
    wait that scanned the whole namespace would abort the *data tier's* wait
    over that and report Cassandra broken -- a false failure of exactly the
    kind #3827 is about. So the blocked-Pod scan is label-scoped."""
    _advancing_clock(monkeypatch)
    selectors: list[str] = []

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout='{"app": "cassandra"}', stderr="")
        # The scan is filtered, and the (foreign, crash-looping) api Pod is
        # not in the filtered answer -- which is what a real cluster returns.
        selectors.append(cmd[cmd.index("-l") + 1] if "-l" in cmd else "")
        return MagicMock(returncode=0, stdout=json.dumps({"items": []}), stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_data_tier()

    assert selectors and all(s == "app=cassandra" for s in selectors)
    # It ran out of budget rather than blaming a Pod belonging to another tier.
    assert "did not become ready in time" in results[-1].message


def test_wait_declines_to_fast_fail_when_the_selector_is_unreadable(monkeypatch) -> None:
    """No selector means no Pod can be attributed to this workload, and a wait
    must never invent a failure out of a Pod that may belong to something
    else -- it waits its budget out instead."""
    _advancing_clock(monkeypatch)
    scanned: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=1, stdout="", stderr="NotFound")
        scanned.append(cmd)
        return MagicMock(returncode=0, stdout=json.dumps({"items": []}), stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_observability(budget_s=900)

    assert not scanned, "no selector -- no Pod scan at all, rather than an unscoped one"
    assert "did not become ready in time" in results[-1].message


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
        patch.object(ops, "_build_and_load_k8s_api_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_provision_glitchtip", return_value=ok),
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
    # The #3990 data-flow probes really exec into the grafana Pod; this test is
    # about the readiness half of the report.
    monkeypatch.setattr(ops, "_k8s_observability_data_flow", lambda state=None: [])

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
        patch.object(ops, "_k8s_provision_glitchtip", return_value=ok) as provision,
        patch.object(ops, "_k8s_observability_health", return_value=ok) as health,
    ):
        results = ops.observability_kubernetes()

    assert all(r.ok for r in results)
    wait.assert_called_once()
    # Deploying the layer without provisioning GlitchTip leaves Grafana on the
    # placeholder token and the api with no DSN (#3990) -- a tier that runs
    # and observes nothing, which is the state this command must not produce.
    provision.assert_called_once()
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


# --- Per-workload wait budgets, and the reasons a wait may not fail fast (#3827) ---


def _budget_recording_run(budgets: list[int]):
    """A `_run` that records each `rollout status --timeout=Ns` slice it is asked for.

    The slice is `min(remaining, K8S_ROLLOUT_POLL_SLICE_S)`, so the *first*
    slice for a workload reveals how much budget that workload actually
    started with once the budget is smaller than one slice.
    """

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            timeout = next(a for a in cmd if a.startswith("--timeout="))
            budgets.append(int(timeout.removeprefix("--timeout=").removesuffix("s")))
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout=json.dumps({"items": []}), stderr="")

    return fake_run


def test_each_workload_gets_its_own_budget_not_a_shared_start(monkeypatch) -> None:
    """A slow first workload must not spend the second one's budget (#3827).

    The waits run one after another, so stamping one `now + budget` up front
    charged Ollama for however long Cassandra took -- and Ollama's larger
    budget exists precisely because a cold default-model pull is the slowest
    thing the install does. An install on a slow link then failed a workload
    that was still making progress, which is this issue's own bug.
    """
    _advancing_clock(monkeypatch)
    budgets: list[int] = []
    monkeypatch.setattr(ops, "_run", _budget_recording_run(budgets))

    results = ops._wait_for_k8s_rollouts(
        [("deploy/first", "first", 60), ("deploy/second", "second", 60)],
        remedy="",
    )

    # First one exhausts its own 60s and fails; the wait stops there, so the
    # second workload's deadline never gets stamped from a used-up clock.
    assert not results[-1].ok
    assert "first did not become ready in time" in results[-1].message
    # Each slice asked for is bounded by that workload's own remaining budget,
    # never by a clock that started before it did.
    assert budgets and all(b <= 60 for b in budgets)


def test_second_workload_starts_with_a_full_budget(monkeypatch) -> None:
    """The positive half, in the shape the defect actually had.

    Cassandra takes minutes to bootstrap and then succeeds; Ollama is waited
    on next. On the shared-start code Ollama's deadline had already passed
    before its first poll, so it was reported as "did not become ready in
    time" without ever being given a single slice -- a false install failure
    on a workload that was fine.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock["t"])

    polled: list[str] = []

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            ref = cmd[cmd.index("status") + 1]
            polled.append(ref)
            if ref == "deploy/slow" and clock["t"] < 300:
                clock["t"] += 30  # a slice spent, still rolling out
                return MagicMock(
                    returncode=1, stdout="", stderr="error: timed out waiting for the condition"
                )
            return MagicMock(returncode=0, stdout="rolled out", stderr="")
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout=json.dumps({"items": []}), stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_rollouts(
        [("deploy/slow", "slow", 600), ("deploy/next", "next", 60)],
        remedy="",
    )

    assert all(r.ok for r in results)
    # The point: the second workload was actually polled. Its 60s budget was
    # stamped at t=300 when its own wait began, not at t=0 alongside the first
    # one's -- under which it would have been 240s overdue before it started.
    assert "deploy/next" in polled


def test_observability_keeps_one_pooled_budget(monkeypatch) -> None:
    """The deliberate exception: the observability layer's dozen small
    workloads share one allowance, so it is passed explicitly rather than
    each of them getting the full budget."""
    _advancing_clock(monkeypatch)
    budgets: list[int] = []
    monkeypatch.setattr(ops, "_run", _budget_recording_run(budgets))

    results = ops._wait_for_k8s_observability(budget_s=60)

    assert not results[-1].ok
    # One shared 60s, drained across the layer -- not 60s per workload.
    assert sum(budgets) <= 60


def test_data_tier_workloads_pass_budgets_not_deadlines() -> None:
    """`K8S_DATA_TIER_WORKLOADS` reaches the wait as-is (`(ref, label, budget)`).

    Pins the shape the per-workload stamping depends on: the moment a caller
    pre-computes `now + timeout` again, every workload after the first is
    silently short-changed.
    """
    with patch.object(ops, "_wait_for_k8s_rollouts", return_value=[]) as wait:
        ops._wait_for_k8s_data_tier()

    passed = wait.call_args.args[0]
    assert passed == list(ops.K8S_DATA_TIER_WORKLOADS)
    assert wait.call_args.kwargs.get("shared_deadline") is None


def test_app_tier_install_wait_is_not_the_restart_budget() -> None:
    """The install's app-tier budget and the restart rollout's are separate (#3827/#3834).

    Both were named `K8S_APP_TIER_ROLLOUT_TIMEOUT_S`, landed by two PRs open
    at the same time. Python rebinds silently, so the later assignment halved
    the install wait -- reinstating exactly the false `[FAIL]` on a Pod that
    was merely still starting that this issue removed.
    """
    assert ops.K8S_APP_TIER_ROLLOUT_TIMEOUT_S == 600
    assert ops.K8S_APP_TIER_RESTART_TIMEOUT_S == 300

    with patch.object(ops, "_wait_for_k8s_rollouts", return_value=[]) as wait:
        ops._wait_for_k8s_app_tier()

    assert all(budget == 600 for _ref, _label, budget in wait.call_args.args[0])


def _crashloop_pod(name: str) -> dict:
    return {
        "metadata": {"name": name},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [
                {
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    }
                }
            ],
        },
    }


def test_crashloopbackoff_needs_more_confirmations_than_a_dead_end(monkeypatch) -> None:
    """`CrashLoopBackOff` is the one blocked reason a healthy bring-up passes
    through -- kubelet escalates the restart delay to 5 minutes, so the reason
    stays visible long after the attempt that will succeed is scheduled. Two
    slices (60s) sits well inside that window."""
    _advancing_clock(monkeypatch)
    polls = {"n": 0}

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            polls["n"] += 1
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout='{"app": "nyxgpt-api"}', stderr="")
        return MagicMock(
            returncode=0, stdout=json.dumps({"items": [_crashloop_pod("api-abc")]}), stderr=""
        )

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._wait_for_k8s_rollouts([("deploy/api", "api", 900)], remedy="")

    assert not results[-1].ok
    assert "api-abc" in results[-1].message
    assert ops.K8S_CRASHLOOP_CONFIRMATIONS > ops.K8S_BLOCKED_CONFIRMATIONS
    # It did give the Pod the longer grace before ruling -- the pre-fix count
    # would have abandoned the rollout after two.
    assert polls["n"] == ops.K8S_CRASHLOOP_CONFIRMATIONS


def test_a_dead_end_reason_still_fails_fast(monkeypatch) -> None:
    """The other half: nothing to wait for means the short count still applies.

    The longer grace is for `CrashLoopBackOff` specifically -- extending it to
    `Unschedulable` would put the install's one *real* failure back at the end
    of the run, which is how #3827's genuine prometheus failure came to be
    buried behind nine false ones.
    """
    _advancing_clock(monkeypatch)
    polls = {"n": 0}

    def fake_run(cmd, **kwargs):
        if "rollout" in cmd:
            polls["n"] += 1
            return MagicMock(
                returncode=1, stdout="", stderr="error: timed out waiting for the condition"
            )
        if any("jsonpath" in arg for arg in cmd):
            return MagicMock(returncode=0, stdout='{"app": "prometheus"}', stderr="")
        return MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "prom-abc"},
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

    results = ops._wait_for_k8s_rollouts([("deploy/prom", "prom", 900)], remedy="")

    assert not results[-1].ok
    assert polls["n"] == ops.K8S_BLOCKED_CONFIRMATIONS


# --- The wiring the ConfigMap omitted (#3990) -------------------------------
#
# `k8s/configmap.yaml` performed three of the five container-network rewrites
# the Compose config generation performs (`[ollama] base_url`, `[rag]
# cassandra_hosts`, `[nyxgpt] session_backend`) and omitted exactly the two
# that make the observability tier function. The result installed, passed
# every health check, and observed nothing: every span went to the api Pod's
# own `localhost:4318`, and no error was ever reported to GlitchTip at all.

CONFIGMAP_PATH = REPO_ROOT / "k8s" / "configmap.yaml"
K8S_DIR_PATH = REPO_ROOT / "k8s"
IN_CLUSTER_COLLECTOR = "http://otel-collector:4318/v1/traces"


def _pod_config_text() -> str:
    """The `config.ini` the api Pod actually mounts."""
    return yaml.safe_load(CONFIGMAP_PATH.read_text())["data"]["config.ini"]


def _pod_config() -> ConfigParser:
    parser = ConfigParser()
    parser.read_string(_pod_config_text())
    return parser


def _app_deployments() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for path in sorted(K8S_DIR_PATH.glob("deployment-*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc["kind"] == "Deployment":
                docs[doc["metadata"]["name"]] = doc
    return docs


def _container_env(deployment: dict) -> dict[str, dict]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_pod_config_sends_spans_to_the_in_cluster_collector() -> None:
    """The rewrite the Compose path makes in `_COMPOSE_CONFIG_OVERRIDES`."""
    tracing_config = get_tracing_config(_pod_config())
    assert tracing_config["enabled"] is True
    assert tracing_config["otlp_endpoint"] == IN_CLUSTER_COLLECTOR


def test_pod_config_without_a_tracing_section_traces_to_the_pod_itself() -> None:
    """Fault injection for the assertion above: strip the section this fix
    added and the fallback is `localhost`, which inside a Pod is that Pod --
    the exact defect (#3990). Without this, the test above would keep passing
    against a file that merely mentioned the collector in a comment."""
    stripped = ConfigParser()
    stripped.read_string(re.sub(r"\n\[tracing\].*?(?=\n\[)", "", _pod_config_text(), flags=re.S))

    assert not stripped.has_section("tracing")
    assert "localhost" in get_tracing_config(stripped)["otlp_endpoint"]


def test_tracing_endpoint_names_a_service_the_overlay_ships() -> None:
    """A collector hostname with no Service behind it resolves to nothing --
    the same silent failure in a different disguise."""
    host = IN_CLUSTER_COLLECTOR.removeprefix("http://").split(":")[0]
    assert host in set(_by_kind("Service"))


def test_pod_config_declares_error_tracking_with_a_runtime_filled_dsn() -> None:
    """The section must exist (or the api reports nothing at all) with an
    EMPTY dsn: GlitchTip mints a project key per install, so the value cannot
    live in a committed manifest -- it arrives from the Secret."""
    error_tracking = get_error_tracking_config(_pod_config())
    assert error_tracking["enabled"] is True
    assert error_tracking["dsn"] == ""
    # ...and the line has to BE there for docker/entrypoint.sh's sed to have
    # something to rewrite.
    assert re.search(r"^\s*dsn\s*=", _pod_config_text(), flags=re.MULTILINE)


def test_browser_facing_urls_stay_localhost() -> None:
    """The same split the Compose path makes: service-to-service endpoints are
    rewritten, `*_ui_url` values are opened from the operator's browser
    through `nyxgpt ops port-forward` and must not be."""
    parser = _pod_config()
    _service, jaeger_port, _remote = ops.K8S_PORT_FORWARD_TARGETS["jaeger"]
    _service, glitchtip_port, _remote = ops.K8S_PORT_FORWARD_TARGETS["glitchtip"]

    assert get_tracing_config(parser)["jaeger_ui_url"] == f"http://localhost:{jaeger_port}"
    assert (
        get_error_tracking_config(parser)["glitchtip_ui_url"]
        == f"http://localhost:{glitchtip_port}"
    )


def test_web_deployments_carry_the_collector_endpoint_in_their_env() -> None:
    """The web tier is a Next.js process reading web/src/instrumentation.ts's
    env vars, NOT the ConfigMap -- so the rewrite has to be repeated there.
    The startup line the acceptance run captured (`otlp_endpoint=
    http://localhost:4318/v1/traces`) came from a web Pod."""
    web = {n: d for n, d in _app_deployments().items() if n.startswith("nyxgpt-web")}
    assert set(web) == {"nyxgpt-web-stable", "nyxgpt-web-canary"}
    for name, deployment in web.items():
        env = _container_env(deployment)
        assert env["NYXGPT_OTLP_ENDPOINT"]["value"] == IN_CLUSTER_COLLECTOR, name
        assert env["NYXGPT_TRACING_ENABLED"]["value"] == "true", name


def test_every_app_deployment_reads_the_dsn_from_the_secret() -> None:
    """Both tracks of both tiers: a canary Pod that reports no errors is the
    same blindness as a stable one that does."""
    deployments = _app_deployments()
    assert set(deployments) == {
        "nyxgpt-api-stable",
        "nyxgpt-api-canary",
        "nyxgpt-web-stable",
        "nyxgpt-web-canary",
    }
    for name, deployment in deployments.items():
        ref = _container_env(deployment)["NYXGPT_ERROR_TRACKING_DSN"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "nyxgpt-secrets", name
        assert ref["key"] == ops.K8S_ERROR_TRACKING_DSN_SECRET_KEY, name


def test_the_secret_template_declares_the_dsn_key() -> None:
    """A `secretKeyRef` to a key that does not exist leaves every app Pod in
    CreateContainerConfigError -- worse than the blindness it was fixing."""
    secret = yaml.safe_load((K8S_DIR_PATH / "secret.example.yaml").read_text())
    assert secret["stringData"][ops.K8S_ERROR_TRACKING_DSN_SECRET_KEY] == ""


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="entrypoint.sh runs GNU sed inside the Linux image; BSD sed -i takes a suffix argument",
)
def test_entrypoint_merges_the_dsn_into_the_pod_config(tmp_path) -> None:
    """Executed, not inspected: the shipped `sed` line is run against the real
    ConfigMap contents, because a merge that silently matches nothing is
    exactly as broken as no merge at all."""
    entrypoint = (REPO_ROOT / "docker" / "entrypoint.sh").read_text()
    line = next(
        line
        for line in entrypoint.splitlines()
        if "sed -i" in line and "NYXGPT_ERROR_TRACKING_DSN" in line
    )
    config = tmp_path / "config.ini"
    config.write_text(_pod_config_text(), encoding="utf-8")
    dsn = "http://publickey@glitchtip:8080/1"

    subprocess.run(
        ["sh", "-c", line.replace('"$CONFIG_DIR/config.ini"', f'"{config}"')],
        env={"NYXGPT_ERROR_TRACKING_DSN": dsn, "PATH": os.environ.get("PATH", "")},
        check=True,
    )

    merged = ConfigParser()
    merged.read_string(config.read_text(encoding="utf-8"))
    assert get_error_tracking_config(merged)["dsn"] == dsn
    # The api-key merge above it must still work on the same file.
    assert merged.get("auth", "api_key") == ""


# --- Provisioning the in-cluster GlitchTip (#3990) --------------------------


def _bootstrap_secret_files(tmp_path, monkeypatch):
    """Stand-ins for the two bootstrapped Secret manifests ops writes into."""
    k8s_dir = tmp_path / "k8s"
    observability_dir = k8s_dir / "observability"
    observability_dir.mkdir(parents=True)
    (k8s_dir / "secret.yaml").write_text(
        'stringData:\n  api-key: "k"\n  error-tracking-dsn: ""\n', encoding="utf-8"
    )
    (observability_dir / "secret.yaml").write_text(
        f'stringData:\n  glitchtip-grafana-token: "{ops.GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "K8S_DIR", k8s_dir)
    monkeypatch.setattr(ops, "K8S_OBSERVABILITY_DIR", observability_dir)
    return k8s_dir / "secret.yaml", observability_dir / "secret.yaml"


def _provisionable_cluster(tmp_path, monkeypatch, *, dsn="http://key@localhost:8080/1"):
    """Patch out everything between `createsuperuser` and the project key, so
    the test is about what this module does with the two values it gets."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        "[error_tracking]\nadmin_email = admin@nyxgpt.local\nadmin_password = pw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"glitchtip": "1/1 ready"}
    )
    monkeypatch.setattr(
        ops, "_k8s_glitchtip_ensure_superuser", lambda *a: ops.OpsResult(True, "superuser")
    )
    monkeypatch.setattr(
        ops, "_k8s_port_forward", lambda *a, **k: contextlib.nullcontext("http://127.0.0.1:65000")
    )
    monkeypatch.setattr(
        ops, "_glitchtip_login", lambda *a: (MagicMock(), ops.OpsResult(True, "login"))
    )
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_api_token", lambda *a: ("tok-abc", ops.OpsResult(True, "token"))
    )
    monkeypatch.setattr(ops, "_glitchtip_http_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_organization", lambda *a: ("nyxgpt", ops.OpsResult(True, "org"))
    )
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_team", lambda *a: ("nyxgpt", ops.OpsResult(True, "team"))
    )
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_team_membership", lambda *a: ops.OpsResult(True, "member")
    )
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_project", lambda *a: ("nyxgpt-backend", ops.OpsResult(True, "prj"))
    )
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_project_key", lambda *a: (dsn, ops.OpsResult(True, "key"))
    )
    monkeypatch.setattr(ops, "_restart_k8s_grafana", lambda: ops.OpsResult(True, "restarted"))


def test_provisioning_writes_the_dsn_and_the_token_into_the_secrets(tmp_path, monkeypatch) -> None:
    """The two values a Kubernetes deployment was missing entirely."""
    app_secret, observability_secret = _bootstrap_secret_files(tmp_path, monkeypatch)
    _provisionable_cluster(tmp_path, monkeypatch)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: (ran.append(cmd), MagicMock(returncode=0, stdout="", stderr=""))[1],
    )

    results = ops._k8s_provision_glitchtip()

    assert all(r.ok for r in results), [r.message for r in results]
    # Rewritten to the in-cluster Service: a Pod using GlitchTip's own
    # browser-facing localhost DSN drops every event silently (#3565).
    assert 'error-tracking-dsn: "http://key@glitchtip:8080/1"' in app_secret.read_text()
    assert 'glitchtip-grafana-token: "tok-abc"' in observability_secret.read_text()
    assert ops.GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER not in observability_secret.read_text()
    # Both files are applied, so the cluster and the manifests agree -- a
    # cluster-only patch would be reverted by the next `kubectl apply -k`.
    assert ["kubectl", "apply", "-f", str(app_secret)] in ran
    assert ["kubectl", "apply", "-f", str(observability_secret)] in ran
    # An environment is fixed at process start, so the Pods that booted with
    # an empty DSN have to be replaced -- and waited for, or everything that
    # reads the cluster after the install describes a half-rolled stack.
    restarted = [c[-1] for c in ran if "restart" in c]
    assert restarted == ["deploy/nyxgpt-api-stable", "deploy/nyxgpt-web-stable"]
    waited = [c for c in ran if "status" in c and "rollout" in c]
    assert {c[c.index("status") + 1] for c in waited} == set(restarted)


def test_reprovisioning_the_same_values_restarts_nothing(tmp_path, monkeypatch) -> None:
    """Idempotent in the same sense as the Compose path: a re-install that
    mints the same DSN and token must not bounce the api, web and Grafana."""
    _bootstrap_secret_files(tmp_path, monkeypatch)
    _provisionable_cluster(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: MagicMock(returncode=0, stdout="", stderr=""))
    ops._k8s_provision_glitchtip()

    ran: list[list[str]] = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: (ran.append(cmd), MagicMock(returncode=0, stdout="", stderr=""))[1],
    )
    with patch.object(ops, "_restart_k8s_grafana") as restart_grafana:
        results = ops._k8s_provision_glitchtip()

    assert all(r.ok for r in results)
    restart_grafana.assert_not_called()
    assert not [c for c in ran if "rollout" in c]


def test_provisioning_skips_when_glitchtip_is_not_ready(monkeypatch) -> None:
    """A skip, not a failure: the app tier is fine without error tracking, and
    an install must not fail over a workload that is still rolling out."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"glitchtip": "0/1 ready"}
    )

    results = ops._k8s_provision_glitchtip()

    assert all(r.ok for r in results)
    assert "Skipped GlitchTip provisioning" in results[0].message
    assert "glitchtip-init --kubernetes" in results[0].details


def test_the_superuser_password_never_reaches_argv(monkeypatch) -> None:
    """`kubectl exec` has no `-e VAR` forwarding, and `_run` logs the command
    it ran -- so an argv-borne password would be logged on every idempotent
    re-run (CodeQL #105/#106). It goes in on stdin instead."""
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops._k8s_glitchtip_ensure_superuser("admin@nyxgpt.local", "sup3r-s3cret")

    assert result.ok
    assert "sup3r-s3cret" not in " ".join(captured["cmd"])  # type: ignore[arg-type]
    assert captured["input"] == "sup3r-s3cret\n"


def test_writing_a_secret_value_reports_whether_it_changed(tmp_path) -> None:
    path = tmp_path / "secret.yaml"
    path.write_text('stringData:\n  error-tracking-dsn: ""\n', encoding="utf-8")

    changed, result = ops._write_k8s_secret_value(path, "error-tracking-dsn", "http://k@g:8080/1")
    assert changed and result.ok

    changed_again, result_again = ops._write_k8s_secret_value(
        path, "error-tracking-dsn", "http://k@g:8080/1"
    )
    assert not changed_again and result_again.ok


def test_an_already_bootstrapped_secret_gains_the_new_key(tmp_path, monkeypatch) -> None:
    """The upgrade trap this fix would otherwise have set: both Secrets are
    written once and left alone, so a machine that installed BEFORE
    `error-tracking-dsn` existed would apply a Secret without it -- and a
    `secretKeyRef` to a missing key leaves every api and web Pod in
    CreateContainerConfigError, which is far worse than no error tracking."""
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path)
    (tmp_path / "secret.example.yaml").write_text(
        'stringData:\n  api-key: "change-me"\n  error-tracking-dsn: ""\n', encoding="utf-8"
    )
    # A pre-#3990 secret.yaml: the real API key, and no DSN key at all.
    (tmp_path / "secret.yaml").write_text(
        'stringData:\n  # keep me\n  api-key: "the-real-key"\n', encoding="utf-8"
    )

    results = ops._ensure_k8s_secret(None)
    written = (tmp_path / "secret.yaml").read_text()

    assert all(r.ok for r in results)
    assert 'error-tracking-dsn: ""' in written
    # ...and nothing else moved: the existing credential and its comments stay.
    assert 'api-key: "the-real-key"' in written
    assert "# keep me" in written


def test_writing_an_unknown_secret_key_fails_with_the_remedy(tmp_path) -> None:
    """A Secret bootstrapped before this key existed must not be reported as
    successfully written -- it is a manifest the operator has to re-bootstrap."""
    path = tmp_path / "secret.yaml"
    path.write_text('stringData:\n  api-key: "k"\n', encoding="utf-8")

    changed, result = ops._write_k8s_secret_value(path, "error-tracking-dsn", "x")

    assert not changed and not result.ok
    assert "re-bootstrap" in result.details


# --- Running is not receiving (#3990) ---------------------------------------


def _telemetry_run(responses: dict[str, tuple[int, str]]):
    """A `_run` stand-in answering the in-cluster probes by URL substring."""

    def fake_run(cmd, **kwargs):
        haystack = " ".join(cmd)
        for needle, (returncode, stdout) in responses.items():
            if needle in haystack:
                return MagicMock(returncode=returncode, stdout=stdout, stderr="")
        return MagicMock(returncode=1, stdout="", stderr="no match")

    return fake_run


def test_jaegers_own_self_traces_do_not_count_as_telemetry(monkeypatch) -> None:
    """The literal payload the acceptance run captured: Jaeger knew exactly one
    service, itself. Ten workloads reported `1/1 ready` over the top of it."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops, "_run", _telemetry_run({"jaeger": (0, '{"data":["jaeger-all-in-one"],"total":1}')})
    )

    result = ops._k8s_traces_flow_result()

    assert ops._result_status_label(result) == ops.K8S_NO_DATA_LABEL
    assert result.ok  # a stack nobody has chatted with is not a broken stack
    assert "no nyxGPT spans" in result.message


def test_nyxgpt_spans_in_jaeger_are_reported_as_flowing(monkeypatch) -> None:
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops,
        "_run",
        _telemetry_run({"jaeger": (0, '{"data":["jaeger-all-in-one","nyxgpt-api","nyxgpt-web"]}')}),
    )

    result = ops._k8s_traces_flow_result()

    assert ops._result_status_label(result) == "OK"
    assert "nyxgpt-api" in result.message and "nyxgpt-web" in result.message


def test_the_placeholder_grafana_token_is_reported_not_hidden(monkeypatch) -> None:
    """The second symptom: `Bearer UNCONFIGURED-glitchtip-token` and a 401 on
    every SRE Home GlitchTip panel."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops,
        "_run",
        _telemetry_run({"cat": (0, f"{ops.GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER}\n")}),
    )

    result = ops._k8s_errors_flow_result()

    assert ops._result_status_label(result) == ops.K8S_NO_DATA_LABEL
    assert "placeholder" in result.message
    assert "401" in result.details


def test_a_real_token_glitchtip_accepts_is_reported_as_working(monkeypatch) -> None:
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops, "_run", _telemetry_run({"cat": (0, "tok-abc\n"), "organizations": (0, "[]")})
    )

    result = ops._k8s_errors_flow_result()

    assert ops._result_status_label(result) == "OK"


def test_health_reports_receiving_alongside_running(monkeypatch) -> None:
    """The signal AC5 asks for: ten `1/1 ready` workloads receiving nothing
    must not read as a healthy observability stack (the #3812 shape)."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ops,
        "_k8s_observability_workload_state",
        lambda: dict.fromkeys(
            (*ops.K8S_OBSERVABILITY_DEPLOYMENTS, *ops.K8S_OBSERVABILITY_DAEMONSETS), "1/1 ready"
        ),
    )
    monkeypatch.setattr(
        ops,
        "_run",
        _telemetry_run(
            {
                "jaeger": (0, '{"data":["jaeger-all-in-one"]}'),
                "prometheus": (0, '{"data":{"activeTargets":[{"health":"up"}]}}'),
                "loki": (0, '{"data":[]}'),
                "cat": (0, f"{ops.GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER}\n"),
            }
        ),
    )

    results = ops._k8s_observability_health()
    labels = {r.message: ops._result_status_label(r) for r in results}

    # Every workload is ready...
    assert all(labels[f"observability {name}: 1/1 ready"] == "OK" for name in ("grafana", "jaeger"))
    # ...and the tier is still not receiving what it exists to receive.
    assert [m for m, label in labels.items() if label == ops.K8S_NO_DATA_LABEL] == [
        "observability traces: Jaeger has no nyxGPT spans yet",
        "observability logs: Loki has received nothing",
        "observability errors: Grafana's GlitchTip token is still the placeholder",
    ]
    assert all(r.ok for r in results)


def test_data_flow_is_not_probed_through_a_grafana_that_is_not_ready(monkeypatch) -> None:
    """Four `[NO DATA]` lines about a tier that is mid-rollout would be the
    wall of false negatives #3827 removed from the readiness half."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    with patch.object(ops, "_run") as run:
        assert ops._k8s_observability_data_flow({"grafana": "0/1 ready"}) == []
        assert ops._k8s_observability_data_flow({"grafana": "absent"}) == []
    run.assert_not_called()
