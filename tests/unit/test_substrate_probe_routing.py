"""Host-side substrate probes must not answer for a Kubernetes deployment (#4137).

The owner ran a `nyxgpt cloud deploy --kubernetes` (k3s) EC2 instance on
v3.0.0rc17 and got two contradictions on one screen.

`nyxgpt cloud ops self-heal`:

    Self-heal watchdog: enabled
    Observability survey: CANNOT DETERMINE from here -- `docker compose ps` exited 125: ...
     [OK] grafana-8686574f7b-d7vjf: state=Running health=ready
     [OK] jaeger-d9748b855-cwvkb: state=Running health=ready
     ... 14 Pods, all Running and ready ...

    WARNING self-heal: `docker compose ps` exited 125 ... querying
    /home/ec2-user/.nyxGPT/docker-compose.yml -- observability survey unavailable

and `nyxgpt cloud ops doctor`:

    Kubernetes deployment: nyxgpt namespace: 14/14 pod(s) ready
    ...
    nyxGPT ops doctor: FAIL
    - Missing local Cassandra container: nyxgpt-cassandra (run: nyxgpt ops install)

while `cassandra-0: Running` was in the Pod list three lines above.

Both have one cause: the choice of substrate was made from WHERE THE PROCESS IS
RUNNING rather than from WHAT ANSWERS. A k3s host is neither in-cluster nor
Compose -- `kubectl` there reaches the cluster perfectly well while every
`docker compose ps` exits 125 against a daemon socket that user cannot reach --
so an `_in_cluster()` gate cannot see it and the code falls through to Compose.

The surfaces this file pins, one section each:

1. `self_heal.component_survey` / `status` -- the cluster is asked FIRST, and
   the Compose probe is not run at all when it holds the tier.
2. `cli.cmd_self_heal_status` -- the CANNOT DETERMINE line keys off
   `compose_probe_undetermined`, not off `compose_probe_available`.
3. `ops.infra_status` -- the Compose card's scope is decided by what answers,
   not by `_in_cluster()` (which #3988 could only ever make half-right).
4. `ops.doctor` -- the Cassandra check is a native/cluster pair.

Throughout: the honest degradation stays. A host with neither substrate still
reports CANNOT DETERMINE with its reason, and never "absent".
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import cli, install_mode, ops, self_heal


class _CP:
    """The subset of `CompletedProcess` these callers read."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _pod(name: str, tier: str, *, healthy: bool = True) -> self_heal.ComponentStatus:
    return self_heal.ComponentStatus(
        name,
        name,
        "Running" if healthy else "Failed",
        "ready" if healthy else "not-ready",
        healthy,
        source="kubernetes",
        tier=tier,
    )


# The owner's instance, reduced to what decides the routing: core Pods plus the
# observability tier, in the cluster, with the Compose probe failing exactly the
# way it failed there.
K3S_PODS = [
    _pod("nyxgpt-api-stable-7c9f", "core"),
    _pod("cassandra-0", "core"),
    _pod("grafana-8686574f7b-d7vjf", "observability"),
]

EXIT_125 = (
    "`docker compose ps` exited 125: permission denied while trying to connect to the "
    "Docker daemon socket"
)


# --- 1. the self-heal survey asks the cluster first --------------------------


@pytest.fixture
def _k3s_host(monkeypatch):
    """A host whose cluster answers and whose Docker socket does not."""
    monkeypatch.setattr(self_heal, "_which", lambda _prog: "/usr/bin/x")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda _name: "absent")
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda _profiles: {"grafana"})
    monkeypatch.setattr(
        self_heal, "_list_kubernetes_component_status", lambda _managed: list(K3S_PODS)
    )
    monkeypatch.setattr(self_heal, "list_intentionally_stopped", lambda: [])


@pytest.mark.unit
def test_the_compose_probe_is_not_run_at_all_when_the_cluster_holds_the_tier(
    monkeypatch, _k3s_host
):
    """The routing fix, at its narrowest.

    The survey used to run `docker compose ps` before it knew the deployment
    mode and then throw the result away -- paying for a probe it could not use
    and logging its failure, naming a `docker-compose.yml` that host does not
    use, on every 15-second pass.
    """
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: pytest.fail("the Compose probe must not run when the cluster holds the tier"),
    )

    survey = self_heal.component_survey()

    assert survey.compose_probe.applicable is False
    assert survey.compose_probe.undetermined is False
    assert {s.service for s in survey.components} == {p.service for p in K3S_PODS}
    # Not one row is "can't check": every component of the tier was readable.
    assert all(s.known for s in survey.components)


@pytest.mark.unit
def test_the_not_applicable_reason_names_no_compose_file_and_no_exit_code(monkeypatch, _k3s_host):
    """AC6. The reason is what sent the operator looking for a Compose stack
    that was never meant to exist on that instance."""
    monkeypatch.setattr(self_heal, "compose_probe", lambda: pytest.fail("not asked"))

    reason = self_heal.component_survey().compose_probe.reason

    assert reason
    assert "docker-compose.yml" not in reason
    assert "docker compose ps" not in reason
    assert "125" not in reason
    # It says where the answer DID come from, which is the actionable half.
    assert "cluster" in reason


@pytest.mark.unit
def test_a_compose_host_with_no_cluster_still_surveys_compose(monkeypatch):
    """AC4, and the regression this change could most easily have caused."""
    monkeypatch.setattr(self_heal, "_which", lambda _prog: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda _name: "absent")
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_list_kubernetes_component_status", lambda _managed: [])
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda _profiles: {"grafana"})
    monkeypatch.setattr(self_heal, "list_intentionally_stopped", lambda: [])
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: self_heal.ComposeProbe(
            available=True,
            statuses=(
                self_heal.ComponentStatus(
                    "grafana", "nyxgpt-grafana-1", "running", "healthy", True, source="compose"
                ),
            ),
        ),
    )

    survey = self_heal.component_survey()
    by_service = {s.service: s for s in survey.components}

    assert survey.compose_probe.applicable is True
    assert by_service["grafana"].source == "compose"
    assert by_service["grafana"].healthy is True


@pytest.mark.unit
def test_a_host_with_neither_substrate_still_reports_unknown_never_absent(monkeypatch):
    """AC5. The honest degradation #3812 built is the correct answer here and
    must survive the routing change -- `unknown`, with the reason, not
    `absent`."""
    monkeypatch.setattr(self_heal, "_which", lambda _prog: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda _name: "absent")
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_list_kubernetes_component_status", lambda _managed: [])
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda _profiles: {"grafana"})
    monkeypatch.setattr(self_heal, "list_intentionally_stopped", lambda: [])
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: self_heal.ComposeProbe(available=False, reason=EXIT_125),
    )

    survey = self_heal.component_survey()
    grafana = next(s for s in survey.components if s.service == "grafana")

    assert survey.compose_probe.undetermined is True
    assert grafana.known is False
    assert grafana.state == "unknown"
    assert grafana.state != "absent"
    assert EXIT_125 in grafana.note


@pytest.mark.unit
def test_an_observability_only_cluster_does_not_blind_the_compose_survey(monkeypatch):
    """The gate is a CORE tier in the cluster, as #3828 set it.

    `nyxgpt ops observability --kubernetes` can put that one tier on a cluster
    while the core stack runs natively, and then the Compose survey is still
    the question about this deployment.
    """
    monkeypatch.setattr(self_heal, "_which", lambda _prog: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda _name: "absent")
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda _profiles: {"grafana"})
    monkeypatch.setattr(self_heal, "list_intentionally_stopped", lambda: [])
    monkeypatch.setattr(
        self_heal,
        "_list_kubernetes_component_status",
        lambda _managed: [_pod("grafana-7f9", "observability")],
    )
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: self_heal.ComposeProbe(available=False, reason=EXIT_125),
    )

    survey = self_heal.component_survey()

    assert survey.compose_probe.applicable is True
    assert survey.compose_probe.undetermined is True


@pytest.mark.unit
def test_status_carries_the_applicability_to_every_surface(monkeypatch):
    """AC2. `observability_source` alone was not enough: it existed before this
    issue and the Self-Heal page already read it, while the CLI and the
    Infrastructure page keyed off `compose_probe_available` and disagreed with
    it about the same instance."""
    monkeypatch.setattr(
        self_heal,
        "component_survey",
        lambda: self_heal.ComponentSurvey(
            components=list(K3S_PODS),
            compose_probe=self_heal._compose_not_applicable_probe(),
        ),
    )

    payload = self_heal.status()

    assert payload["mode"] == "kubernetes"
    assert payload["observability_source"] == "kubernetes"
    assert payload["compose_probe_applicable"] is False
    assert payload["compose_probe_undetermined"] is False
    assert payload["unknown_count"] == 0


# --- 2. the CLI ---------------------------------------------------------------


def _self_heal_payload(**overrides) -> dict:
    payload = {
        "enabled": True,
        "mode": "kubernetes",
        "observability_source": "kubernetes",
        "compose_probe_available": False,
        "compose_probe_applicable": False,
        "compose_probe_undetermined": False,
        "compose_probe_reason": self_heal.COMPOSE_NOT_APPLICABLE_REASON,
        "components": [p.to_dict() for p in K3S_PODS],
        "unhealthy_count": 0,
        "unknown_count": 0,
        "events": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_the_cli_does_not_print_cannot_determine_on_a_kubernetes_deployment(monkeypatch, capsys):
    """AC1 and AC3, and the literal line the owner reported.

    The fault injection is in the same assertion: the payload below carries
    `compose_probe_available: False`, which is what the old reading keyed off,
    so a regression to it reinstates the exact banner.
    """
    monkeypatch.setattr(cli.self_heal_mod, "status", _self_heal_payload)

    rc = cli.cmd_self_heal_status(None)
    out = capsys.readouterr().out

    assert rc == 0
    assert "CANNOT DETERMINE" not in out
    assert "docker-compose.yml" not in out
    # And it says where the rows below DID come from.
    assert "read from the cluster" in out
    assert "[OK] cassandra-0" in out


@pytest.mark.unit
def test_the_cli_still_prints_cannot_determine_when_an_answer_was_owed(monkeypatch, capsys):
    """AC5 on the CLI: #3812's banner is correct on a Compose host whose probe
    could not run, and must not have been traded away."""
    monkeypatch.setattr(
        cli.self_heal_mod,
        "status",
        lambda: _self_heal_payload(
            mode="compose",
            observability_source="compose",
            compose_probe_applicable=True,
            compose_probe_undetermined=True,
            compose_probe_reason=EXIT_125,
        ),
    )

    cli.cmd_self_heal_status(None)
    out = capsys.readouterr().out

    assert "Observability survey: CANNOT DETERMINE from here" in out
    assert EXIT_125 in out


@pytest.mark.unit
def test_the_cli_reads_an_older_api_payload_exactly_as_it_did(monkeypatch, capsys):
    """`nyxgpt cloud ops self-heal` runs the CLI against a remote api that may
    be a release behind, so the two new keys have to be optional and the
    fallback has to be the old behaviour."""
    legacy = _self_heal_payload(mode="compose", observability_source="compose")
    for key in ("compose_probe_applicable", "compose_probe_undetermined"):
        legacy.pop(key)
    legacy["compose_probe_reason"] = EXIT_125
    monkeypatch.setattr(cli.self_heal_mod, "status", lambda: legacy)

    cli.cmd_self_heal_status(None)

    assert "CANNOT DETERMINE" in capsys.readouterr().out


# --- 3. ops.infra_status ------------------------------------------------------


def _infra_host(monkeypatch, *, pods: list[str], compose_probe, in_cluster: bool = False) -> None:
    """`infra_status` with a cluster holding `pods` and a given Compose probe."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_in_cluster", lambda: in_cluster)
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/bin/x" if prog in ("kubectl", "docker") else None
    )

    def fake_run(cmd, check=True, **_kwargs):
        if "pods" in cmd:
            return _CP(
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": name},
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                            }
                            for name in pods
                        ]
                    }
                )
            )
        return _CP(stdout="k3s-nyxgpt\n" if "config" in cmd else "")

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops.self_heal, "compose_probe", compose_probe)


@pytest.mark.unit
def test_the_compose_card_is_out_of_scope_on_a_kubernetes_host(monkeypatch):
    """The Infrastructure page's half of the owner's report.

    `web/src/app/admin/infrastructure/page.tsx` renders
    `inCluster ? 'NOT IN SCOPE' : 'CANNOT DETERMINE'`, so on a k3s instance
    reached over the wrapped tunnel it landed on CANNOT DETERMINE and named
    `/home/ec2-user/.nyxGPT/docker-compose.yml` as the cause -- above its own
    list of fourteen ready Pods. The page now reads the api's scope verdict.
    """
    _infra_host(
        monkeypatch,
        pods=["nyxgpt-api-stable-7c9f", "cassandra-0", "grafana-868"],
        compose_probe=lambda: self_heal.ComposeProbe(available=False, reason=EXIT_125),
    )

    result = ops.infra_status()

    assert result["in_cluster"] is False
    assert result["kubernetes"]["deployed"] is True
    assert result["compose_in_scope"] is False
    assert "docker-compose.yml" not in result["compose_out_of_scope_reason"]
    assert "Kubernetes Pods" in result["compose_out_of_scope_reason"]
    # The reason the badge renders travels with it, so the two cannot diverge.
    assert result["compose_probe_reason"] == result["compose_out_of_scope_reason"]


@pytest.mark.unit
def test_a_working_compose_survey_stays_in_scope_even_beside_a_cluster(monkeypatch):
    """AC4 on this surface. A host that runs both has a real Compose answer,
    and hiding it would also hide the dual-stack conflict next to it."""
    _infra_host(
        monkeypatch,
        pods=["nyxgpt-api-stable-7c9f"],
        compose_probe=lambda: self_heal.ComposeProbe(available=True),
    )

    result = ops.infra_status()

    assert result["kubernetes"]["deployed"] is True
    assert result["compose_in_scope"] is True
    assert result["compose_out_of_scope_reason"] == ""
    assert result["compose_probe_available"] is True


@pytest.mark.unit
def test_a_host_with_no_cluster_and_a_failed_probe_still_cannot_determine(monkeypatch):
    """AC5 on this surface: in scope, unanswered, with the reason."""
    _infra_host(
        monkeypatch,
        pods=[],
        compose_probe=lambda: self_heal.ComposeProbe(available=False, reason=EXIT_125),
    )

    result = ops.infra_status()

    assert result["kubernetes"]["deployed"] is False
    assert result["compose_in_scope"] is True
    assert result["compose_probe_available"] is False
    assert result["compose_probe_reason"] == EXIT_125


@pytest.mark.unit
def test_inside_a_pod_the_compose_probe_is_not_run_at_all(monkeypatch):
    """#3988's scoping, kept -- and now it also costs nothing: a survey with no
    subject should not be paid for either."""
    _infra_host(
        monkeypatch,
        pods=["nyxgpt-api-stable-7c9f"],
        compose_probe=lambda: pytest.fail("no host filesystem in a Pod: nothing to survey"),
        in_cluster=True,
    )

    result = ops.infra_status()

    assert result["compose_in_scope"] is False
    assert "inside a Kubernetes Pod" in result["compose_out_of_scope_reason"]


# --- 4. ops.doctor's Cassandra check -----------------------------------------


def _doctor_on_a_bare_host(monkeypatch, tmp_path) -> None:
    """Strip `doctor` down to the Cassandra branch."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda _tool: None)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "no-checkout")
    (tmp_path / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".nyxGPT" / "config.ini").write_text("[api]\nhost = 127.0.0.1\n")
    for name in (
        "_stale_venv_doctor_issues",
        "_observability_volume_doctor_issues",
        "_glitchtip_secrets_doctor_issues",
        "_k8s_access_bridge_issues",
        "_terraform_install_mode_issues",
        "_running_api_build_doctor_issues",
        "_stale_terraform_state_issues",
        "_dual_stack_conflict_issues",
        "_ops_script_permission_issues",
        "_missing_native_tool_issues",
        "_web_dependency_doctor_issues",
        "_compose_restart_loop_issues",
        "_dev_install_checkout_issues",
        "_k8s_dev_install_checkout_issues",
    ):
        monkeypatch.setattr(ops, name, lambda *_a, **_k: [])
    monkeypatch.setattr(ops, "_foreign_native_service_issues", lambda _identity: [])
    monkeypatch.setattr(ops, "_host_config_doctor_issues", lambda: ([], None))
    for name in (
        "_log_aggregation_wiring_issue",
        "_tracing_wiring_issue",
        "_k8s_tracing_wiring_issue",
        "_tracing_packages_doctor_issue",
        "_prometheus_api_scrape_issue",
        "_k8s_prometheus_api_scrape_issue",
        "_insecure_api_bind_issue",
        "_ollama_env_drift_issue",
        "_linux_ollama_port_conflict_issue",
        "_missing_required_models_issue",
        "_docker_access_doctor_issue",
        "_error_tracking_dsn_drift_issue",
        "_k8s_error_tracking_dsn_drift_issue",
    ):
        monkeypatch.setattr(ops, name, lambda *_a, **_k: None)
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )


def _cluster_pods(*entries: tuple[str, str]) -> list[ops.K8sWorkloadState]:
    return [
        ops.K8sWorkloadState(name=name, state=state, summary=state.title())
        for name, state in entries
    ]


@pytest.mark.unit
def test_doctor_does_not_report_a_missing_local_cassandra_container_on_a_cluster(
    monkeypatch, tmp_path, capsys
):
    """AC7, verbatim: the FAIL the owner reported, on a healthy deployment.

    The fault injection is the host probe below -- it is wired to answer
    `absent`, exactly as it did on that instance, so a regression to the host
    reading reinstates the finding and this test fails.
    """
    _doctor_on_a_bare_host(monkeypatch, tmp_path)
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    monkeypatch.setattr(
        ops,
        "_k8s_deployment_probe",
        lambda: ops.K8sDeploymentProbe(
            _cluster_pods(
                ("nyxgpt-api-stable-7c9f", ops.K8S_STATE_READY),
                ("cassandra-0", ops.K8S_STATE_READY),
            )
        ),
    )
    monkeypatch.setattr(
        ops,
        "_docker_container_probe",
        lambda _name: pytest.fail("the host's containers must not answer for the cluster"),
    )

    rc = ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert "Missing local Cassandra container" not in out
    assert "nyxGPT ops doctor: OK" in out
    assert rc == 0
    # AC8: the report names Cassandra among the cluster-scoped checks, so the
    # operator is never left to work out which machine answered.
    assert "Cassandra, model readiness, tracing wiring" in out


@pytest.mark.unit
def test_the_host_half_still_reports_a_missing_container_without_a_cluster(
    monkeypatch, tmp_path, capsys
):
    """The local-first deployment's finding is real and must survive: this is
    the check that tells an operator their session store was never created."""
    _doctor_on_a_bare_host(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ops, "_k8s_deployment_probe", lambda: ops.K8sDeploymentProbe([], "kubectl not found")
    )
    monkeypatch.setattr(ops, "_which", lambda tool: "/usr/bin/docker" if tool == "docker" else None)
    monkeypatch.setattr(
        ops,
        "_docker_container_probe",
        lambda _name: ops.ContainerProbe(state="absent", known=True),
    )
    monkeypatch.setattr(
        ops,
        "_k8s_cassandra_deployment_issues",
        lambda _pods: pytest.fail("no cluster, nothing to ask it about"),
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 2
    assert f"Missing local Cassandra container: {ops.CASSANDRA_CONTAINER_NAME}" in out


@pytest.mark.unit
def test_a_cluster_whose_cassandra_pod_is_failed_is_still_a_finding():
    """A branch that could only ever return nothing would trade the owner's
    false positive for a blind spot -- the lesson `_k8s_tracing_wiring_issue`
    was built on."""
    issues = ops._k8s_cassandra_deployment_issues(
        _cluster_pods(
            ("nyxgpt-api-stable-7c9f", ops.K8S_STATE_READY),
            ("cassandra-0", ops.K8S_STATE_FAILED),
        )
    )

    assert len(issues) == 1
    assert "cassandra-0" in issues[0]
    # The Kubernetes remedy, never `nyxgpt ops install` (which on that instance
    # would have been the wrong action for a problem that did not exist).
    assert "--kubernetes" in issues[0]
    assert "nyxgpt ops install)" not in issues[0]


@pytest.mark.unit
def test_a_pending_cassandra_pod_is_not_a_finding():
    """Pending is not a failure anywhere else in this module and is not one
    here: a Cassandra booting an empty data directory is doing its job."""
    assert (
        ops._k8s_cassandra_deployment_issues(_cluster_pods(("cassandra-0", ops.K8S_STATE_PENDING)))
        == []
    )


@pytest.mark.unit
def test_a_cluster_with_no_cassandra_pod_at_all_is_a_finding():
    issues = ops._k8s_cassandra_deployment_issues(
        _cluster_pods(("nyxgpt-api-stable-7c9f", ops.K8S_STATE_READY))
    )

    assert len(issues) == 1
    assert ops.K8S_CASSANDRA_WORKLOAD in issues[0]
    assert "--kubernetes" in issues[0]


@pytest.mark.unit
def test_the_cluster_half_reads_the_pods_doctor_already_printed(monkeypatch):
    """It takes the Pod list rather than probing again, so it cannot contradict
    the `N/M pod(s) ready` line above it -- which is precisely what the host
    check did -- and costs no extra kubectl."""
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: pytest.fail("no second cluster read"))
    monkeypatch.setattr(ops, "_which", lambda _prog: pytest.fail("no probe at all"))

    assert ops._k8s_cassandra_deployment_issues(_cluster_pods(("cassandra-0", "ready"))) == []
