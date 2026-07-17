"""Unit tests for nyxgpt.token_counter (src/nyxgpt/token_counter.py).

Broader token-counting/budget-enforcement coverage lives in
tests/unit/test_token_counting.py; this file targets token_counter.py's
own edge cases directly.
"""

from __future__ import annotations

import pytest

from nyxgpt.token_counter import count_message_tokens

pytestmark = pytest.mark.unit


def test_count_message_tokens_with_multimodal_list_content() -> None:
    """A message's `content` can be a list of blocks (multimodal messages),
    not just a plain string. Text-type blocks are joined and counted, and a
    non-dict entry in the list (malformed/unexpected block shape) must be
    skipped rather than raising or being coerced to text."""
    multimodal_tokens = count_message_tokens(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "abc"},
                    "not-a-dict-block",
                    {"type": "text", "text": "def"},
                ],
            }
        ]
    )
    equivalent_tokens = count_message_tokens([{"role": "user", "content": "abc def"}])

    # The non-dict entry contributes nothing, so the two text blocks joined
    # with a space ("abc def") must tokenize identically to that plain string.
    assert multimodal_tokens == equivalent_tokens


def test_count_message_tokens_multimodal_with_multiple_text_blocks() -> None:
    """Multiple text blocks in one multimodal message are joined with a
    space before being tokenized, matching a single combined string."""
    joined_tokens = count_message_tokens(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
    )
    equivalent_tokens = count_message_tokens([{"role": "user", "content": "Hello world"}])

    assert joined_tokens == equivalent_tokens


def test_count_message_tokens_multimodal_with_no_text_blocks() -> None:
    """A multimodal message containing only non-text blocks (e.g. just an
    image) must not error and should count zero content tokens -- only the
    per-message and role overhead."""
    result = count_message_tokens(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}],
            }
        ]
    )
    overhead_only = count_message_tokens([{"role": "user", "content": ""}])

    assert result == overhead_only
