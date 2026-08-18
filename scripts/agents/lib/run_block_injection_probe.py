#!/usr/bin/env python3
"""Fault-injection probe for the huddle-dispatch ``run:`` block (#3837).

``workflow_script_guard.py`` proves the *construct* is gone. This probe proves
the *behavior*, in the shape ``escalation_script_probe.py`` established for
#3820: it takes the real shell body of a workflow step straight out of the
YAML and executes it under ``bash`` with the step's collaborators stubbed, so
what runs here is the shell Actions runs.

The defect (CodeQL alert #124, critical) was in
``huddle_decision_dispatch.yml``'s "Restart the fix cycle" step::

    DECISION="${{ steps.decide.outputs.decision }}"
    ...
    bash -lc "cd '$GITHUB_WORKSPACE' && ... set_issue_status '$ISSUE' ..."

``${{ }}`` substitution happens before bash parses the body, so both values
were shell **source**, and ``$ISSUE`` was substituted a second time into the
nested ``bash -lc`` command string -- two shells parse it. The job carries
``REVIEW_AGENT_TOKEN`` and triggers on ``issue_comment``.

**Honest scope.** The *live* exploitability of this instance is bounded by a
constraint two files away: ``huddle_state.py``'s ``decision()`` returns a value
from a closed set, and ``ISSUE`` is ``sed``-extracted digits. So this was a
construct one edit away from exploitable, not a live exploit -- and nothing at
the interpolation site said so, which is the whole reason it is being removed
rather than annotated. The probe therefore *injects the condition* per D-006
(the ``macos-brew-smoke.yml`` template): it feeds a hostile value through the
step output and shows the pre-fix body executes it while the current body does
not.

Two halves, both asserted:

``vulnerable``
    The pre-fix body (kept here verbatim as a fixture, as
    ``test_create_issue_blocks.sh`` keeps the pre-#3836 script) with the
    payload substituted where ``${{ }}`` would have put it. It must execute
    the injected command -- proving the reproduction still reproduces.

``fixed``
    The body as it stands in the workflow *today*, extracted live so it cannot
    drift, with the same payload arriving through the environment. It must not
    execute the injected command, and the payload must survive intact into the
    comment the step posts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/huddle_decision_dispatch.yml")
DISPATCH_STEP = "Restart the fix cycle"

#: A decision value carrying a shell break-out. The closing ``X="`` re-balances
#: the quote the pre-fix assignment opened, so the devolved line parses and the
#: injected command actually runs -- an unbalanced payload would only produce a
#: syntax error, which proves less.
HOSTILE_DECISION = 'proceed"; touch "$CANARY_OUTER"; X="'

#: An issue number carrying a break-out for the *nested* shell: the pre-fix
#: form substituted it inside ``bash -lc "... '$ISSUE' ..."``, so a single
#: quote escapes into the inner login shell.
HOSTILE_ISSUE = "123'; touch \"$CANARY_INNER\"; :'"

#: The step body as it stood before #3837, verbatim. Kept as a fixture because
#: the fix deleted it: a reproduction that cannot be re-run is not evidence.
#: ``__DECISION__`` / ``__ISSUE__`` / ``__PR__`` mark where ``${{ }}``
#: substitution pasted the values in as source.
VULNERABLE_BODY = """\
set -euo pipefail
PR="__PR__"
ISSUE="__ISSUE__"
DECISION="__DECISION__"

echo "::group::Dispatching huddle decision '$DECISION' for issue #$ISSUE"

bash -lc "cd '$GITHUB_WORKSPACE' && \\
  source scripts/agents/lib/gh_project.sh && \\
  load_config && \\
  require_gh_auth && \\
  set_issue_status '$ISSUE' \\"\\$STATUS_IN_PROGRESS\\" && \\
  assign_and_trigger_developer '$ISSUE'"

DEV_AGENT="$DEV_AGENT"
BODY=$(printf '%s\\n\\n%s' \\
  "Huddle decision dispatched: \\`${DECISION}\\`" \\
  "Issue #${ISSUE} returned to In Progress.")
gh pr comment "$PR" --body "$BODY"

echo "::endgroup::"
"""

#: Stubs for the collaborators the step sources. Recording no-ops: the probe
#: is about what the shell *parses*, not about GitHub state.
GH_PROJECT_STUB = """\
load_config() { STATUS_IN_PROGRESS="In Progress"; export STATUS_IN_PROGRESS; }
require_gh_auth() { :; }
set_issue_status() { printf 'set_issue_status %s %s\\n' "$1" "$2" >> "$CALL_LOG"; }
assign_and_trigger_developer() { printf 'assign %s\\n' "$1" >> "$CALL_LOG"; }
"""

GH_STUB = """\
#!/usr/bin/env bash
# Record `gh pr comment <pr> --body <body>` so the probe can read what the
# step would have posted.
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "comment" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--body" ]; then printf '%s' "$2" > "$COMMENT_BODY_FILE"; fi
    shift
  done
fi
exit 0
"""


def extract_run(workflow: Path = WORKFLOW, step_name: str = DISPATCH_STEP) -> str:
    """Return the inline ``run:`` body of ``step_name`` in ``workflow``."""
    document = yaml.safe_load(workflow.read_text())
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == step_name:
                body = step.get("run")
                if not isinstance(body, str):
                    raise ValueError(f"step {step_name!r} has no inline run: body")
                return body
    raise ValueError(f"step {step_name!r} not found in {workflow}")


def devolve_to_vulnerable(
    decision: str = HOSTILE_DECISION, issue: str = HOSTILE_ISSUE, pr: str = "3837"
) -> str:
    """Substitute the payload into the pre-fix body the way ``${{ }}`` did.

    Verbatim, with no escaping -- escaping it would be the fix.
    """
    return (
        VULNERABLE_BODY.replace("__DECISION__", decision)
        .replace("__ISSUE__", issue)
        .replace("__PR__", pr)
    )


class Sandbox:
    """A throwaway ``GITHUB_WORKSPACE`` with the step's collaborators stubbed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        (self.workspace / "scripts" / "agents" / "lib").mkdir(parents=True)
        (self.workspace / "scripts" / "agents" / "lib" / "gh_project.sh").write_text(
            GH_PROJECT_STUB
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.bin = bin_dir
        self.call_log = root / "calls.log"
        self.comment_body = root / "comment-body.txt"
        self.canary_outer = root / "INJECTED-OUTER"
        self.canary_inner = root / "INJECTED-INNER"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            **os.environ,
            "GITHUB_WORKSPACE": str(self.workspace),
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "CALL_LOG": str(self.call_log),
            "COMMENT_BODY_FILE": str(self.comment_body),
            "CANARY_OUTER": str(self.canary_outer),
            "CANARY_INNER": str(self.canary_inner),
            "DEV_AGENT": "myGPT-developer-agent",
        }
        env.update(extra or {})
        return env

    @property
    def injected(self) -> bool:
        return self.canary_outer.exists() or self.canary_inner.exists()

    def run(
        self, body: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        script = self.root / "step.sh"
        script.write_text(body)
        return subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            cwd=self.workspace,
            env=self.env(extra_env),
            check=False,
        )


def probe_vulnerable() -> str:
    """Run the pre-fix body; return what it injected.

    Raises ``AssertionError`` if the payload does *not* execute -- a
    reproduction that stopped reproducing proves nothing about the fix.
    """
    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(Path(tmp))
        result = box.run(devolve_to_vulnerable())
        if not box.injected:
            raise AssertionError(
                "the pre-fix (interpolated) form executed no injected command -- "
                "the fault was not injected, so this run proves nothing.\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return (
            f"outer shell: {box.canary_outer.exists()}, "
            f"nested bash -lc: {box.canary_inner.exists()}"
        )


def probe_fixed(body: str | None = None) -> str:
    """Run the current body with the payload in ``env:``; return the comment.

    Raises ``AssertionError`` if anything executes, or if the payload is
    mangled rather than carried through as text.
    """
    body = extract_run() if body is None else body
    if "${{" in body:
        raise AssertionError(f"the current body still interpolates an expression:\n{body}")
    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(Path(tmp))
        result = box.run(
            body,
            {"PR": "3837", "ISSUE": HOSTILE_ISSUE, "DECISION": HOSTILE_DECISION},
        )
        if box.injected:
            raise AssertionError(
                "the current body EXECUTED the injected command -- the fix does not hold.\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        if result.returncode != 0:
            raise AssertionError(
                f"the current body failed to run with a hostile decision "
                f"(exit {result.returncode}):\n{result.stderr}"
            )
        calls = box.call_log.read_text() if box.call_log.exists() else ""
        if HOSTILE_ISSUE not in calls:
            raise AssertionError(
                "the issue number did not reach set_issue_status as one intact "
                f"argument.\ncalls:\n{calls}"
            )
        posted = box.comment_body.read_text() if box.comment_body.exists() else ""
        if HOSTILE_DECISION not in posted:
            raise AssertionError(
                "the posted comment does not contain the decision intact.\n"
                f"expected substring: {HOSTILE_DECISION!r}\nbody: {posted!r}"
            )
        return posted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", type=Path, default=WORKFLOW, help="workflow file")
    parser.add_argument("--step", default=DISPATCH_STEP, help="step name")
    args = parser.parse_args(argv)

    print(f"Probing step: {args.step}")
    print(f"Decision payload: {HOSTILE_DECISION!r}")
    print(f"Issue payload:    {HOSTILE_ISSUE!r}\n")

    print("[1/2] pre-fix form (fault injected) must execute the payload ...")
    where = probe_vulnerable()
    print(f"      reproduced -- injected command ran ({where})\n")

    print("[2/2] current form must run inert with the payload intact ...")
    posted = probe_fixed(extract_run(args.workflow, args.step))
    print("      nothing executed; the decision survived as text:")
    for line in posted.splitlines():
        print(f"      | {line}")

    print("\nPASS: both halves demonstrated.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
