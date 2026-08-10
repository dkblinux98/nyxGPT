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


@pytest.mark.unit
def test_resources_resolve_from_installed_non_editable_wheel(tmp_path):
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
