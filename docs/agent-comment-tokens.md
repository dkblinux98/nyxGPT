# Comment tokens are commands, not substrings (#3790)

Several agent workflows are started by a token in an issue comment:

| Token | Started by | Starts |
|---|---|---|
| `RETRY_IMPLEMENTATION` | owner, developer agent, review agent | `developer_auto_implement.yml` |
| `READY_FOR_NEXT_ISSUE` | owner, all three agents, `claude[bot]` | `notify_scrum_ready.yml` (select and start the next issue) |
| `@acceptance-failure` | owner only | `handle_acceptance_failure.yml` |
| `@improvement` | owner only | `handle_improvement.yml` |

`claude[bot]` is the identity every Claude remote session's GitHub writes
carry (the session proxy rewrites all credentials to the Claude GitHub App,
so no PAT can change it). It is an allowed author for the queue kick and for
`@review` on `claude-code-review.yml` (owner decision 2026-08-18, #3870);
`claude-code-review.yml` also passes `allowed_bots: "claude"`, without which
`claude-code-action` refuses bot-actored runs outright. Excluding identities
is not what stopped #3706/#3790, and the two widened triggers are protected
differently:

* **`READY_FOR_NEXT_ISSUE`** keeps its full protection. The anchored-token
  gate and the informational markers below have no author filter, so
  widening the author list cannot weaken them — a `claude[bot]` comment
  that mentions the token, or carries a marker, is as inert as anyone's.
* **`@review`** has no anchored gate; its author list plus a bare
  `contains(body, '@review')` is the whole test. The exposure is bounded
  rather than gated: a triggered review is convergent and one-shot, and its
  own output posts as the review agent, which was already an allowed author
  before #3870 — so no new self-trigger path opens.

A GitHub Actions `if:` expression can only substring-match a comment body —
it has no regex and cannot anchor a match to a line. For a long time that was
the whole test, which means **any comment that merely named a token started
work**, including the agents' own guidance text.

That defect has fired twice:

* **#3706** — the sprint-boundary park note said "comment
  `READY_FOR_NEXT_ISSUE` to continue". Posting a note whose entire point was
  that work had stopped therefore dispatched the next issue.
* **#3790** (2026-08-15) — the developer agent's stop message ended
  "...move the issue back to In Progress and comment `RETRY_IMPLEMENTATION`
  to resume". A run stopped at the In-Progress check, posted that message,
  and the message started another run, which stopped and posted it again:
  one cycle every ~20 seconds, ~500 workflow runs and ~500 comments across
  #3782 and #3784 in under two hours.

## The rule

**A token is a command only where it opens a line.**

```text
RETRY_IMPLEMENTATION                          <- command (this starts a run)
- `RETRY_IMPLEMENTATION`                      <- command (list bullet, decoration)
@improvement the settings page needs a save button   <- command
...and comment `RETRY_IMPLEMENTATION` to resume      <- mention, inert
> RETRY_IMPLEMENTATION                        <- quoted, inert
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

Best is to not name the token at all — the developer agent's stop message
points at `agents/runbooks/developer-runbook.md` instead of spelling out the
command, because that message is posted automatically and often.

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
self-feeding path. Every "stopping — issue is no longer In Progress" comment
carries `<!-- nyxgpt-dev-stop-cycle -->`. When three of them land on one
issue within 30 minutes, the developer agent posts a single escalation
carrying `<!-- nyxgpt-dev-stop-halted -->` instead of a fourth stop notice,
and then says nothing more. While that halt stands:

* agent-authored retry triggers on that issue are ignored, and
* a comment from the repo owner clears it (the same owner-reset convention
  as the retry budget, #3689).

The halt also expires with the 30-minute window.

## Verifying it

* Unit: `tests/unit/test_comment_tokens.py`,
  `tests/unit/test_stop_loop_guard.py`.
* Structural: `tests/unit/test_comment_token_triggers.py` — the stop message
  is asserted token-free, and every trigger is asserted to have its gate.
* Executed: `.github/workflows/comment-token-gate-smoke.yml` runs the gate
  action on a runner over the incident's real comment bodies, proving both
  halves — the old substring rule matches the looping message, the gate
  refuses it, and a genuine command still proceeds.

## Adding a new comment token

1. Add it to `COMMAND_TOKENS` in `scripts/agents/lib/comment_tokens.py`.
2. Give its workflow a `comment_gate` job using the shared action, and make
   the work job depend on the verdict.
3. Add it to `TOKEN_TRIGGERS` in `tests/unit/test_comment_token_triggers.py`
   and to the smoke workflow.
4. Never write agent prose that names it without the marker.
