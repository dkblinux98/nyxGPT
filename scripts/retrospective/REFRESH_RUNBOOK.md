# Retrospective Dashboard - Daily Refresh Runbook

Owner-facing runbook for the scheduled Claude session that refreshes the
**nyxGPT Project Retrospective** artifact. This is owner tooling, not part of the
nyxGPT product; it is executed by an assistant session (executive-assistant role),
not by the agent loop.

Artifact URL (republish to this URL, do not mint a new one):
`https://claude.ai/code/artifact/2b850289-fbb2-4e55-abf7-ea55d4501701`

## How a dump reaches the default branch (#3815)

Every retro workflow publishes its JSON to the **`claude/retro-data`**
branch (`scripts/retrospective/publish_data_branch.sh`), never straight at the
branch it was dispatched on: a repository ruleset requires changes to the
default branch to arrive through a pull request, and a direct push is rejected
with `GH013 - Changes must be made through a pull request`. The same run then
opens that pull request and merges it
(`scripts/retrospective/merge_data_branch.sh`) — the owner-approved review
exception for this tooling (2026-07-31), bounded by the guard that refuses any
branch touching files outside `scripts/retrospective/`.

The same ruleset also requires **one approving review** (`PR Rules`,
`required_approving_review_count: 1`, no bypass actors — read 2026-08-16,
ledger V-023), and a data refresh has no reviewer. So the run approves its own
pull request with a **second** agent identity: the scrummaster token opens and
merges it, the review-agent token approves it, because GitHub refuses
self-approval. That approval is bookkeeping to satisfy the rule — what
actually bounds this path is the guard, which runs before the pull request is
ever opened.

So the "dispatch then `git pull`" in step 2 below is really: dispatch → dumps →
`claude/retro-data` → approve → auto-merged pull request → default branch →
`git pull`.
**A green dump run therefore means the data landed** — the whole failure of
#3815 was that it did not. If a `git pull` still shows nothing, read that
run's "Land it on the default branch" step rather than re-dispatching.

`retro_data_merge.yml` does the same landing for a branch pushed by hand
(step 5), so the manual path needs no extra step either.

## Steps

**Every input is produced by one workflow run, never by this session.** Until
2026-09-17 the session dispatched five single-file dumps and wrote two more
inputs by hand from MCP queries, and it had drifted: each pass dispatched
whichever dumps it chose and generated the rest locally, so each day refreshed
a different subset — churn (the slowest) was dropped most often, and
`project_fields.json`, which nothing but a workflow can produce (it needs the
project-scoped token and the Project GraphQL), sat five days stale while every
pass reported success. There is no local path any more: do not run a
`dump_*.py` here, do not write `all_issues.json` or `pr_times.json` by hand,
and do not dispatch the single-file dumps for a normal pass.

1. **Check out the repository default branch** — resolve it dynamically via
   `git ls-remote --symref origin HEAD`, never hardcode a version (v3.0.0 as of
   2026-08-03; note the default branch is the *development* branch and may
   differ from the `RELEASE_BRANCH` Actions variable during a cutover). The
   template, builder, and data seeds live under `scripts/retrospective/` there.

2. **Refresh every input** → dispatch the workflow **`retro_data_refresh.yml`**
   on the default branch (`actions_run_trigger`, method `run_workflow`; optional
   `window_days` input, default 30), **once**. It runs all seven dumps in
   sequence, publishes whatever was produced to `claude/retro-data` and lands
   it on the default branch through one auto-merged pull request:

   | Step | Files | Single-file re-run |
   |---|---|---|
   | issue corpus + merged-PR times (`dump_issue_corpus.py`) | `all_issues.json`, `pr_times.json` | none — only the refresh produces these |
   | project fields (`dump_project_fields.sh`) | `project_fields.json` | `retro_project_fields_dump.yml` |
   | native relationships (`dump_relationships.py`, #3731) | `relationships.json` | `retro_relationships_dump.yml` |
   | review rounds (`dump_review_rounds.py`, #3667) | `reviews_final.json`, `dashboard_data.json` | `retro_review_rounds_dump.yml` |
   | spend telemetry (`dump_spend.py`, #3696) | `spend.json` | `retro_spend_dump.yml` |
   | churn cost (`dump_churn.py`, #3776) | `churn.json` | `retro_churn_dump.yml` |

   **Wait for it to complete** — expect tens of minutes, not one: the review
   rounds walk every PR's reviews, spend makes a `timing` call per run, and
   churn downloads one job log per Claude round in the window (76 minutes on
   2026-09-17). Poll the run every few minutes; do not start other steps on
   yesterday's files. Each dump first waits for the token's hourly REST budget
   (`await_rate_limit.sh`), so a run may legitimately pause for up to an hour
   between dumps — the log says so.

   **Read the result before pulling:**
   - **Green** — every file landed. `git pull` and go to step 3.
   - **Red** — the last step, *Report every dump that did not land*, names
     each dump that failed (and the single-file workflow that re-runs it); the
     others were published and landed anyway. Open the named dump step and
     read the actual failure (the `gh` stderr is on the log now — a rate-limit
     notice, an auth failure, a schema change). Then see "When a dump does
     not land" below. `git pull` regardless: the files that landed are real.

   A `git pull` that shows nothing after a green run means the landing failed
   silently — read that run's "Land it on the default branch" step rather
   than re-dispatching (#3815).

2b. **What the review-rounds and churn dumps mean** (unchanged semantics):

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

   Where spend telemetry says what a run *cost to run*, churn cost says what
   the agent *spent thinking* and how much of that was re-onboarding. One
   round = one executed `claude-code-action` step (implement, review-fix,
   acceptance-fix, self-heal, review, session), attributed to an issue by
   branch name and numbered per issue in chronological order. Within a round,
   assistant turns before the first file-modifying tool use are counted as
   context re-establishment and the rest as change production, and the
   round's token total is split pro rata by that ratio — an explicit
   approximation (usage is reported once per step, not per turn), restated in
   `churn.json`'s `methodology` block and in the dashboard. Rounds whose logs
   yield no turn markers are counted in token totals but excluded from the
   split; expired logs are recorded with `tokens: null` rather than a false
   zero. Each refresh merges into the previously-dumped rounds, so history
   accumulates instead of being re-fetched.

   **Dollars** appear only when a price sheet is configured, and **the rates
   are never committed** — the repo ships only the zeroed
   `data/price_sheet.example.json`, precisely so it asserts no rate (#3744).
   Take that file's shape, fill in the per-million rates you are actually
   billed at (read from Anthropic's pricing page at that moment), and store
   the JSON in `~/.nyxGPT/config.ini` as `[github] churn_price_sheet_json`
   and run `nyxgpt ops config-sync`, which pushes it to the
   **`CHURN_PRICE_SHEET_JSON` repo variable** (config.ini is the canonical
   store for every Actions secret and variable — #3976); the workflow
   passes it to the dump, so nothing lands in the checkout. Do **not** commit
   a `price_sheet.json` — that path is gitignored (a developer testing
   `dump_churn.py` on a checkout can save the sheet there; a refresh pass
   never runs the dump locally). Re-check the rates whenever you refresh,
   and **re-dispatch the refresh after changing them**: dollars are computed
   at dump time, so a changed sheet changes nothing until the dump re-runs. With no sheet configured,
   the view reports tokens only.

   **Recording a stale-context incident** (the third part of churn cost —
   new-hire errors, where an agent acts on a fact a later session had already
   changed): add an object to `incidents` in
   `data/stale_context_incidents.json`. Required fields `id`, `date`,
   `kind` (one of the documented `kinds`), `title`, `summary`, `recordedIn`;
   optional `refs`, `rounds`, `notes`. The file's own `howToRecord` field
   carries the same instructions for whoever edits it. `dump_churn.py`
   validates it on every refresh and **fails the dump workflow** on a
   malformed or duplicate entry, so a bad edit surfaces immediately. The file
   is seeded with the three incidents documented in #3776 (the Acceptance
   Failed lane sweep, the stale rc4-wheel claims, the rc7 dispatch race).

3. **Build**: `python3 scripts/retrospective/build_dashboard.py`
   → `scripts/retrospective/retro.html`.

   The builder prints the build time and, under it, which inputs are **stale**
   (a day or more behind the build), **unstamped** (#3807) or **missing**
   (#3808). Read those three lines before publishing: a source listed there is
   one whose dump did not actually land in this pass, and the page will say so
   to the reader.

   **Exit status is part of the output.** A build with every input present
   and fresh exits 0. A build missing any input writes the page and exits
   **2**, naming each missing file and the dump that owes it. A build whose
   every input is present but any is **stale** writes the page and exits
   **3**, naming the file, how far behind it is, and what refreshes it. Both
   are the gate — see "When a dump does not land" below. The corpus-coverage
   copy ("Jan 1 – <date>") and the 7-day window copy are derived by the
   builder and the page script; nothing in `retro_template.html` or
   `build_dashboard.py` is hand-edited on a refresh (the `GATE` seeds for
   closed months are the one exception, and only when a month rolls over).

4. **Publish** the built file with the Artifact tool to the URL above
   (`url` parameter — same URL, do not create a new artifact). Favicon stays 🔍.

   **The build stamp in the page header is the check that a refresh landed**
   (#3807). After republishing, reload the artifact URL and read the header: it
   shows `built <date> <time> UTC`, so a page still showing the previous run's
   time means the publish did not take — the same class of silent failure as
   #3815, where green dumps were discarded server-side. The header also counts
   sources that are stale or unstamped, and the footer's **Data provenance**
   table lists every input with its own as-of time; each panel repeats the
   as-of time of the data behind it. Those are the lines to sanity-check
   before telling the owner the dashboard is current.

5. **Commit the built page** (`scripts/retrospective/retro.html`, plus a
   `GATE` seed edit if a month rolled over) via the `claude/retro-data`
   branch. The data files are already on the default branch — the refresh
   workflow landed them — so this commit carries the page only. From the
   checkout:

   ```bash
   BASE_REF="$(git ls-remote --symref origin HEAD | awk '$1=="ref:"{sub("refs/heads/","",$2);print $2;exit}')" \
     scripts/retrospective/publish_data_branch.sh \
     "chore(retro): rebuild retrospective page" scripts/retrospective/retro.html
   ```

   Pushing the branch with your own credentials triggers the
   `retro_data_merge.yml` workflow, which opens a pull request from that
   branch and merges it into the default branch immediately (owner-approved
   exception to the review loop for this tooling, 2026-07-31; it goes through a
   pull request, approved by a second agent identity, because the default
   branch's ruleset requires both — #3815).
   Verify the merge landed (workflow completes in ~1 min; check with
   `git ls-remote` that the default branch tip now contains the merge commit)
   and report a failed merge in the run summary. Touch only files under
   `scripts/retrospective/` — both the publish script and the merge workflow
   refuse anything else — and never push to any other branch. Opening the
   pull request is the merge workflow's job, not yours. **Never commit a data
   file from this session**: a `data/*.json` in this commit means a dump was
   run locally, which is the drift step 2 exists to end.

## When a dump does not land (#3808)

The rule the old wording broke: **a refresh never publishes a
quietly-incomplete dashboard.** Steps 4 and 4b used to say to skip a failed
dump because "the builder omits the section entirely rather than erroring".
That is exactly how `retro_churn_dump.yml` — which had failed on *every run it
ever had* — produced a normal-looking dashboard with the churn section simply
absent, for a day, with nothing anywhere reporting it. A missing panel is
indistinguishable from a feature that was never built.

The same rule now covers the dump that was never *run* (2026-09-17). A dump
that fails leaves no file; a dump that is skipped leaves yesterday's file, the
build used to exit 0, and the page's "2 sources stale" header was a line
nobody was obliged to act on. A stale input is now refused exactly like a
missing one.

What happens now, and what you owe the owner:

1. **The run tells you.** A red `retro_data_refresh.yml` run ends with *Report
   every dump that did not land*, naming each failed dump; the job summary
   repeats it. The other files landed.
2. **The build tells you.** `missing sources: churn.json (retro_churn_dump.yml)`
   and exit **2** when the file is absent; `stale sources: churn.json` and
   exit **3** when it is present but a day or more behind the build. The
   page is still written either way.
3. **The page tells the reader.** A missing section renders a "Section
   unavailable — this is missing data, not zero" notice naming the data file
   and linking the dump's runs, instead of disappearing; a stale source is
   called out on every panel it feeds and in the provenance table. The header
   counts both next to the build stamp.
4. **Read the run before re-dispatching.** Open the named dump's step in the
   refresh run and find the actual failure — `gh`'s stderr is printed there.
   Guessing and re-dispatching is how one defect gets patched three times.
5. **Re-dispatch and rebuild** if the failure was transient: the **single-file
   workflow named in the report** (`retro_<name>_dump.yml`), not the whole
   refresh. Every retro workflow shares the `retro-data-branch` concurrency
   group, so dispatch **one at a time** — GitHub keeps only the newest
   *pending* run in a group and cancels earlier ones, `cancel-in-progress:
   false` notwithstanding. The corpus and PR times have no single-file dump;
   re-dispatch the refresh for those.
6. **If you publish anyway**, do it deliberately: rebuild with
   `--allow-missing-sources` and/or `--allow-stale-sources` (exit 0, the
   notices and stale callouts stay on the page), and **report the failed dump
   with its run URL** in what you tell the owner. "Refreshed" without that
   sentence is the failure this rule exists to stop.

The same applies to `project_fields.json` and `relationships.json`: their
fallbacks (calendar weeks, prose attribution) keep the build usable, but a
missing or stale file still means a dump did not land and is still reported.

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

**Failure-issue model (2026-08-02; storage changed 2026-08-12, #3731):** a
unique acceptance failure is filed as a NEW issue that **blocks** the feature
through GitHub's native blocked-by/blocks relationship — no body prose, no
comment markers. The same is true of an Improvement filed with `@improvement`.
The original feature stays closed. A fix failing re-test REOPENS the same
failure issue — so failure-issue counts understate failure rounds; watch for
reopened failure issues when narrating trends.

Attribution therefore reads `data/relationships.json` (step 2b), with the
retired `Related feature: #N` prose surviving only as the historical fallback
described there.
