# Comment tokens are commands, not substrings (#3790, #3882)

## What starts work: assignment, not text

**No comment starts developer work.** Two levers used to, and both are gone:

| Retired token | What replaced it |
|---|---|
| `READY_FOR_NEXT_ISSUE` (the queue kick) | a `repository_dispatch` of type `dispatch-next-issue`, sent by `dispatch_next_issue` in `scripts/agents/lib/gh_project.sh` and subscribed to by `developer_pull_next_issue.yml` |
| `RETRY_IMPLEMENTATION` (start / resume / rework) | **assigning the developer agent to the issue** |

The rule the project follows now is the one people already follow: a reviewer
comments what they found and **assigns the issue back**; the developer, on
picking it up, moves it to In Progress and works it. The comment carries
findings, the assignment carries the instruction, and **the actor doing the
work owns the status transition**. This is the same move #3731 made for issue
relationships — native edges, never body prose (D-002).

Concretely, in `developer_auto_implement.yml`:

* its only trigger is `issues: [assigned]`;
* the claim step moves the issue to In Progress itself. Two lanes are
  claimable — **Backlog** (new work) and **In Review** (rework: a
  REQUEST_CHANGES round, a huddle decision, a conflict round, a human
  override). `Acceptance Failed`, `Acceptance Testing` and `For Release` are
  deliberately held (D-001/D-008) and an assignment does not release them;
* the assigner must be a permitted identity (the owner, the scrummaster,
  review or developer agent, or `claude[bot]` per D-020). Anyone else leaves
  the issue untouched, with the reason in the run log;
* every automatic retry — the bounded auto-retry after a transient failure,
  the autopilot's auto-resume of a parked issue, the review handoff backstop —
  re-dispatches by re-assigning, and posts its record as an ordinary comment.

Why it matters: a comment trigger can only substring-match, so **any comment
that merely named a token started work**, including the agents' own guidance
text. That defect fired twice:

* **#3706** — the sprint-boundary park note said "comment `<the kick token>`
  to continue". Posting a note whose entire point was that work had stopped
  therefore dispatched the next issue.
* **#3790** (2026-08-15) — the developer agent's stop message told the reader
  to comment the retry token to resume. A run stopped at the In-Progress
  check, posted that message, and the message started another run, which
  stopped and posted it again: one cycle every ~20 seconds, ~500 workflow runs
  and ~500 comments across #3782 and #3784 in under two hours.

Two layers of gating were then built to make comments safe to parse. #3882
removed the mechanism instead: an event and an assignment cannot be fired by
prose that mentions them.

## What is still a comment token

Comments remain the right surface for a human **authoring content** or
**stopping** the loop, and those tokens keep both guards below:

| Token | Issued by | Does |
|---|---|---|
| `@acceptance-failure` | owner only | files an acceptance-failure issue (`handle_acceptance_failure.yml`) |
| `@improvement` | owner only | files an improvement issue (`handle_improvement.yml`) |
| `PAUSE_SPRINT` / `RESUME_SPRINT` | owner | holds / releases the sprint autopilot (read by the autopilot calculation) |
| `CONFLICT_REQUIRES_OWNER_DECISION` | developer agent | the one route from a merge conflict to the owner (`conflict_owner_escalation.yml`) |

`@review` on `claude-code-review.yml` is an entry point rather than a state
transition; `claude[bot]` is an allowed author for it (owner decision
2026-08-18, #3870), and that workflow also passes `allowed_bots: "claude"`,
without which `claude-code-action` refuses bot-actored runs outright. Its
exposure is bounded rather than gated: a triggered review is convergent and
one-shot, and its own output posts as the review agent, which was already an
allowed author.

## The rule

**A token is a command only where it opens a line.**

```text
@improvement the settings page needs a save button   <- command
- `PAUSE_SPRINT`                                     <- command (list bullet, decoration)
...file it with `@improvement` when you get a chance <- mention, inert
> @acceptance-failure the install fails              <- quoted, inert
```

Fenced code blocks and quoted (`>`) lines are stripped before matching, so
quoting or documenting an earlier comment cannot replay its commands.

**Any agent comment that must name a token carries a marker**, and a marked
comment can never be a command, wherever the token sits in it:

```html
<!-- nyxgpt-token-mention -->
```

`<!-- nyxgpt-autopilot-informational -->` (`AUTOPILOT_INFO_MARKER`, #3706)
means the same thing and is honoured identically. The marker is the
structural guard: it holds even if the prose later drifts back into naming
the token at line start.

## How it is enforced

Two layers, because layer 1 is all a workflow `if:` can express:

1. **The job `if:`** keeps the cheap tests: the actor allowlist, `contains()`
   for the token, and `!contains(...'nyxgpt-token-mention')`.
2. **A `comment_gate` job** runs the shared composite action
   `.github/actions/comment-token-gate`, which calls
   `scripts/agents/lib/comment_tokens.py` for the anchored decision. The job
   that does the work `needs: [comment_gate]` and runs only on
   `needs.comment_gate.outputs.proceed == 'true'`.

If the gate fails, work does not start — fail-safe is the right direction for
a loop guard.

## The loop guard

`scripts/agents/lib/stop_loop_guard.py` is the backstop for any *other*
self-feeding path, and it now sits on the assignment path, in the developer
workflow's claim step. Every "stopping — this lane is not claimable" comment
carries `<!-- nyxgpt-dev-stop-cycle -->`. When three of them land on one issue
within 30 minutes, the developer agent posts a single escalation carrying
`<!-- nyxgpt-dev-stop-halted -->` instead of a fourth stop notice, and then
says nothing more. While that halt stands:

* agent-driven re-assignments of that issue are ignored, and
* a comment from the repo owner clears it (the same owner-reset convention
  as the retry budget, #3689) — as does the owner assigning the developer
  themselves, since the halt bounds automated spend and is not a lockout.

The halt also expires with the 30-minute window.

## Verifying it

* Unit: `tests/unit/test_comment_tokens.py`,
  `tests/unit/test_stop_loop_guard.py`.
* Structural: `tests/unit/test_comment_token_triggers.py` (the developer
  workflow is asserted to have no comment trigger and to name neither retired
  token; every surviving trigger is asserted to have its gate) and
  `tests/unit/test_dispatch_is_an_event.py` (nothing posts or subscribes to
  either retired token).
* Executed: `.github/workflows/comment-token-gate-smoke.yml` runs the gate
  action on a runner, proving both halves — the old substring rule matches a
  comment that only names a token, the gate refuses it, a genuine command
  still proceeds — and asserts the retired levers are absent from the token
  list and from the developer workflow.

## Adding a new comment token

Before adding one, check that it is not a *lever*: if it would start, resume
or route work, it should be an event or an assignment. That is the whole
lesson of #3706/#3790/#3882.

1. Add it to `COMMAND_TOKENS` in `scripts/agents/lib/comment_tokens.py`.
2. Give its workflow a `comment_gate` job using the shared action, and make
   the work job depend on the verdict.
3. Add it to `TOKEN_TRIGGERS` in `tests/unit/test_comment_token_triggers.py`
   and to the smoke workflow.
4. Never write agent prose that names it without the marker.
