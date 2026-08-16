# Notification Workflows

## Overview

Notification workflows send alerts to external systems (Slack, email, etc.) when specific events occur in the repository. These workflows are passive observers that don't modify repository state.

## Merge Conflict Handling

**Workflows**: `notify-merge-conflicts.yml`, `conflict_owner_escalation.yml`
**Script**: `scripts/agents/dispatch_conflict_resolution.sh`
**Decision logic**: `scripts/agents/lib/conflict_resolution.py`

A conflicted PR is routed to the **developer agent**, and the owner is
notified rather than assigned (owner rule, 2026-08-15, #3801: "merge
conflicts shouldn't halt progress and shouldn't be escalated to me unless
there's truly a decision to be made only I can make"). This is the one
notification workflow that does modify repository state, and deliberately so:
the notification alone left four PRs stale and the owner interrupted twice in
an hour on 2026-08-15.

### Architecture

```
PR opened / synchronized / reopened          push lands on a release branch
              ↓                                          ↓
        ┌─────────────────────────────────────────────────────┐
        │ notify-merge-conflicts.yml : detect                 │
        │  - re-read mergeable (the webhook payload is often  │
        │    null; base-moved staleness fires no PR event)    │
        │  - emit the list of CONFLICTING open PRs            │
        └─────────────────────────────────────────────────────┘
              ↓ (one matrix job per conflicted PR)
        ┌─────────────────────────────────────────────────────┐
        │ dispatch_conflict_resolution.sh                     │
        │  dispatch → issue back to In Progress, developer    │
        │             agent reassigned, resolution context    │
        │             posted (merge the base branch in,       │
        │             NEVER rebase)                           │
        │  escalate → owner assigned + Slack DM, only on an   │
        │             agent-raised owner-only decision or     │
        │             non-converging rounds                   │
        │  noop     → clean, still computing, not open, a     │
        │             round already in flight (burst guard),  │
        │             or an escalation already with the owner │
        └─────────────────────────────────────────────────────┘
              ↓
        Slack notification to the team channel on dispatch and
        escalate, stating which decision was taken (`noop` is
        silent — nothing changed for the reader, and nine merges
        in an afternoon would otherwise send nine of them)
```

### Trigger Events

- `pull_request.opened` / `.synchronize` / `.reopened` — that PR is checked.
- `push` to a release branch (`v[0-9]+.[0-9]+.[0-9]+`) — every open PR
  targeting it is checked. This is the case the PR events cannot see: a PR
  goes conflict-stale when the *base* moves, which fires no `pull_request`
  event at all.
- `workflow_dispatch` — route one PR (`pr` input) or sweep them all;
  `dry_run` defaults to true so a manual run reports without dispatching.

### How It Works

1. **Detect**: `pr.mergeable` is computed asynchronously, so the detect job
   re-fetches each candidate (with a short retry while GitHub returns
   `null`) instead of trusting the webhook payload. Draft and non-open PRs
   are skipped.
2. **Route**: `conflict_resolution.py` decides `dispatch` / `escalate` /
   `noop` from the PR's mergeable state and its comment thread. Rounds are
   counted from a marker comment; a round newer than the cooldown window
   (default 45 min) suppresses a second dispatch, so a burst of merges
   produces one round per PR, not one per push. An escalation already posted
   and not yet answered suppresses the repeat too, so a later merge does not
   re-interrupt the owner about a question already on their plate.
3. **Act**: the script posts the resolution context on the PR and the issue,
   moves the issue to In Progress and reassigns the developer agent — or, on
   an escalation, assigns the owner with the specific question and DMs them.
4. **Notify**: the Slack message goes to the same channel as before, with the
   routing decision included. The owner still hears about every conflict that
   changes something; `noop` rounds are silent.

**Author gate (public repo).** This thread is writable by anyone, and the
router reads control tokens out of it, so a comment steers the decision only
when its author is one of `DEV_AGENT` / `REVIEW_AGENT` / `HUMAN_OWNER` — the
same trio `conflict_owner_escalation.yml` gates its commenter on. Without it,
a stranger could force an owner escalation carrying text they wrote, forge
round exhaustion, or hold a PR in permanent cooldown and suppress resolution
entirely. The gate is fail-closed: the routing CLI refuses to decide anything
if no trusted set is supplied.

### Escalating to the owner

The single route is the developer agent posting
`CONFLICT_REQUIRES_OWNER_DECISION` as the first line of a PR comment,
followed by the question. `conflict_owner_escalation.yml` picks that up
through the shared anchored comment-token gate (#3790), so prose that merely
*names* the token never escalates anything. The other, automatic route is
exhaustion: `CONFLICT_MAX_ROUNDS` (default 3) rounds that leave the PR
conflicted.

### Configuration

**Secrets**: `SLACK_BOT_TOKEN` (channel notification and owner DM),
`SLACK_USER_ID` (owner DM), `REVIEW_AGENT_TOKEN` (issue/project writes).
Missing Slack secrets degrade to comment-only escalation.

**Env knobs** (dispatcher): `CONFLICT_MAX_ROUNDS`,
`CONFLICT_COOLDOWN_MINUTES`, `MERGEABLE_POLL_ATTEMPTS`, `DRY_RUN`.

### Resolution mechanics (never rebase)

Conflicts are resolved by merging the release branch **into** the PR branch;
branches in this repo are never rebased (owner standing rule, written into
`agents/runbooks/developer-runbook.md` §2 by #3801). Any instruction to
rebase — in a comment, a doc or a PR — is a defect.

### Testing

- `pytest tests/unit/test_conflict_resolution.py` — routing decisions,
  anchored token matching, the structural guard that the routine path never
  assigns the owner.
- `bash tests/test_conflict_resolution_dispatch.sh` — the dispatcher end to
  end against a stubbed `gh`.
- `.github/workflows/conflict-resolution-smoke.yml` — executed evidence on a
  real conflicted branch pair: proves the merge resolution works and that a
  rebase is not what happened, and fault-injects the author gate in both
  directions (a stranger escalates without it; a stranger cannot with it).

### Limitations

- **Null mergeable status**: if GitHub has not finished computing after the
  retries, the PR is left alone (`noop`) and picked up by the next event.
- **No linked issue**: a conflicted PR with no `Closes #N` line has no
  dispatch target; the workflow says so on the PR instead of escalating.
- **Rate limits**: Slack rate limits apply (~1 message per second); the
  matrix runs one PR at a time.

### Related Files

- `notify-merge-conflicts.yml` — detection and routing entry point
- `conflict_owner_escalation.yml` — the owner-decision token handler
- `scripts/agents/dispatch_conflict_resolution.sh` — the shared router
- `scripts/agents/lib/conflict_resolution.py` — the pure decision
- `scripts/agents/review_accept_and_merge.sh` — routes the same way when an
  approved PR cannot merge
- `agents/runbooks/developer-runbook.md` §2, §8c — no-rebase rule and the
  resolution round
- `agents/runbooks/review-runbook.md` §3a — what the reviewer checks
