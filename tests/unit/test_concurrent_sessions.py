"""Tests for concurrent session access and atomic file operations.

These tests verify that session files remain consistent even when
accessed by multiple processes or threads simultaneously.
"""

from __future__ import annotations

import threading

import pytest

from nyxgpt import sessions
from nyxgpt.config import load_config


@pytest.fixture
def test_sessions_dir(tmp_path):
    """Create a temporary sessions directory."""
    sessions_dir = tmp_path / "test_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


@pytest.fixture
def test_config(tmp_path):
    """Create a minimal test config."""
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[nyxgpt]\n" "default_model = llama3.1:8b\n" f"sessions_dir = {tmp_path / 'sessions'}\n"
    )
    return load_config(config_path)


def test_atomic_write_prevents_corruption(test_sessions_dir):
    """Verify that atomic writes prevent file corruption."""
    session_name = "test-atomic"
    session_file = sessions.session_file_for(session_name, test_sessions_dir)

    # Create initial session
    messages = [{"role": "user", "content": "Hello"}]
    sessions.save_session_messages(session_file, messages)

    # Verify file is valid JSON
    loaded = sessions.load_session_messages(session_file)
    assert loaded == messages

    # Simulate rapid successive writes
    for i in range(10):
        new_messages = messages + [{"role": "assistant", "content": f"Reply {i}"}]
        sessions.save_session_messages(session_file, new_messages)

    # File should still be valid JSON
    final_messages = sessions.load_session_messages(session_file)
    assert isinstance(final_messages, list)
    assert all(isinstance(m, dict) for m in final_messages)


def test_concurrent_reads_same_session(test_sessions_dir):
    """Multiple threads reading the same session should not interfere."""
    session_name = "test-concurrent-read"
    session_file = sessions.session_file_for(session_name, test_sessions_dir)

    # Create a session with some messages
    messages = [{"role": "user", "content": f"Message {i}"} for i in range(100)]
    sessions.save_session_messages(session_file, messages)

    results = []
    errors = []

    def read_session():
        try:
            loaded = sessions.load_session_messages(session_file)
            results.append(len(loaded))
        except Exception as e:
            errors.append(str(e))

    # Spawn 10 threads reading concurrently
    threads = [threading.Thread(target=read_session) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All reads should succeed
    assert len(errors) == 0, f"Read errors: {errors}"
    assert all(count == 100 for count in results), "Inconsistent read results"


def test_concurrent_writes_different_sessions(test_sessions_dir):
    """Writing to different sessions concurrently should work correctly."""
    num_sessions = 10

    def write_session(session_id: int):
        session_name = f"session-{session_id}"
        session_file = sessions.session_file_for(session_name, test_sessions_dir)
        messages = [
            {"role": "user", "content": f"Session {session_id}, message {i}"} for i in range(10)
        ]
        sessions.save_session_messages(session_file, messages)

    # Write to multiple sessions concurrently
    threads = [threading.Thread(target=write_session, args=(i,)) for i in range(num_sessions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify all sessions were created and are valid
    for i in range(num_sessions):
        session_name = f"session-{i}"
        session_file = sessions.session_file_for(session_name, test_sessions_dir)
        messages = sessions.load_session_messages(session_file)
        assert len(messages) == 10
        assert all(f"Session {i}" in m["content"] for m in messages)


def test_session_name_validation_edge_cases():
    """Test edge cases in session name validation."""

    # Valid names
    valid_names = [
        "simple",
        "with-hyphens",
        "with_underscores",
        "MixedCase123",
        "a" * 64,  # Max length
    ]
    for name in valid_names:
        result = sessions.validate_session_name(name)
        assert result == name.strip()

    # Invalid names
    invalid_names = [
        "",  # Empty
        "   ",  # Whitespace only
        "with spaces",  # Contains spaces
        "with/slash",  # Path separator
        "with\\backslash",  # Path separator
        "../etc/passwd",  # Path traversal
        "a" * 65,  # Too long
        "with.dots",  # Contains dots
        "special!chars",  # Special characters
    ]
    for name in invalid_names:
        with pytest.raises(ValueError):
            sessions.validate_session_name(name)


def test_metadata_concurrent_updates(test_sessions_dir):
    """Test concurrent updates to session metadata."""
    session_name = "test-meta"
    session_file = sessions.session_file_for(session_name, test_sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    # Create initial metadata
    initial_meta = sessions.ensure_meta_defaults({}, model="test-model")
    sessions.save_session_meta(meta_file, initial_meta)

    def add_tag(tag: str):
        # Load, modify, save (simulating real usage)
        meta = sessions.load_session_meta(meta_file)
        meta = sessions.ensure_meta_defaults(meta)
        tags = meta.get("tags", [])
        tags.append(tag)
        meta["tags"] = sessions.normalize_tags(tags)
        sessions.save_session_meta(meta_file, meta)

    # Add tags concurrently
    threads = [threading.Thread(target=add_tag, args=(f"tag-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Load final metadata
    final_meta = sessions.load_session_meta(meta_file)

    # Due to race conditions, we might not have all 5 tags
    # (last-write-wins), but the file should be valid JSON
    assert isinstance(final_meta, dict)
    assert "tags" in final_meta
    assert isinstance(final_meta["tags"], list)


def test_corrupted_session_recovery(test_sessions_dir):
    """Test that corrupted session files are handled gracefully."""
    session_name = "test-corrupted"
    session_file = sessions.session_file_for(session_name, test_sessions_dir)

    # Write corrupted JSON
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{ invalid json ]", encoding="utf-8")

    # Should return empty list instead of crashing
    messages = sessions.load_session_messages(session_file)
    assert messages == []

    # Should be able to overwrite with valid data
    valid_messages = [{"role": "user", "content": "Hello"}]
    sessions.save_session_messages(session_file, valid_messages)

    # Now it should work
    loaded = sessions.load_session_messages(session_file)
    assert loaded == valid_messages
