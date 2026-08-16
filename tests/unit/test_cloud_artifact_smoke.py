"""Unit tests for the containerized cloud artifact smoke (#3784).

Nothing here runs Docker: `cloud_artifact_smoke._run` is replaced with a
recorder that answers each `docker ...` invocation from a script the test
writes. That leaves exactly the logic worth unit-testing -- the parts that
decide what a run *means*:

* the preflight refuses a machine that is not bare (the anti-"green by luck"
  gate; a regression here would let the smoke pass on a machine that cannot
  reproduce the defects it exists to catch),
* fault injection inverts the verdict, so `--inject` passing means the smoke
  saw the fault rather than that the bootstrap happened to work,
* a failure is classified into the defect class it belongs to,
* the container is removed on every exit path, and
* a run always leaves a record for `--status`, the API and the dashboard.

The behaviour these tests structurally cannot reach -- does the install
actually work on Amazon Linux 2023 -- is what the smoke itself is for, and is
executed by `.github/workflows/cloud-artifact-smoke.yml` (D-006).
"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from nyxgpt import cloud_artifact_smoke as smoke


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


class FakeDocker:
    """Answers `_run` calls from a table of (match, result) rules.

    The default table is a machine that is bare, boots, bootstraps and serves
    -- i.e. a passing run -- so each test only has to describe its own
    deviation from that.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.rules: list[tuple[str, subprocess.CompletedProcess[str]]] = []

    def answer(self, needle: str, result: subprocess.CompletedProcess[str]) -> None:
        # Newest rule wins, so a test can override a default.
        self.rules.insert(0, (needle, result))

    def __call__(
        self, command: list[str], *, timeout: float, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if input_text:
            self.inputs.append(input_text)
        joined = " ".join(command)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return _completed()


@pytest.fixture()
def fake_docker(monkeypatch, tmp_path):
    """Install a scripted `_run`, and keep the recorded result out of $HOME."""
    fake = FakeDocker()
    monkeypatch.setattr(smoke, "_run", fake)
    monkeypatch.setattr(smoke, "RESULT_FILE", tmp_path / "artifact-smoke.json")
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    # A bare AL2023 machine: python3 is 3.9 and none of the tools the
    # bootstrap has to provision are present.
    fake.answer("docker info", _completed(0, "28.0.4"))
    fake.answer("python3 -V", _completed(0, "Python 3.9.25"))
    for tool in ("node", "npm", "docker", "git", "nyxgpt"):
        fake.answer(f"command -v {tool}", _completed(1))
    fake.answer("systemctl is-system-running", _completed(0, "degraded"))
    fake.answer(
        "import nyxgpt",
        _completed(
            0, "/home/ec2-user/.nyxGPT/opt/x/lib/python3.11/site-packages/nyxgpt/__init__.py"
        ),
    )
    fake.answer("http_code", _completed(0, "200"))
    return fake


def _args(**overrides) -> argparse.Namespace:
    namespace = argparse.Namespace(
        container=True,
        version="3.0.0rc9",
        wheel=None,
        image=None,
        inject=[],
        keep=False,
        bootstrap_timeout=None,
        build_timeout=None,
        health_timeout=1.0,
        json=True,
        status=False,
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --- The happy path -----------------------------------------------------


def test_passing_run_records_its_verdict_and_removes_the_container(fake_docker, monkeypatch):
    result = smoke.run_container_smoke(_args())

    assert result["passed"], result["failure"]
    assert [step["step"] for step in result["steps"]] == [
        "docker",
        "render",
        "build",
        "boot",
        "preflight",
        "artifact",
        "bootstrap",
        "repo-less",
        "services",
        "teardown",
    ]
    assert result["teardown"]["removed"] is True
    # The gaps travel with the verdict: a green run that does not say what it
    # did not cover is how "the install works" becomes "the cloud path works".
    assert result["coverage_gaps"] == list(smoke.COVERAGE_GAPS)

    smoke.record_result(result)
    assert smoke.last_result()["passed"] is True


def test_the_bootstrap_that_runs_is_the_real_rendered_user_data(fake_docker):
    """The container executes the packaged EC2 template, not a copy of it."""
    smoke.run_container_smoke(_args())

    assert fake_docker.inputs, "the bootstrap was never written into the container"
    written = fake_docker.inputs[0]
    assert "ec2-user-data-linux" in written
    assert 'PIP_SPEC="nyxgpt==${NYXGPT_VERSION}"' in written
    assert 'NYXGPT_VERSION="3.0.0rc9"' in written


# --- The anti-"green by luck" gate --------------------------------------


@pytest.mark.parametrize(
    ("needle", "answer"),
    [
        ("python3 -V", _completed(0, "Python 3.12.1")),
        ("command -v node", _completed(0, "/usr/bin/node")),
        ("command -v docker", _completed(0, "/usr/bin/docker")),
        ("command -v git", _completed(0, "/usr/bin/git")),
    ],
)
def test_a_machine_that_is_not_bare_fails_the_run(fake_docker, needle, answer):
    """A pre-provisioned machine cannot reproduce the defects, so it is not evidence."""
    fake_docker.answer(needle, answer)

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "not a bare Amazon Linux 2023 machine" in result["failure"]
    # It still cleans up: a failed premise is not a reason to leave a
    # privileged container running.
    assert result["teardown"]["removed"] is True


# --- Fault injection ----------------------------------------------------


def test_injected_fault_that_the_smoke_catches_is_a_pass(fake_docker):
    fake_docker.answer(
        "bash /opt/nyxgpt-smoke/nyxgpt-bootstrap.sh",
        _completed(1, "ERROR: Package requires a different Python: 3.9.25 not in '>=3.11'"),
    )

    result = smoke.run_container_smoke(_args(inject=["old-python"]))

    assert result["passed"]
    assert result["expected_failure"] is True
    # The verdict names the class it required, not just "it failed".
    assert "expected interpreter-selection failure" in result["detail"]
    assert "interpreter selection" in result["failure_class"]


def test_injected_fault_the_smoke_misses_is_a_failure(fake_docker):
    """If the bootstrap succeeds anyway, the harness cannot see that defect class."""
    result = smoke.run_container_smoke(_args(inject=["old-python"]))

    assert not result["passed"]
    assert "cannot see that defect class" in result["detail"]


def test_a_failure_before_the_bootstrap_does_not_pass_an_injected_run(fake_docker):
    """The inverted verdict is not "anything went wrong".

    A build that fails under `--inject` never runs the injected bootstrap at
    all, so it says nothing about whether the smoke sees the defect. Accepting
    it would make the fault-injection job green on a machine whose Docker is
    merely broken -- the "green by luck" condition the job exists to rule out.
    """
    fake_docker.answer(
        "docker build", _completed(1, "", "failed to solve: no space left on device")
    )

    result = smoke.run_container_smoke(_args(inject=["old-python"]))

    assert not result["passed"]
    assert "failed before the bootstrap" in result["detail"]


def test_an_injected_run_that_fails_in_another_defect_class_does_not_pass(fake_docker):
    """A failure of a different class is not evidence the injected one is seen."""
    fake_docker.answer(
        "bash /opt/nyxgpt-smoke/nyxgpt-bootstrap.sh",
        _completed(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
    )

    result = smoke.run_container_smoke(_args(inject=["old-python"]))

    assert not result["passed"]
    assert "docker-handling" in result["detail"]
    assert "interpreter-selection" in result["detail"]


def test_an_injected_run_that_fails_unclassifiably_does_not_pass(fake_docker):
    """An unrecognised failure is not the injected defect either."""
    fake_docker.answer(
        "bash /opt/nyxgpt-smoke/nyxgpt-bootstrap.sh",
        _completed(1, "something entirely unremarkable went wrong"),
    )

    result = smoke.run_container_smoke(_args(inject=["no-node"]))

    assert not result["passed"]
    assert "an unclassified failure" in result["detail"]


def test_every_fault_expects_a_class_the_signature_table_can_produce(fake_docker):
    """A fault whose expected class no signature names could never pass."""
    classes = {name for name, _pattern, _meaning in smoke._FAILURE_SIGNATURES}

    assert {fault.expects for fault in smoke.FAULTS.values()} <= classes


def test_old_python_fault_rewrites_versioned_interpreters():
    script = 'PY=$(command -v python3.12 || command -v python3.11)\n"$PY" -m venv "$V"\n'
    injected, records = smoke.apply_faults(script, ["old-python"])

    assert "python3.12" not in injected and "python3.11" not in injected
    assert records[0]["changed"] is True


def test_no_node_fault_drops_the_node_provisioning():
    script = "curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -\ndnf install -y nodejs\nkeep me\n"
    injected, records = smoke.apply_faults(script, ["no-node"])

    assert "nodesource" not in injected and "nodejs" not in injected
    assert "keep me" in injected
    assert records[0]["changed"] is True


def test_unknown_fault_is_refused():
    with pytest.raises(smoke.ArtifactSmokeFailure, match="Unknown --inject"):
        smoke.apply_faults("#!/bin/sh\n", ["not-a-fault"])


# --- Diagnosis ----------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ERROR: Requires-Python >=3.11", "interpreter selection"),
        ("sh: npm: command not found", "node provisioning"),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "docker handling"),
        (
            "cp: cannot stat '/home/ec2-user/nyxGPT/docker/x': No such file or directory",
            "artifact-relative path",
        ),
        ("Failed to connect to bus: No such file or directory", "systemd --user"),
        (
            "[ops] install: Could not obtain the nyxgpt-api artifact",
            "artifact download",
        ),
        (
            "urllib.error.HTTPError: HTTP Error 404: Not Found\n"
            "  url: https://github.com/dkblinux8/nyxGPT/releases/download/v3.0.0/"
            "nyxgpt-web-3.0.0.tar.gz",
            "artifact download",
        ),
        ("something entirely unremarkable", ""),
    ],
)
def test_failures_are_classified_into_defect_classes(output, expected):
    assert expected in smoke.classify_failure(output)


# The misdiagnosis this table produced in CI run 31905652586: `ops install`
# could not fetch `nyxgpt-api-3.0.0.tar.gz` (the branch's version has no
# release), and the smoke reported "systemd --user session" -- because the
# ops output on the way past mentions `systemctl --user`, and that pattern
# used to match any output containing the words. Pinned as a regression: an
# operator who is told the wrong class spends the round chasing the wrong
# defect, which is the #3762 cost this smoke exists to remove.
_ARTIFACT_404_OUTPUT = """\
[ops] install: step 32/35 nyxgpt-api service unit
[ops] install: reloading systemctl --user daemon
[ops] install: step 33/35 nyxgpt-api artifact
[ops] install: FAILED Could not obtain the nyxgpt-api artifact
  HTTP Error 404: Not Found
"""


def test_an_artifact_404_is_not_diagnosed_as_a_broken_systemd_session():
    assert smoke.failure_class(_ARTIFACT_404_OUTPUT) == "artifact-obtain"
    assert "artifact download" in smoke.classify_failure(_ARTIFACT_404_OUTPUT)


def test_a_healthy_mention_of_systemctl_user_is_not_a_defect_class():
    """The narrowed signature: normal install output names `systemctl --user` constantly."""
    assert smoke.failure_class("[ops] install: enabled nyxgpt-api via systemctl --user") == ""


def test_the_bootstrap_failure_is_classified_from_its_tail_not_the_whole_log(fake_docker):
    """A 35-step install prints everything it did; only the end says why it stopped."""
    # An early, *successful* `npm ci` -- 60 lines back, so out of the tail. It
    # carries the node-provisioning signature, which outranks the artifact one:
    # classify the whole blob and the diagnosis is the step that worked.
    noise = "\n".join(
        ["[ops] install: step 4/35 web dependencies: npm ci"]
        + [f"[ops] install: step {n}/35 ok" for n in range(5, 60)]
    )
    fake_docker.answer(
        "bash /opt/nyxgpt-smoke/nyxgpt-bootstrap.sh",
        _completed(1, f"{noise}\nFAILED Could not obtain the nyxgpt-web artifact\n"),
    )

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "artifact download" in result["failure_class"]
    assert "node provisioning" not in result["failure_class"]


def test_a_failed_run_collects_diagnostics(fake_docker):
    fake_docker.answer(
        "bash /opt/nyxgpt-smoke/nyxgpt-bootstrap.sh", _completed(1, "npm: command not found")
    )

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "node provisioning" in result["failure_class"]
    assert "ops_status" in result["diagnostics"]


# --- Teardown -----------------------------------------------------------


def test_keep_leaves_the_container_and_says_so(fake_docker):
    result = smoke.run_container_smoke(_args(keep=True))

    assert result["teardown"]["kept"] is True
    assert smoke.CONTAINER_NAME in result["teardown"]["reason"]


def test_no_docker_engine_fails_before_anything_is_created(fake_docker):
    fake_docker.answer("docker info", _completed(1, "", "Cannot connect to the Docker daemon"))

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "Docker engine is required" in result["failure"]
    assert result["teardown"]["reason"] == "no container was started"


def test_services_that_never_answer_fail_the_run(fake_docker):
    fake_docker.answer("http_code", _completed(0, "000"))

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "did not answer" in result["failure"]


def test_an_install_from_a_checkout_fails_the_repo_less_check(fake_docker):
    fake_docker.answer(
        "import nyxgpt", _completed(0, "/home/ec2-user/nyxGPT/src/nyxgpt/__init__.py")
    )

    result = smoke.run_container_smoke(_args())

    assert not result["passed"]
    assert "repo-less portability requirement" in result["failure"]


# --- Staging a locally built wheel --------------------------------------
#
# `ops install` fetches `nyxgpt-{api,web}-<version>.tar.gz` from the version's
# GitHub Release. A wheel built from a branch names a version no release
# serves, so `--wheel` has to stage those two assets as well -- without them
# the run reaches step 33 of 35 and 404s, which is how this smoke first failed
# in CI. These tests pin the wiring; that the install then succeeds is the
# workflow's job.


def test_wheel_stages_the_matching_service_tarballs_and_points_ops_at_them(
    fake_docker, monkeypatch, tmp_path
):
    built: list[tuple[str, str]] = []

    def fake_build(tap_dir, name, version, source_root=None):
        built.append((name, version))
        dist = tap_dir / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        tar = dist / f"{name}-{version}.tar.gz"
        tar.write_bytes(b"tarball")
        return tar

    monkeypatch.setattr(smoke.release_tarball, "_create_dist_tarball", fake_build)
    wheel = tmp_path / "nyxgpt-3.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    result = smoke.run_container_smoke(_args(wheel=str(wheel)))

    assert result["passed"], result["failure"]
    # Both services, at the wheel's own version -- a mismatch is a 404.
    assert built == [("nyxgpt-api", "3.0.0"), ("nyxgpt-web", "3.0.0")]
    artifact = next(step for step in result["steps"] if step["step"] == "artifact")
    assert artifact["service_tarballs"] == ["nyxgpt-api-3.0.0.tar.gz", "nyxgpt-web-3.0.0.tar.gz"]
    assert artifact["artifact_dir"] == smoke.ARTIFACT_DIR
    # The bootstrap is what has to be told, or ops downloads instead.
    bootstrap = " ".join(
        " ".join(call) for call in fake_docker.calls if "nyxgpt-bootstrap.sh" in " ".join(call)
    )
    assert f"NYXGPT_ARTIFACT_DIR={smoke.ARTIFACT_DIR}" in bootstrap
    assert f"NYXGPT_PIP_SPEC={smoke.STAGING_DIR}/nyxgpt-3.0.0-py3-none-any.whl" in bootstrap


def test_a_published_version_run_stages_nothing_and_downloads_as_an_instance_does(fake_docker):
    result = smoke.run_container_smoke(_args())

    assert result["passed"], result["failure"]
    artifact = next(step for step in result["steps"] if step["step"] == "artifact")
    assert artifact["artifact_dir"] == ""
    bootstrap = " ".join(
        " ".join(call) for call in fake_docker.calls if "nyxgpt-bootstrap.sh" in " ".join(call)
    )
    assert "NYXGPT_ARTIFACT_DIR" not in bootstrap


def test_a_wheel_whose_name_hides_its_version_is_refused(fake_docker, tmp_path):
    wheel = tmp_path / "nyxgpt.whl"
    wheel.write_bytes(b"wheel")

    result = smoke.run_container_smoke(_args(wheel=str(wheel)))

    assert not result["passed"]
    assert "PEP 427 wheel" in result["failure"]


# --- Status surface -----------------------------------------------------


def test_status_reports_the_last_run_and_the_gaps(fake_docker):
    smoke.record_result(smoke.run_container_smoke(_args()))

    status = smoke.smoke_status()

    assert status["running"] is False
    assert status["last_result"]["passed"] is True
    assert status["docker_available"] is True
    assert status["coverage_gaps"] == list(smoke.COVERAGE_GAPS)
    assert status["commands"]["run"] == "nyxgpt cloud smoke --container"


def test_a_second_background_run_is_refused_while_one_is_in_flight(fake_docker, monkeypatch):
    monkeypatch.setattr(smoke, "_RUNNING", {"started_at": "2026-08-15T00:00:00+00:00"})

    with pytest.raises(smoke.ArtifactSmokeFailure, match="still running"):
        smoke.start_background_run(_args())
