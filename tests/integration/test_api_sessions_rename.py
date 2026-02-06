"""Integration tests for session rename API endpoints.

Tests the following endpoints:
- POST /api/v1/sessions/{name}/rename - Rename session with optional sync
- POST /api/v1/sessions/{name}/sync-filename - Sync filename with title

These endpoints handle session renaming, title-based filename sync, and
filename sanitization for file system compatibility.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest


@pytest.mark.integration
def test_session_rename_direct(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename with direct rename (sync_filename=false)."""
    old_name = f"rename-test-{uuid.uuid4().hex[:8]}"
    new_name = f"renamed-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": old_name})
        assert init_resp.status_code == 200

        # Rename without sync
        rename_resp = client.post(
            f"/api/v1/sessions/{old_name}/rename",
            json={"new_name": new_name, "sync_filename": False},
        )
        assert rename_resp.status_code == 200
        data = rename_resp.json()
        assert data["ok"] is True
        assert data["old_name"] == old_name
        assert data["new_name"] == new_name

        # Verify old name is gone
        get_old_resp = client.get(f"/api/v1/sessions/{old_name}")
        assert get_old_resp.status_code == 404

        # Verify new name exists
        get_new_resp = client.get(f"/api/v1/sessions/{new_name}")
        assert get_new_resp.status_code == 200


@pytest.mark.integration
def test_session_rename_with_sync(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename with sync_filename=true."""
    old_name = f"rename-sync-test-{uuid.uuid4().hex[:8]}"
    title = "Test Session With Spaces And Special-Characters!"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": old_name})
        assert init_resp.status_code == 200

        # Rename with sync (title will be sanitized for filename)
        rename_resp = client.post(
            f"/api/v1/sessions/{old_name}/rename",
            json={"new_name": title, "sync_filename": True},
        )
        assert rename_resp.status_code == 200
        data = rename_resp.json()
        assert data["ok"] is True
        assert data["old_name"] == old_name

        # New name should be sanitized (alphanumeric, hyphens, underscores only)
        new_name = data["new_name"]
        assert " " not in new_name  # Spaces should be converted
        assert "!" not in new_name  # Special chars should be removed

        # Verify title is set
        get_resp = client.get(f"/api/v1/sessions/{new_name}")
        assert get_resp.status_code == 200
        session_data = get_resp.json()
        assert session_data["meta"]["title"] == title


@pytest.mark.integration
def test_session_rename_invalid_characters(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename with invalid characters."""
    old_name = f"rename-invalid-test-{uuid.uuid4().hex[:8]}"
    invalid_name = "../../../etc/passwd"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": old_name})
        assert init_resp.status_code == 200

        # Try to rename with path traversal attempt
        rename_resp = client.post(
            f"/api/v1/sessions/{old_name}/rename",
            json={"new_name": invalid_name, "sync_filename": False},
        )

        # Should reject invalid name
        assert rename_resp.status_code == 400


@pytest.mark.integration
def test_session_rename_nonexistent_session(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename for nonexistent session."""
    nonexistent_session = f"nonexistent-{uuid.uuid4().hex[:8]}"
    new_name = f"new-name-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        rename_resp = client.post(
            f"/api/v1/sessions/{nonexistent_session}/rename",
            json={"new_name": new_name, "sync_filename": False},
        )

    assert rename_resp.status_code == 404


@pytest.mark.integration
def test_session_rename_to_existing_name(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename to an already existing name."""
    session1 = f"rename-conflict-1-{uuid.uuid4().hex[:8]}"
    session2 = f"rename-conflict-2-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create two sessions
        init_resp1 = client.post("/api/v1/sessions/init", json={"name": session1})
        assert init_resp1.status_code == 200

        init_resp2 = client.post("/api/v1/sessions/init", json={"name": session2})
        assert init_resp2.status_code == 200

        # Try to rename session1 to session2's name
        rename_resp = client.post(
            f"/api/v1/sessions/{session1}/rename",
            json={"new_name": session2, "sync_filename": False},
        )

        # Should fail (conflict)
        assert rename_resp.status_code == 400


@pytest.mark.integration
def test_session_sync_filename_with_title(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/sync-filename syncs to title."""
    session_name = f"sync-test-{uuid.uuid4().hex[:8]}"
    title = "My Test Session Title"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Set title
        title_resp = client.post(f"/api/v1/sessions/{session_name}/title", json={"title": title})
        assert title_resp.status_code == 200

        # Sync filename
        sync_resp = client.post(f"/api/v1/sessions/{session_name}/sync-filename")
        assert sync_resp.status_code == 200
        data = sync_resp.json()
        assert data["ok"] is True
        assert "old_name" in data or "name" in data

        # If renamed, verify new name
        if "new_name" in data:
            new_name = data["new_name"]
            # Verify session exists under new name
            get_resp = client.get(f"/api/v1/sessions/{new_name}")
            assert get_resp.status_code == 200


@pytest.mark.integration
def test_session_sync_filename_without_title(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/sync-filename without title."""
    session_name = f"sync-notitle-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session (no title set)
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Try to sync filename without title
        sync_resp = client.post(f"/api/v1/sessions/{session_name}/sync-filename")
        assert sync_resp.status_code == 200
        data = sync_resp.json()
        assert data["ok"] is True

        # Should report no change or no title
        assert data.get("message") in ["No title set, filename unchanged", "no_title"]


@pytest.mark.integration
def test_session_sync_filename_already_synced(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/sync-filename when already synced."""
    session_name = "already-synced-session"
    title = "already synced session"  # Same as session_name (sanitized)

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Set title that matches current filename
        title_resp = client.post(f"/api/v1/sessions/{session_name}/title", json={"title": title})
        assert title_resp.status_code == 200

        # Sync filename
        sync_resp = client.post(f"/api/v1/sessions/{session_name}/sync-filename")
        assert sync_resp.status_code == 200
        data = sync_resp.json()
        assert data["ok"] is True

        # Should report no change
        if "message" in data:
            assert "already" in data["message"].lower() or "no change" in data["message"].lower()


@pytest.mark.integration
def test_session_sync_filename_nonexistent_session(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/sync-filename for nonexistent session."""
    nonexistent_session = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        sync_resp = client.post(f"/api/v1/sessions/{nonexistent_session}/sync-filename")

    assert sync_resp.status_code == 404


@pytest.mark.integration
def test_session_rename_empty_name(api_base_url: str) -> None:
    """Test POST /api/v1/sessions/{name}/rename with empty new_name."""
    session_name = f"rename-empty-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Try to rename with empty name
        rename_resp = client.post(
            f"/api/v1/sessions/{session_name}/rename",
            json={"new_name": "", "sync_filename": False},
        )

        # Should reject empty name (422 for validation errors)
        assert rename_resp.status_code == 422


@pytest.mark.integration
def test_session_rename_preserves_messages(api_base_url: str) -> None:
    """Test that renaming a session preserves its messages and metadata."""
    import json

    old_name = f"rename-preserve-test-{uuid.uuid4().hex[:8]}"
    new_name = f"renamed-preserve-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": old_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add messages
        session_file = sessions_dir / f"{old_name}.json"
        messages = [
            {"role": "user", "content": "Test message 1"},
            {"role": "assistant", "content": "Test response 1"},
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Set title
        title_resp = client.post(f"/api/v1/sessions/{old_name}/title", json={"title": "Test Title"})
        assert title_resp.status_code == 200

        # Rename session
        rename_resp = client.post(
            f"/api/v1/sessions/{old_name}/rename",
            json={"new_name": new_name, "sync_filename": False},
        )
        assert rename_resp.status_code == 200

        # Verify messages are preserved
        get_resp = client.get(f"/api/v1/sessions/{new_name}")
        assert get_resp.status_code == 200
        session_data = get_resp.json()
        assert len(session_data["messages"]) == 2
        assert session_data["messages"][0]["content"] == "Test message 1"
        assert session_data["meta"]["title"] == "Test Title"
