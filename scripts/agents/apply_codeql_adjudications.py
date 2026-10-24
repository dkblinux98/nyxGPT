#!/usr/bin/env python3
"""Re-apply the adjudication register to a ref's open code-scanning alerts (#4222).

Run by `.github/workflows/codeql_adjudications.yml` when an alert appears, and
dispatchable by hand. Reads `security/codeql-adjudications.yml`, matches it
against the open alerts for a ref by FLOW rather than by line
(`scripts/agents/lib/codeql_adjudications.py` holds that decision and its
reasoning), dismisses what the register already adjudicates, and reports what
it does not.

Exit codes, which are the point of running it in CI:

    0   every open critical/high alert on the ref is adjudicated. The release
        ceremony's code-scanning gate would pass.
    1   at least one open critical/high alert matches no register entry. That
        is a finding a human has to look at; nothing was dismissed for it.
    2   the register is malformed, or GitHub could not be read. Nothing was
        attempted -- a register that cannot be read is not an empty one.

`--dry-run` decides and reports without writing, which is what the unit tests
and a curious operator both want.

Why not just re-dismiss by hand each time: it had been done by hand five times
(alert 143 -> 144, and alerts 21/22/23 -> 145) and the sixth was discovered by
a release being blocked. The register makes the decision durable; this script
makes it self-applying.

(Alert numbers carry no `#`: in rendered markdown that is an issue link, and
this repo's issues 21-146 are deleted. See `dismissal_comment`.)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - gh plumbing, fixed argv, no shell
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import codeql_adjudications as adj  # noqa: E402

EXIT_OK = 0
EXIT_UNADJUDICATED = 1
EXIT_UNREADABLE = 2


def _gh(args: list[str], *, accept: str | None = None) -> str:
    """One `gh api` read. Raises `RuntimeError` with gh's own text on failure.

    Reads work under any token holding `repo` or `security-events: read`,
    including an agent session's (ledger **S-011** retires the belief that
    they do not). What this script needs the workflow for is the *write*: the
    dismissal `PATCH` wants `security-events: write`, which
    `.github/workflows/codeql_adjudications.yml` grants the job, and running
    there leaves a citable run log for the review to point at.
    """
    argv = ["gh", "api"] + args
    if accept:
        argv += ["-H", f"Accept: {accept}"]
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        argv, capture_output=True, text=True, check=False, timeout=180
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(argv)}` failed: {(proc.stderr or '').strip()}")
    return proc.stdout


def open_alerts(repo: str, ref: str) -> list[dict[str, Any]]:
    """Every open alert on `ref`, all pages."""
    raw = _gh(
        [
            "--paginate",
            "--slurp",
            f"repos/{repo}/code-scanning/alerts?state=open&ref={ref}&per_page=100",
        ]
    )
    pages = json.loads(raw or "[]")
    # `--slurp` wraps each page in a list; one flatten covers both shapes.
    out: list[dict[str, Any]] = []
    for page in pages:
        out.extend(page if isinstance(page, list) else [page])
    return out


def latest_python_sarif(repo: str, ref: str) -> dict[str, Any]:
    """The SARIF of the most recent Python analysis for `ref`.

    The alert payload names only the sink; the flows that name each source
    live here. Same two calls `.github/workflows/code_scan_report.yml` makes.
    """
    analyses = json.loads(
        _gh([f"repos/{repo}/code-scanning/analyses?ref={ref}&per_page=20"]) or "[]"
    )
    python = [a for a in analyses if "python" in str(a.get("category") or "")]
    if not python:
        raise RuntimeError(f"no python code-scanning analysis for {ref}")
    analysis_id = python[0]["id"]
    return json.loads(
        _gh(
            [f"repos/{repo}/code-scanning/analyses/{analysis_id}"],
            accept="application/sarif+json",
        )
        or "{}"
    )


def dismiss(repo: str, number: int, adjudication: adj.Adjudication) -> None:
    """Record the register's decision on one alert."""
    _gh(
        [
            "--method",
            "PATCH",
            f"repos/{repo}/code-scanning/alerts/{number}",
            "-f",
            "state=dismissed",
            "-f",
            f"dismissed_reason={adjudication.reason}",
            "-f",
            f"dismissed_comment={adj.dismissal_comment(adjudication)}",
        ]
    )


def _describe(alert: dict[str, Any]) -> str:
    rule, path, line = adj.alert_sink(alert)
    severity = adj.alert_severity(alert) or "unrated"
    return f"#{alert.get('number')} [{severity}] {rule} {path}:{line}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name"
    )
    parser.add_argument(
        "--ref",
        default=os.environ.get("SCAN_REF", ""),
        help="the ref to report on, e.g. refs/heads/<release-branch>",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and report, write nothing",
    )
    args = parser.parse_args(argv)

    if not args.repo or not args.ref:
        print("apply-codeql-adjudications: --repo and --ref are both required", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        register = adj.load_register()
    except adj.RegisterError as exc:
        print(f"apply-codeql-adjudications: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    print(f"Register: {len(register)} adjudication(s) from {adj.REGISTER_PATH.name}")
    for entry in register:
        print(f"  {entry.key()}  <- {sorted(entry.sources)}  ({entry.reason})")

    try:
        alerts = open_alerts(args.repo, args.ref)
        sarif = latest_python_sarif(args.repo, args.ref)
    except (RuntimeError, json.JSONDecodeError) as exc:
        # Fail closed and loudly. "Could not read" must never be reported as
        # "nothing open" -- that would hand the release ceremony a green gate
        # it has no evidence for.
        print(f"apply-codeql-adjudications: could not read code scanning: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    flows = adj.source_functions_by_sink(sarif)
    report = adj.match_alerts(alerts, register, flows)

    print(f"\nOpen alerts on {args.ref}: {len(alerts)}")

    for alert, entry in report.adjudicated:
        action = "would dismiss" if args.dry_run else "dismissing"
        print(f"  {action}: {_describe(alert)} -> {entry.reason} ({entry.key()})")
        if not args.dry_run:
            try:
                dismiss(args.repo, int(alert["number"]), entry)
            except RuntimeError as exc:
                print(f"    FAILED: {exc}", file=sys.stderr)
                return EXIT_UNREADABLE

    blocking = report.blocking_unadjudicated
    for alert in report.unadjudicated:
        rule, path, line = adj.alert_sink(alert)
        sink = adj.enclosing_function(adj.REPO_ROOT / path, line) if path and line else None
        sources = sorted(flows.get((rule, path, line), set()))
        tag = "UNADJUDICATED" if alert in blocking else "unadjudicated (not gated)"
        print(f"  {tag}: {_describe(alert)}")
        print(f"      sink function: {sink or 'unresolved'}   flow sources: {sources or 'none'}")

    if blocking:
        # Ordered output: the stderr block below is the operator's next action,
        # and an unflushed stdout would print it above the findings it is about.
        sys.stdout.flush()
        print(
            f"\n{len(blocking)} open critical/high alert(s) match no entry in "
            f"{adj.REGISTER_PATH.relative_to(adj.REPO_ROOT)}.\n"
            "Each is either a defect to fix or a finding to adjudicate -- trace its flow "
            "(`.github/workflows/code_scan_report.yml` prints every codeFlow), then either "
            "fix it at the source or add an entry naming the sink function, every source "
            "function, the reason, and the test that keeps the reason true.",
            file=sys.stderr,
        )
        return EXIT_UNADJUDICATED

    print("\nNo open critical/high alert is unadjudicated: the ceremony's gate would pass.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
