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

## Unresolved-escalation dispatch pause backstop (owner-ratified 2026-08-09, #3687)

Before dispatching, `scrummaster_dispatch_next.sh` checks
`escalation_pause_gate` (`scripts/agents/lib/gh_project.sh`):
"unresolved escalation" = an open issue currently assigned to
`HUMAN_OWNER` (`count_unresolved_escalations`) -- purely derived from live
issue state, no hidden counter to drift out of sync. Both escalation paths
(the review agent's 3-cycle breaker, and the huddle's type-(c)/deadlock
escalation, see below) end in exactly that state.

- **0 or 1 unresolved escalations:** dispatch proceeds unconditionally --
  one escalated item is normal traffic.
- **2 or more unresolved escalations:** new dispatch **pauses**. A loud
  report (listing the escalated issues) is posted, or updated in place if
  already posted, on the release tracking issue. `notify_scrum_ready.yml`
  posts a matching notice on the triggering comment's issue instead of its
  usual "no eligible issues"/"queue blocked" comments.
- **Resuming:** automatic, the next time dispatch runs, once the count
  drops below 2 -- there is no separate "resume" action. Clearing the
  escalations (the owner is already needed for them) is what reopens the
  gate; the stale release-issue report is updated to say so rather than
  left dangling.

## Review huddle mediation (owner-ratified 2026-08-09, #3687)

When `developer_huddle_position.yml` posts `HUDDLE_MEDIATION_REQUESTED` on
a PR (see `agents/runbooks/review-runbook.md` §6b for the full taxonomy and
huddle trigger conditions), `scrummaster_huddle_mediation.yml` runs a
**fresh** scrummaster invocation -- fresh context is structural, every
invocation starts memoryless, so the decision is based only on what's
actually in the PR thread, never an assumption carried from a prior
session. It reads the developer's `## Developer Position` comment and the
review agent's code review comment (the review's position), then posts
exactly one `## Huddle Decision` comment choosing:

- **proceed** -- the existing approach is right, continue as-is.
- **change-approach** -- a specific different approach, stated concretely.
- **descope** -- a specific descope (e.g. drop a named flaky test, split
  off a follow-up issue) that resolves the disagreement.
- **escalate** -- only the owner can resolve this; the mediation run itself
  performs the standard escalation (`assign_issue_verified` +
  `sprint_autopilot_kick`, the same primitives the 3-cycle breaker uses)
  rather than deferring it to a later step.

The decision is advisory text the next fix cycle
(`developer_auto_implement.yml`) reads and executes -- mediation does not
dispatch the fix itself.

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

## Acceptance-criteria capability guardrail (#3647)

When authoring or triaging an issue's acceptance criteria (via `/issue` or
manual creation), every checkbox must be executable by the developer-agent
sandbox itself. The sandbox cannot: dispatch or inspect live
`workflow_dispatch`/Actions runs, change repo **Settings** (branch
protection, secrets, variables, webhooks), run any `gh` CLI command (its
implementation instructions explicitly prohibit this), or use credentials
it isn't issued. An AC that silently requires one of these stalls the loop
on a step no agent can perform and no one notices until a human/EA
intervenes manually.

- If the criterion isn't truly required to close the issue, drop it and
  file a separate owner/EA-assisted follow-up instead.
- If it must stay, mark it explicitly so the review agent doesn't block
  acceptance on it: `- [ ] (owner/EA-assisted) <step>`.
- See `agents/runbooks/developer-runbook.md` §1a for the same guardrail
  from the authoring side, and the incident it's based on (#3614/PR #3645:
  an unmarked live-dispatch AC required manual EA intervention).

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
