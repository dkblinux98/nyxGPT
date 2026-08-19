"""Importing locally-built images into k3s's containerd (#3956).

The defect this closes is silent by construction. `_build_and_load_k8s_image`
knew about Docker Desktop, kind and minikube, and treated anything else as
"unrecognized cluster context -- skipped image load", which it reported as a
SUCCESS. On k3s -- the cluster `nyxgpt cloud deploy --kubernetes` puts on the
instance -- that is wrong in the worst available way: k3s runs its own
containerd with its own image store, so a `docker build` on the same box
produces an image the cluster cannot see. Every `k8s/*.yaml` Deployment pins
`imagePullPolicy: IfNotPresent` against a `:local` tag that exists in no
registry, so `kubectl apply` succeeds, the Pods are created, and every one of
them sits in `ErrImagePull`/`ImagePullBackOff` while the install reports the
image step green and then waits out its readiness timeout.

Whether the import actually lands is not knowable from here -- that is
`.github/workflows/k3s-cloud-smoke.yml`, which builds a real image, imports it
into a real k3s, and proves the Pod runs with the import and fails without it
(D-006's both-halves rule). What these tests hold is the wiring: that k3s is
detected at all, that the failure is reported as a failure, and that the
detection order does not steal a kind cluster's load step.
"""

import subprocess
from unittest.mock import patch

import pytest

from nyxgpt import ops


class CP:
    """Minimal `CompletedProcess` stand-in, matching test_ops.py's."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _build_succeeds(cmd, check=True, **_k):
    """`_run` stub: the image needs building, and builds."""
    if cmd[:3] == ["docker", "image", "inspect"]:
        return CP(returncode=1)
    if cmd[:2] == ["docker", "build"]:
        return CP(returncode=0)
    if cmd[:2] == ["kubectl", "config"]:
        # k3s's default context is called `default` -- a name that says
        # nothing, which is why detection is by binary and not by context.
        return CP(stdout="default")
    raise AssertionError(f"unexpected: {cmd}")


def _only(*present):
    """`_which` stub: only these programs are on PATH."""
    return lambda prog: f"/usr/local/bin/{prog}" if prog in present else None


class _FakePopen:
    """Stands in for `docker save`, which is a pipe rather than a `_run` call."""

    def __init__(self, argv, returncode=0, stderr=""):
        self.argv = argv
        self.returncode = returncode
        self.stdout = None
        self.stderr = None
        self._stderr_text = stderr

    def __enter__(self):
        self.stderr = _FakeStream(self._stderr_text)
        return self

    def __exit__(self, *exc):
        return False


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def close(self):
        pass


@pytest.fixture
def k3s_calls(monkeypatch):
    """Record the `docker save` and `k3s ctr images import` invocations."""
    calls = {"save": [], "import": []}

    def fake_popen(argv, **_kwargs):
        calls["save"].append(argv)
        return _FakePopen(argv, returncode=calls.get("save_rc", 0))

    def fake_run(argv, **kwargs):
        calls["import"].append(argv)
        calls["import_kwargs"] = kwargs
        return CP(returncode=calls.get("import_rc", 0), stdout="unpacking done")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ops.os, "geteuid", lambda: 1000)
    return calls


@pytest.mark.unit
def test_a_k3s_cluster_gets_the_image_imported(monkeypatch, tmp_path, k3s_calls):
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)

    results = ops._build_and_load_k8s_image()

    assert all(r.ok for r in results), [r.message for r in results]
    assert any("Imported" in r.message and "k3s" in r.message for r in results)
    assert k3s_calls["save"] == [["docker", "save", ops.K8S_IMAGE]]
    assert k3s_calls["import"] == [["sudo", "-n", "k3s", "ctr", "images", "import", "-"]]


@pytest.mark.unit
def test_the_import_is_not_reported_as_a_skipped_load(monkeypatch, tmp_path, k3s_calls):
    """The pre-fix behaviour: `ok=True`, "skipped image load", Pods that never
    start. A k3s cluster must never fall through to that branch."""
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)

    results = ops._build_and_load_k8s_image()

    assert not any("skipped image load" in r.message for r in results)


@pytest.mark.unit
def test_a_failed_import_fails_the_step(monkeypatch, tmp_path, k3s_calls):
    """Not the `ok=True` the unrecognized-cluster branch returns.

    There, an operator was told to load the image themselves and a
    bring-your-own cluster may genuinely share the host cache. Here we know it
    does not, so a failed import is a failed install -- reported at the step
    that caused it rather than eight minutes later as an unexplained Pending
    stack.
    """
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)
    k3s_calls["import_rc"] = 1

    results = ops._build_and_load_k8s_image()

    assert not all(r.ok for r in results)
    assert any("k3s ctr images import failed" in r.message for r in results)


@pytest.mark.unit
def test_a_failed_docker_save_names_itself(monkeypatch, tmp_path, k3s_calls):
    """`docker save` and the import are two processes joined by a pipe, and
    either can fail. Reporting only the import's status would attribute a
    missing image to containerd."""
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)
    k3s_calls["save_rc"] = 1

    results = ops._build_and_load_k8s_image()

    assert not all(r.ok for r in results)
    assert any("docker save" in r.message for r in results)


@pytest.mark.unit
def test_the_import_is_bounded(monkeypatch, tmp_path, k3s_calls):
    """D-027: a subprocess reachable from an HTTP handler is bounded.

    `install_kubernetes_local` is called by the dashboard's ops endpoint, and
    an unbounded pipe there is a worker held forever.
    """
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)

    ops._build_and_load_k8s_image()

    assert k3s_calls["import_kwargs"]["timeout"] == ops.K3S_IMAGE_IMPORT_TIMEOUT_SECONDS


@pytest.mark.unit
def test_an_import_that_times_out_is_a_failure_not_a_traceback(monkeypatch, tmp_path):
    """`subprocess.TimeoutExpired` out of an ops step would abort the whole
    install with a traceback instead of a named failed step."""
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)
    monkeypatch.setattr(ops.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: _FakePopen(argv))

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="k3s ctr images import -", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)

    results = ops._build_and_load_k8s_image()

    assert not all(r.ok for r in results)
    assert any("Could not import" in r.message for r in results)


@pytest.mark.unit
def test_root_does_not_shell_out_through_sudo(monkeypatch, tmp_path, k3s_calls):
    """Containers and CI often run as root with no `sudo` installed at all."""
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s", "sudo"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)

    ops._build_and_load_k8s_image()

    assert k3s_calls["import"] == [["k3s", "ctr", "images", "import", "-"]]


@pytest.mark.unit
def test_no_sudo_on_path_is_attempted_directly(monkeypatch, tmp_path, k3s_calls):
    """Prefixing `sudo` that does not exist turns a permission problem into a
    `FileNotFoundError`, which reads as a broken install rather than as the
    one thing an operator can fix."""
    monkeypatch.setattr(ops, "_which", _only("docker", "k3s"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    monkeypatch.setattr(ops, "_run", _build_succeeds)

    ops._build_and_load_k8s_image()

    assert k3s_calls["import"] == [["k3s", "ctr", "images", "import", "-"]]


@pytest.mark.unit
def test_a_kind_cluster_still_takes_the_kind_branch(monkeypatch, tmp_path):
    """A workstation can have k3s installed and be pointed somewhere else.

    Detection is by binary, not by context, so ordering is what keeps this
    correct -- k3s is tried last of the three.
    """
    monkeypatch.setattr(ops, "_which", _only("docker", "kind", "k3s"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    run_calls = []

    def fake_run(cmd, check=True, **_k):
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="kind-nyxgpt")
        if cmd[:2] == ["kind", "load"]:
            return CP(returncode=0)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    with patch.object(ops, "_k3s_import_image") as never:
        results = ops._build_and_load_k8s_image()

    assert all(r.ok for r in results)
    assert never.call_count == 0
    assert ["kind", "load", "docker-image", ops.K8S_IMAGE, "--name", "nyxgpt"] in run_calls


@pytest.mark.unit
def test_a_cluster_with_no_k3s_still_reports_the_skip(monkeypatch, tmp_path):
    """The bring-your-own-cluster branch is unchanged -- this fix adds a case,
    it does not replace the fallback."""
    monkeypatch.setattr(ops, "_which", _only("docker"))
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="some-other-cluster")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()

    assert all(r.ok for r in results)
    assert any("Unrecognized cluster context" in r.message for r in results)
