from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator
from configparser import ConfigParser

from mygpt.config import load_config
from mygpt.ollama_client import ollama_chat, ollama_chat_stream_tokens
from mygpt.rag.rag import retrieve_context, compose_context
from mygpt.sessions import load_session, save_session

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    session: str
    model: str
    reply: str
    rag_used: bool
    rag_chunks: int


@dataclass
class ChatContext:
    """Prepared context for a chat interaction.

    This consolidates all the setup work shared between
    streaming and non-streaming chat.
    """
    messages: list[dict[str, str]]
    state: Any  # SessionState
    chosen_model: str
    base_url: str
    chat_timeout_s: int
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


def _prepare_chat_context(
    prompt: str,
    *,
    session: str = "default",
    new: bool = False,
    model: str | None = None,
    system: str | None = None,
    config_path: str | None = None,
    sessions_dir: str | None = None,
) -> ChatContext:
    """Prepare messages and context for a chat interaction.

    This function consolidates all the setup logic shared between
    chat() and chat_stream(), eliminating code duplication and
    making the codebase easier to maintain.

    Args:
        prompt: User's input message
        session: Session name to use
        new: Whether to start a new session (clearing history)
        model: Model to use (overrides config default)
        system: System prompt (overrides config default)
        config_path: Path to config file
        sessions_dir: Path to sessions directory

    Returns:
        ChatContext with all prepared data needed for the LLM call
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

    # System prompt
    sys_msg = system or _get_str(cfg, "mygpt", "system_prompt", "")
    if sys_msg.strip():
        messages.append({"role": "system", "content": sys_msg.strip()})

    # Optional RAG context injection
    rag_enabled = _get_bool(cfg, "rag", "enable_chat_context", False)
    rag_context = ""
    rag_chunks = 0

    if rag_enabled:
        rows = retrieve_context(prompt)
        rag_chunks = len(rows)
        rag_context = compose_context(rows)

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

    return ChatContext(
        messages=messages,
        state=state,
        chosen_model=chosen_model,
        base_url=base_url,
        chat_timeout_s=chat_timeout_s,
        rag_used=bool(rag_context),
        rag_chunks=rag_chunks,
    )


def _persist_chat_turn(
    context: ChatContext,
    prompt: str,
    reply: str,
    cfg: ConfigParser,
    sessions_dir: str | None = None,
) -> None:
    """Persist a completed chat turn to session storage."""
    context.state.messages.append({"role": "user", "content": prompt})
    context.state.messages.append({"role": "assistant", "content": reply})
    save_session(context.state, cfg, sessions_dir_override=sessions_dir)


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

    context = _prepare_chat_context(
        prompt=prompt,
        session=session,
        new=new,
        model=model,
        system=system,
        config_path=config_path,
        sessions_dir=sessions_dir,
    )

    reply = ollama_chat(
        base_url=context.base_url,
        model=context.chosen_model,
        messages=context.messages,
        timeout_s=context.chat_timeout_s,
    )

    cfg = _cfg(config_path)
    _persist_chat_turn(context, prompt, reply, cfg, sessions_dir)

    return ChatResult(
        session=context.state.name,
        model=context.chosen_model,
        reply=reply,
        rag_used=context.rag_used,
        rag_chunks=context.rag_chunks,
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
) -> Iterator[str]:
    """Yield assistant text chunks for a chat turn while persisting the final reply.

    This function yields incremental text tokens. Callers should print or forward
    chunks as they arrive. Session persistence occurs after streaming completes.
    """

    context = _prepare_chat_context(
        prompt=prompt,
        session=session,
        new=new,
        model=model,
        system=system,
        config_path=config_path,
        sessions_dir=sessions_dir,
    )

    logger.debug(
        f"Starting chat stream for session={session}, model={context.chosen_model}"
    )

    # Stream tokens and assemble final reply
    parts: list[str] = []
    for chunk in ollama_chat_stream_tokens(
        base_url=context.base_url,
        model=context.chosen_model,
        messages=context.messages,
        timeout_s=context.chat_timeout_s,
    ):
        parts.append(chunk)
        yield chunk

    reply = "".join(parts)

    cfg = _cfg(config_path)
    _persist_chat_turn(context, prompt, reply, cfg, sessions_dir)
