"""Integration tests for citations export endpoint (#2671).

Tests the GET /api/v1/sessions/{name}/citations/export endpoint
which exports all RAG citations from a session.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest


@pytest.mark.integration
def test_export_citations_nonexistent_session(api_base_url: str) -> None:
    """Test export citations returns 404 for nonexistent session."""
    nonexistent_session = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        resp = client.get(
            f"/api/v1/sessions/{nonexistent_session}/citations/export"
        )

    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert "message" in data["error"]
    assert nonexistent_session in data["error"]["message"]


@pytest.mark.integration
def test_export_citations_invalid_format(api_base_url: str) -> None:
    """Test export citations returns 400 for invalid format."""
    session_name = f"cite-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Try invalid format
        resp = client.get(
            f"/api/v1/sessions/{session_name}/citations/export?format=xml"
        )

    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "Invalid format" in data["error"]["message"]


@pytest.mark.integration
def test_export_citations_json_format(api_base_url: str) -> None:
    """Test export citations in JSON format."""
    session_name = f"cite-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create session with citations
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Question 1"},
            {
                "role": "assistant",
                "content": "Answer 1",
                "rag_chunks": [
                    {
                        "text": "This is citation 1",
                        "doc_id": "doc-1",
                        "chunk_id": 1,
                        "score": 0.95,
                        "similarity_score": 0.95,
                    },
                    {
                        "text": "This is citation 2",
                        "doc_id": "doc-2",
                        "chunk_id": 2,
                        "score": 0.87,
                        "similarity_score": 0.87,
                    },
                ],
            },
            {"role": "user", "content": "Question 2"},
            {
                "role": "assistant",
                "content": "Answer 2",
                "rag_chunks": [
                    {
                        "text": "This is citation 3",
                        "doc_id": "doc-3",
                        "chunk_id": 3,
                        "score": 0.92,
                        "similarity_score": 0.92,
                    }
                ],
            },
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export citations in JSON format
        resp = client.get(
            f"/api/v1/sessions/{session_name}/citations/export?format=json"
        )

    assert resp.status_code == 200
    data = resp.json()

    # Verify response structure
    assert "session" in data
    assert "total_citations" in data
    assert "citations" in data

    # Verify values
    assert data["session"] == session_name
    assert data["total_citations"] == 3
    assert isinstance(data["citations"], list)
    assert len(data["citations"]) == 3

    # Verify first citation
    cite1 = data["citations"][0]
    assert cite1["message_index"] == 1
    assert cite1["citation_index"] == 0
    assert cite1["doc_id"] == "doc-1"
    assert cite1["chunk_id"] == 1
    assert cite1["text"] == "This is citation 1"
    assert cite1["score"] == 0.95
    assert cite1["similarity_score"] == 0.95

    # Verify second citation
    cite2 = data["citations"][1]
    assert cite2["message_index"] == 1
    assert cite2["citation_index"] == 1
    assert cite2["doc_id"] == "doc-2"

    # Verify third citation (from different message)
    cite3 = data["citations"][2]
    assert cite3["message_index"] == 3
    assert cite3["citation_index"] == 0
    assert cite3["doc_id"] == "doc-3"


@pytest.mark.integration
def test_export_citations_markdown_format(api_base_url: str) -> None:
    """Test export citations in Markdown format."""
    session_name = f"cite-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create session with citations
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Question"},
            {
                "role": "assistant",
                "content": "Answer",
                "rag_chunks": [
                    {
                        "text": "Citation text content",
                        "doc_id": "doc-1",
                        "chunk_id": 1,
                        "score": 0.88,
                        "similarity_score": 0.88,
                    }
                ],
            },
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export citations in Markdown format
        resp = client.get(
            f"/api/v1/sessions/{session_name}/citations/export?format=markdown"
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "Content-Disposition" in resp.headers
    assert f"{session_name}-citations.md" in resp.headers["Content-Disposition"]

    content = resp.text

    # Verify markdown content (doc_id hyphens are escaped for markdown safety)
    assert f"# Citations for {session_name}" in content
    assert "Total sources: 1" in content
    assert "[1] doc\\-1 (chunk 1)" in content  # Hyphen is escaped in markdown
    assert "**Confidence:** 0.880" in content
    assert "**Message:** 1" in content
    assert "Citation text content" in content


@pytest.mark.integration
def test_export_citations_no_citations(api_base_url: str) -> None:
    """Test export citations when session has no RAG citations."""
    session_name = f"cite-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create session WITHOUT citations
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer without RAG"},
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export citations
        resp = client.get(
            f"/api/v1/sessions/{session_name}/citations/export?format=json"
        )

    assert resp.status_code == 200
    data = resp.json()

    # Verify empty citations
    assert data["session"] == session_name
    assert data["total_citations"] == 0
    assert len(data["citations"]) == 0


@pytest.mark.integration
def test_export_citations_only_assistant_messages(api_base_url: str) -> None:
    """Test that citations are only extracted from assistant messages."""
    session_name = f"cite-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Initialize session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Create session with citations in both user and assistant messages
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {
                "role": "user",
                "content": "Question",
                # User message with rag_chunks (should be included in export)
                "rag_chunks": [
                    {
                        "text": "User RAG chunk (should not appear)",
                        "doc_id": "user-doc",
                        "chunk_id": 99,
                        "score": 0.99,
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Answer",
                "rag_chunks": [
                    {
                        "text": "Assistant RAG chunk",
                        "doc_id": "assistant-doc",
                        "chunk_id": 1,
                        "score": 0.95,
                    }
                ],
            },
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export citations
        resp = client.get(
            f"/api/v1/sessions/{session_name}/citations/export?format=json"
        )

    assert resp.status_code == 200
    data = resp.json()

    # Verify only assistant citations are included
    assert data["total_citations"] == 1
    assert data["citations"][0]["doc_id"] == "assistant-doc"
    assert data["citations"][0]["message_index"] == 1  # Assistant message index
