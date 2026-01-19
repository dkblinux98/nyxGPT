"""Integration tests for session metadata API endpoints.

Tests for 9 session metadata endpoints:
- POST /api/v1/sessions/{name}/pin
- POST /api/v1/sessions/{name}/unpin
- POST /api/v1/sessions/{name}/title
- POST /api/v1/sessions/{name}/tags/add
- POST /api/v1/sessions/{name}/tags/remove
- POST /api/v1/sessions/{name}/rename
- POST /api/v1/sessions/{name}/sync-filename
- POST /api/v1/sessions/{name}/summarize
- GET /api/v1/sessions/{name}/metadata
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


def _create_test_session(
    api_base_url: str, session_name: str, sessions_dir: Path
) -> None:
    """Helper to create a test session with some messages."""
    # Initialize session via API
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init", json={"name": session_name}, timeout=5.0
    )
    assert init_resp.status_code == 200

    # Add some messages so session has content
    session_file = sessions_dir / f"{session_name}.json"
    messages = [
        {"role": "user", "content": "Test message 1"},
        {"role": "assistant", "content": "Test response 1"},
    ]
    session_file.write_text(json.dumps(messages, indent=2))


# ==================== PIN/UNPIN TESTS ====================


@pytest.mark.integration
def test_pin_session_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test pinning a session successfully."""
    session_name = "test-pin-success"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Pin the session
    pin_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/pin",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert pin_resp.status_code == 200
    assert pin_resp.json()["ok"] is True

    # Verify pinned status via GET /sessions/{name}
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert session_data["meta"]["pinned"] is True


@pytest.mark.integration
def test_pin_session_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test pinning nonexistent session returns 400."""
    pin_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/pin",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    # The endpoint returns 400 for nonexistent sessions (not 404)
    assert pin_resp.status_code == 400


@pytest.mark.integration
def test_unpin_session_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test unpinning a pinned session successfully."""
    session_name = "test-unpin-success"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Pin the session first
    pin_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/pin",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert pin_resp.status_code == 200

    # Now unpin it
    unpin_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/unpin",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert unpin_resp.status_code == 200
    assert unpin_resp.json()["ok"] is True

    # Verify unpinned status
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert session_data["meta"]["pinned"] is False


@pytest.mark.integration
def test_unpin_session_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test unpinning nonexistent session returns 400."""
    unpin_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/unpin",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert unpin_resp.status_code == 400


# ==================== TITLE TESTS ====================


@pytest.mark.integration
def test_set_title_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test setting session title successfully."""
    session_name = "test-title-success"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Set title
    title = "My Test Session"
    title_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/title",
        json={"title": title},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert title_resp.status_code == 200
    assert title_resp.json()["ok"] is True

    # Verify title was set
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert session_data["meta"]["title"] == title


@pytest.mark.integration
def test_set_title_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test setting title on nonexistent session returns 400."""
    title_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/title",
        json={"title": "Some Title"},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert title_resp.status_code == 400


@pytest.mark.integration
def test_set_title_empty_string(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test setting empty title returns validation error."""
    session_name = "test-title-empty"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Try to set empty title - should fail validation
    title_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/title",
        json={"title": ""},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    # Pydantic validation should reject this (422)
    assert title_resp.status_code == 422


# ==================== TAGS TESTS ====================


@pytest.mark.integration
def test_add_tags_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test adding tags to session successfully."""
    session_name = "test-tags-add"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Add tags
    tags = ["python", "testing", "api"]
    tags_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/tags/add",
        json={"tags": tags},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert tags_resp.status_code == 200
    assert tags_resp.json()["ok"] is True

    # Verify tags were added
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert set(session_data["meta"]["tags"]) == set(tags)


@pytest.mark.integration
def test_add_tags_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test adding tags to nonexistent session returns 400."""
    tags_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/tags/add",
        json={"tags": ["test"]},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert tags_resp.status_code == 400


@pytest.mark.integration
def test_add_tags_empty_list(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test adding empty tag list returns 400."""
    session_name = "test-tags-empty"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Try to add empty tags list
    tags_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/tags/add",
        json={"tags": []},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    # Should fail - endpoint requires at least one tag
    assert tags_resp.status_code == 400


@pytest.mark.integration
def test_remove_tags_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test removing tags from session successfully."""
    session_name = "test-tags-remove"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Add tags first
    tags = ["python", "testing", "api", "remove-me"]
    httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/tags/add",
        json={"tags": tags},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )

    # Remove some tags
    remove_tags = ["testing", "remove-me"]
    remove_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/tags/remove",
        json={"tags": remove_tags},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert remove_resp.status_code == 200
    assert remove_resp.json()["ok"] is True

    # Verify tags were removed
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    remaining_tags = set(session_data["meta"]["tags"])
    assert remaining_tags == {"python", "api"}
    assert "testing" not in remaining_tags
    assert "remove-me" not in remaining_tags


@pytest.mark.integration
def test_remove_tags_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test removing tags from nonexistent session returns 400."""
    remove_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/tags/remove",
        json={"tags": ["test"]},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert remove_resp.status_code == 400


# ==================== RENAME TESTS ====================


@pytest.mark.integration
def test_rename_session_direct(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test direct session rename without filename sync."""
    session_name = "test-rename-direct"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Rename without sync_filename
    new_name = "renamed-session"
    rename_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/rename",
        json={"new_name": new_name, "sync_filename": False},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert rename_resp.status_code == 200
    data = rename_resp.json()
    assert data["ok"] is True
    assert data["old_name"] == session_name
    assert data["new_name"] == new_name

    # Verify old session doesn't exist
    old_file = tmp_sessions_dir / f"{session_name}.json"
    assert not old_file.exists()

    # Verify new session exists
    new_file = tmp_sessions_dir / f"{new_name}.json"
    assert new_file.exists()


@pytest.mark.integration
def test_rename_session_with_title_sync(
    api_base_url: str, tmp_sessions_dir: Path
) -> None:
    """Test session rename with title update and filename sync."""
    session_name = "test-rename-sync"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Rename with sync_filename=True (updates title and syncs filename)
    new_title = "My Renamed Session"
    rename_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/rename",
        json={"new_name": new_title, "sync_filename": True},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert rename_resp.status_code == 200
    data = rename_resp.json()
    assert data["ok"] is True
    assert data["old_name"] == session_name
    # new_name will be sanitized version of title
    sanitized_name = data["new_name"]

    # Verify new session exists with sanitized filename
    new_file = tmp_sessions_dir / f"{sanitized_name}.json"
    assert new_file.exists()

    # Verify title was set
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{sanitized_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert session_data["meta"]["title"] == new_title


@pytest.mark.integration
def test_rename_session_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test renaming nonexistent session returns 404."""
    rename_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/rename",
        json={"new_name": "new-name", "sync_filename": False},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert rename_resp.status_code == 404


@pytest.mark.integration
def test_rename_session_invalid_name(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test renaming with invalid name returns 400."""
    session_name = "test-rename-invalid"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Try to rename with invalid characters (e.g., path separator)
    rename_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/rename",
        json={"new_name": "../invalid/name", "sync_filename": False},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert rename_resp.status_code == 400


# ==================== SYNC-FILENAME TESTS ====================


@pytest.mark.integration
def test_sync_filename_success(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test syncing filename with session title successfully."""
    session_name = "test-sync-filename"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Set a title first
    title = "My Session With Title"
    httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/title",
        json={"title": title},
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )

    # Sync filename
    sync_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/sync-filename",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert sync_resp.status_code == 200
    data = sync_resp.json()
    assert data["ok"] is True
    assert data["old_name"] == session_name
    new_name = data["new_name"]

    # Verify old file is gone and new file exists
    old_file = tmp_sessions_dir / f"{session_name}.json"
    assert not old_file.exists()

    new_file = tmp_sessions_dir / f"{new_name}.json"
    assert new_file.exists()

    # Verify title is preserved
    get_resp = httpx.get(
        f"{api_base_url}/api/v1/sessions/{new_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert session_data["meta"]["title"] == title


@pytest.mark.integration
def test_sync_filename_no_title(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test syncing filename when no title is set."""
    session_name = "test-sync-no-title"
    _create_test_session(api_base_url, session_name, tmp_sessions_dir)

    # Sync filename without setting title
    sync_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/sync-filename",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert sync_resp.status_code == 200
    data = sync_resp.json()
    assert data["ok"] is True
    # Should indicate no title was set
    assert "no title" in data["message"].lower() or data["name"] == session_name


@pytest.mark.integration
def test_sync_filename_nonexistent(api_base_url: str, tmp_sessions_dir: Path) -> None:
    """Test syncing filename for nonexistent session returns 404."""
    sync_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/sync-filename",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert sync_resp.status_code == 404


# ==================== SUMMARIZE TESTS ====================


@pytest.mark.integration
def test_summarize_session_success(
    api_base_url: str, tmp_sessions_dir: Path, require_ollama
) -> None:
    """Test generating session summary/title/tags successfully.

    This test requires a running Ollama instance with a working model.
    Note: The summarize endpoint uses the default sessions_dir from config.
    If the LLM fails to generate valid JSON, the test verifies the endpoint
    returns an appropriate error (400) rather than crashing.
    """
    # Use a unique session name to avoid conflicts with default sessions_dir
    import uuid

    session_name = f"test-summarize-{uuid.uuid4().hex[:8]}"

    # Initialize session in default sessions_dir (not tmp)
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init", json={"name": session_name}, timeout=5.0
    )
    assert init_resp.status_code == 200

    # Get default sessions_dir from API
    info_resp = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    default_sessions_dir = Path(info_resp.json()["sessions_dir"])

    # Add more messages to make summarization meaningful
    session_file = default_sessions_dir / f"{session_name}.json"
    messages = [
        {"role": "user", "content": "What is Python programming language?"},
        {
            "role": "assistant",
            "content": "Python is a high-level, interpreted programming language known for its simplicity.",
        },
        {"role": "user", "content": "How do I install it?"},
        {
            "role": "assistant",
            "content": "You can download Python from python.org or use a package manager.",
        },
    ]
    session_file.write_text(json.dumps(messages, indent=2))

    try:
        # Summarize session (uses default sessions_dir)
        summarize_resp = httpx.post(
            f"{api_base_url}/api/v1/sessions/{session_name}/summarize",
            timeout=60.0,  # LLM call may take time
        )

        # The endpoint should return either 200 (success) or 400 (LLM failure)
        # but never crash (500)
        assert summarize_resp.status_code in [200, 400], (
            f"Expected 200 or 400, got {summarize_resp.status_code}: {summarize_resp.text}"
        )

        # If successful, verify metadata was updated
        if summarize_resp.status_code == 200:
            assert summarize_resp.json()["ok"] is True

            # Verify metadata was updated (title/summary/tags should be set)
            get_resp = httpx.get(
                f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0
            )
            assert get_resp.status_code == 200
            session_data = get_resp.json()
            meta = session_data["meta"]

            # After successful summarization, at least title should be set
            assert "title" in meta
            # Title should not be empty or default
            assert meta["title"] is not None and len(meta["title"]) > 0
        else:
            # If LLM failed (400), verify error message indicates the issue
            error_data = summarize_resp.json()
            assert "error" in error_data
            # This is acceptable - LLM might not be configured or working properly
            pytest.skip(
                f"Summarization failed due to LLM issue: {error_data['error'].get('message', 'Unknown error')}"
            )
    finally:
        # Clean up - delete the test session
        try:
            httpx.delete(f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0)
        except Exception:
            pass  # Ignore cleanup errors


@pytest.mark.integration
def test_summarize_session_nonexistent(
    api_base_url: str, tmp_sessions_dir: Path
) -> None:
    """Test summarizing nonexistent session returns 400."""
    # Use tmp_sessions_dir to ensure session doesn't exist
    summarize_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session-xyz123/summarize",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0,
    )
    assert summarize_resp.status_code == 400


# ==================== GET METADATA TESTS ====================


@pytest.mark.integration
def test_get_metadata_success(api_base_url: str) -> None:
    """Test getting session metadata successfully.

    Note: The metadata endpoint uses the default sessions_dir from config.
    """
    import uuid

    session_name = f"test-metadata-{uuid.uuid4().hex[:8]}"

    # Initialize session in default sessions_dir
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init", json={"name": session_name}, timeout=5.0
    )
    assert init_resp.status_code == 200

    try:
        # Set some metadata
        httpx.post(
            f"{api_base_url}/api/v1/sessions/{session_name}/title",
            json={"title": "Test Metadata Session"},
            timeout=5.0,
        )
        httpx.post(
            f"{api_base_url}/api/v1/sessions/{session_name}/tags/add",
            json={"tags": ["test", "metadata"]},
            timeout=5.0,
        )
        httpx.post(f"{api_base_url}/api/v1/sessions/{session_name}/pin", timeout=5.0)

        # Get metadata directly via metadata endpoint
        meta_resp = httpx.get(
            f"{api_base_url}/api/v1/sessions/{session_name}/metadata", timeout=5.0
        )
        assert meta_resp.status_code == 200
        meta = meta_resp.json()

        # Verify metadata contains expected fields
        assert "title" in meta
        assert meta["title"] == "Test Metadata Session"
        assert "tags" in meta
        assert set(meta["tags"]) == {"test", "metadata"}
        assert "pinned" in meta
        assert meta["pinned"] is True
        # rag_enabled should always be present as a default
        assert "rag_enabled" in meta
    finally:
        # Clean up
        try:
            httpx.delete(f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0)
        except Exception:
            pass


@pytest.mark.integration
def test_get_metadata_creates_session_if_missing(api_base_url: str) -> None:
    """Test GET /sessions/{name}/metadata creates session if it doesn't exist.

    This is special behavior for the metadata endpoint - it creates the session
    with default metadata if it doesn't exist yet.
    """
    import uuid

    session_name = f"test-metadata-autocreate-{uuid.uuid4().hex[:8]}"

    try:
        # Get metadata for non-existent session
        meta_resp = httpx.get(
            f"{api_base_url}/api/v1/sessions/{session_name}/metadata", timeout=5.0
        )
        assert meta_resp.status_code == 200
        meta = meta_resp.json()

        # Should return default metadata structure
        # Note: title is optional and may not be present by default
        assert "tags" in meta
        assert "pinned" in meta
        assert "rag_enabled" in meta
        # Verify default values
        assert meta["tags"] == []
        assert meta["pinned"] is False

        # Verify session was actually created
        get_resp = httpx.get(
            f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0
        )
        assert get_resp.status_code == 200
    finally:
        # Clean up
        try:
            httpx.delete(f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0)
        except Exception:
            pass
