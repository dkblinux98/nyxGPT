from __future__ import annotations

import configparser
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nyxgpt import sessions

pytestmark = pytest.mark.unit


def _cfg_with_sessions_dir(sessions_dir: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "sessions_dir": str(sessions_dir),
        "default_model": "llama3.1:8b",
        # Disable auto-summarization and auto-sync to prevent session renaming during tests
        "auto_summarize_enabled": "false",
        "auto_sync_filename": "false",
    }
    return cfg


def _cfg_auto_summarize(
    sessions_dir: Path, *, after_messages: int = 2, auto_sync: bool = False
) -> configparser.ConfigParser:
    """Config with auto-summarize enabled (and optionally auto-sync), used to
    exercise the auto-summarization trigger paths in persist_after_exchange /
    save_session."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "sessions_dir": str(sessions_dir),
        "default_model": "llama3.1:8b",
        "auto_summarize_enabled": "true",
        "auto_summarize_after_messages": str(after_messages),
        "auto_sync_filename": "true" if auto_sync else "false",
    }
    return cfg


def test_init_session_creates_files_and_defaults(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)

    sf, mf, msgs, meta = sessions.init_session(
        session_name="test-1",
        sessions_dir=sessions_dir,
        new_session=True,
        model="llama3.1:8b",
        system="You are helpful.",
    )

    assert sf.exists(), "session json should be created"
    assert mf.exists(), "meta json should be created"

    # If a system prompt is provided, it is stored as the first message.
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are helpful."

    # Meta defaults
    assert meta.get("model") == "llama3.1:8b"
    assert isinstance(meta.get("created_at"), str)
    assert isinstance(meta.get("updated_at"), str)


def test_load_and_save_session_roundtrip(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Load (creates if missing)
    state = sessions.load_session("s1", cfg, new_session=True)
    assert state.name == "s1"
    assert state.session_file.exists()
    assert state.meta_file.exists()
    assert state.messages == []

    # Add a turn and save
    state.messages.append({"role": "user", "content": "Hello"})
    state.messages.append({"role": "assistant", "content": "Hi!"})
    sessions.save_session(state, cfg)

    # Reload and verify persistence
    state2 = sessions.load_session("s1", cfg)
    assert len(state2.messages) == 2
    assert state2.messages[0]["role"] == "user"
    assert state2.messages[0]["content"] == "Hello"
    assert state2.messages[1]["role"] == "assistant"


def test_sessions_dir_override_is_respected(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    override_dir = tmp_path / "override_sessions"
    state = sessions.load_session(
        "s-override", cfg, sessions_dir_override=str(override_dir), new_session=True
    )
    assert state.session_file.parent == override_dir

    state.messages.append({"role": "user", "content": "A"})
    state.messages.append({"role": "assistant", "content": "B"})
    sessions.save_session(state, cfg, sessions_dir_override=str(override_dir))

    # Ensure it can be reloaded from override location
    state2 = sessions.load_session("s-override", cfg, sessions_dir_override=str(override_dir))
    assert len(state2.messages) == 2


def test_list_sessions_finds_created_sessions(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)

    sessions.init_session("a", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("b", sessions_dir, new_session=True, model="llama3.1:8b")

    found = sessions.list_sessions(cfg)
    # list_sessions may return dict rows; normalize to names
    names: set[str] = set()
    for item in found:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and "name" in item:
            names.add(str(item["name"]))
        elif isinstance(item, dict) and "session" in item:
            names.add(str(item["session"]))

    assert "a" in names
    assert "b" in names


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        " ",
        "../escape",
        "..\\escape",
    ],
)
def test_session_name_validation_rejects_path_traversal(tmp_path: Path, bad_name: str) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # If your implementation allows these, this test will fail and we can tighten validation.
    with pytest.raises(ValueError):
        sessions.load_session(bad_name, cfg, new_session=True)


def test_validate_session_name_rejects_non_string() -> None:
    """validate_session_name should raise ValueError for non-string input."""
    with pytest.raises(ValueError, match="session name must be a string"):
        sessions.validate_session_name(123)  # type: ignore


def test_validate_session_name_rejects_empty_string() -> None:
    """validate_session_name should raise ValueError for empty string."""
    with pytest.raises(ValueError, match="session name cannot be empty"):
        sessions.validate_session_name("")


def test_validate_session_name_rejects_too_long() -> None:
    """validate_session_name should raise ValueError for names > 64 chars."""
    too_long = "a" * 65
    with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
        sessions.validate_session_name(too_long)


def test_validate_session_name_rejects_invalid_chars() -> None:
    """validate_session_name should raise ValueError for invalid characters."""
    with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
        sessions.validate_session_name("invalid@name")


def test_load_session_corrupted_json_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_session should handle corrupted JSON gracefully by returning empty messages."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a corrupted session file
    session_file = sessions_dir / "corrupted.json"
    session_file.write_text("{invalid json content")

    # Per load_session_messages implementation (lines 121-144), corrupted JSON
    # is caught, logged as warning, and returns empty list
    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        state = sessions.load_session("corrupted", cfg)

    # Should succeed with empty messages (not raise exception)
    assert isinstance(state.messages, list)
    assert state.messages == []
    assert state.name == "corrupted"

    # Verify warning was logged
    assert "Invalid JSON in session file" in caplog.text


def test_save_session_creates_parent_directory(tmp_path: Path) -> None:
    """save_session should create parent directory if it doesn't exist."""
    cfg = _cfg_with_sessions_dir(tmp_path / "new_sessions" / "nested")

    # Load (which should create the session)
    state = sessions.load_session("test", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "test"})

    # Save should work even if directory structure doesn't exist
    sessions.save_session(state, cfg)

    # Verify the session was saved
    assert state.session_file.exists()
    assert state.meta_file.exists()


def test_export_session_markdown(tmp_path: Path) -> None:
    """Test session export to Markdown format."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a test session with messages and metadata
    session_file = sessions.session_file_for("test-session", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm doing well, thanks!"},
    ]

    metadata = {
        "title": "Test Session",
        "summary": "A test conversation",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:05:00",
        "tags": ["test", "example"],
        "model": "llama3.1:8b",
        "pinned": False,
        "token_estimate": 100,
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to markdown
    ok, content = sessions.export_session_markdown("test-session", sessions_dir)

    assert ok, "Export should succeed"
    assert "# Test Session" in content
    assert "**Summary:** A test conversation" in content
    assert "**Session:** test-session" in content
    assert "**Tags:** test, example" in content
    assert "**Model:** llama3.1:8b" in content
    assert "## User" in content
    assert "Hello" in content
    assert "## Assistant" in content
    assert "Hi there!" in content


def test_export_session_json(tmp_path: Path) -> None:
    """Test session export to JSON format."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a test session
    session_file = sessions.session_file_for("json-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Test message"},
        {"role": "assistant", "content": "Test response"},
    ]

    metadata = {
        "title": "JSON Test",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:05:00",
        "tags": [],
        "pinned": False,
        "token_estimate": 50,
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to JSON
    ok, content = sessions.export_session_json("json-test", sessions_dir)

    assert ok, "Export should succeed"

    # Parse the JSON to verify structure
    data = json.loads(content)
    assert data["name"] == "json-test"
    assert "metadata" in data
    assert "messages" in data
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][0]["content"] == "Test message"
    assert data["metadata"]["title"] == "JSON Test"


def test_export_session_html(tmp_path: Path) -> None:
    """Test session export to HTML format."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a test session
    session_file = sessions.session_file_for("html-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Test <html> escaping"},
        {"role": "assistant", "content": "Escaping > works"},
    ]

    metadata = {
        "title": "HTML Test",
        "summary": "Testing HTML export",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:05:00",
        "tags": ["html", "test"],
        "model": "llama3.1:8b",
        "pinned": False,
        "token_estimate": 50,
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to HTML
    ok, content = sessions.export_session_html("html-test", sessions_dir)

    assert ok, "Export should succeed"
    assert "<!DOCTYPE html>" in content
    assert "<title>HTML Test</title>" in content
    assert "<h1>HTML Test</h1>" in content
    assert "Testing HTML export" in content
    assert "&lt;html&gt;" in content, "Should escape HTML special characters"
    assert "&gt; works" in content, "Should escape > character"
    assert '<div class="message user">' in content
    assert '<div class="message assistant">' in content


def test_sink_functions_refuse_paths_outside_allowed_roots() -> None:
    """Inline sink-side barriers (CodeQL py/path-injection): the low-level
    session I/O functions must refuse any path resolving outside the user's
    home directory or the system temp directory, regardless of caller-side
    validation -- read functions return their empty contract value, write
    functions raise ValueError and never touch the filesystem."""
    outside = Path("/etc/nyxgpt-attack-test.json")

    assert sessions.load_session_messages(outside) == []
    assert sessions.load_session_messages_paginated(outside) == ([], 0)
    assert sessions.load_session_meta(outside) == {}
    with pytest.raises(ValueError, match="allowed data area"):
        sessions.save_session_messages(outside, [{"role": "user", "content": "x"}])
    with pytest.raises(ValueError, match="allowed data area"):
        sessions.save_session_meta(outside, {"title": "x"})
    assert not outside.exists(), "Write barrier must trigger before any filesystem effect"


def test_sink_functions_refuse_traversal_escapes(tmp_path: Path) -> None:
    """A `..` traversal that escapes the allowed roots is collapsed by
    realpath and refused at the sink."""
    sneaky = tmp_path / ".." / ".." / ".." / ".." / ".." / ".." / "etc" / "shadow"
    assert sessions.load_session_messages(sneaky) == []
    assert sessions.load_session_meta(sneaky) == {}


def test_export_functions_refuse_dir_outside_allowed_roots() -> None:
    """The export functions' own inline barriers refuse a sessions dir that
    escapes the allowed roots (the chokepoint raises first; either way no
    filesystem access happens outside the data area)."""
    with pytest.raises(ValueError):
        sessions.export_session_markdown("valid-name", Path("/etc"))


def test_export_session_html_escapes_untrusted_fields(tmp_path: Path) -> None:
    """HTML export must escape the session name, title, summary, tags, and
    message content so none can inject markup (CodeQL py/reflected-xss).

    The endpoint serves this with ``Content-Type: text/html``; an unescaped
    ``<script>`` in any user-controlled field would execute in the browser.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    payload = "<script>alert('xss')</script>"
    session_file = sessions.session_file_for("xss-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": payload},
        {"role": "assistant", "content": "reply"},
    ]
    metadata = {
        "title": payload,
        "summary": payload,
        "created_at": payload,
        "updated_at": "2024-01-01T12:05:00",
        "tags": [payload],
        "model": payload,
    }
    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    ok, content = sessions.export_session_html("xss-test", sessions_dir)

    assert ok
    # The raw script tag must never appear; its escaped form must.
    assert "<script>alert('xss')</script>" not in content
    assert "&lt;script&gt;" in content


def test_export_session_nonexistent(tmp_path: Path) -> None:
    """Test export of nonexistent session returns error."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Try to export a session that doesn't exist
    ok, msg = sessions.export_session_markdown("nonexistent", sessions_dir)
    assert not ok
    assert msg == "No such session"

    ok, msg = sessions.export_session_json("nonexistent", sessions_dir)
    assert not ok
    assert msg == "No such session"

    ok, msg = sessions.export_session_html("nonexistent", sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_export_includes_all_metadata_fields(tmp_path: Path) -> None:
    """Test that export includes all metadata fields."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("metadata-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "test"}]

    metadata = {
        "title": "Complete Metadata Test",
        "summary": "Has all fields",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T13:00:00",
        "tags": ["tag1", "tag2", "tag3"],
        "model": "llama3.1:8b",
        "pinned": True,
        "token_estimate": 123,
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Test markdown export includes all fields
    ok, content = sessions.export_session_markdown("metadata-test", sessions_dir)
    assert ok
    assert "Complete Metadata Test" in content
    assert "Has all fields" in content
    assert "2024-01-01T12:00:00" in content
    assert "2024-01-01T13:00:00" in content
    assert "tag1, tag2, tag3" in content
    assert "llama3.1:8b" in content

    # Test JSON export includes all fields
    ok, json_content = sessions.export_session_json("metadata-test", sessions_dir)
    assert ok
    data = json.loads(json_content)
    assert data["metadata"]["title"] == "Complete Metadata Test"
    assert data["metadata"]["summary"] == "Has all fields"
    assert data["metadata"]["tags"] == ["tag1", "tag2", "tag3"]

    # Test HTML export includes all fields
    ok, html_content = sessions.export_session_html("metadata-test", sessions_dir)
    assert ok
    assert "Complete Metadata Test" in html_content
    assert "Has all fields" in html_content
    assert "tag1, tag2, tag3" in html_content


# --- File Locking Tests ---


# Helper functions for multiprocessing tests (must be module-level for pickle)
def _hold_lock_for_duration(file_path: Path, duration: float) -> None:
    """Helper: hold a lock for specified duration."""
    import time

    with sessions.file_lock(file_path, timeout=10.0):
        time.sleep(duration)


def _hold_lock_briefly(file_path: Path) -> None:
    """Helper: hold a lock briefly."""
    import time

    with sessions.file_lock(file_path, timeout=10.0):
        time.sleep(0.5)


def test_file_lock_creates_file_atomically(tmp_path: Path) -> None:
    """Test that file_lock creates lock file atomically without TOCTOU race."""
    test_file = tmp_path / "new.lock"

    # File should not exist initially
    assert not test_file.exists()

    # file_lock should create it atomically using O_CREAT (no separate touch)
    with sessions.file_lock(test_file, timeout=1.0) as fd:
        assert fd >= 0  # Valid file descriptor
        assert test_file.exists()  # File was created

    # Should be able to use the same file again
    with sessions.file_lock(test_file, timeout=1.0) as fd:
        assert fd >= 0


def test_file_lock_acquisition_and_release(tmp_path: Path) -> None:
    """Test that file_lock successfully acquires and releases locks."""
    test_file = tmp_path / "test.lock"
    test_file.touch()

    # Should be able to acquire lock
    with sessions.file_lock(test_file, timeout=1.0) as fd:
        assert fd >= 0  # Valid file descriptor

    # Should be able to acquire lock again after release
    with sessions.file_lock(test_file, timeout=1.0) as fd:
        assert fd >= 0


def test_file_lock_timeout_when_locked(tmp_path: Path) -> None:
    """Test that file_lock raises TimeoutError when file is already locked."""
    import multiprocessing
    import time

    test_file = tmp_path / "test.lock"
    test_file.touch()

    # Start a process that holds the lock for 2 seconds
    p = multiprocessing.Process(target=_hold_lock_for_duration, args=(test_file, 2.0))
    p.start()

    try:
        # Give the process time to acquire the lock
        time.sleep(0.5)

        # Try to acquire lock with 0.5s timeout (should fail since lock is held)
        with (
            pytest.raises(TimeoutError, match="Could not acquire lock"),
            sessions.file_lock(test_file, timeout=0.5),
        ):
            pass

    finally:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()


def test_file_lock_waits_and_succeeds(tmp_path: Path) -> None:
    """Test that file_lock waits for lock to become available."""
    import multiprocessing
    import time

    test_file = tmp_path / "test.lock"
    test_file.touch()

    # Start a process that holds the lock briefly
    p = multiprocessing.Process(target=_hold_lock_briefly, args=(test_file,))
    p.start()

    try:
        # Give the process time to acquire the lock
        time.sleep(0.2)

        # Try to acquire lock with 2s timeout (should succeed after waiting)
        with sessions.file_lock(test_file, timeout=2.0) as fd:
            assert fd >= 0

    finally:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()


def test_sync_filename_with_title_uses_file_locking(tmp_path: Path) -> None:
    """Test that sync_filename_with_title uses file locking during rename."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a session with a title
    sf, mf, msgs, meta = sessions.init_session(
        session_name="old-name",
        sessions_dir=sessions_dir,
        new_session=True,
        model="llama3.1:8b",
    )
    meta["title"] = "New Session Title"
    sessions.save_session_meta(mf, meta)

    # Rename should succeed (with file locking)
    success, message, new_name = sessions.sync_filename_with_title(
        "old-name", sessions_dir, force=True
    )

    assert success
    assert message == "renamed"
    assert new_name == "new-session-title"

    # Verify new files exist
    new_sf = sessions.session_file_for(new_name, sessions_dir)
    new_mf = sessions.meta_file_for(new_sf)
    assert new_sf.exists()
    assert new_mf.exists()

    # Verify old files are deleted
    assert not sf.exists()
    assert not mf.exists()


def test_file_lock_ordering_prevents_deadlock(tmp_path: Path) -> None:
    """Test that file locks are acquired in consistent alphabetical order to prevent deadlock."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a session with both session and metadata files
    sf, mf, msgs, meta = sessions.init_session(
        session_name="test-lock-order",
        sessions_dir=sessions_dir,
        new_session=True,
        model="llama3.1:8b",
    )
    meta["title"] = "Lock Order Test"
    sessions.save_session_meta(mf, meta)

    # Verify files exist and have predictable paths
    assert sf.exists()
    assert mf.exists()

    # The implementation should lock files in alphabetical order by path
    # This test verifies the rename operation completes successfully
    # (demonstrating no deadlock occurs even with nested lock requirements)
    success, message, new_name = sessions.sync_filename_with_title(
        "test-lock-order", sessions_dir, force=True
    )

    assert success
    assert message == "renamed"
    assert new_name == "lock-order-test"

    # If deadlock occurred, the operation would timeout and fail
    # Success proves locks were acquired in consistent order


@pytest.mark.unit
def test_file_lock_concurrent_access_no_deadlock(tmp_path: Path) -> None:
    """Test that concurrent file operations don't deadlock with alphabetical lock ordering."""
    import threading
    import time

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create multiple sessions with metadata
    sessions_to_test = []
    for i in range(3):
        session_name = f"concurrent-test-{i}"
        sf, mf, msgs, meta = sessions.init_session(
            session_name=session_name,
            sessions_dir=sessions_dir,
            new_session=True,
            model="llama3.1:8b",
        )
        meta["title"] = f"Concurrent Test {i}"
        sessions.save_session_meta(mf, meta)
        sessions_to_test.append(session_name)

    # Track successes and failures
    results = {"successes": 0, "failures": 0, "errors": []}
    results_lock = threading.Lock()

    def rename_session(session_name: str):
        """Rename a session (requires locking both session and metadata files)."""
        try:
            success, message, new_name = sessions.sync_filename_with_title(
                session_name, sessions_dir, force=True
            )
            with results_lock:
                if success:
                    results["successes"] += 1
                else:
                    results["failures"] += 1
                    results["errors"].append(f"{session_name}: {message}")
        except Exception as e:
            with results_lock:
                results["failures"] += 1
                results["errors"].append(f"{session_name}: {e}")

    # Launch concurrent rename operations
    threads = []
    for session_name in sessions_to_test:
        thread = threading.Thread(target=rename_session, args=(session_name,))
        threads.append(thread)
        thread.start()
        time.sleep(0.01)  # Small delay to increase concurrent overlap

    # Wait for all threads to complete (with timeout to catch deadlocks)
    timeout_per_thread = 5.0
    for thread in threads:
        thread.join(timeout=timeout_per_thread)
        if thread.is_alive():
            # Thread is still running - potential deadlock
            pytest.fail(
                f"Thread timed out after {timeout_per_thread}s - possible deadlock detected!"
            )

    # Verify all operations succeeded
    assert results["failures"] == 0, f"Failures: {results['errors']}"
    assert results["successes"] == len(
        sessions_to_test
    ), f"Expected {len(sessions_to_test)} successes, got {results['successes']}"


@pytest.mark.unit
def test_file_lock_ordering_verification(tmp_path: Path) -> None:
    """Test that verify_lock_ordering catches violations during execution."""
    import threading

    test_file_a = tmp_path / "a.lock"
    test_file_b = tmp_path / "b.lock"

    # Track whether assertion was raised
    assertion_raised = {"value": False}

    def try_wrong_order():
        """Try to verify locks in wrong order (should raise AssertionError)."""
        try:
            # Files in WRONG order (b before a)
            sessions.verify_lock_ordering(test_file_b, test_file_a)
        except AssertionError:
            assertion_raised["value"] = True

    # Only test in debug mode
    if __debug__:
        thread = threading.Thread(target=try_wrong_order)
        thread.start()
        thread.join(timeout=1.0)

        assert assertion_raised[
            "value"
        ], "verify_lock_ordering should raise AssertionError for wrong order"


@pytest.mark.unit
def test_verify_lock_ordering_correct_order(tmp_path: Path) -> None:
    """Test that verify_lock_ordering passes when files are in alphabetical order."""
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_c = tmp_path / "c.txt"

    # Should not raise - files in correct alphabetical order
    sessions.verify_lock_ordering(file_a, file_b, file_c)


@pytest.mark.unit
def test_verify_lock_ordering_single_file(tmp_path: Path) -> None:
    """Test that verify_lock_ordering passes for single file (no ordering needed)."""
    file_a = tmp_path / "a.txt"

    # Should not raise - single file has no ordering concerns
    sessions.verify_lock_ordering(file_a)


@pytest.mark.unit
def test_verify_lock_ordering_violation() -> None:
    """Test that verify_lock_ordering detects ordering violations in debug mode."""
    from pathlib import Path

    file_a = Path("/sessions/a.json")
    file_b = Path("/sessions/b.json")

    # Only test in debug mode (when __debug__ is True)
    if __debug__:
        # Should raise AssertionError - files in wrong order
        with pytest.raises(AssertionError, match="File lock ordering violation detected"):
            sessions.verify_lock_ordering(file_b, file_a)  # Wrong order!
    else:
        # In optimized mode (-O flag), no assertion raised
        sessions.verify_lock_ordering(file_b, file_a)


@pytest.mark.unit
def test_verify_lock_ordering_mixed_paths(tmp_path: Path) -> None:
    """Test that verify_lock_ordering accepts correctly pre-sorted paths."""
    # Paths that should sort correctly alphabetically
    files = [
        tmp_path / "sessions" / "test.json.meta",
        tmp_path / "sessions" / "test.json",
    ]

    # Sort them using same method as production code
    files.sort(key=lambda p: p.as_posix())

    # Should pass - sorted correctly
    sessions.verify_lock_ordering(*files)


def test_metadata_file_deleted_between_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that rename handles metadata file deletion between existence checks (TOCTOU race)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a session with metadata
    sf, mf, msgs, meta = sessions.init_session(
        session_name="old-session-name",
        sessions_dir=sessions_dir,
        new_session=True,
        model="llama3.1:8b",
    )
    meta["title"] = "New TOCTOU Test Title"
    sessions.save_session_meta(mf, meta)

    # Verify both files exist
    assert sf.exists()
    assert mf.exists()

    # Simulate metadata file being deleted between initial check and lock acquisition
    # by monkey-patching file_lock to delete mf when called on it
    original_file_lock = sessions.file_lock
    lock_call_count = {"count": 0}

    def patched_file_lock(file_path: Path, timeout: float = 5.0):
        lock_call_count["count"] += 1
        # Delete metadata file when we try to lock it (simulates race condition)
        if (
            file_path == mf and lock_call_count["count"] == 2 and mf.exists()
        ):  # Second lock call is for metadata
            mf.unlink()
        return original_file_lock(file_path, timeout)

    monkeypatch.setattr("nyxgpt.sessions.file_lock", patched_file_lock)

    # Rename should succeed despite metadata file disappearing
    success, message, new_name = sessions.sync_filename_with_title(
        "old-session-name", sessions_dir, force=True
    )

    assert success
    assert message == "renamed"
    assert new_name == "new-toctou-test-title"  # Title sanitizes to this

    # Session file should be renamed successfully
    new_sf = sessions.session_file_for(new_name, sessions_dir)
    assert new_sf.exists()

    # Metadata file was deleted, so new metadata should not exist
    # The operation should have handled the missing file gracefully
    # (not crashed or left files in inconsistent state)


# --- Message Search Tests ---


@pytest.mark.unit
def test_search_messages_finds_exact_match(tmp_path: Path) -> None:
    """Test that search_messages finds exact string matches in message content."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a test session with known content
    sf = sessions.session_file_for("test-search", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm doing well, thanks for asking!"},
        {"role": "user", "content": "What is Python programming?"},
        {
            "role": "assistant",
            "content": "Python is a high-level programming language.",
        },
    ]

    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "title": "Test Session",
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Search for "Python"
    results = sessions.search_messages("Python", sessions_dir)

    assert len(results) == 2  # Found in 2 messages
    assert results[0]["session_name"] == "test-search"
    assert results[0]["message_index"] == 2
    assert "Python" in results[0]["content"]
    assert results[1]["message_index"] == 3


@pytest.mark.unit
def test_search_messages_case_insensitive_by_default(tmp_path: Path) -> None:
    """Test that search is case-insensitive by default."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("case-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "user", "content": "HELLO World"},
        {"role": "assistant", "content": "hello world"},
    ]

    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Search with lowercase should find both
    results = sessions.search_messages("hello", sessions_dir, case_sensitive=False)
    assert len(results) == 2


@pytest.mark.unit
def test_search_messages_case_sensitive(tmp_path: Path) -> None:
    """Test case-sensitive search mode."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("case-sensitive-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "user", "content": "HELLO World"},
        {"role": "assistant", "content": "hello world"},
    ]

    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Case-sensitive search for "hello" should only find lowercase
    results = sessions.search_messages("hello", sessions_dir, case_sensitive=True)
    assert len(results) == 1
    assert results[0]["message_index"] == 1


@pytest.mark.unit
def test_search_messages_filters_by_role(tmp_path: Path) -> None:
    """Test filtering search results by message role."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("role-filter-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "user", "content": "Tell me about cats"},
        {"role": "assistant", "content": "Cats are amazing animals"},
        {"role": "user", "content": "What about dogs?"},
        {"role": "assistant", "content": "Dogs are also great"},
    ]

    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Search for "about" with role filter
    user_results = sessions.search_messages("about", sessions_dir, role_filter="user")
    assistant_results = sessions.search_messages("about", sessions_dir, role_filter="assistant")

    assert len(user_results) == 2  # Both user messages contain "about"
    assert len(assistant_results) == 0  # No assistant messages contain "about"


@pytest.mark.unit
def test_search_messages_filters_by_session(tmp_path: Path) -> None:
    """Test filtering search to a specific session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create two sessions
    sf1 = sessions.session_file_for("session-1", sessions_dir)
    mf1 = sessions.meta_file_for(sf1)
    messages1 = [{"role": "user", "content": "Python is great"}]
    sessions.save_session_messages(sf1, messages1)
    sessions.save_session_meta(
        mf1,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    sf2 = sessions.session_file_for("session-2", sessions_dir)
    mf2 = sessions.meta_file_for(sf2)
    messages2 = [{"role": "user", "content": "Python is awesome"}]
    sessions.save_session_messages(sf2, messages2)
    sessions.save_session_meta(
        mf2,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Search across all sessions
    all_results = sessions.search_messages("Python", sessions_dir)
    assert len(all_results) == 2

    # Search only in session-1
    filtered_results = sessions.search_messages("Python", sessions_dir, session_filter="session-1")
    assert len(filtered_results) == 1
    assert filtered_results[0]["session_name"] == "session-1"


@pytest.mark.unit
def test_search_messages_respects_limit(tmp_path: Path) -> None:
    """Test that search respects the limit parameter."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("limit-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    # Create 10 messages all containing "test"
    messages = [{"role": "user", "content": f"test message {i}"} for i in range(10)]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Search with limit=5
    results = sessions.search_messages("test", sessions_dir, limit=5)
    assert len(results) == 5


@pytest.mark.unit
def test_search_messages_generates_preview(tmp_path: Path) -> None:
    """Test that search generates content preview with context."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("preview-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    long_content = "A" * 150 + "SEARCHTERM" + "B" * 150
    messages = [{"role": "user", "content": long_content}]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    results = sessions.search_messages("SEARCHTERM", sessions_dir)
    assert len(results) == 1

    preview = results[0]["content_preview"]
    # Preview should contain the search term
    assert "SEARCHTERM" in preview
    # Preview should be truncated (not full content)
    assert len(preview) < len(long_content)
    # Preview should have ellipsis indicating truncation
    assert "..." in preview


@pytest.mark.unit
def test_search_messages_empty_query(tmp_path: Path) -> None:
    """Test that empty query returns no results."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    results = sessions.search_messages("", sessions_dir)
    assert len(results) == 0


@pytest.mark.unit
def test_search_messages_no_matches(tmp_path: Path) -> None:
    """Test that search returns empty list when no matches found."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("no-match-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [{"role": "user", "content": "Hello world"}]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    results = sessions.search_messages("nonexistent", sessions_dir)
    assert len(results) == 0


@pytest.mark.unit
def test_search_messages_includes_session_title(tmp_path: Path) -> None:
    """Test that search results include session title when available."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("titled-session", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [{"role": "user", "content": "Test message"}]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "title": "My Titled Session",
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    results = sessions.search_messages("Test", sessions_dir)
    assert len(results) == 1
    assert results[0]["session_title"] == "My Titled Session"


@pytest.mark.unit
def test_search_messages_counts_matches(tmp_path: Path) -> None:
    """Test that search correctly counts multiple matches in same message."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("match-count-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [{"role": "user", "content": "Python Python Python"}]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    results = sessions.search_messages("Python", sessions_dir)
    assert len(results) == 1
    assert results[0]["matches"] == 3


@pytest.mark.unit
def test_merge_sessions_basic(tmp_path: Path) -> None:
    """Test basic session merge functionality."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create two sessions with messages
    sf1 = sessions.session_file_for("session1", sessions_dir)
    mf1 = sessions.meta_file_for(sf1)
    messages1 = [
        {"role": "user", "content": "Hello from session 1"},
        {"role": "assistant", "content": "Hi from session 1"},
    ]
    sessions.save_session_messages(sf1, messages1)
    sessions.save_session_meta(
        mf1,
        {
            "created_at": "2025-01-01T10:00:00",
            "updated_at": "2025-01-01T10:05:00",
            "pinned": False,
            "tags": ["tag1", "tag2"],
            "title": "First Session",
        },
    )

    sf2 = sessions.session_file_for("session2", sessions_dir)
    mf2 = sessions.meta_file_for(sf2)
    messages2 = [
        {"role": "user", "content": "Hello from session 2"},
        {"role": "assistant", "content": "Hi from session 2"},
    ]
    sessions.save_session_messages(sf2, messages2)
    sessions.save_session_meta(
        mf2,
        {
            "created_at": "2025-01-02T10:00:00",
            "updated_at": "2025-01-02T10:05:00",
            "pinned": False,
            "tags": ["tag2", "tag3"],
        },
    )

    # Merge sessions
    ok, msg = sessions.merge_sessions(["session1", "session2"], "merged", sessions_dir)
    assert ok, f"Merge should succeed: {msg}"
    assert "2 sessions" in msg
    assert "4 messages" in msg

    # Verify merged session exists
    merged_file = sessions.session_file_for("merged", sessions_dir)
    merged_meta_file = sessions.meta_file_for(merged_file)
    assert merged_file.exists()
    assert merged_meta_file.exists()

    # Verify merged messages
    merged_messages = sessions.load_session_messages(merged_file)
    assert len(merged_messages) == 4
    assert merged_messages[0]["content"] == "Hello from session 1"
    assert merged_messages[1]["content"] == "Hi from session 1"
    assert merged_messages[2]["content"] == "Hello from session 2"
    assert merged_messages[3]["content"] == "Hi from session 2"

    # Verify all messages have timestamps and IDs
    for msg in merged_messages:
        assert "timestamp" in msg
        assert "id" in msg

    # Verify merged metadata
    merged_meta = sessions.load_session_meta(merged_meta_file)
    assert merged_meta["created_at"] == "2025-01-01T10:00:00"  # Earliest
    assert merged_meta["pinned"] is False
    assert set(merged_meta["tags"]) == {"tag1", "tag2", "tag3"}
    assert merged_meta["title"] == "First Session"  # From first session
    assert "token_estimate" in merged_meta


@pytest.mark.unit
def test_merge_sessions_output_exists(tmp_path: Path) -> None:
    """Test that merge fails if output session already exists."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create sessions
    sf1 = sessions.session_file_for("session1", sessions_dir)
    sessions.save_session_messages(sf1, [{"role": "user", "content": "Test"}])

    sf_output = sessions.session_file_for("output", sessions_dir)
    sessions.save_session_messages(sf_output, [{"role": "user", "content": "Existing"}])

    # Try to merge - should fail because output exists
    ok, msg = sessions.merge_sessions(["session1"], "output", sessions_dir)
    assert not ok
    assert "already exists" in msg


@pytest.mark.unit
def test_merge_sessions_input_not_found(tmp_path: Path) -> None:
    """Test that merge fails if input session doesn't exist."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.merge_sessions(["nonexistent"], "output", sessions_dir)
    assert not ok
    assert "not found" in msg


@pytest.mark.unit
def test_merge_sessions_no_inputs(tmp_path: Path) -> None:
    """Test that merge fails if no input sessions provided."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.merge_sessions([], "output", sessions_dir)
    assert not ok
    assert "No sessions provided" in msg


@pytest.mark.unit
def test_merge_sessions_empty_sessions(tmp_path: Path) -> None:
    """Test that merge handles empty sessions gracefully."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create two empty sessions
    sf1 = sessions.session_file_for("empty1", sessions_dir)
    mf1 = sessions.meta_file_for(sf1)
    sessions.save_session_messages(sf1, [])
    sessions.save_session_meta(
        mf1,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    sf2 = sessions.session_file_for("empty2", sessions_dir)
    mf2 = sessions.meta_file_for(sf2)
    sessions.save_session_messages(sf2, [])
    sessions.save_session_meta(
        mf2,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
        },
    )

    # Merge empty sessions
    ok, msg = sessions.merge_sessions(["empty1", "empty2"], "merged_empty", sessions_dir)
    assert ok
    assert "0 messages" in msg

    # Verify merged session exists but is empty
    merged_file = sessions.session_file_for("merged_empty", sessions_dir)
    merged_messages = sessions.load_session_messages(merged_file)
    assert len(merged_messages) == 0


@pytest.mark.unit
def test_merge_sessions_single_session(tmp_path: Path) -> None:
    """Test merging a single session (effectively creates a copy)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create one session
    sf = sessions.session_file_for("original", sessions_dir)
    mf = sessions.meta_file_for(sf)
    messages = [
        {"role": "user", "content": "Test message"},
        {"role": "assistant", "content": "Test response"},
    ]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(
        mf,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": ["test"],
            "title": "Original Session",
        },
    )

    # Merge single session
    ok, msg = sessions.merge_sessions(["original"], "copy", sessions_dir)
    assert ok
    assert "1 session" in msg  # Singular
    assert "2 messages" in msg

    # Verify copy exists and matches original
    copy_file = sessions.session_file_for("copy", sessions_dir)
    copy_messages = sessions.load_session_messages(copy_file)
    assert len(copy_messages) == 2
    assert copy_messages[0]["content"] == "Test message"
    assert copy_messages[1]["content"] == "Test response"


def test_batch_delete_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create test sessions
    sessions.init_session("session1", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("session2", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("session3", sessions_dir, new_session=True, model="llama3.1:8b")

    # Batch delete two sessions
    success, failure, failed = sessions.batch_delete_sessions(
        ["session1", "session2"], sessions_dir
    )
    assert success == 2
    assert failure == 0
    assert failed == []

    # Verify deletion
    assert not sessions.session_file_for("session1", sessions_dir).exists()
    assert not sessions.session_file_for("session2", sessions_dir).exists()
    assert sessions.session_file_for("session3", sessions_dir).exists()

    # Delete non-existent session
    success, failure, failed = sessions.batch_delete_sessions(["nonexistent"], sessions_dir)
    assert success == 0
    assert failure == 1
    assert failed == ["nonexistent"]


def test_batch_tag_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create test sessions
    sessions.init_session("session1", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("session2", sessions_dir, new_session=True, model="llama3.1:8b")

    # Batch add tags
    success, failure, failed = sessions.batch_tag_sessions(
        ["session1", "session2"], ["tag1", "tag2"], sessions_dir, remove=False
    )
    assert success == 2
    assert failure == 0
    assert failed == []

    # Verify tags added
    meta1 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session1", sessions_dir))
    )
    meta2 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session2", sessions_dir))
    )
    assert set(meta1.get("tags", [])) == {"tag1", "tag2"}
    assert set(meta2.get("tags", [])) == {"tag1", "tag2"}

    # Batch remove tags
    success, failure, failed = sessions.batch_tag_sessions(
        ["session1", "session2"], ["tag1"], sessions_dir, remove=True
    )
    assert success == 2
    assert failure == 0

    # Verify tags removed
    meta1 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session1", sessions_dir))
    )
    meta2 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session2", sessions_dir))
    )
    assert meta1.get("tags", []) == ["tag2"]
    assert meta2.get("tags", []) == ["tag2"]


def test_batch_export_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "exports"

    # Create test sessions with messages
    sf1, mf1, msgs1, meta1 = sessions.init_session(
        "session1", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs1.append({"role": "user", "content": "Test message 1"})
    sessions.save_session_messages(sf1, msgs1)

    sf2, mf2, msgs2, meta2 = sessions.init_session(
        "session2", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs2.append({"role": "user", "content": "Test message 2"})
    sessions.save_session_messages(sf2, msgs2)

    # Batch export as markdown
    success, failure, failed = sessions.batch_export_sessions(
        ["session1", "session2"], output_dir, sessions_dir, format="markdown"
    )
    assert success == 2
    assert failure == 0
    assert failed == []

    # Verify export files exist
    assert (output_dir / "session1.md").exists()
    assert (output_dir / "session2.md").exists()

    # Verify content
    content1 = (output_dir / "session1.md").read_text()
    assert "Test message 1" in content1

    # Test JSON export
    output_dir_json = tmp_path / "exports_json"
    success, failure, failed = sessions.batch_export_sessions(
        ["session1"], output_dir_json, sessions_dir, format="json"
    )
    assert success == 1
    assert (output_dir_json / "session1.json").exists()


def test_batch_update_metadata(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create test sessions
    sessions.init_session("session1", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("session2", sessions_dir, new_session=True, model="llama3.1:8b")

    # Batch update pinned status
    success, failure, failed = sessions.batch_update_metadata(
        ["session1", "session2"], sessions_dir, pinned=True
    )
    assert success == 2
    assert failure == 0
    assert failed == []

    # Verify pinned status
    meta1 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session1", sessions_dir))
    )
    meta2 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session2", sessions_dir))
    )
    assert meta1.get("pinned") is True
    assert meta2.get("pinned") is True

    # Batch update model
    success, failure, failed = sessions.batch_update_metadata(
        ["session1", "session2"], sessions_dir, model="mistral:7b"
    )
    assert success == 2

    # Verify model updated
    meta1 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session1", sessions_dir))
    )
    meta2 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session2", sessions_dir))
    )
    assert meta1.get("model") == "mistral:7b"
    assert meta2.get("model") == "mistral:7b"

    # Batch update RAG enabled
    success, failure, failed = sessions.batch_update_metadata(
        ["session1"], sessions_dir, rag_enabled=True
    )
    assert success == 1

    # Verify RAG enabled updated
    meta1 = sessions.load_session_meta(
        sessions.meta_file_for(sessions.session_file_for("session1", sessions_dir))
    )
    assert meta1.get("rag_enabled") is True


# --- Additional tests for improved coverage ---


def test_load_session_messages_paginated_basic(tmp_path: Path) -> None:
    """Test paginated message loading with offset and limit."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session with 10 messages
    sf = sessions.session_file_for("paginated-test", sessions_dir)
    messages = [{"role": "user", "content": f"Message {i}"} for i in range(10)]
    sessions.save_session_messages(sf, messages)

    # Load first 3 messages
    msgs, total = sessions.load_session_messages_paginated(sf, offset=0, limit=3)
    assert total == 10
    assert len(msgs) == 3
    assert msgs[0]["content"] == "Message 0"
    assert msgs[2]["content"] == "Message 2"

    # Load messages 5-7
    msgs, total = sessions.load_session_messages_paginated(sf, offset=5, limit=3)
    assert total == 10
    assert len(msgs) == 3
    assert msgs[0]["content"] == "Message 5"

    # Load all remaining messages from offset 8
    msgs, total = sessions.load_session_messages_paginated(sf, offset=8, limit=None)
    assert total == 10
    assert len(msgs) == 2
    assert msgs[0]["content"] == "Message 8"
    assert msgs[1]["content"] == "Message 9"


def test_load_session_messages_paginated_nonexistent(tmp_path: Path) -> None:
    """Test paginated loading of nonexistent session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("nonexistent", sessions_dir)
    msgs, total = sessions.load_session_messages_paginated(sf)
    assert msgs == []
    assert total == 0


def test_load_session_messages_paginated_corrupted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test paginated loading handles corrupted JSON."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("corrupted", sessions_dir)
    sf.write_text("{invalid json")

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        msgs, total = sessions.load_session_messages_paginated(sf)

    assert msgs == []
    assert total == 0
    assert "Invalid JSON" in caplog.text


def test_rename_session_basic(tmp_path: Path) -> None:
    """Test basic session rename functionality."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a session
    sf, mf, msgs, meta = sessions.init_session(
        "old-name", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "Test message"})
    sessions.save_session_messages(sf, msgs)
    meta["title"] = "Test Session"
    sessions.save_session_meta(mf, meta)

    # Rename session
    ok, msg = sessions.rename_session("old-name", "new-name", sessions_dir)
    assert ok
    assert msg == "OK"

    # Verify old files are gone
    assert not sf.exists()
    assert not mf.exists()

    # Verify new files exist
    new_sf = sessions.session_file_for("new-name", sessions_dir)
    new_mf = sessions.meta_file_for(new_sf)
    assert new_sf.exists()
    assert new_mf.exists()

    # Verify content preserved
    new_msgs = sessions.load_session_messages(new_sf)
    assert len(new_msgs) == 1
    assert new_msgs[0]["content"] == "Test message"

    new_meta = sessions.load_session_meta(new_mf)
    assert new_meta["title"] == "Test Session"


def test_rename_session_nonexistent(tmp_path: Path) -> None:
    """Test renaming nonexistent session fails."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.rename_session("nonexistent", "new-name", sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_rename_session_target_exists(tmp_path: Path) -> None:
    """Test renaming to existing session name fails."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create two sessions
    sessions.init_session("session1", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("session2", sessions_dir, new_session=True, model="llama3.1:8b")

    # Try to rename session1 to session2
    ok, msg = sessions.rename_session("session1", "session2", sessions_dir)
    assert not ok
    assert "already exists" in msg


def test_set_pinned_basic(tmp_path: Path) -> None:
    """Test setting pinned status."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session
    sf, mf, msgs, meta = sessions.init_session(
        "test-pinned", sessions_dir, new_session=True, model="llama3.1:8b"
    )

    # Initially not pinned
    assert meta.get("pinned") is False

    # Pin session
    ok, msg = sessions.set_pinned("test-pinned", True, sessions_dir)
    assert ok
    assert msg == "OK"

    # Verify pinned
    meta = sessions.load_session_meta(mf)
    assert meta["pinned"] is True

    # Unpin session
    ok, msg = sessions.set_pinned("test-pinned", False, sessions_dir)
    assert ok

    meta = sessions.load_session_meta(mf)
    assert meta["pinned"] is False


def test_set_pinned_nonexistent(tmp_path: Path) -> None:
    """Test setting pinned on nonexistent session fails."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.set_pinned("nonexistent", True, sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_set_title_basic(tmp_path: Path) -> None:
    """Test setting session title."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session
    sf, mf, msgs, meta = sessions.init_session(
        "test-title", sessions_dir, new_session=True, model="llama3.1:8b"
    )

    # Set title
    ok, msg = sessions.set_title("test-title", "My New Title", sessions_dir)
    assert ok
    assert msg == "OK"

    # Verify title
    meta = sessions.load_session_meta(mf)
    assert meta["title"] == "My New Title"


def test_set_title_nonexistent(tmp_path: Path) -> None:
    """Test setting title on nonexistent session fails."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.set_title("nonexistent", "Title", sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_normalize_tags(tmp_path: Path) -> None:
    """Test tag normalization functionality."""
    # Test deduplication (case-insensitive)
    tags = sessions.normalize_tags(["Python", "python", "PYTHON", "Java"])
    assert tags == ["Java", "Python"]  # Sorted, deduplicated

    # Test whitespace handling
    tags = sessions.normalize_tags(["  tag1  ", "tag2", "", "  ", "tag3"])
    assert tags == ["tag1", "tag2", "tag3"]

    # Test empty input
    tags = sessions.normalize_tags([])
    assert tags == []

    # Test sorting
    tags = sessions.normalize_tags(["zebra", "apple", "banana"])
    assert tags == ["apple", "banana", "zebra"]


def test_token_estimate_from_messages(tmp_path: Path) -> None:
    """Test token estimation from messages."""
    # Empty messages
    estimate = sessions.token_estimate_from_messages([])
    assert estimate == 0

    # Single message
    messages = [{"role": "user", "content": "Hello"}]
    estimate = sessions.token_estimate_from_messages(messages)
    assert estimate > 0

    # Multiple messages
    messages = [
        {"role": "user", "content": "A" * 100},
        {"role": "assistant", "content": "B" * 100},
    ]
    estimate = sessions.token_estimate_from_messages(messages)
    assert estimate >= 50  # Rough estimate: 200 chars / 4 = 50 tokens


def test_sanitize_title_for_filename(tmp_path: Path) -> None:
    """Test title sanitization for filenames."""
    # Basic case
    assert sessions.sanitize_title_for_filename("My Chat Session") == "my-chat-session"

    # Special characters
    assert sessions.sanitize_title_for_filename("Python: Tips & Tricks!") == "python-tips-tricks"

    # Multiple spaces and hyphens
    assert sessions.sanitize_title_for_filename("Test   -  Session") == "test-session"

    # Truncation (> 64 chars)
    long_title = "A" * 100
    sanitized = sessions.sanitize_title_for_filename(long_title)
    assert len(sanitized) == 64

    # Empty after sanitization
    assert sessions.sanitize_title_for_filename("!!!") == "session"


def test_sync_filename_with_title_no_title(tmp_path: Path) -> None:
    """Test sync when session has no title."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session without title
    sf, mf, msgs, meta = sessions.init_session(
        "test-session", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    # Don't set a title

    # Sync should report no title
    success, message, new_name = sessions.sync_filename_with_title(
        "test-session", sessions_dir, force=True
    )
    assert success
    assert message == "no_title"
    assert new_name == "test-session"


def test_sync_filename_with_title_collision(tmp_path: Path) -> None:
    """Test sync when target filename already exists."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create first session with title
    sf1, mf1, msgs1, meta1 = sessions.init_session(
        "session-1", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta1["title"] = "Collision Test"
    sessions.save_session_meta(mf1, meta1)

    # Create second session with same title
    sf2, mf2, msgs2, meta2 = sessions.init_session(
        "session-2", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta2["title"] = "Collision Test"
    sessions.save_session_meta(mf2, meta2)

    # Rename first session
    success, message, new_name = sessions.sync_filename_with_title(
        "session-1", sessions_dir, force=True
    )
    assert success
    assert message == "renamed"
    assert new_name == "collision-test"

    # Rename second session (should get collision-test-1)
    success, message, new_name = sessions.sync_filename_with_title(
        "session-2", sessions_dir, force=True
    )
    assert success
    assert message == "renamed"
    assert new_name == "collision-test-1"


def test_search_messages_invalid_role_filter(tmp_path: Path) -> None:
    """Test search with invalid role filter raises ValueError."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    with pytest.raises(ValueError, match="Invalid role_filter"):
        sessions.search_messages("query", sessions_dir, role_filter="invalid_role")


def test_delete_session_basic(tmp_path: Path) -> None:
    """Test deleting a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session
    sf, mf, msgs, meta = sessions.init_session(
        "delete-me", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    assert sf.exists()
    assert mf.exists()

    # Delete session
    ok = sessions.delete_session("delete-me", sessions_dir)
    assert ok
    assert not sf.exists()
    assert not mf.exists()


def test_delete_session_nonexistent(tmp_path: Path) -> None:
    """Test deleting nonexistent session returns False."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok = sessions.delete_session("nonexistent", sessions_dir)
    assert not ok


def test_load_session_meta_corrupted_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test loading corrupted metadata JSON returns empty dict."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    mf = sessions_dir / "test.meta.json"
    mf.write_text("{invalid json")

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        meta = sessions.load_session_meta(mf)

    assert meta == {}
    assert "Invalid JSON in metadata file" in caplog.text


def test_load_session_meta_io_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test loading metadata with IO error returns empty dict."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    mf = sessions_dir / "test.meta.json"
    mf.write_text('{"title": "test"}')

    # Monkey-patch Path.read_text to raise IOError
    def raise_io_error(*args, **kwargs):
        raise OSError("Simulated IO error")

    monkeypatch.setattr("pathlib.Path.read_text", raise_io_error)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        meta = sessions.load_session_meta(mf)

    assert meta == {}
    assert "Failed to read metadata file" in caplog.text


def test_load_session_messages_io_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test loading session messages with IO error returns empty list."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("test", sessions_dir)
    sf.write_text('[{"role": "user", "content": "test"}]')

    # Monkey-patch Path.read_text to raise IOError
    def raise_io_error(*args, **kwargs):
        raise OSError("Simulated IO error")

    monkeypatch.setattr("pathlib.Path.read_text", raise_io_error)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        messages = sessions.load_session_messages(sf)

    assert messages == []
    assert "Failed to read session file" in caplog.text


def test_load_session_messages_invalid_data_type(tmp_path: Path) -> None:
    """Test loading session with non-list data returns empty list."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("invalid-type", sessions_dir)
    sf.write_text('{"not": "a list"}')  # Valid JSON, but not a list

    messages = sessions.load_session_messages(sf)
    assert messages == []


def test_apply_system_prompt_existing_system(tmp_path: Path) -> None:
    """Test applying system prompt when one already exists."""
    messages = [
        {"role": "system", "content": "Old system prompt"},
        {"role": "user", "content": "Hello"},
    ]

    result = sessions.apply_system_prompt(messages, "New system prompt")
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "New system prompt"


def test_apply_system_prompt_no_existing_system(tmp_path: Path) -> None:
    """Test applying system prompt when none exists."""
    messages = [
        {"role": "user", "content": "Hello"},
    ]

    result = sessions.apply_system_prompt(messages, "System prompt")
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "System prompt"


def test_apply_system_prompt_none(tmp_path: Path) -> None:
    """Test applying None system prompt leaves messages unchanged."""
    messages = [{"role": "user", "content": "Hello"}]
    result = sessions.apply_system_prompt(messages, None)
    assert result == messages


def test_ensure_meta_defaults_missing_fields(tmp_path: Path) -> None:
    """Test ensure_meta_defaults fills in missing fields."""
    meta = {}
    result = sessions.ensure_meta_defaults(meta, model="llama3.1:8b")

    assert "created_at" in result
    assert "updated_at" in result
    assert result["pinned"] is False
    assert result["tags"] == []
    assert result["model"] == "llama3.1:8b"
    assert "rag_enabled" in result


def test_ensure_meta_defaults_invalid_types(tmp_path: Path) -> None:
    """Test ensure_meta_defaults fixes invalid field types."""
    meta = {
        "created_at": 12345,  # Should be string
        "pinned": "yes",  # Should be bool
        "tags": "not a list",  # Should be list
    }

    result = sessions.ensure_meta_defaults(meta)
    assert isinstance(result["created_at"], str)
    assert result["pinned"] is False
    assert result["tags"] == []


def test_meta_file_for(tmp_path: Path) -> None:
    """Test meta_file_for generates correct metadata path."""
    sessions_dir = tmp_path / "sessions"
    sf = sessions.session_file_for("test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    assert mf.name == "test.meta.json"
    assert mf.parent == sf.parent


def test_iso_now(tmp_path: Path) -> None:
    """Test iso_now returns valid ISO 8601 timestamp."""
    timestamp = sessions.iso_now()
    assert isinstance(timestamp, str)
    assert "T" in timestamp
    # Verify it's parseable as ISO 8601
    from datetime import datetime

    parsed = datetime.fromisoformat(timestamp)
    assert parsed is not None


def test_default_sessions_dir(tmp_path: Path) -> None:
    """Test default_sessions_dir returns valid path."""
    result = sessions.default_sessions_dir()
    assert isinstance(result, Path)


def test_batch_export_invalid_format(tmp_path: Path) -> None:
    """Test batch export with invalid format raises ValueError."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    output_dir = tmp_path / "exports"

    with pytest.raises(ValueError, match="Invalid format"):
        sessions.batch_export_sessions(["session1"], output_dir, sessions_dir, format="invalid")


def test_list_sessions_sorting(tmp_path: Path) -> None:
    """Test list_sessions sorts pinned sessions first."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create sessions
    sf1, mf1, msgs1, meta1 = sessions.init_session(
        "unpinned", sessions_dir, new_session=True, model="llama3.1:8b"
    )

    sf2, mf2, msgs2, meta2 = sessions.init_session(
        "pinned", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta2["pinned"] = True
    sessions.save_session_meta(mf2, meta2)

    # List sessions
    result = sessions.list_sessions(sessions_dir)
    assert len(result) == 2

    # Pinned session should come first
    assert result[0]["name"] == "pinned"
    assert result[1]["name"] == "unpinned"


def test_list_sessions_with_path_object(tmp_path: Path) -> None:
    """Test list_sessions accepts Path object directly."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sessions.init_session("test", sessions_dir, new_session=True, model="llama3.1:8b")

    # Pass Path object instead of config
    result = sessions.list_sessions(sessions_dir)
    assert len(result) == 1
    assert result[0]["name"] == "test"


def test_save_session_messages_atomic(tmp_path: Path) -> None:
    """Test save_session_messages uses atomic temp file."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("atomic-test", sessions_dir)
    messages = [{"role": "user", "content": "Test"}]

    # Save messages
    sessions.save_session_messages(sf, messages)

    # Verify file exists and no temp files remain
    assert sf.exists()
    temp_files = list(sessions_dir.glob(".*.tmp"))
    assert len(temp_files) == 0

    # Verify content
    loaded = sessions.load_session_messages(sf)
    assert len(loaded) == 1
    assert loaded[0]["content"] == "Test"


def test_save_session_meta_atomic(tmp_path: Path) -> None:
    """Test save_session_meta uses atomic temp file."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    mf = sessions_dir / "test.meta.json"
    meta = {"title": "Test", "pinned": False, "tags": []}

    # Save metadata
    sessions.save_session_meta(mf, meta)

    # Verify file exists and no temp files remain
    assert mf.exists()
    temp_files = list(sessions_dir.glob(".*.tmp"))
    assert len(temp_files) == 0

    # Verify content
    loaded = sessions.load_session_meta(mf)
    assert loaded["title"] == "Test"


def test_add_tags_preserves_existing(tmp_path: Path) -> None:
    """Test add_tags preserves existing tags."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session with existing tags
    sf, mf, msgs, meta = sessions.init_session(
        "tag-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["tags"] = ["existing1", "existing2"]
    sessions.save_session_meta(mf, meta)

    # Add new tags
    ok, msg = sessions.add_tags("tag-test", ["new1", "new2"], sessions_dir)
    assert ok

    # Verify all tags present
    meta = sessions.load_session_meta(mf)
    assert set(meta["tags"]) == {"existing1", "existing2", "new1", "new2"}


def test_remove_tags_case_insensitive(tmp_path: Path) -> None:
    """Test remove_tags is case-insensitive."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session with tags
    sf, mf, msgs, meta = sessions.init_session(
        "remove-tags-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["tags"] = ["Python", "Java", "Rust"]
    sessions.save_session_meta(mf, meta)

    # Remove tag with different case
    ok, msg = sessions.remove_tags("remove-tags-test", ["python"], sessions_dir)
    assert ok

    # Verify tag removed
    meta = sessions.load_session_meta(mf)
    assert "Python" not in meta["tags"]
    assert set(meta["tags"]) == {"Java", "Rust"}


def test_export_markdown_includes_rag_citations(tmp_path: Path) -> None:
    """Test that markdown export includes RAG citations."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("citations-md-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    # Create messages with RAG citations
    messages = [
        {"role": "user", "content": "What is RAG?"},
        {
            "role": "assistant",
            "content": "RAG is Retrieval-Augmented Generation.",
            "rag_chunks": [
                {
                    "text": "RAG enhances LLM responses with retrieved context from documents.",
                    "doc_id": "rag-guide.md",
                    "chunk_id": 1,
                    "score": 0.95,
                    "similarity_score": 0.95,
                },
                {
                    "text": "Vector search finds relevant chunks using embeddings.",
                    "doc_id": "vector-search.md",
                    "chunk_id": 3,
                    "score": 0.88,
                    "similarity_score": 0.88,
                },
            ],
        },
    ]

    metadata = {
        "title": "RAG Citations Test",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:05:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to markdown
    ok, content = sessions.export_session_markdown("citations-md-test", sessions_dir)
    assert ok

    # Verify citations are included. chunk_id is the zero-based internal key;
    # citations display the human-readable 1-based position instead.
    assert "### RAG Sources" in content
    assert "**[1] rag-guide.md**" in content
    assert "(chunk 2)" in content
    assert "Confidence: 0.950" in content
    assert "RAG enhances LLM responses" in content
    assert "**[2] vector-search.md**" in content
    assert "(chunk 4)" in content
    assert "Confidence: 0.880" in content


def test_export_session_html_with_citations(tmp_path: Path) -> None:
    """Test that HTML export includes RAG citations with proper styling."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("citations-html-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    # Create messages with RAG citations
    messages = [
        {"role": "user", "content": "Question"},
        {
            "role": "assistant",
            "content": "Answer",
            "rag_chunks": [
                {
                    "text": "Citation content here",
                    "doc_id": "source.md",
                    "chunk_id": 2,
                    "score": 0.92,
                    "similarity_score": 0.92,
                }
            ],
        },
    ]

    metadata = {
        "title": "HTML Citations Test",
        "created_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to HTML
    ok, content = sessions.export_session_html("citations-html-test", sessions_dir)
    assert ok

    # Verify citations are included with proper structure
    assert '<div class="citations">' in content
    assert '<div class="citation-header">RAG Sources</div>' in content
    assert '<div class="citation-item">' in content
    # chunk_id=2 is the zero-based internal key; displayed as 1-based "chunk 3".
    assert '<div class="citation-title">[1] source.md (chunk 3)</div>' in content
    assert '<div class="citation-score">Confidence: 0.920</div>' in content
    assert '<div class="citation-text">Citation content here</div>' in content

    # Verify CSS classes are defined
    assert ".citations" in content
    assert ".citation-header" in content
    assert ".citation-item" in content


def test_format_chunk_ref_prefers_chunk_number_and_total() -> None:
    assert sessions.format_chunk_ref({"chunk_number": 2, "total_chunks": 5}) == "chunk 2 of 5"


def test_format_chunk_ref_chunk_number_without_total() -> None:
    assert sessions.format_chunk_ref({"chunk_number": 1, "total_chunks": None}) == "chunk 1"


def test_format_chunk_ref_falls_back_to_chunk_id_plus_one() -> None:
    """Citations persisted before chunk_number/total_chunks were tracked
    fall back to the zero-based chunk_id + 1 -- this is what turns
    "(chunk 0)" into the correct "(chunk 1)"."""
    assert sessions.format_chunk_ref({"chunk_id": 0}) == "chunk 1"
    assert sessions.format_chunk_ref({"chunk_id": 3}) == "chunk 4"


def test_format_chunk_ref_no_chunk_info_falls_back_to_source() -> None:
    assert sessions.format_chunk_ref({}) == "source"


def test_export_markdown_no_citations(tmp_path: Path) -> None:
    """Test that markdown export works correctly when there are no citations."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("no-citations-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    # Create messages WITHOUT RAG citations
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    metadata = {
        "title": "No Citations Test",
        "created_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to markdown
    ok, content = sessions.export_session_markdown("no-citations-test", sessions_dir)
    assert ok

    # Verify no citation sections appear
    assert "### RAG Sources" not in content
    assert "Confidence:" not in content


def test_export_citations_text_truncation(tmp_path: Path) -> None:
    """Test that long citation text is properly truncated in exports."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("long-cite-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    # Create citation with very long text
    long_text = "A" * 500  # 500 characters, should be truncated to 200 + "..."

    messages = [
        {"role": "user", "content": "Question"},
        {
            "role": "assistant",
            "content": "Answer",
            "rag_chunks": [
                {
                    "text": long_text,
                    "doc_id": "long-doc.md",
                    "chunk_id": 1,
                    "score": 0.90,
                }
            ],
        },
    ]

    metadata = {"title": "Long Citation Test"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Export to markdown
    ok, content = sessions.export_session_markdown("long-cite-test", sessions_dir)
    assert ok

    # Verify text is truncated
    assert "A" * 200 in content
    assert "..." in content
    # Full 500 characters should not appear
    assert "A" * 500 not in content


# --- file_lock: win32 branch coverage ---
#
# Tests run on Linux, so sys.platform is monkeypatched to "win32" and a fake
# msvcrt module is injected into sys.modules (the real module doesn't exist on
# Linux and file_lock does an inline `import msvcrt`).


def test_file_lock_win32_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """file_lock should use msvcrt.locking on win32 and unlock on the way out."""
    test_file = tmp_path / "win32.lock"

    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_NBLCK = 1  # type: ignore[attr-defined]
    fake_msvcrt.LK_UNLCK = 2  # type: ignore[attr-defined]
    fake_msvcrt.locking = MagicMock()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")

    with sessions.file_lock(test_file, timeout=1.0) as fd:
        assert fd >= 0

    # Called once to acquire (LK_NBLCK) and once to release (LK_UNLCK)
    assert fake_msvcrt.locking.call_count == 2
    lock_call, unlock_call = fake_msvcrt.locking.call_args_list
    assert lock_call.args[1] == fake_msvcrt.LK_NBLCK
    assert unlock_call.args[1] == fake_msvcrt.LK_UNLCK


def test_file_lock_win32_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """file_lock should raise TimeoutError on win32 if locking() keeps failing."""
    test_file = tmp_path / "win32-timeout.lock"

    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_NBLCK = 1  # type: ignore[attr-defined]
    fake_msvcrt.LK_UNLCK = 2  # type: ignore[attr-defined]
    fake_msvcrt.locking = MagicMock(side_effect=OSError("already locked"))  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")

    with (
        pytest.raises(TimeoutError, match="Could not acquire lock"),
        sessions.file_lock(test_file, timeout=0.2),
    ):
        pass  # pragma: no cover - should never be entered


def test_file_lock_unlock_exception_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If unlocking raises, file_lock should log a warning instead of crashing."""
    from unittest.mock import patch

    test_file = tmp_path / "unlock-fail.lock"

    with (
        caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"),
        # First call (acquire) succeeds, second call (release) raises.
        patch("fcntl.flock", side_effect=[None, OSError("boom")]),
        sessions.file_lock(test_file, timeout=1.0) as fd,
    ):
        assert fd >= 0

    assert "Failed to unlock" in caplog.text


# --- load_session_messages_paginated: remaining branches ---


def test_load_session_messages_paginated_io_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paginated loading should handle OSError while reading gracefully."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("io-error-test", sessions_dir)
    sf.write_text('[{"role": "user", "content": "test"}]')

    def raise_io_error(*args, **kwargs):
        raise OSError("Simulated IO error")

    monkeypatch.setattr("pathlib.Path.read_text", raise_io_error)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        msgs, total = sessions.load_session_messages_paginated(sf)

    assert msgs == []
    assert total == 0
    assert "Failed to read session file" in caplog.text


def test_load_session_messages_paginated_non_list(tmp_path: Path) -> None:
    """Paginated loading should return empty when JSON top-level isn't a list."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf = sessions.session_file_for("non-list", sessions_dir)
    sf.write_text('{"not": "a list"}')

    msgs, total = sessions.load_session_messages_paginated(sf)
    assert msgs == []
    assert total == 0


# --- ensure_meta_defaults: rag_enabled exception fallback ---


def test_ensure_meta_defaults_rag_enabled_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If get_rag_enabled raises, ensure_meta_defaults should fall back to False."""

    def raise_error(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(sessions, "get_rag_enabled", raise_error)

    meta: dict = {}
    result = sessions.ensure_meta_defaults(meta)
    assert result["rag_enabled"] is False


# --- init_session: new_session=True removes pre-existing meta file ---


def test_init_session_new_session_removes_existing_files(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"

    sf, mf, msgs, meta = sessions.init_session(
        "reset-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hello"})
    sessions.save_session_messages(sf, msgs)
    meta["title"] = "Should be gone"
    sessions.save_session_meta(mf, meta)

    assert sf.exists()
    assert mf.exists()

    # Re-initializing with new_session=True should unlink both existing files
    # (session + meta) before recreating them fresh.
    sf2, mf2, msgs2, meta2 = sessions.init_session(
        "reset-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    assert msgs2 == []
    assert meta2.get("title") is None


# --- persist_after_exchange: config loading / error-handling / auto-summarize ---


def test_persist_after_exchange_loads_config_when_none(tmp_path: Path) -> None:
    """persist_after_exchange should load the global config when cfg is None."""
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "persist-no-cfg", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hi"})

    name = sessions.persist_after_exchange(sf, mf, msgs, model="llama3.1:8b")
    assert name == "persist-no-cfg"


class _RaisingCfg:
    """A stub cfg object whose getboolean/getint always raise."""

    def getboolean(self, *args, **kwargs):
        raise RuntimeError("boom")

    def getint(self, *args, **kwargs):
        raise RuntimeError("boom")


def test_persist_after_exchange_config_error_falls_back(tmp_path: Path) -> None:
    """If cfg.getboolean/getint raise, auto-summarize should default to disabled."""
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "persist-bad-cfg", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hi"})

    name = sessions.persist_after_exchange(sf, mf, msgs, model="llama3.1:8b", cfg=_RaisingCfg())
    assert name == "persist-bad-cfg"


def test_persist_after_exchange_auto_summarize_does_not_rename_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #3459: auto-summarize must set title/summary/tags
    but must NOT rename the session file, even with auto_sync_filename=true.

    Callers (web UI, CLI REPL) keep addressing the session by its original
    name for the rest of the conversation; renaming it here would orphan
    that name and cause the next turn to silently start a brand-new,
    empty session instead of appending.
    """
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "auto-sum-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "Hello"})
    msgs.append({"role": "assistant", "content": "Hi there"})

    fake_response = json.dumps({"title": "Auto Title", "summary": "A summary", "tags": ["a", "b"]})
    monkeypatch.setattr(sessions, "ollama_chat", lambda **kwargs: fake_response)

    # Even if the global config (read internally by sync_filename_with_title)
    # reports auto_sync_filename=true, persist_after_exchange must not call
    # into it as a side effect of a chat exchange.
    sync_cfg = configparser.ConfigParser()
    sync_cfg["nyxgpt"] = {"auto_sync_filename": "true"}
    monkeypatch.setattr(sessions, "load_config", lambda path=None: sync_cfg)

    cfg = _cfg_auto_summarize(sessions_dir)
    new_name = sessions.persist_after_exchange(sf, mf, msgs, model="llama3.1:8b", cfg=cfg)

    assert new_name == "auto-sum-test"
    assert sf.exists()

    # Title/summary/tags were still generated -- only the rename is gone.
    new_meta = sessions.load_session_meta(mf)
    assert new_meta.get("title") == "Auto Title"
    assert new_meta.get("tags") == ["a", "b"]

    # No stray "auto-title.json" was created either.
    renamed_sf = sessions.session_file_for("auto-title", sessions_dir)
    assert not renamed_sf.exists()


def test_persist_after_exchange_auto_summarize_failure_logs_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If auto-summarization fails, a warning should be logged and name unchanged."""
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "auto-sum-fail", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "Hello"})
    msgs.append({"role": "assistant", "content": "Hi there"})

    def raise_err(**kwargs):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(sessions, "ollama_chat", raise_err)

    cfg = _cfg_auto_summarize(sessions_dir)
    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        new_name = sessions.persist_after_exchange(sf, mf, msgs, model="llama3.1:8b", cfg=cfg)

    assert new_name == "auto-sum-fail"
    assert "Auto-summarization failed" in caplog.text


def test_save_session_keeps_name_stable_after_auto_summarize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #3459: save_session must keep the SessionState's
    name/session_file stable across auto-summarization, even with
    auto_sync_filename=true -- a caller (e.g. the web UI, which keeps
    posting to the session name it started with) must be able to keep
    addressing the same session name for every subsequent turn."""
    sessions_dir = tmp_path / "sessions"
    cfg = _cfg_auto_summarize(sessions_dir, auto_sync=True)

    state = sessions.load_session("default", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "Hello"})
    state.messages.append({"role": "assistant", "content": "Hi"})

    fake_response = json.dumps({"title": "Renamed Title", "summary": "S", "tags": []})
    monkeypatch.setattr(sessions, "ollama_chat", lambda **kwargs: fake_response)

    sync_cfg = configparser.ConfigParser()
    sync_cfg["nyxgpt"] = {"auto_sync_filename": "true"}
    monkeypatch.setattr(sessions, "load_config", lambda path=None: sync_cfg)

    sessions.save_session(state, cfg)

    assert state.name == "default"
    assert state.session_file.name == "default.json"
    assert state.session_file.exists()

    renamed_sf = sessions.session_file_for("renamed-title", sessions_dir)
    assert not renamed_sf.exists()


# --- list_sessions: stat() exception fallback ---


def test_list_sessions_handles_stat_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sessions.init_session("stat-error", sessions_dir, new_session=True, model="llama3.1:8b")

    original_stat = Path.stat
    call_count = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        # Only fail on the *first* stat() call for this file (the explicit
        # `p.stat().st_mtime` in list_sessions). Later calls for the same path
        # (e.g. via Path.exists() while loading messages) should behave
        # normally, so we don't break unrelated code paths.
        if self.name == "stat-error.json":
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("stat failed")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = sessions.list_sessions(sessions_dir)
    assert len(result) == 1
    assert result[0]["modified"] == "?"


# --- add_tags / remove_tags: nonexistent session ---


def test_add_tags_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.add_tags("does-not-exist", ["tag"], sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_remove_tags_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.remove_tags("does-not-exist", ["tag"], sessions_dir)
    assert not ok
    assert msg == "No such session"


# --- summarize_session: full coverage of branches ---


def test_summarize_session_nonexistent(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.summarize_session("nonexistent", sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_summarize_session_no_messages(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sessions.init_session("empty-session", sessions_dir, new_session=True, model="llama3.1:8b")

    ok, msg = sessions.summarize_session("empty-session", sessions_dir)
    assert not ok
    assert msg == "Session has no messages"


def test_summarize_session_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "summarize-me", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "Tell me about cats"})
    msgs.append({"role": "assistant", "content": "Cats are great"})
    sessions.save_session_messages(sf, msgs)

    fake_response = json.dumps(
        {"title": "Cat Chat", "summary": "About cats", "tags": ["cats", "pets"]}
    )
    monkeypatch.setattr(sessions, "ollama_chat", lambda **kwargs: fake_response)

    ok, msg = sessions.summarize_session("summarize-me", sessions_dir)
    assert ok
    assert msg == "OK"

    result_meta = sessions.load_session_meta(mf)
    assert result_meta["title"] == "Cat Chat"
    assert result_meta["summary"] == "About cats"
    assert set(result_meta["tags"]) == {"cats", "pets"}


def test_summarize_session_ollama_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "summarize-fail", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hi"})
    sessions.save_session_messages(sf, msgs)

    def raise_err(**kwargs):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(sessions, "ollama_chat", raise_err)

    ok, msg = sessions.summarize_session("summarize-fail", sessions_dir)
    assert not ok
    assert msg.startswith("summarize failed:")


def test_summarize_session_invalid_json_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "summarize-badjson", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hi"})
    sessions.save_session_messages(sf, msgs)

    monkeypatch.setattr(sessions, "ollama_chat", lambda **kwargs: "not valid json")

    ok, msg = sessions.summarize_session("summarize-badjson", sessions_dir)
    assert not ok
    assert msg.startswith("summarize failed:")


def test_summarize_session_wrong_types_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If title/summary/tags have unexpected types, fall back to empty defaults."""
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "summarize-wrongtypes", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hi"})
    sessions.save_session_messages(sf, msgs)

    fake_response = json.dumps(
        {"title": 123, "summary": ["not", "a", "string"], "tags": "not-a-list"}
    )
    monkeypatch.setattr(sessions, "ollama_chat", lambda **kwargs: fake_response)

    ok, msg = sessions.summarize_session("summarize-wrongtypes", sessions_dir)
    assert ok
    assert msg == "OK"

    result_meta = sessions.load_session_meta(mf)
    assert "title" not in result_meta
    assert "summary" not in result_meta
    assert result_meta["tags"] == []


# --- export_session_markdown: system role and unrecognized role ---


def test_export_markdown_system_and_unknown_roles(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf = sessions.session_file_for("roles-test", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "system", "content": "System prompt content"},
        {"role": "tool", "content": "Tool output content"},
    ]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(mf, {"title": "Roles Test"})

    ok, content = sessions.export_session_markdown("roles-test", sessions_dir)
    assert ok
    assert "## System" in content
    assert "System prompt content" in content
    assert "## Tool" in content
    assert "Tool output content" in content


# --- export_session_html: score fallback when similarity_score is missing ---


def test_export_html_score_fallback_to_score_key(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf = sessions.session_file_for("score-fallback", sessions_dir)
    mf = sessions.meta_file_for(sf)

    messages = [
        {"role": "user", "content": "Q"},
        {
            "role": "assistant",
            "content": "A",
            "rag_chunks": [
                {"doc_id": "doc.md", "chunk_id": 1, "score": 0.75, "text": "chunk text"}
            ],
        },
    ]
    sessions.save_session_messages(sf, messages)
    sessions.save_session_meta(mf, {"title": "Score Fallback"})

    ok, content = sessions.export_session_html("score-fallback", sessions_dir)
    assert ok
    assert "Confidence: 0.750" in content


# --- sync_filename_with_title: remaining branches ---


def test_sync_filename_with_title_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    success, message, new_name = sessions.sync_filename_with_title("nope", sessions_dir, force=True)
    assert not success
    assert message == "Session not found"
    assert new_name == "nope"


class _BadBoolCfg:
    """A stub cfg whose getboolean always raises (for the auto_sync error path)."""

    def getboolean(self, *args, **kwargs):
        raise ValueError("bad bool")


def test_sync_filename_with_title_config_error_disables_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sf, mf, msgs, meta = sessions.init_session(
        "cfg-error-sync", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Should Not Rename"
    sessions.save_session_meta(mf, meta)

    monkeypatch.setattr(sessions, "load_config", lambda path=None: _BadBoolCfg())

    success, message, new_name = sessions.sync_filename_with_title(
        "cfg-error-sync", sessions_dir, force=False
    )
    assert success
    assert message == "disabled"
    assert new_name == "cfg-error-sync"


def test_sync_filename_with_title_too_many_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If 100+ collisions exist, sync_filename_with_title should give up.

    Creating 100 real colliding session files would be wasteful, so instead we
    make Path.exists() report True for the candidate "busy-title-1.json"
    through "busy-title-100.json" filenames (without actually creating them),
    forcing the collision-counter loop past its `counter > 100` limit.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "collision-source", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Busy Title"
    sessions.save_session_meta(mf, meta)

    # The direct target ("busy-title.json") must actually exist so the
    # collision loop is entered in the first place.
    target_sf = sessions.session_file_for("busy-title", sessions_dir)
    target_sf.write_text("[]")

    original_exists = Path.exists

    def fake_exists(self):
        name = self.name
        if name.startswith("busy-title-") and name.endswith(".json"):
            candidate = name[len("busy-title-") : -len(".json")]
            if candidate.isdigit() and 1 <= int(candidate) <= 100:
                return True
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    success, message, new_name = sessions.sync_filename_with_title(
        "collision-source", sessions_dir, force=True
    )
    assert not success
    assert message == "Could not find unique filename"
    assert new_name == "collision-source"


def test_sync_filename_with_title_single_lock_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the len(files_to_lock) == 1 branch: meta file disappears between
    the initial title-read and the (re-)existence check used to decide which
    files need locking. We simulate this by deleting the meta file as a side
    effect of sanitize_title_for_filename, which runs in between those two
    points in sync_filename_with_title.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "solo-lock-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Solo Lock Title"
    sessions.save_session_meta(mf, meta)
    assert mf.exists()

    original_sanitize = sessions.sanitize_title_for_filename

    def sanitize_and_delete_meta(title):
        if mf.exists():
            mf.unlink()
        return original_sanitize(title)

    monkeypatch.setattr(sessions, "sanitize_title_for_filename", sanitize_and_delete_meta)

    success, message, new_name = sessions.sync_filename_with_title(
        "solo-lock-test", sessions_dir, force=True
    )

    assert success
    assert message == "renamed"
    assert new_name == "solo-lock-title"

    new_sf = sessions.session_file_for(new_name, sessions_dir)
    new_mf = sessions.meta_file_for(new_sf)
    assert new_sf.exists()
    # meta_existed_initially was False by the time it was (re-)checked, so no
    # new metadata file should have been written.
    assert not new_mf.exists()


def test_sync_filename_with_title_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "timeout-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Timeout Title"
    sessions.save_session_meta(mf, meta)

    def raise_timeout(file_path, timeout=5.0):
        raise TimeoutError("locked elsewhere")

    monkeypatch.setattr(sessions, "file_lock", raise_timeout)

    success, message, new_name = sessions.sync_filename_with_title(
        "timeout-test", sessions_dir, force=True
    )
    assert not success
    assert message == "Session is busy, please try again"
    assert new_name == "timeout-test"


def test_sync_filename_with_title_rename_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "fail-rename-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Fail Rename Title"
    sessions.save_session_meta(mf, meta)

    def raise_save_error(session_file, messages):
        # Simulate a failure partway through the copy, after new_sf has
        # already been (partially) written.
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(sessions, "save_session_messages", raise_save_error)

    success, message, new_name = sessions.sync_filename_with_title(
        "fail-rename-test", sessions_dir, force=True
    )

    assert not success
    # The generic message must not leak the underlying exception detail
    # (CodeQL py/stack-trace-exposure); the real error is logged server-side.
    assert message == "Rename failed due to an internal error"
    assert "disk full" not in message
    assert new_name == "fail-rename-test"

    new_sf = sessions.session_file_for("fail-rename-title", sessions_dir)
    new_mf = sessions.meta_file_for(new_sf)
    assert not new_sf.exists()
    assert not new_mf.exists()

    # Original files should remain untouched
    assert sf.exists()
    assert mf.exists()


def test_sync_filename_with_title_cleans_up_new_meta_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the copy succeeds but deleting the original session file fails,
    cleanup should also remove the already-written new meta file."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "cleanup-meta-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Cleanup Meta Title"
    sessions.save_session_meta(mf, meta)

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "cleanup-meta-test.json":
            raise RuntimeError("unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    success, message, new_name = sessions.sync_filename_with_title(
        "cleanup-meta-test", sessions_dir, force=True
    )

    assert not success
    assert message == "Rename failed due to an internal error"
    assert "unlink failed" not in message

    new_sf = sessions.session_file_for("cleanup-meta-title", sessions_dir)
    new_mf = sessions.meta_file_for(new_sf)
    assert not new_sf.exists()
    assert not new_mf.exists()


def test_sync_filename_with_title_cleanup_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cleanup itself raises, the exception should be swallowed and the
    original rename failure message still returned."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "cleanup-fail-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    meta["title"] = "Cleanup Fail Title"
    sessions.save_session_meta(mf, meta)

    def raise_save_error(session_file, messages):
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(sessions, "save_session_messages", raise_save_error)

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        # Make cleanup's own unlink of the partially-written new session file
        # fail too, exercising the inner `except Exception: pass` swallow.
        if self.name == "cleanup-fail-title.json":
            raise RuntimeError("cleanup unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    success, message, new_name = sessions.sync_filename_with_title(
        "cleanup-fail-test", sessions_dir, force=True
    )

    assert not success
    assert message == "Rename failed due to an internal error"
    assert "disk full" not in message
    # Original files should remain untouched
    assert sf.exists()
    assert mf.exists()


# --- edit_message: success paths ---


def test_edit_message_fork_true_truncates(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "edit-fork-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "A2"},
    ]
    sessions.save_session_messages(sf, messages)

    ok, msg = sessions.edit_message("edit-fork-test", 1, "Edited A1", sessions_dir, fork=True)
    assert ok
    assert msg == "Message edited"

    result = sessions.load_session_messages(sf)
    assert len(result) == 2  # truncated after the edited message
    assert result[1]["content"] == "Edited A1"
    assert result[1]["original_content"] == "A1"
    assert "edited_at" in result[1]

    meta_after = sessions.load_session_meta(mf)
    assert "updated_at" in meta_after


def test_edit_message_fork_false_keeps_later_messages(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "edit-nofork-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    sessions.save_session_messages(sf, messages)

    ok, msg = sessions.edit_message("edit-nofork-test", 0, "Edited Q1", sessions_dir, fork=False)
    assert ok

    result = sessions.load_session_messages(sf)
    assert len(result) == 3
    assert result[0]["content"] == "Edited Q1"
    assert result[1]["content"] == "A1"


def test_edit_message_preserves_original_content_once(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "edit-preserve-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    messages = [{"role": "user", "content": "Original"}]
    sessions.save_session_messages(sf, messages)

    sessions.edit_message("edit-preserve-test", 0, "First edit", sessions_dir, fork=False)
    sessions.edit_message("edit-preserve-test", 0, "Second edit", sessions_dir, fork=False)

    result = sessions.load_session_messages(sf)
    assert result[0]["content"] == "Second edit"
    assert result[0]["original_content"] == "Original"  # unchanged by 2nd edit


def test_edit_message_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.edit_message("nonexistent", 0, "content", sessions_dir)
    assert not ok
    assert msg == "No such session"


def test_edit_message_invalid_index(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "edit-invalid-index", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    sessions.save_session_messages(sf, [{"role": "user", "content": "hi"}])

    ok, msg = sessions.edit_message("edit-invalid-index", 5, "content", sessions_dir)
    assert not ok
    assert "Invalid message index" in msg


# --- truncate_after_message ---


def test_truncate_after_message_success(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "truncate-test", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "A2"},
    ]
    sessions.save_session_messages(sf, messages)

    ok, msg = sessions.truncate_after_message("truncate-test", 1, sessions_dir)
    assert ok
    assert msg == "Conversation truncated"

    result = sessions.load_session_messages(sf)
    assert len(result) == 2
    assert result[-1]["content"] == "A1"


def test_truncate_after_message_invalid_index(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf, mf, msgs, meta = sessions.init_session(
        "truncate-invalid", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    sessions.save_session_messages(sf, [{"role": "user", "content": "hi"}])

    ok, msg = sessions.truncate_after_message("truncate-invalid", 10, sessions_dir)
    assert not ok
    assert "Invalid message index" in msg


def test_truncate_after_message_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.truncate_after_message("nonexistent", 0, sessions_dir)
    assert not ok
    assert msg == "No such session"


# --- search_messages: per-session exception handling ---


def test_search_messages_handles_per_session_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf_good = sessions.session_file_for("good-session", sessions_dir)
    sessions.save_session_messages(sf_good, [{"role": "user", "content": "find me"}])

    sf_bad = sessions.session_file_for("bad-session", sessions_dir)
    sessions.save_session_messages(sf_bad, [{"role": "user", "content": "find me"}])

    original_load = sessions.load_session_messages

    def flaky_load(path):
        if path.stem == "bad-session":
            raise RuntimeError("boom")
        return original_load(path)

    monkeypatch.setattr(sessions, "load_session_messages", flaky_load)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.sessions"):
        results = sessions.search_messages("find me", sessions_dir)

    assert len(results) == 1
    assert results[0]["session_name"] == "good-session"
    assert "Error searching session" in caplog.text


# --- _generate_preview: no-match fallback ---


def test_generate_preview_no_match_fallback() -> None:
    short_content = "Some content without the search term"
    preview = sessions._generate_preview(short_content, "nonexistent-term", case_sensitive=False)
    assert preview == short_content

    long_content = "B" * 300
    preview_long = sessions._generate_preview(long_content, "not-there", case_sensitive=False)
    assert preview_long == "B" * 200 + "..."


# --- merge_sessions: remaining validation / metadata / failure branches ---


def test_merge_sessions_missing_output_name(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.merge_sessions(["a"], "", sessions_dir)
    assert not ok
    assert msg == "Output session name is required"


def test_merge_sessions_invalid_output_name(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.merge_sessions(["a"], "invalid name!", sessions_dir)
    assert not ok
    assert "Invalid output session name" in msg


def test_merge_sessions_invalid_input_name(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    ok, msg = sessions.merge_sessions(["bad name!"], "output", sessions_dir)
    assert not ok
    assert "Invalid session name" in msg


def test_merge_sessions_no_metadata_uses_current_time(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf1 = sessions.session_file_for("nometa1", sessions_dir)
    sessions.save_session_messages(sf1, [{"role": "user", "content": "hi"}])
    sf2 = sessions.session_file_for("nometa2", sessions_dir)
    sessions.save_session_messages(sf2, [{"role": "user", "content": "there"}])
    # Intentionally no .meta.json files for either input session.

    before = sessions.iso_now()
    ok, msg = sessions.merge_sessions(["nometa1", "nometa2"], "merged-nometa", sessions_dir)
    assert ok

    merged_meta_file = sessions.meta_file_for(
        sessions.session_file_for("merged-nometa", sessions_dir)
    )
    merged_meta = sessions.load_session_meta(merged_meta_file)
    assert merged_meta["created_at"] >= before


def test_merge_sessions_propagates_summary_model_rag(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf1 = sessions.session_file_for("meta-rich", sessions_dir)
    mf1 = sessions.meta_file_for(sf1)
    sessions.save_session_messages(sf1, [{"role": "user", "content": "hi"}])
    sessions.save_session_meta(
        mf1,
        {
            "created_at": sessions.iso_now(),
            "updated_at": sessions.iso_now(),
            "pinned": False,
            "tags": [],
            "title": "Rich Meta",
            "summary": "A rich summary",
            "model": "llama3.1:8b",
            "rag_enabled": True,
        },
    )

    sf2 = sessions.session_file_for("meta-plain", sessions_dir)
    sessions.save_session_messages(sf2, [{"role": "user", "content": "there"}])

    ok, msg = sessions.merge_sessions(["meta-rich", "meta-plain"], "merged-rich", sessions_dir)
    assert ok

    merged_meta_file = sessions.meta_file_for(
        sessions.session_file_for("merged-rich", sessions_dir)
    )
    merged_meta = sessions.load_session_meta(merged_meta_file)
    assert merged_meta["summary"] == "A rich summary"
    assert merged_meta["model"] == "llama3.1:8b"
    assert merged_meta["rag_enabled"] is True


def test_merge_sessions_save_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf1 = sessions.session_file_for("merge-src", sessions_dir)
    sessions.save_session_messages(sf1, [{"role": "user", "content": "hi"}])

    def raise_save_meta(meta_file, meta):
        # Write the meta file before failing so the cleanup branch has to
        # unlink an *existing* output_meta_file (not just a no-op check).
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text("partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(sessions, "save_session_meta", raise_save_meta)

    ok, msg = sessions.merge_sessions(["merge-src"], "merge-fail-out", sessions_dir)
    assert not ok
    assert msg.startswith("Failed to save merged session:")

    output_file = sessions.session_file_for("merge-fail-out", sessions_dir)
    output_meta_file = sessions.meta_file_for(output_file)
    assert not output_file.exists()
    assert not output_meta_file.exists()


def test_merge_sessions_cleanup_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If cleanup itself raises after a failed save, the error should be
    swallowed and the original failure message still returned."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    sf1 = sessions.session_file_for("merge-src2", sessions_dir)
    sessions.save_session_messages(sf1, [{"role": "user", "content": "hi"}])

    def raise_save_meta(meta_file, meta):
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text("partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(sessions, "save_session_meta", raise_save_meta)

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "merge-fail-out2.json":
            raise RuntimeError("cleanup unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    ok, msg = sessions.merge_sessions(["merge-src2"], "merge-fail-out2", sessions_dir)
    assert not ok
    assert msg.startswith("Failed to save merged session:")


# --- batch operations: remaining failure branches ---


def test_batch_tag_sessions_reports_failures(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sessions.init_session("real-session", sessions_dir, new_session=True, model="llama3.1:8b")

    success, failure, failed = sessions.batch_tag_sessions(
        ["real-session", "ghost-session"], ["tag1"], sessions_dir
    )
    assert success == 1
    assert failure == 1
    assert failed == ["ghost-session"]


def test_batch_export_sessions_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    output_dir = tmp_path / "exports"

    sessions.init_session("export-ok", sessions_dir, new_session=True, model="llama3.1:8b")
    sf2, mf2, msgs2, meta2 = sessions.init_session(
        "export-fail", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs2.append({"role": "user", "content": "content"})
    sessions.save_session_messages(sf2, msgs2)

    original_write_text = Path.write_text

    def flaky_write_text(self, *args, **kwargs):
        if self.name == "export-fail.md":
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    success, failure, failed = sessions.batch_export_sessions(
        ["export-ok", "export-fail"], output_dir, sessions_dir, format="markdown"
    )
    assert success == 1
    assert failure == 1
    assert failed == ["export-fail"]


def test_batch_export_sessions_html_format(tmp_path: Path) -> None:
    """Covers the format == "html" branch of batch_export_sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    output_dir = tmp_path / "exports_html"

    sf, mf, msgs, meta = sessions.init_session(
        "export-html", sessions_dir, new_session=True, model="llama3.1:8b"
    )
    msgs.append({"role": "user", "content": "hello"})
    sessions.save_session_messages(sf, msgs)

    success, failure, failed = sessions.batch_export_sessions(
        ["export-html"], output_dir, sessions_dir, format="html"
    )
    assert success == 1
    assert failure == 0
    assert (output_dir / "export-html.html").exists()
    assert "<!DOCTYPE html>" in (output_dir / "export-html.html").read_text()


def test_batch_export_sessions_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    output_dir = tmp_path / "exports2"

    success, failure, failed = sessions.batch_export_sessions(
        ["nonexistent"], output_dir, sessions_dir, format="markdown"
    )
    assert success == 0
    assert failure == 1
    assert failed == ["nonexistent"]


def test_batch_update_metadata_nonexistent_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    success, failure, failed = sessions.batch_update_metadata(
        ["nonexistent"], sessions_dir, pinned=True
    )
    assert success == 0
    assert failure == 1
    assert failed == ["nonexistent"]


def test_batch_update_metadata_handles_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sessions.init_session("update-fail", sessions_dir, new_session=True, model="llama3.1:8b")

    def raise_error(meta_file, meta):
        raise RuntimeError("write failed")

    monkeypatch.setattr(sessions, "save_session_meta", raise_error)

    success, failure, failed = sessions.batch_update_metadata(
        ["update-fail"], sessions_dir, pinned=True
    )
    assert success == 0
    assert failure == 1
    assert failed == ["update-fail"]
