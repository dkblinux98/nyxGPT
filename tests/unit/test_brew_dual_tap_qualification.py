"""Ops never names a Homebrew formula bare (#3861 re-test failure).

With both `dkblinux98/nyxgpt` and `dkblinux98/nyxgpt-local` tapped -- the
state of every machine that has tested the published tap next to the locally
built one -- Homebrew refuses a bare formula name rather than choosing:

    Error: Formulae found in multiple taps:
             dkblinux98/nyxgpt-local/nyxgpt-api
             dkblinux98/nyxgpt/nyxgpt-api
    Please use the fully-qualified name to refer to the formula.

The install sites already passed `<tap>/<formula>`. The *lookup and lifecycle*
sites did not -- `brew list --versions`, `brew list --formula`, `brew services
start/stop/restart` -- so on those machines the wrapped commands an operator is
supposed to recover with were the ones that could not run, leaving raw
`brew services` as the only way back to a running stack.

The tap that owns an installed keg is recorded by the install itself, in the
keg's `INSTALL_RECEIPT.json`; these tests pin that it is read rather than
guessed, and that the bare name survives when there is no keg to read.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def cellar(monkeypatch, tmp_path):
    """A fake Homebrew prefix; `_which("brew")` points inside it."""
    prefix = tmp_path / "opt" / "homebrew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "brew").write_text("#!/bin/sh\n", encoding="utf-8")
    (prefix / "Cellar").mkdir()
    monkeypatch.setattr(
        ops, "_which", lambda tool: str(prefix / "bin" / "brew") if tool == "brew" else None
    )
    return prefix / "Cellar"


def _install_keg(cellar, name, version, tap):
    keg = cellar / name / version
    keg.mkdir(parents=True)
    (keg / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"source": {"tap": tap, "versions": {"stable": version}}}),
        encoding="utf-8",
    )


def test_formula_spec_is_qualified_from_the_kegs_own_receipt(cellar):
    _install_keg(cellar, "nyxgpt-api", "3.0.0", "dkblinux98/nyxgpt-local")

    assert ops._brew_formula_spec("nyxgpt-api") == "dkblinux98/nyxgpt-local/nyxgpt-api"


def test_formula_spec_falls_back_to_the_bare_name_when_nothing_is_installed(cellar):
    assert ops._brew_formula_spec("nyxgpt-api") == "nyxgpt-api"


def test_formula_spec_leaves_core_formulas_bare(cellar):
    _install_keg(cellar, "ollama", "0.5.0", "homebrew/core")

    assert ops._brew_formula_spec("ollama") == "ollama"


def test_formula_spec_survives_a_malformed_receipt(cellar):
    keg = cellar / "nyxgpt-web" / "3.0.0"
    keg.mkdir(parents=True)
    (keg / "INSTALL_RECEIPT.json").write_text("{ truncated", encoding="utf-8")

    assert ops._brew_formula_spec("nyxgpt-web") == "nyxgpt-web"


def test_restart_uses_the_fully_qualified_formula(monkeypatch, cellar):
    _install_keg(cellar, "nyxgpt-api", "3.0.0", "dkblinux98/nyxgpt-local")
    seen: list[list[str]] = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **_k: seen.append(list(cmd)) or _cp())

    results = ops._restart_brew_service("nyxgpt-api")

    assert seen == [["brew", "services", "restart", "dkblinux98/nyxgpt-local/nyxgpt-api"]]
    # The *message* still names the service the operator sees in
    # `brew services list` -- only the argument is qualified.
    assert results[0].message == "Restarted brew service: nyxgpt-api"


def test_stop_uses_the_fully_qualified_formula(monkeypatch, cellar):
    _install_keg(cellar, "nyxgpt-web", "3.0.0", "dkblinux98/nyxgpt")
    seen: list[list[str]] = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **_k: seen.append(list(cmd)) or _cp())
    monkeypatch.setattr(ops, "_brew_service_is_registered", lambda _n: False)

    ops._stop_brew_service("nyxgpt-web")

    assert ["brew", "services", "stop", "dkblinux98/nyxgpt/nyxgpt-web"] in seen


def test_formula_installed_probe_uses_the_fully_qualified_formula(monkeypatch, cellar):
    _install_keg(cellar, "nyxgpt-api", "2.1.0", "dkblinux98/nyxgpt")
    seen: list[list[str]] = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **_k: seen.append(list(cmd)) or _cp())

    assert ops._brew_formula_installed("nyxgpt-api") is True
    assert seen == [["brew", "list", "--formula", "dkblinux98/nyxgpt/nyxgpt-api"]]
