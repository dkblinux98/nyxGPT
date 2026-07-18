"""Guards the `product_management/` planning-doc convention (see CLAUDE.md's
"Repository Organization" section). Phase-planning and project-management
docs must live under `product_management/`, never at the repo root, and
code/scripts must reference them by their new path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

PLANNING_DOCS = (
    "PHASE_6_PLAN.md",
    "PHASE_7_PLAN.md",
    "PHASE_8_PLAN.md",
    "PHASE_9_PLAN.md",
    "PROJECT_COMPLETION_PLAN.md",
    "PROJECT_AUDIT.md",
)

# Source/script trees that may reference a planning doc by path. Prose docs
# (CLAUDE.md, README.md, product_management/* itself) are allowed to name
# these files bare when describing the convention, so they're excluded.
REFERENCING_DIRS = ("scripts", "src", ".github")


@pytest.mark.parametrize("doc", PLANNING_DOCS)
def test_planning_doc_lives_under_product_management(doc: str) -> None:
    assert (REPO_ROOT / "product_management" / doc).is_file(), (
        f"{doc} must live under product_management/ per CLAUDE.md's "
        "Repository Organization convention."
    )
    assert not (
        REPO_ROOT / doc
    ).exists(), f"{doc} must not exist at the repo root; it belongs under product_management/."


def test_no_stray_root_references_to_planning_docs() -> None:
    stray = []
    for rel_dir in REFERENCING_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for doc in PLANNING_DOCS:
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if doc in line and f"product_management/{doc}" not in line:
                        stray.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {line.strip()}")

    assert not stray, (
        "Found reference(s) to a planning doc's old repo-root path; update "
        "them to point at product_management/:\n" + "\n".join(stray)
    )
