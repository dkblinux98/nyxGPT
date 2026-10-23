"""Guard test for #4221: `web/package.json`'s ranges must exclude the
advisories this project has already remediated.

WHY THIS EXISTS. `security-scan` is a required check, and six times now it
has gone red repo-wide with no relevant diff because an advisory was
published against a version `web/` already resolved to (2026-08-03 #3501,
2026-09-30, 2026-10-04 #4150, 2026-10-21 and 2026-10-23 #4192, and
2026-10-23 #4221 -- whose own fix was interrupted by a seventh,
`happy-dom` GHSA-834v-8h66-3h79, published between two `npm audit` runs
minutes apart). Every one of those was cleared by moving the LOCKFILE and
leaving `package.json` alone, which is the mechanism this test closes.

The defect in that habit is not that it fails -- it works, and the gate
goes green. It is that the remediation then exists only in
`package-lock.json`, while the declared range still admits the vulnerable
version forever:

  * `@sentry/nextjs` was remediated to 10.76.2 in the lock on 2026-10-23
    while `package.json` still said `^10.69.0` -- a range that accepts
    every version GHSA-mchm-gjx6-7h55 covers. Anything that re-resolves
    under an upper constraint may legitimately pick one back, and
    `npm audit fix --force` proposes exactly those moves (#4150 recorded
    it proposing semver-major downgrades).
  * `next` sat at `^16.3.8` while GHSA-2464-fhr4-w9xw covers
    `16.0.0 - 16.4.0`: the range's own floor was inside the advisory.
  * An EXACT pin is the sharper form of the same bug -- it cannot receive
    a lock-level fix at all. `web/audit-ci.jsonc` records moving `next`
    off an exact `16.3.0` for that reason ("patch-range, so the lock fix
    can reach it next time without a package.json edit"), and #4221 found
    `eslint-config-next` still exactly pinned at `16.3.0`, which is the
    path that carried the `zod` HIGH two days earlier. That one is NOT
    fixed here: unpinning it resolves 16.4.3, whose react-hooks 7.1.x
    promotes four rules to error and takes `npx eslint .` from 2 errors to
    29 across ~20 files, which the diff-scoped blocking lint gate would
    then charge to the next unrelated PR. It is recorded in
    `DELIBERATE_EXACT_PINS` below and filed as #4224, so the pin is an
    explicit accepted liability rather than an oversight.

So the rule, enforced below rather than only written down (#4174): when an
advisory is cleared, the `package.json` range moves too, and a dependency
that has carried a high/critical advisory is never exactly pinned.

This test deliberately does NOT call `npm audit`. The live advisory set is
the CI gate's job (`web/audit-ci.jsonc`, run by `security-scan`) and it
needs the network; this guard is about the manifest the gate resolves
from, so it stays offline, hermetic and fast. The two checks answer
different questions: audit-ci asks "is the tree vulnerable today?", this
asks "can the tree become vulnerable again tomorrow for a reason we have
already paid for?"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_PACKAGE_JSON = REPO_ROOT / "web" / "package.json"

# Advisories this project has remediated in `web/`, with the lowest version
# that is NOT covered by the advisory. The floor of the declared range must
# be at or above this.
#
# Read these off the advisory's affected range, not off whatever the lock
# happens to hold: GHSA-2464-fhr4-w9xw covers `16.0.0 - 16.4.0`, so the
# first safe `next` is 16.4.1 even though the lock is further ahead. Using
# the advisory bound rather than the pinned version is what keeps the entry
# from needing an edit every time a dependency moves forward.
REMEDIATED_FLOORS: dict[str, tuple[str, str]] = {
    # @sentry/nextjs: middleware bypass when using the tunnel route.
    # CRITICAL, affects >=10.0.0-alpha.0 <10.75.2. (#4221)
    "@sentry/nextjs": ("10.75.2", "GHSA-mchm-gjx6-7h55"),
    # next: unauthenticated RCE in the image optimization API for SVG.
    # CRITICAL, affects 16.0.0 - 16.4.0. (#4192)
    "next": ("16.4.1", "GHSA-2464-fhr4-w9xw"),
    # happy-dom: incomplete fix for CVE-2026-33943 -- unsanitized import
    # alias leads to host code execution. HIGH, affects
    # >=17.0.0 <=21.0.3. Needed a semver-major range move, 20 -> 21. (#4221)
    "happy-dom": ("21.0.4", "GHSA-834v-8h66-3h79"),
}

# Dependencies deliberately held at an exact version, with the reason.
#
# An exact pin cannot receive a lockfile-level advisory fix, so each one is
# a standing liability accepted on purpose. If any of these ever appears in
# a high/critical advisory, the fix is to move it to a range in the same
# change -- not to add it to `REMEDIATED_FLOORS` and leave the pin.
DELIBERATE_EXACT_PINS: dict[str, str] = {
    "react": "held in lockstep with react-dom and @types/react; React upgrades are their own change",
    "react-dom": "held in lockstep with react; see above",
    "audit-ci": "the security gate's own tool -- pinned so the gate's behaviour does not drift under it",
    # Measured during #4221, not assumed: unpinning resolves 16.4.3, whose
    # eslint-plugin-react-hooks 7.1.1 promotes four rules
    # (set-state-in-effect, refs, purity, preserve-manual-memoization) and
    # takes `npx eslint .` from 2 errors to 29 across ~20 files. The web
    # eslint gate is diff-scoped and blocking (ci-tests.yml), so landing
    # that debt would block the next PR to touch any of those files for a
    # reason unrelated to its own change. Carrying the pin for now is the
    # smaller harm; retiring it is its own change.
    "eslint-config-next": "unpinning imports 27 new react-hooks errors across ~20 files -- see #4224",
}

# Specifier prefixes that let a lockfile fix reach the dependency without a
# `package.json` edit. `*`/`latest`/`x` are ranges too, but they are not
# used in this tree and would be their own problem, so they are not listed.
_RANGE_PREFIXES = ("^", "~", ">=", ">")


def _load_manifest() -> dict:
    return json.loads(WEB_PACKAGE_JSON.read_text())


def _declared_specifiers() -> dict[str, str]:
    """Every direct dependency of `web/`, runtime and dev together.

    Both halves matter: the runtime ones ship, and the dev ones are what
    `security-scan` has actually gone red on most often (`happy-dom`,
    `eslint-config-next`, `vitest`). audit-ci does not distinguish them,
    so neither does this.
    """
    manifest = _load_manifest()
    return {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}


def _version_tuple(version: str) -> tuple[int, ...]:
    """`"16.4.1"` -> `(16, 4, 1)`, dropping any prerelease/build suffix.

    Truncating the suffix is the safe direction here. `10.75.2-beta.1`
    compares equal to `10.75.2` and so passes a `>= 10.75.2` check it
    should arguably fail, but nothing in this tree pins a prerelease, and
    the inverse error -- a `ValueError` turning a security guard into a
    red build for an unrelated reason -- is worse.
    """
    numbers = []
    for part in version.split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers)


def _floor_of(specifier: str) -> str:
    """The lowest version a specifier accepts, as a bare version string.

    `^10.76.2` -> `10.76.2`; `16.3.0` -> `16.3.0`. For `^`/`~` the floor
    IS the written version, which is the whole point: `^10.69.0` accepts
    10.69.0, and 10.69.0 is vulnerable.
    """
    specifier = specifier.strip()
    for prefix in (">=", ">", "^", "~", "="):
        if specifier.startswith(prefix):
            return specifier[len(prefix) :].strip()
    return specifier


def test_the_remediated_floor_registry_names_real_dependencies() -> None:
    """A rename or removal must not silently void the guard.

    Without this, dropping `happy-dom` from the manifest would make its
    entry below vacuous rather than failing, and the next person to add
    the package back would get no protection from it.
    """
    declared = _declared_specifiers()
    missing = sorted(name for name in REMEDIATED_FLOORS if name not in declared)
    assert not missing, (
        f"tests/unit/test_npm_dependency_floors.py pins floors for {missing}, "
        "which web/package.json no longer declares. If the dependency is gone "
        "for good, remove its entry here and say so; if it was renamed, update "
        "the entry. Do not leave a floor guarding nothing."
    )


@pytest.mark.parametrize("package", sorted(REMEDIATED_FLOORS))
def test_the_declared_range_excludes_the_remediated_advisory(package: str) -> None:
    """The floor of the range is at or above the first unaffected version.

    This is the check that would have failed on the release branch before
    #4221: `@sentry/nextjs` declared `^10.69.0` against a CRITICAL
    covering everything below 10.75.2, with only the lockfile holding the
    line.
    """
    fixed_version, advisory = REMEDIATED_FLOORS[package]
    specifier = _declared_specifiers()[package]
    floor = _floor_of(specifier)
    assert _version_tuple(floor) >= _version_tuple(fixed_version), (
        f"web/package.json declares {package} as {specifier!r}, whose lowest "
        f"accepted version ({floor}) is inside {advisory}. Raise the range to "
        f"at least {fixed_version} -- moving the lockfile alone leaves the "
        "manifest accepting the vulnerable version, which is the #4221 defect."
    )


def test_no_dependency_is_exactly_pinned_without_a_recorded_reason() -> None:
    """An exact pin cannot receive a lockfile-level advisory fix.

    `next` and `eslint-config-next` were both exactly pinned at `16.3.0`
    and both turned `security-scan` red; the remedy each time was to move
    to a range. The three accepted pins carry their reason in
    `DELIBERATE_EXACT_PINS` so a fourth cannot arrive unexplained.
    """
    offenders = {
        name: specifier
        for name, specifier in _declared_specifiers().items()
        if not specifier.startswith(_RANGE_PREFIXES) and name not in DELIBERATE_EXACT_PINS
    }
    assert not offenders, (
        f"web/package.json pins {offenders} exactly. An exact pin cannot "
        "receive a lockfile advisory fix, so `security-scan` goes red and "
        "stays red until someone edits the manifest by hand (#4192, #4221). "
        "Use a `^`/`~` range, or record the reason in DELIBERATE_EXACT_PINS."
    )


def test_every_recorded_exact_pin_is_still_exactly_pinned() -> None:
    """The exception list does not outlive the exceptions.

    If `react` moves to a range, its entry here should go with it --
    otherwise the list slowly becomes a set of pre-approvals for pins
    nobody re-read.
    """
    declared = _declared_specifiers()
    stale = sorted(
        name
        for name, specifier in declared.items()
        if name in DELIBERATE_EXACT_PINS and specifier.startswith(_RANGE_PREFIXES)
    )
    assert not stale, (
        f"DELIBERATE_EXACT_PINS still excuses {stale}, which web/package.json "
        "now declares as a range. Remove the entry."
    )


def test_no_recorded_exact_pin_also_carries_a_remediated_floor() -> None:
    """The two registries must not disagree about one package.

    A package that has carried a high/critical advisory needs a range, so
    appearing in both lists means the pin won over the remediation -- the
    exact state this guard exists to prevent.
    """
    both = sorted(set(DELIBERATE_EXACT_PINS) & set(REMEDIATED_FLOORS))
    assert not both, (
        f"{both} is both exactly pinned and recorded as advisory-remediated. "
        "A dependency that has carried a high/critical advisory must stay on a "
        "range so the next lockfile fix can reach it."
    )


@pytest.mark.parametrize(
    ("specifier", "expected_floor"),
    [
        ("^10.69.0", "10.69.0"),
        ("~16.3.8", "16.3.8"),
        (">=21.0.4", "21.0.4"),
        ("16.3.0", "16.3.0"),
        ("  ^20.0.11  ", "20.0.11"),
    ],
)
def test_the_floor_parser_reads_the_specifier_forms_this_tree_uses(
    specifier: str, expected_floor: str
) -> None:
    """`_floor_of` is the whole guard -- it gets its own coverage.

    In particular `^10.69.0` must read as 10.69.0 and not as "whatever the
    caret resolves to today", because the caret's floor is precisely what
    makes the manifest accept a vulnerable version.
    """
    assert _floor_of(specifier) == expected_floor


def test_the_guard_rejects_the_specifier_it_was_written_for() -> None:
    """Positive control (#3753: a guard never seen to reject is not evidence).

    These are the three real pre-#4221 states of this manifest. Each must
    compare as below its advisory's fixed version, or the comparison above
    is decorative.
    """
    assert _version_tuple(_floor_of("^10.69.0")) < _version_tuple("10.75.2")
    assert _version_tuple(_floor_of("^16.3.8")) < _version_tuple("16.4.1")
    assert _version_tuple(_floor_of("^20.0.11")) < _version_tuple("21.0.4")


def test_the_guard_accepts_the_current_manifest_floors() -> None:
    """...and the inverse: the post-fix values must compare as safe.

    A guard that rejects everything would also "pass" the test above.
    """
    assert _version_tuple(_floor_of("^10.76.2")) >= _version_tuple("10.75.2")
    assert _version_tuple(_floor_of("^16.4.3")) >= _version_tuple("16.4.1")
    assert _version_tuple(_floor_of("^21.0.4")) >= _version_tuple("21.0.4")
