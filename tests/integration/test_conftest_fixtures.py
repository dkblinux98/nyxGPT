from __future__ import annotations

import inspect

from tests.integration import conftest


def test_autouse_cleanup_fixtures_do_not_depend_on_api_base_url() -> None:
    """Regression test for the api_base_url skip-cascade bug (#3502).

    `api_base_url` is session-scoped and calls `pytest.skip()` when no live API
    server is reachable. pytest caches a `Skipped` exception raised by a
    session-scoped fixture and re-raises it for every other fixture/test in the
    session that requests it. The three cleanup fixtures below are
    `autouse=True` for the whole `tests/integration/` package, so if any of
    them requested `api_base_url` directly, an unreachable server would skip
    every collected test in the package -- not just the ones that actually
    need a live server. They must resolve the URL via the plain
    `_resolve_api_base_url()` helper instead.
    """
    autouse_cleanup_fixtures = (
        conftest.cleanup_test_rag_documents,
        conftest.cleanup_test_collections,
        conftest.cleanup_test_sessions,
    )
    for fixture in autouse_cleanup_fixtures:
        params = inspect.signature(fixture.__wrapped__).parameters
        assert "api_base_url" not in params, (
            f"{fixture.__name__} must not depend on the skip-raising `api_base_url` "
            "fixture -- use `_resolve_api_base_url()` instead to avoid cascading "
            "a skip to the entire tests/integration/ package"
        )
