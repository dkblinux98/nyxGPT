#!/usr/bin/env python3
"""Prove a real failing subprocess's output reaches the ops failure log (#3783).

The unit tests for this mock `subprocess.run`, so they show the formatting but
never the thing that actually went wrong: on the rc9 cloud round a *real* `pip
install` refused a *real* artifact with "requires a different Python", ops
logged only `rc=1` plus the argv, and the owner had to SSH to the instance and
re-run the command by hand to read the reason.

This script reproduces that scenario on the machine it runs on -- a real venv,
a real `pip`, a real package whose `requires-python` this interpreter cannot
satisfy -- and asserts the refusal text lands both in the WARNING message ops
emits and in the details an ops step would report.

It then runs the same scenario with the excerpt suppressed, which is the
pre-fix behaviour (output carried only in the structured `extra`), and asserts
the diagnostic is then *absent*. Without that second half the check would be
green on any build, fixed or not -- the #3753 lesson: a job that only runs the
happy path passes on every machine that fails to reproduce the bug.

Usage: python3 scripts/ops-subprocess-output-smoke.py
Exit 0 = both halves held.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from nyxgpt import ops

# pip's refusal wording has been stable across many releases; match on the
# part that is the actual diagnosis rather than on exact punctuation.
_REFUSAL_MARKER = "requires a different Python"

_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "nyxgpt-3783-probe"
version = "0.0.1"
requires-python = ">=3.99"
"""


class _Capture(logging.Handler):
    """Collect fully-formatted `nyxgpt.ops` messages, as a log sink would see them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _write_probe_package(workdir: Path) -> Path:
    """Write a real package no interpreter running this script can install."""
    src = workdir / "probe"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    return src


def _install_probe(probe: Path, venv_dir: Path) -> tuple[list[str], str]:
    """Run the failing `pip install` through `ops._run`; return (log messages, details)."""
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    pip = venv_dir / "bin" / "pip"

    capture = _Capture()
    ops.logger.addHandler(capture)
    previous_level = ops.logger.level
    ops.logger.setLevel(logging.DEBUG)
    try:
        cp = ops._run([str(pip), "install", str(probe)], check=False)
    finally:
        ops.logger.removeHandler(capture)
        ops.logger.setLevel(previous_level)

    if cp.returncode == 0:
        raise SystemExit("the probe install unexpectedly SUCCEEDED; the scenario did not reproduce")
    return capture.messages, ops._output_excerpt(cp)


def _failure_lines(messages: list[str]) -> list[str]:
    return [m for m in messages if "Subprocess exited non-zero" in m]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        probe = _write_probe_package(workdir)

        messages, details = _install_probe(probe, workdir / "venv")
        failures = _failure_lines(messages)
        if not failures:
            print("FAIL: ops logged no non-zero-exit record at all")
            return 1
        print("--- ops failure log record ---")
        print(failures[-1])
        print("--- end ---")

        if _REFUSAL_MARKER not in failures[-1]:
            print(f"FAIL: pip's own refusal ({_REFUSAL_MARKER!r}) is missing from the log message")
            return 1
        if _REFUSAL_MARKER not in details:
            print("FAIL: pip's refusal is missing from the result details excerpt")
            return 1
        print("PASS: the real pip refusal reached both the log message and the result details")

        # Fault injection: restore the pre-#3783 behaviour (output kept out of
        # the message) and prove this check would have caught it.
        original = ops._combined_output_excerpt

        def _suppressed(_stdout: str | None, _stderr: str | None) -> str:
            return ""

        ops._combined_output_excerpt = _suppressed  # type: ignore[assignment]
        try:
            messages, _ = _install_probe(probe, workdir / "venv-injected")
        finally:
            ops._combined_output_excerpt = original  # type: ignore[assignment]

        injected = _failure_lines(messages)
        if injected and _REFUSAL_MARKER in injected[-1]:
            print("FAIL: the diagnostic survived suppression -- this check proves nothing")
            return 1
        print("PASS: with the excerpt suppressed the refusal is invisible (the #3783 defect)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
