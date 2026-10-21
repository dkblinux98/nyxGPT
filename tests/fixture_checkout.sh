#!/usr/bin/env bash
# tests/fixture_checkout.sh -- the one place that answers "how does a test get
# a tree it may break?" for every suite in this repository (#4198).
#
# THE DEFECT THIS EXISTS FOR. `tests/test_pr_head_guard_discriminates.sh`
# proved the PR-head guard refuses nine unsafe workflow shapes by editing the
# REAL checkout: three tracked files rewritten in place and a tenth workflow
# planted under `.github/workflows/`, restored only from an `EXIT` trap. Two
# things follow, and both were observed:
#
#   * A concurrent reader sees the injected tree. A full `pytest` running in
#     another process failed `test_context_index.py::test_committed_index_is_current`
#     because the index generator globs `.github/workflows/*.yml` and saw the
#     planted file. Benign that time -- but for the length of the window the
#     checkout really does contain a workflow whose whole purpose is to hand
#     this repository's `workflow`-scoped PAT to a PR author, and anything else
#     reading the tree in that window (a parallel job, a pre-commit hook, a
#     watcher, an `act`-style runner) reads that.
#   * `kill -9`, a job timeout or a runner eviction leaves it behind. An `EXIT`
#     trap does not run on `SIGKILL` and does not run when the process group is
#     killed, so the unsafe workflow stays on disk as an untracked file and the
#     three edits stay as local modifications -- in a checkout nobody knows is
#     dirty.
#
# "An `EXIT` trap restores it" is therefore not a mitigation: it addresses
# neither half. The fixture has to live somewhere the test owns.
#
# WHY A WHOLE-TREE COPY rather than a hand-built minimal fixture. The guards
# these suites exercise read the tree the way the real thing does -- globbing
# `.github/workflows/*.yml`, walking `rglob("*")` for prose, resolving their own
# root from `__file__`, reading `pyproject.toml` for pytest's configuration. A
# fixture holding only the files one case touches would quietly change what the
# guard examines, and a guard measured against a tree that is not shaped like
# the repository is measuring something other than the repository. A copy of
# every tracked file is shaped like the repository by construction, and it is
# cheap: ~1,300 files, ~25 MB, well under a second.
#
# Tracked files only, read from the WORKING TREE (not from `HEAD`): the
# question a suite asks is about the tree in front of it, so a developer's
# uncommitted edit must be in the copy, while the untracked scratch, build
# output and `node_modules` beside it must not.
#
# Usage, from a suite:
#   source "$(dirname "${BASH_SOURCE[0]}")/fixture_checkout.sh"
#   FIXTURE="$(mktemp -d)"
#   fixture_checkout "$ROOT_DIR" "$FIXTURE"
#   trap 'rm -rf "$FIXTURE"' EXIT      # cleanup only -- nothing to restore
#
# Or as a command, which is how the Python side calls it so that the copy is
# made in exactly one place:
#   bash tests/fixture_checkout.sh <root> <dest>
#
# Exit status: 0 the copy is complete, 1 it could not be made (and nothing
# partial is left for a caller to mistake for a tree).

# Guard against double-sourcing: a suite may source this and `pytest_python.sh`
# in either order and both are idempotent.
if [[ -z "${_FIXTURE_CHECKOUT_SOURCED:-}" ]]; then
  _FIXTURE_CHECKOUT_SOURCED=1

  # Files every caller's fixture must contain for the copy to be worth running
  # a guard against. Asserted rather than assumed: a `git ls-files` that
  # silently returned nothing (wrong cwd, a `GIT_DIR` inherited from a parent,
  # a dirty `--assume-unchanged`) would otherwise produce an empty directory
  # that every guard passes over, which is the vacuous-green shape #4195 was
  # filed for.
  _FIXTURE_REQUIRED=(
    "pyproject.toml"
    ".github/workflows"
    "tests/unit"
  )

  # fixture_checkout <root> <dest>
  #
  # Copies every tracked file of the git checkout at <root> into <dest>,
  # preserving layout, so the caller may edit, delete and plant files in <dest>
  # without any other reader of <root> seeing it.
  fixture_checkout() {
    local root="${1:?fixture_checkout: <root> is required}"
    local dest="${2:?fixture_checkout: <dest> is required}"

    root="$(cd "$root" && pwd)" || return 1
    mkdir -p "$dest" || return 1
    dest="$(cd "$dest" && pwd)" || return 1

    # The fixture must not be inside the tree it copies, or the whole point is
    # lost: `rglob`/`ls-files` in the copy would reach the original, and the
    # copy itself would be the mutation of the checkout this exists to stop.
    case "$dest/" in
      "$root"/*)
        echo "fixture_checkout: dest ($dest) is inside root ($root)" >&2
        return 1
        ;;
    esac

    if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "fixture_checkout: $root is not a git checkout, so the set of" >&2
      echo "  files to copy cannot be determined. Suites that build a fixture" >&2
      echo "  copy need one; run them from a clone." >&2
      return 1
    fi

    local list
    list="$(mktemp)" || return 1

    # Skip entries git knows about but the working tree no longer has (a
    # tracked file deleted and not yet committed): `tar` would abort on them
    # and take the rest of the copy with it.
    local rel
    while IFS= read -r -d '' rel; do
      if [[ -e "$root/$rel" || -L "$root/$rel" ]]; then
        printf '%s\0' "$rel"
      fi
    done < <(git -C "$root" ls-files -z) >"$list"

    if [[ ! -s "$list" ]]; then
      echo "fixture_checkout: git ls-files listed no files in $root" >&2
      rm -f "$list"
      return 1
    fi

    # Two tars rather than `cp --parents`/`rsync`: `cp -r` cannot take a file
    # list, `--parents` is GNU-only, and rsync is not on every runner. `tar`
    # with `--null -T` is in both GNU tar and the bsdtar macOS ships.
    if ! tar -cf - -C "$root" --null -T "$list" | tar -xf - -C "$dest"; then
      echo "fixture_checkout: copying $root into $dest failed" >&2
      rm -f "$list"
      return 1
    fi
    rm -f "$list"

    local required
    for required in "${_FIXTURE_REQUIRED[@]}"; do
      if [[ ! -e "$dest/$required" ]]; then
        echo "fixture_checkout: the copy is missing $required -- a guard run" >&2
        echo "  against it would pass over an incomplete tree (#4198)." >&2
        return 1
      fi
    done
  }
fi

# Runnable as well as sourceable, so the Python side does not reimplement the
# copy. `BASH_SOURCE[0]` differs from `$0` only when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -euo pipefail
  fixture_checkout "$@"
fi
