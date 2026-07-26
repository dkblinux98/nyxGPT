"""Method-correctness regression tests for Ollama HTTP calls (issue #3359).

`nyxgpt models list` sent a POST to Ollama's `/api/tags`, which is a GET-only
endpoint -- current Ollama versions enforce this and return a 405. These
tests mock at the `urllib.request.urlopen` layer (not by mocking
`ollama_client`'s own functions) and assert the actual HTTP method sent for
each `nyxgpt.models` endpoint, so a future regression that uses the wrong
verb fails here instead of surfacing as a runtime 405.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nyxgpt import models

pytestmark = pytest.mark.unit

BASE_URL = "http://localhost:11434"


def _mock_response(body: bytes = b"{}") -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    resp.read.return_value = body
    return resp


def test_list_models_sends_get_to_api_tags():
    with patch(
        "urllib.request.urlopen", return_value=_mock_response(b'{"models": []}')
    ) as mock_urlopen:
        models.list_models(base_url=BASE_URL)

    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE_URL}/api/tags"


def test_pull_model_sends_post_to_api_pull():
    with patch(
        "urllib.request.urlopen", return_value=_mock_response(b'{"status": "success"}')
    ) as mock_urlopen:
        models.pull_model("llama3.1:8b", base_url=BASE_URL)

    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "POST"
    assert req.full_url == f"{BASE_URL}/api/pull"


def test_delete_model_sends_delete_to_api_delete():
    with patch("urllib.request.urlopen", return_value=_mock_response(b"{}")) as mock_urlopen:
        models.delete_model("llama3.1:8b", base_url=BASE_URL)

    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "DELETE"
    assert req.full_url == f"{BASE_URL}/api/delete"


def test_show_model_sends_post_to_api_show():
    with patch(
        "urllib.request.urlopen", return_value=_mock_response(b'{"modelfile": "FROM x"}')
    ) as mock_urlopen:
        models.show_model("llama3.1:8b", base_url=BASE_URL)

    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "POST"
    assert req.full_url == f"{BASE_URL}/api/show"


def test_app_models_list_endpoint_sends_get_to_api_tags():
    """The web API path (`/api/v1/models`) must use the same GET verb as the
    CLI now that both share `nyxgpt.ollama_client.get_json`."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    with (
        patch(
            "urllib.request.urlopen", return_value=_mock_response(b'{"models": []}')
        ) as mock_urlopen,
        patch("nyxgpt.app.get_ollama_base_url", return_value=BASE_URL),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE_URL}/api/tags"
