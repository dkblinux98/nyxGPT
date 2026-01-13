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
]