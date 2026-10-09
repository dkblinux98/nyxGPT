"""No user-facing string tells a reader to open a path only a checkout has (#4182).

The rule is the repo-less requirement, stated as a rule: **the entire stack
must be installable and runnable without checking out or downloading the code
repository**, so a message the product prints must not point at a file that
only exists in the repository. On a Homebrew keg, a wheel, a Compose container
or a Kubernetes Pod there is no `docs/` directory beside the running code --
and no `product_management/` anywhere, in any install.

It went unenforced, which is how it drifted to about twenty messages across
the CLI and the API before the owner found it by reading `nyxgpt ops status`
on a keg install. Two of them cited
`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`, a document that is
not shipped even in the packaged docs tree. #4174's lesson applies directly:
a rule this project states but does not CHECK is itself a defect class.

The fix every hit takes is `nyxgpt.doc_links` -- `see_doc()` for a pointer
phrase, `doc_url()` for a bare URL -- which resolves a repo-relative path to
the in-app docs route (for a packaged document) and the hosted copy (always).
One place decides it, so the next document to be added or unpackaged does not
need twenty edits.

What is read, and why:

* **Python string literals in `src/`** that are not docstrings. A docstring is
  developer prose about the code; it is not printed to a user. `--help` text,
  `print()` arguments, `OpsResult` messages, exception text and rendered shell
  scripts all are.
* **JSX/TS string and text content in `web/src/`**, for the same reason: the
  Infrastructure and Canary pages told operators to read `docs/terraform.md`
  from a browser.

What is deliberately NOT read:

* `# comments` and docstrings -- a comment naming `docs/ops.md` is a pointer
  for the next developer, who has the checkout open.
* `tests/` and `scripts/` -- a test fixture asserting the old text, and the
  agent-loop scripts, run only in a checkout by construction.
* The documents themselves: a relative link inside `docs/` is correct in the
  repository and is rewritten for the viewer by `support._rewrite_link`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

#: A path into the repository that an installed artifact does not carry.
#: `docs/` because the artifact packages the tree under `nyxgpt/resources/docs`
#: and serves it on a route, never at this path; `product_management/` because
#: it is not shipped at all.
REPO_PATH = re.compile(r"\b(?:docs/[A-Za-z0-9_.-]+\.md|product_management/[A-Za-z0-9_./-]+\.md)")

#: Files whose own subject is the resolution of these paths. They name the
#: shape in order to rewrite it, so a match in them is the fix, not the defect.
ALLOWED_FILES = {
    "src/nyxgpt/doc_links.py",
    "src/nyxgpt/support.py",
}


#: The resolvers in `nyxgpt.doc_links`. A repo-relative path passed to one of
#: these is the path being RESOLVED, so it is exempt -- what reaches the user
#: is the URL that comes back.
RESOLVERS = {"doc_url", "doc_route", "see_doc"}

#: Keyword arguments whose value is a list of repository artifacts being
#: cited rather than a message being printed. See the comment at the use site.
CITATION_FIELDS = {"evidence"}


def _resolver_name(func: ast.expr) -> str:
    """The bare name a call targets (`doc_url`, `doc_links.doc_url`), or `""`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _python_hits() -> list[str]:
    """Every non-docstring string literal in `src/` naming a repo path."""
    hits: list[str] = []
    for path in sorted(ROOT.glob("src/**/*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES or "__pycache__" in rel:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        exempt = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ) and (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                exempt.add(id(body[0].value))
            # The path handed TO the resolver is the fix, not a defect: it is
            # an argument in code, and what reaches the user is the URL the
            # call returns.
            if isinstance(node, ast.Call) and _resolver_name(node.func) in RESOLVERS:
                for argument in node.args:
                    if isinstance(argument, ast.Constant):
                        exempt.add(id(argument))
            # `Target(evidence=(...))` in `portability.py` is a CITATION list,
            # not a message: naming repository artifacts is the field's entire
            # purpose, `_missing_evidence` resolves each one against a
            # checkout when there is one, and the report already says it
            # cannot when there is not. The printed report names the hosted
            # tree once so every citation is followable anyway.
            for keyword in getattr(node, "keywords", []):
                if keyword.arg in CITATION_FIELDS:
                    for element in ast.walk(keyword.value):
                        if isinstance(element, ast.Constant):
                            exempt.add(id(element))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in exempt:
                continue
            for match in REPO_PATH.finditer(node.value):
                hits.append(f"{rel}:{node.lineno}: {match.group(0)}")
    return hits


#: A single-line `//` comment, or a line continuing a `/* */` block.
_TS_LINE_COMMENT = re.compile(r"^\s*(//|\*/?)")


def _web_hits() -> list[str]:
    """Every non-comment line in `web/src/` naming a repo path.

    Block comments are tracked across lines, not matched per line: the
    Infrastructure page carries long `{/* ... */}` rationales whose interior
    lines look like ordinary prose, and those are notes to the next developer
    (who has the checkout) rather than anything a browser renders.
    """
    hits: list[str] = []
    for pattern in ("web/src/**/*.ts", "web/src/**/*.tsx"):
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            in_block = False
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                opens = "/*" in line
                closes = "*/" in line
                skip = in_block or opens or _TS_LINE_COMMENT.match(line) is not None
                if opens and not closes:
                    in_block = True
                elif closes:
                    in_block = False
                if skip:
                    continue
                for match in REPO_PATH.finditer(line):
                    hits.append(f"{rel}:{number}: {match.group(0)}")
    return hits


def test_no_user_facing_string_names_a_repo_relative_path():
    hits = _python_hits() + _web_hits()
    assert not hits, (
        "A user-facing string names a path only a checkout has. An installed keg, "
        "wheel, container or Pod does not carry `docs/` or `product_management/`, so "
        "this message points at nothing (#4182, repo-less requirement). Use "
        "`nyxgpt.doc_links.see_doc(...)` / `doc_url(...)`, which resolve to the "
        "in-app docs route and the hosted copy. A pointer meant for a developer "
        "belongs in a comment or docstring, which this check skips.\n" + "\n".join(hits)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('x = "see docs/ops.md"', ["docs/ops.md"]),
        ('x = "see product_management/VISION.md"', ["product_management/VISION.md"]),
        ('# see docs/ops.md\nx = "fine"', []),
        ('def f():\n    """See docs/ops.md."""\n    return 1', []),
        ("x = f\"see {doc_url('docs/ops.md')}\"", []),
    ],
)
def test_the_scanner_reads_user_facing_strings_only(source, expected, tmp_path, monkeypatch):
    """The discriminator, pinned: a printed string is read, prose about the
    code is not -- and the fix's own `doc_url('docs/ops.md')` call is a name
    in code, not a string the product prints."""
    package = tmp_path / "src" / "nyxgpt"
    package.mkdir(parents=True)
    (package / "sample.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr("tests.unit.test_no_repo_relative_user_paths.ROOT", tmp_path, raising=False)
    import tests.unit.test_no_repo_relative_user_paths as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert [hit.rsplit(": ", 1)[1] for hit in module._python_hits()] == expected
