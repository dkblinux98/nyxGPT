from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mygpt.config import load_config
from mygpt.ollama_client import ollama_chat, ollama_chat_stream_tokens
from mygpt.rag.rag import retrieve_context
from mygpt.sessions import load_session, save_session


@dataclass
class ChatResult:
    session: str
    model: str
    reply: str
    rag_used: bool
    rag_chunks: int


def _cfg(config_path: str | None) -> Any:
    return load_config(config_path)


def _get_bool(cfg: Any, section: str, key: str, default: bool) -> bool:
    try:
        return cfg.getboolean(section, key, fallback=default)
    except Exception:
        # Robust against missing/invalid types
        try:
            v = cfg.get(section, key, fallback=str(default)).strip().lower()
            return v in {"1", "true", "yes", "on"}
        except Exception:
            return default


def _get_int(cfg: Any, section: str, key: str, default: int) -> int:
    try:
        return cfg.getint(section, key, fallback=default)
    except Exception:
        try:
            return int(cfg.get(section, key, fallback=str(default)))
        except Exception:
            return default


def _get_str(cfg: Any, section: str, key: str, default: str) -> str:
    try:
        return cfg.get(section, key, fallback=default)
    except Exception:
        return default


def _build_rag_context(query: str, top_k: int, max_chars: int) -> tuple[str, int]:
    """Return (context_text, chunks_used). Never raises."""
    try:
        rows = retrieve_context(query, top_k=top_k)
        # Some callers/tests may return a raw string. Normalize to the expected row shape.
        if isinstance(rows, str):
            rows = [{"text": rows, "doc_id": "rag", "chunk_id": 0, "score": None}]
        parts: list[str] = []
        used = 0
        remaining = max_chars

        for r in rows:
            txt = str(r.get("text", "")).strip()
            if not txt:
                continue
            label = f"[doc_id={r.get('doc_id')} chunk_id={r.get('chunk_id')} score={r.get('score')}]"
            block = f"{label}\n{txt}\n"
            if len(block) > remaining:
                # Truncate this block if we can fit anything
                if remaining > 0:
                    block = block[:remaining]
                    parts.append(block)
                    used += 1
                break
            parts.append(block)
            used += 1
            remaining -= len(block)
            if remaining <= 0:
                break

        context = "\n".join(parts).strip()
        return context, used
    except Exception:
        return "", 0


def chat(
    prompt: str,
    *,
    session: str = "default",
    new: bool = False,
    model: str | None = None,
    system: str | None = None,
    config_path: str | None = None,
    sessions_dir: str | None = None,
) -> ChatResult:
    """Run a chat turn, persisting session history. Optionally inject RAG context."""

    cfg = _cfg(config_path)

    base_url = _get_str(cfg, "ollama", "base_url", "http://127.0.0.1:11434")
    default_model = _get_str(cfg, "mygpt", "default_model", "llama3.1:8b")
    chosen_model = model or default_model

    chat_timeout_s = _get_int(cfg, "mygpt", "chat_timeout_seconds", 180)

    # Load session messages
    state = load_session(session, cfg, sessions_dir_override=sessions_dir)
    if new:
        state.messages = []

    messages: list[dict[str, str]] = []

    sys_msg = system or _get_str(cfg, "mygpt", "system_prompt", "")
    if sys_msg.strip():
        messages.append({"role": "system", "content": sys_msg.strip()})

    # Optional RAG context injection
    rag_enabled = _get_bool(cfg, "rag", "enable_chat_context", False)
    rag_top_k = _get_int(cfg, "rag", "chat_top_k", 3)
    rag_max_chars = _get_int(cfg, "rag", "chat_context_max_chars", 4000)

    rag_context, rag_chunks = ("", 0)
    if rag_enabled:
        rag_context, rag_chunks = _build_rag_context(prompt, top_k=rag_top_k, max_chars=rag_max_chars)
        if rag_context:
            messages.append(
                {
                    "role": "system",
                    "content": "Use the retrieved context below when it is relevant and helpful. "
                    "Do not mention that you were given retrieved context unless the user explicitly asks about sources. "
                    "If the context is insufficient, say so and answer from general knowledge.\n\n"
                    "--- BEGIN RETRIEVED CONTEXT ---\n"
                    f"{rag_context}\n"
                    "--- END RETRIEVED CONTEXT ---",
                }
            )

    # Add prior conversation
    for m in state.messages:
        # Ensure only the expected shape
        role = str(m.get("role", "user"))
        content = str(m.get("content", ""))
        if content:
            messages.append({"role": role, "content": content})

    # Add this turn
    messages.append({"role": "user", "content": prompt})

    reply = ollama_chat(base_url=base_url, model=chosen_model, messages=messages, timeout_s=chat_timeout_s)

    # Persist back to session
    state.messages.append({"role": "user", "content": prompt})
    state.messages.append({"role": "assistant", "content": reply})
    save_session(state, cfg, sessions_dir_override=sessions_dir)

    return ChatResult(
        session=session,
        model=chosen_model,
        reply=reply,
        rag_used=bool(rag_context),
        rag_chunks=rag_chunks,
    )


def chat_stream(
    prompt: str,
    *,
    session: str = "default",
    new: bool = False,
    model: str | None = None,
    system: str | None = None,
    config_path: str | None = None,
    sessions_dir: str | None = None,
):
    """Yield assistant text chunks for a chat turn while persisting the final reply.

    This function yields incremental text tokens. Callers should print or forward
    chunks as they arrive. Session persistence occurs after streaming completes.
    """

    cfg = _cfg(config_path)

    base_url = _get_str(cfg, "ollama", "base_url", "http://127.0.0.1:11434")
    default_model = _get_str(cfg, "mygpt", "default_model", "llama3.1:8b")
    chosen_model = model or default_model

    chat_timeout_s = _get_int(cfg, "mygpt", "chat_timeout_seconds", 180)

    # Load session messages
    state = load_session(session, cfg, sessions_dir_override=sessions_dir)
    if new:
        state.messages = []

    messages: list[dict[str, str]] = []

    sys_msg = system or _get_str(cfg, "mygpt", "system_prompt", "")
    if sys_msg.strip():
        messages.append({"role": "system", "content": sys_msg.strip()})

    # Optional RAG context injection
    rag_enabled = _get_bool(cfg, "rag", "enable_chat_context", False)
    rag_top_k = _get_int(cfg, "rag", "chat_top_k", 3)
    rag_max_chars = _get_int(cfg, "rag", "chat_context_max_chars", 4000)

    rag_context, rag_chunks = ("", 0)
    if rag_enabled:
        rag_context, rag_chunks = _build_rag_context(prompt, top_k=rag_top_k, max_chars=rag_max_chars)
        if rag_context:
            messages.append(
                {
                    "role": "system",
                    "content": "Use the retrieved context below when it is relevant and helpful. "
                    "Do not mention that you were given retrieved context unless the user explicitly asks about sources. "
                    "If the context is insufficient, say so and answer from general knowledge.\n\n"
                    "--- BEGIN RETRIEVED CONTEXT ---\n"
                    f"{rag_context}\n"
                    "--- END RETRIEVED CONTEXT ---",
                }
            )

    # Add prior conversation
    for m in state.messages:
        role = str(m.get("role", "user"))
        content = str(m.get("content", ""))
        if content:
            messages.append({"role": role, "content": content})

    # Add this turn
    messages.append({"role": "user", "content": prompt})

    # Stream tokens and assemble final reply
    parts: list[str] = []
    for chunk in ollama_chat_stream_tokens(
        base_url=base_url,
        model=chosen_model,
        messages=messages,
        timeout_s=chat_timeout_s,
    ):
        parts.append(chunk)
        yield chunk

    reply = "".join(parts)

    # Persist back to session
    state.messages.append({"role": "user", "content": prompt})
    state.messages.append({"role": "assistant", "content": reply})
    save_session(state, cfg, sessions_dir_override=sessions_dir)
