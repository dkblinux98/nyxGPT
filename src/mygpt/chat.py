from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from configparser import ConfigParser

from mygpt.config import (
    load_config,
    get_context_window_size,
    get_context_warning_threshold,
    get_rag_instruction_template,
    get_rag_context_format,
)
from mygpt.ollama_client import ollama_chat, ollama_chat_stream_tokens
from mygpt.rag.rag import retrieve_context, compose_context
from mygpt.sessions import load_session, save_session
from mygpt.token_counter import count_message_tokens

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    session: str
    model: str
    reply: str
    rag_used: bool
    rag_chunks: int
    rag_context: list[dict] | None = None  # List of {text, score, doc_id, chunk_id}


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
    rag_context: list[dict] | None = None  # RAG retrieval results


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


def _detect_prompt_mode(message_count: int, cfg: Any) -> str:
    """Detect appropriate prompt mode based on conversation length.

    Args:
        message_count: Number of messages in conversation (excluding system)
        cfg: Config instance

    Returns:
        One of: "short", "medium", "long"
    """
    short_threshold = get_prompt_mode_short_threshold(cfg)
    long_threshold = get_prompt_mode_long_threshold(cfg)

    if message_count < short_threshold:
        return "short"
    elif message_count >= long_threshold:
        return "long"
    else:
        return "medium"


def _get_prompt_template(mode: str) -> str:
    """Get system prompt template for the given mode.

    Args:
        mode: One of "short", "medium", "long"

    Returns:
        Prompt template string appropriate for the mode
    """
    templates = {
        "short": (
            "You are a helpful AI assistant. "
            "Provide clear, concise responses."
        ),
        "medium": (
            "You are a helpful AI assistant engaged in a conversation. "
            "Provide informative responses while maintaining context from previous messages. "
            "Be concise but thorough when needed."
        ),
        "long": (
            "You are a helpful AI assistant engaged in an extended conversation. "
            "Maintain awareness of the full conversation history and refer back to earlier points when relevant. "
            "Provide comprehensive responses that build on previous exchanges. "
            "Be thoughtful about context and continuity throughout the discussion."
        ),
    }
    return templates.get(mode, templates["medium"])


def _truncate_messages_to_budget(
    messages: list[dict[str, str]],
    max_tokens: int,
    cfg: Any,
    model: str,
) -> list[dict[str, str]]:
    """Truncate message history to fit within token budget.

    Preserves system messages and recent history. Removes oldest user/assistant
    turns until the context fits within the budget.

    Args:
        messages: List of message dicts to truncate
        max_tokens: Maximum token budget
        cfg: Config instance for threshold lookup
        model: Model name for logging

    Returns:
        Truncated message list that fits within budget
    """
    # Try to count tokens, fall back gracefully if tiktoken not available
    try:
        token_count = count_message_tokens(messages)
    except ImportError:
        logger.warning(
            "tiktoken not installed, cannot enforce context budget. "
            "Install with: pip install tiktoken"
        )
        return messages

    # If we're within budget, no truncation needed
    if token_count <= max_tokens:
        # Check if we're approaching the warning threshold
        warning_threshold = get_context_warning_threshold(cfg)
        warning_tokens = int(max_tokens * warning_threshold)
        if token_count >= warning_tokens:
            pct = (token_count / max_tokens) * 100
            logger.warning(
                f"Context approaching limit: {token_count}/{max_tokens} tokens ({pct:.1f}%) "
                f"for model {model}"
            )
        return messages

    logger.warning(
        f"Context exceeds budget: {token_count}/{max_tokens} tokens "
        f"for model {model}. Truncating conversation history."
    )

    # Separate system messages from conversation
    system_messages = [m for m in messages if m.get("role") == "system"]
    conversation_messages = [m for m in messages if m.get("role") != "system"]

    # Always preserve system messages and the current user prompt (last message)
    if not conversation_messages:
        # Edge case: only system messages, shouldn't exceed budget but handle gracefully
        return messages

    # Keep the last message (current prompt) and work backwards
    preserved = conversation_messages[-1:]
    remaining = conversation_messages[:-1]

    # Try progressively smaller history until we fit
    while remaining:
        # Try keeping system + remaining + current prompt
        candidate = system_messages + remaining + preserved

        try:
            candidate_tokens = count_message_tokens(candidate)
        except ImportError:
            # If tiktoken fails mid-truncation, return what we have
            break

        if candidate_tokens <= max_tokens:
            logger.info(
                f"Truncated to {len(candidate)} messages "
                f"({candidate_tokens} tokens, removed {len(messages) - len(candidate)} messages)"
            )
            return candidate

        # Remove the oldest conversation message
        remaining = remaining[1:]

    # If we still don't fit, try just system + current prompt
    minimal = system_messages + preserved
    try:
        minimal_tokens = count_message_tokens(minimal)
        logger.warning(
            f"Truncated to minimal context: {len(minimal)} messages "
            f"({minimal_tokens} tokens). All history removed."
        )
    except ImportError:
        pass

    return minimal


def _prepare_chat_context(
    prompt: str,
    *,
    session: str = "default",
    new: bool = False,
    model: str | None = None,
    system: str | None = None,
    config_path: str | None = None,
    sessions_dir: str | None = None,
    rag_enabled: bool | None = None,
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

    # System prompt with adaptive mode
    sys_msg = system or _get_str(cfg, "mygpt", "system_prompt", "")

    # Apply adaptive prompt mode if enabled and no custom system prompt
    if not sys_msg.strip() and get_prompt_mode_enabled(cfg):
        # Count conversation messages to determine mode
        conversation_msg_count = len(state.messages)
        mode = _detect_prompt_mode(conversation_msg_count, cfg)
        sys_msg = _get_prompt_template(mode)
        logger.debug(
            f"Adaptive prompt mode: {mode} "
            f"(conversation has {conversation_msg_count} messages)"
        )

    if sys_msg.strip():
        messages.append({"role": "system", "content": sys_msg.strip()})

    # Optional RAG context injection
    # Priority: 1) explicit rag_enabled param, 2) session metadata, 3) global config
    if rag_enabled is not None:
        should_use_rag = rag_enabled
    else:
        session_rag = state.meta.get("rag_enabled")
        if isinstance(session_rag, bool):
            should_use_rag = session_rag
        else:
            should_use_rag = _get_bool(cfg, "rag", "enable_chat_context", False)

    rag_context = ""
    rag_chunks = 0
    rag_rows = None  # Store raw RAG results

    if should_use_rag:
        rows = retrieve_context(prompt)
        rag_chunks = len(rows)
        rag_rows = rows  # Save raw results
        rag_context = compose_context(rows)

        if rag_context:
            # Load configurable templates
            instruction_template = get_rag_instruction_template(cfg)
            context_format = get_rag_context_format(cfg)

            # Format the context using the template
            formatted_context = context_format.format(context=rag_context)

            # Build the full instruction with formatted context
            full_instruction = instruction_template.format(context=formatted_context)

            messages.append(
                {
                    "role": "system",
                    "content": full_instruction,
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

    # Enforce context window budget
    max_tokens = get_context_window_size(cfg, chosen_model)
    messages = _truncate_messages_to_budget(messages, max_tokens, cfg, chosen_model)

    # Update session metadata with chosen model
    state.meta["model"] = chosen_model

    return ChatContext(
        messages=messages,
        state=state,
        chosen_model=chosen_model,
        base_url=base_url,
        chat_timeout_s=chat_timeout_s,
        rag_used=should_use_rag,
        rag_chunks=rag_chunks,
        rag_context=rag_rows,
    )


def _persist_chat_turn(
    context: ChatContext,
    prompt: str,
    reply: str,
    cfg: ConfigParser,
    sessions_dir: str | None = None,
) -> None:
    """Persist a completed chat turn to session storage."""
    timestamp = datetime.now(timezone.utc).isoformat()
    context.state.messages.append({
        "role": "user",
        "content": prompt,
        "id": str(uuid.uuid4()),
        "timestamp": timestamp
    })

    # Build assistant message with optional RAG chunks
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": reply,
        "id": str(uuid.uuid4()),
        "timestamp": timestamp
    }

    # Include RAG chunks if RAG was used
    if context.rag_used:
        assistant_msg["rag_chunks"] = [
            {
                "text": chunk.get("text", ""),
                "score": chunk.get("score", 0.0),
                "doc_id": chunk.get("doc_id"),
                "chunk_id": chunk.get("chunk_id"),
            }
            for chunk in (context.rag_context or [])
        ]

    context.state.messages.append(assistant_msg)
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
    rag_enabled: bool | None = None,
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
        rag_enabled=rag_enabled,
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
        rag_context=context.rag_context,
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
    rag_enabled: bool | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Iterator[str]:
    """Yield assistant text chunks for a chat turn while persisting the final reply.

    This function yields incremental text tokens. Callers should print or forward
    chunks as they arrive. Session persistence occurs after streaming completes.

    Args:
        prompt: User prompt text
        session: Session name
        new: Whether to create a new session
        model: Override model name
        system: Override system prompt
        config_path: Path to config file
        sessions_dir: Override sessions directory
        rag_enabled: Enable/disable RAG for this request
        on_retry: Optional callback(attempt, delay, error) for connection retries
    """

    context = _prepare_chat_context(
        prompt=prompt,
        session=session,
        new=new,
        model=model,
        system=system,
        config_path=config_path,
        sessions_dir=sessions_dir,
        rag_enabled=rag_enabled,
    )

    logger.debug(
        "Starting chat stream for session=%s, model=%s",
        session,
        context.chosen_model
    )

    # Yield RAG metadata as first chunk if RAG was used
    if context.rag_used and context.rag_context:
        import json
        rag_data = {
            "type": "rag_metadata",
            "chunks": [
                {
                    "text": chunk.get("text", ""),
                    "score": chunk.get("score", 0.0),
                    "doc_id": chunk.get("doc_id"),
                    "chunk_id": chunk.get("chunk_id"),
                }
                for chunk in context.rag_context
            ]
        }
        yield f"__RAG_START__{json.dumps(rag_data)}__RAG_END__\n"

    # Create retry callback that yields status messages
    def _retry_callback(attempt: int, delay: float, error: Exception) -> None:
        # Yield a special marker for retry status
        import json
        retry_data = {
            "type": "retry_status",
            "attempt": attempt,
            "delay": delay,
            "error": str(error),
        }
        # Store in a list that we'll check and yield
        retry_messages.append(f"__RETRY_START__{json.dumps(retry_data)}__RETRY_END__\n")

        # Call the original callback if provided
        if on_retry:
            on_retry(attempt, delay, error)

    # Store retry messages that occur during connection
    retry_messages: list[str] = []

    # Stream tokens and assemble final reply
    parts: list[str] = []
    try:
        for chunk in ollama_chat_stream_tokens(
            base_url=context.base_url,
            model=context.chosen_model,
            messages=context.messages,
            timeout_s=context.chat_timeout_s,
            on_retry=_retry_callback,
        ):
            # Yield any queued retry messages first
            for retry_msg in retry_messages:
                yield retry_msg
            retry_messages.clear()

            parts.append(chunk)
            yield chunk
    except Exception as e:
        # If we have retry messages but the connection ultimately failed,
        # yield them before re-raising
        for retry_msg in retry_messages:
            yield retry_msg
        raise

    reply = "".join(parts)

    cfg = _cfg(config_path)
    _persist_chat_turn(context, prompt, reply, cfg, sessions_dir)
