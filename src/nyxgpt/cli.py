"""Command-line entry point for nyxGPT.

Defines the `nyxgpt` argparse-based CLI: subcommands for chatting, managing
sessions, RAG ingestion/query, Ollama model management, the MCP server, the
terminal UI, and local ops/canary/self-heal operations. Each `cmd_*`
function implements one subcommand and is invoked from `cli()`, which
builds the argument parser and dispatches to the matching handler.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Canary and ops implementations live in separate modules for testability.
# (blue/green lived in nyxgpt.deploy; retired in favor of canary -- see #3409.)
from nyxgpt import canary as canary_mod
from nyxgpt import cloud as cloud_mod
from nyxgpt import cloud_deploy as cloud_deploy_mod
from nyxgpt import cloud_infra as cloud_infra_mod
from nyxgpt import cloud_provision as cloud_provision_mod
from nyxgpt import cloud_smoke as cloud_smoke_mod
from nyxgpt import cloud_state as cloud_state_mod
from nyxgpt import models, sessions
from nyxgpt import ops as ops_mod
from nyxgpt import portability as portability_mod
from nyxgpt import self_heal as self_heal_mod
from nyxgpt.aws_credentials_setup import run_aws_credentials_setup
from nyxgpt.chat import chat, chat_stream
from nyxgpt.config import (
    get_canary_error_rate_threshold,
    get_canary_latency_p95_threshold_ms,
    get_canary_min_requests,
    get_canary_namespace,
    get_canary_step_percent,
    get_canary_total_replicas,
    get_default_model,
    get_ollama_base_url,
    get_session_backend,
    get_sessions_dir,
    load_config,
)
from nyxgpt.logging import configure_logging, mint_correlation_id
from nyxgpt.rag.rag import ingest_document, retrieve_context
from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore
from nyxgpt.secrets_setup import run_secrets_setup
from nyxgpt.wizard import run_wizard


def _list_sessions_in_dir(sessions_dir: Path) -> list[dict[str, object]]:
    """Collect summary rows for every readable session file in `sessions_dir`.

    Args:
        sessions_dir: Directory containing session `*.json` files.

    Returns:
        A list of dicts (name, file, messages, modified, meta) for each
        session, or an empty list if the directory doesn't exist. Files that
        are metadata sidecars (`*.meta.json`) or fail to parse are skipped.
    """
    # Under the Cassandra backend the directory is not the store -- delegate
    # to the backend-aware listing, which returns the same row shape (#3590).
    try:
        if get_session_backend(load_config(None)) == "cassandra":
            return cast(list[dict[str, object]], sessions.list_sessions(None))
    except Exception:
        pass

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
    """Print the resolved config defaults: Ollama base URL and default model.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.

    Returns:
        0 always.
    """
    cfg = load_config(cfg_path)
    base_url = get_ollama_base_url(cfg)
    model = get_default_model(cfg)

    print("nyxGPT OK")
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
    rag_mode: bool | None = None,
) -> int:
    """Chat with the configured Ollama model, either as a single prompt or interactively.

    If `prompt` is given, sends it once (streaming to stdout unless `stream`
    is False) and returns. Otherwise starts a REPL that reads lines from
    stdin, sending each to the model, until `/exit`, `/quit`, EOF, or
    Ctrl-C.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        model_override: Model name to use instead of the configured default.
        system: Optional system prompt to prepend.
        prompt: Single prompt to send; if None, enters interactive mode.
        stream: Whether to stream the reply token-by-token.
        session_name: Name of the conversation session to use/persist.
        new_session: If True, start a fresh session instead of resuming.
        sessions_dir: Override for the directory sessions are stored in.
        rag_mode: If set, force-enable/disable RAG for this chat.

    Returns:
        0 on normal exit (including user-initiated quit).
    """
    cfg = load_config(cfg_path)

    # Single-prompt mode
    if prompt is not None:
        if stream:
            # Show typing indicator before first token
            print("⋯", end="", flush=True)
            first_chunk = True
            for chunk in chat_stream(
                prompt,
                session=session_name,
                new=new_session,
                model=model_override,
                system=system,
                config_path=str(cfg_path) if cfg_path else None,
                sessions_dir=str(sessions_dir) if sessions_dir else None,
                rag_enabled=rag_mode,
            ):
                if first_chunk:
                    # Clear typing indicator on first token
                    print("\r\033[K", end="", flush=True)
                    first_chunk = False
                print(chunk, end="", flush=True)
            print()
            return 0
        else:
            result = chat(
                prompt,
                session=session_name,
                new=new_session,
                model=model_override,
                system=system,
                config_path=str(cfg_path) if cfg_path else None,
                sessions_dir=str(sessions_dir) if sessions_dir else None,
                rag_enabled=rag_mode,
            )
            print(result.reply)
            return 0

    # Interactive mode
    model = model_override or get_default_model(cfg)
    print(f"nyxGPT chat (model: {model}, session: {session_name})")
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
            if stream:
                # Show typing indicator before first token
                print("⋯", end="", flush=True)
                first_chunk = True
                for chunk in chat_stream(
                    user_text,
                    session=session_name,
                    new=False,
                    model=model_override,
                    system=system,
                    config_path=str(cfg_path) if cfg_path else None,
                    sessions_dir=str(sessions_dir) if sessions_dir else None,
                    rag_enabled=rag_mode,
                ):
                    if first_chunk:
                        # Clear typing indicator on first token
                        print("\r\033[K", end="", flush=True)
                        first_chunk = False
                    print(chunk, end="", flush=True)
                print()
            else:
                result = chat(
                    user_text,
                    session=session_name,
                    new=False,
                    model=model_override,
                    system=system,
                    config_path=str(cfg_path) if cfg_path else None,
                    sessions_dir=str(sessions_dir) if sessions_dir else None,
                    rag_enabled=rag_mode,
                )
                print(result.reply)
        except KeyboardInterrupt:
            print()
            continue
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue


def cmd_sessions(
    action: str,
    name: str | None,
    new_name: str | None,
    extras: list[str],
    sessions_dir: Path | None,
    format: str = "markdown",
    output: Path | None = None,
    case_sensitive: bool = False,
    role: str | None = None,
    limit: int = 50,
    model: str | None = None,
    rag_enabled: bool | None = None,
    force_include: bool = False,
) -> int:
    """Manage stored chat sessions: list, inspect, edit, search, or bulk-operate on them.

    Dispatches on `action` (list, show, delete, rename, pin/unpin, tag-add/rm,
    title, summarize, export, search, merge, batch-*, stats, attach, detach,
    list-attachments). The meaning of `name`, `new_name`, and `extras` varies
    per action (e.g. for `rename` they are old/new name; for `merge` they are
    output name/input session names; for `search` `name` is the query).

    Args:
        action: Which sessions operation to perform.
        name: Primary argument (usually a session name), meaning depends on `action`.
        new_name: Secondary argument (e.g. new name, title, or doc_id), meaning depends on `action`.
        extras: Additional positional arguments (e.g. tags, session name lists).
        sessions_dir: Override for the directory sessions are stored in.
        format: Export format (markdown, json, or html).
        output: Output file path for export (stdout if not given).
        case_sensitive: Whether `search` matching is case-sensitive.
        role: Restrict `search` to a specific message role.
        limit: Maximum number of `search` results to return.
        model: Model name to set for `batch-update-meta`.
        rag_enabled: RAG-enabled flag to set for `batch-update-meta`.
        force_include: For `attach`, force-include the document in every RAG query.

    Returns:
        0 on success, 1 if the target session/action failed, 2 on invalid arguments.
    """
    cfg = load_config(None)
    effective_dir = sessions_dir or get_sessions_dir(cfg)

    if action == "list":
        rows = _list_sessions_in_dir(effective_dir)
        if not rows:
            print(f"No sessions found in {effective_dir}")
            return 0
        for r in rows:
            meta_value = r.get("meta")
            meta: dict[str, Any] = meta_value if isinstance(meta_value, dict) else {}
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
                meta_val = r.get("meta")
                meta = meta_val if isinstance(meta_val, dict) else {}
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

    if action == "export":
        if not name:
            print("ERROR: session name required", file=sys.stderr)
            return 2

        # Call appropriate export function based on format
        if format == "markdown":
            ok, content = sessions.export_session_markdown(name, effective_dir)
        elif format == "json":
            ok, content = sessions.export_session_json(name, effective_dir)
        elif format == "html":
            ok, content = sessions.export_session_html(name, effective_dir)
        else:
            print(f"ERROR: unsupported format: {format}", file=sys.stderr)
            return 2

        if not ok:
            print(content, file=sys.stderr)
            return 1

        # Write to file or stdout
        if output:
            try:
                output.write_text(content, encoding="utf-8")
                print(f"Exported session '{name}' to {output} ({format} format)")
            except OSError as e:
                print(f"ERROR: Failed to write to {output}: {e}", file=sys.stderr)
                return 1
        else:
            print(content)

        return 0

    if action == "search":
        # For search, 'name' is the search query
        if not name:
            print("ERROR: search query is required", file=sys.stderr)
            return 2

        search_query = name
        session_filter = new_name  # Optional: search within specific session

        results = sessions.search_messages(
            query=search_query,
            sessions_dir=effective_dir,
            case_sensitive=case_sensitive,
            role_filter=role,
            session_filter=session_filter,
            limit=limit,
        )

        if not results:
            print(f"No results found for '{search_query}'")
            return 0

        print(f"Found {len(results)} result(s) for '{search_query}':\n")

        # Paginate results (10 per page)
        PAGE_SIZE = 10
        total_results = len(results)

        for page_start in range(0, total_results, PAGE_SIZE):
            page_end = min(page_start + PAGE_SIZE, total_results)

            # Display results for this page
            for i in range(page_start, page_end):
                result = results[i]
                session_name = result["session_name"]
                session_title = result.get("session_title")
                msg_idx = result["message_index"]
                role_str = result["role"]
                preview = result["content_preview"]
                matches = result["matches"]

                # Display session info
                session_display = session_title if session_title else session_name
                print(
                    f"{i + 1}. [{session_display}] Message #{msg_idx} ({role_str}) - {matches} match(es)"
                )
                print(f"   {preview}")
                print()

            # Show pagination prompt if there are more results
            if page_end < total_results:
                remaining = total_results - page_end
                try:
                    response = input(
                        f"Showing {page_end}/{total_results} results. Press Enter for more ({remaining} remaining), or 'q' to quit: "
                    )
                    if response.lower().strip() == "q":
                        print(f"Stopped. {remaining} result(s) not shown.")
                        break
                except (KeyboardInterrupt, EOFError):
                    print("\nStopped.")
                    break

        return 0

    if action == "merge":
        # For merge: name is output name, extras are input session names
        if not name:
            print("ERROR: output session name is required", file=sys.stderr)
            return 2
        if not extras or len(extras) < 1:
            print("ERROR: at least one input session name is required", file=sys.stderr)
            return 2

        output_name = name
        input_names = extras

        ok, msg = sessions.merge_sessions(input_names, output_name, effective_dir)
        if not ok:
            print(f"ERROR: {msg}", file=sys.stderr)
            return 1
        print(msg)
        return 0

    if action == "batch-delete":
        # extras contains the list of session names to delete
        if not extras or len(extras) < 1:
            print("ERROR: at least one session name is required", file=sys.stderr)
            return 2

        success, failure, failed = sessions.batch_delete_sessions(extras, effective_dir)
        print(f"Deleted {success} session(s)")
        if failure > 0:
            print(
                f"Failed to delete {failure} session(s): {', '.join(failed)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if action in {"batch-tag-add", "batch-tag-rm"}:
        # name contains tags (space-separated), extras contains session names
        if not name:
            print("ERROR: at least one tag is required", file=sys.stderr)
            return 2
        if not extras or len(extras) < 1:
            print("ERROR: at least one session name is required", file=sys.stderr)
            return 2

        tags = name.split()
        session_names = extras
        is_remove = action == "batch-tag-rm"

        success, failure, failed = sessions.batch_tag_sessions(
            session_names, tags, effective_dir, remove=is_remove
        )
        op = "Removed tags from" if is_remove else "Added tags to"
        print(f"{op} {success} session(s)")
        if failure > 0:
            print(
                f"Failed to update {failure} session(s): {', '.join(failed)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if action == "batch-export":
        # extras contains the list of session names to export
        if not extras or len(extras) < 1:
            print("ERROR: at least one session name is required", file=sys.stderr)
            return 2
        if not output:
            print(
                "ERROR: --output directory is required for batch export",
                file=sys.stderr,
            )
            return 2

        success, failure, failed = sessions.batch_export_sessions(
            extras, output, effective_dir, format=format
        )
        print(f"Exported {success} session(s) to {output}")
        if failure > 0:
            print(
                f"Failed to export {failure} session(s): {', '.join(failed)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if action in {"batch-pin", "batch-unpin"}:
        # extras contains the list of session names
        if not extras or len(extras) < 1:
            print("ERROR: at least one session name is required", file=sys.stderr)
            return 2

        is_pinned = action == "batch-pin"
        success, failure, failed = sessions.batch_update_metadata(
            extras, effective_dir, pinned=is_pinned
        )
        op = "Pinned" if is_pinned else "Unpinned"
        print(f"{op} {success} session(s)")
        if failure > 0:
            print(
                f"Failed to update {failure} session(s): {', '.join(failed)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if action == "batch-update-meta":
        # extras contains the list of session names
        if not extras or len(extras) < 1:
            print("ERROR: at least one session name is required", file=sys.stderr)
            return 2

        if model is None and rag_enabled is None:
            print(
                "ERROR: at least one metadata field is required (--model, --rag-enabled)",
                file=sys.stderr,
            )
            return 2

        success, failure, failed = sessions.batch_update_metadata(
            extras, effective_dir, model=model, rag_enabled=rag_enabled
        )
        print(f"Updated metadata for {success} session(s)")
        if failure > 0:
            print(
                f"Failed to update {failure} session(s): {', '.join(failed)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if action == "stats":
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2

        # Load session data
        rows = _list_sessions_in_dir(effective_dir)
        session_data = None
        for r in rows:
            if r["name"] == name:
                session_data = r
                break

        if not session_data:
            print(f"No such session: {name}", file=sys.stderr)
            return 1

        # Extract data
        messages = sessions.load_session_messages(Path(cast(str, session_data["file"])))
        session_meta_value = session_data.get("meta")
        session_meta: dict[str, Any] = (
            session_meta_value if isinstance(session_meta_value, dict) else {}
        )

        # Calculate statistics
        total_messages = len(messages)
        user_messages = sum(1 for m in messages if m.get("role") == "user")
        assistant_messages = sum(1 for m in messages if m.get("role") == "assistant")
        system_messages = sum(1 for m in messages if m.get("role") == "system")

        # Token estimate
        token_estimate = session_meta.get("token_estimate", 0)
        if not token_estimate and messages:
            token_estimate = sessions.token_estimate_from_messages(messages)

        # Age and activity calculations
        created_at = session_meta.get("created_at", "Unknown")
        updated_at = session_meta.get("updated_at", "Unknown")

        def format_age(timestamp_str: str) -> str:
            """Format age from ISO timestamp to human-readable string."""
            if timestamp_str == "Unknown":
                return "Unknown"
            try:
                ts = datetime.fromisoformat(timestamp_str)
                # Make timezone-aware if naive to prevent comparison errors
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                delta = now - ts

                days = delta.days
                hours = delta.seconds // 3600
                minutes = (delta.seconds % 3600) // 60

                if days > 0:
                    return f"{days} day(s), {hours} hour(s)"
                elif hours > 0:
                    return f"{hours} hour(s), {minutes} minute(s)"
                elif minutes > 0:
                    return f"{minutes} minute(s)"
                else:
                    return "< 1 minute"
            except (ValueError, TypeError):
                return timestamp_str

        session_age = format_age(created_at)
        last_activity = format_age(updated_at)

        # RAG status
        rag_enabled = session_meta.get("rag_enabled", False)
        rag_status = "Enabled" if rag_enabled else "Disabled"

        # Other metadata
        model = session_meta.get("model", "Unknown")
        title = session_meta.get("title", "")
        summary = session_meta.get("summary", "")
        tags = session_meta.get("tags", [])
        pinned = session_meta.get("pinned", False)

        # Display statistics
        print(f"Session Statistics: {name}")
        print("=" * 60)

        if title:
            print(f"Title: {title}")
        if summary:
            print(f"Summary: {summary}")

        print("\nMessage Counts:")
        print(f"  Total messages: {total_messages}")
        print(f"  User messages: {user_messages}")
        print(f"  Assistant messages: {assistant_messages}")
        print(f"  System messages: {system_messages}")

        print("\nToken Estimate:")
        print(f"  Approximate tokens: {token_estimate:,}")

        print("\nSession Age & Activity:")
        print(f"  Created: {created_at}")
        print(f"  Age: {session_age}")
        print(f"  Last updated: {updated_at}")
        print(f"  Time since last activity: {last_activity}")

        print("\nConfiguration:")
        print(f"  Model: {model}")
        print(f"  RAG: {rag_status}")
        print(f"  Pinned: {'Yes' if pinned else 'No'}")

        if tags:
            print(f"  Tags: {', '.join(tags)}")

        return 0

    if action == "list-attachments":
        if not name:
            print("ERROR: session name is required", file=sys.stderr)
            return 2
        sf = sessions.session_file_for(name, effective_dir)
        mf = sessions.meta_file_for(sf)
        meta = sessions.load_session_meta(mf)
        raw = meta.get("attached_doc_ids", [])
        attached: list[str] = raw if isinstance(raw, list) else []
        if not attached:
            print(f"No documents attached to session: {name}")
        else:
            print(f"Attached documents for session '{name}':")
            for doc_id in attached:
                print(f"  - {doc_id}")
        return 0

    if action == "attach":
        if not name or not new_name:
            print("ERROR: session name and doc_id are required", file=sys.stderr)
            return 2
        doc_id = new_name
        sf = sessions.session_file_for(name, effective_dir)
        mf = sessions.meta_file_for(sf)
        if not sessions.session_file_exists(sf):
            sessions.save_session_messages(sf, [])
        meta = sessions.load_session_meta(mf)
        meta = sessions.ensure_meta_defaults(meta)
        raw = meta.get("attached_doc_ids", [])
        cur: list[str] = raw if isinstance(raw, list) else []
        if doc_id not in cur:
            cur = cur + [doc_id]
            meta["attached_doc_ids"] = cur
            sessions.save_session_meta(mf, meta)
        fi_note = " (force-include: enabled)" if force_include else ""
        print(f"Attached document '{doc_id}' to session '{name}'{fi_note}")
        return 0

    if action == "detach":
        if not name or not new_name:
            print("ERROR: session name and doc_id are required", file=sys.stderr)
            return 2
        doc_id = new_name
        sf = sessions.session_file_for(name, effective_dir)
        mf = sessions.meta_file_for(sf)
        meta = sessions.load_session_meta(mf)
        meta = sessions.ensure_meta_defaults(meta)
        raw = meta.get("attached_doc_ids", [])
        cur = raw if isinstance(raw, list) else []
        if doc_id in cur:
            cur = [d for d in cur if d != doc_id]
            meta["attached_doc_ids"] = cur
            sessions.save_session_meta(mf, meta)
        print(f"Detached document '{doc_id}' from session '{name}'")
        return 0

    print(f"Unknown sessions action: {action}", file=sys.stderr)
    return 2


def cmd_rag_ingest(
    doc_id: str,
    path: Path,
    ensure_schema: bool,
    collection: str = "default",
    model: str | None = None,
    dimension: int | None = None,
) -> int:
    """Ingest a text file into the RAG vector store under the given `doc_id`.

    Skips re-ingestion if the document's content hash is unchanged since the
    last ingest, and reports whether the document was newly ingested,
    updated, or skipped.

    Args:
        doc_id: Identifier to store the document under.
        path: Path to the text file to ingest.
        ensure_schema: Whether to create the vector store schema if missing.
        collection: Vector store collection to ingest into.
        model: Embedding model to use (default: from config).
        dimension: Embedding dimension to use (default: from config).

    Returns:
        0 on success.
    """
    text = path.read_text(encoding="utf-8")
    result = ingest_document(
        doc_id,
        text,
        metadata={"path": str(path)},
        ensure_schema=ensure_schema,
        collection=collection,
        embedding_model=model,
        embedding_dim=dimension,
    )

    status = result["status"]
    chunks = result["chunks_ingested"]
    doc_hash = result["doc_hash"]

    if status == "skipped":
        print(f"Document {doc_id} unchanged (hash: {doc_hash[:16]}...), skipped re-ingestion")
    elif status == "updated":
        print(f"Updated {chunks} chunks for doc_id={doc_id} into collection '{collection}'")
        print(f"  Document hash: {doc_hash[:16]}...")
        if result["previous_hash"]:
            print(f"  Previous hash: {result['previous_hash'][:16]}...")
    else:  # status == "ingested"
        print(f"Ingested {chunks} chunks for doc_id={doc_id} into collection '{collection}'")
        print(f"  Document hash: {doc_hash[:16]}...")

    if model:
        print(f"  Using embedding model: {model}")
    if dimension:
        print(f"  Using dimension: {dimension}")
    return 0


def cmd_rag_query(
    question: str,
    top_k: int,
    collection: str = "default",
    model: str | None = None,
    dimension: int | None = None,
    doc_ids: str | None = None,
    filename: str | None = None,
    tags: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Query the RAG vector store for chunks relevant to `question` and print them.

    Optionally restricts results using metadata filters (doc IDs, filename,
    tags, ingestion date range) built from the provided arguments.

    Args:
        question: Query text to retrieve context for.
        top_k: Number of results to return.
        collection: Vector store collection to query.
        model: Embedding model to use (default: from config).
        dimension: Embedding dimension to use (default: from config).
        doc_ids: Comma-separated document IDs to restrict results to.
        filename: Partial filename to filter results by.
        tags: Comma-separated tags results must all have.
        date_from: ISO date; only include documents ingested on/after this date.
        date_to: ISO date; only include documents ingested on/before this date.

    Returns:
        0 always.
    """
    from datetime import datetime

    from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

    # Build metadata filter if any filter params are provided
    metadata_filter = None
    if any([doc_ids, filename, tags, date_from, date_to]):
        # Parse doc_ids
        doc_ids_list = [d.strip() for d in doc_ids.split(",")] if doc_ids else None
        # Parse tags
        tags_list = [t.strip() for t in tags.split(",")] if tags else None
        # Parse dates
        date_from_dt = datetime.fromisoformat(date_from) if date_from else None
        date_to_dt = datetime.fromisoformat(date_to) if date_to else None

        metadata_filter = MetadataFilter(
            doc_ids=doc_ids_list,
            filename=filename,
            tags=tags_list,
            date_from=date_from_dt,
            date_to=date_to_dt,
        )

    results_raw = retrieve_context(
        question,
        top_k=top_k,
        debug_mode=False,  # CLI doesn't need debug info
        collection=collection,
        embedding_model=model,
        embedding_dim=dimension,
        metadata_filter=metadata_filter,
    )
    # Type narrowing: debug_mode=False means result is list[dict]
    results = cast(list[dict], results_raw)
    print(f"Results: {len(results)} (from collection '{collection}')")
    if model:
        print(f"  Using embedding model: {model}")
    if metadata_filter:
        print("  Applied metadata filters:")
        if doc_ids:
            print(f"    doc_ids: {doc_ids}")
        if filename:
            print(f"    filename: {filename}")
        if tags:
            print(f"    tags: {tags}")
        if date_from:
            print(f"    date_from: {date_from}")
        if date_to:
            print(f"    date_to: {date_to}")
    for i, r in enumerate(results, 1):
        print(f"--- {i} ---")
        print(r.get("text", ""))
        if "embedding_model" in r:
            print(f"  [model: {r.get('embedding_model')}, score: {r.get('score', 0):.3f}]")
        # Show doc_id and metadata if filtering
        if metadata_filter:
            print(f"  [doc_id: {r.get('doc_id')}, chunk_id: {r.get('chunk_id')}]")
            meta = r.get("metadata", {})
            if meta.get("filename"):
                print(f"  [filename: {meta.get('filename')}]")
            if meta.get("tags"):
                print(f"  [tags: {', '.join(meta.get('tags', []))}]")
    return 0


def cmd_rag_info(doc_id: str, collection: str = "default") -> int:
    """Print version/ingestion metadata for a single RAG document.

    Args:
        doc_id: Document ID to inspect.
        collection: Vector store collection the document belongs to.

    Returns:
        0 if the document was found, 1 if it doesn't exist in `collection`.
    """
    store = CassandraVectorStore(collection=collection)
    try:
        info = store.get_document_info(doc_id)
    finally:
        store.close()

    if not info:
        print(f"Document '{doc_id}' not found in collection '{collection}'")
        return 1

    print(f"Document: {info['doc_id']}")
    print(f"  Collection: {collection}")
    print(f"  Chunks: {info['chunks']}")
    print(f"  Embedding model: {info['embedding_model'] or 'N/A'}")
    print(f"  Document hash: {info['doc_hash'] or 'N/A'}")
    print(f"  Ingested at: {info['ingested_at'] or 'N/A'}")
    print(f"  Updated at: {info['updated_at'] or 'N/A'}")
    return 0


def cmd_rag_list(collection: str = "default") -> int:
    """Print a table of all documents ingested into a RAG collection.

    Args:
        collection: Vector store collection to list documents from.

    Returns:
        0 always.
    """
    store = CassandraVectorStore(collection=collection)
    try:
        rows = store.list_docs()
    finally:
        store.close()

    if not rows:
        print(f"No documents found in collection '{collection}'")
        return 0

    print(f"Collection: {collection}")
    print(f"{'doc_id':<30} {'chunks':<10} {'model':<30}")
    print("-" * 75)
    for r in rows:
        model_info = r.get("embedding_model", "N/A")
        print(f"{r['doc_id']:<30} {r['chunks']:<10} {model_info:<30}")
    return 0


def cmd_rag_collections() -> int:
    """List all available collections."""
    store = CassandraVectorStore()
    try:
        collections = store.list_collections()
    finally:
        store.close()

    if not collections:
        print("No collections found")
        return 0

    print("Available collections:")
    for coll in collections:
        print(f"  - {coll}")
    return 0


def cmd_rag_compare(
    test_file: Path,
    models_spec: list[str],
) -> int:
    """Compare embedding models performance.

    Args:
        test_file: Path to text file for testing
        models_spec: List of model specifications in format "model:dim:collection"
    """
    from nyxgpt.rag.model_compare import compare_models, print_comparison_table

    # Parse model specs
    models = []
    for spec in models_spec:
        parts = spec.split(":")
        if len(parts) != 3:
            print(
                f"ERROR: Invalid model spec '{spec}'. Expected format: model:dimension:collection",
                file=sys.stderr,
            )
            return 2
        model_name, dim_str, collection = parts
        try:
            dimension = int(dim_str)
        except ValueError:
            print(
                f"ERROR: Invalid dimension '{dim_str}' in spec '{spec}'",
                file=sys.stderr,
            )
            return 2
        models.append((model_name, dimension, collection))

    # Load test texts
    try:
        test_text = test_file.read_text(encoding="utf-8")
        # Use first few sentences as test texts
        test_texts = test_text[:1000].split(".")[:5]
        test_texts = [t.strip() for t in test_texts if t.strip()]
    except Exception as e:
        print(f"ERROR: Failed to read test file: {e}", file=sys.stderr)
        return 2

    if not test_texts:
        print("ERROR: Test file contains no usable text", file=sys.stderr)
        return 2

    print(f"Comparing {len(models)} embedding models...")
    print(f"Test texts: {len(test_texts)}")

    try:
        metrics = compare_models(models, test_texts)
        print_comparison_table(metrics)
        return 0
    except Exception as e:
        print(f"ERROR: Comparison failed: {e}", file=sys.stderr)
        return 2


def cmd_rag_delete(doc_id: str, collection: str = "default") -> int:
    """Delete a single document (and its chunks) from a RAG collection.

    Args:
        doc_id: Document ID to delete.
        collection: Vector store collection the document belongs to.

    Returns:
        0 always.
    """
    store = CassandraVectorStore(collection=collection)
    try:
        store.delete_doc(doc_id)
    finally:
        store.close()

    print(f"Deleted RAG document: {doc_id} from collection '{collection}'")
    return 0


def cmd_rag_wipe(confirm: bool, collection: str = "default") -> int:
    """Delete every document in a RAG collection; requires an explicit confirmation flag.

    Args:
        confirm: Must be True (from `--yes-really`) or the wipe is refused.
        collection: Vector store collection to truncate.

    Returns:
        0 on success, 2 if `confirm` is False.
    """
    if not confirm:
        print("ERROR: refusing to wipe RAG store without --yes-really", file=sys.stderr)
        return 2

    store = CassandraVectorStore(collection=collection)
    try:
        store.truncate()
    finally:
        store.close()

    print(f"Wiped all RAG documents from collection '{collection}'")
    return 0


def cmd_rag_index_repo(
    repo_path: Path,
    prefix: str,
    extensions: str | None,
    docs_only: bool,
    ensure_schema: bool,
    collection: str = "default",
    model: str | None = None,
    dimension: int | None = None,
) -> int:
    """Index a code repository for RAG."""
    from nyxgpt.rag.rag import ingest_repository

    # Parse extensions if provided
    extensions_set = None
    if extensions:
        extensions_set = {ext.strip() for ext in extensions.split(",")}
        # Ensure extensions start with a dot
        extensions_set = {ext if ext.startswith(".") else f".{ext}" for ext in extensions_set}

    try:
        result = ingest_repository(
            repo_path=str(repo_path),
            doc_id_prefix=prefix,
            extensions=extensions_set,
            extract_docs_only=docs_only,
            ensure_schema=ensure_schema,
            collection=collection,
            embedding_model=model,
            embedding_dim=dimension,
        )

        print(f"Indexed repository: {repo_path} into collection '{collection}'")
        print(f"  Files indexed: {result['total_files']}")
        print(f"  Total chunks: {result['total_chunks']}")
        print(f"  Document ID prefix: {prefix}")
        if docs_only:
            print("  Mode: Documentation only (comments/docstrings)")
        else:
            print("  Mode: Full code")

        if model:
            print(f"  Using embedding model: {model}")
        if dimension:
            print(f"  Using dimension: {dimension}")

        return 0
    except Exception as e:
        print(f"ERROR: Failed to index repository: {e}", file=sys.stderr)
        return 1


def cmd_models_list() -> int:
    """List all available Ollama models."""
    try:
        model_list = models.list_models()
        if not model_list:
            print("No models found")
            return 0

        print(f"{'Model Name':<40} {'Size':<12} {'Modified'}")
        print("-" * 80)
        for model in model_list:
            name = model.get("name", "")
            size = models.format_model_size(model.get("size", 0))
            modified = model.get("modified_at", "")
            print(f"{name:<40} {size:<12} {modified}")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to list models: {e}", file=sys.stderr)
        return 1


def cmd_models_pull(name: str) -> int:
    """Pull (download) a model from Ollama library."""
    try:

        def progress_callback(status: str, percentage: float):
            print(f"\r{status}: {percentage:.1f}%", end="", flush=True)

        print(f"Pulling model: {name}")
        models.pull_model(name, progress_callback=progress_callback)
        print()  # New line after progress
        print(f"Successfully pulled model: {name}")
        return 0
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: Failed to pull model: {e}", file=sys.stderr)
        return 1


def cmd_models_delete(name: str, force: bool) -> int:
    """Delete a model from Ollama."""
    if not force:
        try:
            response = input(f"Delete model '{name}'? (y/N): ").strip().lower()
            if response != "y":
                print("Cancelled")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled")
            return 0

    try:
        models.delete_model(name)
        print(f"Deleted model: {name}")
        return 0
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: Failed to delete model: {e}", file=sys.stderr)
        return 1


def cmd_models_show(name: str) -> int:
    """Show detailed information about a model."""
    try:
        info = models.show_model(name)
        print(f"Model: {name}")
        print()

        # Display key fields
        if "modelfile" in info:
            print("Modelfile:")
            print(info["modelfile"])
            print()

        if "parameters" in info:
            print("Parameters:")
            print(info["parameters"])
            print()

        if "template" in info:
            print("Template:")
            print(info["template"])
            print()

        # Display other fields as JSON
        import json

        other_fields = {
            k: v for k, v in info.items() if k not in ("modelfile", "parameters", "template")
        }
        if other_fields:
            print("Other info:")
            print(json.dumps(other_fields, indent=2, ensure_ascii=False))

        return 0
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: Failed to get model info: {e}", file=sys.stderr)
        return 1


def cmd_mcp() -> int:
    """Run the nyxGPT MCP server on stdio.

    Starts a Model Context Protocol (MCP) server that exposes nyxGPT as a
    tool provider. Connect it from Claude Desktop or any other MCP-compatible
    client by configuring it as a stdio-transport server.

    Returns:
        0 on clean exit, 1 on error.
    """
    try:
        from nyxgpt.mcp_server import serve

        serve()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"ERROR: MCP server failed: {exc}", file=sys.stderr)
        return 1


def _canary_namespace(cfg_path: Path | None, override: str | None) -> str:
    """Resolve the Kubernetes namespace for canary commands: `override` if given, else config."""
    if override:
        return override
    return get_canary_namespace(load_config(cfg_path))


def cmd_canary_status(cfg_path: Path | None, namespace: str | None, component: str = "api") -> int:
    """Print canary rollout progress, stable/canary health/version, and live traffic metrics.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        component: Which component's stable/canary pair to report on (default: "api"; #3419).

    Returns:
        0 always.
    """
    ns = _canary_namespace(cfg_path, namespace)
    data = canary_mod.status(ns, component=component)
    rollout_state = "in progress" if data["active"] else "idle"
    print(
        f"Canary rollout: {rollout_state} at {data['weight_percent']}% (namespace={data['namespace']})"
    )
    if not data["mode_supported"]:
        print(f"  note: {data['mode_message']}")
    for track in ("stable", "canary"):
        info = data[track]
        version = f", version={info['version']}" if info["version"] else ""
        print(f"  {track}: {info['state']} - {info['message']}{version}")
    metrics = data["metrics"]
    print(
        f"  metrics: {metrics['total_requests']} requests, "
        f"error_rate={metrics['error_rate_percent']:.2f}%, p95={metrics['p95_latency_ms']:.2f}ms"
    )
    if data["history"]:
        print("\nRecent actions:")
        for entry in data["history"]:
            print(f"  {entry}")
    return 0


def cmd_canary_deploy(cfg_path: Path | None, namespace: str | None, component: str = "api") -> int:
    """Build the current checkout into a versioned image and deploy it to canary only.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        component: Which component to deploy (default: "api"; "api" or "web", #3419).

    Returns:
        0 if the deploy succeeded, 2 if it failed (stable is left untouched either way).
    """
    ns = _canary_namespace(cfg_path, namespace)
    result = canary_mod.deploy(namespace=ns, component=component)
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    if result.details:
        print(f"  {result.details}")
    return 0 if result.ok else 2


def cmd_canary_start(
    cfg_path: Path | None, namespace: str | None, weight_percent: int, component: str = "api"
) -> int:
    """Start a canary rollout, initially routing `weight_percent` of traffic to it.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        weight_percent: Initial percentage of traffic to route to the canary.
        component: Which component to start (default: "api"; "api" or "web", #3419).

    Returns:
        0 if the rollout started successfully, 2 if it failed.
    """
    ns = _canary_namespace(cfg_path, namespace)
    cfg = load_config(cfg_path)
    result = canary_mod.start(
        namespace=ns,
        weight_percent=weight_percent,
        total_replicas=get_canary_total_replicas(cfg),
        component=component,
    )
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    if result.details:
        print(f"  {result.details}")
    return 0 if result.ok else 2


def cmd_canary_evaluate(
    cfg_path: Path | None, namespace: str | None, component: str = "api"
) -> int:
    """Check the canary's live error-rate/latency metrics against configured thresholds.

    Automatically rolls back the canary if it's regressing (see
    `canary.evaluate`).

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        component: Which component to evaluate (default: "api"; "api" or "web", #3419).

    Returns:
        0 if the canary passed evaluation, 2 if it failed (and was rolled back).
    """
    ns = _canary_namespace(cfg_path, namespace)
    cfg = load_config(cfg_path)
    result = canary_mod.evaluate(
        ns,
        error_rate_threshold_percent=get_canary_error_rate_threshold(cfg),
        latency_p95_threshold_ms=get_canary_latency_p95_threshold_ms(cfg),
        min_requests=get_canary_min_requests(cfg),
        component=component,
    )
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    if result.details:
        print(f"  {result.details}")
    return 0 if result.ok else 2


def cmd_canary_promote(
    cfg_path: Path | None,
    namespace: str | None,
    step_percent: int | None,
    component: str = "api",
) -> int:
    """Increase the canary's traffic share by a step percentage.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        step_percent: Percentage points to add (default: from config).
        component: Which component to promote (default: "api"; "api" or "web", #3419).

    Returns:
        0 if the promotion succeeded, 2 if it failed.
    """
    ns = _canary_namespace(cfg_path, namespace)
    cfg = load_config(cfg_path)
    result = canary_mod.promote(
        namespace=ns,
        step_percent=step_percent if step_percent is not None else get_canary_step_percent(cfg),
        total_replicas=get_canary_total_replicas(cfg),
        component=component,
    )
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    if result.details:
        print(f"  {result.details}")
    return 0 if result.ok else 2


def cmd_canary_rollback(
    cfg_path: Path | None, namespace: str | None, component: str = "api"
) -> int:
    """Cut all traffic back to the stable deployment, abandoning the canary.

    Args:
        cfg_path: Optional path to a config.ini to load instead of the default.
        namespace: Kubernetes namespace override (default: from config).
        component: Which component to roll back (default: "api"; "api" or "web", #3419).

    Returns:
        0 if the rollback succeeded, 2 if it failed.
    """
    ns = _canary_namespace(cfg_path, namespace)
    cfg = load_config(cfg_path)
    result = canary_mod.rollback(
        namespace=ns, total_replicas=get_canary_total_replicas(cfg), component=component
    )
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    if result.details:
        print(f"  {result.details}")
    return 0 if result.ok else 2


def cmd_self_heal_status(_cfg_path: Path | None) -> int:
    """Print whether the self-heal watchdog is enabled, per-component health, and recent heal events.

    Args:
        _cfg_path: Unused; accepted for symmetry with the other config-aware commands.

    Returns:
        0 always.
    """
    data = self_heal_mod.status()
    print(f"Self-heal watchdog: {'enabled' if data['enabled'] else 'disabled'}")
    if not data["components"]:
        print("No Docker Compose containers found (is the stack up? `docker compose up -d`)")
        return 0
    for c in data["components"]:
        marker = "OK" if c["healthy"] or not c["desired"] else "!!"
        health = c["health"] or "n/a"
        suffix = " (disabled -- not auto-healed)" if not c["desired"] else ""
        print(f" [{marker}] {c['service']}: state={c['state']} health={health}{suffix}")
    if data["events"]:
        print("\nRecent heal events:")
        for e in data["events"][-10:]:
            print(
                f"  {e['service']}: {e['action']} ({'ok' if e['ok'] else 'FAILED'}) - {e['reason']}"
            )
    return 0


def cmd_self_heal_toggle(enabled: bool) -> int:
    """Enable or disable the automatic self-heal watchdog.

    Args:
        enabled: True to enable automatic self-healing, False to disable it.

    Returns:
        0 always.
    """
    result = self_heal_mod.set_enabled(enabled)
    print(f"Self-heal watchdog: {'enabled' if result else 'disabled'}")
    return 0


def cmd_self_heal_heal(service: str | None) -> int:
    """Manually trigger a self-heal restart for an unhealthy component (or all of them).

    Args:
        service: Compose service name to restart; if None, heals every
            currently unhealthy component.

    Returns:
        0 if healing succeeded (or nothing needed healing), 2 on failure.
    """
    result = self_heal_mod.heal_now(service=service)
    if result.get("error"):
        print(f"[FAIL] {result['error']}")
        return 2
    if not result["healed"]:
        print("Nothing to heal (all checked components are healthy)")
        return 0
    ok = True
    for event in result["healed"]:
        print(f"[{'OK' if event['ok'] else 'FAIL'}] {event['service']}: {event['message']}")
        ok = ok and event["ok"]
    return 0 if ok else 2


def _add_install_arguments(parser: argparse.ArgumentParser) -> None:
    """Add `ops install`'s arguments to `parser`.

    Shared with the top-level `up` alias (#3504) so the two can never drift:
    `up` is meant to accept exactly the same mode flags `ops install` does
    and pass them straight through.
    """
    parser.add_argument("--repo-dir", help="Path to nyxGPT repo root")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument(
        "--skip-observability",
        action="store_true",
        help="Don't start the Grafana/Loki/Jaeger/GlitchTip Compose profiles",
    )
    parser.add_argument(
        "--terraform",
        action="store_true",
        help=(
            "Deploy the core stack via Terraform (init/plan/apply) instead of native/Homebrew "
            "reconciliation -- requires --local"
        ),
    )
    parser.add_argument(
        "--kubernetes",
        action="store_true",
        help=(
            "Deploy nyxgpt-api to a local Kubernetes cluster instead of native/Homebrew "
            "reconciliation -- requires --local. Uses an existing reachable cluster if "
            "kubectl is already configured, otherwise provisions a local kind cluster"
        ),
    )
    locality = parser.add_mutually_exclusive_group()
    locality.add_argument(
        "--local",
        action="store_true",
        help=(
            "Target the local machine (required with --terraform/--kubernetes; the only "
            "locality implemented today)"
        ),
    )
    locality.add_argument(
        "--cloud",
        action="store_true",
        help="Target a cloud deployment (not yet implemented -- --local is the precursor)",
    )
    parser.add_argument(
        "--api-key",
        help=(
            "API key for the Terraform/Kubernetes deploy's auth secret "
            "(skips the interactive prompt/auto-generation)"
        ),
    )


def _add_down_arguments(parser: argparse.ArgumentParser) -> None:
    """Add `ops down`'s arguments to `parser`.

    Shared with the top-level `down` alias (#3504) so the two can never drift.
    """
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--app-only",
        action="store_true",
        dest="app_only",
        help="Only tear down the core app tier (api/web/ollama/cassandra), leave observability up",
    )
    scope.add_argument(
        "--observability-only",
        action="store_true",
        dest="observability_only",
        help="Only tear down the observability Compose profiles, leave the app tier up",
    )
    scope.add_argument(
        "--terraform",
        action="store_true",
        help="Tear down the Terraform-managed stack (terraform destroy) instead of native/Compose",
    )
    scope.add_argument(
        "--kubernetes",
        action="store_true",
        help=(
            "Remove the nyxgpt namespace's Kubernetes resources instead of native/Compose "
            "-- also deletes the local kind cluster if nyxgpt provisioned it, never a "
            "bring-your-own cluster"
        ),
    )
    parser.add_argument(
        "--volumes",
        action="store_true",
        help="Also remove Compose data volumes (Cassandra/Postgres/Grafana/etc.) -- destructive",
    )
    parser.add_argument(
        "--yes-really",
        action="store_true",
        dest="yes_really",
        help="Required together with --volumes to confirm destructive volume removal",
    )


def cli(argv: list[str] | None = None) -> int:
    """Entry point for the `nyxgpt` command-line tool.

    Builds the full argparse parser (chat, sessions, rag, models, mcp,
    wizard, ops, cloud, canary, self-heal subcommands), parses `argv`,
    initializes logging, and dispatches to the matching `cmd_*` handler. If
    no subcommand is given, defaults to `info`. Prints help and returns 2 if
    the resolved command/subcommand combination isn't recognized.

    Args:
        argv: Argument list to parse (default: `sys.argv[1:]` via argparse).

    Returns:
        The invoked subcommand's exit code (0 for success by convention).
    """
    parser = argparse.ArgumentParser(prog="nyxgpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.nyxGPT/config.ini)",
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
    chat_p.add_argument(
        "--rag-mode",
        action="store_true",
        default=None,
        help="Enable RAG for this chat request",
    )

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
            "export",
            "search",
            "merge",
            "batch-delete",
            "batch-tag-add",
            "batch-tag-rm",
            "batch-export",
            "batch-pin",
            "batch-unpin",
            "batch-update-meta",
            "stats",
            "attach",
            "detach",
            "list-attachments",
        ],
    )
    sessions_p.add_argument("name", nargs="?", help="Session name")
    sessions_p.add_argument("new_name", nargs="?", help="Second argument (rename/title/doc_id)")
    sessions_p.add_argument("extras", nargs="*", help="Extra args (tags)")
    sessions_p.add_argument("--sessions-dir", type=Path, help="Override sessions directory")
    sessions_p.add_argument(
        "--format",
        choices=["markdown", "json", "html"],
        default="markdown",
        help="Export format (default: markdown)",
    )
    sessions_p.add_argument("--output", type=Path, help="Output file (default: stdout)")
    sessions_p.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Case-sensitive search (search only)",
    )
    sessions_p.add_argument(
        "--role",
        choices=["user", "assistant", "system"],
        help="Filter by message role (search only)",
    )
    sessions_p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of search results (default: 20, search only)",
    )
    sessions_p.add_argument(
        "--model", help="Model name for batch-update-meta (batch-update-meta only)"
    )
    sessions_p.add_argument(
        "--rag-enabled",
        type=lambda x: x.lower() in ("true", "1", "yes"),
        help="RAG enabled flag for batch-update-meta (batch-update-meta only)",
    )
    sessions_p.add_argument(
        "--force-include",
        action="store_true",
        default=False,
        dest="force_include",
        help="Force-include attached document in every RAG query (attach only)",
    )

    rag_p = sub.add_parser("rag", help="Retrieval-Augmented Generation commands")
    rag_sub = rag_p.add_subparsers(dest="rag_cmd", required=True)

    ingest_p = rag_sub.add_parser("ingest", help="Ingest a document into the vector store")
    ingest_p.add_argument("doc_id", help="Document ID")
    ingest_p.add_argument("path", type=Path, help="Path to text file")
    ingest_p.add_argument("--ensure-schema", action="store_true", help="Create schema if missing")
    ingest_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )
    ingest_p.add_argument("--model", help="Override embedding model (default: from config)")
    ingest_p.add_argument(
        "--dimension",
        type=int,
        help="Override embedding dimension (default: from config)",
    )

    query_p = rag_sub.add_parser("query", help="Query the vector store")
    query_p.add_argument("question", help="Query text")
    query_p.add_argument("--top-k", type=int, default=5, help="Number of results")
    query_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )
    query_p.add_argument("--model", help="Override embedding model (default: from config)")
    query_p.add_argument(
        "--dimension",
        type=int,
        help="Override embedding dimension (default: from config)",
    )
    query_p.add_argument(
        "--doc-ids",
        help="Filter by document IDs (comma-separated)",
    )
    query_p.add_argument(
        "--filename",
        help="Filter by filename (partial match)",
    )
    query_p.add_argument(
        "--tags",
        help="Filter by tags (comma-separated, must have ALL)",
    )
    query_p.add_argument(
        "--date-from",
        help="Filter by ingestion date >= (ISO format: YYYY-MM-DD)",
    )
    query_p.add_argument(
        "--date-to",
        help="Filter by ingestion date <= (ISO format: YYYY-MM-DD)",
    )

    list_p = rag_sub.add_parser("list", help="List ingested documents")
    list_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )

    info_p = rag_sub.add_parser("info", help="Show document version information")
    info_p.add_argument("doc_id", help="Document ID to inspect")
    info_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )

    _ = rag_sub.add_parser("collections", help="List all available collections")

    compare_p = rag_sub.add_parser("compare", help="Compare embedding models performance")
    compare_p.add_argument("test_file", type=Path, help="Path to test file")
    compare_p.add_argument(
        "models",
        nargs="+",
        help="Model specs in format 'model:dimension:collection' (e.g., 'nomic-embed-text:768:default')",
    )

    delete_p = rag_sub.add_parser("delete", help="Delete a document by doc_id")
    delete_p.add_argument("doc_id", help="Document ID to delete")
    delete_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )

    wipe_p = rag_sub.add_parser("wipe", help="Delete ALL documents (dangerous)")
    wipe_p.add_argument("--yes-really", action="store_true", help="Confirm destructive wipe")
    wipe_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )

    # Add index-repo command for code repository indexing
    index_repo_p = rag_sub.add_parser("index-repo", help="Index a code repository for RAG")
    index_repo_p.add_argument("repo_path", type=Path, help="Path to repository root")
    index_repo_p.add_argument(
        "--prefix",
        default="code",
        help="Document ID prefix (default: code)",
    )
    index_repo_p.add_argument(
        "--extensions",
        help="File extensions to include (comma-separated, e.g., '.py,.js'). If omitted, all supported languages are indexed.",
    )
    index_repo_p.add_argument(
        "--docs-only",
        action="store_true",
        help="Extract only comments/docstrings (exclude code)",
    )
    index_repo_p.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Create schema if missing",
    )
    index_repo_p.add_argument(
        "--collection",
        default="default",
        help="Collection name (default: default)",
    )
    index_repo_p.add_argument(
        "--model",
        help="Override embedding model (default: from config)",
    )
    index_repo_p.add_argument(
        "--dimension",
        type=int,
        help="Override embedding dimension (default: from config)",
    )

    # Add models command
    models_p = sub.add_parser("models", help="Manage Ollama models")
    models_sub = models_p.add_subparsers(dest="models_cmd", required=True)

    models_sub.add_parser("list", help="List all available models")

    models_pull_p = models_sub.add_parser("pull", help="Pull (download) a model")
    models_pull_p.add_argument("model", help="Model name (e.g., llama3.1:8b)")

    models_delete_p = models_sub.add_parser("delete", help="Delete a model")
    models_delete_p.add_argument("model", help="Model name to delete")
    models_delete_p.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    models_show_p = models_sub.add_parser("show", help="Show detailed model information")
    models_show_p.add_argument("model", help="Model name to inspect")

    # Add mcp command
    sub.add_parser(
        "mcp",
        help="Start the MCP (Model Context Protocol) server on stdio",
    )

    # Add wizard command
    wizard_p = sub.add_parser("wizard", help="Run interactive configuration wizard")
    wizard_p.add_argument(
        "--output",
        type=Path,
        help="Output path for config.ini (default: ~/.nyxGPT/config.ini)",
    )

    # Add up/down aliases (#3504): thin, single-code-path wrappers around
    # `ops install`/`ops down` that most operators reach for by muscle memory
    # before discovering `nyxgpt ops`.
    up_p = sub.add_parser(
        "up",
        help="Bring up the full local stack (alias for `ops install`; waits for health, prints the web URL)",
    )
    _add_install_arguments(up_p)
    up_p.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for components to report healthy before giving up (default: 180)",
    )
    up_p.add_argument(
        "--no-wait",
        action="store_true",
        dest="no_wait",
        help="Return as soon as install finishes, without waiting for component health",
    )

    down_p = sub.add_parser("down", help="Tear down the full stack (alias for `ops down`)")
    _add_down_arguments(down_p)

    # Add secrets command
    secrets_p = sub.add_parser("secrets", help="Guided setup for human-provided secrets")
    secrets_sub = secrets_p.add_subparsers(dest="secrets_cmd", required=True)

    secrets_setup_p = secrets_sub.add_parser(
        "setup",
        help=(
            "Interactively set [auth] api_key, [openai] api_key, and [github] pat with "
            "masked input, per-key help, and format validation"
        ),
    )
    secrets_setup_p.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (default: ~/.nyxGPT/config.ini)",
    )
    secrets_setup_p.add_argument(
        "--reconfigure",
        action="store_true",
        help="Prompt for every secret again, even ones already set",
    )

    # Add ops command
    ops_p = sub.add_parser("ops", help="Operational helpers")
    ops_sub = ops_p.add_subparsers(dest="ops_cmd", required=True)

    def _add_quiet_flag(parser: argparse.ArgumentParser) -> None:
        """Add the shared `--quiet` flag to a long-running `ops` subcommand's parser.

        Default is live per-step progress (`[n/m] step...` announcements, a
        heartbeat for slow steps, and a final slow-step summary); `--quiet`
        drops back to the old terse OK/FAIL-only-per-result output, for
        scripting (#3558).
        """
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Terse output for scripting: OK/FAIL/SKIP per step, no live progress",
        )

    ops_install = ops_sub.add_parser("install", help="Install operational helpers")
    _add_install_arguments(ops_install)
    _add_quiet_flag(ops_install)

    ops_status = ops_sub.add_parser(
        "status", help="Show status of local services (docker/cassandra/agent/api)"
    )
    ops_status.add_argument(
        "--api-url",
        help="Override API base URL (default: from config or http://127.0.0.1:8000)",
    )
    ops_status.add_argument("--timeout", type=float, default=2.0, help="Timeout seconds for checks")

    ops_doctor = ops_sub.add_parser(
        "doctor", help="Run checks and return non-zero if something is broken"
    )
    ops_doctor.add_argument(
        "--api-url",
        help="Override API base URL (default: from config or http://127.0.0.1:8000)",
    )
    ops_doctor.add_argument("--timeout", type=float, default=2.0, help="Timeout seconds for checks")

    ops_restart = ops_sub.add_parser("restart", help="Restart local services")
    ops_restart.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=[
            "all",
            "api",
            "web",
            "ollama",
            "cassandra",
            "cassandra-logs",
            "observability",
        ],
        help=(
            "Service to restart -- 'all' also restarts every running "
            "observability Compose service (monitoring/logging/tracing/errors)"
        ),
    )
    _add_quiet_flag(ops_restart)

    ops_stop = ops_sub.add_parser("stop", help="Stop local services (native and/or Docker Compose)")
    ops_stop.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=[
            "all",
            "api",
            "web",
            "ollama",
            "cassandra",
            "cassandra-logs",
            "observability",
        ],
        help="Service to stop",
    )
    _add_quiet_flag(ops_stop)

    ops_down = ops_sub.add_parser(
        "down", help="Tear down the full stack (native services + Docker Compose)"
    )
    _add_down_arguments(ops_down)
    _add_quiet_flag(ops_down)

    ops_env_sync = ops_sub.add_parser(
        "env-sync",
        help="Derive Docker Compose's .env secrets from config.ini (single source of truth)",
    )
    ops_env_sync.add_argument("--config", help="Path to config.ini (default: ~/.nyxGPT/config.ini)")
    ops_env_sync.add_argument(
        "--env-file", help="Path to the .env file to update (default: <repo>/.env)"
    )
    _add_quiet_flag(ops_env_sync)

    ops_secrets_sync = ops_sub.add_parser(
        "secrets-sync",
        help=(
            "Push config.ini's write-once secrets (Slack bot token, agent PATs, ...) to this "
            "repo's GitHub Actions secrets, one direction only (config.ini -> Actions)"
        ),
    )
    ops_secrets_sync.add_argument(
        "--config", help="Path to config.ini (default: ~/.nyxGPT/config.ini)"
    )
    ops_secrets_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which secrets would be pushed without contacting the GitHub API",
    )

    ops_logs = ops_sub.add_parser(
        "logs",
        help="Show recent logs for a component, in whichever mode it's actually running",
    )
    ops_logs.add_argument(
        "service", help="Component name, e.g. glitchtip, api, web, ollama, cassandra"
    )
    ops_logs.add_argument(
        "--tail", type=int, default=200, help="Number of trailing log lines to show (default: 200)"
    )

    ops_glitchtip_init = ops_sub.add_parser(
        "glitchtip-init",
        help=(
            "Auto-provision a GlitchTip admin user, org, project, and DSN "
            "(zero-touch error tracking); no-ops if glitchtip isn't up/healthy"
        ),
    )
    _add_quiet_flag(ops_glitchtip_init)

    ops_sub.add_parser(
        "alert-test",
        help=(
            "Fire a synthetic alert into Grafana's Alertmanager to verify the alerting "
            "pipeline (rules -> notification policy -> Slack contact point) end to end"
        ),
    )

    ops_observability = ops_sub.add_parser(
        "observability",
        help=(
            "Start the Grafana/Loki/Jaeger/GlitchTip Compose profiles "
            "(monitoring/logging/tracing/errors) without a raw docker compose command"
        ),
    )
    _add_quiet_flag(ops_observability)

    ops_sub.add_parser(
        "migrate-volumes",
        help=(
            "Migrate container data from pre-#3346 named Docker volumes into "
            "~/.nyxGPT/volumes/ (also run automatically by `nyxgpt ops install`)"
        ),
    )

    ops_port_forward = ops_sub.add_parser(
        "port-forward",
        help=(
            "Forward the Kubernetes web Service to localhost "
            "(wraps `kubectl port-forward` -- see `--kubernetes` in docs/kubernetes.md#4-verify)"
        ),
    )
    ops_port_forward.add_argument(
        "--port", type=int, default=3000, help="Local port to forward to (default: 3000)"
    )

    ops_verify = ops_sub.add_parser(
        "verify",
        help=(
            "Live smoke harness: boot the stack, generate known chat/RAG traffic, assert it "
            "via Prometheus/Grafana, and screenshot the touched dashboards (#3555)"
        ),
    )
    ops_verify.add_argument(
        "--skip-boot",
        action="store_true",
        help="Assume the stack (native or Compose) is already up instead of booting it",
    )
    ops_verify.add_argument(
        "--keep-up",
        action="store_true",
        help="Leave the stack running afterwards instead of tearing it down (ignored with --skip-boot)",
    )
    ops_verify.add_argument(
        "--skip-screenshots",
        action="store_true",
        help="Skip Playwright dashboard screenshots (e.g. on a host with no browsers installed)",
    )
    ops_verify.add_argument(
        "--screenshot-dir",
        help="Where to write dashboard screenshots (default: ~/.nyxGPT/verify-artifacts)",
    )
    ops_verify.add_argument(
        "--dashboards",
        nargs="*",
        help=(
            "Dashboard filenames under docker/grafana/dashboards/ to assert/screenshot "
            "(default: rag-performance, api-metrics -- the ones this harness's traffic touches)"
        ),
    )
    ops_verify.add_argument(
        "--api-url", help="Override API base URL (default: from config, http://127.0.0.1:<port>)"
    )
    ops_verify.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for the booted stack to become healthy (default: 300)",
    )

    # `ops portability` -- the repo-less portability matrix and the
    # clean-machine acceptance sequence (P6-16, #3516). A report by default so
    # it runs anywhere; `--strict` turns it into a gate that fails while any
    # target still needs a repo checkout.
    ops_portability = ops_sub.add_parser(
        "portability",
        help=(
            "Report the repo-less portability matrix (macOS, Linux, Compose, Kubernetes, "
            "AWS EC2) and the clean-machine acceptance sequence (#3516)"
        ),
    )
    ops_portability.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero unless every in-scope target is installable and operable "
            "without a repo checkout (for CI, once the open gaps close)"
        ),
    )
    ops_portability.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable matrix instead of the operator report",
    )

    # Add cloud command (AWS deployment lifecycle -- P6-11-class scope). Today
    # covers `allow-ip`, the lockout-recovery path for the owner-IP-scoped
    # SSH security group described in
    # product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md;
    # `credentials-setup`, the guided AWS identity flow (P6-13, #3512) -- see
    # nyxgpt.aws_credentials_setup's module docstring; `user-data`
    # (P6-12/#3511's target-OS provisioning bootstrap-script renderer); and
    # `infra`/`state`/`deploy`/`destroy`/`tunnel`/`smoke`. `deploy` is what
    # writes ~/.nyxGPT/cloud/state.json so allow-ip can auto-discover the
    # security group without --security-group-id -- see nyxgpt.cloud's module
    # docstring.
    cloud_p = sub.add_parser("cloud", help="AWS cloud deployment lifecycle helpers")
    cloud_sub = cloud_p.add_subparsers(dest="cloud_cmd", required=True)

    cloud_allow_ip = cloud_sub.add_parser(
        "allow-ip",
        help=(
            "Refresh the SSH (port 22) security-group ingress rule to the caller's "
            "current public IP -- talks only to the AWS API, so it works even while "
            "locked out of the instance"
        ),
    )
    cloud_allow_ip.add_argument(
        "--ip",
        help=(
            "Explicit IP or CIDR to allow instead of auto-detecting the caller's "
            "current public IP (bare addresses are scoped to /32; 0.0.0.0/0 is refused)"
        ),
    )
    cloud_allow_ip.add_argument(
        "--security-group-id",
        help=(
            "Security group id to update (default: read from "
            "~/.nyxGPT/cloud/state.json, written by `nyxgpt cloud deploy`)"
        ),
    )
    cloud_allow_ip.add_argument(
        "--region",
        help=(
            "AWS region (default: read from ~/.nyxGPT/cloud/state.json, then "
            "boto3's normal region resolution)"
        ),
    )

    # nyxgpt cloud user-data (P6-12/#3511): renders the EC2 user-data
    # bootstrap script for a target instance OS -- the OS-dispatch layer
    # future `nyxgpt cloud deploy`/the Terraform AWS module (P6-11/P6-8)
    # will embed as an instance's `user_data`. See
    # src/nyxgpt/cloud_provision.py and docs/cloud.md's target-OS support
    # matrix.
    cloud_user_data = cloud_sub.add_parser(
        "user-data",
        help=(
            "Render the EC2 user-data bootstrap script that installs nyxGPT from "
            "published artifacts (no git clone) for a target instance OS"
        ),
    )
    cloud_user_data.add_argument(
        "--os",
        required=True,
        choices=cloud_provision_mod.OS_FAMILIES,
        help="Target instance OS family",
    )
    cloud_user_data.add_argument(
        "--version",
        help=(
            "Pin the installed nyxGPT version (Linux: pip install nyxgpt==<version>; "
            "macOS: recorded for reference only -- the Homebrew tap always tracks its "
            "current formula). Default: latest."
        ),
    )
    cloud_user_data.add_argument(
        "--output",
        help="Write the rendered script to this path instead of stdout",
    )

    cloud_credentials_setup = cloud_sub.add_parser(
        "credentials-setup",
        help=(
            "Guided setup for the AWS identity nyxGPT uses for its own AWS API calls "
            "(P6-13, #3512) -- masked entry, routed to ~/.aws/credentials, the OS "
            "keychain, or left to an already-configured source; never config.ini"
        ),
    )
    cloud_credentials_setup.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (default: ~/.nyxGPT/config.ini)",
    )

    # `cloud infra` -- the wrapped lifecycle of the AWS substrate itself
    # (P6-8, #3509): VPC, public subnet(s), the SSH-only owner-IP-scoped
    # security group, and the single EC2 instance from
    # product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md. This is the only
    # supported way to drive terraform/aws (CLAUDE.md: no raw `terraform` in
    # any user flow), and `apply` is what writes ~/.nyxGPT/cloud/state.json so
    # `allow-ip` above works with no arguments afterwards. Deploying the stack
    # onto the instance is separate (#3513).
    cloud_infra_p = cloud_sub.add_parser(
        "infra",
        help=(
            "Provision and tear down the AWS substrate (VPC, subnets, SSH-only "
            "security group, EC2 instance)"
        ),
    )
    cloud_infra_sub = cloud_infra_p.add_subparsers(dest="infra_cmd", required=True)

    def _add_infra_provision_flags(parser: argparse.ArgumentParser) -> None:
        """Attach the inputs shared by `infra plan`/`apply`/`destroy`.

        Every one of them is remembered in ~/.nyxGPT/cloud/infra.json after a
        run, so a later invocation needs only the flags that change.
        """
        parser.add_argument(
            "--region",
            help="AWS region (default: saved value, then config.ini [cloud] region, then AWS_REGION, then us-east-1)",
        )
        parser.add_argument(
            "--profile",
            help="AWS profile to authenticate with (default: saved value, then config.ini [cloud] profile, then AWS_PROFILE)",
        )
        parser.add_argument(
            "--owner-ip",
            help=(
                "IP or CIDR allowed to SSH to the instance (default: auto-detect this "
                "machine's current public IP; bare addresses are scoped to /32; "
                "0.0.0.0/0 is refused)"
            ),
        )
        parser.add_argument(
            "--ssh-public-key",
            help=(
                "Path to an OpenSSH public key (e.g. ~/.ssh/id_ed25519.pub) to register "
                "as a new EC2 key pair. Mutually exclusive with --ssh-key-name"
            ),
        )
        parser.add_argument(
            "--ssh-key-name",
            help="Name of an EC2 key pair that already exists in the region. Mutually exclusive with --ssh-public-key",
        )
        parser.add_argument(
            "--instance-type",
            help="EC2 instance type (default: saved value, then m5.large)",
        )
        parser.add_argument(
            "--root-volume-size",
            type=int,
            help="Root EBS volume size in GiB (default: saved value, then 100)",
        )

    cloud_infra_plan = cloud_infra_sub.add_parser(
        "plan", help="Show what would be provisioned or changed, creating nothing"
    )
    _add_infra_provision_flags(cloud_infra_plan)

    cloud_infra_apply = cloud_infra_sub.add_parser(
        "apply",
        help=(
            "Provision (or reconcile) the substrate and record its ids for the other "
            "`nyxgpt cloud` commands"
        ),
    )
    _add_infra_provision_flags(cloud_infra_apply)

    cloud_infra_destroy = cloud_infra_sub.add_parser(
        "destroy", help="Tear the substrate down, including the instance and its root volume"
    )
    _add_infra_provision_flags(cloud_infra_destroy)
    cloud_infra_destroy.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the teardown (required -- data that exists only on the instance is lost)",
    )

    cloud_infra_sub.add_parser(
        "status", help="Report what is currently provisioned and how it is reachable"
    )
    cloud_infra_sub.add_parser(
        "test",
        help=(
            "Run the substrate's plan-level tests offline (the same access-model checks "
            "CI runs) -- creates nothing and needs no AWS account"
        ),
    )

    # `cloud state` -- Terraform remote state for the substrate above (P6-9,
    # #3510). A fresh install keeps state in one local file, which breaks down
    # the moment a second operator or a CI runner applies the same substrate;
    # these commands move it to a versioned S3 bucket with a DynamoDB lock
    # table, and are also the recovery path when a run dies holding the lock or
    # writes state that has to be rolled back. No raw `terraform` anywhere
    # (CLAUDE.md's wrapper requirement).
    cloud_state_p = cloud_sub.add_parser(
        "state",
        help=(
            "Manage the substrate's Terraform state: migrate it to a shared S3 backend "
            "with DynamoDB locking, and recover it when a run fails"
        ),
    )
    cloud_state_sub = cloud_state_p.add_subparsers(dest="state_cmd", required=True)

    def _add_state_backend_flags(parser: argparse.ArgumentParser) -> None:
        """Attach the backend inputs shared by `state bootstrap`/`migrate`.

        Every one is remembered in ~/.nyxGPT/cloud/backend.json, so later runs
        need only what changes -- and the defaults are derived (bucket from the
        AWS account id, region from what `cloud infra` provisions into) so the
        common case needs no flags at all.
        """
        parser.add_argument(
            "--bucket",
            help=(
                "S3 bucket for the state file (default: saved value, then "
                "nyxgpt-tfstate-<account-id>-<region>). Bucket names are globally unique"
            ),
        )
        parser.add_argument(
            "--table",
            help="DynamoDB table used for state locking (default: saved value, then nyxgpt-tfstate-locks)",
        )
        parser.add_argument(
            "--key",
            help="Object key for the state file inside the bucket (default: nyxgpt/aws/terraform.tfstate)",
        )
        parser.add_argument(
            "--region",
            help="AWS region holding the bucket and lock table (default: the region `cloud infra` provisions into)",
        )
        parser.add_argument(
            "--profile",
            help="AWS profile to authenticate with (default: saved value, then config.ini [cloud] profile, then AWS_PROFILE)",
        )

    cloud_state_status = cloud_state_sub.add_parser(
        "status", help="Report where the substrate's state lives and how it is locked"
    )
    cloud_state_status.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Also confirm against AWS that the bucket and lock table exist and that "
            "versioning (the recovery story) is on"
        ),
    )

    cloud_state_bootstrap = cloud_state_sub.add_parser(
        "bootstrap",
        help=(
            "Create the versioned, encrypted state bucket and the DynamoDB lock table, "
            "without moving any state yet"
        ),
    )
    _add_state_backend_flags(cloud_state_bootstrap)

    cloud_state_migrate = cloud_state_sub.add_parser(
        "migrate",
        help=(
            "Create the backend if needed and move the substrate's existing local state "
            "into it (safe to re-run)"
        ),
    )
    _add_state_backend_flags(cloud_state_migrate)

    cloud_state_sub.add_parser(
        "local",
        help=(
            "Move state back out of S3 into the local file -- the escape hatch when the "
            "backend itself is unreachable. The bucket and table are left in place"
        ),
    )

    cloud_state_unlock = cloud_state_sub.add_parser(
        "unlock",
        help="Release a state lock left held by a run that was killed mid-apply",
    )
    cloud_state_unlock.add_argument(
        "--lock-id",
        required=True,
        help="The lock id Terraform reports in the error that refused to run (`Lock Info: ID: ...`)",
    )

    cloud_state_backup = cloud_state_sub.add_parser(
        "backup",
        help="Write the current state to a local file, whichever backend holds it",
    )
    cloud_state_backup.add_argument(
        "--output",
        help="Where to write the backup (default: ~/.nyxGPT/cloud/terraform.tfstate.backup)",
    )

    cloud_state_versions = cloud_state_sub.add_parser(
        "versions",
        help="List the stored versions of the remote state file, newest first",
    )
    cloud_state_versions.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many versions to show (default: 20)",
    )

    cloud_state_restore = cloud_state_sub.add_parser(
        "restore",
        help="Make a previous version of the remote state the current one",
    )
    cloud_state_restore.add_argument(
        "--version-id",
        required=True,
        help="The S3 version id to restore -- see `nyxgpt cloud state versions`",
    )

    # `cloud deploy` / `destroy` / `tunnel` -- the one-command story (P6-11,
    # #3513). `deploy` applies the substrate above, installs a *published*
    # nyxGPT release onto the instance (never a clone -- CLAUDE.md's
    # repo-less requirement), opens the SSH tunnel that is the only access
    # path (product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md), waits
    # for health through it, and prints the localhost URLs.
    cloud_deploy_p = cloud_sub.add_parser(
        "deploy",
        help=(
            "Provision AWS and deploy the full stack onto it, then open the access "
            "tunnel and print the URLs (idempotent -- re-runs reconcile)"
        ),
    )
    _add_infra_provision_flags(cloud_deploy_p)

    def _add_ssh_access_flags(parser: argparse.ArgumentParser) -> None:
        """Attach the flags describing how to SSH to the instance."""
        parser.add_argument(
            "--ssh-user",
            help="Login user on the instance (default: ec2-user, the Amazon Linux 2023 default)",
        )
        parser.add_argument(
            "--identity-file",
            help=(
                "Private key to authenticate with (default: let ssh use its own "
                "~/.ssh defaults and agent)"
            ),
        )
        parser.add_argument(
            "--host",
            help=(
                "Target host instead of the provisioned instance recorded in "
                "~/.nyxGPT/cloud/state.json"
            ),
        )

    _add_ssh_access_flags(cloud_deploy_p)
    cloud_deploy_p.add_argument(
        "--version",
        dest="version",
        help=(
            "Published nyxGPT release to install on the instance (default: the version "
            "of this CLI, then whatever the last deploy used)"
        ),
    )
    cloud_deploy_p.add_argument(
        "--skip-observability",
        action="store_true",
        help="Deploy the core app only, without the monitoring/logging/tracing/errors stack",
    )
    cloud_deploy_p.add_argument(
        "--no-tunnel",
        action="store_true",
        help=(
            "Do not open the access tunnel (and therefore do not health-check through it) "
            "-- prints the `nyxgpt cloud tunnel` command to run instead"
        ),
    )
    cloud_deploy_p.add_argument(
        "--health-timeout",
        type=float,
        help="Seconds to wait for the deployed stack to answer /health (default: 900)",
    )
    cloud_deploy_p.add_argument(
        "--ssh-timeout",
        type=float,
        help="Seconds to wait for the instance to accept SSH after apply (default: 300)",
    )
    cloud_deploy_p.add_argument(
        "--status",
        action="store_true",
        help="Report the deployment's state as JSON instead of deploying (touches nothing)",
    )

    cloud_destroy_p = cloud_sub.add_parser(
        "destroy",
        help="Close the access tunnel and tear the whole cloud deployment down",
    )
    _add_infra_provision_flags(cloud_destroy_p)
    cloud_destroy_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the teardown (required -- data that exists only on the instance is lost)",
    )

    cloud_tunnel_p = cloud_sub.add_parser(
        "tunnel",
        help=(
            "Open the SSH tunnel to the deployment and print the localhost URLs for the "
            "app, web UI, and every enabled observability UI"
        ),
    )
    _add_ssh_access_flags(cloud_tunnel_p)
    cloud_tunnel_p.add_argument(
        "--background",
        action="store_true",
        help="Return immediately, leaving the tunnel running (default: hold it in the foreground)",
    )
    cloud_tunnel_p.add_argument(
        "--stop",
        action="store_true",
        help="Close a tunnel previously opened with --background",
    )
    cloud_tunnel_p.add_argument(
        "--status",
        action="store_true",
        help="Report whether a background tunnel is open, and what it forwards",
    )

    # `cloud smoke` -- the cloud counterpart of scripts/smoke-test.sh (P6-17,
    # #3515). Deploys, verifies chat/RAG/observability over the access tunnel,
    # and always tears the deployment down again -- on failure as well as on
    # success, so a run can never leave billed AWS resources behind. A wrapped
    # command rather than a repo script because P6-16 accepts the cloud path
    # from a machine with no checkout (CLAUDE.md, repo-less portability).
    cloud_smoke_p = cloud_sub.add_parser(
        "smoke",
        help=(
            "End-to-end cloud test: deploy, verify chat/RAG/observability over the "
            "access tunnel, then tear the deployment down (always, even on failure)"
        ),
    )
    _add_infra_provision_flags(cloud_smoke_p)
    _add_ssh_access_flags(cloud_smoke_p)
    cloud_smoke_p.add_argument(
        "--version",
        dest="version",
        help="Published nyxGPT release to deploy and test (default: the version of this CLI)",
    )
    cloud_smoke_p.add_argument(
        "--skip-observability",
        action="store_true",
        help=(
            "Deploy and verify the core app only, skipping the "
            "monitoring/logging/tracing/errors stack and its reachability check"
        ),
    )
    cloud_smoke_p.add_argument(
        "--skip-deploy",
        action="store_true",
        help=(
            "Verify the deployment that already exists instead of deploying one "
            "(still torn down afterwards -- requires --yes, or --keep to leave it up)"
        ),
    )
    cloud_smoke_p.add_argument(
        "--keep",
        action="store_true",
        help=(
            "Leave the deployment running after the test instead of destroying it. "
            "It keeps billing until you run `nyxgpt cloud destroy --yes`"
        ),
    )
    cloud_smoke_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destroying a deployment this run did not create (with --skip-deploy)",
    )
    cloud_smoke_p.add_argument(
        "--api-key",
        help=(
            "API key for the deployed stack (default: $NYXGPT_AUTH_API_KEY, then the "
            "key the instance itself is configured with)"
        ),
    )
    cloud_smoke_p.add_argument(
        "--health-timeout",
        type=float,
        help="Seconds to wait for the deployed stack to answer /health (default: 900)",
    )
    cloud_smoke_p.add_argument(
        "--ssh-timeout",
        type=float,
        help="Seconds to wait for the instance to accept SSH after apply (default: 300)",
    )
    cloud_smoke_p.add_argument(
        "--model-timeout",
        type=float,
        help="Seconds to allow for pulling the default model on the instance (default: 1800)",
    )
    cloud_smoke_p.add_argument(
        "--chat-timeout",
        type=float,
        help="Seconds to allow for the chat round-trip (default: 300)",
    )
    cloud_smoke_p.add_argument(
        "--rag-timeout",
        type=float,
        help="Seconds to allow for each RAG ingest/query call (default: 120)",
    )
    cloud_smoke_p.add_argument(
        "--observability-timeout",
        type=float,
        help="Seconds to wait for every observability UI to answer (default: 300)",
    )
    cloud_smoke_p.add_argument(
        "--json",
        action="store_true",
        help="Print the full machine-readable record of the run instead of a summary",
    )

    # Add canary command (local weighted-traffic canary rollout on a local k8s cluster --
    # the sole deployment model since #3409 retired blue/green in favor of it)
    canary_p = sub.add_parser("canary", help="Local canary deployment (kind/minikube/k3s cluster)")
    canary_sub = canary_p.add_subparsers(dest="canary_cmd", required=True)

    # --component is shared by every canary subcommand (default "api"; "web" as of
    # #3419 -- see canary.py's COMPONENTS. "ollama" is accepted but always refused
    # with an explanation, see OLLAMA_UNSUPPORTED_REASON).
    canary_status_p = canary_sub.add_parser(
        "status", help="Show rollout progress, stable/canary health/version, and live metrics"
    )
    canary_status_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_status_p.add_argument(
        "--component", default="api", help="Component to report on (default: api; or web)"
    )

    canary_deploy_p = canary_sub.add_parser(
        "deploy",
        help=(
            "Build the current checkout into a versioned image and deploy it to canary "
            "only (stable is never touched); traffic weighting is a separate step"
        ),
    )
    canary_deploy_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_deploy_p.add_argument(
        "--component", default="api", help="Component to deploy (default: api; or web)"
    )

    canary_start_p = canary_sub.add_parser(
        "start", help="Start a canary rollout at an initial traffic weight"
    )
    canary_start_p.add_argument(
        "--weight", type=int, default=10, help="Initial canary traffic percentage (default: 10)"
    )
    canary_start_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_start_p.add_argument(
        "--component", default="api", help="Component to start (default: api; or web)"
    )

    canary_evaluate_p = canary_sub.add_parser(
        "evaluate",
        help="Check live error-rate/latency metrics against thresholds; auto-rollback on regression",
    )
    canary_evaluate_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_evaluate_p.add_argument(
        "--component", default="api", help="Component to evaluate (default: api; or web)"
    )

    canary_promote_p = canary_sub.add_parser(
        "promote", help="Increase the canary's traffic share by a step (default: from config)"
    )
    canary_promote_p.add_argument(
        "--step", type=int, help="Percentage points to add to the canary's traffic share"
    )
    canary_promote_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_promote_p.add_argument(
        "--component", default="api", help="Component to promote (default: api; or web)"
    )

    canary_rollback_p = canary_sub.add_parser(
        "rollback", help="Cut all traffic back to the stable deployment"
    )
    canary_rollback_p.add_argument(
        "--namespace", help="Kubernetes namespace (default: from config, else nyxgpt)"
    )
    canary_rollback_p.add_argument(
        "--component", default="api", help="Component to roll back (default: api; or web)"
    )

    # Add self-heal command (Docker Compose stack watchdog)
    self_heal_p = sub.add_parser(
        "self-heal", help="Self-heal watchdog for the local Docker Compose stack"
    )
    self_heal_sub = self_heal_p.add_subparsers(dest="self_heal_cmd", required=True)

    self_heal_sub.add_parser("status", help="Show per-component health and recent heal events")

    self_heal_sub.add_parser("enable", help="Enable automatic self-healing")
    self_heal_sub.add_parser("disable", help="Disable automatic self-healing")

    self_heal_heal_p = self_heal_sub.add_parser(
        "heal", help="Manually restart an unhealthy component now (or all of them)"
    )
    self_heal_heal_p.add_argument(
        "--service", help="Compose service name to restart (default: heal every unhealthy one)"
    )

    args = parser.parse_args(argv)
    cmd = args.command or "info"

    # Initialize centralized logging as early as possible.
    try:
        cfg0 = load_config(args.config)
        configure_logging(cfg0, console=True, filename="cli.log")
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
            rag_mode=getattr(args, "rag_mode", None),
        )

    if cmd == "sessions":
        return cmd_sessions(
            action=args.action,
            name=args.name,
            new_name=args.new_name,
            extras=args.extras,
            sessions_dir=args.sessions_dir,
            format=args.format,
            output=args.output,
            case_sensitive=getattr(args, "case_sensitive", False),
            role=getattr(args, "role", None),
            limit=getattr(args, "limit", 50),
            model=getattr(args, "model", None),
            rag_enabled=getattr(args, "rag_enabled", None),
            force_include=getattr(args, "force_include", False),
        )

    if cmd == "rag":
        if args.rag_cmd == "ingest":
            return cmd_rag_ingest(
                args.doc_id,
                args.path,
                args.ensure_schema,
                collection=args.collection,
                model=args.model,
                dimension=args.dimension,
            )
        if args.rag_cmd == "query":
            return cmd_rag_query(
                args.question,
                args.top_k,
                collection=args.collection,
                model=args.model,
                dimension=args.dimension,
                doc_ids=getattr(args, "doc_ids", None),
                filename=getattr(args, "filename", None),
                tags=getattr(args, "tags", None),
                date_from=getattr(args, "date_from", None),
                date_to=getattr(args, "date_to", None),
            )
        if args.rag_cmd == "list":
            return cmd_rag_list(collection=args.collection)
        if args.rag_cmd == "info":
            return cmd_rag_info(args.doc_id, collection=args.collection)
        if args.rag_cmd == "collections":
            return cmd_rag_collections()
        if args.rag_cmd == "compare":
            return cmd_rag_compare(args.test_file, args.models)
        if args.rag_cmd == "delete":
            return cmd_rag_delete(args.doc_id, collection=args.collection)
        if args.rag_cmd == "wipe":
            return cmd_rag_wipe(args.yes_really, collection=args.collection)
        if args.rag_cmd == "index-repo":
            return cmd_rag_index_repo(
                args.repo_path,
                args.prefix,
                args.extensions,
                args.docs_only,
                args.ensure_schema,
                collection=args.collection,
                model=args.model,
                dimension=args.dimension,
            )

    if cmd == "models":
        if args.models_cmd == "list":
            return cmd_models_list()
        if args.models_cmd == "pull":
            return cmd_models_pull(args.model)
        if args.models_cmd == "delete":
            return cmd_models_delete(args.model, args.force)
        if args.models_cmd == "show":
            return cmd_models_show(args.model)

    if cmd == "wizard":
        return run_wizard(output_path=args.output)

    if cmd == "up":
        # Same correlation-id minting as `ops`/`canary`/`self-heal` below --
        # `up` calls straight into ops_mod.install()/the health-wait, which
        # both shell out and record actions the same way `ops install` does.
        os.environ.setdefault("NYXGPT_CORRELATION_ID", mint_correlation_id())
        return ops_mod.up(args)

    if cmd == "down":
        os.environ.setdefault("NYXGPT_CORRELATION_ID", mint_correlation_id())
        return ops_mod.down(args)
    if cmd == "secrets" and args.secrets_cmd == "setup":
        return run_secrets_setup(cfg_path=args.config, reconfigure=args.reconfigure)

    if cmd == "ops":
        # Mint a correlation id once per CLI invocation (#3430): every
        # subprocess.run() call made while handling this command inherits it
        # via the process environment (none pass an explicit env=), and
        # _record_ops_action reads it back for the #3390 event -- so a
        # dashboard-invisible `nyxgpt ops` action can still be joined to the
        # docker/brew/kubectl command it drove.
        os.environ.setdefault("NYXGPT_CORRELATION_ID", mint_correlation_id())
        if args.ops_cmd == "install":
            return ops_mod.install(args)
        if args.ops_cmd == "status":
            return ops_mod.status(args)
        if args.ops_cmd == "doctor":
            return ops_mod.doctor(args)
        if args.ops_cmd == "restart":
            return ops_mod.restart(args)
        if args.ops_cmd == "stop":
            return ops_mod.stop(args)
        if args.ops_cmd == "down":
            return ops_mod.down(args)
        if args.ops_cmd == "env-sync":
            return ops_mod.env_sync(args)
        if args.ops_cmd == "secrets-sync":
            return ops_mod.secrets_sync(args)
        if args.ops_cmd == "logs":
            return ops_mod.logs(args)
        if args.ops_cmd == "glitchtip-init":
            return ops_mod.glitchtip_init(args)
        if args.ops_cmd == "alert-test":
            return ops_mod.alert_test(args)
        if args.ops_cmd == "observability":
            return ops_mod.observability(args)
        if args.ops_cmd == "migrate-volumes":
            return ops_mod.migrate_volumes_cmd(args)
        if args.ops_cmd == "port-forward":
            return ops_mod.port_forward(args)
        if args.ops_cmd == "verify":
            return ops_mod.verify(args)
        if args.ops_cmd == "portability":
            return portability_mod.portability(args)

    if cmd == "cloud" and args.cloud_cmd == "allow-ip":
        return cloud_mod.allow_ip(args)

    if cmd == "cloud" and args.cloud_cmd == "user-data":
        return cloud_provision_mod.user_data(args)

    if cmd == "cloud" and args.cloud_cmd == "credentials-setup":
        return run_aws_credentials_setup(cfg_path=args.config)

    if cmd == "cloud" and args.cloud_cmd == "infra":
        return cloud_infra_mod.infra_command(args)

    if cmd == "cloud" and args.cloud_cmd == "state":
        return cloud_state_mod.state_command(args)

    if cmd == "cloud" and args.cloud_cmd in ("deploy", "destroy", "tunnel"):
        return cloud_deploy_mod.deploy_command(args)

    if cmd == "cloud" and args.cloud_cmd == "smoke":
        return cloud_smoke_mod.smoke_command(args)

    if cmd == "canary":
        # Same per-invocation correlation id as the `ops` dispatch above --
        # canary.py's record_canary_action funnels through the same
        # _record_ops_action (#3390/#3430).
        os.environ.setdefault("NYXGPT_CORRELATION_ID", mint_correlation_id())
        if args.canary_cmd == "status":
            return cmd_canary_status(args.config, args.namespace, args.component)
        if args.canary_cmd == "deploy":
            return cmd_canary_deploy(args.config, args.namespace, args.component)
        if args.canary_cmd == "start":
            return cmd_canary_start(args.config, args.namespace, args.weight, args.component)
        if args.canary_cmd == "evaluate":
            return cmd_canary_evaluate(args.config, args.namespace, args.component)
        if args.canary_cmd == "promote":
            return cmd_canary_promote(args.config, args.namespace, args.step, args.component)
        if args.canary_cmd == "rollback":
            return cmd_canary_rollback(args.config, args.namespace, args.component)

    if cmd == "self-heal":
        # Same per-invocation correlation id as `ops`/`canary` above -- so a
        # CLI-triggered `nyxgpt self-heal heal` restart's HealEvent and the
        # subprocess it drove share one id (#3430).
        os.environ.setdefault("NYXGPT_CORRELATION_ID", mint_correlation_id())
        if args.self_heal_cmd == "status":
            return cmd_self_heal_status(args.config)
        if args.self_heal_cmd == "enable":
            return cmd_self_heal_toggle(True)
        if args.self_heal_cmd == "disable":
            return cmd_self_heal_toggle(False)
        if args.self_heal_cmd == "heal":
            return cmd_self_heal_heal(args.service)

    if cmd == "mcp":
        return cmd_mcp()

    parser.print_help()
    return 2


__all__ = ["cli"]
