"""Guard for `agents/CONTEXT_INDEX.md` (D-021).

The index is what the bootstrap reads *instead of* `.github/workflows/*` and
`scripts/agents/*`. That trade only holds while the index is complete: a
workflow missing from it is a workflow no agent knows exists, which is worse
than the exhaustive reading it replaced -- the agent does not know what it is
missing. So the committed copy must match what the generator produces.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = REPO_ROOT / "agents" / "CONTEXT_INDEX.md"
GENERATOR_PATH = REPO_ROOT / "scripts" / "build_context_index.py"


def _generator():
    spec = importlib.util.spec_from_file_location("build_context_index", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_committed_index_is_current() -> None:
    """Regenerating must be a no-op; otherwise the index has drifted."""
    expected = _generator().build()
    actual = INDEX_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "agents/CONTEXT_INDEX.md is stale -- run `python3 scripts/build_context_index.py` "
        "and commit the result (a file added since the last regeneration is invisible "
        "to every agent that trusts the index)"
    )


def test_index_covers_every_workflow_and_agent_script() -> None:
    """Coverage stated as a property, not inferred from the generator's own output."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        assert f"`{path.name}`" in text, f"workflow {path.name} is missing from the index"
    for path in sorted((REPO_ROOT / "scripts" / "agents").glob("*.sh")):
        assert f"`{path.name}`" in text, f"agent script {path.name} is missing from the index"


def test_every_entry_says_something_useful() -> None:
    """An index line that carries no purpose forces the file open anyway."""
    lines = [
        ln for ln in INDEX_PATH.read_text(encoding="utf-8").splitlines() if ln.startswith("| `")
    ]
    assert lines, "index has no entries"
    empty = [ln for ln in lines if "(no header comment)" in ln]
    assert not empty, f"index entries with no description: {empty}"
