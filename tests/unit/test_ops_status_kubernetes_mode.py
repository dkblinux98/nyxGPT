"""`ops status`/`ops doctor` reporting on the deployment, not the host (#3987).

The owner's Kubernetes acceptance run (`nyxgpt ops install --kubernetes --dev`,
14/14 Pods Running) got a status report that described the native machine for
everything outside its dedicated Kubernetes section, and so said three things
that were false about the deployment actually running:

1. `UNKNOWN (Ollama unreachable)` for two models that `kubectl -n nyxgpt exec
   ollama-0 -- ollama list` showed were both present -- the probe went to
   `127.0.0.1:11434`, where a Kubernetes install is not supposed to have an
   Ollama at all.
2. A ~50-line urllib traceback printed into the terminal for that entirely
   expected condition, two lines above the graceful verdict it duplicated.
3. A block titled "Deployment mode" that enumerated native/docker/compose/
   terraform and stopped, reading as "nothing is deployed" immediately above a
   running cluster.

These tests pin all three, and the doctor half of the same defect. The #3837
constraint they must not undo: the dict `required_models_status` returns is
served verbatim by `GET /models/required`, so a caught exception's *message*
may never travel in it -- only the class name, with the detail in the log.
"""

from __future__ import annotations

import json
import logging
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import model_bootstrap, ops

OLLAMA_LIST_OUTPUT = (
    "NAME                       ID              SIZE      MODIFIED\n"
    "nomic-embed-text:latest    0a109f422b47    274 MB    4 minutes ago\n"
    "qwen3:0.6b                 7df6b6e09427    522 MB    5 minutes ago\n"
)


class _CP:
    """The subset of `CompletedProcess` `ops._run`'s callers actually read."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _cfg() -> ConfigParser:
    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": "qwen3:0.6b"}
    cfg["ollama"] = {"base_url": "http://127.0.0.1:11434"}
    cfg["rag"] = {"embedding_model": "nomic-embed-text"}
    return cfg


def _never_asked_natively(monkeypatch) -> None:
    """Fail loudly if the host's Ollama is probed at all.

    The defect was not "the cluster went unread" -- it was that the *host* was
    read and its answer reported as the deployment's. A test that only asserts
    the models come back PRESENT would pass on a machine that happened to have
    a native Ollama holding them.
    """

    def unexpected(base_url=None):  # noqa: ARG001
        raise AssertionError("the host's Ollama must not be probed in Kubernetes mode")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unexpected)


def _pods(*specs: tuple[str, str]) -> list[ops.K8sWorkloadState]:
    return [ops.K8sWorkloadState(name=name, state=state, summary=state) for name, state in specs]


# --- 1. the required-models check asks the Ollama that serves the deployment ---


@pytest.mark.unit
def test_required_models_are_read_from_the_in_cluster_ollama(monkeypatch):
    _never_asked_natively(monkeypatch)
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _CP(stdout=OLLAMA_LIST_OUTPUT)

    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", fake_run)

    info = ops.required_models_status(cfg=_cfg(), kubernetes=True)

    assert info["reachable"] is True
    assert info["ready"] is True
    assert [m["present"] for m in info["models"]] == [True, True]
    # Reported against the endpoint the api Pods actually use, so the line the
    # operator reads names the Ollama the verdict is about.
    assert info["base_url"] == ops.K8S_OLLAMA_BASE_URL
    assert seen == [
        ["kubectl", "-n", "nyxgpt", "exec", "statefulset/ollama", "--", "ollama", "list"]
    ]


@pytest.mark.unit
def test_the_in_cluster_read_reports_missing_models_as_missing(monkeypatch):
    """The check still has to be able to say no -- a source swap that always
    answers "present" would trade a false negative for a false positive."""
    _never_asked_natively(monkeypatch)
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_k: _CP(stdout="NAME    ID    SIZE    MODIFIED\nqwen3:0.6b  a  1 GB  now\n"),
    )

    info = ops.required_models_status(cfg=_cfg(), kubernetes=True)

    assert info["ready"] is False
    assert [m["present"] for m in info["models"]] == [True, False]
    assert "nomic-embed-text" in info["remediation"]


@pytest.mark.unit
def test_an_unreadable_cluster_ollama_is_unknown_and_names_no_transport_detail(monkeypatch, caplog):
    """#3837 holds on the Kubernetes path too: this dict reaches a browser."""
    cluster_detail = "error: unable to upgrade connection to node ip-10-0-3-14.internal"
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stderr=cluster_detail, returncode=1))

    with caplog.at_level(logging.WARNING, logger="nyxgpt.ops"):
        info = ops.required_models_status(cfg=_cfg(), kubernetes=True)

    assert info["reachable"] is False
    assert [m["present"] for m in info["models"]] == [None, None]
    assert info["error"] == "KubectlExecFailed"
    assert cluster_detail not in json.dumps(info)
    assert cluster_detail in caplog.text


@pytest.mark.unit
def test_the_printed_block_points_at_the_pod_not_the_host_service(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(returncode=1))

    ops._print_required_models_status(kubernetes=True)

    out = capsys.readouterr().out
    assert ops.K8S_OLLAMA_BASE_URL in out
    # "run `nyxgpt ops status` again once the ollama service is up" would send
    # the operator to the wrong machine.
    assert "ollama service is up" not in out
    assert "nyxgpt" in out


@pytest.mark.unit
def test_the_host_ollama_is_still_what_a_non_kubernetes_status_reports(monkeypatch):
    """The default is unchanged, and deliberately so: `GET /models/required` is
    served from inside an api Pod whose own config already names
    `http://ollama:11434`, and every native install must keep asking its host."""
    asked: list[str | None] = []

    def fake_installed(base_url=None):
        asked.append(base_url)
        return {"qwen3:0.6b", "nomic-embed-text:latest"}

    monkeypatch.setattr(model_bootstrap, "installed_model_names", fake_installed)
    monkeypatch.setattr(
        ops,
        "_k8s_installed_model_names",
        lambda: pytest.fail("the cluster must not be probed unless the caller asks"),
    )

    info = ops.required_models_status(cfg=_cfg())

    assert asked == ["http://127.0.0.1:11434"]
    assert info["base_url"] == "http://127.0.0.1:11434"


# --- 2. one readable line, never a stack trace ---


@pytest.mark.unit
def test_an_unreachable_ollama_logs_one_line_and_no_traceback(monkeypatch, caplog):
    """The `exc_info` #3837 added dumped a ~50-line urllib traceback onto the
    terminal of every `ops status` in Kubernetes mode, where an absent host
    Ollama is the *normal* state. The class name still has to reach the dict
    and the message still has to reach the log -- only the stack goes."""
    host_state = "connection refused via proxy.corp.internal:3128"

    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError(host_state)

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.ops"):
        info = ops.required_models_status(cfg=_cfg())

    records = [r for r in caplog.records if "Ollama model lookup failed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "\n" not in records[0].getMessage()
    assert "Traceback (most recent call last)" not in caplog.text
    # Still diagnostic: the class for the browser, the message for the log.
    assert info["error"] == "RuntimeError"
    assert host_state in records[0].getMessage()
    assert host_state not in json.dumps(info)


# --- 3. the "Deployment mode" block names Kubernetes ---


def _stub_status_probes(
    monkeypatch, probe: ops.K8sDeploymentProbe, *, stub_models: bool = True
) -> None:
    """Reduce `ops status` to the one question these tests are about."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP())
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda _name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(ops, "_k8s_deployment_probe", lambda: probe)
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})
    monkeypatch.setattr(ops, "_serving_status", lambda _mode: {"supported": False})
    if stub_models:
        monkeypatch.setattr(ops, "_print_required_models_status", lambda **_k: None)


def _deployment_mode_block(capsys) -> str:
    out = capsys.readouterr().out
    return out[out.index("Deployment mode:") : out.index("Config in use:")]


@pytest.mark.unit
def test_deployment_mode_block_names_a_running_kubernetes_deployment(monkeypatch, capsys):
    _stub_status_probes(
        monkeypatch,
        ops.K8sDeploymentProbe(
            _pods(
                ("nyxgpt-api-stable-0", ops.K8S_STATE_READY),
                ("nyxgpt-web-stable-0", ops.K8S_STATE_READY),
                ("ollama-0", ops.K8S_STATE_PENDING),
            )
        ),
    )

    assert ops.status(SimpleNamespace()) == 0

    assert "kubernetes: nyxgpt namespace: 2/3 pod(s) ready" in _deployment_mode_block(capsys)


@pytest.mark.unit
def test_deployment_mode_block_says_not_detected_with_an_empty_namespace(monkeypatch, capsys):
    _stub_status_probes(monkeypatch, ops.K8sDeploymentProbe([], "no pods in the nyxgpt namespace"))

    assert ops.status(SimpleNamespace()) == 0

    assert "kubernetes: not detected (no pods in the nyxgpt namespace)" in _deployment_mode_block(
        capsys
    )


@pytest.mark.unit
def test_deployment_mode_block_keeps_cannot_determine_apart_from_not_deployed(monkeypatch, capsys):
    """#3468's distinction, which the CLI block did not have at all: a
    configured cluster that did not answer is not evidence of an empty one."""
    _stub_status_probes(
        monkeypatch,
        ops.K8sDeploymentProbe([], "Could not read pod status", determined=False),
    )

    assert ops.status(SimpleNamespace()) == 0

    block = _deployment_mode_block(capsys)
    assert "kubernetes: cannot determine (Could not read pod status)" in block
    assert "kubernetes: not detected" not in block


@pytest.mark.unit
def test_status_reads_the_cluster_once_for_the_whole_command(monkeypatch, capsys):
    """Cost, and consistency: the summary block, the model check and the
    Kubernetes section all describe one deployment and must not each pay for
    -- or disagree about -- their own read of it."""
    reads = 0

    def counted_read():
        nonlocal reads
        reads += 1
        return ops.K8sDeploymentProbe(_pods(("ollama-0", ops.K8S_STATE_READY)))

    _stub_status_probes(monkeypatch, ops.K8sDeploymentProbe([]))
    monkeypatch.setattr(ops, "_k8s_deployment_probe", counted_read)

    assert ops.status(SimpleNamespace()) == 0
    capsys.readouterr()

    assert reads == 1


@pytest.mark.unit
def test_status_asks_the_cluster_for_models_when_pods_are_running(monkeypatch, capsys):
    """The wiring, not just the helper: `status` is what decides which Ollama
    the block below it is about."""
    asked: list[bool] = []
    _stub_status_probes(
        monkeypatch, ops.K8sDeploymentProbe(_pods(("ollama-0", ops.K8S_STATE_READY)))
    )
    monkeypatch.setattr(
        ops,
        "_print_required_models_status",
        lambda *, kubernetes=False, **_k: asked.append(kubernetes),
    )

    assert ops.status(SimpleNamespace()) == 0
    capsys.readouterr()

    assert asked == [True]


@pytest.mark.unit
def test_an_unreadable_namespace_makes_the_model_block_say_it_read_the_host(monkeypatch, capsys):
    """The third state, and the one that could still mislead: with the
    namespace unreadable there is nothing to ask but this host, so the block
    reports it -- but `PRESENT` printed under a `cannot determine` line reads
    as a statement about the deployment unless the scope is said out loud."""
    _stub_status_probes(
        monkeypatch,
        ops.K8sDeploymentProbe([], "Could not read pod status", determined=False),
        stub_models=False,
    )
    monkeypatch.setattr(
        ops,
        "required_models_status",
        lambda **_k: {
            "base_url": "http://127.0.0.1:11434",
            "models": [{"role": "chat", "model": "qwen3:0.6b", "present": True}],
            "reachable": True,
            "ready": True,
            "error": "",
            "remediation": "",
        },
    )

    assert ops.status(SimpleNamespace()) == 0

    out = capsys.readouterr().out
    assert "the nyxgpt namespace could not be read (Could not read pod status)" in out
    assert "this host's Ollama only" in out


@pytest.mark.unit
def test_a_deployed_cluster_adds_no_host_only_note(monkeypatch, capsys):
    """The note is for the unreadable case alone -- printing it when the
    cluster answered would contradict the `(in-cluster)` header above it."""
    _stub_status_probes(
        monkeypatch, ops.K8sDeploymentProbe(_pods(("ollama-0", ops.K8S_STATE_READY)))
    )
    seen: list[str] = []
    monkeypatch.setattr(
        ops,
        "_print_required_models_status",
        lambda *, kubernetes=False, cluster_unreadable="": seen.append(cluster_unreadable),
    )

    assert ops.status(SimpleNamespace()) == 0
    capsys.readouterr()

    assert seen == [""]


# --- 4. the same three defects in `ops doctor` ---


@pytest.mark.unit
def test_doctor_checks_the_in_cluster_ollama_when_the_deployment_is_kubernetes(
    monkeypatch, tmp_path
):
    """Doctor's native reading was a false *silence* rather than a false
    report: with no host Ollama the check returned None, so the one fault it
    exists to catch could not be found on a Kubernetes deployment at all."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[nyxgpt]\ndefault_model = qwen3:0.6b\n\n[rag]\nembedding_model = nomic-embed-text\n",
        encoding="utf-8",
    )
    _never_asked_natively(monkeypatch)
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *_a, **_k: _CP(stdout="NAME    ID    SIZE    MODIFIED\nqwen3:0.6b  a  1 GB  now\n"),
    )

    issue = ops._missing_required_models_issue(cfg_path, kubernetes=True)

    assert issue is not None
    assert "nomic-embed-text" in issue
    # And names the command that pulls into the cluster -- `nyxgpt ops install`
    # and `nyxgpt models pull` both act on this host's Ollama.
    assert "nyxgpt ops install --kubernetes" in issue


@pytest.mark.unit
def test_doctor_is_silent_when_the_in_cluster_ollama_cannot_be_asked(monkeypatch, tmp_path):
    """Same rule as the unreachable host Ollama: that is the Ollama's own
    fault, reported by the Pod lines, and inferring "model missing" from it
    would misname it."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen3:0.6b\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stderr="no such pod", returncode=1))

    assert ops._missing_required_models_issue(cfg_path, kubernetes=True) is None


@pytest.mark.unit
def test_doctor_still_reads_the_host_when_no_kubernetes_deployment_is_running(
    monkeypatch, tmp_path
):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen3:0.6b\n", encoding="utf-8")
    monkeypatch.setattr(
        ops,
        "_k8s_installed_model_names",
        lambda: pytest.fail("the cluster must not be probed unless the caller asks"),
    )
    monkeypatch.setattr(
        model_bootstrap,
        "installed_model_names",
        lambda base_url=None: set(),  # noqa: ARG005
    )

    issue = ops._missing_required_models_issue(cfg_path)

    assert issue is not None
    assert "--kubernetes" not in issue


# --- the shared read itself ---


@pytest.mark.unit
def test_the_probe_costs_nothing_without_kubectl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: None)
    monkeypatch.setattr(
        ops, "_k8s_pod_states", lambda *_a, **_k: pytest.fail("no kubectl, no read")
    )
    monkeypatch.setattr(ops, "_kubectl_context", lambda: pytest.fail("no kubectl, no read"))

    probe = ops._k8s_deployment_probe()

    assert probe.deployed is False
    assert probe.determined is True
    assert probe.summary == "not detected (kubectl not found)"


@pytest.mark.unit
def test_a_kubectl_with_no_context_is_not_deployed_rather_than_unknown(monkeypatch):
    """Nothing on this machine was ever pointed at a cluster, so "not
    detected" is a fact -- and the Pod read is skipped, which is the cheap
    answer on every machine that has kubectl for some other reason."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "")
    monkeypatch.setattr(
        ops, "_k8s_pod_states", lambda *_a, **_k: pytest.fail("no context, no pod read")
    )

    probe = ops._k8s_deployment_probe()

    assert probe.deployed is False
    assert probe.determined is True
    assert "no cluster configured" in probe.summary


@pytest.mark.unit
def test_a_configured_cluster_that_did_not_answer_is_cannot_determine(monkeypatch):
    """#3468: the state reserved for a cluster that exists and did not answer.
    Reporting it as "nothing deployed" is a confident claim about a machine
    nyxGPT could not read."""
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "kind-nyxgpt-local")
    monkeypatch.setattr(
        ops,
        "_k8s_pod_states",
        lambda *_a, **_k: ([], ops.OpsResult(False, "Could not read pod status")),
    )

    probe = ops._k8s_deployment_probe()

    assert probe.deployed is False
    assert probe.determined is False
    assert probe.summary == "cannot determine (Could not read pod status)"


@pytest.mark.unit
def test_the_ollama_list_header_is_not_read_as_a_model(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(stdout=OLLAMA_LIST_OUTPUT))

    names, error, detail = ops._k8s_installed_model_names()

    assert error == "" and detail == ""
    assert names == {"nomic-embed-text:latest", "qwen3:0.6b"}


@pytest.mark.unit
def test_missing_kubectl_is_its_own_classified_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _prog: None)

    names, error, _detail = ops._k8s_installed_model_names()

    assert names is None
    assert error == "KubectlNotFound"


@pytest.mark.unit
def test_ops_status_still_returns_zero_on_a_machine_with_a_cluster(monkeypatch, tmp_path, capsys):
    """`ops status` is the diagnostic run on a machine in any state; its
    contract is to always return 0 (#3824), Kubernetes branch included."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    _stub_status_probes(
        monkeypatch,
        ops.K8sDeploymentProbe(_pods(("ollama-0", ops.K8S_STATE_FAILED))),
        stub_models=False,
    )
    monkeypatch.setattr(ops, "_run", lambda *_a, **_k: _CP(returncode=1))

    assert ops.status(SimpleNamespace()) == 0
    assert "UNKNOWN (Ollama unreachable)" in capsys.readouterr().out
