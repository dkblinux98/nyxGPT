from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, NotRequired

from mygpt.config import load_config, get_default_model, get_ollama_base_url, get_sessions_dir
from mygpt.ollama_client import ollama_chat


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

    Example:
        >>> with file_lock(Path("session.json"), timeout=10.0) as fd:
        ...     # File is locked here
        ...     data = Path("session.json").read_text()
        ...     # Lock automatically released when exiting block
    """
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
                except OSError:
                    if time.time() - start_time > timeout:
                        raise TimeoutError(f"Could not acquire lock on {file_path} within {timeout}s")
                    time.sleep(0.1)
        else:
            import fcntl
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.time() - start_time > timeout:
                        raise TimeoutError(f"Could not acquire lock on {file_path} within {timeout}s")
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


# For backwards compatibility, keep dict[str, Any] in function signatures
# but document the expected structure
SessionMetaDict = dict[str, Any]


# Session names must be alphanumeric with underscores or hyphens, 1-64 chars
# This prevents path traversal and ensures filesystem compatibility
VALID_SESSION_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


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
            "Session name must be 1-64 alphanumeric characters, "
            "underscores, or hyphens"
        )

    return raw


def default_sessions_dir() -> Path:
    cfg = load_config(None)
    return get_sessions_dir(cfg)


def session_file_for(name: str, sessions_dir: Path) -> Path:
    name = validate_session_name(name)
    # name is already validated and safe to use directly
    return sessions_dir / f"{name}.json"


def meta_file_for(session_file: Path) -> Path:
    return session_file.with_suffix(".meta.json")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def token_estimate_from_messages(messages: list[dict[str, str]]) -> int:
    # Rough estimate: ~4 chars per token (very approximate). Keeps us dependency-free.
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
    return max(1, chars // 4) if chars else 0


def load_session_messages(session_file: Path) -> list[dict[str, str]]:
    if not session_file.exists():
        return []

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in session file %s: %s", session_file, e)
        return []
    except (IOError, OSError) as e:
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
    if not session_file.exists():
        return ([], 0)

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in session file %s: %s", session_file, e)
        return ([], 0)
    except (IOError, OSError) as e:
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
    session_file.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file name to avoid race conditions in concurrent writes
    tmp = session_file.parent / f".{session_file.name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, session_file)


def load_session_meta(meta_file: Path) -> SessionMetaDict:
    if not meta_file.exists():
        return {}

    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in metadata file %s: %s", meta_file, e)
        return {}
    except (IOError, OSError) as e:
        log.warning("Failed to read metadata file %s: %s", meta_file, e)
        return {}


def save_session_meta(meta_file: Path, meta: SessionMetaDict) -> None:
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file name to avoid race conditions in concurrent writes
    tmp = meta_file.parent / f".{meta_file.name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, meta_file)


def normalize_tags(tags: list[str]) -> list[str]:
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


def ensure_meta_defaults(
    meta: SessionMetaDict,
    *,
    model: str | None = None
) -> SessionMetaDict:
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
            meta["rag_enabled"] = cfg.getboolean("rag", "enable_chat_context", fallback=False)
        except Exception:
            meta["rag_enabled"] = False

    return meta


def apply_system_prompt(messages: list[dict[str, str]], system: str | None) -> list[dict[str, str]]:
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
    sessions_dir = sessions_dir or default_sessions_dir()
    session_file = session_file_for(session_name, sessions_dir)
    meta_file = meta_file_for(session_file)

    if new_session and session_file.exists():
        session_file.unlink()
        if meta_file.exists():
            meta_file.unlink()

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


def persist_after_exchange(session_file: Path, meta_file: Path, messages: list[dict[str, str]], *, model: str) -> str:
    """Persist session messages and metadata after a chat exchange.

    Also triggers auto-summarization and filename sync if enabled in config.

    Args:
        session_file: Path to session JSON file
        meta_file: Path to metadata JSON file
        messages: List of chat messages
        model: Model name to store in metadata

    Returns:
        Session name (possibly updated if filename was synced)
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
    cfg = load_config(None)
    try:
        auto_summarize_enabled = cfg.getboolean("mygpt", "auto_summarize_enabled", fallback=False)
        auto_summarize_after = cfg.getint("mygpt", "auto_summarize_after_messages", fallback=5)
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

        # Only auto-summarize once (when reaching threshold without a title)
        if not has_title and message_count >= auto_summarize_after:
            log.info(f"Auto-summarizing session '{session_name}' ({message_count} messages)")
            success, msg = summarize_session(session_name, sessions_dir)
            if not success:
                log.warning(f"Auto-summarization failed for '{session_name}': {msg}")
            else:
                # After summarization, sync filename if enabled
                success, status, new_name = sync_filename_with_title(session_name, sessions_dir)
                if success and status == "renamed":
                    log.info(f"Auto-synced filename '{session_name}' -> '{new_name}'")
                    session_name = new_name

    return session_name


@dataclass
class SessionState:
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
    sessions_dir = Path(sessions_dir_override).expanduser() if sessions_dir_override else get_sessions_dir(cfg)
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

    Note: If auto-summarization and filename sync are enabled, the session
    name may change after calling this function. The SessionState object
    will be updated to reflect the new name.
    """
    if sessions_dir_override:
        sessions_dir = Path(sessions_dir_override).expanduser()
        state.session_file = session_file_for(state.name, sessions_dir)
        state.meta_file = meta_file_for(state.session_file)

    chosen_model = model or str(state.meta.get("model") or get_default_model(cfg))
    new_name = persist_after_exchange(state.session_file, state.meta_file, state.messages, model=chosen_model)

    # Update SessionState if name changed (due to filename sync)
    if new_name != state.name:
        state.name = new_name
        sessions_dir = state.session_file.parent
        state.session_file = session_file_for(new_name, sessions_dir)
        state.meta_file = meta_file_for(state.session_file)


# --- Session management operations for the CLI ---

def list_sessions(cfg: Any | None) -> list[dict[str, Any]]:
    # Accept either a config object or a Path
    if isinstance(cfg, Path):
        sessions_dir = cfg
    else:
        sessions_dir = get_sessions_dir(cfg) if cfg is not None else default_sessions_dir()

    sessions_dir.mkdir(parents=True, exist_ok=True)

    files = [p for p in sessions_dir.glob("*.json") if not p.name.endswith(".meta.json")]

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
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not sf.exists():
        return False
    sf.unlink()
    if mf.exists():
        mf.unlink()
    return True


def rename_session(old: str, new: str, sessions_dir: Path | None) -> tuple[bool, str]:
    sessions_dir = sessions_dir or default_sessions_dir()
    old_file = session_file_for(old, sessions_dir)
    new_file = session_file_for(new, sessions_dir)

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
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not sf.exists():
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    meta["pinned"] = pinned
    save_session_meta(mf, meta)
    return True, "OK"


def add_tags(name: str, tags: list[str], sessions_dir: Path | None) -> tuple[bool, str]:
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not sf.exists():
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
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not sf.exists():
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
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)
    if not sf.exists():
        return False, "No such session"
    meta = load_session_meta(mf)
    meta = ensure_meta_defaults(meta)
    meta["title"] = title
    save_session_meta(mf, meta)
    return True, "OK"


def summarize_session(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)

    if not sf.exists():
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


def export_session_markdown(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Export session to Markdown format."""
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)

    if not sf.exists():
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
        else:
            lines.append(f"## {role.title()}\n\n{content}\n")

    return True, "\n".join(lines)


def export_session_json(name: str, sessions_dir: Path | None) -> tuple[bool, str]:
    """Export session to JSON format."""
    sessions_dir = sessions_dir or default_sessions_dir()
    sf = session_file_for(name, sessions_dir)
    mf = meta_file_for(sf)

    if not sf.exists():
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
    mf = meta_file_for(sf)

    if not sf.exists():
        return False, "No such session"

    msgs = load_session_messages(sf)
    meta = load_session_meta(mf)

    title = meta.get("title", name)
    html_parts: list[str] = []
    html_parts.append("<!DOCTYPE html>")
    html_parts.append('<html lang="en">')
    html_parts.append("<head>")
    html_parts.append('  <meta charset="UTF-8">')
    html_parts.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html_parts.append(f"  <title>{title}</title>")
    html_parts.append("  <style>")
    html_parts.append("    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }")
    html_parts.append("    h1 { color: #333; }")
    html_parts.append("    .metadata { background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }")
    html_parts.append("    .metadata p { margin: 5px 0; }")
    html_parts.append("    .message { margin: 20px 0; padding: 15px; border-radius: 5px; }")
    html_parts.append("    .system { background: #fff3cd; border-left: 4px solid #ffc107; }")
    html_parts.append("    .user { background: #e3f2fd; border-left: 4px solid #2196f3; }")
    html_parts.append("    .assistant { background: #f1f8e9; border-left: 4px solid #4caf50; }")
    html_parts.append("    .role { font-weight: bold; margin-bottom: 10px; text-transform: uppercase; font-size: 12px; }")
    html_parts.append("    .content { white-space: pre-wrap; line-height: 1.6; }")
    html_parts.append("  </style>")
    html_parts.append("</head>")
    html_parts.append("<body>")
    html_parts.append(f"  <h1>{title}</h1>")
    html_parts.append('  <div class="metadata">')
    if meta.get("summary"):
        html_parts.append(f"    <p><strong>Summary:</strong> {meta['summary']}</p>")
    html_parts.append(f"    <p><strong>Session:</strong> {name}</p>")
    html_parts.append(f"    <p><strong>Created:</strong> {meta.get('created_at', 'Unknown')}</p>")
    html_parts.append(f"    <p><strong>Updated:</strong> {meta.get('updated_at', 'Unknown')}</p>")
    html_parts.append(f"    <p><strong>Messages:</strong> {len(msgs)}</p>")
    if meta.get("model"):
        html_parts.append(f"    <p><strong>Model:</strong> {meta['model']}</p>")
    if meta.get("tags"):
        html_parts.append(f"    <p><strong>Tags:</strong> {', '.join(meta['tags'])}</p>")
    html_parts.append("  </div>")

    for msg in msgs:
        role = msg.get("role", "unknown")
        content = msg.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
        html_parts.append(f'  <div class="message {role}">')
        html_parts.append(f'    <div class="role">{role}</div>')
        html_parts.append(f'    <div class="content">{content}</div>')
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
    for char in [":", ";", ",", ".", "!", "?", "(", ")", "[", "]", "{", "}", "/", "\\", "|", "&", "@", "#", "$", "%", "^", "*", "+", "=", "~", "`", "'", '"', "<", ">"]:
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
    current_name: str,
    sessions_dir: Path | None,
    *,
    force: bool = False
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

    if not sf.exists():
        return False, "Session not found", current_name

    # Check if auto-sync is enabled
    if not force:
        cfg = load_config(None)
        try:
            auto_sync = cfg.getboolean("mygpt", "auto_sync_filename", fallback=False)
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

    if new_sf.exists():
        # Target exists - append counter to make unique
        counter = 1
        while True:
            candidate = f"{new_name}-{counter}"
            candidate_sf = session_file_for(candidate, sessions_dir)
            if not candidate_sf.exists():
                new_name = candidate
                new_sf = candidate_sf
                new_mf = meta_file_for(new_sf)
                break
            counter += 1
            if counter > 100:
                return False, "Could not find unique filename", current_name

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
            with file_lock(files_to_lock[0], timeout=10.0), file_lock(files_to_lock[1], timeout=10.0):
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
        return False, f"Rename failed: {e}", current_name


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

    if not sf.exists():
        return False, "No such session"

    with file_lock(sf, timeout=5.0), file_lock(mf, timeout=5.0):
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
            messages = messages[:message_index + 1]

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

    if not sf.exists():
        return False, "No such session"

    with file_lock(sf, timeout=5.0), file_lock(mf, timeout=5.0):
        messages = load_session_messages(sf)

        if message_index < 0 or message_index >= len(messages):
            return False, f"Invalid message index: {message_index}"

        # Keep messages up to and including the specified index
        messages = messages[:message_index + 1]
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
    sessions_dir = sessions_dir or default_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)

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
            # Load session messages
            messages = load_session_messages(session_file)

            # Lazy load metadata - only load if we find matches
            # This avoids loading metadata for sessions with no matches
            session_title: str | None = None
            meta_loaded = False

            # Search through messages
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

                # Found a match - load metadata if not already loaded
                if not meta_loaded:
                    meta = load_session_meta(meta_file_for(session_file))
                    session_title = meta.get("title")
                    meta_loaded = True

                # Count matches
                match_count = search_content.count(search_query)

                # Generate content preview with match context
                preview = _generate_preview(content, query, case_sensitive)

                results.append({
                    "session_name": session_name,
                    "session_title": session_title,
                    "message_index": idx,
                    "role": role,
                    "content": content,
                    "content_preview": preview,
                    "timestamp": timestamp,
                    "matches": match_count,
                })

                # Check limit
                if len(results) >= limit:
                    return results

        except Exception as e:
            log.warning(f"Error searching session {session_name}: {e}")
            continue

    return results


def _generate_preview(content: str, query: str, case_sensitive: bool, context_chars: int = 100) -> str:
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
    if output_file.exists():
        return False, f"Output session '{output_name}' already exists"

    # Check if all input sessions exist
    for name in session_names:
        try:
            validate_session_name(name)
        except ValueError as e:
            return False, f"Invalid session name '{name}': {e}"

        sf = session_file_for(name, sessions_dir)
        if not sf.exists():
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
    created_timestamps = [m.get("created_at") for m in all_metadata if m.get("created_at")]
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
            if output_file.exists():
                output_file.unlink()
            if output_meta_file.exists():
                output_meta_file.unlink()
        except Exception:
            pass
        return False, f"Failed to save merged session: {e}"

    message_count = len(all_messages)
    session_count = len(session_names)
    return True, f"Merged {session_count} session{'s' if session_count != 1 else ''} into '{output_name}' ({message_count} message{'s' if message_count != 1 else ''})"


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

            if not sf.exists():
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