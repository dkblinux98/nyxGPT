"""Guarded imports for integration packages that can go missing after a pull.

`tracing.py`, `error_tracking.py`, and `metrics.py` each wrap an integration
(OTel instrumentations, Sentry, Prometheus) that's declared in
`pyproject.toml` but only actually used when its feature is enabled in
config. A venv that wasn't refreshed after a `git pull` added or bumped one
of those packages used to take down every `nyxgpt` command with a bare
`ModuleNotFoundError` at import time (#3487), even for commands that never
touch the missing integration. `try_import` turns that into a `None` the
caller can check, so import failures degrade to the same "feature disabled"
no-op path as an operator-disabled feature.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


def try_import(module_name: str) -> ModuleType | None:
    """Import `module_name`, or return None if it isn't installed."""
    try:
        return import_module(module_name)
    except ModuleNotFoundError:
        return None


def try_import_attr(module_name: str, attr_name: str) -> Any | None:
    """Import `attr_name` from `module_name`, or return None if unavailable.

    Typed `Any` (rather than a precise type) since callers use this for
    untyped third-party classes (see the `opentelemetry.*`/`sentry_sdk.*`
    mypy override) -- the point is a runtime None check, not static typing.
    """
    module = try_import(module_name)
    if module is None:
        return None
    return getattr(module, attr_name, None)
