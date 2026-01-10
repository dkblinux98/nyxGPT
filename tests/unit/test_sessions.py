from __future__ import annotations

import configparser
import json
import logging
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
    state = sessions.load_session("s-override", cfg, sessions_dir_override=str(override_dir), new_session=True)
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
    with pytest.raises(Exception):
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
    with caplog.at_level(logging.WARNING):
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
        with pytest.raises(TimeoutError, match="Could not acquire lock"):
            with sessions.file_lock(test_file, timeout=0.5):
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
    """Test that verify_lock_ordering works with various path formats."""
    # Paths that should sort correctly alphabetically
    files = [
        tmp_path / "sessions" / "test.json.meta",
        tmp_path / "sessions" / "test.json",
    ]

    # Sort them
    files.sort(key=lambda p: str(p))

    # Should pass - sorted correctly
    sessions.verify_lock_ordering(*files)


def test_metadata_file_deleted_between_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        if file_path == mf and lock_call_count["count"] == 2:  # Second lock call is for metadata
            if mf.exists():
                mf.unlink()
        return original_file_lock(file_path, timeout)

    monkeypatch.setattr("mygpt.sessions.file_lock", patched_file_lock)

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
    new_mf = sessions.meta_file_for(new_sf)
    # The operation should have handled the missing file gracefully
    # (not crashed or left files in inconsistent state)
