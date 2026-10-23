#!/usr/bin/env python3
"""A workflow job that runs pytest must be able to COLLECT what it runs (#4192).

THE DEFECT. `pipefail-pipeline-smoke.yml`'s `pipeline-readers-drain` job --
added as a `[required]` check by this very PR's previous round -- installed
`pyyaml pytest` and then ran::

    python -m pytest tests/unit/test_sigpipe_pipeline_guard.py -v

Any target under ``tests/`` makes pytest load the *root* ``tests/conftest.py``,
whose import closure reaches ``tests/log_guard.py`` and therefore ``psutil``.
So the job died before a single assertion ran::

    ImportError while loading conftest 'tests/conftest.py'
    E   ModuleNotFoundError: No module named 'psutil'
    ##[error]Process completed with exit code 4

pytest exit **4** is a usage/collection error, not a test failure. The check
went red while every behaviour it exists to measure was *untested*, and the
head it was red on was fine. That is this issue's own class -- a required
gate whose colour is not evidence about the commit -- arriving through the
gate the round added to enforce it.

WHY A GUARD AND NOT A ONE-LINE FIX. The decision "how does this job make the
root conftest collectible?" is made independently in **15 jobs** across 11
workflows, and the tree already holds three different answers to it:

* ``pip install -e .`` -- the package install, which brings the closure's
  third-party modules with it (``ci-tests``, ``branch-guard-smoke`` x2,
  ``reviewable-head-smoke``, ``developer_auto_implement``);
* ``--noconftest`` -- don't load it at all, correct for the many smoke suites
  that need no app import (``escalation-label-smoke`` x2,
  ``escalation-headline-smoke``, ``conflict-resolution-smoke``,
  ``huddle-session-smoke``, ``macos-brew-smoke``'s ``restart-wait``,
  ``red-head-round-smoke``);
* and one job that answered neither, which is the red check above.

Correcting that one copy leaves 14 places for the next divergence
(CLAUDE.md "One source per decision", ledger D-066). This module is that one
place: it derives what the root conftest needs **from the conftest itself**
and holds every job to the same rule, so there is no restated dependency list
to drift out of date. `red-head-round-smoke.yml` is the warning from the
other direction -- it carries an explicit ``psutil`` install whose comment
says "pytest cannot even collect without it", a premise that stopped being
load-bearing for that job the moment it gained ``--noconftest``.

THE RULE, and it is binary:

    A pytest invocation in a workflow job that loads the root conftest
    requires that job to install the package (``pip install -e .``).

A bare module list can never satisfy it: the closure imports first-party
``nyxgpt`` modules, which only the package install provides. The escape hatch
is ``--noconftest``, which is not a workaround but the honest statement that
the suite needs no app import.

`tests/unit/test_pytest_conftest_guard.py` is the executed half, and it also
pins the link this rule rests on -- that every third-party module in the
closure is a declared runtime dependency of the package, so ``pip install -e .``
genuinely does suffice. If someone adds an import to `tests/conftest.py` that
`pyproject.toml` does not declare, that test fails and names it, rather than
15 jobs discovering it one red check at a time.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
TESTS_DIR = REPO_ROOT / "tests"
ROOT_CONFTEST = TESTS_DIR / "conftest.py"

#: The first-party package. An import of it in the conftest closure is what
#: makes a bare `pip install <modules>` unable to satisfy the rule.
PACKAGE = "nyxgpt"

#: Flags that consume the next argument, so the token after them is never a
#: test path. Without this, `-c /dev/null` and `--deselect tests/unit/x.py::y`
#: would be read as targets.
VALUE_FLAGS = {
    "-c",
    "-k",
    "-m",
    "-o",
    "-p",
    "-n",
    "-r",
    "--rootdir",
    "--deselect",
    "--ignore",
    "--junitxml",
    "--maxfail",
    "--cov",
    "--cov-report",
    "--override-ini",
}


@dataclass(frozen=True)
class Closure:
    """What loading the root `tests/conftest.py` actually requires."""

    files: tuple[Path, ...]
    third_party: frozenset[str]
    needs_package: bool

    def describe(self) -> str:
        mods = ", ".join(sorted(self.third_party))
        who = ", ".join(f.name for f in self.files)
        pkg = f" and the `{PACKAGE}` package" if self.needs_package else ""
        return f"{who} import {mods}{pkg}"


@dataclass(frozen=True)
class Violation:
    workflow: str
    job: str
    call: str

    def __str__(self) -> str:
        return (
            f"{self.workflow}::{self.job}\n"
            f"    {self.call}\n"
            "    loads the root tests/conftest.py but the job never runs "
            "`pip install -e .`"
        )


def conftest_closure(conftest: Path = ROOT_CONFTEST) -> Closure:
    """Third-party modules and package need of the root conftest's imports.

    Walks `conftest.py` and follows its *local* test-helper imports (a module
    that resolves to `tests/<name>.py`), because those are what carry the
    non-obvious requirements -- `psutil` arrives through
    `tests/log_guard.py`, two files away from anything a workflow author
    reads. Standard-library imports are dropped; an import of `nyxgpt` is
    recorded as needing the package rather than as a module to pip-install.
    """
    stdlib = set(sys.stdlib_module_names)
    tests_dir = conftest.parent
    seen: dict[Path, None] = {}
    third: set[str] = set()
    needs_package = False
    queue = [conftest]

    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen[path] = None
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.append(node.module)
        for name in names:
            top = name.split(".")[0]
            if top == PACKAGE:
                needs_package = True
                continue
            local = tests_dir / f"{top}.py"
            if local.is_file():
                queue.append(local)
                continue
            if top in stdlib:
                continue
            third.add(top)

    return Closure(
        files=tuple(sorted(seen, key=str)),
        third_party=frozenset(third),
        needs_package=needs_package,
    )


def logical_lines(blob: str) -> list[str]:
    """`blob` with backslash continuations joined, so a flag on line 2 is seen.

    The continuation case is not hypothetical: every `--noconftest` in this
    repository's smoke workflows sits on a wrapped line, so a line-at-a-time
    scan would report all seven of them as violations.
    """
    out: list[str] = []
    current = ""
    for raw in blob.split("\n"):
        line = raw.rstrip()
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        out.append(current)
        current = ""
    if current:
        out.append(current)
    return out


def pytest_calls(blob: str) -> list[str]:
    """Shell commands in `blob` that invoke pytest (not ones that install it)."""
    calls = []
    for line in logical_lines(blob):
        stripped = line.strip()
        if stripped.startswith("#") or "pip install" in stripped:
            continue
        if not re.search(r"\bpytest\b", stripped):
            continue
        # `python -m pytest ...`, a bare `pytest ...`, or either behind a
        # shell operator / command substitution / assignment.
        if not re.search(r"(python -m pytest\b|(^|[;&|($\"'=]|&&|\|\|)\s*pytest\s)", stripped):
            continue
        calls.append(stripped)
    return calls


def loads_root_conftest(call: str) -> bool:
    """Whether `call` will make pytest load the root `tests/conftest.py`.

    `--noconftest` is the one opt-out. Otherwise pytest loads conftests from
    the rootdir down to each target, so any target under `tests/` pulls the
    root one in -- and a call with no target at all does too, because
    `pyproject.toml` sets `testpaths = ["tests"]`. A target that is a shell
    variable is treated as reaching `tests/` as well: the guard will not
    guess that `$PYTEST_TARGETS` stays out of the suite.
    """
    if "--noconftest" in call:
        return False

    tokens = call.split()
    try:
        start = (
            next(i for i, tok in enumerate(tokens) if tok == "pytest" or tok.endswith("/pytest"))
            + 1
        )
    except StopIteration:
        return True

    targets: list[str] = []
    skip_next = False
    for tok in tokens[start:]:
        if skip_next:
            skip_next = False
            continue
        if tok in VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        # Redirections and chained commands end the argument list.
        if tok in {"|", "||", "&&", ";", ">", ">>", "2>&1"} or tok.startswith(">"):
            break
        targets.append(tok)

    if not targets:
        return True  # testpaths = ["tests"]
    return any(
        tok.startswith("$") or re.match(r"^\.?/?tests(/|$)", tok.split("::")[0]) for tok in targets
    )


def installs_package(blob: str) -> bool:
    """Whether the job pip-installs this repository's package."""
    return bool(re.search(r"pip install\s+(--?\S+\s+)*-e\s+[\"']?\.", blob))


def _job_run_blob(job: dict) -> str:
    steps = job.get("steps") or []
    return "\n".join(str(step.get("run") or "") for step in steps if isinstance(step, dict))


def find_violations(path: Path) -> list[Violation]:
    """Jobs in the workflow at `path` whose pytest call cannot collect."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []

    found: list[Violation] = []
    for job_id, job in (data.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        blob = _job_run_blob(job)
        if installs_package(blob):
            continue
        for call in pytest_calls(blob):
            if loads_root_conftest(call):
                found.append(
                    Violation(workflow=path.name, job=job.get("name") or job_id, call=call)
                )
    return found


def workflow_paths(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def main(argv: list[str] | None = None) -> int:
    paths = workflow_paths(list(argv if argv is not None else sys.argv[1:]))
    violations = [v for path in paths for v in find_violations(path)]
    if not violations:
        return 0

    closure = conftest_closure()
    print(
        f"{len(violations)} workflow job(s) run pytest over the test suite without "
        f"installing the package, so pytest cannot load the root conftest and the "
        f"job dies at collection with exit 4 (#4192):"
    )
    for violation in violations:
        print(f"  {violation}")
    print(f"\nThe root conftest's import closure: {closure.describe()}.")
    print("\nPick one, per job:")
    print("  python -m pip install -e .                  # the suite needs app imports")
    print("  python -m pytest --noconftest <targets>      # it does not")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
