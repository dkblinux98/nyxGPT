#!/usr/bin/env python3
"""Choose which test suites a change actually needs (P-001, #3896).

The pipeline ran the whole tree on every push, every review round and every
developer fix attempt: 2,243 runner-minutes over 30 days, led by `ci-tests.yml`
(173 runs) and `security-scan.yml` (187). A docs-only diff paid exactly what a
`src/nyxgpt/ops.py` rewrite paid.

Selection here is deterministic path and import analysis, not judgment: the
intelligence is in the mapping, and a mapping can be read, tested and argued
with. Three properties hold it together:

* **A floor that nothing can skip.** The process guards (ledger, context index,
  comment tokens, workflow injection, first principles) run for every change,
  so a narrow rule can never switch off the checks that protect the loop.
* **Unknown means everything.** Any path this module cannot classify selects
  the full suite. New directories are safe by default; the failure mode is
  wasted minutes, never a missed regression.
* **The merge boundary is unscoped.** Callers run the full suite on the release
  branch; scoping applies to intermediate pushes and fix attempts.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "nyxgpt"
UNIT_TESTS = REPO_ROOT / "tests" / "unit"

#: Suites that run for every change, whatever it touched. These guard the agent
#: process itself; #3904 is what happens when one of them is red and unnoticed.
FLOOR_TESTS = (
    "tests/unit/test_operating_ledger.py",
    "tests/unit/test_context_index.py",
    "tests/unit/test_comment_token_triggers.py",
    "tests/unit/test_workflow_script_injection.py",
    "tests/unit/test_first_principles_contract.py",
)

#: Paths that cannot affect Python behavior. Anything not listed is unknown,
#: and unknown runs everything.
DOC_SUFFIXES = (".md", ".txt", ".rst")
DOC_DIRS = ("docs/", "product_management/", "agents/")

#: A change here invalidates the whole selection, so do not try to be clever.
FULL_SUITE_PATHS = (
    "pyproject.toml",
    "tests/unit/conftest.py",
    "tests/conftest.py",
    "tests/log_guard.py",
    "scripts/ci/select_tests.py",
)

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(nyxgpt(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", re.MULTILINE)
#: `from nyxgpt import cloud, canary` binds two modules while naming only the
#: package. Collapsing those to a bare `nyxgpt` made 32 of 179 unit files look
#: unmappable, which then had to run for every source change.
FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(nyxgpt(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\s+\(?([^\n()]*)", re.MULTILINE
)
#: A file that never says "nyxgpt" tests something else entirely (the agent
#: scripts, the retro dashboards). Those belong to the script/workflow domain.
NYXGPT_MENTION_RE = re.compile(r"nyxgpt", re.IGNORECASE)


@dataclass
class Selection:
    """What to run, and why -- the reason is printed so a wrong scope is visible."""

    run_python: bool = True
    python_targets: list[str] = field(default_factory=list)
    run_web: bool = False
    full: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def pytest_args(self) -> str:
        if not self.run_python:
            return ""
        if self.full or not self.python_targets:
            return "tests/unit/"
        return " ".join(sorted(set(self.python_targets)))


def _module_name(path: str) -> str | None:
    """`src/nyxgpt/rag/rag.py` -> `nyxgpt.rag.rag`."""
    if not path.startswith("src/nyxgpt/") or not path.endswith(".py"):
        return None
    parts = Path(path).with_suffix("").parts[1:]  # drop leading "src"
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports_of(text: str) -> set[str]:
    """Every nyxgpt module a file binds, including `from pkg import module` names."""
    found = set(IMPORT_RE.findall(text))
    for package, names in FROM_IMPORT_RE.findall(text):
        for raw in names.split(","):
            name = raw.strip().split(" as ")[0].strip()
            # Lowercase identifiers are plausibly submodules; classes and
            # constants are not, and adding them is harmless (they never match
            # a real module) but noisy, so they are skipped.
            if name and name.isidentifier() and name.islower():
                found.add(f"{package}.{name}")
    return found


def _source_dependents(changed_modules: set[str]) -> set[str]:
    """Modules that reach a changed module, transitively.

    Changing `config` matters to every module importing it, and to their
    importers in turn -- so the closure is walked rather than taken one level
    deep, which is how a scoped run misses the regression it was meant to catch.
    """
    graph: dict[str, set[str]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        name = _module_name(rel)
        if name:
            graph[name] = _imports_of(path.read_text(encoding="utf-8", errors="ignore"))

    affected = set(changed_modules)
    changing = True
    while changing:
        changing = False
        for module, imports in graph.items():
            if module in affected:
                continue
            if any(
                imported in affected or imported.startswith(tuple(f"{a}." for a in affected))
                for imported in imports
            ):
                affected.add(module)
                changing = True
    return affected


def _matches(imported: str, affected: set[str]) -> bool:
    """Does an import statement bind to any affected module?

    Three shapes count, and one deliberately does not. `nyxgpt.rag.rag` matches
    itself; the package `nyxgpt.rag` matches a change inside it; and a submodule
    matches a change to the package `__init__` above it. A bare `import nyxgpt`
    does **not** match everything under it -- honouring it as a wildcard
    selected most of the suite for a leaf module and gave back nothing.
    """
    if imported == "nyxgpt":
        return "nyxgpt" in affected
    return any(
        a == imported or a.startswith(f"{imported}.") or imported.startswith(f"{a}.")
        for a in affected
    )


def _classify_tests() -> tuple[dict[str, set[str]], list[str], list[str]]:
    """Partition the unit suite by how it can be reasoned about.

    * **bound** -- names specific nyxgpt modules, so import analysis places it.
    * **unbound** -- mentions nyxgpt but imports nothing specific: it drives the
      CLI or a subprocess, so no import edge exists to follow. Runs for any
      source change; guessing it unaffected is the one error worth avoiding.
    * **process** -- never mentions nyxgpt at all. These test the agent scripts,
      the retrospective dashboards and the workflow contracts, and they are
      unaffected by application code. They run when the process files change.
    """
    bound: dict[str, set[str]] = {}
    unbound: list[str] = []
    process: list[str] = []
    for path in sorted(UNIT_TESTS.glob("test_*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        specific = {i for i in _imports_of(text) if i != "nyxgpt"}
        if specific:
            bound[rel] = specific
        elif NYXGPT_MENTION_RE.search(text):
            unbound.append(rel)
        else:
            process.append(rel)
    return bound, unbound, process


def _is_doc(path: str) -> bool:
    return path.endswith(DOC_SUFFIXES) or path.startswith(DOC_DIRS)


def select(changed: list[str]) -> Selection:
    """Map a changed-file list onto the suites that can see its effects."""
    sel = Selection(full=False, run_python=False, python_targets=list(FLOOR_TESTS))
    bound, unbound, process = _classify_tests()

    if not changed:
        sel.reasons.append("no changed files reported -- running everything")
        sel.run_python = True
        sel.full = True
        return sel

    changed_modules: set[str] = set()
    process_changed = False

    for path in changed:
        if path in FULL_SUITE_PATHS:
            sel.full = True
            sel.run_python = True
            sel.run_web = True
            sel.reasons.append(f"{path} invalidates selection -- full suite")
            return sel

        if path.startswith("web/"):
            sel.run_web = True
            continue
        if path.startswith((".github/workflows/", ".github/actions/", "scripts/")):
            process_changed = True
            continue
        if path.startswith("agents/"):
            # Charters, runbooks and the ledger are prose, but the ledger and
            # index carry structural guards that live in the process bucket.
            process_changed = True
            continue
        if _is_doc(path):
            continue
        if path.startswith("tests/"):
            if path.endswith(".py"):
                sel.run_python = True
                sel.python_targets.append(path)
                sel.reasons.append(f"{path}: changed test runs itself")
            continue

        module = _module_name(path)
        if module:
            changed_modules.add(module)
            continue

        sel.full = True
        sel.run_python = True
        sel.run_web = True
        sel.reasons.append(f"{path}: unclassified path -- full suite")
        return sel

    if process_changed:
        sel.run_python = True
        sel.python_targets.extend(process)
        sel.reasons.append(f"process files changed -> {len(process)} process test file(s)")

    if changed_modules:
        affected = _source_dependents(changed_modules)
        hits = [
            rel for rel, imports in bound.items() if any(_matches(i, affected) for i in imports)
        ]
        sel.run_python = True
        sel.python_targets.extend(hits)
        sel.python_targets.extend(unbound)
        sel.reasons.append(
            f"{len(changed_modules)} module(s) changed -> {len(affected)} affected -> "
            f"{len(hits)} bound + {len(unbound)} unbound test file(s)"
        )

    if not sel.run_python:
        sel.run_python = True
        sel.reasons.append("floor only -- nothing in this diff can change Python behavior")

    return sel


def changed_files(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base ref to diff against")
    parser.add_argument("--files", nargs="*", help="explicit changed-file list (testing)")
    parser.add_argument("--github-output", action="store_true", help="emit GITHUB_OUTPUT lines")
    args = parser.parse_args()

    changed = args.files if args.files is not None else changed_files(args.base or "origin/HEAD")
    sel = select(changed)

    # Always say what was skipped and why: a silent truncation reads as
    # "covered everything" when it did not.
    print(f"changed files: {len(changed)}", file=sys.stderr)
    for reason in sel.reasons:
        print(f"  - {reason}", file=sys.stderr)
    print(f"  => pytest: {sel.pytest_args or '(none)'}", file=sys.stderr)
    print(f"  => web:    {'yes' if sel.run_web else 'no'}", file=sys.stderr)

    if args.github_output:
        print(f"pytest_args={sel.pytest_args}")
        print(f"run_python={'true' if sel.run_python else 'false'}")
        print(f"run_web={'true' if sel.run_web else 'false'}")
        print(f"full={'true' if sel.full else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
