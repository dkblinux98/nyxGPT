"""nyxGPT's icon images are declared in exactly one place (#4235).

Next.js offers two independent ways to tell a browser what a site's icons are,
and an app that uses both does not get a merge -- it gets a precedence fight:

* the **file convention**, where `app/icon.png` and `app/apple-icon.png` become
  the routes `/icon.png` and `/apple-icon.png` by mere presence; and
* **config metadata**, where `metadata.icons` in `app/layout.tsx` emits the
  `<link rel="icon">` tags itself.

This tree had both. Config metadata won, so the two source images were built
into published routes that nothing referenced, and anyone editing them to
change the favicon saw no change -- the authoritative declaration was in
`layout.tsx` and pointed at `web/public/` instead. Worse in the other
direction: trimming `metadata.icons` would have silently handed the icons back
to the file convention, changing what the browser is told with no edit at the
point of change.

A third copy of the same decision lived in `web/public/manifest.json`, which
re-named `favicon-16x16.png`, `favicon-32x32.png` and `apple-touch-icon.png`
beside the two install logos. That is a different standard (the W3C web app
manifest, not HTML `<link>`), so it cannot be expressed *through*
`metadata.icons` -- but it must not drift from it either, which is what three
hand-maintained copies of one list guarantees eventually.

So the resolution is one source and two derivations:

* `web/src/lib/appIcons.ts` is the only place an icon URL is written down;
* `web/src/app/layout.tsx` derives `metadata.icons` from it;
* `web/src/app/manifest.ts` derives the manifest's `icons` from it, replacing
  the static `web/public/manifest.json`.

The class this guard exists for is not those two PNG files. It is that nothing
asserted the app had *one* icon source, so a second mechanism could be added --
or an existing one trimmed -- and the icons would change with no edit saying
so. #4233's guard (`test_web_public_assets_are_referenced.py`) checks
`web/public/` reachability and structurally could not see this: both files were
under `web/src/app/`.

Matching here is textual for the same reason it is in #4233's guard: icons are
addressed as URL strings in TSX, in the manifest and in config, so there is no
import graph to walk. Tracked files only (`git ls-files`), because the build
writes service-worker output into `web/public/` that a clean checkout does not
have.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed argv, no shell
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The one module allowed to contain an icon URL literal.
SINGLE_SOURCE = "web/src/lib/appIcons.ts"

#: The two derivations, which must exist for the single source to reach a
#: browser at all. Named rather than discovered: a guard that passed because
#: `layout.tsx` had been deleted would be worse than no guard.
LAYOUT = "web/src/app/layout.tsx"
MANIFEST_ROUTE = "web/src/app/manifest.ts"

#: Basenames Next.js reserves under `app/` for the icon file convention. A file
#: with one of these names is turned into a published route and an automatic
#: `<link>` tag whether or not anything references it, which is exactly the
#: mechanism that must not come back while `metadata.icons` is authoritative.
#:
#: `icon` and `apple-icon` may also be numbered (`icon0.png`, `icon1.png`) when
#: a site ships several sizes through the convention, hence the `\d*`.
_FILE_CONVENTION_ICON_RE = re.compile(
    r"^(icon\d*|apple-icon\d*|favicon)\.(ico|jpg|jpeg|png|svg|gif|webp|js|jsx|ts|tsx)$"
)

#: The instance #4235 was filed for, pinned by name so a revert or a future
#: `create-next-app` run cannot quietly re-admit it. Both were byte-identical
#: copies of assets already in `web/public/` (`icon.png` of
#: `nyxGPT-logo-512.png`, `apple-icon.png` of `apple-touch-icon.png`), so
#: removing them cost the product no artwork -- only the second mechanism.
_RETIRED_DUPLICATE_DECLARATIONS = (
    "web/src/app/icon.png",
    "web/src/app/apple-icon.png",
    "web/public/manifest.json",
)

#: A rooted URL path to an image, in either quote style.
_ICON_URL_RE = re.compile(r"""['"](/[A-Za-z0-9_.-]+\.(?:png|ico|svg|webp))['"]""")

#: Where an icon URL would plausibly be re-hardcoded. `web/tests` is excluded on
#: purpose: a test asserting that `/favicon.ico` reaches the browser has to name
#: it, and that assertion is the point of the test.
_SCANNED_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".json")


def _tracked_files() -> tuple[str, ...]:
    """Every path in the git index, repo-relative, POSIX-separated.

    Same shape as `test_web_public_assets_are_referenced.py::_tracked_files`:
    fixed argv, no shell, and a hard stop rather than a guess when git cannot
    answer. The index is what "ships" means for a checked-in asset; the working
    tree also holds build output this guard must not judge.
    """
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "ls-files", "-z"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - git absent
        pytest.skip(f"git unavailable, cannot enumerate tracked files: {exc}")
    if out.returncode != 0:  # pragma: no cover - not a work tree
        pytest.skip("not a git work tree, cannot enumerate tracked files")
    return tuple(entry for entry in out.stdout.split("\0") if entry)


def _read_text(path: str) -> str:
    """File text, or `""` for anything that is not decodable UTF-8."""
    try:
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _declared_icon_urls(source: str) -> tuple[str, ...]:
    """The rooted URL paths the single source declares, in file order.

    Read out of the module rather than restated here, so adding an icon to the
    inventory extends every assertion below instead of silently escaping them.
    Only rooted paths that look like an image are collected: the module also
    contains prose and type names, and `start_url`-style values belong to the
    manifest, not to this list.

    Both quote styles are matched. The repo has no formatter for TypeScript, so
    which one a file uses is the author's choice -- reading only one of them is
    how this function silently returned nothing on its first run and made every
    assertion downstream vacuous, which is what `test_the_single_source…`
    below now fails on.
    """
    return tuple(dict.fromkeys(re.findall(_ICON_URL_RE, source)))


TRACKED_FILES: tuple[str, ...] = _tracked_files()


def test_the_single_source_and_both_derivations_exist():
    """A broken enumeration would make every assertion below vacuously true."""
    for path in (SINGLE_SOURCE, LAYOUT, MANIFEST_ROUTE):
        assert path in TRACKED_FILES, f"{path} is not tracked; this guard is asserting nothing"
    urls = _declared_icon_urls(_read_text(SINGLE_SOURCE))
    assert len(urls) >= 4, f"{SINGLE_SOURCE} declares only {urls!r}; the inventory did not parse"
    assert "/favicon.ico" in urls


def test_no_file_convention_icon_shadows_the_metadata_declaration():
    """Nothing under `web/src/app/` uses Next's icon file convention.

    `metadata.icons` is this app's authoritative declaration. A file-convention
    icon beside it is published at its own route and emits its own `<link>`,
    and which of the two the browser ends up honouring is Next's precedence
    rule rather than anything written in this repository.
    """
    offenders = [
        path
        for path in TRACKED_FILES
        if path.startswith("web/src/app/")
        and _FILE_CONVENTION_ICON_RE.match(path.rpartition("/")[2])
    ]
    assert not offenders, (
        f"{offenders} use Next's app-directory icon file convention, which publishes "
        f"a route and emits a <link> by mere presence. This app declares its icons in "
        f"{SINGLE_SOURCE} and renders them through `metadata.icons` in {LAYOUT}; a "
        "second mechanism means the icons are chosen by framework precedence instead "
        "of by an edit anyone can read. Add the image to web/public/ and to the "
        "inventory instead (#4235)."
    )


@pytest.mark.parametrize("path", _RETIRED_DUPLICATE_DECLARATIONS)
def test_the_duplicate_declarations_stay_retired(path: str):
    """The three files #4235 removed, pinned by name.

    Reachability alone would not keep them out: re-adding `web/public/manifest.json`
    *and* a reference to it satisfies #4233's guard while restoring a second
    hand-maintained copy of the icon list.
    """
    assert path not in TRACKED_FILES, (
        f"{path} is back in the tree. It is a second declaration of which images "
        f"are nyxGPT's icons; the only one is {SINGLE_SOURCE}, which {LAYOUT} and "
        f"{MANIFEST_ROUTE} both derive from (#4235)."
    )


def test_no_icon_url_is_hardcoded_outside_the_single_source():
    """Only `appIcons.ts` writes an icon URL down.

    This is the assertion that makes the module *the* source rather than merely
    *a* source: a `/favicon-32x32.png` pasted back into `layout.tsx` or into a
    component would be a copy that the next favicon change would leave behind.
    """
    urls = _declared_icon_urls(_read_text(SINGLE_SOURCE))
    offenders: list[str] = []
    for path in TRACKED_FILES:
        if path == SINGLE_SOURCE or not path.startswith("web/src/"):
            continue
        if not path.endswith(_SCANNED_SUFFIXES):
            continue
        quoted = set(_declared_icon_urls(_read_text(path)))
        offenders.extend(f"{path} -> {url}" for url in urls if url in quoted)
    assert not offenders, (
        f"{offenders} hardcode an icon URL that {SINGLE_SOURCE} already declares. "
        "Import the inventory instead -- a second copy is a place the next favicon "
        "change will not reach (#4235)."
    )


def test_layout_derives_its_icons_from_the_single_source():
    """`layout.tsx` imports the inventory and still publishes `metadata.icons`.

    Both halves matter. Without the import it has its own copy; without
    `metadata.icons` there are no `<link rel="icon">` tags at all, and the file
    convention this guard bans would have been the only thing standing in for
    them.
    """
    layout = _read_text(LAYOUT)
    assert "appIcons" in layout, (
        f"{LAYOUT} does not import the icon inventory from {SINGLE_SOURCE}. Its "
        "`metadata.icons` must derive from the one source, not restate it (#4235)."
    )
    assert "icons:" in layout, (
        f"{LAYOUT} no longer sets `metadata.icons`, so the app emits no "
        '`<link rel="icon">` of its own. That is the trim that silently hands the '
        "icons to whatever other mechanism is present (#4235)."
    )


def test_the_manifest_route_derives_its_icons_from_the_single_source():
    """The web app manifest is generated from the inventory, not hand-written.

    `web/public/manifest.json` was the third copy of the list. Generating it
    from `appIcons.ts` is what stops the PWA install icons from drifting away
    from the favicons without anyone noticing.
    """
    manifest = _read_text(MANIFEST_ROUTE)
    assert "appIcons" in manifest, (
        f"{MANIFEST_ROUTE} does not import the icon inventory from {SINGLE_SOURCE} " "(#4235)."
    )
    assert "icons" in manifest, f"{MANIFEST_ROUTE} declares no manifest icons (#4235)."


def test_every_declared_icon_is_a_tracked_public_asset():
    """Each URL in the inventory resolves to a file `web/public/` actually ships.

    A typo here is a 404 the browser reports to nobody: a missing favicon
    renders as the generic document icon, which looks exactly like a cache miss.
    """
    missing = [
        url
        for url in _declared_icon_urls(_read_text(SINGLE_SOURCE))
        if f"web/public{url}" not in TRACKED_FILES
    ]
    assert not missing, (
        f"{missing} are declared in {SINGLE_SOURCE} but no such file is tracked under "
        "web/public/, so the browser is pointed at a 404 (#4235)."
    )
