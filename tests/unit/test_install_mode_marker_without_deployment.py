"""An install-mode marker is a record, not a description of what is running (#3989).

Every substrate prints its recorded install mode from a marker file, and every
marker outlives the deployment that wrote it -- a teardown, or an install that
failed halfway, leaves the record behind. The native block always said so ("No
native api/web on this machine -- that is a record of the last native
install"); the Terraform block did not, and printed

    Install mode (terraform): dev (images built from the working tree at ...)
      the api/web containers were built from that working tree, not from
      published images -- ...

on a machine whose very next lines reported every Terraform component
`absent`. The second sentence was emitted whenever the recorded mode was dev,
with no check that anything was deployed at all.

These tests pin the "marker present, nothing deployed" case for all three
substrates: the mode line may still be printed (it is a real record), but
nothing below it may describe containers, Pods or services in the present
tense.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from nyxgpt import install_mode, ops

pytestmark = pytest.mark.unit

RECORD_NOT_STATEMENT = "not a statement about whatever is serving now"


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


def _nothing_native_or_terraform(monkeypatch):
    """`ops status` stubs for a machine with no native and no Terraform stack."""
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "none", "web": "none", "ollama": "none", "cassandra": "absent"},
            compose={},
            conflicts=set(),
            terraform={"api": "absent", "web": "absent", "ollama": "absent"},
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_is_macos", lambda: False)
    monkeypatch.setattr(ops, "_which", lambda _tool: None)
    monkeypatch.setattr(ops, "_serving_status", lambda _mode: {})


def test_status_terraform_marker_with_nothing_deployed_is_reported_as_a_record(
    monkeypatch, capsys, tmp_path
):
    """The reported defect, exactly: a dev marker and no containers."""
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "checkout",
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "nyxgpt-api:local", "web": "nyxgpt-web:local"},
    )
    _nothing_native_or_terraform(monkeypatch)

    ops.status(SimpleNamespace())
    out = capsys.readouterr().out

    tf_line = next(ln for ln in out.splitlines() if "Install mode (terraform):" in ln)
    assert "dev" in tf_line
    # The follow-up sentence asserts containers exist. None do.
    assert "the api/web containers were built from that working tree" not in out
    assert "No Terraform deployment on this machine" in out
    assert RECORD_NOT_STATEMENT in out


def test_status_terraform_marker_still_describes_a_deployment_that_is_running(
    monkeypatch, capsys, tmp_path
):
    """The disclaimer must not swallow the case it was carved out of: with
    containers actually up, dev mode still says what they were built from."""
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "checkout",
        substrate=install_mode.SUBSTRATE_TERRAFORM,
    )
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "none", "web": "none", "ollama": "none", "cassandra": "absent"},
            compose={},
            conflicts=set(),
            terraform={"api": "running", "web": "running", "ollama": "running"},
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_is_macos", lambda: False)
    monkeypatch.setattr(ops, "_which", lambda _tool: None)
    monkeypatch.setattr(ops, "_serving_status", lambda _mode: {})

    ops.status(SimpleNamespace())
    out = capsys.readouterr().out

    assert "the api/web containers were built from that working tree" in out
    assert "No Terraform deployment on this machine" not in out


def test_status_native_marker_with_nothing_installed_is_reported_as_a_record(
    monkeypatch, capsys, tmp_path
):
    """The block the other two are being brought into line with -- a guard so
    it cannot regress into the shape #3989 was filed about."""
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "checkout")
    _nothing_native_or_terraform(monkeypatch)

    ops.status(SimpleNamespace())
    out = capsys.readouterr().out

    assert "Install mode (native api/web): dev" in out
    assert "No native api/web on this machine" in out
    assert RECORD_NOT_STATEMENT in out
    assert "api/web run the working tree" not in out


def _k8s_status_stubs(monkeypatch, pods):
    pod_json = json.dumps(
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
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "none", "web": "none", "ollama": "none"},
            compose={},
            terraform={},
            conflicts=[],
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops, "_which", lambda tool: "/usr/bin/kubectl" if tool == "kubectl" else None
    )
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "kind-nyxgpt-local")
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})
    monkeypatch.setattr(ops, "_serving_status", lambda _m: {"supported": False, "message": "n/a"})
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: _cp(stdout=pod_json))


def test_status_kubernetes_marker_with_no_app_pods_is_reported_as_a_record(
    monkeypatch, capsys, tmp_path
):
    """`_k8s_pod_states` reads every Pod in the namespace, observability
    included, so a namespace holding only those reaches the install-mode block
    with no api/web to describe."""
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "checkout",
        substrate=install_mode.SUBSTRATE_KUBERNETES,
    )
    _k8s_status_stubs(monkeypatch, pods=["grafana-7d9f", "loki-0"])

    assert ops.status(SimpleNamespace()) == 0
    out = capsys.readouterr().out

    assert "Install mode: dev (images built from the working tree" in out
    assert "The Pods run images built from that working tree" not in out
    assert "No nyxGPT api/web Pods in this namespace" in out
    assert RECORD_NOT_STATEMENT in out


def test_status_kubernetes_still_describes_pods_that_are_there(monkeypatch, capsys, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, checkout, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    _k8s_status_stubs(monkeypatch, pods=["grafana-7d9f", "nyxgpt-web-stable-abc"])

    assert ops.status(SimpleNamespace()) == 0
    out = capsys.readouterr().out

    assert "The Pods run images built from that working tree" in out
    assert "No nyxGPT api/web Pods in this namespace" not in out


def test_doctor_terraform_marker_with_nothing_deployed_is_reported_as_a_record(
    monkeypatch, capsys, tmp_path
):
    """`doctor` reaches the same block from a marker alone, and printed the
    recorded mode with nothing saying the stack was down."""
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "gone",
        substrate=install_mode.SUBSTRATE_TERRAFORM,
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent", "web": "absent"})

    issues = ops._terraform_install_mode_issues()
    out = capsys.readouterr().out

    assert "Install mode (terraform): dev" in out
    assert "No Terraform deployment on this machine" in out
    assert RECORD_NOT_STATEMENT in out
    # A record of a past install is not a fault: nothing to fix, so nothing
    # to fail `ops verify` on. In particular the missing-checkout issue below
    # is about images that are *running*.
    assert issues == []
