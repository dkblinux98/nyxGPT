"""In-memory tracking of pending, wizard-triggered restart requirements (#3407).

Populated when a Configuration Wizard save (`POST /api/v1/config/sections`)
touches a field whose schema entry declares a `restart_component`, so the
Admin Dashboard can show a single "restart required" action instead of the
wizard offering per-field restart buttons scattered across its steps.
Cleared once `POST /api/v1/infra/restart-required` actually restarts that
component (`app.py`'s `_do_restart_required`).

Deliberately process-local, in-memory only, with no on-disk persistence:
this mirrors `admin_activity`'s ring buffer, and for the one component the
wizard schema currently ever flags (`api`), a process restart of that exact
component is itself the strongest possible evidence the restart happened --
losing the flag when the flagged process restarts is the correct behavior,
not a gap.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}


def mark_pending(component: str, keys: list[str]) -> None:
    """Record that `component` needs a restart because `keys` changed.

    Merges with any keys already pending for `component` rather than
    overwriting them, so several saves in a row (e.g. one per wizard page)
    accumulate a complete picture of what's waiting on the restart.
    """
    if not keys:
        return
    with _lock:
        entry = _pending.setdefault(component, {"keys": [], "since": time.time()})
        entry["keys"] = sorted(set(entry["keys"]) | set(keys))


def clear_pending(component: str) -> None:
    """Clear `component`'s pending-restart flag, e.g. after it's been restarted."""
    with _lock:
        _pending.pop(component, None)


def snapshot() -> dict[str, dict[str, Any]]:
    """Return a deep-enough copy of the current pending-restart state for a JSON response."""
    with _lock:
        return {k: {"keys": list(v["keys"]), "since": v["since"]} for k, v in _pending.items()}


def reset() -> None:
    """Clear all pending-restart state. Test-only -- this module has no other writer to undo."""
    with _lock:
        _pending.clear()
