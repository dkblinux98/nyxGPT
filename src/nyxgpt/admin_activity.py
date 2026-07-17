"""Lightweight audit trail of admin dashboard actions.

Records structured events (config changes, deploy/canary actions, model
management, access changes) so the admin dashboard can show an activity
log. Events are kept in an in-memory ring buffer for fast reads and
appended to a JSONL file under the log directory so recent history
survives a process restart.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from nyxgpt.logging import get_log_dir

_MAX_EVENTS = 500

_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)


def _activity_log_path(cfg: ConfigParser | None = None) -> Path:
    """Return the path of the JSONL file admin activity events are appended to."""
    return get_log_dir(cfg) / "admin_activity.jsonl"


def record(action: str, detail: str, cfg: ConfigParser | None = None) -> dict[str, Any]:
    """Append an admin activity event and return it."""
    event: dict[str, Any] = {"ts": time.time(), "action": action, "detail": detail}
    with _lock:
        _events.append(event)
        try:
            path = _activity_log_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            # Activity logging is best-effort; never fail the caller's request over it.
            pass
    return event


def recent(limit: int = 50, cfg: ConfigParser | None = None) -> list[dict[str, Any]]:
    """Return up to `limit` most recent events, newest last."""
    with _lock:
        items = list(_events)
    if not items:
        items = _load_from_disk(limit, cfg)
    return items[-limit:]


def _load_from_disk(limit: int, cfg: ConfigParser | None) -> list[dict[str, Any]]:
    """Load up to the last `limit` events from the on-disk JSONL log.

    Used as a fallback by `recent` when the in-memory ring buffer is empty
    (e.g. right after a process restart). Malformed lines are skipped.

    Args:
        limit: Maximum number of trailing lines to read from the log file.
        cfg: Optional config, used to locate the log directory.

    Returns:
        Parsed events in file order (oldest to newest), or an empty list
        if the log file doesn't exist or can't be read.
    """
    path = _activity_log_path(cfg)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
