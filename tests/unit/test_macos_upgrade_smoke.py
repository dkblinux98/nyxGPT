"""Contract tests for `scripts/macos-upgrade-smoke.sh` (#4133).

That script is this repo's only executed proof that a `brew upgrade` on a host
with the stack running leaves the api executing the NEW keg's venv (D-006), and
it runs on a `macos-15` runner. Everything it couples to -- the function names
it drives inside the keg, the literal it injects to revert the fix, the strings
it greps out of `nyxgpt ops status` -- is therefore pinned here, on every PR,
in cheap tests.

Why that matters more than usual: a coupling that drifts does not turn the
macOS job red in a way anyone reads as "the gate broke". A renamed `same_tree`
makes the injection a no-op, and a no-op injection makes the "prove it fails
without the fix" half pass by looking exactly like the "prove it passes with
the fix" half (#3753's lesson, and the reason that job asserts the edit landed
at runtime too -- this is the earlier, cheaper copy of the same check).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import ops
from nyxgpt.running_build import BUILD_MISMATCH, RuntimeBuild

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "macos-upgrade-smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"
RUNNING_BUILD = REPO_ROOT / "src" / "nyxgpt" / "running_build.py"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.access(
        SCRIPT, os.X_OK
    ), f"{SCRIPT} must be executable -- the workflow runs it directly"


def test_the_injection_needle_is_a_real_signature(script):
    """The revert the job injects has to find its target.

    The script asserts this at runtime as well, so a drifted signature fails
    the macOS job rather than silently neutering it. This test is the same
    assertion one layer earlier, where it costs nothing.
    """
    needle = "def same_tree(running: str, expected: str) -> bool:"
    assert needle in script, "the script no longer names the function it injects"
    assert needle in RUNNING_BUILD.read_text(encoding="utf-8"), (
        "running_build.same_tree's signature changed, so the macOS job's injection would be a "
        "no-op -- and a no-op injection makes the fails-without-the-fix half pass vacuously"
    )


@pytest.mark.parametrize("attribute", ["_native_api_build_drift", "_reconcile_running_api_build"])
def test_the_driven_ops_attributes_exist(script, attribute):
    """The script drives the real install-step code inside the keg, by name."""
    assert f"ops.{attribute}" in script
    assert hasattr(ops, attribute), f"ops.{attribute} is gone; the macOS job would error out"


def test_the_remediation_grepped_for_is_the_one_ops_prints(script):
    """One literal, four surfaces (#4133). The script greps for it verbatim."""
    assert ops._RUNNING_BUILD_REMEDIATION == "nyxgpt ops restart api"
    assert f"grep -qF '{ops._RUNNING_BUILD_REMEDIATION}'" in script


def test_the_mismatch_word_grepped_for_is_the_one_status_prints(script, capsys):
    """AC3 is asserted by grepping `ops status` for MISMATCH, so the printer
    has to emit exactly that token -- inside the block that names this subject.

    The scoping is the point, not an implementation detail. `MISMATCH` is the
    word EVERY `ops status` subject uses for its own verdict, and since #4194
    the same output carries `Node runtime (web tier): MISMATCH ...` on any host
    whose `node` is not the declared major -- which a macOS runner's routinely
    is. A whole-transcript `grep` read that as this subject's verdict and
    reddened the injection half of the fault-injected check (run 39679510191)
    while the injected comparison was reporting a match exactly as intended.
    """
    ops._print_running_api_build(
        ops.BuildDrift(
            state=BUILD_MISMATCH,
            running=None,
            expected_prefix="/new/venv",
            expected_source="",
            detail="d",
            remediation=ops._RUNNING_BUILD_REMEDIATION,
        )
    )
    printed = capsys.readouterr().out
    assert "MISMATCH" in printed
    # The block header this subject's verdict is printed under, and the only
    # thing the script reads `MISMATCH` out of.
    assert "Running api build" in printed
    assert "/^Running api build/" in script
    assert "grep -q 'MISMATCH' <<<\"$block\"" in script
    body = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "grep -q 'MISMATCH' \"$WORK" not in body, (
        "a reader greps the whole `ops status` transcript for MISMATCH; it would "
        "report another subject's verdict as the api build's"
    )
    assert body.count("api_build_says_mismatch") >= 3, (
        "both MISMATCH readers must go through the one subject-scoped helper "
        "(its definition plus the two call sites)"
    )


def _api_match_then_node_mismatch(capsys, *, squeezed: bool) -> str:
    """What `ops status` prints for a matching api build followed by a Node of
    the wrong major -- the state a macOS runner reaches on its own."""
    from nyxgpt import node_runtime

    capsys.readouterr()
    ops._print_running_api_build(
        ops.BuildDrift(
            state=ops.BUILD_MATCH,
            running=None,
            expected_prefix="/new/venv",
            expected_source="the keg",
            detail="d",
            remediation="",
        )
    )
    wrong_major = node_runtime.NodeRuntimeReport(
        state=node_runtime.NODE_MISMATCH,
        path="/opt/homebrew/bin/node",
        version="v22.23.2",
        major=22,
        declared_major=node_runtime.NODE_MAJOR,
        detail="wrong major",
        remediation=node_runtime.NODE_REMEDIATION_COMMAND,
    )
    with patch.object(node_runtime, "node_version_report", return_value=wrong_major):
        ops._print_node_runtime()
    printed = capsys.readouterr().out
    if squeezed:
        printed = "\n".join(line for line in printed.splitlines() if line.strip()) + "\n"
    return printed


def test_the_node_runtime_line_is_its_own_section(capsys):
    """The producer half: `Node runtime` opens with a blank line, as every
    other `ops status` section header does, so an operator does not read its
    verdict as the api build's either."""
    printed = _api_match_then_node_mismatch(capsys, squeezed=False)
    assert "\n\nNode runtime (web tier): MISMATCH" in printed, printed


@pytest.mark.parametrize("squeezed", [False, True], ids=["as-printed", "no-blank-lines"])
def test_another_subjects_mismatch_is_not_read_as_the_api_builds(
    script, tmp_path, capsys, squeezed
):
    """Executed, in both directions: the real helper against a real transcript.

    A `node` of the wrong major and an api build that matches is a state a
    macOS runner reaches on its own, so the transcript below is the one the
    injection half actually sees. The retired whole-file `grep` is run beside
    the shipped helper to show the difference is the fix and not the fixture.

    The transcript comes from the two real printers, called in the order
    `status` calls them -- never from hand-typed separators. The first version
    of this test wrote the blank line before `Node runtime` itself, which the
    printer did not emit; it passed here while run 40090506617 failed live on
    exactly that difference. The `squeezed` case removes every blank line, so
    the reader is also proven not to depend on the printer's spacing.
    """
    transcript = tmp_path / "status.log"
    transcript.write_text(
        _api_match_then_node_mismatch(capsys, squeezed=squeezed), encoding="utf-8"
    )

    helper = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            f'source <(sed -n "/^api_build_block()/,/^}}/p;/^api_build_says_mismatch()/,/^}}/p" '
            f'"{SCRIPT}")\n'
            f'api_build_says_mismatch "{transcript}"',
        ],
        capture_output=True,
        text=True,
    )
    assert helper.returncode == 1, (
        "the subject-scoped reader saw a mismatch in a transcript whose api build "
        f"matched: {helper.stdout}{helper.stderr}"
    )

    retired = subprocess.run(
        ["bash", "-c", f'grep -q "MISMATCH" "{transcript}"'], capture_output=True, text=True
    )
    assert retired.returncode == 0, (
        "the retired whole-file grep did NOT fire on this transcript, so this test "
        "is not measuring the defect it exists for"
    )


def test_the_script_never_asserts_from_a_checkout(script):
    """A command run from inside a clone can resolve repo-relative paths a real
    install does not have (#3759). The script `cd`s to $HOME first, and the
    formula it copies is made absolute before that."""
    assert 'cd "$HOME"' in script
    assert 'NEW_RB="$(cd "$(dirname "$NEW_RB")" && pwd)/$(basename "$NEW_RB")"' in script
    # Comments are stripped first: the header explains the scenario in prose
    # and names these commands long before any of them runs.
    body = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    cd_home = body.index('cd "$HOME"')
    # Everything that drives the product comes after the chdir.
    for driven in ("brew services start", "nyxgpt ops status", "drift_driver.py"):
        assert body.index(driven) > cd_home, f"{driven!r} runs before the chdir to $HOME"


def test_the_printed_remediation_is_measured_from_the_staged_state(script):
    """The hole the review of #4155 found.

    `nyxgpt ops restart api` is what every mismatch surface prints, and the
    job used to run it only *after* the install step had already removed the
    survivor -- so it could not see that a bare `brew services restart` acts
    on the registered service and nothing is registered in that state. The
    survivor is therefore staged twice: once for the install step, once for
    the command the operator is actually told to run.
    """
    body = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert body.count('stage_survivor "$OLD_KEG"') == 2, (
        "the survivor is staged once, so one of the two repairs under test is measured on an "
        "already-repaired machine"
    )
    restart = body.index("nyxgpt ops restart api > ")
    assert body.rindex('stage_survivor "$OLD_KEG"') < restart, (
        "the printed remediation runs before the survivor is re-staged, which is the vacuous "
        "ordering this test exists to forbid"
    )
    # And the re-staged state is asserted to BE a mismatch before the
    # remediation runs: otherwise the assertions after it pass on a machine
    # that never reproduced anything.
    assert "the remediation below would have nothing to repair" in script
    # Convergence, by interpreter path and by pid -- not by exit code.
    assert "after the printed remediation the api runs" in script
    assert "is still alive after the printed remediation" in script


def test_the_restart_line_grepped_for_is_the_one_ops_prints(script):
    """`restart api` must report stopping the unmanaged survivor, because that
    is the step a bare service restart structurally cannot perform."""
    needle = "no service manager accounts for"
    assert f"grep -qF '{needle}'" in script
    stale = ops.BuildDrift(
        state=BUILD_MISMATCH,
        running=RuntimeBuild(
            executable="/old/venv/bin/python3",
            prefix="/old/venv",
            python="3.11.9",
            pid=4242,
            version="3.0.0rc14",
            prefix_exists=False,
        ),
        expected_prefix="/new/venv",
        expected_source="the keg",
        detail="d",
        remediation=ops._RUNNING_BUILD_REMEDIATION,
    )
    with (
        patch.object(ops, "_native_api_build_drift", return_value=stale),
        patch.object(ops, "_repair_running_api_build", return_value=[]),
    ):
        results = ops._restart_native_service("api")
    assert any(needle in r.message for r in results), (
        "ops no longer prints the line the macOS job greps for, so that assertion would fail on "
        "a machine where the repair is working"
    )


def test_both_halves_are_measured(script):
    """A job that only runs the fixed code is green on every machine that fails
    to reproduce the bug (#3753). Both the injected defect and the restored fix
    have to be asserted, and the restore has to be verified."""
    assert "the injection did not take" in script
    assert "not restored" in script
    assert "the injection produced state" in script


def test_the_workflow_runs_it_and_filters_on_it():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "candidate-upgrade:" in workflow
    assert "scripts/macos-upgrade-smoke.sh" in workflow
    assert "- 'scripts/macos-upgrade-smoke.sh'" in workflow, (
        "the script is not in the pull_request paths filter, so a change to the thing this gate "
        "asserts would not re-run the gate"
    )
    assert "- 'src/nyxgpt/running_build.py'" in workflow


def test_the_workflow_builds_two_versions_of_one_formula():
    """The upgrade only exists if both builds render the same formula name at
    different versions. Pinned here as well as asserted in the job, because a
    second *install* passing as an upgrade is the way this gate goes hollow."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "build_homebrew_artifacts.py 3.0.0rc0 dist/rc0" in workflow
    assert "build_homebrew_artifacts.py 3.0.0rc1 dist/rc1" in workflow
    assert "there is no upgrade here" in workflow
