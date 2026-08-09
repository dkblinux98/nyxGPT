# How this project is run

nyxGPT is built by an agent-staffed engineering organization managed by a
human owner with an SRE/Release Management background. See
[What nyxGPT actually is](../README.md#what-nyxgpt-actually-is) in the
README for why that's the point of this project, not incidental to it.
This page is the mechanics.

## Agent roles and workflow

Four roles carry the work, each restricted to a fixed set of allowed
actions — see [AGENTS.md](../AGENTS.md) for the authoritative permission
matrix; nothing not explicitly allowed there may be done by an agent.

- **scrummaster-agent** — selects the next backlog issue (lowest Phase,
  then lowest issue number) and hands it to the developer agent. Charter:
  [agents/charters/scrummaster-agent.md](../agents/charters/scrummaster-agent.md) ·
  runbook: [agents/runbooks/scrummaster-runbook.md](../agents/runbooks/scrummaster-runbook.md).
- **developer-agent** — implements the issue, writes/updates tests, and
  opens a PR. Charter:
  [agents/charters/developer-agent.md](../agents/charters/developer-agent.md) ·
  runbook: [agents/runbooks/developer-runbook.md](../agents/runbooks/developer-runbook.md).
- **review-agent** — reviews the PR against the issue's acceptance
  criteria and code quality, decides APPROVE or REQUEST_CHANGES, and owns
  the merge on approval. Charter:
  [agents/charters/review-agent.md](../agents/charters/review-agent.md) ·
  runbook: [agents/runbooks/review-runbook.md](../agents/runbooks/review-runbook.md).
- **stakeholder-agent** — an optional notifier role that surfaces when
  human stakeholder approval is required; it does not modify repo state
  directly. Charter:
  [agents/charters/stakeholder-agent.md](../agents/charters/stakeholder-agent.md).

A fifth role, the **human owner**, closes releases, advances phases, and
is the only actor who can move an issue to "For Release." After three
rejected review cycles on a PR, the issue escalates to the human owner
directly (see the review runbook, §6).

Each role's prompt (what the agent is told when it runs) lives under
[agents/prompts/](../agents/prompts/), alongside its charter and runbook.

## The project board status flow

Every issue moves through a fixed set of statuses on the GitHub project
board:

```
Backlog -> In Progress -> In Review -> Acceptance Testing -> For Release -> Closed
```

- **Backlog** — approved, unscheduled.
- **In Progress** — the developer agent is actively implementing it.
- **In Review** — a PR is open; the review agent (or, after 3 rejected
  cycles, the human owner) is deciding. After merge, the issue *stays* "In
  Review" until acceptance testing formally begins — this prevents
  unmerged work from being tested by mistake.
- **Acceptance Testing** — merged, and assigned to the human owner to
  verify the acceptance criteria against the real, running system.
- **For Release** — accepted; only the human owner sets this.
- **Closed** — released.

An issue can also be "parked" mid-flow — merged but held at "In Review"
instead of advancing to Acceptance Testing — if its acceptance criteria
depend on other, still-open issues; it promotes automatically once every
blocker clears. Full semantics are in
[AGENTS.md](../AGENTS.md#project-status-semantics) and
[agents/runbooks/review-runbook.md](../agents/runbooks/review-runbook.md) §9.

## Decision records

Architecture and product choices with lasting consequences are captured as
standalone decision records under
[`product_management/`](../product_management/) (`DECISION_*.md`) — for
example
[DECISION_AWS_COMPUTE_SUBSTRATE.md](../product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md)
and
[DECISION_PRIVATE_ACCESS_MECHANISM.md](../product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md).
Each records the problem, the constraints inherited from prior decisions,
the options considered, and the owner's approval, so a later reader can
see why a choice was made without reconstructing it from commit history.

## Definition of Done, and the acceptance-failure / improvement taxonomy

The
[Definition of Done](../CLAUDE.md#definition-of-done-owner-requirement-2026-07-08)
requires every user-facing feature to be reachable end-to-end: a backend
change with no web UI surface, or an ops feature with no admin-dashboard
surface, is an incomplete implementation, and the review agent is required
to block on it as a Medium finding.

When human acceptance testing (after merge) turns something up, it is
classified into one of two buckets, because they mean different things
about where the process broke:

- **Acceptance Failure** — the implementation does not do what the
  accepted issue specified. Filed as a new, related issue that blocks the
  original feature from reaching "For Release" until the failure is fixed
  and itself accepted.
- **Improvement** — the implementation does exactly what was specified,
  but the specification itself was incomplete or wrong. This is charged to
  requirements, not implementation — a product management failure, not an
  acceptance failure. Filed as a normal Backlog issue; it does not gate
  the original feature.

Full mechanics — the related-issue model, the blocking dependency, and the
sweep that automatically promotes a feature to "For Release" once every
related failure clears — are in
[agents/runbooks/review-runbook.md](../agents/runbooks/review-runbook.md) §9.

## The retrospective

A regularly-refreshed retrospective dashboard tracks review-cycle
outcomes, acceptance-failure/improvement rates, and time-to-merge across
the project's history — the evidence behind the claim that gate activity
in this project is real and routine, not theoretical. It's built from the
same issue/PR history visible in this repository and refreshed via
[scripts/retrospective/REFRESH_RUNBOOK.md](../scripts/retrospective/REFRESH_RUNBOOK.md).

**As part of the v3.0.0 release, the retrospective is made publicly
reachable** (the share action itself is owner-side, performed at release
time):
[nyxGPT Project Retrospective](https://claude.ai/code/artifact/2b850289-fbb2-4e55-abf7-ea55d4501701).
Until that release step happens, this link is only reachable from the
owner's own Claude session.
