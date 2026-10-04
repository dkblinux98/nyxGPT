"""The api formulas' interpreter preflight claims only what an import proves.

#4121's acceptance criterion was a choice: the preflight at the top of the
`install` block "either evaluates trust for real or stops claiming to; a guard
pins whichever is chosen". It stops claiming to, and this is that guard.

Why that is the only honest branch. The preflight's entries are all `import`
checks -- each says a module loads, never that the thing the module is used
for works. On the host that failed this install twice, `ssl` and `ctypes` both
imported perfectly and the build still died: what could not be *performed* was
Security.framework's trust evaluation inside Homebrew's `sandbox-exec` build
sandbox (`OSStatus -26276`, an `errSecInternal`). An import cannot reach that.
A preflight that evaluated Apple trust for real would also fail by design on
every macOS install, because it runs inside that same sandbox -- and the
recipe no longer needs the capability at all, since it verifies with OpenSSL
and certifi (#4122, `--use-deprecated=legacy-certs`).

The pip calls themselves are guarded next door, structurally, in
`test_brew_pip_sandbox_trust.py`: no networked pip call may reach truststore
and no source-tree install may spawn an isolated build. This module does not
restate that -- it pins only the claim the preflight makes about itself, which
is what reads as coverage of the fault when it is wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Both copies of the recipe. The template is what publishes to the remote tap
#: and the other is the `file://` tap `nyxgpt ops install` builds, so a change
#: in one of them covers half the install paths -- which is exactly what the
#: owner's acceptance comment asks these to pin ("the fix is landed in BOTH").
FORMULAS = ("homebrew/nyxgpt-api.rb", "homebrew/tap/nyxgpt-api.rb.tmpl")

#: The wording that shipped, and that read as coverage of the trust evaluation
#: nothing in the preflight can reach. Kept as the negative control's input.
RETIRED_CLAIM = "truststore loads Security.framework through it"


def _formula(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def assert_preflight_claims_only_imports(formula: str, label: str) -> None:
    """Fail unless the preflight stops claiming to cover Apple trust."""
    # The entry itself, not the whole file: the comment above it quotes the
    # retired wording on purpose, so that the next reader knows what changed.
    ctypes_entry = re.search(r'^\s*\("ctypes", "(?P<why>[^"]*)"\),', formula, re.MULTILINE)
    assert ctypes_entry, f"{label}: the preflight no longer checks ctypes at all"
    why = ctypes_entry.group("why")
    assert "truststore" not in why and "Security.framework" not in why, (
        f"{label}: the preflight's ctypes line claims coverage of exactly the fault that "
        f"reached owner acceptance twice ({why!r}). An import of ctypes succeeded on that "
        "machine; what failed was a trust evaluation no import can reach."
    )
    # And it says so, rather than leaving the next reader to re-derive it.
    assert "Every entry is an IMPORT check" in formula, (
        f"{label}: nothing in the recipe says the preflight is import-only, which is how "
        "the retired wording came to be read as coverage in the first place."
    )
    assert "sandbox" in formula


@pytest.mark.parametrize("path", FORMULAS)
def test_the_preflight_no_longer_claims_to_cover_apple_trust_evaluation(path):
    assert_preflight_claims_only_imports(_formula(path), path)


@pytest.mark.parametrize("path", FORMULAS)
def test_the_guard_rejects_the_wording_that_shipped(path):
    """The claim this replaces, so the check is known to be able to fail.

    #3753's lesson: a guard that has only ever run against a good input is
    indistinguishable from no guard.
    """
    regressed = re.sub(
        r'^(\s*)\("ctypes", "[^"]*"\),',
        rf'\1("ctypes", "{RETIRED_CLAIM}"),',
        _formula(path),
        count=1,
        flags=re.MULTILINE,
    )
    assert RETIRED_CLAIM in regressed, "the mutation did not apply; the entry has moved"
    with pytest.raises(AssertionError, match="claims coverage"):
        assert_preflight_claims_only_imports(regressed, f"{path} (pre-fix wording)")
