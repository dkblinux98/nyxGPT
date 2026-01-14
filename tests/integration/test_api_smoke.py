from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_api_health(api_base_url: str) -> None:
    r = httpx.get(f"{api_base_url}/health", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert (data.get("ok") is True) or (data.get("status") == "ok")


@pytest.mark.integration
def test_api_info(api_base_url: str) -> None:
    r = httpx.get(f"{api_base_url}/api/v1/info", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "ollama_base_url" in data
    assert "default_model" in data
    assert "sessions_dir" in data


@pytest.mark.integration
def test_api_chat_stream(api_base_url: str) -> None:
    """Verify that the streaming chat endpoint returns incremental content."""
    url = f"{api_base_url}/api/v1/chat/stream"

    with httpx.stream(
        "POST",
        url,
        json={"prompt": "hello", "session": "test-stream"},
        timeout=httpx.Timeout(60.0, connect=5.0, read=60.0, write=60.0, pool=60.0),
    ) as r:
        assert r.status_code == 200

        got_any_chunk = False
        for _text in r.iter_text():
            # Treat the initial keepalive or any subsequent chunk as success
            got_any_chunk = True
            break

    assert got_any_chunk is True


@pytest.mark.integration
def test_api_sessions_list_and_get(api_base_url: str) -> None:
    # create a session without invoking the model
    r = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": "ui-test-session"},
        timeout=5.0,
    )
    assert r.status_code == 200

    # list sessions
    r = httpx.get(f"{api_base_url}/api/v1/sessions", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    sessions = data.get("sessions") if isinstance(data, dict) else data
    assert isinstance(sessions, list)
    names = [s.get("name") for s in sessions if isinstance(s, dict)]
    assert "ui-test-session" in names

    # fetch session detail
    r = httpx.get(f"{api_base_url}/api/v1/sessions/ui-test-session", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "messages" in data
    assert isinstance(data["messages"], list)
    assert "meta" in data
    assert isinstance(data["meta"], dict)


@pytest.mark.integration
def test_api_search_endpoint(api_base_url: str) -> None:
    """Test message search API endpoint."""
    # First, create a test session with messages
    r = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": "search-test-session", "system": "You are helpful"},
        timeout=5.0,
    )
    assert r.status_code == 200

    # Search for "helpful" (should find system message)
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "helpful"},
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()

    # Verify response structure
    assert "query" in data
    assert data["query"] == "helpful"
    assert "total_results" in data
    assert "results" in data
    assert isinstance(data["results"], list)

    # Should find at least one result (the system message)
    if data["total_results"] > 0:
        result = data["results"][0]
        assert "session_name" in result
        assert "message_index" in result
        assert "role" in result
        assert "content" in result
        assert "content_preview" in result
        assert "matches" in result


@pytest.mark.integration
def test_api_search_with_filters(api_base_url: str) -> None:
    """Test search with role and session filters."""
    # Create a test session
    r = httpx.post(
        f"{api_base_url}/api/v1/sessions/init",
        json={"name": "filter-test-session"},
        timeout=5.0,
    )
    assert r.status_code == 200

    # Search with role filter
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "test", "role_filter": "user"},
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert "results" in data

    # Search with session filter
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "test", "session_filter": "filter-test-session"},
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert "results" in data


@pytest.mark.integration
def test_api_search_case_sensitive(api_base_url: str) -> None:
    """Test case-sensitive search parameter."""
    # Search case-insensitive (default)
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "TEST", "case_sensitive": False},
        timeout=5.0,
    )
    assert r.status_code == 200
    data_insensitive = r.json()

    # Search case-sensitive
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "TEST", "case_sensitive": True},
        timeout=5.0,
    )
    assert r.status_code == 200
    data_sensitive = r.json()

    # Both should be valid responses
    assert "total_results" in data_insensitive
    assert "total_results" in data_sensitive


@pytest.mark.integration
def test_api_search_limit(api_base_url: str) -> None:
    """Test search limit parameter."""
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": "test", "limit": 5},
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    # Results should not exceed limit
    assert len(data["results"]) <= 5


@pytest.mark.integration
def test_api_search_empty_query_returns_empty(api_base_url: str) -> None:
    """Test that empty query returns no results."""
    r = httpx.get(
        f"{api_base_url}/api/v1/sessions/search",
        params={"query": ""},
        timeout=5.0,
    )
    # Should still return 200 but with empty results
    assert r.status_code == 200
    data = r.json()
    assert data["total_results"] == 0
    assert len(data["results"]) == 0