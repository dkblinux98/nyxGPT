"""nyxGPT declares ONE Node.js version, and every surface takes it from there.

This file is the enforcement half of #4194. The decision it guards is in
`src/nyxgpt/node_runtime.py`; what is here is the reason a *copy* of that
decision cannot drift away from it again.

What it is stopping, measured 2026-10-10 before the fix:

| Surface | Node it used | Where it was decided |
|---|---|---|
| CI (7 `setup-node` steps) | 20 | a literal `node-version: '20'` in each workflow |
| the retro-dashboard smoke | 22 | a literal `node-version: "22"` |
| the web container | 20 | `FROM node:20-alpine` x3 |
| Linux / EC2 | 20 | NodeSource `setup_20.x`, twice, independently |
| **macOS (the Homebrew keg, and the owner's machine)** | **26.11** | nothing at all -- `depends_on "node"` plus the first `node` on `PATH` |
| `package.json` (both) | none | no `engines`, no version file, no `engine-strict` |

Two things followed, and both had already happened. CI could not break on the
Node macOS users run, so a Node-26-only break would first appear on the owner's
machine; and the version CI *did* test went end-of-life in April 2026 with
nothing in the tree to notice.

Why a guard test rather than a generator: the non-Python surfaces are files
humans read and review -- a Dockerfile, a Homebrew formula, nine workflows --
and generating them would move the drift rather than remove it. A copy with a
guard that names every file to change is one declaration with several
spellings. A copy with no guard is how this defect happened.

The checks are written against the DEFECT, not against the current lines:
`_node_majors_named_by` finds every Node major any of these files names,
whatever syntax it comes to use, so adding an eighth workflow or a second
container stage is covered without touching this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nyxgpt import node_runtime

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

DECLARED = node_runtime.NODE_MAJOR

# Every way this tree spells "a Node major". Each pattern's group 1 is the
# major. Deliberately broad: the point is to catch a NEW literal, not to
# describe the ones that exist today.
_NODE_MAJOR_PATTERNS = (
    # `actions/setup-node` with a literal instead of `node-version-file`.
    re.compile(r"""node-version:\s*['"]?(\d+)"""),
    # A container base image: `FROM node:24-alpine`, `node:24.1.0-slim`, ...
    re.compile(r"\bnode:(\d+)[-.\s'\"]"),
    # NodeSource: `setup_24.x`.
    re.compile(r"\bsetup_(\d+)\.x\b"),
    # A versioned Homebrew keg or distro package: `node@24`, `nodejs24`.
    re.compile(r"\bnode@(\d+)\b"),
    re.compile(r"\bnodejs(\d+)\b"),
)

# The files every surface's Node decision lives in. Listed rather than
# globbed for the workflows/docs so a missing one is a failure here (see
# `test_every_named_surface_exists`) instead of a silently skipped check.
_DECLARATION_SURFACES = (
    "web/Dockerfile",
    "homebrew/nyxgpt-web.rb",
    "homebrew/tap/nyxgpt-web.rb.tmpl",
    "scripts/cloud/ec2-user-data-linux.sh.tmpl",
    "src/nyxgpt/cloud_deploy.py",
    "src/nyxgpt/node_runtime.py",
)


_TRIPLE_QUOTED = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
_FULL_LINE_COMMENT = re.compile(r"^[ \t]*#.*$", re.MULTILINE)
_TRAILING_COMMENT = re.compile(r"[ \t]{2,}#.*$", re.MULTILINE)


def _strip_commentary(text: str, *, python: bool) -> str:
    """`text` with comments (and, for Python, docstrings) removed.

    A comment is prose, not configuration: this very file's docstring names
    Node 20, 22 and 26 while describing the defect, and the formulas explain at
    length why `depends_on "node"` was wrong. Scanning raw text makes every one
    of those a false positive, and a guard that cries wolf gets its subject
    narrowed until it stops guarding.

    Trailing comments are cut only after two or more spaces, deliberately:
    Ruby's `#{...}` interpolation is how the formulas name the keg's
    `opt_bin`, and a one-space rule would eat it.

    Python's generated shell (`cloud_deploy.NATIVE_NODE_SECTION`) goes out with
    the docstrings, which is correct -- its content is checked by *rendering*
    it (`test_the_nodesource_bootstraps_install_the_declared_major`), which is
    a stronger claim than reading its source.
    """
    if python:
        text = _TRIPLE_QUOTED.sub("", text)
    text = _FULL_LINE_COMMENT.sub("", text)
    return _TRAILING_COMMENT.sub("", text)


def _node_majors_named_by(text: str, *, python: bool = False) -> set[int]:
    """Every Node major the non-comment part of `text` names."""
    stripped = _strip_commentary(text, python=python)
    found: set[int] = set()
    for pattern in _NODE_MAJOR_PATTERNS:
        found.update(int(m) for m in pattern.findall(stripped))
    return found


def _surface_files() -> list[Path]:
    """The named surfaces plus every workflow -- the full set to check."""
    return [REPO_ROOT / rel for rel in _DECLARATION_SURFACES] + sorted(WORKFLOWS.glob("*.yml"))


def test_every_named_surface_exists():
    """A renamed or deleted surface must fail here, not vanish from the sweep.

    Without this, moving `web/Dockerfile` would silently reduce this file to
    checking one fewer thing -- the failure mode where a guard goes green by
    losing its subject.
    """
    missing = [rel for rel in _DECLARATION_SURFACES if not (REPO_ROOT / rel).is_file()]
    assert not missing, (
        "These surfaces are named by this guard but do not exist: "
        + ", ".join(missing)
        + ". If one moved, update `_DECLARATION_SURFACES` in the same commit."
    )


def test_the_version_file_holds_the_declared_major():
    """`.nvmrc` is what `nvm` and `actions/setup-node` read; it has to agree.

    It is the copy with the most leverage -- every CI job's Node comes from it
    -- and the one a Python constant cannot be read by.
    """
    nvmrc = REPO_ROOT / node_runtime.NODE_VERSION_FILE
    assert nvmrc.is_file(), f"{node_runtime.NODE_VERSION_FILE} is missing"
    content = nvmrc.read_text(encoding="utf-8").strip()
    assert content == str(DECLARED), (
        f"{node_runtime.NODE_VERSION_FILE} says {content!r} but "
        f"node_runtime.NODE_MAJOR is {DECLARED}"
    )


@pytest.mark.parametrize("manifest", ["package.json", "web/package.json"])
def test_both_package_manifests_declare_the_major_and_only_it(manifest: str):
    """`engines.node` is `<major>.x` in both manifests -- a single major, not `>=`.

    `>=24` would be satisfied by Node 26, which is precisely how `brew upgrade
    node` walked the macOS install off the tested version with nothing to say
    so. The range has to exclude the next major for the declaration to mean
    anything.
    """
    data = json.loads((REPO_ROOT / manifest).read_text(encoding="utf-8"))
    engines = data.get("engines") or {}
    assert engines.get("node") == node_runtime.NODE_ENGINES_RANGE, (
        f"{manifest} declares engines.node = {engines.get('node')!r}; expected "
        f"{node_runtime.NODE_ENGINES_RANGE!r} (one major, from "
        "node_runtime.NODE_ENGINES_RANGE)"
    )


@pytest.mark.parametrize("npmrc", [".npmrc", "web/.npmrc"])
def test_engine_strict_is_on_so_a_mismatch_fails_instead_of_warning(npmrc: str):
    """Without `engine-strict`, `engines` is a line of scrollback, not a gate.

    npm's default on an `engines` mismatch is to print EBADENGINE and install
    anyway. `web/` needs its own file as well as the root one: it ships as a
    standalone tarball and the Homebrew keg runs `npm ci` inside it with no
    repo above it, so a root-only setting would leave the one install that
    builds the shipped bundle unguarded.
    """
    path = REPO_ROOT / npmrc
    assert path.is_file(), f"{npmrc} is missing"
    body = path.read_text(encoding="utf-8")
    assert re.search(r"^\s*engine-strict\s*=\s*true\s*$", body, re.MULTILINE), (
        f"{npmrc} does not set `engine-strict=true`, so an `engines` mismatch "
        "would warn and install rather than fail"
    )


def test_no_surface_names_a_node_major_other_than_the_declared_one():
    """THE guard. Any file above naming a different major fails the build.

    This is the check that would have caught every row of the table in this
    module's docstring, and it is the one to read the failure of: it names the
    file and the major, so moving the declaration is a matter of following the
    list rather than remembering where the copies are.
    """
    offenders: list[str] = []
    for path in _surface_files():
        named = _node_majors_named_by(path.read_text(encoding="utf-8"), python=path.suffix == ".py")
        for major in sorted(named):
            if major != DECLARED:
                offenders.append(f"  {path.relative_to(REPO_ROOT)}: Node {major}")
    assert not offenders, (
        f"nyxGPT declares Node {DECLARED} (node_runtime.NODE_MAJOR / "
        f"{node_runtime.NODE_VERSION_FILE}), but these name a different major:\n"
        + "\n".join(offenders)
        + "\nEvery surface that installs, tests or runs Node takes the major from the "
        "declaration. To move lines, change node_runtime.NODE_MAJOR and `.nvmrc` and "
        "let this test name what has to move with them."
    )


def test_every_setup_node_step_reads_the_version_file():
    """A workflow may not choose its own Node, even the right one.

    `node-version: '24'` would pass the check above and still be a copy: the
    next time the declaration moves, that workflow stays behind and CI tests a
    version nothing else runs. `node-version-file` makes the workflow a reader.
    """
    literals: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for match in re.finditer(r"""node-version:\s*['"]?[\d.]+""", text):
            line = text[: match.start()].count("\n") + 1
            literals.append(f"  {workflow.name}:{line}: {match.group(0)}")
    assert not literals, (
        "These `setup-node` steps name a Node version instead of reading the "
        f"declaration. Use `node-version-file: '{node_runtime.NODE_VERSION_FILE}'`:\n"
        + "\n".join(literals)
    )


def test_some_workflow_actually_sets_node_up_from_the_file():
    """The converse of the test above: it must not pass by there being no Node.

    A test that only forbids literals goes green on a tree with `setup-node`
    deleted everywhere -- the #4174 shape, a rule whose guard measures nothing.
    """
    readers = [
        workflow.name
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        if f"node-version-file: '{node_runtime.NODE_VERSION_FILE}'"
        in workflow.read_text(encoding="utf-8")
    ]
    assert len(readers) >= 5, (
        "Expected the CI jobs that need Node to read "
        f"`node-version-file: '{node_runtime.NODE_VERSION_FILE}'`; found only {readers}"
    )


def test_the_homebrew_formulas_depend_on_the_versioned_keg():
    """macOS has to pin, and `depends_on "node"` is the opposite of pinning.

    The unversioned formula resolves to whatever major homebrew-core has moved
    to -- 26.11 on the owner's machine -- so the keg a macOS user installs was
    the surface with no declaration at all. `node@NN` is also keg-only, which
    is what keeps `brew upgrade node` from changing what nyxGPT runs.
    """
    expected = node_runtime.HOMEBREW_NODE_FORMULA
    for rel in ("homebrew/nyxgpt-web.rb", "homebrew/tap/nyxgpt-web.rb.tmpl"):
        body = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f'depends_on "{expected}"' in body, (
            f'{rel} does not `depends_on "{expected}"` -- a macOS install would take '
            "whatever major Homebrew's unversioned `node` is today"
        )
        # Commentary stripped: both formulas quote the old `depends_on "node"`
        # in the comment explaining why it was wrong.
        assert 'depends_on "node"' not in _strip_commentary(body, python=False), (
            f"{rel} still depends on the UNVERSIONED `node` formula, which is "
            "satisfied by any major"
        )
        # The dependency alone does not decide what the service runs: `node@NN`
        # is keg-only, so the wrapper has to put *that* keg's bin first.
        assert f'Formula["{expected}"].opt_bin' in body, (
            f"{rel} depends on {expected} but its service wrapper does not put that "
            "keg's `opt_bin` on PATH, so the running service would still resolve the "
            "unversioned `node` from /opt/homebrew/bin"
        )


def test_the_nodesource_bootstraps_install_the_declared_major():
    """Both Linux bootstraps, which were two independent literals.

    `cloud_deploy.NATIVE_NODE_SECTION` and the EC2 user-data template each
    carried their own `setup_20.x`; one could have been bumped without the
    other, and nothing compared them.
    """
    from nyxgpt import cloud_provision

    rendered_template = cloud_provision.render_user_data("linux", "0.0.0", None)
    from nyxgpt import cloud_deploy

    for name, body in (
        ("the rendered EC2 linux user-data", rendered_template),
        ("cloud_deploy.NATIVE_NODE_SECTION", cloud_deploy.NATIVE_NODE_SECTION),
    ):
        assert node_runtime.NODESOURCE_SETUP_SCRIPT in body, (
            f"{name} does not install NodeSource " f"{node_runtime.NODESOURCE_SETUP_SCRIPT}"
        )
        assert not _node_majors_named_by(body) - {DECLARED}, (
            f"{name} names a Node major other than {DECLARED}: "
            f"{sorted(_node_majors_named_by(body) - {DECLARED})}"
        )


def test_no_placeholder_survives_rendering_the_linux_bootstrap():
    """The template's Node placeholders are substituted, not shipped literally.

    A `__NYXGPT_NODE_MAJOR__` that reached an instance would make the bootstrap
    a syntax error in `[ "$NODE_MAJOR" -ne __NYXGPT_NODE_MAJOR__ ]` -- failing
    the install, but only on a real EC2 boot.
    """
    from nyxgpt import cloud_provision

    rendered = cloud_provision.render_user_data("linux", "0.0.0", None)
    for placeholder in (
        cloud_provision.NODE_MAJOR_PLACEHOLDER,
        cloud_provision.NODESOURCE_RPM_PLACEHOLDER,
        cloud_provision.NODESOURCE_DEB_PLACEHOLDER,
    ):
        assert placeholder not in rendered, f"{placeholder} survived rendering"


def test_the_declared_line_is_a_supported_lts():
    """A floor, not a pin: the declaration may not be an end-of-life line.

    Half of #4194 was that the version CI tested had been out of support since
    April 2026 and nothing noticed. Node 20 went EOL then, 22 entered
    maintenance, and the next session to read this has no way to know which is
    which -- so the lower bound is asserted, and moving it is a deliberate edit
    with the reasoning in `node_runtime`'s docstring.
    """
    assert DECLARED >= 24, (
        f"Node {DECLARED} is below the oldest line this project considers "
        "supported (24, Active LTS as of October 2025). Node 20 reached "
        "end-of-life in April 2026. If a newer line is being adopted, raise this "
        "floor in the same commit and say why in node_runtime's docstring."
    )
    # Even majors only: odd-numbered Node lines never become LTS.
    assert DECLARED % 2 == 0, (
        f"Node {DECLARED} is an odd-numbered line, which never becomes LTS -- "
        "nyxGPT installs and runs on LTS lines only"
    )
