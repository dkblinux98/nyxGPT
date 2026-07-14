"""Usage analytics for the admin dashboard.

Records per-request usage events (session, model, token counts, request
duration) emitted by the chat and streaming chat endpoints, so the admin
dashboard can show usage trends, model breakdowns, and session counts, and
export them as a report. Events are kept in an in-memory ring buffer for
fast reads and appended to a JSONL file under the log directory so recent
history survives a process restart.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from collections import deque
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from nyxgpt.logging import get_log_dir

_MAX_EVENTS = 2000

_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)

_EVENT_FIELDS = ["ts", "session", "model", "prompt_tokens", "completion_tokens", "duration_s"]


def _usage_log_path(cfg: ConfigParser | None = None) -> Path:
    return get_log_dir(cfg) / "usage_analytics.jsonl"


def record(
    session: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_s: float,
    cfg: ConfigParser | None = None,
) -> dict[str, Any]:
    """Append a usage event for a completed chat request and return it."""
    event: dict[str, Any] = {
        "ts": time.time(),
        "session": session,
        "model": model,
        "prompt_tokens": max(0, int(prompt_tokens)),
        "completion_tokens": max(0, int(completion_tokens)),
        "duration_s": max(0.0, float(duration_s)),
    }
    with _lock:
        _events.append(event)
        try:
            path = _usage_log_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            # Usage logging is best-effort; never fail the caller's request over it.
            pass
    return event


def recent(limit: int = 500, cfg: ConfigParser | None = None) -> list[dict[str, Any]]:
    """Return up to `limit` most recent usage events, newest last."""
    with _lock:
        items = list(_events)
    if not items:
        items = _load_from_disk(limit, cfg)
    return items[-limit:]


def _load_from_disk(limit: int, cfg: ConfigParser | None) -> list[dict[str, Any]]:
    path = _usage_log_path(cfg)
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


def summary(cfg: ConfigParser | None = None) -> dict[str, Any]:
    """Aggregate recorded usage events into totals and per-model/per-day breakdowns."""
    events = recent(limit=_MAX_EVENTS, cfg=cfg)

    total_prompt_tokens = sum(e.get("prompt_tokens", 0) for e in events)
    total_completion_tokens = sum(e.get("completion_tokens", 0) for e in events)
    sessions = {e["session"] for e in events if e.get("session")}

    by_model: dict[str, dict[str, int]] = {}
    by_day: dict[str, dict[str, int]] = {}
    for e in events:
        model = e.get("model") or "unknown"
        model_stats = by_model.setdefault(
            model, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        model_stats["requests"] += 1
        model_stats["prompt_tokens"] += e.get("prompt_tokens", 0)
        model_stats["completion_tokens"] += e.get("completion_tokens", 0)

        day = time.strftime("%Y-%m-%d", time.localtime(e.get("ts", 0)))
        day_stats = by_day.setdefault(
            day, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        day_stats["requests"] += 1
        day_stats["prompt_tokens"] += e.get("prompt_tokens", 0)
        day_stats["completion_tokens"] += e.get("completion_tokens", 0)

    return {
        "total_requests": len(events),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "session_count": len(sessions),
        "by_model": [{"model": model, **stats} for model, stats in sorted(by_model.items())],
        "by_day": [{"date": day, **stats} for day, stats in sorted(by_day.items())],
    }


def export_report(fmt: str, cfg: ConfigParser | None = None) -> tuple[str, str, str]:
    """Build an export of recorded usage events.

    Args:
        fmt: Export format, either "json" or "csv".
        cfg: Optional config, used to locate the log directory.

    Returns:
        A tuple of (content, content_type, filename).

    Raises:
        ValueError: If `fmt` is not a supported export format.
    """
    normalized_fmt = fmt.lower()
    events = recent(limit=_MAX_EVENTS, cfg=cfg)

    if normalized_fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EVENT_FIELDS)
        writer.writeheader()
        for e in events:
            writer.writerow({field: e.get(field, "") for field in _EVENT_FIELDS})
        return buf.getvalue(), "text/csv", "usage_report.csv"

    if normalized_fmt == "json":
        payload = {"summary": summary(cfg=cfg), "events": events}
        return json.dumps(payload, indent=2), "application/json", "usage_report.json"

    raise ValueError(f"Unsupported export format: {fmt!r}. Use 'json' or 'csv'.")


__all__ = ["record", "recent", "summary", "export_report"]
