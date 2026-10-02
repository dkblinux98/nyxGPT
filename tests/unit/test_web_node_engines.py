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

The claim is major-level on purpose: the Node baseline is written as a major
everywhere (`node:20-alpine`, `node-version: '20'`), so both sides resolve to
"latest 20.x" and that is the strongest thing that can honestly be asserted.
Only a dependency demanding a NEWER major than the baseline fails here;
ranges this file cannot parse are skipped rather than guessed at, so a future
dependency bump cannot redden the build on a parser quirk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_FROM_NODE = re.compile(r"^FROM\s+node:(\d+)[-.]", re.MULTILINE)
_NODE_VERSION = re.compile(r"""node-version:\s*['"]?(\d+)""")
# A workflow that runs npm against the web tier, however it gets there.
_TOUCHES_WEB = re.compile(r"working-directory:\s*\.?/?web\b|cd\s+web\b|web/package")

# One `||` alternative's lower bound: `>=22.19.0`, `^20.9.0`, `~18.1`, `18`,
# `4.x`. Anything else in the alternative (upper bounds, `*`, prose) carries no
# lower bound and so cannot demand a newer major.
_LOWER_BOUND = re.compile(r"(?:>=?|\^|~|=)?\s*(\d+)(?:\.[\dx*]+)*\s*$")


def _node_baseline_major() -> int:
    """The Node major the web image is built with, read from its Dockerfile."""
    dockerfile = (WEB / "Dockerfile").read_text(encoding="utf-8")
    majors = {int(m) for m in _FROM_NODE.findall(dockerfile)}
    assert majors, "web/Dockerfile declares no `FROM node:<major>` stage"
    assert len(majors) == 1, (
        f"web/Dockerfile builds on more than one Node major ({sorted(majors)}). "
        "The deps stage, the builder and the runner must agree, or the tree that "
        "installs is not the tree that runs."
    )
    return majors.pop()


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


def test_every_workflow_that_runs_npm_in_web_uses_the_build_images_node():
    """A split baseline is the same defect with a longer fuse.

    `validate-web` passing on Node 22 while `web/Dockerfile` builds on 20 would
    let exactly this class through the gate that exists to catch it.
    """
    baseline = _node_baseline_major()
    mismatched: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        if not _TOUCHES_WEB.search(text):
            continue
        for major in _NODE_VERSION.findall(text):
            if int(major) != baseline:
                mismatched.append(f"  {workflow.name}: node-version {major}")
    assert not mismatched, (
        f"web/Dockerfile builds on Node {baseline}; these workflows run npm against "
        "web/ on a different major:\n" + "\n".join(mismatched)
    )
