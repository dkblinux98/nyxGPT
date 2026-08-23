"""Unit tests for `nyxgpt cloud deploy` / `destroy` / `tunnel` (P6-11, #3513).

Nothing here talks to AWS, opens an SSH connection, or runs Terraform:
`cloud_infra.apply_infra`/`destroy_infra` and every `subprocess` call are
replaced with recorders, so the tests assert on the orchestration -- the
order of steps, the command lines built, what gets recorded, and the
repo-less guarantee on the provisioning script.
"""

import argparse
import io
import json
import subprocess

import pytest

from nyxgpt import cloud_deploy, cloud_infra, cloud_mac
from nyxgpt.cloud import CloudCommandError


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the module reads or writes at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_deploy, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", cloud_dir / "deploy.json")
    # #3993: without this the suite reads the developer's own
    # ~/.nyxGPT/cloud/deploy-attempt.json and reports whatever their last real
    # deploy did.
    monkeypatch.setattr(cloud_deploy, "DEPLOY_ATTEMPT_FILE", cloud_dir / "deploy-attempt.json")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_HISTORY_FILE", cloud_dir / "history.jsonl")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_STATE_FILE", cloud_dir / "tunnel.json")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_LOG_FILE", cloud_dir / "tunnel.log")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    # #3995's EC2 Mac roots keep state of their own, and their module-level
    # paths bind at import -- unpatched, `mac_state_exists()` would answer from
    # the developer's real ~/.nyxGPT and a test would pass or fail depending on
    # whose machine ran it.
    monkeypatch.setattr(cloud_mac, "MAC_TFSTATE_FILE", cloud_dir / "mac.tfstate")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFSTATE_FILE", cloud_dir / "mac-release.tfstate")
    monkeypatch.setattr(cloud_mac, "MAC_TFVARS_FILE", cloud_dir / "mac.tfvars")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFVARS_FILE", cloud_dir / "mac-release.tfvars")
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


def test_provision_script_puts_nyxgpt_on_the_login_users_path():
    """#3993: an operator who SSHes in to diagnose must be able to run `nyxgpt`.

    The venv is never activated by a login shell, so every wrapped command the
    docs, the dashboard and this script's own output name answered `nyxgpt:
    command not found` -- while the binary sat in a directory nothing told the
    operator about. That turned "run the instance's own doctor" into a
    scavenger hunt at the exact moment it mattered.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert "/etc/profile.d/nyxgpt.sh" in script
    assert 'PATH="$HOME/.nyxGPT/venv/bin:$PATH"' in script
    # Quoted heredoc delimiter: $HOME must be expanded by the login shell that
    # sources the file, not by the provisioning run, or every user would get
    # the deploying user's home directory.
    assert "<<'PROFILE_EOF'" in script
    # And it happens before the stack comes up, so a bring-up failure still
    # leaves a usable CLI behind to diagnose it with.
    assert script.index("/etc/profile.d/nyxgpt.sh") < script.index("run_nyxgpt ops install")


def test_provision_script_installs_the_pinned_published_release():
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(version="3.1.2")))
    assert 'NYXGPT_VERSION="3.1.2"' in script
    assert 'pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"' in script


def test_provision_script_resolves_a_python_that_meets_the_requires_python_floor():
    """#3782: Amazon Linux 2023's `python3` is 3.9 -- pip refuses nyxGPT in it.

    The owner's rc9 run got as far as `pip install ...nyxgpt-api-3.0.0rc9.tar.gz
    (rc=1)` because a 3.9 venv cannot satisfy `requires-python = ">=3.11"`.
    Provisioning must install and then *resolve* an interpreter that does,
    asking each candidate its own version rather than trusting its name.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert "for pkg in python3.13 python3.12 python3.11; do" in script
    assert "for cand in python3.13 python3.12 python3.11 python3; do" in script
    assert "sys.version_info >= (3, 11)" in script
    # The venv is built from the resolved interpreter, never from a bare
    # `python3` assumed to be new enough.
    assert '"$PY" -m venv "$HOME/.nyxGPT/venv"' in script
    assert "python3 -m venv" not in script
    # ...and it is resolved before anything uses it.
    assert script.index("for cand in python3.13") < script.index('"$PY" -m venv')


def test_provision_script_fails_fast_when_no_python_meets_the_floor():
    """A clear failure naming found/required versions, not an opaque pip rc=1."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert 'if [ -z "$PY" ]; then' in script
    assert "no Python >= 3.11 is installed or installable" in script
    assert script.index("no Python >= 3.11") < script.index("run_nyxgpt ops install")


def test_provision_script_installs_node_before_ops_install():
    """#3761: without npm, `ops install`'s web step fails and the deploy has no web UI.

    The owner's acceptance run stopped at `[14/23] native web service` ->
    `npm not found; cannot install nyxgpt-web`, which per the Definition of
    Done is a failed deploy, not a partial one.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    # Both package-manager branches of the support matrix (dnf on Amazon
    # Linux, apt-get on Ubuntu) get Node 20, not just one.
    assert "rpm.nodesource.com/setup_20.x" in script
    assert "deb.nodesource.com/setup_20.x" in script
    # Before the install that needs it -- the real invocation, not the
    # narration around it.
    assert script.index("nodesource.com") < script.index("run_nyxgpt ops install")


def test_provision_script_keeps_an_existing_node_20_installation():
    """Re-deploys and Node-preinstalled AMIs shouldn't reinstall the toolchain."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert 'if ! command -v npm >/dev/null 2>&1 || [ "${NODE_MAJOR:-0}" -lt 20 ]; then' in script


def test_provision_script_fails_fast_when_npm_is_still_missing():
    """A missing toolchain should name itself, not surface 14 steps later."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert "node/npm could not be installed" in script
    assert script.index("node/npm could not be installed") < script.index("run_nyxgpt ops install")

    # The script runs under `set -euo pipefail`, so an install that exits
    # non-zero would abort before the diagnostic ever printed. Every install
    # in the Node block has to be non-fatal for the check above to be
    # reachable at all.
    assert "set -euo pipefail" in script
    node_block = script[
        script.index("NODE_MAJOR=0") : script.index("node/npm could not be installed")
    ]
    installs = [
        line.strip()
        for line in node_block.splitlines()
        if ("dnf install" in line or "apt-get install" in line) and not line.strip().startswith("#")
    ]
    assert installs, "the Node block installs nothing"
    # A continued command is only fatal on its last line, where the `|| true` sits.
    assert all(
        line.endswith("|| true") or line.endswith("\\") for line in installs
    ), f"an install can abort before the diagnostic: {installs}"


def test_provision_script_skips_observability_when_asked():
    with_obs = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    without = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(skip_observability=True))
    )
    assert 'NYXGPT_PROFILES="monitoring,logging,tracing,errors"' in with_obs
    # With profiles the *install* reconciles them (its "observability stack"
    # step), so the script no longer follows it with a second `ops
    # observability` pass -- see the single-pass test below (#3762).
    assert "run_nyxgpt ops install\n" in with_obs
    assert 'NYXGPT_PROFILES=""' in without
    assert "run_nyxgpt ops install --skip-observability" in without


@pytest.mark.parametrize("profiles", ["monitoring,logging,tracing,errors", ""])
def test_provision_script_runs_exactly_one_install_pass(profiles):
    """#3762: the owner watched the full 23-step install run twice in one deploy.

    Two causes, both gone: rc5's `sg docker -c "..." || "$NYXGPT" ...` retry
    (removed by #3760) and the `ops observability` call that followed a full
    `ops install`, re-running the same Grafana provisioning reconcile.

    The `if` block is executed with `run_nyxgpt` stubbed rather than
    string-matched, so both branches are counted as they actually run -- a
    text scan sees the taken and untaken branch alike.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    start = script.index('if [ -n "$NYXGPT_PROFILES" ]; then')
    block = script[start : script.index("\nfi\n", start) + len("\nfi\n")]

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f'run_nyxgpt() {{ echo "PASS: $*"; }}\nNYXGPT_PROFILES="{profiles}"\n{block}',
        ],
        capture_output=True,
        text=True,
    )

    passes = [line for line in completed.stdout.splitlines() if line.startswith("PASS:")]
    assert passes == [
        "PASS: ops install" if profiles else "PASS: ops install --skip-observability"
    ], completed.stdout


def test_provision_script_never_runs_nyxgpt_without_the_docker_group():
    """#3760: `usermod -aG docker` doesn't reach this already-open SSH session,
    so the old `sg docker -c "..." || "$NYXGPT" ...` form silently retried the
    whole command *without* the group whenever the first attempt failed for an
    unrelated reason -- turning one failed step into a "permission denied ...
    /var/run/docker.sock" cascade."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert 'sg docker -c "$NYXGPT $*"' in script
    assert '|| "$NYXGPT" ops' not in script
    assert "run_nyxgpt ops install" in script


def test_provision_script_leaves_the_compose_plugin_to_ops_install():
    """Amazon Linux 2023 packages no compose at all, so the per-distro decision
    lives in `nyxgpt ops install` (distro package, then release binary) rather
    than being guessed here (#3760)."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert "docker-compose-plugin" not in script
    assert "docker-compose-v2" not in script


def test_provision_script_enables_self_healing(monkeypatch):
    """P6-16 (#3516) accepts a *self-healing* deployment, not merely a running one.

    The watchdog ships disabled (example.config.ini's `[self_heal] enabled =
    false` seeds the runtime flag), and a cloud instance is unattended by
    definition, so the deploy turns it on explicitly.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))

    assert '"$NYXGPT" self-heal enable' in script
    # After the stack exists, not before -- enabling the watchdog is
    # meaningless until there are components for it to watch. Anchored on the
    # invocation, not the bare words: #3761's Node block narrates `ops install`
    # in a comment far earlier in the script.
    assert script.index("run_nyxgpt ops install") < script.index("self-heal enable")


def test_provision_script_enables_self_healing_without_observability(monkeypatch):
    script = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(skip_observability=True))
    )

    assert '"$NYXGPT" self-heal enable' in script


def _fake_provision_run(monkeypatch, returncode, output):
    """Stand in for the remote run, recording the script it was handed."""
    seen = {}

    def fake_run(target, script, remote_command="bash -s"):
        seen["script"] = script
        seen["remote_command"] = remote_command
        return returncode, list(output)

    monkeypatch.setattr(cloud_deploy, "_run_provision_script", fake_run)
    return seen


def test_provision_instance_reports_self_heal_enabled(monkeypatch):
    _fake_provision_run(monkeypatch, 0, [])
    target = cloud_deploy.DeployTarget(host="198.51.100.10")

    result = cloud_deploy.provision_instance(target, cloud_deploy.resolve_plan(_args()))

    assert result["self_heal_enabled"] is True


def test_provision_instance_raises_with_the_remote_diagnostic(monkeypatch):
    _fake_provision_run(monkeypatch, 1, ["dnf: no such package"])
    target = cloud_deploy.DeployTarget(host="198.51.100.10")
    with pytest.raises(CloudCommandError, match="no such package"):
        cloud_deploy.provision_instance(target, cloud_deploy.resolve_plan(_args()))


def test_provision_failure_lists_every_failed_step_untruncated():
    """#3762: the summary quoted a character slice of stderr, so the owner's run
    ended on `Provisioning the instance failed: 2-user/.nyxGPT/volumes/...` --
    the tail of `ec2-user`, spliced mid-word, and not a failed step at all."""
    long_path = "/home/ec2-user/.nyxGPT/volumes/prometheus"
    output = [
        "[1/23] sync packaged ops resources...",
        "[OK] Synced packaged ops resources",
        "[4/23] docker engine...",
        "[FAIL] Could not install Docker Compose automatically",
        "[OK] Docker daemon is reachable",
        "[FAIL] step 4/23 'docker engine' did not fully succeed: 1 of 3 checks failed",
        f"[FAIL] {long_path} is owned by the container's uid (65534)",
    ]

    detail = cloud_deploy._provision_failure_detail(output)

    assert detail.splitlines()[0] == "Provisioning the instance failed. Failed steps:"
    assert "[FAIL] Could not install Docker Compose automatically" in detail
    assert "'docker engine' did not fully succeed" in detail
    # Untruncated and never spliced mid-word: the whole path survives.
    assert long_path in detail
    # Successful steps are not dressed up as failures.
    assert "Docker daemon is reachable" not in detail


def test_provision_failure_falls_back_to_whole_tail_lines():
    """A failure before `ops install` starts has no `[FAIL]` line to name, so the
    fallback quotes the tail -- cut on line boundaries, never mid-line."""
    output = [f"line {i}" for i in range(60)]

    detail = cloud_deploy._provision_failure_detail(output)

    lines = detail.splitlines()
    assert lines[0].startswith("Provisioning the instance failed with no step-level diagnostic.")
    quoted = [line.strip() for line in lines[1:]]
    assert quoted == [f"line {i}" for i in range(60 - cloud_deploy._PROVISION_TAIL_LINES, 60)]


def test_provision_failure_says_so_when_nothing_was_returned():
    assert "no diagnostic returned" in cloud_deploy._provision_failure_detail(["", "  "])


def test_run_provision_script_streams_and_captures_merged_output(monkeypatch, capsys):
    """The operator sees the run live *and* the summary can quote it (#3762):
    stderr is merged into stdout so both are captured in the right order."""
    handed = {}

    class _FakeProc:
        returncode = 2

        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("[1/23] config...\n[FAIL] boom\n")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_popen(argv, **kwargs):
        handed["argv"] = argv
        handed["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(cloud_deploy.subprocess, "Popen", fake_popen)
    target = cloud_deploy.DeployTarget(host="198.51.100.10")

    returncode, output = cloud_deploy._run_provision_script(target, "echo hi")

    assert returncode == 2
    assert output == ["[1/23] config...", "[FAIL] boom"]
    assert handed["kwargs"]["stderr"] is subprocess.STDOUT
    assert handed["argv"][-1] == "bash -s"
    # Streamed as it arrives rather than withheld until the end.
    assert "[FAIL] boom" in capsys.readouterr().out


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


# --- lifecycle history (P6-15, #3514) -------------------------------------


def test_history_is_empty_before_anything_has_been_deployed():
    assert cloud_deploy.deploy_history() == []


def test_deploy_records_a_succeeded_history_entry(stubbed_deploy):
    cloud_deploy.deploy(_args())
    history = cloud_deploy.deploy_history()
    assert len(history) == 1
    assert history[0]["action"] == "deploy"
    assert history[0]["outcome"] == "succeeded"
    assert history[0]["version"] == "3.0.0"
    assert history[0]["host"] == "198.51.100.10"
    assert history[0]["instance_id"] == "i-0abc"
    assert history[0]["ts"] > 0


def test_history_is_newest_first_across_repeated_deploys(stubbed_deploy):
    cloud_deploy.deploy(_args(version="3.0.0"))
    cloud_deploy.deploy(_args(version="3.0.1"))
    history = cloud_deploy.deploy_history()
    assert [entry["version"] for entry in history] == ["3.0.1", "3.0.0"]


def test_a_deploy_that_never_goes_healthy_is_still_in_the_history(stubbed_deploy, monkeypatch):
    """The failure case is the one the history most needs to show."""
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda timeout, **k: {"healthy": False, "url": "u", "status": 0, "waited": 1.0},
    )
    with pytest.raises(CloudCommandError):
        cloud_deploy.deploy(_args())
    history = cloud_deploy.deploy_history()
    assert len(history) == 1
    assert history[0]["outcome"] == "failed"
    assert "never returned 200" in history[0]["detail"]


def test_destroy_records_what_it_tore_down_before_the_record_is_deleted(
    monkeypatch, _isolated_cloud_home
):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "version": "3.0.0", "instance_id": "i-0abc"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(
        cloud_infra, "destroy_infra", lambda args: {"settings": {"aws_region": "us-east-1"}}
    )
    cloud_deploy.destroy(_args(yes=True))
    history = cloud_deploy.deploy_history()
    assert history[0]["action"] == "destroy"
    assert history[0]["outcome"] == "succeeded"
    assert history[0]["version"] == "3.0.0"
    assert history[0]["instance_id"] == "i-0abc"


def test_a_teardown_that_fails_partway_is_still_in_the_history(monkeypatch, _isolated_cloud_home):
    """Symmetric with the failed deploy: a half-torn-down substrate is the
    state most worth a trace, and `deploy.json` must survive it."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "version": "3.0.0", "instance_id": "i-0abc"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(
        cloud_infra,
        "destroy_infra",
        lambda args: (_ for _ in ()).throw(CloudCommandError("terraform destroy failed")),
    )
    with pytest.raises(CloudCommandError):
        cloud_deploy.destroy(_args(yes=True))
    history = cloud_deploy.deploy_history()
    assert len(history) == 1
    assert history[0]["action"] == "destroy"
    assert history[0]["outcome"] == "failed"
    assert history[0]["instance_id"] == "i-0abc"
    assert "terraform destroy failed" in history[0]["detail"]
    # The deployment record is left in place: nothing proved it is gone.
    assert (_isolated_cloud_home / "deploy.json").exists()


def test_history_skips_a_truncated_line_rather_than_losing_everything(_isolated_cloud_home):
    (_isolated_cloud_home / "history.jsonl").write_text(
        json.dumps({"action": "deploy", "outcome": "succeeded", "ts": 1.0})
        + "\n"
        + '{"action": "destroy", "outc',
        encoding="utf-8",
    )
    history = cloud_deploy.deploy_history()
    assert len(history) == 1
    assert history[0]["action"] == "deploy"


def test_history_honours_its_limit(_isolated_cloud_home):
    (_isolated_cloud_home / "history.jsonl").write_text(
        "".join(json.dumps({"action": "deploy", "ts": float(i)}) + "\n" for i in range(30)),
        encoding="utf-8",
    )
    assert len(cloud_deploy.deploy_history()) == cloud_deploy.HISTORY_LIMIT
    assert len(cloud_deploy.deploy_history(limit=5)) == 5
    assert cloud_deploy.deploy_history(limit=0) == []
    # Newest first: the last line written is the first one returned.
    assert cloud_deploy.deploy_history(limit=5)[0]["ts"] == 29.0


def test_recording_history_never_fails_a_deploy(monkeypatch, _isolated_cloud_home):
    """An unwritable history file is a reporting problem, not a deploy failure."""
    # A plain file where the history's parent directory should be: mkdir and
    # the append both fail with an OSError, which is what has to be swallowed.
    blocker = _isolated_cloud_home / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_HISTORY_FILE", blocker / "history.jsonl")

    assert cloud_deploy.record_history("deploy", "succeeded")["outcome"] == "succeeded"
    assert cloud_deploy.deploy_history() == []


# --- status --------------------------------------------------------------


def test_deploy_status_reports_nothing_deployed_on_a_fresh_machine(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})
    status = cloud_deploy.deploy_status()
    assert status["deployed"] is False
    assert status["tunnel"]["running"] is False
    assert status["access_command"] == "nyxgpt cloud tunnel"
    assert status["history"] == []
    # Every lifecycle pointer the dashboard renders is a wrapped `nyxgpt`
    # command -- CLAUDE.md forbids handing the user a raw docker/terraform one.
    assert all(command.startswith("nyxgpt ") for command in status["commands"].values())


def test_deploy_status_does_not_probe_health_unless_asked(monkeypatch):
    """The polled default must make no network calls."""
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(
        cloud_deploy, "_probe", lambda url, timeout: pytest.fail("probed without being asked")
    )
    assert cloud_deploy.deploy_status()["health"]["checked"] is False


def test_deploy_status_skips_the_health_probe_when_no_tunnel_is_open(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(cloud_deploy, "tunnel_status", lambda: {"running": False, "urls": {}})
    monkeypatch.setattr(
        cloud_deploy, "_probe", lambda url, timeout: pytest.fail("probed with no tunnel open")
    )
    health = cloud_deploy.deploy_status(probe_health=True)["health"]
    assert health["checked"] is False
    assert "tunnel" in health["reason"]


def test_deploy_status_probes_the_tunneled_health_endpoint_when_asked(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(cloud_deploy, "tunnel_status", lambda: {"running": True, "urls": {}})
    monkeypatch.setattr(cloud_deploy, "_probe", lambda url, timeout: 200)
    health = cloud_deploy.deploy_status(probe_health=True)["health"]
    assert health == {
        "checked": True,
        "healthy": True,
        "status": 200,
        "url": "http://localhost:8000/health",
        "reason": "",
    }


def test_deploy_status_reports_an_unhealthy_probe(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(cloud_deploy, "tunnel_status", lambda: {"running": True, "urls": {}})
    monkeypatch.setattr(cloud_deploy, "_probe", lambda url, timeout: 503)
    health = cloud_deploy.deploy_status(probe_health=True)["health"]
    assert health["checked"] is True
    assert health["healthy"] is False
    assert health["status"] == 503


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


def test_deploy_status_names_the_deploy_record_as_its_source(monkeypatch, _isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10"}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_DEPLOY_RECORD
    assert status["known"] is True
    assert status["on_instance"] is False


def test_deploy_status_reports_itself_when_served_from_the_instance(monkeypatch):
    """On the instance there is no deploy record -- the answering stack is the answer (#3804).

    The record lives on the workstation that ran `nyxgpt cloud deploy`, so a
    dashboard served from the box would otherwise report "not deployed" about
    the very release rendering the page.
    """
    monkeypatch.setattr(
        cloud_infra,
        "infra_status",
        lambda: {"provisioned": True, "on_ec2": True, "public_ip": "203.0.113.10"},
    )
    monkeypatch.setattr(cloud_deploy, "installed_version", lambda: "3.0.0rc12")

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_LOCAL_INSTANCE
    assert status["known"] is True
    assert status["on_instance"] is True
    assert status["deployed"] is True
    assert status["version"] == "3.0.0rc12"
    assert status["host"] == "203.0.113.10"


def test_deploy_status_does_not_probe_a_tunnel_from_the_instance(monkeypatch):
    """The tunnel is not the access path here, and the prober is the probed."""
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"on_ec2": True, "public_ip": ""})
    monkeypatch.setattr(cloud_deploy, "tunnel_status", lambda: {"running": True, "urls": {}})
    monkeypatch.setattr(
        cloud_deploy, "_probe", lambda url, timeout: pytest.fail("probed itself through a tunnel")
    )

    health = cloud_deploy.deploy_status(probe_health=True)["health"]

    assert health["checked"] is False
    assert "served from the instance" in health["reason"]


def test_deploy_status_is_unknown_rather_than_not_deployed_on_a_third_machine(monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_UNKNOWN
    assert status["known"] is False
    assert status["version"] == ""


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


# --- Remote credential retrieval (`nyxgpt cloud credentials`, #3718) ------


_REMOTE_CREDS = [
    {
        "service": "grafana",
        "url": "http://localhost:3001",
        "username": "admin",
        "password": "remote-grafana-pw",  # pragma: allowlist secret
        "source": "/home/ec2-user/.nyxGPT/secrets/grafana-admin-password",
        "remediation": "",
        "available": True,
    },
    {
        "service": "glitchtip",
        "url": "http://localhost:8080",
        "username": "admin@nyxgpt.local",
        "password": "remote-glitchtip-pw",  # pragma: allowlist secret
        "source": "/home/ec2-user/.nyxGPT/config.ini [error_tracking] admin_password",
        "remediation": "",
        "available": True,
    },
]


def test_remote_credentials_runs_the_wrapped_ops_command_over_ssh(monkeypatch):
    """No hand-rolled `ssh` + `cat`: the instance's own wrapped command is what
    reads the secrets, through the same run_remote path as every other step."""
    seen = {}

    def fake_remote(target, command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(["ssh"], 0, stdout=json.dumps(_REMOTE_CREDS), stderr="")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)

    creds = cloud_deploy.remote_credentials(cloud_deploy.DeployTarget(host="h"))

    assert "nyxgpt" in seen["command"] and "ops credentials --json" in seen["command"]
    assert "cat" not in seen["command"]
    assert [c["service"] for c in creds] == ["grafana", "glitchtip"]
    assert creds[0]["password"] == "remote-grafana-pw"  # pragma: allowlist secret


def test_remote_credentials_tolerates_leading_stderr_noise_on_stdout(monkeypatch):
    """A remote CLI that prints a config warning before its JSON is still
    parseable -- the payload starts at the first `[`."""
    noisy = "prometheus-client is not installed\n" + json.dumps(_REMOTE_CREDS)
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(["ssh"], 0, stdout=noisy, stderr=""),
    )

    creds = cloud_deploy.remote_credentials(cloud_deploy.DeployTarget(host="h"))

    assert len(creds) == 2


def test_remote_credentials_passes_the_service_filter(monkeypatch):
    seen = {}

    def fake_remote(target, command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(_REMOTE_CREDS[:1]), stderr=""
        )

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)

    cloud_deploy.remote_credentials(cloud_deploy.DeployTarget(host="h"), service="grafana")

    assert "--service grafana" in seen["command"]


def test_remote_credentials_reports_an_unreadable_instance(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            ["ssh"], 127, stdout="", stderr="nyxgpt: command not found"
        ),
    )

    with pytest.raises(CloudCommandError, match="command not found"):
        cloud_deploy.remote_credentials(cloud_deploy.DeployTarget(host="h"))


def test_credentials_command_prints_the_logins_and_the_tunnel_hint(
    _isolated_cloud_home, monkeypatch, capsys
):
    _write_cloud_state(_isolated_cloud_home)
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(_REMOTE_CREDS), stderr=""
        ),
    )

    code = cloud_deploy.deploy_command(_args(cloud_cmd="credentials", service="all", json=False))

    out = capsys.readouterr().out
    assert code == 0
    assert "remote-grafana-pw" in out
    assert "remote-glitchtip-pw" in out
    assert "nyxgpt cloud tunnel" in out


def test_credentials_command_exits_two_when_a_service_is_unprovisioned(
    _isolated_cloud_home, monkeypatch, capsys
):
    _write_cloud_state(_isolated_cloud_home)
    unprovisioned = [
        dict(_REMOTE_CREDS[0]),
        {
            **_REMOTE_CREDS[1],
            "password": "",
            "available": False,
            "remediation": "run `nyxgpt ops glitchtip-init`",
        },
    ]
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(unprovisioned), stderr=""
        ),
    )

    code = cloud_deploy.deploy_command(_args(cloud_cmd="credentials", service="all", json=False))

    out = capsys.readouterr().out
    assert code == 2
    assert "(not provisioned)" in out
    assert "nyxgpt ops glitchtip-init" in out


# --- The connection target and `nyxgpt cloud status` (#3813) --------------


def _write_deploy_record(cloud_dir, **overrides):
    """Write the record a completed `nyxgpt cloud deploy` leaves behind."""
    record = {
        "version": "3.0.0",
        "profiles": ["monitoring"],
        "ssh_user": "ec2-user",
        "identity_file": "/keys/nyxgpt.pem",
        "host": "198.51.100.10",
        "instance_id": "i-0abc",
        "region": "us-east-1",
    }
    record.update(overrides)
    (cloud_dir / "deploy.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_recorded_target_is_none_before_any_deploy():
    assert cloud_deploy.recorded_target() is None


def test_recorded_target_carries_the_user_and_key_the_deploy_used(_isolated_cloud_home):
    _write_deploy_record(_isolated_cloud_home)

    target = cloud_deploy.recorded_target()

    assert target is not None
    assert (target.user, target.host) == ("ec2-user", "198.51.100.10")
    assert target.identity_file == "/keys/nyxgpt.pem"


def test_connection_status_reports_the_ssh_target_and_what_the_tunnel_runs(_isolated_cloud_home):
    """The gap #3813 was filed for: `host` alone is not a reachable address."""
    _write_deploy_record(_isolated_cloud_home)

    connection = cloud_deploy.connection_status()

    assert connection["known"] is True
    assert connection["target"] == "ec2-user@198.51.100.10"
    assert connection["identity_file"] == "/keys/nyxgpt.pem"
    # `tunnel_invocation` was written for this surface and had no callers.
    assert connection["tunnel_invocation"].startswith("ssh ")
    assert "-L 8000:127.0.0.1:8000" in connection["tunnel_invocation"]
    assert connection["tunnel_invocation"].endswith("ec2-user@198.51.100.10")
    # The wrapped command is what the operator is pointed at, never the ssh.
    assert connection["command"] == "nyxgpt cloud tunnel"


def test_connection_status_forwards_only_the_profiles_the_deploy_enabled(_isolated_cloud_home):
    _write_deploy_record(_isolated_cloud_home, profiles=[])

    invocation = cloud_deploy.connection_status()["tunnel_invocation"]

    assert "-L 3000:127.0.0.1:3000" in invocation
    assert "16686" not in invocation


def test_connection_status_says_why_it_cannot_answer_rather_than_blanking():
    connection = cloud_deploy.connection_status()

    assert connection["known"] is False
    assert connection["target"] == ""
    assert "no deploy has been recorded" in connection["reason"]


def test_connection_status_on_the_instance_points_at_the_workstations_record():
    connection = cloud_deploy.connection_status(on_instance=True)

    assert connection["known"] is False
    assert "instance itself" in connection["reason"]


def test_deploy_status_carries_the_connection_and_the_instance_type(
    monkeypatch, _isolated_cloud_home
):
    _write_deploy_record(_isolated_cloud_home)
    monkeypatch.setattr(
        cloud_infra,
        "infra_status",
        lambda: {
            "provisioned": True,
            "instance_type": "t3.large",
            "owner_ip_cidr": "203.0.113.5/32",
        },
    )

    status = cloud_deploy.deploy_status()

    assert status["connection"]["target"] == "ec2-user@198.51.100.10"
    assert status["instance_type"] == "t3.large"


def test_resolve_access_target_restores_the_recorded_key(_isolated_cloud_home):
    """Inspecting a deployment made with a non-default key must not require
    re-typing --identity-file every time."""
    _write_cloud_state(_isolated_cloud_home)
    _write_deploy_record(_isolated_cloud_home)

    target = cloud_deploy.resolve_access_target(_args())

    assert target.user == "ec2-user"
    assert target.identity_file == "/keys/nyxgpt.pem"


def test_resolve_access_target_lets_explicit_flags_win(_isolated_cloud_home, tmp_path):
    _write_cloud_state(_isolated_cloud_home)
    _write_deploy_record(_isolated_cloud_home)
    override = tmp_path / "other.pem"

    target = cloud_deploy.resolve_access_target(
        _args(ssh_user="ubuntu", identity_file=str(override))
    )

    assert target.user == "ubuntu"
    assert target.identity_file == str(override)


def _status_out(capsys, monkeypatch, cloud_dir, **args_overrides) -> str:
    monkeypatch.setattr(
        cloud_infra,
        "infra_status",
        lambda: {
            "provisioned": True,
            "instance_type": "t3.large",
            "owner_ip_cidr": "203.0.113.5/32",
            "access_model": {"open_ports": [22]},
        },
    )
    code = cloud_deploy.deploy_command(
        _args(cloud_cmd="status", json=False, no_probe=True, **args_overrides)
    )
    assert code == 0
    return capsys.readouterr().out


def test_status_command_prints_an_operator_readable_summary(
    monkeypatch, _isolated_cloud_home, capsys
):
    _write_deploy_record(_isolated_cloud_home)

    out = _status_out(capsys, monkeypatch, _isolated_cloud_home)

    # Everything the operator had to scroll back through deploy output for.
    assert "DEPLOYED" in out
    assert "3.0.0" in out
    assert "i-0abc (t3.large)" in out
    assert "us-east-1" in out
    assert "198.51.100.10" in out
    assert "ec2-user@198.51.100.10" in out
    assert "/keys/nyxgpt.pem" in out
    assert "203.0.113.5/32" in out
    assert "http://localhost:3000" in out
    # And the wrapped route to the instance's containers, which used to be a
    # hand-rolled ssh plus a raw `docker compose ps`.
    assert "nyxgpt cloud ops status" in out
    assert "docker compose" not in out


def test_status_command_labels_the_raw_ssh_as_diagnostics_not_an_instruction(
    monkeypatch, _isolated_cloud_home, capsys
):
    """CLAUDE.md forbids handing an operator a raw command as the instruction."""
    _write_deploy_record(_isolated_cloud_home)

    out = _status_out(capsys, monkeypatch, _isolated_cloud_home)
    diagnostics = out[out.index("Diagnostics") :]

    assert "run the wrapped command, not this" in diagnostics.lower()
    assert "ssh -o StrictHostKeyChecking" in diagnostics
    # The ssh line appears only under that heading.
    assert out.count("ssh -o StrictHostKeyChecking") == 1


def test_status_command_json_emits_the_machine_payload(monkeypatch, _isolated_cloud_home, capsys):
    _write_deploy_record(_isolated_cloud_home)
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})

    code = cloud_deploy.deploy_command(_args(cloud_cmd="status", json=True, no_probe=True))

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["deployed"] is True
    assert payload["connection"]["target"] == "ec2-user@198.51.100.10"


def test_status_command_is_unknown_rather_than_not_deployed(monkeypatch, capsys):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    code = cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    out = capsys.readouterr().out
    assert code == 0
    assert "UNKNOWN" in out
    assert "not the same as nothing being deployed" in out


# --- a failed deploy is describable, not invisible (#3993) ----------------
#
# The defect: `deploy.json` was written only on success, so a provision that
# died partway left no record at all -- and `cloud status` reported "UNKNOWN
# from this machine" while a live, billing EC2 instance ran and `state.json`
# on the same disk named its instance id and IPs. That is precisely the state
# an operator most needs status to describe, and it was the one state it could
# not see.


def _write_attempt(cloud_dir, **overrides):
    """Write the record `nyxgpt cloud deploy` leaves after a run that did not finish."""
    attempt = {
        "status": cloud_deploy.ATTEMPT_FAILED,
        "phase": "provision",
        "started_at": 1.0,
        "updated_at": 2.0,
        "version": "3.0.0",
        "host": "198.51.100.10",
        "instance_id": "i-0abc",
        "region": "us-east-1",
        "error": "Could not reconcile Grafana admin credential",
    }
    attempt.update(overrides)
    (cloud_dir / "deploy-attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
    return attempt


def test_deploy_records_the_attempt_before_provisioning(
    stubbed_deploy, _isolated_cloud_home, monkeypatch
):
    """The record has to exist *before* the work, or a crash leaves nothing."""
    seen: list[dict] = []

    def _capture(target, plan):
        seen.append(cloud_deploy.load_deploy_attempt())
        return {"version": plan.version, "profiles": plan.profiles}

    monkeypatch.setattr(cloud_deploy, "provision_instance", _capture)

    cloud_deploy.deploy(_args(no_tunnel=True))

    assert seen and seen[0]["status"] == cloud_deploy.ATTEMPT_RUNNING
    assert seen[0]["phase"] == "provision"
    assert seen[0]["host"] == "198.51.100.10"


def test_a_successful_deploy_closes_the_attempt_out(stubbed_deploy, _isolated_cloud_home):
    cloud_deploy.deploy(_args(no_tunnel=True))

    attempt = cloud_deploy.load_deploy_attempt()
    assert attempt["status"] == cloud_deploy.ATTEMPT_SUCCEEDED
    assert attempt["phase"] == "done"


def test_a_failed_deploy_leaves_a_record_naming_the_phase(
    stubbed_deploy, _isolated_cloud_home, monkeypatch
):
    """The core of #3993: the failure itself must leave something behind."""

    def _explode(target, plan):
        raise CloudCommandError("[FAIL] Could not reconcile Grafana admin credential")

    monkeypatch.setattr(cloud_deploy, "provision_instance", _explode)

    with pytest.raises(CloudCommandError):
        cloud_deploy.deploy(_args(no_tunnel=True))

    attempt = cloud_deploy.load_deploy_attempt()
    assert attempt["status"] == cloud_deploy.ATTEMPT_FAILED
    assert attempt["phase"] == "provision"
    assert "Grafana" in attempt["error"]
    assert attempt["instance_id"] == "i-0abc"


def test_status_describes_a_failed_deploy_instead_of_reporting_unknown(
    monkeypatch, _isolated_cloud_home, capsys
):
    """The owner's observation, exactly: a live instance reported as UNKNOWN."""
    _write_attempt(_isolated_cloud_home)
    monkeypatch.setattr(
        cloud_infra,
        "infra_status",
        lambda: {"provisioned": True, "instance_id": "i-0abc", "security_group_id": "sg-0abc"},
    )

    code = cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    out = capsys.readouterr().out
    assert code == 0
    assert "UNKNOWN" not in out
    assert "NOT COMPLETED" in out
    assert "provision" in out
    assert "Grafana" in out
    assert "i-0abc" in out
    assert "an instance exists and is being billed" in out


def test_a_deploy_that_failed_before_the_substrate_claims_no_billing(
    monkeypatch, _isolated_cloud_home, capsys
):
    """D-018 inside the fix written to enforce it (#4007 review).

    A deploy can fail at `start`/`infra` before Terraform creates anything --
    no terraform binary, no AWS credentials, a failed `terraform init`. The
    attempt records FAILED with no instance_id and no substrate record, and the
    summary used to print "an instance exists and is being billed" over it, then
    prescribe `cloud destroy`, which raises "nothing to destroy". Both are
    claims nothing checked.
    """
    _write_attempt(
        _isolated_cloud_home,
        phase="infra",
        instance_id="",
        host="",
        error="terraform init failed: no such binary",
    )
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    code = cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    out = capsys.readouterr().out
    assert code == 0
    assert "NOT COMPLETED" in out
    assert "an instance exists and is being billed" not in out
    assert "nothing is recorded as provisioned by this attempt" in out.lower()
    assert "cloud destroy" not in out, "destroy sends the operator to 'nothing to destroy'"


def test_a_declined_mac_allocation_is_not_reported_as_billing(
    monkeypatch, _isolated_cloud_home, capsys
):
    """`confirm_allocation` records "nothing was allocated and nothing is billed".

    That error line used to sit inside a frame asserting the exact opposite.
    """
    _write_attempt(
        _isolated_cloud_home,
        phase="infra",
        instance_id="",
        host="",
        error="Not confirmed -- nothing was allocated and nothing is billed",
    )
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    out = capsys.readouterr().out
    assert "nothing was allocated and nothing is billed" in out
    assert "an instance exists and is being billed" not in out


def test_status_payload_marks_a_failed_deploy_as_not_deployed(monkeypatch, _isolated_cloud_home):
    """Describable is not the same as deployed -- the three outcomes stay apart."""
    _write_attempt(_isolated_cloud_home)
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_DEPLOY_ATTEMPT
    assert status["known"] is True
    assert status["deployed"] is False
    assert status["attempt"]["phase"] == "provision"


def test_status_falls_back_to_the_substrate_record(monkeypatch, _isolated_cloud_home):
    """No deploy and no attempt, but this machine's own state file names an instance."""
    monkeypatch.setattr(
        cloud_infra,
        "infra_status",
        lambda: {"provisioned": True, "instance_id": "i-0abc", "public_ip": "198.51.100.10"},
    )

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_SUBSTRATE_RECORD
    assert status["known"] is True
    assert status["deployed"] is False
    assert status["instance_id"] == "i-0abc"


def test_status_still_reports_unknown_with_no_source_at_all(monkeypatch, _isolated_cloud_home):
    """D-018 is intact: a machine with nothing to answer from still says unknown."""
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    status = cloud_deploy.deploy_status()

    assert status["source"] == cloud_deploy.SOURCE_UNKNOWN
    assert status["known"] is False


def test_a_completed_deployment_still_reports_a_later_failed_attempt(
    monkeypatch, _isolated_cloud_home, capsys
):
    """The box is up on the old release while the operator thinks they shipped a new one."""
    _write_deploy_record(_isolated_cloud_home)
    _write_attempt(_isolated_cloud_home, version="3.1.0", phase="health")
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})

    code = cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    out = capsys.readouterr().out
    assert code == 0
    assert "DEPLOYED" in out, "a real deployment must not be downgraded by a later failure"
    assert "Last deploy attempt" in out
    assert "3.1.0" in out


def test_destroy_clears_the_attempt_record(stubbed_deploy, _isolated_cloud_home, monkeypatch):
    """A torn-down instance must not keep reporting NOT COMPLETED."""
    _write_attempt(_isolated_cloud_home)
    monkeypatch.setattr(cloud_infra, "destroy_infra", lambda args: {"action": "destroy"})
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"running": False})

    cloud_deploy.destroy(_args(yes=True))

    assert cloud_deploy.load_deploy_attempt() == {}


def test_status_command_probes_health_by_default(monkeypatch, _isolated_cloud_home, capsys):
    _write_deploy_record(_isolated_cloud_home)
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(
        cloud_deploy, "tunnel_status", lambda: {"running": True, "pid": 7, "urls": {}}
    )
    monkeypatch.setattr(cloud_deploy, "_probe", lambda url, timeout: 200)

    cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=False))

    assert "healthy" in capsys.readouterr().out


def test_status_command_no_probe_makes_no_network_call(monkeypatch, _isolated_cloud_home, capsys):
    _write_deploy_record(_isolated_cloud_home)
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": True})
    monkeypatch.setattr(
        cloud_deploy, "tunnel_status", lambda: {"running": True, "pid": 7, "urls": {}}
    )
    monkeypatch.setattr(
        cloud_deploy, "_probe", lambda url, timeout: pytest.fail("probed despite --no-probe")
    )

    cloud_deploy.deploy_command(_args(cloud_cmd="status", json=False, no_probe=True))

    assert "not checked" in capsys.readouterr().out


def test_deploy_summary_points_at_the_status_command(stubbed_deploy, capsys):
    """The information has to be recoverable once the scrollback is gone."""
    cloud_deploy.deploy_command(_args(cloud_cmd="deploy"))

    out = capsys.readouterr().out

    assert "nyxgpt cloud status" in out
    assert "nyxgpt cloud ops status" in out


def test_deploy_status_flag_still_emits_json_and_names_its_replacement(monkeypatch, capsys):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"provisioned": False})

    code = cloud_deploy.deploy_command(_args(cloud_cmd="deploy", status=True))

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["deployed"] is False
    assert "nyxgpt cloud status" in captured.err


# --- `nyxgpt cloud ops` (wrapped remote inspection, #3813) ----------------


def test_remote_ops_inspections_are_read_only_wrapped_nyxgpt_commands():
    # `session-backend` is a read for the same reason `status` is: `nyxgpt ops
    # session-backend` with no backend argument reports the value in force and
    # writes nothing. It is in this allowlist *without* an argument, which is
    # what keeps it a read -- appending one here would make it a write.
    for remote in cloud_deploy.REMOTE_OPS_COMMANDS.values():
        assert remote.split()[-1] in ("status", "doctor", "session-backend")
        assert "docker" not in remote

    assert cloud_deploy.REMOTE_OPS_COMMANDS["session-backend"] == "ops session-backend"


def test_remote_ops_runs_the_instances_own_command_over_ssh(monkeypatch):
    seen = {}

    def fake_remote(target, command, **kwargs):
        seen["command"] = command
        seen["stream"] = kwargs.get("stream")
        return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)

    code = cloud_deploy.remote_ops(cloud_deploy.DeployTarget(host="h"), "status")

    assert code == 0
    assert seen["command"].endswith("ops status")
    assert "/.nyxGPT/venv/bin/nyxgpt" in seen["command"]
    # Streamed, so a long remote status is not a silent wait -- and never a
    # raw `docker compose` typed by the operator.
    assert seen["stream"] is True
    assert "docker" not in seen["command"]


def test_remote_ops_returns_the_inspections_own_verdict(monkeypatch):
    """`ops doctor` exiting non-zero is a reportable answer, not a failure."""
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(["ssh"], 3, stdout="", stderr="unhealthy"),
    )

    assert cloud_deploy.remote_ops(cloud_deploy.DeployTarget(host="h"), "doctor") == 3


def test_remote_ops_reports_an_unreachable_instance_with_the_allow_ip_fix(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            ["ssh"], 255, stdout="", stderr="ssh: connect to host: Connection timed out"
        ),
    )

    with pytest.raises(CloudCommandError, match="nyxgpt cloud allow-ip"):
        cloud_deploy.remote_ops(cloud_deploy.DeployTarget(host="h"), "status")


def test_remote_ops_reports_an_instance_without_nyxgpt_installed(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(["ssh"], 127, stdout="", stderr="not found"),
    )

    with pytest.raises(CloudCommandError, match="nyxgpt cloud deploy"):
        cloud_deploy.remote_ops(cloud_deploy.DeployTarget(host="h"), "status")


def test_ops_command_names_the_instance_and_the_remote_command(
    monkeypatch, _isolated_cloud_home, capsys
):
    _write_cloud_state(_isolated_cloud_home)
    _write_deploy_record(_isolated_cloud_home)
    seen = {}

    def fake_remote(target, command, **kwargs):
        seen["identity"] = target.identity_file
        return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)

    code = cloud_deploy.deploy_command(_args(cloud_cmd="ops", inspection="self-heal"))

    out = capsys.readouterr().out
    assert code == 0
    assert "ec2-user@198.51.100.10" in out
    assert "nyxgpt self-heal status" in out
    # The recorded key is used without the operator re-passing it.
    assert seen["identity"] == "/keys/nyxgpt.pem"


def test_ops_command_defaults_to_the_container_state_inspection(
    monkeypatch, _isolated_cloud_home, capsys
):
    _write_cloud_state(_isolated_cloud_home)
    seen = {}

    def fake_remote(target, command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_remote)

    cloud_deploy.deploy_command(_args(cloud_cmd="ops", inspection=None))

    assert seen["command"].endswith("ops status")


# --- Session storage backend (#3865) --------------------------------------


def test_a_deploy_defaults_to_the_cassandra_session_backend():
    """The gap this closes: nothing in the cloud path used to set it at all.

    The instance kept `example.config.ini`'s back-compat `file` value, so
    chats became JSON on ephemeral instance disk that no other deployment
    mode pointed at the same Cassandra could see.
    """
    plan = cloud_deploy.resolve_plan(_args(version="3.0.0"))

    assert plan.session_backend == "cassandra"


def test_the_provision_script_applies_the_backend_with_the_wrapped_command():
    plan = cloud_deploy.resolve_plan(_args(version="3.0.0"))

    script = cloud_deploy.render_provision_script(plan)

    assert "__SESSION_BACKEND__" not in script
    assert 'NYXGPT_SESSION_BACKEND_CHOICE="cassandra"' in script
    assert '"$NYXGPT" ops session-backend "$NYXGPT_SESSION_BACKEND_CHOICE"' in script
    # Before `ops install`: `_generate_compose_config` derives the
    # containerized config from the native one, so a backend set afterwards
    # would leave the derived file on the old value until the next reconcile.
    assert script.index("ops session-backend") < script.index("run_nyxgpt ops install")


def test_file_backed_is_available_for_a_deliberately_single_instance_deploy():
    plan = cloud_deploy.resolve_plan(_args(version="3.0.0", session_backend="file"))

    assert plan.session_backend == "file"
    assert 'NYXGPT_SESSION_BACKEND_CHOICE="file"' in cloud_deploy.render_provision_script(plan)


def test_an_unknown_backend_is_refused_before_anything_is_provisioned():
    with pytest.raises(CloudCommandError, match="session backend"):
        cloud_deploy.resolve_plan(_args(version="3.0.0", session_backend="postgres"))


def test_the_backend_carries_forward_from_the_last_deploy():
    """A re-deploy must not silently move an instance's sessions back to files.

    Same carry-forward as --version/--ssh-user/--identity-file: omitting the
    flag means "as before", not "back to the default".
    """
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"version": "3.0.0", "session_backend": "file"}
    )

    plan = cloud_deploy.resolve_plan(_args(version=None))

    assert plan.session_backend == "file"


def test_an_explicit_flag_beats_the_recorded_choice():
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"version": "3.0.0", "session_backend": "file"}
    )

    plan = cloud_deploy.resolve_plan(_args(version=None, session_backend="cassandra"))

    assert plan.session_backend == "cassandra"


def test_deploy_status_reports_where_this_deployments_sessions_live():
    """Observable, not operable (Definition of Done): the dashboard reads this."""
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE,
        {"version": "3.0.0", "host": "1.2.3.4", "session_backend": "cassandra"},
    )

    assert cloud_deploy.deploy_status()["session_backend"] == "cassandra"


def test_a_pre_flag_deploy_record_reports_nothing_rather_than_guessing_file():
    """ "Not recorded" and "file" are different claims, and only one is true here."""
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"version": "3.0.0", "host": "1.2.3.4"}
    )

    assert cloud_deploy.deploy_status()["session_backend"] == ""


# --- Target OS dispatch (#3867) ------------------------------------------
#
# Before #3867 `deploy` drove exactly one bootstrap -- the Linux SSH script --
# and the only route to an EC2 Mac was to render `nyxgpt cloud user-data --os
# macos` and paste it into an AWS console instance launch by hand, which is
# the raw-operations flow CLAUDE.md's Operational Command Wrapping requirement
# forbids. These assert the dispatch, that the macOS bootstrap now travels the
# same wrapped SSH path, that a Mac with nowhere to run fails *before* the
# substrate is touched, and -- the acceptance criterion that matters most --
# that the Linux path is byte-for-byte what it was.


@pytest.mark.parametrize(
    "instance_type",
    ["mac1.metal", "mac2.metal", "mac2-m2.metal", "mac2-m2pro.metal", "mac2-m1ultra.metal"],
)
def test_every_ec2_mac_instance_type_resolves_to_the_macos_bootstrap(instance_type):
    assert cloud_deploy.instance_type_os_family(instance_type) == "macos"


@pytest.mark.parametrize(
    "instance_type",
    # Including the two traps: a non-Mac `.metal` type, and a type whose name
    # merely starts with "mac".
    ["m5.large", "", "c7g.metal", "m5.metal", "mac.large", "machine.large"],
)
def test_everything_else_resolves_to_the_linux_bootstrap(instance_type):
    assert cloud_deploy.instance_type_os_family(instance_type) == "linux"


def test_auto_reads_the_instance_type_the_substrate_is_configured_for(monkeypatch):
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {"instance_type": "mac2.metal"})

    assert cloud_deploy.resolve_os_family(_args(os_family="auto")) == "macos"


def test_this_runs_instance_type_flag_wins_over_the_remembered_one(monkeypatch):
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {"instance_type": "m5.large"})

    assert (
        cloud_deploy.resolve_os_family(_args(os_family="auto", instance_type="mac2-m2.metal"))
        == "macos"
    )


def test_an_explicit_os_flag_overrides_the_instance_type(monkeypatch):
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {"instance_type": "mac2.metal"})

    assert cloud_deploy.resolve_os_family(_args(os_family="linux")) == "linux"


def test_redeploying_a_recorded_host_keeps_that_hosts_target_os(monkeypatch):
    """An operator-supplied Mac has no instance type here to read: the substrate
    does not manage it, so `infra.json` still says `m5.large`. Without this, a
    re-deploy of a Mac would drive the *Linux* bootstrap at it."""
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {"instance_type": "m5.large"})
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"host": "198.51.100.77", "os_family": "macos"}
    )

    assert cloud_deploy.resolve_os_family(_args(os_family="auto", host="198.51.100.77")) == "macos"


def test_a_different_host_does_not_inherit_the_recorded_target_os(monkeypatch):
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {"instance_type": "m5.large"})
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"host": "198.51.100.77", "os_family": "macos"}
    )

    assert cloud_deploy.resolve_os_family(_args(os_family="auto", host="203.0.113.9")) == "linux"


def test_an_unsupported_os_is_refused_by_name():
    with pytest.raises(CloudCommandError, match="not a supported target OS"):
        cloud_deploy.resolve_os_family(_args(os_family="windows"))


def test_the_macos_deploy_delivers_the_same_bootstrap_cloud_user_data_renders():
    """One renderer, not a second copy of it: `cloud deploy --os macos` now
    delivers the script that used to be pasted into the console by hand."""
    from nyxgpt import cloud_provision

    plan = cloud_deploy.resolve_plan(_args(os_family="macos"))

    assert cloud_deploy.render_provision_script(plan) == cloud_provision.render_user_data(
        "macos", plan.version, plan.session_backend
    )


def test_the_macos_bootstrap_installs_from_the_remote_tap_and_never_clones():
    script = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(os_family="macos"))
    )

    assert "tap dkblinux98/nyxgpt" in script
    assert "install nyxgpt-api nyxgpt-web" in script
    assert "services start nyxgpt-api" in script
    # Only executable lines: the header comments *mention* `git clone` to
    # document that the script deliberately never runs one.
    executable = [
        line for line in script.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    for forbidden in ("git clone", "git://", "github.com/dkblinux98/nyxGPT.git"):
        assert not any(forbidden in line for line in executable)


def test_the_linux_bootstrap_is_untouched_by_the_dispatch():
    """The acceptance criterion the rest of this change must not cost: `--os
    linux` renders exactly the script a native Linux deploy always rendered.

    Later placeholders (#3956's substrate sections, #3950's install block)
    are substituted here rather than left unresolved because the template
    gained them after #3867: a *native, non-dev* Linux plan must still
    produce the script it always did, which is the same claim this test was
    written to make about the `--os` dispatch.
    """
    plan = cloud_deploy.resolve_plan(_args(os_family="linux"))
    # Both substitutions below stand in for blocks the shared template gained
    # after #3867: the substrate sections (#3956) and the artifact install
    # block plus empty `ops install` flags (#3950). Substituting them keeps
    # this asserting what it was written to assert -- that the OS dispatch
    # changes nothing about the Linux script -- rather than re-asserting that
    # `--kubernetes` or `--dev` exist, which their own files cover.
    assert plan.kubernetes is False
    assert plan.dev is False
    expected = (
        cloud_deploy.PROVISION_SCRIPT_TEMPLATE.replace(
            "__NYXGPT_INSTALL__", cloud_deploy.ARTIFACT_INSTALL_BLOCK
        )
        .replace("__REMOTE_SOURCE__", cloud_deploy.REMOTE_SOURCE_DIR)
        .replace("__VERSION__", plan.version)
        .replace("__PROFILES__", ",".join(plan.profiles))
        .replace("__SESSION_BACKEND__", plan.session_backend)
        .replace("__NODE_SECTION__", cloud_deploy.NATIVE_NODE_SECTION)
        .replace("__LLM_RUNTIME_SECTION__", cloud_deploy.NATIVE_LLM_RUNTIME_SECTION)
        .replace("__STACK_BRINGUP_SECTION__", cloud_deploy.NATIVE_STACK_BRINGUP_SECTION)
        # After the section, not before: #3956 lifted the `ops install` call
        # out of the template and into the bringup sections, so the flags
        # placeholder now arrives with the section rather than ahead of it.
        .replace("__OPS_INSTALL_FLAGS__", "")
    )

    assert cloud_deploy.render_provision_script(plan) == expected
    assert cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args())) == expected
    # No placeholder survived the render -- the assertion above would pass if
    # both sides shared an unsubstituted marker.
    assert "__" not in expected.replace("__VERSION__", "")


def test_the_macos_bootstrap_is_elevated_and_told_which_user_to_install_for():
    """It refuses to run as anyone but root (ec2-macos-init hands user-data to
    root) and drops to the login user with `sudo -u` for every brew call."""
    plan = cloud_deploy.resolve_plan(_args(os_family="macos", ssh_user="admin"))

    assert cloud_deploy.provision_remote_command(plan) == (
        "sudo -n NYXGPT_TARGET_USER=admin bash -s"
    )


def test_the_linux_bootstrap_is_still_piped_into_a_plain_shell():
    assert cloud_deploy.provision_remote_command(cloud_deploy.resolve_plan(_args())) == "bash -s"


def test_a_macos_plan_defaults_sessions_to_file_and_a_linux_plan_to_cassandra():
    """Nothing on the Mac provisions a Cassandra -- its bootstrap does not run
    `ops install` -- so defaulting it to `cassandra` would point the API at a
    database that is not on the machine."""
    assert cloud_deploy.resolve_plan(_args(os_family="macos")).session_backend == "file"
    assert cloud_deploy.resolve_plan(_args(os_family="linux")).session_backend == "cassandra"


def test_the_recorded_session_backend_does_not_carry_across_target_os_families():
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE,
        {"version": "3.0.0", "session_backend": "cassandra", "os_family": "linux"},
    )

    assert cloud_deploy.resolve_plan(_args(os_family="macos")).session_backend == "file"
    assert cloud_deploy.resolve_plan(_args(os_family="linux")).session_backend == "cassandra"


def test_a_macos_plan_enables_no_observability_profiles():
    """Its bootstrap installs none, so recording them would make `tunnel`
    forward ports nothing listens on and promise URLs that never answer."""
    assert cloud_deploy.resolve_plan(_args(os_family="macos")).profiles == []


def test_provisioning_a_mac_does_not_claim_a_self_heal_watchdog(monkeypatch):
    seen = _fake_provision_run(monkeypatch, 0, [])
    target = cloud_deploy.DeployTarget(host="198.51.100.10")

    result = cloud_deploy.provision_instance(
        target, cloud_deploy.resolve_plan(_args(os_family="macos"))
    )

    assert result["os_family"] == "macos"
    assert result["self_heal_enabled"] is False
    assert seen["remote_command"].startswith("sudo -n ")


def test_a_mac_deploy_with_no_host_offers_to_allocate_instead_of_refusing(
    monkeypatch, stubbed_deploy, _isolated_cloud_home
):
    """#3995 replaced the refusal at `--os macos` with no `--host`. What used to
    raise now allocates -- after a typed confirmation -- and never applies the
    Linux substrate, which would bill for a box nothing deploys to."""
    allocated = {}

    def _fake_allocate(args, *, assume_yes=False):
        allocated["assume_yes"] = assume_yes
        return {
            "allocated": True,
            "host_id": "h-0abc",
            "instance_id": "i-0mac",
            "public_ip": "203.0.113.7",
            "security_group_id": "sg-0mac",
            "region": "us-east-1",
            "owner_ip_cidr": "198.51.100.5/32",
            "release_at": "2026-08-23T18:30:00+00:00",
        }

    monkeypatch.setattr(cloud_mac, "allocate", _fake_allocate)

    result = cloud_deploy.deploy(_args(os_family="macos", no_tunnel=True, yes=True))

    # The Linux substrate is still never applied for a Mac.
    assert stubbed_deploy == ["ssh", "provision"]
    assert allocated["assume_yes"] is True
    step = next(s for s in result["steps"] if s["step"] == "mac-host")
    assert step["host_id"] == "h-0abc"
    assert result["target"]["host"] == "203.0.113.7"
    assert result["target"]["instance_id"] == "i-0mac"
    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["mac_host_id"] == "h-0abc"
    assert recorded["os_family"] == "macos"


def test_a_supplied_mac_is_provisioned_without_applying_the_linux_substrate(
    stubbed_deploy, _isolated_cloud_home
):
    result = cloud_deploy.deploy(_args(os_family="macos", host="198.51.100.77", no_tunnel=True))

    # No `apply`: reconciling the substrate would bill for a Linux instance
    # that nothing then deploys to.
    assert stubbed_deploy == ["ssh", "provision"]
    infra_step = next(s for s in result["steps"] if s["step"] == "infra")
    assert infra_step["skipped"] is True
    assert result["target"]["host"] == "198.51.100.77"
    # Not the unrelated Linux instance's ids from the substrate handoff.
    assert result["target"]["instance_id"] == ""

    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["os_family"] == "macos"
    assert recorded["host"] == "198.51.100.77"


def test_a_linux_deploy_records_its_target_os_too(stubbed_deploy, _isolated_cloud_home):
    cloud_deploy.deploy(_args())

    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["os_family"] == "linux"


def test_deploy_status_reports_the_target_os_family():
    """Observable, not operable (Definition of Done): the Infrastructure page
    reads this, because nothing else on it distinguishes the two bootstraps."""
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE,
        {"version": "3.0.0", "host": "1.2.3.4", "os_family": "macos"},
    )

    assert cloud_deploy.deploy_status()["os_family"] == "macos"


def test_a_pre_flag_deploy_record_reports_no_target_os_rather_than_guessing_linux():
    cloud_deploy._write_json(
        cloud_deploy.DEPLOY_STATE_FILE, {"version": "3.0.0", "host": "1.2.3.4"}
    )

    assert cloud_deploy.deploy_status()["os_family"] == ""


def test_destroying_after_a_mac_deploy_says_the_mac_is_still_running(
    monkeypatch, _isolated_cloud_home
):
    """`destroy` tears down the substrate, and an operator-supplied EC2 Mac was
    never in it. Saying only "Cloud deployment destroyed" would imply a Mac --
    and the Dedicated Host under it -- stopped billing when it did not."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.77", "version": "3.0.0", "os_family": "macos"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(
        cloud_infra, "destroy_infra", lambda args: {"settings": {"aws_region": "us-east-1"}}
    )

    result = cloud_deploy.destroy(_args(yes=True))

    assert result["unmanaged_target"] == "198.51.100.77"
    assert "left running" in cloud_deploy.deploy_history()[-1]["detail"]


def test_destroying_a_mac_nyxgpt_allocated_does_not_report_it_as_unmanaged(
    monkeypatch, _isolated_cloud_home
):
    """The `--host` case above says "nyxGPT does not manage it". A Mac nyxGPT
    allocated itself (#3995) is the opposite claim, and printing both would
    tell an operator to go release a host that is already scheduled."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "203.0.113.7", "version": "3.0.0", "os_family": "macos"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(
        cloud_mac,
        "teardown",
        lambda args: {
            "managed": True,
            "host_id": "h-0abc",
            "release_at": "2026-08-23T18:30:00+00:00",
            "instance_terminated": True,
            "release_scheduled": True,
            "schedule": {"slack_channel": "C0ABH478QC8"},
            "errors": [],
        },
    )
    monkeypatch.setattr(cloud_mac, "load_mac_record", lambda: {"mac_host_id": "h-0abc"})
    monkeypatch.setattr(
        cloud_infra, "destroy_infra", lambda args: {"settings": {"aws_region": "us-east-1"}}
    )

    result = cloud_deploy.destroy(_args(yes=True))

    assert result["unmanaged_target"] == ""
    assert result["mac"]["release_scheduled"] is True
    detail = cloud_deploy.deploy_history()[-1]["detail"]
    assert "h-0abc" in detail
    assert "2026-08-23T18:30:00+00:00" in detail


def test_a_mac_only_teardown_succeeds_with_no_substrate_state_to_destroy(
    monkeypatch, _isolated_cloud_home
):
    """A macOS deploy never applies the Linux substrate, so `destroy_infra`'s
    "nothing to destroy" error would otherwise become the whole teardown's exit
    code -- after the Mac had already come down successfully."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "203.0.113.7", "version": "3.0.0", "os_family": "macos"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(cloud_mac, "load_mac_record", lambda: {"mac_host_id": "h-0abc"})
    monkeypatch.setattr(
        cloud_mac,
        "teardown",
        lambda args: {
            "managed": True,
            "host_id": "h-0abc",
            "region": "us-east-1",
            "release_at": "2026-08-23T18:30:00+00:00",
            "instance_terminated": True,
            "release_scheduled": True,
            "schedule": {},
            "errors": [],
        },
    )

    def _never(_args):
        raise AssertionError("the substrate has no state and must not be destroyed")

    monkeypatch.setattr(cloud_infra, "destroy_infra", _never)
    monkeypatch.setattr(cloud_infra, "TFSTATE_FILE", _isolated_cloud_home / "terraform.tfstate")

    result = cloud_deploy.destroy(_args(yes=True))

    assert result["mac"]["host_id"] == "h-0abc"
    assert not (_isolated_cloud_home / "deploy.json").exists()


def test_status_surfaces_a_pending_host_release_even_with_no_deployment_left(monkeypatch):
    """The ordinary end state of a macOS teardown: no deployment, one Dedicated
    Host still billing until tomorrow. A status that reported only the
    deployment would make the single remaining charge invisible at exactly the
    moment it is all that is left."""
    monkeypatch.setattr(
        cloud_mac,
        "pending_release",
        lambda: {"host_id": "h-0abc", "release_at": "2026-08-23T18:30:00+00:00"},
    )

    status = cloud_deploy.deploy_status()

    assert status["known"] is False
    assert status["mac_host"]["host_id"] == "h-0abc"


def test_the_status_summary_prints_the_pending_host_when_nothing_is_deployed(monkeypatch, capsys):
    monkeypatch.setattr(
        cloud_mac,
        "pending_release",
        lambda: {
            "host_id": "h-0abc",
            "instance_type": "mac2.metal",
            "region": "us-east-1",
            "availability_zone": "us-east-1c",
            "allocated_at": "2026-08-22T18:00:00+00:00",
            "release_at": "2026-08-23T18:30:00+00:00",
            "release_scheduled": True,
            "hourly_rate": 0.65,
            "accrued_cost": 15.6,
            "releasable_now": False,
            "billing": True,
        },
    )

    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())

    out = capsys.readouterr().out
    assert "h-0abc" in out
    assert "2026-08-23T18:30:00+00:00" in out
    assert "$15.60" in out
    assert "still billing" in out


def test_a_linux_deployment_prints_no_dedicated_host_block(capsys):
    """A permanent "no Dedicated Host" row on every Linux deployment would be
    noise, and noise is how the row that matters gets skimmed past."""
    cloud_deploy._print_pending_mac_host({})

    assert capsys.readouterr().out == ""


def test_destroying_after_a_linux_deploy_has_nothing_left_over_to_report(
    monkeypatch, _isolated_cloud_home
):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"host": "198.51.100.10", "version": "3.0.0", "os_family": "linux"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(
        cloud_infra, "destroy_infra", lambda args: {"settings": {"aws_region": "us-east-1"}}
    )

    assert cloud_deploy.destroy(_args(yes=True))["unmanaged_target"] == ""
