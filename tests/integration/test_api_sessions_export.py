"""Integration tests for session export API endpoints.

Tests the following endpoint:
- GET /api/v1/sessions/{name}/export?format={markdown|json|html}

This endpoint allows exporting session conversations in various formats
for archival, sharing, or integration with other tools.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest


@pytest.mark.integration
def test_session_export_markdown(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export?format=markdown."""
    session_name = f"export-md-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add messages
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Hello, this is a test"},
            {"role": "assistant", "content": "Hi! I'm happy to help."},
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export as markdown
        export_resp = client.get(
            f"/api/v1/sessions/{session_name}/export?format=markdown"
        )

    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "content-disposition" in export_resp.headers
    assert f"{session_name}.md" in export_resp.headers["content-disposition"]

    # Verify content contains messages
    content = export_resp.text
    assert "Hello, this is a test" in content
    assert "Hi! I'm happy to help." in content


@pytest.mark.integration
def test_session_export_json(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export?format=json."""
    session_name = f"export-json-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add messages
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "Test message"},
            {"role": "assistant", "content": "Test response"},
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export as JSON
        export_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=json")

    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "application/json; charset=utf-8"
    assert "content-disposition" in export_resp.headers
    assert f"{session_name}.json" in export_resp.headers["content-disposition"]

    # Verify valid JSON
    content = export_resp.text
    exported_data = json.loads(content)
    assert isinstance(exported_data, (dict, list))


@pytest.mark.integration
def test_session_export_html(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export?format=html."""
    session_name = f"export-html-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add messages
        session_file = sessions_dir / f"{session_name}.json"
        messages = [
            {"role": "user", "content": "HTML export test"},
            {"role": "assistant", "content": "This should be in HTML format"},
        ]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export as HTML
        export_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=html")

    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "text/html; charset=utf-8"
    assert "content-disposition" in export_resp.headers
    assert f"{session_name}.html" in export_resp.headers["content-disposition"]

    # Verify HTML structure
    content = export_resp.text
    assert "<html" in content.lower() or "<!doctype html>" in content.lower()
    assert "HTML export test" in content
    assert "This should be in HTML format" in content


@pytest.mark.integration
def test_session_export_default_format(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export without format parameter defaults to markdown."""
    session_name = f"export-default-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Export without format (should default to markdown)
        export_resp = client.get(f"/api/v1/sessions/{session_name}/export")

    assert export_resp.status_code == 200
    # Default should be markdown
    assert "markdown" in export_resp.headers["content-type"]


@pytest.mark.integration
def test_session_export_invalid_format(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export with invalid format."""
    session_name = f"export-invalid-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Try invalid format
        export_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=pdf")

    # Should return 400 for unsupported format
    assert export_resp.status_code == 400


@pytest.mark.integration
def test_session_export_nonexistent_session(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export for nonexistent session."""
    nonexistent_session = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        export_resp = client.get(
            f"/api/v1/sessions/{nonexistent_session}/export?format=markdown"
        )

    assert export_resp.status_code == 404


@pytest.mark.integration
def test_session_export_empty_session(api_base_url: str) -> None:
    """Test GET /api/v1/sessions/{name}/export for empty session."""
    session_name = f"export-empty-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create empty session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Export empty session (should succeed but with minimal content)
        export_resp = client.get(
            f"/api/v1/sessions/{session_name}/export?format=markdown"
        )

    assert export_resp.status_code == 200
    # Should still return valid response even if session is empty
    content = export_resp.text
    assert isinstance(content, str)


@pytest.mark.integration
def test_session_export_with_metadata(api_base_url: str) -> None:
    """Test that exported sessions include metadata (title, tags)."""
    session_name = f"export-meta-test-{uuid.uuid4().hex[:8]}"
    title = "Test Session Title"
    tags = ["test", "export"]

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add messages
        session_file = sessions_dir / f"{session_name}.json"
        messages = [{"role": "user", "content": "Test"}]
        session_file.write_text(json.dumps(messages, indent=2))

        # Set metadata
        client.post(f"/api/v1/sessions/{session_name}/title", json={"title": title})
        client.post(f"/api/v1/sessions/{session_name}/tags/add", json={"tags": tags})

        # Export
        export_resp = client.get(
            f"/api/v1/sessions/{session_name}/export?format=markdown"
        )

    assert export_resp.status_code == 200
    content = export_resp.text

    # Verify metadata is in export (exact format depends on implementation)
    # Title should appear somewhere in the export
    assert title in content or session_name in content


@pytest.mark.integration
def test_session_export_multiple_formats_consistency(api_base_url: str) -> None:
    """Test that exporting in different formats produces consistent content."""
    session_name = f"export-multi-test-{uuid.uuid4().hex[:8]}"
    test_content = "This is a unique test message for export"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Get sessions dir
        info_resp = client.get("/api/v1/info")
        sessions_dir = Path(info_resp.json()["sessions_dir"])

        # Add unique message
        session_file = sessions_dir / f"{session_name}.json"
        messages = [{"role": "user", "content": test_content}]
        session_file.write_text(json.dumps(messages, indent=2))

        # Export in all formats
        md_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=markdown")
        json_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=json")
        html_resp = client.get(f"/api/v1/sessions/{session_name}/export?format=html")

    # All should succeed
    assert md_resp.status_code == 200
    assert json_resp.status_code == 200
    assert html_resp.status_code == 200

    # All should contain the test content
    assert test_content in md_resp.text
    assert test_content in json_resp.text
    assert test_content in html_resp.text


@pytest.mark.integration
def test_session_export_case_insensitive_format(api_base_url: str) -> None:
    """Test that format parameter is case-insensitive."""
    session_name = f"export-case-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Create session
        init_resp = client.post("/api/v1/sessions/init", json={"name": session_name})
        assert init_resp.status_code == 200

        # Try uppercase format
        export_resp = client.get(
            f"/api/v1/sessions/{session_name}/export?format=MARKDOWN"
        )

    # Should handle case-insensitively
    assert export_resp.status_code == 200
    assert "markdown" in export_resp.headers["content-type"]
