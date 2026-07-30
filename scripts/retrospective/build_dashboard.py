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
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent

MS_ORDER = ['Phase 0: Application Scaffolding', 'Phase 1: Quality & Security',
            'Phase 2: User Experience', 'Phase 3: Intelligence',
            'Phase 3.5: Post Release 1.0.0 Fixes', 'Phase 4: Scale & Performance',
            'Phase 5: Enterprise Features', 'Phase 5.5: Post Release 2.0.0 Fixes', None]
MS_SHORT = ['Phase 0', 'Phase 1', 'Phase 2', 'Phase 3', 'Phase 3.5', 'Phase 4',
            'Phase 5', 'Phase 5.5', '(none)']
EXCLUDED_MILESTONES = {'Phase X: Rejected'}
LABELS = ['Acceptance Failure', 'Feature', 'Release Management', 'Improvement', 'Documentation']
CAUSES = ['defect', 'spec', 'workflow']

# Review-gate monthly series: PRs merged (GitHub API) vs PRs with >=1
# REQUEST_CHANGES (notification-email archive). Update the current month on refresh.
GATE = [
    {'m': 'Jan', 'merged': 153, 'rejected': 62},
    {'m': 'Feb', 'merged': 15, 'rejected': 26},
    {'m': 'Mar', 'merged': 7, 'rejected': 5},
    {'m': 'Apr', 'merged': 0, 'rejected': 0},
    {'m': 'May', 'merged': 0, 'rejected': 0},
    {'m': 'Jun', 'merged': 0, 'rejected': 0},
    {'m': 'Jul', 'merged': 168, 'rejected': 67},
]

WORKFLOW_RE = re.compile(
    r'agent workflow|review workflow|developer workflow|code review and developer|review gate|review.agent|'
    r'scrummaster|hygiene|accept-and-merge|snapshot step|usage-limit|auto-implement|auto-review|'
    r'branch hygiene|superseded|PR submission|CI Workflow Analytics|smoke.?test script|'
    r'agent branches|closed-issue assignee|skips milestone|unresolved .* issues from', re.I)
SPEC_RE = re.compile(
    r'redesign|consolidat|retire\b|rework|clarif|back-nav|layout|honest|\bIA\b|menu structure|'
    r'should (match|go to|be consistent)|single pane|disabled state|schema from example|'
    r'fails acceptance', re.I)
SPEC_OVERRIDES = {3406, 3344, 3346, 3324, 3326, 3328}

MODULE_RULES = [
    ("web-ui", r"\bweb\b|webui|\bui\b|dashboard|\bpage\b|screen|frontend|sidebar|wizard|admin\b|settings|button|panel|nav\b|menu"),
    ("observability", r"observab|grafana|loki|prometheus|tempo|jaeger|glitchtip|metrics|logging|\blogs?\b|tracing|instrument|self-heal|canary|monitor|alert|probe|smoke|spog"),
    ("documentation", r"\bdocs?\b|documentation|readme|release notes|changelog"),
    ("testing", r"\btests?\b|testing|coverage|pytest|vitest|flaky"),
    ("security", r"security|secret|api.?key|auth|token|timing attack|vulnerab|cve"),
    ("rag", r"\brag\b|ingest|embedding|retrieval|chunk|collection|epub|document q"),
    ("tui", r"\btui\b"),
    ("cli", r"\bcli\b|\bops\b|command|homebrew|brew|install|deploy|terraform|k8s|kubernetes|docker|compose|cassandra|ollama|infra"),
]


def detect_module(title):
    t = title.lower()
    for m, pat in MODULE_RULES:
        if re.search(pat, t):
            return m
    return "api"


def classify(issue):
    if 'Acceptance Failure' not in issue['labels']:
        return None
    if issue['n'] in SPEC_OVERRIDES:
        return 'spec'
    if WORKFLOW_RE.search(issue['title']):
        return 'workflow'
    if SPEC_RE.search(issue['title']):
        return 'spec'
    return 'defect'


def sprint_buckets(issues, project_fields):
    """Return (buckets, source). Each bucket: {'w': key, 'label': str|None, 'issues': [...]}.
    With a project snapshot, buckets are the Project's real Sprint iterations
    (issues without a sprint go to a trailing '(no sprint)' bucket); otherwise
    calendar Mondays."""
    if project_fields:
        sprint_of = {}
        for item in project_fields.get('items', []):
            if item.get('type') not in (None, 'ISSUE', 'Issue'):
                continue
            for f in item.get('fields', []):
                if f.get('field') == 'Sprint' and f.get('value'):
                    sprint_of[item['number']] = f['value']
        cal = sorted(project_fields.get('sprints', []), key=lambda s: s['startDate'])
        buckets = [{'w': s['startDate'], 'label': s['title'], 'issues': []} for s in cal]
        by_title = {b['label']: b for b in buckets}
        stray = {'w': '9999-12-31', 'label': '(no sprint)', 'issues': []}
        for i in issues:
            t = sprint_of.get(i['n'])
            (by_title.get(t) or stray)['issues'].append(i)
        if stray['issues']:
            buckets.append(stray)
        buckets = [b for b in buckets if b['issues']]
        return buckets, 'project'
    # calendar fallback
    start, end = date(2025, 12, 29), date.today()
    buckets, d = [], start
    while d <= end:
        buckets.append({'w': d.isoformat(), 'label': None, 'issues': []})
        d += timedelta(days=7)
    idx = {b['w']: b for b in buckets}
    for i in issues:
        di = date.fromisoformat(i['created'][:10])
        monday = (di - timedelta(days=di.weekday())).isoformat()
        if monday in idx:
            idx[monday]['issues'].append(i)
    return buckets, 'calendar'


def build_qdata(issues, project_fields):
    issues = [i for i in issues if i.get('milestone') not in EXCLUDED_MILESTONES]
    for i in issues:
        i['module'] = detect_module(i['title'])
        i['cause'] = classify(i)
    modc = Counter(i['module'] for i in issues)
    mods = [m for m, _ in modc.most_common(7)]
    mods_all = mods + ['other']
    mod = lambda i: i['module'] if i['module'] in mods else 'other'

    def empty():
        return {'cause': dict.fromkeys(CAUSES, 0), 'label': dict.fromkeys(LABELS, 0),
                'module': dict.fromkeys(mods_all, 0)}

    buckets, source = sprint_buckets(issues, project_fields)
    weeks = []
    for b in buckets:
        agg = empty()
        for i in b['issues']:
            for l in i['labels']:
                if l in LABELS:
                    agg['label'][l] += 1
            agg['module'][mod(i)] += 1
            if i['cause']:
                agg['cause'][i['cause']] += 1
        entry = {'w': b['w'], **agg}
        if b['label']:
            entry['label_name'] = b['label']
        weeks.append({'w': b['w'], 'label': b.get('label'), **agg})

    ms = {s: {**empty(), 'total': 0, 'af': 0} for s in MS_SHORT}
    for i in issues:
        s = MS_SHORT[MS_ORDER.index(i['milestone'])] if i['milestone'] in MS_ORDER else '(none)'
        for l in i['labels']:
            if l in LABELS:
                ms[s]['label'][l] += 1
        ms[s]['module'][mod(i)] += 1
        ms[s]['total'] += 1
        if i['cause']:
            ms[s]['af'] += 1
            ms[s]['cause'][i['cause']] += 1

    return {
        'weeks': weeks,
        'sprintSource': source,
        'milestones': [{'name': s, **ms[s]} for s in MS_SHORT if ms[s]['total'] > 0],
        'gate': GATE,
        'qtotals': {'issues': len(issues), 'af': sum(1 for i in issues if i['cause']),
                    'defect': sum(1 for i in issues if i['cause'] == 'defect'),
                    'spec': sum(1 for i in issues if i['cause'] == 'spec'),
                    'workflow': sum(1 for i in issues if i['cause'] == 'workflow'),
                    'production': 0},
        'lens': {'causes': CAUSES, 'labels': LABELS, 'modules': mods_all},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=str(HERE / 'data'))
    ap.add_argument('--template', default=str(HERE / 'retro_template.html'))
    ap.add_argument('--out', default=str(HERE / 'retro.html'))
    args = ap.parse_args()
    data = Path(args.data_dir)

    issues = json.loads((data / 'all_issues.json').read_text())
    dashboard = json.loads((data / 'dashboard_data.json').read_text())
    pf_path = data / 'project_fields.json'
    project_fields = json.loads(pf_path.read_text()) if pf_path.exists() else None

    qdata = build_qdata(issues, project_fields)
    html = Path(args.template).read_text()
    html = html.replace('__QDATA__', json.dumps(qdata, separators=(',', ':')))
    html = html.replace('__DATA__', json.dumps(dashboard, separators=(',', ':')))
    Path(args.out).write_text(html)
    print(f"built {args.out}: {qdata['qtotals']} sprintSource={qdata['sprintSource']}")


if __name__ == '__main__':
    main()
