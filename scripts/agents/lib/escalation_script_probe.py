#!/usr/bin/env python3
"""Fault-injection probe for the fatal-error escalation script (#3820).

The regression guard (``workflow_script_guard.py``) proves the *construct* is
gone. This probe proves the *behavior*: it extracts the real ``script:`` body
of a workflow step straight out of the YAML and executes it under Node with
stubbed ``context``/``github``/``core`` globals, so what runs here is the same
JavaScript GitHub Actions runs.

Two halves, per D-006 -- a diagnosis with no quote character passes either way,
which is exactly why the defect shipped:

``fixed``
    Run the script as it stands, with a hostile diagnosis in the environment
    (apostrophe, double quote, backtick, ``${``, newline, backslash). The
    script must complete and the posted comment body must contain that text
    intact.

``vulnerable``
    Rewrite each ``process.env.NAME`` read back into the pre-fix construct --
    a single-quoted JS literal holding the substituted text, which is what
    ``${{ }}`` produced before the fix -- and run that. It must die with a
    ``SyntaxError``, reproducing run 31959968196.

Both halves are asserted, so the probe fails if the bug is reintroduced *and*
if the reproduction stops reproducing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

DEVELOPER_WORKFLOW = Path(".github/workflows/developer_auto_implement.yml")
ESCALATION_STEP = "Escalate fatal error (Phase 1+2+3 - no retry)"

#: A diagnosis carrying every character that can terminate a JS literal:
#: apostrophe, double quote, backtick, template-substitution opener, newline
#: and backslash. The apostrophe in "it's" is the exact shape of the reported
#: failure.
HOSTILE_DIAGNOSIS = (
    "it's the branch's fault: \"Submit PR\" failed because "
    "`git push` hit a wall\nsecond line with a backslash \\ and ${injected}"
)

#: Environment the escalation script reads. Values are deliberately plain
#: except the diagnosis, which is the free-form prose that broke it.
PROBE_ENV = {
    "ERROR_CLASS": "fatal:auth_failure",
    "PHASE2_STATUS": "FATAL",
    "PHASE3_STATUS": "FATAL",
    "PHASE3_DIAGNOSIS": HOSTILE_DIAGNOSIS,
}

_ENV_READ = re.compile(r"process\.env\.([A-Z0-9_]+)(?:\s*\|\|\s*'')?")

_HARNESS = """\
'use strict';
let captured = null;
const context = {
  payload: { issue: { number: 3820 }, pull_request: { number: 3820 } },
  repo: { owner: 'dkblinux98', repo: 'nyxGPT' },
  runId: 31959968196,
};
const github = { rest: { issues: {
  createComment: async (params) => { captured = params; return { data: {} }; },
  addAssignees: async () => ({ data: {} }),
  get: async () => ({ data: { assignees: [] } }),
} } };
const core = {
  setOutput: () => {},
  setFailed: () => {},
  notice: () => {},
};
(async () => {
__SCRIPT__
})().then(
  () => {
    process.stdout.write(JSON.stringify({ body: captured ? captured.body : null }));
  },
  (err) => {
    // A rejected promise from the script's own logic is not what this probe
    // is about -- only a parse failure is -- but surface it rather than
    // reporting a silent success.
    process.stderr.write('script rejected: ' + err + '\\n');
    process.exit(3);
  },
);
"""


def extract_script(workflow: Path = DEVELOPER_WORKFLOW, step_name: str = ESCALATION_STEP) -> str:
    """Return the inline ``with.script`` body of ``step_name`` in ``workflow``."""
    document = yaml.safe_load(workflow.read_text())
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == step_name:
                script = (step.get("with") or {}).get("script")
                if not isinstance(script, str):
                    raise ValueError(f"step {step_name!r} has no inline script body")
                return script
    raise ValueError(f"step {step_name!r} not found in {workflow}")


def devolve_to_vulnerable(script: str, env: dict[str, str]) -> str:
    """Rewrite ``process.env.NAME`` reads back into the pre-fix construct.

    ``${{ steps.x.outputs.y }}`` was substituted into the source before parsing,
    producing ``const v = '<the text, verbatim>';``. Reproduce that by putting
    the value between single quotes with no escaping -- escaping it would be
    the fix.
    """

    def replace(match: re.Match[str]) -> str:
        return "'" + env.get(match.group(1), "") + "'"

    return _ENV_READ.sub(replace, script)


def run_script(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute ``script`` under Node with github-script's globals stubbed."""
    indented = "\n".join(f"  {line}" if line.strip() else line for line in script.splitlines())
    harness = _HARNESS.replace("__SCRIPT__", indented)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(harness)
        return subprocess.run(
            ["node", str(path)],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            check=False,
        )


def probe_fixed(script: str, env: dict[str, str] | None = None) -> str:
    """Run the current script; return the posted comment body.

    Raises ``AssertionError`` if it fails to run or drops the diagnosis.
    """
    env = env or PROBE_ENV
    result = run_script(script, env)
    if result.returncode != 0:
        raise AssertionError(
            "escalation script failed to execute with a quote-bearing diagnosis "
            f"(exit {result.returncode}):\n{result.stderr}"
        )
    body = str(json.loads(result.stdout).get("body") or "")
    if not body:
        raise AssertionError("escalation script posted no comment")
    diagnosis = env["PHASE3_DIAGNOSIS"]
    if diagnosis not in body:
        raise AssertionError(
            "posted comment does not contain the diagnosis intact.\n"
            f"expected substring: {diagnosis!r}\nbody: {body!r}"
        )
    return body


def probe_vulnerable(script: str, env: dict[str, str] | None = None) -> str:
    """Run the pre-fix form; return its stderr.

    Raises ``AssertionError`` if it does *not* fail -- a reproduction that
    stopped reproducing proves nothing about the fix.
    """
    env = env or PROBE_ENV
    result = run_script(devolve_to_vulnerable(script, env), env)
    if result.returncode == 0:
        raise AssertionError(
            "the pre-fix (interpolated) form ran cleanly -- the fault was not "
            "injected, so this run proves nothing"
        )
    if "SyntaxError" not in result.stderr:
        raise AssertionError(
            "the pre-fix form failed for some reason other than a parse error:\n" f"{result.stderr}"
        )
    return result.stderr


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", type=Path, default=DEVELOPER_WORKFLOW, help="workflow file")
    parser.add_argument("--step", default=ESCALATION_STEP, help="step name")
    args = parser.parse_args(argv)

    script = extract_script(args.workflow, args.step)

    print(f"Probing step: {args.step}")
    print(f"Diagnosis payload: {PROBE_ENV['PHASE3_DIAGNOSIS']!r}\n")

    print("[1/2] pre-fix form (fault injected) must fail to parse ...")
    stderr = probe_vulnerable(script)
    first = next((ln for ln in stderr.splitlines() if "SyntaxError" in ln), "SyntaxError")
    print(f"      reproduced: {first.strip()}\n")

    print("[2/2] current form must escalate with the diagnosis intact ...")
    body = probe_fixed(script)
    print("      escalation comment posted, diagnosis preserved verbatim:")
    for line in body.splitlines():
        print(f"      | {line}")

    print("\nPASS: both halves demonstrated.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
