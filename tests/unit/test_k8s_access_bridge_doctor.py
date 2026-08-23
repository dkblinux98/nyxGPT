"""`nyxgpt ops doctor` reports the Kubernetes access bridge (#3956).

On a `nyxgpt cloud deploy --kubernetes` instance the thing holding
`127.0.0.1:8000` is a systemd --user unit running the wrapped `nyxgpt ops
port-forward`, not the API process: `k8s/`'s Services are ClusterIP-only by
#3506's premise and #3503's requirement. So the bridge is a component that can
be down while every Pod is Running and every cluster-side check is green --
the tunnel simply finds nothing on the other end, and an operator told only
"the API did not answer" goes looking in the cluster, where nothing is wrong.

Nothing else names it. The deploy's own failure hint points at `nyxgpt cloud
ops doctor`, which is what makes this check the thing that has to answer.
"""

from unittest.mock import patch

import pytest

from nyxgpt import ops


class CP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_template(tmp_path):
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / f"{ops.K8S_ACCESS_BRIDGE_UNIT}.service").write_text("[Unit]\n", encoding="utf-8")
    return unit_dir


def _states(enabled: dict[str, str], active: dict[str, str]):
    """A `_run` stand-in answering `is-enabled`/`is-active` per unit."""

    def fake_run(cmd, check=True, **_k):
        assert cmd[:2] == ["systemctl", "--user"], cmd
        verb, unit = cmd[2], cmd[3]
        target = unit[len(ops.K8S_ACCESS_BRIDGE_UNIT) : -len(".service")]
        table = enabled if verb == "is-enabled" else active
        return CP(stdout=table.get(target, "") + "\n")

    return fake_run


@pytest.mark.unit
def test_a_box_with_no_bridge_template_is_silent(monkeypatch, tmp_path, capsys):
    """A local kind/minikube cluster has no bridge -- the operator runs
    `nyxgpt ops port-forward` in a terminal. `doctor` must not invent a
    component, nor run a single probe looking for one."""
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: tmp_path / "nothing" / "here")

    def never(*_a, **_k):
        raise AssertionError("probed for a bridge that was never installed")

    monkeypatch.setattr(ops, "_run", never)

    assert ops._k8s_access_bridge_issues() == []
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_a_running_bridge_is_reported_and_is_not_an_issue(monkeypatch, tmp_path, capsys):
    unit_dir = _install_template(tmp_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(
        ops,
        "_run",
        _states(
            enabled={"api": "enabled", "web": "enabled", "observability": "enabled"},
            active={"api": "active", "web": "active", "observability": "active"},
        ),
    )

    assert ops._k8s_access_bridge_issues() == []
    out = capsys.readouterr().out
    for target in ("api", "web", "observability"):
        assert f"Kubernetes access bridge ({target}): active" in out


@pytest.mark.unit
def test_a_dead_bridge_is_an_issue_that_says_where_to_look(monkeypatch, tmp_path):
    """The whole point: a down bridge is invisible from the cluster side, so
    the message has to say that the Pods being fine is not the answer."""
    unit_dir = _install_template(tmp_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(
        ops,
        "_run",
        _states(
            enabled={"api": "enabled", "web": "enabled", "observability": "enabled"},
            active={"api": "failed", "web": "active", "observability": "active"},
        ),
    )

    issues = ops._k8s_access_bridge_issues()

    assert len(issues) == 1
    assert "access bridge for api is failed" in issues[0]
    assert "even with every Pod Running" in issues[0]
    assert "nyxgpt cloud deploy" in issues[0]


@pytest.mark.unit
def test_a_disabled_bridge_is_not_a_fault(monkeypatch, tmp_path, capsys):
    """`--skip-observability` deliberately leaves the observability bridge
    alone, so reporting it as broken would make a correct deploy look sick."""
    unit_dir = _install_template(tmp_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(
        ops,
        "_run",
        _states(
            enabled={"api": "enabled", "web": "enabled", "observability": "disabled"},
            active={"api": "active", "web": "active", "observability": "inactive"},
        ),
    )

    assert ops._k8s_access_bridge_issues() == []
    assert "observability" not in capsys.readouterr().out


@pytest.mark.unit
def test_the_check_does_not_run_off_linux(monkeypatch):
    """macOS cloud targets (#3867) never get a cluster, and a local macOS
    workstation has no `systemctl` to ask."""
    monkeypatch.setattr(ops, "_is_linux", lambda: False)

    def never(*_a, **_k):
        raise AssertionError("ran systemctl on a machine that has none")

    monkeypatch.setattr(ops, "_run", never)

    assert ops._k8s_access_bridge_issues() == []


@pytest.mark.unit
def test_doctor_asks_the_bridge_only_when_a_kubernetes_install_is_recorded():
    """Gated with the rest of the Kubernetes block: a native-only box has no
    cluster and no bridge, and would answer with silence anyway -- but the
    gate is what keeps `doctor` from probing on every native run."""
    import inspect

    source = inspect.getsource(ops.doctor)
    gate = source.index("if k8s_install_mode.recorded:")
    call = source.index("_k8s_access_bridge_issues()")

    assert gate < call
    assert "issues.extend(_k8s_access_bridge_issues())" in source
    # And the failure hint the deploy prints points here, so the two must not
    # drift apart into a pointer at a check that does not exist. Scoped to
    # `_deploy` (the body) rather than `deploy` (the wrapper): #3993 split the
    # attempt-record bookkeeping out into the wrapper, so the hint itself --
    # and every other step -- now lives in the body.
    from nyxgpt import cloud_deploy

    assert "reports the bridge units by name" in inspect.getsource(cloud_deploy._deploy)


@pytest.mark.unit
def test_the_probes_are_read_only(monkeypatch, tmp_path):
    """`doctor` diagnoses; it never starts, stops or enables anything."""
    unit_dir = _install_template(tmp_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    seen: list[list[str]] = []

    def record(cmd, check=True, **_k):
        seen.append(cmd)
        return CP(stdout="enabled\n" if cmd[2] == "is-enabled" else "active\n")

    monkeypatch.setattr(ops, "_run", record)

    with patch.object(ops, "_k3s_import_image") as untouched:
        ops._k8s_access_bridge_issues()

    assert untouched.call_count == 0
    assert seen, "the check probed nothing at all"
    assert all(cmd[2] in ("is-enabled", "is-active") for cmd in seen)
