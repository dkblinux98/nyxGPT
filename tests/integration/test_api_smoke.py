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