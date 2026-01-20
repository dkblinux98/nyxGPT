"""Tests for message edit and regenerate functionality (#2641)."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from mygpt import sessions

pytestmark = pytest.mark.unit


def _cfg_with_sessions_dir(sessions_dir: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["mygpt"] = {
        "sessions_dir": str(sessions_dir),
        "default_model": "llama3.1:8b",
    }
    return cfg


def test_edit_message_updates_content(tmp_path: Path) -> None:
    """Test that edit_message updates message content and adds metadata."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Create a session with messages
    state = sessions.load_session("test-edit", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Original message"})
    state.messages.append({"role": "assistant", "content": "Original response"})
    sessions.save_session(state, cfg)

    # Edit the first message
    ok, msg = sessions.edit_message(
        session_name="test-edit",
        message_index=0,
        new_content="Edited message",
        sessions_dir=sessions.get_sessions_dir(cfg),
        fork=False,  # Don't fork for this test
    )

    assert ok, f"Edit should succeed: {msg}"

    # Reload and verify
    state2 = sessions.load_session("test-edit", cfg)
    assert len(state2.messages) == 2
    assert state2.messages[0]["content"] == "Edited message"
    assert "edited_at" in state2.messages[0]
    assert state2.messages[0]["original_content"] == "Original message"


def test_edit_message_forks_conversation(tmp_path: Path) -> None:
    """Test that edit_message with fork=True truncates following messages."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Create a session with multiple turns
    state = sessions.load_session("test-fork", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Message 1"})
    state.messages.append({"role": "assistant", "content": "Response 1"})
    state.messages.append({"role": "user", "content": "Message 2"})
    state.messages.append({"role": "assistant", "content": "Response 2"})
    sessions.save_session(state, cfg)

    # Edit the first message with fork=True
    ok, msg = sessions.edit_message(
        session_name="test-fork",
        message_index=0,
        new_content="Edited message 1",
        sessions_dir=sessions.get_sessions_dir(cfg),
        fork=True,
    )

    assert ok, f"Edit should succeed: {msg}"

    # Reload and verify only messages up to and including edited one remain
    state2 = sessions.load_session("test-fork", cfg)
    assert len(state2.messages) == 1
    assert state2.messages[0]["content"] == "Edited message 1"


def test_edit_message_validates_index(tmp_path: Path) -> None:
    """Test that edit_message rejects invalid message indices."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Create a session with one message
    state = sessions.load_session("test-invalid", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Only message"})
    sessions.save_session(state, cfg)

    # Try to edit with invalid index
    ok, msg = sessions.edit_message(
        session_name="test-invalid",
        message_index=5,
        new_content="Should fail",
        sessions_dir=sessions.get_sessions_dir(cfg),
    )

    assert not ok
    assert "Invalid message index" in msg


def test_edit_message_nonexistent_session(tmp_path: Path) -> None:
    """Test that edit_message fails gracefully for nonexistent session."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    ok, msg = sessions.edit_message(
        session_name="nonexistent",
        message_index=0,
        new_content="Should fail",
        sessions_dir=sessions.get_sessions_dir(cfg),
    )

    assert not ok
    assert "No such session" in msg


def test_truncate_after_message(tmp_path: Path) -> None:
    """Test that truncate_after_message keeps messages up to specified index."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Create a session with multiple messages
    state = sessions.load_session("test-truncate", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Message 1"})
    state.messages.append({"role": "assistant", "content": "Response 1"})
    state.messages.append({"role": "user", "content": "Message 2"})
    state.messages.append({"role": "assistant", "content": "Response 2"})
    sessions.save_session(state, cfg)

    # Truncate after index 1 (keep first 2 messages)
    ok, msg = sessions.truncate_after_message(
        session_name="test-truncate",
        message_index=1,
        sessions_dir=sessions.get_sessions_dir(cfg),
    )

    assert ok, f"Truncate should succeed: {msg}"

    # Reload and verify
    state2 = sessions.load_session("test-truncate", cfg)
    assert len(state2.messages) == 2
    assert state2.messages[0]["content"] == "Message 1"
    assert state2.messages[1]["content"] == "Response 1"


def test_truncate_after_message_validates_index(tmp_path: Path) -> None:
    """Test that truncate_after_message rejects invalid indices."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    state = sessions.load_session("test-trunc-invalid", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Only message"})
    sessions.save_session(state, cfg)

    ok, msg = sessions.truncate_after_message(
        session_name="test-trunc-invalid",
        message_index=10,
        sessions_dir=sessions.get_sessions_dir(cfg),
    )

    assert not ok
    assert "Invalid message index" in msg


def test_message_ids_and_timestamps_preserved(tmp_path: Path) -> None:
    """Test that message IDs and timestamps are preserved when editing."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Create a session with a message that has extra fields
    state = sessions.load_session("test-preserve", cfg, new_session=True)
    state.messages.append(
        {
            "role": "user",
            "content": "Original",
            "id": "msg-123",
            "timestamp": "2024-01-01T00:00:00Z",
        }
    )
    sessions.save_session(state, cfg)

    # Edit the message
    ok, msg = sessions.edit_message(
        session_name="test-preserve",
        message_index=0,
        new_content="Edited",
        sessions_dir=sessions.get_sessions_dir(cfg),
        fork=False,
    )

    assert ok

    # Verify extra fields are preserved
    state2 = sessions.load_session("test-preserve", cfg)
    assert state2.messages[0]["id"] == "msg-123"
    assert state2.messages[0]["timestamp"] == "2024-01-01T00:00:00Z"
    assert state2.messages[0]["content"] == "Edited"
    assert "edited_at" in state2.messages[0]
