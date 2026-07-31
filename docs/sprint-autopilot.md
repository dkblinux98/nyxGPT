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
  (or sprint-complete note), gated on `SPRINT_AUTOPILOT` and `PAUSE_SPRINT`.
- `scripts/agents/scrummaster_sprint_report.sh` -- new: posts the sprint
  standing report (+ reorg proposal when off-track).
- `scripts/agents/scrummaster_sprint_reorg_apply.sh` -- new: applies the
  most recent unapplied reorg proposal.
- `agents/charters/{scrummaster,stakeholder}-agent.md`,
  `agents/prompts/{scrummaster,stakeholder}-agent.prompt.md`,
  `agents/runbooks/scrummaster-runbook.md` -- updated.
- Tests: `tests/unit/test_sprint_calc.py`,
  `tests/unit/test_summarize_backlog_page.py`,
  `tests/test_sprint_autopilot_lib.sh`.

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
