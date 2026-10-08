# What an escalation tells the owner (#4176)

An escalation is the pipeline handing one issue to the human. Its first line is
the whole product: the owner reads the headline, decides whether this is theirs
to fix, and either acts or puts it down. A headline that says nothing costs a
log-reading session per escalation.

Developer-agent escalations routinely said nothing. The headline was

> **Error type could not be determined. Manual investigation needed.**

and the owner's summary of it was "the usual unhelpful reason". The worked
example is #4166 ([run 37709148793][run]), where the same comment's own
blast-radius section said, three lines below the headline:

> **Is `v3.0.1` red?** **yes** — `test` failing. Everything built on this head
> is affected.

The pipeline had found the cause and printed the sentence for not having found
it. The cause was simple: `v3.0.1` itself was red, #4166 inherited the failure,
and its Final Verification failed three times on tests unrelated to its work.

[run]: https://github.com/dkblinux98/nyxGPT/actions/runs/37709148793

## Three failures, each sufficient on its own

1. **The gate recorded no reason.** `Final Verification (Must Pass)` was an
   inline `run:` block that failed and said nothing. Phase 1 harvests the text
   it classifies from GitHub's per-job logs API *mid-run*, which returns nothing
   while the job is still running, so the harvest fell back to the failed step's
   **name** — the entire "error log" was the string
   `Final Verification (Must Pass)`. No signature in `classify_error` matches
   that, so the class was `unknown`.
2. **Phase 3, which exists to diagnose `unknown`s, never reached the model.**
   `claude-code-action` died in its own branch setup with
   `fatal: 'v3.0.1' is already used by worktree at '/tmp/base-wt'` — a worktree
   the agent had created during attempt 2 to check whether the failures already
   existed on the base (the right question) and never removed. `claude_result`
   is gated on `claude_analysis.conclusion == 'success'`, so a Phase 3 that
   **crashed** was reported exactly like one that concluded nothing.
3. **The headline was a lookup, not a composition.** `escalate_fatal` computed
   `phase3Diagnosis || phase2Diagnosis || errorExplanations[errorClass]`. With
   both diagnoses empty, `unknown` maps to the generic sentence. It never read
   the failure detail it appends below itself, never read the base-red finding,
   and never said that Phase 3 had failed to run.

Fixing any one of them leaves the other two producing the same headline, which
is why all three are fixed here.

## How the headline is composed now

`scripts/agents/lib/escalation_evidence.py` is pure over an evidence dict, so
every combination is unit-tested (`tests/unit/test_escalation_headline.py`).
The parts, in the order the owner reads them:

| Evidence | What the headline says |
|---|---|
| Phase 3 (or Phase 2) diagnosis | that diagnosis, first |
| the base is red | `Inherited failure: <branch>@<sha> is ALREADY RED — <check> is failing on the base itself` |
| Final Verification's own reason | `failed at the <gate> gate: N failing test(s) — <node ids>` |
| `claude_analysis` failed | `Phase 3 diagnosis did not run: the deep-analysis step itself failed — <its first error line>` |
| none of the above | the generic sentence, which is then true |

A red base and a list of failing tests are not competing answers — they are
"whose fault" and "what broke" — so the parts **compose**. The generic sentence
appears only when the run genuinely holds nothing, and the "Compose the
escalation headline" step logs a warning when it does, so the next instance of
this defect is visible from the run itself.

### A red base is one cause, not one per issue

CLAUDE.md: *one systemic cause gets ONE escalation.* When the base is red the
cause key becomes **`base-red:<branch>:<failing checks>`**, so every issue that
inherits that base links to the first escalation instead of raising its own —
the collapse `escalate_to_owner` already applies to a cause key. Without a red
base the key stays `developer-failure:<step>`.

The key deliberately ignores the head sha: "`test` is failing on `v3.0.1`" is
the same fault after the next commit lands on a branch that is still red.

### Where the base-red finding comes from

`release_head_state_json` (`scripts/agents/lib/gh_project.sh`) — **one**
definition of "is the base red, on which check, at which sha", called both by
the escalation headline and by `blast_radius_report`'s first question. That is
the point: the #4166 comment disagreed with itself because two code paths
answered the same question separately.

`release_head_red` is **absent** when the head's checks could not be read.
"Not checked" is a third answer and is printed as one; a head nobody could ask
about is never reported as green, and never called red.

## Final Verification records its own reason

`scripts/agents/run_final_verification.sh` runs the gates and, when one fails,
writes through `write_agent_error_detail` (#3971):

```
Final Verification failed: gate=pytest
7 failing test(s):
FAILED tests/unit/test_release_candidate.py::test_formula_version
...
```

`classify_error` reads that first line and answers
**`verification_failed:<gate>`**, ahead of the older `FAILED.*test` signature —
which would otherwise send the 3-attempt fix loop round again on a gate that
runs *after* attempt 3 has been spent.

The node IDs come from pytest's `-rf` **short summary**, the one part of its
output whose shape is a contract. The gate is a script rather than an inline
block so that the recording can be *executed* in CI instead of inspected.

## Stray worktrees

`scripts/agents/prune_stray_worktrees.sh` removes every worktree except the
workspace and prunes the administrative records, and it runs before **every**
`claude-code-action` invocation in `developer_auto_implement.yml` — not just
Phase 3's. It never fails the job: failing there would block the step it exists
to unblock.

The fix prompts also now tell the agent to compare against the base with

```bash
git worktree add --detach /tmp/base-check origin/<release branch>
```

because `--detach` holds no branch name and therefore cannot block a later
checkout. Prompt guidance is not a guard, though — the next trap will be set by
something no prompt anticipated, which is why the prune runs regardless.

## Executed evidence

`.github/workflows/escalation-headline-smoke.yml` reproduces the #4166 step
sequence on a runner, with fault injection in both directions (#3753):

- the gate is run against a planted failing test **and** against a clean tree;
- the classifier is run over the recorded detail **and** over the bare step
  name, which must still answer `unknown`;
- the branch checkout is attempted **before** the prune (and must fail with the
  #4166 `fatal:`) and again after it;
- the headline is composed with the evidence **and** with none, and the generic
  sentence must appear in exactly the second case.

The job prints the resulting headline into the run summary. What it cannot do
is make a required check genuinely fail on the release branch, so the GitHub
answer to "is the base red" is served by a stubbed `gh` while
`release_head_state_json` runs for real over it.
