"""Integration tests for SSE (Server-Sent Events) streaming."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


def test_sse_streaming_content_type(require_ollama):
    """Verify that streaming endpoint returns SSE content type."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-content-type",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200
        # Verify SSE content type
        content_type = response.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type
        assert "Cache-Control" in response.headers
        assert response.headers["Cache-Control"] == "no-cache"


def test_sse_event_format(require_ollama):
    """Verify that streaming responses contain properly formatted SSE events."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-format",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Collect all chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)

        # Verify we have SSE events (event: and data: fields)
        assert "event:" in full_response
        assert "data:" in full_response
        assert "id:" in full_response

        # Verify we have a heartbeat event
        assert "event: heartbeat" in full_response

        # Verify we have metadata event
        assert "event: metadata" in full_response

        # Verify we have text events (new structured format)
        assert "event: text" in full_response

        # Verify we have a done event
        assert "event: done" in full_response


def test_sse_heartbeat_event(require_ollama):
    """Verify that the first event is a heartbeat."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-heartbeat",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Read first event
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)
            # Check if we have a complete event (ends with \n\n)
            if "".join(chunks).count("\n\n") >= 1:
                break

        first_event = "".join(chunks).split("\n\n")[0]

        # Verify first event is heartbeat
        assert "event: heartbeat" in first_event
        assert "data:" in first_event

        # Parse the heartbeat data
        data_line = [line for line in first_event.split("\n") if line.startswith("data:")][0]
        data_json = data_line[5:].strip()  # Remove "data:" prefix
        heartbeat_data = json.loads(data_json)
        assert "timestamp" in heartbeat_data


def test_sse_message_events(require_ollama):
    """Verify that text events contain proper JSON data with structured fields."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-messages",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Collect all chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)
        events = full_response.split("\n\n")

        # Find text events (new structured format)
        text_events = [e for e in events if "event: text" in e]
        assert len(text_events) > 0, "Expected at least one text event"

        # Verify text event structure
        for event in text_events[:3]:  # Check first 3 text events
            lines = event.split("\n")
            event_type_line = [line for line in lines if line.startswith("event:")]
            data_line = [line for line in lines if line.startswith("data:")]
            id_line = [line for line in lines if line.startswith("id:")]

            assert len(event_type_line) > 0, "Expected event: field"
            assert len(data_line) > 0, "Expected data: field"
            assert len(id_line) > 0, "Expected id: field"

            # Parse data as JSON
            data_json = data_line[0][5:].strip()
            message_data = json.loads(data_json)
            assert "content" in message_data
            assert "tokens" in message_data  # New: token count
            assert "elapsed" in message_data  # New: elapsed time


def test_sse_done_event(require_ollama):
    """Verify that stream ends with a done event containing performance data."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-done",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Collect all chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)
        events = full_response.split("\n\n")

        # Find done event
        done_events = [e for e in events if "event: done" in e]
        assert len(done_events) >= 1, "Expected at least one done event"

        # Verify done event is last meaningful event
        last_event = [e for e in events if e.strip()][-1]
        assert "event: done" in last_event

        # Parse done event data
        lines = last_event.split("\n")
        data_line = [line for line in lines if line.startswith("data:")][0]
        data_json = data_line[5:].strip()
        done_data = json.loads(data_json)
        assert "event_id" in done_data
        assert "total_tokens" in done_data  # New: total token count
        assert "elapsed" in done_data  # New: total elapsed time


def test_sse_metadata_event(require_ollama):
    """Verify that streaming includes metadata event with session and model info."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-metadata",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Collect all chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)
        events = full_response.split("\n\n")

        # Find metadata event
        metadata_events = [e for e in events if "event: metadata" in e]
        assert len(metadata_events) >= 1, "Expected at least one metadata event"

        # Parse metadata event
        metadata_event = metadata_events[0]
        lines = metadata_event.split("\n")
        data_line = [line for line in lines if line.startswith("data:")][0]
        data_json = data_line[5:].strip()
        metadata = json.loads(data_json)

        assert "session" in metadata
        assert "model" in metadata
        assert "timestamp" in metadata
        assert metadata["session"] == "test-sse-metadata"
        assert metadata["model"] == "llama3.1:8b"


def test_sse_event_ids_incremental(require_ollama):
    """Verify that event IDs are incremental."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-event-ids",
            "model": "llama3.1:8b",
        },
    ) as response:
        assert response.status_code == 200

        # Collect all chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)
        events = full_response.split("\n\n")

        # Extract event IDs
        event_ids = []
        for event in events:
            if not event.strip():
                continue
            id_lines = [line for line in event.split("\n") if line.startswith("id:")]
            if id_lines:
                event_id = int(id_lines[0][3:].strip())
                event_ids.append(event_id)

        # Verify IDs are incremental
        assert len(event_ids) > 0
        for i in range(1, len(event_ids)):
            assert event_ids[i] > event_ids[i - 1], f"Event IDs should be incremental: {event_ids}"
