"""The documented install paths a fresh user actually follows (#3752).

Acceptance testing failed on a stock Homebrew-Python Mac: the README led
with a pip install, which Homebrew's PEP 668 externally-managed Python
refuses outright, and the tap sequence stopped at Homebrew's third-party
trust gate because no document mentioned it. Both failures are in prose, so
these tests read the shipped Markdown rather than any code path -- prose is
the deliverable here, and it is what regressed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nyxgpt import portability

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_HOMEBREW_DOC = _REPO_ROOT / "docs" / "homebrew.md"

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
