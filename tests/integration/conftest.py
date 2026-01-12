from __future__ import annotations

import os
import socket
from typing import Any, Generator
from urllib.parse import urlparse

import httpx
import pytest

from mygpt.config import load_config


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def cfg() -> Any:
    # Allow overriding config for CI / alt setups:
    #   export MYGPT_TEST_CONFIG=/path/to/config.ini
    path = os.environ.get("MYGPT_TEST_CONFIG")
    return load_config(path)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    return os.environ.get("MYGPT_TEST_API_BASE", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def require_ollama(cfg: Any) -> None:
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    u = urlparse(base_url)
    host = u.hostname or "127.0.0.1"
    port = int(u.port or 11434)

    if not _can_connect(host, port):
        pytest.skip(f"Ollama not reachable at {host}:{port}")


@pytest.fixture(scope="session")
def require_cassandra(cfg: Any) -> None:
    # Keep this tolerant: different keys possible over time.
    # Defaults assume local Docker Cassandra.
    host = (
            cfg.get("rag", "cassandra_host", fallback=None)
            or cfg.get("rag", "cassandra_contact_points", fallback="127.0.0.1").split(",")[0].strip()
            or "127.0.0.1"
    )
    port = int(cfg.get("rag", "cassandra_port", fallback="9042"))

    if not _can_connect(host, port, timeout=2.0):
        pytest.skip(f"Cassandra not reachable at {host}:{port}")


@pytest.fixture
def restore_config(api_base_url: str) -> Generator[dict[str, Any], None, None]:
    """
    Fixture that saves the current API config and restores it after the test.
    Guarantees cleanup even if the test fails, preventing test state pollution.

    Usage:
        def test_something(api_base_url: str, restore_config: dict[str, Any]):
            original = restore_config
            # modify config...
            # cleanup happens automatically
    """
    # Save original config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    r.raise_for_status()
    original = r.json()

    yield original

    # Restore original config (runs even if test fails)
    restore_payload = {
        "log_level": original["log_level"],
        "rag_enabled": original["rag_enabled"],
    }
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    r.raise_for_status()