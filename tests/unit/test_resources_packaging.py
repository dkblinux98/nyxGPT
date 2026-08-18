"""#3621: nyxgpt.resources must resolve from an installed, non-editable
build with no repo checkout present -- not just the editable dev tree.

Builds a real wheel from the current source tree, installs it (--no-deps,
to keep this fast -- only resource resolution is under test) into a fresh,
isolated venv, then runs a subprocess against that venv from a working
directory outside the repo entirely, so there is no way it could
accidentally fall back to reading the checkout instead of the installed
package.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytest.importorskip("build", reason="`build` (dev dependency) not installed")

REPO_ROOT = Path(__file__).resolve().parents[2]

_RESOLUTION_SCRIPT = """
import importlib.resources
from pathlib import Path

root = importlib.resources.files("nyxgpt.resources")

compose = root.joinpath("docker-compose.yml")
assert compose.is_file(), f"missing {compose}"
assert "services:" in compose.read_text(encoding="utf-8")

env_example = root.joinpath(".env.example")
assert env_example.is_file(), f"missing {env_example}"

datasource = root.joinpath("docker/grafana/provisioning/datasources/datasource.yml")
assert datasource.is_file(), f"missing {datasource}"

plist = root.joinpath("ops/launchagents/com.nyxgpt.cassandra-logs.plist")
assert plist.is_file(), f"missing {plist}"

unit = root.joinpath("ops/systemd/nyxgpt-api.service")
assert unit.is_file(), f"missing {unit}"

script = root.joinpath("scripts/run-web.sh")
assert script.is_file(), f"missing {script}"

example_config = root.joinpath("example.config.ini")
assert example_config.is_file(), f"missing {example_config}"
assert "[nyxgpt]" in example_config.read_text(encoding="utf-8")

# #3745: the product documentation ships in the wheel -- it is the only
# documentation a PyPI/Homebrew user has, rendered by the web UI under
# Support -> Docs. Checked through importlib.resources rather than by
# importing nyxgpt.support, because this venv installs with --no-deps and
# that module needs markdown/bs4 at import time; what matters here is that
# the *files* are in the artifact.
docs = root.joinpath("docs")
assert docs.is_dir(), f"missing {docs}"
packaged_docs = {p.name for p in docs.iterdir() if p.name.endswith(".md")}
assert "README.md" in packaged_docs, f"docs index missing from wheel: {packaged_docs}"
assert len(packaged_docs) > 10, f"docs tree looks truncated in the wheel: {packaged_docs}"
assert "configuration.md" in packaged_docs, f"docs tree incomplete in wheel: {packaged_docs}"

# #3809: and *only* the product documentation. The agent/CI process and
# contributor docs stay in the repository; a wheel that carries them is
# shipping this project's internal operating detail to end users. Asserted
# against the built artifact, not the source tree, because the whole point
# is what setuptools put in the wheel.
not_product = {
    "KNOWN_LIMITATIONS.md",
    "acceptance-drain-gate.md",
    "adding-api-endpoints.md",
    "agent-comment-tokens.md",
    "agent-smoke.md",
    "cloud-artifact-smoke.md",
    "development.md",
    "file-lock-audit.md",
    "github-tokens.md",
    "how-this-project-is-run.md",
    "live-verification-ci.md",
    "portability-matrix.md",
    "security-scanning-ci.md",
    "sprint-autopilot.md",
    "testing.md",
}
leaked_docs = sorted(packaged_docs & not_product)
assert not leaked_docs, f"process/contributor docs leaked into the wheel: {leaked_docs}"

# #3509: `nyxgpt cloud infra` provisions the AWS substrate from the packaged
# terraform/aws configuration (nyxgpt.cloud_infra.packaged_terraform_dir), so
# a checkout-free machine must find the whole module tree here.
for tf in (
    "terraform/aws/main.tf",
    "terraform/aws/variables.tf",
    "terraform/aws/outputs.tf",
    "terraform/aws/versions.tf",
    "terraform/aws/modules/network/main.tf",
    "terraform/aws/modules/security/main.tf",
    "terraform/aws/modules/compute/main.tf",
    "terraform/aws/tests/plan.tftest.hcl",
):
    assert root.joinpath(tf).is_file(), f"missing {tf}"

# ...and must NOT find a developer's local Terraform working files: state and
# tfvars carry infrastructure ids and the .terraform plugin cache is hundreds
# of megabytes. They are gitignored and excluded in pyproject.toml, but a
# wheel built from a tree where someone ran Terraform in-place would otherwise
# quietly embed them.
tf_dir = root.joinpath("terraform/aws")
leaked = [
    str(p)
    for p in Path(str(tf_dir)).rglob("*")
    if p.name == ".terraform" or p.suffix == ".tfvars" or ".tfstate" in p.name
]
assert not leaked, f"local Terraform working files leaked into the wheel: {leaked}"

# #3834: `nyxgpt ops install --kubernetes --local` applies these manifests
# (synced to ~/.nyxGPT/k8s) and builds the api image from this Dockerfile.
# They were the last runtime data the Kubernetes path read from REPO_ROOT,
# which is why that mode could not run on a machine with no checkout at all.
for k8s_file in (
    "k8s/kustomization.yaml",
    "k8s/namespace.yaml",
    "k8s/secret.example.yaml",
    "k8s/deployment-stable.yaml",
    "k8s/deployment-web-stable.yaml",
    "k8s/statefulset-cassandra.yaml",
    "k8s/statefulset-ollama.yaml",
    "k8s/observability/kustomization.yaml",
    "docker/nyxgpt-api.Dockerfile",
):
    assert root.joinpath(k8s_file).is_file(), f"missing {k8s_file}"

# ...and never either of the secrets the Kubernetes path generates:
# `_ensure_k8s_secret` writes k8s/secret.yaml (the real `[auth] api_key`) and
# `_ensure_k8s_observability_secret` writes k8s/observability/secret.yaml (the
# Grafana admin password, Slack webhook and GlitchTip DSN), both next to the
# kustomization they belong to. A wheel built from a tree where someone ran
# either command in-place must not carry them.
for generated_secret in ("k8s/secret.yaml", "k8s/observability/secret.yaml"):
    assert not root.joinpath(
        generated_secret
    ).is_file(), f"a generated k8s secret leaked into the wheel: {generated_secret}"

# #3622: a bare `pip install nyxgpt` (no Homebrew/systemd installer step)
# must still resolve example.config.ini -- config_wizard.WIZARD_SCHEMA
# (which `nyxgpt.app` builds at import time) derives from this file, and
# with no repo checkout and no installer-placed package-adjacent copy, the
# packaged nyxgpt.resources copy is the only place left to find it.
from nyxgpt import config_wizard

resolved = config_wizard._resolve_example_config_path()
assert resolved.is_file(), f"config_wizard could not resolve {resolved}"
assert config_wizard.WIZARD_SCHEMA, "WIZARD_SCHEMA built empty from the packaged resource"

print("RESOURCE_RESOLUTION_OK")
"""


@pytest.fixture
def planted_generated_secrets():
    """Put the k8s secrets an install generates into the tree being built.

    Without this the "no secret reached the wheel" assertions in the script
    above are vacuous everywhere they actually run: CI builds from a fresh
    checkout that has never run `nyxgpt ops install --kubernetes`, so the
    files the `exclude-package-data` entries exist for are not there to leak
    in the first place. Planting them is the fault-injection half (#3753) --
    drop either exclusion from pyproject.toml and this test fails.

    A developer's real generated secret is never touched or deleted: only
    files this fixture created are removed -- and they are removed from
    setuptools' `build/lib` staging tree as well as from the source tree.
    That second half is not tidiness: `build/lib` is not cleaned between
    builds, so a file that reached it once is copied into every later wheel
    built from this checkout even after the source file is gone. Leaving one
    behind would poison the developer's next `python -m build` with a secret
    this test invented.
    """
    planted = []
    for relative in ("k8s/secret.yaml", "k8s/observability/secret.yaml"):
        path = REPO_ROOT / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# planted by test_resources_packaging.py -- must not reach a wheel\n"
            'stringData:\n  api-key: "planted-secret"\n',
            encoding="utf-8",
        )
        planted.append(relative)
    try:
        yield
    finally:
        for relative in planted:
            (REPO_ROOT / relative).unlink(missing_ok=True)
            (REPO_ROOT / "build" / "lib" / "nyxgpt" / "resources" / relative).unlink(
                missing_ok=True
            )


@pytest.mark.unit
def test_resources_resolve_from_installed_non_editable_wheel(tmp_path, planted_generated_secrets):
    dist_dir = tmp_path / "dist"
    build_cp = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (
        build_cp.returncode == 0
    ), f"wheel build failed:\nstdout:\n{build_cp.stdout}\nstderr:\n{build_cp.stderr}"

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"

    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    venv_python = env_dir / "bin" / "python"

    install_cp = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", "--quiet", str(wheels[0])],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (
        install_cp.returncode == 0
    ), f"wheel install failed:\nstdout:\n{install_cp.stdout}\nstderr:\n{install_cp.stderr}"

    # Run from a directory outside the repo entirely -- verifies resolution
    # comes from the installed package, not an accidental fallback to files
    # still sitting in the checkout at a predictable relative path.
    outside_cwd = tmp_path / "no-repo-here"
    outside_cwd.mkdir()
    run_cp = subprocess.run(
        [str(venv_python), "-c", _RESOLUTION_SCRIPT],
        capture_output=True,
        text=True,
        cwd=str(outside_cwd),
        timeout=30,
    )
    assert (
        run_cp.returncode == 0
    ), f"resource resolution failed:\nstdout:\n{run_cp.stdout}\nstderr:\n{run_cp.stderr}"
    assert "RESOURCE_RESOLUTION_OK" in run_cp.stdout
