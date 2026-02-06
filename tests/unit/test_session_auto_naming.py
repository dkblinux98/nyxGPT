"""Unit tests for automatic session naming and filename sync functionality."""

import pytest

from nyxgpt.sessions import (
    meta_file_for,
    sanitize_title_for_filename,
    save_session_messages,
    save_session_meta,
    session_file_for,
    sync_filename_with_title,
)


@pytest.mark.unit
def test_sanitize_title_for_filename_basic():
    """Test basic title sanitization."""
    assert sanitize_title_for_filename("My Chat Session") == "my-chat-session"
    assert sanitize_title_for_filename("Python Tips") == "python-tips"
    assert sanitize_title_for_filename("Session 42") == "session-42"


@pytest.mark.unit
def test_sanitize_title_for_filename_special_chars():
    """Test sanitization of special characters."""
    assert sanitize_title_for_filename("Python: Tips & Tricks!") == "python-tips-tricks"
    assert sanitize_title_for_filename("Session #42 (Important)") == "session-42-important"
    assert sanitize_title_for_filename("Hello/World\\Test") == "hello-world-test"
    assert sanitize_title_for_filename("Test@Home.com") == "test-home-com"


@pytest.mark.unit
def test_sanitize_title_for_filename_collapse_hyphens():
    """Test that multiple consecutive hyphens are collapsed."""
    assert sanitize_title_for_filename("Test   -   Session") == "test-session"
    assert sanitize_title_for_filename("A -- B -- C") == "a-b-c"


@pytest.mark.unit
def test_sanitize_title_for_filename_truncation():
    """Test that long titles are truncated to 64 characters."""
    long_title = "A" * 100
    sanitized = sanitize_title_for_filename(long_title)
    assert len(sanitized) <= 64
    assert sanitized == "a" * 64


@pytest.mark.unit
def test_sanitize_title_for_filename_empty_fallback():
    """Test fallback for empty titles after sanitization."""
    assert sanitize_title_for_filename("!!!") == "session"
    assert sanitize_title_for_filename("...") == "session"
    assert sanitize_title_for_filename("---") == "session"


@pytest.mark.unit
def test_sync_filename_with_title_no_title(tmp_path):
    """Test filename sync when session has no title."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create session without title
    session_name = "test-session"
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    save_session_messages(sf, [{"role": "user", "content": "Hello"}])
    save_session_meta(mf, {})

    # Should not rename (no title)
    success, message, new_name = sync_filename_with_title(session_name, sessions_dir, force=True)

    assert success is True
    assert message == "no_title"
    assert new_name == session_name


@pytest.mark.unit
def test_sync_filename_with_title_no_change_needed(tmp_path):
    """Test filename sync when title already matches filename."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_name = "my-chat-session"
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    save_session_messages(sf, [{"role": "user", "content": "Hello"}])
    save_session_meta(mf, {"title": "My Chat Session"})  # Sanitizes to same name

    success, message, new_name = sync_filename_with_title(session_name, sessions_dir, force=True)

    assert success is True
    assert message == "no_change"
    assert new_name == session_name


@pytest.mark.unit
def test_sync_filename_with_title_renames(tmp_path):
    """Test filename sync successfully renames files."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    old_name = "session_123"
    new_expected_name = "python-tips"

    sf = session_file_for(old_name, sessions_dir)
    mf = meta_file_for(sf)

    messages = [{"role": "user", "content": "Tell me about Python"}]
    meta = {"title": "Python Tips"}

    save_session_messages(sf, messages)
    save_session_meta(mf, meta)

    # Perform rename
    success, message, new_name = sync_filename_with_title(old_name, sessions_dir, force=True)

    assert success is True
    assert message == "renamed"
    assert new_name == new_expected_name

    # Verify old files deleted
    assert not sf.exists()
    assert not mf.exists()

    # Verify new files exist
    new_sf = session_file_for(new_name, sessions_dir)
    new_mf = meta_file_for(new_sf)
    assert new_sf.exists()
    assert new_mf.exists()

    # Verify content preserved
    from nyxgpt.sessions import load_session_messages

    loaded_messages = load_session_messages(new_sf)
    assert loaded_messages == messages


@pytest.mark.unit
def test_sync_filename_with_title_handles_collision(tmp_path):
    """Test filename sync handles collisions by appending counter."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create target file first
    target_name = "my-session"
    target_sf = session_file_for(target_name, sessions_dir)
    save_session_messages(target_sf, [{"role": "user", "content": "Existing"}])

    # Create source session that wants to use same name
    source_name = "session_123"
    sf = session_file_for(source_name, sessions_dir)
    mf = meta_file_for(sf)

    save_session_messages(sf, [{"role": "user", "content": "New"}])
    save_session_meta(mf, {"title": "My Session"})

    # Should append -1
    success, message, new_name = sync_filename_with_title(source_name, sessions_dir, force=True)

    assert success is True
    assert message == "renamed"
    assert new_name == "my-session-1"


@pytest.mark.unit
def test_sync_filename_with_title_disabled_by_config(tmp_path, monkeypatch):
    """Test that filename sync respects config setting."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_name = "test-session"
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    save_session_messages(sf, [{"role": "user", "content": "Hello"}])
    save_session_meta(mf, {"title": "Python Tips"})

    # Create config with auto_sync disabled
    config_content = "[nyxgpt]\nauto_sync_filename = no\n"
    config_dir = tmp_path / ".nyxGPT"
    config_dir.mkdir()
    config_file = config_dir / "config.ini"
    config_file.write_text(config_content)

    # Monkeypatch DEFAULT_CONFIG_PATH to use temp directory
    import nyxgpt.config

    monkeypatch.setattr(nyxgpt.config, "DEFAULT_CONFIG_PATH", config_file)

    # Clear config cache to force reload
    nyxgpt.config._CACHED_CFG = None
    nyxgpt.config._CACHED_PATH = None
    nyxgpt.config._CACHED_MTIME_NS = None

    # Should not rename (disabled)
    success, message, new_name = sync_filename_with_title(session_name, sessions_dir, force=False)

    assert success is True
    assert message == "disabled"
    assert new_name == session_name


@pytest.mark.unit
def test_sync_filename_with_title_force_override_config(tmp_path, monkeypatch):
    """Test that force=True overrides config setting."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_name = "test-session"
    sf = session_file_for(session_name, sessions_dir)
    mf = meta_file_for(sf)

    save_session_messages(sf, [{"role": "user", "content": "Hello"}])
    save_session_meta(mf, {"title": "Python Tips"})

    # Create config with auto_sync disabled
    config_content = "[nyxgpt]\nauto_sync_filename = no\n"
    config_dir = tmp_path / ".nyxGPT"
    config_dir.mkdir()
    config_file = config_dir / "config.ini"
    config_file.write_text(config_content)

    # Monkeypatch DEFAULT_CONFIG_PATH to use temp directory
    import nyxgpt.config

    monkeypatch.setattr(nyxgpt.config, "DEFAULT_CONFIG_PATH", config_file)

    # Clear config cache to force reload
    nyxgpt.config._CACHED_CFG = None
    nyxgpt.config._CACHED_PATH = None
    nyxgpt.config._CACHED_MTIME_NS = None

    # Should rename anyway (force=True)
    success, message, new_name = sync_filename_with_title(session_name, sessions_dir, force=True)

    assert success is True
    assert message == "renamed"
    assert new_name == "python-tips"
