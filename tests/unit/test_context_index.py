"""Guard for `agents/CONTEXT_INDEX.md` (D-021).

The index is what the bootstrap reads *instead of* `.github/workflows/*` and
`scripts/agents/*`. That trade only holds while the index is complete: a
workflow missing from it is a workflow no agent knows exists, which is worse
than the exhaustive reading it replaced -- the agent does not know what it is
missing. So the committed copy must match what the generator produces.

**Staleness is not coverage, and the generator cannot be its own oracle
(#4228).** `test_committed_index_is_current` only proves the committed file
equals `build()`'s output, so every omission the generator makes it certifies
as correct: the `*.sh` glob hid `inverse_claims_sweep.py` and all ~37
`scripts/agents/lib/` modules from the map while this file stayed green. The
coverage tests below therefore enumerate the directories **from the
filesystem**, by a rule stated here and nowhere the generator can see:

* a workflow is any `*.yml`/`*.yaml` under `.github/workflows/` (that is
  GitHub's rule, not ours);
* an agent entry point is **any non-documentation file** directly under
  `scripts/agents/` or `scripts/agents/lib/` -- deliberately language-blind,
  so adding a `.rb`/`.ts`/`.mjs` script fails the build here instead of
  silently narrowing the map the way the next language after `.sh` did;
* every subdirectory of `scripts/agents/` must be named by a section of the
  index, so a new one cannot appear uncovered.

That duplication of the discovery rule is deliberate and must not be
"consolidated" into a call to the generator's own helpers (D-066 asks for one
source per *decision*; an oracle that shares its subject's blind spot is not a
second source at all). Everything else in the pair -- extracting a purpose
line, rendering a row -- does live in exactly one place in the generator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = REPO_ROOT / "agents" / "CONTEXT_INDEX.md"
GENERATOR_PATH = REPO_ROOT / "scripts" / "build_context_index.py"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
AGENT_SCRIPTS_DIR = REPO_ROOT / "scripts" / "agents"

#: Suffixes GitHub itself reads as a workflow definition.
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})

#: Documentation lives beside the scripts and is not an entry point. Stated as
#: what to *exclude* so that an unknown script language is included by default.
DOC_SUFFIXES = frozenset({".md", ".txt", ".rst"})


def _generator():
    spec = importlib.util.spec_from_file_location("build_context_index", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _entry_points(directory: Path) -> list[Path]:
    """Every file in `directory` the index has to account for.

    Derived from the filesystem and from nothing the generator knows about --
    see this module's docstring for why that duplication is on purpose.
    """
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() not in DOC_SUFFIXES
    )


def test_committed_index_is_current() -> None:
    """Regenerating must be a no-op; otherwise the index has drifted."""
    expected = _generator().build()
    actual = INDEX_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "agents/CONTEXT_INDEX.md is stale -- run `python3 scripts/build_context_index.py` "
        "and commit the result (a file added since the last regeneration is invisible "
        "to every agent that trusts the index)"
    )


def test_index_covers_every_workflow() -> None:
    """Coverage stated as a property, not inferred from the generator's own output."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    workflows = [
        path
        for path in sorted(WORKFLOWS_DIR.iterdir())
        if path.is_file() and path.suffix.lower() in WORKFLOW_SUFFIXES
    ]
    assert workflows, "no workflows found -- the enumeration itself is broken"
    missing = [path.name for path in workflows if f"`{path.name}`" not in text]
    assert not missing, f"workflows missing from agents/CONTEXT_INDEX.md: {missing}"


def _missing_entry_points(directory: Path, index_text: str) -> list[str]:
    """Entry points of `directory` that `index_text` does not name."""
    return [path.name for path in _entry_points(directory) if f"`{path.name}`" not in index_text]


def test_index_covers_every_agent_script_whatever_its_language() -> None:
    """The map must cover the directory it claims to index, `.sh` or not (#4228)."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    assert _entry_points(AGENT_SCRIPTS_DIR), "no agent scripts found -- the enumeration is broken"
    missing = _missing_entry_points(AGENT_SCRIPTS_DIR, text)
    assert not missing, (
        f"entry points under scripts/agents/ missing from agents/CONTEXT_INDEX.md: {missing} "
        "-- extend scripts/build_context_index.py (SCRIPT_SUFFIXES and, for a new "
        "language, a _script_purpose() arm) and regenerate"
    )


def test_index_covers_every_agent_library_module() -> None:
    """`scripts/agents/lib/` is listed, not skipped: 34 of its 37 modules have a
    `__main__` and the workflows invoke them as commands, so an omitted one is
    an invisible entry point exactly like an omitted top-level script."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    lib_dir = AGENT_SCRIPTS_DIR / "lib"
    assert _entry_points(lib_dir), "no agent library modules found -- the enumeration is broken"
    missing = _missing_entry_points(lib_dir, text)
    assert not missing, f"scripts/agents/lib/ modules missing from the index: {missing}"


def test_every_agent_subdirectory_is_a_section_of_the_index() -> None:
    """A new subdirectory must fail the build rather than go unmapped."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    subdirs = sorted(
        path.name
        for path in AGENT_SCRIPTS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    )
    missing = [name for name in subdirs if f"`scripts/agents/{name}/`" not in text]
    assert not missing, (
        f"subdirectories of scripts/agents/ with no section in the index: {missing} "
        "-- add a table for each in scripts/build_context_index.py"
    )


def test_python_purpose_comes_from_the_module_docstring() -> None:
    """A Python script states its purpose where PEP 257 puts it (#4228)."""
    purpose = _generator()._script_purpose(
        '#!/usr/bin/env python3\n"""Sweep the prose a diff may have falsified (#4015).\n\n'
        'Long rationale that must not reach the index.\n"""\n',
        Path("inverse_claims_sweep.py"),
    )
    assert purpose == "Sweep the prose a diff may have falsified (#4015)"


def test_python_purpose_falls_back_to_the_argparse_description() -> None:
    """No docstring is still no reason to force the file open."""
    purpose = _generator()._script_purpose(
        "import argparse\n"
        'parser = argparse.ArgumentParser(description="Adjudicate CodeQL alerts in bulk")\n',
        Path("apply_codeql_adjudications.py"),
    )
    assert purpose == "Adjudicate CodeQL alerts in bulk"


def test_python_purpose_falls_through_to_a_header_comment() -> None:
    """Neither docstring nor argparse: the shell arm's `#` header still applies."""
    purpose = _generator()._script_purpose(
        "#!/usr/bin/env python3\n# Stamp the board after a merge lands.\nimport sys\n",
        Path("stamp_board.py"),
    )
    assert purpose == "Stamp the board after a merge lands"


def test_a_purpose_line_stops_at_a_banner_rule_or_bullet() -> None:
    """`gh_project.sh`'s box banner used to end up inside its purpose line."""
    purpose = _generator()._script_purpose(
        "#!/usr/bin/env bash\nset -euo pipefail\n\n"
        "# ============================\n"
        "# Shared library for agent scripts\n"
        "# - SAFE TO SOURCE: nothing runs at import time\n"
        "# ============================\n",
        Path("gh_project.sh"),
    )
    assert purpose == "Shared library for agent scripts"


def test_the_coverage_check_is_language_blind(tmp_path: Path) -> None:
    """Injected proof that the next `.sh`-shaped narrowing fails the build (#4228).

    Run against a fabricated directory rather than the real one, because
    `test_tests_own_their_fixtures.py` (#4198) rightly forbids a test writing
    into the checkout. What is under test is the *rule*, and the rule is the
    same object the two tests above apply to `scripts/agents/`.
    """
    (tmp_path / "listed.sh").write_text("# listed\n", encoding="utf-8")
    (tmp_path / "listed.py").write_text('"""listed"""\n', encoding="utf-8")
    (tmp_path / "unhandled.rb").write_text("# a language the generator cannot describe\n", "utf-8")
    (tmp_path / "README.md").write_text("docs, not an entry point\n", encoding="utf-8")

    index = "| `listed.sh` | x |\n| `listed.py` | x |\n"
    assert _missing_entry_points(tmp_path, index) == ["unhandled.rb"]

    index += "| `unhandled.rb` | x |\n"
    assert _missing_entry_points(tmp_path, index) == []


def test_every_entry_says_something_useful() -> None:
    """An index line that carries no purpose forces the file open anyway."""
    lines = [
        ln for ln in INDEX_PATH.read_text(encoding="utf-8").splitlines() if ln.startswith("| `")
    ]
    assert lines, "index has no entries"
    empty = [ln for ln in lines if "(no header comment)" in ln]
    assert not empty, f"index entries with no description: {empty}"
