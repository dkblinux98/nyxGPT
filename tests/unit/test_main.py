"""Unit tests for nyxgpt.__main__ (module entry point)."""

from __future__ import annotations

import runpy
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def test_main_module_invokes_cli_and_exits_with_its_return_code():
    """Running `python -m nyxgpt` should call cli() and exit with its return value."""
    # Ensure a fresh execution of the module body (it's a script, not import-cached).
    sys.modules.pop("nyxgpt.__main__", None)

    with patch("nyxgpt.cli.cli", return_value=7) as mock_cli, pytest.raises(SystemExit) as exc_info:
        runpy.run_module("nyxgpt.__main__", run_name="__main__")

    mock_cli.assert_called_once_with()
    assert exc_info.value.code == 7


def test_main_module_exits_zero_on_success():
    """A cli() return of 0 should still raise SystemExit(0), not be swallowed."""
    sys.modules.pop("nyxgpt.__main__", None)

    with patch("nyxgpt.cli.cli", return_value=0) as mock_cli, pytest.raises(SystemExit) as exc_info:
        runpy.run_module("nyxgpt.__main__", run_name="__main__")

    mock_cli.assert_called_once_with()
    assert exc_info.value.code == 0
