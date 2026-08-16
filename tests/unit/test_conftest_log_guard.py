"""Tests for the production-log-dir guard's external-writer attribution.

The guard itself lives in `tests/conftest.py` (`_isolate_test_log_dir`) and
exists to keep test noise out of the real `~/.nyxGPT/logs` (#3443). Once the
stack can be installed natively on Linux as well as macOS (#3508), the normal
state of a developer machine is that the *running services* hold those log
files open and append to them throughout a pytest session -- so the guard has
to tell "a service wrote this" apart from "a test wrote this", and must keep
failing for the latter.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest
from log_guard import externally_held_log_files


class _FakeOpenFile:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeProcess:
    """Stands in for a psutil.Process with a fixed set of open files."""

    def __init__(self, pid: int, paths: list[str] | None = None, error: Exception | None = None):
        self.pid = pid
        self._paths = paths or []
        self._error = error

    def open_files(self) -> list[_FakeOpenFile]:
        if self._error is not None:
            raise self._error
        return [_FakeOpenFile(p) for p in self._paths]


@pytest.fixture
def fake_processes(monkeypatch):
    """Replace psutil.process_iter with a fixed list of fake processes."""

    def _install(processes: list[_FakeProcess]) -> None:
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **kw: list(processes))

    return _install


def test_reports_a_log_file_held_by_an_external_process(tmp_path, fake_processes):
    """A service holding a log file open is what the exclusion is for."""
    held = tmp_path / "nyxgpt-api.err.log"
    held.write_text("service output\n")
    fake_processes([_FakeProcess(pid=os.getpid() + 10_000, paths=[str(held)])])

    assert externally_held_log_files(tmp_path) == {str(held)}


def test_ignores_files_outside_the_log_dir(tmp_path, fake_processes):
    """Only paths under the production log dir are in scope."""
    outside = tmp_path.parent / "somewhere-else.log"
    outside.write_text("unrelated\n")
    fake_processes([_FakeProcess(pid=os.getpid() + 10_000, paths=[str(outside)])])

    assert externally_held_log_files(tmp_path) == set()


def test_does_not_exclude_a_file_held_by_the_pytest_process(tmp_path, fake_processes):
    """The guard must keep its teeth: our own writes are never excused.

    This is the #3443 regression itself -- a code path under test writing to
    the real log dir. If this process's open files were treated as
    "externally held", the guard would go silent exactly when it matters.
    """
    ours = tmp_path / "written-by-a-test.log"
    ours.write_text("test noise\n")
    fake_processes([_FakeProcess(pid=os.getpid(), paths=[str(ours)])])

    assert externally_held_log_files(tmp_path) == set()


def test_a_process_we_cannot_inspect_is_left_in_scope(tmp_path, fake_processes):
    """AccessDenied fails closed -- the file stays subject to the assertion."""
    other = tmp_path / "another-users-service.log"
    other.write_text("output\n")
    fake_processes([_FakeProcess(pid=os.getpid() + 10_000, error=psutil.AccessDenied(pid=1234))])

    assert externally_held_log_files(tmp_path) == set()
    # The file exists and is unattributed, so a change to it would still fail
    # the guard rather than be silently excused.
    assert other.exists()


def test_a_vanished_process_does_not_break_the_scan(tmp_path, fake_processes):
    """Processes exiting mid-scan are routine and must not raise."""
    survivor = tmp_path / "cassandra.log"
    survivor.write_text("output\n")
    fake_processes(
        [
            _FakeProcess(pid=os.getpid() + 10_000, error=psutil.NoSuchProcess(pid=4321)),
            _FakeProcess(pid=os.getpid() + 10_001, paths=[str(survivor)]),
        ]
    )

    assert externally_held_log_files(tmp_path) == {str(survivor)}


def test_real_open_file_in_this_process_is_not_reported(tmp_path):
    """End-to-end against real psutil, no fakes: our own fd is not excluded."""
    ours = tmp_path / "held-by-us.log"
    with ours.open("w") as handle:
        handle.write("still open\n")
        handle.flush()
        assert str(ours) not in externally_held_log_files(tmp_path)


def test_missing_log_dir_is_handled(tmp_path):
    """A machine that has never run the stack has no log dir at all."""
    assert externally_held_log_files(tmp_path / "does-not-exist") == set()


def test_symlinked_log_dir_still_matches(tmp_path, fake_processes):
    """Paths are compared as real paths, so a symlinked log dir still matches."""
    real_dir = tmp_path / "real-logs"
    real_dir.mkdir()
    link_dir = tmp_path / "linked-logs"
    link_dir.symlink_to(real_dir)

    held = real_dir / "nyxgpt-web.log"
    held.write_text("service output\n")
    fake_processes([_FakeProcess(pid=os.getpid() + 10_000, paths=[str(held)])])

    assert externally_held_log_files(Path(link_dir)) == {str(held)}
