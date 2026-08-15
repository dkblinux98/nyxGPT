"""Service-venv interpreter selection (#3782).

The rc9 cloud round failed because `_install_native_api_systemd` built the
`nyxgpt-api` venv with bare `python3`, which is 3.9 on Amazon Linux 2023 --
pip then refused the artifact with "requires a different Python: 3.9.16 not
in '>=3.11'". These tests pin the selection rules: prefer the interpreter ops
itself runs under, then an explicitly-versioned `python3.X` from PATH, and
never assume bare `python3` qualifies.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from nyxgpt import ops

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _version_probe_response(version: str) -> subprocess.CompletedProcess[str]:
    return _cp(0, stdout=f"{version}\n")


def _fake_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: tuple[str, tuple[int, int]],
    on_path: dict[str, str],
    venv_returncodes: dict[str, int] | None = None,
) -> list[list[str]]:
    """Stand in a machine: a running interpreter, a PATH, and venv outcomes.

    `on_path` maps an interpreter name to the version string it reports
    (e.g. `{"python3": "3.9"}`). `venv_returncodes` maps an executable path
    to the exit code its `-m venv` run should produce (default 0). Returns
    the list every command run is appended to.
    """
    calls: list[list[str]] = []
    rcs = venv_returncodes or {}

    monkeypatch.setattr(ops, "_running_interpreter", lambda: running)
    monkeypatch.setattr(
        ops, "_which", lambda name: f"/opt/testbin/{name}" if name in on_path else None
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if len(cmd) >= 2 and cmd[1] == "-c":
            name = Path(cmd[0]).name
            return _version_probe_response(on_path[name])
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            return _cp(rcs.get(cmd[0], 0), stderr="No module named venv")
        return _cp(0)

    monkeypatch.setattr(ops, "_run", fake_run)
    return calls


def test_min_service_python_matches_pyproject_requires_python():
    # The floor is a literal in ops (a venv is built before anything is
    # installed into it), so it has to be kept in step with the real one.
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires_python = data["project"]["requires-python"]

    floor = ".".join(str(part) for part in ops._MIN_SERVICE_PYTHON)
    assert requires_python == f">={floor}"


def test_candidates_prefer_the_interpreter_ops_runs_under():
    # It is provably able to run nyxGPT -- it is running it -- and on a cloud
    # instance it is the one provisioning installed to satisfy the floor.
    candidates = ops._service_python_candidates()

    assert candidates
    exe, version = candidates[0]
    assert exe == ops._running_interpreter()[0]
    assert version >= ops._MIN_SERVICE_PYTHON


def test_candidates_are_newest_first_and_deduplicated(monkeypatch):
    _fake_environment(
        monkeypatch,
        running=("/opt/nyxgpt/venv/bin/python", (3, 11)),
        on_path={"python3.12": "3.12", "python3.11": "3.11", "python3": "3.9"},
    )

    assert ops._service_python_candidates() == [
        ("/opt/nyxgpt/venv/bin/python", (3, 11)),
        ("/opt/testbin/python3.12", (3, 12)),
        ("/opt/testbin/python3.11", (3, 11)),
        ("/opt/testbin/python3", (3, 9)),
    ]


def test_candidates_drop_an_interpreter_that_cannot_be_run(monkeypatch):
    # A `python3.11` on PATH that is a broken symlink or not executable must
    # not be selected on the strength of its name.
    monkeypatch.setattr(ops, "_running_interpreter", lambda: ("/opt/testbin/python3.11", (3, 11)))
    monkeypatch.setattr(
        ops, "_which", lambda name: f"/opt/testbin/{name}" if name == "python3" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(127, stderr="No such file or directory"))

    assert ops._service_python_candidates() == [("/opt/testbin/python3.11", (3, 11))]


def test_candidates_drop_unparseable_version_output(monkeypatch):
    monkeypatch.setattr(ops, "_running_interpreter", lambda: ("/opt/venv/bin/python", (3, 12)))
    monkeypatch.setattr(
        ops, "_which", lambda name: f"/opt/testbin/{name}" if name == "python3" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0, stdout="banana\n"))

    assert ops._service_python_candidates() == [("/opt/venv/bin/python", (3, 12))]


def test_create_service_venv_uses_the_ops_interpreter_not_bare_python3(tmp_path, monkeypatch):
    # The Amazon Linux 2023 shape of #3782: ops runs under 3.11 (installed by
    # provisioning), the distro's `python3` is 3.9.
    calls = _fake_environment(
        monkeypatch,
        running=("/home/ec2-user/.nyxGPT/venv/bin/python", (3, 11)),
        on_path={"python3": "3.9"},
    )

    result = ops._create_service_venv(tmp_path / "venv", "nyxgpt-api")

    assert result.ok is True
    assert "Python 3.11" in result.message
    venv_calls = [c for c in calls if c[1:3] == ["-m", "venv"]]
    assert venv_calls == [
        ["/home/ec2-user/.nyxGPT/venv/bin/python", "-m", "venv", str(tmp_path / "venv")]
    ]


def test_create_service_venv_falls_back_to_a_versioned_python_on_path(tmp_path, monkeypatch):
    # ops itself running on an old interpreter (a 3.9 `pip install --user`
    # install, say) must still build the service venv on a qualifying one.
    calls = _fake_environment(
        monkeypatch,
        running=("/opt/testbin/python3", (3, 9)),
        on_path={"python3.12": "3.12", "python3": "3.9"},
    )

    result = ops._create_service_venv(tmp_path / "venv", "nyxgpt-api")

    assert result.ok is True
    assert "Python 3.12" in result.message
    venv_calls = [c for c in calls if c[1:3] == ["-m", "venv"]]
    assert venv_calls == [["/opt/testbin/python3.12", "-m", "venv", str(tmp_path / "venv")]]


def test_create_service_venv_tries_the_next_candidate_when_venv_creation_fails(
    tmp_path, monkeypatch
):
    # Debian splits `venv`/`ensurepip` into python3.X-venv: a present but
    # venv-less 3.12 must not fail the install when a working 3.11 is there.
    calls = _fake_environment(
        monkeypatch,
        running=("/opt/testbin/python3", (3, 9)),
        on_path={"python3.12": "3.12", "python3.11": "3.11", "python3": "3.9"},
        venv_returncodes={"/opt/testbin/python3.12": 1},
    )

    result = ops._create_service_venv(tmp_path / "venv", "nyxgpt-api")

    assert result.ok is True
    assert "Python 3.11" in result.message
    venv_calls = [c[0] for c in calls if c[1:3] == ["-m", "venv"]]
    assert venv_calls == ["/opt/testbin/python3.12", "/opt/testbin/python3.11"]


def test_create_service_venv_names_found_and_required_versions_when_none_qualify(
    tmp_path, monkeypatch
):
    # Acceptance criterion: a clear step failure, not an opaque pip rc=1
    # several minutes later.
    calls = _fake_environment(
        monkeypatch,
        running=("/opt/testbin/python3", (3, 9)),
        on_path={"python3": "3.9"},
    )

    result = ops._create_service_venv(tmp_path / "venv", "nyxgpt-api")

    assert result.ok is False
    assert "No Python >= 3.11" in result.message
    assert "nyxgpt-api" in result.message
    assert "/opt/testbin/python3 (Python 3.9)" in result.details
    assert "dnf install -y python3.11" in result.details
    assert not [c for c in calls if c[1:3] == ["-m", "venv"]]


def test_create_service_venv_reports_every_attempt_when_all_fail(tmp_path, monkeypatch):
    _fake_environment(
        monkeypatch,
        running=("/opt/venv/bin/python", (3, 11)),
        on_path={"python3.12": "3.12", "python3": "3.9"},
        venv_returncodes={"/opt/venv/bin/python": 1, "/opt/testbin/python3.12": 1},
    )

    result = ops._create_service_venv(tmp_path / "venv", "nyxgpt-api")

    assert result.ok is False
    assert result.message == "Failed to create nyxgpt-api venv"
    assert "/opt/venv/bin/python (Python 3.11)" in result.details
    assert "/opt/testbin/python3.12 (Python 3.12)" in result.details
    assert "No module named venv" in result.details


def test_native_api_install_never_creates_its_venv_with_bare_python3(tmp_path, monkeypatch):
    # The regression guard for the line #3782 was filed against:
    # `_run(["python3", "-m", "venv", ...])`.
    repo = tmp_path / "repo"
    (repo / "src" / "nyxgpt").mkdir(parents=True)
    (repo / "src" / "nyxgpt" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    (repo / "example.config.ini").write_text("[api]\nhost = 127.0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    tpl = tmp_path / "nyxgpt-api.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/x\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))
    calls = _fake_environment(
        monkeypatch,
        running=("/home/ec2-user/.nyxGPT/venv/bin/python", (3, 11)),
        on_path={"python3": "3.9"},
    )

    results = ops._install_native_api_systemd()

    assert all(r.ok for r in results), results
    venv_calls = [c for c in calls if c[1:3] == ["-m", "venv"]]
    assert venv_calls, "no venv was created at all"
    assert all(Path(c[0]).name != "python3" for c in venv_calls), venv_calls
