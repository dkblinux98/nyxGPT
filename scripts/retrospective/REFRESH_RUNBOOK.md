# Retrospective Dashboard - Daily Refresh Runbook

Owner-facing runbook for the scheduled Claude session that refreshes the
**nyxGPT Project Retrospective** artifact. This is owner tooling, not part of the
nyxGPT product; it is executed by an assistant session (executive-assistant role),
not by the agent loop.

Artifact URL (republish to this URL, do not mint a new one):
`https://claude.ai/code/artifact/2b850289-fbb2-4e55-abf7-ea55d4501701`

## Steps

1. **Check out the repository default branch** — resolve it dynamically via
   `git ls-remote --symref origin HEAD`, never hardcode a version (v3.0.0 as of
   2026-08-03; note the default branch is the *development* branch and may
   differ from the `RELEASE_BRANCH` Actions variable during a cutover). The
   template, builder, and data seeds live under `scripts/retrospective/` there.

2. **Refresh the issue corpus** → `data/all_issues.json`.
   Via the GitHub MCP server: `search_issues` with query
   `repo:dkblinux98/nyxGPT is:issue`, `sort:created`, `order:asc`, 100/page, all
   pages. Keep per issue: `n` (number), `title`, `labels` (names), `milestone`
   (title), `created` (created_at), `state`, `closed` (closed_at or null), and
   `related` (the issue number from the first `Related feature: #N` line in the
   body, or null — Acceptance Failure and Improvement issues carry this per the
   2026-08-02 related-issue model). Overwrite the file.

3. **Refresh real sprint assignments** → `data/project_fields.json`.
   Dispatch the workflow `retro_project_fields_dump.yml` on the default branch
   (`actions_run_trigger`, method `run_workflow`), wait for completion (~1 min),
   then `git pull` — the workflow commits the JSON to the dispatching branch. If
   dispatch fails, skip; the builder falls back to calendar weeks automatically.

4. **Refresh per-issue spend telemetry** → `data/spend.json` (#3696).
   Dispatch the workflow `retro_spend_dump.yml` on the default branch
   (`actions_run_trigger`, method `run_workflow`), wait for completion (this
   one walks GitHub Actions run history across several workflows, including a
   per-run `jobs`/`timing` API call for each `developer_auto_implement.yml`
   and cost-tracked run — expect several minutes, not the ~1 min of the other
   dumps), then `git pull` — the workflow commits the JSON to the dispatching
   branch. If dispatch fails or `data/spend.json` doesn't exist yet, skip; the
   builder omits the spend section entirely rather than erroring.

5. **Refresh the last-7-days review detail** → `data/dashboard_data.json`.
   a. Gmail MCP `search_threads`:
      `from:notifications@github.com "Code Review - REQUEST_CHANGES" newer_than:8d`,
      fetch each thread (`get_thread`), and parse every message containing
      `## Code Review - REQUEST_CHANGES`: findings live under
      `### Critical|Medium|Minor Issues` headings (`(if any)` suffix and `####`
      variants occur) as `- **title**` bullets; `None.` means empty. Subject gives
      `(#ISSUE)` and `(PR #N)`. Count one round per pullrequestreview id; keep only
      rounds dated inside the trailing 7-day window.
   b. GitHub `search_pull_requests`: PRs merged in the window
      (`merged:YYYY-MM-DD..YYYY-MM-DD`), and unreviewed merges (`review:none`).
      Clean passes = merged, reviewed, and not in the rejected set — spot-check the
      earliest ones' review history for pre-window rejections.
   c. Rebuild the JSON with the same shape as the checked-in seed: `modules`
      (C/M/m/rounds/issues per module), `days`, `issues` (per-work-item rollup with
      finding titles), `cleanPRs`, `cleanByModule`, `totals`
      (rounds/C/M/m/items/clean/reviewed/merged/unreviewed).
   d. Refresh `data/pr_times.json` (all merged PRs Jan 1→now: number → [created_at, merged_at], via search_pull_requests) and update the current month in `GATE` inside `build_dashboard.py` (rejected count
      via a Gmail month-window search). Merged counts and median time-to-merge are derived from pr_times.json automatically.
   e. Update the hard-coded window copy in `retro_template.html` (the
      "Last 7 days in review · <dates>" divider and the totals sentences in the
      first-pass panel and footer) to the new window.

6. **Build**: `python3 scripts/retrospective/build_dashboard.py`
   → `scripts/retrospective/retro.html`.

7. **Publish** the built file with the Artifact tool to the URL above
   (`url` parameter — same URL, do not create a new artifact). Favicon stays 🔍.

8. **Commit** refreshed `data/*.json` (and GATE/template edits) via the
   `claude/retro-data` branch: force-reset `claude/retro-data` to the current
   default-branch tip (`git checkout -B claude/retro-data`), commit there, and
   `git push --force origin claude/retro-data`. The
   `retro_data_merge.yml` workflow then merges it into the default branch
   immediately (owner-approved exception to the review loop for this tooling,
   2026-07-31). Verify the merge landed (workflow completes in ~30 s; check with
   `git ls-remote` that the default branch tip now contains the merge commit)
   and report a failed merge in the run summary. Touch only files under
   `scripts/retrospective/` — the workflow refuses to merge anything else — and
   never open PRs or push to any other branch.

## Module attribution and classification

Modules are inferred from issue titles per the repo taxonomy (until
`project_fields.json` provides real Module values — the builder prefers those for
sprint bucketing only; title inference remains for the 7-day view). Acceptance
failures are classified defect/spec/workflow by the heuristics in
`build_dashboard.py`; review new-issue classifications when they look off and add
overrides to `SPEC_OVERRIDES` or the regexes as needed.

**Product management failures (owner decision 2026-08-01/02):** issues labeled
`Improvement` are a separate failure statistic with cause `pm` — an Improvement
filed during acceptance testing means the spec was incomplete (a planning
failure), distinct from an Acceptance Failure (implementation defect). The
builder counts them separately everywhere (`qtotals.pm`, the `pm` cause lens,
monthly `gate[].pm`); AF-only aggregates (aging, backlog flow, interception
rate) exclude them.

**Failure-issue model (2026-08-02):** a unique acceptance failure is filed as a
NEW issue related to the feature via `Related feature: #N` + a blocking
dependency; the original feature stays closed. A fix failing re-test REOPENS the
same failure issue — so failure-issue counts understate failure rounds; watch
for reopened failure issues when narrating trends.
