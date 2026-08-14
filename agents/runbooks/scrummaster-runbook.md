# Scrum Master Runbook (scrummaster-agent)

## Mission
Keep work flowing by selecting the next issue deterministically.

## The operating ledger (#3774)

Read `agents/LEDGER.md` in full at session start. It is the system of record
for cross-session memory: decisions, verified facts (with method and date),
parked items (with revisit condition), open questions.

- **A claim that is not in the ledger and not freshly verified is not asserted
  as fact.** Board state you did not read this session is recollection. Re-read
  it rather than reporting from memory.
- **Never sweep state you did not create.** Before treating a lane, marker or
  field as stale, look for a `P-` entry explaining it. Items in
  `Acceptance Failed` are deliberately placed — open ones held by the drain
  gate, closed ones parked there by the owner as features they tested and
  failed (ledger D-001 and D-008, and "Park semantics and auto-resume"
  below) — not stale board state to clean up. The incident
  that created the ledger was exactly this sweep destroying the owner's parked
  failure markers.
- **Check Superseded before correcting anyone**; re-asserting a retired belief
  is itself the defect.
- **Append** an entry when your session settles something: an owner decision
  from an issue thread, work deliberately parked, a question that gates
  selection. Entries go through the normal branch/PR path, and carry
  load-bearing facts only — never narration of the selection you just made.

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

Both this pause and the "every eligible Backlog candidate was unclaimable"
queue-blocked case (`scrummaster_dispatch_next.sh`'s fall-through loop
exhausting `MAX_ATTEMPTS`) are head-of-line blocks on the whole queue, so
`scrummaster_dispatch_next.sh` also sends a Slack DM to the owner for each
(`notify_human_escalation`, `scripts/agents/lib/gh_project.sh`, #3695),
attached to and deduped against `RELEASE_ISSUE_NUMBER` -- the same
dispatch-wide target the release-issue report above and
`sprint_autopilot_kick` already use. Skipped silently if
`RELEASE_ISSUE_NUMBER` is not configured, and never blocks the dispatch
loop itself on a Slack failure (same graceful-degradation contract as
every other `notify_human_escalation` caller).

## Cross-issue infrastructure-anomaly dispatch pause backstop (#3694)

Composes with the escalation-pause backstop above: `scrummaster_dispatch_next.sh`
also checks `cross_issue_anomaly_pause_gate` (`scripts/agents/lib/gh_project.sh`)
before selecting -- either gate pausing skips dispatch entirely
(`paused=true`), and `pause_reason` (`"escalation"` or
`"cross_issue_anomaly"`) tells `notify_scrum_ready.yml` which report to
quote back on the triggering comment's issue.

See `agents/runbooks/developer-runbook.md` §3f for the full detection
mechanism (developer-side): the same step failing on multiple in-flight
issues within a short window (default 60 minutes) is treated as one
infrastructure event, not N coding problems, and the first issue to hit it
opens a single tracking-record marker comment on the release tracking
issue. While that record is open:

- **New dispatch pauses.** A loud report is posted, or updated in place,
  on the release tracking issue -- mirroring the escalation-pause report's
  shape. The dispatch also sends the #3695 Slack DM with its own state
  (`anomaly-paused`, distinct from the escalation pause's
  `dispatch-paused` so the message names the actual cause and the two
  backstops never de-duplicate against each other).
- **Resuming:** automatic, once the tracking record is resolved (an
  OWNER-authored `RESOLVE_ANOMALY` comment) or its detection window
  elapses -- no separate "resume" action, same as the escalation backstop.

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

The decision's content is advisory text the next fix cycle
(`developer_auto_implement.yml`) reads and executes; mediation does not
dispatch that fix itself. **Starting it is not left to chance, though
(#3736):** `huddle_decision_dispatch.yml` reacts to the decision comment and,
for proceed / change-approach / descope, puts the issue back to In Progress
and hands it to the developer agent. Before that workflow existed the
decision just sat there -- on PR #3733 the thing that eventually moved was
the 3-cycle escalation firing six minutes after a "proceed", parking the
issue on the owner. Post exactly one decision comment; a duplicated or
re-posted decision is ignored by the dispatcher (only the round's first
decision runs, and only once).

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
park note instead of a kick, and autopilot stops dispatching -- starting
work outside the sprint still needs a manual `READY_FOR_NEXT_ISSUE`. What
that note *says* depends on the sprint's whole population, not just its
Backlog: see "Park semantics" below.

**Kill switch:** set the `SPRINT_AUTOPILOT` repo var to `false`/unset it, or
post `PAUSE_SPRINT` as a comment on the release tracking issue (resume with
`RESUME_SPRINT`). With autopilot off or no active Sprint, behavior is
exactly the manual-kick flow above.

## The sprint boundary is where the auto loop stops (owner policy 2026-08-10, #3706)

**The automatic loop is bound by the current sprint.** Sprint membership is
a real work boundary, not bookkeeping:

- The autopilot's continue/park decision counts open Backlog issues in the
  **active sprint iteration only** -- the iteration whose date window
  contains today, evaluated in `SPRINT_TIMEZONE`
  (`iteration_active_title` + `count_sprint_backlog_open`,
  `scripts/agents/lib/gh_project.sh`).
- Agent-posted kicks select `--sprint-scoped`, and that scope is **hard**:
  Backlog issues in a future sprint, or with no Sprint set, are skipped with
  a log line and are never dispatched automatically. There is no
  release-wide fall-through.
- When the active sprint's Backlog drains, the autopilot **parks with a
  loud note** on the release tracking issue: which park state it is in (see
  "Park semantics"), what remains in the release per future sprint (and how
  much has no sprint at all), any parked issues waiting on gates, and that
  work resumes when the next sprint's window opens or a human posts a kick.
  The boundary is an **acceptance gate** (owner context 2026-08-10: sprint
  completes -> owner runs acceptance testing -> next sprint begins), so
  nothing resumes the loop by itself -- a new window opening does not
  dispatch work, because only a kick starts selection and agents post kicks
  only after a merge.
- **Informational notes are inert by construction.**
  `notify_scrum_ready.yml` dispatches on a bare substring test for the kick
  token with the agents on its actor allowlist, so a park or `PAUSE_SPRINT`
  notice that merely *named* the token dispatched work -- a "park" that was
  really a kick. Such notes now avoid the token entirely and carry
  `<!-- nyxgpt-autopilot-informational -->` (`AUTOPILOT_INFO_MARKER`), which
  the workflow's job `if:` negates. When adding any agent-posted status
  comment, follow the same rule.
- **Human override stays.** A `READY_FOR_NEXT_ISSUE` posted by the owner
  runs unscoped (`notify_scrum_ready.yml`), so the owner can deliberately
  pull work forward across the sprint boundary. Agent-posted kicks cannot.
- The **release wall** survives, but only as the outer boundary: agents
  merge to `RELEASE_BRANCH`, so no candidate outside the current release's
  milestone version is ever eligible, scoped or not.
- The claim-state matrix (#3665) and the escalation/anomaly dispatch pause
  backstops (#3687, #3694) are unchanged by this -- only the sprint filter
  moved.

**Drift caveat (load-bearing):** because the boundary now decides whether
work continues, sprint iteration date windows must be kept current on the
project board. If no iteration's window contains today, there is no active
sprint, and both the autopilot and `scrummaster_next_issue.sh
--sprint-scoped` stop (conservative stop) rather than falling back to
release-wide work -- the selector exits 1. Check this in the daily sprint
report; a stale window shows up as a parked loop, not as cross-sprint work.

**History, so the record is accurate:** the autopilot code previously gated
on the *release* -- work continued while the release had open Backlog issues
in any sprint -- and its comments attributed that to an "owner decision
2026-07-31". The owner has stated that attribution was wrong: the quoted
rationale was out of context and release-gating across sprint boundaries was
never intended. That rationale was agent-authored; sprint-gating is the
owner's standing policy. The observed consequence of the release-gated
window: issues moved out of Sprint 8 by the 2026-08-08 reorg were dispatched
and worked on 2026-08-09/10 with no planning event.

**Process rule (same decision):** a code comment or runbook line claiming
"owner decision" must cite a traceable source -- an issue number or a link
to the owner's comment. An uncited decision claim is agent rationale, not
policy, and may be corrected as such.

## Park semantics and auto-resume (#3709)

Two rules, both enforced on every autopilot kick
(`sprint_autopilot_kick`, `scripts/agents/lib/gh_project.sh`).

### 1. The park decision counts the sprint's whole open population

`sprint_population_snapshot` reads every issue in the active sprint --
open and closed -- bucketed by Status, and `sprint_calc.py
sprint-park-state` classifies it. Only `continue` dispatches; every other
state parks with a note that says exactly which state it is:

| State | Meaning | What the note says |
| --- | --- | --- |
| `continue` | open Backlog work remains | kick, `READY_FOR_NEXT_ISSUE` |
| `work_in_flight` | Backlog empty, issues still open (In Progress / In Review) | "parked, work still in flight" -- **not** completion |
| `awaiting_acceptance` | every item closed, not all accepted | "agentic work complete; awaiting owner acceptance", lists the items |
| `sprint_complete` | every item accepted and in **For Release** | "sprint complete" |
| `empty` | the sprint has no items | "parked", nothing to work or accept |

**Owner definition (2026-08-10, #3709):** *"The sprint isn't done until all
agentic work is complete AND in For Release status."* So `sprint_complete`
is the only state that may call the sprint done, and the next sprint's work
does not begin before it. Promotion to **For Release** stays owner-only (the
acceptance sweep / `promote_accepted_features` tooling) -- **agents never
self-promote items to For Release.**

Before #3709 the decision counted only open *Backlog* issues, so a sprint
with live In Progress / In Review work announced "sprint complete --
acceptance next" while work was demonstrably unfinished.

If the snapshot cannot be read (GraphQL/parse failure), the decision falls
back to the pre-#3709 Backlog-only count and the note claims no completion
state -- a data hiccup must not park a sprint that still has work, nor
declare one done.

### 2. Parked issues resume themselves when their blockers merge

An In Progress issue is **parked** when it has (a) no open PR closing it and
(b) no in-flight developer workflow run. That happens when it refused
earlier behind a `Blocked by: #N` gate, or when its runs died in an
incident. On every kick, before any park, the loop scans the active
sprint's In Progress issues and, for **one** parked issue whose declared
blockers are all closed, posts the same `RETRY_IMPLEMENTATION` trigger a
human would. One resume per kick cycle: each merge opens one gate, and the
next merge kicks again, so a sequenced chain walks itself.

- **Nothing is dropped silently.** Parked issues whose blockers are still
  open appear in a **"waiting on gates"** line in the loud report (park note
  *and* continue kick), along with anything out of budget or in flight.
- **Bounded.** Auto-resumes are counted from `<!-- nyxgpt-autoresume: ... -->`
  markers in the issue's own comment thread and capped at the #3689 retry
  cap. Only a comment from the repo owner resets the count. An issue that
  spends its budget is reported as gate-stuck instead of retried forever.
- **Observable state only.** Open PRs come from the plain pulls list (never
  `gh api search/issues` -- the endpoint whose failure caused the 2026-08-09
  multi-issue incident, #3694); live runs are matched on the runs API's
  `display_title`, which carries the issue title for issue-triggered runs.
  Nothing is cached, so there is no counter to drift.
- **The `Blocked by:` parser is interim.** It reads issue-body references
  only (`scripts/agents/lib/parked_resume.py`) and is unioned with the
  native `blocked_by` dependencies. It is superseded by the native
  Relationships work (W1/W2 in
  `product_management/AGENTIC_SDLC_DESIGN.md`, deferred to nyxAgent); when
  that lands, delete the parser and read dependencies natively.

**Why this exists:** the Sprint 8 cloud chain (#3509 -> #3510 -> #3513 ->
#3514/#3515/#3516) was hand-walked by a human posting
`RETRY_IMPLEMENTATION` at each gate opening. Owner requirement: **no
babysitting -- the loop drives its own chain.**

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
