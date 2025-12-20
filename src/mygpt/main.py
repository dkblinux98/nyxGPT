from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Optional

from mygpt.config import load_config


# --- Session helpers ---

def _default_sessions_dir() -> Path:
    return Path.home() / ".myGPT" / "sessions"


def _load_session_messages(session_file: Path) -> list[dict[str, str]]:
    if not session_file.exists():
        return []
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        out: list[dict[str, str]] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("role"), str) and isinstance(item.get("content"), str):
                out.append({"role": item["role"], "content": item["content"]})
        return out
    return []


def _save_session_messages(session_file: Path, messages: list[dict[str, str]]) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = session_file.with_suffix(session_file.suffix + ".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, session_file)


def _session_file_for(name: str, sessions_dir: Path) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.").strip("._")
    if not safe:
        safe = "default"
    return sessions_dir / f"{safe}.json"


def _post_json(url: str, payload: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


# Streaming POST helper: yields JSON objects from a newline-delimited JSON HTTP response.
def _post_json_lines(url: str, payload: dict[str, Any], timeout_s: float = 120.0):
    """Yield decoded JSON objects from a newline-delimited JSON HTTP response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield json.loads(line)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


def _ollama_chat(base_url: str, model: str, messages: list[dict[str, str]]) -> str:
    # Ollama expects /api/chat with messages like: {role: user|assistant|system, content: "..."}
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    data = _post_json(url, payload)

    msg = (data.get("message") or {})
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {data}")
    return content


# Streaming chat function: streams tokens from Ollama and returns the final assistant message.
def _ollama_chat_stream(base_url: str, model: str, messages: list[dict[str, str]]) -> str:
    """Stream tokens from Ollama and return the final assistant message content."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": True}

    chunks: list[str] = []
    for obj in _post_json_lines(url, payload):
        msg = (obj.get("message") or {})
        part = msg.get("content")
        if isinstance(part, str) and part:
            chunks.append(part)
            print(part, end="", flush=True)
        if obj.get("done") is True:
            break

    # Ensure we end on a newline for a nicer terminal UX.
    if chunks:
        print()

    return "".join(chunks)


def _meta_file_for(session_file: Path) -> Path:
    return session_file.with_suffix(".meta.json")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _token_estimate_from_messages(messages: list[dict[str, str]]) -> int:
    # Rough estimate: ~4 chars per token (very approximate). Keeps us dependency-free.
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
    return max(1, chars // 4) if chars else 0


def _load_session_meta(meta_file: Path) -> dict[str, Any]:
    if not meta_file.exists():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_session_meta(meta_file: Path, meta: dict[str, Any]) -> None:
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_file.with_suffix(meta_file.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, meta_file)


def _normalize_tags(tags: list[str]) -> list[str]:
    norm = []
    seen = set()
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


def _ensure_meta_defaults(meta: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    now = _iso_now()
    if "created_at" not in meta or not isinstance(meta.get("created_at"), str):
        meta["created_at"] = now
    meta["updated_at"] = now
    if "pinned" not in meta or not isinstance(meta.get("pinned"), bool):
        meta["pinned"] = False
    if "tags" not in meta or not isinstance(meta.get("tags"), list):
        meta["tags"] = []
    else:
        meta["tags"] = _normalize_tags([str(x) for x in meta["tags"]])
    if model:
        meta["model"] = model
    return meta


def _cmd_info(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path)
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

    print("myGPT OK")
    print(f"Ollama base_url: {base_url}")
    print(f"Default model: {model}")
    return 0


def _cmd_chat(
    cfg_path: Path | None,
    model_override: str | None,
    system: str | None,
    prompt: str | None,
    stream: bool,
    session_name: str,
    new_session: bool,
    sessions_dir: Path | None,
) -> int:
    cfg = load_config(cfg_path)
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = model_override or cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

    sessions_dir = sessions_dir or _default_sessions_dir()
    session_file = _session_file_for(session_name, sessions_dir)
    meta_file = _meta_file_for(session_file)

    if new_session and session_file.exists():
        session_file.unlink()
        mf = _meta_file_for(session_file)
        if mf.exists():
            mf.unlink()

    messages: list[dict[str, str]] = _load_session_messages(session_file)
    meta = _load_session_meta(meta_file)
    meta = _ensure_meta_defaults(meta, model=model)
    meta["token_estimate"] = _token_estimate_from_messages(messages)
    _save_session_meta(meta_file, meta)

    # Apply/override the system prompt for this run (do not duplicate system messages).
    if system:
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": system}
        else:
            messages.insert(0, {"role": "system", "content": system})

    def ask_once(user_text: str) -> str:
        messages.append({"role": "user", "content": user_text})

        if stream:
            reply = _ollama_chat_stream(base_url=base_url, model=model, messages=messages)
        else:
            reply = _ollama_chat(base_url=base_url, model=model, messages=messages)

        messages.append({"role": "assistant", "content": reply})
        _save_session_messages(session_file, messages)
        meta = _load_session_meta(meta_file)
        meta = _ensure_meta_defaults(meta, model=model)
        meta["token_estimate"] = _token_estimate_from_messages(messages)
        _save_session_meta(meta_file, meta)
        return reply

    # Single prompt mode
    if prompt is not None:
        if stream:
            ask_once(prompt)
        else:
            print(ask_once(prompt))
        return 0

    # Interactive mode
    print(f"myGPT chat (model: {model}, session: {session_file.name})")
    print("Type /exit to quit.")

    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit"}:
            return 0

        try:
            reply = ask_once(user_text)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue

        if not stream:
            print(reply)


# --- Sessions command handlers ---

def _count_messages(session_file: Path) -> int:
    msgs = _load_session_messages(session_file)
    return len(msgs)


def _cmd_sessions_list(sessions_dir: Path | None) -> int:
    sessions_dir = sessions_dir or _default_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    files = list(sessions_dir.glob("*.json"))

    def sort_key(p: Path):
        meta = _load_session_meta(_meta_file_for(p))
        pinned = bool(meta.get("pinned"))
        # pinned first, then name
        return (0 if pinned else 1, p.name.lower())

    files = sorted(files, key=sort_key)

    if not files:
        print(f"No sessions found in {sessions_dir}")
        return 0

    print(f"Sessions in {sessions_dir}:")
    for p in files:
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "?"
        try:
            n = _count_messages(p)
        except Exception:
            n = 0
        name = p.stem
        meta = _load_session_meta(_meta_file_for(p))
        pinned = bool(meta.get("pinned"))
        title = meta.get("title") if isinstance(meta.get("title"), str) else ""
        summary = meta.get("summary") if isinstance(meta.get("summary"), str) else ""
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        tags_s = ",".join([str(t) for t in tags]) if tags else ""
        tok = meta.get("token_estimate")
        tok_s = str(tok) if isinstance(tok, int) else "?"

        prefix = "📌 " if pinned else "- "
        line = f"{prefix}{name}  (messages: {n}, tokens~: {tok_s}, modified: {mtime})"
        if tags_s:
            line += f"  [tags: {tags_s}]"
        print(line)
        if title:
            print(f"    title: {title}")
        if summary:
            print(f"    summary: {summary}")

    return 0


def _cmd_sessions_delete(name: str, sessions_dir: Path | None) -> int:
    sessions_dir = sessions_dir or _default_sessions_dir()
    session_file = _session_file_for(name, sessions_dir)

    if not session_file.exists():
        print(f"No such session: {name}")
        return 1

    session_file.unlink()
    mf = _meta_file_for(session_file)
    if mf.exists():
        mf.unlink()
    print(f"Deleted session: {name}")
    return 0


def _cmd_sessions_rename(old: str, new: str, sessions_dir: Path | None) -> int:
    sessions_dir = sessions_dir or _default_sessions_dir()
    old_file = _session_file_for(old, sessions_dir)
    new_file = _session_file_for(new, sessions_dir)

    if not old_file.exists():
        print(f"No such session: {old}")
        return 1

    if new_file.exists():
        print(f"Target session already exists: {new}")
        return 1

    new_file.parent.mkdir(parents=True, exist_ok=True)
    os.replace(old_file, new_file)
    old_meta = _meta_file_for(old_file)
    new_meta = _meta_file_for(new_file)
    if old_meta.exists():
        os.replace(old_meta, new_meta)
    print(f"Renamed session: {old} -> {new}")
    return 0


def _cmd_sessions(action: str, sessions_dir: Path | None, name: str | None, new_name: str | None, extras: list[str]) -> int:
    sessions_dir = sessions_dir or _default_sessions_dir()

    if action == "list":
        return _cmd_sessions_list(sessions_dir)

    if action == "show":
        if not name:
            print("ERROR: session name is required for show", file=sys.stderr)
            return 2
        sf = _session_file_for(name, sessions_dir)
        mf = _meta_file_for(sf)
        msgs = _load_session_messages(sf)
        meta = _load_session_meta(mf)
        print(f"Session: {name}")
        print(f"File: {sf}")
        print(f"Meta: {mf}")
        print(f"Messages: {len(msgs)}")
        if meta:
            print(json.dumps(meta, ensure_ascii=False, indent=2))
        else:
            print("(no metadata)")
        return 0

    if action == "delete":
        if not name:
            print("ERROR: session name is required for delete", file=sys.stderr)
            return 2
        return _cmd_sessions_delete(name, sessions_dir)

    if action == "rename":
        if not name or not new_name:
            print("ERROR: old and new session names are required for rename", file=sys.stderr)
            return 2
        return _cmd_sessions_rename(name, new_name, sessions_dir)

    if action in {"pin", "unpin"}:
        if not name:
            print(f"ERROR: session name is required for {action}", file=sys.stderr)
            return 2
        sf = _session_file_for(name, sessions_dir)
        mf = _meta_file_for(sf)
        if not sf.exists():
            print(f"No such session: {name}")
            return 1
        meta = _load_session_meta(mf)
        meta = _ensure_meta_defaults(meta)
        meta["pinned"] = (action == "pin")
        _save_session_meta(mf, meta)
        print(f"{'Pinned' if action == 'pin' else 'Unpinned'} session: {name}")
        return 0

    if action in {"tag-add", "tag-rm"}:
        if not name:
            print(f"ERROR: session name is required for {action}", file=sys.stderr)
            return 2
        sf = _session_file_for(name, sessions_dir)
        mf = _meta_file_for(sf)
        if not sf.exists():
            print(f"No such session: {name}")
            return 1
        if not extras:
            print("ERROR: at least one tag is required", file=sys.stderr)
            return 2
        meta = _load_session_meta(mf)
        meta = _ensure_meta_defaults(meta)
        existing = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        existing = [str(t) for t in existing]
        tags = set(t.lower() for t in existing)
        if action == "tag-add":
            for t in extras:
                t2 = t.strip()
                if not t2:
                    continue
                tags.add(t2.lower())
            meta["tags"] = _normalize_tags(list(tags))
            _save_session_meta(mf, meta)
            print(f"Added tags to session: {name}")
            return 0
        else:
            for t in extras:
                tags.discard(t.strip().lower())
            meta["tags"] = _normalize_tags(list(tags))
            _save_session_meta(mf, meta)
            print(f"Removed tags from session: {name}")
            return 0

    if action == "title":
        if not name or not new_name:
            print("ERROR: session name and title are required", file=sys.stderr)
            return 2
        sf = _session_file_for(name, sessions_dir)
        mf = _meta_file_for(sf)
        if not sf.exists():
            print(f"No such session: {name}")
            return 1
        meta = _load_session_meta(mf)
        meta = _ensure_meta_defaults(meta)
        meta["title"] = new_name
        _save_session_meta(mf, meta)
        print(f"Set title for session: {name}")
        return 0

    if action == "summarize":
        if not name:
            print("ERROR: session name is required for summarize", file=sys.stderr)
            return 2
        sf = _session_file_for(name, sessions_dir)
        mf = _meta_file_for(sf)
        if not sf.exists():
            print(f"No such session: {name}")
            return 1
        msgs = _load_session_messages(sf)
        if not msgs:
            print("Session has no messages; nothing to summarize.")
            return 1

        # Use Ollama to create title/summary/tags.
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
            out = _ollama_chat(base_url=base_url, model=model, messages=messages)
            data = json.loads(out)
        except Exception as e:
            print(f"ERROR: summarize failed: {e}", file=sys.stderr)
            return 1

        title = data.get("title") if isinstance(data.get("title"), str) else ""
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        tags = data.get("tags") if isinstance(data.get("tags"), list) else []
        tags = _normalize_tags([str(t) for t in tags])

        meta = _load_session_meta(mf)
        meta = _ensure_meta_defaults(meta, model=model)
        if title:
            meta["title"] = title
        if summary:
            meta["summary"] = summary
        meta["tags"] = tags
        meta["token_estimate"] = _token_estimate_from_messages(msgs)
        _save_session_meta(mf, meta)

        print(f"Summarized session: {name}")
        return 0

    print(f"ERROR: unknown sessions action: {action}", file=sys.stderr)
    return 2


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mygpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.myGPT/config.ini)",
    )

    sub = parser.add_subparsers(dest="command")

    # Keep the existing behavior as the default "info" command.
    sub.add_parser("info", help="Show config-derived defaults (base_url, model)")

    chat_p = sub.add_parser("chat", help="Chat with the configured Ollama model")
    chat_p.add_argument("prompt", nargs="?", help="Optional single prompt (otherwise interactive)")
    chat_p.add_argument("--model", dest="model_override", help="Override model for this run")
    chat_p.add_argument("--system", help="Optional system prompt")
    chat_p.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output",
    )

    chat_p.add_argument(
        "--session",
        default="default",
        help="Conversation session name (stored under ~/.myGPT/sessions)",
    )
    chat_p.add_argument(
        "--new",
        action="store_true",
        help="Start a fresh session (delete any existing session file)",
    )
    chat_p.add_argument(
        "--sessions-dir",
        type=Path,
        help="Override the sessions directory (defaults to ~/.myGPT/sessions)",
    )

    # --- Add sessions subcommand ---
    sessions_p = sub.add_parser("sessions", help="Manage stored chat sessions")
    sessions_p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "show", "delete", "rename", "pin", "unpin", "tag-add", "tag-rm", "title", "summarize"],
        help="Action to perform",
    )
    sessions_p.add_argument("name", nargs="?", help="Session name (for delete/rename)")
    sessions_p.add_argument("new_name", nargs="?", help="Second argument (rename: NEW, title: TITLE)")
    sessions_p.add_argument("extras", nargs="*", help="Extra args (tag-add/tag-rm: tags...)")
    sessions_p.add_argument(
        "--sessions-dir",
        type=Path,
        help="Override the sessions directory (defaults to ~/.myGPT/sessions)",
    )

    args = parser.parse_args(argv)

    cmd = args.command or "info"

    if cmd == "info":
        return _cmd_info(args.config)

    if cmd == "chat":
        return _cmd_chat(
            args.config,
            args.model_override,
            args.system,
            args.prompt,
            stream=(not args.no_stream),
            session_name=args.session,
            new_session=args.new,
            sessions_dir=args.sessions_dir,
        )

    if cmd == "sessions":
        return _cmd_sessions(
            action=args.action,
            sessions_dir=args.sessions_dir,
            name=args.name,
            new_name=args.new_name,
            extras=args.extras,
        )

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
