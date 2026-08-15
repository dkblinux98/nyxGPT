# Agentic SDLC v2 — Process Design

**Created:** 2026-08-08
**Status:** Owner-ratified design (workshop of 2026-08-08). This document is
stage 1 of the ratified build sequence; the remaining stages are specced as
issue-ready work items in [§8](#8-implementation-plan-issue-ready).
**Supersedes:** the number-ordered push dispatch described implicitly by
`notify_scrum_ready.yml` / `scrummaster_next_issue.sh` (mechanism retained,
decision-making replaced — see §6).

---

## 1. Problem

The agent loop today *mimics* an SDLC team but carries none of its
intelligence (owner's framing, 2026-08-08):

1. **The scrummaster does nothing a scrummaster does.** It relays the lowest
   open issue number. No grooming, no sprint planning, no reasoning.
2. **Priority is an afterthought and effort is underthought.** Every issue is
   filed P1/XS by default and nothing downstream reads either field.
3. **Issues are pushed by number, not pulled by reasoning.** The dev agent has
   no say in what it works next, no memory of its own in-flight work, and no
   awareness of conflicts it is about to create.
4. **Issue relationships guide nothing.** The owner's explicit convention is
   the **native Relationships project field** (2026-08-02 decision, reaffirmed
   2026-08-08 — *not* sub-issues), but `create_issue.sh --blocks` posts prose
   comments that nothing traverses.
5. **Sprint scope has no statistical basis.** Sprints are labels, not
   commitments derived from velocity, failure-arrival rate, or effort
   calibration; retrospectives exist as an owner dashboard but feed nothing
   back into planning.

## 2. Target operating model (roles and authority)

Ratified authority delta (2026-08-08 — "authority delta is fine"):

| Role | Authority / responsibility |
|---|---|
| **Owner / PM** | Creates **milestones** and **releases**; assigns milestones to releases; communicates fuzzy scope to the scrummaster via a **release draft document**. Declares priority and Relationships at issue creation. Final acceptance. May veto a sprint plan before dispatch. Intervention otherwise rare. |
| **Scrummaster agent** | **Creates and time-boxes sprints** (2-week default), assigns sprints to milestones following the release draft. **Grooms**: selects and orders sprint scope with a statistics-based justification, writes the sprint plan doc (§4), runs mid-sprint regrooms when acceptance failures displace features, and runs the **retrospective** (§7). |
| **Developer agent** | **Pulls** its next issue (§6) instead of being pushed one: sprint order, filtered by Relationships eligibility, WIP limit, and file-overlap check. Implements, submits for review. Unchanged: `developer_submit_for_review.sh`, verification gates. |
| **Review agent** | **Unchanged.** Same review loop, same 3-strike escalation, same merge-on-APPROVE. |
| **Owner as QA** | Acceptance testing when work reaches the acceptance column; acceptance failures are filed per the existing convention (label "Acceptance Failure", `Related feature: #N`). |

The delegation of **sprint creation** to the scrummaster is an explicit,
owner-granted exception to the standing rule "do not create project metadata
without permission" (CLAUDE.md Tooling) — scoped to sprint iterations only.
Labels, milestones, releases, and other field options remain owner-only.

## 3. Planning hierarchy

```
Release (owner)  ──▶  Milestone(s) (owner)  ──▶  Sprint(s) (scrummaster)  ──▶  Issues
        ▲ release draft doc conveys fuzzy scope to the scrummaster
```

- The owner creates milestones and releases and maps milestones to releases.
  The **release draft document** is the owner→scrummaster channel for intent
  and scope; the release process itself is unchanged.
- The scrummaster creates sprints, sets their time windows, assigns them to
  milestones following the release draft, and populates them via grooming.
- Grooming cadence is **fluid**: incoming acceptance failures may push feature
  issues into later sprints at any time; each displacement is recorded in the
  sprint plan doc (§4) and analyzed in the retrospective (§7).

## 4. The sprint plan doc (grooming artifact)

Location (owner decision 2026-08-08):

```
product_management/sprint_planning/sprint_<N>/PLAN.md
```

Written by the scrummaster **when the sprint is groomed, before dispatch
begins** — it is the owner's veto surface. Required contents:

1. **Time window** (start/end dates) and the milestone the sprint serves.
2. **Selected issues in intended pull order**, each with:
   - priority, effort, and the **expected-files list** (seeded from the
     issue body's "Files affected"; required — it drives the overlap check
     in §6);
   - Relationships summary (what it blocks / is blocked by).
3. **Statistical justification of scope**: velocity of recent sprints
   (effort-points completed), the **failure-arrival reserve** (capacity held
   back for acceptance failures, from the observed arrival rate), and effort
   calibration notes (estimate vs. actual from the retro data).
4. **Deliberate deferrals** — what was considered and left out, and why.
5. **Regroom log** — mid-sprint changes update this doc in place (append,
   don't rewrite) so drift from the original plan stays visible.

The retrospective appends an **actual-vs-planned** section to the same
sprint folder at sprint close (§7).

## 5. Issue lifecycle and assignment state machine

Consolidates the owner's 2026-08-08 decision matrix (the #3665 guard
reconciliation) — this section is the reference the guard implements:

| Backlog issue state | Meaning | Loop behavior |
|---|---|---|
| Assigned to **scrummaster** | Normal claimable state (stamped by `assign_backlog.yml` on open) | Claimable; work it when groomed into the active sprint. |
| **Unassigned** | Equivalent to scrummaster's | Stamp the scrummaster assignment (keeps history correct), then proceed as claimable. |
| Assigned to **owner** | Owner is holding it | Skip; never reassign; move to the next issue. |
| Assigned to **anyone else** | Anomaly | Inspect the assignment history for hiccups and resolve; do not silently reassign. |

Dispatch ordering (bootstrap lesson from #3665): status transitions **before**
assignment — set Status = In Progress first, then assign the dev agent, because
`developer_auto_implement.yml` verifies In Progress on wake and exits
otherwise. The pull conversion (§6) keeps this mechanism; only the decision of
*which* issue moves is relocated.

## 6. Pull model, WIP, and conflict avoidance

**Decision moves; mechanism stays.** The `issues: assigned` trigger and the
In-Progress verification in `developer_auto_implement.yml` are unchanged. What
changes is *who decides* the next issue: a pull step run in the dev agent's
context replaces the scrummaster's lowest-number push.

Pull algorithm (per free WIP slot):

1. Candidates = active-sprint issues in **plan-doc order**, in claimable state
   (§5), with Status Backlog.
2. Filter by **Relationships eligibility**: an issue blocked by any unmerged
   issue is ineligible.
3. **WIP limit 2** (owner decision 2026-08-08): at most two issues In
   Progress/In Review for the dev agent at once. A fresh agent session has no
   memory, so in-flight WIP is *read from the board and open PRs*, never from
   session memory.
4. **File-overlap check** (one-against-one, since WIP=2): compare the
   candidate's expected-files list (plan doc) against the in-flight issue's
   actual footprint (its open PR's diff; before a PR exists, its plan-doc
   expected-files). Disjoint → pull. Overlapping → take the next eligible
   candidate instead.
5. Pull = set Status In Progress, then assign the dev agent (§5 ordering).

**No rebases (owner decision 2026-08-08: "merge, don't rebase").** Hard rule:

- History is append-only. No `git rebase`, no force-push, no history rewriting
  on shared branches.
- A stale feature branch is freshened by **forward merge**
  (`git merge origin/<RELEASE_BRANCH>`) — an ordinary merge commit whose
  conflict resolution is visible to the review agent.
- **No branch stacking.** Dependent work is *sequenced* (step 2 above), never
  stacked; stacking is only ergonomic with rebase, which is banned.
- PR merge method is out of scope for this rule — the review agent merges
  exactly as it does today.

Scheduling (steps 2 and 4) is the entire conflict-mitigation strategy; merge
conflicts should be rare by construction rather than resolved after the fact.

## 7. Statistics substrate and retrospectives

- **Single substrate: repo-committed JSON** under
  `scripts/retrospective/data/`, produced by dispatchable workflows that
  commit their output (pattern: `retro_project_fields_dump.yml`). The groomer
  and the retro dashboard read the **same files**; no parallel pipeline.
- **#3667** (filed 2026-08-08, owner decision: workflow-dump approach) removes
  the Gmail dependency by deriving review-round detail from GitHub PR reviews
  via a new dump workflow. Once merged, every groomer input is reachable from
  Actions.
- The **retro dashboard artifact already exists** and is **updated, not
  replaced** (owner, 2026-08-08): it grows per-sprint velocity in
  effort-points and failure-arrival rate, which the groomer's scope
  justification (§4.3) consumes.
- **Retrospective** (scrummaster, at sprint close): append actual-vs-planned
  to `product_management/sprint_planning/sprint_<N>/` — completed vs. planned
  scope, failures that arrived and what they displaced, effort estimate vs.
  actual — and refresh the data files the next grooming pass will read.

## 8. Implementation plan (issue-ready)

**Timing (owner decision, 2026-08-09): W1–W6 are NOT filed during the v3.0.0
cycle.** This is post-v3 work and folds into the **nyxAgent** effort (together
with the §9 agent-dashboard/intelligent-watcher consequences). The breakdown
below stays issue-ready so filing is mechanical when the owner gives the go —
the owner creates the milestone and chooses the target release at that point.

Stage 1 (this document) is complete when this file merges. Stages 2–4 below
are written to file directly with `scripts/agents/create_issue.sh`: each item
gives the title (house format), suggested Module / Priority / Effort, the
dependency edges to record in the **native Relationships field**, and the
acceptance-criteria skeleton for the issue body. House defaults apply
(Status Backlog, Label "Feature").

**Milestone/sprint mapping:** the owner creates the milestone for this work
(suggested name: *Agentic SDLC v2*) and maps it to a release via the release
draft; the scrummaster slots the issues into sprints. Suggested slotting:
Sprint A = stage 2 (graph + merge hygiene), Sprint B = stage 3 (groomer),
Sprint C = stage 4 (pull conversion + docs). Until stage 3 lands, the first
sprint plan docs are written by hand as part of W4's acceptance.

**Dependency graph** (record as Relationships, `blocked by`):

```
#3667 (review-round dump)  ──▶ W4 (groomer)
W1 (native relationships)  ──▶ W2 (eligibility lib) ──▶ W4 ──▶ W5 (pull conversion)
W3 (forward-merge freshen)  [independent]
W5 ──▶ W6 (charters/runbooks/docs)
```

### Stage 2 — graph and merge hygiene

- **W1 — `feat: create_issue.sh --blocks writes native Relationships - api`**
  (Module: api · P1 · Effort: S · blocked by: none)
  Replace the prose-comment implementation of `--blocks` with the native
  Relationships project field. AC: `--blocks N` produces a visible
  Relationships panel edge; no relationship comments are posted; field
  verified by re-query after creation (house rule); existing prose
  "Related to #N" bodies are unaffected (no backfill in this issue).

- **W2 — `feat: Relationships eligibility library for agent scripts - api`**
  (Module: api · P1 · Effort: S · blocked by: W1)
  A shared helper in `scripts/agents/lib/` answering: for issue N, is it
  blocked by any open/unmerged issue, and what does it block. AC: callable
  from bash workflows; returns eligibility + blocker list; unit-tested
  against a fixture project; no Project field writes.

- **W3 — `feat: Forward-merge freshening for stale agent branches - api`**
  (Module: api · P2 · Effort: XS · blocked by: none)
  When a dev branch is behind the release branch at submit/review-fix time,
  freshen via `git merge origin/$RELEASE_BRANCH`. AC: no `git rebase` or
  force-push anywhere in agent scripts/workflows (grep-verified); merge
  commit appears in PR history; review agent sees conflict resolutions.

### Stage 3 — the groomer

- **W4 — `feat: Scrummaster grooming - sprint plan doc + statistics - api`**
  (Module: api · P1 · Effort: M · blocked by: W2, #3667)
  The scrummaster generates `product_management/sprint_planning/sprint_<N>/PLAN.md`
  per §4: ordered scope with per-issue expected-files, Relationships summary,
  velocity + failure-reserve justification from `scripts/retrospective/data/`,
  deferrals, regroom log. Includes sprint creation/time-boxing (§2 delegation)
  and the retrospective actual-vs-planned append (§7). AC: plan doc committed
  before any dispatch for that sprint; owner veto window respected (plan lands
  as a PR or a committed doc the owner can reject); regroom updates append.

### Stage 4 — pull conversion

- **W5 — `feat: Dev agent pull replaces number-ordered push - api`**
  (Module: api · P1 · Effort: M · blocked by: W4)
  Implement §6: plan-doc order → eligibility filter → WIP-2 check (board/PR
  state, not session memory) → one-against-one file-overlap check → Status
  In Progress then assign. Replaces the lowest-number selection in
  `scrummaster_next_issue.sh` / `notify_scrum_ready.yml`. AC: §5 assignment
  matrix honored (scrummaster-assigned claimable, unassigned stamped, owner
  skipped, anomalies inspected); dispatch ordering status-before-assign;
  overlap conflict yields next candidate, never a parallel pull.

- **W6 — `docs: Update charters, runbooks, AGENTS.md for pull-model SDLC - documentation`**
  (Module: documentation · P2 · Effort: S · blocked by: W5)
  Bring `agents/charters/*`, `agents/runbooks/*`, `AGENTS.md`, and CLAUDE.md's
  process sections in line with §§2–7 so the operating docs describe the
  system that actually runs. AC: no doc still describes number-ordered push;
  sprint-plan-doc and pull algorithm documented where agents read them.

### Already in flight (prerequisites, not re-filed)

- **#3665** — start-guard reconciliation with the §5 assignment matrix
  (in review as of this writing).
- **#3666** — project hygiene becomes fill-if-missing, opened-only.
- **#3667** — review-round data from GitHub via dump workflow (unblocks W4's
  statistics inputs).

## 9. Watching must be intelligent, not scripted (owner principle, 2026-08-09)

### 9a. Deciding must be intelligent too (owner principle, 2026-08-13)

The 2026-08-13 rc-cut standstill extends the watching principle one level
deeper — from *observing* the system to *deciding* within it:

After the last agentic merge of the day, the board reached a state no rule
anticipated: Backlog empty, nothing in flight — but one drain-gated failure
frozen in `Acceptance Failed`, untouchable by design. The autopilot's park
logic asked a boolean ("any open sprint issues?"), answered "not complete,"
and refused to cut a candidate — while the owner sat blocked on a second
machine needing the already-merged fix published to continue the very round
the held issue belonged to. Every rule behaved as written; their
composition produced "wait" at the moment the owner needed "ship." A human
supervisor resolved it in seconds by weighing context no boolean holds:
what merged, what is held and why, and what the owner is actually doing.

**Principle:** at genuine decision points — cut a candidate or hold; select
an issue or park; escalate or continue — the decision function is an agent
invocation with the owner's cadence and intent as its charter, not a
condition expression. Scripts remain as guardrails defining what may never
happen (no stable publish without ceremony sign-off; no touching held
issues; no Support-labeled selection); inside those rails, judgment picks
the action and writes down its reasoning, the way the park comment already
writes down its boolean. The huddle mediation (#3687) is the existing proof
of the pattern: a scripted trigger hands a contested decision to an agent
that reads the whole thread and rules.

Floors, never ceilings — same as watching.

**The rc-cut decision, codified (owner, 2026-08-15 — implementation banked
for Sprint 9 grooming; the pipeline mechanics stay as-is this sprint):**

- A candidate is cut when **the sprint's product backlog — nyxGPT issues,
  not agent-process issues — has fully cleared into Acceptance Testing**
  (nothing product in Backlog / In Progress / In Review; held-lane
  semantics per the drain gate unchanged).
- **Agent-process issues are invisible to the cut decision in both
  directions**: their merges never trigger a candidate (rc10/rc11 were cut
  on process-batch merges with no product change since rc9 — wasted
  versions under this rule), and their in-flight state never blocks one.
- The decision is an **agent invocation** reading the board with the
  product/process distinction, one cut per clearance, reasoning written
  down; the pipeline's scripted guardrails (serialized cuts, tip-unchanged
  no-op, channel guards) remain the floors beneath it.
- **No human hand-dispatches candidates** — the 2026-08-14 rc7 burn was a
  manual dispatch racing the autopilot; the assistant no longer cuts, and a
  missed cut is a defect to file, not a gap to fill by hand. An explicit
  owner order is the only exception.


Recorded from the 2026-08-09 incident post-mortem, **binding on the future
nyxAgent agent-dashboard design**:

> Process-based scripted watching doesn't work. You have to think of every
> possible vector of failure and script for that — it's untenable. A human
> watcher would have caught this quickly.

The incident that proves it: a runner-image change flipped `gh api`'s HTTP
method, a step every dev run passes through began failing deterministically,
and the self-heal pipeline — plus this owner-session's own scripted monitors —
burned ~50 redundant Claude invocations across five issues over ten hours.
Every safety mechanism in the loop was *correctness*-shaped (retry,
re-diagnose, keep trying); none was *budget*-shaped (stop — this is costing
money faster than it is producing value). The scripted watchers missed it
because they watched pre-enumerated signals (specific branch names, specific
status transitions), and the failure arrived on a vector nobody had enumerated
— which is the only kind of failure that matters. Meanwhile the system's own
FATAL self-diagnosis, containing the complete correct remedy, sat unread in an
issue thread for eight hours because escalations had no route to a human.

Design consequences:

1. **The watcher is an agent, not a script.** The nyxAgent dashboard's
   monitoring layer must put an intelligent context in front of *raw, broad
   state* (run history, failure counts, spend counters, board state) at a
   regular cadence and ask "does anything here look wrong?" — anomaly
   judgment, not pattern matching. Scripted checks remain as cheap tripwires,
   but they are the floor, never the ceiling.
2. **Cross-issue anomaly rule**: the same step failing on *different* issues
   in a short window is one infrastructure event, not N coding problems —
   one diagnosis, global pause, never N parallel loops.
3. **Escalations reach a human channel** (push/email/dashboard alert), never
   only a thread comment.
4. **Spend telemetry is first-class sprint data**: Claude-invocation counts
   and runner minutes per issue land in the retro data substrate (§7), so
   every issue has a price and retrospectives surface cost regressions.

**Deferred cost item — intelligent test selection (owner, 2026-08-10, to be
filed post-v3.0.0):** CI gates and the developer workflow's verification loop
run the full Python *and* Node test suites regardless of a change's footprint.
Observed on #3709's PR: a bash/workflow/runbook-only change ran the entire
pytest and vitest suites — and the dev workflow's verification phase repeats
the full suites on up to three fix attempts for *every* issue, making this the
largest recurring runner-spend multiplier in the pipeline. Test runs should be
chosen intelligently from the change's path/dependency impact: script- or
workflow-only diffs run `bash -n`/shellcheck/YAML lint plus any directly
related tests only; Python-only diffs skip the Node suite and scope pytest;
web-only diffs skip Python. Applies to `ci-tests.yml` and the verification
steps in `developer_auto_implement.yml`. Owner scheduling (2026-08-10): this
is part of the **next sprint (Sprint 9), which is nyxAgent-focused** — file it
there at grooming.

A global hard budget circuit breaker (fixed caps on expensive invocations per
unit time) was proposed and **rejected by the owner (2026-08-09)**: a
threshold constant is itself a scripted guard of the kind this section rules
out — the intelligent watcher reading the spend telemetry (item 4) provides
the same protection with judgment instead of a constant. Items 2–4 are
owner-endorsed direction; filing them as issues awaits explicit owner
go-ahead.

### 9b. Memory must be durable, not reconstructive (owner principle, 2026-08-14)

Judgment (§9a) is only as good as the state it reasons over, and the state an
agent session holds is *reconstructed*: re-derived each session from artifacts
and lossy self-summaries. When the artifacts are incomplete the reconstruction
still completes — confidently — and nothing distinguishes it from something
actually checked. Three incidents in the week of 2026-08-14 share that root:
the `Acceptance Failed` lane sweep that reclassified the owner's deliberately
parked failure markers as stale board state, stale cross-session claims about
wheel and tap state, and re-derivation of already-settled facts.

**Principle:** the system of record carries the memory, not the agent.
`agents/LEDGER.md` (#3774) records decisions made, facts verified with method
and timestamp, items deliberately parked with their revisit conditions, and
questions left open. Sessions read it at start, append as things happen, and
treat their own recollection as untrusted input wherever the ledger or the live
system can answer instead. A claim that is not in the ledger and not freshly
verified is not asserted as fact.

This bounds churn cost the same way §9's watcher bounds spend: it converts an
unbounded invisible expense — re-derive everything every session, sometimes
wrongly — into a bounded visible one, reading a single dense file and
re-verifying only what is both stale and load-bearing.

## 10. Non-goals

- No sub-issues — relationships live in the native Relationships field only.
- No rebase tooling of any kind; no force-push allowances.
- No change to the review agent's loop, the release process, or the
  `nyxgpt`-wrapper and repo-less-portability requirements (unaffected).
- No live GitHub API calls from `build_dashboard.py` (data comes from
  committed dumps, per #3667's owner decision).
