from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Iterator, cast
from configparser import ConfigParser

from nyxgpt.config import (
    load_config,
    get_context_window_size,
    get_context_warning_threshold,
    get_system_prompt_minimize,
    get_rag_instruction_template,
    get_rag_context_format,
    get_prompt_mode_enabled,
    get_prompt_mode_short_threshold,
    get_prompt_mode_long_threshold,
)
from nyxgpt.ollama_client import ollama_chat, ollama_chat_stream_tokens
from nyxgpt.rag.rag import retrieve_context, compose_context
from nyxgpt.sessions import load_session, save_session
from nyxgpt.token_counter import count_message_tokens
from nyxgpt.cache import get_cached_response, cache_response

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    session: str
    model: str
    reply: str
    rag_used: bool
    rag_chunks: int
    rag_context: list[dict] | None = None  # List of {text, score, doc_id, chunk_id, similarity_score}


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
        result: bool = cfg.getboolean(section, key, fallback=default)
        return result
    except Exception:
        # Robust against missing/invalid types
        try:
            v = cfg.get(section, key, fallback=str(default)).strip().lower()
            return v in {"1", "true", "yes", "on"}
        except Exception:
            return default


def _get_int(cfg: Any, section: str, key: str, default: int) -> int:
    try:
        result: int = cfg.getint(section, key, fallback=default)
        return result
    except Exception:
        try:
            return int(cfg.get(section, key, fallback=str(default)))
        except Exception:
            return default


def _get_str(cfg: Any, section: str, key: str, default: str) -> str:
    try:
        result: str = cfg.get(section, key, fallback=default)
        return result
    except Exception:
        return default


def _minimize_system_prompt(prompt: str) -> str:
    """Minimize system prompt to reduce token usage.

    Applies conservative optimizations that preserve semantic meaning:
    - Normalizes whitespace (removes extra spaces, newlines)
    - Removes redundant filler words and phrases
    - Condenses common verbose patterns

    Args:
        prompt: Original system prompt text

    Returns:
        Minimized version of the prompt
    """
    import re

    if not prompt or not prompt.strip():
        return prompt

    text = prompt.strip()

    # Normalize whitespace: collapse multiple spaces/newlines
    text = re.sub(r"\s+", " ", text)

    # Remove redundant courtesies and filler words (case-insensitive)
    # These patterns preserve meaning while reducing tokens
    patterns = [
        (r"\bPlease\s+", ""),  # "Please respond" -> "Respond"
        (r"\bYou are\s+", ""),  # "You are a helpful assistant" -> "helpful assistant"
        (r"\bYou should\s+", ""),  # "You should answer" -> "Answer"
        (r"\bI want you to\s+", ""),  # "I want you to act" -> "Act"
        (r"\bYour task is to\s+", ""),  # "Your task is to help" -> "Help"
        (r"\bMake sure to\s+", ""),  # "Make sure to respond" -> "Respond"
        (r"\bBe sure to\s+", ""),  # "Be sure to answer" -> "Answer"
        (r"\s+in order to\s+", " to "),  # "do X in order to Y" -> "do X to Y"
        (r"\s+as well as\s+", " and "),  # "X as well as Y" -> "X and Y"
    ]

    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Clean up any double spaces created by replacements
    text = re.sub(r"\s{2,}", " ", text).strip()

    logger.debug(
        "Minimized system prompt: %d -> %d chars (%.1f%% reduction)",
        len(prompt),
        len(text),
        100 * (1 - len(text) / len(prompt)) if len(prompt) > 0 else 0,
    )

    return text


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
        "short": ("You are a helpful AI assistant. Provide clear, concise responses."),
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
    rag_filters: dict[str, Any] | None = None,
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
        rag_enabled: Enable/disable RAG for this request
        rag_filters: Metadata filters for RAG document selection

    Returns:
        ChatContext with all prepared data needed for the LLM call
    """
    cfg = _cfg(config_path)

    base_url = _get_str(cfg, "ollama", "base_url", "http://127.0.0.1:11434")
    default_model = _get_str(cfg, "nyxgpt", "default_model", "llama3.1:8b")
    chosen_model = model or default_model
    chat_timeout_s = _get_int(cfg, "nyxgpt", "chat_timeout_seconds", 180)

    # Load session messages
    state = load_session(session, cfg, sessions_dir_override=sessions_dir)
    if new:
        state.messages = []

    messages: list[dict[str, str]] = []

    # System prompt with adaptive mode
    sys_msg = system or _get_str(cfg, "nyxgpt", "system_prompt", "")

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
        # Apply minimization if enabled
        if get_system_prompt_minimize(cfg):
            sys_msg = _minimize_system_prompt(sys_msg)
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
        # Build metadata filter from rag_filters dict if provided
        metadata_filter = None
        if rag_filters:
            from datetime import datetime
            from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

            # Parse dates if provided
            date_from_dt = None
            date_to_dt = None
            if rag_filters.get("date_from"):
                try:
                    date_from_dt = datetime.fromisoformat(rag_filters["date_from"])
                except (ValueError, TypeError):
                    logger.warning(f"Invalid date_from format: {rag_filters['date_from']}")
            if rag_filters.get("date_to"):
                try:
                    date_to_dt = datetime.fromisoformat(rag_filters["date_to"])
                except (ValueError, TypeError):
                    logger.warning(f"Invalid date_to format: {rag_filters['date_to']}")

            metadata_filter = MetadataFilter(
                doc_ids=rag_filters.get("doc_ids"),
                filename=rag_filters.get("filename"),
                tags=rag_filters.get("tags"),
                date_from=date_from_dt,
                date_to=date_to_dt,
            )

        # Disable debug mode for chat path - we don't need debug info here
        rows_result = retrieve_context(prompt, debug_mode=False, metadata_filter=metadata_filter)
        # Type narrowing: debug_mode=False means result is list[dict], not tuple
        rows = cast(list[dict], rows_result)
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
    context.state.messages.append(
        {
            "role": "user",
            "content": prompt,
            "id": str(uuid.uuid4()),
            "timestamp": timestamp,
        }
    )

    # Build assistant message with optional RAG chunks
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": reply,
        "id": str(uuid.uuid4()),
        "timestamp": timestamp,
    }

    # Include RAG chunks if RAG was used
    if context.rag_used:
        assistant_msg["rag_chunks"] = [
            {
                "text": chunk.get("text", ""),
                "score": chunk.get("score", 0.0),
                "doc_id": chunk.get("doc_id"),
                "chunk_id": chunk.get("chunk_id"),
                "similarity_score": chunk.get("similarity_score"),
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
    rag_filters: dict[str, Any] | None = None,
) -> ChatResult:
    """Run a chat turn, persisting session history. Optionally inject RAG context.

    Supports response caching for identical prompts (configurable via [cache] section).
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
        rag_filters=rag_filters,
    )

    # Try to get cached response first
    cached_reply = get_cached_response(
        messages=context.messages,
        model=context.chosen_model,
        session=session,
    )

    if cached_reply is not None:
        logger.info("Response cache hit for session %s", session)
        reply = cached_reply
    else:
        logger.debug("Response cache miss for session %s", session)
        reply = ollama_chat(
            base_url=context.base_url,
            model=context.chosen_model,
            messages=context.messages,
            timeout_s=context.chat_timeout_s,
        )
        # Cache the response
        cache_response(
            messages=context.messages,
            model=context.chosen_model,
            response=reply,
            session=session,
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
    rag_filters: dict[str, Any] | None = None,
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
        rag_filters: Metadata filters for RAG document selection
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
        rag_filters=rag_filters,
    )

    logger.debug(
        "Starting chat stream for session=%s, model=%s", session, context.chosen_model
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
                    "similarity_score": chunk.get("similarity_score"),
                }
                for chunk in context.rag_context
            ],
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
    except Exception:
        # If we have retry messages but the connection ultimately failed,
        # yield them before re-raising
        for retry_msg in retry_messages:
            yield retry_msg
        raise

    reply = "".join(parts)

    cfg = _cfg(config_path)
    _persist_chat_turn(context, prompt, reply, cfg, sessions_dir)
