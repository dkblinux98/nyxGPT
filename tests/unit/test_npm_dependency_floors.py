"""Guard test for #4221: no npm manifest or lockfile in this tree may sit
inside an advisory this project has already remediated.

WHY THIS EXISTS. `security-scan` is a required check, and seven times now it
has gone red repo-wide with no relevant diff because an advisory was
published against a version this tree already resolved to (2026-08-03
#3501, 2026-09-30, 2026-10-04 #4150, 2026-10-21 and 2026-10-23 #4192, and
2026-10-23 #4221 -- whose own fix was interrupted by a seventh, `happy-dom`
GHSA-834v-8h66-3h79, published between two `npm audit` runs minutes apart).
Every one of those was cleared by moving the LOCKFILE and leaving
`package.json` alone, which is the mechanism this test closes.

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

WHAT THE FIRST VERSION OF THIS GUARD MISSED, and why it is now two layers
and two manifests. Review of #4226 found ten of #4221's fifteen Dependabot
alerts still live on the branch that was fixing them, and the reason is
instructive: this guard read `web/package.json`'s `dependencies` and
`devDependencies` only.

  * It could not see `overrides`. An override IS a declared floor -- it is
    the stronger form, since it overrules what parents ask for -- and two
    of them were sitting inside live advisories: `web`'s
    `serialize-javascript` at `^7.0.5` (GHSA-m5mm-p8h7-m29v, fixed in
    7.1.3) and the root manifest's `fast-uri` at `^3.1.8`
    (GHSA-w74x-v6f9-j3xg and three siblings, fixed in 3.1.9). Both were
    themselves earlier remediations of this same class, left behind at the
    floor they were raised to at the time.
  * It could not see the ROOT manifest at all, which has its own lockfile
    and its own advisories (#4223 covers the fact that no CI gate audits
    it -- this guard is the only thing in CI that reads it today).
  * It only checked DECLARED floors, so a transitive package could regress
    freely. `@vitest/mocker` is the case that matters: `vitest` depends on
    it by exact version, so the only floor that reaches it is `vitest`'s
    own range, and `^4.0.16` admitted the vulnerable 4.1.11
    (GHSA-qxg6-89mg-rqv8, GHSA-xp26-3454-mjgp). Raising `vitest` to
    `^4.1.12` is what fixes it; an `overrides` entry on `@vitest/mocker`
    would also have worked and was rejected deliberately, because a
    floor of `^4.1.12` forced into a future vitest 5 tree is the next
    trap rather than a fix.

So the rule, enforced below rather than only written down (#4174), is now:
when an advisory is cleared, every manifest range that admits it moves too
-- `dependencies`, `devDependencies` AND `overrides`, in `web/` and at the
root -- the lockfiles must RESOLVE at or above the fixed version wherever
the package appears, and a dependency that has carried a high/critical
advisory is never exactly pinned.

The resolved-version layer is also the mechanically checkable form of
#4221's "all 15 Dependabot alerts are resolved" criterion. Dependabot
alerts track the DEFAULT branch, so after a merge to a release branch the
alert count cannot be observed until the release ceremony pushes master;
the resolved versions in the two lockfiles can be, on every run, offline.

This test deliberately does NOT call `npm audit`. The live advisory set is
the CI gate's job (`web/audit-ci.jsonc`, run by `security-scan`) and it
needs the network; this guard is about the manifests and locks the gate
resolves from, so it stays offline, hermetic and fast. The two checks
answer different questions: audit-ci asks "is the tree vulnerable today?",
this asks "can the tree become vulnerable again tomorrow for a reason we
have already paid for?"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The npm projects in this tree. Both have advisories, both have lockfiles,
# and before #4226 only the first was checked by anything here.
MANIFESTS: dict[str, Path] = {
    "web": REPO_ROOT / "web" / "package.json",
    "root": REPO_ROOT / "package.json",
}
LOCKFILES: dict[str, Path] = {
    "web": REPO_ROOT / "web" / "package-lock.json",
    "root": REPO_ROOT / "package-lock.json",
}


def _lock_name(scope: str) -> str:
    """`"web/package-lock.json"`, not `"package-lock.json"`.

    The two lockfiles share a basename, so a failure message naming only
    the basename sends the reader to the wrong file half the time.
    """
    return f"web/{LOCKFILES[scope].name}" if scope == "web" else LOCKFILES[scope].name


class Remediation(NamedTuple):
    """One advisory this project has cleared, and where the floor lives.

    `first_unaffected` is read off the ADVISORY's affected range, never off
    whatever the lock happens to hold: GHSA-2464-fhr4-w9xw covers
    `16.0.0 - 16.4.0`, so the first safe `next` is 16.4.1 even though the
    lock is further ahead. Using the advisory bound rather than the pinned
    version is what keeps an entry from needing an edit every time a
    dependency moves forward.

    `declared` says whether the manifest names the package. True means both
    layers apply -- the declared range must exclude the advisory AND the
    lock must resolve above it. False means the package is transitive and
    only the lock can be checked; `floor_holder` records what keeps it
    there, so the next reader does not have to re-derive it.
    """

    scope: str
    package: str
    first_unaffected: str
    advisory: str
    declared: bool
    floor_holder: str


# Advisories this project has remediated, in both npm projects. Add an entry
# whenever a high/critical is cleared -- that is what stops the clearing
# from being undone by a re-resolution later.
REMEDIATIONS: tuple[Remediation, ...] = (
    # --- web/, declared floors -------------------------------------------
    Remediation(
        "web",
        "@sentry/nextjs",
        "10.75.2",
        # Middleware bypass when using the tunnel route. CRITICAL,
        # >=10.0.0-alpha.0 <10.75.2. (#4221)
        "GHSA-mchm-gjx6-7h55",
        True,
        "dependencies",
    ),
    Remediation(
        "web",
        "next",
        "16.4.1",
        # Unauthenticated RCE in the image optimization API for SVG.
        # CRITICAL, 16.0.0 - 16.4.0. (#4192)
        "GHSA-2464-fhr4-w9xw",
        True,
        "dependencies",
    ),
    Remediation(
        "web",
        "undici",
        "6.29.1",
        # Proxy-Authorization header leak on cross-origin redirect. HIGH,
        # <6.29.1 (with GHSA-4qv8-2p9j-2685, moderate, alongside it).
        # Dependabot alerts 174/173. (#4221)
        "GHSA-xjj6-f33q-929c",
        True,
        "dependencies",
    ),
    Remediation(
        "web",
        "happy-dom",
        "21.0.4",
        # Incomplete fix for CVE-2026-33943 -- unsanitized import alias
        # leads to host code execution. HIGH, >=17.0.0 <=21.0.3. Needed a
        # semver-major range move, 20 -> 21. Dependabot alert 171. (#4221)
        "GHSA-834v-8h66-3h79",
        True,
        "devDependencies",
    ),
    Remediation(
        "web",
        "vitest",
        "4.1.12",
        # `vitest` itself is not the vulnerable package: it depends on
        # @vitest/mocker by EXACT version, so its own range is the only
        # floor that reaches it. `^4.0.16` admitted mocker 4.1.11, which
        # carries GHSA-qxg6-89mg-rqv8 and GHSA-xp26-3454-mjgp (Dependabot
        # alerts 157/156, both fixed in 4.1.12). @vitest/ui and
        # @vitest/coverage-v8 peer-depend on vitest by exact version too,
        # so all three move together. (#4221)
        "GHSA-qxg6-89mg-rqv8",
        True,
        "devDependencies (pins @vitest/mocker by exact version)",
    ),
    Remediation(
        "web",
        "serialize-javascript",
        "7.1.3",
        # Cross-site scripting via improper escaping. LOW, <7.1.3.
        # Reached only through @rollup/plugin-terser, which asks for
        # `^6.0.1`, so the web `overrides` entry is the whole floor --
        # it was raised to `^7.0.5` by #3644 for an earlier advisory and
        # left there. Dependabot alert 172. (#4221)
        "GHSA-m5mm-p8h7-m29v",
        True,
        "overrides",
    ),
    # --- web/, transitive: no manifest floor exists to check -------------
    Remediation(
        "web",
        "@vitest/mocker",
        "4.1.12",
        # The actually-vulnerable package behind the `vitest` entry above
        # (with GHSA-xp26-3454-mjgp alongside it); listed separately so the
        # resolved-version layer checks the package the alerts name, not
        # only its parent. A nested older copy under some other parent
        # would satisfy the `vitest` floor and still be reported.
        "GHSA-qxg6-89mg-rqv8",
        False,
        "vitest's own range is the only floor that reaches it",
    ),
    Remediation(
        "web",
        "fast-uri",
        "3.1.9",
        # Four advisories at once (GHSA-w74x-v6f9-j3xg, GHSA-xhq7-xmqj-769h,
        # GHSA-wjr9-j795-jxcr, GHSA-333x-w4r6-6crp), all fixed in 3.1.9.
        # Dependabot alerts 170/169/168/167. Every parent in web/ is an
        # `ajv` asking for `^3.0.1`, which admits the fix, so the lock is
        # the only place this can be recorded -- deliberately NOT given an
        # `overrides` entry, since the parents' own ranges will carry the
        # next fix as well. (#4221)
        "GHSA-w74x-v6f9-j3xg",
        False,
        "parents declare ^3.0.1, which admits the fix",
    ),
    # --- root, declared floors -------------------------------------------
    Remediation(
        "root",
        "fast-uri",
        "3.1.9",
        # Same four advisories as above, in the root project. Here an
        # `overrides` entry already existed at `^3.1.8` -- an earlier
        # remediation of this same class, left at the floor it was raised
        # to then, and so inside the new advisory the day it published.
        # Dependabot alerts 164/163/162/161. (#4221)
        "GHSA-w74x-v6f9-j3xg",
        True,
        "overrides",
    ),
    # --- root, transitive ------------------------------------------------
    Remediation(
        "root",
        "undici",
        "6.29.1",
        # Dependabot alert 166, HIGH. Reached through
        # @upstash/context7-mcp, which asks for `^6.26.0` and so admits
        # the fix; no override needed. (#4221)
        "GHSA-xjj6-f33q-929c",
        False,
        "parent declares ^6.26.0, which admits the fix",
    ),
)

# Dependencies deliberately held at an exact version, with the reason,
# per manifest.
#
# An exact pin cannot receive a lockfile-level advisory fix, so each one is
# a standing liability accepted on purpose. If any of these ever appears in
# a high/critical advisory, the fix is to move it to a range in the same
# change -- not to add it to `REMEDIATIONS` and leave the pin.
DELIBERATE_EXACT_PINS: dict[str, dict[str, str]] = {
    "web": {
        "react": "held in lockstep with react-dom and @types/react; React upgrades are their own change",
        "react-dom": "held in lockstep with react; see above",
        "audit-ci": "the security gate's own tool -- pinned so the gate's behaviour does not drift under it",
        # Measured during #4221, not assumed: unpinning resolves 16.4.3,
        # whose eslint-plugin-react-hooks 7.1.1 promotes four rules
        # (set-state-in-effect, refs, purity, preserve-manual-memoization)
        # and takes `npx eslint .` from 2 errors to 29 across ~20 files.
        # The web eslint gate is diff-scoped and blocking (ci-tests.yml),
        # so landing that debt would block the next PR to touch any of
        # those files for a reason unrelated to its own change. Carrying
        # the pin for now is the smaller harm; retiring it is its own
        # change.
        "eslint-config-next": "unpinning imports 27 new react-hooks errors across ~20 files -- see #4224",
    },
    "root": {},
}

# Specifier prefixes that let a lockfile fix reach the dependency without a
# `package.json` edit. `*`/`latest`/`x` are ranges too, but they are not
# used in this tree and would be their own problem, so they are not listed.
_RANGE_PREFIXES = ("^", "~", ">=", ">")


def _declared_specifiers(scope: str) -> dict[str, str]:
    """Every floor one manifest declares: deps, devDeps and overrides.

    All three matter. The runtime deps ship; the dev ones are what
    `security-scan` has actually gone red on most often (`happy-dom`,
    `eslint-config-next`, `vitest`); and the overrides are the floors that
    overrule what parents ask for, which is the only kind of floor some
    transitive packages have (`serialize-javascript` in `web/`, `fast-uri`
    at the root). audit-ci does not distinguish them, so neither does this.

    Where a package has a floor in more than one section, the strongest
    wins -- an override beats a declared range by definition, since npm
    applies it last.
    """
    manifest = json.loads(MANIFESTS[scope].read_text())
    specifiers: dict[str, str] = {
        **manifest.get("dependencies", {}),
        **manifest.get("devDependencies", {}),
    }
    specifiers.update(_flatten_overrides(manifest.get("overrides", {})))
    return specifiers


def _flatten_overrides(overrides: dict) -> dict[str, str]:
    """npm's `overrides` block, flattened to {package: specifier}.

    The nested form (`{"foo": {"bar": "1.0.0"}}`, optionally with a `"."`
    key giving foo's own override) is not used in this tree, but it is
    legal npm, and silently dropping it would make this guard quietly
    blind to exactly the kind of floor it was extended to read.
    """
    flat: dict[str, str] = {}
    for name, value in overrides.items():
        if isinstance(value, str):
            flat[name] = value
            continue
        if isinstance(value, dict):
            own = value.get(".")
            if isinstance(own, str):
                flat[name] = own
            flat.update(_flatten_overrides({k: v for k, v in value.items() if k != "."}))
    return flat


def _package_name_of(lock_path: str) -> str:
    """`"node_modules/a/node_modules/fast-uri"` -> `"fast-uri"`.

    One source for "which package is this lock entry", so the nested-copy
    walk below and its control test cannot drift apart. Scoped names keep
    their `@scope/` prefix, and a prefix match is deliberately NOT used:
    `node_modules/fast-uri-extras` is a different package.
    """
    return lock_path.rsplit("node_modules/", 1)[-1]


def _resolved_versions(scope: str, package: str) -> dict[str, str]:
    """Every copy of `package` in one lockfile, as {lock path: version}.

    Nested copies count. npm will happily place a second, older copy of a
    package under a parent whose range cannot reach the fixed version
    (`node_modules/x/node_modules/fast-uri`), and a guard that looked only
    at the hoisted copy would pass while the vulnerable one is still
    installed and still reported by `npm audit`.
    """
    lock = json.loads(LOCKFILES[scope].read_text())
    resolved: dict[str, str] = {}
    for path, entry in lock.get("packages", {}).items():
        if not path:
            continue
        if _package_name_of(path) != package:
            continue
        version = entry.get("version")
        if isinstance(version, str):
            resolved[path] = version
    return resolved


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


def _remediation_id(remediation: Remediation) -> str:
    return f"{remediation.scope}:{remediation.package}"


_DECLARED = [r for r in REMEDIATIONS if r.declared]


@pytest.mark.parametrize("scope", sorted(MANIFESTS))
def test_every_npm_project_in_the_tree_is_covered(scope: str) -> None:
    """Both manifests and both lockfiles exist and parse.

    #4226's finding was partly that the root project was unexamined -- by
    this guard and, per #4223, by any CI gate at all. If one of these files
    moves or is renamed, this guard must fail loudly rather than skip the
    project.
    """
    assert MANIFESTS[scope].is_file(), f"{MANIFESTS[scope]} is missing"
    assert LOCKFILES[scope].is_file(), f"{LOCKFILES[scope]} is missing"
    assert json.loads(MANIFESTS[scope].read_text())
    assert json.loads(LOCKFILES[scope].read_text()).get("packages")


@pytest.mark.parametrize("remediation", _DECLARED, ids=_remediation_id)
def test_a_declared_remediation_is_really_declared(remediation: Remediation) -> None:
    """A rename or removal must not silently void the declared-floor layer.

    Without this, dropping `happy-dom` from the manifest would make its
    entry vacuous rather than failing, and the next person to add the
    package back would get no protection from it.
    """
    declared = _declared_specifiers(remediation.scope)
    assert remediation.package in declared, (
        f"{remediation.scope}'s manifest no longer declares "
        f"{remediation.package}, but this registry records a floor for it "
        f"({remediation.floor_holder}). If the dependency is gone for good, "
        "remove the entry and say so; if it became transitive, set "
        "declared=False and record what holds the floor. Do not leave a "
        "floor guarding nothing."
    )


@pytest.mark.parametrize(
    "remediation", [r for r in REMEDIATIONS if not r.declared], ids=_remediation_id
)
def test_a_transitive_remediation_is_still_transitive(remediation: Remediation) -> None:
    """The `declared` flag must not go stale in the other direction.

    If someone adds an `overrides` entry or a direct dependency for one of
    these, its declared range becomes checkable -- and would go unchecked,
    because `declared=False` skips the range layer. Flip the flag instead.
    """
    declared = _declared_specifiers(remediation.scope)
    assert remediation.package not in declared, (
        f"{remediation.scope}'s manifest now declares {remediation.package} "
        f"as {declared.get(remediation.package)!r}, but this registry still "
        "marks it transitive, which skips the declared-range check. Set "
        "declared=True and point floor_holder at the section that holds it."
    )


@pytest.mark.parametrize("remediation", _DECLARED, ids=_remediation_id)
def test_the_declared_range_excludes_the_remediated_advisory(
    remediation: Remediation,
) -> None:
    """Layer 1: the floor of the range is at or above the first safe version.

    This is the check that would have failed on the release branch before
    #4221 (`@sentry/nextjs` declared `^10.69.0` against a CRITICAL covering
    everything below 10.75.2, with only the lockfile holding the line) and
    again on #4226's own head, where `web`'s `serialize-javascript`
    override sat at `^7.0.5` and the root's `fast-uri` override at
    `^3.1.8`, both inside live advisories.
    """
    specifier = _declared_specifiers(remediation.scope)[remediation.package]
    floor = _floor_of(specifier)
    assert _version_tuple(floor) >= _version_tuple(remediation.first_unaffected), (
        f"{remediation.scope}'s manifest declares {remediation.package} as "
        f"{specifier!r} ({remediation.floor_holder}), whose lowest accepted "
        f"version ({floor}) is inside {remediation.advisory}. Raise it to at "
        f"least {remediation.first_unaffected} -- moving the lockfile alone "
        "leaves the manifest accepting the vulnerable version, which is the "
        "#4221 defect."
    )


@pytest.mark.parametrize("remediation", REMEDIATIONS, ids=_remediation_id)
def test_the_lockfile_resolves_above_the_remediated_floor(
    remediation: Remediation,
) -> None:
    """Layer 2: every copy in the lock is at or above the first safe version.

    This is the layer the declared-range check cannot stand in for. A
    transitive package has no range of its own to raise (`fast-uri` in
    `web/`, `undici` at the root), and even a correct declared floor says
    nothing about a nested copy npm placed under a parent that could not
    reach it. It is also the mechanically checkable form of #4221's
    "all 15 Dependabot alerts are resolved": alerts track the default
    branch and cannot be re-read from a release branch, resolved versions
    can.
    """
    resolved = _resolved_versions(remediation.scope, remediation.package)
    assert resolved, (
        f"{remediation.package} does not appear in {_lock_name(remediation.scope)} "
        "at all, so this entry checks nothing. If the dependency is gone for "
        "good, remove the entry and say so -- do not leave a vacuous floor."
    )
    vulnerable = {
        path: version
        for path, version in resolved.items()
        if _version_tuple(version) < _version_tuple(remediation.first_unaffected)
    }
    assert not vulnerable, (
        f"{_lock_name(remediation.scope)} resolves {remediation.package} "
        f"below {remediation.first_unaffected} at {vulnerable}, which is inside "
        f"{remediation.advisory}. Re-resolve it ({remediation.floor_holder}); a "
        "nested copy counts -- `npm audit` reports it and Dependabot alerts on "
        "it even when the hoisted copy is fixed."
    )


@pytest.mark.parametrize("scope", sorted(MANIFESTS))
def test_no_dependency_is_exactly_pinned_without_a_recorded_reason(scope: str) -> None:
    """An exact pin cannot receive a lockfile-level advisory fix.

    `next` and `eslint-config-next` were both exactly pinned at `16.3.0`
    and both turned `security-scan` red; the remedy each time was to move
    to a range. The accepted pins carry their reason in
    `DELIBERATE_EXACT_PINS` so another cannot arrive unexplained.
    """
    excused = DELIBERATE_EXACT_PINS[scope]
    offenders = {
        name: specifier
        for name, specifier in _declared_specifiers(scope).items()
        if not specifier.startswith(_RANGE_PREFIXES) and name not in excused
    }
    assert not offenders, (
        f"{scope}'s manifest pins {offenders} exactly. An exact pin cannot "
        "receive a lockfile advisory fix, so `security-scan` goes red and "
        "stays red until someone edits the manifest by hand (#4192, #4221). "
        "Use a `^`/`~` range, or record the reason in DELIBERATE_EXACT_PINS."
    )


@pytest.mark.parametrize("scope", sorted(MANIFESTS))
def test_every_recorded_exact_pin_is_still_exactly_pinned(scope: str) -> None:
    """The exception list does not outlive the exceptions.

    If `react` moves to a range, its entry here should go with it --
    otherwise the list slowly becomes a set of pre-approvals for pins
    nobody re-read.
    """
    declared = _declared_specifiers(scope)
    stale = sorted(
        name
        for name, specifier in declared.items()
        if name in DELIBERATE_EXACT_PINS[scope] and specifier.startswith(_RANGE_PREFIXES)
    )
    assert not stale, (
        f"DELIBERATE_EXACT_PINS[{scope!r}] still excuses {stale}, which the "
        "manifest now declares as a range. Remove the entry."
    )


@pytest.mark.parametrize("scope", sorted(MANIFESTS))
def test_no_recorded_exact_pin_also_carries_a_remediated_floor(scope: str) -> None:
    """The two registries must not disagree about one package.

    A package that has carried a high/critical advisory needs a range, so
    appearing in both lists means the pin won over the remediation -- the
    exact state this guard exists to prevent.
    """
    remediated = {r.package for r in REMEDIATIONS if r.scope == scope}
    both = sorted(set(DELIBERATE_EXACT_PINS[scope]) & remediated)
    assert not both, (
        f"{both} is both exactly pinned and recorded as advisory-remediated in "
        f"{scope}. A dependency that has carried a high/critical advisory must "
        "stay on a range so the next lockfile fix can reach it."
    )


def test_the_registry_records_one_floor_per_package_per_project() -> None:
    """One source for each decision (D-066).

    Two entries for the same package in the same project could disagree
    about the floor, and then the guard's verdict would depend on which
    test ran -- the shape of divergence #4179 took seven weeks to find.
    """
    counts: dict[tuple[str, str], int] = {}
    for remediation in REMEDIATIONS:
        key = (remediation.scope, remediation.package)
        counts[key] = counts.get(key, 0) + 1
    duplicates = sorted(f"{scope}:{package}" for (scope, package), n in counts.items() if n > 1)
    assert not duplicates, (
        f"REMEDIATIONS records {duplicates} more than once for the same "
        "project. Keep one entry per package per project, at the highest "
        "floor, so there is one answer to 'what is safe here'."
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
    """`_floor_of` is the whole of layer 1 -- it gets its own coverage.

    In particular `^10.69.0` must read as 10.69.0 and not as "whatever the
    caret resolves to today", because the caret's floor is precisely what
    makes the manifest accept a vulnerable version.
    """
    assert _floor_of(specifier) == expected_floor


def test_the_overrides_flattener_reads_both_npm_forms() -> None:
    """`overrides` is a declared floor, and npm writes it two ways.

    The nested form is the one this guard would have been blind to a
    second time, so it is covered before the tree needs it.
    """
    assert _flatten_overrides({"serialize-javascript": "^7.1.3"}) == {
        "serialize-javascript": "^7.1.3"
    }
    assert _flatten_overrides({"foo": {".": "^2.0.0", "bar": "^3.0.0"}}) == {
        "foo": "^2.0.0",
        "bar": "^3.0.0",
    }
    assert _flatten_overrides({"foo": {"bar": "^3.0.0"}}) == {"bar": "^3.0.0"}


def test_the_resolved_version_reader_sees_nested_copies() -> None:
    """Positive control for layer 2's path matching.

    `node_modules/a/node_modules/fast-uri` must read as `fast-uri`, and
    `node_modules/fast-uri-extras` must NOT -- a prefix match would make
    the guard both blind and noisy.
    """
    assert _package_name_of("node_modules/a/node_modules/fast-uri") == "fast-uri"
    assert _package_name_of("node_modules/fast-uri-extras") != "fast-uri"
    assert _package_name_of("node_modules/@vitest/mocker") == "@vitest/mocker"


def test_the_guard_rejects_the_specifiers_it_was_written_for() -> None:
    """Positive control (#3753: a guard never seen to reject is not evidence).

    These are the five real pre-fix states of these manifests -- three from
    #4221 and the two `overrides` floors #4226 found still live. Each must
    compare as below its advisory's fixed version, or the comparison in
    layer 1 is decorative.
    """
    assert _version_tuple(_floor_of("^10.69.0")) < _version_tuple("10.75.2")
    assert _version_tuple(_floor_of("^16.3.8")) < _version_tuple("16.4.1")
    assert _version_tuple(_floor_of("^20.0.11")) < _version_tuple("21.0.4")
    assert _version_tuple(_floor_of("^7.0.5")) < _version_tuple("7.1.3")
    assert _version_tuple(_floor_of("^3.1.8")) < _version_tuple("3.1.9")
    assert _version_tuple(_floor_of("^4.0.16")) < _version_tuple("4.1.12")


def test_the_guard_rejects_the_resolutions_it_was_written_for() -> None:
    """...and the same control for layer 2.

    These are the ten versions #4226's review read out of the two
    lockfiles. Every one must compare as vulnerable, or the resolved-version
    check would have passed the branch it was written to catch.
    """
    for resolved, first_unaffected in (
        ("3.1.8", "3.1.9"),  # fast-uri, both lockfiles (alerts 170-167, 164-161)
        ("7.0.7", "7.1.3"),  # serialize-javascript, web (alert 172)
        ("4.1.11", "4.1.12"),  # @vitest/mocker via vitest, web (alerts 157/156)
        ("6.29.0", "6.29.1"),  # undici, root (alert 166)
    ):
        assert _version_tuple(resolved) < _version_tuple(first_unaffected)


def test_the_guard_accepts_the_current_manifest_floors() -> None:
    """...and the inverse: the post-fix values must compare as safe.

    A guard that rejects everything would also "pass" the two tests above.
    """
    assert _version_tuple(_floor_of("^10.76.2")) >= _version_tuple("10.75.2")
    assert _version_tuple(_floor_of("^16.4.3")) >= _version_tuple("16.4.1")
    assert _version_tuple(_floor_of("^21.0.4")) >= _version_tuple("21.0.4")
    assert _version_tuple(_floor_of("^7.1.3")) >= _version_tuple("7.1.3")
    assert _version_tuple(_floor_of("^3.1.9")) >= _version_tuple("3.1.9")
    assert _version_tuple(_floor_of("^4.1.12")) >= _version_tuple("4.1.12")
