from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_message_pagination_basic(api_base_url: str, tmp_sessions_dir) -> None:
    """Test basic pagination functionality for loading messages."""
    # Create a test session with many messages
    session_name = "pagination-test"

    # Create session with multiple messages
    messages = []
    for i in range(100):
        messages.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {i}"
        })

    # Save session file
    import json
    from pathlib import Path
    session_file = tmp_sessions_dir / f"{session_name}.json"
    session_file.write_text(json.dumps(messages))

    # Test 1: Get all messages (no pagination params)
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir)},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 100
    assert data["total"] == 100
    assert data["offset"] == 0
    assert data["limit"] == 100

    # Test 2: Get first 10 messages
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 0, "limit": 10},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 10
    assert data["messages"][0]["content"] == "Message 0"
    assert data["messages"][9]["content"] == "Message 9"
    assert data["total"] == 100
    assert data["offset"] == 0
    assert data["limit"] == 10

    # Test 3: Get messages from offset 50, limit 20
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 50, "limit": 20},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 20
    assert data["messages"][0]["content"] == "Message 50"
    assert data["messages"][19]["content"] == "Message 69"
    assert data["total"] == 100
    assert data["offset"] == 50
    assert data["limit"] == 20


@pytest.mark.integration
def test_message_pagination_edge_cases(api_base_url: str, tmp_sessions_dir) -> None:
    """Test edge cases for message pagination."""
    session_name = "pagination-edge-test"

    # Create session with 25 messages
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Msg {i}"}
        for i in range(25)
    ]

    import json
    from pathlib import Path
    session_file = tmp_sessions_dir / f"{session_name}.json"
    session_file.write_text(json.dumps(messages))

    # Test 1: Offset beyond total messages
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 100, "limit": 10},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 0
    assert data["total"] == 25

    # Test 2: Limit larger than remaining messages
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 20, "limit": 100},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 5  # Only 5 messages from offset 20
    assert data["total"] == 25

    # Test 3: Offset only (no limit)
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 10},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 15  # All remaining messages from offset 10
    assert data["total"] == 25

    # Test 4: Limit only (no offset)
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "limit": 10},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 10
    assert data["messages"][0]["content"] == "Msg 0"
    assert data["total"] == 25


@pytest.mark.integration
def test_message_pagination_empty_session(api_base_url: str, tmp_sessions_dir) -> None:
    """Test pagination with an empty session."""
    session_name = "pagination-empty-test"

    # Create empty session
    import json
    from pathlib import Path
    session_file = tmp_sessions_dir / f"{session_name}.json"
    session_file.write_text(json.dumps([]))

    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/{session_name}",
        params={"sessions_dir": str(tmp_sessions_dir), "offset": 0, "limit": 10},
        timeout=5.0
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 0
    assert data["total"] == 0
    assert data["offset"] == 0
    assert data["limit"] == 10
