"""No pip call in a Homebrew formula may verify through Apple's trust store.

#4122, and why this is a guard rather than three corrected strings.

Homebrew runs a formula's `install` block under `sandbox-exec` with
`(deny mach-lookup)` plus an allowlist. Security.framework's trust evaluation
needs a mach service that is not on that list, so the evaluation cannot be
*performed* -- which is why the failure is `errSecInternal`:

    SSLError(SSLCertVerificationError('OSStatus -26276'))
    ERROR: No matching distribution found for pip

and not a named certificate rejection such as -67843/-67818. It is also why
`curl` fetching the formula's own tarball succeeds -- that download runs
outside the sandbox -- while pip fails seconds later inside it.

The condition is **deterministic**, on every Mac, at any time after boot.
Measured on the reporting host in the same minute, seven hours after boot:
a login shell downloaded pip, the sandboxed build did not, and the same shell
with `--use-deprecated=legacy-certs` downloaded it again. The variable is the
sandbox, not the clock, so "retry it" is not a workaround and an earlier
diagnosis of a transient post-boot window was reading a changed sandbox as
elapsed time.

Two rules, because the first one alone is not a fix. With both documented pip
calls flagged, the failure MOVED rather than cleared -- `Could not fetch URL
https://pypi.org/simple/pip/` became `.../simple/setuptools/`. `pip install
<source tree>` triggers build isolation, which spawns a **separate** pip
process to fetch the build backend, and that child inherits no
`--use-deprecated` from its parent. No flag on the parent can reach it. So:

  1. a pip call that is not provably offline (`--no-index`) carries
     `--use-deprecated=legacy-certs`, which routes pip through OpenSSL and
     certifi instead of Security.framework;
  2. a pip call handed a source tree carries `--no-build-isolation`, and the
     build backend is seeded into the venv beforehand so there is nothing for
     the isolated build to fetch.

That second rule is also why a vendored wheelhouse would not have produced an
offline install (owner rejected it 2026-10-03; recorded so it is not revived):
build isolation still fetches backends for anything without a matching wheel.

Asserted structurally, against the parsed `system` statements, rather than by
pinning today's command strings. The guard this replaces -- a
`contains ... "install nyxgpt-api nyxgpt-web"` assertion in
`scripts/cloud-target-os-smoke.sh` -- is the cautionary case: it certified the
defect it was written beside and passed every run. A string pin here would go
green on a fourth pip call added without either flag, which is exactly the
regression that matters.

`--use-deprecated=legacy-certs` is deprecated deliberately. The upstream
condition that retires it, and the only one, is pip's truststore path working
inside Homebrew's build sandbox: Security.framework trust evaluation no longer
needing a service the sandbox denies, or Homebrew's profile allowing it. pip
offers no non-deprecated way to decline truststore today, so until then this
guard requires the deprecated spelling on purpose.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_homebrew_artifacts.py"

_spec = importlib.util.spec_from_file_location("build_homebrew_artifacts", SCRIPT)
assert _spec is not None and _spec.loader is not None
build_homebrew_artifacts = importlib.util.module_from_spec(_spec)
# Registered before `exec_module`, as in `test_build_homebrew_artifacts.py`:
# the script is loaded by path rather than imported, and `@dataclass` resolves
# its own module out of `sys.modules` while the class body is processed.
sys.modules[_spec.name] = build_homebrew_artifacts
_spec.loader.exec_module(build_homebrew_artifacts)

LEGACY_CERTS = build_homebrew_artifacts.LEGACY_CERTS_FLAG
NO_BUILD_ISOLATION = build_homebrew_artifacts.NO_BUILD_ISOLATION_FLAG

# Every formula that ships, both copies of each. The tap template is listed
# first in spirit if not in sort order: it is what a clean machine installs,
# and #4122 was reported against a published tap, so a fix present only in the
# locally-generated formula would have left the reported failure in place.
FORMULAS = {
    "api-local": REPO_ROOT / "homebrew" / "nyxgpt-api.rb",
    "api-tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-api.rb.tmpl",
    "web-local": REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
    "web-tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
}

API_FORMULAS = {name: path for name, path in FORMULAS.items() if name.startswith("api-")}


def _calls(which: str) -> list:
    return build_homebrew_artifacts.pip_invocations(FORMULAS[which].read_text(encoding="utf-8"))


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_no_networked_pip_call_can_reach_truststore(which):
    """Rule 1, over every pip call the formula makes rather than named ones."""
    offenders = [
        call.statement
        for call in _calls(which)
        if not call.offline and LEGACY_CERTS not in call.literals
    ]
    assert offenders == [], (
        f"{FORMULAS[which]}: these pip calls reach the network without "
        f"{LEGACY_CERTS}, so Homebrew's build sandbox refuses them with "
        f"OSStatus -26276: {offenders}"
    )


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_a_source_tree_install_never_spawns_an_isolated_build(which):
    """Rule 2: the child pip no parent flag reaches is never started."""
    offenders = [
        call.statement
        for call in _calls(which)
        if call.installs_a_source_tree and NO_BUILD_ISOLATION not in call.literals
    ]
    assert offenders == [], (
        f"{FORMULAS[which]}: these calls hand pip a source tree without "
        f"{NO_BUILD_ISOLATION}, so build isolation fetches the backend in a "
        f"subprocess that inherits no --use-deprecated: {offenders}"
    )


@pytest.mark.parametrize("which", sorted(API_FORMULAS))
def test_the_build_backend_is_seeded_before_the_source_tree_install(which):
    """`--no-build-isolation` is only safe because the backend is already there.

    Dropping build isolation without seeding setuptools and wheel into the
    venv first trades one failure for another -- pip would find no backend at
    all. Order matters, so it is asserted.
    """
    calls = _calls(which)
    seeds = [
        call
        for call in calls
        if "install" in call.literals and {"setuptools", "wheel"} <= set(call.literals)
    ]
    assert seeds, f"{FORMULAS[which]}: nothing seeds the build backend into the keg venv"

    tree_installs = [call for call in calls if call.installs_a_source_tree]
    assert tree_installs, f"{FORMULAS[which]}: no pip call installs the vendored source tree"
    assert max(call.line for call in seeds) < min(call.line for call in tree_installs)


@pytest.mark.parametrize("which", sorted(API_FORMULAS))
def test_the_deprecated_flag_names_what_would_retire_it(which):
    """CLAUDE.md: the reasoning for a guard lives with the guard.

    A deprecated flag with no stated exit condition is how a workaround
    becomes permanent. The formula has to say what upstream change removes
    it -- pip's truststore path working inside Homebrew's build sandbox --
    not merely that the flag is deprecated.
    """
    text = FORMULAS[which].read_text(encoding="utf-8")
    comments = "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("#")
    ).lower()

    assert "truststore" in comments
    assert "sandbox" in comments
    assert "deprecated" in comments
    # The status code is the searchable part of the operator's transcript.
    assert "-26276" in comments


def test_both_api_formulas_run_the_same_pip_calls():
    """One recipe in two files: a fix in one copy only is the #4122 shape.

    The reported failure was against the published tap, whose formula comes
    from the template. These compare the *arguments*, not the prose: the two
    files document themselves with different issue references on purpose.
    """
    local = [call.literals for call in _calls("api-local")]
    template = [call.literals for call in _calls("api-tap-template")]

    assert local == template


def test_the_validator_rejects_a_formula_whose_pip_predates_the_fix():
    """The negative control: a guard never seen to fail is not a guard.

    Reverts each half of the fix out of the real formula and asserts the
    publish-time validator refuses it -- so this file is known to fail on the
    recipe the owner's Mac actually ran, rather than merely to pass on the
    current one.
    """
    text = FORMULAS["api-tap-template"].read_text(encoding="utf-8")
    # Clean as it stands.
    build_homebrew_artifacts.validate_pip_sandbox_flags(text, "nyxgpt-api")

    pre_fix_tls = text.replace(f'"{LEGACY_CERTS}",\n', "").replace(f'"{LEGACY_CERTS}", ', "")
    with pytest.raises(ValueError, match="-26276"):
        build_homebrew_artifacts.validate_pip_sandbox_flags(pre_fix_tls, "nyxgpt-api")

    pre_fix_isolation = text.replace(f'"{NO_BUILD_ISOLATION}", ', "")
    with pytest.raises(ValueError, match="build-backend fetch"):
        build_homebrew_artifacts.validate_pip_sandbox_flags(pre_fix_isolation, "nyxgpt-api")


def test_a_pip_call_added_without_the_flags_is_caught():
    """The property, not the file: a fourth call has to fail too.

    This is what a string pin on the three known calls would miss, and the
    reason the guard parses statements instead.
    """
    text = FORMULAS["api-tap-template"].read_text(encoding="utf-8")
    sneaked = text.replace(
        "    pip_wheel = Dir.glob",
        '    system venv/"bin/pip", "install", "httpx"\n    pip_wheel = Dir.glob',
    )
    assert sneaked != text

    with pytest.raises(ValueError, match="-26276"):
        build_homebrew_artifacts.validate_pip_sandbox_flags(sneaked, "nyxgpt-api")


@pytest.mark.parametrize("which", ["web-local", "web-tap-template"])
def test_a_pip_call_added_to_the_WEB_formula_is_caught_too(which):
    """The guard's scope is the same for both formulas (#4192).

    `nyxgpt-web` runs no pip today, and the two rules above are vacuous on it
    for that reason -- which is exactly the state in which nobody notices that
    the guard has stopped looking. #4192 was filed partly on the belief that a
    pip call on the web install path was reaching Apple's trust store, and
    answering that needed this to be a measurement rather than a reading.

    `pip_invocations` ended its scan at `(bin/"nyxgpt-api").write`, a name the
    web formula does not contain, so on it the scan ran to end of file. That
    errs safe, but it meant one rule with two scopes (ledger D-066). Both
    halves are asserted here: a pip call in the web formula's `install` block
    is caught, and one in its service wrapper's bash heredoc -- text, not an
    executed command -- is not.
    """
    text = FORMULAS[which].read_text(encoding="utf-8")
    # Clean as it stands, which is also the claim that the web formula runs no
    # pip at all.
    build_homebrew_artifacts.validate_pip_sandbox_flags(text, "nyxgpt-web")
    assert build_homebrew_artifacts.pip_invocations(text) == []

    in_install = text.replace(
        '    system "npm", "ci"',
        '    system "npm", "ci"\n    system "pip3", "install", "something"',
        1,
    )
    assert in_install != text
    with pytest.raises(ValueError, match="-26276"):
        build_homebrew_artifacts.validate_pip_sandbox_flags(in_install, "nyxgpt-web")

    # And the exclusion the scan boundary exists for: the wrapper heredoc is
    # bash source written to a file, so a `pip` word in it runs nothing at
    # install time and must not be reported.
    in_wrapper = text.replace(
        '      cd "#{libexec}"',
        '      cd "#{libexec}"\n      # system "pip", "install", "x"',
        1,
    )
    assert in_wrapper != text
    build_homebrew_artifacts.validate_pip_sandbox_flags(in_wrapper, "nyxgpt-web")


@pytest.mark.parametrize(
    "spelling",
    [
        '    system "pip3", "install", "httpx"\n',
        '    system venv/"bin/pip3", "install", "httpx"\n',
        '    system venv/"bin/pip3.12", "install", "httpx"\n',
        '    system python3, "-m", "pip", "install", "httpx"\n',
    ],
)
def test_every_spelling_of_pip_is_measured(spelling):
    """A versioned pip is still pip (#4192).

    The detector matched `pip` and `*/pip` only, so `pip3` / `pip3.12` -- the
    names `ensurepip` leaves on PATH, and the ones a person writes first --
    were invisible to it. A guard that only sees the project's current
    convention certifies the next call that does not follow it.
    """
    text = FORMULAS["api-tap-template"].read_text(encoding="utf-8")
    sneaked = text.replace("    pip_wheel = Dir.glob", spelling + "    pip_wheel = Dir.glob")
    assert sneaked != text
    with pytest.raises(ValueError, match="-26276"):
        build_homebrew_artifacts.validate_pip_sandbox_flags(sneaked, "nyxgpt-api")
