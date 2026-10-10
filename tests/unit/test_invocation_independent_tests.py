"""A test's answer comes from the tree, not from the command that started it (#4195).

THE DEFECT CLASS. `tests/unit/test_resources_packaging.py` skipped under
`pytest <file>` and FAILED under `python -m pytest <file>` on the same
checkout: `-m` prepends the current directory to `sys.path`, the gitignored
`build/` directory setuptools leaves in the repo root imports as a *namespace
package*, and `pytest.importorskip("build")` therefore said yes to an
interpreter that cannot run `python -m build`.

Three sibling instances, all the same shape -- a test or suite that asks the
environment a question instead of asking the tree:

* `bash tests/test_escalation_evidence.sh` printed **"All tests passed."**
  with its entire EXECUTED half skipped, because `$PYTHON` defaulted to PATH's
  `python3` and that has no pytest on a CI runner or most dev machines. Via
  pytest (which passes `NYXGPT_TEST_PYTHON`) the half ran. Two answers, one
  tree, and the green one was over nothing.
* `bash tests/test_pr_head_guard_discriminates.sh` ran a bare
  `python3 -m pytest` and read any non-zero status as "the guard is red, as it
  must be". With no pytest on PATH that was all nine fault-injection cases
  reporting `[ok]` without the guard ever starting.
* `scripts/agents/run_final_verification.sh` ran its gates under `$PY` and
  composed the recorded failure reason under a bare `python3`.

The remedy the issue warns about is the one that hides the problem: a
`--deselect` in the single job that runs the test, leaving the assertions in
the tree while nothing gates them. So the rules below are the ones this repo
now holds to, and this file is what enforces them -- a rule stated in a
runbook and checked by nothing is its own defect class (#4174).

Each test here is written so that reverting the #4195 fix makes it fail.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from shell_suite import bash4_or_skip, runnable_module_or_skip

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHELL_LIB = ROOT / "tests" / "pytest_python.sh"
WORKFLOWS = ROOT / ".github" / "workflows"

#: Shell suites under `tests/` that run pytest in a subprocess. Each has to get
#: its interpreter from the shared resolver rather than from PATH.
_SUITES_RUNNING_PYTEST = (
    "tests/test_escalation_evidence.sh",
    "tests/test_pr_head_guard_discriminates.sh",
)

#: Every shell suite that runs ANY python, pytest or not. Each must take its
#: interpreter from `tests/pytest_python.sh` rather than making the choice
#: itself -- the #4195 sweep found five copies of that one decision, two of
#: them wrong in different ways.
_SUITES_RUNNING_PYTHON = _SUITES_RUNNING_PYTEST + (
    "tests/test_red_head_round.sh",
    "tests/test_reviewable_head_gate.sh",
)


def _shell_suites() -> list[Path]:
    return sorted(p for p in (ROOT / "tests").glob("*.sh") if p.name != "pytest_python.sh")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """`(line number, text)` for the lines that EXECUTE, comments dropped.

    These guards have to read code, not prose: every file they police explains
    the #4195 defect in a header comment, and those comments necessarily quote
    the very constructs being banned. A guard that matched the explanation
    would fail on the fix -- and the obvious workaround (stop explaining) is
    worse than the guard.
    """
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((i, line))
    return out


#: The ways a test module starts a child process. `Popen` included: a wrapper
#: that streams output still has three outcomes to handle.
_SUBPROCESS_CALLS = frozenset({"run", "check_output", "check_call", "call", "Popen"})


def _string_literals(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _suites_invoked(path: Path) -> set[str]:
    """Which of `_SUITES_RUNNING_PYTHON` this module actually EXECUTES.

    "The suite's filename appears somewhere in the file" was the first cut of
    this, and it is wrong in the direction that matters: a module that quotes a
    suite path as *data* -- a fixture, an expected message, an example in a
    docstring the code-line filter does not drop -- gets reported as a wrapper
    of a suite it never starts. `tests/unit/test_class_sweep.py` tripped it
    exactly that way, by using a real sweep's text as test input, and the only
    fix available to its author would have been to stop naming the file.

    So the question asked is the one the guard means: does the suite reach the
    arguments of a subprocess call? Both shapes in the tree count --
    `subprocess.run([bash, "tests/x.sh"])` and the `suite = ROOT / "tests" /
    "x.sh"` indirection both wrappers actually use. The precision cuts the
    other way too, so `test_the_wrapper_detector_finds_every_real_wrapper`
    pins that the detector still sees the wrappers that exist; a detector that
    found nothing would make this guard vacuous, which is the failure mode
    #4195 already had to fix once.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken module fails louder elsewhere
        return set()

    # Names bound to an expression that names a suite: `suite = ROOT / "x.sh"`.
    bound: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        literals = _string_literals(node.value)
        named = {s for s in _SUITES_RUNNING_PYTHON if any(Path(s).name in t for t in literals)}
        if not named:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    bound.setdefault(name.id, set()).update(named)

    invoked: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in _SUBPROCESS_CALLS:
            continue
        literals = _string_literals(node)
        invoked.update(
            s for s in _SUITES_RUNNING_PYTHON if any(Path(s).name in t for t in literals)
        )
        for name in ast.walk(node):
            if isinstance(name, ast.Name) and name.id in bound:
                invoked.update(bound[name.id])
    return invoked


class TestTheNamespacePackageTrap:
    """The mechanism itself, injected -- so this file cannot pass by luck."""

    def test_a_bare_directory_satisfies_import_but_not_dash_m(self, tmp_path, monkeypatch):
        """`import build` yes, `python -m build` no. That gap is the whole defect.

        Proven by manufacturing exactly what the repo manufactures for itself:
        a directory named after a module, on `sys.path`, with no `__init__.py`.
        """
        (tmp_path / "pretend_pkg").mkdir()
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()

        # The question `importorskip` asks -- answered by the bare directory.
        assert importlib.util.find_spec("pretend_pkg") is not None

        # The question the subprocess asks -- not answered by it.
        assert importlib.util.find_spec("pretend_pkg.__main__") is None

        # And that is not a theory about `find_spec`: the interpreter agrees.
        cp = subprocess.run(
            [sys.executable, "-m", "pretend_pkg"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=60,
        )
        assert cp.returncode != 0
        assert "pretend_pkg" in cp.stderr

    def test_the_shared_helper_skips_where_importorskip_would_have_failed(
        self, tmp_path, monkeypatch
    ):
        """The fix, measured against the injected trap rather than against prose."""
        (tmp_path / "pretend_pkg2").mkdir()
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()

        with pytest.raises(pytest.skip.Exception) as skipped:
            runnable_module_or_skip("pretend_pkg2", needed_for="the injected trap")
        assert "namespace package" in str(skipped.value)

        # The other direction (#3753): a module that really IS runnable must
        # not be skipped, or the helper would skip everything and assert
        # nothing. `pip` ships a `__main__` in every supported environment.
        runnable_module_or_skip("pip", needed_for="the opposite direction")

    def test_the_repo_root_really_is_the_shadowing_case(self):
        """`build/` is gitignored, so the trap is invisible in a fresh clone.

        Which is why this defect survived: the file is not in the tree, it is
        manufactured by `pip install -e .`, so the failure appears only on a
        machine that has installed the project -- i.e. every machine that runs
        the suite, and no code review.
        """
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "build/" in [line.strip() for line in gitignore]


class TestNoTestGuardsADashMSubprocessWithImportorskip:
    """The rule: guard what you RUN, not what happens to be importable."""

    #: `python -m X` subprocesses in the unit suite, and the module each needs.
    _DASH_M_MODULES = ("build",)

    def test_resources_packaging_does_not_use_importorskip_for_build(self):
        path = ROOT / "tests" / "unit" / "test_resources_packaging.py"
        source = "\n".join(text for _n, text in _code_lines(path))
        assert 'importorskip("build"' not in source, (
            "test_resources_packaging.py builds a wheel with `python -m build`; "
            "`importorskip` is satisfied by the repo-root `build/` directory and "
            "would make this test's result depend on the command that started "
            "pytest (#4195). Use `runnable_module_or_skip` from shell_suite."
        )
        assert "runnable_module_or_skip" in source

    def test_no_unit_test_guards_a_dash_m_module_with_importorskip(self):
        """The class, not the one file: any future `-m build` guard too."""
        offenders = []
        for path in sorted((ROOT / "tests" / "unit").glob("*.py")):
            code = "\n".join(text for _n, text in _code_lines(path))
            for module in self._DASH_M_MODULES:
                if f'"-m", "{module}"' not in code:
                    continue
                if f'importorskip("{module}"' in code:
                    offenders.append(f"{path.relative_to(ROOT)} -> {module}")
        assert not offenders, (
            "these tests run `python -m <module>` but guard it with "
            f"`importorskip`, which a namespace directory satisfies: {offenders}"
        )


class TestNoTestImportsItselfByDottedPath:
    """The mirror image of the `build/` trap, found by criterion 6's full run.

    `tests/unit/test_no_repo_relative_user_paths.py` re-imported ITSELF as
    `tests.unit.test_no_repo_relative_user_paths` in order to monkeypatch its
    own `ROOT`. That needs the repo root on `sys.path` and `tests` /
    `tests.unit` importable -- neither has an `__init__.py`, and pytest's
    prepend import mode puts `tests/unit` on `sys.path`, not the repo root. So:

        python -m pytest   ->  5 passed   (cwd prepended, `tests` is a
                                           namespace package)
        pytest             ->  5 failed   ModuleNotFoundError: No module
                                          named 'tests'

    The same namespace-package mechanism as `build/`, in the other direction,
    and found the same way: by running the full suite both ways rather than by
    reading the file. `sys.modules[__name__]` answers the same question with no
    dependency on how pytest was started.
    """

    def test_no_test_module_imports_the_tests_package(self):
        offenders = []
        for path in sorted((ROOT / "tests").rglob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for line_no, text in _code_lines(path):
                if re.search(r"^\s*(?:import|from)\s+tests[. ]", text) or re.search(
                    r"""monkeypatch\.setattr\(\s*["']tests\.""", text
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {text.strip()}")
        assert not offenders, (
            "a test module imported through the `tests` package resolves only "
            "when the repo root is on sys.path, which `python -m pytest` "
            "arranges and the `pytest` console script does not -- so the "
            "result depends on the command (#4195). Use "
            f"`sys.modules[__name__]` for the running module: {offenders}"
        )

    def test_the_tests_tree_really_is_not_an_importable_package(self):
        """Fault injection for the guard above: the premise, asserted.

        If someone adds `tests/__init__.py` the dotted import would start
        working and the guard would look like pedantry. It is not -- but the
        reason would have changed, so make the premise fail loudly instead of
        letting the rule survive as folklore.
        """
        for missing in (ROOT / "tests" / "__init__.py", ROOT / "tests" / "unit" / "__init__.py"):
            assert not missing.exists(), (
                f"{missing.relative_to(ROOT)} now exists, which changes why "
                "dotted `tests.` imports are fragile. Re-derive the rule in "
                "test_no_test_module_imports_the_tests_package (#4195)."
            )


class TestShellSuitesResolveTheirInterpreter:
    """The rule: one resolver, and a loud third outcome when it finds nothing."""

    def _bash(self, snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
        # `bash` resolved to an absolute path HERE, because some of these cases
        # hand the child an empty PATH on purpose and would otherwise fail to
        # find the shell rather than the interpreter.
        return subprocess.run(
            [bash4_or_skip(), "-c", f'source "{SHELL_LIB}" && {snippet}'],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **env},
        )

    def test_the_shared_resolver_finds_the_interpreter_running_this_test(self):
        """`NYXGPT_TEST_PYTHON` is how every pytest wrapper hands it over.

        Note what this test must NOT do: look for an interpreter on PATH. On a
        runner where PATH's `python3` is the system one, `resolve_pytest_python`
        correctly finds nothing, and a test that demanded success would be the
        very defect this file is about -- its answer coming from the
        environment rather than from the tree.
        """
        assert SHELL_LIB.is_file()
        cp = self._bash(f'resolve_pytest_python "{ROOT}"', NYXGPT_TEST_PYTHON=sys.executable)
        assert cp.returncode == 0, cp.stderr
        resolved = cp.stdout.strip()
        assert Path(resolved).is_absolute(), f"not absolutised: {resolved!r}"
        assert Path(resolved).resolve() == Path(sys.executable).resolve()

    def test_an_interpreter_without_pytest_is_rejected_rather_than_returned(self):
        """The other direction (#3753): a resolver that returned anything.

        Injected with an interpreter that exists and cannot import pytest --
        `/bin/sh`, which fails `-c "import pytest"` the same way a system
        python with no pytest does.
        """
        cp = self._bash(
            'resolve_pytest_python "" && echo "RETURNED:$?"',
            NYXGPT_TEST_PYTHON="/bin/sh",
            PATH="/nonexistent",
            VIRTUAL_ENV="",
        )
        assert cp.returncode != 0, f"a non-pytest interpreter was accepted: {cp.stdout!r}"
        assert cp.stdout.strip() == ""

    def test_requiring_one_where_there_is_none_exits_77_and_says_so(self):
        """Not 0 (a pass over nothing) and not 1 (a red about the code)."""
        cp = self._bash(
            'require_pytest_python "" && echo UNEXPECTED_PASS',
            NYXGPT_TEST_PYTHON="",
            PATH="/nonexistent",
            VIRTUAL_ENV="",
        )
        assert cp.returncode == 77, (cp.returncode, cp.stdout, cp.stderr)
        assert "UNEXPECTED_PASS" not in cp.stdout
        assert "NOT a pass" in cp.stderr
        assert "NYXGPT_TEST_PYTHON" in cp.stderr, "the message must name the fix"

    def test_the_resolver_absolutises_so_a_stub_on_path_cannot_shadow_it(self):
        """tests/test_red_head_round.sh's lesson, now enforced centrally.

        A suite that puts a stub directory on PATH shadows `python3`; a shim
        that re-execs the bare name execs itself forever. The resolver must
        hand back the absolute path it validated, not the name it was given.
        """
        cp = self._bash('resolve_python ""', NYXGPT_TEST_PYTHON="python3", VIRTUAL_ENV="")
        assert cp.returncode == 0, cp.stderr
        assert Path(cp.stdout.strip()).is_absolute(), cp.stdout

    def test_the_cannot_run_status_is_the_same_number_on_both_sides(self):
        """A mismatch here would turn a skip back into a failure, silently."""
        from shell_suite import NO_PYTEST_PYTHON_EXIT

        text = SHELL_LIB.read_text(encoding="utf-8")
        assert f"NO_PYTEST_PYTHON_EXIT={NO_PYTEST_PYTHON_EXIT}" in text

    @pytest.mark.parametrize("suite", _SUITES_RUNNING_PYTHON)
    def test_each_suite_takes_its_interpreter_from_the_shared_resolver(self, suite):
        """ONE SOURCE PER DECISION (CLAUDE.md): not five copies of it.

        The #4195 sweep found the "which interpreter" decision made five times
        across four suites, each slightly differently, and the two that were
        wrong were wrong in different ways. Correcting copies leaves a place
        for the next divergence, so the decision moved to one file and this
        asserts nobody has made a sixth copy.
        """
        text = (ROOT / suite).read_text(encoding="utf-8")
        assert "pytest_python.sh" in text, f"{suite} does not source the shared resolver"
        offenders = [
            f"{line_no}: {line.strip()}"
            for line_no, line in _code_lines(ROOT / suite)
            if "NYXGPT_TEST_PYTHON:-" in line
        ]
        assert not offenders, (
            f"{suite} still resolves its own interpreter inline: {offenders}. Take "
            "it from `require_python` / `require_pytest_python` instead (#4195)."
        )

    def test_no_shell_suite_takes_its_pytest_interpreter_from_path(self):
        """`python3 -m pytest` in a suite is the #4195 defect, by construction."""
        offenders = []
        for path in _shell_suites():
            for line_no, text in _code_lines(path):
                match = re.search(r"(?<![\"'$\w{])python3?\s+-m\s+pytest", text)
                if match:
                    offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {match.group(0)}")
        assert not offenders, (
            "these shell suites run pytest through a bare PATH lookup, so their "
            "result depends on which `python3` the caller happens to have "
            "(#4195). Source tests/pytest_python.sh and use "
            f'`"$PYTHON" -m pytest`: {offenders}'
        )

    @pytest.mark.parametrize("suite", _SUITES_RUNNING_PYTEST)
    def test_each_pytest_running_suite_requires_an_interpreter_up_front(self, suite):
        text = (ROOT / suite).read_text(encoding="utf-8")
        assert "pytest_python.sh" in text, f"{suite} does not source the shared resolver"
        assert "require_pytest_python" in text, (
            f"{suite} must call `require_pytest_python` so a machine that cannot "
            "run pytest exits 77 rather than reporting a pass over an "
            "unexecuted section"
        )

    def test_every_wrapper_of_an_exit_77_suite_handles_all_three_outcomes(self):
        """A wrapper that only knows pass/fail turns a skip back into a red.

        Scoped to the suites that CAN exit 77 -- the ones taking their
        interpreter from the shared resolver. A wrapper around a suite with no
        third outcome has nothing to handle, and demanding the helper there
        would be ceremony rather than a guard.
        """
        offenders = []
        for path in sorted((ROOT / "tests" / "unit").glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            # Only wrappers that RUN a suite -- not ones that read it, or
            # quote its path as test data (`_suites_invoked`'s docstring).
            suites = _suites_invoked(path)
            if not suites:
                continue
            code = "\n".join(text for _n, text in _code_lines(path))
            # `assert_shell_suite_ran(` -- the CALL. Matching the bare name
            # would be satisfied by the import line alone, which is how this
            # guard was vacuous when first written.
            if "assert_shell_suite_ran(" not in code:
                offenders += [f"{path.relative_to(ROOT)} runs {suite}" for suite in suites]
        assert not sorted(set(offenders)), (
            "these pytest wrappers run a suite that can exit 77 ('could not "
            "run') but do not use `assert_shell_suite_ran`, so that third "
            f"outcome would be reported as a test failure (#4195): {offenders}"
        )

    def test_the_wrapper_detector_finds_every_real_wrapper(self):
        """The other half of the precision trade: it must not find nothing.

        `_suites_invoked` replaced "the filename appears in the file" with "the
        suite reaches a subprocess call", which is narrower by design -- and a
        detector that narrowed to zero would make the guard above pass by
        finding nobody to police. These are the wrappers that exist today, each
        using one of the two shapes the detector has to understand.
        """
        detected = {
            path.name: _suites_invoked(path)
            for path in sorted((ROOT / "tests" / "unit").glob("*.py"))
        }
        assert "tests/test_escalation_evidence.sh" in detected["test_escalation_headline.py"], (
            "the detector no longer sees test_escalation_headline.py as a "
            "wrapper of test_escalation_evidence.sh, so nothing checks that it "
            "handles exit 77 (#4195)"
        )
        assert "tests/test_red_head_round.sh" in detected["test_error_classes.py"], (
            "the detector no longer sees test_error_classes.py as a wrapper of "
            "test_red_head_round.sh (#4195)"
        )
        # ...and a module that only quotes a suite path is not a wrapper of it.
        assert not detected["test_class_sweep.py"], (
            "test_class_sweep.py uses a suite path as test data and starts no "
            "suite; reporting it as a wrapper is the false positive "
            "`_suites_invoked` exists to remove"
        )

    def test_the_escalation_suite_can_no_longer_skip_its_executed_section(self):
        """The specific regression: a `[SKIP]` that still printed a pass."""
        text = (ROOT / "tests" / "test_escalation_evidence.sh").read_text(encoding="utf-8")
        assert "VERIFY_SKIPPED" not in text, (
            "the skip branch is back: it let the suite print 'All tests passed.' "
            "with the Final Verification gate never executed (#4195)"
        )

    def test_the_guard_suite_only_accepts_pytest_exit_1_as_red(self):
        """Any other non-zero means the guard never ran, which is not a pass."""
        text = (ROOT / "tests" / "test_pr_head_guard_discriminates.sh").read_text(encoding="utf-8")
        assert 'rc" -ne 1' in text, (
            "`_expect_red` must require pytest exit 1 (an assertion failed). Any "
            "other status means the guard did not run, and reporting that as "
            "'red, as it must be' made all nine fault-injection cases vacuous "
            "(#4195)"
        )


class TestVerificationGateUsesOneInterpreter:
    """The rule: the gate and its own failure report run under the same python."""

    def test_run_final_verification_never_shells_a_bare_python3(self):
        script = ROOT / "scripts" / "agents" / "run_final_verification.sh"
        offenders = []
        for line_no, text in _code_lines(script):
            # `command -v python` is the resolution itself, not an invocation.
            probed = re.sub(r"command -v python3?", "", text)
            if re.search(r"(?<![\"'$\w{/])python3?\s", probed):
                offenders.append(f"{script.name}:{line_no}: {text.strip()}")
        assert not offenders, (
            "the gate resolves its interpreter once into `$PY`; a bare `python3` "
            "makes the RECORDED FAILURE REASON depend on PATH, and an empty "
            "reason is the 'Error type could not be determined' headline this "
            f"script exists to prevent (#4176, #4195): {offenders}"
        )


class TestEveryWorkflowExclusionIsJustified:
    """The rule: a test excluded from the job that runs it says why, in place.

    "`--deselect` it in the one job that runs it" is the remedy #4195 names as
    the thing that hides the defect -- the assertions stay in the tree and
    nothing gates them. An exclusion is allowed, but not silently: the reason
    has to be readable next to it, so the next person can tell "deferred to the
    step below" from "this was failing and we stopped looking".
    """

    _EXCLUSION = re.compile(r"--deselect|(?<![\w-])-k\s+[\"']")

    def _pytest_exclusions(self) -> list[tuple[Path, int, str]]:
        found = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not self._EXCLUSION.search(line):
                    continue
                # `kubectl apply -k <dir>` is not a pytest selection.
                window = "\n".join(lines[max(0, i - 6) : i + 1])
                if "pytest" not in window:
                    continue
                found.append((path, i + 1, line.strip()))
        return found

    def test_the_only_exclusions_are_the_ones_this_repo_knows_about(self):
        """A NEW exclusion fails here until it is listed and justified."""
        known = {("escalation-headline-smoke.yml", "TestShellSuite")}
        actual = set()
        for path, _line, text in self._pytest_exclusions():
            match = re.search(r"::(\w+)", text)
            actual.add((path.name, match.group(1) if match else text))
        assert actual == known, (
            "the set of pytest exclusions in .github/workflows changed. Each one "
            "is a test that exists but is not gating: either remove it or add it "
            f"here with the reason written beside it (#4195). Found: {actual}"
        )

    def test_each_exclusion_carries_its_reason_in_the_workflow(self):
        for path, line_no, text in self._pytest_exclusions():
            lines = path.read_text(encoding="utf-8").splitlines()
            # The comment block for a `run:` step sits above the step, so look
            # back far enough to reach it rather than only at adjacent lines.
            window = "\n".join(lines[max(0, line_no - 30) : line_no])
            comments = [ln.strip() for ln in window.splitlines() if ln.strip().startswith("#")]
            assert comments, (
                f"{path.name}:{line_no} excludes a test with no comment " f"explaining why: {text}"
            )
            prose = " ".join(comments).lower()
            assert "4195" in prose or "deselect" in prose, (
                f"{path.name}:{line_no}: the comment above this exclusion does "
                "not explain the exclusion itself. State what gates the excluded "
                f"test instead, or remove it (#4195). Comment: {prose[:200]}"
            )

    def test_the_deselected_shell_suite_is_gated_by_a_later_step(self):
        """The justification has to be TRUE, not just present.

        `escalation-headline-smoke.yml` may deselect `TestShellSuite` only
        because a later step runs the same shell suite and checks both its exit
        status and its executed section's own output. If that step goes, the
        deselect is hiding an ungated test and this fails.
        """
        workflow = yaml.safe_load(
            (WORKFLOWS / "escalation-headline-smoke.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["the-pure-decisions-and-the-shell-suite"]["steps"]
        runs = [step.get("run") or "" for step in steps]
        suite_steps = [r for r in runs if "tests/test_escalation_evidence.sh" in r]
        assert suite_steps, (
            "TestShellSuite is deselected from the pytest step but no step in "
            "this job runs tests/test_escalation_evidence.sh -- the suite's "
            "assertions would be gating nothing (#4195)"
        )
        body = suite_steps[0]
        assert "it records the first failing node ID" in body, (
            "the gating step must grep for one of the executed section's own "
            "[ok] lines; exit 0 alone was what let a skipped section read as a "
            "pass (#4195)"
        )
        assert "77" in body, (
            "the gating step must call out exit 77 ('could not run') separately "
            "-- on a runner where setup-python just installed pytest, 77 means "
            "the job is broken (#4195)"
        )
