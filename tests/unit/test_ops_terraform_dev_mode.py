"""Unit tests for the Terraform install mode: dev vs artifact (#3835).

Before this, `nyxgpt ops install --terraform --local` was stuck permanently
in dev mode and never said so: it built both images from `REPO_ROOT`, read
its `.tf` files out of the checkout, and recorded no install mode at all, so
`ops status` labelled the deployment with whatever the *native* install had
last recorded. These tests cover the four claims the issue makes:

- `--dev` is accepted and means "build the api/web images from this
  checkout's working tree", and is refused when there is no checkout;
- without `--dev` the deploy runs published images and touches no checkout
  -- neither for the images nor for the Terraform configuration itself;
- the deployment records its own install mode, in its own marker, so a
  Terraform install can never restate the native services' mode (or
  overwrite the marker that decides how they are restarted);
- `status`/`doctor`/`infra_status` report the mode of the deployment that is
  actually running.

Conventions follow test_ops_dev_mode.py: `_run`/`_which` are mocked and the
markers/working directory are redirected into `tmp_path` by the autouse
fixtures in conftest.py, so nothing reads or writes a real ~/.nyxGPT.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from nyxgpt import install_mode, ops

pytestmark = pytest.mark.unit


class Args:
    """Minimal CLI-args stand-in (attribute bag, like argparse.Namespace)."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def checkout(tmp_path):
    """A directory shaped like a source checkout, as `_dev_checkout_root` sees one."""
    root = tmp_path / "src-checkout"
    (root / "src" / "nyxgpt").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='nyxgpt'\n", encoding="utf-8")
    (root / "web" / "node_modules").mkdir(parents=True)
    (root / "web" / "package.json").write_text("{}", encoding="utf-8")
    return root


# --- the Terraform marker is its own file ---


def test_terraform_marker_defaults_to_artifact_when_nothing_recorded():
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)
    assert state.mode == install_mode.INSTALL_MODE_ARTIFACT
    assert state.is_dev is False
    assert state.is_terraform is True
    assert state.recorded is False
    # Nothing is deployed, so the artifact default is the whole truth.
    assert "artifact" in state.label()


def test_nothing_recorded_under_a_running_stack_is_reported_as_unknown():
    """The default is a safe fallback, not a claim about the machine.

    For Terraform it is the *wrong* claim under a live stack: every
    deployment made before #3835 was built from the working tree, because
    that path had no other mode. `deployed=True` must therefore report the
    build as unrecorded rather than as artifact.
    """
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)

    label = state.label(deployed=True)
    assert "not recorded" in label
    assert not label.startswith("artifact")
    assert state.short_label(deployed=True) == install_mode.INSTALL_MODE_UNRECORDED
    assert state.short_label(deployed=False) == install_mode.INSTALL_MODE_ARTIFACT
    # The way out is named, and it is a wrapped command (never raw terraform).
    assert "nyxgpt up --terraform --local" in label


def test_a_recorded_artifact_deployment_is_still_reported_as_artifact():
    """`deployed` only reclassifies the *unrecorded* case."""
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT, None, substrate=install_mode.SUBSTRATE_TERRAFORM
    )
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)

    assert state.recorded is True
    assert state.label(deployed=True).startswith("artifact")
    assert state.short_label(deployed=True) == install_mode.INSTALL_MODE_ARTIFACT


def test_a_dev_deployment_is_reported_as_dev_whether_or_not_it_is_running():
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV, "/src/nyxGPT", substrate=install_mode.SUBSTRATE_TERRAFORM
    )
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)

    assert state.short_label(deployed=True) == install_mode.INSTALL_MODE_DEV
    assert "/src/nyxGPT" in state.label(deployed=True)


def test_an_unreadable_marker_is_not_read_as_a_recorded_mode():
    """A corrupt marker recorded nothing legible, so it recorded nothing."""
    install_mode.install_mode_file(install_mode.SUBSTRATE_TERRAFORM).parent.mkdir(
        parents=True, exist_ok=True
    )
    install_mode.install_mode_file(install_mode.SUBSTRATE_TERRAFORM).write_text(
        "{not json", encoding="utf-8"
    )

    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)

    assert state.recorded is False
    assert "not recorded" in state.label(deployed=True)


def test_the_native_default_is_unaffected_by_the_terraform_rule():
    """Artifact really was the only native mode before #3789, so the native
    default stays a true statement even under running services."""
    state = install_mode.read_install_mode()

    assert state.recorded is False
    assert state.label(deployed=True).startswith("artifact")
    assert state.short_label(deployed=True) == install_mode.INSTALL_MODE_ARTIFACT


def test_terraform_marker_records_mode_and_the_images_it_runs(tmp_path):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT,
        None,
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "ghcr.io/dkblinux98/nyxgpt-api:2.1.0"},
    )
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)
    assert state.images == {"api": "ghcr.io/dkblinux98/nyxgpt-api:2.1.0"}
    # The label names the image, so an operator reading `ops status` can see
    # which build is serving rather than inferring it.
    assert "ghcr.io/dkblinux98/nyxgpt-api:2.1.0" in state.label()


def test_terraform_install_never_touches_the_native_marker(tmp_path):
    """The native marker also decides whether `restart api` drives launchd or
    `brew services` -- a Terraform deploy must not rewrite it (#3835)."""
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")

    ops._record_terraform_install_mode(False, {"api": "img", "web": "img"})

    assert install_mode.read_install_mode().is_dev is True
    assert (
        install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM).is_dev is False
    )


def test_terraform_dev_marker_records_the_checkout(monkeypatch, checkout):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    results = ops._record_terraform_install_mode(True, {"api": "nyxgpt-api:local"})
    assert all(r.ok for r in results)
    state = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)
    assert state.is_dev is True
    assert state.checkout == str(checkout)


# --- image resolution: published images, override, fallback ---


def test_published_image_is_pulled_at_this_version(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "2.1.0")
    pulls: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        pulls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    images, results = ops._pull_terraform_published_images()

    assert images == {
        "api": "ghcr.io/dkblinux98/nyxgpt-api:2.1.0",
        "web": "ghcr.io/dkblinux98/nyxgpt-web:2.1.0",
    }
    assert all(r.ok for r in results)
    assert ["docker", "pull", "ghcr.io/dkblinux98/nyxgpt-api:2.1.0"] in pulls


def test_unpublished_version_falls_back_and_says_so(monkeypatch):
    """A release candidate has no images of its own (release-artifacts.yml runs
    on `released`), so the fallback is real -- but it is version skew and the
    result has to name it rather than quietly deploying another release."""
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc9")

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["docker", "pull"] and cmd[2].endswith(":3.0.0rc9"):
            return _cp(returncode=1, stderr="manifest unknown")
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    images, results = ops._pull_terraform_published_images()

    assert images["api"] == "ghcr.io/dkblinux98/nyxgpt-api:latest"
    assert all(r.ok for r in results)
    skew = [r for r in results if "is not published" in r.message]
    assert skew and "NOT version 3.0.0rc9" in skew[0].details


def test_no_pullable_image_fails_with_the_two_ways_out(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "9.9.9")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: _cp(returncode=1, stderr="nope"))

    images, results = ops._pull_terraform_published_images()

    assert images == {}
    failure = next(r for r in results if not r.ok)
    assert "NYXGPT_TF_API_IMAGE" in failure.details
    assert "--dev" in failure.details


def test_staged_image_override_is_used_without_pulling(monkeypatch):
    """The `NYXGPT_TF_*_IMAGE` override mirrors NYXGPT_ARTIFACT_DIR for the
    native tarballs: an image already in the local daemon is the artifact."""
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setenv("NYXGPT_TF_API_IMAGE", "staged/nyxgpt-api:under-test")
    monkeypatch.setenv("NYXGPT_TF_WEB_IMAGE", "staged/nyxgpt-web:under-test")
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    images, results = ops._pull_terraform_published_images()

    assert images["api"] == "staged/nyxgpt-api:under-test"
    assert all(r.ok for r in results)
    assert not any(cmd[:2] == ["docker", "pull"] for cmd in calls)


def test_image_resolution_without_docker_fails_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    images, results = ops._pull_terraform_published_images()
    assert images == {}
    assert not results[0].ok


# --- what reaches `terraform plan` ---


def test_artifact_plan_passes_published_images_and_no_build(monkeypatch, checkout):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    args = ops._terraform_image_vars(
        {"api": "ghcr.io/x/nyxgpt-api:1", "web": "ghcr.io/x/nyxgpt-web:1"}, dev=False
    )
    assert "-var=build_from_source=false" in args
    assert "-var=api_image=ghcr.io/x/nyxgpt-api:1" in args
    # The build context is what makes a deploy need a checkout -- the
    # artifact path must not send one at all.
    assert not any(a.startswith("-var=repo_path=") for a in args)


def test_dev_plan_builds_from_the_checkout(monkeypatch, checkout):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    args = ops._terraform_image_vars({"api": "nyxgpt-api:local", "web": "nyxgpt-web:local"}, True)
    assert "-var=build_from_source=true" in args
    assert f"-var=repo_path={checkout}" in args


# --- the configuration itself is packaged, not read from a checkout ---


def test_sync_materializes_the_packaged_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    results = ops._sync_local_terraform_config()
    assert all(r.ok for r in results)
    for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf"):
        assert (ops.TERRAFORM_DIR / name).is_file()


def test_sync_preserves_tfvars_and_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    ops.TERRAFORM_DIR.mkdir(parents=True)
    (ops.TERRAFORM_DIR / "terraform.tfvars").write_text('auth_api_key = "keep-me"\n')
    (ops.TERRAFORM_DIR / "terraform.tfstate").write_text('{"resources": [1]}')

    ops._sync_local_terraform_config()

    assert "keep-me" in (ops.TERRAFORM_DIR / "terraform.tfvars").read_text()
    assert (ops.TERRAFORM_DIR / "terraform.tfstate").read_text() == '{"resources": [1]}'


def test_pre_3835_checkout_state_is_migrated_not_orphaned(monkeypatch, tmp_path):
    """A machine that deployed before #3835 has its state in the checkout.
    Leaving it there would make `apply` collide with the running containers
    and `down --terraform` destroy nothing while reporting success."""
    repo = tmp_path / "checkout"
    (repo / "terraform").mkdir(parents=True)
    (repo / "terraform" / "terraform.tfstate").write_text(
        json.dumps({"resources": [{"type": "docker_container"}]})
    )
    monkeypatch.setattr(ops, "REPO_ROOT", repo)

    results = ops._sync_local_terraform_config()

    assert any("Migrated" in r.message for r in results)
    migrated = json.loads((ops.TERRAFORM_DIR / "terraform.tfstate").read_text())
    assert migrated["resources"]


def test_pre_3835_tfvars_is_adopted_so_the_auth_key_is_not_rotated(monkeypatch, tmp_path):
    """`_ensure_terraform_tfvars` generates a random auth key when there is no
    file -- so an upgrade that left the old tfvars behind would silently
    rotate a running deployment's key on its next apply."""
    repo = tmp_path / "checkout"
    (repo / "terraform").mkdir(parents=True)
    (repo / "terraform" / "terraform.tfvars").write_text('auth_api_key = "the-operators-key"\n')
    monkeypatch.setattr(ops, "REPO_ROOT", repo)

    ops._sync_local_terraform_config()

    migrated = ops.TERRAFORM_DIR / "terraform.tfvars"
    assert "the-operators-key" in migrated.read_text()
    # Carried over as a secret, not as a world-readable copy.
    assert oct(migrated.stat().st_mode)[-3:] == "600"
    # And the bootstrap then leaves it alone.
    ops._ensure_terraform_tfvars("some-other-key")
    assert "the-operators-key" in migrated.read_text()


def test_empty_checkout_state_is_not_migrated(monkeypatch, tmp_path):
    """A post-destroy state records nothing worth carrying over; copying it
    would only make it look like a migration happened."""
    repo = tmp_path / "checkout"
    (repo / "terraform").mkdir(parents=True)
    (repo / "terraform" / "terraform.tfstate").write_text(json.dumps({"resources": []}))
    monkeypatch.setattr(ops, "REPO_ROOT", repo)

    results = ops._sync_local_terraform_config()

    assert not any("Migrated" in r.message for r in results)
    assert not (ops.TERRAFORM_DIR / "terraform.tfstate").exists()


def test_existing_state_is_never_overwritten_by_the_migration(monkeypatch, tmp_path):
    repo = tmp_path / "checkout"
    (repo / "terraform").mkdir(parents=True)
    (repo / "terraform" / "terraform.tfstate").write_text(json.dumps({"resources": ["old"]}))
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    ops.TERRAFORM_DIR.mkdir(parents=True)
    (ops.TERRAFORM_DIR / "terraform.tfstate").write_text(json.dumps({"resources": ["current"]}))

    ops._sync_local_terraform_config()

    state = json.loads((ops.TERRAFORM_DIR / "terraform.tfstate").read_text())
    assert state["resources"] == ["current"]


# --- the install steps ---


def _stub_install_steps(monkeypatch):
    """Stub every step of `_install_terraform_steps` except the ones under test."""
    ok = [ops.OpsResult(True, "ok")]
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    for name in (
        "_sync_packaged_resources",
        "migrate_legacy_volumes",
        "_ensure_terraform_binary",
        "_sync_local_terraform_config",
        "_generate_compose_config",
        "_ensure_glitchtip_secrets_dir",
        "_sync_grafana_slack_webhook_secret",
        "_start_observability_stack_terraform",
        "_provision_glitchtip",
        "_terraform_stack_health",
    ):
        monkeypatch.setattr(ops, name, lambda *a, **k: list(ok))
    monkeypatch.setattr(ops, "_clear_intentional_stops", lambda components: list(ok))
    monkeypatch.setattr(ops, "_ensure_terraform_tfvars", lambda api_key: list(ok))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_ops_action_outcome", lambda results: ("success", ""))


def test_artifact_install_pulls_published_images_and_never_builds(monkeypatch):
    _stub_install_steps(monkeypatch)
    built: list[str] = []
    monkeypatch.setattr(ops, "_build_terraform_docker_images", lambda: built.append("built") or [])
    monkeypatch.setattr(
        ops,
        "_pull_terraform_published_images",
        lambda: (
            {"api": "ghcr.io/x/nyxgpt-api:1", "web": "ghcr.io/x/nyxgpt-web:1"},
            [ops.OpsResult(True, "pulled")],
        ),
    )
    applied: list[tuple] = []
    monkeypatch.setattr(
        ops,
        "_terraform_init_plan_apply",
        lambda images, dev: applied.append((images, dev)) or [ops.OpsResult(True, "applied")],
    )

    results = ops.install_terraform_local(api_key="k")

    assert all(r.ok for r in results)
    assert built == []
    assert applied == [({"api": "ghcr.io/x/nyxgpt-api:1", "web": "ghcr.io/x/nyxgpt-web:1"}, False)]
    recorded = install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM)
    assert recorded.mode == install_mode.INSTALL_MODE_ARTIFACT
    assert recorded.images["api"] == "ghcr.io/x/nyxgpt-api:1"


def test_dev_install_builds_from_the_working_tree(monkeypatch, checkout):
    _stub_install_steps(monkeypatch)
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    built: list[str] = []
    monkeypatch.setattr(
        ops,
        "_build_terraform_docker_images",
        lambda: built.append("built") or [ops.OpsResult(True, "built")],
    )
    monkeypatch.setattr(
        ops,
        "_pull_terraform_published_images",
        lambda: pytest.fail("dev mode must not pull published images"),
    )
    applied: list[tuple] = []
    monkeypatch.setattr(
        ops,
        "_terraform_init_plan_apply",
        lambda images, dev: applied.append((images, dev)) or [ops.OpsResult(True, "applied")],
    )

    results = ops._install_terraform_steps("k", dev=True)

    assert all(r.ok for r in results)
    assert built == ["built"]
    assert applied == [({"api": ops.TF_API_IMAGE, "web": ops.TF_WEB_IMAGE}, True)]
    assert install_mode.read_install_mode(substrate=install_mode.SUBSTRATE_TERRAFORM).is_dev is True


def test_dev_is_refused_without_a_checkout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    monkeypatch.setattr(ops, "_resolve_locality", lambda args: "local")
    monkeypatch.setattr(
        ops, "_install_terraform_steps", lambda *a, **k: pytest.fail("must not deploy")
    )

    rc = ops._install_terraform(Args(dev=True, local=True, cloud=False, api_key=None))

    assert rc == 2
    assert "--dev needs a source checkout" in capsys.readouterr().err


def test_default_install_is_the_artifact_path(monkeypatch):
    """No `--dev` attribute at all (an older caller, or the dashboard) must
    take the repo-less default, never the checkout build."""
    monkeypatch.setattr(ops, "_resolve_locality", lambda args: "local")
    seen: list[bool] = []
    monkeypatch.setattr(
        ops,
        "_install_terraform_steps",
        lambda api_key, dev=False: seen.append(dev) or [ops.OpsResult(True, "ok")],
    )
    monkeypatch.setattr(ops, "_emit_results", lambda action, results: True)

    assert ops._install_terraform(Args(local=True, cloud=False, api_key=None)) == 0
    assert seen == [False]


# --- teardown ---


def test_down_destroys_with_the_recorded_images_and_never_builds(monkeypatch):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        "/gone/checkout",
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "nyxgpt-api:local"},
    )
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_sync_local_terraform_config", lambda: [ops.OpsResult(True, "sync")])
    monkeypatch.setattr(
        ops, "_stop_observability_stack_terraform", lambda: [ops.OpsResult(True, "obs")]
    )
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    commands: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        commands.append(list(cmd))
        return _cp(stdout="destroyed")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops.down_terraform()

    assert all(r.ok for r in results)
    destroy = next(cmd for cmd in commands if "destroy" in cmd)
    assert "-var=api_image=nyxgpt-api:local" in destroy
    # A dev deployment whose checkout has since been deleted must still be
    # destroyable, so teardown never asks terraform to build anything.
    assert "-var=build_from_source=false" in destroy
    assert not any(a.startswith("-var=repo_path=") for a in destroy)


# --- reporting ---


def test_status_reports_each_deployment_s_own_mode(monkeypatch, capsys, tmp_path):
    """The defect: a Terraform deployment labelled with the native install
    mode. Both are stated, each named, and neither speaks for the other."""
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT,
        None,
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "ghcr.io/x/nyxgpt-api:1"},
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
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(ops, "_serving_status", lambda mode: {})

    ops.status(Args())
    out = capsys.readouterr().out

    assert "Install mode (native api/web):" in out
    assert "Install mode (terraform):" in out
    # The terraform deployment is artifact-mode; the native services are dev.
    tf_line = next(ln for ln in out.splitlines() if "Install mode (terraform):" in ln)
    assert "artifact" in tf_line
    assert "ghcr.io/x/nyxgpt-api:1" in tf_line
    assert "terraform api: running  [artifact]" in out


def test_infra_status_reports_the_terraform_deployment_s_install_mode(monkeypatch, tmp_path):
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_ARTIFACT,
        None,
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "ghcr.io/x/nyxgpt-api:1"},
    )
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={}, compose={}, conflicts=set(), terraform={}, terraform_conflicts=[]
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})
    monkeypatch.setattr(ops, "_which", lambda tool: "/usr/bin/docker" if tool == "docker" else None)
    monkeypatch.setattr(
        ops.self_heal, "compose_probe", lambda: ops.self_heal.ComposeProbe(True, "")
    )
    monkeypatch.setattr(ops, "_serving_status", lambda mode: {})

    status = ops.infra_status()

    assert status["install_mode"]["mode"] == "dev"  # the native services
    assert status["terraform"]["install_mode"]["mode"] == "artifact"
    assert status["terraform"]["install_mode"]["images"] == {"api": "ghcr.io/x/nyxgpt-api:1"}
    assert status["terraform"]["install_mode"]["recorded"] is True


def test_status_never_calls_an_unrecorded_running_deployment_artifact(monkeypatch, capsys):
    """A stack deployed before #3835 has containers up and no marker. It was
    built from a checkout -- so the one thing status must not print is the
    artifact default."""
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
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(ops, "_serving_status", lambda mode: {})

    ops.status(Args())
    out = capsys.readouterr().out

    tf_line = next(ln for ln in out.splitlines() if "Install mode (terraform):" in ln)
    assert "not recorded" in tf_line
    assert "artifact (" not in tf_line
    # The per-component tags agree with the line above them.
    assert "terraform api: running  [unrecorded]" in out
    assert "[artifact]" not in out.split("terraform api")[1]


def test_infra_status_reports_an_unrecorded_terraform_deployment_as_such(monkeypatch):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={}, compose={}, conflicts=set(), terraform={}, terraform_conflicts=[]
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})
    monkeypatch.setattr(ops, "_which", lambda tool: "/usr/bin/docker" if tool == "docker" else None)
    monkeypatch.setattr(
        ops.self_heal, "compose_probe", lambda: ops.self_heal.ComposeProbe(True, "")
    )
    monkeypatch.setattr(ops, "_serving_status", lambda mode: {})

    status = ops.infra_status()

    # The page keys its badge off `recorded`; the label it renders as a
    # fallback must not contradict it.
    assert status["terraform"]["deployed"] is True
    assert status["terraform"]["install_mode"]["recorded"] is False
    assert "not recorded" in status["terraform"]["install_mode"]["label"]


def test_doctor_reports_a_running_terraform_deployment_that_recorded_nothing(monkeypatch, capsys):
    """Reported, and reported as unknown -- but not as a fault.

    The stack is serving; nobody wrote down what it was built from. Raising
    that as a `doctor` issue would fail every machine that deployed before
    the marker existed, calling a healthy stack broken.
    """
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})

    issues = ops._terraform_install_mode_issues()
    out = capsys.readouterr().out

    assert issues == []
    assert "not recorded" in out
    assert "nothing recorded what these containers were built from" in out
    assert "nyxgpt up --terraform --local" in out


def test_doctor_says_nothing_about_an_undeployed_machine_with_no_marker(monkeypatch, capsys):
    """No deployment, no marker, nothing to report -- the unrecorded state is
    only a finding when something is actually running."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})

    assert ops._terraform_install_mode_issues() == []
    assert capsys.readouterr().out == ""


def test_doctor_reports_a_dev_terraform_deployment_whose_checkout_is_gone(monkeypatch, capsys):
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        "/gone/checkout",
        substrate=install_mode.SUBSTRATE_TERRAFORM,
        images={"api": "nyxgpt-api:local"},
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})

    issues = ops._terraform_install_mode_issues()

    assert any("checkout is missing" in issue for issue in issues)
