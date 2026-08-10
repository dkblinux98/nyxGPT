"""Session persistence and management for chat conversations.

Handles reading/writing session message and metadata files on disk (with
cross-platform file locking for safe concurrent access), plus higher-level
operations used by the CLI/API: creating, renaming, pinning, tagging,
searching, merging, exporting, and batch-updating sessions.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, NotRequired, TypedDict, cast

from nyxgpt.config import (
    get_default_model,
    get_ollama_base_url,
    get_rag_enabled,
    get_session_backend,
    get_sessions_dir,
    load_config,
)
from nyxgpt.ollama_client import ollama_chat

log = logging.getLogger(__name__)


# --- File locking utilities ---
#
# LOCK ORDERING REQUIREMENT (Deadlock Prevention):
# When multiple file locks are needed, always acquire them in alphabetical
# order by file path. This ensures consistent lock ordering across all code
# paths and prevents deadlock scenarios.
#
# Example:
#   files = [session_file, meta_file]
#   files.sort(key=lambda p: p.as_posix())
#   with file_lock(files[0]), file_lock(files[1]):
#       # ... operations ...


def verify_lock_ordering(*file_paths: Path) -> None:
    """Verify that file paths are in alphabetical order (for deadlock prevention).

    This function should be called when acquiring multiple file locks to ensure
    locks are acquired in consistent alphabetical order. Raises AssertionError
    in debug mode if ordering is violated.

    Args:
        *file_paths: File paths to check (in the order they will be locked)

    Raises:
        AssertionError: If paths are not in alphabetical order (only in debug mode)

    Example:
        >>> files = [session_file, meta_file]
        >>> files.sort(key=lambda p: p.as_posix())
        >>> verify_lock_ordering(*files)  # Passes if files sorted correctly
    """
    if len(file_paths) < 2:
        return  # No ordering concerns for single lock

    # Only enforce in debug mode (__debug__ is True unless -O flag used)
    if __debug__:
        paths_str = [p.as_posix() for p in file_paths]
        sorted_paths = sorted(paths_str)
        assert paths_str == sorted_paths, (
            f"File lock ordering violation detected! "
            f"Locks must be acquired in alphabetical order to prevent deadlock.\n"
            f"Expected order: {sorted_paths}\n"
            f"Actual order:   {paths_str}"
        )


@contextmanager
def file_lock(file_path: Path, timeout: float = 5.0):
    """Cross-platform file locking context manager.

    Acquires an exclusive lock on the specified file, waiting up to `timeout`
    seconds if the file is already locked. Ensures the lock is released even
    if an exception occurs.

    Args:
        file_path: Path to the file to lock
        timeout: Maximum seconds to wait for lock acquisition (default: 5.0)

    Yields:
        File descriptor (int) of the locked file

    Raises:
        TimeoutError: If lock cannot be acquired within timeout
        OSError: If file cannot be opened or locked
        ValueError: If `file_path` resolves outside the allowed data area

    Example:
        >>> with file_lock(Path("session.json"), timeout=10.0) as fd:
        ...     # File is locked here
        ...     data = Path("session.json").read_text()
        ...     # Lock automatically released when exiting block
    """
    # Inline sink-side barrier (CodeQL py/path-injection): a lock target
    # resolving outside the allowed data roots is refused before any
    # filesystem effect. Rebind under a single-condition startswith guard
    # (disjunctions are never credited -- see PR #3657).
    real = os.path.realpath(str(file_path))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    if real.startswith(_home + os.sep):  # noqa: SIM114
        file_path = Path(real)
    elif real.startswith(_tmp + os.sep):
        file_path = Path(real)
    else:
        raise ValueError(f"Lock file resolves outside the allowed data area: {file_path!r}")
    # Open file for reading (create if doesn't exist)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomically open file, creating if needed (avoids TOCTOU race)
    fd = os.open(str(file_path), os.O_RDONLY | os.O_CREAT, 0o644)

    try:
        # Platform-specific locking
        if sys.platform == "win32":
            import msvcrt

            start_time = time.time()
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as err:
                    if time.time() - start_time > timeout:
                        raise TimeoutError(
                            f"Could not acquire lock on {file_path} within {timeout}s"
                        ) from err
                    time.sleep(0.1)
        else:
            import fcntl

            start_time = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError) as err:
                    if time.time() - start_time > timeout:
                        raise TimeoutError(
                            f"Could not acquire lock on {file_path} within {timeout}s"
                        ) from err
                    time.sleep(0.1)

        yield fd

    finally:
        # Release lock and close file
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as e:
            log.warning(f"Failed to unlock {file_path}: {e}")
        finally:
            os.close(fd)


class SessionMetadata(TypedDict):
    """Type-safe structure for session metadata.

    This provides IDE autocomplete, type checking, and documentation
    for all metadata fields stored in .meta.json files.
    """

    created_at: str  # ISO 8601 datetime
    updated_at: str  # ISO 8601 datetime
    pinned: bool
    tags: list[str]
    token_estimate: int

    # Optional fields (use NotRequired for Python 3.11+)
    title: NotRequired[str]
    summary: NotRequired[str]
    model: NotRequired[str]
    rag_enabled: NotRequired[bool]  # Per-session RAG enable/disable
    attached_doc_ids: NotRequired[list[str]]  # Force-included document IDs for RAG


# For backwards compatibility, keep dict[str, Any] in function signatures
# but document the expected structure
SessionMetaDict = dict[str, Any]


# Session names must be alphanumeric with underscores or hyphens, 1-64 chars
# This prevents path traversal and ensures filesystem compatibility
VALID_SESSION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_session_name(name: str) -> str:
    """Validate and normalize a session name.

    Session names are used as filenames, so validation is critical for:
    - Security: Prevent path traversal attacks
    - Compatibility: Ensure names work across filesystems
    - Predictability: Avoid special character issues

    **Allowed Pattern:**
    - 1-64 characters
    - Alphanumeric (a-z, A-Z, 0-9)
    - Hyphens (-)
    - Underscores (_)

    **Examples:**

        Valid: "my-session", "chat_2024", "ProjectX"
        Invalid: "my session" (space), "../etc" (path), "x"*65 (too long)

    Args:
        name: The session name to validate

    Returns:
        The validated, stripped session name

    Raises:
        ValueError: If name is invalid (with descriptive error message)
    """
    if not isinstance(name, str):
        raise ValueError("session name must be a string")

    raw = name.strip()
    if not raw:
        raise ValueError("session name cannot be empty")

    if not VALID_SESSION_NAME_PATTERN.match(raw):
        raise ValueError(
            "Session name must be 1-64 alphanumeric characters, underscores, or hyphens"
        )

    return raw


def default_sessions_dir() -> Path:
    """Return the configured sessions directory, loading global config if needed."""
    cfg = load_config(None)
    return get_sessions_dir(cfg)


def _resolve_sessions_dir(sessions_dir: Path) -> Path:
    """Validate `sessions_dir` resolves inside an allowed root, return its realpath.

    Sink-side barrier (CodeQL py/path-injection): this is the single
    resolver every sessions.py function routes its `sessions_dir` through
    before touching the filesystem, so present and future sinks in this
    module are covered by one barrier. Uses `os.path.realpath` + string-prefix
    containment against the user's home directory / system temp directory --
    the CodeQL-recognised barrier pattern also used by
    `app._sessions_dir_from_str` and `config._expand_path`.
    `Path.relative_to()` is NOT modelled as a sanitizer.

    Raises:
        ValueError: If `sessions_dir` resolves outside the allowed roots.
    """
    real = os.path.realpath(str(sessions_dir))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # Each branch below is controlled by exactly one condition: CodeQL's
    # barrier-guard analysis only credits a guard whose branch is dominated
    # by a single sanitizing comparison, never a disjunction of them.
    if real.startswith(_home + os.sep):
        return Path(real)
    if real.startswith(_tmp + os.sep):
        return Path(real)
    raise ValueError(f"sessions_dir resolves outside the allowed data area: {sessions_dir!r}")


def session_file_for(name: str, sessions_dir: Path) -> Path:
    """Return the session JSON file path for a validated session `name`."""
    if not isinstance(name, str):
        raise ValueError("session name must be a string")
    stripped = name.strip()
    # Inline `re.fullmatch` allowlist barrier (CodeQL py/path-injection sink-side
    # chokepoint): must be a direct `re.fullmatch(...)` call in this function --
    # delegating to a helper that wraps a precompiled `re.Pattern` is not
    # recognised as a sanitizer by CodeQL's py/path-injection query.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", stripped):
        raise ValueError(
            "Session name must be 1-64 alphanumeric characters, underscores, or hyphens"
        )
    resolved_dir = _resolve_sessions_dir(sessions_dir)
    candidate = resolved_dir / f"{stripped}.json"
    # Re-anchor the COMPOSED path before returning it (CodeQL
    # py/path-injection): the `re.fullmatch` name check above is not a
    # recognized barrier for this query (regex guards only credit for
    # command-line injection), so without this the name-tainted composed
    # path re-taints every caller's filesystem sink. A realpath +
    # single-condition startswith rebind here is a barrier node on every
    # caller's flow. It also refuses a session file that is a symlink
    # escaping the allowed roots.
    real = os.path.realpath(str(candidate))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    if real.startswith(_home + os.sep):
        return Path(real)
    if real.startswith(_tmp + os.sep):
        return Path(real)
    raise ValueError(f"Session file resolves outside the allowed data area: {candidate!r}")


def meta_file_for(session_file: Path) -> Path:
    """Return the `.meta.json` metadata file path paired with a session file."""
    # Inline `re.fullmatch` allowlist barrier, defense-in-depth: `session_file`
    # is expected to already be a validated `session_file_for()` result, but
    # this sink is validated independently so it is covered even if a future
    # caller derives `session_file` some other way.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_file.stem):
        raise ValueError(
            "Session file name must be 1-64 alphanumeric characters, underscores, or hyphens"
        )
    candidate = session_file.with_suffix(".meta.json")
    # Re-anchor the composed metadata path -- same chokepoint barrier as
    # session_file_for (regex checks are not credited path-injection
    # barriers, so the composed path must pass a realpath +
    # single-condition startswith rebind to deliver a clean value to
    # every caller's sink).
    real = os.path.realpath(str(candidate))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    if real.startswith(_home + os.sep):
        return Path(real)
    if real.startswith(_tmp + os.sep):
        return Path(real)
    raise ValueError(f"Metadata file resolves outside the allowed data area: {candidate!r}")


# --- Storage backend dispatch (#3590) ---
#
# Sessions are stored either as JSON files under `sessions_dir` (the legacy
# "file" backend) or in the stack's Cassandra (`[nyxgpt] session_backend =
# cassandra`, see nyxgpt.session_db). Every operation in this module funnels
# through the primitives below (load/save messages+meta, exists, list,
# delete, rename), so dispatching here covers all callers (API, CLI, chat,
# MCP). Under the DB backend the Path values produced by session_file_for()
# still act as validated name/directory tokens -- they are just never touched
# on disk.


def _use_db_backend() -> bool:
    """Return whether the Cassandra session backend is active."""
    try:
        cfg = load_config(None)
        return get_session_backend(cfg) == "cassandra"
    except Exception:
        return False


def _db_store() -> Any:
    """Return the Cassandra session store singleton (imported lazily)."""
    from nyxgpt import session_db

    return session_db.get_session_store()


def _db_name_for(path: Path) -> str:
    """Derive the session name from a session or metadata file path."""
    n = path.name
    if n.endswith(".meta.json"):
        return n[: -len(".meta.json")]
    return path.stem


def session_file_exists(session_file: Path) -> bool:
    """Backend-aware existence check for a `session_file_for()` path."""
    if _use_db_backend():
        return bool(_db_store().exists(_db_name_for(session_file)))
    return session_file.exists()


def session_exists(name: str, sessions_dir: Path | None = None) -> bool:
    """Return whether a session named `name` exists in the active backend.

    Raises:
        ValueError: If `name` is not a valid session name.
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    return session_file_exists(session_file_for(name, sessions_dir))


def session_lock(file_path: Path, timeout: float = 5.0) -> ContextManager[int]:
    """Advisory lock for a session file under the file backend.

    Under the DB backend this is a no-op: Cassandra row writes are atomic per
    partition, and a host-local file lock cannot coordinate multiple API
    instances anyway (which is the point of the DB backend).
    """
    if _use_db_backend():
        return nullcontext(-1)
    return file_lock(file_path, timeout=timeout)


def _delete_session_storage(session_file: Path, meta_file: Path) -> None:
    """Delete a session's stored data in the active backend (best effort)."""
    if _use_db_backend():
        _db_store().delete(_db_name_for(session_file))
        return
    if session_file.exists():
        session_file.unlink()
    if meta_file.exists():
        meta_file.unlink()


def iso_now() -> str:
    """Return the current local time as an ISO 8601 string (second precision)."""
    return datetime.now().isoformat(timespec="seconds")


def token_estimate_from_messages(messages: list[dict[str, str]]) -> int:
    """Estimate token count for a list of messages (~4 chars per token)."""
    # Rough estimate: ~4 chars per token (very approximate). Keeps us dependency-free.
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
    return max(1, chars // 4) if chars else 0


def load_session_messages(session_file: Path) -> list[dict[str, str]]:
    """Load and validate all messages from a session JSON file.

    Args:
        session_file: Path to the session JSON file

    Returns:
        List of valid message dicts (each with string "role" and "content").
        Returns an empty list if the file is missing, unreadable, or invalid.
    """
    if _use_db_backend():
        return _db_store().load_messages(_db_name_for(session_file))
    # Inline sink-side barrier (CodeQL py/path-injection): CodeQL does not
    # credit caller-side sanitization (session_file_for/_resolve_sessions_dir)
    # across the function boundary, so every function containing filesystem
    # sinks must guard and REBIND the received path itself -- same lesson as
    # the self_heal.py inline barriers (#3624). Contract-preserving refusal.
    # Each rebind must be dominated by exactly ONE `startswith` condition:
    # CodeQL's barrier-guard analysis never credits a disjunction of checks.
    real = os.path.realpath(str(session_file))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        session_file = Path(real)
    elif real.startswith(_tmp + os.sep):
        session_file = Path(real)
    else:
        log.warning("Refused session file outside allowed data area: %r", str(session_file))
        return []
    if not session_file.exists():
        return []

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in session file %s: %s", session_file, e)
        return []
    except OSError as e:
        log.warning("Failed to read session file %s: %s", session_file, e)
        return []

    if isinstance(data, list):
        out: list[dict[str, str]] = []
        for item in data:
            if (
                isinstance(item, dict)
                and isinstance(item.get("role"), str)
                and isinstance(item.get("content"), str)
            ):
                # Preserve all fields from storage (id, timestamp, edited_at, etc.)
                out.append(item)
        return out
    return []


def load_session_messages_paginated(
    session_file: Path,
    offset: int = 0,
    limit: int | None = None,
) -> tuple[list[dict[str, str]], int]:
    """
    Load messages with pagination support, avoiding loading entire file into memory
    when only a subset is needed.

    Args:
        session_file: Path to the session JSON file
        offset: Number of messages to skip from the start (default: 0)
        limit: Maximum number of messages to return (default: None, returns all after offset)

    Returns:
        tuple of (messages, total_count) where messages is the paginated slice
        and total_count is the total number of valid messages in the file
    """
    if _use_db_backend():
        msgs = _db_store().load_messages(_db_name_for(session_file))
        end = offset + limit if limit is not None else len(msgs)
        return (msgs[offset:end], len(msgs))
    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(session_file))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        session_file = Path(real)
    elif real.startswith(_tmp + os.sep):
        session_file = Path(real)
    else:
        log.warning("Refused session file outside allowed data area: %r", str(session_file))
        return ([], 0)
    if not session_file.exists():
        return ([], 0)

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in session file %s: %s", session_file, e)
        return ([], 0)
    except OSError as e:
        log.warning("Failed to read session file %s: %s", session_file, e)
        return ([], 0)

    if isinstance(data, list):
        # Filter valid messages first
        valid_messages: list[dict[str, str]] = []
        for item in data:
            if (
                isinstance(item, dict)
                and isinstance(item.get("role"), str)
                and isinstance(item.get("content"), str)
            ):
                valid_messages.append(item)

        total_count = len(valid_messages)

        # Apply pagination slice
        end = offset + limit if limit is not None else total_count
        paginated = valid_messages[offset:end]

        return (paginated, total_count)

    return ([], 0)


def save_session_messages(session_file: Path, messages: list[dict[str, str]]) -> None:
    """Atomically write session messages to `session_file` as JSON.

    Writes to a unique temp file first, then renames it into place, to
    avoid corrupting the file if multiple writers race or a write fails.

    Raises:
        ValueError: If `session_file` resolves outside the allowed data area
            (a write is refused loudly rather than silently dropped).
    """
    if _use_db_backend():
        _db_store().save_messages(_db_name_for(session_file), messages)
        return
    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(session_file))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        session_file = Path(real)
    elif real.startswith(_tmp + os.sep):
        session_file = Path(real)
    else:
        raise ValueError(f"Session file resolves outside the allowed data area: {session_file!r}")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file name to avoid race conditions in concurrent writes
    tmp = session_file.parent / f".{session_file.name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, session_file)


def load_session_meta(meta_file: Path) -> SessionMetaDict:
    """Load session metadata from `meta_file`, returning `{}` if absent/invalid."""
    if _use_db_backend():
        return cast(SessionMetaDict, _db_store().load_meta(_db_name_for(meta_file)))
    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(meta_file))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        meta_file = Path(real)
    elif real.startswith(_tmp + os.sep):
        meta_file = Path(real)
    else:
        log.warning("Refused metadata file outside allowed data area: %r", str(meta_file))
        return {}
    if not meta_file.exists():
        return {}

    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in metadata file %s: %s", meta_file, e)
        return {}
    except OSError as e:
        log.warning("Failed to read metadata file %s: %s", meta_file, e)
        return {}


def save_session_meta(meta_file: Path, meta: SessionMetaDict) -> None:
    """Atomically write session metadata to `meta_file` as JSON.

    Writes to a unique temp file first, then renames it into place, to
    avoid corrupting the file if multiple writers race or a write fails.

    Raises:
        ValueError: If `meta_file` resolves outside the allowed data area.
    """
    if _use_db_backend():
        _db_store().save_meta(_db_name_for(meta_file), meta)
        return
    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(meta_file))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        meta_file = Path(real)
    elif real.startswith(_tmp + os.sep):
        meta_file = Path(real)
    else:
        raise ValueError(f"Metadata file resolves outside the allowed data area: {meta_file!r}")
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file name to avoid race conditions in concurrent writes
    tmp = meta_file.parent / f".{meta_file.name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, meta_file)


def normalize_tags(tags: list[str]) -> list[str]:
    """Trim, deduplicate (case-insensitively), and alphabetically sort tags."""
    norm: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t2 = t.strip()
        if not t2:
            continue
        key = t2.lower()
        if key in seen:
            continue
        seen.add(key)
        norm.append(t2)
    return sorted(norm, key=lambda s: s.lower())


def ensure_meta_defaults(meta: SessionMetaDict, *, model: str | None = None) -> SessionMetaDict:
    """Ensure metadata has all required fields with valid defaults.

    Args:
        meta: Existing metadata dictionary (may be incomplete)
        model: Optional model name to set

    Returns:
        Metadata with all required fields populated
    """
    now = iso_now()
    if "created_at" not in meta or not isinstance(meta.get("created_at"), str):
        meta["created_at"] = now
    meta["updated_at"] = now
    if "pinned" not in meta or not isinstance(meta.get("pinned"), bool):
        meta["pinned"] = False
    if "tags" not in meta or not isinstance(meta.get("tags"), list):
        meta["tags"] = []
    else:
        meta["tags"] = normalize_tags([str(x) for x in meta["tags"]])
    if model:
        meta["model"] = model

    # Initialize rag_enabled from global config if not set
    if "rag_enabled" not in meta or not isinstance(meta.get("rag_enabled"), bool):
        cfg = load_config(None)
        try:
            meta["rag_enabled"] = get_rag_enabled(cfg)
        except Exception:
            meta["rag_enabled"] = False

    return meta


def apply_system_prompt(messages: list[dict[str, str]], system: str | None) -> list[dict[str, str]]:
    """Insert or replace the leading system message with `system`.

    If `system` is falsy, `messages` is returned unchanged. Otherwise the
    first message is replaced if it is already a system message, or a new
    system message is inserted at the start.
    """
    if not system:
        return messages
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": system}
    else:
        messages.insert(0, {"role": "system", "content": system})
    return messages


def init_session(
    session_name: str,
    sessions_dir: Path | None,
    *,
    new_session: bool,
    model: str,
    system: str | None = None,
) -> tuple[Path, Path, list[dict[str, str]], dict[str, Any]]:
    """Load or create a session's files, applying defaults and system prompt.

    If `new_session` is True, any existing session/meta files are deleted
    first so the session starts fresh.

    Args:
        session_name: Name of the session
        sessions_dir: Sessions directory (or None for the default)
        new_session: If True, reset any existing session with this name
        model: Model name to store in metadata
        system: Optional system prompt to apply to the message history

    Returns:
        Tuple of (session_file, meta_file, messages, meta)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    session_file = session_file_for(session_name, sessions_dir)
    meta_file = meta_file_for(session_file)

    if new_session and session_file_exists(session_file):
        _delete_session_storage(session_file, meta_file)

    messages = load_session_messages(session_file)
    messages = apply_system_prompt(messages, system)

    # Ensure files exist for new sessions
    if new_session:
        save_session_messages(session_file, messages)

    meta = load_session_meta(meta_file)
    meta = ensure_meta_defaults(meta, model=model)
    meta["token_estimate"] = token_estimate_from_messages(messages)
    save_session_meta(meta_file, meta)

    return session_file, meta_file, messages, meta


def persist_after_exchange(
    session_file: Path,
    meta_file: Path,
    messages: list[dict[str, str]],
    *,
    model: str,
    cfg: Any = None,
) -> str:
    """Persist session messages and metadata after a chat exchange.

    Also triggers auto-summarization (title/summary/tags) if enabled in
    config. Deliberately does NOT rename the session file to match the
    generated title -- see the note below.

    Args:
        session_file: Path to session JSON file
        meta_file: Path to metadata JSON file
        messages: List of chat messages
        model: Model name to store in metadata
        cfg: Optional config object. If not provided, loads global config.

    Returns:
        Session name (unchanged; kept in the return type for API stability)
    """
    save_session_messages(session_file, messages)
    meta = load_session_meta(meta_file)
    meta = ensure_meta_defaults(meta, model=model)
    meta["token_estimate"] = token_estimate_from_messages(messages)
    save_session_meta(meta_file, meta)

    # Extract session name from file path
    session_name = session_file.stem
    sessions_dir = session_file.parent

    # Auto-summarization trigger
    # Use passed config if provided, otherwise load global config
    if cfg is None:
        cfg = load_config(None)
    try:
        auto_summarize_enabled = cfg.getboolean("nyxgpt", "auto_summarize_enabled", fallback=False)
        auto_summarize_after = cfg.getint("nyxgpt", "auto_summarize_after_messages", fallback=5)
    except Exception:
        auto_summarize_enabled = False
        auto_summarize_after = 5

    # Trigger auto-summarization if:
    # 1. Auto-summarization is enabled
    # 2. Message count threshold is met
    # 3. Session doesn't already have a title
    if auto_summarize_enabled and auto_summarize_after > 0:
        message_count = len(messages)
        has_title = bool(meta.get("title"))

        # Only auto-summarize once (when reaching threshold without a title).
        # This only sets meta.title/summary/tags -- it must NOT also rename
        # the session file here. Callers (web UI, CLI REPL) keep addressing
        # the session by its original name for the rest of the conversation;
        # renaming the file out from under them mid-conversation orphans it,
        # so the next turn silently creates a brand-new empty session under
        # the old name instead of appending (#3459). Filename sync remains
        # available on demand via sync_filename_with_title()/the
        # POST /sessions/{name}/sync-filename and
        # POST /sessions/{name}/rename (sync_filename=true) endpoints, which
        # the caller triggers explicitly and can react to the new name.
        if not has_title and message_count >= auto_summarize_after:
            log.info(f"Auto-summarizing session '{session_name}' ({message_count} messages)")
            success, msg = summarize_session(session_name, sessions_dir)
            if not success:
                log.warning(f"Auto-summarization failed for '{session_name}': {msg}")

    return session_name


@dataclass
class SessionState:
    """In-memory handle to a loaded session's files, messages, and metadata."""

    name: str
    session_file: Path
    meta_file: Path
    messages: list[dict[str, str]]
    meta: SessionMetaDict


def load_session(
    name: str,
    cfg: Any,
    *,
    sessions_dir_override: str | None = None,
    new_session: bool = False,
    model: str | None = None,
    system: str | None = None,
) -> SessionState:
    """High-level session loader used by chat + API.

    Returns a SessionState with messages + meta loaded and defaults applied.
    """
    sessions_dir = (
        Path(sessions_dir_override).expanduser() if sessions_dir_override else get_sessions_dir(cfg)
    )
    chosen_model = model or get_default_model(cfg)

    sf, mf, msgs, meta = init_session(
        name,
        sessions_dir,
        new_session=new_session,
        model=chosen_model,
        system=system,
    )
    return SessionState(name=name, session_file=sf, meta_file=mf, messages=msgs, meta=meta)


def save_session(
    state: SessionState,
    cfg: Any,
    *,
    sessions_dir_override: str | None = None,
    model: str | None = None,
) -> None:
    """Persist messages and meta for an existing SessionState.

    May trigger auto-summarization (title/summary/tags), but never renames
    the session file -- see `persist_after_exchange`.
    """
    if sessions_dir_override:
        sessions_dir = Path(sessions_dir_override).expanduser()
        state.session_file = session_file_for(state.name, sessions_dir)
        state.meta_file = meta_file_for(state.session_file)

    chosen_model = model or str(state.meta.get("model") or get_default_model(cfg))
    new_name = persist_after_exchange(
        state.session_file, state.meta_file, state.messages, model=chosen_model, cfg=cfg
    )

    # Defensive: persist_after_exchange() never returns a different name
    # today, but keep this in sync in case that changes.
    if new_name != state.name:
        state.name = new_name
        sessions_dir = state.session_file.parent
        state.session_file = session_file_for(new_name, sessions_dir)
        state.meta_file = meta_file_for(state.session_file)


# --- Session management operations for the CLI ---


def list_sessions(cfg: Any | None) -> list[dict[str, Any]]:
    """List all sessions in the sessions directory, pinned sessions first.

    Args:
        cfg: A config object, a Path to the sessions directory, or None
            to use the default sessions directory

    Returns:
        List of dicts with keys "name", "file", "messages" (count),
        "modified" (timestamp string), and "meta" (session metadata)
    """
    if _use_db_backend():
        return cast(list[dict[str, Any]], _db_store().list_sessions())
    # Accept either a config object or a Path
    if isinstance(cfg, Path):
        sessions_dir = cfg
    else:
        sessions_dir = get_sessions_dir(cfg) if cfg is not None else default_sessions_dir()

    sessions_dir = _resolve_sessions_dir(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    files = [p for p in sessions_dir.glob("*.json") if not p.name.endswith(".meta.json")]
    # Skip files whose stem doesn't pass the meta_file_for() chokepoint's
    # allowlist (e.g. legacy/hand-copied filenames) instead of letting a
    # single non-conforming file take down the whole listing.
    files = [p for p in files if VALID_SESSION_NAME_PATTERN.match(p.stem)]

    def sort_key(p: Path):
        meta = load_session_meta(meta_file_for(p))
        pinned = bool(meta.get("pinned"))
        return (0 if pinned else 1, p.name.lower())

    files = sorted(files, key=sort_key)

    out: list[dict[str, Any]] = []
    for p in files:
        name = p.stem
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "?"
        msgs = load_session_messages(p)
        meta = load_session_meta(meta_file_for(p))
        out.append(
            {
                "name": name,
                "file": str(p),
                "messages": len(msgs),
                "modified": mtime,
                "meta": meta,
            }
        )

    return out


def delete_session(name: str, sessions_dir: Path | None) -> bool:
    """Delete a session's message and metadata files.

    Returns:
        True if the session existed and was deleted, False if not found.
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    if _use_db_backend():
        return bool(_db_store().delete(name))
    mf = meta_file_for(sf)
    if not session_file_exists(sf):
        return False
    sf.unlink()
    if mf.exists():
        mf.unlink()
    return True


def rename_session(old: str, new: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Rename a session's message and metadata files from `old` to `new`.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    old_file = session_file_for(old, sessions_dir)
    new_file = session_file_for(new, sessions_dir)

    if _use_db_backend():
        ok, msg = _db_store().rename(old, new)
        return bool(ok), str(msg)

    if not old_file.exists():
        return False, "No such session"
    if new_file.exists():
        return False, "Target session already exists"

    new_file.parent.mkdir(parents=True, exist_ok=True)
    os.replace(old_file, new_file)

    old_meta = meta_file_for(old_file)
    new_meta = meta_file_for(new_file)
    if old_meta.exists():
        os.replace(old_meta, new_meta)

    return True, "OK"


def set_pinned(name: str, pinned: bool, sessions_dir: Path | None) -> tuple[bool, str]:
    """Set the pinned flag on a session's metadata.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not session_file_exists(sf):
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    meta["pinned"] = pinned
    save_session_meta(mf, meta)
    return True, "OK"


def add_tags(name: str, tags: list[str], sessions_dir: Path | None) -> tuple[bool, str]:
    """Add tags to a session's metadata, merging with and deduplicating existing tags.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not session_file_exists(sf):
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    tags_value = meta.get("tags")
    existing: list[Any] = tags_value if isinstance(tags_value, list) else []
    existing = [str(t) for t in existing]
    combined = normalize_tags(existing + tags)
    meta["tags"] = combined
    save_session_meta(mf, meta)
    return True, "OK"


def remove_tags(name: str, tags: list[str], sessions_dir: Path | None) -> tuple[bool, str]:
    """Remove tags (case-insensitively) from a session's metadata.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not session_file_exists(sf):
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    tags_value = meta.get("tags")
    existing: list[Any] = tags_value if isinstance(tags_value, list) else []
    existing = [str(t) for t in existing]
    remove_set = {t.strip().lower() for t in tags if t.strip()}
    kept = [t for t in existing if t.strip().lower() not in remove_set]
    meta["tags"] = normalize_tags(kept)
    save_session_meta(mf, meta)
    return True, "OK"


def set_title(name: str, title: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Set the title stored in a session's metadata.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not session_file_exists(sf):
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    meta["title"] = title
    save_session_meta(mf, meta)
    return True, "OK"


def summarize_session(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Generate and store a title, summary, and tags for a session via the LLM.

    Sends the session's messages to the configured Ollama model, asking it
    to return JSON metadata, then merges the result into the session's
    metadata file.

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)

    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(sf))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        sf = Path(real)
    elif real.startswith(_tmp + os.sep):
        sf = Path(real)
    else:
        return False, "No such session"
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    msgs = load_session_messages(sf)
    if not msgs:
        return False, "Session has no messages"

    cfg = load_config(None)
    base_url = get_ollama_base_url(cfg)
    model = get_default_model(cfg)

    prompt = (
        "You are generating metadata for a chat session.\n"
        "Return ONLY valid JSON with keys: title (string), summary (string), tags (array of strings).\n"
        "Keep title <= 60 chars. Summary 1-2 sentences. Tags 2-6 short words.\n"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(msgs, ensure_ascii=False)[:12000]},
    ]

    try:
        out = ollama_chat(base_url=base_url, model=model, messages=messages)
        data = json.loads(out)
    except Exception as e:
        return False, f"summarize failed: {e}"

    title = data.get("title") if isinstance(data.get("title"), str) else ""
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    tags = normalize_tags([str(t) for t in tags])

    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta, model=model)
    if title:
        meta["title"] = title
    if summary:
        meta["summary"] = summary
    meta["tags"] = tags
    meta["token_estimate"] = token_estimate_from_messages(msgs)
    save_session_meta(mf, meta)

    return True, "OK"


def format_chunk_ref(chunk: dict[str, Any]) -> str:
    """Human-readable citation ref for a RAG chunk, e.g. "chunk 2 of 5".

    Prefers the 1-based `chunk_number`/`total_chunks` fields. Falls back to
    `chunk_id + 1` for citations persisted before those fields were tracked,
    since the raw zero-based `chunk_id` alone reads as a chunk *count* of 0
    rather than an index.
    """
    chunk_number = chunk.get("chunk_number")
    if chunk_number is not None:
        total_chunks = chunk.get("total_chunks")
        return (
            f"chunk {chunk_number} of {total_chunks}" if total_chunks else f"chunk {chunk_number}"
        )

    chunk_id = chunk.get("chunk_id")
    if chunk_id is not None:
        return f"chunk {int(chunk_id) + 1}"

    return "source"


def export_session_markdown(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Export session to Markdown format."""
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)

    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(sf))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        sf = Path(real)
    elif real.startswith(_tmp + os.sep):
        sf = Path(real)
    else:
        return False, "No such session"
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    msgs = load_session_messages(sf)
    meta = load_session_meta(mf)

    lines: list[str] = []
    title = meta.get("title", name)
    lines.append(f"# {title}\n")

    if meta.get("summary"):
        lines.append(f"**Summary:** {meta['summary']}\n")

    lines.append(f"**Session:** {name}")
    lines.append(f"**Created:** {meta.get('created_at', 'Unknown')}")
    lines.append(f"**Updated:** {meta.get('updated_at', 'Unknown')}")
    lines.append(f"**Messages:** {len(msgs)}")

    if meta.get("model"):
        lines.append(f"**Model:** {meta['model']}")

    if meta.get("tags"):
        lines.append(f"**Tags:** {', '.join(meta['tags'])}")

    lines.append("\n---\n")

    for msg in msgs:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "system":
            lines.append(f"## System\n\n{content}\n")
        elif role == "user":
            lines.append(f"## User\n\n{content}\n")
        elif role == "assistant":
            lines.append(f"## Assistant\n\n{content}\n")

            # Include RAG citations if available
            rag_chunks: list[Any] = cast(list[Any], msg.get("rag_chunks", []))
            if rag_chunks and isinstance(rag_chunks, list) and len(rag_chunks) > 0:
                lines.append("### RAG Sources\n")
                for idx, chunk in enumerate(rag_chunks, 1):
                    doc_id = chunk.get("doc_id", "Unknown")
                    # Use explicit None checking to avoid treating 0.0 as falsy
                    score = chunk.get("similarity_score")
                    if score is None:
                        score = chunk.get("score", 0.0)
                    text = chunk.get("text", "")

                    chunk_ref = format_chunk_ref(chunk)
                    collection = chunk.get("collection")
                    collection_suffix = (
                        f", {collection}" if collection and collection != "default" else ""
                    )
                    lines.append(
                        f"**[{idx}] {doc_id}** ({chunk_ref}{collection_suffix}) - Confidence: {score:.3f}\n"
                    )

                    # Include preview of source text (first 200 chars)
                    if text:
                        preview = text[:200] + "..." if len(text) > 200 else text
                        lines.append(f"> {preview}\n")

                lines.append("")  # Add blank line after citations
        else:
            lines.append(f"## {role.title()}\n\n{content}\n")

    return True, "\n".join(lines)


def export_session_json(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Export session to JSON format."""
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)

    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(sf))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        sf = Path(real)
    elif real.startswith(_tmp + os.sep):
        sf = Path(real)
    else:
        return False, "No such session"
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    msgs = load_session_messages(sf)
    meta = load_session_meta(mf)

    export_data = {
        "name": name,
        "metadata": meta,
        "messages": msgs,
    }

    return True, json.dumps(export_data, ensure_ascii=False, indent=2)


def export_session_html(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Export session to HTML format."""
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)

    # Inline sink-side barrier (CodeQL py/path-injection) -- see
    # load_session_messages for rationale.
    real = os.path.realpath(str(sf))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # SIM114 wants these branches merged with `or`, but CodeQL only credits a
    # barrier guard whose branch is dominated by a single condition.
    if real.startswith(_home + os.sep):  # noqa: SIM114
        sf = Path(real)
    elif real.startswith(_tmp + os.sep):
        sf = Path(real)
    else:
        return False, "No such session"
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    msgs = load_session_messages(sf)
    meta = load_session_meta(mf)

    # Everything below is served with `Content-Type: text/html`, so every
    # interpolated value MUST be HTML-escaped or a session name / stored
    # message / RAG chunk containing markup becomes reflected/stored XSS
    # (CodeQL py/reflected-xss). `html.escape(..., quote=True)` covers the
    # attribute and element contexts used here.
    title = html.escape(str(meta.get("title", name)))
    safe_name = html.escape(name)
    html_parts: list[str] = []
    html_parts.append("<!DOCTYPE html>")
    html_parts.append('<html lang="en">')
    html_parts.append("<head>")
    html_parts.append('  <meta charset="UTF-8">')
    html_parts.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html_parts.append(f"  <title>{title}</title>")
    html_parts.append("  <style>")
    html_parts.append(
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }"
    )
    html_parts.append("    h1 { color: #333; }")
    html_parts.append(
        "    .metadata { background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }"
    )
    html_parts.append("    .metadata p { margin: 5px 0; }")
    html_parts.append("    .message { margin: 20px 0; padding: 15px; border-radius: 5px; }")
    html_parts.append("    .system { background: #fff3cd; border-left: 4px solid #ffc107; }")
    html_parts.append("    .user { background: #e3f2fd; border-left: 4px solid #2196f3; }")
    html_parts.append("    .assistant { background: #f1f8e9; border-left: 4px solid #4caf50; }")
    html_parts.append(
        "    .role { font-weight: bold; margin-bottom: 10px; text-transform: uppercase; font-size: 12px; }"
    )
    html_parts.append("    .content { white-space: pre-wrap; line-height: 1.6; }")
    html_parts.append(
        "    .citations { margin-top: 15px; padding: 10px; background: #e3f2fd; border-left: 3px solid #2196f3; border-radius: 4px; }"
    )
    html_parts.append(
        "    .citation-header { font-weight: 600; font-size: 13px; margin-bottom: 8px; color: #1976d2; }"
    )
    html_parts.append(
        "    .citation-item { margin: 8px 0; padding: 8px; background: white; border-radius: 4px; font-size: 12px; }"
    )
    html_parts.append("    .citation-title { font-weight: 600; color: #333; }")
    html_parts.append("    .citation-score { color: #666; font-size: 11px; }")
    html_parts.append(
        "    .citation-text { margin-top: 6px; padding: 6px; background: #f5f5f5; border-left: 2px solid #ccc; font-size: 11px; color: #555; white-space: pre-wrap; }"
    )
    html_parts.append("  </style>")
    html_parts.append("</head>")
    html_parts.append("<body>")
    html_parts.append(f"  <h1>{title}</h1>")
    html_parts.append('  <div class="metadata">')
    if meta.get("summary"):
        html_parts.append(
            f"    <p><strong>Summary:</strong> {html.escape(str(meta['summary']))}</p>"
        )
    html_parts.append(f"    <p><strong>Session:</strong> {safe_name}</p>")
    html_parts.append(
        f"    <p><strong>Created:</strong> {html.escape(str(meta.get('created_at', 'Unknown')))}</p>"
    )
    html_parts.append(
        f"    <p><strong>Updated:</strong> {html.escape(str(meta.get('updated_at', 'Unknown')))}</p>"
    )
    html_parts.append(f"    <p><strong>Messages:</strong> {len(msgs)}</p>")
    if meta.get("model"):
        html_parts.append(f"    <p><strong>Model:</strong> {html.escape(str(meta['model']))}</p>")
    if meta.get("tags"):
        tags = ", ".join(html.escape(str(t)) for t in meta["tags"])
        html_parts.append(f"    <p><strong>Tags:</strong> {tags}</p>")
    html_parts.append("  </div>")

    for msg in msgs:
        role = html.escape(str(msg.get("role", "unknown")))
        content = html.escape(str(msg.get("content", "")))
        html_parts.append(f'  <div class="message {role}">')
        html_parts.append(f'    <div class="role">{role}</div>')
        html_parts.append(f'    <div class="content">{content}</div>')

        # Include RAG citations if available (for assistant messages)
        if role == "assistant":
            rag_chunks: list[Any] = cast(list[Any], msg.get("rag_chunks", []))
            if rag_chunks and isinstance(rag_chunks, list) and len(rag_chunks) > 0:
                html_parts.append('    <div class="citations">')
                html_parts.append('      <div class="citation-header">RAG Sources</div>')
                for idx, chunk in enumerate(rag_chunks, 1):
                    doc_id = html.escape(str(chunk.get("doc_id", "Unknown")))
                    # Use explicit None checking to avoid treating 0.0 as falsy
                    score = chunk.get("similarity_score")
                    if score is None:
                        score = chunk.get("score", 0.0)
                    text = chunk.get("text", "")

                    chunk_ref = html.escape(str(format_chunk_ref(chunk)))
                    html_parts.append('      <div class="citation-item">')
                    html_parts.append(
                        f'        <div class="citation-title">[{idx}] {doc_id} ({chunk_ref})</div>'
                    )
                    html_parts.append(
                        f'        <div class="citation-score">Confidence: {score:.3f}</div>'
                    )

                    # Include preview of source text (first 200 chars), HTML-escaped.
                    if text:
                        preview = text[:200] + "..." if len(text) > 200 else text
                        html_parts.append(
                            f'        <div class="citation-text">{html.escape(str(preview))}</div>'
                        )

                    html_parts.append("      </div>")
                html_parts.append("    </div>")

        html_parts.append("  </div>")

    html_parts.append("</body>")
    html_parts.append("</html>")

    return True, "\n".join(html_parts)


def sanitize_title_for_filename(title: str) -> str:
    """Convert a session title to a valid filesystem-safe session name.

    Transforms a human-readable title into a valid session name by:
    - Replacing spaces with hyphens
    - Removing or replacing special characters
    - Truncating to 64 characters
    - Ensuring it matches VALID_SESSION_NAME_PATTERN

    Args:
        title: Human-readable session title

    Returns:
        Sanitized filename-safe session name (alphanumeric, hyphens, underscores only)

    Examples:
        >>> sanitize_title_for_filename("My Chat Session")
        "my-chat-session"
        >>> sanitize_title_for_filename("Python: Tips & Tricks!")
        "python-tips-tricks"
        >>> sanitize_title_for_filename("Session #42 (Important)")
        "session-42-important"
    """
    # Convert to lowercase and replace spaces with hyphens
    sanitized = title.lower().strip()
    sanitized = sanitized.replace(" ", "-")

    # Replace common special chars with hyphens
    for char in [
        ":",
        ";",
        ",",
        ".",
        "!",
        "?",
        "(",
        ")",
        "[",
        "]",
        "{",
        "}",
        "/",
        "\\",
        "|",
        "&",
        "@",
        "#",
        "$",
        "%",
        "^",
        "*",
        "+",
        "=",
        "~",
        "`",
        "'",
        '"',
        "<",
        ">",
    ]:
        sanitized = sanitized.replace(char, "-")

    # Keep only alphanumeric, hyphens, and underscores
    sanitized = "".join(c for c in sanitized if c.isalnum() or c in "-_")

    # Collapse multiple consecutive hyphens
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")

    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-_")

    # Truncate to 64 characters
    if len(sanitized) > 64:
        sanitized = sanitized[:64].rstrip("-_")

    # Fallback if empty after sanitization
    if not sanitized:
        return "session"

    return sanitized


def sync_filename_with_title(
    current_name: str, sessions_dir: Path | None, *, force: bool = False
) -> tuple[bool, str, str]:
    """Rename session file to match its title (atomic copy-then-delete with locking).

    Uses atomic operations with file locking to safely rename both session and
    metadata files to match the sanitized session title. If auto_sync_filename
    is disabled in config, does nothing unless force=True.

    **Safety:**
    - Uses copy-then-delete pattern to prevent data loss if rename fails
    - Acquires exclusive file locks to prevent race conditions during concurrent access
    - Locks are held for up to 10 seconds; returns "Session is busy" if unavailable
    - Automatically releases locks even if an exception occurs

    **Concurrency:** If another process is writing to the session during rename,
    this function will wait up to 10 seconds for the lock. If the lock cannot be
    acquired, it returns an error message asking the user to try again.

    Args:
        current_name: Current session name (filename without extension)
        sessions_dir: Session directory path (or None for default)
        force: Force rename even if auto_sync_filename is disabled

    Returns:
        Tuple of (success, message, new_name)
        - success: True if renamed or no action needed, False on error
        - message: Status message ("renamed", "no_change", "disabled",
                   "Session is busy, please try again", or error message)
        - new_name: New session name (equals current_name if no change)

    Examples:
        >>> sync_filename_with_title("session_123", None)
        (True, "renamed", "my-chat-session")

        >>> sync_filename_with_title("my-chat-session", None)
        (True, "no_change", "my-chat-session")
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(current_name, sessions_dir)
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "Session not found", current_name

    # Check if auto-sync is enabled
    if not force:
        cfg = load_config(None)
        try:
            auto_sync = cfg.getboolean("nyxgpt", "auto_sync_filename", fallback=False)
        except Exception:
            auto_sync = False

        if not auto_sync:
            return True, "disabled", current_name

    # Load metadata and get title
    meta = load_session_meta(mf)
    title = meta.get("title")

    if not title or not isinstance(title, str):
        return True, "no_title", current_name

    # Sanitize title for filename
    new_name = sanitize_title_for_filename(title)

    # No change needed
    if new_name == current_name:
        return True, "no_change", current_name

    # Check if target already exists
    new_sf = session_file_for(new_name, sessions_dir)
    new_mf = meta_file_for(new_sf)

    if session_file_exists(new_sf):
        # Target exists - append counter to make unique
        counter = 1
        while True:
            candidate = f"{new_name}-{counter}"
            candidate_sf = session_file_for(candidate, sessions_dir)
            if not session_file_exists(candidate_sf):
                new_name = candidate
                new_sf = candidate_sf
                new_mf = meta_file_for(new_sf)
                break
            counter += 1
            if counter > 100:
                return False, "Could not find unique filename", current_name

    # DB backend: a rename is a row-atomic copy+delete in the store; no
    # files or file locks are involved.
    if _use_db_backend():
        ok, msg = _db_store().rename(current_name, new_name)
        if not ok:
            return False, str(msg), current_name
        log.info(f"Renamed session '{current_name}' -> '{new_name}'")
        return True, "renamed", new_name

    # Atomic rename using copy-then-delete pattern with file locking
    try:
        # Lock both files upfront in alphabetical order to prevent deadlock
        # (consistent ordering ensures no process can hold locks in conflicting order)
        # Capture metadata file existence state before locking to avoid TOCTOU race
        files_to_lock = [sf]
        meta_existed_initially = mf.exists()
        if meta_existed_initially:
            files_to_lock.append(mf)
        files_to_lock.sort(key=lambda p: p.as_posix())  # Cross-platform alphabetical order

        # Verify ordering in debug mode (catches violations during development)
        verify_lock_ordering(*files_to_lock)

        # Acquire locks in consistent order
        if len(files_to_lock) == 2:
            with (
                file_lock(files_to_lock[0], timeout=10.0),
                file_lock(files_to_lock[1], timeout=10.0),
            ):
                # 1. Copy session file to new location
                new_sf.parent.mkdir(parents=True, exist_ok=True)
                msgs = load_session_messages(sf)
                save_session_messages(new_sf, msgs)

                # 2. Copy metadata file to new location
                # Only save if metadata existed before locking (not an empty lock file)
                if meta_existed_initially:
                    save_session_meta(new_mf, meta)

                # 3. Delete old files (only after successful copy)
                sf.unlink()
                if meta_existed_initially:
                    mf.unlink()
        else:
            # Only session file needs locking
            with file_lock(files_to_lock[0], timeout=10.0):
                # 1. Copy session file to new location
                new_sf.parent.mkdir(parents=True, exist_ok=True)
                msgs = load_session_messages(sf)
                save_session_messages(new_sf, msgs)

                # 3. Delete old file
                sf.unlink()

        log.info(f"Renamed session '{current_name}' -> '{new_name}'")
        return True, "renamed", new_name

    except TimeoutError as e:
        log.error(f"Failed to acquire lock for session '{current_name}': {e}")
        return False, "Session is busy, please try again", current_name

    except Exception as e:
        log.error(f"Failed to rename session '{current_name}': {e}")
        # Clean up partial copy if it exists
        try:
            if new_sf.exists():
                new_sf.unlink()
            if new_mf.exists():
                new_mf.unlink()
        except Exception:
            pass
        # The full exception is logged above; return a generic status so the
        # detail never reaches an API client (CodeQL py/stack-trace-exposure).
        return False, "Rename failed due to an internal error", current_name


def edit_message(
    session_name: str,
    message_index: int,
    new_content: str,
    sessions_dir: Path | None = None,
    fork: bool = True,
) -> tuple[bool, str]:
    """Edit a message in a session.

    Args:
        session_name: Name of the session
        message_index: Index of message to edit (0-based)
        new_content: New content for the message
        sessions_dir: Optional sessions directory override
        fork: If True, truncate conversation after edited message (default behavior)

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    with session_lock(sf, timeout=5.0), session_lock(mf, timeout=5.0):
        messages = load_session_messages(sf)

        if message_index < 0 or message_index >= len(messages):
            return False, f"Invalid message index: {message_index}"

        message = messages[message_index]

        # Store original content if not already edited
        if "original_content" not in message:
            message["original_content"] = message["content"]

        # Update content and metadata
        message["content"] = new_content
        message["edited_at"] = iso_now()

        # Fork conversation: truncate all messages after the edited one
        if fork:
            messages = messages[: message_index + 1]

        save_session_messages(sf, messages)

        # Update metadata timestamp
        meta = load_session_meta(mf)
        meta["updated_at"] = iso_now()
        save_session_meta(mf, meta)

    return True, "Message edited"


def truncate_after_message(
    session_name: str,
    message_index: int,
    sessions_dir: Path | None = None,
) -> tuple[bool, str]:
    """Truncate conversation after a specific message.

    Useful for regenerating responses from a specific point.

    Args:
        session_name: Name of the session
        message_index: Index of message to keep as the last message (0-based)
        sessions_dir: Optional sessions directory override

    Returns:
        Tuple of (success, message)
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    if not session_file_exists(sf):
        return False, "No such session"

    with session_lock(sf, timeout=5.0), session_lock(mf, timeout=5.0):
        messages = load_session_messages(sf)

        if message_index < 0 or message_index >= len(messages):
            return False, f"Invalid message index: {message_index}"

        # Keep messages up to and including the specified index
        messages = messages[: message_index + 1]
        save_session_messages(sf, messages)

        # Update metadata timestamp
        meta = load_session_meta(mf)
        meta["updated_at"] = iso_now()
        save_session_meta(mf, meta)

    return True, "Conversation truncated"


def search_messages(
    query: str,
    sessions_dir: Path | None = None,
    *,
    case_sensitive: bool = False,
    role_filter: str | None = None,
    session_filter: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search for messages across all sessions or within a specific session.

    Performance characteristics:
        - Processes sessions sequentially, one at a time (memory-efficient)
        - Terminates early when limit is reached
        - Sessions are processed in reverse chronological order (newest first)
        - For best performance with large session directories:
            * Use session_filter to search specific sessions
            * Use lower limit values (default: 50, max recommended: 500)
            * Consider role_filter to reduce search space
        - Time complexity: O(sessions × messages_per_session) in worst case
        - Memory usage: O(1 session at a time + results up to limit)

    Args:
        query: Text to search for in message content
        sessions_dir: Optional sessions directory override
        case_sensitive: Whether to perform case-sensitive search (default: False)
        role_filter: Filter by message role ("user", "assistant", "system")
        session_filter: Filter to specific session name (None for all sessions)
        limit: Maximum number of results to return (recommended: 50-500)

    Returns:
        List of matching results with structure:
        [
            {
                "session_name": str,
                "session_title": str | None,
                "message_index": int,
                "role": str,
                "content": str,
                "content_preview": str,  # Snippet with match context
                "timestamp": str | None,
                "matches": int  # Number of times query appears in this message
            }
        ]
    """
    if not query:
        return []

    # Validate role_filter
    VALID_ROLES = {"user", "assistant", "system"}
    if role_filter and role_filter not in VALID_ROLES:
        raise ValueError(
            f"Invalid role_filter '{role_filter}'. Must be one of: {', '.join(sorted(VALID_ROLES))}"
        )

    # Prepare search query
    search_query = query if case_sensitive else query.lower()
    results: list[dict[str, Any]] = []

    if _use_db_backend():
        store = _db_store()
        rows = store.list_sessions()
        if session_filter:
            rows = [r for r in rows if r.get("name") == session_filter]
        # Newest first, mirroring the file backend's mtime sort
        rows.sort(key=lambda r: str(r.get("modified") or ""), reverse=True)
        for row in rows:
            session_name = str(row["name"])
            meta = row.get("meta") or {}
            try:
                messages = store.load_messages(session_name)
                if _collect_session_matches(
                    session_name,
                    messages,
                    lambda m=meta: m.get("title"),
                    query=query,
                    search_query=search_query,
                    case_sensitive=case_sensitive,
                    role_filter=role_filter,
                    results=results,
                    limit=limit,
                ):
                    return results
            except Exception as e:
                log.warning(f"Error searching session {session_name}: {e}")
                continue
        return results

    sessions_dir = _resolve_sessions_dir(sessions_dir or default_sessions_dir())
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_files = [p for p in sessions_dir.glob("*.json") if not p.name.endswith(".meta.json")]

    # Filter to specific session if requested
    if session_filter:
        session_files = [sf for sf in session_files if sf.stem == session_filter]

    # Sort sessions by modification time (newest first) for better UX
    # Users are more likely to search for content in recent sessions
    session_files = sorted(session_files, key=lambda p: p.stat().st_mtime, reverse=True)

    for session_file in session_files:
        session_name = session_file.stem

        try:
            messages = load_session_messages(session_file)
            if _collect_session_matches(
                session_name,
                messages,
                # Lazy title load: metadata is only read once a match is found
                lambda sf=session_file: load_session_meta(meta_file_for(sf)).get("title"),
                query=query,
                search_query=search_query,
                case_sensitive=case_sensitive,
                role_filter=role_filter,
                results=results,
                limit=limit,
            ):
                return results
        except Exception as e:
            log.warning(f"Error searching session {session_name}: {e}")
            continue

    return results


def _collect_session_matches(
    session_name: str,
    messages: list[dict[str, Any]],
    get_title: Callable[[], Any],
    *,
    query: str,
    search_query: str,
    case_sensitive: bool,
    role_filter: str | None,
    results: list[dict[str, Any]],
    limit: int,
) -> bool:
    """Append one session's matches to `results`; True once `limit` is reached.

    `get_title` is called lazily, only after the first match in the session,
    so sessions with no matches never load their metadata.
    """
    session_title: str | None = None
    title_loaded = False

    for idx, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp")

        # Apply role filter
        if role_filter and role != role_filter:
            continue

        # Perform search
        search_content = content if case_sensitive else content.lower()
        if search_query not in search_content:
            continue

        if not title_loaded:
            title = get_title()
            session_title = title if isinstance(title, str) else None
            title_loaded = True

        results.append(
            {
                "session_name": session_name,
                "session_title": session_title,
                "message_index": idx,
                "role": role,
                "content": content,
                "content_preview": _generate_preview(content, query, case_sensitive),
                "timestamp": timestamp,
                "matches": search_content.count(search_query),
            }
        )

        if len(results) >= limit:
            return True

    return False


def _generate_preview(
    content: str, query: str, case_sensitive: bool, context_chars: int = 100
) -> str:
    """Generate a content preview showing the match in context.

    Args:
        content: Full message content
        query: Search query
        case_sensitive: Whether search was case-sensitive
        context_chars: Number of characters to show before/after match

    Returns:
        Preview string with "..." if truncated, highlighting match context
    """
    # Find first match
    search_content = content if case_sensitive else content.lower()
    search_query = query if case_sensitive else query.lower()

    match_pos = search_content.find(search_query)
    if match_pos == -1:
        # Fallback if no match found (shouldn't happen)
        return content[:200] + ("..." if len(content) > 200 else "")

    # Calculate preview window
    start = max(0, match_pos - context_chars)
    end = min(len(content), match_pos + len(query) + context_chars)

    preview = content[start:end]

    # Add ellipsis if truncated
    if start > 0:
        preview = "..." + preview
    if end < len(content):
        preview = preview + "..."

    return preview


def merge_sessions(
    session_names: list[str],
    output_name: str,
    sessions_dir: Path | None = None,
) -> tuple[bool, str]:
    """Merge multiple sessions into a single new session.

    Combines message histories from multiple sessions in the order specified,
    merges metadata (tags are combined and deduplicated, earliest created_at
    is preserved), and handles conflicts gracefully.

    **Message Merging:**
    - Messages from each session are appended in order
    - Timestamps are preserved from original messages
    - If a message lacks a timestamp, one is generated
    - Message IDs are preserved if they exist

    **Metadata Merging:**
    - created_at: Uses earliest timestamp from all sessions
    - updated_at: Set to current time
    - pinned: False (new session starts unpinned)
    - tags: Combined and deduplicated from all sessions
    - title: Uses title from first session if available
    - summary: Uses summary from first session if available
    - model: Uses model from first session if available
    - rag_enabled: Uses setting from first session if available
    - token_estimate: Recalculated for merged messages

    **Conflict Handling:**
    - If output session already exists, returns error
    - If any input session doesn't exist, returns error
    - If no input sessions provided, returns error
    - Empty sessions are allowed (contribute zero messages)

    Args:
        session_names: List of session names to merge (in order)
        output_name: Name for the merged output session
        sessions_dir: Optional sessions directory override

    Returns:
        Tuple of (success, message)
        - success: True if merge completed successfully
        - message: Success message or error description

    Examples:
        >>> merge_sessions(["chat1", "chat2"], "combined", None)
        (True, "Merged 2 sessions into 'combined' (45 messages)")

        >>> merge_sessions(["nonexistent"], "output", None)
        (False, "Session 'nonexistent' not found")

        >>> merge_sessions(["chat1"], "chat1", None)
        (False, "Output session 'chat1' already exists")
    """
    sessions_dir = sessions_dir or default_sessions_dir()

    # Validation
    if not session_names:
        return False, "No sessions provided to merge"

    if not output_name:
        return False, "Output session name is required"

    # Validate output name
    try:
        validate_session_name(output_name)
    except ValueError as e:
        return False, f"Invalid output session name: {e}"

    # Check if output session already exists
    output_file = session_file_for(output_name, sessions_dir)
    if session_file_exists(output_file):
        return False, f"Output session '{output_name}' already exists"

    # Check if all input sessions exist
    for name in session_names:
        try:
            validate_session_name(name)
        except ValueError as e:
            return False, f"Invalid session name '{name}': {e}"

        sf = session_file_for(name, sessions_dir)
        if not session_file_exists(sf):
            return False, f"Session '{name}' not found"

    # Collect all messages and metadata from input sessions
    all_messages: list[dict[str, str]] = []
    all_metadata: list[SessionMetaDict] = []

    for name in session_names:
        sf = session_file_for(name, sessions_dir)
        mf = meta_file_for(sf)

        # Load messages
        messages = load_session_messages(sf)

        # Ensure all messages have timestamps and IDs
        for msg in messages:
            if "timestamp" not in msg or not msg.get("timestamp"):
                msg["timestamp"] = iso_now()
            if "id" not in msg or not msg.get("id"):
                msg["id"] = str(uuid.uuid4())

        all_messages.extend(messages)

        # Load metadata
        meta = load_session_meta(mf)
        if meta:
            all_metadata.append(meta)

    # Merge metadata
    merged_meta: SessionMetaDict = {}

    # created_at: earliest from all sessions
    created_timestamps = [ts for m in all_metadata if (ts := m.get("created_at")) is not None]
    if created_timestamps:
        merged_meta["created_at"] = min(created_timestamps)
    else:
        merged_meta["created_at"] = iso_now()

    # updated_at: current time
    merged_meta["updated_at"] = iso_now()

    # pinned: false for new session
    merged_meta["pinned"] = False

    # tags: combine and deduplicate from all sessions
    all_tags: list[str] = []
    for meta in all_metadata:
        tags = meta.get("tags")
        if isinstance(tags, list):
            all_tags.extend(str(t) for t in tags)
    merged_meta["tags"] = normalize_tags(all_tags)

    # title, summary, model, rag_enabled: use from first session if available
    if all_metadata:
        first_meta = all_metadata[0]
        if first_meta.get("title"):
            merged_meta["title"] = first_meta["title"]
        if first_meta.get("summary"):
            merged_meta["summary"] = first_meta["summary"]
        if first_meta.get("model"):
            merged_meta["model"] = first_meta["model"]
        if "rag_enabled" in first_meta:
            merged_meta["rag_enabled"] = first_meta["rag_enabled"]

    # token_estimate: recalculate for merged messages
    merged_meta["token_estimate"] = token_estimate_from_messages(all_messages)

    # Ensure all required metadata fields are present
    merged_meta = ensure_meta_defaults(merged_meta)

    # Save merged session
    output_meta_file = meta_file_for(output_file)

    try:
        save_session_messages(output_file, all_messages)
        save_session_meta(output_meta_file, merged_meta)
    except Exception as e:
        # Clean up on failure
        try:
            _delete_session_storage(output_file, output_meta_file)
        except Exception:
            pass
        return False, f"Failed to save merged session: {e}"

    message_count = len(all_messages)
    session_count = len(session_names)
    return (
        True,
        f"Merged {session_count} session{'s' if session_count != 1 else ''} into '{output_name}' ({message_count} message{'s' if message_count != 1 else ''})",
    )


# --- Batch operations ---


def batch_delete_sessions(
    session_names: list[str],
    sessions_dir: Path | None = None,
) -> tuple[int, int, list[str]]:
    """Delete multiple sessions at once.

    Args:
        session_names: List of session names to delete
        sessions_dir: Optional sessions directory override

    Returns:
        Tuple of (success_count, failure_count, failed_names)
        - success_count: Number of sessions successfully deleted
        - failure_count: Number of sessions that failed to delete
        - failed_names: List of session names that failed to delete
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    success_count = 0
    failed_names: list[str] = []

    for name in session_names:
        if delete_session(name, sessions_dir):
            success_count += 1
        else:
            failed_names.append(name)

    failure_count = len(failed_names)
    return success_count, failure_count, failed_names


def batch_tag_sessions(
    session_names: list[str],
    tags: list[str],
    sessions_dir: Path | None = None,
    *,
    remove: bool = False,
) -> tuple[int, int, list[str]]:
    """Add or remove tags from multiple sessions at once.

    Args:
        session_names: List of session names to update
        tags: List of tags to add or remove
        sessions_dir: Optional sessions directory override
        remove: If True, remove tags; if False, add tags (default: False)

    Returns:
        Tuple of (success_count, failure_count, failed_names)
        - success_count: Number of sessions successfully updated
        - failure_count: Number of sessions that failed to update
        - failed_names: List of session names that failed to update
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    success_count = 0
    failed_names: list[str] = []

    for name in session_names:
        if remove:
            ok, _msg = remove_tags(name, tags, sessions_dir)
        else:
            ok, _msg = add_tags(name, tags, sessions_dir)

        if ok:
            success_count += 1
        else:
            failed_names.append(name)

    failure_count = len(failed_names)
    return success_count, failure_count, failed_names


def batch_export_sessions(
    session_names: list[str],
    output_dir: Path,
    sessions_dir: Path | None = None,
    *,
    format: str = "markdown",
) -> tuple[int, int, list[str]]:
    """Export multiple sessions to files at once.

    Args:
        session_names: List of session names to export
        output_dir: Directory to write exported files to
        sessions_dir: Optional sessions directory override
        format: Export format - "markdown", "json", or "html" (default: "markdown")

    Returns:
        Tuple of (success_count, failure_count, failed_names)
        - success_count: Number of sessions successfully exported
        - failure_count: Number of sessions that failed to export
        - failed_names: List of session names that failed to export
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    success_count = 0
    failed_names: list[str] = []

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Choose export function based on format
    if format == "markdown":
        export_fn = export_session_markdown
        extension = ".md"
    elif format == "json":
        export_fn = export_session_json
        extension = ".json"
    elif format == "html":
        export_fn = export_session_html
        extension = ".html"
    else:
        raise ValueError(f"Invalid format: {format}")

    for name in session_names:
        ok, content = export_fn(name, sessions_dir)
        if ok:
            try:
                output_file = output_dir / f"{name}{extension}"
                output_file.write_text(content, encoding="utf-8")
                success_count += 1
            except OSError:
                failed_names.append(name)
        else:
            failed_names.append(name)

    failure_count = len(failed_names)
    return success_count, failure_count, failed_names


def batch_update_metadata(
    session_names: list[str],
    sessions_dir: Path | None = None,
    *,
    pinned: bool | None = None,
    model: str | None = None,
    rag_enabled: bool | None = None,
) -> tuple[int, int, list[str]]:
    """Update metadata fields for multiple sessions at once.

    Args:
        session_names: List of session names to update
        sessions_dir: Optional sessions directory override
        pinned: Set pinned status (None = no change)
        model: Set model name (None = no change)
        rag_enabled: Set RAG enabled status (None = no change)

    Returns:
        Tuple of (success_count, failure_count, failed_names)
        - success_count: Number of sessions successfully updated
        - failure_count: Number of sessions that failed to update
        - failed_names: List of session names that failed to update
    """
    sessions_dir = sessions_dir or default_sessions_dir()
    success_count = 0
    failed_names: list[str] = []

    for name in session_names:
        try:
            sf = session_file_for(name, sessions_dir)
            mf = meta_file_for(sf)

            if not session_file_exists(sf):
                failed_names.append(name)
                continue

            meta = load_session_meta(mf)
            meta = ensure_meta_defaults(meta)

            # Apply updates
            if pinned is not None:
                meta["pinned"] = pinned
            if model is not None:
                meta["model"] = model
            if rag_enabled is not None:
                meta["rag_enabled"] = rag_enabled

            save_session_meta(mf, meta)
            success_count += 1

        except Exception:
            failed_names.append(name)

    failure_count = len(failed_names)
    return success_count, failure_count, failed_names


__all__ = [
    "default_sessions_dir",
    "session_file_for",
    "session_file_exists",
    "session_exists",
    "session_lock",
    "meta_file_for",
    "iso_now",
    "token_estimate_from_messages",
    "load_session_messages",
    "save_session_messages",
    "load_session_meta",
    "save_session_meta",
    "normalize_tags",
    "ensure_meta_defaults",
    "apply_system_prompt",
    "init_session",
    "persist_after_exchange",
    "SessionState",
    "load_session",
    "save_session",
    "list_sessions",
    "delete_session",
    "rename_session",
    "set_pinned",
    "add_tags",
    "remove_tags",
    "set_title",
    "summarize_session",
    "export_session_markdown",
    "export_session_json",
    "export_session_html",
    "sanitize_title_for_filename",
    "sync_filename_with_title",
    "edit_message",
    "truncate_after_message",
    "search_messages",
    "merge_sessions",
    "batch_delete_sessions",
    "batch_tag_sessions",
    "batch_export_sessions",
    "batch_update_metadata",
]
