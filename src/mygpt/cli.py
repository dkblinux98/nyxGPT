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
from mygpt.chat import chat
from mygpt.logging import configure_logging
from mygpt.rag.rag import ingest_document, retrieve_context
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore


def _list_sessions_in_dir(sessions_dir: Path) -> list[dict[str, object]]:
    sessions_dir = Path(sessions_dir).expanduser()
    if not sessions_dir.exists():
        return []

    rows: list[dict[str, object]] = []
    for sf in sorted(sessions_dir.glob("*.json")):
        # Ignore metadata files
        if sf.name.endswith(".meta.json"):
            continue
        try:
            mf = sessions.meta_file_for(sf)
            msgs = sessions.load_session_messages(sf)
            meta = sessions.load_session_meta(mf)
            rows.append(
                {
                    "name": sf.stem,
                    "file": str(sf),
                    "messages": len(msgs),
                    "modified": sf.stat().st_mtime,
                    "meta": meta,
                }
            )
        except Exception:
            # Skip unreadable/corrupt session files
            continue

    return rows


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

    # Single-prompt mode
    if prompt is not None:
        result = chat(
            prompt,
            session=session_name,
            new=new_session,
            model=model_override,
            system=system,
            config_path=str(cfg_path) if cfg_path else None,
            sessions_dir=str(sessions_dir) if sessions_dir else None,
        )
        print(result.reply)
        return 0

    # Interactive mode
    model = model_override or get_default_model(cfg)
    print(f"myGPT chat (model: {model}, session: {session_name})")
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
            result = chat(
                user_text,
                session=session_name,
                new=False,
                model=model_override,
                system=system,
                config_path=str(cfg_path) if cfg_path else None,
                sessions_dir=str(sessions_dir) if sessions_dir else None,
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue

        print(result.reply)


def cmd_sessions(action: str, name: str | None, new_name: str | None, extras: list[str], sessions_dir: Path | None) -> int:
    cfg = load_config(None)
    effective_dir = sessions_dir or get_sessions_dir(cfg)

    if action == "list":
        rows = _list_sessions_in_dir(effective_dir)
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
        rows = _list_sessions_in_dir(effective_dir)
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


def cmd_rag_ingest(doc_id: str, path: Path, ensure_schema: bool) -> int:
    text = path.read_text(encoding="utf-8")
    n = ingest_document(doc_id, text, metadata={"path": str(path)}, ensure_schema=ensure_schema)
    print(f"Ingested {n} chunks for doc_id={doc_id}")
    return 0



def cmd_rag_query(question: str, top_k: int) -> int:
    results = retrieve_context(question, top_k=top_k)
    print(f"Results: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"--- {i} ---")
        print(r.get("text", ""))
    return 0


def cmd_rag_list() -> int:
    store = CassandraVectorStore()
    try:
        rows = store.list_docs()
    finally:
        store.close()

    if not rows:
        print("No documents found in RAG store")
        return 0

    print(f"{'doc_id':<30} chunks")
    print("-" * 40)
    for r in rows:
        print(f"{r['doc_id']:<30} {r['chunks']}")
    return 0


def cmd_rag_delete(doc_id: str) -> int:
    store = CassandraVectorStore()
    try:
        store.delete_doc(doc_id)
    finally:
        store.close()

    print(f"Deleted RAG document: {doc_id}")
    return 0


def cmd_rag_wipe(confirm: bool) -> int:
    if not confirm:
        print("ERROR: refusing to wipe RAG store without --yes-really", file=sys.stderr)
        return 2

    store = CassandraVectorStore()
    try:
        store.truncate()
    finally:
        store.close()

    print("Wiped all RAG documents")
    return 0


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

    rag_p = sub.add_parser("rag", help="Retrieval-Augmented Generation commands")
    rag_sub = rag_p.add_subparsers(dest="rag_cmd", required=True)

    ingest_p = rag_sub.add_parser("ingest", help="Ingest a document into the vector store")
    ingest_p.add_argument("doc_id", help="Document ID")
    ingest_p.add_argument("path", type=Path, help="Path to text file")
    ingest_p.add_argument("--ensure-schema", action="store_true", help="Create schema if missing")

    query_p = rag_sub.add_parser("query", help="Query the vector store")
    query_p.add_argument("question", help="Query text")
    query_p.add_argument("--top-k", type=int, default=5, help="Number of results")

    list_p = rag_sub.add_parser("list", help="List ingested documents")

    delete_p = rag_sub.add_parser("delete", help="Delete a document by doc_id")
    delete_p.add_argument("doc_id", help="Document ID to delete")

    wipe_p = rag_sub.add_parser("wipe", help="Delete ALL documents (dangerous)")
    wipe_p.add_argument("--yes-really", action="store_true", help="Confirm destructive wipe")

    args = parser.parse_args(argv)
    cmd = args.command or "info"

    # Initialize centralized logging as early as possible.
    try:
        cfg0 = load_config(args.config)
        configure_logging(cfg0, console=True)
    except Exception:
        # Logging should never prevent the CLI from running.
        pass

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

    if cmd == "rag":
        if args.rag_cmd == "ingest":
            return cmd_rag_ingest(args.doc_id, args.path, args.ensure_schema)
        if args.rag_cmd == "query":
            return cmd_rag_query(args.question, args.top_k)
        if args.rag_cmd == "list":
            return cmd_rag_list()
        if args.rag_cmd == "delete":
            return cmd_rag_delete(args.doc_id)
        if args.rag_cmd == "wipe":
            return cmd_rag_wipe(args.yes_really)

    parser.print_help()
    return 2


__all__ = ["cli"]