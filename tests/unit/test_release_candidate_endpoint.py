"""Tests for `GET /api/v1/ops/release-candidate` (#3727).

CLAUDE.md's Definition of Done requires ops features to be reachable from
the SRE/admin dashboard, not only the CLI. Cutting an RC publishes to PyPI
with the owner's credentials, so the dashboard half is deliberately
read-only -- these tests pin that shape: the endpoint returns exactly what
the CLI plans, and there is no write route beside it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nyxgpt import release_candidate
from nyxgpt.app import app

pytestmark = pytest.mark.unit

PUBLISHED = ["2.1.0", "3.0.0rc1"]


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test reaches pypi.org."""
    monkeypatch.setattr(release_candidate, "fetch_published_versions", lambda *a, **k: PUBLISHED)


def test_endpoint_returns_the_plan_the_cli_reports():
    response = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0")

    assert response.status_code == 200
    assert response.json() == release_candidate.plan("v3.0.0", published=PUBLISHED)


def test_endpoint_payload_carries_what_the_dashboard_renders():
    payload = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0").json()

    assert payload["next_rc_version"] == "3.0.0rc2"
    assert payload["is_prerelease"] is True
    assert payload["publishable"] is True
    # Status-plus-CLI-pointers (#3514): the page renders wrapped commands
    # from the backend rather than hand-typed strings that could drift.
    assert payload["commands"]["publish"] == "nyxgpt release rc --publish"
    assert payload["commands"]["install"] == "pip install nyxgpt==3.0.0rc2"
    assert payload["guardrails"]


def test_endpoint_defaults_to_the_configured_release_branch(monkeypatch):
    monkeypatch.setattr(release_candidate, "default_branch", lambda: "v3.0.0")

    payload = TestClient(app).get("/api/v1/ops/release-candidate").json()

    assert payload["branch"] == "v3.0.0"


def test_endpoint_reports_a_blocked_branch_without_failing():
    """A non-release branch is a normal answer ("you cannot cut here"), not a 500."""
    response = TestClient(app).get("/api/v1/ops/release-candidate?branch=feat/x")

    assert response.status_code == 200
    assert response.json()["publishable"] is False
    assert response.json()["blockers"]


def test_endpoint_survives_an_unreachable_pypi(monkeypatch):
    def explode(*args, **kwargs):
        raise release_candidate.ReleaseCandidateError("Could not reach PyPI at ...: boom")

    monkeypatch.setattr(release_candidate, "fetch_published_versions", explode)
    payload = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0").json()

    assert payload["publishable"] is False
    assert "Could not reach PyPI" in payload["pypi_lookup_error"]


def test_endpoint_is_read_only():
    """Publishing carries PyPI credentials -- it is never a button in a browser."""
    client = TestClient(app)

    assert client.post("/api/v1/ops/release-candidate", json={}).status_code == 405


def test_endpoint_does_not_dispatch_anything(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - reaching it fails the test
        raise AssertionError("the endpoint must never dispatch the publish workflow")

    monkeypatch.setattr(release_candidate, "dispatch", explode)

    assert TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0").status_code == 200
