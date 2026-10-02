"""A `Diagnostics on failure` step must still have a cluster to ask (#3990).

**The defect this pins.** Every kind-based smoke script ends its EXIT trap with
`nyxgpt ops down --kubernetes`, and on the Kubernetes path that *deletes the
kind cluster* (`ops._delete_kind_cluster`). The trap runs when the script
exits -- which is before the workflow's next step. So a workflow that put a
`Diagnostics on failure` step after the script had, on every red run, no
cluster, no kubeconfig context and no node container: forty lines of
`connection refused` and `The connection to the server localhost:8080 was
refused` where the Pod logs should have been.

`k8s-local-smoke.yml` run 37006300831 is that output. The red was
`[FAIL] the api accepted the error but GlitchTip never received it` -- a
#3990 assertion whose only possible evidence is the GlitchTip and api Pod
logs, and not one line of either survived.

**Why a guard and not just a fix.** The workaround in place before was to
hand-copy individual `kubectl logs` calls into the script's own trap (where
the cluster is still alive), one Pod at a time, as each new assertion was
found to be undiagnosable. That list cannot keep up with the assertions, and
its presence makes the workflow step *look* like it works. The ordering is the
real fix -- `NYXGPT_SMOKE_KEEP_UP=1` on the smoke step, teardown in a later
`always()` step -- and this guard is derived from the workflows rather than
maintained as a list, so the next workflow to add a diagnostics step is held
to it too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The teardown that makes a later step blind, wherever it is reached from: the
# scripts call it through their EXIT trap, so grepping the *script* is what
# identifies a smoke whose cluster does not outlive it.
TEARDOWN_IN_TRAP = "ops down --kubernetes"


def _smoke_scripts_that_delete_the_cluster() -> set[str]:
    """`scripts/*.sh` whose EXIT trap tears the kind cluster down."""
    found = set()
    for script in (REPO_ROOT / "scripts").glob("*.sh"):
        text = script.read_text(encoding="utf-8")
        if TEARDOWN_IN_TRAP in text and "trap cleanup EXIT" in text:
            found.add(f"scripts/{script.name}")
    return found


def _steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) or [] if isinstance(s, dict)]


def _runs_one_of(step: dict, scripts: set[str]) -> str | None:
    run = step.get("run") or ""
    for script in scripts:
        if script in run:
            return script
    return None


def _diagnostics_before_teardown(
    steps: list[dict], smoke_index: int
) -> tuple[int | None, int | None]:
    """Indices of the first `failure()` step and the first kind teardown after the smoke."""
    diagnostics = teardown = None
    for index, step in enumerate(steps[smoke_index + 1 :], start=smoke_index + 1):
        condition = str(step.get("if") or "")
        run = str(step.get("run") or "")
        if diagnostics is None and "failure()" in condition:
            diagnostics = index
        if teardown is None and "kind delete cluster" in run:
            teardown = index
    return diagnostics, teardown


def _jobs_running_a_cluster_deleting_smoke() -> list[tuple[Path, str, list[dict], int, str]]:
    scripts = _smoke_scripts_that_delete_the_cluster()
    assert scripts, "no smoke script tears the cluster down any more -- this guard is stale"

    located = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job_name, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            steps = _steps(job)
            for index, step in enumerate(steps):
                script = _runs_one_of(step, scripts)
                if script is not None:
                    located.append((workflow, job_name, steps, index, script))
    return located


def test_a_diagnostics_step_is_not_placed_after_the_cluster_is_gone() -> None:
    """Whoever wants diagnostics must keep the cluster up and tear it down later.

    Both halves are required and neither is sufficient: without the env the
    step runs against a deleted cluster, and without the later teardown step
    the cluster would simply leak.
    """
    checked = 0
    for workflow, job_name, steps, index, script in _jobs_running_a_cluster_deleting_smoke():
        diagnostics, teardown = _diagnostics_before_teardown(steps, index)
        if diagnostics is None:
            # No diagnostics step to protect: the script's trap is the whole
            # record, which is a choice, not this defect.
            continue
        checked += 1
        where = f"{workflow.name}:{job_name} (runs {script})"
        env = steps[index].get("env") or {}
        assert str(env.get("NYXGPT_SMOKE_KEEP_UP")) == "1", (
            f"{where} has a `failure()` diagnostics step, but lets the script's EXIT trap "
            f"delete the kind cluster first -- set NYXGPT_SMOKE_KEEP_UP: '1' on the smoke step"
        )
        assert teardown is not None, (
            f"{where} keeps the cluster up for diagnostics and never tears it down -- "
            f"add an `if: always()` `kind delete cluster` step after the diagnostics"
        )
        assert teardown > diagnostics, (
            f"{where} tears the cluster down at step {teardown}, before the diagnostics at "
            f"step {diagnostics} -- the diagnostics would read a deleted cluster"
        )

    assert checked >= 2, (
        "expected at least k8s-local-smoke and k8s-artifact-smoke to be covered; "
        f"only {checked} job(s) matched, so this guard has stopped looking at anything"
    )


def test_the_local_smoke_trap_dumps_every_pod_not_a_hand_picked_one() -> None:
    """The local-run fallback must be derived from the namespace, not a list.

    `ollama-0` alone was what the trap named, because the teardown below it
    made the workflow step useless -- so each assertion added afterwards was
    undiagnosable until someone noticed and copied another `kubectl logs` in.
    A loop over `get pods -o name` cannot fall behind the assertions.
    """
    text = (REPO_ROOT / "scripts" / "k8s-local-smoke.sh").read_text(encoding="utf-8")
    cleanup = text.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]
    assert "get pods -o name" in cleanup, "the trap no longer derives the Pod list"
    assert 'kubectl -n "$NAMESPACE" logs "$pod"' in cleanup, "the trap logs no Pod at all"
