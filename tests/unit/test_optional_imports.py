"""Unit tests for the guarded-import helpers (nyxgpt.optional_imports)."""

from __future__ import annotations

import pytest

from nyxgpt.optional_imports import try_import, try_import_attr

pytestmark = pytest.mark.unit

_MISSING_MODULE = "nyxgpt_test_definitely_missing_module_xyz"


def test_try_import_returns_module_when_installed() -> None:
    import json

    assert try_import("json") is json


def test_try_import_returns_none_when_module_not_found() -> None:
    assert try_import(_MISSING_MODULE) is None


def test_try_import_attr_returns_attr_when_installed() -> None:
    assert try_import_attr("json", "dumps") is not None


def test_try_import_attr_returns_none_when_module_not_found() -> None:
    assert try_import_attr(_MISSING_MODULE, "whatever") is None


def test_try_import_attr_returns_none_when_attr_missing() -> None:
    assert try_import_attr("json", "definitely_not_a_real_attr") is None
