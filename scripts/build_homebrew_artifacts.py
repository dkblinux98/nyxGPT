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
* ``rc`` stamps **separate** `nyxgpt-api@rc.rb` / `nyxgpt-web@rc.rb`
  formulas from the same templates, for acceptance-testing an unreleased
  release candidate. Homebrew has no pre-release semantics, so channel
  separation lives in the formula *names*: an rc publish therefore never
  writes a stable formula file at all, and `brew install nyxgpt-api` keeps
  resolving to the latest stable release no matter how many RCs are cut.
  The `@rc` formulas declare `conflicts_with` their stable counterparts, so
  switching channels on one machine is an explicit uninstall rather than a
  silent clobber.

Run from a repo checkout (CI's release-artifacts.yml / release-publish-pypi.yml
jobs) -- this is a release-tooling script, not part of the installed package,
so it's exempt from the REPO_ROOT self-containment boundary #3621 drew around
`nyxgpt ops install`/`up` (see tests/unit/test_repo_root_allowlist.py).

Usage:
    python scripts/build_homebrew_artifacts.py VERSION OUT_DIR BASE_URL [--channel rc]

    VERSION   e.g. 2.1.0 (or 3.0.0rc4 with --channel rc)
    OUT_DIR   directory to write tarballs + stamped formulas into
    BASE_URL  download URL prefix the tarballs will be published under,
              e.g. https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0
              (formula `url` becomes "<BASE_URL>/<tarball filename>")
    --channel stable (default) or rc -- see above
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

#: Formula-name suffix per channel. Empty for stable -- the stable formulas
#: are the ones `brew install nyxgpt-api` resolves, and nothing but a stable
#: release may ever write them.
_CHANNEL_SUFFIX = {"stable": "", "rc": "@rc"}

_CLASS_RE = re.compile(r"^class\s+(\w+)\s+<\s+Formula\b", re.MULTILINE)

_LICENSE_RE = re.compile(r"^([ \t]*)license\s+\"[^\"]*\"[ \t]*$", re.MULTILINE)


def formula_name(name: str, channel: str) -> str:
    """The formula this channel publishes for service `name`.

    `("nyxgpt-api", "rc")` -> `nyxgpt-api@rc`. The suffix is what keeps an RC
    off the stable formula: they are different formulas in the same tap, not
    two versions of one.
    """
    if channel not in _CHANNEL_SUFFIX:
        raise ValueError(f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}")
    return f"{name}{_CHANNEL_SUFFIX[channel]}"


def formula_class_name(formula: str) -> str:
    """Homebrew's class name for a formula file name.

    Homebrew derives the class from the file name (`Formulary.class_s`):
    hyphens camel-case, and `@` becomes a literal `AT` -- `python@3.12` is
    `PythonAT312`, so `nyxgpt-api@rc` is `NyxgptApiATRc`. Getting this wrong
    makes `brew install` fail to load the formula at all.
    """
    base, _, tag = formula.partition("@")
    class_name = "".join(part.capitalize() for part in base.split("-"))
    if tag:
        class_name += "AT" + "".join(part.capitalize() for part in re.split(r"[-.]", tag))
    return class_name


def render_rc_formula(template_text: str, name: str) -> str:
    """Turn a stamped stable formula into its `@rc` counterpart.

    Derived from the same template rather than a parallel copy, so the two
    channels can never drift about what the keg actually installs -- only
    the class name, the description and the conflict declaration differ.
    """
    rc_formula = formula_name(name, "rc")
    text, substitutions = _CLASS_RE.subn(
        f"class {formula_class_name(rc_formula)} < Formula", template_text, count=1
    )
    if substitutions != 1:
        raise ValueError(f"{name}: template has no `class ... < Formula` line to rename")

    text = text.replace('desc "', 'desc "Release candidate: ', 1)

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


def build(version: str, out_dir: Path, base_url: str, channel: str = "stable") -> list[Path]:
    """Build both tarballs and stamp both formulas for `channel`."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")

    written: list[Path] = []
    for name in _FORMULAS:
        tarball = build_release_dist_tarball(name, version, out_dir)
        sha256 = _sha256_file(tarball)
        template_path = REPO_ROOT / "homebrew" / "tap" / f"{name}.rb.tmpl"
        stamped = (
            template_path.read_text(encoding="utf-8")
            .replace("__URL__", f"{base_url}/{tarball.name}")
            .replace("__SHA256__", sha256)
            .replace("__VERSION__", version)
        )
        if channel == "rc":
            stamped = render_rc_formula(stamped, name)
        formula_path = out_dir / f"{formula_name(name, channel)}.rb"
        formula_path.write_text(stamped, encoding="utf-8")
        written.append(formula_path)
        print(f"{name}: {tarball} (sha256={sha256}) -> {formula_path}")

    return written


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    channel = "stable"
    if "--channel" in args:
        index = args.index("--channel")
        if index + 1 >= len(args):
            print(f"usage: {argv[0]} VERSION OUT_DIR BASE_URL [--channel rc]", file=sys.stderr)
            return 2
        channel = args[index + 1]
        del args[index : index + 2]
    if len(args) != 3 or channel not in CHANNELS:
        print(f"usage: {argv[0]} VERSION OUT_DIR BASE_URL [--channel rc]", file=sys.stderr)
        return 2

    version, out_dir_arg, base_url = args
    build(version, Path(out_dir_arg), base_url, channel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
