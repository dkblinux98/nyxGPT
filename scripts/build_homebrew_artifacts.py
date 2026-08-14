#!/usr/bin/env python3
"""Build remote-Homebrew-tap release artifacts for a given version (#3622).

Builds the `nyxgpt-api`/`nyxgpt-web` vendored source tarballs (the same
tarball `nyxgpt.release_tarball.build_release_dist_tarball` / the local
file:// tap flow builds), then stamps `homebrew/tap/*.rb.tmpl` with the
tarballs' real url/sha256/version to produce ready-to-publish formula files.

Two channels share that machinery (#3727, owner decision 2026-08-11):

* ``stable`` (the default) stamps `nyxgpt-api.rb` / `nyxgpt-web.rb` -- what
  `release-artifacts.yml` pushes to the tap on every GitHub Release, and
  what `brew install nyxgpt-api` resolves to.
* ``rc`` stamps **separate** `nyxgpt-api@<release>rc.rb` /
  `nyxgpt-web@<release>rc.rb` formulas (e.g. `nyxgpt-api@3.0.0rc.rb`) from
  the same templates, for acceptance-testing an unreleased release
  candidate. Homebrew has no pre-release semantics, so channel separation
  lives in the formula *names*: an rc publish therefore never writes a
  stable formula file at all, and `brew install nyxgpt-api` keeps resolving
  to the latest stable release no matter how many RCs are cut. The
  candidate formulas declare `conflicts_with` their stable counterparts, so
  switching channels on one machine is an explicit uninstall rather than a
  silent clobber.

  The name carries the **release line** (owner decision 2026-08-12, #3735):
  `nyxgpt-api@3.0.0rc` is a candidate for 3.0.0 and can never silently
  become a candidate for the next line -- 3.1.0's candidates are a
  differently named formula, so a machine left on `@3.0.0rc` stays where the
  operator put it and the released line's candidates are retired by name.
  Homebrew's `@` spelling is what makes that legible, and it loads because a
  **digit** follows the `@`: `Formulary.class_s("nyxgpt-api@3.0.0rc")` ->
  `NyxgptApiAT300rc`. `@rc` (no digit) would render the illegal constant
  `NyxgptApi@rc` -- `assert_loadable_formula_name` keeps that mistake out.

Run from a repo checkout (CI's release-artifacts.yml / release-publish-pypi.yml
jobs) -- this is a release-tooling script, not part of the installed package,
so it's exempt from the REPO_ROOT self-containment boundary #3621 drew around
`nyxgpt ops install`/`up` (see tests/unit/test_repo_root_allowlist.py).

`--source-root` separates the two trees this script uses (#3737). By default
both are this checkout: the tooling (templates, tarball builder) and the
service source it vendors. A tag that *predates* the tooling has no copy of
it -- 2.1.0 has no `scripts/build_homebrew_artifacts.py` and no
`homebrew/tap/*.rb.tmpl` at all -- so publishing that tag's formulas means
running this script from a branch that has the tooling and pointing
`--source-root` at a checkout of the tag. The tarballs are then 2.1.0's real
source, stamped by templates the tag never contained.

`--tarballs-from` stamps the formulas from tarballs that are *already
published* rather than building new ones (#3763). Tarball builds are not
byte-reproducible -- gzip embeds a timestamp and the tar members carry the
checkout's mtimes -- so two builds of the identical source have different
sha256s. Whenever the serving release already carries the assets (a re-run,
or a release from before this machinery), re-stamping from a fresh build
would publish formulas whose `sha256` cannot match the bytes brew downloads,
and immutability means those bytes can never be corrected. Pointing this at
the downloaded assets makes the formula agree with what is actually served.

Usage:
    python scripts/build_homebrew_artifacts.py VERSION OUT_DIR BASE_URL \
        [--channel rc] [--source-root DIR | --tarballs-from DIR]
    python scripts/build_homebrew_artifacts.py --asset-tag VERSION

    VERSION   e.g. 2.1.0 (or 3.0.0rc4 with --channel rc)
    OUT_DIR   directory to write tarballs + stamped formulas into
    BASE_URL  download URL prefix the tarballs will be published under,
              e.g. https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0-homebrew
              (formula `url` becomes "<BASE_URL>/<tarball filename>")
    --channel stable (default) or rc -- see above
    --source-root  checkout to vendor the service source from (default: this
              checkout) -- see above
    --tarballs-from  directory holding the already-published
              `<name>-<version>.tar.gz` files to stamp against, instead of
              building them -- see above. Mutually exclusive with
              `--source-root`: nothing is vendored when the tarballs are given.
    --asset-tag  print the release tag a stable version's tarballs are
              published under (`2.1.0` -> `2.1.0-homebrew`) and exit -- what
              the tap job builds its BASE_URL from (#3763, see
              `asset_release_tag`)
"""

from __future__ import annotations

import re
import shutil
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# `nyxgpt.release_tarball` imports nothing but the standard library (#3741),
# so this script runs from a bare checkout + `setup-python` with no
# `pip install` step at all -- which is exactly what the two CI jobs that run
# it provide. Importing the same helpers from `nyxgpt.ops` instead pulled in
# httpx/pynacl and the whole metrics/tracing stack, and killed the rc tap job
# after the candidate had already been published to PyPI.
from nyxgpt.release_tarball import (  # noqa: E402
    _sha256_file,
    build_release_dist_tarball,
    homebrew_asset_release_tag,
)

_FORMULAS = ("nyxgpt-api", "nyxgpt-web")

CHANNELS = ("stable", "rc")

# `3.0.0rc4` / `3.0.0` -> the `3.0.0` release line the candidate belongs to.
_RELEASE_LINE_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:rc\d+)?$")

# A released version, exactly: what a real release tag looks like. The stable
# formulas are the ones `brew install nyxgpt-api` resolves, so nothing that is
# not a release may ever stamp them.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# What a checkout must contain to be vendorable as `--source-root`: the two
# trees `_create_dist_tarball` copies from, plus the two files the api tarball
# ships. Checked up front so a wrong `--source-root` fails with a readable
# message instead of a FileNotFoundError from inside the tarball builder --
# or, worse, an api tarball vendored from the tooling checkout by accident.
_SOURCE_TREE_MARKERS = ("pyproject.toml", "example.config.ini", "src/nyxgpt", "web")

# Markers around the `sitecustomize.py` the api formulas write into their
# build environment (#3753). They delimit Python living inside a Ruby heredoc,
# which nothing else in this repo can syntax-check -- `extract_build_shim`
# below is what lets the release build, the unit suite and the macOS smoke job
# all read that one source instead of re-deriving it.
_SHIM_BEGIN = "# nyxgpt-mac-ver-shim: begin"
_SHIM_END = "# nyxgpt-mac-ver-shim: end"

_CLASS_RE = re.compile(r"^class\s+(\w+)\s+<\s+Formula\b", re.MULTILINE)

_LICENSE_RE = re.compile(r"^([ \t]*)license\s+\"[^\"]*\"[ \t]*$", re.MULTILINE)

# `Formulary.class_s`, transcribed from Homebrew: capitalize, camel-case away
# the separators, `+` -> `x`, and `@` -> `AT` *only* before a digit.
_SEPARATOR_RE = re.compile(r"[-_.\s]([a-zA-Z0-9])")
_AT_VERSION_RE = re.compile(r"(.)@(\d)")

# An `@` that is not followed by a digit survives `class_s` verbatim and lands
# in a would-be Ruby constant, which cannot be declared -- see `class_s` above.
_UNLOADABLE_AT_RE = re.compile(r"@(?!\d)")


def assert_loadable_formula_name(formula: str) -> str:
    """Fail loudly on a formula file name Homebrew could never load.

    `brew` derives the Ruby constant it looks for from the *file name*, and
    `@` only becomes `AT` when a digit follows it. `nyxgpt-api@rc` therefore
    resolves to `NyxgptApi@rc` -- not a legal constant, so no class
    declaration inside the file can satisfy the loader and `brew info` fails
    with "Expected to find class NyxgptApi@rc". The candidate formulas spell
    the release line right after the `@` (`nyxgpt-api@3.0.0rc` ->
    `NyxgptApiAT300rc`), which is exactly the digit-gated form brew loads.
    """
    if _UNLOADABLE_AT_RE.search(formula):
        raise ValueError(
            f"{formula!r} is not a loadable Homebrew formula name: `@` is only "
            "translated to `AT` when a digit follows it, so brew would look for "
            f"the illegal constant {formula_class_name(formula)!r}"
        )
    return formula


def release_line(version: str) -> str:
    """The release a version belongs to: `3.0.0rc4` -> `3.0.0`.

    Raises on anything that is not a release or an RC of one -- the formula
    name is derived from this, and a name that misstates its line is exactly
    what the versioned naming exists to prevent.
    """
    match = _RELEASE_LINE_RE.match(version.strip())
    if match is None:
        raise ValueError(
            f"{version!r} is not a release or release-candidate version -- expected X.Y.Z "
            "or X.Y.ZrcN, e.g. 3.0.0rc1"
        )
    return match.group(1)


def assert_release_version(version: str) -> str:
    """Refuse to stamp a stable formula for anything but a real release.

    The stable formulas are what `brew install nyxgpt-api` resolves to, so
    the only version allowed to write them is a released `X.Y.Z`. A
    candidate (`3.0.0rc4`) or a dev build reaching this channel would put a
    pre-release on every clean Mac -- the rc channel's separately named
    `@<line>rc` formulas exist precisely so it never has to.
    """
    if _RELEASE_VERSION_RE.match(version.strip()) is None:
        raise ValueError(
            f"{version!r} is not a release version -- the stable formulas are stamped "
            "only from a real release tag (X.Y.Z). A release candidate belongs on the "
            "rc channel (--channel rc), which writes its own @<line>rc formulas."
        )
    return version.strip()


def asset_release_tag(version: str) -> str:
    """The release tag the stable tarballs for `version` are published under.

    `2.1.0` -> `2.1.0-homebrew`. Releases in this repository are immutable,
    so the release named by the version itself -- published by the ceremony
    before these tarballs exist -- can never gain them (#3763); the formulas'
    `url` therefore points at a sidecar release created with its assets
    attached. `release-artifacts.yml` asks this script for the tag rather
    than spelling the suffix in shell, so the tag the assets are published
    under and the tag the formulas are stamped with are one definition.
    """
    return homebrew_asset_release_tag(assert_release_version(version))


def resolve_source_root(source_root: str | Path | None) -> Path | None:
    """Validate `--source-root` before anything is built from it.

    Returns None for "vendor from this checkout" (the default), otherwise
    the resolved path -- refusing a directory that is not a nyxGPT source
    tree. Silently vendoring the wrong tree would produce a tarball stamped
    with a version whose source it does not contain, which is the one
    failure mode a tap cannot recover from after publication.
    """
    if source_root is None:
        return None
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"--source-root {root} does not exist or is not a directory")
    missing = [marker for marker in _SOURCE_TREE_MARKERS if not (root / marker).exists()]
    if missing:
        raise ValueError(
            f"--source-root {root} is not a nyxGPT source checkout -- missing "
            f"{', '.join(missing)}"
        )
    return root


def resolve_published_tarballs(tarballs_from: str | Path | None, version: str) -> Path | None:
    """Validate `--tarballs-from` before a formula is stamped against it.

    Returns None for "build the tarballs" (the default), otherwise the
    resolved directory -- refusing one that does not hold a non-empty
    tarball for *both* services. Stamping half a pair would push one formula
    agreeing with the served bytes and one not, which is the failure this
    mode exists to prevent.
    """
    if tarballs_from is None:
        return None
    root = Path(tarballs_from).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"--tarballs-from {root} does not exist or is not a directory")
    missing = [
        f"{name}-{version}.tar.gz"
        for name in _FORMULAS
        if not (root / f"{name}-{version}.tar.gz").is_file()
        or (root / f"{name}-{version}.tar.gz").stat().st_size == 0
    ]
    if missing:
        raise ValueError(
            f"--tarballs-from {root} does not hold the published tarballs for {version} "
            f"-- missing or empty: {', '.join(missing)}"
        )
    return root


def formula_name(name: str, channel: str, version: str = "") -> str:
    """The formula this channel publishes for service `name`.

    `("nyxgpt-api", "rc", "3.0.0rc4")` -> `nyxgpt-api@3.0.0rc`. The suffix is
    what keeps an RC off the stable formula: they are different formulas in
    the same tap, not two versions of one. It carries the release line, so a
    candidate for 3.0.0 can never silently become a candidate for 3.1.0 --
    that is a differently named formula, installed deliberately or not at all.
    """
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}")
    if channel == "stable":
        return assert_loadable_formula_name(name)
    if not version.strip():
        raise ValueError("The rc channel needs the candidate's version to name its formula")
    return assert_loadable_formula_name(f"{name}@{release_line(version)}rc")


def formula_class_name(formula: str) -> str:
    """Homebrew's class name for a formula file name.

    Homebrew derives the class from the file name (`Formulary.class_s`):
    separators camel-case (`nyxgpt-api` -> `NyxgptApi`), `+` becomes `x`,
    and `@` becomes `AT` **only when a digit follows** -- `python@3.12` is
    `PythonAT312` and `nyxgpt-api@3.0.0rc` is `NyxgptApiAT300rc`, but
    `nyxgpt-api@rc` is `NyxgptApi@rc`, which is why that name is rejected by
    `assert_loadable_formula_name` rather than stamped. Getting this wrong
    makes `brew install` fail to load the formula at all.
    """
    class_name = formula[:1].upper() + formula[1:].lower()
    class_name = _SEPARATOR_RE.sub(lambda match: match.group(1).upper(), class_name)
    class_name = class_name.replace("+", "x")
    return _AT_VERSION_RE.sub(lambda match: f"{match.group(1)}AT{match.group(2)}", class_name, 1)


def has_build_shim(formula_text: str) -> bool:
    """True if this formula embeds the mac_ver build shim (the api one does)."""
    return _SHIM_BEGIN in formula_text and _SHIM_END in formula_text


def extract_build_shim(formula_text: str) -> str:
    """Return the `sitecustomize.py` source an api formula writes at build time.

    The shim is Python nested inside a Ruby heredoc, so no linter, formatter
    or import in this repo ever looks at it -- a typo there would first be
    heard about from a failed `brew install` on someone's Mac, which is
    exactly how #3753 reached owner acceptance twice. Pulling the source back
    out gives the release build something to compile (`validate_build_shim`),
    the unit suite something to execute, and the macOS smoke job something to
    inject a fault into, all from the single copy the formula ships.
    """
    lines = formula_text.splitlines(keepends=True)
    begin = next((index for index, line in enumerate(lines) if _SHIM_BEGIN in line), None)
    end = next((index for index, line in enumerate(lines) if _SHIM_END in line), None)
    if begin is None or end is None or end < begin:
        raise ValueError(
            f"formula does not embed a build shim delimited by {_SHIM_BEGIN!r} / {_SHIM_END!r}"
        )
    # Ruby's `<<~` strips the heredoc indentation at runtime; the file on disk
    # still carries it, so the extracted source has to be dedented to be the
    # Python that actually lands in the build environment.
    return textwrap.dedent("".join(lines[begin : end + 1]))


def validate_build_shim(formula_text: str, formula: str) -> None:
    """Refuse to publish a formula whose embedded shim is not valid Python.

    A no-op for formulas that embed no shim (`nyxgpt-web` builds with npm and
    never starts a Python interpreter).
    """
    if not has_build_shim(formula_text):
        return
    try:
        compile(extract_build_shim(formula_text), f"<{formula} build shim>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"{formula}: the embedded build shim is not valid Python: {exc}") from exc


def render_rc_formula(template_text: str, name: str, version: str) -> str:
    """Turn a stamped stable formula into its release-candidate counterpart.

    Derived from the same template rather than a parallel copy, so the two
    channels can never drift about what the keg actually installs -- only
    the class name, the description and the conflict declaration differ.
    """
    rc_formula = formula_name(name, "rc", version)
    text, substitutions = _CLASS_RE.subn(
        f"class {formula_class_name(rc_formula)} < Formula", template_text, count=1
    )
    if substitutions != 1:
        raise ValueError(f"{name}: template has no `class ... < Formula` line to rename")

    text = text.replace('desc "', f'desc "Release candidate {version} -- ', 1)

    match = _LICENSE_RE.search(text)
    if match is None:
        raise ValueError(f"{name}: template has no `license` line to anchor conflicts_with to")
    indent = match.group(1)
    # The counterpart is derived, never spelled by hand: it is exactly the
    # formula the stable channel of *this* script stamps, so the reference can
    # never dangle at a name nothing publishes.
    stable = formula_name(name, "stable")
    block = "\n\n" + "\n".join(
        [
            f"{indent}# Acceptance-only channel (#3727): `brew install {stable}` must always",
            f"{indent}# resolve to the latest stable release, so this is a separate formula",
            f"{indent}# rather than a newer version of that one. Installing both would fight",
            f"{indent}# over the same bin wrapper and the same brew service name.",
            f"{indent}#",
            f"{indent}# A tap that does not (yet) carry {stable}.rb makes brew warn that the",
            f"{indent}# conflict names an unknown formula, and warn is all it does -- the",
            f"{indent}# install proceeds (#3753). The declaration is deliberately kept",
            f"{indent}# unconditional: Homebrew resolves conflicts_with at load time with no",
            f"{indent}# way to make one tolerant, and dropping it to silence a warning would",
            f"{indent}# trade a cosmetic message for the silent channel clobber it prevents.",
            f'{indent}conflicts_with "{stable}",',
            f'{indent}  because: "both install the same {stable} wrapper and brew service"',
        ]
    )
    rest = text[match.end() :]
    if not rest.startswith("\n\n"):
        # The template puts `depends_on` straight after `license` -- keep the
        # blank line the inserted block needs to read as its own stanza.
        block += "\n"
    return text[: match.end()] + block + rest


def build(
    version: str,
    out_dir: Path,
    base_url: str,
    channel: str = "stable",
    source_root: str | Path | None = None,
    tarballs_from: str | Path | None = None,
) -> list[Path]:
    """Build both tarballs and stamp both formulas for `channel`.

    `source_root` is the checkout the service source is vendored from,
    defaulting to this one; the templates are always this checkout's, which
    is what lets a pre-tooling tag be published at all (#3737).

    `tarballs_from` stamps against tarballs that are already published
    instead of building any (#3763) -- see the module docstring for why a
    rebuild cannot be substituted for the served bytes. The two are mutually
    exclusive: there is nothing to vendor when the tarballs are given.
    """
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}")
    if channel == "stable":
        assert_release_version(version)
    if source_root is not None and tarballs_from is not None:
        raise ValueError(
            "--source-root and --tarballs-from are mutually exclusive -- no source is "
            "vendored when the published tarballs are what the formulas are stamped against"
        )
    src_root = resolve_source_root(source_root)
    published = resolve_published_tarballs(tarballs_from, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")
    if src_root is not None:
        # Worth a line in the CI log: a backfill run's whole correctness is
        # "these tarballs are the tag's source, these templates are not".
        print(f"vendoring {version} service source from {src_root} (templates from {REPO_ROOT})")
    if published is not None:
        print(f"stamping {version} against the already-published tarballs in {published}")

    written: list[Path] = []
    for name in _FORMULAS:
        if published is None:
            tarball = build_release_dist_tarball(name, version, out_dir, src_root)
        else:
            # Copied into the layout a build would have produced, so every
            # downstream consumer (the workflow's existence guard, the
            # uploaded artifact) reads one place whichever mode ran.
            dist_dir = out_dir / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            tarball = dist_dir / f"{name}-{version}.tar.gz"
            if (published / tarball.name).resolve() != tarball.resolve():
                shutil.copyfile(published / tarball.name, tarball)
        sha256 = _sha256_file(tarball)
        template_path = REPO_ROOT / "homebrew" / "tap" / f"{name}.rb.tmpl"
        stamped = (
            template_path.read_text(encoding="utf-8")
            .replace("__URL__", f"{base_url}/{tarball.name}")
            .replace("__SHA256__", sha256)
            .replace("__VERSION__", version)
        )
        if channel == "rc":
            stamped = render_rc_formula(stamped, name, version)
        validate_build_shim(stamped, name)
        formula_path = out_dir / f"{formula_name(name, channel, version)}.rb"
        formula_path.write_text(stamped, encoding="utf-8")
        written.append(formula_path)
        print(f"{name}: {tarball} (sha256={sha256}) -> {formula_path}")

    return written


_USAGE = (
    "usage: {prog} VERSION OUT_DIR BASE_URL [--channel rc] "
    "[--source-root DIR | --tarballs-from DIR]\n"
    "       {prog} --asset-tag VERSION"
)


def _take_option(args: list[str], flag: str) -> str | None:
    """Pop `--flag VALUE` out of `args`, or None if the flag is absent.

    Returns the empty string for a flag given without a value, which the
    caller reports as a usage error.
    """
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        del args[index:]
        return ""
    value = args[index + 1]
    del args[index : index + 2]
    return value


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    asset_tag_for = _take_option(args, "--asset-tag")
    channel = _take_option(args, "--channel")
    source_root = _take_option(args, "--source-root")
    tarballs_from = _take_option(args, "--tarballs-from")
    channel = "stable" if channel is None else channel

    if asset_tag_for is not None:
        # Query mode: print where this version's tarballs are published and
        # exit. The tap job needs that tag before anything is built, because
        # it is what the formulas' `url` is stamped with.
        if not asset_tag_for.strip() or args:
            print(_USAGE.format(prog=argv[0]), file=sys.stderr)
            return 2
        try:
            print(asset_release_tag(asset_tag_for))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0

    if len(args) != 3 or channel not in CHANNELS or source_root == "" or tarballs_from == "":
        print(_USAGE.format(prog=argv[0]), file=sys.stderr)
        return 2

    version, out_dir_arg, base_url = args
    try:
        build(version, Path(out_dir_arg), base_url, channel, source_root, tarballs_from)
    except ValueError as exc:
        # Guardrail failures (a non-release version on the stable channel, a
        # --source-root that is not a checkout) are operator errors, not
        # crashes -- say what is wrong without a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
