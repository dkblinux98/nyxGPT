"""Unit tests for the /api/v1/infra/* endpoints.

Exercises src/nyxgpt/app.py's `infra_status` route handler with nyxgpt.ops
mocked out, so no terraform/kubectl/docker is needed. Install/destroy are
`nyxgpt ops` CLI-only (see #3410) -- there is no mutating endpoint here to
test, and a regression test below confirms none is reachable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_infra_status_endpoint_returns_module_status():
    expected = {
        "mode": "none",
        "native": {},
        "compose": {},
        "conflicts": [],
        "terraform": {"probe_available": True, "deployed": False, "containers": {"api": "absent"}},
        "kubernetes": {
            "available": False,
            "probe_available": False,
            "deployed": False,
            "namespace": "nyxgpt",
            "pods": [],
        },
    }
    with patch("nyxgpt.app.ops_module.infra_status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/infra/status")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/infra/terraform/install"),
        ("POST", "/api/v1/infra/terraform/down"),
        ("POST", "/api/v1/infra/kubernetes/install"),
        ("POST", "/api/v1/infra/kubernetes/down"),
    ],
)
def test_infra_mutating_endpoints_are_retired(method, path):
    """Install/Destroy are CLI-only (`nyxgpt ops install|down`) -- see #3410.

    Not reachable from the web UI or its API: these routes must not exist.
    """
    client = TestClient(app)
    response = client.request(method, path)
    assert response.status_code == 404


def test_admin_overview_does_not_break_with_infra_endpoints_added():
    client = TestClient(app)
    response = client.get("/api/v1/admin/overview")
    assert response.status_code == 200
