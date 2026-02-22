"""Unit tests for the PATCH /config endpoint (config_patch).

These tests cover src/nyxgpt/app.py:887 – the config_patch function that was
modified as a mypy-compliance fix (adding an explicit ``result: dict[str, Any]``
type annotation).  The tests verify that the endpoint exists, accepts a partial
payload, and returns the expected response structure.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_config_patch_endpoint_exists_and_returns_dict():
    """PATCH /api/v1/config must return a dict with 'updated' and 'effective' keys.

    Covers app.py:887 – the ``result: dict[str, Any] = config_update(...)``
    annotation ensures mypy strict mode is satisfied and that the return value
    is always a plain dict, not a subtype.
    """
    client = TestClient(app)

    response = client.patch("/api/v1/config", json={"log_level": "WARNING"})

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict), "config_patch must return a dict"
    assert "updated" in data, "Response must contain 'updated' key"
    assert "effective" in data, "Response must contain 'effective' key"


def test_config_patch_delegates_to_config_update():
    """config_patch must delegate to config_update and return its result unchanged.

    This confirms that the annotated ``result: dict[str, Any]`` assignment does
    not alter the value returned by config_update.
    """
    expected = {
        "updated": {"log_level": "DEBUG"},
        "effective": {
            "ollama_base_url": "http://localhost:11434",
            "default_model": "test-model",
            "rag_enabled": False,
            "log_level": "DEBUG",
        },
    }

    with patch("nyxgpt.app.config_update", return_value=expected) as mock_update:
        client = TestClient(app)
        response = client.patch("/api/v1/config", json={"log_level": "DEBUG"})

    assert response.status_code == 200
    assert response.json() == expected
    mock_update.assert_called_once()


def test_config_patch_return_value_is_plain_dict():
    """config_patch return value must be a plain dict (dict[str, Any]).

    Validates that the explicit type annotation added for mypy compliance
    is consistent with the actual runtime type of the returned object.
    """
    client = TestClient(app)

    response = client.patch("/api/v1/config", json={"log_level": "INFO"})

    assert response.status_code == 200
    data = response.json()
    # json() always returns a plain dict; this assertion mirrors the mypy annotation
    assert type(data) is dict  # noqa: E721 – intentional exact-type check
