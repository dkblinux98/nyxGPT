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
fire), and a `pull_request`-triggered job that the file never classified (a
real gate the review never waits for). Adding a smoke workflow therefore forces
the decision instead of defaulting to "not a gate", which is the direction that
fails quietly.

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
