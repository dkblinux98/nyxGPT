#!/usr/bin/env bash
# One source for "what version does this sdist's vendored pyproject.toml
# DECLARE?" -- the #3850 question, asked by three steps of
# `.github/workflows/macos-brew-smoke.yml`.
#
# Why it matters: the keg's own `nyxgpt --version` and
# `ops._native_service_version` read that metadata, so a tarball named
# `nyxgpt-api-3.0.0rc13.tar.gz` whose vendored pyproject says `3.0.0` makes
# the keg claim the stable release and `nyxgpt up` reconcile the stable
# channel. `release_tarball._vendor_pyproject` stamps it; these steps check
# the stamp on the tarballs the runner just built.
#
# Why it is a script and not three copies of a shell function (#4183, D-066):
# the three sites made the same decision independently and all three made it
# the same wrong way --
#
#     tar -xzOf "$tgz" "$member" | awk -F'"' '/^version =/{print $2; exit}'
#
# `awk`'s `exit` closes the pipe while `tar` still has the rest of the member
# to write, `tar` takes EPIPE, and `set -o pipefail` hands that failure to the
# command substitution: the step dies HAVING READ the right version. It
# reddened #4192's own PR head (run 39679850952, `candidate-upgrade` job) and
# is a race on how much of the member is left, so the colour said nothing
# about the commit either way. Correcting three copies would leave three
# places for the next divergence.
#
# The reader here holds the first match in a variable and prints it from
# `END`, which prints what `{print; exit}` printed, having drained the input.
# `scripts/agents/lib/sigpipe_pipeline_guard.py` enforces that tree-wide and
# scans this file too.
#
# Usage:
#   tarball_declared_version.sh TARBALL [MEMBER]
#
# MEMBER defaults to `<tarball basename minus .tar.gz>/pyproject.toml`, which
# is what `python -m build --sdist` lays out. Exits 1 with a diagnosis if the
# tarball has no version line at all, so a tarball that declares nothing
# cannot pass quietly as "the version is empty".
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $(basename "$0") TARBALL [MEMBER]" >&2
  exit 2
fi

TARBALL="$1"
if [[ ! -f "$TARBALL" ]]; then
  echo "error: no such tarball: ${TARBALL}" >&2
  exit 1
fi

if [[ $# -eq 2 ]]; then
  MEMBER="$2"
else
  base="$(basename "$TARBALL")"
  MEMBER="${base%.tar.gz}/pyproject.toml"
fi

# `!declared` keeps the FIRST `version =` -- a pyproject may carry later ones
# under `[tool.*]` tables -- and `END` prints it after the input is drained.
version="$(
  tar -xzOf "$TARBALL" "$MEMBER" \
    | awk -F'"' '!declared && /^version[[:space:]]*=/ {declared=$2} END {print declared}'
)"

if [[ -z "$version" ]]; then
  echo "error: ${TARBALL}'s ${MEMBER} declares no version (#3850)" >&2
  exit 1
fi

printf '%s\n' "$version"
