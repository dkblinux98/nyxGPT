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
