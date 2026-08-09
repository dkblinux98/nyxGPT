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

4. **Refresh review-round detail** → `data/reviews_final.json` and
   `data/dashboard_data.json`.
   a. Dispatch the workflow `retro_review_rounds_dump.yml` on the default
      branch (`actions_run_trigger`, method `run_workflow`), wait for
      completion (~5 min — it walks every PR's reviews via the GitHub API),
      then `git pull` — the workflow commits both JSON files to the
      dispatching branch (same shape as `retro_project_fields_dump.yml`).
      The dump derives review rounds directly from PR reviews (the review
      agent posts every `## Code Review - REQUEST_CHANGES` round as a formal
      PR review, so this is GitHub-native, not a Gmail parse — owner decision
      2026-08-08, #3667): `### Critical|Medium|Minor Issues` headings
      (`(if any)` suffix and `####` variants included) hold `- **title**`
      bullets (or a bare bullet line when there's no bold lead-in); `None.`
      with no bullet means empty. One round per pull-request-review id.
      `reviews_final.json` keeps every round ever seen (used for `GATE`'s
      monthly rejected count and the finding-theme lens); `dashboard_data.json`
      is the trailing-7-day rollup (`modules`, `days`, `issues`, `cleanPRs`,
      `cleanByModule`, `totals`) — clean passes are merged PRs that were
      reviewed (not `review:none`) and never appear in `reviews_final.json`'s
      full history, not just the 7-day window.
   b. Refresh `data/pr_times.json` (all merged PRs Jan 1→now: number →
      [created_at, merged_at], via `search_pull_requests`). Merged counts and
      median time-to-merge in `GATE` are derived from it automatically; the
      current month's rejected count is derived from `reviews_final.json`
      automatically too (`gate_series()` in `build_dashboard.py`) — no manual
      Gmail month-window search. Older months in `GATE` predate the
      PR-review dump and stay as seeded historical constants.
   c. Update the hard-coded window copy in `retro_template.html` (the
      "Last 7 days in review · <dates>" divider and the totals sentences in
      the first-pass panel and footer) to the new window.

5. **Build**: `python3 scripts/retrospective/build_dashboard.py`
   → `scripts/retrospective/retro.html`.

6. **Publish** the built file with the Artifact tool to the URL above
   (`url` parameter — same URL, do not create a new artifact). Favicon stays 🔍.

7. **Commit** refreshed `data/*.json` (and GATE/template edits) via the
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
