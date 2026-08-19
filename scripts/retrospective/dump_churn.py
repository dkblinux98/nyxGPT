#!/usr/bin/env python3
"""Dump per-round churn-cost telemetry for the retrospective dashboard (#3776).

Owner decision (2026-08-14, churn-cost discussion): AI-coding productivity
math counts token spend as "usage" and stale-context errors as "rework", so
the *churn cost* — the per-session cost of re-onboarding an agent that
carries zero memory between rounds — is invisible. It has three parts:
bootstrap tokens (re-reading context), re-verification (confirming facts a
persistent employee would remember) and new-hire errors (stale-context
mistakes). `dump_spend.py` already records what a run *cost to run*
(runner minutes, Claude step counts); this script records what the agent
*spent thinking*, split into context re-establishment vs change production.

Invoked only by `.github/workflows/retro_churn_dump.yml`, which runs this
with `gh` authenticated (GH_TOKEN) and REPO set to "owner/repo". It walks
the recent run history of the Claude-invoking workflows, attributes each run
to an issue via its head branch (the same convention `dump_spend.py` uses —
`issue_of` is imported from there rather than duplicated), slices each job's
log into per-step segments, and writes:

  data/churn.json - {rounds: [...], issues: {n: rollup}, totals, methodology}

Each *round* is one executed `anthropics/claude-code-action` step: the
initial implement, a review-fix, an acceptance-fix, a self-heal attempt, or
a standalone session. Rounds are numbered per issue in chronological order,
so a multi-round issue shows its cumulative onboarding tax.

Context vs production split (methodology, deliberately an approximation —
stated here and echoed into churn.json and the dashboard):

  A step's log is read in order. Assistant turns are counted; the first
  file-modifying tool use (Edit/Write/MultiEdit/NotebookEdit/...) marks the
  moment the agent stopped re-establishing context and started producing
  change. Turns before it are "context", the rest are "production", and the
  step's token total is split pro rata by that turn ratio. Token usage is
  reported once per step (the action's terminal result event), not per turn,
  so a turn-proportional split is the best available approximation. A step
  that never modified a file is 100% context; a step whose log yields no
  turn markers is recorded with an unknown split and excluded from split
  aggregates (but still counted in token totals).

Dollars are attached only when a price sheet is configured — the dispatched
workflow passes one in from the `CHURN_PRICE_SHEET_JSON` repo variable; a
local run may instead drop a (gitignored) `data/price_sheet.json`. See
`load_price_sheet` for the full resolution order. Rates are owner-configured
and never committed to this repo: `data/price_sheet.example.json` ships
zeroed placeholders so no rate is asserted here (#3744). With no sheet
configured, the churn view reports tokens only.

Log windowing: only runs started within CHURN_WINDOW_DAYS (default 30) are
walked, because each round costs a log download. Rounds already recorded in
the previous data/churn.json are preserved, so history accumulates across
refreshes instead of being re-fetched.
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from dump_spend import gh, gh_optional, issue_of, iter_json_objects  # noqa: E402

DATA_DIR = HERE / "data"
DEFAULT_WINDOW_DAYS = 30

# Every workflow that invokes anthropics/claude-code-action. This list and
# CLAUDE_STEP_RE below are a pair, and both are pinned by
# tests/unit/test_dump_churn.py::TestWorkflowCoverage, which parses the real
# workflow YAML: every workflow in the tree that uses claude-code-action must
# be listed here, and every one of its action steps must have a name this
# regex matches. Without that test a step rename silently drops rounds.
#
# review_agent_auto_review.yml is deliberately absent: it invokes no
# claude-code-action step at all (its reviews are produced by
# claude-code-review.yml, which is listed).
CHURN_WORKFLOWS = [
    "developer_auto_implement.yml",
    "claude.yml",
    "claude-code-review.yml",
    "developer_huddle_position.yml",
    "scrummaster_huddle_mediation.yml",
    # #3919's grooming workflow. Two branches added it independently, the
    # de-duplication of that (#3927) removed both copies, and `collect()`
    # iterates this list directly -- so every grooming round was dropped from
    # the retrospective rather than double-counted, until #3928 restored it.
    # Exactly one entry; `test_no_claude_invoking_workflow_is_missing_from_the_list`
    # and `test_listed_workflows_are_not_stale_duplicates` pin both directions.
    "scrummaster_groom_sprint.yml",
    "claude-md-binding-canary.yml",
]

# Step names that ARE a Claude invocation. Deliberately stricter than
# dump_spend.py's "name contains claude": shell steps like "Check Claude
# progress completion" and "Read Claude analysis result" mention Claude but
# spend no tokens, and counting them would invent rounds. The huddle steps
# ("Post developer position", "Run mediation") do not say "Claude" at all,
# hence the explicit alternatives rather than a name-contains rule -- and
# neither does the sprint-grooming step ("Groom the draft (the judgment the
# seed cannot make)", #3919), whose tokens were invisible to the retrospective
# until it was named here.
CLAUDE_STEP_RE = re.compile(
    r"^\s*(run claude|claude fix issues|deep analysis with claude"
    r"|post developer position|run mediation|groom the draft)",
    re.I,
)

# Round kind, first match wins (an acceptance-fix step also says "fix").
ROUND_KIND_RULES = [
    ("groom", r"groom"),
    ("huddle", r"position|mediation"),
    ("acceptance-fix", r"acceptance"),
    ("review-fix", r"review fix|fix review issues"),
    ("self-heal", r"fix issues|attempt\s*\d|deep analysis"),
    ("implement", r"implement"),
    ("review", r"review"),
]

# Usage keys emitted by claude-code-action's execution log.
USAGE_KEYS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_creation",
    "cache_read_input_tokens": "cache_read",
}
USAGE_RE = re.compile(
    r'"?(input_tokens|output_tokens|cache_creation_input_tokens|cache_read_input_tokens)"?'
    r"\s*[:=]\s*\"?([\d,]+)"
)
# The action's terminal `result` event repeats the cumulative usage of the
# whole step; when present it wins over summing per-message usage events.
TERMINAL_RE = re.compile(r'total_cost_usd|"type"\s*:\s*"result"')
COST_RE = re.compile(r'"?total_cost_usd"?\s*[:=]\s*([0-9.]+)')
MODEL_RE = re.compile(r'"model"\s*:\s*"(claude[a-zA-Z0-9._-]*)"')
ASSISTANT_TURN_RE = re.compile(r'"type"\s*:\s*"assistant"')
MUTATION_RE = re.compile(
    r'"name"\s*:\s*"(Edit|Write|MultiEdit|NotebookEdit|str_replace_editor|'
    r'str_replace_based_edit_tool)"'
)
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$", re.S)

METHODOLOGY = {
    "split": (
        "Per Claude-invoking step: assistant turns before the first "
        "file-modifying tool use are context re-establishment, the rest are "
        "change production, and the step's token total is split pro rata by "
        "that turn ratio. Usage is reported once per step, so the turn ratio "
        "is an approximation, not a measured per-turn split."
    ),
    "round": (
        "One round = one executed claude-code-action step (implement, "
        "review-fix, acceptance-fix, self-heal, review, session). Rounds are "
        "numbered per issue in chronological order."
    ),
    "attribution": "Runs are attributed to an issue by head-branch name (see dump_spend.py).",
    "dollars": (
        "Reported only when a price sheet is configured at dump time (the "
        "CHURN_PRICE_SHEET_JSON repo variable, or a local gitignored "
        "data/price_sheet.json). Per-million rates are owner-supplied and "
        "never committed to this repo."
    ),
}


def parse_ts(value):
    """GitHub timestamp -> aware datetime, or None."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def round_kind(step_name):
    name = (step_name or "").lower()
    for kind, pattern in ROUND_KIND_RULES:
        if re.search(pattern, name):
            return kind
    return "session"


def is_claude_step(step):
    """True for executed steps that actually invoked claude-code-action."""
    if step.get("conclusion") in (None, "skipped"):
        return False
    return bool(CLAUDE_STEP_RE.match(step.get("name") or ""))


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
    for page in iter_json_objects(out, source=f"runs of {workflow_file}"):
        runs.extend(page.get("workflow_runs", []))
    return runs


def list_jobs(repo, run_id):
    """Jobs for one run; [] when the call fails.

    Per-run call inside a walk over thousands of runs, so it degrades rather
    than aborting -- a transient failure here threw away a whole churn dump
    (run 32010934175, on an endpoint that returned 200 on retry). See
    dump_spend.gh_optional.
    """
    out = gh_optional(
        "api",
        "-X",
        "GET",
        f"repos/{repo}/actions/runs/{run_id}/jobs",
        "--paginate",
        "-f",
        "per_page=100",
        what="churn_jobs",
    )
    if out is None:
        return []
    jobs = []
    for page in iter_json_objects(out, source=f"jobs of run {run_id}"):
        jobs.extend(page.get("jobs", []))
    return jobs


# Why each log fetch produced nothing. An expired log, a rate-limited or
# refused download, and a log that genuinely carries no usage all used to
# return the same "" -- so a dump where EVERY fetch failed was indistinguishable
# from one where every log was simply token-free, and both rendered as an empty
# churn panel with no explanation. The 2026-08-17 dump recorded tokens: null for
# all 304 rounds while the newest round's log was fetchable and contained 90
# `input_tokens` markers, and nothing in the output said which had happened.
LOG_FETCH = {"ok": 0, "expired": 0, "failed": 0}


def job_log(repo, job_id):
    """Plain-text log for one job; '' when GitHub has expired or refused it.

    Records the outcome in LOG_FETCH so the snapshot can say why tokens are
    missing. GitHub returns 410 Gone for an expired log -- this repository's
    retention is short (a 2026-08-09 log was already gone on 2026-08-17, while
    2026-08-16 was still served), so most of a 30-day window is expected to be
    expired and that is not a defect. A non-410 failure is, and the two must
    not look alike.
    """
    try:
        # --allow-escape-sequences is REQUIRED for this endpoint. Workflow logs
        # are full of ANSI colour codes, and gh refuses to emit a response
        # containing terminal escape sequences without it: "the response
        # contains terminal escape sequences; pass --allow-escape-sequences to
        # output it anyway", exit 1. That guard arrived in a gh release under
        # this script, so every log fetch failed and every round recorded
        # tokens: null -- 0 ok / 89 failed on run 32023282331 -- while the
        # dump itself reported success (#3808).
        out = gh(
            "api",
            "-X",
            "GET",
            "--allow-escape-sequences",
            f"repos/{repo}/actions/jobs/{job_id}/logs",
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "") if hasattr(exc, "stderr") else ""
        if "410" in stderr or "Gone" in stderr:
            LOG_FETCH["expired"] += 1
        else:
            LOG_FETCH["failed"] += 1
            print(
                f"[dump_churn] log fetch failed for job {job_id} "
                f"(gh exit {exc.returncode}): {stderr.strip()[:200]}",
                flush=True,
            )
        return ""
    LOG_FETCH["ok"] += 1
    return out


def step_key(step):
    """Stable per-job identity for a step.

    The Jobs API numbers steps within a job, so `number` is unique even when
    two steps share a name; the name is only a fallback for callers (and
    older dumps) that carry no number.
    """
    number = step.get("number")
    return number if number is not None else (step.get("name") or "")


def split_log_by_steps(log_text, steps):
    """Slice a job log into {step_key: [lines]} using each step's time window.

    Raw job logs carry no step *names*, only `##[group]` markers for the
    command — but every line is timestamp-prefixed and the Jobs API gives
    each step's started_at/completed_at, so the timestamps are the reliable
    boundary. Untimestamped continuation lines stay with the step that the
    preceding timestamped line landed in. Segments are keyed by `step_key`,
    not by name, so two identically-named steps in one job cannot collide.
    """
    windows = []
    for step in steps:
        start, end = parse_ts(step.get("started_at")), parse_ts(step.get("completed_at"))
        if start is None:
            continue
        windows.append((start, end, step_key(step)))
    windows.sort(key=lambda w: w[0])

    segments = defaultdict(list)
    current = None
    for line in (log_text or "").splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            ts, body = parse_ts(m.group(1)), m.group(2)
            for start, end, key in windows:
                if ts >= start and (end is None or ts <= end):
                    current = key
                    break
            else:
                current = None
        else:
            body = line
        if current is not None:
            segments[current].append(body)
    return dict(segments)


def parse_usage(lines):
    """Token/cost/model/turn signals from one step's log lines.

    Returns {tokens, cost_usd_reported, model, turns, first_mutation_turn}.
    `tokens` is None when the log carried no usage at all (expired log, or a
    step whose output format this parser doesn't recognise) — an honest
    unknown, not a zero.
    """
    per_line, terminal = [], []
    cost = None
    models = []
    turns = 0
    first_mutation_turn = None

    for line in lines:
        if ASSISTANT_TURN_RE.search(line):
            turns += 1
        if first_mutation_turn is None and MUTATION_RE.search(line):
            first_mutation_turn = max(turns, 1)
        models.extend(MODEL_RE.findall(line))
        found = {}
        for key, value in USAGE_RE.findall(line):
            found[USAGE_KEYS[key]] = int(value.replace(",", ""))
        if found:
            (terminal if TERMINAL_RE.search(line) else per_line).append(found)
        m = COST_RE.search(line)
        if m:
            cost = float(m.group(1))

    records = [terminal[-1]] if terminal else per_line
    tokens = None
    if records:
        tokens = dict.fromkeys(USAGE_KEYS.values(), 0)
        for record in records:
            for name, value in record.items():
                tokens[name] += value
        tokens["total"] = sum(tokens[name] for name in USAGE_KEYS.values())

    return {
        "tokens": tokens,
        "cost_usd_reported": cost,
        "model": models[-1] if models else None,
        "turns": turns,
        "first_mutation_turn": first_mutation_turn,
    }


def split_tokens(usage):
    """Apply the documented turn-ratio split to a step's token total."""
    turns, first = usage["turns"], usage["first_mutation_turn"]
    tokens = usage["tokens"]
    total = tokens["total"] if tokens else 0

    if turns == 0 or tokens is None:
        # No turn markers (nothing to split by) or no usage at all (expired
        # log) — either way the split is unknown, not a measured 0/0. Both
        # aggregates key off this, so they agree: totals_of counts
        # source == "unknown", rollup_issues counts context_tokens is None.
        return {
            "context_turns": turns if tokens is None else 0,
            "production_turns": 0,
            "context_tokens": None,
            "production_tokens": None,
            "source": "unknown",
        }
    if first is None:
        # Never touched a file: the whole step was context/verification.
        return {
            "context_turns": turns,
            "production_turns": 0,
            "context_tokens": total,
            "production_tokens": 0,
            "source": "no-change",
        }
    context_turns = max(0, first - 1)
    production_turns = turns - context_turns
    context_tokens = round(total * context_turns / turns)
    return {
        "context_turns": context_turns,
        "production_turns": production_turns,
        "context_tokens": context_tokens,
        "production_tokens": total - context_tokens,
        "source": "turns",
    }


def rounds_from_run(repo, run, jobs_fn, job_log_fn):
    """Every Claude step of one workflow run, as unnumbered round records.

    `issue` is None for runs whose head branch carries no issue number. That
    is the normal case for the two huddle workflows and for claude.yml: they
    are `issue_comment`-triggered and check out the release branch, so there
    is no per-issue attribution to be had from run metadata. Their rounds are
    still recorded and still counted in totals — as *unattributed* rounds
    (see `totals_of`) — rather than being dropped or, worse, guessed at.
    """
    issue = issue_of(run.get("head_branch"))
    out = []
    for job in jobs_fn(repo, run["id"]):
        steps = job.get("steps") or []
        claude_steps = [s for s in steps if is_claude_step(s)]
        if not claude_steps:
            continue
        segments = split_log_by_steps(job_log_fn(repo, job["id"]), steps)
        for step in claude_steps:
            name = step.get("name") or ""
            usage = parse_usage(segments.get(step_key(step), []))
            out.append(
                {
                    "issue": issue,
                    "run_id": run["id"],
                    "job_id": job["id"],
                    "workflow": run.get("path", "").rsplit("/", 1)[-1]
                    or run.get("name")
                    or "unknown",
                    "step": name,
                    "step_number": step.get("number"),
                    "kind": round_kind(name),
                    "started_at": step.get("started_at") or run.get("run_started_at"),
                    "tokens": usage["tokens"],
                    "model": usage["model"],
                    "cost_usd_reported": usage["cost_usd_reported"],
                    "turns": usage["turns"],
                    "split": split_tokens(usage),
                }
            )
    return out


def collect(repo, since, list_runs_fn=list_runs, jobs_fn=list_jobs, job_log_fn=job_log):
    """Walk the Claude-invoking workflows and return every round since `since`."""
    rounds = []
    for wf in CHURN_WORKFLOWS:
        for run in list_runs_fn(repo, wf):
            if run.get("status") != "completed":
                continue
            started = parse_ts(run.get("run_started_at") or run.get("created_at"))
            if started is None or started < since:
                continue
            rounds.extend(rounds_from_run(repo, run, jobs_fn, job_log_fn))
    return rounds


def round_key(r):
    """Identity of a round across refreshes: its job, and its step within it.

    Prefers the step *number* over its name for the same reason
    `split_log_by_steps` does — two identically-named steps in one job would
    otherwise collide and one would overwrite the other.
    """
    number = r.get("step_number")
    return (r.get("job_id"), number if number is not None else (r.get("step") or ""))


def merge_rounds(existing, fresh):
    """Union of previously-dumped and freshly-walked rounds, freshest winning.

    Keyed by `round_key` so a re-walk of the same window updates rather than
    duplicates, and rounds that have aged out of the window survive.
    """
    by_key = {round_key(r): r for r in existing or []}
    for r in fresh:
        by_key[round_key(r)] = r
    merged = list(by_key.values())
    merged.sort(
        key=lambda r: (r.get("started_at") or "", r.get("job_id") or 0, r.get("step") or "")
    )
    return merged


def number_rounds(rounds):
    """Stamp per-issue `round` and per-kind `kind_round` ordinals, in place."""
    per_issue = defaultdict(int)
    per_kind = defaultdict(int)
    for r in sorted(rounds, key=lambda r: (r.get("started_at") or "", r.get("job_id") or 0)):
        issue = r.get("issue")
        if issue is None:
            r["round"] = None
            r["kind_round"] = None
            continue
        per_issue[issue] += 1
        per_kind[(issue, r["kind"])] += 1
        r["round"] = per_issue[issue]
        r["kind_round"] = per_kind[(issue, r["kind"])]
    return rounds


INCIDENT_REQUIRED = ("id", "date", "kind", "title", "summary", "recordedIn")


def validate_incidents(doc):
    """Check the hand-maintained incident file; returns it, raises on damage.

    Run on every refresh so a malformed hand edit fails the dump workflow
    loudly instead of quietly vanishing from the dashboard.
    """
    incidents = doc.get("incidents")
    if not isinstance(incidents, list):
        raise ValueError("stale_context_incidents.json: 'incidents' must be a list")
    kinds = doc.get("kinds") or {}
    seen = set()
    for entry in incidents:
        missing = [f for f in INCIDENT_REQUIRED if not entry.get(f)]
        if missing:
            raise ValueError(f"incident {entry.get('id') or '(no id)'}: missing {missing}")
        if entry["id"] in seen:
            raise ValueError(f"duplicate incident id {entry['id']}")
        seen.add(entry["id"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"]):
            raise ValueError(f"incident {entry['id']}: date must be YYYY-MM-DD")
        if kinds and entry["kind"] not in kinds:
            raise ValueError(f"incident {entry['id']}: unknown kind {entry['kind']!r}")
    return doc


def load_incidents(path=None):
    """Validated stale-context incident log, or None when the file is absent."""
    path = Path(path or DATA_DIR / "stale_context_incidents.json")
    if not path.exists():
        return None
    return validate_incidents(json.loads(path.read_text()))


def incident_tally(doc):
    """Counts by kind and by month, plus the incident list, for the retro data."""
    if not doc:
        return None
    incidents = doc["incidents"]
    by_kind = defaultdict(int)
    by_month = defaultdict(int)
    for entry in incidents:
        by_kind[entry["kind"]] += 1
        by_month[entry["date"][:7]] += 1
    return {
        "total": len(incidents),
        "byKind": dict(sorted(by_kind.items())),
        "byMonth": dict(sorted(by_month.items())),
        "kinds": doc.get("kinds") or {},
        "howToRecord": doc.get("howToRecord"),
        "incidents": sorted(incidents, key=lambda e: e["date"], reverse=True),
    }


def load_price_sheet(path=None, env=None):
    """Owner-configured per-million token prices, or None when unconfigured.

    Resolution order, and the single story about where rates live: rates are
    never committed to this repo (#3744 — a committed rate is a world-state
    claim that goes stale, and the shipped `price_sheet.example.json` is
    zeroed precisely so the repo asserts none).

      1. `CHURN_PRICE_SHEET_JSON` — the sheet's JSON *content*. This is how
         the dispatched workflow gets it: from the repo variable of the same
         name, so nothing is written to the checkout and nothing can be
         committed by accident.
      2. `CHURN_PRICE_SHEET` — a path, for pointing at a sheet held anywhere.
      3. `data/price_sheet.json` — a local sheet, for running the dump by
         hand. It is gitignored, so this path cannot become a committed rate.

    None of the three configured means the churn view reports tokens only.
    """
    env = os.environ if env is None else env
    inline = (env.get("CHURN_PRICE_SHEET_JSON") or "").strip() if path is None else ""
    if inline:
        return json.loads(inline)
    path = Path(path or env.get("CHURN_PRICE_SHEET") or DATA_DIR / "price_sheet.json")
    if not path.exists():
        return None
    return json.loads(path.read_text())


def price_tokens(tokens, model, sheet):
    """USD for one token bundle, or None when no price sheet is configured."""
    if not sheet or not tokens:
        return None
    rates = (sheet.get("models") or {}).get(model or "") or sheet.get("default")
    if not rates:
        return None
    per_million = (
        tokens.get("input", 0) * rates.get("input", 0)
        + tokens.get("output", 0) * rates.get("output", 0)
        + tokens.get("cache_creation", 0) * rates.get("cache_write", rates.get("input", 0))
        + tokens.get("cache_read", 0) * rates.get("cache_read", 0)
    )
    return round(per_million / 1_000_000, 4)


def _empty_tokens():
    return dict.fromkeys(list(USAGE_KEYS.values()) + ["total"], 0)


def rollup_issues(rounds, sheet=None):
    """Per-issue churn rollup: rounds, tokens, context/production split, cost.

    `repeat_context_tokens` is the onboarding tax proper — context tokens
    spent in every round *after* the first, i.e. what a persistent memory
    would have saved.
    """
    issues = {}
    for r in rounds:
        if r.get("issue") is None:
            continue
        entry = issues.setdefault(
            r["issue"],
            {
                "issue": r["issue"],
                "rounds": 0,
                "kinds": defaultdict(int),
                "tokens": _empty_tokens(),
                "context_tokens": 0,
                "production_tokens": 0,
                "repeat_context_tokens": 0,
                "unknown_split_rounds": 0,
                "cost_usd": None,
                "first_round_at": r.get("started_at"),
                "last_round_at": r.get("started_at"),
            },
        )
        entry["rounds"] += 1
        entry["kinds"][r["kind"]] += 1
        for key in _empty_tokens():
            entry["tokens"][key] += (r.get("tokens") or {}).get(key, 0)
        split = r.get("split") or {}
        if split.get("context_tokens") is None:
            entry["unknown_split_rounds"] += 1
        else:
            entry["context_tokens"] += split["context_tokens"]
            entry["production_tokens"] += split["production_tokens"]
            if (r.get("round") or 1) > 1:
                entry["repeat_context_tokens"] += split["context_tokens"]
        cost = price_tokens(r.get("tokens"), r.get("model"), sheet)
        if cost is not None:
            entry["cost_usd"] = round((entry["cost_usd"] or 0.0) + cost, 4)
        for key, op in (("first_round_at", min), ("last_round_at", max)):
            # A first-seen round with no timestamp seeds the entry with None;
            # a later round that does have one must still fill it in.
            if r.get("started_at"):
                entry[key] = op(entry[key], r["started_at"]) if entry[key] else r["started_at"]

    for entry in issues.values():
        entry["kinds"] = dict(entry["kinds"])
        known = entry["context_tokens"] + entry["production_tokens"]
        entry["context_pct"] = round(entry["context_tokens"] / known * 100, 1) if known else None
    return issues


def totals_of(rounds, issues, sheet=None):
    """Project-wide churn totals.

    Token totals span *every* round, including the unattributed ones (huddle
    and standalone-session runs, which carry no issue in their branch name);
    the per-issue rollups necessarily do not. `unattributed_rounds` /
    `unattributed_tokens` make that gap explicit, so the two figures are not
    read as disagreeing and partial per-issue data is not read as complete.
    """
    unattributed = [r for r in rounds if r.get("issue") is None]
    totals = {
        "rounds": len(rounds),
        "issues": len(issues),
        "unattributed_rounds": len(unattributed),
        "unattributed_tokens": sum((r.get("tokens") or {}).get("total", 0) for r in unattributed),
        "tokens": _empty_tokens(),
        "context_tokens": sum(i["context_tokens"] for i in issues.values()),
        "production_tokens": sum(i["production_tokens"] for i in issues.values()),
        "repeat_context_tokens": sum(i["repeat_context_tokens"] for i in issues.values()),
        "unknown_split_rounds": sum(
            1 for r in rounds if (r.get("split") or {}).get("source") == "unknown"
        ),
        "unknown_token_rounds": sum(1 for r in rounds if not r.get("tokens")),
        "multi_round_issues": sum(1 for i in issues.values() if i["rounds"] > 1),
    }
    for r in rounds:
        for key, value in (r.get("tokens") or {}).items():
            totals["tokens"][key] = totals["tokens"].get(key, 0) + value
    known = totals["context_tokens"] + totals["production_tokens"]
    totals["context_pct"] = round(totals["context_tokens"] / known * 100, 1) if known else None
    costs = [i["cost_usd"] for i in issues.values() if i["cost_usd"] is not None]
    totals["cost_usd"] = round(sum(costs), 2) if costs else None
    totals["priced"] = bool(sheet)
    return totals


def build_snapshot(rounds, sheet=None, incidents=None):
    number_rounds(rounds)
    issues = rollup_issues(rounds, sheet)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        # Why tokens are missing where they are missing: "expired" is the
        # normal cost of a window longer than GitHub's log retention;
        # "failed" is a real problem. Without this, an all-null dump and a
        # correctly-empty one are the same output (#3808).
        "logFetch": dict(LOG_FETCH),
        "methodology": METHODOLOGY,
        "rounds": rounds,
        "issues": {str(n): entry for n, entry in sorted(issues.items())},
        "totals": totals_of(rounds, issues, sheet),
        "staleContextIncidents": incident_tally(incidents),
    }


def main():
    repo = os.environ["REPO"]
    window_days = int(os.environ.get("CHURN_WINDOW_DAYS") or DEFAULT_WINDOW_DAYS)
    since = datetime.now(UTC) - timedelta(days=window_days)

    out_path = DATA_DIR / "churn.json"
    previous = json.loads(out_path.read_text())["rounds"] if out_path.exists() else []
    fresh = collect(repo, since)
    rounds = merge_rounds(previous, fresh)

    snapshot = build_snapshot(rounds, load_price_sheet(), load_incidents())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=1) + "\n")
    print(
        f"wrote {len(rounds)} rounds ({len(fresh)} walked in the last {window_days}d) "
        f"across {snapshot['totals']['issues']} issues to data/churn.json: {snapshot['totals']}"
    )


if __name__ == "__main__":
    main()
