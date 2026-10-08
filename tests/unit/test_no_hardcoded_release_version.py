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
