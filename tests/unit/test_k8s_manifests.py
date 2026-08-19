"""Guards on the `k8s/` manifests (#3786).

#3786 was an acceptance failure of the shape "everything reports healthy and
nothing works": the local Kubernetes deployment shipped only the api and web
pairs, pointed the api's Ollama URL at a host address no Pod can resolve, and
disabled the session store -- so the web UI loaded and no chat was possible.

These tests assert the properties that failure violated, and that a later
change cannot quietly violate again:

* the data tier (Cassandra) and LLM tier (Ollama) are part of the applied
  kustomization, not optional extras;
* the api's ConfigMap addresses them by their in-cluster Service names, with
  the ports those Services actually publish;
* the model Ollama pre-pulls is the model the api asks for by default;
* the container image pins stay identical to the ones every other deployment
  mode uses (docs/docker-compose.md#image-pinning).

They are static-manifest checks. What they cannot show -- that the deployed
result can actually answer a chat -- is covered by executed verification:
scripts/k8s-local-smoke.sh / .github/workflows/k8s-local-smoke.yml.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

import pytest
import yaml

from nyxgpt import canary

K8S_DIR = Path(__file__).resolve().parents[2] / "k8s"
REPO_ROOT = K8S_DIR.parent


def _load(name: str) -> dict[str, Any]:
    with (K8S_DIR / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _api_config() -> configparser.ConfigParser:
    """The api's config.ini as embedded in the ConfigMap."""
    cfg = configparser.ConfigParser()
    cfg.read_string(_load("configmap.yaml")["data"]["config.ini"])
    return cfg


def _container(workload: dict[str, Any], name: str) -> dict[str, Any]:
    containers = workload["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == name)


@pytest.mark.unit
def test_kustomization_includes_the_data_and_llm_tier():
    resources = _load("kustomization.yaml")["resources"]
    for manifest in (
        "statefulset-cassandra.yaml",
        "service-cassandra.yaml",
        "statefulset-ollama.yaml",
        "service-ollama.yaml",
    ):
        assert manifest in resources, f"{manifest} is not applied -- #3786 regression"


@pytest.mark.unit
def test_api_config_points_ollama_at_the_in_cluster_service():
    """`host.docker.internal` does not resolve from a Pod on Linux clusters, and
    the host's Ollama is stopped by `ops install --kubernetes --local` anyway."""
    base_url = _api_config().get("ollama", "base_url")
    svc = _load("service-ollama.yaml")
    port = svc["spec"]["ports"][0]["port"]
    assert base_url == f"http://{svc['metadata']['name']}:{port}"
    assert "host.docker.internal" not in base_url


@pytest.mark.unit
def test_api_config_points_cassandra_at_the_in_cluster_service():
    cfg = _api_config()
    svc = _load("service-cassandra.yaml")
    assert cfg.get("rag", "cassandra_hosts") == svc["metadata"]["name"]
    assert cfg.getint("rag", "cassandra_port") == svc["spec"]["ports"][0]["port"]


@pytest.mark.unit
def test_sessions_are_shared_across_replicas():
    """With the `file` backend each api replica keeps its own session list, so
    consecutive requests from one browser see different sessions -- the
    "Failed to load sessions" half of #3786. Stable rests at one replica
    since #3833, which would make the file backend look fine right up until
    a canary rollout grows the pool and forks the session list, so the store
    still has to be the shared one."""
    assert _api_config().get("nyxgpt", "session_backend") == "cassandra"
    resting = _load("deployment-stable.yaml")["spec"]["replicas"]
    assert canary._plan_rollout(resting, 25, 4).pool > 1


@pytest.mark.unit
@pytest.mark.parametrize("manifest", ["deployment-stable.yaml", "deployment-web-stable.yaml"])
def test_stable_deployments_rest_at_one_replica(manifest: str):
    """No install may carry a standing pool just to make canary possible (#3833).

    The pool is grown by `nyxgpt canary start` and handed back on
    promote/rollback (see canary._plan_rollout), so the manifests ship the
    resting count -- not the widest one a rollout might need.
    """
    assert _load(manifest)["spec"]["replicas"] == 1


@pytest.mark.unit
def test_ollama_prepulls_the_models_the_api_asks_for():
    """A Ready Ollama holding a different model than `default_model` answers
    every chat with a 404 -- and one without `[rag] embedding_model` fails the
    first request after RAG is toggled on (#3824)."""
    ollama = _container(_load("statefulset-ollama.yaml"), "ollama")
    env = {e["name"]: e["value"] for e in ollama["env"]}
    cfg = _api_config()
    assert env["NYXGPT_DEFAULT_MODEL"] == cfg.get("nyxgpt", "default_model")
    assert env["NYXGPT_EMBEDDING_MODEL"] == cfg.get("rag", "embedding_model")
    post_start = ollama["lifecycle"]["postStart"]["exec"]["command"][-1]
    assert "ollama pull" in post_start
    assert "NYXGPT_DEFAULT_MODEL" in post_start
    assert "NYXGPT_EMBEDDING_MODEL" in post_start


@pytest.mark.unit
def test_ollama_readiness_requires_both_models_not_just_the_port():
    """Endpoints must not appear while either model is still downloading, or the
    first chat (or first RAG-enabled) request races the pull."""
    ollama = _container(_load("statefulset-ollama.yaml"), "ollama")
    probe = ollama["readinessProbe"]
    assert "exec" in probe, "a port probe cannot tell whether the models are there"
    command = probe["exec"]["command"][-1]
    assert "NYXGPT_DEFAULT_MODEL" in command
    assert "NYXGPT_EMBEDDING_MODEL" in command


@pytest.mark.unit
def test_k8s_default_models_match_the_shipped_defaults():
    """Every run mode names the same two models (#3824): a deployment that
    quietly picks a different chat model is one the acceptance test for one
    mode cannot speak for."""
    cfg = _api_config()
    assert cfg.get("nyxgpt", "default_model") == "qwen3:0.6b"
    assert cfg.get("rag", "embedding_model") == "nomic-embed-text"


@pytest.mark.unit
def test_cassandra_readiness_is_cql_level():
    """The api issues CQL the moment the Service has endpoints, and 9042 accepts
    connections before the node can serve queries."""
    cassandra = _container(_load("statefulset-cassandra.yaml"), "cassandra")
    assert "cqlsh" in cassandra["readinessProbe"]["exec"]["command"][-1]


@pytest.mark.unit
def test_cassandra_cluster_name_matches_every_other_deployment_mode():
    """Cassandra refuses to start against a data directory stamped with a
    different cluster name (see ops.CASSANDRA_CLUSTER_NAME)."""
    from nyxgpt import ops

    cassandra = _container(_load("statefulset-cassandra.yaml"), "cassandra")
    env = {e["name"]: e["value"] for e in cassandra["env"]}
    assert env["CASSANDRA_CLUSTER_NAME"] == ops.CASSANDRA_CLUSTER_NAME


@pytest.mark.unit
def test_data_tier_image_pins_match_the_other_deployment_modes():
    """docs/docker-compose.md#image-pinning: Compose, Terraform, ops.py and now
    these manifests are bumped together, never one at a time."""
    from nyxgpt import ops

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    terraform = (REPO_ROOT / "terraform" / "main.tf").read_text(encoding="utf-8")

    cassandra_image = _container(_load("statefulset-cassandra.yaml"), "cassandra")["image"]
    ollama_image = _container(_load("statefulset-ollama.yaml"), "ollama")["image"]

    assert cassandra_image == compose["services"]["cassandra"]["image"]
    assert cassandra_image == ops.CASSANDRA_IMAGE
    assert f'"{cassandra_image}"' in terraform
    assert ollama_image == compose["services"]["ollama"]["image"]
    assert f'"{ollama_image}"' in terraform


@pytest.mark.unit
def test_data_tier_workloads_own_their_storage():
    """Both are stateful singletons: Cassandra's data directory and Ollama's
    model blob store must never be shared between two Pods."""
    for manifest, claim in (
        ("statefulset-cassandra.yaml", "data"),
        ("statefulset-ollama.yaml", "models"),
    ):
        workload = _load(manifest)
        assert workload["kind"] == "StatefulSet"
        assert workload["spec"]["replicas"] == 1
        claims = [c["metadata"]["name"] for c in workload["spec"]["volumeClaimTemplates"]]
        assert claim in claims
        # No storageClassName: use whatever default class the local cluster
        # provides (kind, minikube, Docker Desktop all differ).
        assert all(
            "storageClassName" not in c["spec"] for c in workload["spec"]["volumeClaimTemplates"]
        )


@pytest.mark.unit
def test_ops_waits_for_exactly_these_workloads():
    """The install's wait list and the manifests must not drift apart -- a wait
    for a workload that no longer exists passes instantly."""
    from nyxgpt import ops

    waited = {ref for ref, _label, _timeout in ops.K8S_DATA_TIER_WORKLOADS}
    deployed = {
        f"statefulset/{_load(m)['metadata']['name']}"
        for m in ("statefulset-cassandra.yaml", "statefulset-ollama.yaml")
    }
    assert waited == deployed
