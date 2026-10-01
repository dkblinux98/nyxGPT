#!/usr/bin/env python3
"""Dump the issue corpus and merged-PR times for the retrospective dashboard.

Invoked by `.github/workflows/retro_data_refresh.yml`, which runs this with
`gh` authenticated (GH_TOKEN) and REPO set to "owner/repo". Not a library for
build_dashboard.py -- that script stays free of live API calls and only reads
the two files this writes:

  data/all_issues.json  {"generated_at", "issues": [{n, title, labels,
                         milestone, created, state, closed[, related]}]}
                        -- every issue created on or after CORPUS_START.
  data/pr_times.json    {"generated_at", "prs": {"<number>": [created_at,
                         merged_at]}} -- every pull request merged on or
                        after CORPUS_START.

Until 2026-09 these two were the only inputs written by the refresh session
by hand (GitHub MCP queries paginated into a file), which is what let the
session drift into generating the *other* inputs locally too and skipping
the dumps that cannot be run locally at all. Producing them here makes the
workflow the only producer of every input the dashboard reads.

Shapes are deliberately those the builder already reads: `load_issues`
accepts the stamped envelope, and `load_pr_times` accepts the stamped
envelope plus the historical bare `{number: [created, merged]}` map.

The `related` field is retired (owner decision 2026-08-12, #3731) and is
never written fresh, but a value already recorded for an issue in the
existing file is carried over: build_dashboard.py still reads it as the
documented fallback for issues that predate native relationships.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from dump_spend import gh, iter_json_objects  # noqa: E402  (one implementation, #3808)

DATA_DIR = HERE / "data"

# The dashboard covers "Jan 1 – <today>" (the corpus-coverage copy the builder
# derives); issues created and pull requests merged before this are outside it.
CORPUS_START = "2026-01-01T00:00:00Z"


def _paginated(repo, path, *params, source):
    """Every item of a paginated list endpoint, as one flat list."""
    args = ["api", "-X", "GET", f"repos/{repo}/{path}", "--paginate", "-f", "per_page=100"]
    for p in params:
        args += ["-f", p]
    items = []
    for page in iter_json_objects(gh(*args), source=source):
        items.extend(page)
    return items


def fetch_issues(repo, start=CORPUS_START):
    """Issues (not pull requests) created on or after `start`, ascending by number.

    Uses the repository issues list rather than the search API: search caps a
    query at 1,000 results and fails on the eleventh page, which the corpus
    will silently grow past. `since` is an updated-at filter, so it is only a
    server-side page cut -- anything created after `start` was necessarily
    updated after it -- and the created-at filter below is the real one.
    """
    raw = _paginated(
        repo,
        "issues",
        "state=all",
        f"since={start}",
        "sort=created",
        "direction=asc",
        source=f"issues of {repo} since {start}",
    )
    issues = []
    for item in raw:
        if "pull_request" in item or (item.get("created_at") or "") < start:
            continue
        issues.append(
            {
                "n": item["number"],
                "title": item.get("title") or "",
                "labels": [lb["name"] for lb in item.get("labels") or []],
                "milestone": (item.get("milestone") or {}).get("title"),
                "created": item["created_at"],
                "state": item.get("state"),
                "closed": item.get("closed_at"),
            }
        )
    issues.sort(key=lambda i: i["n"])
    return issues


def fetch_pr_times(repo, start=CORPUS_START):
    """{"<number>": [created_at, merged_at]} for PRs merged on or after `start`."""
    raw = _paginated(
        repo,
        "pulls",
        "state=closed",
        "sort=updated",
        "direction=desc",
        source=f"closed pulls of {repo}",
    )
    prs = {}
    for pr in raw:
        merged = pr.get("merged_at")
        if not merged or merged < start:
            continue
        prs[str(pr["number"])] = [pr["created_at"], merged]
    return dict(sorted(prs.items(), key=lambda kv: int(kv[0])))


def carry_related(issues, existing_path):
    """Copy a recorded `related` value onto the same issue in the new corpus."""
    if not existing_path.exists():
        return issues
    try:
        raw = json.loads(existing_path.read_text())
    except (OSError, json.JSONDecodeError):
        return issues
    old = raw.get("issues") if isinstance(raw, dict) else raw
    related = {
        int(i["n"]): i["related"]
        for i in old or []
        if isinstance(i, dict) and i.get("n") is not None and i.get("related") is not None
    }
    for issue in issues:
        if issue["n"] in related:
            issue["related"] = related[issue["n"]]
    return issues


def main():
    repo = os.environ["REPO"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()

    issues = carry_related(fetch_issues(repo), DATA_DIR / "all_issues.json")
    (DATA_DIR / "all_issues.json").write_text(
        json.dumps({"generated_at": generated_at, "issues": issues}, indent=1) + "\n"
    )
    print(f"wrote {len(issues)} issues to data/all_issues.json (created >= {CORPUS_START[:10]})")

    prs = fetch_pr_times(repo)
    (DATA_DIR / "pr_times.json").write_text(
        json.dumps({"generated_at": generated_at, "prs": prs}, indent=1) + "\n"
    )
    print(f"wrote {len(prs)} merged PRs to data/pr_times.json (merged >= {CORPUS_START[:10]})")


if __name__ == "__main__":
    main()
