#!/usr/bin/env python3
"""Build the nyxGPT Project Retrospective dashboard HTML.

Inputs (all under scripts/retrospective/data/ unless overridden):
  all_issues.json      - full issue corpus: [{n, title, labels, milestone, created}],
                         or {generated_at, issues: [...]} when the refresh stamped
                         it (#3807). Both shapes are read; the stamped one lets the
                         page report the corpus's own as-of time instead of "unknown".
  dashboard_data.json  - last-7-days review detail (modules/days/issues/cleanPRs/totals)
  project_fields.json  - OPTIONAL: real Project v2 field snapshot produced by the
                         "Retro Dashboard - Dump Project Fields" workflow. When present,
                         the sprint axis uses the Project's actual Sprint iterations;
                         otherwise calendar weeks stand in (labeled provisional).
  reviews_final.json   - every REQUEST_CHANGES review round, all time, produced by the
                         "Retro Dashboard - Dump Review Rounds" workflow from the GitHub
                         PR-review API (owner decision 2026-08-08, #3667 — this used to be
                         a Gmail notification-email parse; no live API calls happen here,
                         this script only reads the committed dump).
  spend.json           - OPTIONAL: per-issue spend telemetry (Claude-invoking workflow
                         steps, total workflow runs, runner minutes, retry/self-heal
                         cycle count) produced by the "Retro Dashboard - Dump Spend
                         Telemetry" workflow (#3696). When present, adds a per-sprint
                         cost view; when absent the section renders as explicitly
                         unavailable and the build exits non-zero (#3808).
  churn.json           - OPTIONAL: per-round churn-cost telemetry (tokens split into
                         context re-establishment vs change production, per-issue
                         onboarding tax across rounds, stale-context incident tally)
                         produced by the "Retro Dashboard - Dump Churn Cost" workflow
                         (#3776). Dollars appear only when that dump ran with a price
                         sheet configured; when the file is absent the section renders
                         as explicitly unavailable and the build exits non-zero (#3808).
  relationships.json   - OPTIONAL: native issue relationships (blocked-by/blocks)
                         produced by the "Retro Dashboard - Dump Relationships"
                         workflow (#3731). Failure/improvement attribution is read
                         from here, NOT from `Related feature: #N` body prose. When
                         an issue has no native edge the corpus's prose-derived
                         `related` field is used as a documented fallback so
                         historical issues keep attributing correctly.

Output: retro.html next to this script (publish it as the Artifact).

The page stamps itself: `qdata["build"]` carries the UTC time this script ran
plus each input's own `generated_at`, so a reader can tell how old the page is
and how far behind it any one dump has fallen (#3807).

The review-gate monthly series is seeded below (older months are historical
constants mined before the review agent posted rounds as PR reviews); the
current month's rejected count is instead computed live from
data/reviews_final.json in gate_series().
"""

import argparse
import json
import re
import statistics
import sys
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

# Data-source provenance (#3807). This dashboard is a static artifact rebuilt
# on demand, so "how old is what I am reading?" has to be answerable from the
# page itself: the build stamp says when the HTML was produced, and each dump's
# own `generated_at` says how old the data behind a section is. Every dump in a
# refresh pass is dispatched in the same session, so a source a day or more
# behind the build did not get refreshed in that pass and is called out.
STALE_SOURCE_DAYS = 1.0

# Exit status when an input dump did not land (#3808). The page is still
# written — every missing section says on its face that it is unavailable —
# but the build refuses to report success, so a refresh cannot publish a
# quietly-incomplete dashboard the way it did for spend and churn.
MISSING_SOURCE_EXIT = 2


def load_issues(path):
    """Read all_issues.json in either shape, returning (issues, generated_at).

    The corpus has historically been a bare list, which carries no refresh
    stamp — the page then reports its as-of time as unknown rather than
    implying it is as fresh as the build (#3807). A refresh may instead write
    {"generated_at": ..., "issues": [...]}, so the stamp can start being
    recorded without a flag day.
    """
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        return raw.get("issues") or [], raw.get("generated_at")
    return raw, None


def parse_stamp(value):
    """Parse a dump's `generated_at` into an aware datetime (None if unusable)."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def source_stamps(now, sources):
    """Per-source as-of provenance for the page's freshness lines (#3807).

    `sources` is an iterable of
    (key, label, filename, present, generated_at, workflow).
    A source whose file carries no parseable stamp keeps `generatedAt: None`,
    which the page renders as an explicit "unknown" — an unstamped dump must
    not be able to pass for a freshly refreshed one. `stale` marks data
    materially older than the build, so a week-old dump cannot hide behind a
    build that ran a minute ago.

    `workflow` names the dump that owes the file (None for the hand-written
    corpus). It is what turns an absent source from a blank space into an
    actionable line on the page: which run to go and read (#3808).
    """
    out = {}
    for key, label, filename, present, generated_at, workflow in sources:
        stamp = parse_stamp(generated_at)
        age = (now - stamp).total_seconds() / 86400 if stamp else None
        out[key] = {
            "label": label,
            "file": filename,
            "generatedAt": stamp.isoformat() if stamp else None,
            "ageDays": round(age, 2) if age is not None else None,
            "stale": bool(age is not None and age >= STALE_SOURCE_DAYS),
            "present": present,
            "workflow": workflow,
        }
    return out


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
# REQUEST_CHANGES round (GitHub PR-review API, data/reviews_final.json).
# merged/medianHrs are recomputed from data/pr_times.json when present; the
# current month's "rejected" is recomputed from reviews_final.json in
# gate_series() — older months predate that dump and keep their seeded value.
MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]  # fmt: skip
GATE = [
    {"m": "Jan", "merged": 153, "rejected": 62},
    {"m": "Feb", "merged": 15, "rejected": 26},
    {"m": "Mar", "merged": 7, "rejected": 5},
    {"m": "Apr", "merged": 0, "rejected": 0},
    {"m": "May", "merged": 0, "rejected": 0},
    {"m": "Jun", "merged": 0, "rejected": 0},
    {"m": "Jul", "merged": 168, "rejected": 80},
    # August closed on 2026-09-01. `gate_series()` only recomputes the CURRENT
    # month's `rejected`, so a month keeps whatever seed it holds once it rolls
    # over -- and August's was still the placeholder 8 it was given early in the
    # month. Frozen here to the value the chart's own legend promises ("PRs
    # rejected >=1x"): distinct PRs with at least one REQUEST_CHANGES round in
    # August, counted from the now-complete reviews_final.json (135 of them).
    {"m": "Aug", "merged": 0, "rejected": 135},
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


def blocks_map(relationships):
    """issue number -> the issues it natively blocks, from relationships.json."""
    if not relationships:
        return {}
    out = {}
    for key, entry in (relationships.get("issues") or {}).items():
        blocks = [int(b) for b in (entry or {}).get("blocks") or []]
        if blocks:
            out[int(key)] = blocks
    return out


def attribute_related(issues, relationships):
    """Attach `related` / `relatedSource` to every issue, native first (#3731).

    The native blocked-by/blocks relationship is the storage, so it wins.
    Issues filed before that change have no native edge and fall back to the
    corpus's `related` field (derived from the retired `Related feature: #N`
    body line when the corpus was refreshed) -- the documented read-both
    fallback, so historical attribution does not disappear.

    Returns the source counts, which `qtotals` surfaces: when `prose` reaches
    zero the fallback can be deleted outright.
    """
    native = blocks_map(relationships)
    counts = {"native": 0, "prose": 0, "none": 0}
    for issue in issues:
        blocked = native.get(int(issue["n"]))
        if blocked:
            issue["related"] = blocked[0]
            issue["relatedSource"] = "native"
        elif issue.get("related"):
            issue["related"] = int(issue["related"])
            issue["relatedSource"] = "prose"
        else:
            issue["related"] = None
            issue["relatedSource"] = None
        # Only failure/improvement issues are expected to relate to anything;
        # an unattributed feature is not a gap worth counting.
        if issue["relatedSource"]:
            counts[issue["relatedSource"]] += 1
        elif classify(issue) is not None:
            counts["none"] += 1
    return counts


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


def gate_series(issues, pr_times, reviews, now=None):
    gate = [dict(g) for g in GATE]
    now = now or datetime.now(UTC)
    current_month = now.month

    def month(m):
        """The row for month `m`, appending months past the seeded list.

        GATE is hand-seeded and stops at the month it was last edited, so on
        the first day of a new month every `gate[month_of(...) - 1]` below
        indexed off the end and the whole build died -- an unattended refresh
        losing a day to a calendar roll. Rows added here carry no seeded
        history (there is none yet); merged/rejected/af/pm are all derived
        from the data further down, which is what a current month uses anyway.
        """
        while len(gate) < m:
            gate.append({"m": MONTH_ABBR[len(gate)], "merged": 0, "rejected": 0})
        return gate[m - 1]

    month(current_month)["rejected"] = sum(
        1 for r in reviews if month_of(r["date"]) == current_month
    )
    for i in issues:
        row = month(month_of(i["created"]))
        if i.get("cause") in AF_CAUSES:
            row["af"] = row.get("af", 0) + 1
        elif i.get("cause") == "pm":
            row["pm"] = row.get("pm", 0) + 1
    for g in gate:
        g.setdefault("af", 0)
        g.setdefault("pm", 0)
    if pr_times:
        by_month = defaultdict(list)
        merged_count = Counter()
        for created, merged in pr_times.values():
            m = month_of(merged)
            merged_count[m] += 1
            month(m)
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
    # Same calendar-roll trap as gate_series(): seeded off GATE, this list used
    # to stop at the month GATE was last hand-edited and index off the end on
    # the first issue of a new month. Cover through the current month instead.
    months = max(len(GATE), now.month)
    flow = [{"m": MONTH_ABBR[i], "opened": 0, "closed": 0} for i in range(months)]
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
        # Whether these minutes are billed or wall-clock. A public repository
        # gets free Actions, so the API reports zero billable and the dump
        # falls back to run duration -- the view must say which it is showing
        # rather than labelling unbilled time "billable" (#3808).
        "minutesSource": spend.get("minutes_source") or {},
        "degraded": spend.get("degraded") or {},
    }


def churn_view(churn, project_fields):
    """Shape dump_churn.py's data/churn.json into the churn-cost view (#3776).

    The churn cost is what re-onboarding a zero-memory agent costs per round:
    context re-establishment tokens vs change-production tokens, the repeat
    onboarding tax paid by multi-round issues, and the tally of stale-context
    incidents. Dollars appear only when the dump ran with a price sheet
    configured. Returns None when no churn dump exists; the page then renders
    the section as explicitly unavailable rather than dropping it (#3808) —
    a missing panel reads as "never built", which is how a churn dump that had
    never once succeeded went unnoticed for a day."""
    if not churn:
        return None
    sprint_of = sprint_of_map(project_fields) if project_fields else {}
    per_issue = []
    for n_str, entry in churn.get("issues", {}).items():
        n = int(n_str)
        per_issue.append({**entry, "issue": n, "sprint": sprint_of.get(n) or "(no sprint)"})
    per_issue.sort(key=lambda r: r["tokens"]["total"], reverse=True)

    by_kind = defaultdict(lambda: {"rounds": 0, "tokens": 0, "context_tokens": 0})
    for r in churn.get("rounds", []):
        agg = by_kind[r.get("kind", "session")]
        agg["rounds"] += 1
        agg["tokens"] += (r.get("tokens") or {}).get("total", 0)
        agg["context_tokens"] += (r.get("split") or {}).get("context_tokens") or 0
    kinds = [{"kind": k, **v} for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["tokens"])]

    return {
        "totals": churn.get("totals", {}),
        "byKind": kinds,
        "topIssues": per_issue[:12],
        "multiRound": [r for r in per_issue if r["rounds"] > 1][:12],
        "incidents": churn.get("staleContextIncidents"),
        "methodology": churn.get("methodology", {}),
        "generatedAt": churn.get("generated_at"),
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


def build_qdata(issues, project_fields, relationships=None):
    issues = [i for i in issues if i.get("milestone") not in EXCLUDED_MILESTONES]
    attribution = attribute_related(issues, relationships)
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
        "attribution": attribution,
        "lens": {"causes": CAUSES, "labels": LABELS, "modules": mods_all},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(HERE / "data"))
    ap.add_argument("--template", default=str(HERE / "retro_template.html"))
    ap.add_argument("--out", default=str(HERE / "retro.html"))
    ap.add_argument(
        "--allow-missing-sources",
        action="store_true",
        help=(
            "build and exit 0 even when an input dump is absent. The page still "
            "renders those sections as unavailable; this only stops the build "
            f"from exiting {MISSING_SOURCE_EXIT} (#3808)."
        ),
    )
    args = ap.parse_args()
    data = Path(args.data_dir)

    issues, issues_generated_at = load_issues(data / "all_issues.json")
    dashboard = json.loads((data / "dashboard_data.json").read_text())
    pf_path = data / "project_fields.json"
    project_fields = json.loads(pf_path.read_text()) if pf_path.exists() else None
    pt_path = data / "pr_times.json"
    pr_times = json.loads(pt_path.read_text()) if pt_path.exists() else None
    rv_path = data / "reviews_final.json"
    reviews = json.loads(rv_path.read_text()) if rv_path.exists() else []
    sp_path = data / "spend.json"
    spend = json.loads(sp_path.read_text()) if sp_path.exists() else None
    rel_path = data / "relationships.json"
    relationships = json.loads(rel_path.read_text()) if rel_path.exists() else None
    ch_path = data / "churn.json"
    churn = json.loads(ch_path.read_text()) if ch_path.exists() else None

    now = datetime.now(UTC)
    qdata = build_qdata(issues, project_fields, relationships)
    classified = qdata.pop("issues_classified")
    qdata["gate"] = gate_series(classified, pr_times, reviews, now)
    qdata["aging"], qdata["flow"], open_af = aging_flow(classified, now)
    qdata["themes"] = finding_themes(reviews)
    qdata["takeaways"] = takeaways(classified, dashboard, qdata["weeks"], open_af)
    qdata["releases"] = RELEASES
    qdata["spend"] = spend_by_sprint(spend, project_fields)
    qdata["churn"] = churn_view(churn, project_fields)
    qdata["build"] = {
        "at": now.isoformat(),
        "staleAfterDays": STALE_SOURCE_DAYS,
        "sources": source_stamps(
            now,
            [
                (
                    "issues",
                    "Issue corpus",
                    "all_issues.json",
                    True,
                    issues_generated_at,
                    None,  # hand-written by the refresh session, not a dump
                ),
                (
                    "relationships",
                    "Issue relationships",
                    "relationships.json",
                    relationships is not None,
                    (relationships or {}).get("generated_at"),
                    "retro_relationships_dump.yml",
                ),
                (
                    "reviews",
                    "Review rounds",
                    "dashboard_data.json",
                    True,
                    dashboard.get("generated_at"),
                    "retro_review_rounds_dump.yml",
                ),
                (
                    "projectFields",
                    "Sprint / module snapshot",
                    "project_fields.json",
                    project_fields is not None,
                    (project_fields or {}).get("generated_at"),
                    "retro_project_fields_dump.yml",
                ),
                (
                    "spend",
                    "Spend telemetry",
                    "spend.json",
                    spend is not None,
                    (spend or {}).get("generated_at"),
                    "retro_spend_dump.yml",
                ),
                (
                    "churn",
                    "Churn cost",
                    "churn.json",
                    churn is not None,
                    (churn or {}).get("generated_at"),
                    "retro_churn_dump.yml",
                ),
            ],
        ),
    }
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
    stale = [s["file"] for s in qdata["build"]["sources"].values() if s["present"] and s["stale"]]
    unstamped = [
        s["file"]
        for s in qdata["build"]["sources"].values()
        if s["present"] and not s["generatedAt"]
    ]
    missing = [s for s in qdata["build"]["sources"].values() if not s["present"]]
    print(f"built {args.out}: {qdata['qtotals']} sprintSource={qdata['sprintSource']}")
    print(f"  built at {qdata['build']['at']}")
    print(f"  stale sources: {', '.join(stale) or 'none'}")
    print(f"  unstamped sources: {', '.join(unstamped) or 'none'}")
    print(
        "  missing sources: "
        + (", ".join(f"{s['file']} ({s['workflow'] or 'hand-written'})" for s in missing) or "none")
    )
    if missing and not args.allow_missing_sources:
        # Non-zero, after writing the page: the dashboard is publishable (each
        # missing section says so on its face), but publishing it is now a
        # deliberate act. #3808's churn dump had never once succeeded and the
        # refresh reported success anyway, because a skipped input cost nothing
        # here. Re-dispatch the named dump, or pass --allow-missing-sources and
        # report the failed dump with its run URL.
        print(
            "ERROR: "
            + "; ".join(
                f"{s['label']} is missing — {s['file']} was not produced by "
                f"{s['workflow'] or 'the refresh session'}"
                for s in missing
            )
            + ". Re-dispatch that dump and read its most recent run, or rebuild with "
            "--allow-missing-sources to publish deliberately (the page will show the "
            "section as unavailable).",
            flush=True,
        )
        return MISSING_SOURCE_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
