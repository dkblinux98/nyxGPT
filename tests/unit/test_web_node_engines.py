"""The web tier's dependencies must be installable on the Node it is built with.

The defect this exists to stop (2026-10-02, found while fixing #3986): a
Dependabot commit (`04911aec`) moved `web/package.json`'s direct `undici` from
`^6.15.0` to `^8.11.2` while declaring only a `@vitest/mocker` bump for
`/web`. `undici@8` declares ``engines: {node: ">=22.19.0"}`` and calls
``require('node:worker_threads').markAsUncloneable``, which does not exist on
Node 20 -- and every CI job plus `web/Dockerfile` build on Node 20. So
`next build` died collecting the one route that imports it::

    Error: Failed to collect page data for /api/chat/stream
      [cause]: TypeError: e.util.markAsUncloneable is not a function

`npm ci` said nothing (engine mismatches are warnings), the types checked, the
1937 web tests passed, and every local dev machine on Node 22 built it fine.
What it took down was 15 CI checks at once -- `validate-web`,
`artifact-install`, `linux-native-smoke`, `terraform-local-smoke`,
`k8s-local-smoke`, `k8s-artifact-smoke`, `restart-activation-smoke` and
`session-backend-fault-injection` -- because all of them build the web image,
and none of them reports the cause anywhere near the top.

The claim is major-level on purpose: the Node baseline is written as a major,
so both sides resolve to "latest <major>.x" and that is the strongest thing
that can honestly be asserted. Only a dependency demanding a NEWER major than
the baseline fails here; ranges this file cannot parse are skipped rather than
guessed at, so a future dependency bump cannot redden the build on a parser
quirk.

Since #4194 the baseline is nyxGPT's ONE Node declaration
(`node_runtime.NODE_MAJOR`), not a value derived from `web/Dockerfile`. The
derivation was the right shape with the wrong source: the Dockerfile was itself
an unguarded copy, so "the Node the web image is built with" could be -- and
was -- a different major from the Node CI ran, the Linux installs provisioned
and the macOS keg served. `tests/unit/test_node_version_declaration.py` is what
now holds the Dockerfile equal to the declaration; this file asks the separate
question of whether the locked dependency tree can run on it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nyxgpt import node_runtime

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_FROM_NODE = re.compile(r"^FROM\s+node:(\d+)[-.]", re.MULTILINE)
# `node-version:` whose value is an Actions expression rather than a literal.
# `developer_auto_implement.yml` has to use this form: it runs from the release
# branch's definition against a checked-out WORK branch that may predate the
# version file, so a step resolves the declaration in shell first and
# `setup-node` consumes its output. That is still a reader -- the accompanying
# check is that such a workflow does read the declaration file.
_NODE_VERSION_EXPR = re.compile(r"node-version:\s*\$\{\{")
# A workflow that runs npm against the web tier, however it gets there.
_TOUCHES_WEB = re.compile(r"working-directory:\s*\.?/?web\b|cd\s+web\b|web/package")

# One `||` alternative's lower bound: `>=22.19.0`, `^20.9.0`, `~18.1`, `18`,
# `4.x`. Anything else in the alternative (upper bounds, `*`, prose) carries no
# lower bound and so cannot demand a newer major.
_LOWER_BOUND = re.compile(r"(?:>=?|\^|~|=)?\s*(\d+)(?:\.[\dx*]+)*\s*$")


def _node_baseline_major() -> int:
    """The Node major nyxGPT declares -- and which the web image must be built on.

    Read from the declaration (#4194) rather than from `web/Dockerfile`, with
    the Dockerfile then checked AGAINST it. The old direction made this file's
    whole premise depend on an unguarded copy: whatever major the Dockerfile
    happened to name became "the baseline", so the file could not notice that
    the image was built on 20 while nothing else was.
    """
    declared = node_runtime.NODE_MAJOR
    dockerfile = (WEB / "Dockerfile").read_text(encoding="utf-8")
    majors = {int(m) for m in _FROM_NODE.findall(dockerfile)}
    assert majors, "web/Dockerfile declares no `FROM node:<major>` stage"
    assert majors == {declared}, (
        f"web/Dockerfile builds on Node {sorted(majors)}, but nyxGPT declares Node "
        f"{declared} (node_runtime.NODE_MAJOR / .nvmrc). Every stage -- deps, builder, "
        "runner -- takes the declared major, or the tree that installs is not the tree "
        "that runs."
    )
    return declared


def _lower_bound_major(alternative: str) -> int:
    """The lowest Node major this `||` alternative admits, or 0 if unbounded.

    An alternative is a space-separated conjunction (`>=8.10.0 <9.0.0`); its
    lower bound is the largest lower bound among its comparators. A comparator
    this cannot read contributes nothing, which keeps the check conservative.
    """
    bound = 0
    for comparator in alternative.split():
        if comparator.startswith("<"):
            continue
        match = _LOWER_BOUND.match(comparator)
        if match:
            bound = max(bound, int(match.group(1)))
    return bound


def _required_major(engines_node: str) -> int:
    """The lowest Node major the whole range admits (0 when it admits anything)."""
    alternatives = [part.strip() for part in engines_node.split("||")]
    bounds = [_lower_bound_major(part) for part in alternatives if part]
    return min(bounds) if bounds else 0


def _locked_engines() -> list[tuple[str, str]]:
    """`(package path, engines.node)` for every package in the web lockfile."""
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for path, meta in lock.get("packages", {}).items():
        engines = meta.get("engines")
        if isinstance(engines, dict):
            declared = engines.get("node")
        elif isinstance(engines, list):
            # The legacy array form, e.g. ["node >= 0.2.0"].
            declared = next((e for e in engines if "node" in e), None)
        else:
            declared = None
        if isinstance(declared, str) and declared.strip():
            found.append((path, declared.strip()))
    return found


def test_the_web_lockfile_has_engine_declarations_to_check():
    """A lockfile that suddenly declares nothing would make this file vacuous."""
    assert len(_locked_engines()) > 100


def test_no_web_dependency_requires_a_newer_node_than_the_build_image():
    """The #4126 catch: a dep demanding Node > the baseline breaks every build.

    The failure surfaces as a Next.js page-data error on whichever route
    happens to import the package, nowhere near the dependency that caused it,
    so the message here names the package and the range instead.
    """
    baseline = _node_baseline_major()
    offenders = [
        (path, declared)
        for path, declared in _locked_engines()
        if _required_major(declared) > baseline
    ]
    assert not offenders, (
        f"web/Dockerfile builds on Node {baseline}, but these locked packages require a "
        "newer major:\n"
        + "\n".join(f"  {path}: engines.node = {declared}" for path, declared in offenders)
        + "\nEither pin the dependency back to a range that supports Node "
        f"{baseline}, or raise the Node baseline everywhere at once (web/Dockerfile "
        "and every workflow that runs npm in web/)."
    )


def test_every_workflow_that_runs_npm_in_web_sets_node_up_from_the_declaration():
    """A split baseline is the same defect with a longer fuse.

    `validate-web` passing on Node 22 while `web/Dockerfile` builds on 20 would
    let exactly this class through the gate that exists to catch it.

    Rewritten for #4194. It used to compare each workflow's `node-version`
    literal against the baseline, which now passes vacuously -- there are no
    literals left, and a test that goes green by losing its subject has stopped
    guarding. The claim it makes instead is the stronger one the fix made
    available: a workflow that runs npm against `web/` must SET NODE UP, from
    the declaration file, rather than take whatever the runner image ships.
    """
    baseline = _node_baseline_major()
    offenders: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        if not _TOUCHES_WEB.search(text):
            continue
        # Reusable workflows and jobs that only *read* a web path (a `paths:`
        # filter, a `web/package` mention in a comment) have no npm step to
        # set Node up for; `setup-node` being absent entirely is the signal.
        if "actions/setup-node" not in text:
            continue
        reads_the_file = f"node-version-file: '{node_runtime.NODE_VERSION_FILE}'" in text
        # The indirect form is a reader too, provided the version it feeds
        # `setup-node` comes out of the declaration file in this same workflow.
        resolves_it = bool(_NODE_VERSION_EXPR.search(text)) and (
            node_runtime.NODE_VERSION_FILE in text
        )
        if not (reads_the_file or resolves_it):
            offenders.append(f"  {workflow.name}")
    assert not offenders, (
        f"nyxGPT declares Node {baseline} in {node_runtime.NODE_VERSION_FILE}; these "
        "workflows set Node up for the web tier without reading it:\n" + "\n".join(offenders)
    )
