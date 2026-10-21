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

A third section (#4192) answers the other half of the same question: when the
output is too long to carry whole, does the excerpt still contain the
diagnosis? `_bounded_output` rescues "salient" lines out of the elided middle
for exactly that reason (#3861), and until 2026-10-21 it looked for the keyword
at the START of the line -- which no real producer writes, because the output
has already been labelled by whatever wrapped it (BuildKit's `#23 90.52 `,
npm's own `npm `). So the rescue was vacuous on every tool ops shells out to,
with every test green, because the tests supplied their own column-0 fixtures.
This section runs the real bytes of a real failed `docker build` (captured in
`tests/fixtures/bounded_output/`, see its README) through the real excerpt, and
fault-injects the retired anchored pattern to prove the check is not vacuous
either.

Usage: python3 scripts/ops-subprocess-output-smoke.py
Exit 0 = all three sections held.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from nyxgpt import ops

#: The real `docker build` -> `npm ci` network failure, captured by running it.
_PREFIXED_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "bounded_output"
    / "docker-build-npm-ci-network-failure.txt"
)
#: npm's own name for what went wrong, which is the line the excerpt must keep.
_PREFIXED_MARKER = "npm error code"
#: `_OUTPUT_EXCERPT_SALIENT_RE` as it stood before #4192: the same keywords,
#: anchored to the start of the line. Injected, not described.
_RETIRED_ANCHORED_SALIENT_RE = re.compile(
    r"^\s*(?:error|warning|fatal|failed|exception|traceback)\b[: ]",
    re.IGNORECASE,
)

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


def _check_prefixed_producer_output() -> int:
    """Section 3 (#4192): a long, line-prefixed failure still carries its cause.

    Runs the captured `docker build` -> `npm ci` output through the real
    `_bounded_output`, then injects the retired anchored pattern and requires
    the cause to disappear. The second half is the point: with the anchor in
    place this fixture rescues nothing at all, which is how the mechanism could
    be shipped broken past a green suite.
    """
    raw = _PREFIXED_FIXTURE.read_text(encoding="utf-8")
    lines = raw.strip().splitlines()
    if len(lines) <= ops._OUTPUT_EXCERPT_VERBATIM_MAX_LINES:
        print(
            f"FAIL: the fixture is {len(lines)} lines, at or under the "
            f"{ops._OUTPUT_EXCERPT_VERBATIM_MAX_LINES}-line verbatim floor -- "
            "nothing would be elided, so this section would prove nothing"
        )
        return 1

    # Which lines only the rescue can carry: salient, and not already kept by
    # the head/tail window. Derived from the module's own constants so a
    # recapture of the fixture (different step numbers, different elapsed
    # times) cannot turn this into a test of two hardcoded strings.
    head, tail = ops._OUTPUT_EXCERPT_HEAD_LINES, ops._OUTPUT_EXCERPT_TAIL_LINES
    kept_anyway = set(lines[:head]) | set(lines[-tail:])
    rescue_only = [
        line
        for line in lines[head : len(lines) - tail]
        if line not in kept_anyway and _PREFIXED_MARKER in line
    ]
    if not rescue_only:
        print(
            f"FAIL: no {_PREFIXED_MARKER!r} line sits in the elided middle of this "
            "fixture, so the rescue is not what is being measured -- recapture it"
        )
        return 1

    excerpt = ops._bounded_output(raw)
    if "lines omitted" not in excerpt:
        print("FAIL: the fixture was not elided at all; the premise does not hold")
        return 1
    missing = [line for line in rescue_only if line not in excerpt]
    if missing:
        print("FAIL: the excerpt of a real build failure dropped the line naming its cause:")
        for line in missing:
            print(f"    {line}")
        print("--- excerpt ---")
        print(excerpt)
        print("--- end ---")
        return 1
    print(
        f"PASS: a {len(lines)}-line prefixed build failure still names its cause "
        f"({len(rescue_only)} line(s) carried out of the elided middle)"
    )

    original = ops._OUTPUT_EXCERPT_SALIENT_RE
    ops._OUTPUT_EXCERPT_SALIENT_RE = _RETIRED_ANCHORED_SALIENT_RE  # type: ignore[assignment]
    try:
        injected = ops._bounded_output(raw)
    finally:
        ops._OUTPUT_EXCERPT_SALIENT_RE = original  # type: ignore[assignment]

    survived = [line for line in rescue_only if line in injected]
    if survived:
        print(
            "FAIL: the cause survived the retired line-start anchor -- this "
            "section proves nothing about the pattern"
        )
        return 1
    if "error/warning line(s) among them follow" in injected:
        print("FAIL: the retired anchor rescued something; the injection is not faithful")
        return 1
    print("PASS: anchored to the line start it rescues nothing at all (the #4192 defect)")
    return 0


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

    return _check_prefixed_producer_output()


if __name__ == "__main__":
    raise SystemExit(main())
