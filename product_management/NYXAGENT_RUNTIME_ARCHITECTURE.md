# nyxAgent Runtime Architecture — off Actions, onto persistent sessions

**Created:** 2026-08-20 · **Author:** executive-assistant session, at owner request
**Status:** Proposal for owner decision. Nothing filed or implemented.
**Owner framing (2026-08-20):** *"my goal is to get away from github actions
workflow. a lot of the debt we now have is caused by creating workflows to get
around claude permission limitations."*

That framing is the design brief, and it is correct. This document says what
the current architecture actually is, why it grew 87 workflows, what replaces
each class of them, and how to get there without a big bang.

---

## 1. Why there are 87 workflows

Almost none of them exist because the delivery loop is complicated. They exist
because **a GitHub Actions job is a short-lived process with one identity and
one permission scope**, and every act of agency had to be expressed as: a
trigger that starts a process + a token that authorizes it + a gate that stops
the wrong caller. Sort the workflow suite by what it is working around:

| Class | Examples | What it exists to work around | Count (approx.) |
|---|---|---|---|
| **Triggers** — how does an agent get started? | `developer_auto_implement.yml`, `developer_pull_next_issue.yml`, `review_agent_auto_review.yml`, `assign_backlog.yml` | A process cannot wait; it must be woken by a GitHub event. Assignment (#3882), `repository_dispatch`, and comment tokens are all *ignition switches*, not domain concepts. | ~12 |
| **Identity gates** — is this caller allowed? | `comment-token-gate`, actor gates, `SCRUMMASTER_AGENT_TOKEN` overrides in `huddle_session.yml`, `allowed_bots` | Each job runs as one PAT. To let a different role act, you must start a different job with a different token, then prove the caller was legitimate. | ~10 + gates inside others |
| **Hand-offs** — how does work move between steps? | `review_ensure_handoff.yml`, `developer_ensure_pr_exists.yml`, `huddle_decision_dispatch.yml`, drain/promote sweeps | State cannot live in a process, so it lives on the board, and every transition needs a workflow to notice it and a backstop for when the notice is missed. | ~15 |
| **Watchers / sweeps** | `sweep_pr_status.yml`, `sweep_parked_blocked_issues.yml`, `usage_limit_retry.yml`, `promote_accepted_features.yml`, `acceptance_drain_gate.yml` | No process is alive to notice anything, so cron polls the board. §9's "watching must be intelligent" cannot be satisfied by a thing that is only awake for 40 seconds. | ~8 |
| **Real CI** — tests, smokes, scanning, release artifacts | `ci-tests.yml`, `*-smoke.yml`, `security-scan.yml`, `release-artifacts.yml` | **Nothing.** This is what Actions is genuinely for. | ~40 |

Roughly **45 of the 87 are ignition, identity, hand-off and polling** — pure
consequence of the substrate. In a long-lived process holding a credential,
with roles as in-process sessions, that entire column does not exist. It is
not refactored, it is deleted.

The remaining ~40 are CI and stay exactly where they are.

---

## 2. The inversion

**Today:** GitHub is the scheduler, the permission broker, and the memory.
The agent is a function GitHub calls, from zero, 961 times.

**Target:** a long-lived **nyxAgent daemon** is the scheduler and holds the
credentials. GitHub is a *tracker* and an *artifact store*, reached through
one adapter. CI is an *executor* the daemon calls, not a thing that calls the
daemon.

```
        ┌──────────────────────── nyxAgent daemon (long-lived) ─────────────────────┐
        │                                                                            │
 events │  ┌───────────┐   ┌─────────────────┐   ┌──────────────┐   ┌─────────────┐  │
 ──────►│  │ event bus │──►│ session router  │──►│ agent session│──►│ tool layer  │  │
 GitHub │  └───────────┘   └─────────────────┘   │  (per item)  │   │ + guards    │  │
 webhook│        ▲                                └──────────────┘   └──────┬──────┘  │
 + poll │        │ heartbeat (watcher)                    │                 │         │
        │  ┌─────┴─────┐                            session store           │         │
        │  │ scheduler │                            (durable, on disk)      │         │
        │  └───────────┘                                                    │         │
        └───────────────────────────────────────────────────────────────────┼─────────┘
                                                                            │
                    ┌───────────────────────┬───────────────────────────────┼──────────┐
                    ▼                       ▼                               ▼          ▼
              tracker adapter         human channel                    executor    git worktrees
             (GitHub Projects)      (Slack / voice)              (Actions, or runners)
```

---

## 3. The session model — this is the whole point

**One durable session per work item, not per invocation.**

```
Session(issue=3825)
  created  ─► active ─► suspended ─► active ─► … ─► closed
  carries: conversation transcript (durable)
           a git worktree (durable)
           role, tool allowlist, budget
           links: issue, PR, branch
```

- Selection opens it. Implementation, review feedback, CI failures, the
  acceptance fix — all arrive **into the same session**. It is suspended
  between events (flushed to disk), not destroyed.
- **Review feedback lands in the session that wrote the code.** There is no
  hand-off document, because there is no hand-off. The 81-turn `review-fix`
  round that re-derives everything becomes a ~10-turn continuation.
- Resume is a **cache-warm continuation**, not a re-read. This is the token
  fix: `cache_read` is 94% of your spend precisely because every round
  rebuilds a ~137k-token prefix from scratch. A resumed session pays for the
  delta.
- **Concurrency stays as it is** — WIP 2, file-overlap check against the other
  session's worktree. That logic is good and it becomes ten lines in-process
  instead of a script plus a workflow.

Three memory tiers, kept separate — the current system collapsed the first two,
which is why `LEDGER.md` is 90KB and still doesn't stop re-derivation:

| Tier | Holds | Lifetime | Read how |
|---|---|---|---|
| **Session memory** | this item's transcript, what was tried, what the reviewer said | one work item | *resumed*, never re-read as a document |
| **Project memory** | decisions, parked items, open questions (today's ledger, minus the noise) | project | read at session open, small |
| **Verified facts** | invariants | forever | the guard/test that enforces them |

---

## 4. The permission model — the part that deletes the debt

Replace *trigger-level gates* with **a tool allowlist per session plus a small
set of invariant guards at the write boundary.**

```python
ROLE_TOOLS = {
  "scrummaster": {"tracker.read", "tracker.write_status", "tracker.sprint", "chat.post"},
  "developer":   {"repo.*", "exec.run", "tracker.read", "pr.open", "pr.push", "chat.post"},
  "reviewer":    {"repo.read", "pr.read", "pr.review", "pr.merge", "tracker.read", "chat.post"},
}
```

Every write passes one guard module. Candidate invariant list — six, not sixty:

1. Never push to `master`/`main`; merges target the active release branch only.
2. Never merge a PR whose required checks are not green (this is P1 of the
   overhaul proposal, and here it is one `if`, not a workflow).
3. Never merge without verifying the content actually landed on the release
   branch (today's `review_accept_and_merge.sh` rule, #3862).
4. Never move an issue out of a deliberately held lane (`Acceptance Failed`).
5. Never rewrite history on a branch the daemon did not create — no rebase, no
   force-push.
6. Never act on a work item that already has an active session (the
   duplicate-trigger race, guarded today by `is_primary_marker_comment`, is
   gone by construction: sessions are keyed by item).

That is "floors, never ceilings" made literal: guards say what may never
happen, the session's judgment picks what does. And the script count falls,
which is the health metric §3.2 of the separation plan already names.

**Credentials.** Today: PATs in GitHub secrets, one per role, plus the token
gates that exist to stop the wrong workflow using the wrong one. In the daemon
the credential is held once, and roles are separated in the tool layer instead.
If you run on Managed Agents, vault `environment_variable` credentials are
stronger than what you have now — secrets are substituted at egress and never
visible in the sandbox at all.

---

## 5. Choosing the substrate

Three real options. They are not mutually exclusive, and the honest answer is
that the delivery loop and the human channel want different ones.

### (a) Claude Agent SDK, self-hosted — *recommended for the delivery loop*

Claude Code packaged as a library: the agent loop, built-in file/bash/search
tools, context management, hooks, subagents, permissions, sessions. You host
it. **This is the same harness `claude-code-action` already runs inside your
workflows** — which makes the migration a lift rather than a rewrite: the
prompts, the charters, the runbooks all still apply. The difference is that
you own the process, so it can stay alive between events.

Docs: `code.claude.com/docs/en/agent-sdk`. Confirm its session persistence and
resume semantics there before building — I have not verified those specifics
this session, and the whole design leans on them.

### (b) Managed Agents (Anthropic-hosted, beta) — worth a serious look

Anthropic runs the loop *and* hosts a per-session container. What it gives you
out of the box maps unusually well onto this backlog:

- **Persisted, versioned agent configs**, defined as YAML and applied with the
  `ant` CLI — your charters become agent definitions under version control.
- **Sessions with their own workspace** and an SSE event stream.
- **Scheduled deployments** — cron-fired sessions with per-firing run records.
  That is the §9 intelligent watcher without a single cron workflow.
- **Session budgets** — hard, platform-enforced, dollar-denominated caps per
  session. Note the tension with P-002 honestly: the owner rejected a *global*
  fixed cap as a scripted guard. A per-session budget is a floor under one
  unit of work, not a threshold constant deciding policy — but it is the
  owner's call whether that distinction holds.
- **Vault credentials**, memory stores, webhooks, and multiagent rosters (an
  agent delegating to other agents — the scrummaster/dev/reviewer shape).

The catch: the hosted sandbox cannot run *your* verification. nyxGPT's smokes
need macOS runners, `kind`, Docker, Homebrew. So even here, execution goes out
to real machines.

### (c) OpenClaw — for the human channel, not the delivery loop

Gateway process, channel routing across Slack/Signal/WhatsApp/iMessage, a
heartbeat, an AgentSkill system. It is a personal-assistant runtime, not a
coding harness. It is the strongest answer available to §4 of the separation
plan (the voice-huddle interaction model) and to §8's open question about
which surface carries the huddle — and it is the wrong tool for running a
review loop. Two published security analyses exist; read them before it holds
a token with `workflow` scope and merge rights.

**Recommendation:** (a) or (b) for the loop, (c) or plain Slack for the human
channel. Decide (a) vs (b) with the spike in §7, not on paper.

---

## 6. What stays in GitHub Actions

Everything that is genuinely CI, and nothing else:

- `ci-tests.yml`, the `*-smoke.yml` family, `security-scan.yml`,
  `terraform-*-validate`, `validate-web-routes.yml`, `release-artifacts.yml`.
- These are *called* by the daemon (`workflow_dispatch` + wait for conclusion)
  or fire on push as they do now. The daemon reads their results; it is not
  started by them.

The executor question is separate from the brain question, and worth keeping
separate: you can move the loop off Actions this quarter and still run every
test on Actions runners, which are good at exactly that.

---

## 7. Migration — strangler, four phases, each independently valuable

**Phase 0 — the observer (days, zero risk).**
Stand the daemon up read-only. It watches the board and PRs, holds no write
credential, and produces one thing: the owner's briefing in Slack — board
state, what is red, what is waiting. Proves the event loop, the tracker
adapter, and the human channel. Deletes nothing yet.

**Phase 1 — move the reviewer (the highest-value single move).**
The reviewer is the right first role: **287 rounds, 29% of all tokens, 2.3
invocations per PR**, read-mostly, and its only writes are a review and a
merge — so the blast radius is small and the guards are three of the six.
Implementation still runs in Actions. Deletes:
`review_agent_auto_review.yml`, `review_ensure_handoff.yml`, the review half
of the comment-token gates.

**Phase 2 — move implementation onto durable sessions (the token collapse).**
Selection opens a session; review feedback resumes it. This is where the
189-turn implement round and the 81-turn review-fix round stop being separate
onboardings. Deletes: `developer_auto_implement.yml`,
`developer_pull_next_issue.yml`, `developer_ensure_pr_exists.yml`,
`assign_backlog.yml`, and the assignment-as-lever mechanism (#3882) —
assignment goes back to being a *record* of who is working, not the ignition.

**Phase 3 — the watcher and the sweeps.**
Heartbeat replaces cron: drain gate, promotion sweep, PR-lane sweep,
usage-limit retry, conflict notification. One scheduled session that reads
broad state and asks "does anything here look wrong?", with the scripted
tripwires kept as floors. Deletes the sweep workflows.

**End state: ~87 workflows → ~40, all of them CI.**

Each phase is reversible: the workflow it replaces is disabled, not deleted,
until the phase has run a full sprint.

---

## 8. What this does and does not fix

| Problem | Fixed by this? |
|---|---|
| New-hire agent syndrome | **Yes, at the root.** Durable sessions per work item; feedback returns to the session that did the work. |
| Token burn | **Largely.** Cost is rounds × turns × context; resume removes the rebuild. Turn count still needs prompt work — a 189-turn round is also a prompting problem, and the runtime will not fix that by itself. |
| Workflow / permission debt | **Yes.** ~45 workflows exist only as ignition, identity and hand-off plumbing. |
| Tests not catching acceptance failures | **No.** That is P4 in `PROCESS_OVERHAUL_PROPOSAL.md` — the first-run rehearsal on a clean target. It is needed whatever the runtime is, and no substrate choice defers it. |

---

## 9. Risks, stated plainly

- **Availability.** A long-lived process can die. Session state must be durable
  on disk and resume on restart from day one, or you have swapped a stateless
  system for a lossy one.
- **Credentials on a box.** GitHub secrets are a managed store; a daemon
  holding a PAT is not. Managed Agents' vaults are better than both; a
  self-hosted daemon needs a real keyring and a threat model.
- **Audit trail.** Actions run logs are today's forensic record — the churn
  and spend dumps are built from them. The daemon must emit its own structured
  run log from the start, or you lose the retrospective the moment you migrate.
- **Two systems during migration.** Phases 1–2 run a hybrid. The tracker is the
  shared truth; do not let the daemon and a workflow both own the same
  transition.

## 10. What I have not verified

- Claude Agent SDK session persistence and resume specifics — read
  `code.claude.com/docs/en/agent-sdk` before committing to (a). The design
  depends on it.
- Whether Managed Agents' hosted sandbox can reach a private GitHub org and
  dispatch workflows under your network policy.
- OpenClaw's code, license, and the findings of the two security analyses.
- Whether any of nyxGPT's smoke matrix can run outside Actions runners — I
  assume not, and designed the executor split around that assumption.
