"""Tests for `GET /api/v1/ops/release-candidate` (#3727).

CLAUDE.md's Definition of Done requires ops features to be reachable from
the SRE/admin dashboard, not only the CLI. Publishing to PyPI carries the
owner's credentials, so the dashboard half is deliberately read-only --
these tests pin that shape: the endpoint returns exactly what the CLI plans
for either channel, and there is no write route beside it.
"""

from __future__ import annotations

import logging

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
    assert payload["commands"]["publish"] == "nyxgpt release publish --channel rc --publish"
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
    assert payload["pypi_lookup_error"]


def test_endpoint_does_not_serve_the_transport_error(monkeypatch):
    """#3837 (CodeQL #123): the 200 response is the flow that stayed open.

    The sink CodeQL names is the `return release_candidate_module.plan(...)`
    on the success path, not the `except` branch PR #3899 redacted -- an
    unreachable PyPI is answered 200 by design (the dashboard still renders
    the line's state), so the leak travelled on the ordinary response.
    """
    host_state = "proxy.corp.internal:3128"

    def explode(*args, **kwargs):
        raise release_candidate.ReleaseCandidateError(
            f"Could not reach PyPI at https://pypi.org/pypi/nyxgpt/json: "
            f"ProxyError connecting via {host_state}"
        )

    monkeypatch.setattr(release_candidate, "fetch_published_versions", explode)
    response = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0")

    assert response.status_code == 200
    assert host_state not in response.text
    # The failure is still reported -- redacting it into silence would leave
    # the dashboard claiming an unknown next version with no reason given.
    payload = response.json()
    assert payload["publishable"] is False
    assert payload["pypi_lookup_error"]
    assert any("is unknown" in blocker for blocker in payload["blockers"])


def test_endpoint_is_read_only():
    """Publishing carries PyPI credentials -- it is never a button in a browser."""
    client = TestClient(app)

    assert client.post("/api/v1/ops/release-candidate", json={}).status_code == 405


def test_endpoint_does_not_dispatch_anything(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - reaching it fails the test
        raise AssertionError("the endpoint must never dispatch the publish workflow")

    monkeypatch.setattr(release_candidate, "dispatch", explode)

    assert TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0").status_code == 200


def test_endpoint_reports_the_stable_channel_the_ceremony_publishes():
    """The release channel has to be inspectable from the dashboard too."""
    payload = (
        TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0&channel=stable").json()
    )

    assert payload["channel"] == "stable"
    assert payload["is_prerelease"] is False
    assert payload["commands"]["publish"] == "scripts/release_ceremony.sh"


def test_endpoint_names_the_tap_formulas_for_the_line():
    """The panel renders the `brew install` line, so the API has to carry it (#3735)."""
    payload = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0").json()

    assert payload["rc_formulas"] == ["nyxgpt-api@3.0.0rc", "nyxgpt-web@3.0.0rc"]
    assert payload["commands"]["brew"].endswith(
        "brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc"
    )


@pytest.mark.parametrize("channel", ["dev", "nightly"])
def test_endpoint_rejects_the_retired_dev_channel(channel):
    """#3735 retired the nightly -- asking for it is a 400, not a plan."""
    response = TestClient(app).get(f"/api/v1/ops/release-candidate?branch=v3.0.0&channel={channel}")

    assert response.status_code == 400


def test_endpoint_rejects_an_unknown_channel():
    """A typo is the caller's error (400), not a backend failure (502)."""
    response = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0&channel=nightly")

    assert response.status_code == 400
    assert "Unknown channel" in response.json()["error"]["message"]


# --- Error detail must not carry host state (#3837, CodeQL #123) --------------


def test_502_detail_does_not_leak_the_host_filesystem_path(monkeypatch, caplog):
    """A `ReleaseCandidateError` message is not automatically client-safe.

    `plan` reaches `declared_version`, which raises
    `ReleaseCandidateError(f"Cannot read {path}: {exc}")` from a caught
    `OSError` -- so returning `str(e)` verbatim publishes the API host's
    absolute filesystem path (and the OS error string) to any dashboard
    viewer. The operator still needs the real message, so it goes to the
    API log instead.
    """
    leaky = "Cannot read /srv/deploy/nyxGPT/pyproject.toml: [Errno 13] Permission denied"

    def explode(*args, **kwargs):
        raise release_candidate.ReleaseCandidateError(leaky)

    monkeypatch.setattr(release_candidate, "plan", explode)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.api"):
        response = TestClient(app).get("/api/v1/ops/release-candidate?branch=v3.0.0")

    assert response.status_code == 502
    detail = response.json()["error"]["message"]
    assert "/srv/deploy" not in detail
    assert "Errno 13" not in detail
    # It still has to be actionable: name the operation and where to look.
    assert "v3.0.0" in detail
    assert "nyxgpt release plan" in detail

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert leaky in logged, "the operator's own logs must still carry the real error"
