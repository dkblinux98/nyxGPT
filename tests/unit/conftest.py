"""Unit-test configuration and fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_cassandra_pool():
    """Reset the module-level Cassandra connection pool before and after each test.

    Without this, a pool created in one test would be reused in the next test,
    causing stale mocks and unexpected connection attempts.
    """
    import contextlib

    from nyxgpt.rag.vectorstore_cassandra import reset_connection_pool

    with contextlib.suppress(Exception):
        reset_connection_pool()

    yield

    with contextlib.suppress(Exception):
        reset_connection_pool()


@pytest.fixture(autouse=True)
def _reset_query_result_cache():
    """Reset the module-level RAG query result cache before and after each test.

    Without this, a cache backend (or disabled NoOpCache) created in one test
    would be reused in later tests, causing stale hits/misses depending on
    test execution order.
    """
    import nyxgpt.rag.rag as rag_module

    rag_module._query_result_cache = None
    yield
    rag_module._query_result_cache = None


@pytest.fixture(autouse=True)
def _reset_config_fallback_warnings():
    """Reset the config module's once-per-key fallback warning dedup set.

    `config._log_fallback_once` only logs a given key's fallback once per
    process, so without this reset a test asserting a fallback WARNING would
    pass or fail depending on whether an earlier test already triggered the
    same key's fallback.
    """
    from nyxgpt.config import reset_fallback_warnings

    reset_fallback_warnings()
    yield
    reset_fallback_warnings()


@pytest.fixture(autouse=True)
def _isolate_install_mode_marker(monkeypatch, tmp_path):
    """Redirect the install-mode marker into `tmp_path` for every unit test (#3789).

    `install_mode.INSTALL_MODE_FILE` defaults to the developer's real
    `~/.nyxGPT/install-mode.json`, and it is not an inert file: it is what
    `ops._reconcile_install_mode()` compares the requested mode against, so a
    test that reaches that function on a machine recording `dev` (exactly the
    machine `nyxgpt up --dev` produces) makes it decide the mode is *changing*
    -- which deletes the real `~/.nyxGPT/opt/nyxgpt-api/venv`, boots out the
    live dev LaunchAgents on macOS, and rewrites the real marker to artifact.
    The suite would then be destroying the state of the very machine it runs
    on, and passing or failing according to what that machine happens to have
    installed.

    Isolating the marker here, once, closes that for every present and future
    test rather than per call site. Tests that need to exercise the marker
    itself still write to it -- they just write into `tmp_path`.
    """
    from nyxgpt import install_mode

    monkeypatch.setattr(
        install_mode, "INSTALL_MODE_FILE", tmp_path / "install-mode-home" / "install-mode.json"
    )
    yield
