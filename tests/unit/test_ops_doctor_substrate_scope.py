"""`ops doctor` reports on the deployment that is serving (#3987, AC4).

The owner's Kubernetes acceptance re-test of #3987 passed AC1/2/3/5 and failed
AC4: `nyxgpt ops doctor` exited 2 on a cluster with 14/14 Pods Running and
21h uptime, saying

    Tracing is enabled ([tracing] otlp_endpoint=http://localhost:4318/v1/traces)
    but nothing is listening there -- spans are being silently dropped and
    Jaeger will stay empty. Confirm the otel-collector Compose service ...

minutes after `curl http://localhost:16686/api/services` on the same machine
returned three services with real spans behind them. A second instance in the
same run reported `GET .../api/0/projects/nyxgpt/nyxgpt-backend/keys/ 401`
against a GlitchTip that was healthy. Both checks had read THIS HOST's config,
probed THIS HOST's port, and reported the verdict as the deployment's.

The owner asked for the class, not the two instances: "any check that reads
host config or probes a host port needs the same substrate branch
`required_models_status` now has. A sweep of `ops doctor`'s checks for that
shape is the deliverable -- naming the ones that are correctly host-scoped is
as useful as fixing the ones that are not."

So this module holds two things:

1. `CHECK_SCOPE` -- the sweep's result, one entry per finding-producing check
   `doctor` runs, with the reason it is scoped the way it is. The first test
   fails if `doctor` grows a check that is not in it, so the *next* check with
   this shape is caught here instead of by an owner running the product.
2. Behaviour tests for the three branches the sweep found, in both modes.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import install_mode, ops

# Scope vocabulary, and what each value promises.
HOST = "host"
"""The finding is a claim about THIS HOST -- its PATH, its files, its service
managers, its venv, its containers. Correct as it stands on every substrate."""

COMPOSE_GATED = "compose-gated"
"""Reads host config, but cannot produce a finding unless a Compose stack is
actually running, so it structurally cannot speak about a Kubernetes
deployment."""

NATIVE_HALF = "native-half"
"""The host-side half of a substrate-branched pair. `doctor` calls it only
when no Kubernetes deployment is present."""

CLUSTER_HALF = "cluster-half"
"""The cluster-side half of a substrate-branched pair."""

CLUSTER = "cluster"
"""Already asks the cluster and nothing else."""

PARAMETERISED = "parameterised"
"""One function that takes the substrate as an argument rather than splitting
into a pair -- `doctor` passes which deployment the question is about."""


CHECK_SCOPE: dict[str, tuple[str, str]] = {
    # --- the three the #3987 sweep found, and their cluster twins ---
    "_tracing_wiring_issue": (
        NATIVE_HALF,
        "reads [tracing] otlp_endpoint from ~/.nyxGPT/config.ini and TCP-connects to it",
    ),
    "_k8s_tracing_wiring_issue": (
        CLUSTER_HALF,
        "reads the nyxgpt-config ConfigMap and judges the collector workload's readiness",
    ),
    "_prometheus_api_scrape_issue": (
        NATIVE_HALF,
        "reads [monitoring] prometheus_ui_url and asks whatever answers on the host",
    ),
    "_k8s_prometheus_api_scrape_issue": (
        CLUSTER_HALF,
        "asks the in-cluster Prometheus for its own targets, from inside the cluster",
    ),
    "_error_tracking_dsn_drift_issue": (
        NATIVE_HALF,
        "authenticates to [error_tracking] glitchtip_ui_url with the host's token",
    ),
    "_k8s_error_tracking_dsn_drift_issue": (
        CLUSTER_HALF,
        "compares the Secret's DSN against the in-cluster GlitchTip's live keys",
    ),
    # --- already substrate-aware before this change ---
    "_missing_required_models_issue": (
        PARAMETERISED,
        "takes kubernetes=; asks the in-cluster Ollama when the deployment is Kubernetes",
    ),
    "_k8s_access_bridge_issues": (
        CLUSTER,
        "reports the cloud access bridge's units for a Kubernetes deployment only",
    ),
    # --- host-scoped by construction: the finding IS about this machine ---
    "_foreign_native_service_issues": (
        HOST,
        "compares the brew/systemd services registered here against this host's marker",
    ),
    "_terraform_install_mode_issues": (
        HOST,
        "a claim about the Terraform deployment on this host, self-gated on its marker",
    ),
    "_tracing_packages_doctor_issue": (
        HOST,
        "the OTel packages in THIS venv; an api Pod's come from its image",
    ),
    "_insecure_api_bind_issue": (
        HOST,
        "the bind posture of the native api process on this host",
    ),
    "_ollama_env_drift_issue": (
        HOST,
        "this login session's launchctl OLLAMA_MODELS, for the native Ollama",
    ),
    "_linux_ollama_port_conflict_issue": (
        HOST,
        "this host's system-wide ollama.service contending for port 11434",
    ),
    "_docker_access_doctor_issue": (
        HOST,
        "whether this session can reach this host's Docker socket",
    ),
    "_observability_volume_doctor_issues": (
        HOST,
        "ownership of this host's ~/.nyxGPT/volumes bind-mount directories",
    ),
    "_stale_venv_doctor_issues": (
        HOST,
        "declared dependencies missing from the interpreter running this command",
    ),
    # --- read host config, but cannot fire without a running Compose stack ---
    "_log_aggregation_wiring_issue": (
        COMPOSE_GATED,
        "returns None unless the Compose promtail container is running",
    ),
    "_glitchtip_secrets_doctor_issues": (
        COMPOSE_GATED,
        "the token half is gated on a running Compose grafana; the rest is a local dir",
    ),
    "_loki_recent_volume_by_logger": (
        COMPOSE_GATED,
        "only reached when the Compose promtail container is running",
    ),
}

# `_loki_recent_volume_by_logger` produces a `doctor` finding without carrying
# the `_issue`/`_issues` suffix the convention below keys on. Listed so the
# sweep covers it; everything else is found by name.
UNCONVENTIONALLY_NAMED = {"_loki_recent_volume_by_logger"}

_FINDING_HELPER = re.compile(r"^_.*_issues?$")


def _doctor_check_calls() -> set[str]:
    """Every finding-producing helper `ops.doctor` calls, read from its source."""
    tree = ast.parse(inspect.getsource(ops.doctor))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return {n for n in called if _FINDING_HELPER.match(n)} | (called & UNCONVENTIONALLY_NAMED)


@pytest.mark.unit
def test_every_doctor_check_is_classified_by_which_machine_it_is_about():
    """The guard, and the reason this module exists.

    #3987's AC4 was not one bad check -- it was that nothing in the codebase
    recorded which machine each check speaks for, so two of them drifted into
    describing a Kubernetes deployment out of the host's config and no test
    could tell. A new check lands in `doctor` without an entry here only once.
    """
    called = _doctor_check_calls()
    unclassified = called - CHECK_SCOPE.keys()
    assert not unclassified, (
        "these ops doctor checks are not classified in CHECK_SCOPE: "
        f"{sorted(unclassified)}. Decide which machine each one's finding is a claim "
        "about: host-scoped (this machine's tools/files/services/venv) or about the "
        "deployment that is serving -- and if the latter, give it a substrate branch "
        "like _tracing_wiring_issue/_k8s_tracing_wiring_issue."
    )
    stale = CHECK_SCOPE.keys() - called
    assert not stale, f"CHECK_SCOPE names checks doctor no longer runs: {sorted(stale)}"


@pytest.mark.unit
def test_every_branched_check_has_both_halves_and_doctor_calls_both():
    """A pair with only one half wired up is the defect wearing a fix's clothes."""
    called = _doctor_check_calls()
    for name, (scope, _why) in CHECK_SCOPE.items():
        if scope != NATIVE_HALF:
            continue
        twin = f"_k8s_{name.lstrip('_')}"
        assert hasattr(ops, twin), f"{name} is branched but {twin} does not exist"
        assert CHECK_SCOPE.get(twin, ("", ""))[0] == CLUSTER_HALF
        assert {name, twin} <= called, f"doctor must call both halves of {name}"


@pytest.mark.unit
def test_no_classified_check_is_left_without_a_reason():
    for name, (scope, why) in CHECK_SCOPE.items():
        assert scope in {HOST, COMPOSE_GATED, NATIVE_HALF, CLUSTER_HALF, CLUSTER, PARAMETERISED}
        assert why, f"{name} is classified but says nothing about why"


# --- the cluster-side tracing check ---


class _CP:
    """The subset of `CompletedProcess` `ops._run`'s callers actually read."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


CLUSTER_CONFIG = """
[tracing]
enabled = true
service_name = nyxgpt-api
otlp_endpoint = http://otel-collector:4318/v1/traces

[error_tracking]
enabled = true
dsn =
"""

LOOPBACK_CLUSTER_CONFIG = CLUSTER_CONFIG.replace(
    "http://otel-collector:4318/v1/traces", "http://localhost:4318/v1/traces"
)


def _configmap(monkeypatch, body: str) -> None:
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stdout=body))


@pytest.mark.unit
def test_tracing_is_judged_against_the_endpoint_the_pods_actually_use(monkeypatch):
    """THE AC4 failure: a healthy in-cluster collector reported as broken
    because the host has nothing on `localhost:4318` and is not supposed to."""
    _configmap(monkeypatch, CLUSTER_CONFIG)
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"otel-collector": "1/1 ready"}
    )

    assert ops._k8s_tracing_wiring_issue() is None


@pytest.mark.unit
def test_the_cluster_tracing_check_never_probes_the_host(monkeypatch):
    """A swap that merely added the cluster as a second opinion would leave the
    owner's false finding in place -- the host must not be asked at all."""
    _configmap(monkeypatch, CLUSTER_CONFIG)
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"otel-collector": "1/1 ready"}
    )
    monkeypatch.setattr(
        ops.tracing,
        "otlp_endpoint_reachable",
        lambda _endpoint: pytest.fail("the host's OTLP port must not be probed in Kubernetes mode"),
    )

    assert ops._k8s_tracing_wiring_issue() is None


@pytest.mark.unit
def test_a_collector_that_is_not_ready_is_still_reported(monkeypatch):
    """The check has to be able to say no: a branch that always returns None
    would trade a false positive for a blind spot."""
    _configmap(monkeypatch, CLUSTER_CONFIG)
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"otel-collector": "0/1 ready"}
    )

    issue = ops._k8s_tracing_wiring_issue()

    assert issue is not None
    assert "otel-collector" in issue
    # The Kubernetes remedy, not the Compose one the owner was handed.
    assert "nyxgpt ops observability --kubernetes" in issue
    assert "Compose" not in issue


@pytest.mark.unit
def test_an_absent_collector_workload_is_reported_as_not_deployed(monkeypatch):
    _configmap(monkeypatch, CLUSTER_CONFIG)
    monkeypatch.setattr(
        ops, "_k8s_observability_workload_state", lambda: {"otel-collector": "absent"}
    )

    issue = ops._k8s_tracing_wiring_issue()

    assert issue is not None and "not deployed" in issue


@pytest.mark.unit
def test_a_loopback_endpoint_inside_a_pod_is_its_own_finding(monkeypatch):
    """#3990's shape: `localhost` inside a Pod is that Pod, and no collector
    readiness makes it work -- so it gets the fix that does."""
    _configmap(monkeypatch, LOOPBACK_CLUSTER_CONFIG)
    monkeypatch.setattr(
        ops,
        "_k8s_observability_workload_state",
        lambda: pytest.fail("a loopback endpoint is decided without asking the workloads"),
    )

    issue = ops._k8s_tracing_wiring_issue()

    assert issue is not None
    assert "that Pod itself" in issue
    assert "nyxgpt ops install --kubernetes" in issue


@pytest.mark.unit
def test_tracing_disabled_in_the_cluster_is_not_a_finding(monkeypatch):
    _configmap(monkeypatch, "[tracing]\nenabled = false\notlp_endpoint = http://x:4318/v1/traces\n")

    assert ops._k8s_tracing_wiring_issue() is None


@pytest.mark.unit
def test_an_unreadable_configmap_says_nothing_rather_than_guessing(monkeypatch):
    """Cannot-tell is not broken -- the #3468 distinction, applied to config
    instead of Pods."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stderr="NotFound", returncode=1))

    assert ops._k8s_deployment_config() is None
    assert ops._k8s_tracing_wiring_issue() is None


@pytest.mark.unit
def test_a_missing_configmap_key_is_absence_not_content(monkeypatch):
    """`go-template`'s `index` renders a missing key as this literal rather
    than failing, so it has to be read as absence."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stdout="<no value>"))

    assert ops._k8s_configmap_entry("nyxgpt-config", "config.ini") is None


@pytest.mark.unit
def test_the_configmap_read_costs_nothing_without_kubectl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: None)
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: pytest.fail("no kubectl, no read"))

    assert ops._k8s_configmap_entry("nyxgpt-config", "config.ini") is None


@pytest.mark.unit
def test_the_configmap_key_is_indexed_not_path_split(monkeypatch):
    """`config.ini` through `-o jsonpath` would be read as `.data.config.ini`,
    a path two levels deep that does not exist."""
    seen: list[list[str]] = []
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _CP(stdout=CLUSTER_CONFIG)

    monkeypatch.setattr(ops, "_run", fake_run)

    ops._k8s_configmap_entry("nyxgpt-config", "config.ini")

    assert seen == [
        [
            "kubectl",
            "-n",
            "nyxgpt",
            "get",
            "configmap",
            "nyxgpt-config",
            "-o",
            'go-template={{index .data "config.ini"}}',
        ]
    ]


# --- the cluster-side Prometheus scrape check ---


@pytest.mark.unit
def test_the_scrape_check_asks_the_cluster_prometheus(monkeypatch):
    asked: list[str] = []

    def fake_get(url, **_kwargs):
        asked.append(url)
        return True, json.dumps(
            {"data": {"activeTargets": [{"labels": {"job": "nyxgpt-api"}, "health": "up"}]}}
        )

    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_k8s_incluster_get", fake_get)

    assert ops._k8s_prometheus_api_scrape_issue() is None
    assert asked == ["http://prometheus:9090/api/v1/targets?state=active"]


@pytest.mark.unit
def test_a_down_scrape_target_in_the_cluster_names_a_kubernetes_remedy(monkeypatch):
    """The native twin's remedy is `host-api-relay`, which exists only on the
    Compose/native path -- prescribing it on a cluster is the same class of
    wrong answer as the tracing finding the owner reported."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops,
        "_k8s_incluster_get",
        lambda *_a, **_k: (
            True,
            json.dumps(
                {
                    "data": {
                        "activeTargets": [
                            {
                                "labels": {"job": "nyxgpt-api"},
                                "health": "down",
                                "lastError": "connection refused",
                            }
                        ]
                    }
                }
            ),
        ),
    )

    issue = ops._k8s_prometheus_api_scrape_issue()

    assert issue is not None
    assert "connection refused" in issue
    assert "nyxgpt ops observability --kubernetes" in issue
    assert "host-api-relay" not in issue


@pytest.mark.unit
def test_a_prometheus_that_cannot_be_asked_is_not_this_checks_finding(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_k8s_incluster_get", lambda *_a, **_k: (False, ""))

    assert ops._k8s_prometheus_api_scrape_issue() is None


# --- the cluster-side error-tracking DSN check ---


LIVE_KEYS = json.dumps([{"dsn": {"public": "http://livekey@glitchtip:8080/1"}}])


def _dsn_in_secret(monkeypatch, dsn: str) -> None:
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_k8s_error_tracking_dsn", lambda: dsn)


@pytest.mark.unit
def test_a_matching_dsn_in_the_cluster_is_no_drift(monkeypatch):
    _dsn_in_secret(monkeypatch, "http://livekey@glitchtip:8080/1")
    monkeypatch.setattr(ops, "_k8s_incluster_get", lambda *_a, **_k: (True, LIVE_KEYS))

    assert ops._k8s_error_tracking_dsn_drift_issue() is None


@pytest.mark.unit
def test_a_drifted_dsn_in_the_cluster_is_reported_with_the_kubernetes_fix(monkeypatch):
    _dsn_in_secret(monkeypatch, "http://stalekey@glitchtip:8080/1")
    monkeypatch.setattr(ops, "_k8s_incluster_get", lambda *_a, **_k: (True, LIVE_KEYS))

    issue = ops._k8s_error_tracking_dsn_drift_issue()

    assert issue is not None
    assert "nyxgpt ops glitchtip-init --kubernetes" in issue
    # The secret itself never travels into the finding -- only the fact of it.
    assert "stalekey" not in issue


@pytest.mark.unit
def test_the_cluster_dsn_check_asks_glitchtip_with_grafanas_mounted_token(monkeypatch):
    """The credential the cluster's own consumer presents, not the host's --
    the host token answering 401 is exactly what the owner reported."""
    seen: dict[str, str] = {}

    def fake_get(url, *, bearer_token_file=""):
        seen["url"] = url
        seen["token_file"] = bearer_token_file
        return True, LIVE_KEYS

    _dsn_in_secret(monkeypatch, "http://livekey@glitchtip:8080/1")
    monkeypatch.setattr(ops, "_k8s_incluster_get", fake_get)

    ops._k8s_error_tracking_dsn_drift_issue()

    assert seen["url"] == "http://glitchtip:8080/api/0/projects/nyxgpt/nyxgpt-backend/keys/"
    assert seen["token_file"] == ops.K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT


@pytest.mark.unit
def test_an_empty_cluster_dsn_is_inert_not_drifted(monkeypatch):
    """A `--skip-observability` install leaves the DSN empty by design."""
    _dsn_in_secret(monkeypatch, "")
    monkeypatch.setattr(
        ops, "_k8s_incluster_get", lambda *_a, **_k: pytest.fail("nothing to compare")
    )

    assert ops._k8s_error_tracking_dsn_drift_issue() is None


@pytest.mark.unit
def test_a_glitchtip_that_cannot_be_asked_is_not_a_drift(monkeypatch):
    _dsn_in_secret(monkeypatch, "http://anykey@glitchtip:8080/1")
    monkeypatch.setattr(ops, "_k8s_incluster_get", lambda *_a, **_k: (False, ""))

    assert ops._k8s_error_tracking_dsn_drift_issue() is None


@pytest.mark.unit
def test_the_dsn_is_read_from_the_secret_and_never_put_on_an_argv(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _CP(stdout="http://key@glitchtip:8080/1\n")

    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", fake_run)

    assert ops._k8s_error_tracking_dsn() == "http://key@glitchtip:8080/1"
    assert seen[0][:6] == ["kubectl", "-n", "nyxgpt", "get", "secret", "nyxgpt-secrets"]
    assert not any("http://key@" in part for part in seen[0])


@pytest.mark.unit
def test_an_absent_dsn_key_reads_as_empty(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stdout="<no value>"))

    assert ops._k8s_error_tracking_dsn() == ""


# --- doctor itself picks the branch, and says which machine answered ---


def _doctor_on_a_bare_host(monkeypatch, tmp_path) -> None:
    """Strip `doctor` down to the three branched checks (no tools, no repo)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda _tool: None)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(ops, "_stale_venv_doctor_issues", lambda: [])
    monkeypatch.setattr(ops, "_foreign_native_service_issues", lambda _identity: [])
    monkeypatch.setattr(ops, "_terraform_install_mode_issues", lambda: [])
    monkeypatch.setattr(ops, "_k8s_access_bridge_issues", lambda: [])
    monkeypatch.setattr(ops, "_observability_volume_doctor_issues", lambda: [])
    monkeypatch.setattr(ops, "_glitchtip_secrets_doctor_issues", lambda: [])
    monkeypatch.setattr(ops, "_missing_required_models_issue", lambda *_a, **_k: None)
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops, "detect_deployment_mode", lambda: ops.DeploymentMode(native={}, compose={})
    )


def _pods() -> list[ops.K8sWorkloadState]:
    return [
        ops.K8sWorkloadState(name="nyxgpt-api-stable-abc", state=ops.K8S_STATE_READY, summary="1/1")
    ]


@pytest.mark.unit
def test_doctor_runs_the_cluster_halves_on_a_kubernetes_deployment(monkeypatch, tmp_path, capsys):
    _doctor_on_a_bare_host(monkeypatch, tmp_path)
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, tmp_path, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    monkeypatch.setattr(ops, "_k8s_deployment_probe", lambda: ops.K8sDeploymentProbe(_pods()))
    for native in (
        "_tracing_wiring_issue",
        "_prometheus_api_scrape_issue",
        "_error_tracking_dsn_drift_issue",
    ):
        monkeypatch.setattr(
            ops, native, lambda *_a, **_k: pytest.fail("the host must not answer for the cluster")
        )
    monkeypatch.setattr(ops, "_k8s_tracing_wiring_issue", lambda: "cluster tracing finding")
    monkeypatch.setattr(ops, "_k8s_prometheus_api_scrape_issue", lambda: None)
    monkeypatch.setattr(ops, "_k8s_error_tracking_dsn_drift_issue", lambda: None)

    rc = ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 2
    assert "- cluster tracing finding" in out
    # The smoke script greps for this phrase on a live cluster (#3987 AC5).
    assert "reported against the cluster" in out
    assert "Every other check below is about this host" in out


@pytest.mark.unit
def test_doctor_runs_the_native_halves_when_no_cluster_is_deployed(monkeypatch, tmp_path, capsys):
    _doctor_on_a_bare_host(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ops, "_k8s_deployment_probe", lambda: ops.K8sDeploymentProbe([], "kubectl not found")
    )
    for cluster_half in (
        "_k8s_tracing_wiring_issue",
        "_k8s_prometheus_api_scrape_issue",
        "_k8s_error_tracking_dsn_drift_issue",
    ):
        monkeypatch.setattr(
            ops,
            cluster_half,
            lambda *_a, **_k: pytest.fail("no deployment, no cluster-scoped check"),
        )
    monkeypatch.setattr(ops, "_tracing_wiring_issue", lambda: "host tracing finding")
    monkeypatch.setattr(ops, "_prometheus_api_scrape_issue", lambda: None)
    monkeypatch.setattr(ops, "_error_tracking_dsn_drift_issue", lambda: None)

    rc = ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 2
    assert "- host tracing finding" in out
    assert "reported against the cluster" not in out


@pytest.mark.unit
def test_a_recorded_cluster_that_is_gone_falls_back_to_this_host(monkeypatch, tmp_path, capsys):
    """A marker left behind by a torn-down deployment yields no Pods, and the
    host reading stands -- the same rule the model block already follows."""
    _doctor_on_a_bare_host(monkeypatch, tmp_path)
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    monkeypatch.setattr(
        ops, "_k8s_deployment_probe", lambda: ops.K8sDeploymentProbe([], "no pods in the namespace")
    )
    monkeypatch.setattr(
        ops,
        "_k8s_tracing_wiring_issue",
        lambda: pytest.fail("no Pods, nothing to ask the cluster about"),
    )
    monkeypatch.setattr(ops, "_tracing_wiring_issue", lambda: None)
    monkeypatch.setattr(ops, "_prometheus_api_scrape_issue", lambda: None)
    monkeypatch.setattr(ops, "_error_tracking_dsn_drift_issue", lambda: None)

    ops.doctor(SimpleNamespace())

    assert "The checks below report on this host." in capsys.readouterr().out
