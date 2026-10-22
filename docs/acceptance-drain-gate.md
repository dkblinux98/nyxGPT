# Acceptance drain gate and the automated release ceremony

Owner decisions of 2026-08-12 (#3730), implemented across the agent loop.

Two things changed:

1. Acceptance failures and improvements filed **during** an acceptance round
   are held until the round drains, instead of being worked immediately.
2. The owner moving the **release tracking issue** to `For Release` is now the
   release sign-off; one `gh workflow run release_ceremony.yml` then starts
   the release ceremony, which runs unattended (no schedule since 2026-10-05).

---

## 1. The drain gate

### Why

Before this, `@acceptance-failure` put the failure straight into `In Progress`
and dispatched the developer agent. Fixes therefore merged back into
`Acceptance Testing` while the owner was still testing the round — flooding the
lane with fresh work mid-round and burning RC cycles on candidates nobody had
finished testing.

The rhythm the loop now respects:

> test the whole round → let the agents drain the failures → test the next
> candidate

### The lanes

| Status lane | Meaning |
|---|---|
| `Acceptance Testing` | items the owner is testing this round |
| `Acceptance Failed` | failures/improvements found this round, **held** — *and* features the owner has tested and failed, **parked** (see below) |
| `Backlog` | released work, normal scrummaster selection |

### `Acceptance Failed` holds two different things (owner decision 2026-08-14, #3780)

The owner also parks **features they have tested and failed** in that lane —
"so that I don't get lost as to what I've tested that has failed" — and since
owner standard **D-042** (2026-08-22) a **reopened original** sits there too.
The machinery tells the populations apart with `acceptance_role`
(`scripts/agents/lib/drain_gate.py`), from a rework label plus the native
relationship edges:

| In `Acceptance Failed` | Role | What it is | What the machinery does |
|---|---|---|---|
| this round's rework | **`rework`** | carries `Acceptance Failure`/`Improvement` **and** natively blocks another issue — a failure/improvement the handlers filed against something, or a fix that failed re-test | **held** — released to `Backlog` when the gate opens |
| a reopened original | **`original`** | is natively **blocked by** other issues: it did not pass acceptance and its work lives in the issues blocking it | **left alone** — the only move is promotion to `For Release` once its whole blocked-by closure is accepted, at which point it is reassigned to the owner and closed |
| a standalone failure | **`work`** | neither — D-042 (b), filed with no relationship because it spans issues or fits none | **held** — drains with the batch and is worked like a brand-new issue |
| a parked feature | any, **CLOSED** | already implemented and merged, then failed by the owner; waiting on its blockers | **left alone** — promotion only, exactly as above |

**Why not issue state, which is what this read until 2026-08-22.** #3780 took
"open in that lane" to mean held rework. D-042 makes an owner's reopen mean the
*opposite* — "this issue did not pass acceptance, see its last comment" — so
the gate was dispatching developers against issues with nothing to implement:
on 2026-08-22 it released #3835 (blocked by #3984/#3985/#3989) and #3829
(blocked by #3991) into `Backlog`, and both had to be moved back by hand.
State still answers the one narrow question it is good for — "did the owner
park an already-merged feature here?" — and nothing else.

**Why neither signal alone.** The label alone misreads #3829, which carries
`Acceptance Failure` and is an original, and #3835, which carries neither
rework label and is one too. A blocking edge alone misreads a plain feature
that blocks a sequenced successor. Both signals are records the process writes
deliberately, so this is not inference from issue shape.

Two rules follow, and both are enforced rather than documented:

- `promote_accepted_features.sh` treats a feature parked in `Acceptance Failed`
  **identically to one parked in `Acceptance Testing`** — a promotion candidate
  whose transitive blocked-by closure gates it. It applies the same
  `acceptance_role` rule as the gate, so an item the gate refuses to release
  is exactly an item this sweep will promote: a `rework` or `work` item is
  never promoted (its own fix has not shipped), an `original` is.
- **Nothing moves a feature out of `Acceptance Failed` while any blocker is
  open.** The placement is owner signal: agents read it, they do not rearrange
  it. Only the all-blockers-accepted promotion moves it, and the drain gate
  reports it (`parked` in the gate state, and a line in the release log) rather
  than releasing it.

Motivating incident: #3508, #3509 and #3596 sat parked with no cascade even
though their failure issues had been fixed, and the placements were then
mistaken for stale board state and swept — recorded in `agents/LEDGER.md`
(D-001 note, 2026-08-14).

### What "accepted" means

One module answers it: `scripts/agents/lib/acceptance_status.py` (#4204,
ledger D-066). Two questions, named rather than ordered, because the board's
lanes are not a total order:

| Question | True for | Who asks |
|---|---|---|
| **`is_accepted`** — the owner has accepted this issue | `For Release`, `Done` | `promote_accepted_features.sh` (the blocker gate), the drain gate's sprint scope, `sprint_calc.py`'s park state, the sprint report's `done` count |
| **`has_reached_acceptance`** — merged and at least in the acceptance queue | `Acceptance Testing`, `For Release`, `Done` | `sweep_parked_blocked_issues()` (the parked-blocked completeness test, "Acceptance Testing or beyond") |

`Done` is accepted because it is the lane **this loop writes itself**: the
release ceremony's Phase 3 close-out sets every closed issue in the released
milestone to it. Until #4204 each of those four callers carried its own
hard-coded list and `Done` was on none of them, so an issue that was accepted
and then *released* read as "not accepted yet" — permanently, since nothing
moves an issue back out of `Done`. Anything parked behind such a blocker was
unpromotable forever.

`Acceptance Failed` is in **neither** list, although it sits "beyond"
`Acceptance Testing` in board order. Work is still owed on both populations
that park there — held rework whose fix has not shipped, and an original the
owner reopened — so reading it as a completed blocker would promote parked
work onto a blocker the owner rejected. That is why the two predicates are
named, not derived from a lane ordering.

The shell asks through `status_is_accepted` /
`status_has_reached_acceptance` in `lib/gh_project.sh`, which read the
vocabulary once per run. The lane names are config keys with literal
defaults (`STATUS_FOR_RELEASE`, `STATUS_DONE`, `STATUS_ACCEPTANCE_TESTING`),
so a rename stays a config change. `tests/unit/test_acceptance_status.py`
fails the build if any script starts comparing a Status value to decide
acceptance on its own, or writes `Done` as a bare literal — the single
source is enforced, not merely documented.

### The rule

- `@acceptance-failure` (`handle_acceptance_failure.yml`) and `@improvement`
  (`handle_improvement.yml`) **file into `Acceptance Failed`**. They are never
  assigned, kicked or auto-resumed while they sit there — and
  `classify_backlog_claim_state` refuses to start anything in that lane, so
  the hold does not depend on the selector's Backlog filter alone.
- The gate **opens when `Acceptance Testing` is empty except its exempt
  items**:
  - the **release tracking issue** — it stays in that lane until the whole
    release is accepted;
  - any **feature awaiting rework** — a feature the owner has already failed
    parks closed in `Acceptance Testing` (or in `Acceptance Failed`, #3780)
    until everything blocking it is **accepted** — see
    [What "accepted" means](#what-accepted-means) —
    (`promote_accepted_features.sh`, owner flow 2026-08-02).
    It is exempt while its own blockers are held, because otherwise the gate
    would deadlock on the work it is holding: the feature waits on its
    failure, the failure waits on the gate, and the gate waits on the
    feature. The link is the **native blocked-by/blocks relationship** the
    promotion sweep reads (owner decision 2026-08-12, #3731; the retired
    `Related feature: #N` marker is still read as a fallback for issues filed
    before that), so the two sweeps always agree on which held issue parks
    which. Once the blockers are released the exemption lapses — the feature
    is then waiting on ordinary in-flight work, which moves on its own.

    **Held issues labeled `Acceptance Failure` *or* `Improvement` park what
    they block** — the same label filter the promotion sweep applies, so the
    two sweeps agree on this too. This changed with #3731: `@improvement` now
    writes the same blocking relationship as `@acceptance-failure`, so the
    sweep will not promote an issue while a held improvement blocks it. Were
    the gate to exempt fewer issues than the sweep parks, the deadlock would
    simply reappear for improvements. Anything carrying neither label parks
    nothing. (`DRAIN_GATE_REWORK_LABELS` overrides the list;
    `DRAIN_GATE_REWORK_LABEL` remains a back-compatible alias.)
- On the opening, every held item moves to `Backlog` and the queue is kicked
  **once** for the whole batch (one kick, not one per issue — the dispatcher
  picks the next item itself). Parked items (closed features and reopened
  originals, see above) are not held items: they are named in the run log and
  left in place.
- While `PAUSE_SPRINT` is in force the lane is still released, but no kick is
  posted; the note carries the informational marker so it cannot itself
  dispatch work.

### Bypass: agent-process work

The gate is for **product acceptance** work. Agent-process issues are worked
immediately. The rule lives in `scripts/agents/lib/drain_gate.py`:

- the owner-created **`Agent`** label, which marks nyxAGENT-side work as
  distinct from nyxGPT product work (owner decision 2026-10-03, #4134). It is
  the DEFAULT of `DRAIN_GATE_BYPASS_LABELS`, not an env var each workflow has
  to remember to pass — that is what went wrong before, when the rule was
  wired, the label existed and no caller supplied it;
- an owner-authored process exception in the issue body — any phrasing
  matching *"bypasses the drain gate"*;
- the machine marker `<!-- drain-gate: bypass -->`, for automation.

An `Agent` issue also skips rc acceptance testing entirely: on merge it goes
to **For Release**, not **Acceptance Testing** (`review_accept_and_merge.sh`).
It ships no product surface, so there is nothing for the owner to test in a
candidate — and parking it in the lane the gate watches would both block the
gate and trigger a candidate the work has no part in.

The owner's `@acceptance-failure` / `@improvement` comment text is checked
too, so an exception can be declared at filing time.

Both commands must **open a line** of the comment to file anything (#3790) —
`@improvement the settings page needs a save button` files; "I already filed
that as an @improvement on #3731" does not. See
[Comment tokens](agent-comment-tokens.md).

### The two sweeps are dispatched, not polled (#4201)

`promote_accepted_features.yml` and `sweep_parked_blocked_issues.yml` ran on
`*/30 * * * *` until 2026-10-21 — 96 runs a day between them, each checking
out the release branch and walking every parked issue's transitive blocked-by
closure to report `Promoted 0 issue(s)`. **Both are now dispatch-only**, for
the reasons that removed the ceremony's cron (ledger **D-060**) and this
gate's (2026-08-25): GitHub throttles a busy cron to every 3–8 hours, so the
poll was unreliable as well as expensive, and first principle 1 says do not
poll what will notify you. Ledger **D-070**.

Every actor that changes the state a sweep waits on dispatches it, through
one shared helper — `dispatch_acceptance_sweeps` in
`scripts/agents/lib/gh_project.sh`, which dispatches *both* sweeps on the
release branch and is best-effort (it always returns 0, so a refused dispatch
can never fail a merge that already happened):

| Actor | When |
|---|---|
| `scripts/agents/review_accept_and_merge.sh` | on merge, after the issue lands in `In Review` (parked), `Acceptance Testing` or `For Release` |
| `scripts/agents/drain_gate.sh` | after a release that actually moved something — **edge-triggered**, because this workflow subscribes to both sweeps' `workflow_run` completion and an unconditional dispatch would be a cycle |
| `scripts/release_ceremony.sh` | at the end, after Phase 4 restores the agent flags (both sweeps gate on `AGENTS_ENABLED`, so a dispatch sent during the pause would be accepted and then skipped) |

The acceptance handlers (`@acceptance-failure`, `@improvement`) deliberately
dispatch nothing: all they write is a new *blocker* plus a park, so they add a
gate and can never satisfy one.

**The one case no actor can announce is yours.** This is a *user* project, so
moving an issue on the board raises no `projects_v2_item` event anything can
subscribe to. If you move the last blocker to `For Release` yourself, one
dispatch picks it up — the same shape **D-060** uses for the ceremony:

```bash
gh workflow run promote_accepted_features.yml --ref <release branch> -f dry_run=true
gh workflow run promote_accepted_features.yml --ref <release branch>
gh workflow run sweep_parked_blocked_issues.yml --ref <release branch>
```

Nothing is stranded by the crons' absence. Both sweeps converge **within one
run** — `sweep_parked_blocked_issues()` always did, and
`promote_accepted_features.sh` gained the same fixed-point loop in #4201
because the cron had been doubling as its cascade iterator, resolving one hop
per tick. A run with nothing promotable moves nothing and posts nothing.

Executed evidence for the trigger itself is
`.github/workflows/acceptance-sweep-dispatch-smoke.yml`, which performs a
real `dry_run=true` dispatch through the real helper and cites the resulting
run URLs (D-006 — `schedule` only ever runs the default branch's copy, so the
cron this replaced could never be exercised from a branch at all).

### Moving parts

| Path | Role |
|---|---|
| `scripts/agents/lib/drain_gate.py` | pure decisions: lane summary (including the parked/held split), gate state, rework exemption, bypass rule |
| `scripts/agents/promote_accepted_features.sh` | promotes a parked issue out of either lane once its whole blocked-by closure is accepted; iterates to a fixed point so a cascade resolves in one run |
| `scripts/agents/lib/gh_project.sh` → `dispatch_acceptance_sweeps` | the one place that dispatches either sweep (D-066); `drain_gate_wants_sweeps` is the edge-trigger rule |
| `scripts/agents/lib/gh_project.sh` | `acceptance_lane_snapshot`, `drain_gate_classify_held`, `drain_gate_state`, `issue_acceptance_role`, `acceptance_reopen_original`, `drain_gate_hold`, `drain_gate_release`, `issue_bypasses_drain_gate` |
| `scripts/agents/drain_gate.sh` | `state` (read-only) / `release` (open the gate) |
| `.github/workflows/acceptance_drain_gate.yml` | the watcher |

### Detection

The project is a **user** project, so its item changes raise no
`projects_v2_item` event a workflow can subscribe to. The watcher therefore
combines events with an explicit manual dispatch:

- `workflow_run` after **Promote Accepted Features** and **Sweep Parked
  Blocked Issues** — the two automations that move items out of Acceptance
  Testing, so a drain is normally detected within seconds of one running.
  Since #4201 those two are themselves dispatch-only, so this path fires when
  an actor dispatched them (a merge, a ceremony, a gate release) rather than
  on every half hour — which is the same set of moments that could have
  drained the lane in the first place;
- `issues: [closed]`;
- `workflow_dispatch` — the owner opens the gate by hand at the end of a round.

**There is no schedule** (owner decision, 2026-08-25). A 15-minute cron used to
back-stop the one case no event covers — the owner moving the last item by hand
on the board. That case is unchanged and still real; it is now answered by the
owner dispatching the workflow rather than by polling for it 96 times a day
(first principle 1: do not keep a watch armed over a process that is idle by
design). **So accepting the last issue of a round does not wake the gate** —
see [Operating it by hand](#operating-it-by-hand) below. Nothing is stranded
permanently: the next `issues: [closed]` or sweep completion evaluates it
normally.

Evaluation is safe whenever it happens: with the holding lane empty the run
releases nothing and posts nothing.

### Operating it by hand

```bash
scripts/agents/drain_gate.sh state              # read-only gate state JSON
DRY_RUN=1 scripts/agents/drain_gate.sh release  # report what would be released
scripts/agents/drain_gate.sh release            # open the gate now
```

or dispatch **Acceptance Drain Gate** from the Actions tab (`dry_run` input).

---

## 2. The automated release ceremony

### The signal

**The owner moving the release tracking issue to `For Release` is the human
sign-off.** This supersedes the old "master/main merges are human-controlled"
rule in `CLAUDE.md`: master is still never merged by agents in the normal
loop, and now reaches a release only through this one signed-off path.

### What runs, unattended

1. **Phase 0** prerequisite inventory → provision → entry gate (#4166): it
   checks every prerequisite for all five phases in one pass, creates the ones
   automation can (see **Prerequisites** below), and stops before Phase 1 —
   listing *every* gap, not the first — if anything it cannot create is
   missing: milestone not fully closed, release issue task list unclean, open
   critical/high code-scanning alerts, a `pyproject.toml` version mismatch, a
   tag already taken.
2. **Phase 1** master fast-forward → normalize and publish the draft release
   (tag created on master).
3. **Phase 2** `stable` publish, delegated to the single publish pipeline
   (`release-publish-pypi.yml`, #3727) with Trusted Publishing; verified live
   on pypi.org.
4. **Stable Homebrew tap stamp** — `release-artifacts.yml` triggers on the
   published release.
5. **Phase 3** project close-out: statuses → Done, milestone closed, release
   issue closed with a summary.
6. **rc retirement** — `scripts/retire_rc_formulas.sh` removes that line's
   `nyxgpt-api@<VERSION>rc` / `nyxgpt-web@<VERSION>rc` formulas from the tap.
   Version-scoped by name (#3735): a later line's candidates are a different
   formula and are left alone.

7. **Phase 4** next line + repoint (automated since 2026-10-07, ledger D-061):
   the next branch is named by the lowest open milestone titled `(vX.Y.Z)`
   above the release; it is created from the release tag (or, if it exists,
   the release is forward-ported into it), its `Release Management` release
   issue and draft release are created, `pyproject.toml` is bumped, and the
   default branch / `RELEASE_BRANCH` / `RELEASE_ISSUE_NUMBER` are repointed.

**Agent pause.** Phase 0 sets `AGENTS_ENABLED`, `SPRINT_AUTOPILOT` and
`CLAUDE_REVIEW_ENABLED` to `false` (prior values saved in the repo variable
`CEREMONY_PAUSED_FLAGS`) so nothing merges into the line being released;
Phase 4 restores them after the repoint, so the agents resume on the new line.
A run that stops part-way leaves them paused deliberately.

**Prerequisites.** Phase 0 checks every prerequisite for all five phases in
one pass and provisions what it can (#4166): the next line's `(vX.Y.Z)`
milestone — as a `Placeholder — next line (vX.Y.Z)` for the next patch
version, for the owner to rename — the next `Sprint <N+1>` iteration, this
release's draft release, and the release issue's `Release Management` label
and milestone. Each is re-verified by query and listed in a note on the
release issue. Anything automation cannot create (open issues in the release
milestone, unchecked release-issue tasks, open critical/high code-scanning
alerts, a `pyproject.toml` version mismatch, an existing tag, missing
tap/Slack wiring) stops the run before Phase 1, with every gap listed. The
milestone and sprint used to be checked only in Phase 4, i.e. after the
release was already published.

**Resume.** Re-dispatching after a failure needs no `force` once the release
tag exists: the watcher runs `--phase4-only`, which verifies Phases 0-3
completed, re-runs the line-prerequisite inventory, and runs only Phase 4.
Anything Phase 0 provisioned is idempotent and stays in place. The session
that dispatched the run reconciles the owner's local `config.ini` mirror
afterwards.

### Guardrails

`scripts/agents/lib/ceremony_trigger.py` (unit-tested) decides, and it fires
only when **all** of these hold:

- the issue is the **release tracking issue** (`RELEASE_ISSUE_NUMBER`);
- its Status is **`For Release`**;
- its title carries a parseable **`vX.Y.Z`** — no version is a conservative
  stop;
- no **version-scoped ceremony marker** is already on the issue. This is what
  makes the trigger edge-triggered: a repeat or concurrent dispatch of a
  finished (or running) release is a no-op. A previous line's marker never suppresses a new line's
  ceremony.

The marker is posted **before** anything irreversible happens — and if it
cannot be posted, the ceremony does not start at all. Claiming is the point:
an unclaimed ceremony would let a second dispatch start a second one, which
then dies at the tag gate and DMs the owner a false alarm. Nothing has changed
at that point, so re-running is simply another dispatch
(`gh workflow run release_ceremony.yml`) -- nothing retries on its own. The ceremony token is checked at
the same point, before the claim.

### Failure handling

Any failure stops the ceremony where it is, posts a loud report on the release
issue, and **DMs the owner on Slack** through the existing escalation channel
(#3695). Nothing after the failed step runs.

Re-run after fixing the cause: dispatch **Release Ceremony (Automated)** with
`force=true`, or run `scripts/release_ceremony.sh <VERSION>` locally.

### Configuration

| Name | Kind | Purpose |
|---|---|---|
| `RELEASE_CEREMONY_TOKEN` | secret | owner-level token; Phase 1 pushes master (ruleset bypass). Checked **before** the ceremony claims the release issue: unconfigured, the watcher reports it on the issue, DMs the owner and stops without changing anything |
| `HOMEBREW_TAP_REPO` / `HOMEBREW_TAP_TOKEN` | var / secret | tap stamp and rc retirement (optional — an unconfigured tap is a warning, not a failure) |
| `SLACK_BOT_TOKEN` / `SLACK_USER_ID` | secrets | owner DM on failure (#3695) |
| `RELEASE_ISSUE_NUMBER`, `STATUS_FOR_RELEASE`, `RELEASE_BRANCH` | vars | existing loop configuration |

### Dry run

```bash
scripts/agents/release_ceremony_watch.sh --check-only   # decide only, no writes
```

or dispatch the workflow with `check_only=true`.

---

## Tests

```bash
pytest tests/unit/test_drain_gate.py tests/unit/test_ceremony_trigger.py  # pure decisions + the drain-gate shell suite (CI)
pytest tests/unit/test_board_sweeps_are_dispatch_driven.py  # no sub-daily cron, one dispatch source, every actor wired (#4201)
bash tests/test_drain_gate_lib.sh          # lane snapshot, parked/held split, gate open/closed, hold, release, start guard
bash tests/test_promote_accepted_features.sh  # promotion out of both parking lanes (also run by pytest)
bash tests/test_release_ceremony_watch.sh  # ceremony trigger guardrails, against a fake gh
bash tests/test_retire_rc_formulas.sh      # rc retirement, against a local bare repo standing in for the tap
```

The shell suites stub `gh`/`graphql` (or use a local git repo), so none of
them touch GitHub. The two acceptance-cascade suites are wired into
`pytest tests/unit/` — the gate this repo actually runs — so the lane
snapshot, the release loop and the promotion sweep are *executed*, not just
inspected, on every CI run; the remaining agent-loop shell suites are still
run on demand.
