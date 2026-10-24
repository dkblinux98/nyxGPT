"""Instructional prose in this tree is about *this* project (#4231).

A scaffolding generator writes a README about the thing it generated. Left in
place, that file sits in a source directory telling every contributor who
opens it how to operate a different project: `web/README.md` shipped
`create-next-app`'s text for the life of the repository, which meant the first
file anyone opened in `web/` told them to run the UI with `npm run dev` and to
deploy it to Vercel -- a package-manager invocation in a product where every
lifecycle operation is a `nyxgpt` command (CLAUDE.md, "Operational Command
Wrapping"), and a hosted platform in a product whose premise is that no cloud
dependency is required.

Nothing caught it, and nothing would have: the wrapping rule names `docker`,
`kubectl` and `terraform`, so `npm run dev` is outside it, and the text was
never *stale* -- it was never about nyxGPT to begin with, so no freshness
check had a claim to compare against. This is the guard that would have, and
it is deliberately narrow in one way and broad in another:

* **Narrow in what it looks for.** The fingerprints below are strings a
  *generator* writes and a human writing about nyxGPT does not. A blocklist of
  "suspicious" commands would be a different, much noisier test, and it would
  go red on the genuine `npm`/`docker` references in `docs/` that the wrapper
  backlog (#3226) tracks -- a guard that cannot be made green teaches people
  to ignore it.
* **Broad in where it looks.** Every Markdown file in the tree, not just
  `web/README.md`. The defect is not that one file was wrong; it is that
  generated instructional text can land anywhere a generator is run, and the
  next one will not be Next.js.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories never descended into: vendored, generated or build output.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
    }
)

#: Substrings a scaffolding generator emits, mapped to what is wrong with
#: shipping them. Matched case-insensitively against whole-file text.
#:
#: Each one is a phrase no one writing about nyxGPT would type. That is the
#: selection criterion, and it is why there is no allowlist: a file needing an
#: exemption would be a file genuinely documenting Vercel deployment, which
#: this product does not have.
SCAFFOLD_FINGERPRINTS: dict[str, str] = {
    "bootstrapped with": (
        "a generator's self-description. Say what the directory is in this project instead"
    ),
    "deploy on vercel": (
        "nyxGPT is local-first; its deployment targets are in docs/portability-matrix.md"
    ),
    "vercel.com/new": "a hosted-platform deploy link; nyxGPT installs via `nyxgpt` commands",
    "utm_source=create-next-app": "a create-next-app template link left unedited",
    "nextjs.org/learn": (
        "the scaffold's link farm. Link the nyxGPT document a reader actually needs"
    ),
    "your feedback and contributions are welcome": (
        "create-next-app soliciting contributions to vercel/next.js, not to this repository"
    ),
}

#: The scaffold "Getting Started" block names several package managers for the
#: same dev server. One of these in nyxGPT prose is a deliberate reference to
#: the tool; three of them together is only ever the generated block.
_PACKAGE_MANAGER_DEV_COMMANDS = ("yarn dev", "pnpm dev", "bun dev")


def _markdown_files() -> list[str]:
    """Repo-relative paths of every Markdown file outside the skipped trees.

    `os.walk` with pruning rather than `rglob`, so `node_modules` is never
    descended into rather than descended into and filtered. Symlinks are
    skipped: `src/nyxgpt/resources/docs/README.md` is `docs/README.md`, and a
    failure reported against both paths would read as two defects.
    """
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = Path(root) / name
            if path.is_symlink():
                continue
            found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


#: Collected once: the walk is the expensive part, and parametrising over the
#: fingerprints instead of the files keeps one failure per *reason*.
MARKDOWN_FILES: tuple[str, ...] = tuple(_markdown_files())


def test_the_inventory_is_not_empty():
    """A broken walk would make every assertion below vacuously true."""
    assert "web/README.md" in MARKDOWN_FILES
    assert "docs/README.md" in MARKDOWN_FILES
    assert not any(path.startswith("web/node_modules/") for path in MARKDOWN_FILES)


@pytest.mark.parametrize(("fingerprint", "why"), sorted(SCAFFOLD_FINGERPRINTS.items()))
def test_no_markdown_carries_generator_scaffold_text(fingerprint: str, why: str):
    """One failure per fingerprint, naming every file that carries it."""
    offenders = [
        path
        for path in MARKDOWN_FILES
        if fingerprint in (REPO_ROOT / path).read_text(encoding="utf-8").lower()
    ]
    assert not offenders, (
        f"{offenders} contain {fingerprint!r}: {why}. "
        "This is scaffolding text describing another project -- rewrite the file "
        "for nyxGPT or delete it in favour of the document that covers it."
    )


def test_no_markdown_instructs_the_scaffold_dev_server_block():
    """The multi-package-manager run block, which is never hand-written."""
    offenders = []
    for path in MARKDOWN_FILES:
        text = (REPO_ROOT / path).read_text(encoding="utf-8").lower()
        if all(command in text for command in _PACKAGE_MANAGER_DEV_COMMANDS):
            offenders.append(path)
    assert not offenders, (
        f"{offenders} list {list(_PACKAGE_MANAGER_DEV_COMMANDS)} as ways to start the "
        "web UI. nyxGPT's web service is started with `nyxgpt up` / "
        "`nyxgpt ops restart web`; `nyxgpt up --dev` is the dev-server path."
    )


def test_the_web_readme_describes_this_project():
    """The instance #4231 was filed for, pinned as a property rather than as
    the absence of the old text: a README in a source directory says what the
    directory is here and points at the documents that cover it."""
    text = (REPO_ROOT / "web/README.md").read_text(encoding="utf-8")
    assert "nyxGPT" in text, "web/README.md never names the project it belongs to"
    assert "nyxgpt up" in text, "it does not say how the web UI is actually started"
    assert "../docs/" in text, "it points at none of the documents that cover the web UI"
