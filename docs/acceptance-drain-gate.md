# Acceptance drain gate and the automated release ceremony

Owner decisions of 2026-08-12 (#3730), implemented across the agent loop.

Two things changed:

1. Acceptance failures and improvements filed **during** an acceptance round
   are held until the round drains, instead of being worked immediately.
2. The owner moving the **release tracking issue** to `For Release` is now the
   release sign-off, and the release ceremony runs from it unattended.

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
"so that I don't get lost as to what I've tested that has failed". So the lane
carries two populations, and the machinery tells them apart by **issue state**:

| In `Acceptance Failed` | State | What it is | What the machinery does |
|---|---|---|---|
| this round's rework | **OPEN** | a failure/improvement the handlers just filed (or a reopened fix that failed re-test) | **held** — released to `Backlog` when the gate opens |
| a parked feature | **CLOSED** | already implemented and merged, then failed by the owner; waiting on its blockers | **left alone** — the only move is promotion to `For Release` once its whole blocked-by closure is accepted |

State is the honest discriminator: held rework is always open (the handlers
file a fresh issue, and a fix that fails re-test is *reopened*), while a parked
feature is closed because it was merged. A label check would misread a closed
failure issue the owner parked after re-testing it.

Two rules follow, and both are enforced rather than documented:

- `promote_accepted_features.sh` treats a feature parked in `Acceptance Failed`
  **identically to one parked in `Acceptance Testing`** — a promotion candidate
  whose transitive blocked-by closure gates it. An OPEN item in the lane is
  held rework and is never promoted; #3730's holding-pen behavior is unchanged.
- **Nothing moves a feature out of `Acceptance Failed` while any blocker is
  open.** The placement is owner signal: agents read it, they do not rearrange
  it. Only the all-blockers-accepted promotion moves it, and the drain gate
  reports it (`parked` in the gate state, and a line in the release log) rather
  than releasing it.

Motivating incident: #3508, #3509 and #3596 sat parked with no cascade even
though their failure issues had been fixed, and the placements were then
mistaken for stale board state and swept — recorded in `agents/LEDGER.md`
(D-001 note, 2026-08-14).

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
    until everything blocking it reaches `For Release`
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
  picks the next item itself). Parked features (closed items in the lane, see
  above) are not held items: they are named in the run log and left in place.
- While `PAUSE_SPRINT` is in force the lane is still released, but no kick is
  posted; the note carries the informational marker so it cannot itself
  dispatch work.

### Bypass: agent-process work

The gate is for **product acceptance** work. Agent-process issues are worked
immediately. The rule lives in `scripts/agents/lib/drain_gate.py`:

- an owner-authored process exception in the issue body — any phrasing
  matching *"bypasses the drain gate"*;
- the machine marker `<!-- drain-gate: bypass -->`, for automation;
- optionally, a label listed in `DRAIN_GATE_BYPASS_LABELS` (off by default —
  agents may not create labels without owner permission).

The owner's `@acceptance-failure` / `@improvement` comment text is checked
too, so an exception can be declared at filing time.

### Moving parts

| Path | Role |
|---|---|
| `scripts/agents/lib/drain_gate.py` | pure decisions: lane summary (including the parked/held split), gate state, rework exemption, bypass rule |
| `scripts/agents/promote_accepted_features.sh` | promotes a parked issue out of either lane once its whole blocked-by closure is accepted |
| `scripts/agents/lib/gh_project.sh` | `acceptance_lane_snapshot`, `drain_gate_rework_features`, `drain_gate_state`, `drain_gate_hold`, `drain_gate_release`, `issue_bypasses_drain_gate` |
| `scripts/agents/drain_gate.sh` | `state` (read-only) / `release` (open the gate) |
| `.github/workflows/acceptance_drain_gate.yml` | the watcher |

### Detection

The project is a **user** project, so its item changes raise no
`projects_v2_item` event a workflow can subscribe to. The watcher therefore
combines events with a cheap poll:

- `workflow_run` after **Promote Accepted Features** and **Sweep Parked
  Blocked Issues** — the two automations that move items out of Acceptance
  Testing, so a drain is normally detected within seconds;
- `issues: [closed]`;
- a 15-minute schedule as the backstop, for the owner moving the last item by
  hand on the board.

Polling is safe: with the holding lane empty the run releases nothing and
posts nothing.

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

1. **Phase 0** entry gate (read-only): milestone fully closed, release issue
   task list clean, no open critical/high code-scanning alerts, draft release
   present, tag not already taken.
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

**Phase 4 (next-line preparation and the repoint) stays owner-run** — it needs
the owner's local `config.ini` mirror and next-line decisions:

```bash
scripts/release_ceremony.sh <VERSION> --phase4-only --next-branch v<NEXT>
```

### Guardrails

`scripts/agents/lib/ceremony_trigger.py` (unit-tested) decides, and it fires
only when **all** of these hold:

- the issue is the **release tracking issue** (`RELEASE_ISSUE_NUMBER`);
- its Status is **`For Release`**;
- its title carries a parseable **`vX.Y.Z`** — no version is a conservative
  stop;
- no **version-scoped ceremony marker** is already on the issue. The watcher
  polls, so this is what makes the trigger edge-triggered: a finished release
  is never re-run. A previous line's marker never suppresses a new line's
  ceremony.

The marker is posted **before** anything irreversible happens — and if it
cannot be posted, the ceremony does not start at all. Claiming is the point:
an unclaimed ceremony would let the next poll start a second one, which then
dies at the tag gate and DMs the owner a false alarm. Nothing has changed at
that point, so the next poll simply retries. The ceremony token is checked at
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
