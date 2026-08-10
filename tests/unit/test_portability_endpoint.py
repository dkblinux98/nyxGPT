"""Tests for `GET /api/v1/ops/portability` (P6-16, #3516).

CLAUDE.md's Definition of Done requires ops features to be operable from the
SRE/admin dashboard, not only the CLI. The portability matrix has nothing to
*act* on -- it describes the product, not this machine -- so the dashboard
half is a read-only surface, and these tests pin that: the endpoint returns
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


def test_endpoint_payload_carries_what_the_dashboard_renders():
    payload = TestClient(app).get("/api/v1/ops/portability").json()

    assert [t["key"] for t in payload["targets"]] == [t.key for t in portability.TARGETS]
    assert payload["summary"]["total"] == len(portability.TARGETS)
    assert payload["acceptance_sequence"]
    # Status-plus-CLI-pointers (the #3514 decision): the dashboard renders
    # wrapped commands, so they come from the backend rather than being
    # hand-typed into the page where they could drift.
    assert payload["commands"]["report"] == "nyxgpt ops portability"
    for target in payload["targets"]:
        assert target["checks"]
        assert "acceptance_ready" in target


def test_endpoint_is_read_only():
    """Nothing here mutates state, so a POST must not exist to be found."""
    client = TestClient(app)

    assert client.post("/api/v1/ops/portability", json={}).status_code == 405


def test_endpoint_does_not_shell_out(monkeypatch):
    """It has to be cheap enough for the dashboard to load on every visit."""
    import subprocess

    def explode(*args, **kwargs):  # pragma: no cover - fails the test if reached
        raise AssertionError("the portability endpoint must not run subprocesses")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    assert TestClient(app).get("/api/v1/ops/portability").status_code == 200
