"""The api formulas keep every networked pip call off macOS's trust store (#4121).

Two acceptance rounds on fresh `mac*.metal` hardware died inside `brew install`
with `SSLError(SSLCertVerificationError('OSStatus -26276'))` -- an
`errSecInternal`, a trust evaluation that could not be *performed*, because
Homebrew builds under `sandbox-exec` with `(deny mach-lookup)` and
Security.framework's evaluation needs a mach service that is not on the
allowlist. The recipe's answer is to verify with OpenSSL + certifi instead
(`--use-deprecated=legacy-certs`) and to leave no pip process out of that
choice -- including the one `pip install <source tree>` spawns for build
isolation, which inherits no command line and which is why patching the two
obvious call sites moved the owner's failure from `/simple/pip/` to
`/simple/setuptools/` rather than fixing it.

These are *guards*, not a substitute for running it. The executed half is
`macos-brew-smoke.yml`'s `keg-install` job, which injects the truststore
failure and proves the pre-fix bootstrap dies on it and this one does not. What
no job can produce is a `mac*.metal` instance, which is on the short D-006
exception list in docs/live-verification-ci.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Both copies of the recipe. The template is what publishes to the remote tap
#: and the other is the `file://` tap `nyxgpt ops install` builds, so a fix in
#: one of them is a fix for half the install paths -- which is exactly what the
#: owner's acceptance comment asks these to pin ("the fix is landed in BOTH").
FORMULAS = ("homebrew/nyxgpt-api.rb", "homebrew/tap/nyxgpt-api.rb.tmpl")

#: The flag that takes pip off the Apple trust path, and the Ruby local the
#: recipe splats it in from. The local rather than the literal, because the
#: recipe only passes the flag to a pip that accepts it (it did not exist
#: before pip 24.2, where truststore is not the default backend either).
LEGACY_CERTS_FLAG = "--use-deprecated=legacy-certs"
LEGACY_CERTS_SPLAT = "*legacy_certs"


def _formula(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _statements(formula_text: str) -> list[str]:
    """The formula's Ruby statements, with comments dropped and wraps rejoined.

    The `system` calls here break across lines on a trailing comma, so a flag
    sitting on the second line of a two-line call reads as absent to a
    line-at-a-time scan -- the shape that would make this whole module pass
    vacuously.
    """
    statements: list[str] = []
    current = ""
    for raw in formula_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}" if current else line
        if current.endswith(",") or current.endswith("\\"):
            continue
        statements.append(current)
        current = ""
    if current:
        statements.append(current)
    return statements


#: What makes a statement a pip *invocation* -- `python -m pip`, the venv's own
#: `bin/pip`, or pip run out of a wheel by zipimport. Deliberately not a bare
#: `pip` substring: `system python, "-m", "venv", "--without-pip", venv` is not
#: a pip call and demanding TLS flags of it would be nonsense.
_RUNS_PIP = re.compile(r'"-m", "pip"|bin/pip"|/pip", "install"')


def _pip_calls(formula_text: str) -> list[str]:
    """Every statement that runs pip."""
    return [
        statement
        for statement in _statements(formula_text)
        if re.match(r"(quiet_)?system\b", statement) and _RUNS_PIP.search(statement)
    ]


def _networked(pip_call: str) -> bool:
    """True unless the call is pinned to the local filesystem.

    `--no-index` is the whole test: the zipimport bootstrap installs the wheel
    that was already downloaded, so it opens no socket and needs no CA bundle.
    """
    return "--no-index" not in pip_call


def assert_pip_trust_path(formula_text: str, label: str) -> None:
    """Fail unless `formula_text` keeps every pip call off the Apple trust path.

    Shared by the real formulas and by the deliberately-broken copies below,
    so the checks are known to be capable of failing (#3753's lesson: a guard
    that has only ever been run against a good input is indistinguishable from
    no guard).
    """
    pip_calls = _pip_calls(formula_text)
    assert pip_calls, f"{label}: no pip invocation found at all -- has the recipe moved?"

    # 1. Whether the flag is passed is decided by asking pip, once.
    assert (
        f'quiet_system python, "-m", "pip", "download", "{LEGACY_CERTS_FLAG}", "--help"'
        in formula_text
    ), (
        f"{label}: the recipe must ASK whether this pip accepts {LEGACY_CERTS_FLAG} before "
        "passing it. The flag is deprecated and absent before pip 24.2, where truststore is "
        "not the default backend either -- passing it blind turns a TLS fix into an "
        "unrecognized-option failure on an older interpreter."
    )
    assert f'legacy_certs = ["{LEGACY_CERTS_FLAG}"]' in formula_text, (
        f"{label}: nothing sets the legacy_certs local, so {LEGACY_CERTS_SPLAT} expands to "
        "nothing and every pip call below is back on the Apple trust path."
    )
    # 2. ...and any pip child the recipe does not spell out inherits it.
    assert 'ENV["PIP_USE_DEPRECATED"] = "legacy-certs"' in formula_text, (
        f"{label}: the recipe must also export PIP_USE_DEPRECATED, so a pip subprocess it "
        "does not name on a command line is covered too (#4121, call site 3)."
    )

    # 3. Every networked pip call carries it.
    for call in pip_calls:
        if not _networked(call):
            continue
        if call.startswith("quiet_system"):
            continue
        assert LEGACY_CERTS_SPLAT in call, (
            f"{label}: this pip call reaches the network through pip's default TLS backend, "
            f"which is truststore on pip 24.2+ and cannot evaluate trust inside Homebrew's "
            f"build sandbox:\n  {call}"
        )

    # 4. The call handed a source tree disables build isolation, and the
    #    backend it then needs is already in the venv.
    source_tree_installs = [
        call for call in pip_calls if '"install"' in call and "buildpath" in call
    ]
    assert source_tree_installs, f"{label}: nothing installs the vendored source tree"
    for call in source_tree_installs:
        assert "--no-build-isolation" in call, (
            f"{label}: `pip install <source tree>` spawns a SEPARATE pip to fetch the build "
            "backend, and that child inherits no command line -- so the flags on this call "
            "do not reach it. Measured on the owner's host: the failure moved from "
            f"`/simple/pip/` to `/simple/setuptools/`.\n  {call}"
        )
    seeds = [
        index
        for index, call in enumerate(pip_calls)
        if '"setuptools"' in call and '"wheel"' in call
    ]
    assert seeds, (
        f"{label}: --no-build-isolation needs the build backend already present in the venv; "
        "nothing seeds setuptools/wheel."
    )
    assert min(seeds) < min(pip_calls.index(call) for call in source_tree_installs), (
        f"{label}: the build backend is seeded AFTER the source tree is installed, so the "
        "isolation-free install has nothing to build with."
    )


@pytest.mark.parametrize("path", FORMULAS)
def test_no_networked_pip_call_uses_macos_trust_evaluation(path):
    assert_pip_trust_path(_formula(path), path)


@pytest.mark.parametrize("path", FORMULAS)
def test_the_recipe_declares_setuptools_build_meta_which_is_what_is_seeded(path):
    """`--no-build-isolation` is only correct if the venv has *this* backend."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'build-backend = "setuptools.build_meta"' in pyproject, (
        "the formulas seed setuptools/wheel because that is the declared backend; a different "
        "backend has to be seeded alongside them or --no-build-isolation cannot build the tree"
    )
    assert '"setuptools", "wheel"' in _formula(path)


# --- The guards are capable of failing -----------------------------------
#
# Each mutation below is one of the pre-fix recipes this issue was filed
# against, or one of the half-fixes that were tried on the live machine and
# observed not to work.


@pytest.mark.parametrize("path", FORMULAS)
def test_the_guard_rejects_the_recipe_that_shipped(path):
    """The rc17 recipe: no flag anywhere, which is what failed twice."""
    pre_fix = (
        _formula(path)
        .replace(f'legacy_certs = ["{LEGACY_CERTS_FLAG}"]', "legacy_certs = []")
        .replace(f"{LEGACY_CERTS_SPLAT}, ", "")
    )
    with pytest.raises(AssertionError):
        assert_pip_trust_path(pre_fix, f"{path} (pre-fix)")


@pytest.mark.parametrize("path", FORMULAS)
def test_the_guard_rejects_the_half_fix_that_left_build_isolation_alone(path):
    """Flags on both documented calls and nothing else -- the state in which
    the owner watched the failure move to `/simple/setuptools/`."""
    half_fixed = _formula(path).replace('"--no-build-isolation", ', "")
    with pytest.raises(AssertionError, match="spawns a SEPARATE pip"):
        assert_pip_trust_path(half_fixed, f"{path} (half-fixed)")


@pytest.mark.parametrize("path", FORMULAS)
def test_the_guard_rejects_no_build_isolation_without_the_backend(path):
    """`--no-build-isolation` with nothing to build with is a different
    failure, not a fix."""
    unseeded = _formula(path).replace('"setuptools", "wheel"', '"a-package-that-is-not-a-backend"')
    with pytest.raises(AssertionError, match="seed"):
        assert_pip_trust_path(unseeded, f"{path} (unseeded)")


# --- What the interpreter preflight claims -------------------------------


@pytest.mark.parametrize("path", FORMULAS)
def test_the_preflight_no_longer_claims_to_cover_apple_trust_evaluation(path):
    """The owner's criterion: the preflight either evaluates trust for real or
    stops claiming to.

    It stops claiming to, and that is the only honest option left: an import
    check cannot reach a trust evaluation, and a real evaluation would now fail
    by design on every macOS install, because the preflight runs inside the
    same `sandbox-exec` build sandbox that broke it. The recipe no longer needs
    the capability either -- it verifies with OpenSSL + certifi.
    """
    formula = _formula(path)
    # The entry itself, not the whole file: the comment above it quotes the
    # retired wording on purpose, so that the next reader knows what changed.
    ctypes_entry = re.search(r'^\s*\("ctypes", "(?P<why>[^"]*)"\),', formula, re.MULTILINE)
    assert ctypes_entry, f"{path}: the preflight no longer checks ctypes at all"
    why = ctypes_entry.group("why")
    assert "truststore" not in why and "Security.framework" not in why, (
        f"{path}: the preflight's ctypes line claims coverage of exactly the fault that "
        f"reached owner acceptance twice ({why!r}). An import of ctypes succeeded on that "
        "machine; what failed was a trust evaluation no import can reach."
    )
    # And it says so, rather than leaving the next reader to re-derive it.
    assert "Every entry is an IMPORT check" in formula
    assert "sandbox" in formula
