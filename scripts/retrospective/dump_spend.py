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
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"

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


def list_runs(repo, workflow_file):
    out = gh(
        "api",
        "-X",
        "GET",
        f"repos/{repo}/actions/workflows/{workflow_file}/runs",
        "--paginate",
        "-f",
        "per_page=100",
    )
    runs = []
    for page in iter_json_objects(out):
        runs.extend(page.get("workflow_runs", []))
    return runs


def run_minutes(repo, run_id):
    """Billable runner-minutes for a run, summed across OS/runner types."""
    out = gh("api", "-X", "GET", f"repos/{repo}/actions/runs/{run_id}/timing")
    billable = json.loads(out).get("billable", {})
    total_ms = sum(v.get("total_ms", 0) for v in billable.values())
    return total_ms / 60000


def claude_steps_dynamic(repo, run_id):
    """Executed (non-skipped) Claude-named steps for one workflow run."""
    out = gh(
        "api",
        "-X",
        "GET",
        f"repos/{repo}/actions/runs/{run_id}/jobs",
        "--paginate",
        "-f",
        "per_page=100",
    )
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
    return {
        "generated_at": datetime.now(UTC).isoformat(),
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
