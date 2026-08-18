"""Unit tests for the Kubernetes install modes (#3834).

`nyxgpt ops install --kubernetes --local` used to build both images from
`REPO_ROOT` unconditionally and apply a kustomization that only exists in a
checkout, so it was permanently a dev deployment that never recorded or
reported the fact -- and it could not run at all on a machine with no
checkout, which the Repo-less Portability requirement names as an in-scope
target.

These tests cover the claims that replaced it:

- the default path builds both images from the *published* artifacts, staged
  into their own build context, with no reference to the checkout;
- `--dev` builds the working tree and is refused when there isn't one;
- the deployment records its own install mode, separately from the native
  api/web marker, and a mode switch rolls the app tier so the cluster and the
  record cannot disagree;
- `status`/`doctor`/`infra_status` report the Kubernetes deployment's mode,
  and never stamp a native mode on components that are not running.

Conventions follow test_ops.py: `_run`/`_which` are stubbed, and the
install-mode markers are redirected into `tmp_path` by the autouse fixture in
conftest.py, so nothing here reads or writes the developer's real ~/.nyxGPT.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import install_mode, ops

pytestmark = pytest.mark.unit


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


def _write_tarball(path: Path, root_name: str, files: dict[str, str]) -> None:
    """Write a `<root_name>/...` tarball, the shape a release artifact has."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for rel, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{root_name}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


# --- the per-substrate markers ---


def test_kubernetes_and_native_markers_are_separate_files():
    assert install_mode.install_mode_file(install_mode.SUBSTRATE_NATIVE) != (
        install_mode.install_mode_file(install_mode.SUBSTRATE_KUBERNETES)
    )
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, "/co")
    # Recording a native dev install says nothing about Kubernetes -- this is
    # exactly the confusion that made `ops status` call a pure-k8s deployment
    # "dev (editable checkout at ...)".
    k8s = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert k8s.recorded is False
    assert k8s.is_dev is False
    assert "unrecorded" in k8s.label()


def test_kubernetes_mode_roundtrips_and_labels_its_source():
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, "/co", substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.is_dev, state.recorded, state.checkout) == (True, True, "/co")
    assert "images built from the working tree at /co" in state.label()

    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.is_dev, state.recorded) == (False, True)
    assert "published nyxgpt-api/nyxgpt-web artifacts" in state.label()


def test_clearing_the_marker_makes_it_unrecorded_again():
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    install_mode.clear_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (
        install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES).recorded
        is False
    )
    # Idempotent: tearing down twice is not an error.
    install_mode.clear_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)


# --- staging the published artifacts into a build context ---


@pytest.fixture
def staged_artifacts(monkeypatch, tmp_path):
    """A packaged-resources root and `$NYXGPT_ARTIFACT_DIR` holding both tarballs.

    Mirrors the real repo-less machine: no checkout to vendor from, the
    artifacts supplied the way `_service_source_tarball` finds them.
    """
    artifact_dir = tmp_path / "artifacts"
    _write_tarball(
        artifact_dir / "nyxgpt-api-9.9.9.tar.gz",
        "nyxgpt-api-9.9.9",
        {
            "pyproject.toml": "[project]\nname='nyxgpt'\n",
            "src/nyxgpt/__init__.py": "",
            "src/nyxgpt/resources/docker/entrypoint.sh": "#!/bin/sh\nexec uvicorn\n",
            "example.config.ini": "[api]\n",
        },
    )
    _write_tarball(
        artifact_dir / "nyxgpt-web-9.9.9.tar.gz",
        "nyxgpt-web-9.9.9",
        {"Dockerfile": "FROM node:20-alpine\n", "package.json": "{}"},
    )
    packaged = tmp_path / "packaged"
    (packaged / "docker").mkdir(parents=True)
    (packaged / "docker" / "nyxgpt-api.Dockerfile").write_text(
        "FROM python:3.11-slim\n", encoding="utf-8"
    )

    monkeypatch.setenv(ops.LOCAL_ARTIFACT_DIR_ENV, str(artifact_dir))
    monkeypatch.setattr(ops, "_packaged_resources_root", lambda: packaged)
    monkeypatch.setattr(ops, "_native_service_version", lambda: "9.9.9")
    monkeypatch.setattr(ops, "K8S_BUILD_DIR", tmp_path / "build")
    # No checkout anywhere: this is the machine the artifact path exists for.
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    return SimpleNamespace(artifact_dir=artifact_dir, packaged=packaged, build=tmp_path / "build")


def test_staged_api_context_carries_the_dockerfile_and_entrypoint(staged_artifacts):
    context = ops._stage_k8s_artifact_context(ops.K8S_IMAGE)

    # What the api Dockerfile COPYs, all present without a checkout.
    assert (context / "pyproject.toml").is_file()
    assert (context / "src" / "nyxgpt").is_dir()
    assert (context / "example.config.ini").is_file()
    assert (context / "Dockerfile").read_text(encoding="utf-8") == "FROM python:3.11-slim\n"
    entrypoint = context / "docker" / "entrypoint.sh"
    assert entrypoint.read_text(encoding="utf-8") == "#!/bin/sh\nexec uvicorn\n"
    assert entrypoint.stat().st_mode & 0o111


def test_staged_web_context_uses_the_artifacts_own_dockerfile(staged_artifacts):
    context = ops._stage_k8s_artifact_context(ops.TF_WEB_IMAGE)
    assert (context / "Dockerfile").read_text(encoding="utf-8") == "FROM node:20-alpine\n"
    assert (context / "package.json").is_file()
    # Named `web`, so an unchanged tree fingerprints identically in either
    # mode and toggling `--dev` doesn't force a pointless rebuild.
    assert context.name == "web"


def test_staging_rebuilds_the_context_from_the_artifact_every_time(staged_artifacts):
    context = ops._stage_k8s_artifact_context(ops.K8S_IMAGE)
    (context / "leftover.txt").write_text("hand-edited", encoding="utf-8")
    context = ops._stage_k8s_artifact_context(ops.K8S_IMAGE)
    assert not (context / "leftover.txt").exists()


def test_staging_reports_a_missing_artifact_as_a_failed_build_step(staged_artifacts, monkeypatch):
    monkeypatch.setattr(ops, "_native_service_version", lambda: "0.0.0-nope")
    results = ops._build_and_load_k8s_api_image(dev=False)
    assert [r.ok for r in results] == [False]
    assert "Could not stage the published nyxgpt-api artifact" in results[0].message
    assert "nyxgpt-api-0.0.0-nope.tar.gz" in results[0].details


# --- which source each mode builds ---


def _record_docker_builds(monkeypatch):
    """Stub docker/kubectl so a build records its context instead of running."""
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return _cp(returncode=1)
        if cmd[:2] == ["kubectl", "config"]:
            return _cp(stdout="docker-desktop")
        return _cp()

    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops, "_run", fake_run)
    return calls


def test_default_api_build_uses_the_staged_artifact_not_the_checkout(
    staged_artifacts, monkeypatch, tmp_path
):
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path / "markers")
    calls = _record_docker_builds(monkeypatch)

    results = ops._build_and_load_k8s_api_image()

    assert all(r.ok for r in results), [r.message for r in results]
    build = next(c for c in calls if c[:2] == ["docker", "build"])
    assert build[-1] == str(staged_artifacts.build / "nyxgpt-api" / "context")
    assert str(ops.REPO_ROOT) not in build[-1]


def test_dev_api_build_uses_the_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path / "markers")
    calls = _record_docker_builds(monkeypatch)

    results = ops._build_and_load_k8s_api_image(dev=True)

    assert all(r.ok for r in results), [r.message for r in results]
    build = next(c for c in calls if c[:2] == ["docker", "build"])
    assert build[-1] == str(ops.REPO_ROOT)


def test_default_web_build_uses_the_staged_artifact(staged_artifacts, monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path / "markers")
    calls = _record_docker_builds(monkeypatch)

    results = ops._build_and_load_k8s_web_image()

    assert all(r.ok for r in results), [r.message for r in results]
    build = next(c for c in calls if c[:2] == ["docker", "build"])
    assert build[-1] == str(staged_artifacts.build / "nyxgpt-web" / "web")


# --- recording the mode, and keeping the cluster in step with it ---


def test_recording_a_first_install_writes_the_marker_without_rolling_anything(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_restart_k8s_app_tier",
        lambda: pytest.fail("a first install has nothing to roll"),
    )
    results = ops._record_k8s_install_mode(dev=False)
    assert all(r.ok for r in results)
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.mode, state.recorded) == (install_mode.INSTALL_MODE_ARTIFACT, True)


def test_switching_modes_rolls_the_app_tier_before_recording(monkeypatch, tmp_path):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: checkout)
    rolled: list[str] = []

    def fake_roll():
        rolled.append("rolled")
        return [ops.OpsResult(True, "rolled")]

    monkeypatch.setattr(ops, "_restart_k8s_app_tier", fake_roll)

    results = ops._record_k8s_install_mode(dev=True)

    assert rolled == ["rolled"]
    assert any("install mode changing: artifact -> dev" in r.message for r in results)
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.is_dev, state.checkout) == (True, str(checkout))


def test_a_failed_rollout_does_not_record_the_new_mode(monkeypatch):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: Path("/co"))
    monkeypatch.setattr(
        ops, "_restart_k8s_app_tier", lambda: [ops.OpsResult(False, "rollout restart failed")]
    )

    results = ops._record_k8s_install_mode(dev=True)

    assert not all(r.ok for r in results)
    # Still artifact: recording dev here would claim a cluster state that the
    # failed rollout means is not true.
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert state.mode == install_mode.INSTALL_MODE_ARTIFACT


def test_restart_app_tier_skips_deployments_that_are_not_there(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        seen.append(list(cmd))
        if cmd[3:4] == ["get"]:
            # Only the api stable Deployment exists.
            return _cp(returncode=0 if cmd[-1] == "deploy/nyxgpt-api-stable" else 1)
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._restart_k8s_app_tier()

    assert all(r.ok for r in results)
    restarted = [c[-1] for c in seen if c[3:5] == ["rollout", "restart"]]
    assert restarted == ["deploy/nyxgpt-api-stable"]


# --- the install entrypoint ---


def test_dev_is_refused_without_a_checkout(monkeypatch):
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: None)
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    results = ops._install_kubernetes_steps(None, dev=True)
    assert [r.ok for r in results] == [False]
    assert "needs a source checkout" in results[0].message
    assert "drop `--dev`" in results[0].details


def _stub_install_steps(monkeypatch, calls):
    """Stub every k8s install step but the two whose dev/artifact choice we assert."""
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda _c: None)
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    # Kwarg-tolerant on purpose: `_preflight_k8s_capacity` takes
    # `skip_observability`, and a step gaining a keyword should not turn this
    # helper into a silent unstubbed call that shells out to `kubectl`.
    for name in (
        "_sync_packaged_resources",
        "_ensure_kubectl_and_cluster",
        # #3825 and #3827 added these two to the step list after this test was
        # written; unstubbed they reach a cluster that does not exist here.
        "_preflight_k8s_capacity",
        "_kubectl_apply_kustomization",
        "_wait_for_k8s_data_tier",
        "_wait_for_k8s_app_tier",
        "_k8s_stack_health",
    ):
        monkeypatch.setattr(ops, name, lambda *_a, name=name, **_k: [ops.OpsResult(True, name)])
    monkeypatch.setattr(ops, "_clear_intentional_stops", lambda _c: [ops.OpsResult(True, "stops")])
    monkeypatch.setattr(ops, "_ensure_k8s_secret", lambda _k: [ops.OpsResult(True, "secret")])

    def fake_api_build(dev=False):
        calls.append(("api", dev))
        return [ops.OpsResult(True, f"api dev={dev}")]

    def fake_web_build(dev=False):
        calls.append(("web", dev))
        return [ops.OpsResult(True, f"web dev={dev}")]

    monkeypatch.setattr(ops, "_build_and_load_k8s_api_image", fake_api_build)
    monkeypatch.setattr(ops, "_build_and_load_k8s_web_image", fake_web_build)


def test_install_defaults_to_the_artifact_path_and_records_it(monkeypatch):
    calls: list[tuple[str, bool]] = []
    _stub_install_steps(monkeypatch, calls)

    results = ops._install_kubernetes_steps(None, skip_observability=True)

    assert all(r.ok for r in results), [r.message for r in results]
    assert calls == [("api", False), ("web", False)]
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert state.mode == install_mode.INSTALL_MODE_ARTIFACT
    # The manifests are packaged resources now, so the sync is not optional --
    # without it there is nothing to `kubectl apply -k` on a clean machine.
    assert any("_sync_packaged_resources" in r.message for r in results)


def test_install_dev_builds_the_working_tree_and_records_the_checkout(monkeypatch, tmp_path):
    calls: list[tuple[str, bool]] = []
    _stub_install_steps(monkeypatch, calls)
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: tmp_path / "checkout")

    results = ops._install_kubernetes_steps(None, skip_observability=True, dev=True)

    assert all(r.ok for r in results), [r.message for r in results]
    assert calls == [("api", True), ("web", True)]
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.is_dev, state.checkout) == (True, str(tmp_path / "checkout"))


def test_a_failed_image_build_leaves_the_previous_record_alone(monkeypatch):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, "/previous", substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    calls: list[tuple[str, bool]] = []
    _stub_install_steps(monkeypatch, calls)
    monkeypatch.setattr(
        ops,
        "_build_and_load_k8s_api_image",
        lambda dev=False: [ops.OpsResult(False, "no artifact")],
    )

    results = ops._install_kubernetes_steps(None, skip_observability=True)

    assert not all(r.ok for r in results)
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_KUBERNETES)
    assert (state.is_dev, state.checkout) == (True, "/previous")


def test_install_kubernetes_cli_passes_dev_through(monkeypatch):
    seen: dict[str, object] = {}

    def fake_steps(api_key, *, skip_observability=False, dev=False):
        seen.update(api_key=api_key, skip_observability=skip_observability, dev=dev)
        return [ops.OpsResult(True, "ok")]

    monkeypatch.setattr(ops, "_install_kubernetes_steps", fake_steps)
    monkeypatch.setattr(ops, "_resolve_locality", lambda _a: "local")
    rc = ops._install_kubernetes(
        SimpleNamespace(api_key="k", skip_observability=False, dev=True, local=True, cloud=False)
    )
    assert rc == 0
    assert seen["dev"] is True


# --- reporting ---


def _status_stubs(monkeypatch, native, pods):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native=native, compose={}, terraform={}, conflicts=[], terraform_conflicts=[]
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    monkeypatch.setattr(
        ops, "_which", lambda tool: "/usr/bin/kubectl" if tool == "kubectl" else None
    )
    monkeypatch.setattr(ops, "_kubectl_context", lambda: "kind-nyxgpt-local")
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})
    monkeypatch.setattr(ops, "_serving_status", lambda _m: {"supported": False, "message": "n/a"})
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=True, **_k: _cp(stdout="\n".join(pods) + "\n" if pods else ""),
    )


def test_status_reports_the_kubernetes_deployments_own_mode(monkeypatch, capsys):
    # The reported failure: a native dev marker left over from an earlier
    # `nyxgpt up --dev`, and a deployment that is entirely in Kubernetes.
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, "/old-checkout")
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    _status_stubs(
        monkeypatch,
        native={"api": "none", "web": "none", "ollama": "none"},
        pods=["nyxgpt-api-stable-abc   1/1   Running   0   1m"],
    )

    assert ops.status(SimpleNamespace()) == 0
    out = capsys.readouterr().out

    # The native marker is attributed to native api/web and is not asserted
    # of anything that is running...
    assert "Install mode (native api/web): dev" in out
    assert "No native api/web on this machine" in out
    assert "[dev]" not in out
    # ...and the deployment that IS running reports what it was built from.
    assert "Install mode: artifact (images built from the published" in out


def test_status_warns_when_a_dev_kubernetes_checkout_is_gone(monkeypatch, capsys, tmp_path):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "deleted",
        substrate=install_mode.SUBSTRATE_KUBERNETES,
    )
    _status_stubs(
        monkeypatch,
        native={"api": "none", "web": "none", "ollama": "none"},
        pods=["nyxgpt-api-stable-abc   1/1   Running   0   1m"],
    )

    assert ops.status(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "Install mode: dev (images built from the working tree" in out
    assert "that checkout no longer exists" in out


def test_doctor_flags_a_dev_kubernetes_deployment_whose_checkout_is_gone(
    monkeypatch, capsys, tmp_path
):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "deleted-checkout",
        substrate=install_mode.SUBSTRATE_KUBERNETES,
    )
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    monkeypatch.setattr(ops, "_stale_venv_doctor_issues", lambda: [])

    rc = ops.doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 2
    assert "Install mode (kubernetes): dev" in out
    assert "Dev-mode Kubernetes deployment recorded, but its checkout is missing" in out


def test_infra_status_reports_the_kubernetes_install_mode(monkeypatch):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, "/co", substrate=install_mode.SUBSTRATE_KUBERNETES
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops.self_heal, "compose_probe", lambda: ops.self_heal.ComposeProbe(available=True)
    )

    result = ops.infra_status()

    assert result["kubernetes"]["install_mode"]["mode"] == install_mode.INSTALL_MODE_DEV
    assert result["kubernetes"]["install_mode"]["recorded"] is True
    assert result["kubernetes"]["install_mode"]["checkout"] == "/co"


def test_infra_status_reports_an_unrecorded_kubernetes_mode_as_unrecorded(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops.self_heal, "compose_probe", lambda: ops.self_heal.ComposeProbe(available=True)
    )

    result = ops.infra_status()

    assert result["kubernetes"]["install_mode"]["recorded"] is False
    assert "unrecorded" in result["kubernetes"]["install_mode"]["label"]


# --- the generated secrets never ship in the wheel ---


def test_every_generated_k8s_secret_is_excluded_from_the_package():
    """Both k8s secrets the product generates are excluded from package data.

    `src/nyxgpt/resources/k8s` is a symlink onto the tracked `k8s/` tree and
    the include pattern is `**/*`, so anything sitting in a developer's
    checkout under that path lands in a wheel built from it. Two files there
    are generated and gitignored rather than tracked -- `secret.yaml` (the
    deployment's real `[auth] api_key`) and `observability/secret.yaml` (the
    Grafana admin password, Slack webhook and GlitchTip DSN) -- and a
    checkout that ran a pre-#3834 `nyxgpt ops install --kubernetes` or
    `nyxgpt ops observability --kubernetes` still has them in-tree.

    Asserted as a class against what the code writes, not as two literals:
    the exclusion was already half-swept once (`k8s/secret.yaml` excluded,
    `k8s/observability/secret.yaml` not), and a third generated secret must
    not be able to repeat that quietly.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():  # pragma: no cover - installed (non-editable) tree
        pytest.skip("no pyproject.toml -- not running from a source checkout")

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - python < 3.11
        pytest.skip("tomllib unavailable")

    with pyproject.open("rb") as fh:
        spec = tomllib.load(fh)["tool"]["setuptools"]["exclude-package-data"]

    # Flattened to paths relative to `nyxgpt.resources`, because the key a
    # pattern lives under is part of its meaning: setuptools applies a
    # pattern only to the package it is keyed under, and an importable-looking
    # subdirectory (`k8s/observability`) is discovered as a namespace package
    # of its own. `k8s/observability/secret.yaml` keyed under
    # `nyxgpt.resources` is inert; the same file keyed under
    # `nyxgpt.resources.k8s.observability` is what actually excludes it.
    excluded = set()
    for package, patterns in spec.items():
        prefix = package.removeprefix("nyxgpt.resources").lstrip(".").replace(".", "/")
        for pattern in patterns:
            excluded.add(f"{prefix}/{pattern}" if prefix else pattern)

    generated = {
        ops.K8S_DIR / "secret.yaml": "_ensure_k8s_secret",
        ops.K8S_OBSERVABILITY_DIR / "secret.yaml": "_ensure_k8s_observability_secret",
    }
    for path, writer in generated.items():
        relative = f"k8s/{path.relative_to(ops.K8S_DIR).as_posix()}"
        assert relative in excluded, (
            f"{relative} is generated by {writer} and gitignored, but nothing excludes it "
            "from nyxgpt.resources -- a wheel built from a checkout that ran that command "
            "would embed the operator's secret"
        )
