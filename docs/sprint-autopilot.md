# Sprint Autopilot (#3480)

Implements the owner requirement (2026-07-31): stop requiring a human to
watch every merge to kick off the next issue, have the scrummaster continue
working a sprint until it's done, report on sprint standing against the
sprint's end date, and let the stakeholder approve reorganizing the sprint
when it's off track.

## What ships in this change

Everything that is **not** a `.github/workflows/*` file ships directly in
this PR:

- `scripts/agents/lib/gh_project.sh` -- shared `BACKLOG_PAGE_QUERY`,
  `count_sprint_backlog_open`, `sprint_autopilot_paused`,
  `clear_project_field_value`.
- `scripts/agents/lib/summarize_backlog_page.py` -- the sprint-scoped
  selection guard, shared by `scrummaster_next_issue.sh` and
  `count_sprint_backlog_open`.
- `scripts/agents/lib/sprint_calc.py` -- pure sprint math (velocity,
  verdict, reorg candidate selection, autopilot stop condition) and the
  markdown report renderer.
- `scripts/agents/scrummaster_next_issue.sh` -- new `--sprint-scoped` flag.
- `scripts/agents/review_accept_and_merge.sh` -- post-merge autopilot kick
  (or sprint-drained park note, #3706), gated on `SPRINT_AUTOPILOT` and
  `PAUSE_SPRINT`.

## The sprint boundary (owner policy, 2026-08-10, #3706)

The autopilot's continue/park decision is **sprint-gated**: the automatic
loop is bound by the current sprint, and sprint membership is a real work
boundary rather than bookkeeping.

- The **dispatch** input is the count of open Backlog issues in the
  **active sprint iteration** -- the iteration whose date window contains
  today (`iteration_active_title` + `count_sprint_backlog_open`).
- The **park** input is the active sprint's whole issue population, open
  and closed, bucketed by Status (`sprint_population_snapshot` +
  `sprint_calc.py sprint-park-state`, #3709) -- see "Park states" below.
- `--sprint-scoped` selection is hard: future-sprint and no-sprint Backlog
  issues are skipped with a log line and never dispatched automatically.
  There is **no release-wide fall-through**.
- When the active sprint's Backlog drains, the autopilot posts a loud park
  note on the release tracking issue (rendered by
  `sprint_calc.build_sprint_park_note`): which park state it is in, what
  remains in the release per future sprint, which parked issues are waiting
  on gates, and how work resumes.
- **Human override:** a `READY_FOR_NEXT_ISSUE` posted by the owner runs
  unscoped, so the owner can deliberately pull work forward across the
  boundary. Agent-posted kicks cannot.
- **Drift caveat:** the boundary is load-bearing, so iteration date windows
  must be kept current. With no active iteration, both the autopilot and
  `scrummaster_next_issue.sh --sprint-scoped` stop (conservative stop) --
  the selector exits 1 rather than falling back to release-wide selection,
  so a kick that lands after the window closes cannot dispatch
  future-sprint work.

### Park states (#3709)

An empty Backlog is not completion. `sprint_calc.py sprint-park-state`
classifies the sprint's whole population into one of five states, and the
park note says which one:

| State | Meaning |
| --- | --- |
| `continue` | open Backlog work remains -- kick, don't park |
| `work_in_flight` | Backlog empty, but issues are still open (In Progress / In Review) |
| `awaiting_acceptance` | every item closed, not all accepted: "agentic work complete; awaiting owner acceptance" |
| `sprint_complete` | every item accepted and in **For Release** |
| `empty` | the sprint has no items |

**Owner definition (2026-08-10, #3709):** *"The sprint isn't done until all
agentic work is complete AND in For Release status."* Only `sprint_complete`
may declare the sprint done. Promotion to For Release is owner-only --
agents never self-promote. If the population snapshot can't be read, the
decision degrades to the pre-#3709 Backlog-only count and the note claims no
completion state.

### Dependency-aware auto-resume (#3709)

On every kick, before any park, the loop scans the active sprint's In
Progress issues for **parked** ones -- no open PR closing the issue and no
in-flight developer run -- and auto-posts `RETRY_IMPLEMENTATION` on **one**
whose declared blockers have all closed. A sequenced chain therefore walks
itself: each merge opens the next gate and kicks the loop again.

- Parked issues whose blockers are still open are reported in a **"waiting
  on gates"** line in the park note and the continue kick -- never dropped.
- Auto-resumes are bounded by the #3689 retry cap, counted from
  `<!-- nyxgpt-autoresume: ... -->` markers in the issue's own thread and
  reset only by an owner comment. Out-of-budget issues are reported as
  gate-stuck instead of retried.
- Liveness signals are observable state only: open PRs from the plain pulls
  list (never `gh api search/issues`, #3694) and live runs matched on the
  runs API's `display_title`.
- The prose `Blocked by: #N` parser (`scripts/agents/lib/parked_resume.py`)
  is **interim** -- issue-body references only, unioned with native
  `blocked_by` deps, and superseded by the native Relationships work
  (W1/W2, `product_management/AGENTIC_SDLC_DESIGN.md`).

This does not resume the loop across a sprint boundary or consume the
owner's acceptance window: it only restarts work the sprint already owns.

### Informational notes must never look like a kick

`notify_scrum_ready.yml` dispatches on a bare
`contains(github.event.comment.body, 'READY_FOR_NEXT_ISSUE')` with the agent
accounts on its actor allowlist. Any agent comment that *names* the kick
token therefore starts the next issue, even one whose whole point is that
work has stopped. Two rules keep status reports inert:

1. Informational autopilot comments (the park note, the `PAUSE_SPRINT`
   notice) never spell the token out -- they point here instead.
2. They carry the marker `<!-- nyxgpt-autopilot-informational -->`
   (`AUTOPILOT_INFO_MARKER` in `scripts/agents/lib/gh_project.sh` and
   `scripts/agents/lib/sprint_calc.py`), which the workflow's job `if:`
   negates. This is the structural guard: it holds even if a note's prose
   later drifts back into naming the token.

To kick manually, post a comment containing `READY_FOR_NEXT_ISSUE` (and no
marker) on the release tracking issue.

### The sprint boundary is an acceptance gate

Owner context, 2026-08-10: a sprint completes -> the owner runs acceptance
testing on it -> the next sprint begins. The park note says so explicitly,
and nothing resumes the loop on its own -- a new sprint window opening does
not by itself dispatch work, because only a kick starts selection and agents
post kicks only after a merge. That is deliberate: auto-resume would consume
the owner's acceptance window, which is exactly what happened on 2026-08-09.

*Correction of the record:* this file previously documented a
release-gated decision as an "owner decision, 2026-07-31". The owner has
stated that attribution was wrong -- release-gating across sprint
boundaries was not their intention, and the rationale was agent-authored.
Sprint-gating is the standing policy. Related process rule: a comment or
doc line claiming "owner decision" must cite a traceable source (issue
number or owner comment link); uncited claims are agent rationale.

## The release wall (outer boundary)

The release wall remains, but only as the branch-safety boundary -- agents
merge to `RELEASE_BRANCH`, so next-release work must never be started:

- The release tracking issue's title carries the release version
  ("Release v2.0.0"), and every milestone title carries its release version
  ("Phase 5.5: ... (v2.0.0)", "Phase 6 — ... (v3.0.0)"). An issue is
  eligible for autopilot continuation and scrummaster selection **only if
  its milestone version matches the configured release issue's version**
  (`RELEASE_VERSION` filter in `lib/summarize_backlog_page.py`; wall applies
  to manual kicks too).
- Sprint-date boundaries are evaluated in the owner's timezone, not UTC
  (owner rule 2026-07-31: "midnight is midnight EDT"). Every "has this
  sprint started/ended?" comparison uses `sprint_today()` from
  `lib/gh_project.sh`, which computes today in `SPRINT_TIMEZONE`
  (optional config key, default `America/New_York` -- EST/EDT handled
  automatically). Under UTC, sprints flipped at 8pm Eastern.
- When the release's Backlog drains too, the park note says so. There is no
  switch to remember: the gate reopens automatically when the owner performs
  the release ceremony -- pointing `RELEASE_ISSUE_NUMBER` at the next
  release's tracking issue and `RELEASE_BRANCH` at its branch. Until then
  the next release's issues are structurally invisible to the loop.
- `add-to-release-issue-on-milestone.yml` applies the same version match:
  a milestoned issue is appended only to the tracking issue of its own
  release.
- `scripts/agents/scrummaster_sprint_report.sh` -- new: posts the sprint
  standing report (+ reorg proposal when off-track).
- `scripts/agents/scrummaster_sprint_reorg_apply.sh` -- new: applies the
  most recent unapplied reorg proposal.
- `agents/charters/{scrummaster,stakeholder}-agent.md`,
  `agents/prompts/{scrummaster,stakeholder}-agent.prompt.md`,
  `agents/runbooks/scrummaster-runbook.md` -- updated.
- Tests: `tests/unit/test_sprint_calc.py`,
  `tests/unit/test_summarize_backlog_page.py`,
  `tests/test_sprint_autopilot_lib.sh`,
  `tests/test_scrummaster_sprint_boundary.sh` (#3706, end-to-end selector
  boundary check against a fake `gh`).

## What the owner needs to apply by hand

Agent tokens cannot write `.github/workflows/*` (same hand-carry pattern as
#3454/#3479). Two things need manual application:

### 1. New repo variables

Add alongside the existing ones (Settings -> Secrets and variables ->
Actions -> Variables):

| Variable Name | Example Value | Description |
|---|---|---|
| `SPRINT_AUTOPILOT` | `false` | Kill switch: `true` enables the self-continuing merge -> next-issue loop. Off by default -- unset/`false` reproduces today's manual-kick flow exactly. |
| `SPRINT_FIELD` | `Sprint` | Project iteration field name used for sprint scoping/reporting. Optional -- every script defaults to `Sprint` if unset. |

`RELEASE_ISSUE_NUMBER` already exists (see `docs/github-tokens.md`) but is
not currently written into every workflow's ephemeral config.ini -- see
below.

### 2. Workflow file changes

**`review_agent_auto_review.yml`** -- the "Write ephemeral config.ini" step
needs three more lines so the merge script can find the release tracking
issue and read the autopilot switch:

```diff
           RELEASE_BRANCH=${{ vars.RELEASE_BRANCH }}
+          RELEASE_ISSUE_NUMBER=${{ vars.RELEASE_ISSUE_NUMBER }}
+          SPRINT_AUTOPILOT=${{ vars.SPRINT_AUTOPILOT }}
+          SPRINT_FIELD=${{ vars.SPRINT_FIELD }}
           EOF
```

**`notify_scrum_ready.yml`** -- same config.ini addition (drop the
`SPRINT_AUTOPILOT` line here too, or keep it for parity -- the selection
step below is what actually reads it), plus the "Select next issue" step
needs to sprint-scope selection while autopilot is on:

```diff
           RELEASE_BRANCH=${{ vars.RELEASE_BRANCH }}
+          RELEASE_ISSUE_NUMBER=${{ vars.RELEASE_ISSUE_NUMBER }}
+          SPRINT_AUTOPILOT=${{ vars.SPRINT_AUTOPILOT }}
+          SPRINT_FIELD=${{ vars.SPRINT_FIELD }}
           EOF
```

```diff
       - name: Select next issue
         id: select
         shell: bash
         run: |
           set -euo pipefail
           echo "::group::Selecting next issue from Backlog"
-          # Use --select-only since workflow handles starting separately
-          NEXT_ISSUE=$(bash -lc './scripts/agents/scrummaster_next_issue.sh --select-only' || echo "")
+          # Use --select-only since workflow handles starting separately.
+          # Sprint-scope selection while autopilot is on (#3480) -- the
+          # script itself falls back to unscoped selection if no Sprint is
+          # currently active, so this is safe to pass unconditionally.
+          SPRINT_ARGS=""
+          if [[ "${{ vars.SPRINT_AUTOPILOT }}" == "true" ]]; then
+            SPRINT_ARGS="--sprint-scoped"
+          fi
+          NEXT_ISSUE=$(bash -lc "./scripts/agents/scrummaster_next_issue.sh --select-only $SPRINT_ARGS" || echo "")
```

> **Already applied, and since superseded (#3706).** `notify_scrum_ready.yml`
> now runs a single "Select and start next issue" step through
> `scrummaster_dispatch_next.sh`, and its sprint-scoping condition also
> requires that the kick was *not* posted by the human owner:
> `[[ "$SPRINT_AUTOPILOT" == "true" && "${KICK_ACTOR,,}" != "${KICK_OWNER,,}" ]]`
> (case-insensitive, to match the job-level `if:`). That is the owner
> override -- a manual `READY_FOR_NEXT_ISSUE` selects unscoped and can pull
> future-sprint work forward on purpose. The "falls back to unscoped
> selection if no Sprint is active" comment in the snippet above is also
> history: `--sprint-scoped` with no active iteration now exits 1
> (conservative stop). Agents can write `.github/workflows/*` in this repo
> (see `CLAUDE.md`), so this section is history, not a pending hand-carry.

**New file `scrummaster_sprint_report.yml`** (daily standing report):

```yaml
name: Scrummaster Agent - Sprint Report

on:
  schedule:
    - cron: '0 13 * * *'  # daily; adjust to the team's timezone/preference
  workflow_dispatch: {}

permissions:
  issues: write
  contents: read

jobs:
  sprint-report:
    if: vars.AGENTS_ENABLED == 'true'
    runs-on: ubuntu-latest

    env:
      GH_TOKEN: ${{ secrets.SCRUMMASTER_AGENT_TOKEN }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          ref: ${{ vars.RELEASE_BRANCH }}
          fetch-depth: 0

      - name: Auth gh
        run: gh auth status

      - name: Write ephemeral config.ini (runner temp)
        shell: bash
        run: |
          set -euo pipefail
          CFG="$RUNNER_TEMP/nyxgpt-config.ini"

          cat > "$CFG" <<EOF
          REPO_OWNER=${{ vars.REPO_OWNER }}
          REPO_NAME=${{ vars.REPO_NAME }}
          PROJECT_OWNER=${{ vars.PROJECT_OWNER }}
          PROJECT_NUMBER=${{ vars.PROJECT_NUMBER }}

          DEV_AGENT=${{ vars.DEV_AGENT }}
          REVIEW_AGENT=${{ vars.REVIEW_AGENT }}
          SCRUM_AGENT=${{ vars.SCRUM_AGENT }}
          HUMAN_OWNER=${{ vars.HUMAN_OWNER }}

          STATUS_FIELD=${{ vars.STATUS_FIELD }}
          STATUS_BACKLOG=${{ vars.STATUS_BACKLOG }}
          STATUS_IN_PROGRESS=${{ vars.STATUS_IN_PROGRESS }}
          STATUS_IN_REVIEW=${{ vars.STATUS_IN_REVIEW }}
          STATUS_FOR_RELEASE=${{ vars.STATUS_FOR_RELEASE }}

          RELEASE_BRANCH=${{ vars.RELEASE_BRANCH }}
          RELEASE_ISSUE_NUMBER=${{ vars.RELEASE_ISSUE_NUMBER }}
          SPRINT_FIELD=${{ vars.SPRINT_FIELD }}
          EOF

          echo "NYXGPT_CONFIG_FILE=$CFG" >> "$GITHUB_ENV"

      - name: Post sprint report
        shell: bash
        run: |
          set -euo pipefail
          bash -lc './scripts/agents/scrummaster_sprint_report.sh'
```

**New file `scrummaster_sprint_reorg_apply.yml`** (stakeholder approval
trigger):

```yaml
name: Scrummaster Agent - Apply Sprint Reorg

on:
  issue_comment:
    types: [created]

permissions:
  issues: write
  contents: read

jobs:
  apply-reorg:
    if: >
      vars.AGENTS_ENABLED == 'true' &&
      contains(github.event.comment.body, 'APPROVE_SPRINT_REORG') &&
      github.event.comment.author_association == 'OWNER' &&
      github.event.issue.number == fromJSON(vars.RELEASE_ISSUE_NUMBER)
    runs-on: ubuntu-latest

    env:
      GH_TOKEN: ${{ secrets.SCRUMMASTER_AGENT_TOKEN }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          ref: ${{ vars.RELEASE_BRANCH }}
          fetch-depth: 0

      - name: Auth gh
        run: gh auth status

      - name: Write ephemeral config.ini (runner temp)
        shell: bash
        run: |
          set -euo pipefail
          CFG="$RUNNER_TEMP/nyxgpt-config.ini"

          cat > "$CFG" <<EOF
          REPO_OWNER=${{ vars.REPO_OWNER }}
          REPO_NAME=${{ vars.REPO_NAME }}
          PROJECT_OWNER=${{ vars.PROJECT_OWNER }}
          PROJECT_NUMBER=${{ vars.PROJECT_NUMBER }}

          DEV_AGENT=${{ vars.DEV_AGENT }}
          REVIEW_AGENT=${{ vars.REVIEW_AGENT }}
          SCRUM_AGENT=${{ vars.SCRUM_AGENT }}
          HUMAN_OWNER=${{ vars.HUMAN_OWNER }}

          STATUS_FIELD=${{ vars.STATUS_FIELD }}
          STATUS_BACKLOG=${{ vars.STATUS_BACKLOG }}
          STATUS_IN_PROGRESS=${{ vars.STATUS_IN_PROGRESS }}
          STATUS_IN_REVIEW=${{ vars.STATUS_IN_REVIEW }}
          STATUS_FOR_RELEASE=${{ vars.STATUS_FOR_RELEASE }}

          RELEASE_BRANCH=${{ vars.RELEASE_BRANCH }}
          RELEASE_ISSUE_NUMBER=${{ vars.RELEASE_ISSUE_NUMBER }}
          SPRINT_FIELD=${{ vars.SPRINT_FIELD }}
          EOF

          echo "NYXGPT_CONFIG_FILE=$CFG" >> "$GITHUB_ENV"

      - name: Apply sprint reorg proposal
        shell: bash
        run: |
          set -euo pipefail
          bash -lc './scripts/agents/scrummaster_sprint_reorg_apply.sh'
```

### 3. No workflow change needed for `PAUSE_SPRINT` / `RESUME_SPRINT`

These are read directly by `review_accept_and_merge.sh` (via
`sprint_autopilot_paused` in `gh_project.sh`, a live `gh api` comments
lookup at kick time) -- no new trigger/workflow required. Just post the
comment on the release tracking issue.

## Verifying the kill switch

With `SPRINT_AUTOPILOT` unset or `false`: `review_accept_and_merge.sh`
never posts `READY_FOR_NEXT_ISSUE`, and `scrummaster_next_issue.sh` without
`--sprint-scoped` behaves exactly as before -- the manual `READY_FOR_NEXT_ISSUE`
comment path in `notify_scrum_ready.yml` is untouched.

With `SPRINT_AUTOPILOT=true`: post `PAUSE_SPRINT` on the release tracking
issue; the next merge posts a paused notice instead of a kick. Post
`RESUME_SPRINT` to continue.
