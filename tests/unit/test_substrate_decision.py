"""One substrate decision per run, and every check routed through it (#4184).

#4137 made the substrate choice depend on WHAT ANSWERS rather than on where
the process runs, and that was right for a deployment that is up. The owner's
re-test on a live k3s instance failed acceptance because the same class --
**a probe or record that answers for a substrate the run was not about** --
still had four instances, and inference could not have fixed any of them:

1. During the deploy, before a single Pod existed, `self_heal.component_survey`
   had nothing to infer from, fell through to `docker compose ps` and logged
   `exited 125 ... querying /home/ec2-user/.nyxGPT/docker-compose.yml` on an
   instance that was never meant to have a Compose stack. (AC6 violated.)
2. `ops doctor` printed `Install mode (native api/web): artifact
   (published/vendored build -- the repo-less default)` on an instance whose
   api and web run only as Pods, because its only gate was "is a native unit
   registered here".
3. `doctor` read `GET http://127.0.0.1:8000/api/v1/info`, got HTTP 401 --
   the host's `config.ini` carries no key, and the Pod answering there runs
   with the random one `_ensure_k8s_secret` minted into the cluster Secret --
   and reported nothing at all.
4. `ops session-backend`, run from the provisioning script BEFORE `ops
   install`, printed "Restart the API to pick this up (`nyxgpt ops restart
   api`)" on a box with no api to restart.

So the decision is made once, in `substrate.decide`, from evidence the caller
gathers, with the declaration arm first -- the arm inference structurally
cannot reach. This module pins the decision itself and each surface that now
routes through it; `test_substrate_probe_routing.py` keeps pinning #4137's
"what answers" rule, which this change preserves rather than replaces.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import install_mode, ops, self_heal, substrate

# --- 1. the decision itself ---------------------------------------------------


@pytest.mark.unit
def test_nothing_answering_is_its_own_substrate_not_native():
    """A half-built box is a real state, and it is not "native".

    Collapsing it into native is finding 4: the guidance a native install
    affords -- restart the api, read its config -- is exactly what is not
    available there.
    """
    decision = substrate.decide(substrate.Evidence())

    assert decision.substrate == substrate.SUBSTRATE_UNKNOWN
    assert decision.known is False
    assert decision.kubernetes is False
    assert decision.declared is False


@pytest.mark.unit
def test_what_answers_decides_when_nothing_declared():
    """#4137's rule, preserved: the cluster's core tier wins over a host survey."""
    decision = substrate.decide(
        substrate.Evidence(cluster_core_pods=True, compose_core=True, native_registered=True)
    )

    assert decision.substrate == substrate.SUBSTRATE_KUBERNETES
    assert decision.source == substrate.BY_CLUSTER
    assert decision.declared is False


@pytest.mark.unit
def test_an_observability_only_cluster_is_not_a_kubernetes_deployment():
    """The core/observability split #4137 drew, now expressed as evidence.

    `nyxgpt ops observability --kubernetes` can put that tier on a cluster
    while api/web/Cassandra/Ollama run natively, and that deployment's
    substrate is the native one -- which is why a Compose answer is still
    owed there.
    """
    decision = substrate.decide(substrate.Evidence(cluster_core_pods=False, native_registered=True))

    assert decision.substrate == substrate.SUBSTRATE_NATIVE


@pytest.mark.unit
def test_a_declaration_beats_every_piece_of_evidence():
    """The arm inference cannot reach, and the whole point of the fix.

    Mid-install there are no Pods and the host still looks native, so the
    evidence below is exactly what a `--kubernetes` install sees about itself.
    """
    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        decision = substrate.decide(substrate.Evidence(native_registered=True, compose_core=True))

    assert decision.substrate == substrate.SUBSTRATE_KUBERNETES
    assert decision.declared is True


@pytest.mark.unit
def test_the_declaration_reaches_subprocesses_and_is_restored_on_exit():
    """How the deploy keeps a whole provisioning script on one decision.

    The environment variable is the inter-process form, so every `nyxgpt` the
    script runs -- the session-backend write, the install, the watchdog step --
    routes the same way. And it is restored on the way out, because these same
    functions run inside the long-lived api server.
    """
    import os

    assert os.environ.get(substrate.SUBSTRATE_ENV_VAR) is None
    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        assert os.environ[substrate.SUBSTRATE_ENV_VAR] == "kubernetes"
    assert os.environ.get(substrate.SUBSTRATE_ENV_VAR) is None
    assert substrate.declared() is None


@pytest.mark.unit
def test_an_inherited_environment_declaration_is_honoured(monkeypatch):
    """What the provisioning script's `export NYXGPT_SUBSTRATE=` buys."""
    monkeypatch.setenv(substrate.SUBSTRATE_ENV_VAR, "kubernetes")

    decision = substrate.decide(substrate.Evidence(native_registered=True))

    assert decision.substrate == substrate.SUBSTRATE_KUBERNETES
    assert decision.source == substrate.BY_ENVIRONMENT
    assert decision.declared is True


@pytest.mark.unit
def test_an_unrecognised_environment_value_falls_back_to_inference(monkeypatch):
    """A typo must not route every check to a substrate that does not exist."""
    monkeypatch.setenv(substrate.SUBSTRATE_ENV_VAR, "kubernets")

    assert substrate.declared() is None
    assert substrate.decide(substrate.Evidence(native_registered=True)).substrate == "native"


@pytest.mark.unit
def test_declaring_an_unknown_substrate_is_refused():
    """Loudly, because the silent form of this is the inference it overrides."""
    with pytest.raises(ValueError, match="unknown substrate"), substrate.declared_as("k8s"):
        pass


@pytest.mark.unit
def test_the_bridge_holding_the_host_ports_answers_when_the_pod_read_fails():
    """A read that failed is not evidence that there is no cluster.

    The owner's #4137 step-8 condition -- `kubectl` on a k3s node defaulting
    to a root-only kubeconfig -- leaves the cluster serving :8000 through the
    access bridge while no Pod list can be had. Reporting "native" there hands
    the operator a host reading for the deployment that is answering.
    """
    decision = substrate.decide(
        substrate.Evidence(
            cluster_core_pods=False,
            kubernetes_recorded=True,
            kubernetes_owns_host_ports=True,
            native_registered=True,
        )
    )

    assert decision.substrate == substrate.SUBSTRATE_KUBERNETES
    assert decision.source == substrate.BY_HOST_PORTS


@pytest.mark.unit
def test_the_bridge_alone_is_not_enough_without_a_recorded_install():
    """k3s on PATH for someone else's cluster is not this product's deployment."""
    decision = substrate.decide(
        substrate.Evidence(kubernetes_owns_host_ports=True, native_registered=True)
    )

    assert decision.substrate == substrate.SUBSTRATE_NATIVE


@pytest.mark.unit
def test_the_substrate_names_have_exactly_one_definition():
    """One source per decision, applied to the vocabulary the decision uses.

    Three modules had defined their own copies of these strings, so a surface
    comparing one module's to another's was comparing by luck.
    """
    from nyxgpt import cloud_deploy

    assert install_mode.SUBSTRATE_KUBERNETES is substrate.SUBSTRATE_KUBERNETES
    assert install_mode.SUBSTRATE_NATIVE is substrate.SUBSTRATE_NATIVE
    assert install_mode.SUBSTRATE_TERRAFORM is substrate.SUBSTRATE_TERRAFORM
    assert cloud_deploy.SUBSTRATE_KUBERNETES is substrate.SUBSTRATE_KUBERNETES
    assert cloud_deploy.SUBSTRATE_NATIVE is substrate.SUBSTRATE_NATIVE


# --- 2. finding 1: the survey a deploy triggers as a side effect --------------


@pytest.fixture
def _box_being_built(monkeypatch):
    """An instance mid-`--kubernetes` deploy: no Pods yet, no reachable Docker.

    Exactly what `ops install --kubernetes` sees about itself at its first
    step, and what the owner's deploy logged a Compose failure from.
    """
    monkeypatch.setattr(self_heal, "_which", lambda _prog: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda _name: "absent")
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_list_kubernetes_component_status", lambda _managed: [])
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda _profiles: {"grafana"})
    monkeypatch.setattr(self_heal, "list_intentionally_stopped", lambda: [])


@pytest.mark.unit
def test_a_declared_kubernetes_run_never_probes_compose(monkeypatch, _box_being_built):
    """AC6, at the moment it was violated: during the deploy, with no Pods yet.

    Inference cannot get this right -- nothing answers during a deployment's
    own creation -- so the declaration is what makes the survey route.
    """
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: pytest.fail("a declared Kubernetes run must not probe Compose"),
    )

    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        survey = self_heal.component_survey()

    assert survey.compose_probe.applicable is False
    assert survey.compose_probe.undetermined is False
    assert "docker-compose.yml" not in survey.compose_probe.reason
    assert "docker compose ps" not in survey.compose_probe.reason
    assert "125" not in survey.compose_probe.reason


@pytest.mark.unit
def test_the_same_box_with_no_declaration_still_surveys_compose(monkeypatch, _box_being_built):
    """The fault injection, stated as a test: the fixture above is a box where
    the old reading DID probe Compose, so the test above measures the
    declaration and not something else."""
    probed: list[bool] = []

    def _probe() -> self_heal.ComposeProbe:
        probed.append(True)
        return self_heal.ComposeProbe(available=False, reason="exited 125")

    monkeypatch.setattr(self_heal, "compose_probe", _probe)

    survey = self_heal.component_survey()

    assert probed == [True]
    assert survey.compose_probe.undetermined is True


@pytest.mark.unit
def test_the_declared_scope_statement_does_not_point_at_rows_that_do_not_exist(
    monkeypatch, _box_being_built
):
    """Finding 4's rule, applied to a scope statement: true when printed.

    "the core stack runs as Kubernetes Pods ... see the rows below" is the
    right sentence for a cluster that is serving and the wrong one for an
    install that has created nothing.
    """
    monkeypatch.setattr(self_heal, "compose_probe", lambda: pytest.fail("not asked"))

    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        reason = self_heal.component_survey().compose_probe.reason

    assert "rows below" not in reason
    assert "Kubernetes" in reason


@pytest.mark.unit
def test_the_status_payload_reports_the_decision_it_actually_used(monkeypatch, _box_being_built):
    """`observability_source` is read off the survey's decision, not re-derived.

    A second `kubernetes_mode_active(components)` call here labelled the tier
    `compose` on a pass whose Compose survey the survey itself had just
    declined to make -- one payload carrying both answers.
    """
    monkeypatch.setattr(self_heal, "compose_probe", lambda: pytest.fail("not asked"))
    monkeypatch.setattr(self_heal, "is_enabled", lambda: True)

    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        payload = self_heal.status()

    assert payload["observability_source"] == "kubernetes"
    assert payload["substrate"] == "kubernetes"
    assert payload["substrate_declared"] is True
    assert payload["substrate_source"]


@pytest.mark.unit
def test_a_hand_assembled_survey_still_derives_its_substrate_from_its_rows():
    """The compatibility this must not break: a caller holding rows and nothing
    else gets exactly the pre-#4184 reading, through the same decision
    function."""
    survey = self_heal.ComponentSurvey(
        components=[
            self_heal.ComponentStatus(
                "cassandra-0",
                "cassandra-0",
                "Running",
                "ready",
                True,
                source="kubernetes",
                tier="core",
            )
        ],
        compose_probe=self_heal._compose_not_applicable_probe(),
    )

    assert survey.substrate_decision.kubernetes is True


@pytest.mark.unit
def test_the_cli_says_which_substrate_the_rows_are_about(monkeypatch, capsys, _box_being_built):
    """The surface the owner reads over `nyxgpt cloud ops self-heal`.

    An operator comparing that output with the dashboard must not be shown two
    different deployments for one instance, so the CLI prints the same pair the
    three pages render, from the one decision.
    """
    from nyxgpt import cli

    monkeypatch.setattr(self_heal, "compose_probe", lambda: pytest.fail("not asked"))
    monkeypatch.setattr(self_heal, "is_enabled", lambda: True)

    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        cli.cmd_self_heal_status(None)
    out = capsys.readouterr().out

    assert "Substrate: kubernetes" in out
    assert "CANNOT DETERMINE" not in out
    assert "docker-compose.yml" not in out


@pytest.mark.unit
def test_the_cli_prints_no_substrate_line_for_an_older_api_payload(monkeypatch, capsys):
    """`cloud ops self-heal` reads the payload of the api ON THE INSTANCE, which
    can predate this change -- and then there is no decision to report."""
    from nyxgpt import cli

    monkeypatch.setattr(
        self_heal_mod_status_owner(),
        "status",
        lambda: {
            "enabled": True,
            "mode": "native",
            "components": [],
            "events": [],
            "unhealthy_count": 0,
            "compose_probe_available": True,
        },
    )

    cli.cmd_self_heal_status(None)

    assert "Substrate:" not in capsys.readouterr().out


def self_heal_mod_status_owner():
    """The module `cli` calls `status()` on -- imported there under an alias."""
    from nyxgpt import cli

    return cli.self_heal_mod


# --- 3. finding 2: a record that describes nothing that exists ---------------


@pytest.fixture
def _k3s_instance(monkeypatch, tmp_path):
    """The owner's instance: api/web are Pods, with a native marker on disk.

    The marker is the thing under test. `cloud deploy --kubernetes` writes the
    Kubernetes one, and a box that ever ran a native install (or simply has
    the artifact default read back) also answers for a native one.

    Every check whose subject is not this test is stubbed out, the way
    `test_substrate_probe_routing._doctor_on_a_bare_host` does it: `doctor`
    runs twenty-odd checks and the claim here is about which substrate ONE
    record and ONE branch answered for.
    """
    monkeypatch.setattr(ops, "_in_cluster", lambda: False)
    monkeypatch.setattr(ops, "_is_macos", lambda: False)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
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
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_NATIVE
    )
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )


def _k8s_probe(ops_module, monkeypatch, *, pods=("nyxgpt-api-stable-7c9f", "cassandra-0")):
    """Point `_k8s_deployment_probe` at a cluster running the core tier."""
    states = [
        SimpleNamespace(name=name, state="ready", summary="Running", details="") for name in pods
    ]
    monkeypatch.setattr(
        ops_module,
        "_k8s_deployment_probe",
        lambda: ops_module.K8sDeploymentProbe(states, f"nyxgpt namespace: {len(states)} pod(s)"),
    )
    return states


@pytest.mark.unit
def test_doctor_does_not_print_a_native_install_mode_for_a_cluster_served_host(
    monkeypatch, capsys, _k3s_instance
):
    """Finding 2. The record is real; it just does not describe what serves.

    The gate used to be "is a native unit registered here", which on the
    owner's instance was true of a unit left behind and said nothing about the
    api that was actually answering.
    """
    _k8s_probe(ops, monkeypatch)
    monkeypatch.setattr(
        ops, "_native_services_snapshot", lambda: {"api": "started", "web": "started"}
    )

    ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert "Install mode (native api/web):" not in out
    # Not hidden -- repositioned as the dated history it is, by the renderer
    # `ops status` uses for the same marker (#4182).
    assert ops.INSTALL_HISTORY_HEADING in out
    assert "native api/web" in out


@pytest.mark.unit
def test_a_native_service_really_running_beside_the_cluster_is_a_finding(
    monkeypatch, _k3s_instance
):
    """Withdrawing the claim must not mean going quiet about a live conflict.

    A native api and the access bridge both want :8000, and whichever won the
    bind is what the operator is talking to. That is the thing to report --
    it was being presented as `Install mode (native api/web): artifact`.
    """
    decision = substrate.SubstrateDecision(substrate.SUBSTRATE_KUBERNETES, substrate.BY_CLUSTER)

    issues = ops._native_on_cluster_conflict_issues({"api": "started", "web": "none"}, decision)

    assert len(issues) == 1
    assert "native api" in issues[0]
    assert "nyxgpt ops down" in issues[0]


@pytest.mark.unit
def test_a_registered_but_stopped_native_service_is_not_a_conflict():
    """What `ops down` leaves behind on every converted instance.

    A finding here would fire on every box that has ever switched substrate,
    which is how a check teaches operators to ignore the list.
    """
    decision = substrate.SubstrateDecision(substrate.SUBSTRATE_KUBERNETES, substrate.BY_CLUSTER)

    assert ops._native_on_cluster_conflict_issues({"api": "none", "web": "none"}, decision) == []


@pytest.mark.unit
def test_the_conflict_check_is_silent_on_a_native_deployment():
    """The whole point of the host-scoped pair: a native box IS its own answer."""
    decision = substrate.SubstrateDecision(substrate.SUBSTRATE_NATIVE, substrate.BY_HOST)

    assert (
        ops._native_on_cluster_conflict_issues({"api": "started", "web": "started"}, decision) == []
    )


@pytest.mark.unit
def test_doctor_asks_the_cluster_about_cassandra_when_the_run_declared_kubernetes(
    monkeypatch, capsys, _k3s_instance
):
    """The host half's remedy on a Kubernetes box would install a second stack.

    `k8s_deployed` is False here -- no Pods -- and the old branch therefore
    took the host half, which reports "Missing local Cassandra container (run:
    nyxgpt ops install)". The cluster half says the honest thing about the
    same evidence.
    """
    monkeypatch.setattr(
        ops,
        "_k8s_deployment_probe",
        lambda: ops.K8sDeploymentProbe([], "nyxgpt namespace: 0 pod(s)"),
    )
    monkeypatch.setattr(ops, "_native_services_snapshot", lambda: {})
    # No Kubernetes marker yet: `_record_k8s_install_mode` is a later step of
    # the very install this run is in, so the marker-gated block cannot speak
    # for it. This is the gap the declaration fills.
    install_mode.clear_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    monkeypatch.setattr(
        ops,
        "_cassandra_deployment_issues",
        lambda: pytest.fail("the host Cassandra check must not answer for a Kubernetes run"),
    )

    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert "Missing local Cassandra container" not in out
    assert "has no Cassandra Pod" in out
    # And it says which substrate the host-scoped checks below are about, which
    # the marker-gated block could not do for a declared run.
    assert "Substrate: kubernetes" in out


# --- 4. finding 3: an unauthenticated read reported as nothing ---------------


@pytest.mark.unit
def test_the_internal_api_read_uses_the_cluster_secret_on_a_kubernetes_deployment(monkeypatch):
    """Finding 3's cause. The host's config.ini is the wrong credential there.

    A non-interactive `cloud deploy --kubernetes` has no `--api-key`, so
    `_ensure_k8s_secret` mints a random one into the cluster. The host keeps
    the example config's empty key, so every host-side read of
    `/api/v1/info` on that instance got HTTP 401 forever -- with the right key
    sitting in a Secret the same command can read.
    """
    monkeypatch.setattr(
        ops, "_k8s_secret_entry", lambda key: "cluster-key" if key == "api-key" else ""
    )

    key, source = ops._serving_api_key(
        substrate.SubstrateDecision(substrate.SUBSTRATE_KUBERNETES, substrate.BY_CLUSTER)
    )

    assert key == "cluster-key"
    assert ops.K8S_APP_SECRET_NAME in source


@pytest.mark.unit
def test_the_internal_api_read_falls_back_to_the_host_config(monkeypatch):
    """A cluster whose Secret this process cannot read behaves as it did before."""
    monkeypatch.setattr(ops, "_k8s_secret_entry", lambda _key: "")
    monkeypatch.setattr(ops, "load_config", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("nyxgpt.config.get_auth_api_key", lambda _cfg: "host-key")

    key, source = ops._serving_api_key(
        substrate.SubstrateDecision(substrate.SUBSTRATE_KUBERNETES, substrate.BY_CLUSTER)
    )

    assert key == "host-key"
    assert source == ops.NATIVE_CONFIG_HINT


@pytest.mark.unit
def test_a_native_deployment_never_pays_for_a_cluster_secret_read(monkeypatch):
    """First principle 1: the question only arises where the cluster serves."""
    monkeypatch.setattr(
        ops, "_k8s_secret_entry", lambda _key: pytest.fail("no cluster is serving this host")
    )
    monkeypatch.setattr(ops, "load_config", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("nyxgpt.config.get_auth_api_key", lambda _cfg: "host-key")

    key, _source = ops._serving_api_key(
        substrate.SubstrateDecision(substrate.SUBSTRATE_NATIVE, substrate.BY_HOST)
    )

    assert key == "host-key"


@pytest.mark.unit
def test_doctor_reports_a_read_it_could_not_make(monkeypatch):
    """Finding 3's symptom: the 401 was swallowed on every pass.

    A check that cannot make its read has not passed -- it has failed to run,
    and an operator is owed that.
    """
    monkeypatch.setattr(
        ops,
        "_native_api_build_drift",
        lambda: ops.BuildDrift(
            state=ops.BUILD_UNDETERMINED,
            running=None,
            expected_prefix="/keg/venv",
            expected_source="the keg",
            detail="http://127.0.0.1:8000/api/v1/info refused the probe (HTTP 401)",
            remediation="",
            unreadable=True,
        ),
    )

    issues = ops._running_api_build_doctor_issues()

    assert len(issues) == 1
    assert "could not read the api" in issues[0]
    assert "401" in issues[0]


@pytest.mark.unit
def test_doctor_stays_quiet_when_nothing_is_serving(monkeypatch):
    """The distinction that keeps the finding above worth printing.

    `doctor` runs on machines whose stack is deliberately down. A finding
    there is noise, and noise is what trains an operator to skip the list.
    """
    monkeypatch.setattr(
        ops,
        "_native_api_build_drift",
        lambda: ops.BuildDrift(
            state=ops.BUILD_UNDETERMINED,
            running=None,
            expected_prefix="/keg/venv",
            expected_source="the keg",
            detail="http://127.0.0.1:8000/api/v1/info did not answer (ConnectError)",
            remediation="",
        ),
    )

    assert ops._running_api_build_doctor_issues() == []


@pytest.mark.unit
def test_a_refusal_names_the_credential_that_was_refused(monkeypatch, real_running_api_probe):
    """ "does not accept the key" is not actionable until the operator knows
    WHICH key was offered -- the host's file, or the cluster's Secret.

    `real_running_api_probe` is requested by name because the unit conftest
    stubs this probe for every test by default -- no test may ask the
    developer's own machine what it is serving -- and this one is about the
    probe itself.
    """

    class _Resp:
        status_code = 401

    cfg = ConfigParser()
    cfg.add_section("auth")
    monkeypatch.setattr(ops, "load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.config.get_api_port", lambda _cfg: 8000)
    monkeypatch.setattr(
        ops, "_serving_api_key", lambda _d: ("k", "the nyxgpt-secrets Secret in the nyxgpt ns")
    )
    monkeypatch.setattr(ops.httpx, "get", lambda *_a, **_k: _Resp())

    probe = real_running_api_probe(
        substrate.SubstrateDecision(substrate.SUBSTRATE_KUBERNETES, substrate.BY_CLUSTER)
    )

    assert probe.build is None
    assert probe.unreadable is True
    assert "nyxgpt-secrets" in probe.reason
    # The 2-tuple every pre-#4184 caller unpacks still works.
    build, reason = probe
    assert build is None and reason == probe.reason


# --- 5. finding 4: guidance that was wrong when it was printed ---------------


@pytest.fixture
def _seeded_config(tmp_path, monkeypatch):
    """A `config.ini` as the provisioning script seeds it, before `ops install`."""
    cfg = tmp_path / "config.ini"
    cfg.write_text("[nyxgpt]\nsession_backend = file\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_native_services_snapshot", lambda: {})
    return cfg


@pytest.mark.unit
def test_the_session_backend_write_does_not_name_a_restart_mid_deploy(_seeded_config, monkeypatch):
    """Finding 4, exactly where the owner read it: before the api is installed.

    The script runs this BEFORE `ops install`, so `nyxgpt ops restart api`
    would have failed. What makes the value take effect there is the install
    that is about to run.
    """
    results = ops.set_session_backend("cassandra", cfg_path=_seeded_config)

    assert results[0].ok
    assert "nyxgpt ops restart api" not in results[0].details
    assert "nothing to restart" in results[0].details


@pytest.mark.unit
def test_the_session_backend_write_names_the_configmap_on_a_kubernetes_run(
    _seeded_config, monkeypatch
):
    """The api Pods read `session_backend` from the cluster's ConfigMap, so no
    restart on this host affects them -- and the file still governs every
    `nyxgpt` command run here, which is why the write is not refused."""
    with substrate.declared_as(substrate.SUBSTRATE_KUBERNETES):
        results = ops.set_session_backend("cassandra", cfg_path=_seeded_config)

    assert results[0].ok
    assert "nyxgpt ops restart api" not in results[0].details
    assert ops.K8S_CONFIG_CONFIGMAP in results[0].details


@pytest.mark.unit
def test_the_session_backend_write_still_names_the_restart_on_a_native_install(
    _seeded_config, monkeypatch
):
    """The regression this change could most easily have caused: on the
    substrate where the guidance was always right, it is unchanged."""
    monkeypatch.setattr(ops, "_native_services_snapshot", lambda: {"api": "started"})

    results = ops.set_session_backend("cassandra", cfg_path=_seeded_config)

    assert "Restart the API to pick this up (`nyxgpt ops restart api`)." in results[0].details


@pytest.mark.unit
def test_the_session_backend_write_pays_for_no_cluster_probe(_seeded_config, monkeypatch):
    """One line of config write must not grow a `kubectl get pods`."""
    monkeypatch.setattr(
        ops, "_k8s_deployment_probe", lambda: pytest.fail("a config write surveyed the cluster")
    )
    monkeypatch.setattr(
        ops, "_k8s_pod_states", lambda **_k: pytest.fail("a config write surveyed the cluster")
    )

    assert ops.set_session_backend("cassandra", cfg_path=_seeded_config)[0].ok


# --- 6. the deploy declares the substrate for its whole script ---------------


@pytest.mark.unit
@pytest.mark.parametrize("kubernetes", [True, False])
def test_the_provisioning_script_exports_the_substrate_before_any_nyxgpt_command(kubernetes):
    """How every survey a deploy triggers as a side effect gets routed.

    Asserted as an ORDERING, not merely as presence: a declaration after the
    first `nyxgpt` invocation would leave exactly the step the owner read the
    Compose warning from inferring its own substrate.
    """
    from nyxgpt import cloud_deploy

    args = SimpleNamespace(
        instance_id=None,
        version="3.0.1",
        profiles=None,
        session_backend=None,
        os_family="linux",
        kubernetes=kubernetes,
        dev=False,
        yes=True,
    )
    plan = cloud_deploy.resolve_plan(args)
    script = cloud_deploy.render_provision_script(plan)

    expected = "kubernetes" if kubernetes else "native"
    assert f'export NYXGPT_SUBSTRATE="{expected}"' in script
    assert script.index("NYXGPT_SUBSTRATE") < script.index('"$NYXGPT" ops ')
