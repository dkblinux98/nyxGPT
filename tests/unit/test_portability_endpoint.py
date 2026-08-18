"""Tests for `GET /api/v1/ops/portability` (P6-16, #3516).

The matrix has nothing to *act* on -- it describes the product, not this
machine -- which is why #3803 removed the dashboard screen #3516 had added
and left this endpoint as the machine-readable counterpart of
`nyxgpt ops portability`. These tests pin that shape: the endpoint returns
the same payload the CLI renders, and there is no write route beside it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nyxgpt import portability
from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_endpoint_returns_the_same_matrix_the_cli_reports():
    response = TestClient(app).get("/api/v1/ops/portability")

    assert response.status_code == 200
    assert response.json() == portability.check_matrix()


def test_endpoint_payload_carries_the_whole_matrix():
    payload = TestClient(app).get("/api/v1/ops/portability").json()

    assert [t["key"] for t in payload["targets"]] == [t.key for t in portability.TARGETS]
    assert payload["summary"]["total"] == len(portability.TARGETS)
    assert payload["acceptance_sequence"]
    # The wrapped commands ship in the payload rather than being hand-typed
    # into a consumer, where they could drift from what the CLI accepts.
    assert payload["commands"]["report"] == "nyxgpt ops portability"
    for target in payload["targets"]:
        assert target["checks"]
        assert "acceptance_ready" in target


def test_endpoint_is_read_only():
    """Nothing here mutates state, so a POST must not exist to be found."""
    client = TestClient(app)

    assert client.post("/api/v1/ops/portability", json={}).status_code == 405


def test_endpoint_does_not_shell_out(monkeypatch):
    """It has to stay cheap enough for a caller to poll freely."""
    import subprocess

    def explode(*args, **kwargs):  # pragma: no cover - fails the test if reached
        raise AssertionError("the portability endpoint must not run subprocesses")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    assert TestClient(app).get("/api/v1/ops/portability").status_code == 200
