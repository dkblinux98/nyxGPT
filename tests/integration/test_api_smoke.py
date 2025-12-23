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