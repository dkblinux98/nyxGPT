#!/usr/bin/env python3
"""Dump GitHub-native review-round detail for the retrospective dashboard.

Replaces the Gmail notification-email parse (owner decision 2026-08-08,
#3667): the review agent posts every `## Code Review - REQUEST_CHANGES`
round as a pull-request review, so rounds and their Critical/Medium/Minor
findings are queryable via the GitHub API instead of a personal mailbox.

Invoked only by `.github/workflows/retro_review_rounds_dump.yml`, which runs
this with `gh` authenticated (GH_TOKEN) and REPO set to "owner/repo". Not a
library for build_dashboard.py — that script must stay free of live API
calls and only reads the two files this writes:

  data/reviews_final.json  - every REQUEST_CHANGES review round, all time
  data/dashboard_data.json - last-WINDOW_DAYS-days rollup (modules/days/
                              issues/cleanPRs/cleanByModule/totals)
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from build_dashboard import detect_module  # noqa: E402
from dump_spend import iter_json_objects  # noqa: E402  (one implementation, #3808)

WINDOW_DAYS = 7
DATA_DIR = HERE / "data"

SECTION_RE = re.compile(r"^#{3,4}\s*(Critical|Medium|Minor)\s+Issues\b", re.I)
TOP_BULLET_RE = re.compile(r"^-\s+(.*)$")
BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def search_issues(query):
    out = gh(
        "api", "-X", "GET", "search/issues", "--paginate", "-f", f"q={query}", "-f", "per_page=100"
    )
    items = []
    for page in iter_json_objects(out):
        items.extend(page.get("items", []))
    return items


def parse_review_body(body):
    """Extract Critical/Medium/Minor finding titles from a review-agent body.

    Findings are `- **bold title**: description` bullets; some sections (seen
    in practice for Minor) omit the bold lead-in, in which case the whole
    bullet is the title. Nested (indented) sub-bullets are description detail,
    not separate findings, so only column-0 `-` bullets count. `None.` (no
    bullet) for an empty section is naturally skipped since it isn't a bullet.
    """
    if not body or "## Code Review - REQUEST_CHANGES" not in body:
        return None
    findings = {"critical": [], "medium": [], "minor": []}
    current = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = SECTION_RE.match(stripped)
        if m:
            current = m.group(1).lower()
            continue
        if stripped.startswith("#"):
            current = None
            continue
        if current is None:
            continue
        b = TOP_BULLET_RE.match(line)
        if b:
            rest = b.group(1)
            bm = BOLD_RE.match(rest)
            findings[current].append((bm.group(1) if bm else rest).strip())
    return findings


def dump_all_rounds(repo):
    """One round per pull-request-review id, across every PR, all time."""
    prs = []
    out = gh(
        "api",
        "-X",
        "GET",
        f"repos/{repo}/pulls",
        "--paginate",
        "-f",
        "state=all",
        "-f",
        "per_page=100",
    )
    for page in iter_json_objects(out):
        prs.extend(page)

    closes_re = re.compile(r"\bCloses\s+#(\d+)\b", re.I)
    rounds = []
    for pr in prs:
        number = pr["number"]
        title = pr["title"]
        m = closes_re.search(pr.get("body") or "")
        issue = int(m.group(1)) if m else None
        module = detect_module(title)
        reviews_out = gh(
            "api",
            "-X",
            "GET",
            f"repos/{repo}/pulls/{number}/reviews",
            "--paginate",
            "-f",
            "per_page=100",
        )
        for page in iter_json_objects(reviews_out):
            for rv in page:
                findings = parse_review_body(rv.get("body") or "")
                if findings is None:
                    continue
                rounds.append(
                    {
                        "issue": issue,
                        "pr": number,
                        "title": f"[{repo}] {title} (PR #{number})",
                        "module": module,
                        "date": rv["submitted_at"],
                        "critical": findings["critical"],
                        "medium": findings["medium"],
                        "minor": findings["minor"],
                    }
                )
    rounds.sort(key=lambda r: r["date"])
    return rounds


def window_merged_prs(repo, start, end):
    query = f"repo:{repo} is:pr is:merged merged:{start}..{end}"
    prs = []
    for item in search_issues(query):
        prs.append(
            {
                "number": item["number"],
                "title": item["title"],
                "module": detect_module(item["title"]),
                "merged": item["pull_request"]["merged_at"],
            }
        )
    return prs


def window_unreviewed_numbers(repo, start, end):
    query = f"repo:{repo} is:pr is:merged merged:{start}..{end} review:none"
    return {item["number"] for item in search_issues(query)}


def build_dashboard_snapshot(all_rounds, window_start, window_end, merged_prs, unreviewed_numbers):
    rounds_in_window = [r for r in all_rounds if window_start <= r["date"] < window_end]
    rejected_ever = {r["pr"] for r in all_rounds}

    modules = defaultdict(lambda: {"C": 0, "M": 0, "m": 0, "rounds": 0, "_items": set()})
    days = defaultdict(lambda: {"C": 0, "M": 0, "m": 0, "rounds": 0})
    items = {}
    for r in rounds_in_window:
        key = (r["issue"], r["pr"])
        c, med, mi = len(r["critical"]), len(r["medium"]), len(r["minor"])

        mod = modules[r["module"]]
        mod["C"] += c
        mod["M"] += med
        mod["m"] += mi
        mod["rounds"] += 1
        mod["_items"].add(key)

        day = days[r["date"][:10]]
        day["C"] += c
        day["M"] += med
        day["m"] += mi
        day["rounds"] += 1

        it = items.setdefault(
            key,
            {
                "issue": r["issue"],
                "pr": r["pr"],
                "title": r["title"],
                "module": r["module"],
                "rounds": 0,
                "C": 0,
                "M": 0,
                "m": 0,
                "last": r["date"],
                "findings": [],
            },
        )
        it["rounds"] += 1
        it["C"] += c
        it["M"] += med
        it["m"] += mi
        it["last"] = max(it["last"], r["date"])
        for sev, titles in (
            ("Critical", r["critical"]),
            ("Medium", r["medium"]),
            ("Minor", r["minor"]),
        ):
            for t in titles:
                it["findings"].append([sev, t])

    modules_out = {
        mod: {
            "C": v["C"],
            "M": v["M"],
            "m": v["m"],
            "rounds": v["rounds"],
            "issues": len(v["_items"]),
        }
        for mod, v in modules.items()
    }
    issues_out = sorted(items.values(), key=lambda i: i["rounds"], reverse=True)

    clean_prs = [
        {
            "pr": pr["number"],
            "merged": pr["merged"][:10],
            "title": pr["title"],
            "module": pr["module"],
        }
        for pr in merged_prs
        if pr["number"] not in unreviewed_numbers and pr["number"] not in rejected_ever
    ]
    clean_prs.sort(key=lambda p: p["merged"])
    clean_by_module = dict(Counter(p["module"] for p in clean_prs))

    totals = {
        "rounds": sum(v["rounds"] for v in modules_out.values()),
        "C": sum(v["C"] for v in modules_out.values()),
        "M": sum(v["M"] for v in modules_out.values()),
        "m": sum(v["m"] for v in modules_out.values()),
        "items": len(issues_out),
        "clean": len(clean_prs),
        "reviewed": len(merged_prs) - len(unreviewed_numbers),
        "merged": len(merged_prs),
        "unreviewed": len(unreviewed_numbers),
    }
    return {
        "modules": modules_out,
        "days": dict(days),
        "issues": issues_out,
        "cleanPRs": clean_prs,
        "cleanByModule": clean_by_module,
        "totals": totals,
    }


def main():
    repo = os.environ["REPO"]
    all_rounds = dump_all_rounds(repo)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "reviews_final.json").write_text(json.dumps(all_rounds, indent=1) + "\n")
    print(f"wrote {len(all_rounds)} review rounds to data/reviews_final.json")

    now = datetime.now(UTC)
    end_date = now.date()
    start_date = end_date - timedelta(days=WINDOW_DAYS)
    window_start = start_date.isoformat() + "T00:00:00Z"
    window_end = (end_date + timedelta(days=1)).isoformat() + "T00:00:00Z"

    merged_prs = window_merged_prs(repo, start_date.isoformat(), end_date.isoformat())
    unreviewed_numbers = window_unreviewed_numbers(
        repo, start_date.isoformat(), end_date.isoformat()
    )
    snapshot = build_dashboard_snapshot(
        all_rounds, window_start, window_end, merged_prs, unreviewed_numbers
    )
    (DATA_DIR / "dashboard_data.json").write_text(json.dumps(snapshot, indent=1) + "\n")
    print(f"wrote {WINDOW_DAYS}-day snapshot to data/dashboard_data.json: {snapshot['totals']}")


if __name__ == "__main__":
    main()
