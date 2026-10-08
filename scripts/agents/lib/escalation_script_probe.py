#!/usr/bin/env python3
"""Fault-injection probe for the fatal-error escalation script (#3820).

The regression guard (``workflow_script_guard.py``) proves the *construct* is
gone. This probe proves the *behavior*: it extracts the real ``script:`` body
of a workflow step straight out of the YAML and executes it under Node with
stubbed ``context``/``github``/``core`` globals, so what runs here is the same
JavaScript GitHub Actions runs.

**The channel moved, the property did not (#4176).** The escalation's one-line
diagnosis is no longer a step output read from ``process.env``: it is now
COMPOSED from the run's evidence by a shell step and handed over in a file,
because the lookup on the error class printed "Error type could not be
determined" over runs whose cause was already known. So this probe injects the
hostile text through the FILE the script reads, and its vulnerable half
devolves ``fs.readFileSync(...)`` into the same unescaped literal that ``${{ }}``
used to produce. That also closes a gap: ``/tmp/failure_detail.txt`` has always
carried raw log excerpts into this script and was never probed.

Two halves, per D-006 -- a diagnosis with no quote character passes either way,
which is exactly why the defect shipped:

``fixed``
    Run the script as it stands, with a hostile diagnosis in the environment
    (apostrophe, double quote, backtick, ``${``, newline, backslash). The
    script must complete and the posted comment body must contain that text
    intact.

``vulnerable``
    Rewrite each ``process.env.NAME`` read *and each file read* back into the
    pre-fix construct -- a single-quoted JS literal holding the text, which is
    what ``${{ }}`` produced before the fix -- and run that. It must die with a
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

#: Environment the escalation script reads. Values are deliberately plain:
#: since #4176 the free-form prose arrives in a FILE (see `HOSTILE_FILES`),
#: because the headline is composed from the run's evidence by a shell step
#: rather than looked up on the error class in this script.
PROBE_ENV = {
    "ERROR_CLASS": "fatal:auth_failure",
    "PHASE2_STATUS": "FATAL",
    "PHASE3_STATUS": "FATAL",
    "HEADLINE_PHASE": "Phase 3 (Claude reasoning)",
}

#: The files the script reads, keyed by the env var that overrides each path,
#: and what the probe puts in them. BOTH carry text the pipeline did not
#: author: the headline composes a model's prose, and the failure detail is a
#: raw log excerpt -- which has been read into this script since #3360 and was
#: never probed until #4176 moved the headline alongside it.
HOSTILE_FILES = {
    "HEADLINE_FILE": HOSTILE_DIAGNOSIS,
    "FAILURE_DETAIL_FILE": "**Failed step(s):**\n```\nit's `Final Verification`\n```",
}

_ENV_READ = re.compile(r"process\.env\.([A-Z0-9_]+)(?:\s*\|\|\s*'[^']*')?")

#: `fs.readFileSync(<path expr>, 'utf8')`, with or without a trailing
#: `.trim()`. The pre-fix construct for a FILE channel is the same one #3820
#: fixed for a step output: the text pasted in as source.
_FILE_READ = re.compile(r"fs\.readFileSync\([^)]*\)(?:\.trim\(\))?")

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


def devolve_to_vulnerable(script: str, env: dict[str, str], file_payload: str | None = None) -> str:
    """Rewrite the script's data reads back into the pre-fix construct.

    ``${{ steps.x.outputs.y }}`` was substituted into the source before parsing,
    producing ``const v = '<the text, verbatim>';``. Reproduce that by putting
    the value between single quotes with no escaping -- escaping it would be
    the fix.

    Applied to ``process.env.NAME`` reads and, since #4176, to
    ``fs.readFileSync(...)`` reads: a file is the channel the composed headline
    and the Phase 1 failure detail arrive on, and "interpolated as source" is
    the same defect whichever side the text came from. ``file_payload``
    defaults to the hostile diagnosis.
    """

    def replace_env(match: re.Match[str]) -> str:
        return "'" + env.get(match.group(1), "") + "'"

    payload = HOSTILE_DIAGNOSIS if file_payload is None else file_payload
    devolved = _ENV_READ.sub(replace_env, script)
    return _FILE_READ.sub(lambda _: "'" + payload + "'", devolved)


def run_script(
    script: str, env: dict[str, str], files: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Execute ``script`` under Node with github-script's globals stubbed.

    ``files`` maps each path-override env var to the content to put in a temp
    file for it. The script reads its production paths by default, so a probe
    that did not do this would silently read whatever a previous real run left
    in ``/tmp`` -- or nothing, and report an empty diagnosis as a pass.
    """
    indented = "\n".join(f"  {line}" if line.strip() else line for line in script.splitlines())
    harness = _HARNESS.replace("__SCRIPT__", indented)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(harness)
        file_env: dict[str, str] = {}
        for var, content in (files or {}).items():
            target = Path(tmp) / f"{var.lower()}.txt"
            target.write_text(content)
            file_env[var] = str(target)
        return subprocess.run(
            ["node", str(path)],
            capture_output=True,
            text=True,
            env={**os.environ, **env, **file_env},
            check=False,
        )


def probe_fixed(
    script: str, env: dict[str, str] | None = None, files: dict[str, str] | None = None
) -> str:
    """Run the current script; return the posted comment body.

    Raises ``AssertionError`` if it fails to run or drops the diagnosis.
    """
    env = env or PROBE_ENV
    files = HOSTILE_FILES if files is None else files
    result = run_script(script, env, files)
    if result.returncode != 0:
        raise AssertionError(
            "escalation script failed to execute with a quote-bearing diagnosis "
            f"(exit {result.returncode}):\n{result.stderr}"
        )
    body = str(json.loads(result.stdout).get("body") or "")
    if not body:
        raise AssertionError("escalation script posted no comment")
    diagnosis = files.get("HEADLINE_FILE", env.get("PHASE3_DIAGNOSIS", HOSTILE_DIAGNOSIS))
    if diagnosis not in body:
        raise AssertionError(
            "posted comment does not contain the diagnosis intact.\n"
            f"expected substring: {diagnosis!r}\nbody: {body!r}"
        )
    return body


def probe_vulnerable(
    script: str, env: dict[str, str] | None = None, files: dict[str, str] | None = None
) -> str:
    """Run the pre-fix form; return its stderr.

    Raises ``AssertionError`` if it does *not* fail -- a reproduction that
    stopped reproducing proves nothing about the fix.
    """
    env = env or PROBE_ENV
    files = HOSTILE_FILES if files is None else files
    result = run_script(devolve_to_vulnerable(script, env, files.get("HEADLINE_FILE")), env, files)
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
    print(f"Diagnosis payload (via {'/'.join(HOSTILE_FILES)}): {HOSTILE_DIAGNOSIS!r}\n")

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
