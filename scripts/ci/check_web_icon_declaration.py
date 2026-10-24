#!/usr/bin/env python3
"""Assert a finished web build tells the browser about one set of icons (#4235).

This is the executed half of #4235. The unit guard
(`tests/unit/test_web_icon_declaration_is_single_sourced.py`) reads the source
tree and can prove the *inputs* are single-sourced: one module holds the icon
URLs, both derivations import it, no file-convention icon exists. It cannot
prove the *output* -- what Next actually puts in the `<head>` and which routes
it publishes -- because that is produced by the framework at build time and no
unit test reaches it. That gap is exactly how the defect survived: the two
source images were real, their routes were real, and every test passed while no
`<link>` in the shipped HTML named either one.

So this runs against `web/.next` after a real `npm run build` and checks the
rendered artifacts:

1. **One manifest link, at the generated route.** Two `<link rel="manifest">`
   tags -- which is what setting `metadata.manifest` alongside the
   `app/manifest.ts` convention produces -- point the browser at two manifests
   and let it pick.
2. **Every icon the `<head>` names is in the generated manifest**, whose
   `icons` are derived from the single source. This is what ties the HTML
   derivation to the manifest derivation at runtime rather than by inspection:
   a URL hardcoded back into `layout.tsx` fails here even though it would
   render perfectly well. `favicon.ico` is the one allowed exclusion, and it is
   named rather than pattern-matched -- a manifest entry wants a concrete MIME
   type and a multi-resolution `.ico` has none.
3. **No published route is a Next icon file convention.** `app/icon.png` and
   `app/apple-icon.png` become `/icon.png` and `/apple-icon.png` by mere
   presence, whether or not anything references them, and whether or not they
   win the precedence fight with `metadata.icons`.

Usage:

    python3 scripts/ci/check_web_icon_declaration.py [--next-dir web/.next]

Exit 0 if the build is consistent, 1 with the specific failures on stdout
otherwise. `.github/workflows/web-icon-declaration-smoke.yml` runs it both
ways: against the tree as it ships, and again after putting `app/icon.png`
back, where it must fail -- a check that cannot fail is not evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: A `<link>` element in Next's prerendered HTML, with its attributes.
_LINK_RE = re.compile(r"<link\s[^>]*?/?>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')

#: The `rel` values that declare an icon to a browser.
_ICON_RELS = frozenset({"icon", "shortcut icon", "apple-touch-icon", "apple-touch-startup-image"})

#: Icon URLs allowed in the `<head>` without a matching manifest entry, with
#: the reason each is excluded. Deliberately a fixed list rather than a rule:
#: "anything the manifest omits is fine" would accept every drift this check
#: exists to catch.
_MANIFEST_EXEMPT = {
    "/favicon.ico": "a multi-resolution .ico has no single MIME type for a manifest entry",
}

#: Routes Next publishes from the app-directory icon file convention.
#:
#: Matched against the *route list*, never against a `<link href>`. The two are
#: indistinguishable by URL -- `/favicon.ico` is this app's `web/public/` asset
#: and would also be the route of an `app/favicon.ico`, so judging hrefs by
#: this pattern flags the correct configuration. Which mechanism produced a URL
#: is only answerable from the route manifest.
_FILE_CONVENTION_ROUTE_RE = re.compile(
    r"^/(icon\d*|apple-icon\d*|favicon)\.(ico|jpg|jpeg|png|svg|gif|webp)$"
)

#: A page that must be present, so a rename cannot turn this check into a
#: silent no-op over an empty file list.
_REQUIRED_PAGE = "settings.html"

#: Prerendered pages that legitimately carry no icon metadata.
#:
#: `_global-error.html` is Next's global-error boundary, which *replaces* the
#: root layout rather than nesting inside it -- so the `metadata` export never
#: reaches it and it ships no `<head>` metadata at all. Excluded by name rather
#: than by a "pages without icon links are skipped" rule, which would make the
#: missing-icons assertion below unfailable.
_PAGES_WITHOUT_METADATA = frozenset({"_global-error.html"})


def _links(html: str) -> list[dict[str, str]]:
    """Every `<link>` element's attributes, lowercased keys."""
    return [
        {key.lower(): value for key, value in _ATTR_RE.findall(element)}
        for element in _LINK_RE.findall(html)
    ]


def _check_page(path: Path, manifest_sources: set[str]) -> list[str]:
    """Failures for one prerendered page, empty if it is consistent."""
    failures: list[str] = []
    links = _links(path.read_text(encoding="utf-8", errors="replace"))

    manifest_links = [link for link in links if link.get("rel") == "manifest"]
    if len(manifest_links) != 1:
        failures.append(
            f'{path.name}: {len(manifest_links)} <link rel="manifest"> elements, expected '
            "exactly 1. Two of them means `metadata.manifest` is set as well as the "
            "`app/manifest.ts` convention, and the browser chooses between them."
        )
    elif manifest_links[0].get("href") != "/manifest.webmanifest":
        failures.append(
            f'{path.name}: <link rel="manifest"> points at '
            f"{manifest_links[0].get('href')!r}, not the generated "
            "/manifest.webmanifest route."
        )

    icon_links = [link for link in links if link.get("rel", "") in _ICON_RELS]
    if not icon_links:
        failures.append(
            f"{path.name}: no icon <link> at all. `metadata.icons` has stopped reaching "
            "the browser, which is the trim that hands the icons to any other mechanism "
            "present."
        )
    for link in icon_links:
        href = link.get("href", "")
        if href not in manifest_sources and href not in _MANIFEST_EXEMPT:
            failures.append(
                f"{path.name}: icon <link> names {href!r}, which the generated manifest "
                "does not list. Both are supposed to derive from "
                "web/src/lib/appIcons.ts, so this is a URL that has been written down "
                "twice and drifted."
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--next-dir",
        default="web/.next",
        type=Path,
        help="the Next build output directory (default: web/.next)",
    )
    args = parser.parse_args(argv)

    next_dir: Path = args.next_dir
    app_dir = next_dir / "server" / "app"
    if not app_dir.is_dir():
        print(f"FAIL: {app_dir} does not exist -- run `npm run build` in web/ first.")
        return 1

    failures: list[str] = []

    manifest_body = app_dir / "manifest.webmanifest.body"
    manifest_sources: set[str] = set()
    if not manifest_body.is_file():
        failures.append(
            f"FAIL: {manifest_body} is missing, so the build published no web app "
            "manifest. `web/src/app/manifest.ts` is what generates it."
        )
    else:
        icons = json.loads(manifest_body.read_text(encoding="utf-8")).get("icons") or []
        manifest_sources = {icon.get("src", "") for icon in icons}
        if not manifest_sources:
            failures.append(
                "FAIL: the generated manifest declares no icons, so an install has "
                "nothing to show. Check `manifestIcons()` in web/src/lib/appIcons.ts."
            )

    routes_manifest = next_dir / "app-path-routes-manifest.json"
    if not routes_manifest.is_file():
        failures.append(f"FAIL: {routes_manifest} is missing; cannot read the route list.")
    else:
        routes = json.loads(routes_manifest.read_text(encoding="utf-8"))
        published = sorted(
            route.removesuffix("/route")
            for route in routes.values()
            if _FILE_CONVENTION_ROUTE_RE.match(route.removesuffix("/route"))
        )
        if published:
            failures.append(
                f"FAIL: {published} are published routes produced by Next's "
                "app-directory icon file convention. That convention declares the "
                "app's icons a second time, independently of `metadata.icons`, and "
                "publishes a URL whether or not anything references it (#4235)."
            )

    pages = sorted(
        page for page in app_dir.glob("*.html") if page.name not in _PAGES_WITHOUT_METADATA
    )
    if not pages:
        failures.append(
            f"FAIL: no prerendered page under {app_dir}, so every assertion below "
            "would be vacuous."
        )
    elif not any(page.name == _REQUIRED_PAGE for page in pages):
        failures.append(
            f"FAIL: {_REQUIRED_PAGE} is not among the prerendered pages "
            f"({[page.name for page in pages]}). This check names a page on purpose so "
            "a route rename cannot reduce it to a no-op."
        )

    for page in pages:
        failures.extend(f"FAIL: {failure}" for failure in _check_page(page, manifest_sources))

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print(
        f"OK: {len(pages)} prerendered pages each name one manifest "
        f"(/manifest.webmanifest) and only icons the generated manifest lists "
        f"({sorted(manifest_sources)} plus {sorted(_MANIFEST_EXEMPT)}); no "
        "file-convention icon route is published."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
