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

    assert '"$NYXGPT_PYTHON" -m venv "$CLI_VENV"' in body
    assert "pip install --user" not in body


def test_linux_bootstrap_puts_nyxgpt_on_the_login_users_path():
    """#3993: `nyxgpt` has to be runnable by an operator who SSHes in.

    `run_as_target` sets PATH for the bootstrap's own invocations only, so
    without this an operator diagnosing a failed provision got `nyxgpt:
    command not found` from every wrapped command the docs and the dashboard
    name -- while the binary sat in $CLI_VENV/bin, which nothing told them
    about.
    """
    body = "\n".join(_linux_executable_lines())

    assert "/etc/profile.d/nyxgpt.sh" in body
    assert 'PATH="$HOME/.nyxGPT/opt/nyxgpt-cli/bin:$PATH"' in body
    # Quoted delimiter: $HOME must be expanded by the login shell that sources
    # the file, not by this bootstrap run.
    assert "<<'PROFILE_EOF'" in body


def test_linux_bootstrap_builds_the_cli_venv_on_a_python_that_meets_the_floor():
    # Amazon Linux 2023's `python3` is 3.9 and nyxGPT's requires-python is
    # ">=3.11", so a venv built from the distro default is one pip refuses to
    # install nyxGPT into (#3782). Install an explicit 3.11+, then resolve by
    # asking each candidate its own version.
    body = "\n".join(_linux_executable_lines())

    assert "for pkg in python3.13 python3.12 python3.11; do" in body
    assert "for cand in python3.13 python3.12 python3.11 python3; do" in body
    assert "sys.version_info >= (3, 11)" in body
    # Bare `python3 -m venv` is exactly the defect; it must not come back.
    assert "python3 -m venv" not in body


def test_linux_bootstrap_fails_loudly_when_no_python_meets_the_floor():
    # Criterion: a clear failure naming found/required versions, not an
    # opaque pip error minutes later inside `ops install`.
    body = "\n".join(_linux_executable_lines())

    assert 'if [ -z "$NYXGPT_PYTHON" ]; then' in body
    assert "no Python >= 3.11" in body
    assert "requires-python is '>=3.11'" in body


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


# --- Session storage backend (#3865) ------------------------------------


def test_linux_defaults_to_the_cassandra_session_backend():
    """A Linux instance must not silently come up on host-local JSON sessions.

    `ops install` provisions the `nyxgpt-cassandra` container on the instance
    as a core service, so the backend the Kubernetes overlay asserts
    declaratively is available here too -- and the cross-mode session
    guarantee only holds if the cloud path actually selects it.
    """
    rendered = cloud_provision.render_user_data("linux")

    assert 'NYXGPT_SESSION_BACKEND_CHOICE="cassandra"' in rendered
    assert cloud_provision.SESSION_BACKEND_PLACEHOLDER not in rendered


def test_macos_defaults_to_file_because_nothing_provisions_a_cassandra():
    """The EC2 Mac template runs brew only -- it never runs `ops install`.

    Defaulting that instance to `cassandra` would point its API at a database
    that is not on the machine. File-backed by default is the honest answer,
    and it is what docs/session-storage.md documents.
    """
    rendered = cloud_provision.render_user_data("macos")

    assert 'NYXGPT_SESSION_BACKEND_CHOICE="file"' in rendered


def test_the_backend_is_selectable_on_either_target():
    assert 'NYXGPT_SESSION_BACKEND_CHOICE="file"' in cloud_provision.render_user_data(
        "linux", session_backend="file"
    )
    assert 'NYXGPT_SESSION_BACKEND_CHOICE="cassandra"' in cloud_provision.render_user_data(
        "macos", session_backend="cassandra"
    )


def test_both_templates_apply_the_choice_with_the_wrapped_command():
    """Never a hand-rolled sed/python edit of config.ini on the instance.

    The operational command wrapping requirement is what makes `nyxgpt ops
    session-backend` the mechanism: it is the same command an operator runs,
    and it is idempotent, so a re-provision is a no-op.
    """
    for os_family in cloud_provision.OS_FAMILIES:
        rendered = cloud_provision.render_user_data(os_family)
        assert "ops session-backend" in rendered


def test_the_linux_choice_is_applied_before_ops_install():
    """Order matters: `ops install` derives the containerized config from this one.

    `_generate_compose_config` copies the native config.ini verbatim, so a
    backend set *after* the install would leave the derived
    `config.docker.ini` on the old value until the next reconcile.
    """
    body = "\n".join(_linux_executable_lines())
    # The invocations, not the mentions: several `echo` diagnostics name
    # `nyxgpt ops install` too.
    set_backend = 'run_as_target "$NYXGPT_CLI" ops session-backend'
    install = 'run_as_target "$NYXGPT_CLI" ops install'

    assert body.index(set_backend) < body.index(install)


def test_an_unsupported_backend_is_refused_at_render_time():
    with pytest.raises(cloud_provision.CloudCommandError) as excinfo:
        cloud_provision.render_user_data("linux", session_backend="postgres")

    assert "--session-backend" in str(excinfo.value)


def test_user_data_passes_the_flag_through(capsys):
    args = argparse.Namespace(os="linux", version=None, output=None, session_backend="file")

    exit_code = cloud_provision.user_data(args)

    assert exit_code == 0
    assert 'NYXGPT_SESSION_BACKEND_CHOICE="file"' in capsys.readouterr().out
