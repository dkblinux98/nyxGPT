"""Every file published in the web bundle is referenced by the application (#4233).

`web/public/` is served verbatim by Next: each file in it is a URL on every
nyxGPT install, inside the `web` container image and inside the published
artifact. Nothing distinguished an asset the UI displays from one a generator
left behind, so `create-next-app`'s `file.svg`, `globe.svg`, `window.svg`,
`next.svg` and `vercel.svg` -- the last of which is another product's logo --
were public URLs for the life of the repository, and an owner-added
`stone-soup-creative-logo.png` nothing ever rendered shipped beside them.

The class is not those seven files: it is that a directory whose contents are
published by mere presence had no reachability check, so the next unused asset
lands exactly the same way (the rule-with-no-guard class, #4174). This is that
check, and it covers the two surfaces the class reaches:

* **`web/public/`** -- published by presence; an unreferenced file here is a
  live URL serving something no part of the product asks for.
* **`web/src/**/*.module.css`** -- a CSS module no module imports is dropped by
  the bundler, so it costs no bytes, but it is read by the next contributor as
  the live styling for the component it is named after. `page.module.css` was
  the scaffold's styling for a `page.tsx` this project replaced entirely.

Deliberate design choices, both of which exist to keep the guard green-able:

* **Tracked files only** (`git ls-files`). The PWA build writes `sw.js`,
  `workbox-*.js` and `fallback-*.js` into `web/public/` and `web/.gitignore`
  excludes them; walking the directory instead would go red on any machine
  that has run `npm run build` and stay green in a clean checkout. Reading the
  ignore list here would make a second copy of a decision `.gitignore` already
  owns, so the index is the single source.
* **A reference is a string match, basename or URL path.** Assets are
  addressed as URLs in TSX, in the generated web app manifest and in config,
  sometimes built up at runtime, so there is no import graph to walk. Matching
  the basename as well as the rooted path can let a coincidental hit pass;
  that is the right way to err -- a guard with false failures is a guard
  people delete.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv, no shell
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a `web/public/` asset may legitimately be referenced from. Exactly the
#: places the application reads one: the UI and its tests -- which since #4235
#: includes the generated PWA manifest, `web/src/app/manifest.ts`, and the icon
#: inventory it derives from, `web/src/lib/appIcons.ts` -- and the Next/Docker
#: build config.
#:
#: `web/README.md` is deliberately absent. Prose naming an asset is not the UI
#: displaying it, and counting it would let a sentence in a document keep a
#: dead file published.
_REFERRER_DIRS = ("web/src", "web/tests")
_REFERRER_FILES = (
    "web/next.config.ts",
    "web/Dockerfile",
    "web/.dockerignore",
    "web/package.json",
)
#: Public files that are themselves referrers -- a JSON or manifest file in
#: `web/public/` can name other published assets. There are none today: #4235
#: replaced the hand-written `web/public/manifest.json` with the generated
#: `web/src/app/manifest.ts` route, which the referrer *dirs* above already
#: cover. The mechanism stays because the next published config file to land
#: here would need it, and because the self-reference exclusion below is the
#: non-obvious part of getting it right.
_REFERRER_PUBLIC_SUFFIXES = (".json", ".webmanifest")

#: The scaffold assets #4233 removed, pinned so a future generator run or a
#: revert cannot quietly re-admit them. Stated as names rather than as "the
#: reachability test passes", because re-adding `vercel.svg` *and* a reference
#: to it would satisfy reachability and still ship another product's logo.
_RETIRED_SCAFFOLD_ASSETS = (
    "web/public/file.svg",
    "web/public/globe.svg",
    "web/public/next.svg",
    "web/public/vercel.svg",
    "web/public/window.svg",
    "web/src/app/page.module.css",
)


def _tracked_files() -> tuple[str, ...]:
    """Every path in the git index, repo-relative, POSIX-separated.

    Same shape as `tests/unit/test_install_instructions.py::_tracked_paths`:
    fixed argv, no shell, and a hard stop rather than a guess when git cannot
    answer. The index is what "ships" means for a checked-in asset -- the
    working tree also holds build output this guard must not judge.
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


def _read_text(path: Path) -> str:
    """File text, or `""` for anything that is not decodable UTF-8.

    A referrer directory can hold a binary -- #4233 hit `web/src/app/icon.png`,
    which Next's app-directory icon convention put in source before #4235
    retired that mechanism. A binary in the corpus contributes no references;
    it does not stop the sweep.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _public_assets(tracked: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in tracked if p.startswith("web/public/"))


def _application_corpus(tracked: tuple[str, ...]) -> str:
    """Concatenated text of the UI, its tests and the build config."""
    return "\n".join(
        _read_text(REPO_ROOT / path)
        for path in tracked
        if path.startswith(_REFERRER_DIRS) or path in _REFERRER_FILES
    )


def _public_referrer_texts(tracked: tuple[str, ...]) -> dict[str, str]:
    """Public files that are themselves referrers, keyed by path.

    Kept separate from the application corpus so a file cannot vouch for
    itself. An asset that mentioned its own filename -- an SVG titled after
    itself, a JSON config naming its own path -- would otherwise certify its
    own reachability.
    """
    return {
        path: _read_text(REPO_ROOT / path)
        for path in tracked
        if path.startswith("web/public/") and path.endswith(_REFERRER_PUBLIC_SUFFIXES)
    }


TRACKED_FILES: tuple[str, ...] = _tracked_files()


def test_the_inventory_is_not_empty():
    """A broken enumeration would make every assertion below vacuously true."""
    assets = _public_assets(TRACKED_FILES)
    assert "web/public/favicon.ico" in assets
    assert "web/public/nyxGPT-logo.png" in assets
    assert "web/src/app/layout.tsx" in TRACKED_FILES
    assert not any(p.startswith("web/node_modules/") for p in TRACKED_FILES)


def test_no_published_web_asset_is_unreferenced():
    """Every tracked file under `web/public/` is named by the application.

    The failure message names the file and what satisfies the guard, because
    the two correct answers are opposite: display it, or delete it.
    """
    base = _application_corpus(TRACKED_FILES)
    public_referrers = _public_referrer_texts(TRACKED_FILES)
    orphans = []
    for asset in _public_assets(TRACKED_FILES):
        name = asset.rpartition("/")[2]
        url = "/" + asset[len("web/public/") :]
        corpus = "\n".join(
            [base] + [text for path, text in public_referrers.items() if path != asset]
        )
        if name not in corpus and url not in corpus:
            orphans.append(asset)
    assert not orphans, (
        f"{orphans} are published at their URLs by every nyxGPT install and "
        "referenced by nothing in web/src, web/tests or the Next/Docker "
        "config. Either reference the asset from the surface "
        "that should display it, or delete it -- `web/public/` has no inert "
        "files, only files nobody has noticed are public (#4233)."
    )


def test_every_css_module_is_imported():
    """A `*.module.css` under `web/src` is imported by some module there.

    An unimported CSS module compiles to nothing, which is why the scaffold's
    `page.module.css` survived every build: the cost is not bytes, it is the
    next contributor editing styling that cannot reach the page.
    """
    modules = [p for p in TRACKED_FILES if p.startswith("web/src/") and p.endswith(".module.css")]
    corpus = "\n".join(
        _read_text(REPO_ROOT / p)
        for p in TRACKED_FILES
        if p.startswith("web/src/") and p.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs"))
    )
    orphans = [p for p in modules if p.rpartition("/")[2] not in corpus]
    assert not orphans, (
        f"{orphans} are imported by no module under web/src, so the bundler drops "
        "them and they style nothing. Import the module from the component it "
        "belongs to, or delete it (#4233)."
    )


@pytest.mark.parametrize("path", _RETIRED_SCAFFOLD_ASSETS)
def test_the_create_next_app_assets_stay_retired(path: str):
    """The instance #4233 was filed for, pinned by name.

    `next.svg` and `vercel.svg` are another product's marks; the rest are a
    generator's placeholder art for a page this project does not have. None of
    them becomes nyxGPT's by acquiring a reference.
    """
    assert path not in TRACKED_FILES, (
        f"{path} is back in the tree. It is `create-next-app` scaffolding: "
        "generator placeholder art, or another product's logo published inside "
        "the nyxGPT web bundle (#4233)."
    )
