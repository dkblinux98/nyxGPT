"""SQLite-backed store for GitHub Actions workflow run history.

Extends the real-time monitoring in ``scripts/watch_agents.py`` with a durable,
queryable history: completed workflow runs are stored locally so they survive
GitHub's log retention window and can be analyzed for trends (success rate,
duration creep, recurring failures) after the fact.

This module lives inside the installed ``nyxgpt`` package (rather than under the
repo-only ``scripts/`` directory) so both the maintainer CLI
(``scripts/collect_workflow_logs.py``) and the admin dashboard backend
(``nyxgpt.workflow_analytics``) can reach it via a normal import in every
deployment layout: source checkout, ``pip install``, the native Homebrew
install, and the Docker image.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".nyxGPT" / "logs" / "workflow_runs.sqlite3"
DEFAULT_RETENTION_DAYS = 90

_ISSUE_NUMBER_RE = re.compile(r"(?:^|/|-)(\d+)(?:-|$)")


def get_db_path(db_path: str | None) -> Path:
    """Resolve the SQLite database path, creating its parent directory."""
    path = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the workflow run store and ensure its schema exists."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the workflow_runs table and indexes if they don't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id INTEGER PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            status TEXT NOT NULL,
            conclusion TEXT,
            branch TEXT,
            issue_number INTEGER,
            title TEXT,
            url TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            duration_s REAL,
            collected_at REAL NOT NULL
        )
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_issue ON workflow_runs(issue_number)"
    )
    conn.commit()


def parse_issue_number(branch: str | None) -> int | None:
    """Extract an issue number from a branch name, if one is embedded in it.

    Matches conventions used across this repo's branches, e.g.
    ``feat/2844-slug``, ``fix/2844-slug``, ``claude/issue-2844-20260714-0753``.
    """
    if not branch:
        return None
    match = _ISSUE_NUMBER_RE.search(branch)
    if not match:
        return None
    return int(match.group(1))


def _parse_iso8601(value: str) -> float:
    """Parse an ISO 8601 timestamp (with trailing "Z") into a Unix epoch float."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def to_record(run: dict[str, Any]) -> dict[str, Any]:
    """Normalize a `gh run list --json ...` entry into a storable record."""
    created_at = _parse_iso8601(run["createdAt"])
    updated_at = _parse_iso8601(run["updatedAt"])
    branch = run.get("headBranch")
    return {
        "run_id": run["databaseId"],
        "workflow_name": run.get("workflowName", "Unknown"),
        "status": run.get("status", "unknown"),
        "conclusion": run.get("conclusion") or None,
        "branch": branch,
        "issue_number": parse_issue_number(branch),
        "title": run.get("displayTitle"),
        "url": run.get("url"),
        "created_at": created_at,
        "updated_at": updated_at,
        "duration_s": max(0.0, updated_at - created_at),
        "collected_at": time.time(),
    }


def ingest_runs(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> int:
    """Upsert normalized run records into the store. Returns the number ingested."""
    if not records:
        return 0
    conn.executemany(
        """
        INSERT INTO workflow_runs (
            run_id, workflow_name, status, conclusion, branch, issue_number,
            title, url, created_at, updated_at, duration_s, collected_at
        ) VALUES (
            :run_id, :workflow_name, :status, :conclusion, :branch, :issue_number,
            :title, :url, :created_at, :updated_at, :duration_s, :collected_at
        )
        ON CONFLICT(run_id) DO UPDATE SET
            workflow_name=excluded.workflow_name,
            status=excluded.status,
            conclusion=excluded.conclusion,
            branch=excluded.branch,
            issue_number=excluded.issue_number,
            title=excluded.title,
            url=excluded.url,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            duration_s=excluded.duration_s,
            collected_at=excluded.collected_at
        """,
        records,
    )
    conn.commit()
    return len(records)


def run_gh_command(args: list[str]) -> str:
    """Run a `gh` CLI command and return its stdout (empty string on failure)."""
    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True, check=True)
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error running gh command: {e}", file=sys.stderr)
        return ""


def fetch_workflow_runs(repo: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch recent workflow runs for `repo` via the gh CLI."""
    result = run_gh_command(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            str(limit),
            "--json",
            "databaseId,workflowName,status,conclusion,createdAt,updatedAt,url,headBranch,displayTitle",
        ]
    )
    if not result:
        return []
    runs: list[dict[str, Any]] = json.loads(result)
    return runs


def collect(
    repo: str, conn: sqlite3.Connection, limit: int = 200, include_in_progress: bool = False
) -> int:
    """Fetch runs from GitHub and ingest completed ones (or all, if requested)."""
    runs = fetch_workflow_runs(repo, limit=limit)
    if not include_in_progress:
        runs = [r for r in runs if r.get("status") == "completed"]
    records = [to_record(r) for r in runs]
    return ingest_runs(conn, records)


def query_runs(
    conn: sqlite3.Connection,
    workflow: str | None = None,
    status: str | None = None,
    conclusion: str | None = None,
    issue_number: int | None = None,
    branch: str | None = None,
    since_days: float | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query stored workflow runs with optional filters, newest first."""
    clauses = []
    params: dict[str, Any] = {}
    if workflow:
        clauses.append("workflow_name = :workflow")
        params["workflow"] = workflow
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if conclusion:
        clauses.append("conclusion = :conclusion")
        params["conclusion"] = conclusion
    if issue_number is not None:
        clauses.append("issue_number = :issue_number")
        params["issue_number"] = issue_number
    if branch:
        clauses.append("branch = :branch")
        params["branch"] = branch
    if since_days is not None:
        clauses.append("created_at >= :since")
        params["since"] = time.time() - since_days * 86400

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params["limit"] = limit
    rows = conn.execute(
        f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT :limit",  # noqa: S608
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def compute_summary(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Aggregate stored runs from the last `days` days into analytics metrics."""
    since = time.time() - days * 86400
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM workflow_runs WHERE created_at >= ? ORDER BY created_at", (since,)
        ).fetchall()
    ]

    total_runs = len(rows)
    successes = sum(1 for r in rows if r["conclusion"] == "success")
    failures = sum(1 for r in rows if r["conclusion"] == "failure")
    success_rate = (successes / total_runs * 100) if total_runs else 0.0
    avg_duration_s = (sum(r["duration_s"] or 0.0 for r in rows) / total_runs) if total_runs else 0.0

    by_workflow: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        wf = by_workflow.setdefault(
            r["workflow_name"], {"runs": 0, "successes": 0, "failures": 0, "total_duration_s": 0.0}
        )
        wf["runs"] += 1
        wf["total_duration_s"] += r["duration_s"] or 0.0
        if r["conclusion"] == "success":
            wf["successes"] += 1
        elif r["conclusion"] == "failure":
            wf["failures"] += 1

        day = datetime.fromtimestamp(r["created_at"], tz=UTC).strftime("%Y-%m-%d")
        day_stats = by_day.setdefault(day, {"runs": 0, "failures": 0})
        day_stats["runs"] += 1
        if r["conclusion"] == "failure":
            day_stats["failures"] += 1

    by_workflow_list = [
        {
            "workflow": name,
            "runs": stats["runs"],
            "success_rate": round(stats["successes"] / stats["runs"] * 100, 1),
            "avg_duration_s": round(stats["total_duration_s"] / stats["runs"], 1),
            "failures": stats["failures"],
        }
        for name, stats in sorted(by_workflow.items())
    ]
    top_failing = sorted(
        (w for w in by_workflow_list if w["failures"] > 0),
        key=lambda w: w["failures"],
        reverse=True,
    )

    return {
        "window_days": days,
        "total_runs": total_runs,
        "success_rate": round(success_rate, 1),
        "avg_duration_s": round(avg_duration_s, 1),
        "failures": failures,
        "by_workflow": by_workflow_list,
        "by_day": [{"date": day, **stats} for day, stats in sorted(by_day.items())],
        "top_failing": top_failing,
    }


def purge_old(conn: sqlite3.Connection, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete runs older than `retention_days`. Returns the number of rows deleted."""
    cutoff = time.time() - retention_days * 86400
    cur = conn.execute("DELETE FROM workflow_runs WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount
