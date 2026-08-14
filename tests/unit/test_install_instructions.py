"""The install paths a fresh user actually follows (#3752).

Acceptance testing failed on a stock Homebrew-Python Mac: the README led
with a pip install, which Homebrew's PEP 668 externally-managed Python
refuses outright, and the tap sequence stopped at Homebrew's third-party
trust gate because no document mentioned it. Most of that is prose, so these
tests read the shipped Markdown -- prose is the deliverable there, and it is
what regressed.

The same sequence also ships as *generated* text (`nyxgpt release publish`'s
`commands.brew`, rendered verbatim by the admin portability panel -- the
surface the owner installs rc builds from) and as *executable* shell (the
EC2 Mac user-data template, the published-tap CI job). A Markdown-only sweep
cannot see either, so they are pinned here too: the gate does not care which
kind of file the sequence came from.
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

#: Shipped shell that taps and installs on a machine with no operator at the
#: keyboard. Each is a real first-install sequence, so each has to trust.
_SHELL_INSTALL_SEQUENCES = (
    Path("scripts") / "cloud" / "ec2-user-data-macos.sh.tmpl",
    Path(".github") / "workflows" / "macos-brew-smoke.yml",
)

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


def _untrusted_taps(script: str) -> list[str]:
    """Tap commands that reach a nyxGPT install without trusting in between.

    Line-wise rather than whole-file, because these files hold several
    sequences: the smoke workflow installs `python@3.12` and a locally
    built formula long before it ever reaches the remote tap.
    """
    tapped: str | None = None
    untrusted: list[str] = []
    for raw in script.splitlines():
        line = raw.strip()
        if "brew tap-trust" in line:
            tapped = None
        elif re.search(r"brew tap [\"'$\w]", line):  # `tap-new` has no space
            tapped = line
        elif tapped and "brew install" in line and "nyxgpt-" in line:
            untrusted.append(tapped)
            tapped = None
    return untrusted


@pytest.mark.parametrize(
    "relative_path",
    _SHELL_INSTALL_SEQUENCES,
    ids=lambda p: p.name,  # type: ignore[misc]
)
def test_every_shipped_shell_install_sequence_trusts_the_tap(relative_path: Path):
    """Non-interactive shell cannot answer a trust prompt -- it just stops."""
    script = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert not _untrusted_taps(
        script
    ), f"tap added without trusting before a nyxGPT install in {relative_path}"


def test_the_untrusted_tap_scan_would_catch_a_regression():
    """The scan above only means something if it can fail."""
    assert _untrusted_taps("brew tap dkblinux98/nyxgpt\nbrew install nyxgpt-api\n")
    assert not _untrusted_taps(
        "brew tap dkblinux98/nyxgpt\nbrew tap-trust dkblinux98/nyxgpt\nbrew install nyxgpt-api\n"
    )


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
