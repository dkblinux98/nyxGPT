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


# --- Linux bootstrap prerequisites (review findings on #3684) --------------
#
# `nyxgpt ops install` is not self-contained, and `sudo -u` does not carry a
# login session's environment. Each test below pins one prerequisite the
# Linux bootstrap must set up itself; without them `ops install` exits
# non-zero on a real AMI and `set -e` aborts the whole user-data run. CI's
# `ec2-linux-user-data-smoke` job proves these end to end against a fresh
# account, but only at release time -- these keep the regressions cheap to
# catch.


def _linux_executable_lines() -> list[str]:
    rendered = cloud_provision.render_user_data("linux")
    return [
        line for line in rendered.splitlines() if line.strip() and not line.strip().startswith("#")
    ]


def test_linux_bootstrap_forwards_user_session_env_across_the_sudo_boundary():
    # Without these, every `systemctl --user` call inside `ops install` has
    # no service manager to reach and each unit fails to enable/start.
    body = "\n".join(_linux_executable_lines())

    assert "XDG_RUNTIME_DIR=$TARGET_RUNTIME_DIR" in body
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=$TARGET_RUNTIME_DIR/bus" in body
    # ...and `ops install` must actually be invoked through that wrapper.
    assert 'run_as_target "$NYXGPT_CLI" ops install --skip-observability' in body


def test_linux_bootstrap_waits_for_the_user_dbus_socket_after_enabling_lingering():
    # logind creates /run/user/<uid> and its bus asynchronously, so
    # `enable-linger` returning is not proof the session is usable.
    body = "\n".join(_linux_executable_lines())

    assert 'loginctl enable-linger "$NYXGPT_TARGET_USER"' in body
    assert 'TARGET_RUNTIME_DIR="/run/user/$TARGET_UID"' in body
    assert '[ -S "$TARGET_RUNTIME_DIR/bus" ]' in body


def test_linux_bootstrap_installs_and_enables_docker_for_the_target_user():
    # `_ensure_cassandra_container` shells out to `docker` as the target
    # user; no stock AL2023/Ubuntu AMI ships an engine or that group.
    body = "\n".join(_linux_executable_lines())

    assert "dnf install -y python3 python3-pip docker" in body
    assert "apt-get install -y python3 python3-pip python3-venv docker.io" in body
    assert "systemctl enable --now docker" in body
    assert 'usermod -aG docker "$NYXGPT_TARGET_USER"' in body


def test_linux_bootstrap_installs_node_for_the_web_bundle_build():
    # `_install_native_web_systemd` runs `npm ci`/`npm run build` and fails
    # outright without npm; the distro packages are below Node 20.
    body = "\n".join(_linux_executable_lines())

    assert "nodesource.com/setup_20.x" in body
    assert '[ "$NODE_MAJOR" -lt 20 ]' in body


def test_linux_bootstrap_installs_the_cli_into_a_venv_not_pip_user():
    # Ubuntu 24.04 LTS (in the support matrix) marks its system Python PEP
    # 668 externally-managed, making `pip install --user` a hard error.
    body = "\n".join(_linux_executable_lines())

    assert "python3 -m venv" in body
    assert "pip install --user" not in body


def test_linux_bootstrap_preflights_the_target_user_environment():
    # Fail with a message naming the cause, rather than midway through
    # `ops install` with a pile of unit-start errors.
    body = "\n".join(_linux_executable_lines())

    assert "run_as_target systemctl --user show-environment" in body
    assert "run_as_target docker info" in body


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
