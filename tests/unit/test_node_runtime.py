"""`node_runtime` resolves and classifies the Node nyxGPT actually runs (#4194).

The behavior under test is the part of #4194 that unit tests can reach: given a
host, which `node` does nyxGPT pick, and what does it say about the version it
finds. The *other* half -- that the macOS keg really is the versioned one and
that the installed service really resolves it -- is a runtime claim and is
proven by `macos-brew-smoke.yml` / `linux-native-smoke.yml` executing it.

Two defects are being locked down here, and they are not the same defect:

1. **Resolution.** `shutil.which("node")` on a Mac finds
   `/opt/homebrew/bin/node`, the *unversioned* keg, because `/opt/homebrew/bin`
   leads `PATH`. Every surface that asked PATH first therefore got whatever
   `brew upgrade node` had last moved to -- 26.11 on the owner's machine.
2. **Classification.** Every check in the tree asked `_which("node") is None`,
   which is false on a wrong major exactly as it is on a right one. So a Node
   26 install reported "Web deps already installed (undici OK)" and `doctor`
   found nothing to say.
"""

from __future__ import annotations

import subprocess

import pytest

from nyxgpt import node_runtime

pytestmark = pytest.mark.unit

# Captured at import, before `tests/conftest.py`'s `_runner_runs_the_declared_
# node` fixture replaces them: exercising the real resolution and
# classification is the point of this file, and the suite-wide stub exists so
# that every *other* test is not a test of the runner's PATH.
_REAL_RESOLVE_NODE = node_runtime.resolve_node_bin
_REAL_RESOLVE_NPM = node_runtime.resolve_npm_bin
_REAL_REPORT = node_runtime.node_version_report


@pytest.fixture(autouse=True)
def _real_node_runtime(monkeypatch):
    """Undo the suite-wide stub, and keep the ambient environment out."""
    monkeypatch.setattr(node_runtime, "resolve_node_bin", _REAL_RESOLVE_NODE)
    monkeypatch.setattr(node_runtime, "resolve_npm_bin", _REAL_RESOLVE_NPM)
    monkeypatch.setattr(node_runtime, "node_version_report", _REAL_REPORT)
    monkeypatch.delenv(node_runtime.NODE_BIN_ENV_VAR, raising=False)
    monkeypatch.delenv("HOMEBREW_PREFIX", raising=False)


def _fake_run(stdout: str = "", *, returncode: int = 0):
    """A `subprocess.run` stand-in that reports `stdout` for any argv."""

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return runner


# --- Resolution ---------------------------------------------------------


def test_the_env_override_wins_over_everything(monkeypatch):
    """`NYXGPT_NODE_BIN` is read first -- it is how the smokes inject a wrong major."""
    monkeypatch.setenv(node_runtime.NODE_BIN_ENV_VAR, "/tmp/injected/node")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/usr/bin/node")
    assert node_runtime.resolve_node_bin() == "/tmp/injected/node"


def test_macos_prefers_the_versioned_keg_over_path(monkeypatch):
    """THE #4194 resolution defect, in one assertion.

    `PATH` leads with `/opt/homebrew/bin`, which holds the unversioned keg, so
    a PATH-first search on macOS structurally cannot find the declared Node.
    """
    keg = f"/opt/homebrew/opt/{node_runtime.HOMEBREW_NODE_FORMULA}/bin/node"
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_runtime.os, "access", lambda path, mode: path == keg)
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/opt/homebrew/bin/node")
    assert node_runtime.resolve_node_bin() == keg


def test_macos_falls_back_to_path_when_the_keg_is_absent(monkeypatch):
    """A Mac with no versioned keg yet still gets an answer, not an empty string.

    The report then calls it a mismatch if the major is wrong, which is a
    finding with a remedy -- as distinct from "Node not found", which would send
    the operator to install something they already have.
    """
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_runtime.os, "access", lambda path, mode: False)
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/opt/homebrew/bin/node")
    assert node_runtime.resolve_node_bin() == "/opt/homebrew/bin/node"


def test_linux_uses_path(monkeypatch):
    """On Linux NodeSource installs the declared major AS the system `node`."""
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: f"/usr/bin/{prog}")
    assert node_runtime.resolve_node_bin() == "/usr/bin/node"
    assert node_runtime.resolve_npm_bin() == "/usr/bin/npm"


def test_no_node_anywhere_resolves_to_empty(monkeypatch):
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: None)
    assert node_runtime.resolve_node_bin() == ""


def test_homebrew_prefix_env_is_searched_before_the_standard_roots(monkeypatch):
    """An unusual Homebrew root (or a `brew` session's own env) is honoured."""
    monkeypatch.setenv("HOMEBREW_PREFIX", "/custom/brew")
    dirs = node_runtime.homebrew_node_bin_dirs()
    assert dirs[0] == f"/custom/brew/opt/{node_runtime.HOMEBREW_NODE_FORMULA}/bin"
    assert f"/opt/homebrew/opt/{node_runtime.HOMEBREW_NODE_FORMULA}/bin" in dirs


def test_node_environment_puts_the_declared_bin_first_on_path(monkeypatch):
    """`npm` re-execs `node` from ITS OWN PATH, so an absolute npm is not enough.

    Without this prefix, the declared keg's `npm` would hand off to whichever
    `node` the inherited `PATH` resolves -- the same split, one level down.
    """
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/opt/node24/bin/node")
    env = node_runtime.node_environment({"PATH": "/usr/bin:/bin"})
    assert env["PATH"].startswith("/opt/node24/bin:")


def test_node_environment_is_unchanged_when_no_node_was_found(monkeypatch):
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: None)
    assert node_runtime.node_environment({"PATH": "/usr/bin"})["PATH"] == "/usr/bin"


# --- Classification -----------------------------------------------------


def test_the_declared_major_reports_ok(monkeypatch):
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/usr/bin/node")
    monkeypatch.setattr(
        node_runtime.subprocess, "run", _fake_run(f"v{node_runtime.NODE_MAJOR}.21.0\n")
    )
    report = node_runtime.node_version_report()
    assert report.state == node_runtime.NODE_OK
    assert report.ok
    assert report.major == node_runtime.NODE_MAJOR
    assert report.remediation == ""


def test_a_newer_major_is_a_mismatch_not_a_pass(monkeypatch):
    """`>=` thinking is the defect: Node 26 is wrong, not "newer than fine".

    This is the owner's machine as of 2026-10-10 -- Homebrew `node` 26.11 first
    on PATH -- and the whole point is that it reports as wrong.
    """
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/opt/homebrew/bin/node")
    monkeypatch.setattr(node_runtime.subprocess, "run", _fake_run("v26.11.0\n"))
    report = node_runtime.node_version_report()
    assert report.state == node_runtime.NODE_MISMATCH
    assert not report.ok
    assert report.major == 26
    assert str(node_runtime.NODE_MAJOR) in report.detail
    assert report.remediation == node_runtime.NODE_REMEDIATION_COMMAND


def test_an_older_major_is_a_mismatch(monkeypatch):
    """Node 20 -- what CI tested until this issue -- is end-of-life, so also wrong."""
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/usr/bin/node")
    monkeypatch.setattr(node_runtime.subprocess, "run", _fake_run("v20.11.1\n"))
    assert node_runtime.node_version_report().state == node_runtime.NODE_MISMATCH


def test_missing_node_is_reported_as_missing_not_as_a_mismatch(monkeypatch):
    """Three states, three remedies -- the check this replaces had one."""
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: None)
    report = node_runtime.node_version_report()
    assert report.state == node_runtime.NODE_MISSING
    assert report.path == ""
    assert report.remediation == node_runtime.NODE_REMEDIATION_COMMAND


def test_an_unreadable_version_is_its_own_state(monkeypatch):
    """A `node` that exists but cannot answer `-v` is neither absent nor wrong.

    A broken keg, a shim that errors, a binary for the wrong architecture:
    saying "wrong version" would name a cause that is not the cause.
    """
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/usr/bin/node")
    monkeypatch.setattr(
        node_runtime.subprocess, "run", _fake_run("cannot execute binary", returncode=126)
    )
    report = node_runtime.node_version_report()
    assert report.state == node_runtime.NODE_UNREADABLE
    assert report.major is None


def test_the_report_serializes_for_the_dashboard(monkeypatch):
    """`infra_status` ships this dict straight to the Infrastructure page."""
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: "/usr/bin/node")
    monkeypatch.setattr(node_runtime.subprocess, "run", _fake_run("v26.0.0\n"))
    payload = node_runtime.node_version_report().to_dict()
    assert payload["state"] == node_runtime.NODE_MISMATCH
    assert payload["ok"] is False
    assert payload["declared_major"] == node_runtime.NODE_MAJOR
    assert payload["major"] == 26
    assert payload["remediation"] == node_runtime.NODE_REMEDIATION_COMMAND


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v24.21.0", 24),
        ("24.21.0", 24),
        ("v26.11.0\n", 26),
        ("", None),
        ("not a version", None),
    ],
)
def test_parse_node_major(text: str, expected: int | None):
    assert node_runtime.parse_node_major(text) == expected


# --- Provisioning -------------------------------------------------------


def test_nodesource_urls_name_the_declared_major():
    """One function, both package managers -- the two literals it replaced."""
    major = node_runtime.NODE_MAJOR
    assert node_runtime.nodesource_setup_url("dnf") == (
        f"https://rpm.nodesource.com/setup_{major}.x"
    )
    assert node_runtime.nodesource_setup_url("apt") == (
        f"https://deb.nodesource.com/setup_{major}.x"
    )
