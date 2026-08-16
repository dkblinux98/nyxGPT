#!/usr/bin/env python3
"""Guard against GitHub Actions expression injection into script bodies.

Motivating defect (#3820): the developer agent's fatal-error escalation step
built its JavaScript by interpolating ``${{ ... }}`` workflow expressions
*inside single-quoted JS string literals*::

    const phase3Diagnosis = '${{ steps.claude_result.outputs.diagnosis }}';

``${{ }}`` substitution happens before the script is parsed, so the substituted
text becomes JavaScript *source*. A diagnosis containing an apostrophe (the
norm in free-form prose) terminates the literal early and leaves bare
identifiers behind -- the observed ``SyntaxError: Unexpected identifier
'issues'``. The step that exists to report fatal errors crashed before
reporting anything, so the run's real failure was never surfaced.

It is not merely a quoting bug: these steps hold ``github-token``, so whatever
the substituted text parses as executes with that token.

The supported shape is GitHub's documented one -- pass the value through
``env:`` and read it with ``process.env``, where it is data and never source::

    env:
      PHASE3_DIAGNOSIS: ${{ steps.claude_result.outputs.diagnosis }}
    with:
      script: |
        const phase3Diagnosis = process.env.PHASE3_DIAGNOSIS;

This module finds any remaining violation. It is used by
``tests/unit/test_workflow_script_injection.py`` and runnable as a CLI.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

#: Directory scanned when no explicit paths are given.
DEFAULT_WORKFLOW_DIR = Path(".github/workflows")

#: The construct that must never appear in a ``script:`` body.
EXPRESSION_OPEN = "${{"


@dataclass(frozen=True)
class Violation:
    """One ``${{ ... }}`` expression interpolated into a ``script:`` body."""

    path: Path
    job: str
    step: str
    line: int
    text: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: [{self.job} / {self.step}] {self.text}"


@dataclass(frozen=True)
class ScriptStep:
    """A workflow step carrying an inline ``with.script`` body."""

    path: Path
    job: str
    step: str
    line: int
    script: str


def _step_name(step: dict, index: int) -> str:
    for key in ("name", "id"):
        value = step.get(key)
        if isinstance(value, str) and value:
            return value
    return f"step[{index}]"


def _script_start_line(path: Path, script: str) -> int:
    """Best-effort 1-based line of a script body's first line.

    ``yaml.safe_load`` discards positions, and re-parsing with a node-tracking
    loader would still not survive the block-scalar folding, so anchor on the
    first non-empty line of the body instead. Line numbers here are a
    navigation aid for the report; correctness of the scan does not rest on
    them.
    """
    first = next((ln for ln in script.splitlines() if ln.strip()), "")
    if not first:
        return 1
    needle = first.strip()
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if line.strip() == needle:
            return number
    return 1


def iter_script_steps(path: Path) -> Iterator[ScriptStep]:
    """Yield every step in ``path`` that supplies an inline ``with.script``."""
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - malformed workflow
        raise ValueError(f"{path}: not parseable as YAML: {exc}") from exc
    if not isinstance(document, dict):
        return
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            with_block = step.get("with")
            if not isinstance(with_block, dict):
                continue
            script = with_block.get("script")
            if not isinstance(script, str):
                continue
            yield ScriptStep(
                path=path,
                job=str(job_name),
                step=_step_name(step, index),
                line=_script_start_line(path, script),
                script=script,
            )


def find_violations(paths: Sequence[Path]) -> list[Violation]:
    """Return every ``${{`` interpolation found in a ``script:`` body."""
    violations: list[Violation] = []
    for path in paths:
        for step in iter_script_steps(path):
            for offset, line in enumerate(step.script.splitlines()):
                if EXPRESSION_OPEN in line:
                    violations.append(
                        Violation(
                            path=step.path,
                            job=step.job,
                            step=step.step,
                            line=step.line + offset,
                            text=line.strip(),
                        )
                    )
    return violations


def workflow_files(directory: Path = DEFAULT_WORKFLOW_DIR) -> list[Path]:
    """Return every workflow file under ``directory``, sorted by name."""
    return sorted([p for p in directory.iterdir() if p.suffix in (".yml", ".yaml") and p.is_file()])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="workflow files to scan (default: every file in .github/workflows)",
    )
    args = parser.parse_args(argv)

    paths = args.paths or workflow_files()
    if not paths:
        print("No workflow files to scan.", file=sys.stderr)
        return 1

    violations = find_violations(paths)
    if not violations:
        print(f"OK: no ${{{{ }}}} interpolation in {len(paths)} workflow script bodies.")
        return 0

    print(
        f"{len(violations)} GitHub Actions expression(s) interpolated into a "
        "github-script body:",
        file=sys.stderr,
    )
    for violation in violations:
        print(f"  {violation.format()}", file=sys.stderr)
    print(
        "\nPass these values through `env:` and read them with `process.env.NAME` " "(#3820).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
