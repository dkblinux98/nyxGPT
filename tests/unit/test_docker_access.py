"""The shared Docker-socket hop (`nyxgpt.docker_access`, #4022).

`ops.py` and `self_heal.py` each grew their own copy of this probe, and the
copies diverged on the answer they exist to give identically -- self-heal's
retried a denied read through `sg docker` and reported "unknown", while
`ops._docker_container_state` did neither and rendered a running Cassandra as
`absent`. These tests pin the mechanism itself; the two callers' tests pin
what each does with it.
"""

from __future__ import annotations

import subprocess

import pytest

from nyxgpt import docker_access


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_sg_hop_shell_quotes_the_argv():
    """`sg` takes a shell *string*, not an argv, so the argv has to be rendered
    into one -- with `shlex.join`, which is the sanitizer barrier for this call
    (CodeQL py/command-line-injection) and also what keeps a compose file path
    containing a space from becoming two arguments."""
    argv = docker_access.hop_argv(["docker", "compose", "-f", "/a b/c.yml", "ps"], "sg")

    assert argv[:3] == ["sg", "docker", "-c"]
    assert "'/a b/c.yml'" in argv[3]


@pytest.mark.unit
def test_sudo_hop_opts_out_of_env_reset():
    """Plain `sudo` hands the Docker CLI `HOME=/root` on Amazon Linux/RHEL,
    which would relocate every Compose bind mount. `--preserve-env` is not
    optional here."""
    assert docker_access.hop_argv(["docker", "ps"], "sudo") == [
        "sudo",
        "-n",
        "--preserve-env",
        "docker",
        "ps",
    ]


@pytest.mark.unit
def test_unknown_or_absent_hop_leaves_the_argv_alone():
    assert docker_access.hop_argv(["docker", "ps"], None) == ["docker", "ps"]
    assert docker_access.hop_argv(["docker", "ps"], "doas") == ["docker", "ps"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "permission denied while trying to connect to the Docker daemon socket",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        "permission denied while trying to connect to the docker API at unix://...",
        "Is the docker daemon running?",
    ],
)
def test_socket_access_failures_are_recognised(stderr):
    """Docker's CLI and the Compose plugin word it differently, and the owner's
    rc12 and rc13 instances produced one each."""
    assert docker_access.looks_like_access_failure(_cp(1, stderr=stderr)) is True


@pytest.mark.unit
def test_a_docker_refusal_is_not_a_socket_failure():
    """An image pull that 404'd must not drag a hop probe along behind it on
    every watchdog pass."""
    assert (
        docker_access.looks_like_access_failure(
            _cp(1, stderr="Error response from daemon: manifest unknown")
        )
        is False
    )


def _hop(runner, *, candidates=("sg",), which=lambda name: f"/usr/bin/{name}", **kw):
    return docker_access.DockerSocketHop(runner=runner, which=which, candidates=candidates, **kw)


def _denying_runner(seen, *, hop_works=True, hop_preserves_env=True, working_hops=("sg", "sudo")):
    """Bare docker calls are denied; a hop in `working_hops` gets through."""

    def _run(cmd, *, timeout, expected=False, env=None):
        seen.append(list(cmd))
        hop = "sg" if cmd[:2] == ["sg", "docker"] else "sudo" if cmd[:1] == ["sudo"] else None
        if hop is None:
            return _cp(1, stderr="permission denied while trying to connect to the Docker daemon")
        if not hop_works or hop not in working_hops:
            return _cp(1, stderr=f"{hop}: refused")
        if "printf" in cmd[-1]:
            if not hop_preserves_env:
                return _cp(0, stdout="/root\n\n")
            import os

            return _cp(
                0,
                stdout=f"{os.environ.get('HOME', '')}\n{(env or {}).get(docker_access.ENV_PROBE_VAR, '')}\n",
            )
        if "docker info" in cmd[-1]:
            return _cp(0, stdout="27.1.1\n")
        return _cp(0, stdout="running\n")

    return _run


@pytest.mark.unit
def test_a_healthy_host_pays_nothing():
    """The bare command is tried first and returned untouched when it works, so
    no probe runs on a machine that never had this problem."""
    seen: list[list[str]] = []

    def _run(cmd, *, timeout, expected=False, env=None):
        seen.append(list(cmd))
        return _cp(0, stdout="running\n")

    hop = _hop(_run)
    assert hop.run(["docker", "ps"], timeout=5.0).stdout == "running\n"
    assert seen == [["docker", "ps"]]
    assert hop.active is None


@pytest.mark.unit
def test_a_denied_call_is_retried_through_the_hop_and_the_hop_is_then_reused():
    """First principle 1: the watchdog runs every ~15s. Re-deriving the hop on
    every call would be three subprocesses where one is needed."""
    seen: list[list[str]] = []
    hop = _hop(_denying_runner(seen))

    assert hop.run(["docker", "ps"], timeout=5.0).returncode == 0
    assert hop.active == "sg"
    first_pass = len(seen)

    hop.run(["docker", "ps"], timeout=5.0)
    assert len(seen) == first_pass + 1
    assert seen[-1][:2] == ["sg", "docker"]


@pytest.mark.unit
def test_a_hop_that_resets_the_environment_is_refused():
    """`${HOME}` is interpolated into every Compose bind-mount source and
    `docker compose exec -e VAR` forwards secrets out of the environment. A hop
    that drops either is worse than no hop: the failure would be silent."""
    seen: list[list[str]] = []
    hop = _hop(_denying_runner(seen, hop_preserves_env=False))

    cp = hop.run(["docker", "ps"], timeout=5.0)

    assert hop.active is None
    assert cp.returncode != 0


@pytest.mark.unit
def test_a_negative_answer_expires_but_is_not_re_probed_immediately():
    """An operator who runs `usermod -aG docker` while the API is up must not
    have to restart it for the repair to take -- but the watchdog must not
    re-probe every 15s either."""
    seen: list[list[str]] = []
    hop = _hop(_denying_runner(seen, hop_works=False), recheck_seconds=3600.0)

    hop.run(["docker", "ps"], timeout=5.0)
    after_first = len(seen)
    hop.run(["docker", "ps"], timeout=5.0)

    # Second call: the bare attempt only. No hop probe inside the window.
    assert len(seen) == after_first + 1
    assert seen[-1][:1] == ["docker"]

    hop.reset()
    hop.run(["docker", "ps"], timeout=5.0)
    assert len(seen) > after_first + 2


@pytest.mark.unit
def test_sudo_is_reached_only_by_asking_for_it():
    """The module default is `sg` only, because the API process is where most
    of these calls happen. `ops._enable_docker_socket_hop` opts into `sudo` at
    its own call site; nothing inherits it."""
    # A host where the group change itself was never made: `sg` cannot help,
    # `sudo` could -- and the sg-only default must still refuse to use it.
    seen: list[list[str]] = []
    hop = _hop(_denying_runner(seen, working_hops=("sudo",)))

    assert hop.resolve() is None
    assert not any(cmd[:1] == ["sudo"] for cmd in seen), seen

    hop.reset()
    seen.clear()
    assert hop.adopt(("sg", "sudo")) == "sudo"
    assert any(cmd[:1] == ["sudo"] for cmd in seen), seen


@pytest.mark.unit
def test_apply_only_rewrites_a_bare_docker_argv():
    """An already-privileged argv (`_privileged_run` puts `sudo` in front) must
    not be double-wrapped, and no non-Docker command may be touched."""
    hop = _hop(lambda cmd, *, timeout, expected=False, env=None: _cp(0))
    hop.force("sg")

    assert hop.apply(["docker", "ps"]) == ["sg", "docker", "-c", "docker ps"]
    assert hop.apply(["sudo", "-n", "docker", "ps"]) == ["sudo", "-n", "docker", "ps"]
    assert hop.apply(["systemctl", "status"]) == ["systemctl", "status"]
