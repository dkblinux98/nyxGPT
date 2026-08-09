import argparse
import shutil
import subprocess

import pytest

from nyxgpt import cloud_provision

# --- render_user_data ----------------------------------------------------


def test_render_user_data_linux_pins_version():
    rendered = cloud_provision.render_user_data("linux", "3.0.0")

    assert 'NYXGPT_VERSION="3.0.0"' in rendered
    assert cloud_provision.VERSION_PLACEHOLDER not in rendered
    # Pinning happens at the script's own runtime (PIP_SPEC built from
    # $NYXGPT_VERSION), not by literal substitution -- assert the
    # conditional pinning logic itself is present.
    assert 'PIP_SPEC="nyxgpt==${NYXGPT_VERSION}"' in rendered


def test_render_user_data_linux_defaults_to_latest():
    rendered = cloud_provision.render_user_data("linux")

    assert 'NYXGPT_VERSION=""' in rendered
    assert 'PIP_SPEC="nyxgpt"' in rendered


def test_render_user_data_macos_records_version_for_reference_only():
    rendered = cloud_provision.render_user_data("macos", "3.0.0")

    assert 'NYXGPT_VERSION="3.0.0"' in rendered
    assert cloud_provision.VERSION_PLACEHOLDER not in rendered
    # The macOS path never pins a Homebrew formula to a specific version.
    assert "brew install nyxgpt-api nyxgpt-web" in rendered


def test_render_user_data_rejects_unsupported_os():
    with pytest.raises(cloud_provision.CloudCommandError, match="linux, macos"):
        cloud_provision.render_user_data("windows")


@pytest.mark.parametrize("os_family", cloud_provision.OS_FAMILIES)
def test_rendered_scripts_never_clone_the_repo(os_family):
    # The header comments *mention* "git clone" (documenting that the
    # script deliberately never does it) -- only the executable lines
    # matter here.
    rendered = cloud_provision.render_user_data(os_family)
    executable_lines = [
        line for line in rendered.splitlines() if line.strip() and not line.strip().startswith("#")
    ]

    assert not any("git clone" in line for line in executable_lines)


@pytest.mark.parametrize("os_family", cloud_provision.OS_FAMILIES)
def test_rendered_scripts_are_syntactically_valid_bash(os_family):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    rendered = cloud_provision.render_user_data(os_family)

    result = subprocess.run(
        ["bash", "-n", "/dev/stdin"],
        input=rendered,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --- support matrix --------------------------------------------------------


def test_linux_ami_support_matrix_is_nonempty():
    assert len(cloud_provision.LINUX_AMI_SUPPORT_MATRIX) >= 2
    for entry in cloud_provision.LINUX_AMI_SUPPORT_MATRIX:
        assert entry["family"]
        assert entry["package_manager"] in ("dnf", "apt")


def test_macos_ec2_support_matrix_is_nonempty():
    assert len(cloud_provision.MACOS_EC2_SUPPORT_MATRIX) >= 2
    for entry in cloud_provision.MACOS_EC2_SUPPORT_MATRIX:
        assert entry["instance_type"]
        assert entry["macos_version"]


# --- user_data() CLI entry point ------------------------------------------


def test_user_data_prints_to_stdout(capsys):
    args = argparse.Namespace(os="linux", version=None, output=None)

    exit_code = cloud_provision.user_data(args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "ec2-user-data-linux" in captured.out


def test_user_data_writes_to_output_file(tmp_path, capsys):
    output_path = tmp_path / "user-data.sh"
    args = argparse.Namespace(os="macos", version=None, output=str(output_path))

    exit_code = cloud_provision.user_data(args)

    assert exit_code == 0
    assert output_path.exists()
    assert "ec2-user-data-macos" in output_path.read_text()


def test_user_data_reports_unsupported_os(capsys):
    args = argparse.Namespace(os="windows", version=None, output=None)

    exit_code = cloud_provision.user_data(args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Unsupported --os" in captured.err
