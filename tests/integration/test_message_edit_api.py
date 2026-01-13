"""Integration tests for message edit and regenerate API endpoints (#2641)."""
from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_edit_message_endpoint(api_base_url: str) -> None:
    """Test PATCH /api/v1/sessions/{name}/messages/{index} endpoint."""
    session_name = "test-edit-api"

    # Initialize session
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": session_name},
        timeout=5.0
    )
    assert init_resp.status_code == 200

    # Get session and manually add messages
    import json
    from pathlib import Path

    # We'll use the sessions dir from the API to directly manipulate the session file
    info_resp = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    sessions_dir = Path(info_resp.json()["sessions_dir"])

    session_file = sessions_dir / f"{session_name}.json"
    messages = [
        {"role": "user", "content": "Original message"},
        {"role": "assistant", "content": "Original response"}
    ]
    session_file.write_text(json.dumps(messages, indent=2))

    # Edit the first message
    edit_resp = httpx.patch(
        f"{api_base_url}/api/v1/sessions/{session_name}/messages/0",
        json={"content": "Edited message", "fork": False},
        timeout=5.0
    )
    assert edit_resp.status_code == 200
    data = edit_resp.json()
    assert data["ok"] is True

    # Verify the edit persisted
    get_resp = httpx.get(f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0)
    assert get_resp.status_code == 200
    session_data = get_resp.json()
    assert len(session_data["messages"]) == 2
    assert session_data["messages"][0]["content"] == "Edited message"
    assert "edited_at" in session_data["messages"][0]
    assert session_data["messages"][0]["original_content"] == "Original message"


@pytest.mark.integration
def test_edit_message_with_fork(api_base_url: str) -> None:
    """Test that editing with fork=true truncates following messages."""
    session_name = "test-edit-fork-api"

    # Initialize session
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": session_name},
        timeout=5.0
    )
    assert init_resp.status_code == 200

    # Add multiple messages
    import json
    from pathlib import Path

    info_resp = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    sessions_dir = Path(info_resp.json()["sessions_dir"])

    session_file = sessions_dir / f"{session_name}.json"
    messages = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Response 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Response 2"}
    ]
    session_file.write_text(json.dumps(messages, indent=2))

    # Edit first message with fork=true
    edit_resp = httpx.patch(
        f"{api_base_url}/api/v1/sessions/{session_name}/messages/0",
        json={"content": "Edited message", "fork": True},
        timeout=5.0
    )
    assert edit_resp.status_code == 200

    # Verify only first message remains
    get_resp = httpx.get(f"{api_base_url}/api/v1/sessions/{session_name}", timeout=5.0)
    session_data = get_resp.json()
    assert len(session_data["messages"]) == 1
    assert session_data["messages"][0]["content"] == "Edited message"


@pytest.mark.integration
def test_edit_message_invalid_index(api_base_url: str) -> None:
    """Test that editing with invalid index returns 400."""
    session_name = "test-edit-invalid-api"

    # Initialize session with one message
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": session_name},
        timeout=5.0
    )
    assert init_resp.status_code == 200

    import json
    from pathlib import Path

    info_resp = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    sessions_dir = Path(info_resp.json()["sessions_dir"])

    session_file = sessions_dir / f"{session_name}.json"
    messages = [{"role": "user", "content": "Only message"}]
    session_file.write_text(json.dumps(messages, indent=2))

    # Try to edit with invalid index
    edit_resp = httpx.patch(
        f"{api_base_url}/api/v1/sessions/{session_name}/messages/99",
        json={"content": "Should fail", "fork": False},
        timeout=5.0
    )
    assert edit_resp.status_code == 400


@pytest.mark.integration
def test_regenerate_endpoint_requires_user_message(api_base_url: str) -> None:
    """Test that regenerate endpoint requires a user message."""
    session_name = "test-regen-validation"

    # Initialize session
    init_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": session_name},
        timeout=5.0
    )
    assert init_resp.status_code == 200

    # Add an assistant message (not user)
    import json
    from pathlib import Path

    info_resp = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    sessions_dir = Path(info_resp.json()["sessions_dir"])

    session_file = sessions_dir / f"{session_name}.json"
    messages = [{"role": "assistant", "content": "Assistant message"}]
    session_file.write_text(json.dumps(messages, indent=2))

    # Try to regenerate from assistant message
    regen_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/{session_name}/messages/0/regenerate",
        json={},
        timeout=5.0
    )
    assert regen_resp.status_code == 400
    data = regen_resp.json()
    assert "user messages" in data["detail"].lower()


@pytest.mark.integration
def test_regenerate_endpoint_nonexistent_session(api_base_url: str) -> None:
    """Test that regenerate fails gracefully for nonexistent session."""
    regen_resp = httpx.post(
        f"{api_base_url}/api/v1/sessions/nonexistent-session/messages/0/regenerate",
        json={},
        timeout=5.0
    )
    assert regen_resp.status_code == 404
