"""Unit tests for `nyxgpt ops install-extra` (#4122).

The defect this command closes. Every `nyxgpt cloud` path that needs boto3 told
the operator to run `pip install nyxgpt[cloud]`, and on a Homebrew keg that
cannot be followed: `pip` is not on PATH, `pip3` is a different interpreter
whose site-packages `nyxgpt` never reads, and the only pip that reaches the
right venv is a raw path into the Cellar -- which is both unwrapped (CLAUDE.md's
Operational Command Wrapping requirement) and layout-specific. So the remedy the
owner was handed in acceptance testing could not work, and no remedy that could
existed.

What is asserted here is the one property that makes the command correct on
every layout: it installs into `sys.executable`, the interpreter that will do
the importing, rather than into whatever `pip` a PATH lookup finds.
"""

import argparse
import subprocess
import sys

import pytest

from nyxgpt import ops
from nyxgpt.optional_imports import CLOUD_EXTRA_REMEDY


def _args(**overrides) -> argparse.Namespace:
    base = {"extra": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_it_installs_into_the_interpreter_that_will_do_the_importing(monkeypatch):
    """The whole point. A `pip` found on PATH is a different environment on a
    Homebrew keg, and installing there reports success while the import this was
    run to fix still fails -- the worst of the available outcomes."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, **kwargs):
        captured["argv"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ops, "_run", _record)

    results = ops.install_extra("cloud")

    assert results[0].ok
    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "pip", "install"]
    assert any(arg.startswith("nyxgpt[cloud]") for arg in argv)
    # Never a bare `pip`/`pip3`, which is the thing that made the old remedy
    # unfollowable.
    assert "pip" not in argv[0]
    assert "pip3" not in argv


def test_the_install_is_pinned_to_the_running_version(monkeypatch):
    """An extras install must not drag a keg onto a different release: the extra
    is a set of dependencies, not an upgrade."""
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kw: (
            captured.setdefault("argv", list(cmd)),
            subprocess.CompletedProcess(cmd, 0, "", ""),
        )[1],
    )
    monkeypatch.setattr("nyxgpt.version.running_version", lambda: "3.0.0rc14")

    ops.install_extra("cloud")

    assert "nyxgpt[cloud]==3.0.0rc14" in captured["argv"]


def test_an_unresolvable_version_is_pinned_to_nothing_rather_than_to_a_guess(monkeypatch):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kw: (
            captured.setdefault("argv", list(cmd)),
            subprocess.CompletedProcess(cmd, 0, "", ""),
        )[1],
    )
    monkeypatch.setattr("nyxgpt.version.running_version", lambda: "unknown")

    ops.install_extra("cloud")

    assert "nyxgpt[cloud]" in captured["argv"]
    assert not any("==unknown" in arg for arg in captured["argv"])


def test_an_unknown_extra_is_refused_and_names_the_ones_that_exist(monkeypatch):
    def _must_not_run(cmd, **kwargs):  # pragma: no cover - the point of the test
        raise AssertionError("an unknown extra must not reach pip")

    monkeypatch.setattr(ops, "_run", _must_not_run)

    results = ops.install_extra("kubernetes")

    assert not results[0].ok
    assert "cloud" in results[0].details
    assert "verify" in results[0].details


def test_a_failed_pip_run_is_reported_as_a_failure_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, "", "ERROR: Could not find a version that satisfies the requirement"
        ),
    )

    results = ops.install_extra("cloud")

    assert not results[0].ok
    assert "Could not find a version" in results[0].details


def test_listing_the_extras_names_the_target_environment(capsys):
    """An operator has to be able to see *which* environment this is about --
    especially on a keg, where the answer is not the one `which pip` suggests."""
    assert ops.install_extra_command(_args()) == 0

    out = capsys.readouterr().out
    assert "cloud" in out
    assert "verify" in out
    assert sys.executable in out


@pytest.mark.parametrize(
    "extra",
    sorted(ops.INSTALLABLE_EXTRAS),
)
def test_every_offered_extra_exists_in_the_package_metadata(extra):
    """An offered extra that pyproject.toml does not declare is a command that
    always fails. Read from the installed distribution rather than parsed out of
    pyproject.toml, so this holds for the artifact as shipped."""
    from importlib.metadata import metadata

    declared = set(metadata("nyxgpt").get_all("Provides-Extra") or [])
    assert extra in declared


def test_the_cloud_remedy_names_the_wrapped_command_and_not_a_bare_pip():
    """The remedy text is a single constant precisely so that five modules
    cannot go on naming a command that does not work -- which is what they all
    did until #4122."""
    assert "nyxgpt ops install-extra cloud" in CLOUD_EXTRA_REMEDY
    # The pip form survives as an explanation of when it *does* work, never as
    # the instruction.
    assert CLOUD_EXTRA_REMEDY.index("nyxgpt ops install-extra") < CLOUD_EXTRA_REMEDY.index(
        "pip install"
    )


def test_every_module_that_needs_the_cloud_extra_shares_one_remedy():
    """The inverse-claims half: a module that kept its own copy of the old text
    would still be handing out the unfollowable command."""
    import pathlib

    root = pathlib.Path(ops.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # Comments are allowed to quote the retired text -- several of them
            # explain why it was retired, which is the reasoning this change
            # most needs to keep. What must not survive is a *raised* copy.
            if line.lstrip().startswith("#"):
                continue
            if "Install with `pip install nyxgpt[" in line:
                offenders.append(f"{path.name}:{number}")
    assert offenders == []
