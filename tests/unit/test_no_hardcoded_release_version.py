"""No live code names a release line; the release version is never hard-coded.

The rule is the owner's (#3614, 2026-08-04; restated 2026-10-08): release
branches roll every version, so a literal `vX.Y.Z` in code that runs is a time
bomb that goes off at the next roll. It was enforced by
`scripts/agents/check_no_hardcoded_release_branch.sh` -- which no workflow
invoked, so the rule drifted exactly as an unenforced rule does. On the day
v3.0.1 was cut the drift was:

* `inverse_claims_sweep.default_base` tried `origin/v3.0.0` first. It still
  worked only because that branch had been deleted and the loop fell through.
* `code_scan_report.yml` defaulted its dispatch ref to `refs/heads/v2.1.0`, a
  branch retired a release earlier.
* `ledger_ids.py` taught `--base origin/v3.0.0` in its usage and `--help` --
  the command agents copy, which fails the moment that branch is gone.
* `release_candidate.py` told operators "release branches (v3.0.0) only".

This is that script's rule as a check that runs: it lives in `tests/unit`, so
`ci-tests.yml` runs it with the rest of pytest, and the script is deleted.

**#4182 extended it to Markdown, and that gap was the proof it was needed.**
The scanner read code only, so `docs/` kept telling readers to run
`brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc` -- a formula the release
ceremony had already retired from the tap, in the install instructions of
three documents. An install command that cannot work is the same class of
defect as a status line that is not true for the machine it prints on, which
is what #4182 is about.

What the Markdown half reads is narrower than the code half, and deliberately:
**a line inside a fenced block that begins with a command.** That is what a
reader copies and runs. Prose naming a past release is a record, and sample
*output* showing a concrete version is evidence about a real run -- the
`"version": "3.0.0rc13"` in `docs/api.md`'s `/api/v1/info` response is more
useful with a real version in it than with a placeholder, and no one can run
it. So the discriminator is "would a reader type this?", not "does this name
a version?"

What it reads is *live text*: code and the strings it prints. Comments are
skipped on purpose -- "the v3.0.0 ruleset" in a comment is a record of what
happened, not a claim about what is current, and rewriting history to satisfy
a scanner would destroy the reasons the comments exist. A Python docstring is
NOT a comment here: it is what `--help` and readers copy, so an example in one
uses a placeholder (`vX.Y.Z`, `origin/<release-branch>`).

What it does not read, and why:

* `tests/` -- a test may need a concrete line, and the ones that do supply it
  themselves (`test_release_candidate.py` pins `FIXTURE_RELEASE`;
  `test_pr_lane_hygiene.sh` derives the line from pyproject.toml). Whether a
  test silently depends on the *live* version is a behavioural question no
  scanner answers; bumping pyproject.toml and running the suite does, and that
  is how the two that did were found.
* `scripts/retrospective/` -- its era table maps past phases to the releases
  they built. That is recorded history, not an assumption about the present.
* Sandbox fixtures use `v9.9.9`, the repo's fake-line convention
  (`conflict-resolution-smoke.yml`), which is allowed by name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

#: Where live code lives. Globs are relative to ROOT.
SCANNED = (
    ".github/workflows/*.yml",
    "scripts/**/*.sh",
    "scripts/**/*.py",
    "src/**/*.py",
    "web/src/**/*.ts",
    "web/src/**/*.tsx",
)

EXCLUDED_DIRS = ("scripts/retrospective/",)

#: A release-line name: `v` + three dotted numbers, not part of a longer token.
RELEASE_LINE = re.compile(r"(?<![\w.:@-])v\d+\.\d+\.\d+(?![\w.])")

#: The sandbox fake line. Anything else that matches must be a placeholder.
ALLOWED = {"v9.9.9"}


def _live_text(path: Path, line: str) -> str:
    """The part of `line` that is code or string, with comments removed."""
    stripped = line.strip()
    if path.suffix in (".ts", ".tsx"):
        if stripped.startswith(("//", "*", "/*")):
            return ""
        return line.split(" //", 1)[0]
    # `//` too: workflows embed JavaScript (actions/github-script).
    if stripped.startswith(("#", "//")):
        return ""
    # A trailing `# comment` (a pinned action's `# v1.0.201`, an explanatory
    # aside). A `#` inside a string would be cut too; the cost is a missed hit
    # in that string, never a false one.
    return line.split(" #", 1)[0]


def _hits() -> list[str]:
    found = []
    for pattern in SCANNED:
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if any(rel.startswith(d) for d in EXCLUDED_DIRS) or "__pycache__" in rel:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in RELEASE_LINE.finditer(_live_text(path, line)):
                    if match.group(0) not in ALLOWED:
                        found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_no_live_code_names_a_release_line():
    hits = _hits()
    assert not hits, (
        "A release line is named in live code. Resolve it at run time "
        "(vars.RELEASE_BRANCH, get_release_branch(), the repo default branch, "
        "or pyproject.toml's declared version), or use a placeholder in an "
        "example (vX.Y.Z, origin/<release-branch>). A record of the past belongs "
        "in a comment.\n" + "\n".join(hits)
    )


# --- the Markdown half (#4182) -------------------------------------------

#: The documentation a reader follows. `README.md` is included even though it
#: is a pointer layer by owner decision (#3743) -- it still carries an install
#: pointer, which is exactly the shape that breaks.
SCANNED_DOCS = ("docs/**/*.md", "README.md")

#: A version a reader would be told to install: `3.0.0`, `3.0.0rc17`,
#: `v3.0.0`. Unlike `RELEASE_LINE` an `@` may precede it, because
#: `nyxgpt-api@3.0.0rc` is the exact shape that broke; `:` and `/` still may
#: not, so a pinned image tag (`traefik/whoami:v1.10.1`) is not a hit.
DOC_VERSION = re.compile(r"(?<![\w.:/-])v?\d+\.\d+\.\d+(?:rc\d*)?(?![\w.])")

#: What a reader types. Anchored at the start of a fenced-block line, with an
#: optional `$` prompt, and requiring whitespace or end-of-line after the word
#: so `nyxgpt-api@3.0.0rc started` -- a line of sample *output* -- is not read
#: as a `nyxgpt` command.
DOC_COMMAND = re.compile(
    r"^\s*(?:\$\s*)?(?:brew|pip|pip3|pipx|python|python3|nyxgpt|curl|kubectl|docker|"
    r"terraform|gh|sudo)(?:\s|$)"
)


def _doc_hits() -> list[str]:
    """Every fenced command line in the docs that hard-codes a version."""
    found = []
    for pattern in SCANNED_DOCS:
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            fenced = False
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                if not fenced or not DOC_COMMAND.match(line):
                    continue
                for match in DOC_VERSION.finditer(line):
                    if match.group(0) not in ALLOWED:
                        found.append(f"{rel}:{number}: {line.strip()}")
                        break
    return found


def test_no_doc_instruction_hard_codes_a_release_version():
    hits = _doc_hits()
    assert not hits, (
        "A command a reader is told to run names a hard-coded release version. "
        "Release lines roll and the ceremony retires the previous line's rc "
        "formulas, so this instruction stops working -- `brew install "
        "nyxgpt-api@3.0.0rc` was already dead in three documents when #4182 was "
        "filed. Use the placeholder form the surrounding prose already uses "
        "(`nyxgpt-api@<release>rc`, `--version <version>`, `nyxgpt==<version>`) "
        "and say where the current value comes from. Sample OUTPUT may carry a "
        "concrete version: it is evidence about a run, not something to type.\n" + "\n".join(hits)
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc", True),
        ("brew install nyxgpt-api@<release>rc", False),
        ("pip install nyxgpt==3.0.0rc3", True),
        ("pip install nyxgpt==<version>", False),
        ("nyxgpt cloud deploy --version 3.0.0rc3", True),
        ("nyxgpt cloud deploy --version <version>", False),
        # Sample output, not a command: `nyxgpt-api@...` is not `nyxgpt ...`.
        ("nyxgpt-api@3.0.0rc started", False),
        ("nyxgpt-api@3.0.0rc: refusing to build against python@3.12", False),
        # A pinned third-party image is not a nyxGPT release.
        ("docker pull traefik/whoami:v1.10.1", False),
        ("brew install python@3.12", False),
    ],
)
def test_the_doc_scanner_reads_commands_only(line, expected):
    """The discriminator, pinned: what a reader types is read, what the
    machine printed back is not."""
    matched = bool(DOC_COMMAND.match(line)) and any(
        m.group(0) not in ALLOWED for m in DOC_VERSION.finditer(line)
    )
    assert matched is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('BASE="v3.0.0"', ["v3.0.0"]),
        ('for candidate in ("origin/v3.0.0", "origin/HEAD"):', ["v3.0.0"]),
        ("        default: 'refs/heads/v2.1.0'", ["v2.1.0"]),
        ("# the v3.0.0 ruleset rejected it", []),
        ("uses: actions/checkout@v4", []),
        ("uses: anthropics/claude-code-action@c81e3bc # v1.0.201", []),
        ('IMAGE="traefik/whoami:v1.10.1"', []),
        ('BASE="v9.9.9"', ["v9.9.9"]),
    ],
)
def test_the_scanner_reads_live_text_only(line, expected):
    """The discriminator, pinned: code is read, comments and pins are not."""
    live = _live_text(Path("x.sh"), line)
    assert [m.group(0) for m in RELEASE_LINE.finditer(live)] == expected
