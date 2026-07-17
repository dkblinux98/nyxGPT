"""Filesystem tools (`ls`, `cat`, `grep`) shared by the CLI and agent tool layer.

Each function operates on a path optionally confined to a `root` directory
(via `_resolve_within_root`) so that agent-invoked filesystem access can be
sandboxed, and returns a process-style exit code (0 success, non-zero
failure) while writing its output to stdout/stderr.
"""

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
    """List `path`: prints its contents if a directory, or its own name if a file.

    Args:
        path: File or directory to list.
        root: If given, `path` must resolve to `root` or a descendant of it.

    Returns:
        0 on success, 1 if `path` escapes `root`, doesn't exist, or can't be listed.
    """
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
    """Print the contents of a text file, optionally limited to its first/last lines.

    Args:
        path: File to read.
        head: If given, print only the first `head` lines.
        tail: If given, print only the last `tail` lines. Mutually exclusive with `head`.
        root: If given, `path` must resolve to `root` or a descendant of it.

    Returns:
        0 on success; 1 if the path escapes `root`, isn't a file, or can't be
        read; 2 if both `head` and `tail` are given.
    """
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
    """Search `path` for lines matching a regex, printing `file:line: text` for each hit.

    If `path` is a directory, it is walked recursively, skipping hidden
    files/directories (names starting with `.`).

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search.
        max_matches: Maximum number of matches to print before stopping.
        root: If given, `path` must resolve to `root` or a descendant of it.

    Returns:
        0 if at least one match was found; 1 if none were found, the path
        escapes `root`, or the path doesn't exist; 2 if `pattern` is invalid.
    """
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
