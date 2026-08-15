#!/usr/bin/env python3
"""Prove a service venv is built on a Python that satisfies requires-python (#3782).

On the rc9 cloud round `nyxgpt ops install` created the `nyxgpt-api` venv with
bare `python3 -m venv`. On Amazon Linux 2023 that is Python 3.9, so pip refused
the artifact -- "requires a different Python: 3.9.16 not in '>=3.11'" -- and the
deploy died on an opaque `pip install ... (rc=1)` several minutes in.

The unit tests for the selection logic mock `_which`/`_run`, so they show the
decision but never the thing that actually went wrong: a real old interpreter,
a real venv built from it, and real pip refusing a real artifact. This script
runs that on the machine it is invoked on, with a genuinely too-old Python
supplied by the caller.

Four halves, all executed:

  1. Fault injection -- the pre-fix line verbatim (`<old python> -m venv`),
     then `pip install` of the real sdist into it. It MUST fail with pip's
     requires-python refusal; if it succeeds the old interpreter is not
     actually old and the rest of this check would prove nothing (#3753).
  2. The Amazon Linux 2023 shape -- `python3` on PATH is the old one while
     ops itself runs on a qualifying interpreter. `_create_service_venv` must
     pick a >= 3.11 one and the same `pip install` must then SUCCEED.
  3. ops itself on a too-old interpreter -- selection must fall through to an
     explicitly-versioned `python3.X` on PATH.
  4. Nothing qualifies -- the step must fail naming the found and required
     versions, instead of building a doomed venv.

Usage:
    python3 scripts/service-venv-python-smoke.py \\
        --old-python /path/to/python3.9 --sdist dist/nyxgpt-*.tar.gz

Exit 0 = all four held.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from nyxgpt import ops

# pip's refusal wording has been stable across many releases; match on the
# part that is the diagnosis rather than on exact punctuation.
_REFUSAL_MARKER = "requires a different Python"


def _fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def _interpreter_version(exe: str) -> tuple[int, int]:
    out = subprocess.run(
        [exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    major, minor = out.split(".")
    return (int(major), int(minor))


def _pip_install(venv_dir: Path, sdist: Path) -> subprocess.CompletedProcess[str]:
    """Install `sdist` into `venv_dir`, deps excluded.

    `--no-deps` keeps the run to seconds: pip checks `Requires-Python` from the
    sdist's metadata before it resolves anything, so the refusal this check is
    about happens either way.
    """
    return subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", str(sdist)],
        capture_output=True,
        text=True,
    )


def _venv_python_version(venv_dir: Path) -> tuple[int, int]:
    return _interpreter_version(str(venv_dir / "bin" / "python"))


def _path_with(entries: dict[str, str], workdir: Path, name: str) -> str:
    """Build a PATH holding only `entries` (name -> real interpreter)."""
    bindir = workdir / name
    bindir.mkdir()
    for link_name, target in entries.items():
        (bindir / link_name).symlink_to(target)
    return str(bindir)


def _reproduce_the_defect(old_python: str, sdist: Path, workdir: Path) -> int:
    """Half 1: the pre-fix implementation, run through this same harness.

    Not a paraphrase of the defect -- the literal line #3782 was filed
    against, `_run(["python3", "-m", "venv", ...])`, with the injected PATH
    deciding what `python3` means. Everything downstream (the venv, the pip
    run, the artifact) is real, so this is what "it fails without the fix"
    means for this job: were `_create_service_venv` to regress to resolving
    bare `python3`, half 2 below would fail exactly as this one does.
    """
    path = _path_with({"python3": old_python}, workdir, "bin-prefix")
    venv_dir = workdir / "venv-prefix"
    previous = os.environ["PATH"]
    os.environ["PATH"] = path
    try:
        subprocess.run(["python3", "-m", "venv", str(venv_dir)], check=True)
    finally:
        os.environ["PATH"] = previous

    built_on = _venv_python_version(venv_dir)
    cp = _pip_install(venv_dir, sdist)
    if cp.returncode == 0:
        return _fail(
            f"the artifact installed cleanly into a Python {built_on[0]}.{built_on[1]} venv -- "
            "the scenario did not reproduce, so this check proves nothing"
        )
    output = f"{cp.stdout}\n{cp.stderr}"
    if _REFUSAL_MARKER not in output:
        return _fail(f"pip failed for some other reason:\n{output.strip()}")
    print(f"--- pre-fix: `python3 -m venv` -> Python {built_on[0]}.{built_on[1]}, then pip ---")
    print(next(ln for ln in output.splitlines() if _REFUSAL_MARKER in ln))
    print("PASS (1/4): resolving bare `python3` builds a venv the artifact is refused by")
    return 0


def _selects_over_an_old_python3(sdist: Path, workdir: Path, old_python: str) -> int:
    """Half 2: `python3` on PATH is 3.9, ops runs on a qualifying interpreter."""
    path = _path_with({"python3": old_python}, workdir, "bin-al2023")
    venv_dir = workdir / "venv-al2023"
    previous = os.environ["PATH"]
    os.environ["PATH"] = path
    try:
        result = ops._create_service_venv(venv_dir, "nyxgpt-api")
    finally:
        os.environ["PATH"] = previous

    if not result.ok:
        return _fail(f"no venv was created at all: {result.message}\n{result.details}")
    version = _venv_python_version(venv_dir)
    if version < ops._MIN_SERVICE_PYTHON:
        return _fail(f"the venv was built on Python {version[0]}.{version[1]} -- below the floor")
    cp = _pip_install(venv_dir, sdist)
    if cp.returncode != 0:
        return _fail(f"the artifact still would not install:\n{cp.stdout}\n{cp.stderr}")
    print(f"--- with the fix: {result.message} ({result.details}) ---")
    print("PASS (2/4): the artifact installs into the selected venv")
    return 0


def _falls_through_to_a_versioned_python(workdir: Path, old_python: str) -> int:
    """Half 3: ops itself too old -- an explicit `python3.X` on PATH must win."""
    new_python = sys.executable
    new_version = _interpreter_version(new_python)
    versioned = f"python3.{new_version[1]}"
    if versioned not in ops._SERVICE_PYTHON_NAMES:
        return _fail(
            f"this smoke runs on Python {new_version[0]}.{new_version[1]}, which "
            f"`ops._SERVICE_PYTHON_NAMES` does not look for -- add {versioned!r} to it"
        )

    path = _path_with({"python3": old_python, versioned: new_python}, workdir, "bin-oldops")
    venv_dir = workdir / "venv-oldops"
    previous_path = os.environ["PATH"]
    original = ops._running_interpreter
    os.environ["PATH"] = path
    ops._running_interpreter = lambda: (old_python, _interpreter_version(old_python))
    try:
        result = ops._create_service_venv(venv_dir, "nyxgpt-api")
    finally:
        ops._running_interpreter = original
        os.environ["PATH"] = previous_path

    if not result.ok:
        return _fail(f"selection gave up instead of using {versioned}: {result.details}")
    version = _venv_python_version(venv_dir)
    if version < ops._MIN_SERVICE_PYTHON:
        return _fail(f"selection picked Python {version[0]}.{version[1]} -- below the floor")
    print(f"--- ops on an old interpreter: {result.message} ({result.details}) ---")
    print("PASS (3/4): selection fell through to a versioned python on PATH")
    return 0


def _fails_clearly_when_nothing_qualifies(workdir: Path, old_python: str) -> int:
    """Half 4: no qualifying interpreter anywhere -- name it, don't build it."""
    path = _path_with({"python3": old_python}, workdir, "bin-nothing")
    venv_dir = workdir / "venv-nothing"
    previous_path = os.environ["PATH"]
    original = ops._running_interpreter
    old_version = _interpreter_version(old_python)
    os.environ["PATH"] = path
    ops._running_interpreter = lambda: (old_python, old_version)
    try:
        result = ops._create_service_venv(venv_dir, "nyxgpt-api")
    finally:
        ops._running_interpreter = original
        os.environ["PATH"] = previous_path

    if result.ok:
        return _fail("a venv was created from an interpreter below the floor")
    if venv_dir.exists():
        return _fail(f"a doomed venv was left behind at {venv_dir}")
    found = f"{old_version[0]}.{old_version[1]}"
    required = ".".join(str(p) for p in ops._MIN_SERVICE_PYTHON)
    report = f"{result.message}\n{result.details}"
    if found not in report or required not in report:
        return _fail(
            f"the failure names neither the found ({found}) nor required ({required}):\n{report}"
        )
    print("--- nothing qualifies ---")
    print(report)
    print("PASS (4/4): the step failed naming the found and required versions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--old-python",
        required=True,
        help="path to a real interpreter below nyxGPT's requires-python floor",
    )
    parser.add_argument("--sdist", required=True, help="path to a built nyxGPT sdist")
    args = parser.parse_args()

    sdist = Path(args.sdist).resolve()
    if not sdist.is_file():
        return _fail(f"no such sdist: {sdist}")

    old_python = str(Path(args.old_python).resolve())
    old_version = _interpreter_version(old_python)
    if old_version >= ops._MIN_SERVICE_PYTHON:
        return _fail(
            f"--old-python is Python {old_version[0]}.{old_version[1]}, which already "
            "satisfies the floor -- this check needs a genuinely too-old interpreter"
        )
    print(f"old interpreter: {old_python} (Python {old_version[0]}.{old_version[1]})")
    print(
        f"this interpreter: {sys.executable} (Python {sys.version_info[0]}.{sys.version_info[1]})"
    )
    print(f"artifact: {sdist.name}\n")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for check in (
            lambda: _reproduce_the_defect(old_python, sdist, workdir),
            lambda: _selects_over_an_old_python3(sdist, workdir, old_python),
            lambda: _falls_through_to_a_versioned_python(workdir, old_python),
            lambda: _fails_clearly_when_nothing_qualifies(workdir, old_python),
        ):
            rc = check()
            if rc != 0:
                return rc
            print()

    print("all four halves held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
