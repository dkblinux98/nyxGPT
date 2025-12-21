from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mygpt.config import (
    load_config,
    get_default_model,
    get_ollama_base_url,
    get_sessions_dir,
)
from mygpt import sessions
from mygpt import tools_fs
from mygpt.ollama_client import ollama_chat, ollama_chat_stream


def cmd_info(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path)
    base_url = get_ollama_base_url(cfg)
    model = get_default_model(cfg)

    print("myGPT OK")
    print(f"Ollama base_url: {base_url}")
    print(f"Default model: {model}")
    return 0


def cmd_chat(
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
    base_url = get_ollama_base_url(cfg)
    model = model_override or get_default_model(cfg)

    session_file, meta_file, messages, _meta = sessions.init_session(
        session_name=session_name,
        sessions_dir=sessions_dir or get_sessions_dir(cfg),
        new_session=new_session,
        model=model,
        system=system,
    )

    def ask_once(user_text: str) -> str:
        messages.append({"role": "user", "content": user_text})

        if stream:
            reply = ollama_chat_stream(base_url=base_url, model=model, messages=messages)
        else:
            reply = ollama_chat(base_url=base_url, model=model, messages=messages)

        messages.append({"role": "assistant", "content": reply})
        sessions.persist_after_exchange(
            session_file=session_file,
            meta_file=meta_file,
            messages=messages,
            model=model,
        )
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


def cmd_sessions(action: str, name: str | None, new_name: str | None, extras: list[str], sessions_dir: Path | None) -> int:
    cfg = load_config(None)
    effective_dir = sessions_dir or get_sessions_dir(cfg)

    if action == "list":
        rows = sessions.list_sessions(effective_dir)
        if not rows:
            print(f"No sessions found in {effective_dir}")
            return 0
        for r in rows:
            meta = r.get("meta") or {}
            pinned = bool(meta.get("pinned"))
            title = meta.get("title", "")
            summary = meta.get("summary", "")
            tags = meta.get("tags") or []
            tok = meta.get("token_estimate")

            prefix = "📌 " if pinned else "- "
            line = f"{prefix}{r['name']}  (messages: {r['messages']}, tokens~: {tok}, modified: {r['modified']})"
            if tags:
                line += f"  [tags: {','.join(tags)}]"
            print(line)
            if title:
                print(f"    title: {title}")
            if summary:
                print(f"    summary: {summary}")
        return 0

    if action == "show":
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2
        rows = sessions.list_sessions(effective_dir)
        for r in rows:
            if r["name"] == name:
                print(f"Session: {name}")
                print(f"File: {r['file']}")
                print(f"Messages: {r['messages']}")
                meta = r.get("meta") or {}
                if meta:
                    import json

                    print(json.dumps(meta, ensure_ascii=False, indent=2))
                else:
                    print("(no metadata)")
                return 0
        print(f"No such session: {name}")
        return 1

    if action == "delete":
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2
        ok = sessions.delete_session(name, effective_dir)
        if not ok:
            print(f"No such session: {name}")
            return 1
        print(f"Deleted session: {name}")
        return 0

    if action == "rename":
        if not name or not new_name:
            print("ERROR: old and new names required", file=sys.stderr)
            return 2
        ok, msg = sessions.rename_session(name, new_name, effective_dir)
        if not ok:
            print(msg)
            return 1
        print(f"Renamed session: {name} -> {new_name}")
        return 0

    if action in {"pin", "unpin"}:
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2
        ok, msg = sessions.set_pinned(name, action == "pin", effective_dir)
        if not ok:
            print(msg)
            return 1
        print(f"{'Pinned' if action == 'pin' else 'Unpinned'} session: {name}")
        return 0

    if action in {"tag-add", "tag-rm"}:
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2
        if not extras:
            print("ERROR: at least one tag is required", file=sys.stderr)
            return 2
        if action == "tag-add":
            ok, msg = sessions.add_tags(name, extras, effective_dir)
        else:
            ok, msg = sessions.remove_tags(name, extras, effective_dir)
        if not ok:
            print(msg)
            return 1
        print(f"Updated tags for session: {name}")
        return 0

    if action == "title":
        if not name or not new_name:
            print("ERROR: session name and title required", file=sys.stderr)
            return 2
        ok, msg = sessions.set_title(name, new_name, effective_dir)
        if not ok:
            print(msg)
            return 1
        print(f"Set title for session: {name}")
        return 0

    if action == "summarize":
        if not name:
            print("ERROR: session name required", file=sys.stderr)
            return 2
        ok, msg = sessions.summarize_session(name, effective_dir)
        if not ok:
            print(msg)
            return 1
        print(f"Summarized session: {name}")
        return 0

    print(f"Unknown sessions action: {action}", file=sys.stderr)
    return 2


def cmd_tools(action: str, path: Path, pattern: str | None, head: int | None, tail: int | None, max_matches: int) -> int:
    if action == "ls":
        return tools_fs.ls(path)
    if action == "cat":
        return tools_fs.cat(path, head=head, tail=tail)
    if action == "grep":
        if not pattern:
            print("ERROR: pattern is required for grep", file=sys.stderr)
            return 2
        return tools_fs.grep(pattern, path, max_matches=max_matches)
    print(f"Unknown tools action: {action}", file=sys.stderr)
    return 2


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mygpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.myGPT/config.ini)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="Show config-derived defaults (base_url, model)")

    chat_p = sub.add_parser("chat", help="Chat with the configured Ollama model")
    chat_p.add_argument("prompt", nargs="?", help="Optional single prompt (otherwise interactive)")
    chat_p.add_argument("--model", dest="model_override", help="Override model for this run")
    chat_p.add_argument("--system", help="Optional system prompt")
    chat_p.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    chat_p.add_argument("--session", default="default", help="Conversation session name")
    chat_p.add_argument("--new", action="store_true", help="Start a fresh session")
    chat_p.add_argument("--sessions-dir", type=Path, help="Override sessions directory")

    sessions_p = sub.add_parser("sessions", help="Manage stored chat sessions")
    sessions_p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=[
            "list",
            "show",
            "delete",
            "rename",
            "pin",
            "unpin",
            "tag-add",
            "tag-rm",
            "title",
            "summarize",
        ],
    )
    sessions_p.add_argument("name", nargs="?", help="Session name")
    sessions_p.add_argument("new_name", nargs="?", help="Second argument (rename/title)")
    sessions_p.add_argument("extras", nargs="*", help="Extra args (tags)")
    sessions_p.add_argument("--sessions-dir", type=Path, help="Override sessions directory")

    tools_p = sub.add_parser("tools", help="Explicit local filesystem tools")
    tools_p.add_argument("action", choices=["ls", "cat", "grep"], help="Tool to run")
    tools_p.add_argument("pattern", nargs="?", help="Regex pattern (grep)")
    tools_p.add_argument("path", type=Path, help="File or directory path")
    tools_p.add_argument("--head", type=int, help="Print first N lines (cat)")
    tools_p.add_argument("--tail", type=int, help="Print last N lines (cat)")
    tools_p.add_argument("--max", dest="max_matches", type=int, default=50, help="Max matches (grep)")

    args = parser.parse_args(argv)
    cmd = args.command or "info"

    if cmd == "info":
        return cmd_info(args.config)

    if cmd == "chat":
        return cmd_chat(
            cfg_path=args.config,
            model_override=args.model_override,
            system=args.system,
            prompt=args.prompt,
            stream=(not args.no_stream),
            session_name=args.session,
            new_session=args.new,
            sessions_dir=args.sessions_dir,
        )

    if cmd == "sessions":
        return cmd_sessions(
            action=args.action,
            name=args.name,
            new_name=args.new_name,
            extras=args.extras,
            sessions_dir=args.sessions_dir,
        )

    if cmd == "tools":
        return cmd_tools(
            action=args.action,
            path=args.path,
            pattern=args.pattern,
            head=getattr(args, "head", None),
            tail=getattr(args, "tail", None),
            max_matches=getattr(args, "max_matches", 50),
        )

    parser.print_help()
    return 2


__all__ = ["cli"]