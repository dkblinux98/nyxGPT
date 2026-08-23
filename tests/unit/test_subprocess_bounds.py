"""Every subprocess a request thread can wait on is bounded, and says so honestly (#3858).

The failure these guard against is not "a command was slow": it is a status
endpoint holding a Starlette threadpool worker forever on a dependency that
blackholes rather than refuses, until the pool is exhausted and every sync
endpoint -- `/health` included -- queues behind it. So the assertions are about
two things the code must never lose: the bound exists, and an expired bound
comes back as a degraded *reading* rather than an exception on a status read.
"""

from __future__ import annotations

import subprocess

import pytest

from nyxgpt import canary, ops, self_heal, subprocess_bounds
from nyxgpt.subprocess_bounds import (
    PROBE_TIMEOUT_SECONDS,
    TIMEOUT_RETURNCODE,
    bounded_argv,
    timed_out,
    timeout_result,
)


def _expired(cmd: list[str], timeout: float) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(cmd, timeout, output=b"partial stdout", stderr=b"")


def _raise_timeout(*_args, **kwargs):
    """A `subprocess.run` stand-in that hangs past its bound."""
    raise subprocess.TimeoutExpired(["kubectl"], kwargs.get("timeout") or 1.0)


@pytest.fixture(autouse=True)
def _outside_a_pod(monkeypatch):
    """Pin the environment the argv assertions below assume: not inside a Pod.

    `bounded_argv` deliberately withholds kubectl's `--request-timeout` when
    `KUBERNETES_SERVICE_HOST` is set, so every "the flag is added" assertion is
    conditional on that variable being absent. A CI runner happens to satisfy
    that; saying it explicitly means these tests keep testing what they claim
    even when something in the environment does not.
    """
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)


# --- The shared vocabulary ---------------------------------------------------


@pytest.mark.unit
def test_timeout_result_is_distinguishable_and_keeps_what_the_command_emitted():
    result = timeout_result(["kubectl", "get", "pods"], _expired(["kubectl"], 5.0), 5.0)

    assert timed_out(result) is True
    assert result.returncode == TIMEOUT_RETURNCODE
    # Whatever it managed to print before being killed is often the diagnosis.
    assert "partial stdout" in result.stdout
    assert "timed out after 5s" in result.stderr


@pytest.mark.unit
def test_a_normal_failure_is_not_reported_as_a_timeout():
    assert timed_out(subprocess.CompletedProcess(["x"], 1, "", "boom")) is False
    assert timed_out(subprocess.CompletedProcess(["x"], 0, "", "")) is False


@pytest.mark.unit
def test_kubectl_gets_the_tools_own_dial_bound_as_well_as_the_python_one():
    """Deliberately both, *outside* a Pod: the flag yields a clean message, `timeout=` the rest."""
    argv = bounded_argv(["kubectl", "get", "pods", "-n", "nyxgpt"], PROBE_TIMEOUT_SECONDS)

    assert argv == ["kubectl", "--request-timeout=5s", "get", "pods", "-n", "nyxgpt"]


@pytest.mark.unit
def test_the_dial_bound_flag_is_withheld_inside_a_pod(monkeypatch):
    """In-cluster, `--request-timeout` doesn't bound kubectl -- it breaks it.

    kubectl uses the mounted service account only when the kubeconfig it merged
    equals the built-in default; the flag makes it stop comparing equal, so
    kubectl skips the in-cluster fallback and dials `http://localhost:8080`.
    A live kind cluster proved it (`canary-track-metrics-smoke`:
    `Get "http://localhost:8080/api?timeout=5s": connection refused`), and no
    stub-below-the-rewrite unit test could -- which is why this one asserts the
    *absence* of the flag rather than the behavior it causes.
    """
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    cmd = ["kubectl", "get", "pods", "-n", "nyxgpt", "-o", "json"]

    assert bounded_argv(cmd, PROBE_TIMEOUT_SECONDS) == cmd

    # The bound that carries the actual safety property is unconditional: the
    # caller still passes `timeout=` to subprocess.run, in a Pod or out of one.
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert timed_out(canary._run(["kubectl", "get", "deployment", "x"]))


@pytest.mark.unit
def test_bounded_argv_leaves_alone_what_it_must():
    # No bound asked for -> no flag invented.
    assert bounded_argv(["kubectl", "get", "pods"], None) == ["kubectl", "get", "pods"]
    # A caller that already set it keeps its own value.
    already = ["kubectl", "--request-timeout=30s", "get", "pods"]
    assert bounded_argv(already, 5.0) == already
    # A watch would be cut off mid-stream by --request-timeout, so it is exempt.
    watch = ["kubectl", "rollout", "status", "deployment/x", "--timeout=180s"]
    assert bounded_argv(watch, 210.0) == watch
    # Nothing else on earth takes this flag.
    assert bounded_argv(["docker", "ps"], 5.0) == ["docker", "ps"]
    assert bounded_argv([], 5.0) == []


@pytest.mark.unit
def test_a_kubectl_path_is_recognized_by_its_program_name_not_its_argv0():
    argv = bounded_argv(["/opt/homebrew/bin/kubectl", "get", "pods"], 5.0)

    assert argv[1] == "--request-timeout=5s"


@pytest.mark.unit
def test_the_watch_exemption_reads_subcommands_not_any_matching_token():
    """A flag *value* spelling a streaming subcommand must not suppress the bound.

    `-n logs` is a namespace called "logs", not `kubectl logs`; a scan over
    every argv token would drop the flag for it.
    """
    for argv_in in (
        ["kubectl", "get", "pods", "-n", "logs"],
        ["kubectl", "-n", "logs", "get", "pods"],
        ["kubectl", "--namespace=wait", "get", "deployment", "x"],
        ["kubectl", "get", "pods", "-o", "wait"],
    ):
        assert bounded_argv(argv_in, 5.0)[1] == "--request-timeout=5s", argv_in

    # The real watches stay exempt, wherever their global flags sit.
    for watch in (
        ["kubectl", "-n", "nyxgpt", "rollout", "status", "deployment/x"],
        ["kubectl", "logs", "-n", "nyxgpt", "pod/x"],
        ["kubectl", "wait", "--for=condition=Ready", "pod/x"],
    ):
        assert bounded_argv(watch, 5.0) == watch


@pytest.mark.unit
def test_a_sub_second_bound_never_renders_as_the_no_timeout_value():
    """kubectl reads `--request-timeout=0s` as *no* timeout, so rounding down disarms it."""
    assert bounded_argv(["kubectl", "get", "pods"], 0.4)[1] == "--request-timeout=1s"
    assert bounded_argv(["kubectl", "get", "pods"], 1.2)[1] == "--request-timeout=2s"


# --- canary ------------------------------------------------------------------


@pytest.mark.unit
def test_canary_run_turns_an_expired_bound_into_a_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(canary.subprocess, "run", _raise_timeout)

    cp = canary._run(["kubectl", "get", "pods"], timeout=1.0)

    assert timed_out(cp)
    assert "timed out after 1s" in cp.stderr


@pytest.mark.unit
def test_canary_probes_are_bounded_at_the_probe_timeout(monkeypatch):
    """A polled endpoint's probe must not inherit the (much longer) default."""
    seen: list[float | None] = []

    def _capture(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(canary.subprocess, "run", _capture)
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")

    canary.deployment_health("nyxgpt-api-stable", "nyxgpt")

    assert seen == [PROBE_TIMEOUT_SECONDS]


@pytest.mark.unit
def test_deployment_health_reports_a_timeout_as_a_degraded_reading(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary.subprocess, "run", _raise_timeout)

    health = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")

    assert health.state == "error"
    assert "health check timed out" in health.message
    # Not silently rendered as "just not installed yet" -- that's a different fact.
    assert health.state != "not_deployed"


@pytest.mark.unit
def test_current_mode_says_unknown_rather_than_guessing_native_on_a_timeout(monkeypatch):
    """Falling back to "native" would assert something about the substrate nothing checked."""
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary.ops_module, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(canary.subprocess, "run", _raise_timeout)
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)

    assert canary.current_mode() == "unknown"
    assert "Could not determine the deployment mode" in (canary._mode_message("unknown") or "")


@pytest.mark.unit
def test_status_does_not_touch_kubectl_outside_kubernetes_mode(monkeypatch, tmp_path):
    """The cheaper half of the fix: on a native install, don't make the calls at all (#3468)."""
    monkeypatch.setattr(canary, "_state_path", lambda: tmp_path / "canary_state.json")
    # kubectl present but the mode is native: the binary being installed is
    # exactly the case a `_which` check does not catch, since a stale context
    # dials a cluster that is not there.
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary, "current_mode", lambda: "native")
    monkeypatch.setattr(canary, "_current_mode_with_reason", lambda: ("native", ""))
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda *_a, **_k: pytest.fail("deployment_health must not run outside Kubernetes mode"),
    )
    # The per-track metrics reads (#3829) are cluster calls too -- a Pod list
    # plus a Pod-proxy read per Pod -- and are covered by the same guard.
    monkeypatch.setattr(
        canary,
        "track_metrics",
        lambda *_a, **_k: pytest.fail("track_metrics must not run outside Kubernetes mode"),
    )
    monkeypatch.setattr(
        canary,
        "_run",
        lambda *_a, **_k: pytest.fail("status() must make no subprocess call outside Kubernetes"),
    )

    data = canary.status("nyxgpt")

    assert data["mode"] == "native"
    assert data["mode_supported"] is False
    assert data["stable"]["state"] == "not_deployed"
    assert "Kubernetes" in data["stable"]["message"]
    # The panels still answer -- non-attributable, naming the mode as the
    # reason, rather than reporting numbers nothing measured.
    for key in ("metrics", "stable_metrics"):
        assert data[key]["attributable"] is False
        assert "Kubernetes" in data[key]["reason"]


# --- ops ---------------------------------------------------------------------


@pytest.mark.unit
def test_ops_run_returns_a_timeout_result_when_the_caller_handles_failures(monkeypatch):
    monkeypatch.setattr(ops.subprocess, "run", _raise_timeout)

    cp = ops._run(["docker", "ps"], check=False, timeout=1.0)

    assert timed_out(cp)


@pytest.mark.unit
def test_ops_run_raises_the_failure_callers_already_catch_when_check_is_set(monkeypatch):
    """check=True callers handle CalledProcessError; they must not have to learn TimeoutExpired."""
    monkeypatch.setattr(ops.subprocess, "run", _raise_timeout)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        ops._run(["docker", "ps"], check=True, timeout=1.0)

    assert excinfo.value.returncode == TIMEOUT_RETURNCODE


@pytest.mark.unit
def test_ops_run_is_bounded_by_default(monkeypatch):
    """No caller can opt out of a bound by forgetting one."""
    seen: list[float | None] = []

    def _capture(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ops.subprocess, "run", _capture)

    ops._run(["true"], check=False)

    assert seen == [ops.DEFAULT_RUN_TIMEOUT_SECONDS]
    assert ops.DEFAULT_RUN_TIMEOUT_SECONDS is not None


@pytest.mark.unit
def test_ops_pod_read_says_the_cluster_did_not_answer(monkeypatch):
    monkeypatch.setattr(ops.subprocess, "run", _raise_timeout)

    states, failure = ops._k8s_pod_states(expected=True)

    assert states == []
    assert failure is not None and not failure.ok
    assert "did not answer" in failure.message or "timed out" in failure.message


# --- self_heal ---------------------------------------------------------------


@pytest.mark.unit
def test_self_heal_run_no_longer_lets_a_timeout_escape_to_the_handler(monkeypatch):
    monkeypatch.setattr(self_heal.subprocess, "run", _raise_timeout)

    cp = self_heal._run(["docker", "compose", "ps"], timeout=1.0)

    assert timed_out(cp)
    assert "timed out after 1s" in cp.stderr


# --- the enumeration itself --------------------------------------------------


@pytest.mark.unit
def test_the_probe_bound_is_short_enough_to_be_a_bound():
    """A "bound" a dashboard poll can't survive isn't one; keep these honest if edited."""
    assert 0 < subprocess_bounds.PROBE_TIMEOUT_SECONDS <= 10
    assert subprocess_bounds.PROBE_TIMEOUT_SECONDS < subprocess_bounds.LOCAL_PROBE_TIMEOUT_SECONDS
    assert subprocess_bounds.LOCAL_PROBE_TIMEOUT_SECONDS < ops.DEFAULT_RUN_TIMEOUT_SECONDS
