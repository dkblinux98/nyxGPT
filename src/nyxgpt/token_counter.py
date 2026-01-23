"""Token counting utilities for context window budget management."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Lazy import tiktoken to avoid import errors if not installed
_tiktoken_encoder = None


def _get_encoder():
    """Get or create tiktoken encoder instance."""
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        try:
            import tiktoken

            # Use cl100k_base encoding which is used by GPT-3.5/GPT-4
            # This provides reasonable estimates for most LLMs
            _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            logger.warning(
                "tiktoken not installed, token counting unavailable. "
                "Install with: pip install tiktoken"
            )
            raise
    return _tiktoken_encoder


def count_tokens(text: str) -> int:
    """Count tokens in a text string.

    Uses tiktoken with cl100k_base encoding (GPT-3.5/GPT-4 tokenizer).
    This provides reasonable estimates for most LLMs including Ollama models.

    Args:
        text: Text to count tokens for

    Returns:
        Number of tokens in the text

    Raises:
        ImportError: If tiktoken is not installed
    """
    if not text:
        return 0

    try:
        encoder = _get_encoder()
        return len(encoder.encode(text))
    except ImportError:
        # Fallback to character-based estimation if tiktoken not available
        # Rough estimate: ~4 characters per token for English text
        return len(text) // 4


def count_message_tokens(messages: list[dict[str, str]]) -> int:
    """Count total tokens in a list of chat messages.

    This accounts for message formatting overhead in addition to content tokens.
    Based on OpenAI's token counting formula with adjustments for chat format.

    Args:
        messages: List of message dicts with 'role' and 'content' keys

    Returns:
        Total token count including formatting overhead

    Raises:
        ImportError: If tiktoken is not installed
    """
    if not messages:
        return 0

    try:
        encoder = _get_encoder()

        # Token overhead per message (role markers, formatting, etc.)
        # Based on OpenAI's documented formula:
        # 3 tokens per message (formatting) + 1 token per message (role)
        tokens_per_message = 4

        total_tokens = 0

        for message in messages:
            total_tokens += tokens_per_message

            # Count role tokens
            role = message.get("role", "")
            if role:
                total_tokens += len(encoder.encode(role))

            # Count content tokens
            content = message.get("content", "")
            if content:
                total_tokens += len(encoder.encode(content))

        # Add 2 tokens for reply priming (assistant response start)
        total_tokens += 2

        return total_tokens

    except ImportError:
        # Fallback to simple character-based estimation
        total_chars = sum(
            len(msg.get("role", "")) + len(msg.get("content", "")) for msg in messages
        )
        # Add overhead estimate and divide by 4 chars/token
        return (total_chars + len(messages) * 10) // 4


__all__ = [
    "count_tokens",
    "count_message_tokens",
]
