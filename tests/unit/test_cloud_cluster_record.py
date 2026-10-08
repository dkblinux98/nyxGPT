"""The cloud deployment's own facts, recorded in its cluster (#4138).

The defect: the admin Infrastructure page served BY an EC2 `--kubernetes`
deployment reported `AWS substrate [UNKNOWN]` and `Cloud deployment [UNKNOWN]`
-- "it is neither an EC2 instance nor one that has provisioned the substrate"
-- while `nyxgpt cloud status` on that same instance printed the instance id,
type, region, public IP and version in full. The api Pod answered honestly for
itself and had nothing to answer with: a Pod reaches neither IMDS (link-local
is not routed into the Pod network) nor the host's `~/.nyxGPT/cloud/`.

The fix follows #3988's precedent exactly -- the install records the fact *in
the cluster*, and nothing probes the host from inside a Pod. These tests cover
the claims that makes:

- the record is built from the instance's own IMDS facts, and is NOT written
  when this machine is not an instance;
- `ops install --kubernetes` writes it as a step, beside the install-mode one;
- the read happens only from inside a Pod, is bounded, is cached, and treats
  a partial record as no record;
- both status surfaces -- `cloud_infra.infra_status` and
  `cloud_deploy.deploy_status`, which the CLI and the web UI share -- report
  the recorded values and name the vantage point they answered from;
- a workstation that neither ran the deploy nor is the instance still reports
  UNKNOWN with #3804's wording.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from nyxgpt import (
    canary,
    cloud_cluster_record,
    cloud_deploy,
    cloud_infra,
    ops,
    self_heal,
)

pytestmark = pytest.mark.unit


INSTANCE_FACTS = {
    "instance_id": "i-081ec19e9a96fa4ff",
    "instance_type": "m5.xlarge",
    "region": "us-east-1",
    "availability_zone": "us-east-1a",
    "public_ip": "184.193.4.58",
    "private_ip": "10.0.1.20",
    "vpc_id": "vpc-0def456",
    "subnet_id": "subnet-0aaa111",
    "security_group_id": "sg-0bbb222",
    "ssh_key_name": "nyxgpt-owner",
}


def _record(**overrides) -> dict[str, str]:
    """The record `ops install --kubernetes` writes on the owner's instance."""
    record = cloud_cluster_record.build_record(
        INSTANCE_FACTS, version="3.0.0rc17", substrate="kubernetes", os_family="linux"
    )
    record.update(overrides)
    return record


@pytest.fixture
def in_cluster(monkeypatch):
    """Make this process read as an api Pod, the vantage point #4138 is about."""
    monkeypatch.setenv(cloud_cluster_record.IN_CLUSTER_ENV, "10.43.0.1")
    cloud_cluster_record.reset_cache()
    return None


@pytest.fixture
def recorded_in_cluster(monkeypatch, in_cluster):
    """An api Pod of a deployment whose cluster carries the record."""
    monkeypatch.setattr(cloud_cluster_record, "_kubectl_get_configmap", lambda: dict(_record()))
    cloud_cluster_record.reset_cache()
    return _record()


# --- the record itself ------------------------------------------------------


def test_the_record_carries_every_fact_cloud_status_prints_first_hand():
    """AC2: the values must match what `nyxgpt cloud status` prints on the box."""
    record = _record()

    assert record["instance_id"] == "i-081ec19e9a96fa4ff"
    assert record["instance_type"] == "m5.xlarge"
    assert record["region"] == "us-east-1"
    assert record["public_ip"] == "184.193.4.58"
    assert record["version"] == "3.0.0rc17"
    assert record["substrate"] == "kubernetes"
    assert set(cloud_cluster_record.RECORD_FIELDS) <= set(record)


def test_no_record_is_built_off_an_ec2_instance():
    """A local kind cluster is not a cloud deployment, and must never claim to be.

    The decision "am I on EC2" is made in exactly one place -- the install,
    which reads IMDS on the host -- and handed in. No facts, no record, which
    is what keeps #3804's UNKNOWN honest for a workstation.
    """
    assert (
        cloud_cluster_record.build_record(
            None, version="3.0.1", substrate="kubernetes", os_family="linux"
        )
        == {}
    )
    assert (
        cloud_cluster_record.build_record(
            {"region": "us-east-1"}, version="3.0.1", substrate="kubernetes", os_family="linux"
        )
        == {}
    )


def test_a_record_with_no_instance_id_reads_back_as_no_record():
    """D-032: a half record is unknown, never a determinate rendering of one."""
    assert cloud_cluster_record.parse_record({"data": {"region": "us-east-1"}}) == {}
    assert cloud_cluster_record.parse_record({"data": {"instance_id": "   "}}) == {}
    assert cloud_cluster_record.parse_record("not a configmap") == {}
    assert cloud_cluster_record.parse_record({"data": None}) == {}
    assert cloud_cluster_record.parse_record({"data": dict(_record())})["instance_id"] == (
        "i-081ec19e9a96fa4ff"
    )


def test_the_manifest_lands_in_the_deployments_own_namespace():
    manifest = cloud_cluster_record.configmap_manifest(_record())

    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["name"] == cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP
    assert manifest["metadata"]["namespace"] == cloud_cluster_record.K8S_NAMESPACE
    assert manifest["data"]["instance_id"] == "i-081ec19e9a96fa4ff"


def test_the_namespace_constant_agrees_with_every_other_copy_of_it():
    """Duplicated to keep this module import-free of `ops`; never allowed to drift."""
    assert cloud_cluster_record.K8S_NAMESPACE == ops.K8S_NAMESPACE
    assert cloud_cluster_record.K8S_NAMESPACE == self_heal.K8S_NAMESPACE
    assert cloud_cluster_record.K8S_NAMESPACE == canary.DEFAULT_NAMESPACE


# --- the write half: the install, on the instance ---------------------------


def test_the_install_writes_the_record_from_the_instances_own_metadata(monkeypatch):
    applied: dict[str, object] = {}

    def fake_run(cmd, check=True, input=None, **_kwargs):
        applied["cmd"] = list(cmd)
        applied["manifest"] = json.loads(input)
        return SimpleNamespace(returncode=0, stdout="configmap/nyxgpt-cloud-deploy configured")

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "running_version", lambda: "3.0.0rc17")
    monkeypatch.setattr(ops, "_is_macos", lambda: False)
    monkeypatch.setattr(ops.cloud_imds, "instance_facts", lambda **_kw: dict(INSTANCE_FACTS))

    results = ops._record_k8s_cloud_deploy()

    assert [r.ok for r in results] == [True]
    assert applied["cmd"] == ["kubectl", "-n", ops.K8S_NAMESPACE, "apply", "-f", "-"]
    data = applied["manifest"]["data"]
    assert data["instance_id"] == "i-081ec19e9a96fa4ff"
    assert data["version"] == "3.0.0rc17"
    assert data["substrate"] == "kubernetes"
    assert data["os_family"] == "linux"


def test_the_install_writes_nothing_and_reports_nothing_off_ec2(monkeypatch):
    """A local `--kubernetes --local` install must stay silent and apply nothing."""

    def refuse(*_a, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("a non-EC2 install tried to write a cloud-deploy record")

    monkeypatch.setattr(ops, "_run", refuse)
    monkeypatch.setattr(ops.cloud_imds, "instance_facts", lambda **_kw: None)

    assert ops._record_k8s_cloud_deploy() == []


def test_a_failed_write_says_what_the_page_will_be_missing(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
    )
    monkeypatch.setattr(ops, "running_version", lambda: "3.0.0rc17")
    monkeypatch.setattr(ops.cloud_imds, "instance_facts", lambda **_kw: dict(INSTANCE_FACTS))

    results = ops._record_k8s_cloud_deploy()

    assert [r.ok for r in results] == [False]
    assert "Infrastructure page" in results[0].message


def test_the_kubernetes_install_runs_the_record_step_after_the_install_mode_one():
    """Both records are written by the install, in that order (#3988 then #4138)."""
    source = ops._install_kubernetes_steps.__code__.co_consts
    rendered = "\n".join(c for c in source if isinstance(c, str))

    assert "record install mode" in rendered
    assert "record cloud deployment" in rendered
    assert rendered.index("record install mode") < rendered.index("record cloud deployment")


# --- the read half: bounded, cached, and only from inside a Pod -------------


def test_nothing_is_read_off_cluster_and_no_subprocess_is_spawned(monkeypatch):
    """AC: a workstation pays no kubectl for a record about someone else's box."""

    def refuse():  # pragma: no cover - must not be reached
        raise AssertionError("an off-cluster process ran kubectl for the cluster record")

    monkeypatch.setattr(cloud_cluster_record, "_kubectl_get_configmap", refuse)

    assert cloud_cluster_record.read_cloud_deploy_record() == {}


def test_the_read_is_cached_so_a_polled_endpoint_pays_once(in_cluster, monkeypatch):
    calls: list[int] = []

    def once():
        calls.append(1)
        return dict(_record())

    monkeypatch.setattr(cloud_cluster_record, "_kubectl_get_configmap", once)

    first = cloud_cluster_record.read_cloud_deploy_record()
    second = cloud_cluster_record.read_cloud_deploy_record()

    assert first == second
    assert len(calls) == 1
    assert cloud_cluster_record.read_cloud_deploy_record(force=True)["instance_id"]
    assert len(calls) == 2


def test_a_refused_or_absent_configmap_reads_as_no_record(in_cluster, monkeypatch):
    """RBAC refusal, missing namespace, pre-#4138 deployment: all the same answer."""
    monkeypatch.setattr(
        cloud_cluster_record.subprocess,
        "run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            ["kubectl"], 1, stdout="", stderr='configmaps "nyxgpt-cloud-deploy" not found'
        ),
    )
    monkeypatch.setattr(cloud_cluster_record, "which", lambda _name: "/usr/bin/kubectl")

    assert cloud_cluster_record.read_cloud_deploy_record() == {}


def test_a_hung_read_is_a_result_not_a_traceback(in_cluster, monkeypatch):
    """A 500 on a polled status endpoint is the failure `subprocess_bounds` exists for."""

    def hang(*_a, **_kw):
        raise subprocess.TimeoutExpired(["kubectl"], 5.0)

    monkeypatch.setattr(cloud_cluster_record.subprocess, "run", hang)
    monkeypatch.setattr(cloud_cluster_record, "which", lambda _name: "/usr/bin/kubectl")

    assert cloud_cluster_record.read_cloud_deploy_record() == {}


def test_the_read_is_bounded_and_scoped_to_the_one_configmap(in_cluster, monkeypatch):
    seen: dict[str, object] = {}

    def capture(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": dict(_record())}))

    monkeypatch.setattr(cloud_cluster_record.subprocess, "run", capture)
    monkeypatch.setattr(cloud_cluster_record, "which", lambda _name: "/usr/bin/kubectl")

    assert cloud_cluster_record.read_cloud_deploy_record()["region"] == "us-east-1"
    assert seen["timeout"] == cloud_cluster_record.PROBE_TIMEOUT_SECONDS
    assert cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP in seen["cmd"]
    assert "get" in seen["cmd"]
    # Read-only from the Pod, by construction: the page reports the deployment
    # and can no more change it than before (D-017).
    assert not {"apply", "delete", "patch", "edit"} & set(seen["cmd"])


def test_no_kubectl_in_the_container_reads_as_no_record(in_cluster, monkeypatch):
    monkeypatch.setattr(cloud_cluster_record, "which", lambda _name: None)

    assert cloud_cluster_record.read_cloud_deploy_record() == {}


# --- the substrate card -----------------------------------------------------


def test_the_substrate_card_reports_the_instance_from_inside_the_cluster(
    recorded_in_cluster,
):
    """AC1: real values on the page the deployment serves, not UNKNOWN."""
    status = cloud_infra.infra_status()

    assert status["known"] is True
    assert status["provisioned"] is True
    assert status["source"] == cloud_infra.SOURCE_CLUSTER_RECORD
    assert status["instance_id"] == "i-081ec19e9a96fa4ff"
    assert status["instance_type"] == "m5.xlarge"
    assert status["region"] == "us-east-1"
    assert status["public_ip"] == "184.193.4.58"
    assert status["vpc_id"] == "vpc-0def456"
    assert status["security_group_id"] == "sg-0bbb222"
    # Same posture the CLI prints on the instance: one port, the operator's IP.
    assert status["access_model"]["open_ports"] == [22]
    # Still not knowable from here -- it is a security-group rule, not metadata
    # and not something the install recorded. Left empty rather than guessed.
    assert status["owner_ip_cidr"] == ""


def test_the_substrate_card_names_the_vantage_point_it_answered_from(recorded_in_cluster):
    """AC3: as #3988's Kubernetes card does."""
    label = cloud_infra.infra_status()["source_label"]

    assert cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP in label
    assert "inside the cluster" in label
    assert "instance" in label


def test_on_ec2_means_running_on_the_instance_whichever_source_said_so(recorded_in_cluster):
    """Every consumer of the flag asks that, and renders correctly for a Pod.

    The SSH allowed-from rule is as invisible from a Pod as from the host,
    Terraform state is as absent, and the access tunnel is as much somebody
    else's -- so the three places the page keys off `on_ec2` need the Pod to
    answer them exactly as the host does.
    """
    assert cloud_infra.infra_status()["on_ec2"] is True


def test_imds_still_wins_when_this_process_is_the_host(monkeypatch, in_cluster):
    """First-hand beats a record, so a host never answers from someone's ConfigMap."""
    monkeypatch.setattr(
        cloud_infra.cloud_imds,
        "instance_facts",
        lambda **_kw: {"instance_id": "i-from-imds", "region": "eu-west-2"},
    )

    def refuse():  # pragma: no cover - must not be reached
        raise AssertionError("the cluster record was read while IMDS was answering")

    monkeypatch.setattr(cloud_cluster_record, "_kubectl_get_configmap", refuse)

    status = cloud_infra.infra_status()

    assert status["source"] == cloud_infra.SOURCE_IMDS
    assert status["instance_id"] == "i-from-imds"


def test_a_workstation_that_is_neither_still_reports_unknown():
    """AC4: #3804's case is unchanged -- nothing here can answer, so it says so."""
    status = cloud_infra.infra_status()

    assert status["known"] is False
    assert status["source"] == cloud_infra.SOURCE_UNKNOWN
    assert status["on_ec2"] is False


def test_an_in_cluster_pod_with_no_record_still_reports_unknown(in_cluster, monkeypatch):
    """A local kind cluster, or a deployment made before #4138. Unknown, not a guess."""
    monkeypatch.setattr(cloud_cluster_record, "_kubectl_get_configmap", lambda: {})

    status = cloud_infra.infra_status()

    assert status["known"] is False
    assert status["source"] == cloud_infra.SOURCE_UNKNOWN
    assert status["on_ec2"] is False


# --- the deployment card ----------------------------------------------------


def test_the_deployment_card_reports_the_deployment_from_inside_it(
    recorded_in_cluster, monkeypatch
):
    """AC1/AC2 for the second card: DEPLOYED, with the values the CLI prints."""
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "3.0.0rc17")

    status = cloud_deploy.deploy_status()

    assert status["known"] is True
    assert status["deployed"] is True
    assert status["source"] == cloud_deploy.SOURCE_CLUSTER_RECORD
    assert status["on_instance"] is True
    assert status["version"] == "3.0.0rc17"
    assert status["instance_id"] == "i-081ec19e9a96fa4ff"
    assert status["instance_type"] == "m5.xlarge"
    assert status["region"] == "us-east-1"
    assert status["host"] == "184.193.4.58"
    assert status["substrate"] == "kubernetes"
    assert status["os_family"] == "linux"


def test_the_version_is_the_process_answering_not_the_recorded_one(
    recorded_in_cluster, monkeypatch
):
    """Same precedence #3988's card uses: in-cluster, this api IS the deployment.

    The record's version is what the install was built from, which a canary
    promotion or a redeploy can leave behind; the process serving the request
    cannot be out of date about itself.
    """
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "3.0.1")

    assert cloud_deploy.deploy_status()["version"] == "3.0.1"


def test_the_recorded_version_answers_when_the_process_cannot(recorded_in_cluster, monkeypatch):
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "")

    assert cloud_deploy.deploy_status()["version"] == "3.0.0rc17"


def test_a_deploy_record_on_this_machine_still_wins(recorded_in_cluster, monkeypatch):
    """The operator's workstation knows more than the cluster does, and keeps the answer."""
    monkeypatch.setattr(
        cloud_deploy,
        "load_deploy_state",
        lambda: {"host": "203.0.113.9", "version": "3.0.0", "instance_id": "i-recorded"},
    )

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_DEPLOY_RECORD
    assert status["host"] == "203.0.113.9"


def test_no_tunnel_probe_is_attempted_from_inside_the_deployment(recorded_in_cluster, monkeypatch):
    """The stack answering the request is the one being asked about."""

    def refuse(*_a, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("a Pod of the deployment probed itself through a tunnel")

    monkeypatch.setattr(cloud_deploy, "_probe", refuse)

    health = cloud_deploy.deploy_status(probe_health=True)["health"]

    assert health["checked"] is False
    assert "served from the instance" in health["reason"]


def test_cloud_status_names_the_cluster_record_as_its_vantage_point(
    recorded_in_cluster, monkeypatch, capsys
):
    """AC5: one endpoint, so the CLI prints what the page renders."""
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "3.0.0rc17")

    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
    out = capsys.readouterr().out

    assert "DEPLOYED" in out
    assert "api Pod" in out
    assert cloud_cluster_record.CLOUD_DEPLOY_CONFIGMAP in out
    assert "i-081ec19e9a96fa4ff" in out
    assert "184.193.4.58" in out
    assert "3.0.0rc17" in out
    # The posture row `nyxgpt cloud status` prints on the instance.
    assert "port 22 from" in out
