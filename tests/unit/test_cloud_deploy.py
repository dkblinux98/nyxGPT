"""Unit tests for `nyxgpt cloud deploy` / `destroy` / `tunnel` (P6-11, #3513).

Nothing here talks to AWS, opens an SSH connection, or runs Terraform:
`cloud_infra.apply_infra`/`destroy_infra` and every `subprocess` call are
replaced with recorders, so the tests assert on the orchestration -- the
order of steps, the command lines built, what gets recorded, and the
repo-less guarantee on the provisioning script.
"""

import argparse
import json
import subprocess

import pytest

from nyxgpt import cloud_deploy, cloud_infra
from nyxgpt.cloud import CloudCommandError


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the module reads or writes at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_deploy, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", cloud_dir / "deploy.json")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_STATE_FILE", cloud_dir / "tunnel.json")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_LOG_FILE", cloud_dir / "tunnel.log")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    return cloud_dir


def _write_cloud_state(cloud_dir, **overrides):
    """Write the substrate handoff `cloud infra apply` would have written."""
    state = {
        "region": "us-east-1",
        "instance_id": "i-0abc",
        "public_ip": "198.51.100.10",
        "security_group_id": "sg-0abc",
    }
    state.update(overrides)
    (cloud_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def _args(**overrides) -> argparse.Namespace:
    base = {
        "host": None,
        "ssh_user": None,
        "identity_file": None,
        "version": "3.0.0",
        "skip_observability": False,
        "no_tunnel": False,
        "health_timeout": None,
        "ssh_timeout": None,
        "status": False,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- Target and plan resolution ------------------------------------------


def test_resolve_target_reads_the_substrate_handoff(_isolated_cloud_home):
    _write_cloud_state(_isolated_cloud_home)
    target = cloud_deploy.resolve_target(_args())
    assert target.host == "198.51.100.10"
    assert target.user == "ec2-user"
    assert target.instance_id == "i-0abc"
    assert target.region == "us-east-1"


def test_resolve_target_without_a_provisioned_instance_explains_the_next_step():
    with pytest.raises(CloudCommandError, match="nyxgpt cloud deploy"):
        cloud_deploy.resolve_target(_args())


def test_resolve_target_honours_an_explicit_host_and_identity(_isolated_cloud_home, tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("x", encoding="utf-8")
    target = cloud_deploy.resolve_target(
        _args(host="203.0.113.9", ssh_user="ubuntu", identity_file=str(key))
    )
    assert target.host == "203.0.113.9"
    assert target.user == "ubuntu"
    assert target.identity_file == str(key)


def test_resolve_plan_defaults_to_every_observability_profile():
    plan = cloud_deploy.resolve_plan(_args())
    assert plan.profiles == ["monitoring", "logging", "tracing", "errors"]
    assert plan.open_tunnel is True


def test_resolve_plan_skip_observability_deploys_the_core_only():
    plan = cloud_deploy.resolve_plan(_args(skip_observability=True))
    assert plan.profiles == []


def test_resolve_plan_falls_back_to_the_previous_deploys_version(_isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "2.9.1"}), encoding="utf-8"
    )
    plan = cloud_deploy.resolve_plan(_args(version=None))
    assert plan.version == "2.9.1"


def test_resolve_plan_without_any_resolvable_version_is_an_error(monkeypatch):
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "")
    with pytest.raises(CloudCommandError, match="published artifact"):
        cloud_deploy.resolve_plan(_args(version=None))


@pytest.mark.parametrize("bad", ['3.0.0"; rm -rf /', "$(id)", "-3.0.0", ""])
def test_resolve_plan_rejects_a_version_that_would_not_survive_the_remote_shell(bad, monkeypatch):
    """The version is spliced into the provisioning script; catch it locally.

    Without this the operator's first sign of trouble is a confusing shell
    error ten minutes into a remote run.
    """
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "")
    with pytest.raises(CloudCommandError):
        cloud_deploy.resolve_plan(_args(version=bad))


def test_resolve_plan_restores_the_identity_file_the_last_deploy_used(_isolated_cloud_home):
    """A re-run shouldn't have to repeat --identity-file, same as --version."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "identity_file": "/keys/owner"}), encoding="utf-8"
    )
    plan = cloud_deploy.resolve_plan(_args(version=None, identity_file=None))
    assert plan.identity_file == "/keys/owner"


# --- The provisioning script (repo-less requirement) ----------------------


def test_provision_script_never_clones_the_repository():
    """CLAUDE.md, 2026-08-01: a target machine must never get a checkout."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    lowered = script.lower()
    assert "git clone" not in lowered
    assert "git://" not in lowered
    assert ".git" not in lowered
    assert "curl" in lowered  # it does fetch things -- just never source control


def test_provision_script_installs_the_pinned_published_release():
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(version="3.1.2")))
    assert 'NYXGPT_VERSION="3.1.2"' in script
    assert 'pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"' in script


def test_provision_script_skips_observability_when_asked():
    with_obs = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    without = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(skip_observability=True))
    )
    assert 'NYXGPT_PROFILES="monitoring,logging,tracing,errors"' in with_obs
    assert "ops observability" in with_obs
    assert 'NYXGPT_PROFILES=""' in without


def test_provision_instance_raises_with_the_remote_diagnostic(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="dnf: no such package")

    monkeypatch.setattr(cloud_deploy.subprocess, "run", fake_run)
    target = cloud_deploy.DeployTarget(host="198.51.100.10")
    with pytest.raises(CloudCommandError, match="no such package"):
        cloud_deploy.provision_instance(target, cloud_deploy.resolve_plan(_args()))


# --- SSH command construction --------------------------------------------


def test_ssh_argv_uses_batch_mode_and_the_given_identity():
    target = cloud_deploy.DeployTarget(host="198.51.100.10", identity_file="/keys/id")
    argv = cloud_deploy.ssh_argv(target)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[-1] == "ec2-user@198.51.100.10"
    assert "-i" in argv and "/keys/id" in argv
    assert "IdentitiesOnly=yes" in argv


def test_wait_for_ssh_returns_once_the_instance_answers(monkeypatch):
    attempts = {"n": 0}

    def fake_remote(target, command, **kwargs):
        attempts["n"] += 1
        code = 0 if attempts["n"] >= 3 else 255
        return subprocess.CompletedProcess(["ssh"], code, stdout="", stderr="refused")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    cloud_deploy.wait_for_ssh(cloud_deploy.DeployTarget(host="h"), timeout=60, interval=0)
    assert attempts["n"] == 3


def test_wait_for_ssh_times_out_with_the_allow_ip_hint(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="timeout"),
    )
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 1.0, 2.0, 100.0, 100.0])
    monkeypatch.setattr(cloud_deploy.time, "monotonic", lambda: next(clock))
    with pytest.raises(CloudCommandError, match="nyxgpt cloud allow-ip"):
        cloud_deploy.wait_for_ssh(cloud_deploy.DeployTarget(host="h"), timeout=10, interval=0)


# --- The tunnel (the P6-4 access path) -----------------------------------


def test_tunnel_ports_cover_the_core_plus_enabled_observability_uis():
    assert cloud_deploy.tunnel_ports([]) == [("api", 8000), ("web", 3000)]
    full = dict(cloud_deploy.tunnel_ports(["monitoring", "logging", "tracing", "errors"]))
    assert full == {
        "api": 8000,
        "web": 3000,
        "grafana": 3001,
        "prometheus": 9090,
        "jaeger": 16686,
        "glitchtip": 8080,
    }


def test_tunnel_argv_forwards_every_port_to_instance_loopback():
    target = cloud_deploy.DeployTarget(host="198.51.100.10")
    argv = cloud_deploy.tunnel_argv(target, ["monitoring"])
    assert "-N" in argv
    forwards = [argv[i + 1] for i, part in enumerate(argv) if part == "-L"]
    assert forwards == [
        "8000:127.0.0.1:8000",
        "3000:127.0.0.1:3000",
        "3001:127.0.0.1:3001",
        "9090:127.0.0.1:9090",
    ]


def test_tunnel_urls_are_localhost_only():
    """There is no instance-facing URL by design -- everything is a local forward."""
    urls = cloud_deploy.tunnel_urls(["tracing"])
    assert all(url.startswith("http://localhost:") for url in urls.values())
    assert urls["jaeger"] == "http://localhost:16686"


class _FakeProcess:
    """Stand-in for the backgrounded `ssh -N` child."""

    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


def _fake_popen(*, alive=True, stderr_text=""):
    """Popen replacement that writes ssh's diagnostics into the log it is handed.

    The real child is detached and outlives its parent, so the module hands it
    a log file rather than a pipe; the failure path reads that file back.
    """

    def popen(_argv, **kwargs):
        log = kwargs.get("stderr")
        if stderr_text and hasattr(log, "write"):
            log.write(stderr_text)
        return _FakeProcess(alive=alive)

    return popen


def test_start_tunnel_records_the_pid_for_later_stop(monkeypatch, _isolated_cloud_home):
    monkeypatch.setattr(cloud_deploy.subprocess, "Popen", _fake_popen())
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda pid: pid == 4242)

    result = cloud_deploy.start_tunnel(cloud_deploy.DeployTarget(host="h"), ["monitoring"])
    assert result["running"] is True
    assert result["pid"] == 4242
    assert json.loads((_isolated_cloud_home / "tunnel.json").read_text())["pid"] == 4242
    assert cloud_deploy.tunnel_status()["running"] is True


def test_start_tunnel_is_idempotent_while_one_is_running(monkeypatch, _isolated_cloud_home):
    (_isolated_cloud_home / "tunnel.json").write_text(
        json.dumps({"pid": 99, "host": "h", "profiles": []}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda pid: True)

    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("a second ssh child must not be spawned")

    monkeypatch.setattr(cloud_deploy.subprocess, "Popen", explode)
    assert cloud_deploy.start_tunnel(cloud_deploy.DeployTarget(host="h"))["already_running"] is True


def test_start_tunnel_reports_a_failed_local_port_bind(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy.subprocess,
        "Popen",
        _fake_popen(alive=False, stderr_text="bind: Address already in use"),
    )
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    with pytest.raises(CloudCommandError, match="Address already in use"):
        cloud_deploy.start_tunnel(cloud_deploy.DeployTarget(host="h"))


def test_start_tunnel_never_leaves_ssh_writing_to_a_pipe(monkeypatch, _isolated_cloud_home):
    """The detached child outlives the CLI, so its stderr must be a file, not a pipe.

    A pipe would be left unread under the API server, and closed under the
    tunnel's feet once the CLI process exits -- a later ssh write (a
    ServerAlive notice, say) could then take the tunnel down with SIGPIPE.
    """
    handed: dict[str, object] = {}

    def popen(_argv, **kwargs):
        handed.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(cloud_deploy.subprocess, "Popen", popen)
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda pid: pid == 4242)

    cloud_deploy.start_tunnel(cloud_deploy.DeployTarget(host="h"))

    assert handed["stderr"] is not subprocess.PIPE
    assert getattr(handed["stderr"], "name", "") == str(_isolated_cloud_home / "tunnel.log")
    assert handed["start_new_session"] is True


def test_tunnel_status_clears_a_stale_record(_isolated_cloud_home, monkeypatch):
    (_isolated_cloud_home / "tunnel.json").write_text(
        json.dumps({"pid": 12345, "host": "h", "profiles": []}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda pid: False)
    assert cloud_deploy.tunnel_status()["running"] is False
    assert not (_isolated_cloud_home / "tunnel.json").exists()


def test_stop_tunnel_signals_and_clears(monkeypatch, _isolated_cloud_home):
    (_isolated_cloud_home / "tunnel.json").write_text(
        json.dumps({"pid": 4242, "host": "h", "profiles": []}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_deploy, "_process_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(cloud_deploy.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = cloud_deploy.stop_tunnel()
    assert result == {"action": "tunnel-stop", "stopped": True, "pid": 4242}
    assert killed == [(4242, cloud_deploy.signal.SIGTERM)]
    assert not (_isolated_cloud_home / "tunnel.json").exists()


def test_stop_tunnel_without_one_running_is_not_an_error():
    assert cloud_deploy.stop_tunnel()["stopped"] is False


# --- Health --------------------------------------------------------------


def test_wait_for_health_returns_as_soon_as_the_api_answers(monkeypatch):
    statuses = iter([0, 503, 200])
    monkeypatch.setattr(cloud_deploy, "_probe", lambda url, timeout: next(statuses))
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    result = cloud_deploy.wait_for_health(timeout=60, interval=0)
    assert result["healthy"] is True
    assert result["url"] == "http://localhost:8000/health"


def test_wait_for_health_gives_up_and_reports_the_last_status(monkeypatch):
    monkeypatch.setattr(cloud_deploy, "_probe", lambda url, timeout: 502)
    monkeypatch.setattr(cloud_deploy.time, "sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 1.0, 2.0, 100.0, 100.0])
    monkeypatch.setattr(cloud_deploy.time, "monotonic", lambda: next(clock))
    result = cloud_deploy.wait_for_health(timeout=10, interval=0)
    assert result["healthy"] is False
    assert result["status"] == 502


# --- deploy / destroy end to end (all I/O stubbed) -----------------------


@pytest.fixture
def stubbed_deploy(monkeypatch, _isolated_cloud_home):
    """Stub every external effect and record the order steps run in."""
    order: list[str] = []

    def fake_apply(args):
        order.append("apply")
        _write_cloud_state(_isolated_cloud_home)
        return {
            "action": "apply",
            "settings": {"owner_ip_cidr": "203.0.113.5/32", "aws_region": "us-east-1"},
            "outputs": {"instance_id": "i-0abc", "public_ip": "198.51.100.10"},
        }

    monkeypatch.setattr(cloud_infra, "apply_infra", fake_apply)
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {})
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_ssh",
        lambda target, timeout, **k: order.append("ssh") or 1.0,
    )
    monkeypatch.setattr(
        cloud_deploy,
        "provision_instance",
        lambda target, plan: (
            order.append("provision") or {"version": plan.version, "profiles": plan.profiles}
        ),
    )
    monkeypatch.setattr(
        cloud_deploy,
        "start_tunnel",
        lambda target, profiles=None, **k: (
            order.append("tunnel") or {"running": True, "pid": 7, "urls": {}}
        ),
    )
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda timeout, **k: (
            order.append("health") or {"healthy": True, "url": "u", "status": 200, "waited": 1.0}
        ),
    )
    return order


def test_deploy_runs_the_steps_in_order_and_records_what_it_installed(
    stubbed_deploy, _isolated_cloud_home
):
    result = cloud_deploy.deploy(_args())
    assert stubbed_deploy == ["apply", "ssh", "provision", "tunnel", "health"]
    assert [step["step"] for step in result["steps"]] == [
        "infra",
        "access",
        "ssh",
        "provision",
        "tunnel",
        "health",
    ]
    # The access step is what tells the operator who can reach the box.
    access = next(s for s in result["steps"] if s["step"] == "access")
    assert access["owner_ip_cidr"] == "203.0.113.5/32"
    assert access["open_ports"] == [22]

    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["version"] == "3.0.0"
    assert recorded["host"] == "198.51.100.10"
    assert result["urls"]["web"] == "http://localhost:3000"


def test_deploy_is_idempotent_across_repeated_runs(stubbed_deploy, _isolated_cloud_home):
    first = cloud_deploy.deploy(_args())
    second = cloud_deploy.deploy(_args())
    assert first["target"] == second["target"]
    assert first["urls"] == second["urls"]
    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["host"] == "198.51.100.10"


def test_deploy_without_a_tunnel_skips_the_health_wait(stubbed_deploy):
    result = cloud_deploy.deploy(_args(no_tunnel=True))
    assert stubbed_deploy == ["apply", "ssh", "provision"]
    assert result["tunnel"]["skipped"] is True
    # The URLs are still reported -- they just need `nyxgpt cloud tunnel` first.
    assert result["urls"]["api"] == "http://localhost:8000"


def test_deploy_fails_loudly_when_the_stack_never_becomes_healthy(stubbed_deploy, monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda timeout, **k: {"healthy": False, "url": "u", "status": 0, "waited": 1.0},
    )
    with pytest.raises(CloudCommandError, match="never returned 200"):
        cloud_deploy.deploy(_args())


def test_destroy_closes_the_tunnel_before_tearing_down(monkeypatch, _isolated_cloud_home):
    order: list[str] = []
    (_isolated_cloud_home / "deploy.json").write_text(json.dumps({"host": "h"}), encoding="utf-8")
    monkeypatch.setattr(
        cloud_deploy, "stop_tunnel", lambda: (order.append("tunnel") or {"stopped": True})
    )
    monkeypatch.setattr(
        cloud_infra,
        "destroy_infra",
        lambda args: (order.append("destroy") or {"settings": {"aws_region": "us-east-1"}}),
    )
    result = cloud_deploy.destroy(_args(yes=True))
    assert order == ["tunnel", "destroy"]
    assert result["action"] == "destroy"
    assert not (_isolated_cloud_home / "deploy.json").exists()


# --- status --------------------------------------------------------------


def test_deploy_status_reports_nothing_deployed_on_a_fresh_machine(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})
    status = cloud_deploy.deploy_status()
    assert status["deployed"] is False
    assert status["tunnel"]["running"] is False
    assert status["access_command"] == "nyxgpt cloud tunnel"


def test_deploy_status_reflects_the_last_deploy(monkeypatch, _isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps(
            {
                "version": "3.0.0",
                "host": "198.51.100.10",
                "instance_id": "i-0abc",
                "region": "us-east-1",
                "profiles": ["monitoring"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    status = cloud_deploy.deploy_status()
    assert status["deployed"] is True
    assert status["version"] == "3.0.0"
    assert status["urls"]["grafana"] == "http://localhost:3001"


# --- CLI dispatch ---------------------------------------------------------


def test_deploy_command_refuses_destroy_without_confirmation(capsys):
    assert cloud_deploy.deploy_command(_args(cloud_cmd="destroy", yes=False)) == 1
    assert "--yes" in capsys.readouterr().err


def test_deploy_command_prints_the_tunnel_urls(stubbed_deploy, capsys):
    code = cloud_deploy.deploy_command(_args(cloud_cmd="deploy"))
    out = capsys.readouterr().out
    assert code == 0
    assert "http://localhost:3000" in out
    assert "nyxgpt cloud allow-ip" in out


def test_deploy_command_status_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})
    code = cloud_deploy.deploy_command(_args(cloud_cmd="deploy", status=True))
    assert code == 0
    assert json.loads(capsys.readouterr().out)["deployed"] is False


def test_deploy_command_turns_a_cloud_error_into_a_message(monkeypatch, capsys):
    monkeypatch.setattr(
        cloud_infra,
        "apply_infra",
        lambda args: (_ for _ in ()).throw(CloudCommandError("terraform apply failed")),
    )
    code = cloud_deploy.deploy_command(_args(cloud_cmd="deploy"))
    assert code == 1
    assert "terraform apply failed" in capsys.readouterr().err


def test_tunnel_command_stop_reports_when_nothing_is_running(capsys):
    code = cloud_deploy.deploy_command(_args(cloud_cmd="tunnel", stop=True))
    assert code == 0
    assert "No tunnel is running" in capsys.readouterr().out


def test_tunnel_invocation_shows_the_wrapped_ssh_command():
    target = cloud_deploy.DeployTarget(host="198.51.100.10")
    invocation = cloud_deploy.tunnel_invocation(target, ["tracing"])
    assert invocation.startswith("ssh ")
    assert "16686:127.0.0.1:16686" in invocation
