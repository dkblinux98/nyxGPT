#!/usr/bin/env python3
"""
Collect and analyze GitHub Actions workflow run history for this repository.

CLI for `nyxgpt.workflow_log_store`, the SQLite-backed history store also used
by the admin dashboard (`nyxgpt.workflow_analytics`). This script is a
maintainer/dev tool that only ever runs from a repo checkout (it isn't part of
the installed `nyxgpt` package), so it falls back to importing the package
from `src/` when it isn't already installed in the current environment.

Usage:
  collect_workflow_logs.py collect --repo OWNER/NAME [--limit N] [--db PATH]
  collect_workflow_logs.py query [--workflow NAME] [--status S] [--conclusion C]
                                  [--issue N] [--branch B] [--since-days N]
                                  [--limit N] [--json] [--db PATH]
  collect_workflow_logs.py stats [--days N] [--json] [--db PATH]
  collect_workflow_logs.py purge [--retention-days N] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from nyxgpt import workflow_log_store as store
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from nyxgpt import workflow_log_store as store

DEFAULT_DB_PATH = store.DEFAULT_DB_PATH
DEFAULT_RETENTION_DAYS = store.DEFAULT_RETENTION_DAYS


def _print_runs_table(runs: list[dict[str, Any]]) -> None:
    print(f"\n{'Workflow':<35} {'Status':<12} {'Branch':<40} {'Duration':<10} {'Issue':<6}")
    print("-" * 110)
    for r in runs:
        duration = f"{int(r['duration_s'] or 0)}s"
        conclusion = r.get("conclusion") or r["status"]
        branch = (r.get("branch") or "")[:38]
        issue = str(r["issue_number"]) if r.get("issue_number") else "-"
        print(
            f"{r['workflow_name'][:33]:<35} {conclusion:<12} {branch:<40} {duration:<10} {issue:<6}"
        )


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\nWindow: last {summary['window_days']} days")
    print(f"Total runs: {summary['total_runs']}")
    print(f"Success rate: {summary['success_rate']}%")
    print(f"Avg duration: {summary['avg_duration_s']}s")
    print(f"Failures: {summary['failures']}")

    if summary["by_workflow"]:
        print(f"\n{'Workflow':<35} {'Runs':<6} {'Success %':<10} {'Avg s':<8} {'Failures':<8}")
        print("-" * 70)
        for w in summary["by_workflow"]:
            print(
                f"{w['workflow'][:33]:<35} {w['runs']:<6} {w['success_rate']:<10} "
                f"{w['avg_duration_s']:<8} {w['failures']:<8}"
            )

    if summary["top_failing"]:
        print("\nTop failing workflows:")
        for w in summary["top_failing"][:5]:
            print(f"  - {w['workflow']}: {w['failures']} failures ({w['success_rate']}% success)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect and analyze historical GitHub Actions workflow run logs"
    )
    parser.add_argument("--db", help=f"Path to the SQLite store (default: {DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="Fetch and store recent workflow runs")
    collect_p.add_argument("--repo", required=True, help="Repository as OWNER/NAME")
    collect_p.add_argument(
        "--limit", type=int, default=200, help="Max runs to fetch (default: 200)"
    )
    collect_p.add_argument(
        "--include-in-progress", action="store_true", help="Also store non-completed runs"
    )

    query_p = sub.add_parser("query", help="Query stored workflow runs")
    query_p.add_argument("--workflow", help="Filter by workflow name")
    query_p.add_argument("--status", help="Filter by status")
    query_p.add_argument("--conclusion", help="Filter by conclusion (success/failure/cancelled)")
    query_p.add_argument("--issue", type=int, help="Filter by issue number")
    query_p.add_argument("--branch", help="Filter by exact branch name")
    query_p.add_argument("--since-days", type=float, help="Only runs created in the last N days")
    query_p.add_argument("--limit", type=int, default=50, help="Max rows to return (default: 50)")
    query_p.add_argument("--json", action="store_true", help="Output as JSON")

    stats_p = sub.add_parser("stats", help="Show aggregated analytics")
    stats_p.add_argument("--days", type=int, default=30, help="Window size in days (default: 30)")
    stats_p.add_argument("--json", action="store_true", help="Output as JSON")

    purge_p = sub.add_parser("purge", help="Delete runs older than the retention window")
    purge_p.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Retention window in days (default: {DEFAULT_RETENTION_DAYS})",
    )

    args = parser.parse_args(argv)
    db_path = store.get_db_path(args.db)
    conn = store.get_connection(db_path)

    try:
        if args.command == "collect":
            count = store.collect(
                args.repo, conn, limit=args.limit, include_in_progress=args.include_in_progress
            )
            print(f"Collected {count} workflow run(s) into {db_path}")
        elif args.command == "query":
            runs = store.query_runs(
                conn,
                workflow=args.workflow,
                status=args.status,
                conclusion=args.conclusion,
                issue_number=args.issue,
                branch=args.branch,
                since_days=args.since_days,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps(runs, indent=2))
            else:
                _print_runs_table(runs)
        elif args.command == "stats":
            summary = store.compute_summary(conn, days=args.days)
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                _print_summary(summary)
        elif args.command == "purge":
            deleted = store.purge_old(conn, retention_days=args.retention_days)
            print(f"Purged {deleted} run(s) older than {args.retention_days} days")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
