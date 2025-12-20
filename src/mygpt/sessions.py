

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mygpt.config import load_config
from mygpt.ollama_client import ollama_chat


def default_sessions_dir() -> Path:
    return Path.home() / ".myGPT" / "sessions"


def session_file_for(name: str, sessions_dir: Path) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.").strip("._")
    if not safe:
        safe = "default"
    return sessions_dir / f"{safe}.json"


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
    except Exception:
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
    tmp = session_file.with_suffix(session_file.suffix + ".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, session_file)


def load_session_meta(meta_file: Path) -> dict[str, Any]:
    if not meta_file.exists():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_session_meta(meta_file: Path, meta: dict[str, Any]) -> None:
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_file.with_suffix(meta_file.suffix + ".tmp")
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


def ensure_meta_defaults(meta: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
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
    system: str | None,
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


# --- Session management operations for the CLI ---

def list_sessions(sessions_dir: Path | None) -> list[dict[str, Any]]:
    sessions_dir = sessions_dir or default_sessions_dir()
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
    existing = meta.get("tags") if isinstance(meta.get("tags"), list) else []
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
    existing = meta.get("tags") if isinstance(meta.get("tags"), list) else []
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
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

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
    "list_sessions",
    "delete_session",
    "rename_session",
    "set_pinned",
    "add_tags",
    "remove_tags",
    "set_title",
    "summarize_session",
]