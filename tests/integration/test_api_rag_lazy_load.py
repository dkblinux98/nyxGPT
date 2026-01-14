"""Integration tests for RAG lazy load endpoint (#2647).

Tests the GET /api/v1/sessions/{name}/messages/{message_index}/rag endpoint
which returns RAG chunks associated with a specific message.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest


@pytest.mark.integration
def test_get_rag_chunks_nonexistent_session(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/messages/{index}/rag returns 404 for nonexistent session."""
    nonexistent_session = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        resp = client.get(f"/api/v1/sessions/{nonexistent_session}/messages/0/rag")

    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert "message" in data["error"]
    assert nonexistent_session in data["error"]["message"]


@pytest.mark.integration
def test_get_rag_chunks_invalid_negative_index(api_base_url: str) -> None:
    """Test GET returns 400 for negative message index."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add a message
        session_file = sessions_dir / f"{session_name}.json"
        messages = [{"role": "user", "content": "Test message"}]
        session_file.write_text(json.dumps(messages, indent=2))

        # Try negative index
        resp = client.get(f"/api/v1/sessions/{session_name}/messages/-1/rag")

    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "Invalid message_index" in data["error"]["message"]


@pytest.mark.integration
def test_get_rag_chunks_index_out_of_bounds(api_base_url: str) -> None:
    """Test GET returns 400 for message index >= session length."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add 2 messages (indices 0 and 1)
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"}
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Try index 2 (out of bounds)
        resp = client.get(f"/api/v1/sessions/{session_name}/messages/2/rag")

    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "Invalid message_index" in data["error"]["message"]
    assert "2" in data["error"]["message"]
    assert "2 messages" in data["error"]["message"]


@pytest.mark.integration
def test_get_rag_chunks_message_with_rag_data(api_base_url: str) -> None:
    """Test GET returns correct RAG chunks for message with rag_chunks field."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create test RAG chunks
        test_chunks = [
            {
                "text": "This is chunk 1 from RAG search",
                "doc_id": "doc-123",
                "chunk_id": "chunk-1",
                "score": 0.95
            },
            {
                "text": "This is chunk 2 from RAG search",
                "doc_id": "doc-456",
                "chunk_id": "chunk-2",
                "score": 0.87
            }
        ]

        # Add message with RAG chunks
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {
                "role": "user",
                "content": "Question about RAG",
                "rag_chunks": test_chunks
            },
            {
                "role": "assistant",
                "content": "Answer using RAG"
            }
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Fetch RAG chunks for first message
        resp = client.get(f"/api/v1/sessions/{session_name}/messages/0/rag")

    assert resp.status_code == 200
    data = resp.json()

    # Verify response format
    assert "message_index" in data
    assert "has_rag" in data
    assert "chunks" in data

    # Verify values
    assert data["message_index"] == 0
    assert data["has_rag"] is True
    assert isinstance(data["chunks"], list)
    assert len(data["chunks"]) == 2

    # Verify chunk structure
    chunk = data["chunks"][0]
    assert "text" in chunk
    assert "doc_id" in chunk
    assert "chunk_id" in chunk
    assert "score" in chunk
    assert chunk["text"] == "This is chunk 1 from RAG search"
    assert chunk["doc_id"] == "doc-123"


@pytest.mark.integration
def test_get_rag_chunks_message_without_rag_data(api_base_url: str) -> None:
    """Test GET returns empty chunks list for message without rag_chunks field."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add message WITHOUT rag_chunks
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Regular message without RAG"},
            {"role": "assistant", "content": "Regular response"}
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Fetch RAG chunks for message without RAG data
        resp = client.get(f"/api/v1/sessions/{session_name}/messages/0/rag")

    assert resp.status_code == 200
    data = resp.json()

    # Verify response format
    assert "message_index" in data
    assert "has_rag" in data
    assert "chunks" in data

    # Verify values for message without RAG
    assert data["message_index"] == 0
    assert data["has_rag"] is False
    assert isinstance(data["chunks"], list)
    assert len(data["chunks"]) == 0


@pytest.mark.integration
def test_get_rag_chunks_response_format_validation(api_base_url: str) -> None:
    """Test GET response format matches specification for all fields."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add mixed messages: some with RAG, some without
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {
                "role": "user",
                "content": "Message with RAG",
                "rag_chunks": [
                    {
                        "text": "RAG content",
                        "doc_id": "doc-1",
                        "chunk_id": "chunk-1",
                        "score": 0.9
                    }
                ]
            },
            {"role": "assistant", "content": "Response"},
            {"role": "user", "content": "Message without RAG"}
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Test message with RAG
        resp_with_rag = client.get(f"/api/v1/sessions/{session_name}/messages/0/rag")
        assert resp_with_rag.status_code == 200
        data_with_rag = resp_with_rag.json()

        # Validate required fields and types
        assert isinstance(data_with_rag["message_index"], int)
        assert isinstance(data_with_rag["has_rag"], bool)
        assert isinstance(data_with_rag["chunks"], list)
        assert data_with_rag["message_index"] == 0
        assert data_with_rag["has_rag"] is True

        # Test message without RAG
        resp_without_rag = client.get(f"/api/v1/sessions/{session_name}/messages/2/rag")
        assert resp_without_rag.status_code == 200
        data_without_rag = resp_without_rag.json()

        # Validate required fields and types
        assert isinstance(data_without_rag["message_index"], int)
        assert isinstance(data_without_rag["has_rag"], bool)
        assert isinstance(data_without_rag["chunks"], list)
        assert data_without_rag["message_index"] == 2
        assert data_without_rag["has_rag"] is False


@pytest.mark.integration
def test_get_rag_chunks_multiple_messages_different_indices(api_base_url: str) -> None:
    """Test GET correctly identifies rag_chunks for different message indices."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create session with multiple messages, RAG at different indices
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Message 0 - no RAG"},
            {
                "role": "assistant",
                "content": "Response 1 - has RAG",
                "rag_chunks": [{"text": "Assistant RAG", "doc_id": "d1", "chunk_id": "c1", "score": 0.9}]
            },
            {"role": "user", "content": "Message 2 - no RAG"},
            {
                "role": "user",
                "content": "Message 3 - has RAG",
                "rag_chunks": [
                    {"text": "User RAG chunk 1", "doc_id": "d2", "chunk_id": "c2", "score": 0.85},
                    {"text": "User RAG chunk 2", "doc_id": "d3", "chunk_id": "c3", "score": 0.80}
                ]
            }
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Test each message
        resp0 = client.get(f"/api/v1/sessions/{session_name}/messages/0/rag")
        resp1 = client.get(f"/api/v1/sessions/{session_name}/messages/1/rag")
        resp2 = client.get(f"/api/v1/sessions/{session_name}/messages/2/rag")
        resp3 = client.get(f"/api/v1/sessions/{session_name}/messages/3/rag")

    # Verify all responses are successful
    assert resp0.status_code == 200
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 200

    # Verify message 0: no RAG
    data0 = resp0.json()
    assert data0["message_index"] == 0
    assert data0["has_rag"] is False
    assert len(data0["chunks"]) == 0

    # Verify message 1: has RAG (assistant)
    data1 = resp1.json()
    assert data1["message_index"] == 1
    assert data1["has_rag"] is True
    assert len(data1["chunks"]) == 1
    assert data1["chunks"][0]["text"] == "Assistant RAG"

    # Verify message 2: no RAG
    data2 = resp2.json()
    assert data2["message_index"] == 2
    assert data2["has_rag"] is False
    assert len(data2["chunks"]) == 0

    # Verify message 3: has RAG (user, multiple chunks)
    data3 = resp3.json()
    assert data3["message_index"] == 3
    assert data3["has_rag"] is True
    assert len(data3["chunks"]) == 2
    assert data3["chunks"][0]["text"] == "User RAG chunk 1"
    assert data3["chunks"][1]["text"] == "User RAG chunk 2"
