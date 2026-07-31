# Scrum Master Runbook (scrummaster-agent)

## Mission
Keep work flowing by selecting the next issue deterministically.

## Backlog ownership
- scrummaster-agent is assignee for all Backlog issues.

## Deterministic selection
1) Find lowest-numbered Phase with remaining open issues
2) Within that Phase, choose lowest issue number
3) Ensure it is in the active Sprint (if active Sprint exists)
   - If not, add it to active Sprint

## Dispatch
- Move issue status Backlog -> In Progress
- Assign to developer-agent

## Triggering the workflow

To start the next issue:

**Option 1: CLI script (recommended)**
```bash
./scripts/trigger_next_issue.sh <release_issue_number>
```

Example:
```bash
./scripts/trigger_next_issue.sh 2843
```

**Option 2: Manual comment**
Post a comment containing `READY_FOR_NEXT_ISSUE` in the **Release tracking issue**:
```
@nyxGPT-scrummaster-agent READY_FOR_NEXT_ISSUE
```

The workflow will:
- Post status updates on the Release tracking issue
- Select the next backlog issue based on deterministic rules
- Move that issue to In Progress and assign to developer-agent
- Trigger the developer auto-implementation workflow

**Monitoring:**
Use `./scripts/watch_agents.sh` to monitor all agent workflows in real-time.

## Wait
- Remain idle until triggered by READY_FOR_NEXT_ISSUE signal

## Phase completion
- When all issues in active Phase are complete:
  - notify human owner for acceptance
  - do not start next phase until human closes phase

## Sprint autopilot (#3480)

With the `SPRINT_AUTOPILOT` repo var set to `true` and a Sprint active,
`scripts/agents/review_accept_and_merge.sh` posts `READY_FOR_NEXT_ISSUE`
itself after every merge -- no human kick needed -- as long as the active
Sprint still has open Backlog issues:

```bash
./scripts/agents/scrummaster_next_issue.sh --sprint-scoped --select-only
```

Once the sprint has no open Backlog issues left, the merge script posts a
"sprint complete" note instead of a kick, and autopilot stops -- starting
work outside the sprint still needs a manual `READY_FOR_NEXT_ISSUE`.

**Kill switch:** set the `SPRINT_AUTOPILOT` repo var to `false`/unset it, or
post `PAUSE_SPRINT` as a comment on the release tracking issue (resume with
`RESUME_SPRINT`). With autopilot off or no active Sprint, behavior is
exactly the manual-kick flow above.

## Sprint reporting and reorganization (#3480)

On a schedule (intended: daily), post sprint standing to the release
tracking issue:

```bash
./scripts/agents/scrummaster_sprint_report.sh          # posts the report
./scripts/agents/scrummaster_sprint_report.sh --dry-run # prints it instead
```

The report includes done/in-review/in-progress/remaining counts, velocity,
a projected-completion-vs-end-date verdict (on-track / at-risk / off-track),
and any blockers. An off-track verdict includes a concrete reorganization
proposal (issues to move out of the sprint, lowest priority / not started
first) embedded as a machine-readable marker in the comment.

scrummaster-agent never applies that proposal itself. It only takes effect
once the human owner (stakeholder-agent's approval role) comments
`APPROVE_SPRINT_REORG` on the release tracking issue, which runs:

```bash
./scripts/agents/scrummaster_sprint_reorg_apply.sh
```

This applies the most recent unapplied proposal via the existing Sprint
project-field scripts and posts a summary of exactly what moved. Declining
or ignoring the proposal changes nothing.
