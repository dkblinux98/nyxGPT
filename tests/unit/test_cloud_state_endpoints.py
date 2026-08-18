"""Unit tests for the /api/v1/cloud/state endpoint (P6-9, #3510; #3804).

These exercise src/nyxgpt/app.py's cloud_state route handler with
nyxgpt.cloud_state mocked out, so no Terraform binary and no AWS account are
needed.

The state *actions* -- migrate, unlock, list versions, restore -- used to be
dashboard controls here. The owner removed them (2026-08-16, #3804): they
rewrite the record of the substrate the dashboard itself may be running on,
which is exactly the operation a self-hosted UI cannot safely offer. They live
in `nyxgpt cloud state` and nowhere else, so the tests below pin their
*absence* as deliberately as the surviving read is pinned present.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_status_endpoint_is_offline_by_default():
    expected = {"backend": "local", "remote_enabled": False, "verified": False}

    with patch("nyxgpt.app.cloud_state_module.state_status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once_with(verify=False)


def test_status_endpoint_forwards_the_verify_flag():
    with patch("nyxgpt.app.cloud_state_module.state_status", return_value={}) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state?verify=true")

    assert response.status_code == 200
    mock_status.assert_called_once_with(verify=True)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/cloud/state/migrate"),
        ("post", "/api/v1/cloud/state/unlock"),
        ("post", "/api/v1/cloud/state/restore"),
        ("get", "/api/v1/cloud/state/versions"),
    ],
)
def test_state_actions_are_not_reachable_over_http(method: str, path: str):
    """No HTTP path rewrites Terraform state -- `nyxgpt cloud state` owns it (#3804).

    Asserted route by route rather than by "the page has no button": a button
    is a UI detail, whereas the endpoint being gone is what makes the guarantee
    hold for anything that can reach the API.
    """
    client = TestClient(app)
    response = client.post(path, json={}) if method == "post" else client.get(path)

    assert response.status_code == 404


def test_no_state_route_survives_under_the_cloud_state_prefix():
    """Belt and braces: the only /cloud/state* route left is the status read."""
    paths = {path for path in app.openapi()["paths"] if path.startswith("/api/v1/cloud/state")}

    assert paths == {"/api/v1/cloud/state"}
