"""Regression tests for `tests/integration/conftest.py` fixture wiring (#3502).

This module deliberately lives in `tests/unit/`, **not** in
`tests/integration/`, and loads the integration `conftest.py` by explicit
file path via `importlib` rather than through pytest's collection machinery.

Why: the bug under test is that a `pytest.skip()` raised by the
session-scoped `api_base_url` fixture cascades to every test in the
`tests/integration/` package (pytest caches a `Skipped` raised by a
session-scoped fixture and re-raises it for every other requester in the
session, and the package's cleanup fixtures are `autouse=True`). A regression
test placed *inside* that package is subject to the very fixtures it guards:
when the bug is reintroduced in a no-live-server environment, the test reports
SKIPPED instead of FAILED and its assertions never run — exactly the
environment the standalone CI gate is designed for. Collecting it from
`tests/unit/` keeps it outside that autouse chain, so it genuinely fails.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

import pytest

INTEGRATION_CONFTEST = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


def _load_integration_conftest() -> ModuleType:
    """Import `tests/integration/conftest.py` by path, without collecting it.

    Loaded under a private module name and intentionally *not* registered in
    `sys.modules`, so this never collides with the `conftest` module pytest
    itself imports when collecting `tests/integration/`.
    """
    spec = importlib.util.spec_from_file_location(
        "_nyxgpt_integration_conftest_under_test", INTEGRATION_CONFTEST
    )
    assert (
        spec is not None and spec.loader is not None
    ), f"could not build an import spec for {INTEGRATION_CONFTEST}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_integration_conftest_exists() -> None:
    """Guard against the relocated regression test silently going stale."""
    assert INTEGRATION_CONFTEST.is_file(), (
        f"expected the integration conftest at {INTEGRATION_CONFTEST}; if it moved, "
        "update this module rather than deleting the regression coverage"
    )


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
    conftest = _load_integration_conftest()

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


def test_resolve_api_base_url_helper_does_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-skipping helper must stay non-skipping.

    If `_resolve_api_base_url()` ever grew a reachability check with a
    `pytest.skip()`, the cleanup fixtures that call it would re-acquire the
    skip-cascade behavior this fix removed, without changing their signatures
    (so the assertion above would not catch it). Point it at a deliberately
    unreachable address and assert it still returns plainly.

    The bare `except BaseException` is load-bearing: `pytest.skip()` raises
    `Skipped`, which derives from `BaseException`, and letting it propagate
    would mark this test SKIPPED rather than FAILED -- the exact failure mode
    that made the previous location of this test useless.
    """
    conftest = _load_integration_conftest()

    unreachable = "http://127.0.0.1:19999"
    monkeypatch.setenv("NYXGPT_TEST_API_BASE", unreachable + "/")

    try:
        resolved = conftest._resolve_api_base_url()
    except BaseException as exc:  # noqa: BLE001 - see docstring
        raise AssertionError(
            "_resolve_api_base_url() must remain a plain, non-skipping lookup -- the "
            "autouse cleanup fixtures call it precisely to avoid inheriting a skip, "
            f"but it raised {type(exc).__name__}: {exc}"
        ) from exc

    assert resolved == unreachable
