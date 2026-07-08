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
