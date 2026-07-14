"""CI workflow-run analytics for the admin dashboard.

Wraps the SQLite-backed store built by ``scripts/collect_workflow_logs.py``
(populated via its ``collect`` subcommand) so the admin dashboard can show
the same success-rate/duration/failure analytics the CLI exposes, without
duplicating the schema or query logic in a second module.

The collector lives under ``scripts/`` (not the installed ``nyxgpt`` package)
so it's loaded by file path rather than imported normally; this only works
against a repo checkout, not a package installed outside the repo (e.g. via
``pip install`` without the source tree). ``summary()`` degrades gracefully
in that case instead of raising.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "collect_workflow_logs.py"
_MODULE_NAME = "nyxgpt._collect_workflow_logs"


def _load_collector() -> ModuleType:
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load workflow log collector from {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def summary(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """Aggregated analytics plus the most recent runs, for the admin dashboard.

    Returns an "unavailable"/"not yet collected" payload instead of raising
    when the collector script can't be found or no runs have been collected
    yet, so the dashboard can render a helpful empty state.
    """
    if not _SCRIPT_PATH.is_file():
        return {
            "available": False,
            "reason": "collector script not found",
            "stats": None,
            "recent_runs": [],
        }

    collector = _load_collector()
    db_path = collector.get_db_path(None)
    if not db_path.exists():
        return {"available": True, "collected": False, "stats": None, "recent_runs": []}

    conn = collector.get_connection(db_path)
    try:
        stats = collector.compute_summary(conn, days=days)
        recent_runs = collector.query_runs(conn, limit=limit)
    finally:
        conn.close()

    return {"available": True, "collected": True, "stats": stats, "recent_runs": recent_runs}
