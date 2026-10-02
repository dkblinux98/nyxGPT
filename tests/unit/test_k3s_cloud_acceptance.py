"""The three blockers from #3956's 2026-08-26 owner acceptance round.

All three were found by RUNNING `nyxgpt cloud deploy --kubernetes` against a
real EC2 instance, and all three have the same shape: the cluster was healthy
and the *product's reading of it* was wrong.

1. **A Pod no live ReplicaSet owns was read as the deployment's state.** The
   install applies `k8s/` (whose ConfigMap carries the placeholder
   error-tracking DSN), waits for the stack, then provisions GlitchTip -- which
   writes the real DSN and rolls api/web onto a new pod template. The pre-DSN
   ReplicaSet is scaled to zero and leaves a terminated Pod behind, and
   `_k8s_stack_health` failed the install on it (`pod
   nyxgpt-web-stable-77c7d9c6f4-gz62g: Failed`) three lines above
   `nyxgpt-web-stable 1/1`. Deterministic, not flaky: two deploys failed on the
   same ReplicaSet hash, because the hash is derived from the pod template. The
   deploy then exited before installing the access bridge, so the whole feature
   produced an unreachable deployment.

2. **`kubectl` on a k3s node is not kubectl.** It is a symlink to the `k3s`
   binary, whose shim defaults `KUBECONFIG` to the root-only
   `/etc/rancher/k3s/k3s.yaml` -- so `~/.kube/config`, which the deploy writes
   for exactly this purpose, was never read, and `nyxgpt cloud canary status`
   reported *"running in native mode"* on a running cluster. Two independent
   halves: hand every kubectl child the default kubeconfig, and stop reading a
   FAILED probe as evidence of a native install.

3. **Four build paths shared two mutable image tags.** `nyxgpt-api:local` /
   `nyxgpt-web:local` were written by Terraform dev, Kubernetes dev and
   Kubernetes artifact alike, so an instance running published 3.0.0rc14
   reported its images as `local` and `nyxgpt canary status` -- which reads the
   version straight off the Pod's image tag -- could not name the release.

Plus the two findings the owner noted as non-blocking: a misconfiguration
logged as a passing step, and a `Failed` Pod reported with no reason at all.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from nyxgpt import canary, k8s_pod_state, ops, self_heal, subprocess_bounds


class CP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- 1. Pods a retired ReplicaSet owns -------------------------------------


def _pod(name, *, phase="Running", ready=True, replicaset="", status_extra=None):
    status = {"phase": phase, "conditions": [{"type": "Ready", "status": str(ready)}]}
    status.update(status_extra or {})
    metadata = {"name": name}
    if replicaset:
        metadata["ownerReferences"] = [{"kind": "ReplicaSet", "name": replicaset}]
    return {"metadata": metadata, "status": status}


def _cluster(pods, replicasets):
    """A `_run` stand-in answering the Pod list and the ReplicaSet scale read."""

    def fake_run(cmd, check=True, **_k):
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "pods"]:
            return CP(stdout=json.dumps({"items": pods}))
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "rs"]:
            return CP(stdout="".join(f"{n}={r};" for n, r in replicasets.items()))
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "svc"]:
            return CP(stdout=f"{cmd[5]}  ClusterIP\n")
        raise AssertionError(f"unexpected: {cmd}")

    return fake_run


@pytest.mark.unit
def test_the_corpse_of_a_rolled_deployment_does_not_fail_the_install(monkeypatch):
    """THE 2026-08-26 blocker, in the shape the owner measured it.

    `nyxgpt-web-stable` is 1/1 from the current ReplicaSet; the pre-DSN one is
    scaled to zero and its terminated Pod is still in the namespace.
    """
    monkeypatch.setattr(
        ops,
        "_run",
        _cluster(
            pods=[
                _pod("nyxgpt-web-stable-69b45dd5db-aaa", replicaset="nyxgpt-web-stable-69b45dd5db"),
                _pod(
                    "nyxgpt-web-stable-77c7d9c6f4-gz62g",
                    phase="Failed",
                    ready=False,
                    replicaset="nyxgpt-web-stable-77c7d9c6f4",
                ),
            ],
            replicasets={"nyxgpt-web-stable-69b45dd5db": "1", "nyxgpt-web-stable-77c7d9c6f4": "0"},
        ),
    )

    results = ops._k8s_stack_health()
    pods = [r for r in results if r.message.startswith("pod ")]

    assert [r.message for r in pods] == ["pod nyxgpt-web-stable-69b45dd5db-aaa: Running"]
    assert all(r.ok for r in results), [r.message for r in results if not r.ok]


@pytest.mark.unit
def test_a_failed_pod_of_the_CURRENT_replicaset_still_fails(monkeypatch):
    """The narrow fix the owner ruled out, ruled out by a test.

    Filtering on the phase would have hidden this one too. The discriminator is
    ownership by a live controller, not the phase.
    """
    monkeypatch.setattr(
        ops,
        "_run",
        _cluster(
            pods=[
                _pod(
                    "nyxgpt-web-stable-69b45dd5db-aaa",
                    phase="Failed",
                    ready=False,
                    replicaset="nyxgpt-web-stable-69b45dd5db",
                )
            ],
            replicasets={"nyxgpt-web-stable-69b45dd5db": "1"},
        ),
    )

    results = ops._k8s_stack_health()
    assert any(not r.ok and r.message.startswith("pod ") for r in results)


@pytest.mark.unit
def test_a_statefulset_pod_is_never_filtered(monkeypatch):
    """Cassandra and Ollama have no ReplicaSet, so the filter must not reach them."""
    monkeypatch.setattr(
        ops,
        "_run",
        _cluster(
            pods=[_pod("cassandra-0", phase="Failed", ready=False)],
            replicasets={"nyxgpt-web-stable-77c7d9c6f4": "0"},
        ),
    )

    results = ops._k8s_stack_health()
    assert any(r.message.startswith("pod cassandra-0") and not r.ok for r in results)


@pytest.mark.unit
def test_an_unreadable_replicaset_read_removes_nothing(monkeypatch):
    """The filter may only remove a Pod on positive evidence its owner is done."""

    def fake_run(cmd, check=True, **_k):
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "pods"]:
            return CP(
                stdout=json.dumps(
                    {"items": [_pod("api-x", phase="Failed", ready=False, replicaset="rs-old")]}
                )
            )
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "rs"]:
            return CP(returncode=1, stderr="the server could not find the requested resource")
        return CP(stdout="x ClusterIP\n")

    monkeypatch.setattr(ops, "_run", fake_run)
    states, read_failure = ops._k8s_pod_states()
    assert read_failure is None
    assert [s.name for s in states] == ["api-x"]


@pytest.mark.unit
def test_a_healthy_namespace_does_not_pay_for_the_replicaset_read(monkeypatch):
    """`/infra/status` polls this; a read that cannot change the answer is not made."""
    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "pods"]:
            return CP(stdout=json.dumps({"items": [_pod("api-x", replicaset="rs-new")]}))
        return CP(stdout="x ClusterIP\n")

    monkeypatch.setattr(ops, "_run", fake_run)
    states, _ = ops._k8s_pod_states()
    assert [s.name for s in states] == ["api-x"]
    assert not any(c[4] == "rs" for c in calls if len(c) > 4)


@pytest.mark.unit
def test_the_wait_cannot_fast_fail_on_a_retired_replicasets_pod(monkeypatch):
    """`_k8s_blocked_pods` feeds the rollout wait's fast-fail from the same read."""
    monkeypatch.setattr(
        ops,
        "_run",
        _cluster(
            pods=[
                _pod(
                    "nyxgpt-api-stable-old-1",
                    phase="Failed",
                    ready=False,
                    replicaset="nyxgpt-api-stable-old",
                )
            ],
            replicasets={"nyxgpt-api-stable-old": "0"},
        ),
    )
    assert ops._k8s_blocked_pods(selector="app=nyxgpt-api,track=stable") == []


# --- 1b. EVERY reader of a Pod list, not just the install's -----------------
#
# The first cut of blocker 1 fixed `ops._k8s_pod_states` alone. The identical
# corpse carries `app: nyxgpt-web-canary-pool`, so self-heal's own Pod listing
# put it in the `core` tier and rendered it on the Self-Heal dashboard as a
# component that is Failed and unhealable forever -- on a deployment whose two
# Deployments are both 1/1. The rule now lives in `k8s_pod_state` and all three
# readers apply it.


def _self_heal_pod(name, *, app, phase="Running", ready=True, replicaset=""):
    pod = _pod(name, phase=phase, ready=ready, replicaset=replicaset)
    pod["metadata"]["labels"] = {"app": app}
    return pod


def _self_heal_cluster(pods, replicasets, calls=None):
    """A `self_heal._run` stand-in answering the Pod list and the scale read."""

    def fake_run(cmd, timeout=30.0, **_k):
        if calls is not None:
            calls.append(cmd)
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return CP(stdout=json.dumps({"items": pods}))
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "rs"]:
            if replicasets is None:
                return CP(returncode=1, stderr="Unauthorized")
            return CP(stdout="".join(f"{n}={r};" for n, r in replicasets.items()))
        raise AssertionError(f"unexpected: {cmd}")

    return fake_run


def _self_heal_components(monkeypatch, pods, replicasets, calls=None):
    monkeypatch.setattr(self_heal, "_which", lambda _p: "/usr/bin/kubectl")
    monkeypatch.setattr(self_heal, "_run", _self_heal_cluster(pods, replicasets, calls))
    return self_heal._list_kubernetes_component_status(set())


@pytest.mark.unit
def test_the_corpse_is_not_a_self_heal_component(monkeypatch):
    """THE review-round finding: the dashboard showed the same Pod as Failed."""
    components = _self_heal_components(
        monkeypatch,
        pods=[
            _self_heal_pod(
                "nyxgpt-web-stable-69b45dd5db-aaa",
                app="nyxgpt-web-canary-pool",
                replicaset="nyxgpt-web-stable-69b45dd5db",
            ),
            _self_heal_pod(
                "nyxgpt-web-stable-77c7d9c6f4-gz62g",
                app="nyxgpt-web-canary-pool",
                phase="Failed",
                ready=False,
                replicaset="nyxgpt-web-stable-77c7d9c6f4",
            ),
        ],
        replicasets={"nyxgpt-web-stable-69b45dd5db": "1", "nyxgpt-web-stable-77c7d9c6f4": "0"},
    )

    assert [c.service for c in components] == ["nyxgpt-web-stable-69b45dd5db-aaa"]
    assert all(c.healthy for c in components)


@pytest.mark.unit
def test_a_failed_pod_of_the_current_replicaset_is_still_a_self_heal_component(monkeypatch):
    """The narrow fix the owner ruled out, ruled out on this reader too."""
    components = _self_heal_components(
        monkeypatch,
        pods=[
            _self_heal_pod(
                "nyxgpt-web-stable-69b45dd5db-aaa",
                app="nyxgpt-web-canary-pool",
                phase="Failed",
                ready=False,
                replicaset="nyxgpt-web-stable-69b45dd5db",
            )
        ],
        replicasets={"nyxgpt-web-stable-69b45dd5db": "1"},
    )

    assert [(c.service, c.healthy) for c in components] == [
        ("nyxgpt-web-stable-69b45dd5db-aaa", False)
    ]


@pytest.mark.unit
def test_a_statefulset_pod_is_never_filtered_out_of_self_heal(monkeypatch):
    """Cassandra and Ollama have no ReplicaSet: a real failure there must stand."""
    components = _self_heal_components(
        monkeypatch,
        pods=[_self_heal_pod("cassandra-0", app="cassandra", phase="Failed", ready=False)],
        replicasets={"nyxgpt-web-stable-77c7d9c6f4": "0"},
    )
    assert [(c.service, c.healthy) for c in components] == [("cassandra-0", False)]


@pytest.mark.unit
def test_an_unreadable_replicaset_read_removes_no_self_heal_component(monkeypatch):
    """Fail open: without positive evidence the owner is finished, the Pod stays."""
    components = _self_heal_components(
        monkeypatch,
        pods=[
            _self_heal_pod(
                "api-x", app="nyxgpt-api-canary-pool", phase="Failed", ready=False, replicaset="rs"
            )
        ],
        replicasets=None,
    )
    assert [c.service for c in components] == ["api-x"]


@pytest.mark.unit
def test_a_healthy_namespace_does_not_pay_for_the_read_on_every_watchdog_pass(monkeypatch):
    """This runs every 15 seconds, and the read cannot change an all-healthy answer."""
    calls: list[list[str]] = []
    components = _self_heal_components(
        monkeypatch,
        pods=[_self_heal_pod("api-x", app="nyxgpt-api-canary-pool", replicaset="rs-new")],
        replicasets={"rs-old": "0"},
        calls=calls,
    )
    assert [c.service for c in components] == ["api-x"]
    assert not any("rs" in cmd for cmd in calls)


@pytest.mark.unit
def test_a_retired_replicasets_pod_does_not_explain_an_unhealthy_track(monkeypatch):
    """`canary.pod_failure_reasons` selects by label, which also matches corpses.

    A track that is unhealthy *now* must not be explained by the terminated
    remains of the rollout before it -- that sends the operator after a cause
    Kubernetes has already finished with.
    """

    def fake_run(cmd, **_k):
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return CP(
                stdout=json.dumps(
                    {
                        "items": [
                            _pod(
                                "nyxgpt-api-canary-old-1",
                                phase="Failed",
                                ready=False,
                                replicaset="nyxgpt-api-canary-old",
                                status_extra={
                                    "containerStatuses": [
                                        {
                                            "name": "nyxgpt-api",
                                            "ready": False,
                                            "state": {
                                                "terminated": {"reason": "Error", "exitCode": 1}
                                            },
                                        }
                                    ]
                                },
                            ),
                            _pod(
                                "nyxgpt-api-canary-new-1",
                                phase="Pending",
                                ready=False,
                                replicaset="nyxgpt-api-canary-new",
                                status_extra={
                                    "containerStatuses": [
                                        {
                                            "name": "nyxgpt-api",
                                            "ready": False,
                                            "state": {
                                                "waiting": {
                                                    "reason": "ImagePullBackOff",
                                                    "message": "pull access denied",
                                                }
                                            },
                                        }
                                    ]
                                },
                            ),
                        ]
                    }
                )
            )
        if cmd[:5] == ["kubectl", "-n", "nyxgpt", "get", "rs"]:
            return CP(stdout="nyxgpt-api-canary-old=0;nyxgpt-api-canary-new=1;")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(canary, "_which", lambda _p: "/usr/bin/kubectl")
    monkeypatch.setattr(canary, "_run", fake_run)

    reasons = canary.pod_failure_reasons("app=nyxgpt-api-canary-pool,track=canary")

    assert len(reasons) == 1
    assert "ImagePullBackOff" in reasons[0]
    assert "nyxgpt-api-canary-old-1" not in reasons[0]


@pytest.mark.unit
def test_every_pod_reader_shares_one_retired_replicaset_rule():
    """Three readers, one decision -- the drift this review round was about.

    A reader that re-implemented the rule is free to disagree with the other
    two, which is how the install stopped failing on the corpse while the
    dashboard went on reporting it.
    """
    for module in (ops, self_heal, canary):
        assert module.pod_is_retired is k8s_pod_state.pod_is_retired
        assert module.parse_retired_replicasets is k8s_pod_state.parse_retired_replicasets
        assert module.retired_replicaset_argv is k8s_pod_state.retired_replicaset_argv


# --- A `Failed` Pod says why (the owner's second "noted, not blocking") ----


@pytest.mark.unit
def test_a_failed_pod_names_its_reason_and_exit_code():
    state = k8s_pod_state.classify_pod(
        {
            "metadata": {"name": "api-1"},
            "status": {
                "phase": "Failed",
                "containerStatuses": [
                    {
                        "name": "nyxgpt-api",
                        "state": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "exitCode": 137,
                                "message": "container exceeded its memory limit",
                            }
                        },
                    }
                ],
            },
        }
    )
    assert state.reason == "OOMKilled"
    assert "exited 137" in state.detail
    assert "memory limit" in state.detail


@pytest.mark.unit
def test_an_evicted_pod_reports_the_kubelets_own_reason():
    state = k8s_pod_state.classify_pod(
        {
            "metadata": {"name": "api-1"},
            "status": {"phase": "Failed", "reason": "Evicted", "message": "The node was low on"},
        }
    )
    assert (state.reason, state.detail) == ("Evicted", "The node was low on")


@pytest.mark.unit
def test_the_install_line_for_a_failed_pod_carries_the_reason():
    """`pod <name>: Failed` with nothing after it is the line that cost an SSH session."""
    state = ops._classify_k8s_pod(
        {
            "metadata": {"name": "api-1"},
            "status": {"phase": "Failed", "reason": "Evicted", "message": "low on ephemeral"},
        }
    )
    assert state.as_result(prefix="pod ").message == "pod api-1: Failed: Evicted"
    assert state.details == "low on ephemeral"


@pytest.mark.unit
def test_a_running_pod_is_not_described_by_its_finished_init_container():
    """`Running: Completed` would be a worse answer than the generic one."""
    state = ops._classify_k8s_pod(
        {
            "metadata": {"name": "api-1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "False"}],
                "initContainerStatuses": [
                    {
                        "name": "wait",
                        "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                    }
                ],
            },
        }
    )
    assert state.summary == "Running: containers not ready yet"


# --- 2a. Every kubectl child gets the kubeconfig kubectl would have used ----


@pytest.fixture
def home_kubeconfig(monkeypatch, tmp_path):
    """A `~/.kube/config` and a HOME that resolves to it."""
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    kube = tmp_path / ".kube"
    kube.mkdir()
    (kube / "config").write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return kube / "config"


@pytest.mark.unit
def test_a_kubectl_child_is_handed_the_default_kubeconfig(home_kubeconfig):
    env = subprocess_bounds.kubectl_env(["kubectl", "-n", "nyxgpt", "get", "pods"], None)
    assert env is not None
    assert env["KUBECONFIG"] == str(home_kubeconfig)


@pytest.mark.unit
def test_a_stated_kubeconfig_choice_is_never_overruled(home_kubeconfig, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
    assert subprocess_bounds.kubectl_env(["kubectl", "get", "pods"], None) is None


@pytest.mark.unit
def test_in_a_pod_the_service_account_fallback_is_left_alone(home_kubeconfig, monkeypatch):
    """The same trap `--request-timeout` fell into: naming a kubeconfig in a Pod
    stops client-go consulting `InClusterConfig`, and kubectl then dials
    `http://localhost:8080`."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    assert subprocess_bounds.kubectl_env(["kubectl", "get", "pods"], None) is None


@pytest.mark.unit
def test_no_kubeconfig_means_nothing_is_invented(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert subprocess_bounds.kubectl_env(["kubectl", "get", "pods"], None) is None


@pytest.mark.unit
def test_only_kubectl_is_touched(home_kubeconfig):
    assert subprocess_bounds.kubectl_env(["docker", "ps"], None) is None
    assert subprocess_bounds.kubectl_env([], None) is None


@pytest.mark.unit
def test_a_callers_own_env_is_preserved_alongside_the_kubeconfig(home_kubeconfig):
    env = subprocess_bounds.kubectl_env(["kubectl", "get", "pods"], {"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "KUBECONFIG": str(home_kubeconfig)}


@pytest.mark.unit
def test_ops_and_canary_and_self_heal_all_run_kubectl_with_it(home_kubeconfig, monkeypatch):
    """One helper, three `_run`s. The watchdog needs it as much as the CLI does:
    self-heal on a k3s instance could not read the namespace it heals."""
    from nyxgpt import self_heal

    seen = {}

    def record(name):
        def fake_run(argv, **kwargs):
            seen[name] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        return fake_run

    for module, name in ((ops, "ops"), (canary, "canary"), (self_heal, "self_heal")):
        monkeypatch.setattr(module.subprocess, "run", record(name))
        module._run(["kubectl", "-n", "nyxgpt", "get", "pods"])

    for name, env in seen.items():
        assert env is not None, name
        assert env["KUBECONFIG"] == str(home_kubeconfig), name


# --- 2b. A probe that could not ask is not an answer about the substrate ----


def _probe(cp):
    """A canary `_run` stand-in: the Kubernetes probe answers `cp`."""

    def fake_run(cmd, **_k):
        assert cmd[0] == "kubectl", cmd
        return cp

    return fake_run


@pytest.fixture
def no_other_substrate(monkeypatch):
    monkeypatch.setattr(canary, "_compose_mode", lambda: False)
    monkeypatch.setattr(canary.ops_module, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(canary, "_which", lambda prog: f"/usr/bin/{prog}")


@pytest.mark.unit
def test_an_unreadable_kubeconfig_is_unknown_not_native(monkeypatch, no_other_substrate):
    """Exactly what the owner measured: k3s's kubectl, root-only kubeconfig."""
    monkeypatch.setattr(
        canary,
        "_run",
        _probe(
            CP(
                returncode=1,
                stderr='error: error loading config file "/etc/rancher/k3s/k3s.yaml": '
                "open /etc/rancher/k3s/k3s.yaml: permission denied",
            )
        ),
    )

    mode, cause = canary._current_mode_with_reason()

    assert mode == "unknown"
    message = canary._non_kubernetes_mode_message(mode, cause)
    assert "permission denied" in message
    # The old answer, and why it was worse than no answer: it told the operator
    # to install the thing that was already running.
    assert "native mode" not in message
    assert "ops install --kubernetes" not in message


@pytest.mark.unit
def test_a_refused_connection_is_unknown_too(monkeypatch, no_other_substrate):
    monkeypatch.setattr(
        canary,
        "_run",
        _probe(
            CP(
                returncode=1,
                stderr="The connection to the server 10.1.1.137:6443 was refused "
                "- did you specify the right host or port?",
            )
        ),
    )
    assert canary._current_mode_with_reason()[0] == "unknown"


@pytest.mark.unit
def test_the_api_server_saying_no_such_namespace_is_a_real_answer(monkeypatch, no_other_substrate):
    """A cluster that answered and said this deployment is not on it IS
    determinate -- otherwise every workstation with kubectl and a kind cluster
    would report `unknown` forever."""
    monkeypatch.setattr(
        canary,
        "_run",
        _probe(
            CP(returncode=1, stderr='Error from server (NotFound): namespaces "nyxgpt" not found')
        ),
    )
    assert canary._current_mode_with_reason() == ("native", "")


@pytest.mark.unit
def test_a_forbidden_namespace_is_not_read_as_native(monkeypatch, no_other_substrate):
    """`Forbidden` comes from the server too, and a namespace this process may
    not list is precisely one whose contents are unknown."""
    monkeypatch.setattr(
        canary,
        "_run",
        _probe(
            CP(
                returncode=1,
                stderr='Error from server (Forbidden): pods is forbidden: User "x" cannot list',
            )
        ),
    )
    assert canary._current_mode_with_reason()[0] == "unknown"


@pytest.mark.unit
def test_an_empty_namespace_on_a_reachable_cluster_is_still_native(monkeypatch, no_other_substrate):
    monkeypatch.setattr(canary, "_run", _probe(CP(returncode=0, stdout="\n")))
    assert canary._current_mode_with_reason() == ("native", "")


@pytest.mark.unit
def test_a_populated_namespace_is_kubernetes(monkeypatch, no_other_substrate):
    monkeypatch.setattr(canary, "_run", _probe(CP(returncode=0, stdout="cassandra-0 1/1 Running")))
    assert canary._current_mode_with_reason() == ("kubernetes", "")


@pytest.mark.unit
def test_the_timeout_cause_still_reports_the_timeout(monkeypatch, no_other_substrate):
    monkeypatch.setattr(
        canary,
        "_run",
        _probe(CP(returncode=subprocess_bounds.TIMEOUT_RETURNCODE, stderr="timed out after 5s")),
    )
    mode, cause = canary._current_mode_with_reason()
    assert (mode, cause) == ("unknown", "kubectl-timeout")
    assert "timed out" in canary._non_kubernetes_mode_message(mode, cause)


@pytest.mark.unit
def test_the_probe_error_is_bounded(monkeypatch, no_other_substrate):
    monkeypatch.setattr(canary, "_run", _probe(CP(returncode=1, stderr="x" * 5000)))
    _mode, cause = canary._current_mode_with_reason()
    assert len(cause) < 400


# --- 3. One tag namespace per build path, every one versioned --------------


@pytest.mark.unit
def test_the_four_build_paths_no_longer_share_a_tag():
    refs = {
        "tf-dev": ops._terraform_dev_image_refs()["api"],
        "tf-artifact": ops._terraform_artifact_image_ref("api", ops._native_service_version()),
        "k8s-dev": ops.k8s_image_refs(dev=True)["api"],
        "k8s-artifact": ops.k8s_image_refs(dev=False)["api"],
    }
    # Nothing is `:local` any more, and every tag names its version.
    assert not any(ref.endswith(":local") for ref in refs.values())
    assert all(ops._native_service_version() in ref for ref in refs.values())
    # The two MODES cannot overwrite each other...
    assert refs["tf-dev"] != refs["tf-artifact"]
    # ...while the two substrates share each mode's tag deliberately: same
    # source, same Dockerfile, same staging helper, so one tag is one image.
    assert refs["tf-dev"] == refs["k8s-dev"]
    assert refs["tf-artifact"] == refs["k8s-artifact"]


@pytest.mark.unit
def test_a_version_bump_changes_the_tag():
    assert ops.local_image_ref("api", dev=False, version="3.0.0rc14") == (
        "nyxgpt-api:artifact-3.0.0rc14"
    )
    assert ops.local_image_ref("web", dev=True, version="3.1.0") == "nyxgpt-web:dev-3.1.0"


@pytest.mark.unit
def test_the_overlay_pins_the_tag_without_touching_the_manifests(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "NYXGPT_HOME", tmp_path)
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path / "k8s")
    monkeypatch.setattr(ops, "K8S_IMAGE_OVERLAY_DIR", tmp_path / "k8s-images")
    (tmp_path / "k8s").mkdir()
    shipped = tmp_path / "k8s" / "kustomization.yaml"
    shipped.write_text("kind: Kustomization\n", encoding="utf-8")

    overlay = ops._write_k8s_image_overlay(dev=False)
    text = (overlay / "kustomization.yaml").read_text(encoding="utf-8")

    version = ops._native_service_version()
    assert "- name: nyxgpt-api" in text
    assert f"newTag: artifact-{version}" in text
    assert "- name: nyxgpt-web" in text
    # The shipped manifest set is what #3506's rationale rests on, so the
    # overlay lives outside it and leaves it byte for byte alone.
    assert shipped.read_text(encoding="utf-8") == "kind: Kustomization\n"
    assert overlay != ops.K8S_DIR
    assert f"- {ops.os.path.relpath(ops.K8S_DIR, overlay)}" in text


@pytest.mark.unit
def test_the_dev_overlay_pins_the_dev_tag(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path / "k8s")
    monkeypatch.setattr(ops, "K8S_IMAGE_OVERLAY_DIR", tmp_path / "k8s-images")
    text = (ops._write_k8s_image_overlay(dev=True) / "kustomization.yaml").read_text(
        encoding="utf-8"
    )
    assert f"newTag: dev-{ops._native_service_version()}" in text


@pytest.mark.unit
def test_the_apply_goes_through_the_overlay_and_names_the_refs(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path / "k8s")
    monkeypatch.setattr(ops, "K8S_IMAGE_OVERLAY_DIR", tmp_path / "k8s-images")
    applied = []

    def fake_run(cmd, check=True, **_k):
        applied.append(cmd)
        return CP(returncode=0, stdout="deployment.apps/nyxgpt-api-stable configured")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._kubectl_apply_kustomization(dev=False)

    assert applied == [["kubectl", "apply", "-k", str(tmp_path / "k8s-images")]]
    assert results[0].ok
    # The operator can read which images the cluster was just pointed at.
    assert ops.k8s_image_refs(dev=False)["api"] in results[0].message


@pytest.mark.unit
def test_the_install_builds_the_tag_it_applies(monkeypatch, tmp_path):
    """The one invariant that makes this safe: a tag the manifests name that no
    build produced is an `ImagePullBackOff` on every Pod."""
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/local/bin/{prog}")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", Path(__file__).resolve().parents[2])
    built = []

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            built.append(cmd[3])
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="docker-desktop")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    ops._build_and_load_k8s_api_image(dev=True)
    ops._build_and_load_k8s_web_image(dev=True)

    assert built == [
        ops.k8s_image_refs(dev=True)["api"],
        ops.k8s_image_refs(dev=True)["web"],
    ]


@pytest.mark.unit
def test_canary_reads_the_version_off_the_applied_tag(monkeypatch):
    """What the owner could not do on the instance: tell which release a Pod runs."""
    ref = ops.local_image_ref("api", dev=False, version="3.0.0rc14")

    def fake_run(cmd, **_k):
        return CP(
            stdout=json.dumps(
                {
                    "spec": {
                        "replicas": 1,
                        "template": {"spec": {"containers": [{"image": ref}]}},
                    },
                    "status": {"readyReplicas": 1},
                }
            )
        )

    monkeypatch.setattr(canary, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(canary, "_run", fake_run)

    health = canary.deployment_health("nyxgpt-api-stable")
    assert health.version == "artifact-3.0.0rc14"
    assert "3.0.0rc14" in health.version


# --- The owner's "noted, not blocking": an error logged as a passing step ---


@pytest.mark.unit
def test_an_attention_result_does_not_log_as_ok(caplog):
    result = ops._attention("observability errors: GlitchTip rejected Grafana's token", "re-run x")
    with caplog.at_level("INFO"):
        assert ops._emit_results("install", [result]) is True
    logged = [r for r in caplog.records if "GlitchTip rejected" in r.getMessage()]
    assert logged, [r.getMessage() for r in caplog.records]
    assert "ops: install attention:" in logged[0].getMessage()
    assert logged[0].levelname == "WARNING"
    # The remedy travels with the line, not only in the structured extra.
    assert "re-run x" in logged[0].getMessage()


@pytest.mark.unit
def test_a_plain_success_still_logs_as_ok(caplog):
    with caplog.at_level("INFO"):
        ops._emit_results("install", [ops.OpsResult(True, "nyxgpt-api installed")])
    assert any("ops: install ok:" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_every_label_a_result_prints_is_the_verb_it_logs():
    """The stdout label and the logged verb come from one place, so a line
    cannot read `[NO DATA]` on screen and `ok` in Loki."""
    expected = {
        "OK": "ok",
        "NO DATA": "no-data",
        "ATTENTION": "attention",
        "PENDING": "pending",
        "SKIP": "skip",
    }
    for result in (
        ops.OpsResult(True, "x"),
        ops._no_data("x"),
        ops._attention("x"),
        ops.OpsResult(True, "x", status=ops.K8S_STATE_PENDING.upper()),
        ops.OpsResult(True, "Skipped: no docker found"),
    ):
        label = ops._result_status_label(result)
        assert ops._result_log_verb(result) == expected[label], label
    # A failure is a failure whatever label it carries.
    assert ops._result_log_verb(ops.OpsResult(False, "x", status="PENDING")) == "failed"


@pytest.mark.unit
def test_the_attention_label_is_not_a_failure():
    """An install that brought a working stack up must not exit non-zero over a
    credential an operator can fix afterwards."""
    assert ops._attention("x").ok is True
    assert ops._result_status_label(ops._attention("x")) == "ATTENTION"


@pytest.mark.unit
def test_the_overlay_header_marks_itself_generated(monkeypatch, tmp_path):
    """A file in the ops-managed home that an operator will eventually read."""
    monkeypatch.setattr(ops, "K8S_DIR", tmp_path / "k8s")
    monkeypatch.setattr(ops, "K8S_IMAGE_OVERLAY_DIR", tmp_path / "k8s-images")
    text = (ops._write_k8s_image_overlay(dev=False) / "kustomization.yaml").read_text(
        encoding="utf-8"
    )
    assert re.search(r"GENERATED by .*do not edit", text)
    assert "#3956" in text
