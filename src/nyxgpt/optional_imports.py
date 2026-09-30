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

# --- Remedy text for a missing optional extra (#4122) -------------------
#
# One definition, shared by every module that raises on an absent `[cloud]`
# dependency (`cloud`, `cloud_mac`, `cloud_state`, `cloud_secrets`,
# `aws_credentials_setup`). Five copies of a remedy is five chances for one of
# them to go on naming a command that does not work -- and that is exactly what
# happened: they all said "Install with `pip install nyxgpt[cloud]`", which the
# owner's 2026-09-30 acceptance run showed is unfollowable on a Homebrew keg.
# `pip` is not on PATH there, `pip3` is a different interpreter whose
# site-packages `nyxgpt` never reads, and the only pip that reaches the right
# venv is a raw path into the Cellar -- unwrapped and layout-specific, which
# CLAUDE.md's Operational Command Wrapping requirement forbids.
# `nyxgpt ops install-extra cloud` installs into `sys.executable`, i.e. the
# interpreter that will do the importing, so it is correct on every layout.
#
# Lives in this module because this is the leaf that already owns "the optional
# dependency is not here": `nyxgpt.config` imports `cloud_secrets`, so hanging
# the constant off `nyxgpt.cloud` would make the config module import the cloud
# module and put a cycle in the way of every `nyxgpt` command.
CLOUD_EXTRA_REMEDY = (
    "Add it with `nyxgpt ops install-extra cloud`, which installs into the "
    'environment this `nyxgpt` runs from (`pip install "nyxgpt[cloud]"` works '
    "only when pip and nyxgpt share an environment -- on a Homebrew keg they do "
    "not)."
)


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
