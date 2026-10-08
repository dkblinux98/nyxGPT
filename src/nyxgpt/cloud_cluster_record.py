"""The cloud deployment's own facts, recorded *in the cluster* it runs (#4138).

A `--kubernetes` cloud deployment serves its own admin dashboard from an api
Pod, and that Pod could not report the deployment it is part of. Both things
`nyxgpt cloud status` reads on the host are structurally out of its reach:

1. **IMDS.** The host reaches `169.254.169.254` and identifies the instance;
   a Pod on k3s generally cannot -- the link-local address is not routed into
   the Pod network.
2. **The deploy record.** `~/.nyxGPT/cloud/state.json` and `cloud/deploy.json`
   live on the host filesystem and on the workstation that ran the deploy.
   Neither is mounted into the cluster, and neither should be.

So the api answered *unknown* accurately about itself, and the operator was
told to go run a CLI command on the box whose own dashboard they were already
looking at (owner observation, 2026-10-03, v3.0.0rc17).

**This module is the same answer #3988 gave for the install mode**: the
install does not teach the Pod to probe the host, it *records the fact into
the cluster* where anything describing the deployment can read it.
`configmap/nyxgpt-install-mode` is that record for "which build is this";
`configmap/nyxgpt-cloud-deploy` is this one for "which instance is this, and
where". Written by `nyxgpt ops install --kubernetes` on the instance -- which
can read IMDS, being the host -- and read back by the api Pod through its own
ServiceAccount, one bounded `kubectl get configmap`.

Three properties the callers depend on:

* **Read only from inside the cluster.** `in_cluster()` gates every read, so a
  workstation pays no subprocess for a record that would not describe its own
  machine anyway, and a laptop pointed at an unrelated local cluster can never
  have that cluster answer a question about AWS. The vantage point #3804 was
  written for -- a machine that neither ran the deploy nor is the instance --
  keeps reporting *unknown*, which is still the honest answer there.
* **Written only when the host really is an EC2 instance.** `build_record`
  takes the IMDS facts as an argument rather than reading them, so there is
  exactly one place that decides "am I on EC2" (the install) and the record
  can never be a guess. No facts, no record.
* **A partial record is no record.** `parse_record` rejects anything without an
  `instance_id`, for the same reason `InstallIdentity` treats a half-written
  marker as unknown: a determinate rendering of an unknown is the defect class
  this whole surface exists to remove (D-032).

Deliberately free of a `nyxgpt.ops` import, exactly like `install_mode`: `ops`
writes the record and `cloud_infra` reads it, and `ops` imports `cloud_infra`
nowhere. The teardown needs no entry here -- `ops down --kubernetes` deletes
the namespace, and the ConfigMap goes with it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Mapping
from shutil import which

from nyxgpt.subprocess_bounds import (
    PROBE_TIMEOUT_SECONDS,
    bounded_argv,
    kubectl_env,
    timed_out,
    timeout_result,
)

logger = logging.getLogger(__name__)

# The ConfigMap the deployment's cloud facts live in, beside #3988's
# `nyxgpt-install-mode`. Two records rather than one because they answer
# different questions and are written by different knowledge: the install mode
# is about the *build*, this is about the *substrate underneath it*.
CLOUD_DEPLOY_CONFIGMAP = "nyxgpt-cloud-deploy"

# The namespace `k8s/*.yaml` deploys into. Duplicated from `nyxgpt.ops`
# (`K8S_NAMESPACE`) rather than imported, for the module-independence reason in
# the docstring -- the same duplication `self_heal` and `canary` already carry,
# and `tests/unit/test_cloud_cluster_record.py` fails if the three drift apart.
K8S_NAMESPACE = "nyxgpt"

# Set by the kubelet in every Pod. The same variable `ops._in_cluster` and
# `subprocess_bounds` key on, named here for the same reason the namespace is.
IN_CLUSTER_ENV = "KUBERNETES_SERVICE_HOST"

# What the record carries: every substrate fact the host knows first-hand from
# IMDS, plus the three the install itself knows. Nothing derived and nothing
# about the operator's workstation -- the SSH user, the identity file and the
# tunnel are properties of the machine that ran the deploy, and the Pod is
# right to keep saying it cannot see them.
IMDS_FIELDS = (
    "instance_id",
    "instance_type",
    "region",
    "availability_zone",
    "public_ip",
    "private_ip",
    "vpc_id",
    "subnet_id",
    "security_group_id",
    "ssh_key_name",
)
INSTALL_FIELDS = ("version", "substrate", "os_family")
RECORD_FIELDS = IMDS_FIELDS + INSTALL_FIELDS

# Substrate facts do not change while an instance runs, so the read is cached
# on the same reasoning (and with the same TTL) as `cloud_imds`: the endpoints
# that ask are polled, and one `kubectl get` per poll per endpoint is a cost
# with nothing to buy.
CACHE_TTL_SECONDS = 300.0

# `(expires_at, record)`. Module level so `/cloud/infra` and `/cloud/deploy`
# -- which asks the first -- share one answer.
_cache: tuple[float, dict[str, str]] | None = None


def reset_cache() -> None:
    """Forget any cached record. Used by tests and by an explicit re-read."""
    global _cache
    _cache = None


def in_cluster() -> bool:
    """True when this process is running inside a Kubernetes Pod.

    Read at call time rather than at import, so a test (and a long-lived
    process whose environment is rewritten) can change the answer.
    """
    return bool(os.environ.get(IN_CLUSTER_ENV))


def build_record(
    facts: Mapping[str, str] | None,
    *,
    version: str,
    substrate: str,
    os_family: str,
) -> dict[str, str]:
    """The ConfigMap `data` for a deployment on the instance `facts` describes.

    `facts` is `cloud_imds.instance_facts()` -- passed in, never read here, so
    "is this machine an EC2 instance?" is decided in exactly one place. `None`
    (or facts with no instance id) returns `{}`: this machine is not an
    instance, so there is no cloud deployment of it to record, and writing a
    record anyway is how a local kind cluster would start claiming to be in
    AWS.
    """
    if not facts or not str(facts.get("instance_id") or "").strip():
        return {}
    record = {field: str(facts.get(field) or "") for field in IMDS_FIELDS}
    record["version"] = version
    record["substrate"] = substrate
    record["os_family"] = os_family
    return record


def configmap_manifest(record: Mapping[str, str]) -> dict[str, object]:
    """`record` as the ConfigMap the install applies into the deployment's namespace."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CLOUD_DEPLOY_CONFIGMAP,
            "namespace": K8S_NAMESPACE,
            "labels": {"app": "nyxgpt", "component": "cloud-deploy-record"},
        },
        "data": {str(k): str(v) for k, v in record.items()},
    }


def parse_record(raw: object) -> dict[str, str]:
    """A ConfigMap's `data` block as a record, or `{}` when it is not one.

    Never raises. A record with no `instance_id` is not a partial record to be
    filled in with defaults -- it is no record, and the caller must report
    *unknown* rather than render an instance nobody named (D-032).
    """
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data") if "data" in raw else raw
    if not isinstance(data, dict):
        return {}
    record = {str(k): str(v) for k, v in data.items()}
    if not record.get("instance_id", "").strip():
        return {}
    return record


def _kubectl_get_configmap() -> dict[str, str]:
    """One bounded `kubectl get configmap` for the record, or `{}`.

    Never raises and never distinguishes "no record" from "could not read
    one" -- the same contract `ops._read_k8s_install_record` has, and for the
    same reason: both mean the caller has nothing recorded to report, and a
    namespace that is gone, an RBAC refusal and a deployment made before this
    existed all land here.
    """
    kubectl = which("kubectl")
    if kubectl is None:
        return {}
    cmd = [
        "kubectl",
        "-n",
        K8S_NAMESPACE,
        "get",
        "configmap",
        CLOUD_DEPLOY_CONFIGMAP,
        "-o",
        "json",
    ]
    env = kubectl_env(cmd, None)
    bounded = bounded_argv(cmd, PROBE_TIMEOUT_SECONDS)
    try:
        cp = subprocess.run(  # noqa: S603 - fixed argv, no shell
            bounded,
            check=False,
            text=True,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        cp = timeout_result(bounded, exc, PROBE_TIMEOUT_SECONDS)
    except OSError as exc:  # pragma: no cover - kubectl vanished between which() and run()
        logger.debug("Could not run kubectl to read the cloud-deploy record: %s", exc)
        return {}
    if timed_out(cp) or cp.returncode != 0:
        logger.debug(
            "No readable %s ConfigMap (rc=%s): %s",
            CLOUD_DEPLOY_CONFIGMAP,
            cp.returncode,
            (cp.stderr or "").strip()[:200],
        )
        return {}
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return parse_record(payload)


def read_cloud_deploy_record(*, force: bool = False) -> dict[str, str]:
    """The cloud-deploy record this cluster carries, or `{}` when there is none.

    `{}` off-cluster, always and without a subprocess: this record describes
    the instance the *cluster* runs on, and a machine that is not in that
    cluster has its own sources for that question (IMDS, the deploy record,
    Terraform state) which are better evidence than someone else's ConfigMap.
    """
    global _cache
    if not in_cluster():
        return {}
    now = time.monotonic()
    if not force and _cache is not None and _cache[0] > now:
        return dict(_cache[1])
    record = _kubectl_get_configmap()
    _cache = (now + CACHE_TTL_SECONDS, record)
    return dict(record)


def record_source_label() -> str:
    """How a card names the vantage point this record answered from.

    Named rather than asserted, exactly as #3988's Kubernetes card names its
    own: the point of recording the fact in the cluster is that the reader can
    say *where the answer came from*, and "this page is served from a Pod" is
    half the answer -- the other half is that the facts were put there by the
    install running on the instance.
    """
    return (
        f"the cloud-deploy record in this cluster (configmap/{CLOUD_DEPLOY_CONFIGMAP} in "
        f"namespace {K8S_NAMESPACE}) -- this page is served from a Pod inside the cluster "
        "on the instance, and `nyxgpt ops install --kubernetes` wrote these facts there "
        "from the instance's own metadata"
    )
