"""Unit tests for `nyxgpt cloud screen` (#4121).

Nothing here opens an SSH connection or touches a Mac: `subprocess` is
replaced with recorders, so these assert on the things inspection *can*
settle -- what gets rendered, what refuses, what is recorded, and the one
property the issue says may not be traded away: that no security-group port
is opened and the rendered script makes the listener loopback-only before it
starts.

The half these structurally cannot reach -- that an installed `nyxgpt`
actually puts this script on a real machine over a real SSH connection -- is
executed by `scripts/cloud-target-os-smoke.sh` phase 5 against a real sshd
(see `.github/workflows/cloud-target-os-smoke.yml`). What no CI job can
produce is a `mac*.metal` instance executing it, which is on the short D-006
exception list in docs/live-verification-ci.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from nyxgpt import cloud_deploy, cloud_infra, cloud_mac, cloud_screen
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path this module reads or writes at a temp dir."""
    home = tmp_path / "home"
    cloud_dir = home / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cloud_deploy, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", cloud_dir / "deploy.json")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_ATTEMPT_FILE", cloud_dir / "deploy-attempt.json")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_HISTORY_FILE", cloud_dir / "history.jsonl")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_STATE_FILE", cloud_dir / "tunnel.json")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_LOG_FILE", cloud_dir / "tunnel.log")
    monkeypatch.setattr(cloud_screen, "SCREEN_STATE_FILE", cloud_dir / "screen.json")
    monkeypatch.setattr(cloud_screen, "SCREEN_LOG_FILE", cloud_dir / "screen.log")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    return cloud_dir


def _args(**overrides) -> argparse.Namespace:
    base = {
        "host": None,
        "ssh_user": None,
        "identity_file": None,
        "stop": False,
        "disable": False,
        "status": False,
        "local_port": None,
        "show_password": False,
        "rotate_password": False,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _record_macos_deploy(cloud_dir, host="198.51.100.10", *, managed=True):
    """Write the state a `nyxgpt cloud deploy --os macos` run would have left."""
    (cloud_dir / "deploy.json").write_text(
        json.dumps(
            {"host": host, "os_family": "macos", "ssh_user": "ec2-user", "version": "3.0.0"}
        ),
        encoding="utf-8",
    )
    state = {"region": "us-east-1", "public_ip": host}
    if managed:
        state.update(
            {
                "mac_host_id": "h-0abc",
                "mac_instance_id": "i-0mac",
                "mac_public_ip": host,
                "mac_region": "us-east-1",
                "mac_security_group_id": "sg-0mac",
            }
        )
    (cloud_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


# --- The credential ------------------------------------------------------


def test_the_credential_lands_in_the_nyxgpt_secrets_directory():
    password, created = cloud_screen.ensure_vnc_password()
    path = cloud_screen.vnc_password_path()

    assert created is True
    assert path.parts[-3:] == (".nyxGPT", "secrets", "cloud-mac-vnc-password")
    assert path.read_text(encoding="utf-8").strip() == password
    # Same 0700/0600 shape every other ops-managed secret on this machine has.
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_the_credential_fits_apples_eight_byte_vnc_password():
    """Longer would be silently truncated, handing over a password that fails."""
    password, _ = cloud_screen.ensure_vnc_password()
    assert len(password) == cloud_screen.VNC_PASSWORD_LENGTH == 8
    assert password.isalnum()


def test_the_credential_is_stable_across_runs_and_rotates_on_request():
    first, created = cloud_screen.ensure_vnc_password()
    second, created_again = cloud_screen.ensure_vnc_password()
    assert (first, created, created_again) == (second, True, False)

    rotated = cloud_screen.rotate_vnc_password()
    assert rotated != first
    assert cloud_screen.read_vnc_password() == rotated


# --- The rendered script: loopback-only, and nothing else ----------------


def test_the_rendered_script_loads_the_loopback_rule_before_enabling_the_agent():
    """The ordering IS the guarantee -- see render_enable_script's docstring."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")

    pf_index = script.index("block drop in quick proto tcp from any to any port 5900")
    readback_index = script.index("-s rules | grep -q")
    activate_index = script.index("-activate -configure -access -on")
    assert pf_index < readback_index < activate_index
    # And the read-back is what gates the activation: a rule that did not load
    # exits before anything listens.
    gate = script[readback_index:activate_index]
    assert "exit 1" in gate
    assert "was NOT enabled" in gate


def test_the_rendered_script_binds_nothing_to_a_non_loopback_address():
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")
    assert "pass in quick on lo0 proto tcp from any to any port 5900" in script
    # The alternative DECISION_PRIVATE_ACCESS_MECHANISM.md considered and
    # rejected -- a network-restricted public bind -- must not appear anywhere.
    assert "0.0.0.0" not in script
    assert "authorize-security-group-ingress" not in script
    assert "allow-ip" not in script


def test_one_credential_serves_apples_client_and_a_third_party_one():
    """#4121's acceptance failure: a VNC-only password is one Apple's own
    Screen Sharing.app structurally cannot use, because it offers security
    types 30/33 first and both authenticate against the ACCOUNT password. The
    command's own output names `vnc://...`, which macOS hands to that client,
    so the operator was given a credential their client would always reject."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")

    # The account half, which is what makes `open vnc://...` work...
    assert 'dscl . -passwd "/Users/$TARGET_USER" "$NYXGPT_VNC_PASSWORD"' in script
    # ...verified rather than assumed, in the same step...
    assert 'dscl . -authonly "$TARGET_USER" "$NYXGPT_VNC_PASSWORD"' in script
    # ...and the VNC half, so one secret covers both clients.
    assert "-setvnclegacy -vnclegacy yes" in script
    assert "-setvncpw -vncpw" in script
    # One secret, not two: the same shell variable feeds all three.
    assert script.count("$NYXGPT_VNC_PASSWORD") == 3


def _commands_only(script: str) -> str:
    """`script` with its comment lines removed.

    An absence assertion has to be made against what the Mac will *run*: the
    rendered scripts carry long comments naming the APIs that were tried and
    rejected, so a plain `"sysadminctl" not in script` fails on the paragraph
    explaining why `sysadminctl` is not used.
    """
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def test_the_account_password_is_set_with_dscl_not_sysadminctl():
    """`sysadminctl -resetPasswordFor` is the obvious API and it fails on an
    EC2 Mac: "Operation is not permitted without secure token unlock"."""
    commands = _commands_only(cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h"))
    assert "sysadminctl" not in commands
    assert "-resetPasswordFor" not in commands
    # And not the interactive tool the hand-rolled flow used, which no wrapped
    # command can answer a prompt for.
    assert "sudo passwd" not in commands
    assert "\npasswd " not in commands


def test_the_credential_is_written_before_anything_listens():
    """A listener already running when the credential is written never loads
    it -- on the owner's host `screensharingd` started 4m21s BEFORE the VNC
    password was written, and the password was rejected for that reason."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")

    account = script.index("dscl . -passwd")
    vnc_password = script.index("-setvncpw -vncpw")
    activate = script.index("-activate -configure -access -on")
    restart = script.index("launchctl kickstart -k system/com.apple.screensharing")
    assert account < vnc_password < activate < restart


def test_the_listener_is_restarted_not_the_ard_agent():
    """`kickstart -restart -agent` cycles ARDAgent; the pid on 5900 was 7927
    before and 7927 after. `screensharingd` is the process that authenticates,
    and it belongs to the `com.apple.screensharing` launchd job."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")
    assert "launchctl kickstart -k system/com.apple.screensharing" in script
    assert "-restart -agent" not in _commands_only(script)


def test_the_script_measures_the_ordering_rather_than_asserting_it():
    """The script's own line order is not evidence about two processes: the
    listener may predate the script. So it reads the times off the machine and
    fails closed if the listener is the older of the two."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")
    assert 'CREDENTIAL_WRITTEN_AT="$(date +%s)"' in script
    assert "lsof -nP -iTCP:" in script
    assert "ps -o lstart= -p" in script
    assert '[ "$LISTENER_STARTED_AT" -lt "$CREDENTIAL_WRITTEN_AT" ]' in script
    # And it is the comparison that aborts, not a log line about it.
    verdict = script[script.index('[ "$LISTENER_STARTED_AT" -lt') :]
    assert "exit 1" in verdict.split("fi")[0]


def test_the_rendered_script_installs_nothing_on_the_mac():
    """The issue's explicit non-goal: enable what macOS ships, add nothing."""
    script = cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h")
    for installer in ("brew install", "pip install", "git clone", "curl -", "softwareupdate"):
        assert installer not in script


def test_the_rendered_script_grants_access_to_the_login_user_only():
    script = cloud_screen.render_enable_script("someone-else", "Ab3dEf7h")
    assert "TARGET_USER=someone-else" in script
    assert '-users "$TARGET_USER"' in script
    # Not `-allUsers`, which would let any account on the box take the screen.
    assert "-allUsers" not in script


def test_a_hand_edited_credential_that_cannot_work_is_refused_not_spliced():
    """The only way an unusable password reaches here is a hand-edited file."""
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.render_enable_script("ec2-user", 'a"; rm -rf /; echo "')
    assert "alphanumeric" in str(excinfo.value)


def test_the_disable_script_leaves_the_loopback_rule_in_place():
    script = cloud_screen.render_disable_script()
    assert "-deactivate -configure -access -off" in script
    assert "packet-filter rule is left in place" in script


def test_the_remote_command_elevates_non_interactively():
    assert cloud_screen.remote_command("ec2-user") == (
        "sudo -n NYXGPT_TARGET_USER=ec2-user bash -s"
    )


def test_the_credential_travels_on_stdin_never_in_an_argv(monkeypatch):
    """A password in ssh's argv is in the operator's process list and history."""
    calls: dict[str, object] = {}

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdin = _Sink()
            self.stdout = iter(["nyxgpt: done\n"])

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Sink:
        def write(self, data):
            calls["stdin"] = data

        def close(self):
            pass

    def fake_popen(argv, **_kwargs):
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr(cloud_screen.subprocess, "Popen", fake_popen)
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    cloud_screen.configure_remote_screen_sharing(target, "Ab3dEf7h")

    assert "Ab3dEf7h" not in " ".join(calls["argv"])  # type: ignore[arg-type]
    assert "Ab3dEf7h" in calls["stdin"]  # type: ignore[operator]


def test_remote_output_is_redacted_before_it_reaches_the_terminal(monkeypatch, capsys):
    """`kickstart` prints its own argv on a usage error -- password included."""

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdin = _Sink()
            self.stdout = iter(["usage: kickstart -vncpw Ab3dEf7h\n"])

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Sink:
        def write(self, data):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cloud_screen.subprocess, "Popen", lambda *a, **k: _Proc())
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    cloud_screen.configure_remote_screen_sharing(target, "Ab3dEf7h")

    printed = capsys.readouterr().out
    assert "Ab3dEf7h" not in printed
    assert "***" in printed


def test_a_failed_remote_configuration_says_nothing_is_listening(monkeypatch):
    class _Proc:
        returncode = 1

        def __init__(self):
            self.stdin = _Sink()
            self.stdout = iter(["error: the loopback-only rule for port 5900 did not load\n"])

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Sink:
        def write(self, data):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cloud_screen.subprocess, "Popen", lambda *a, **k: _Proc())
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.configure_remote_screen_sharing(target, "Ab3dEf7h")
    message = str(excinfo.value)
    assert "Nothing is listening" in message
    assert "did not load" in message


# --- The tunnel ----------------------------------------------------------


def test_the_tunnel_forwards_only_5900_and_only_to_loopback():
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    argv = cloud_screen.screen_argv(target)
    assert "-N" in argv
    assert argv[argv.index("-L") + 1] == "5901:127.0.0.1:5900"
    assert argv[-1] == "ec2-user@198.51.100.10"


def test_the_local_port_defaults_away_from_5900():
    """#4121's acceptance failure. The remote 5900 cannot move -- macOS binds
    it and the launchd job is SIP-protected -- but as a LOCAL default it is
    unusable: `vnc://localhost:5900` is the operator's own Mac's Screen
    Sharing, so Apple's client refuses with "you can't control your own
    screen" inside the client, before the forward is consulted and before
    nyxGPT's own message about the port is ever read. macOS is the operator
    platform this feature exists for, so the default was always wrong exactly
    where it is always used."""
    assert cloud_screen.SCREEN_PORT == 5900
    assert cloud_screen.DEFAULT_LOCAL_SCREEN_PORT == 5901
    assert cloud_screen.DEFAULT_LOCAL_SCREEN_PORT != cloud_screen.SCREEN_PORT
    # Every local-side default is the new one, and the printed URL follows it.
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    assert "5901:127.0.0.1:5900" in cloud_screen.screen_invocation(target)
    assert cloud_screen.screen_status()["local_port"] == 5901


def test_a_request_for_a_different_local_port_is_not_answered_with_the_open_one(monkeypatch):
    """#4121: with a path alive on 5900, `--local-port 5901` reported 5900 and
    opened nothing, which made the flag look inert. The open path does not
    satisfy a request for a different one."""
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE,
        {"pid": 4242, "host": "198.51.100.10", "user": "ec2-user", "local_port": 5900},
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda _pid: True)
    killed: list[int] = []
    monkeypatch.setattr(cloud_screen.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(cloud_screen.time, "sleep", lambda _s: None)

    class _Alive:
        pid = 5151

        def poll(self):
            return None

    opened: list[list[str]] = []

    def _popen(argv, **_kwargs):
        opened.append(argv)
        return _Alive()

    monkeypatch.setattr(cloud_screen.subprocess, "Popen", _popen)

    result = cloud_screen.start_screen_tunnel(target, 5901)

    # The old forward was closed rather than leaked, and the new one is on the
    # port that was asked for.
    assert killed == [4242]
    assert result["already_running"] is False
    assert result["replaced_local_port"] == 5900
    assert result["local_port"] == 5901
    assert result["url"] == "vnc://localhost:5901"
    assert "5901:127.0.0.1:5900" in " ".join(opened[0])


def test_an_open_path_on_the_requested_port_is_reported_not_duplicated(monkeypatch):
    """The other half: asking for the port that is already open is a no-op."""
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE,
        {"pid": 4242, "host": "198.51.100.10", "user": "ec2-user", "local_port": 5901},
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(
        cloud_screen.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("a second ssh forward was spawned for an open path"),
    )

    result = cloud_screen.start_screen_tunnel(target, 5901)
    assert result["already_running"] is True
    assert result["pid"] == 4242


def test_a_local_port_collision_names_the_flag_that_moves_it(monkeypatch):
    target = cloud_deploy.DeployTarget(host="198.51.100.10", user="ec2-user")

    class _Dead:
        pid = 4242

        def poll(self):
            return 255

    monkeypatch.setattr(cloud_screen.subprocess, "Popen", lambda *a, **k: _Dead())
    monkeypatch.setattr(cloud_screen.time, "sleep", lambda _s: None)
    cloud_screen.SCREEN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.start_screen_tunnel(target)
    assert "--local-port" in str(excinfo.value)


def test_a_dead_tunnel_keeps_the_configured_half_of_the_record():
    """A reboot takes the tunnel; it does not turn Screen Sharing off."""
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE,
        {"pid": 999999999, "host": "198.51.100.10", "configured": True, "configured_at": "T"},
    )
    status = cloud_screen.screen_status()
    assert status["running"] is False
    assert status["configured"] is True
    assert status["configured_at"] == "T"
    # And it was healed in place rather than deleted.
    assert cloud_screen.screen_state()["pid"] == 0


def test_stopping_the_tunnel_does_not_claim_screen_sharing_was_disabled(monkeypatch):
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE,
        {"pid": 4242, "host": "198.51.100.10", "configured": True, "configured_at": "T"},
    )
    monkeypatch.setattr(cloud_screen, "screen_status", lambda: {"running": True, "pid": 4242})
    killed: list[int] = []
    monkeypatch.setattr(cloud_screen.os, "kill", lambda pid, _sig: killed.append(pid))

    result = cloud_screen.stop_screen_tunnel()
    assert result == {"action": "screen-stop", "stopped": True, "pid": 4242}
    assert killed == [4242]
    assert cloud_screen.screen_state()["configured"] is True


# --- Scoping: the two refusals #4121 requires ----------------------------


def test_it_refuses_a_non_macos_target(_isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "os_family": "linux"}), encoding="utf-8"
    )
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.resolve_screen_target(_args())
    message = str(excinfo.value)
    assert "macOS capability" in message
    assert "linux" in message


def test_it_refuses_a_deploy_that_predates_the_os_record(_isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10"}), encoding="utf-8"
    )
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.resolve_screen_target(_args())
    assert "not recorded" in str(excinfo.value)


def test_it_refuses_a_mac_nyxgpt_did_not_configure(_isolated_cloud_home):
    """A `--host` Mac's security group is not nyxGPT's, so 5900 is unknowable."""
    _record_macos_deploy(_isolated_cloud_home, managed=False)
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.resolve_screen_target(_args())
    message = str(excinfo.value)
    assert "did not configure the Mac" in message
    assert "loopback" in message
    assert "nyxgpt cloud allow-ip" in message


def test_it_refuses_a_different_mac_than_the_one_it_manages(_isolated_cloud_home):
    _record_macos_deploy(_isolated_cloud_home, host="198.51.100.10")
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.resolve_screen_target(_args(host="203.0.113.7"))
    assert "198.51.100.10" in str(excinfo.value)


def test_it_refuses_when_no_deploy_is_recorded_at_all():
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_screen.resolve_screen_target(_args())
    assert "No deploy is recorded" in str(excinfo.value)


def test_it_accepts_the_mac_nyxgpt_manages(_isolated_cloud_home):
    _record_macos_deploy(_isolated_cloud_home)
    target = cloud_screen.resolve_screen_target(_args())
    assert target.host == "198.51.100.10"
    assert target.user == "ec2-user"


def test_it_finds_the_mac_without_the_linux_substrates_public_ip(_isolated_cloud_home):
    """A macOS deploy never applies the Linux substrate, so `state.json` has no
    `public_ip` -- only `mac_public_ip`. Resolving through
    `cloud_deploy.resolve_target` would therefore refuse a Mac nyxGPT really
    does manage, with "No provisioned instance found"."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "os_family": "macos", "ssh_user": "ec2-user"}),
        encoding="utf-8",
    )
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps(
            {
                "mac_host_id": "h-0abc",
                "mac_instance_id": "i-0mac",
                "mac_public_ip": "198.51.100.10",
                "mac_region": "us-east-1",
                "mac_security_group_id": "sg-0mac",
            }
        ),
        encoding="utf-8",
    )
    target = cloud_screen.resolve_screen_target(_args())
    assert (target.host, target.region, target.instance_id) == (
        "198.51.100.10",
        "us-east-1",
        "i-0mac",
    )


def test_the_identity_file_the_deploy_used_is_reused(_isolated_cloud_home):
    _record_macos_deploy(_isolated_cloud_home)
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps(
            {
                "host": "198.51.100.10",
                "os_family": "macos",
                "ssh_user": "mac-admin",
                "identity_file": "~/.ssh/mac_key",
            }
        ),
        encoding="utf-8",
    )
    target = cloud_screen.resolve_screen_target(_args())
    assert target.user == "mac-admin"
    assert target.identity_file.endswith("/.ssh/mac_key")
    assert "~" not in target.identity_file


# --- The command ---------------------------------------------------------


def test_the_command_opens_the_path_and_never_echoes_the_credential(
    _isolated_cloud_home, monkeypatch, capsys
):
    _record_macos_deploy(_isolated_cloud_home)
    monkeypatch.setattr(
        cloud_screen, "configure_remote_screen_sharing", lambda *_a: {"configured": True}
    )
    monkeypatch.setattr(
        cloud_screen,
        "start_screen_tunnel",
        lambda *_a, **_k: {"action": "screen", "already_running": False},
    )
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE, {"pid": 4242, "host": "198.51.100.10", "local_port": 5900}
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda _pid: True)

    assert cloud_screen.screen_command(_args()) == 0
    out = capsys.readouterr().out
    password = cloud_screen.read_vnc_password()
    assert password
    assert password not in out
    assert "vnc://localhost:5900" in out
    assert str(cloud_screen.vnc_password_path()) in out
    assert "--show-password" in out


def test_show_password_is_the_only_way_the_credential_is_printed(
    _isolated_cloud_home, monkeypatch, capsys
):
    _record_macos_deploy(_isolated_cloud_home)
    cloud_screen.ensure_vnc_password()
    assert cloud_screen.screen_command(_args(status=True, show_password=True)) == 0
    assert cloud_screen.read_vnc_password() in capsys.readouterr().out


def test_the_json_status_never_carries_the_credential(_isolated_cloud_home, capsys):
    _record_macos_deploy(_isolated_cloud_home)
    password, _ = cloud_screen.ensure_vnc_password()
    assert cloud_screen.screen_command(_args(status=True, json=True, show_password=True)) == 0
    payload = capsys.readouterr().out
    assert password not in payload
    assert str(cloud_screen.vnc_password_path()) in json.loads(payload)["password_file"]


def test_disable_closes_the_path_and_turns_the_listener_off(
    _isolated_cloud_home, monkeypatch, capsys
):
    _record_macos_deploy(_isolated_cloud_home)
    cloud_screen.record_configured(cloud_deploy.DeployTarget(host="198.51.100.10"))
    disabled: list[str] = []
    monkeypatch.setattr(
        cloud_screen,
        "disable_remote_screen_sharing",
        lambda target: disabled.append(target.host) or {"configured": False},
    )

    assert cloud_screen.screen_command(_args(disable=True)) == 0
    assert disabled == ["198.51.100.10"]
    assert cloud_screen.screen_state()["configured"] is False
    assert "Screen Sharing is off" in capsys.readouterr().out


def test_stop_does_not_claim_anything_about_a_mac_it_never_configured(capsys):
    """D-018's rule: a surface says nothing it has not got a source for."""
    assert cloud_screen.screen_command(_args(stop=True)) == 0
    out = capsys.readouterr().out
    assert "No screen path is open." in out
    assert "still enabled on the Mac" not in out


def test_stop_names_the_disable_command_when_it_did_configure_one(capsys):
    cloud_screen.record_configured(cloud_deploy.DeployTarget(host="198.51.100.10"))
    assert cloud_screen.screen_command(_args(stop=True)) == 0
    out = capsys.readouterr().out
    assert "still enabled on the Mac" in out
    assert "nyxgpt cloud screen --disable" in out


def test_rotating_the_credential_says_so_and_sets_the_new_one(
    _isolated_cloud_home, monkeypatch, capsys
):
    _record_macos_deploy(_isolated_cloud_home)
    first, _ = cloud_screen.ensure_vnc_password()
    delivered: list[str] = []
    monkeypatch.setattr(
        cloud_screen,
        "configure_remote_screen_sharing",
        lambda _t, password: delivered.append(password) or {"configured": True},
    )
    monkeypatch.setattr(
        cloud_screen, "start_screen_tunnel", lambda *_a, **_k: {"already_running": False}
    )

    assert cloud_screen.screen_command(_args(rotate_password=True)) == 0
    out = capsys.readouterr().out
    assert "Rotated the VNC credential" in out
    assert delivered and delivered[0] != first
    assert delivered[0] == cloud_screen.read_vnc_password()
    assert delivered[0] not in out


def test_a_refusal_exits_non_zero_with_the_reason_on_stderr(capsys):
    assert cloud_screen.screen_command(_args()) == 1
    assert "No deploy is recorded" in capsys.readouterr().err


# --- What `nyxgpt cloud status` reports ----------------------------------


def test_cloud_status_reports_the_screen_path(_isolated_cloud_home, monkeypatch):
    _record_macos_deploy(_isolated_cloud_home)
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE,
        {"pid": 4242, "host": "198.51.100.10", "local_port": 5900, "configured": True},
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda _pid: True)

    status = cloud_deploy.deploy_status()
    assert status["screen"]["running"] is True
    assert status["screen"]["url"] == "vnc://localhost:5900"
    assert status["commands"]["screen"] == "nyxgpt cloud screen"
    assert status["commands"]["screen_stop"] == "nyxgpt cloud screen --stop"


def test_the_status_summary_names_the_screen_row_only_for_a_mac(
    _isolated_cloud_home, monkeypatch, capsys
):
    """A Linux box has no screen, so a row claiming one is closed is noise."""
    _record_macos_deploy(_isolated_cloud_home)
    monkeypatch.setattr(cloud_mac, "pending_release", lambda: {})
    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
    assert "Screen path" in capsys.readouterr().out

    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "os_family": "linux"}), encoding="utf-8"
    )
    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
    assert "Screen path" not in capsys.readouterr().out


def test_the_summary_distinguishes_configured_but_closed_from_never_set_up():
    commands = cloud_deploy.LIFECYCLE_COMMANDS
    never = cloud_deploy._screen_label({"running": False, "configured": False}, commands)
    closed = cloud_deploy._screen_label({"running": False, "configured": True}, commands)
    assert "not set up" in never
    assert "no tunnel is open" in closed
    assert never != closed


def test_destroy_closes_the_screen_path_and_forgets_the_record(_isolated_cloud_home, monkeypatch):
    """An `ssh -N` left pointed at a terminated instance never exits."""
    _record_macos_deploy(_isolated_cloud_home)
    cloud_deploy._write_json(
        cloud_screen.SCREEN_STATE_FILE, {"pid": 4242, "host": "198.51.100.10", "configured": True}
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": False})
    monkeypatch.setattr(cloud_screen, "screen_status", lambda: {"running": True, "pid": 4242})
    killed: list[int] = []
    monkeypatch.setattr(cloud_screen.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(cloud_mac, "load_mac_record", lambda: {})
    monkeypatch.setattr(cloud_mac, "mac_state_exists", lambda: False)
    monkeypatch.setattr(
        cloud_infra, "destroy_infra", lambda _args: {"settings": {"aws_region": "us-east-1"}}
    )

    result = cloud_deploy.destroy(_args(yes=True))
    assert killed == [4242]
    assert result["screen"]["stopped"] is True
    assert not cloud_screen.SCREEN_STATE_FILE.exists()


# --- The rendered script is valid shell ----------------------------------


@pytest.mark.parametrize(
    "script",
    [
        cloud_screen.render_enable_script("ec2-user", "Ab3dEf7h"),
        cloud_screen.render_disable_script(),
    ],
    ids=["enable", "disable"],
)
def test_the_rendered_scripts_parse_as_bash(script, tmp_path):
    """Cheap, and it catches the one class of defect inspection cannot: a
    script that is delivered to a Mac and dies on a syntax error, after the
    operator has already paid for the hardware."""
    path = tmp_path / "rendered.sh"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
