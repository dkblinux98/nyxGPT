#!/usr/bin/env python3
"""Build the nyxGPT Project Retrospective dashboard HTML.

Inputs (all under scripts/retrospective/data/ unless overridden):
  all_issues.json      - full issue corpus: [{n, title, labels, milestone, created}]
  dashboard_data.json  - last-7-days review detail (modules/days/issues/cleanPRs/totals)
  project_fields.json  - OPTIONAL: real Project v2 field snapshot produced by the
                         "Retro Dashboard - Dump Project Fields" workflow. When present,
                         the sprint axis uses the Project's actual Sprint iterations;
                         otherwise calendar weeks stand in (labeled provisional).
  spend.json           - OPTIONAL: per-issue spend telemetry (Claude-invoking workflow
                         steps, total workflow runs, runner minutes, retry/self-heal
                         cycle count) produced by the "Retro Dashboard - Dump Spend
                         Telemetry" workflow (#3696). When present, adds a per-sprint
                         cost view; omitted entirely from the dashboard otherwise.

Output: retro.html next to this script (publish it as the Artifact).

The review-gate monthly series is seeded below (Jan-Jun are historical constants
mined from the complete GitHub notification-email archive); refresh July+ counts
when regenerating.
"""

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Milestone titles get renamed upstream (owner reorgs), so the phase axis is
# derived from the "Phase <n>" prefix rather than from full titles.
MS_SHORT = [
    "Phase 0",
    "Phase 1",
    "Phase 2",
    "Phase 3",
    "Phase 3.5",
    "Phase 4",
    "Phase 5",
    "Phase 5.5",
    "Phase 6",
    "Phase 7",
    "Phase 8",
    "Phase 9",
    "(none)",
]
MS_PREFIX_RE = re.compile(r"^Phase\s+(\d+(?:\.\d+)?)\b")
EXCLUDED_MILESTONES = {"Phase X: Rejected"}


def milestone_short(title):
    """Map a milestone title to its short phase label ('(none)' when unknown)."""
    m = MS_PREFIX_RE.match(title or "")
    short = f"Phase {m.group(1)}" if m else None
    return short if short in MS_SHORT else "(none)"


LABELS = ["Acceptance Failure", "Feature", "Release Management", "Improvement", "Documentation"]
# "pm" = product management failure: an Improvement filed during acceptance
# testing is a spec gap (owner decision 2026-08-01/02), distinct from an
# Acceptance Failure (implementation defect). Counted as its own failure
# statistic everywhere; AF-only aggregates use AF_CAUSES.
CAUSES = ["defect", "spec", "workflow", "pm"]
AF_CAUSES = ("defect", "spec", "workflow")

# Review-gate monthly series: PRs merged (GitHub API) vs PRs with >=1
# REQUEST_CHANGES (notification-email archive). Update the current month on refresh.
# merged/medianHrs are recomputed from data/pr_times.json when present.
GATE = [
    {"m": "Jan", "merged": 153, "rejected": 62},
    {"m": "Feb", "merged": 15, "rejected": 26},
    {"m": "Mar", "merged": 7, "rejected": 5},
    {"m": "Apr", "merged": 0, "rejected": 0},
    {"m": "May", "merged": 0, "rejected": 0},
    {"m": "Jun", "merged": 0, "rejected": 0},
    {"m": "Jul", "merged": 168, "rejected": 80},
    {"m": "Aug", "merged": 0, "rejected": 8},
]

# Release annotations on the sprint axis.
RELEASES = [
    {"date": "2026-01-29", "label": "v1.0.0"},
    {"date": "2026-08-03", "label": "v2.0.0"},  # 2.0.0 tag pushed, master fast-forwarded
]

# Phase -> release era (owner mapping, 2026-07-31): Phases 0-3.5 built v1.0.0,
# Phases 4-5.5 build v2.0.0. Sprint buckets get the era of their majority phase;
# empty sprints inherit the preceding sprint's era.
PHASE_ERA = {
    "Phase 0": "v1.0.0",
    "Phase 1": "v1.0.0",
    "Phase 2": "v1.0.0",
    "Phase 3": "v1.0.0",
    "Phase 3.5": "v1.0.0",
    "Phase 4": "v2.0.0",
    "Phase 5": "v2.0.0",
    "Phase 5.5": "v2.0.0",
    "Phase 6": "v3.0.0",
}

# Recurring-finding themes (last-7-days review findings), first match wins.
THEME_RULES = [
    ("Missing tests / coverage gate", r"test|coverage|vitest|pytest|mock|isolation"),
    (
        "Acceptance criteria not fully implemented",
        r"acceptance criteria|not (shown|implemented|displayed)|missing (the|a )|incomplete|never|doesn.t",
    ),
    (
        "Definition of Done: frontend surface",
        r"frontend|web surface|usable from|dashboard surface|admin page",
    ),
    ("Config / secrets / environment", r"config|secret|token|env\b|\.ini"),
    ("Error handling & edge cases", r"error|exception|race|leak|crash|401|500|edge"),
    ("Docs not updated", r"doc|readme|runbook"),
]

WORKFLOW_RE = re.compile(
    r"agent workflow|review workflow|developer workflow|code review and developer|review gate|review.agent|"
    r"scrummaster|hygiene|accept-and-merge|snapshot step|usage-limit|auto-implement|auto-review|"
    r"branch hygiene|superseded|PR submission|CI Workflow Analytics|smoke.?test script|"
    r"agent branches|closed-issue assignee|skips milestone|unresolved .* issues from",
    re.I,
)
SPEC_RE = re.compile(
    r"redesign|consolidat|retire\b|rework|clarif|back-nav|layout|honest|\bIA\b|menu structure|"
    r"should (match|go to|be consistent)|single pane|disabled state|schema from example|"
    r"fails acceptance",
    re.I,
)
SPEC_OVERRIDES = {3406, 3344, 3346, 3324, 3326, 3328}

MODULE_RULES = [
    (
        "web-ui",
        r"\bweb\b|webui|\bui\b|dashboard|\bpage\b|screen|frontend|sidebar|wizard|admin\b|settings|button|panel|nav\b|menu",
    ),
    (
        "observability",
        r"observab|grafana|loki|prometheus|tempo|jaeger|glitchtip|metrics|logging|\blogs?\b|tracing|instrument|self-heal|canary|monitor|alert|probe|smoke|spog",
    ),
    ("documentation", r"\bdocs?\b|documentation|readme|release notes|changelog"),
    ("testing", r"\btests?\b|testing|coverage|pytest|vitest|flaky"),
    ("security", r"security|secret|api.?key|auth|token|timing attack|vulnerab|cve"),
    ("rag", r"\brag\b|ingest|embedding|retrieval|chunk|collection|epub|document q"),
    ("tui", r"\btui\b"),
    (
        "cli",
        r"\bcli\b|\bops\b|command|homebrew|brew|install|deploy|terraform|k8s|kubernetes|docker|compose|cassandra|ollama|infra",
    ),
]


def detect_module(title):
    t = title.lower()
    for m, pat in MODULE_RULES:
        if re.search(pat, t):
            return m
    return "api"


def classify(issue):
    if "Acceptance Failure" in issue["labels"]:
        if issue["n"] in SPEC_OVERRIDES:
            return "spec"
        if WORKFLOW_RE.search(issue["title"]):
            return "workflow"
        if SPEC_RE.search(issue["title"]):
            return "spec"
        return "defect"
    if "Improvement" in issue["labels"]:
        return "pm"
    return None


def sprint_of_map(project_fields):
    """issue number -> Sprint field value, from a project_fields.json snapshot."""
    sprint_of = {}
    for item in project_fields.get("items", []):
        if item.get("type") not in (None, "ISSUE", "Issue"):
            continue
        for f in item.get("fields", []):
            if f.get("field") == "Sprint" and f.get("value"):
                sprint_of[item["number"]] = f["value"]
    return sprint_of


def sprint_buckets(issues, project_fields):
    """Return (buckets, source). Each bucket: {'w': key, 'label': str|None, 'issues': [...]}.
    With a project snapshot, buckets are the Project's real Sprint iterations
    (issues without a sprint go to a trailing '(no sprint)' bucket); otherwise
    calendar Mondays."""
    if project_fields:
        sprint_of = sprint_of_map(project_fields)
        cal = sorted(project_fields.get("sprints", []), key=lambda s: s["startDate"])
        buckets = [
            {
                "w": s["startDate"],
                "label": s["title"],
                "end": (
                    date.fromisoformat(s["startDate"]) + timedelta(days=s["duration"])
                ).isoformat(),
                "issues": [],
            }
            for s in cal
        ]
        by_title = {b["label"]: b for b in buckets}
        stray = {"w": "9999-12-31", "label": "(no sprint)", "issues": []}
        for i in issues:
            t = sprint_of.get(i["n"])
            (by_title.get(t) or stray)["issues"].append(i)
        if stray["issues"]:
            buckets.append(stray)
        return buckets, "project"
    # calendar fallback
    start, end = date(2025, 12, 29), date.today()
    buckets, d = [], start
    while d <= end:
        buckets.append({"w": d.isoformat(), "label": None, "issues": []})
        d += timedelta(days=7)
    idx = {b["w"]: b for b in buckets}
    for i in issues:
        di = date.fromisoformat(i["created"][:10])
        monday = (di - timedelta(days=di.weekday())).isoformat()
        if monday in idx:
            idx[monday]["issues"].append(i)
    return buckets, "calendar"


def month_of(iso):
    return int(iso[5:7])


def gate_series(issues, pr_times):
    gate = [dict(g) for g in GATE]
    for i in issues:
        if i.get("cause") in AF_CAUSES:
            gate[month_of(i["created"]) - 1].setdefault("af", 0)
            gate[month_of(i["created"]) - 1]["af"] += 1
        elif i.get("cause") == "pm":
            gate[month_of(i["created"]) - 1].setdefault("pm", 0)
            gate[month_of(i["created"]) - 1]["pm"] += 1
    for g in gate:
        g.setdefault("af", 0)
        g.setdefault("pm", 0)
    if pr_times:
        by_month = defaultdict(list)
        merged_count = Counter()
        for created, merged in pr_times.values():
            m = month_of(merged)
            merged_count[m] += 1
            dt = datetime.fromisoformat(merged.replace("Z", "+00:00")) - datetime.fromisoformat(
                created.replace("Z", "+00:00")
            )
            by_month[m].append(dt.total_seconds() / 3600)
        for g, m in zip(gate, range(1, 1 + len(gate)), strict=False):
            g["merged"] = merged_count.get(m, 0)
            g["medianHrs"] = round(statistics.median(by_month[m]), 1) if by_month.get(m) else None
    return gate


def aging_flow(issues, now):
    open_af = [
        i for i in issues if i.get("cause") in AF_CAUSES and i.get("state", "").upper() == "OPEN"
    ]
    buckets = [("<2d", 0, 2), ("2-7d", 2, 7), ("7-30d", 7, 30), (">30d", 30, 10**6)]
    aging = []
    for name, lo, hi in buckets:
        n = sum(
            1
            for i in open_af
            if lo <= (now - datetime.fromisoformat(i["created"].replace("Z", "+00:00"))).days < hi
        )
        aging.append({"bucket": name, "n": n})
    flow = [{"m": g["m"], "opened": 0, "closed": 0} for g in GATE]
    for i in issues:
        if i.get("cause") not in AF_CAUSES:
            continue
        flow[month_of(i["created"]) - 1]["opened"] += 1
        if i.get("closed"):
            flow[month_of(i["closed"]) - 1]["closed"] += 1
    return aging, flow, len(open_af)


def finding_themes(reviews):
    counts = Counter()
    for r in reviews:
        for sev in ("critical", "medium", "minor"):
            for t in r.get(sev, []):
                for name, pat in THEME_RULES:
                    if re.search(pat, t, re.I):
                        counts[name] += 1
                        break
                else:
                    counts["(unclustered)"] += 1
    other = counts.pop("(unclustered)", 0)
    top = [{"theme": k, "n": v} for k, v in counts.most_common(5)]
    return {"top": top, "other": other}


def af_sum(cause_dict):
    return sum(v for k, v in cause_dict.items() if k in AF_CAUSES)


def spend_by_sprint(spend, project_fields):
    """Aggregate dump_spend.py's per-issue data/spend.json by sprint: totals
    plus the per-issue distribution, so retrospectives can surface cost
    regressions and outliers (#3696). Issues without a Sprint field value
    (or with no project_fields.json snapshot at all) land in '(no sprint)'."""
    if not spend:
        return None
    sprint_of = sprint_of_map(project_fields) if project_fields else {}
    per_issue = []
    for n_str, b in spend["issues"].items():
        n = int(n_str)
        per_issue.append(
            {
                "issue": n,
                "sprint": sprint_of.get(n) or "(no sprint)",
                "claude_steps": b["claude_steps"],
                "runs": b["runs"],
                "runner_minutes": b["runner_minutes"],
                "retry_cycles": b["retry_cycles"],
            }
        )

    sprints = defaultdict(
        lambda: {
            "issues": 0,
            "claude_steps": 0,
            "runs": 0,
            "runner_minutes": 0.0,
            "retry_cycles": 0,
            "perIssue": [],
        }
    )
    for row in per_issue:
        s = sprints[row["sprint"]]
        s["issues"] += 1
        s["claude_steps"] += row["claude_steps"]
        s["runs"] += row["runs"]
        s["runner_minutes"] += row["runner_minutes"]
        s["retry_cycles"] += row["retry_cycles"]
        s["perIssue"].append(row)

    by_sprint = []
    for name, agg in sprints.items():
        agg["runner_minutes"] = round(agg["runner_minutes"], 2)
        agg["perIssue"].sort(key=lambda r: r["runner_minutes"], reverse=True)
        by_sprint.append({"sprint": name, **agg})
    if project_fields:
        order = [
            s["title"]
            for s in sorted(project_fields.get("sprints", []), key=lambda s: s["startDate"])
        ]
        order.append("(no sprint)")
        rank = {name: i for i, name in enumerate(order)}
        by_sprint.sort(key=lambda s: rank.get(s["sprint"], len(order)))
    else:
        by_sprint.sort(key=lambda s: s["sprint"])

    unattributed = spend["unattributed"]
    totals = {
        "issues": len(per_issue),
        "claude_steps": sum(r["claude_steps"] for r in per_issue) + unattributed["claude_steps"],
        "runs": sum(r["runs"] for r in per_issue) + unattributed["runs"],
        "runner_minutes": round(
            sum(r["runner_minutes"] for r in per_issue) + unattributed["runner_minutes"], 2
        ),
        "retry_cycles": sum(r["retry_cycles"] for r in per_issue) + unattributed["retry_cycles"],
    }
    outliers = sorted(per_issue, key=lambda r: r["runner_minutes"], reverse=True)[:10]

    return {
        "bySprint": by_sprint,
        "totals": totals,
        "unattributed": unattributed,
        "outliers": outliers,
        "generatedAt": spend.get("generated_at"),
    }


def takeaways(issues, dashboard, weeks, open_af):
    out = []
    mods = dashboard.get("modules", {})
    if mods:
        worst, v = max(mods.items(), key=lambda kv: kv[1]["C"] + kv[1]["M"])
        out.append(
            {
                "k": "Worst module (7d)",
                "v": f"{worst} — {v['C'] + v['M']} blocking findings across {v['rounds']} rejected rounds",
            }
        )

    # Compare the two most recent sprints that actually carry acceptance
    # failures. Sprints planned ahead (issues filed, none accepted yet) would
    # otherwise render the trend as a meaningless "0 vs 0".
    def sprint_name(w):
        return w.get("sname") or w["w"]

    fail_weeks = [w for w in weeks if w.get("sname") != "(no sprint)" and af_sum(w["cause"]) > 0]
    if fail_weeks:
        cur = af_sum(fail_weeks[-1]["cause"])
        name = sprint_name(fail_weeks[-1])
        if len(fail_weeks) >= 2:
            prev = af_sum(fail_weeks[-2]["cause"])
            delta = cur - prev
            v = (
                f"{cur} acceptance failure{'s' if cur != 1 else ''} in {name} "
                f"vs {prev} in {sprint_name(fail_weeks[-2])} "
                f"({'+' if delta >= 0 else ''}{delta})"
            )
        else:
            v = f"{cur} acceptance failure{'s' if cur != 1 else ''} in {name}"
        out.append({"k": "Failure trend", "v": v})
    items = dashboard.get("issues", [])
    if items:
        sticky = max(items, key=lambda i: i["rounds"])
        ref = f"#{sticky['issue']}" if sticky.get("issue") else f"PR #{sticky['pr']}"
        out.append(
            {
                "k": "Stickiest item (7d)",
                "v": f"{ref} — {sticky['rounds']} review rounds ({sticky['module']})",
            }
        )
    out.append(
        {"k": "Open failure backlog", "v": f"{open_af} acceptance-failure issues currently open"}
    )
    pm = [i for i in issues if i.get("cause") == "pm"]
    pm_open = sum(1 for i in pm if i.get("state", "").upper() == "OPEN")
    out.append(
        {
            "k": "Product management failures",
            "v": f"{len(pm)} Improvement issues (spec gaps found in acceptance), {pm_open} open",
        }
    )
    return out


def build_qdata(issues, project_fields):
    issues = [i for i in issues if i.get("milestone") not in EXCLUDED_MILESTONES]
    real_module = {}
    if project_fields:
        for item in project_fields.get("items", []):
            for f in item.get("fields", []):
                if f.get("field") == "Module" and f.get("value"):
                    real_module[item["number"]] = f["value"]
    for i in issues:
        i["module"] = real_module.get(i["n"]) or detect_module(i["title"])
        i["cause"] = classify(i)
    modc = Counter(i["module"] for i in issues)
    mods = [m for m, _ in modc.most_common(7)]
    mods_all = mods + ["other"]

    def mod(i):
        return i["module"] if i["module"] in mods else "other"

    def empty():
        return {
            "cause": dict.fromkeys(CAUSES, 0),
            "label": dict.fromkeys(LABELS, 0),
            "module": dict.fromkeys(mods_all, 0),
        }

    buckets, source = sprint_buckets(issues, project_fields)
    weeks = []
    prev_era = None
    for b in buckets:
        agg = empty()
        for i in b["issues"]:
            for label in i["labels"]:
                if label in LABELS:
                    agg["label"][label] += 1
            agg["module"][mod(i)] += 1
            if i["cause"]:
                agg["cause"][i["cause"]] += 1
        votes = Counter()
        for i in b["issues"]:
            s = milestone_short(i["milestone"])
            if s in PHASE_ERA:
                votes[PHASE_ERA[s]] += 1
        era = votes.most_common(1)[0][0] if votes else prev_era
        prev_era = era
        weeks.append({"w": b["w"], "sname": b.get("label"), "era": era, "end": b.get("end"), **agg})

    ms = {s: {**empty(), "total": 0, "af": 0} for s in MS_SHORT}
    for i in issues:
        s = milestone_short(i["milestone"])
        for label in i["labels"]:
            if label in LABELS:
                ms[s]["label"][label] += 1
        ms[s]["module"][mod(i)] += 1
        ms[s]["total"] += 1
        if i["cause"]:
            ms[s]["cause"][i["cause"]] += 1
            if i["cause"] in AF_CAUSES:
                ms[s]["af"] += 1

    return {
        "weeks": weeks,
        "sprintSource": source,
        "milestones": [{"name": s, **ms[s]} for s in MS_SHORT if ms[s]["total"] > 0],
        "gate": GATE,
        "issues_classified": issues,
        "qtotals": {
            "issues": len(issues),
            "af": sum(1 for i in issues if i["cause"] in AF_CAUSES),
            "defect": sum(1 for i in issues if i["cause"] == "defect"),
            "spec": sum(1 for i in issues if i["cause"] == "spec"),
            "workflow": sum(1 for i in issues if i["cause"] == "workflow"),
            "pm": sum(1 for i in issues if i["cause"] == "pm"),
            "production": sum(1 for i in issues if "Production Defect" in i["labels"]),
        },
        "lens": {"causes": CAUSES, "labels": LABELS, "modules": mods_all},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(HERE / "data"))
    ap.add_argument("--template", default=str(HERE / "retro_template.html"))
    ap.add_argument("--out", default=str(HERE / "retro.html"))
    args = ap.parse_args()
    data = Path(args.data_dir)

    issues = json.loads((data / "all_issues.json").read_text())
    dashboard = json.loads((data / "dashboard_data.json").read_text())
    pf_path = data / "project_fields.json"
    project_fields = json.loads(pf_path.read_text()) if pf_path.exists() else None
    pt_path = data / "pr_times.json"
    pr_times = json.loads(pt_path.read_text()) if pt_path.exists() else None
    rv_path = data / "reviews_final.json"
    reviews = json.loads(rv_path.read_text()) if rv_path.exists() else []
    sp_path = data / "spend.json"
    spend = json.loads(sp_path.read_text()) if sp_path.exists() else None

    qdata = build_qdata(issues, project_fields)
    classified = qdata.pop("issues_classified")
    qdata["gate"] = gate_series(classified, pr_times)
    now = datetime.now(UTC)
    qdata["aging"], qdata["flow"], open_af = aging_flow(classified, now)
    qdata["themes"] = finding_themes(reviews)
    qdata["takeaways"] = takeaways(classified, dashboard, qdata["weeks"], open_af)
    qdata["releases"] = RELEASES
    qdata["spend"] = spend_by_sprint(spend, project_fields)
    html = Path(args.template).read_text()
    html = re.sub(r"across all \d+ issues", f"across all {qdata['qtotals']['issues']} issues", html)
    html = re.sub(
        r'The \d+ issues labeled <span class="mono">Acceptance Failure</span>',
        f'The {qdata["qtotals"]["af"]} issues labeled <span class="mono">Acceptance Failure</span>',
        html,
    )
    html = re.sub(
        r'The \d+ issues labeled <span class="mono">Improvement</span>',
        f'The {qdata["qtotals"]["pm"]} issues labeled <span class="mono">Improvement</span>',
        html,
    )
    html = html.replace("__QDATA__", json.dumps(qdata, separators=(",", ":")))
    html = html.replace("__DATA__", json.dumps(dashboard, separators=(",", ":")))
    Path(args.out).write_text(html)
    print(f"built {args.out}: {qdata['qtotals']} sprintSource={qdata['sprintSource']}")


if __name__ == "__main__":
    main()
