#!/usr/bin/env python3
"""Build the nyxGPT Project Retrospective dashboard HTML.

Inputs (all under scripts/retrospective/data/ unless overridden):
  all_issues.json      - full issue corpus: [{n, title, labels, milestone, created}]
  dashboard_data.json  - last-7-days review detail (modules/days/issues/cleanPRs/totals)
  project_fields.json  - OPTIONAL: real Project v2 field snapshot produced by the
                         "Retro Dashboard - Dump Project Fields" workflow. When present,
                         the sprint axis uses the Project's actual Sprint iterations;
                         otherwise calendar weeks stand in (labeled provisional).

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
CAUSES = ["defect", "spec", "workflow"]

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
    {"m": "Aug", "merged": 0, "rejected": 3},
]

# Release annotations on the sprint axis.
RELEASES = [
    {"date": "2026-01-29", "label": "v1.0.0"},
    {"date": "2026-07-20", "label": "v2.0.0"},  # approx: Post Release 2.0.0 milestone created
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
    if "Acceptance Failure" not in issue["labels"]:
        return None
    if issue["n"] in SPEC_OVERRIDES:
        return "spec"
    if WORKFLOW_RE.search(issue["title"]):
        return "workflow"
    if SPEC_RE.search(issue["title"]):
        return "spec"
    return "defect"


def sprint_buckets(issues, project_fields):
    """Return (buckets, source). Each bucket: {'w': key, 'label': str|None, 'issues': [...]}.
    With a project snapshot, buckets are the Project's real Sprint iterations
    (issues without a sprint go to a trailing '(no sprint)' bucket); otherwise
    calendar Mondays."""
    if project_fields:
        sprint_of = {}
        for item in project_fields.get("items", []):
            if item.get("type") not in (None, "ISSUE", "Issue"):
                continue
            for f in item.get("fields", []):
                if f.get("field") == "Sprint" and f.get("value"):
                    sprint_of[item["number"]] = f["value"]
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
        if i.get("cause"):
            gate[month_of(i["created"]) - 1].setdefault("af", 0)
            gate[month_of(i["created"]) - 1]["af"] += 1
    for g in gate:
        g.setdefault("af", 0)
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
    open_af = [i for i in issues if i.get("cause") and i.get("state", "").upper() == "OPEN"]
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
        if not i.get("cause"):
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


def takeaways(_issues, dashboard, weeks, open_af):
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
    data_weeks = [
        w
        for w in weeks
        if w.get("sname") != "(no sprint)"
        and (sum(w["cause"].values()) or sum(w["label"].values()))
    ]
    if len(data_weeks) >= 2:
        cur, prev = sum(data_weeks[-1]["cause"].values()), sum(data_weeks[-2]["cause"].values())
        delta = cur - prev
        name = data_weeks[-1].get("sname") or "this sprint"
        out.append(
            {
                "k": "Failure trend",
                "v": (
                    f"{cur} acceptance failure{'s' if cur != 1 else ''} in {name} "
                    f"vs {prev} last sprint ({'+' if delta >= 0 else ''}{delta})"
                ),
            }
        )
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
            ms[s]["af"] += 1
            ms[s]["cause"][i["cause"]] += 1

    return {
        "weeks": weeks,
        "sprintSource": source,
        "milestones": [{"name": s, **ms[s]} for s in MS_SHORT if ms[s]["total"] > 0],
        "gate": GATE,
        "issues_classified": issues,
        "qtotals": {
            "issues": len(issues),
            "af": sum(1 for i in issues if i["cause"]),
            "defect": sum(1 for i in issues if i["cause"] == "defect"),
            "spec": sum(1 for i in issues if i["cause"] == "spec"),
            "workflow": sum(1 for i in issues if i["cause"] == "workflow"),
            "production": 0,
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

    qdata = build_qdata(issues, project_fields)
    classified = qdata.pop("issues_classified")
    qdata["gate"] = gate_series(classified, pr_times)
    now = datetime.now(UTC)
    qdata["aging"], qdata["flow"], open_af = aging_flow(classified, now)
    qdata["themes"] = finding_themes(reviews)
    qdata["takeaways"] = takeaways(classified, dashboard, qdata["weeks"], open_af)
    qdata["releases"] = RELEASES
    html = Path(args.template).read_text()
    html = re.sub(r"across all \d+ issues", f"across all {qdata['qtotals']['issues']} issues", html)
    html = html.replace("__QDATA__", json.dumps(qdata, separators=(",", ":")))
    html = html.replace("__DATA__", json.dumps(dashboard, separators=(",", ":")))
    Path(args.out).write_text(html)
    print(f"built {args.out}: {qdata['qtotals']} sprintSource={qdata['sprintSource']}")


if __name__ == "__main__":
    main()
