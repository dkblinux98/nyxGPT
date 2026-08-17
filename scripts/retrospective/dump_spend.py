#!/usr/bin/env python3
"""Dump per-issue spend telemetry for the retrospective dashboard.

Owner-endorsed direction from the 2026-08-09 cost post-mortem (#3696): this
is the raw signal an anomaly-watching process reads to judge whether agent
spend on an issue is unusual -- deliberately not a fixed cap. Agent work has
a real dollar cost (Claude invocations + GitHub Actions runner minutes) and
nothing recorded it before this.

Invoked only by `.github/workflows/retro_spend_dump.yml`, which runs this
with `gh` authenticated (GH_TOKEN) and REPO set to "owner/repo". Walks the
run history of the workflows that either invoke Claude directly or are
commonly triggered on an issue's feature/fix branch, attributes each run to
an issue number via its head branch, and writes:

  data/spend.json - per-issue {claude_steps, runs, runner_minutes,
                     retry_cycles, workflows: {name: run_count}}, plus an
                     "unattributed" bucket for runs whose branch carries no
                     issue number (release-branch runs, cron sweeps, etc.)

build_dashboard.py aggregates this per sprint using project_fields.json; it
never calls the GitHub API itself.
"""

import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
# Days of run history to walk. See window_start(); 0 = all history.
DEFAULT_WINDOW_DAYS = 30

# Conclusions meaning "this run did nothing": no billable minutes, no executed
# step. Excluded from the walk entirely -- see collect().
SKIPPED_CONCLUSIONS = frozenset({"skipped", "cancelled"})

# Workflows that invoke Claude directly (anthropics/claude-code-action) with
# a single, unconditional step -- a static count of 1 Claude step per
# completed run is accurate without an extra API call.
CLAUDE_WORKFLOWS_STATIC = {
    "claude.yml": 1,
    "claude-code-review.yml": 1,
    "developer_huddle_position.yml": 1,
    "scrummaster_huddle_mediation.yml": 1,
    # Dispatch-only, and both halves run unconditionally when it does (#3821).
    "claude-md-binding-canary.yml": 2,
}
# developer_auto_implement.yml calls the action conditionally, up to 6 times
# per run (initial attempt, review-fix, acceptance-fix, and up to 3 self-heal
# phases) -- its per-run step count is fetched from the Jobs API instead.
CLAUDE_WORKFLOW_DYNAMIC = "developer_auto_implement.yml"

# Additional runner-minute cost commonly triggered on issue/PR branches that
# is not itself a Claude invocation (CI, security/smoke gates, the review
# agent's merge-decision executor, the usage-limit retry cron).
COST_ONLY_WORKFLOWS = {
    "ci-tests.yml",
    "security-scan.yml",
    "linux-native-smoke.yml",
    "terraform-local-smoke.yml",
    "handle_acceptance_failure.yml",
    "review_agent_auto_review.yml",
    "usage_limit_retry.yml",
}

ALL_WORKFLOWS = sorted(
    set(CLAUDE_WORKFLOWS_STATIC) | {CLAUDE_WORKFLOW_DYNAMIC} | COST_ONLY_WORKFLOWS
)

# Branch naming conventions in use across the agent scripts: developer
# branches are "{feat,fix,chore,test,docs,refactor}/{issue}-{slug}"
# (scripts/agents/developer_create_branch.sh); issue-triggered Claude
# sessions are "claude/issue-{issue}-{timestamp}".
BRANCH_ISSUE_RE = re.compile(r"^(?:feat|fix|chore|test|docs|refactor)/(\d+)-|^claude/issue-(\d+)-")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def iter_json_objects(text):
    dec = json.JSONDecoder()
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        # raw_decode returns the ABSOLUTE index in `text` where the document
        # ended, not a length relative to `idx`. `idx += end` therefore
        # double-counts everything already consumed: correct for the first
        # document (which starts at 0) and wrong for every one after it. The
        # overshoot either runs past the end -- exiting this loop and silently
        # discarding the remaining pages -- or lands mid-document and raises
        # "Expecting value" (#3808).
        obj, end = dec.raw_decode(text, idx)
        yield obj
        idx = end


def issue_of(branch):
    """Issue number attributed to a branch name, or None if unattributable."""
    m = BRANCH_ISSUE_RE.match(branch or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def window_start():
    """Oldest run date to walk, as YYYY-MM-DD, or None to walk all history.

    The walk costs one `/timing` API call per completed run, so its size is
    the token's hourly REST budget divided by roughly one. This repository
    holds ~36k workflow runs; unbounded, the dump exhausts the agent token
    mid-walk and dies (#3808 round two). It only ever appeared bounded because
    the paginated-JSON defect silently stopped it after two pages.

    `SPEND_WINDOW_DAYS=0` restores the full walk deliberately, for a one-off
    backfill run with a fresh budget.
    """
    raw = (os.environ.get("SPEND_WINDOW_DAYS") or str(DEFAULT_WINDOW_DAYS)).strip()
    try:
        days = int(raw)
    except ValueError:
        print(
            f"[dump_spend] bad SPEND_WINDOW_DAYS {raw!r}; using {DEFAULT_WINDOW_DAYS}", flush=True
        )
        days = DEFAULT_WINDOW_DAYS
    if days <= 0:
        return None
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def list_runs(repo, workflow_file, since=None):
    """Runs for one workflow, bounded to `since` (YYYY-MM-DD) when given.

    The date bound is applied server-side via the runs API's `created` filter,
    so it cuts pages fetched rather than filtering after the fact.
    """
    since = window_start() if since is None else since
    args = [
        "api",
        "-X",
        "GET",
        f"repos/{repo}/actions/workflows/{workflow_file}/runs",
        "--paginate",
        "-f",
        "per_page=100",
    ]
    if since:
        args += ["-f", f"created=>={since}"]
    runs = []
    for page in iter_json_objects(gh(*args)):
        runs.extend(page.get("workflow_runs", []))
    return runs


# Per-run API calls that failed and were counted as zero. Surfaced in the
# snapshot rather than swallowed: a dump that quietly under-reports is the
# failure mode this whole issue is about.
DEGRADED = {"timing": 0, "claude_steps": 0}

# Which measure each runner-minutes figure came from. A public repository
# reports zero billable minutes, so the numbers are wall-clock duration; the
# snapshot says so rather than labelling unbilled time as billed.
MINUTES_SOURCE = {"billable": 0, "duration": 0}


def run_minutes(repo, run_id):
    """Runner-minutes for a run: billable where billed, wall-clock otherwise.

    **This repository is public, so Actions minutes are free and the API's
    `billable` block is all zeros** -- reading only `billable`, as this did,
    produced a spend panel of zeros no matter what else was fixed (#3808).
    `run_duration_ms` carries the real elapsed time and is populated either
    way, so it is the fallback.

    The two are not the same measure and are deliberately not blended
    silently: `billable` sums per-OS job minutes and can exceed wall clock
    when jobs run in parallel, while `run_duration_ms` is the run's own
    elapsed time. Which one each figure came from is recorded in
    MINUTES_SOURCE and reported in the snapshot, so a reader is never told
    "billed minutes" when they are looking at unbilled duration. If the
    repository ever goes private, `billable` becomes non-zero and takes over
    with no code change.

    Returns 0.0 and records the miss if the call fails. One unavailable run
    must not abort a walk over thousands of them -- `gh(check=True)` raising
    here is what killed run 32001662234 after 22 minutes of work.
    """
    try:
        out = gh("api", "-X", "GET", f"repos/{repo}/actions/runs/{run_id}/timing")
    except subprocess.CalledProcessError as exc:
        DEGRADED["timing"] += 1
        print(
            f"[dump_spend] timing unavailable for run {run_id} "
            f"(gh exit {exc.returncode}); counted as 0 minutes",
            flush=True,
        )
        return 0.0
    payload = json.loads(out)
    billable_ms = sum(v.get("total_ms", 0) for v in payload.get("billable", {}).values())
    if billable_ms > 0:
        MINUTES_SOURCE["billable"] += 1
        return billable_ms / 60000
    duration_ms = payload.get("run_duration_ms") or 0
    if duration_ms:
        MINUTES_SOURCE["duration"] += 1
    return duration_ms / 60000


def claude_steps_dynamic(repo, run_id):
    """Executed (non-skipped) Claude-named steps for one workflow run.

    Returns 0 and records the miss if the call fails, for the same reason as
    run_minutes: one unavailable run must not abort the whole walk.
    """
    try:
        out = gh(
            "api",
            "-X",
            "GET",
            f"repos/{repo}/actions/runs/{run_id}/jobs",
            "--paginate",
            "-f",
            "per_page=100",
        )
    except subprocess.CalledProcessError as exc:
        DEGRADED["claude_steps"] += 1
        print(
            f"[dump_spend] jobs unavailable for run {run_id} "
            f"(gh exit {exc.returncode}); counted as 0 steps",
            flush=True,
        )
        return 0
    count = 0
    for page in iter_json_objects(out):
        for job in page.get("jobs", []):
            for step in job.get("steps", []):
                if step.get("conclusion") in (None, "skipped"):
                    continue
                if re.search(r"claude", step.get("name") or "", re.I):
                    count += 1
    return count


def empty_bucket():
    return {
        "claude_steps": 0,
        "runs": 0,
        "runner_minutes": 0.0,
        "retry_cycles": 0,
        "workflows": defaultdict(int),
    }


def collect(
    repo, list_runs_fn=list_runs, run_minutes_fn=run_minutes, claude_steps_fn=claude_steps_dynamic
):
    """Walk every tracked workflow's run history and bucket spend by issue."""
    issues = defaultdict(empty_bucket)
    unattributed = empty_bucket()

    for wf in ALL_WORKFLOWS:
        for run in list_runs_fn(repo, wf):
            if run.get("status") != "completed":
                continue
            # A skipped or cancelled run did no work: it burned no billable
            # minutes and executed no Claude step. Counting it was wrong twice
            # over -- it inflated `runs` and, for the static workflows, credited
            # a Claude step that never ran (claude.yml is 3,969 runs of which
            # 3,969 are skipped), and it spent a /timing call to learn zero.
            # 21,637 of this repo's 23,963 tracked runs are skipped, so the
            # filter is also what keeps the walk inside one token's hourly
            # REST budget (#3808).
            if run.get("conclusion") in SKIPPED_CONCLUSIONS:
                continue
            issue = issue_of(run.get("head_branch"))
            bucket = issues[issue] if issue is not None else unattributed
            bucket["runs"] += 1
            bucket["workflows"][wf] += 1
            bucket["runner_minutes"] += run_minutes_fn(repo, run["id"])
            if wf in CLAUDE_WORKFLOWS_STATIC:
                bucket["claude_steps"] += CLAUDE_WORKFLOWS_STATIC[wf]
            elif wf == CLAUDE_WORKFLOW_DYNAMIC:
                n = claude_steps_fn(repo, run["id"])
                bucket["claude_steps"] += n
                bucket["retry_cycles"] += max(0, n - 1)

    return issues, unattributed


def finalize_bucket(bucket):
    bucket["runner_minutes"] = round(bucket["runner_minutes"], 2)
    bucket["workflows"] = dict(bucket["workflows"])
    return bucket


def build_snapshot(issues, unattributed):
    since = window_start()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        # What the walk covered, so a consumer can tell a real zero from an
        # out-of-window one, and a clean dump from a degraded one.
        "window_start": since,
        "window_days": (
            None
            if since is None
            else int(os.environ.get("SPEND_WINDOW_DAYS") or DEFAULT_WINDOW_DAYS)
        ),
        "degraded": dict(DEGRADED),
        "minutes_source": dict(MINUTES_SOURCE),
        "issues": {str(n): finalize_bucket(b) for n, b in sorted(issues.items())},
        "unattributed": finalize_bucket(unattributed),
    }


def main():
    repo = os.environ["REPO"]
    issues, unattributed = collect(repo)
    snapshot = build_snapshot(issues, unattributed)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "spend.json").write_text(json.dumps(snapshot, indent=1) + "\n")
    totals = {
        "issues": len(snapshot["issues"]),
        "runs": sum(b["runs"] for b in snapshot["issues"].values()) + unattributed["runs"],
        "claude_steps": sum(b["claude_steps"] for b in snapshot["issues"].values())
        + unattributed["claude_steps"],
        "runner_minutes": round(
            sum(b["runner_minutes"] for b in snapshot["issues"].values())
            + unattributed["runner_minutes"],
            2,
        ),
    }
    print(f"wrote spend telemetry for {totals['issues']} issues to data/spend.json: {totals}")


if __name__ == "__main__":
    main()
