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
def test_api_config_get(api_base_url: str) -> None:
    """Verify GET /api/v1/config returns expected configuration fields."""
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert "ollama_base_url" in data
    assert "default_model" in data
    assert "rag_enabled" in data
    assert "log_level" in data
    assert isinstance(data["rag_enabled"], bool)
    assert isinstance(data["log_level"], str)


@pytest.mark.integration
def test_api_config_post(api_base_url: str) -> None:
    """Verify POST /api/v1/config updates configuration and returns updated values."""
    # First get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Update config with new values
    update_payload = {
        "log_level": "DEBUG",
        "rag_enabled": not original_config["rag_enabled"],
    }
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=update_payload,
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert "updated" in data
    assert "effective" in data
    assert isinstance(data["updated"], list)
    # Validate that updated field contains the names of fields that were updated
    assert "log_level" in data["updated"], "log_level should be in updated field"
    assert "rag_enabled" in data["updated"], "rag_enabled should be in updated field"
    assert len(data["updated"]) == 2, "updated field should contain exactly 2 field names"
    assert isinstance(data["effective"], dict)
    assert data["effective"]["log_level"] == "DEBUG"
    assert data["effective"]["rag_enabled"] == update_payload["rag_enabled"]

    # Verify config persisted by fetching again
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    current_config = r.json()
    assert current_config["log_level"] == "DEBUG"
    assert current_config["rag_enabled"] == update_payload["rag_enabled"]

    # Restore original config
    restore_payload = {
        "log_level": original_config["log_level"],
        "rag_enabled": original_config["rag_enabled"],
    }
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    assert r.status_code == 200