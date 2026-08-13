#!/usr/bin/env python3
"""Build remote-Homebrew-tap release artifacts for a given version (#3622).

Builds the `nyxgpt-api`/`nyxgpt-web` vendored source tarballs (the same
tarball `nyxgpt.ops.build_release_dist_tarball` / the local file:// tap
flow builds), then stamps `homebrew/tap/*.rb.tmpl` with the tarballs'
real url/sha256/version to produce ready-to-publish formula files.

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

Usage:
    python scripts/build_homebrew_artifacts.py VERSION OUT_DIR BASE_URL \
        [--channel rc] [--source-root DIR]

    VERSION   e.g. 2.1.0 (or 3.0.0rc4 with --channel rc)
    OUT_DIR   directory to write tarballs + stamped formulas into
    BASE_URL  download URL prefix the tarballs will be published under,
              e.g. https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0
              (formula `url` becomes "<BASE_URL>/<tarball filename>")
    --channel stable (default) or rc -- see above
    --source-root  checkout to vendor the service source from (default: this
              checkout) -- see above
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nyxgpt.ops import _sha256_file, build_release_dist_tarball  # noqa: E402

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
    block = "\n\n" + "\n".join(
        [
            f"{indent}# Acceptance-only channel (#3727): `brew install {name}` must always",
            f"{indent}# resolve to the latest stable release, so this is a separate formula",
            f"{indent}# rather than a newer version of that one. Installing both would fight",
            f"{indent}# over the same bin wrapper and the same brew service name.",
            f'{indent}conflicts_with "{name}",',
            f'{indent}  because: "both install the same {name} wrapper and brew service"',
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
) -> list[Path]:
    """Build both tarballs and stamp both formulas for `channel`.

    `source_root` is the checkout the service source is vendored from,
    defaulting to this one; the templates are always this checkout's, which
    is what lets a pre-tooling tag be published at all (#3737).
    """
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}")
    if channel == "stable":
        assert_release_version(version)
    src_root = resolve_source_root(source_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")
    if src_root is not None:
        # Worth a line in the CI log: a backfill run's whole correctness is
        # "these tarballs are the tag's source, these templates are not".
        print(f"vendoring {version} service source from {src_root} (templates from {REPO_ROOT})")

    written: list[Path] = []
    for name in _FORMULAS:
        tarball = build_release_dist_tarball(name, version, out_dir, src_root)
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
        formula_path = out_dir / f"{formula_name(name, channel, version)}.rb"
        formula_path.write_text(stamped, encoding="utf-8")
        written.append(formula_path)
        print(f"{name}: {tarball} (sha256={sha256}) -> {formula_path}")

    return written


_USAGE = "usage: {prog} VERSION OUT_DIR BASE_URL [--channel rc] [--source-root DIR]"


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
    channel = _take_option(args, "--channel")
    source_root = _take_option(args, "--source-root")
    channel = "stable" if channel is None else channel

    if len(args) != 3 or channel not in CHANNELS or source_root == "":
        print(_USAGE.format(prog=argv[0]), file=sys.stderr)
        return 2

    version, out_dir_arg, base_url = args
    try:
        build(version, Path(out_dir_arg), base_url, channel, source_root)
    except ValueError as exc:
        # Guardrail failures (a non-release version on the stable channel, a
        # --source-root that is not a checkout) are operator errors, not
        # crashes -- say what is wrong without a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
