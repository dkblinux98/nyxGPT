"""Regression tests for #3183: the admin health/overview/config endpoints
must agree with the chat/session runtime about whether RAG is enabled.

Previously the health/overview/config endpoints read `[rag] enabled` while
the chat runtime read `[rag] enable_chat_context`, so a Compose deployment
with only `enable_chat_context = true` set (the documented, canonical key)
showed `rag_enabled: false` on the dashboard -- and reported Cassandra as
"not required" -- even though RAG was live and Cassandra was in use.
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import _chat_runtime_defaults, app
from nyxgpt.config import get_rag_enabled
from nyxgpt.health import DependencyCheck

pytestmark = pytest.mark.unit


def _cfg_with_enable_chat_context(value: bool) -> ConfigParser:
    cfg = ConfigParser()
    cfg.read_string(f"[rag]\nenable_chat_context = {value}\n")
    return cfg


@pytest.mark.parametrize("enabled", [True, False])
def test_health_overview_and_config_agree_with_chat_runtime(enabled: bool) -> None:
    cfg = _cfg_with_enable_chat_context(enabled)

    # What the chat runtime actually uses to decide whether to inject RAG context.
    runtime_rag_enabled = _chat_runtime_defaults(cfg)["rag_enabled"]
    assert runtime_rag_enabled is enabled
    assert get_rag_enabled(cfg) is enabled

    cassandra_check = DependencyCheck(
        name="cassandra",
        ok=True,
        detail="Connected" if enabled else "RAG disabled; Cassandra is not required",
        applicable=enabled,
    )

    with (
        patch("nyxgpt.app.load_config", return_value=cfg),
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
        patch(
            "nyxgpt.app.health_module.check_ollama",
            return_value=DependencyCheck(name="ollama", ok=True, detail="ok"),
        ),
        patch(
            "nyxgpt.app.health_module.check_cassandra", return_value=cassandra_check
        ) as mock_check_cassandra,
        patch("nyxgpt.app.canary_module.status", return_value={}),
    ):
        client = TestClient(app)
        health_resp = client.get("/api/v1/admin/health")
        overview_resp = client.get("/api/v1/admin/overview")
        config_resp = client.get("/api/v1/config")

    assert health_resp.status_code == 200
    assert overview_resp.status_code == 200
    assert config_resp.status_code == 200

    # The health dependency check must be told the same rag_enabled value
    # the chat runtime uses, so Cassandra is checked/required exactly when
    # RAG is actually live.
    mock_check_cassandra.assert_called_with(enabled)

    assert overview_resp.json()["info"]["rag_enabled"] == runtime_rag_enabled
    assert config_resp.json()["rag_enabled"] == runtime_rag_enabled
