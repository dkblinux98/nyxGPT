"""`_bounded_output`'s salient-line rescue must survive a line prefix (#4192).

#3861 added the rescue because a head+tail window threw away the one line that
said *why* a command failed. The pattern it added was anchored with `^`, and
every test written for it supplied a fixture with the keyword at column 0
(`"error: 499"`, `"Error: Failed changing dylib ID of ..."`). No producer ops
shells out to writes that way -- the output has already been through something
that labels each line -- so on 2026-10-21 a `terraform-local-smoke` failure on
`docker build nyxgpt-web` logged a bare `... [67 lines omitted] ...` with npm's
`npm error code <errno>` inside the omitted region, and a diagnosis round was
spent guessing at exactly what #3861 exists to prevent.

The executed half of this lives in `scripts/ops-subprocess-output-smoke.py`
(the required `ops-subprocess-output` job), which runs the real captured bytes
of that build through the real excerpt in both directions. What is pinned here
is the pattern's two properties, which is where the defect actually was:

* it must match the keyword *wherever on the line* the producer put it, and
* loosening it must not have made it "keep everything" -- a word that merely
  occurs inside an identifier is still not a tool reporting a failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nyxgpt import ops

#: `_OUTPUT_EXCERPT_SALIENT_RE` as it stood before #4192 -- the same keywords,
#: anchored to the start of the line. Injected rather than described, so the
#: "it would have been missed" half is measured and not asserted in prose.
RETIRED_ANCHORED_SALIENT_RE = re.compile(
    r"^\s*(?:error|warning|fatal|failed|exception|traceback)\b[: ]",
    re.IGNORECASE,
)

#: One line per producer ops actually runs, in that producer's own format. Each
#: of these was thrown away by the anchored pattern.
PREFIXED_FAILURE_LINES = [
    pytest.param("#23 90.52 npm error code EAI_AGAIN", id="buildkit+npm"),
    pytest.param(
        '#12 ERROR: process "/bin/sh -c npm ci" did not complete successfully',
        id="buildkit-step",
    ),
    pytest.param("nyxgpt-api-1  | ERROR: could not connect to cassandra", id="compose"),
    pytest.param("[pod/nyxgpt-api-0/api] error: readiness probe failed", id="kubectl-prefix"),
    pytest.param("make[1]: *** Error 1", id="make"),
    pytest.param("  2026-10-21 20:39:09 WARNING  disk is nearly full", id="timestamped"),
]

#: ...and the other direction. Every one of these appears in output ops carries
#: today, and none of them is a tool saying something went wrong: the keyword is
#: part of an identifier, or is a different word that merely starts the same.
#: If the loosened pattern matched these, the rescue would be "keep the last 12
#: lines of the middle", which is not a rescue.
INCIDENTAL_LINES = [
    pytest.param("    except urllib.error.HTTPError as e:", id="dotted-attribute"),
    pytest.param('      + error_tracking_dsn = "https://..."', id="snake-case-identifier"),
    pytest.param("        raise ReleaseCandidateError(", id="camel-case-suffix"),
    pytest.param("# Do not fail the request if logging reconfig fails", id="different-word"),
    pytest.param("# failure mode #3976 was filed for:", id="failure-not-failed"),
    pytest.param("npm notice New major version of npm available!", id="notice"),
    pytest.param("==> Installing nyxgpt-api", id="progress"),
]

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "bounded_output"
    / "docker-build-npm-ci-network-failure.txt"
)


@pytest.mark.unit
@pytest.mark.parametrize("line", PREFIXED_FAILURE_LINES)
def test_a_prefixed_failure_line_is_salient(line: str) -> None:
    assert ops._OUTPUT_EXCERPT_SALIENT_RE.search(line), line


@pytest.mark.unit
@pytest.mark.parametrize("line", PREFIXED_FAILURE_LINES)
def test_the_retired_anchor_missed_every_one_of_them(line: str) -> None:
    """The fault-injection half: without it the test above passes either way."""
    assert not RETIRED_ANCHORED_SALIENT_RE.match(line), line


@pytest.mark.unit
@pytest.mark.parametrize("line", INCIDENTAL_LINES)
def test_an_incidental_mention_is_not_salient(line: str) -> None:
    assert not ops._OUTPUT_EXCERPT_SALIENT_RE.search(line), line


@pytest.mark.unit
def test_the_rescue_is_searched_not_anchored() -> None:
    """Pin the call shape, not just the pattern.

    `re.match` with the anchor removed is the same defect wearing a different
    hat: the pattern would look right and still only ever match at column 0.
    """
    assert not ops._OUTPUT_EXCERPT_SALIENT_RE.pattern.startswith("^")
    source = Path(ops.__file__).read_text(encoding="utf-8")
    assert "_OUTPUT_EXCERPT_SALIENT_RE.search(line)" in source
    assert "_OUTPUT_EXCERPT_SALIENT_RE.match(" not in source


@pytest.mark.unit
def test_the_real_capture_defeats_the_retired_anchor_entirely() -> None:
    """Measured against the bytes, not against a line someone typed.

    The fixture is a real failed `docker build` -> `npm ci` (see
    `tests/fixtures/bounded_output/README.md`). Scoped to the ELIDED MIDDLE,
    which is the only region the rescue is for: BuildKit's closing
    `ERROR: failed to build: ...` is unprefixed and the anchored pattern does
    match it, but it is the last line of the output and the tail window keeps
    it either way. Inside the middle -- where npm put the errno and the
    syscall -- the anchored pattern matches nothing at all, which is why every
    test of this mechanism was green while it rescued nothing.
    """
    lines = FIXTURE.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) > ops._OUTPUT_EXCERPT_VERBATIM_MAX_LINES
    middle = lines[ops._OUTPUT_EXCERPT_HEAD_LINES : len(lines) - ops._OUTPUT_EXCERPT_TAIL_LINES]

    assert [line for line in middle if ops._OUTPUT_EXCERPT_SALIENT_RE.search(line)]
    assert not [line for line in middle if RETIRED_ANCHORED_SALIENT_RE.match(line)]


@pytest.mark.unit
def test_the_excerpt_of_the_real_capture_still_names_the_cause() -> None:
    excerpt = ops._bounded_output(FIXTURE.read_text(encoding="utf-8"))
    assert "lines omitted" in excerpt
    assert "npm error code" in excerpt
