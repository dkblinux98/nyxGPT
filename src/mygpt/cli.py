from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from mygpt.config import (
    load_config,
    get_default_model,
    get_ollama_base_url,
    get_sessions_dir,
)
from mygpt import sessions
from mygpt import tools_fs
from mygpt import models
from mygpt.chat import chat, chat_stream
from mygpt.logging import configure_logging
from mygpt.rag.rag import ingest_document, retrieve_context
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore
from mygpt.tui import MyGPTTUI
from mygpt.wizard import run_wizard

# Ops implementation lives in a separate module for testability.
from mygpt import ops as ops_mod


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
) -> int:
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
                    ts = ts.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
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

    print(f"Unknown sessions action: {action}", file=sys.stderr)
    return 2


def cmd_tools(
    action: str,
    path: Path,
    pattern: str | None,
    head: int | None,
    tail: int | None,
    max_matches: int,
) -> int:
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


def cmd_rag_ingest(
    doc_id: str,
    path: Path,
    ensure_schema: bool,
    collection: str = "default",
    model: str | None = None,
    dimension: int | None = None,
) -> int:
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
        print(
            f"Document {doc_id} unchanged (hash: {doc_hash[:16]}...), skipped re-ingestion"
        )
    elif status == "updated":
        print(
            f"Updated {chunks} chunks for doc_id={doc_id} into collection '{collection}'"
        )
        print(f"  Document hash: {doc_hash[:16]}...")
        if result["previous_hash"]:
            print(f"  Previous hash: {result['previous_hash'][:16]}...")
    else:  # status == "ingested"
        print(
            f"Ingested {chunks} chunks for doc_id={doc_id} into collection '{collection}'"
        )
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
) -> int:
    results_raw = retrieve_context(
        question,
        top_k=top_k,
        debug_mode=False,  # CLI doesn't need debug info
        collection=collection,
        embedding_model=model,
        embedding_dim=dimension,
    )
    # Type narrowing: debug_mode=False means result is list[dict]
    results = cast(list[dict], results_raw)
    print(f"Results: {len(results)} (from collection '{collection}')")
    if model:
        print(f"  Using embedding model: {model}")
    for i, r in enumerate(results, 1):
        print(f"--- {i} ---")
        print(r.get("text", ""))
        if "embedding_model" in r:
            print(
                f"  [model: {r.get('embedding_model')}, score: {r.get('score', 0):.3f}]"
            )
    return 0


def cmd_rag_info(doc_id: str, collection: str = "default") -> int:
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
    from mygpt.rag.model_compare import compare_models, print_comparison_table

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
    store = CassandraVectorStore(collection=collection)
    try:
        store.delete_doc(doc_id)
    finally:
        store.close()

    print(f"Deleted RAG document: {doc_id} from collection '{collection}'")
    return 0


def cmd_rag_wipe(confirm: bool, collection: str = "default") -> int:
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
            k: v
            for k, v in info.items()
            if k not in ("modelfile", "parameters", "template")
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


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mygpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.myGPT/config.ini)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="Show config-derived defaults (base_url, model)")

    tui_p = sub.add_parser("tui", help="Launch the terminal UI")
    tui_p.add_argument("--session", default="default", help="Session name")
    tui_p.add_argument("--api-url", dest="api_url", help="Override API base URL")

    chat_p = sub.add_parser("chat", help="Chat with the configured Ollama model")
    chat_p.add_argument(
        "prompt", nargs="?", help="Optional single prompt (otherwise interactive)"
    )
    chat_p.add_argument(
        "--model", dest="model_override", help="Override model for this run"
    )
    chat_p.add_argument("--system", help="Optional system prompt")
    chat_p.add_argument(
        "--no-stream", action="store_true", help="Disable streaming output"
    )
    chat_p.add_argument(
        "--session", default="default", help="Conversation session name"
    )
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
        ],
    )
    sessions_p.add_argument("name", nargs="?", help="Session name")
    sessions_p.add_argument(
        "new_name", nargs="?", help="Second argument (rename/title)"
    )
    sessions_p.add_argument("extras", nargs="*", help="Extra args (tags)")
    sessions_p.add_argument(
        "--sessions-dir", type=Path, help="Override sessions directory"
    )
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

    tools_p = sub.add_parser("tools", help="Explicit local filesystem tools")
    tools_p.add_argument("action", choices=["ls", "cat", "grep"], help="Tool to run")
    tools_p.add_argument("pattern", nargs="?", help="Regex pattern (grep)")
    tools_p.add_argument("path", type=Path, help="File or directory path")
    tools_p.add_argument("--head", type=int, help="Print first N lines (cat)")
    tools_p.add_argument("--tail", type=int, help="Print last N lines (cat)")
    tools_p.add_argument(
        "--max", dest="max_matches", type=int, default=50, help="Max matches (grep)"
    )

    rag_p = sub.add_parser("rag", help="Retrieval-Augmented Generation commands")
    rag_sub = rag_p.add_subparsers(dest="rag_cmd", required=True)

    ingest_p = rag_sub.add_parser(
        "ingest", help="Ingest a document into the vector store"
    )
    ingest_p.add_argument("doc_id", help="Document ID")
    ingest_p.add_argument("path", type=Path, help="Path to text file")
    ingest_p.add_argument(
        "--ensure-schema", action="store_true", help="Create schema if missing"
    )
    ingest_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )
    ingest_p.add_argument(
        "--model", help="Override embedding model (default: from config)"
    )
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
    query_p.add_argument(
        "--model", help="Override embedding model (default: from config)"
    )
    query_p.add_argument(
        "--dimension",
        type=int,
        help="Override embedding dimension (default: from config)",
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

    compare_p = rag_sub.add_parser(
        "compare", help="Compare embedding models performance"
    )
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
    wipe_p.add_argument(
        "--yes-really", action="store_true", help="Confirm destructive wipe"
    )
    wipe_p.add_argument(
        "--collection", default="default", help="Collection name (default: default)"
    )

    # Add models command
    models_p = sub.add_parser("models", help="Manage Ollama models")
    models_sub = models_p.add_subparsers(dest="models_cmd", required=True)

    models_sub.add_parser("list", help="List all available models")

    models_pull_p = models_sub.add_parser("pull", help="Pull (download) a model")
    models_pull_p.add_argument("model", help="Model name (e.g., llama3.1:8b)")

    models_delete_p = models_sub.add_parser("delete", help="Delete a model")
    models_delete_p.add_argument("model", help="Model name to delete")
    models_delete_p.add_argument(
        "--force", action="store_true", help="Skip confirmation prompt"
    )

    models_show_p = models_sub.add_parser(
        "show", help="Show detailed model information"
    )
    models_show_p.add_argument("model", help="Model name to inspect")

    # Add wizard command
    wizard_p = sub.add_parser("wizard", help="Run interactive configuration wizard")
    wizard_p.add_argument(
        "--output",
        type=Path,
        help="Output path for config.ini (default: ~/.myGPT/config.ini)",
    )

    # Add ops command
    ops_p = sub.add_parser("ops", help="Operational helpers")
    ops_sub = ops_p.add_subparsers(dest="ops_cmd", required=True)

    ops_install = ops_sub.add_parser("install", help="Install operational helpers")
    ops_install.add_argument("--repo-dir", help="Path to myGPT repo root")
    ops_install.add_argument(
        "--force", action="store_true", help="Overwrite existing files"
    )

    ops_status = ops_sub.add_parser(
        "status", help="Show status of local services (docker/cassandra/agent/api)"
    )
    ops_status.add_argument(
        "--api-url",
        help="Override API base URL (default: from config or http://127.0.0.1:8000)",
    )
    ops_status.add_argument(
        "--timeout", type=float, default=2.0, help="Timeout seconds for checks"
    )

    ops_doctor = ops_sub.add_parser(
        "doctor", help="Run checks and return non-zero if something is broken"
    )
    ops_doctor.add_argument(
        "--api-url",
        help="Override API base URL (default: from config or http://127.0.0.1:8000)",
    )
    ops_doctor.add_argument(
        "--timeout", type=float, default=2.0, help="Timeout seconds for checks"
    )

    ops_restart = ops_sub.add_parser("restart", help="Restart local services")
    ops_restart.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=["all", "api", "web", "ollama", "cassandra", "cassandra-logs"],
        help="Service to restart",
    )

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

    if cmd == "tui":
        app = MyGPTTUI(session=args.session, api_base_url=args.api_url)
        app.run()
        return 0

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
            format=args.format,
            output=args.output,
            case_sensitive=getattr(args, "case_sensitive", False),
            role=getattr(args, "role", None),
            limit=getattr(args, "limit", 50),
            model=getattr(args, "model", None),
            rag_enabled=getattr(args, "rag_enabled", None),
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

    if cmd == "ops":
        if args.ops_cmd == "install":
            return ops_mod.install(args)
        if args.ops_cmd == "status":
            return ops_mod.status(args)
        if args.ops_cmd == "doctor":
            return ops_mod.doctor(args)
        if args.ops_cmd == "restart":
            return ops_mod.restart(args)

    parser.print_help()
    return 2


__all__ = ["cli"]
