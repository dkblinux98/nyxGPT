# The reviewable-head gate

**A red head is the developer's problem, not the reviewer's. A pending head is
nobody's problem yet — it is waited on, never rejected.** (#3971)

This is an agent-process document: it describes how this repository reviews its
own pull requests, not how nyxGPT works. It is deliberately not packaged into
the product artifact (ledger **D-019**).

## Why it exists

Measured over 2026-08-13..19, from `scripts/retrospective/data/`:

| | |
|---|---|
| Blocking (Critical + Medium) review findings in the window | 240 |
| …that reported machine-observable check state | ~36% (87) |
| Rejected work items carrying at least one such finding | 39 of 65 |
| Cost of one rejection | ~7.2M tokens (review) + ~10.7M (review-fix) |

"CI is red on this head", "17 CI checks pending", "the web coverage gate
fails", "`k8s-artifact-smoke` fails deterministically" — every one of those is
displayed on the PR page before anyone reads it, and every one of them cost a
full reject/re-fix round-trip to relay. The review agent's *real* findings are
good; this gate exists so a third of its attention stops going to CI relay.

## The three answers

`required_check_state <sha>` (`scripts/agents/lib/gh_project.sh`) reads the
check runs GitHub reports for a commit, keeps only the ones the required set
names, and answers:

| state | meaning | what happens |
|---|---|---|
| `failed` | a required check on the head concluded failure | `developer_submit_for_review.sh` refuses to submit (exit 3, before any GitHub write). A head that turns red *after* submission is handed back to the developer by assignment — no review invocation, no REQUEST_CHANGES round, no review cycle counted. |
| `pending` | a required check is present and unconcluded | The review trigger **waits** (`await_required_checks`). Nothing is posted, nothing is rejected. |
| `absent` | no required check is attached to the head yet | A bounded grace (default 5 min) covering the seconds between a push and GitHub creating the checks; then treated as clear. |
| `clear` | every required check present on the head passed | The review runs. |
| `unknown` | the list or the API could not be read | **Fails open**: everything proceeds exactly as it did before this gate existed. Jamming every submission and every review on a GitHub blip, to prevent a rare wasted review, is the wrong trade. |

`failed` wins over `pending`: one concluded failure decides the head without
waiting for the rest.

### The `failed` row was switched off for six weeks (#4192)

Between 2026-08-26 and 2026-10-10 the review workflow's `head-gate` job decided
`proceed` on `failed` under a temporary owner rule, passing the failing check
names into the review prompt as an observation instead of refusing. The rule
was scoped "REVERT AFTER #4034"; #4034 merged on 2026-09-30 and nothing
reverted it, because the one suite that measured the decision
(`tests/test_reviewable_head_gate.sh`) had been changed to assert the temporary
answer. Red heads merged for ten days, ending with PR #4191 landing three red
required checks on `v3.0.1`.

Two things changed so a pause cannot go quiet again: the suite pins `failed` as
the decision **and** as reachable from the `head-not-reviewable` job that acts
on it, and the submission side's refusal classifies deterministically (the
overload signature used to match a bare `529` in a head SHA — see
`classify_error`). The lesson is the general one: **a gate switched off needs a
test that goes red while it is off**, or the revert depends on someone
remembering.

### The refusal continues the round, and that takes a file

Exiting 3 with a clear sentence on stderr does not, by itself, deliver "the
developer round continues rather than handing off". `developer_auto_implement.yml`'s
Phase 1 classifies whatever text it managed to harvest, and mid-run its two
sources both lose the reason: GitHub's per-job logs API returns nothing while
the job is still running, and the fallback is the failed step's *name*. So
`classify_error` is handed `"Submit PR for review"`, matches no signature,
answers `unknown` — and `unknown` means non-retriable, which means the owner is
DM'd about a failure the pipeline knew how to retry.

This gate's own first refusal is the proof: on run `32419181728` it fired
exactly as designed against this branch's red `gate-is-delegated`, and the
round still ended in a FATAL owner notification.

So the refusal writes its reason to the **agent error-detail file** on the
runner (`write_agent_error_detail`, default
`$RUNNER_TEMP/nyxgpt-agent-error.txt`), and Phase 1 reads it
(`read_agent_error_detail`) *before* trying either indirect source. The
signature `red head is not reviewable` then classifies `retriable:ci_red` and
the round retries itself.

The write is best-effort: a script that is already failing must not fail
differently because a temp directory was not writable, and the log harvest
stays as the fallback. **A new agent script that knows why it failed should
record it the same way** — this is the difference between a signature in
`classify_error` that works and one that only reads as if it does.

### Classifying it was only half (#4179)

Phase 1 answering `retriable:ci_red` is not the decision. **Phase 2 used to
classify the failure again**, with a `case` that had no `ci_red` arm and a
default that wrote `STATUS=FATAL`, from a harvest (`gh run view <id> --log`)
that cannot answer while the run is in progress. So the refusal was classified
correctly and escalated anyway: on #4138
([run 37722699004](https://github.com/dkblinux98/nyxGPT/actions/runs/37722699004))
the owner was woken with *"Error type: retriable:ci_red … Diagnosis:
Unrecognized error type."* for a one-line bug in that branch's own smoke
script.

Now `scripts/agents/lib/error_classes.py` is the single table of what each
class does, Phase 2 acts on the class Phase 1 already derived, and a Phase 2
that finds nothing **defers** instead of overruling it. See
`docs/escalation-evidence.md` → *The error-class table*.

### What the continued round is told

A refusal leaves no PR behind, so `developer_ensure_pr_exists.sh` opens a
rescue draft and the next round takes the Review Fix path.

When a later round's verification passes, that draft stops being a rescue —
and so does its **record**. `scripts/agents/lib/rescue_pr.py` is the one place
that makes the change: the deliberately non-closing `Refs #N` becomes
`Closes #N`, the `wip:` title prefix goes, and the body's "⚠️ Rescue PR …
**This is not a submission for review**" preamble is replaced with a short
note that the branch reached review the long way. Only the first of those
three used to happen, so a PR under review asserted it was not a submission
while being reviewed as one, and its merge commit carried `wip:` (review of
PR #4191). It runs on every hand-off rather than only on a draft — a PR
promoted by an earlier round is exactly the one left carrying the stale
record — and is a no-op on a body that has already been promoted.

The brief it is
handed used to say only "finish the work on that branch" — which is how a
round re-submitted into the same red head and spent the retry budget on a
cause nobody had named.

The refusal therefore records **which** checks failed and **where to read
them**, as `red-head-check: <name> <url>` lines in the error-detail file
(`red_head_check_lines`, read back by
`escalation_evidence.parse_red_head_detail`), and the auto-retry comment names
them. The brief itself is derived **live from the PR's own head** rather than
relayed through that comment: GitHub's answer is the unforgeable one, and a
check that has since gone green must not send the round chasing it.

The round is bounded by the same unforgeable retry budget as any other retry
(3 per `(issue, failed step)` since the last owner comment, `retry_budget.py`).
When it runs out, the escalation is keyed `head-red:<checks>` — the failing
check, not the step that noticed it.

## The required set is a named list

`.github/required-checks.txt`, in two sections.

**Why not "every check attached to the head":** two of them belong to the
review itself (`claude-review`, `head-gate`), so gating on all of them
deadlocks the gate against its own run — the deadlock **Q-005** records for the
merge path. The rest of the not-required half is project bookkeeping (adding a
card to the board, stamping a lane, detecting a conflict): a flake in one of
those must not be able to wedge the pipeline, and none of them judges the code.

**Absent is not pending.** Most jobs in the list are path-filtered, so on any
given PR most of them never run. A required check that is not attached to the
head is not waited for and not counted against it — the path filter already
decided it does not apply. That is what makes the list safe to extend: naming a
check costs nothing on the PRs where it does not run.

**Keeping it honest.** `tests/unit/test_required_checks.py` fails in both
directions: a name that matches no job in any workflow (a gate that can never
fire), and a `pull_request`- or `pull_request_target`-triggered job that the
file never classified (a real gate the review never waits for). Adding a smoke
workflow therefore forces the decision instead of defaulting to "not a gate",
which is the direction that fails quietly.

`pull_request_target` is in that invariant as of #4167, and it carries a
consequence for the classification itself: such a run is associated with the
**base** commit, so its check run never attaches to the PR head this gate asks
about. A `pull_request_target` job therefore cannot be `[required]` — it would
hold every head at `absent`/`pending` forever. Both of today's
(`pr-hygiene`, `stamp-closed-lane`) are `[not-required]` on their own merits as
project bookkeeping; this is the second, independent reason.

Names are **check-run names**, which are the job's `name:` when it has one and
its job id otherwise — hence entries like `Install the working tree's formulas`
(macos-brew-smoke.yml's `keg-install`) beside `k8s-artifact-smoke`.

## Waiting, and why it is a wait rather than an event

The `head-gate` job polls until the required checks conclude, up to
`vars.REVIEW_CI_WAIT_MINUTES` (default 60).

The event-driven alternative — re-trigger the review on
`check_suite: completed` — is not available here. GitHub runs the **default
branch's** copy of a workflow for events not attached to a pull request, and
this project's default branch is release-ceremony-only (**D-003**), so such a
trigger would not exist until the next release ceremony and would run a stale
definition forever after. It is the same trap `review-runbook.md` §5a records
for `gh workflow run` without `--ref`. An idle ubuntu runner costs cents; a
review invocation spent saying "17 checks pending", plus the round-trip it
starts, costs ~18M tokens.

If the wait expires, the gate says so once on the PR, notifies the owner, and
stands down — a required check that has not concluded in an hour means CI is
stuck, not that this PR is wrong. `@review` restarts it.

## The developer override

For the legitimate case — the failure reproduces on the base branch without
this change:

```bash
scripts/agents/developer_submit_for_review.sh \
  --ci-override "security-scan fails identically on v3.0.0, run <URL>" <ISSUE>
```

The submission proceeds and the reason is written into the PR body under
`<!-- nyxgpt-ci-override -->`. The review workflow reads that marker and hands
the reason to the review agent **as a claim to verify** — never as an accepted
exception. The reviewer checks it (the same check on the base branch, the
check's own log), reports what it checked, and treats a reason that does not
hold as a Medium (blocking) finding. An override accepted silently is a process
violation, so state the reason with something checkable in it.

## What this did not change

- **Merge-on-APPROVE, the finding severities and the 3-strike escalation** are
  untouched. A hand-back for a red head is not a REQUEST_CHANGES round: it
  posts no verdict and increments no counter.
- **The owner-carried bypass path** is untouched. `@approve-merge` and the
  other owner comment triggers run in `review_agent_auto_review.yml`, which
  this change does not touch, so a merge that deliberately skips agent review
  still works.
- **The merge path's own check gating** stays parked under **Q-005**. This gate
  guards the *review trigger*, where the reviewer's own in-flight check runs
  are excludable by name; the merge path's version of that question is a
  separate design decision on the pipeline's core merge path.

## Where the pieces are

| Piece | File |
|---|---|
| The named required set | `.github/required-checks.txt` |
| The read and the wait | `scripts/agents/lib/gh_project.sh` (`required_check_names`, `head_check_runs`, `required_check_state`, `await_required_checks`) |
| Submit-time refusal and the override | `scripts/agents/developer_submit_for_review.sh` |
| The gate and the review it guards | `.github/workflows/claude-code-review.yml` (`head-gate`, `head-not-reviewable`) |
| The hand-back / escalation | `scripts/agents/review_head_gate_action.sh` |
| Executed evidence | `tests/test_reviewable_head_gate.sh`, `.github/workflows/reviewable-head-smoke.yml` |
| List maintenance guard | `tests/unit/test_required_checks.py` |
