from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, NotRequired

from mygpt.config import load_config, get_default_model, get_ollama_base_url, get_sessions_dir
from mygpt.ollama_client import ollama_chat


log = logging.getLogger(__name__)


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
                out.append({"role": item["role"], "content": item["content"]})
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


def persist_after_exchange(session_file: Path, meta_file: Path, messages: list[dict[str, str]], *, model: str) -> None:
    save_session_messages(session_file, messages)
    meta = load_session_meta(meta_file)
    meta = ensure_meta_defaults(meta, model=model)
    meta["token_estimate"] = token_estimate_from_messages(messages)
    save_session_meta(meta_file, meta)


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
    """Persist messages and meta for an existing SessionState."""
    if sessions_dir_override:
        sessions_dir = Path(sessions_dir_override).expanduser()
        state.session_file = session_file_for(state.name, sessions_dir)
        state.meta_file = meta_file_for(state.session_file)

    chosen_model = model or str(state.meta.get("model") or get_default_model(cfg))
    persist_after_exchange(state.session_file, state.meta_file, state.messages, model=chosen_model)


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
]