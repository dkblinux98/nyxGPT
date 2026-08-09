#!/usr/bin/env python3
"""Build remote-Homebrew-tap release artifacts for a given version (#3622).

Builds the `nyxgpt-api`/`nyxgpt-web` vendored source tarballs (the same
tarball `nyxgpt.ops.build_release_dist_tarball` / the local file:// tap
flow builds), then stamps `homebrew/tap/*.rb.tmpl` with the tarballs'
real url/sha256/version to produce ready-to-publish formula files.

Run from a repo checkout (CI's release-artifacts.yml job) -- this is a
release-tooling script, not part of the installed package, so it's exempt
from the REPO_ROOT self-containment boundary #3621 drew around
`nyxgpt ops install`/`up` (see tests/unit/test_repo_root_allowlist.py).

Usage:
    python scripts/build_homebrew_artifacts.py VERSION OUT_DIR BASE_URL

    VERSION   e.g. 2.1.0
    OUT_DIR   directory to write tarballs + stamped formulas into
    BASE_URL  download URL prefix the tarballs will be published under,
              e.g. https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0
              (formula `url` becomes "<BASE_URL>/<tarball filename>")
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nyxgpt.ops import _sha256_file, build_release_dist_tarball  # noqa: E402

_FORMULAS = ("nyxgpt-api", "nyxgpt-web")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(f"usage: {argv[0]} VERSION OUT_DIR BASE_URL", file=sys.stderr)
        return 2
    _, version, out_dir_arg, base_url = argv
    out_dir = Path(out_dir_arg)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")

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
        formula_path = out_dir / f"{name}.rb"
        formula_path.write_text(stamped, encoding="utf-8")
        print(f"{name}: {tarball} (sha256={sha256}) -> {formula_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
