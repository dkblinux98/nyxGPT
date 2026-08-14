"""Vendored `nyxgpt-api`/`nyxgpt-web` source tarballs -- the one implementation.

Split out of `nyxgpt.ops` (#3741) for one reason: **this module must import
nothing but the standard library.**

`scripts/build_homebrew_artifacts.py` is release *tooling*. Both jobs that
run it (`release-artifacts.yml`'s `homebrew-tap` and
`release-publish-pypi.yml`'s `homebrew-tap-rc`) do nothing but check the
repo out and set Python up -- they never `pip install` the package, because
all they need is to copy two source trees into two tarballs. While the
tarball builder lived in `ops.py`, importing it dragged in `ops.py`'s entire
runtime dependency surface (`httpx`, `pynacl`, and transitively the metrics
/ tracing / self-heal stack), and the rc tap job died at
`ModuleNotFoundError: No module named 'httpx'` *after* the candidate was
already on PyPI -- run 31659948020, the first live rc cut.

The fix is the import boundary, not a longer install step: what those jobs
need is `shutil.copytree` and `tarfile`, so the module they import needs no
third-party package at all. `tests/unit/test_build_homebrew_artifacts.py`
pins that -- it runs the script with every non-stdlib import blocked, so
adding a third-party import anywhere in this module's closure fails in CI
rather than mid-release.

`nyxgpt.ops` re-exports these names, so `ops._create_dist_tarball` and
friends keep working for the local file:// tap install path.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from pathlib import Path

# Repo root: .../nyxGPT/src/nyxgpt/release_tarball.py -> parents[2].
#
# Building a distributable artifact FROM a source checkout is one of the
# operations #3621 left REPO_ROOT-relative on purpose (see the header of
# `tests/unit/test_repo_root_allowlist.py`): the tarballs vendor the repo's
# own `src/nyxgpt/` and `web/` trees, which are not package data and have no
# importlib.resources equivalent.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories `_vendor_tree` never copies into a dist tarball -- gitignored
# build artifacts the formula regenerates fresh inside the Cellar keg
# (`web/`'s `node_modules`/`.next`/etc.), or VCS metadata that has no
# business in a release tarball.
_WEB_VENDOR_EXCLUDES = frozenset(
    {
        "node_modules",
        ".next",
        "coverage",
        "out",
        "build",
        ".vercel",
        ".turbo",
        ".git",
    }
)

# Names `_vendor_tree` skips in *every* tree it copies -- api and web alike,
# on top of whatever the caller passes as `excludes` (#3757). Interpreter
# bytecode caches belong to whatever Python last ran in the checkout, not to
# a distribution: the formula's keg venv recompiles from source anyway, so
# vendoring them makes tarballs built from a used checkout nondeterministic
# and carries version-specific `.pyc` junk (a `cpython-314` cache built by a
# 3.14 run into a tarball a 3.12 keg installs). It also broke the install
# outright -- on a checkout inside a paused-sync cloud-storage folder, a
# dehydrated (online-only) `.pyc` placeholder cannot be materialized, so
# `shutil.copytree` died with `Errno 60: Operation timed out` and `nyxgpt up`
# failed at the `native api service` step. `.DS_Store` is Finder metadata
# with the same "never belongs in a release artifact" status.
_ALWAYS_VENDOR_EXCLUDES = frozenset({"__pycache__", ".DS_Store"})

# File suffixes excluded everywhere, alongside `_ALWAYS_VENDOR_EXCLUDES`.
# `shutil.copytree`'s `ignore` callback sees file names as well as directory
# names, so loose `.pyc`/`.pyo` files outside a `__pycache__` directory are
# dropped too.
_ALWAYS_VENDOR_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

# The tag of the release that SERVES a version's tarballs (#3763).
#
# GitHub releases in this repository are immutable: once published, one can
# never gain or change an asset. A stable release is published by the release
# ceremony (a draft flipped to `draft=false`) *before* its Homebrew tarballs
# are built, so uploading them onto it afterwards is a guaranteed
# `HTTP 422: Cannot upload assets to an immutable release` -- which is exactly
# how both 2.1.0 backfill runs died, and how every future release's tap push
# would have died too.
#
# So the tarballs get a release of their own, tagged `<version>-homebrew`,
# created with its assets attached in the same `gh release create` call. It is
# published as a prerelease and never "latest", so it is not a second release
# of nyxGPT and cannot re-trigger `release-artifacts.yml` (which listens for
# `released`, not `prereleased`).
#
# `scripts/build_homebrew_artifacts.py` stamps the formulas' `url` against
# this tag, and `nyxgpt.ops._download_release_tarball` falls back to it when
# an artifact install fetches the same tarballs -- both from this one
# definition, so the publisher and the consumers cannot disagree about where
# the assets live.
HOMEBREW_ASSET_TAG_SUFFIX = "-homebrew"


def homebrew_asset_release_tag(version: str) -> str:
    """The tag of the release carrying `version`'s Homebrew source tarballs.

    `2.1.0` -> `2.1.0-homebrew`. See `HOMEBREW_ASSET_TAG_SUFFIX` for why the
    assets do not live on the release named by the version itself.
    """
    tag = version.strip()
    if not tag:
        raise ValueError("a version is required to name its Homebrew asset release")
    if tag.endswith(HOMEBREW_ASSET_TAG_SUFFIX):
        raise ValueError(
            f"{version!r} is already a Homebrew asset release tag -- pass the version "
            "it serves (e.g. 2.1.0), not the tag"
        )
    return f"{tag}{HOMEBREW_ASSET_TAG_SUFFIX}"


def _sha256_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of the file at `path`."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _vendor_tree(src: Path, dst: Path, *, excludes: frozenset[str] = frozenset()) -> None:
    """Copy the directory tree `src` to `dst`, minus excluded entries.

    Skips any entry named in `excludes` (the caller's tree-specific list,
    e.g. `_WEB_VENDOR_EXCLUDES`) *plus* `_ALWAYS_VENDOR_EXCLUDES` /
    `_ALWAYS_VENDOR_EXCLUDE_SUFFIXES`, which apply to every vendored tree so
    no caller of `_create_dist_tarball` can ship interpreter caches or
    Finder metadata (#3757).
    """

    def _ignore(_dir_path: str, names: list[str]) -> set[str]:
        return {
            n
            for n in names
            if n in excludes
            or n in _ALWAYS_VENDOR_EXCLUDES
            or n.endswith(_ALWAYS_VENDOR_EXCLUDE_SUFFIXES)
        }

    shutil.copytree(src, dst, ignore=_ignore)


def _create_dist_tarball(
    tap_dir: Path, name: str, version: str, source_root: Path | None = None
) -> Path:
    """Build a `<name>-<version>.tar.gz` distribution under `tap_dir/dist`.

    Vendors the actual source the formula needs to build a self-contained
    app inside the Cellar keg -- `pyproject.toml` + `src/nyxgpt/` for
    `nyxgpt-api` (the formula creates a Cellar-local venv and `pip install`s
    this tree), or the `web/` source tree, minus its gitignored
    `node_modules`/`.next` build output (the formula runs `npm ci`/`npm run
    build` fresh inside the keg), for `nyxgpt-web`. Either way, the
    installed app no longer depends on the repo checkout or an editable
    `.venv` at runtime (#3406) -- replacing the old placeholder tarball that
    only ever contained a `README.txt`. Both trees go through `_vendor_tree`,
    so neither can carry `__pycache__`/`.pyc`/`.DS_Store` leftovers from a
    used checkout (#3757).

    `source_root` is the tree to vendor *from*, defaulting to this checkout
    (`REPO_ROOT`) -- what every local install path wants. Release tooling
    passes a second checkout instead, so the tarballs can be built from a
    release tag's source while the tooling itself runs from a branch that
    has it (#3737: 2.1.0 predates this machinery entirely).

    Replaces any existing tarball of the same name/version. Returns the
    path to the created tarball.
    """
    src_root = REPO_ROOT if source_root is None else Path(source_root)
    dist_dir = tap_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    tar_path = dist_dir / f"{name}-{version}.tar.gz"

    tmp = dist_dir / f".tmp-{name}-{version}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    root = tmp / f"{name}-{version}"

    if name == "nyxgpt-web":
        _vendor_tree(src_root / "web", root, excludes=_WEB_VENDOR_EXCLUDES)
    else:
        _vendor_tree(src_root / "src" / "nyxgpt", root / "src" / "nyxgpt")
        shutil.copy2(src_root / "pyproject.toml", root / "pyproject.toml")
        # config_wizard builds its schema from example.config.ini at import
        # time (#3388), so `import nyxgpt.app` needs the file present. A venv
        # install has no repo root above the package, so ship the template in
        # the tarball; the formula copies it next to the installed package
        # where _resolve_example_config_path() finds it with no env var (#3406).
        shutil.copy2(src_root / "example.config.ini", root / "example.config.ini")

    if tar_path.exists():
        tar_path.unlink()

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(root, arcname=f"{name}-{version}")

    shutil.rmtree(tmp, ignore_errors=True)
    return tar_path


def build_release_dist_tarball(
    name: str, version: str, dist_root: Path, source_root: Path | None = None
) -> Path:
    """Public wrapper around `_create_dist_tarball` for release tooling (#3622).

    Builds the same vendored `nyxgpt-api`/`nyxgpt-web` source tarball the
    local file:// Homebrew tap flow (`_install_homebrew_api`/`_web`) has
    always built, but at an arbitrary output directory rather than a live
    `brew --repo` tap checkout -- one implementation of "what goes in the
    tarball", used both by the local install path and by
    scripts/build_homebrew_artifacts.py to produce the tarballs a *remote*
    tap's formula points at (attached, at creation, to the
    `<version>-homebrew` sidecar release -- see
    `homebrew_asset_release_tag`).

    `source_root` selects the tree the tarball is vendored from (default:
    this checkout). Publishing a tag whose own tree has no release tooling
    -- the 2.1.0 tap backfill, #3737 -- runs the tooling from a branch that
    has it and points this at a checkout of the tag.
    """
    return _create_dist_tarball(dist_root, name, version, source_root)
