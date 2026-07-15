"""CI workflow-run analytics for the admin dashboard.

Wraps the SQLite-backed store in ``nyxgpt.workflow_log_store`` (populated via
``scripts/collect_workflow_logs.py collect``) so the admin dashboard can show
the same success-rate/duration/failure analytics the CLI exposes, without
duplicating the schema or query logic in a second module.

``workflow_log_store`` lives inside the installed ``nyxgpt`` package, so it's
reachable via a normal import in every deployment layout (source checkout,
``pip install``, the native Homebrew install, and the Docker image) — unlike a
source-tree-relative path guess, which only resolves from a repo checkout.
"""

from __future__ import annotations

from typing import Any

from nyxgpt import workflow_log_store


def summary(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """Aggregated analytics plus the most recent runs, for the admin dashboard.

    Returns an "unavailable"/"not yet collected" payload instead of raising
    when the store can't be resolved or no runs have been collected yet, so
    the dashboard can render a helpful empty state.
    """
    try:
        db_path = workflow_log_store.get_db_path(None)
    except OSError as e:
        return {
            "available": False,
            "reason": f"could not resolve workflow log store: {e}",
            "stats": None,
            "recent_runs": [],
        }

    if not db_path.exists():
        return {"available": True, "collected": False, "stats": None, "recent_runs": []}

    conn = workflow_log_store.get_connection(db_path)
    try:
        stats = workflow_log_store.compute_summary(conn, days=days)
        recent_runs = workflow_log_store.query_runs(conn, limit=limit)
    finally:
        conn.close()

    return {"available": True, "collected": True, "stats": stats, "recent_runs": recent_runs}
