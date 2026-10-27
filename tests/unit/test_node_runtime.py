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

import inspect
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


def test_the_override_takes_the_npm_beside_it(monkeypatch):
    """A pinned `node` is served by ITS OWN `npm`, when one is there."""
    monkeypatch.setenv(node_runtime.NODE_BIN_ENV_VAR, "/tmp/injected/node")
    monkeypatch.setattr(node_runtime.os, "access", lambda path, mode: True)
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: f"/usr/bin/{prog}")
    assert node_runtime.resolve_npm_bin() == "/tmp/injected/npm"


def test_an_override_with_no_npm_beside_it_answers_empty_not_someone_elses(monkeypatch):
    """The mismatch this module removes, wearing its other name.

    `NYXGPT_NODE_BIN` pins `node`. If there is no `npm` in that directory, the
    earlier code fell through to the keg and then to `PATH` -- handing back an
    `npm` from a DIFFERENT major to drive the pinned `node`, which is exactly
    the pairing `resolve_npm_bin`'s docstring says it exists to prevent, and
    silently, because the caller asked for "the npm belonging to the declared
    Node". "" is the honest answer; the callers have their own fallbacks.
    """
    monkeypatch.setenv(node_runtime.NODE_BIN_ENV_VAR, "/tmp/injected/node")
    monkeypatch.setattr(node_runtime.os, "access", lambda path, mode: False)
    # Both of the paths that used to be taken instead are made to answer, so a
    # regression returns one of them rather than failing to find anything.
    monkeypatch.setattr(node_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_runtime.shutil, "which", lambda prog: f"/usr/bin/{prog}")
    assert node_runtime.resolve_npm_bin() == ""


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


# --- `nyxgpt ops node` ---------------------------------------------------
#
# The wrapped remedy every other surface points at. Tested here rather than in
# test_ops.py because what it is about is this module's report; `ops` only
# renders it and dispatches the install.


def _report(state, *, path="", version="", major=None):
    return node_runtime.NodeRuntimeReport(
        state=state,
        path=path,
        version=version,
        major=major,
        declared_major=node_runtime.NODE_MAJOR,
        detail="stubbed detail",
        remediation=node_runtime.NODE_REMEDIATION_COMMAND,
    )


def test_ops_node_installs_nothing_on_a_host_already_on_the_declared_major(monkeypatch, capsys):
    """Idempotent, and it says so: no install, exit 0."""
    from nyxgpt import ops

    monkeypatch.setattr(
        ops.node_runtime,
        "node_version_report",
        lambda *, timeout=15.0: _report(
            node_runtime.NODE_OK,
            path="/usr/bin/node",
            version=f"v{node_runtime.NODE_MAJOR}.0.0",
            major=node_runtime.NODE_MAJOR,
        ),
    )

    def _no_install():
        raise AssertionError("ops node must not install on a correct host")

    monkeypatch.setattr(ops, "_install_declared_node", _no_install)
    monkeypatch.delenv(node_runtime.NODE_BIN_ENV_VAR, raising=False)

    assert ops.node(object()) == 0
    out = capsys.readouterr().out
    assert f"declared: Node {node_runtime.NODE_MAJOR}" in out


def test_ops_node_reports_only_when_the_binary_is_pinned_by_env(monkeypatch, capsys):
    """An explicit `NYXGPT_NODE_BIN` is an instruction, not a problem to fix.

    Installing another Node would not change what resolution answers -- the
    override is read first -- so the honest action is to report and exit
    non-zero. It is also what keeps the smoke jobs' fault injection from
    triggering a NodeSource install on the runner.
    """
    from nyxgpt import ops

    monkeypatch.setenv(node_runtime.NODE_BIN_ENV_VAR, "/tmp/injected/node")
    monkeypatch.setattr(
        ops.node_runtime,
        "node_version_report",
        lambda *, timeout=15.0: _report(
            node_runtime.NODE_MISMATCH, path="/tmp/injected/node", version="v20.0.0", major=20
        ),
    )

    def _no_install():
        raise AssertionError("ops node must not install while the binary is pinned")

    monkeypatch.setattr(ops, "_install_declared_node", _no_install)

    assert ops.node(object()) == 1
    out = capsys.readouterr().out
    assert node_runtime.NODE_BIN_ENV_VAR in out
    assert "report only" in out


def test_ops_node_reverifies_by_resolving_again_not_by_the_installer_exit_code(monkeypatch, capsys):
    """A successful `brew install node@NN` of a keg-only formula changes no PATH.

    So "did it work" cannot be the installer's exit code -- it has to be a
    fresh resolution. This stubs an installer that succeeds while the host
    stays wrong, and requires a non-zero exit.
    """
    from nyxgpt import ops

    monkeypatch.delenv(node_runtime.NODE_BIN_ENV_VAR, raising=False)
    monkeypatch.setattr(
        ops.node_runtime,
        "node_version_report",
        lambda *, timeout=15.0: _report(
            node_runtime.NODE_MISMATCH, path="/opt/homebrew/bin/node", version="v26.11.0", major=26
        ),
    )
    monkeypatch.setattr(ops, "_install_declared_node", lambda: [ops.OpsResult(True, "Installed")])

    assert ops.node(object()) == 1
    assert "still not on the declared Node" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `ops install` reconciles Node -- it does not merely refuse (#4194, round 3)
# ---------------------------------------------------------------------------
#
# Found by the class sweep over `_node_toolchain_gate`'s call sites. FOUR
# install steps gate on the declared Node (web deps, mcp deps, and both native
# web installs), and nothing in `install` put it there. On a machine with a
# checkout that is a nuisance; on the machine the repo-less requirement
# actually targets -- a clean host, no checkout, whatever `node` its image
# shipped -- it is an install that cannot complete, and
# `release-artifacts.yml`'s `artifact-install-smoke` job is exactly that
# machine: `pip install nyxgpt` then `nyxgpt ops install`, with no Node step
# anywhere and no `.nvmrc` to read.


def test_install_reconciles_node_before_the_steps_that_gate_on_it():
    """The step exists, and it runs before every step that would refuse."""
    from nyxgpt import ops

    source = inspect.getsource(ops.install)
    assert '("node runtime", _ensure_declared_node)' in source, (
        "`ops install` has no step that puts the declared Node on the host, so the "
        "four steps that gate on it would fail on a clean machine"
    )
    node_at = source.index('("node runtime"')
    for gated in ('("web deps"', '("mcp deps"', '("native web service"'):
        assert source.index(gated) > node_at, (
            f"{gated} runs before the node runtime step, so it would gate on a Node "
            "the install had not yet put there"
        )


def test_the_reconciler_is_shared_with_the_cli_remedy():
    """One source for "what does this host need doing about Node" (D-066).

    `ops node` is the command every other surface points at; `install`'s step
    is the same decision made at install time. Two implementations is how the
    gate and the remedy would come to disagree about one machine.
    """
    from nyxgpt import ops

    assert "_ensure_declared_node()" in inspect.getsource(ops.node)


def test_the_reconciler_installs_nothing_on_a_correct_host(monkeypatch):
    from nyxgpt import ops

    monkeypatch.delenv(node_runtime.NODE_BIN_ENV_VAR, raising=False)
    monkeypatch.setattr(
        ops.node_runtime,
        "node_version_report",
        lambda *, timeout=15.0: _report(
            node_runtime.NODE_OK,
            path="/usr/bin/node",
            version=f"v{node_runtime.NODE_MAJOR}.0.0",
            major=node_runtime.NODE_MAJOR,
        ),
    )
    monkeypatch.setattr(
        ops,
        "_install_declared_node",
        lambda: (_ for _ in ()).throw(AssertionError("installed on a correct host")),
    )

    results = ops._ensure_declared_node()
    assert [r.ok for r in results] == [True]


def test_the_reconciler_installs_on_a_wrong_host_and_reverifies(monkeypatch):
    """It installs, then asks the resolver again -- both halves asserted.

    The verdict may not be the installer's exit code: `brew install node@NN`
    of a keg-only formula succeeds while changing nothing a resolver would see.
    """
    from nyxgpt import ops

    monkeypatch.delenv(node_runtime.NODE_BIN_ENV_VAR, raising=False)
    states = iter(
        [
            _report(node_runtime.NODE_MISSING, path="", version="", major=None),
            _report(
                node_runtime.NODE_OK,
                path="/usr/bin/node",
                version=f"v{node_runtime.NODE_MAJOR}.1.0",
                major=node_runtime.NODE_MAJOR,
            ),
        ]
    )
    monkeypatch.setattr(
        ops.node_runtime, "node_version_report", lambda *, timeout=15.0: next(states)
    )
    installed = []
    monkeypatch.setattr(
        ops,
        "_install_declared_node",
        lambda: installed.append(1) or [ops.OpsResult(True, "Installed")],
    )

    results = ops._ensure_declared_node()
    assert installed, "a host with no Node at all was not given one"
    assert results[-1].ok, "the reconciler did not re-resolve after installing"

    # And the other direction: an installer that succeeds over a host that
    # stays wrong must still report failure.
    still_wrong = _report(
        node_runtime.NODE_MISMATCH, path="/opt/homebrew/bin/node", version="v26.11.0", major=26
    )
    monkeypatch.setattr(
        ops.node_runtime, "node_version_report", lambda *, timeout=15.0: still_wrong
    )
    results = ops._ensure_declared_node()
    assert not results[-1].ok
    assert node_runtime.NODE_REMEDIATION_COMMAND in (results[-1].details or "")


def test_the_nodesource_pipeline_fails_when_the_fetch_fails(monkeypatch):
    """An unreachable NodeSource must fail AS an unreachable NodeSource.

    `curl ... | bash -` in a plain shell reports the exit status of `bash -`,
    which reads empty stdin and exits 0. So a fetch that could not reach the
    repository looked like a repository that was added, the step went on to
    `install nodejs`, and the DISTRO's Node (18 on AL2023) went on instead.
    `_ensure_declared_node`'s end-state re-read still calls that host wrong,
    but it names the package manager, not the one thing an operator can act
    on. Both halves are asserted: that the invocation asks for `pipefail`, and
    -- by running the two shells -- that asking for it is what makes the
    difference.
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "_is_macos", lambda: False)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}" if prog == "dnf" else None)
    calls: list[list[str]] = []

    def fake_privileged(cmd, *, expected=False):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "could not resolve host")

    monkeypatch.setattr(ops, "_privileged_run", fake_privileged)

    results = ops._install_declared_node()
    assert not results[0].ok
    assert "NodeSource repository" in results[0].message

    setup_argv = calls[0]
    assert setup_argv[0] == "bash"
    assert "-o" in setup_argv and "pipefail" in setup_argv, (
        "the NodeSource fetch runs in a shell this code spawns itself, which "
        f"inherits no shell options, so it has to set pipefail: {setup_argv}"
    )
    pipeline = setup_argv[-1]
    assert "| bash -" in pipeline and node_runtime.NODESOURCE_SETUP_SCRIPT in pipeline

    # The mechanism, executed: the same pipeline shape with a failing fetch,
    # with and without the option. `false` stands in for the unreachable curl.
    shape = "false | bash -"
    without = subprocess.run(["bash", "-c", shape], capture_output=True, text=True)
    with_it = subprocess.run(
        ["bash", "-o", "pipefail", "-c", shape], capture_output=True, text=True
    )
    assert without.returncode == 0, "the defect no longer reproduces; re-derive this test"
    assert with_it.returncode != 0, "pipefail did not propagate the fetch's failure"


def test_the_reconciler_honours_an_explicit_pin_without_installing(monkeypatch):
    """`NYXGPT_NODE_BIN` is an instruction. It is also what keeps the smoke
    jobs' fault injection from triggering a real NodeSource install."""
    from nyxgpt import ops

    monkeypatch.setenv(node_runtime.NODE_BIN_ENV_VAR, "/tmp/injected/node")
    monkeypatch.setattr(
        ops.node_runtime,
        "node_version_report",
        lambda *, timeout=15.0: _report(
            node_runtime.NODE_MISMATCH, path="/tmp/injected/node", version="v20.0.0", major=20
        ),
    )
    monkeypatch.setattr(
        ops,
        "_install_declared_node",
        lambda: (_ for _ in ()).throw(AssertionError("installed while pinned")),
    )

    results = ops._ensure_declared_node()
    assert not results[0].ok
    assert "report only" in (results[0].details or "")
