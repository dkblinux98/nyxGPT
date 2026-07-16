from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _resolve_within_root(path: Path, root: Path | None) -> tuple[Path, str | None]:
    """Resolve `path`, optionally confining it to `root`.

    Returns (resolved_path, error). `error` is set if `root` is given and the
    resolved path is not `root` itself or a descendant of it.
    """
    try:
        p = path.expanduser().resolve()
    except Exception:
        p = path.expanduser()

    if root is not None:
        root_resolved = root.expanduser().resolve()
        if p != root_resolved and root_resolved not in p.parents:
            return p, f"Path escapes allowed root {root_resolved}: {p}"

    return p, None


def ls(path: Path, *, root: Path | None = None) -> int:
    p, err = _resolve_within_root(path, root)
    if err:
        print(err, file=sys.stderr)
        return 1

    if not p.exists():
        print(f"No such path: {p}", file=sys.stderr)
        return 1

    if p.is_file():
        print(str(p))
        return 0

    try:
        entries = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except Exception as err:
        print(f"ERROR: cannot list {p}: {err}", file=sys.stderr)
        return 1

    for entry in entries:
        suffix = "/" if entry.is_dir() else ""
        print(f"{entry.name}{suffix}")

    return 0


def cat(
    path: Path, *, head: int | None = None, tail: int | None = None, root: Path | None = None
) -> int:
    p, err = _resolve_within_root(path, root)
    if err:
        print(err, file=sys.stderr)
        return 1

    if not p.exists() or not p.is_file():
        print(f"No such file: {p}", file=sys.stderr)
        return 1

    if head is not None and tail is not None:
        print("ERROR: use only one of --head or --tail", file=sys.stderr)
        return 2

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"ERROR: cannot read {p}: {e}", file=sys.stderr)
        return 1

    lines = text.splitlines()

    if head is not None:
        lines = lines[: max(0, head)]
    elif tail is not None:
        lines = lines[-max(0, tail) :]

    for line in lines:
        print(line)

    return 0


def grep(pattern: str, path: Path, *, max_matches: int = 50, root: Path | None = None) -> int:
    p, err = _resolve_within_root(path, root)
    if err:
        print(err, file=sys.stderr)
        return 1

    if not p.exists():
        print(f"No such path: {p}", file=sys.stderr)
        return 1

    try:
        rx = re.compile(pattern)
    except re.error as e:
        print(f"Invalid regex: {e}", file=sys.stderr)
        return 2

    files: list[Path] = []
    if p.is_file():
        files = [p]
    else:
        # Walk directory, avoid hidden directories/files
        for walk_root, dirs, filenames in os.walk(p):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                files.append(Path(walk_root) / fn)

    matches = 0
    for f in files:
        if matches >= max_matches:
            break
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                print(f"{f}:{i}: {line}")
                matches += 1
                if matches >= max_matches:
                    break

    if matches == 0:
        return 1

    return 0


__all__ = ["ls", "cat", "grep"]
