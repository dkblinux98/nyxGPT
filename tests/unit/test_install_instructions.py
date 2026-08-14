"""The install paths a fresh user actually follows (#3752).

Acceptance testing failed on a stock Homebrew-Python Mac: the README led
with a pip install, which Homebrew's PEP 668 externally-managed Python
refuses outright, and the tap sequence stopped at Homebrew's third-party
trust gate because no document mentioned it. Most of that is prose, so these
tests read the shipped Markdown -- prose is the deliverable there, and it is
what regressed.

The same sequence also ships as *generated* text (`nyxgpt release publish`'s
`commands.brew`, rendered verbatim by the admin portability panel -- the
surface the owner installs rc builds from; the acceptance-round note; the rc
release notes and run summaries) and as *executable* shell (the EC2 Mac
user-data template, the published-tap CI job). A Markdown-only sweep cannot
see any of it, so the gate does not care which kind of file the sequence came
from: `_untrusted_taps` reads every shipped text file in the tree.

That whole-tree sweep is the point, not an implementation detail. Two review
rounds on #3752 were each spent enumerating the offending files by hand, and
each hand-grep had a different blind spot; a scan over a fixed list of files
inherits exactly that weakness. This one is bounded by *file class*
(`_SCANNED_SUFFIXES`) with every exclusion stated in `_SCAN_ALLOWLIST`, so a
newly written install sequence is covered the day it lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nyxgpt import portability
from nyxgpt import release_candidate as rc

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_HOMEBREW_DOC = _REPO_ROOT / "docs" / "homebrew.md"

#: Every shipped text file class the untrusted-tap scan reads.
#:
#: Enumerating the *files* that carry an install sequence is what failed
#: twice on #3752: two hand-written greps, two different blind spots, and a
#: regression suite that inherited them. The scan reads the whole tree
#: instead, so a new sequence is covered the moment it is written -- in any
#: file of these classes -- rather than when someone remembers to list it.
_SCANNED_SUFFIXES = frozenset({".md", ".py", ".sh", ".tmpl", ".yml", ".yaml", ".ts", ".tsx"})

#: Directories with nothing of ours in them (vendored, generated, or VCS).
_SKIPPED_DIRECTORIES = frozenset(
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

#: Files the scan skips, each with the reason it has to. Anything not listed
#: here is scanned: a skip is a deliberate, reviewable act.
_SCAN_ALLOWLIST: dict[Path, str] = {
    Path("tests")
    / "unit"
    / "test_install_instructions.py": (
        "this module's own negative fixtures are deliberately untrusted "
        "sequences -- they are what proves the scan can fail"
    ),
}

#: The tap's Homebrew name (`dkblinux98/homebrew-nyxgpt` is the repository
#: behind it; brew strips the conventional `homebrew-` prefix).
_TAP = "dkblinux98/nyxgpt"

_FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _shell_blocks(path: Path) -> list[str]:
    """Every fenced shell block in `path`, body only."""
    return [
        body
        for language, body in _FENCE.findall(path.read_text(encoding="utf-8"))
        if language in ("bash", "sh", "shell", "console", "")
    ]


def _documents_with_install_sequences() -> list[Path]:
    docs = sorted((_REPO_ROOT / "docs").glob("*.md"))
    return [_README, *docs]


def test_readme_routes_macos_to_the_tap_before_it_mentions_pip():
    """A macOS reader must meet brew first -- reaching pip at all is the bug."""
    readme = _README.read_text(encoding="utf-8")

    brew = readme.index(f"brew tap {_TAP}")
    pip = readme.index("pip")

    assert brew < pip, "pip appears above the macOS brew sequence in the Install section"


def test_readme_never_gives_an_unqualified_pip_install_as_the_install_command():
    """`pip install nyxgpt` on its own line is the instruction that failed."""
    commands = [line.strip() for block in _shell_blocks(_README) for line in block.splitlines()]

    assert "pip install nyxgpt" not in commands
    assert "pip3 install nyxgpt" not in commands


def test_readme_documents_the_trust_step_in_its_macos_sequence():
    macos_block = next(
        block for block in _shell_blocks(_README) if "brew install nyxgpt-api" in block
    )

    assert f"brew tap {_TAP}" in macos_block
    assert f"brew tap-trust {_TAP}" in macos_block


def test_readme_names_pep_668_so_the_macos_reader_knows_why_not_pip():
    readme = _README.read_text(encoding="utf-8")

    assert "PEP 668" in readme


def test_homebrew_doc_has_a_trust_section_the_install_sequences_can_point_at():
    homebrew = _HOMEBREW_DOC.read_text(encoding="utf-8")

    assert "## Trusting the tap" in homebrew
    assert f"brew tap-trust {_TAP}" in homebrew


@pytest.mark.parametrize("path", _documents_with_install_sequences(), ids=lambda p: p.name)
def test_every_documented_first_install_trusts_the_tap_it_adds(path: Path):
    """A block that taps *and* installs is a first-install sequence.

    Homebrew stops such a sequence at the trust gate, so one that adds a tap
    and installs from it in the same breath has to trust it in the same
    breath too. Blocks that only install (a machine whose tap is already
    trusted -- switching channels, upgrading) are deliberately not covered.
    """
    for block in _shell_blocks(path):
        taps = "brew tap " in block
        installs = "brew install nyxgpt-" in block
        if taps and installs:
            assert "brew tap-trust " in block, f"untrusted tap sequence in {path.name}:\n{block}"


def test_the_generated_rc_install_command_trusts_the_tap_it_adds():
    """`commands.brew` is rendered verbatim on the admin portability panel.

    It is the sequence the owner installs an rc from to acceptance-test it --
    i.e. the exact journey that produced #3752 -- so it has to survive the
    trust gate as written, without an operator editing it first.
    """
    brew = rc.plan("v3.0.0", "rc", published=("3.0.0rc1",))["commands"]["brew"]

    assert f"brew tap {_TAP}" in brew
    assert f"brew tap-trust {_TAP}" in brew
    assert brew.index("brew tap-trust") < brew.index("brew install")


def test_the_generated_rc_install_command_survives_a_homebrew_without_tap_trust():
    """Trust is tolerated-failure: one line has to run on both Homebrews.

    A bare `&& brew tap-trust ... && brew install` would abort the whole
    command on a Homebrew old enough not to gate third-party taps, trading
    one dead end for another.
    """
    brew = rc.plan("v3.0.0", "rc", published=("3.0.0rc1",))["commands"]["brew"]

    assert f"(brew tap-trust {_TAP} || true)" in brew


#: A shell command chain is one *logical* line per command. Splitting on the
#: separators matters because the sequences that survived two review rounds
#: were written as a single physical line -- `brew tap X && brew install Y`
#: in a release-notes body, and the same chain fragmented across two adjacent
#: Python string literals (an escaped `\n` inside one of them, or none at
#: all). A physical-line scan reads all of those as one line and sees nothing.
_CHAIN_SEPARATOR = re.compile(r"&&|\|\||;|\\n")

#: `brew tap <target>`, but not `brew tap-new`/`brew tap-trust` and not the
#: tail of the word "Homebrew" -- "the remote Homebrew tap and ..." is prose.
_BREW_TAP = re.compile(r"(?<![\w-])brew tap(?!-)\s+[\"'$\w]")
_BREW_TAP_TRUST = re.compile(r"(?<![\w-])brew tap-trust(?![\w-])")
_BREW_INSTALL = re.compile(r"(?<![\w-])brew install(?![\w-])")

#: Prose about an install sequence is not an install sequence. Comments
#: legitimately name the commands they are explaining -- including this
#: repo's own `# ... without this, brew install stops ...` notes.
_COMMENT = re.compile(r"^(#|//|/\*|\*(?!\*)|<!--|--\s)")


def _untrusted_taps(script: str) -> list[str]:
    """Tap commands that reach a `brew install` without trusting in between.

    Line-wise rather than whole-file, because these files hold several
    sequences: the smoke workflow installs `python@3.12` and a locally
    built formula long before it ever reaches the remote tap. Arming only on
    a real `brew tap` is what keeps those out -- an install with no tap
    ahead of it needs no trust.
    """
    tapped: str | None = None
    untrusted: list[str] = []
    for raw in script.splitlines():
        for segment in _CHAIN_SEPARATOR.split(raw):
            line = segment.strip()
            if _COMMENT.match(line):
                continue
            if _BREW_TAP_TRUST.search(line):
                tapped = None
            elif _BREW_TAP.search(line):
                tapped = line
            elif tapped and _BREW_INSTALL.search(line):
                untrusted.append(tapped)
                tapped = None
    return untrusted


def _scanned_files() -> list[Path]:
    """Every shipped text file the untrusted-tap scan reads."""
    files = []
    for path in sorted(_REPO_ROOT.rglob("*")):
        if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(_REPO_ROOT)
        # `venv` in the name rather than an exact match: a developer's
        # virtualenv is whatever they named it, and site-packages holds
        # thousands of files none of which are ours.
        if any(part in _SKIPPED_DIRECTORIES or "venv" in part for part in relative.parts):
            continue
        if relative in _SCAN_ALLOWLIST:
            continue
        files.append(relative)
    return files


def test_no_shipped_file_taps_without_trusting_before_it_installs():
    """The whole tree, not a hand-kept list of the files someone remembered.

    Non-interactive shell cannot answer a trust prompt -- it just stops --
    and a human reading a copy-paste block has no way to know a step is
    missing. Either way the sequence dead-ends, whether it ships as Markdown,
    as a generated string, or as executable shell.
    """
    offenders = {
        str(relative): taps
        for relative in _scanned_files()
        if (taps := _untrusted_taps((_REPO_ROOT / relative).read_text(encoding="utf-8")))
    }

    assert not offenders, "tap added without trusting before a brew install:\n" + "\n".join(
        f"  {path}: {taps}" for path, taps in offenders.items()
    )


def test_the_scan_reaches_the_files_that_carry_install_sequences():
    """A scan that quietly stopped reading a file class would pass forever."""
    scanned = set(_scanned_files())

    for expected in (
        Path("README.md"),
        Path("docs") / "homebrew.md",
        Path("scripts") / "cloud" / "ec2-user-data-macos.sh.tmpl",
        Path(".github") / "workflows" / "macos-brew-smoke.yml",
        Path(".github") / "workflows" / "release-publish-pypi.yml",
        Path("scripts") / "agents" / "lib" / "sprint_calc.py",
        Path("src") / "nyxgpt" / "release_candidate.py",
        Path("web") / "tests" / "mocks" / "handlers.ts",
        Path("web") / "tests" / "app" / "admin" / "portability.test.tsx",
    ):
        assert expected in scanned, f"{expected} is no longer scanned"

    # The file classes themselves, so narrowing the sweep is a visible edit.
    assert {
        ".md",
        ".py",
        ".sh",
        ".tmpl",
        ".yml",
        ".yaml",
        ".ts",
        ".tsx",
    } == _SCANNED_SUFFIXES


def test_every_skipped_file_states_why_it_is_skipped():
    """A skip is an argument, not a convenience."""
    for path, reason in _SCAN_ALLOWLIST.items():
        assert (_REPO_ROOT / path).exists(), f"stale allowlist entry: {path}"
        assert len(reason) > 20, f"allowlisted {path} with no stated reason"


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "brew tap dkblinux98/nyxgpt\nbrew install nyxgpt-api\n",
            id="separate-lines",
        ),
        pytest.param(
            "brew tap dkblinux98/nyxgpt && brew install nyxgpt-api@3.0.0rc\n",
            id="same-line-chain",
        ),
        pytest.param(
            'lines.append(f"brew tap dkblinux98/nyxgpt && brew install "\n'
            '             f"nyxgpt-api@{release}rc")\n',
            id="chain-split-across-string-literals",
        ),
        pytest.param(
            'run: "brew tap dkblinux98/nyxgpt\\nbrew install nyxgpt-api"\n',
            id="escaped-newline-inside-a-literal",
        ),
        pytest.param(
            'echo "brew tap dkblinux98/nyxgpt"\necho "brew install nyxgpt-api"\n',
            id="echoed-into-a-summary",
        ),
    ],
)
def test_the_untrusted_tap_scan_would_catch_a_regression(script: str):
    """The scan only means something if it can fail -- in every shape.

    Each case here is a shape that actually shipped untrusted at some point
    during #3752 and got past an earlier version of this scan.
    """
    assert _untrusted_taps(script)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "brew tap dkblinux98/nyxgpt\n"
            "brew tap-trust dkblinux98/nyxgpt\n"
            "brew install nyxgpt-api\n",
            id="trusted-on-its-own-line",
        ),
        pytest.param(
            "brew tap dkblinux98/nyxgpt && (brew tap-trust dkblinux98/nyxgpt || true) "
            "&& brew install nyxgpt-api@3.0.0rc\n",
            id="trusted-tolerantly-in-a-chain",
        ),
        pytest.param(
            "brew install python@3.12\nbrew tap-new nyxgpt/brew-smoke --no-git\n"
            "brew install nyxgpt/brew-smoke/nyxgpt-api@3.0.0rc\n",
            id="a-local-tap-new-needs-no-trust",
        ),
        pytest.param(
            "# `brew tap dkblinux98/nyxgpt` is where this starts, and without\n"
            "# the trust step `brew install` stops at the prompt.\n",
            id="prose-in-a-comment-is-not-a-sequence",
        ),
        pytest.param(
            "The remote Homebrew tap and the PyPI wheel are the only sources;\n"
            "`brew install nyxgpt-api` resolves to the latest stable release.\n",
            id="the-word-homebrew-is-not-a-tap-command",
        ),
    ],
)
def test_the_untrusted_tap_scan_does_not_cry_wolf(script: str):
    """A scan that flags prose gets silenced, and then it protects nothing."""
    assert not _untrusted_taps(script)


def test_the_portability_matrix_row_carries_the_trust_step():
    """`nyxgpt ops portability` and the admin panel render these commands."""
    macos = next(t for t in portability.TARGETS if t.key == "macos-native")

    assert macos.install == (
        f"brew tap {_TAP}",
        f"brew tap-trust {_TAP}",
        "brew install nyxgpt-api nyxgpt-web",
    )
    # The row still has to satisfy the matrix's own mechanical invariants --
    # `brew tap-trust` is a package-manager command, not a raw orchestrator one.
    assert portability.check_target(macos)["invariants_passed"]
