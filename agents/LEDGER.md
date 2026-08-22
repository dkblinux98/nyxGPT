# nyxGPT Operating Ledger

The system of record for cross-session agent memory (owner decision,
2026-08-14, #3774).

Agent memory is reconstructive: each session re-derives project state from
artifacts and lossy self-summaries, and — when the artifacts are incomplete —
asserts the reconstruction with the same confidence as something it actually
checked. That failure is invisible to both parties, because reconstructed
state is indistinguishable from verified state.

This file is the fix. **The repository carries the memory, not the agent.**
Sessions read this file at start, treat their own recollection as untrusted
input wherever this ledger or the live system can answer instead, and append
here when they decide something, park something, or hit a question they
cannot close. Facts about how the system *behaves* are not written here — they
are encoded in the guard that enforces them (see below).

**The rule: a claim that is not in this ledger and not freshly checked is not
asserted as fact.** Say "I have not checked" and then check.

This also bounds churn cost. It converts an unbounded invisible expense
(re-derive everything every session, sometimes wrongly) into a bounded visible
one: read one short file of decisions, and let the guards carry everything
that can be checked instead of remembered.

---

## How to use this ledger

**At session start.** Read it — all of it. It is deliberately short enough to
read in full, and keeping it that way is a maintenance obligation (see
[Granularity](#granularity-what-goes-in)). It is on the `CLAUDE.md` bootstrap
list for this reason.

**Before asserting project state.** If you are about to state a fact about how
this project works, what was decided, what is published, or what is deliberately
not being done, one of these must be true:

1. It is an entry below, and that entry is not stale for your purpose; or
2. You just checked it this session, and you can name how; or
3. You say plainly that you have not checked it.

Recalling it "from earlier in the project" is not one of the three. Neither is
inferring it from a plausible-looking artifact.

**Check the [Superseded](#superseded) section before you correct anyone.** It
lists beliefs this project has *already* held and retired. Re-asserting one of
those is the specific mistake this ledger exists to stop.

**When you learn something.** Append an entry. Doing the work and leaving the
finding only in a PR comment or a session summary is how the last round of
knowledge was lost.

**Do not silently overwrite state you did not create.** If the board, a lane, or
a marker looks wrong to you, check the ledger for a parked item explaining it
before "cleaning it up". Board placements can be deliberate owner signal even
when they look stale (see D-001/D-008). Operational incident history is kept in
the owner's private annex (`product_management/private/LEDGER.md`, not in the
repository).

---

## Granularity (what goes in)

The ledger is only useful while it stays cheap to read. Entries are
load-bearing facts and decisions, **not narration**.

**Include:**

- Decisions that change how the system operates, and who made them.
- Things deliberately *not* being done, with the condition that would revive
  them.
- Questions whose answers gate work.
- **Corrections**: beliefs this project asserted and later found wrong. These
  earn their space; they are the entries that prevent repeat incidents.

**Exclude:**

- Narration of what a session did. That is what commits, PRs and issue threads
  are for.
- Anything git or GitHub answers directly in one lookup: what merged, what a PR
  changed, an issue's current status, who is assigned.
- Restatements of rules already written in `CLAUDE.md`, `AGENTS.md`, the
  charters or the runbooks. Link to them instead; two copies of a rule
  guarantee one is wrong later.
- Anything true for only one session.
- **Facts about behavior.** These go in the test or guard that enforces them,
  with the reasoning in its docstring. An entry restating what a guard already
  checks is a second copy of the truth, paid for on every run, free to go stale,
  and — as 2026-08-18 proved five times over — able to redden the build by
  itself.

**The test:** *could a future session get this wrong, confidently, and would
neither the live system nor a single doc lookup catch it?* If yes, it belongs
here. If no, it does not.

**Pruning:** never delete an entry. Move it to [Superseded](#superseded) with a
pointer to what replaced it. A deleted entry becomes re-derivable — and
re-derived wrong — which is the whole problem.

---

## Entry schema

Three kinds. Each has a stable ID (`D`/`P`/`Q` + zero-padded number).
**IDs are never reused**, including after supersession.

**Allocate with the helper, not by eye** (#3806):

```
git fetch origin <release branch>
python3 scripts/agents/lib/ledger_ids.py next D --base origin/<release branch>
```

It applies the two rules that are easy to get wrong by hand. It takes
**max + 1, not the lowest unused number** — the gaps below are entries
relocated to the private annex, and those IDs are still taken. And it reads
the **live release branch** as well as your working copy — two PRs open at
once each see a base without the other's entries, so a branch-only scan hands
both of them the same number and the collision is created at merge, by neither
branch alone. That is how `V-034`/`V-035` came to be defined twice, failing
`test_ledger_entry_ids_are_unique` for every review on the branch afterwards.

**The number you are given is provisional, and that is fine** (#3862). A
branch open for days cannot know what the base will hold by the time it lands,
so `review_accept_and_merge.sh` re-runs the allocation immediately before the
merge:

```
python3 scripts/agents/lib/ledger_ids.py reallocate --base origin/<release branch> --write
```

Only IDs that **both** sides invented since the merge base are moved, only on
the branch, and cross-references move with them. Entries you merely edited keep
their IDs. So do not hand-resolve a duplicate-ID conflict on a long-lived
branch — the by-hand resolution got the `theirs`/`mine` sides backwards on the
first attempt (#3836/`V-030`). `test_ledger_entry_ids_are_unique` stays as the
backstop; it works, and it is what catches anything this misses.

**Decision** — something was settled, and by whom.

```
- **D-000** · YYYY-MM-DD · <who decided> — <the decision, one or two sentences>.
  Source: <issue / PR / doc §>.
```

**Parked** — deliberately not being worked, so nobody re-proposes it.

```
- **P-000** · YYYY-MM-DD · <who parked it> — <what is parked>.
  Reason: <why>.
  Revisit when: <the condition that revives it>.
```

**Open question** — unresolved, and something depends on it.

```
- **Q-000** · YYYY-MM-DD · <who raised it> — <the question>.
  Needs: <who or what can answer it>.
  Blocks: <what is waiting, or "nothing yet">.
```

**Staleness.** A decision stands until it is superseded; there is nothing to
re-verify. If the world has moved and a decision no longer holds, move it to
Superseded with a pointer to what replaced it — never edit it into a different
decision.

---

## How entries reach the ledger

Agents append through the normal branch/PR path — no direct commits to the
release branch, same as any other change.

An entry may ride along in whatever PR produced the fact; it does not need its
own issue, and it is **in scope by definition**. Review agents must not treat a
ledger entry in a fix PR as scope creep, and must not block a PR over entry
wording. (An entry that contradicts the change it ships with is a normal
finding, like any other wrong statement in a diff.)

The owner may append directly; owner-authored entries are authoritative and are
not agent-editable except to add a supersession pointer.

**This public ledger is machine-facing.** Incident narratives, owner-sensitive
decisions and product-forward material live in the owner's private annex
(`product_management/private/LEDGER.md` — gitignored, never in the repository).
Public entries state mechanics; they do not narrate the owner. Some entry IDs
are absent here by design (relocated to the annex; IDs are never reused).

---

## Verifications were retired (owner decision, 2026-08-18)

There is no verification log any more. 54 `V-` entries, 13,752 words — **71% of
this file** — were removed at `a802a04`; git history holds every one of them,
and `V-NNN` citations left in code comments still resolve there.

They were removed because they cost more than they returned. Every agent run
paid to read them. Five separate id collisions on 2026-08-18 turned the release
branch red, which failed the gate for every open PR, cost #3825 an entire cycle
and paged the owner — over bookkeeping with no product meaning. And the rule
they most loudly enforced ("IDs are never reused") was in context every time it
was broken: the rule was right and the *mechanism* was racy, which is not
something prose can fix.

What they were mostly doing was restating, in a second place, something a test
already enforced — a copy of the truth that has to be maintained, can go stale,
and could turn the build red on its own. **A fact worth keeping is worth
encoding in the guard that enforces it**, where it is checked rather than
remembered and costs nothing per run. If you find yourself wanting to write a
verification, write the test instead and say why in its docstring.

Decisions and corrections stay, because they record intent and retired beliefs
rather than mechanism, and nothing can enforce them.

## Decisions

- **D-001** · 2026-08-12 · owner — Acceptance failures and improvements filed
  during an acceptance round land in the `Acceptance Failed` Status lane and are
  held there: not selected, not kicked, not auto-resumed. The gate opens when
  `Acceptance Testing` drains (the release tracking issue is exempt and stays
  until the whole release is accepted), moving held items to `Backlog` and
  kicking the queue once. Agent-process issues bypass the gate.
  **Items sitting in `Acceptance Failed` are deliberately held state, never
  stale board state to sweep.**
  Source: #3730; `CLAUDE.md` §Acceptance Drain Gate; `scripts/agents/lib/drain_gate.py`;
  `scripts/agents/drain_gate.sh`.

- **D-002** · 2026-08-12 · owner — GitHub's native issue relationships
  (blocked by / blocks) are the only storage for links between issues — never
  body prose, never comment markers. A new acceptance issue blocks acceptance of
  the marked issue and, transitively, anything blocked by that one; transitivity
  is walked, not written as extra edges. The `Related feature: #N` /
  `Parent feature: #N` body convention is retired (still *read* as a fallback for
  pre-decision issues, and healed into a native edge by
  `promote_accepted_features.sh`).
  Source: #3731; `CLAUDE.md` §Issue Relationships;
  `scripts/agents/lib/issue_relationships.py`.

- **D-003** · 2026-08-12 · owner — Master merges are ceremony-only and
  automated. The human control point is the owner moving the release tracking
  issue to `For Release`; from that move the ceremony runs unattended through
  master fast-forward, tag, GitHub Release, `stable` publish, tap stamp and
  `-rc` retirement. No human step happens after the move. Nothing else may push
  master.
  Source: #3730; `CLAUDE.md` §Branch Rules; `.github/workflows/release_ceremony.yml`.

- **D-004** · 2026-08-14 · owner — At genuine decision points (cut a candidate
  or hold; select an issue or park; escalate or continue) the decision function
  is an agent invocation with the owner's cadence and intent as its charter, not
  a condition expression. Scripts stay as guardrails defining what may never
  happen; inside those rails judgment picks the action and writes down its
  reasoning. Floors, never ceilings.
  Source: `product_management/AGENTIC_SDLC_DESIGN.md` §9/§9a.

- **D-005** · 2026-08-14 · owner — This ledger is the system of record for
  cross-session agent memory. A claim not in it and not freshly verified is not
  asserted as fact.
  Source: #3774.

- **D-006** · 2026-08-14 · owner — Nothing reaches owner acceptance testing
  whose runtime, install or platform claim has not been demonstrated by
  *execution on the target platform*. Inspection is not evidence; the PR cites a
  run (smoke workflow, dispatched workflow, `nyxgpt ops verify`, or a command
  transcript from the target). Where the runner is green by luck the evidence
  must inject the failing condition and show both halves. Missing executed
  evidence on an in-scope change is a Medium (blocking) review finding; pure
  logic covered by unit tests and prose-only changes are exempt.
  Source: #3775; `agents/runbooks/review-runbook.md` §1c;
  `agents/runbooks/developer-runbook.md` §4a; `CLAUDE.md` §Definition of Done.

- **D-007** · 2026-08-14 · owner — The repository's immutable-releases setting
  stays **enabled** (owner decision, 2026-08-14). The
  supply-chain guarantee (a published asset can never be silently swapped) is
  retained; the companion-release pattern (`<version>-homebrew`, #3763) is the
  designed answer for adding assets tied to an already-published version. Do not
  re-propose disabling immutability as a fix for `HTTP 422` asset-upload errors.
  Source: owner in session, 2026-08-14; #3763.

- **D-008** · 2026-08-14 · owner — The `Acceptance Failed` lane holds **two**
  populations, and the cascade machinery reads both. Besides this round's held
  rework (#3730), the owner parks there the *features they have tested and
  failed*, keeping tested-failed work visually separate from work still to test.
  The discriminator is issue **state**: **open** = held rework, released to
  `Backlog` when the gate opens; **closed** = a parked feature, which
  `promote_accepted_features.sh` treats exactly like one parked in
  `Acceptance Testing` (promoted to `For Release` when its whole transitive
  blocked-by closure is accepted) and which **nothing else may move** — the
  placement is owner signal, and while any blocker is open the machinery leaves
  it untouched. Extends **D-001**, whose "held state, never stale board state
  to sweep" rule now covers parked features too.
  Source: #3780; `docs/acceptance-drain-gate.md` §`Acceptance Failed` holds two
  different things; `scripts/agents/promote_accepted_features.sh`;
  `scripts/agents/lib/drain_gate.py`.

- **D-009** · 2026-08-15 · owner — A **dev install mode** exists alongside the
  artifact path: `nyxgpt up --dev` / `nyxgpt ops install --dev` installs the api
  as an editable venv on the current checkout and runs the web UI's Next dev
  server from `<checkout>/web`, so the stack runs whatever HEAD the last
  `git pull` produced with no keg, tap or tarball build. It is opt-in and
  checkout-only; a bare `nyxgpt up` remains the artifact path and the
  repo-less guarantee (#3504) is unchanged — dev mode is a development and
  mid-stream-testing path, never an acceptance path. The mode is recorded in
  `~/.nyxGPT/install-mode.json` (`nyxgpt.install_mode`) and reported by
  `ops status`/`doctor`, because macOS drives different service managers per
  mode (dev LaunchAgents `com.nyxgpt.api`/`com.nyxgpt.web` vs. `brew
  services`) and self-heal must not restart an old keg onto the dev process's
  port. Installing either mode over the other stops the other's services and
  rebuilds the shared api venv from empty.
  Source: #3789; `docs/ops.md` §`--dev`; `src/nyxgpt/install_mode.py`.

- **D-010** · 2026-08-15 · owner — Agent model assignment is a **deliberate
  split**: the **review** agent (`claude-code-review.yml`, the `@claude`
  entry point and huddle mediation) runs on **`claude-fable-5`**; the
  **developer** agent's implementation paths run on **`claude-opus-5`**. The
  Fable pins are a chosen configuration, not an oversight — do not "upgrade"
  them to Opus on sight.
  **Override applied and ENDED, both on 2026-08-15** — the split above is the
  live configuration again; the paragraph below is history, not current state.
  For ~2 hours every agent invocation was pinned to
  `claude-opus-5` (commit `e56c3d9b`, reverted once the owner raised the
  limit) because Fable was refused at the API with
  a **monthly spend limit**, not a rolling usage window. The evidence is in the
  Claude step's own result payload (review run 31908249723):
  `"api_error_status": 429`, `"terminal_reason": "api_error"`,
  `"result": "You've hit your monthly spend limit. Switch to another model to
  continue."` — i.e. Anthropic's own guidance is the remedy applied here. Every
  review run from 20:20 UTC failed this way while the Opus-pinned developer
  paths ran normally. The owner raised the limit the same evening and the
  override was reverted in full, restoring the split exactly. The reason the
  split exists is still not recorded here — ask the owner before changing it.
  **Diagnostic trap (cost an hour on 2026-08-15):** that 429 is not what the
  workflow reports. `claude-code-action` surfaced only
  `--json-schema was provided but Claude did not return structured_output.
  Result subtype: success` — calling a refused run a success — with the real
  429 buried in `claude-execution-output.json`. The usage-limit detector missed
  it too: `count_fast_claude_steps` is called with the step name
  `Run Claude Code Review`, but the action's step is actually named
  `Run Claude Code Action`, so it printed "No usage-limit signature detected"
  and never applied the retry label. Read the result payload's
  `api_error_status`/`result`, never the action's top-level error text.
  Source: owner in session, 2026-08-15; commits `54c38faa` (original all-Fable
  pinning), `e56c3d9b` (this override); run 31908249723 log.

- **D-011** · 2026-08-15 · owner — Merge conflicts are the **developer
  agent's** work, and branches are **never rebased**. Two rules settled
  together: (a) a conflicted PR is dispatched to the developer agent, which
  merges `origin/<release-branch>` into the PR branch, resolves preserving both
  sides, re-runs the gates and pushes — the owner is assigned **only** when the
  agent issues `CONFLICT_REQUIRES_OWNER_DECISION` with a specific question, or
  when the automated rounds (default 3) stop converging; Slack still notifies
  the owner of every conflict. (b) Resolution and freshening are always a
  forward **merge** — no `git rebase`, no `git pull --rebase`, no force-push or
  history rewriting on shared branches. The no-rebase half was decided
  2026-08-08 ("merge, don't rebase") but lived only in
  `product_management/AGENTIC_SDLC_DESIGN.md`, so agents kept proposing rebases;
  it is now runbook doctrine and a review finding.
  Source: #3801; `agents/runbooks/developer-runbook.md` §2 / §8c;
  `agents/runbooks/review-runbook.md` §3a;
  `scripts/agents/dispatch_conflict_resolution.sh`;
  `scripts/agents/lib/conflict_resolution.py`.

- **D-012** · 2026-08-16 · owner — **Agentic first principles**, binding on all
  agentic work wherever it occurs and prior to every role's permissions:
  (1) consider cost — every run and re-check spends real money; (2) consider
  future harm, to the agentic process as much as to the application;
  (3) minimize both **without compromising quality or completeness** — these
  constrain how the work is done, never how much is delivered, and scaling
  scope down remains the owner's call; (4) **never take change action without
  first seeking to understand** — diagnose before fixing. Occasioned by two
  defects each patched narrowly against a guessed cause and each recurring:
  the broken-`pyexpat` chain (#3753 → #3788 → #3814, where the real fault was
  a Homebrew bottle, not pip) and the project-hygiene clobber (#3500 → #3816,
  fixed once by exempting a single author while every other author kept
  racing). **Binding at runtime is `CLAUDE.md` itself** — the agent action
  loads it as project instructions in every agent run (**V-028**), so the
  principles are deliberately *not* copied into the prompts: a copy would be
  paid for on every run and would drift out of step with the source. Enforced
  at the review checkpoint by `review-runbook.md` §1d (principle 4, diagnosis)
  and §1e (principle 2, generality), both by citation (#3821).
  Source: owner directive 2026-08-16; `CLAUDE.md` § Agentic First Principles;
  `AGENTS.md` § First Principles; #3821.

- **D-013** · 2026-08-18 · owner — **Branch cleanup is event-driven, never
  scheduled.** A scheduled sweep is rejected on cost (first principle 1: do not
  keep a watch armed over a process that is idle by design). The developer
  agent cleans up her own branches at the moments work changes state: the
  merge flow deletes the PR branch (exists — `delete_branch_on_merge`), and
  any path that abandons or supersedes a branch (retry onto a fresh branch,
  rebase, escalation abort) deletes the branch it replaced — after the
  blob-level content check in #3862, never on ancestry, commit count, PR
  existence, or mergeability, each of which was disproven against a real
  branch set on 2026-08-18. Do not re-propose a scheduled sweep as a fix for
  branch accumulation.
  Source: owner in session, 2026-08-18; #3862.

- **D-014** · 2026-08-18 · owner — **Model assignment per work type: review
  work runs Fable 5, dev work runs Opus 5, huddle work runs Fable 5.** The
  knobs are the repo Actions variables `AGENT_MODEL_REVIEW`,
  `AGENT_MODEL_DEV` and `AGENT_MODEL_HUDDLE`; the workflow fallbacks encode
  the policy (`claude-fable-5` / `claude-opus-5` / `claude-fable-5`), so with
  no variables set the policy holds by default. Before this decision the
  developer's huddle-position leg rode `AGENT_MODEL_DEV` (Opus 5) and the
  scrummaster mediation leg rode `AGENT_MODEL_REVIEW`; both huddle legs now
  use `AGENT_MODEL_HUDDLE`.
  Source: owner in session, 2026-08-18.

- **D-015** · 2026-08-18 · owner — **The portability matrix gets no dashboard
  screen.** #3516 read CLAUDE.md's Definition of Done ("Ops/SRE features MUST
  be operable from the SRE/admin dashboard") as requiring a surface for
  `nyxgpt ops portability`, and built one; owner acceptance on rc12 removed
  it. The matrix states the *product*'s portability claims, not the state of
  the machine whose dashboard is being viewed, so there is nothing to observe
  and nothing to act on — and the page asserted acceptance status that owner
  testing contradicted. A page that restates documentation inside the running
  product is not an ops surface. `nyxgpt ops portability` (plus
  `GET /api/v1/ops/portability` for machine readers) is how the matrix is
  read. Do not re-add the tile under the Definition of Done; the broader DoD
  refinement (observable from the dashboard, lifecycle operated from the CLI)
  is separate and still open with the owner.
  Source: #3803; owner acceptance round 2026-08-16.

- **D-016** · 2026-08-18 · owner — **Credential and secret *entry* does not
  belong in the web UI.** The `/admin/secrets` (Guided Secrets Setup, #3505)
  and `/admin/aws-credentials` (AWS Credentials Setup, #3512) screens are
  removed, along with the four write endpoints behind them (`POST
  /api/v1/config/secrets`, `/config/secrets/sync`, `/config/aws-credentials`,
  `/config/aws-credentials/secret-store`). Two reasons: a browser is a worse
  surface for a credential than a terminal (the value crosses an HTTP request
  and the page's process, and the same screen reached through a cloud access
  tunnel would carry it over that path), and by the time the web UI is
  running these screens are too late to be useful — reaching the dashboard
  already required the secrets they collected, and AWS credentials are needed
  before a deploy exists to observe. `nyxgpt secrets setup`, `nyxgpt ops
  secrets-sync` and `nyxgpt cloud credentials-setup` are the surfaces; the
  dashboard's Configuration card names them as text, not controls. The
  **Configuration Wizard is explicitly unchanged**, including its `[auth]
  api_key` rotation field — it stays the sanctioned in-product configuration
  surface for a *running* system. The `GET` status paths remain (read-only,
  masked, never cleartext) as the CLI's machine-readable counterparts. Same
  Definition-of-Done refinement as **D-015**: observable from the dashboard;
  pre-product setup and consequential lifecycle run from the CLI.
  Source: #3805 (siblings #3803, #3804); owner acceptance round 2026-08-16.

- **D-017** · 2026-08-18 · owner — **The Definition of Done says *observable*,
  not *operable*: ops/SRE state is observed from the dashboard, ops/SRE
  lifecycle is operated from the CLI.** This closes the refinement D-015 left
  open. It is structural, not a judgment about how often lifecycle actions are
  run: (a) *the self-hosting paradox* — every acting control changes the
  substrate the UI itself runs on, so applying a substrate change or migrating
  Terraform state from a page served by that instance pulls the rug out from
  under it, and a half-completed operation removes the surface that would
  report it; (b) *no usable escape hatch* — driving it safely needs a second
  nyxGPT controlling the first, and two local instances collide on
  `:8000`/`:3000` while a k8s-hosted one collides with the native install on
  the same host ports, so the control surface is unusable where it would be
  safe and unsafe where it is usable. Reading has neither problem. Applied in
  #3804: the `/admin/cloud-infrastructure` screen and its tile are gone, its
  information folded read-only into `/admin/infrastructure`, and Plan, the
  Terraform state actions and tunnel start/stop removed from the web UI *and*
  the API (`POST /cloud/infra/plan`, `POST /cloud/state/{migrate,unlock,restore}`,
  `GET /cloud/state/versions`, `POST /cloud/deploy/tunnel`). CLAUDE.md's
  Definition of Done and `agents/runbooks/review-runbook.md` §"End-to-end
  usability" now carry the rule; a new acting control on a substrate the
  dashboard runs on is a Medium (blocking) review finding. Do not rebuild
  those screens citing the old "operable from the dashboard" wording.
  Source: #3804; owner acceptance round 2026-08-16.

- **D-018** · 2026-08-18 · owner — **A cloud status surface reports which
  machine answered, and says *unknown* when none can.** Deriving AWS substrate
  facts from Terraform state alone made the dashboard read "not provisioned",
  every field blank, while being served *from* the instance Terraform had
  created minutes earlier (rc12) — the state file lives on the operator's
  workstation. The rule: on an EC2 instance read instance metadata (IMDSv2,
  `nyxgpt.cloud_imds`), which describes the actual running machine; on the
  workstation read Terraform state; on a machine that is neither report
  **unknown**, never a blank "not provisioned" that implies an answer nothing
  checked. Same three-way shape for the deployment (`deploy-record` /
  `local-instance` / `none`) and for the Terraform state backend, which is
  reported as *not on this machine* rather than as a local file when the page
  is served from the instance. Unreachable IMDS means "not on EC2", never an
  error, and answers are cached both ways so the polled endpoints never pay
  the link-local timeout.
  Source: #3804; owner observation 2026-08-16 (rc12 cloud deploy).

- **D-019** · 2026-08-18 · owner — **The artifact ships product documentation
  only; how this repository builds itself stays in the repository.** `docs/`
  held both, and packaging the directory wholesale put the agent loop, CI
  process, contributor setup and this project's own GitHub token setup in
  front of a user opening Support → Docs. The split is an allow-list, not a UI
  filter: `src/nyxgpt/resources/docs/` holds one symlink per product document,
  so an unlisted doc is absent from the wheel rather than shipped and hidden.
  The selection is named and *grouped* in `nyxgpt.support.DOC_SECTIONS`
  (Getting started → Using nyxGPT → Configuration → Operating → Reference →
  Help) — the index is grouped by that data, never a flat alphabetical list —
  and `tests/unit/test_support_docs.py` fails if the packaged set and the
  grouping ever diverge in either direction. Excluded docs stay in `docs/`,
  where the agent loop and `CLAUDE.md`'s bootstrap read them; links to them
  from packaged docs resolve to the hosted copy on GitHub instead of a dead
  in-app route. Also excluded, beyond the owner's list of 12: `development.md`,
  `adding-api-endpoints.md`, `file-lock-audit.md` — contributor docs the owner
  named in the issue body and left out of every proposed group.
  Source: #3809; owner acceptance round 2026-08-16 (testing #3745).

- **D-020** · 2026-08-18 · owner — **`claude[bot]` is an allowed author for
  the sanctioned comment triggers** (`@review` on `claude-code-review.yml`,
  which also passes `allowed_bots: "claude"` to the review action).
  **The `READY_FOR_NEXT_ISSUE`/`notify_scrum_ready.yml` half of this entry is
  superseded — see S-006.** What remains true is the identity fact. Every GitHub write
  from a Claude remote session carries that App identity — the session proxy
  rewrites all credentials, so no PAT changes it (verified 2026-08-18: a
  PAT-signed reviewer request still produced a `claude[bot]`-actored run).
  The #3706/#3790 runaway-loop protection lives in the anchored-token gate
  and informational markers, **not in identity exclusion** — removing the
  identity wholesale was a ceiling, not a floor (D-004). Scope note: that
  anchored gate covers the *kick* only; `@review` has no layer-2 gate (author
  list + bare `contains`), and is bounded instead by being convergent and
  one-shot with its own output posting as the already-allowed REVIEW_AGENT.
  The owner's earlier
  same-day "fine as is" close of #3870 was a misunderstanding (they believed
  the restriction already lifted) and was reversed within hours — a session
  reading only the close comment will get this wrong.
  Source: #3870; owner in session, 2026-08-18.

- **D-021** · 2026-08-18 · owner — **Context loading is scoped, not
  exhaustive.** The bootstrap no longer tells agents to read
  `.github/workflows/*`, `scripts/agents/*`, every charter and every runbook:
  it loads `AGENTS.md`, this ledger and `agents/CONTEXT_INDEX.md` (one line per
  workflow and script), plus the one charter and one runbook for the role being
  acted in; everything else is opened on demand through the index. Measured
  cause, 2026-08-18: the old list was ~137k words (~180k tokens) per run and
  the churn data put **97.3% of all tokens in context rather than production**
  (2.26B tokens / 30 days, 84.6M of it repeat context), with workflows and
  scripts alone 70% of the corpus. Two standing rules follow: reading is a cost
  decision like any other (first principle 1), and **ledger entries are
  appended at the end, never reflowed mid-file** — an edit high in a stable
  prompt invalidates every cached token after it, and cache reads were 2.16B of
  that 2.26B.
  Source: owner directive 2026-08-18; `CLAUDE.md` § Bootstrap;
  `scripts/build_context_index.py`.

- **D-022** · 2026-08-18 · developer agent (#3832) — **Self-heal never
  deletes a `Pending` Kubernetes Pod.** `kubectl delete pod` is a repair for
  exactly one state, `Running`-but-not-`Ready`; an unschedulable Pod cannot be
  fixed by deleting it (the ReplicaSet recreates it Pending for the identical
  reason, and the reset Pod age destroys the operator's evidence), and a
  starting one converges on its own. The rule is not overridable by a manual
  "Heal now", and is enforced at the destructive action itself
  (`heal_kubernetes_pod` re-reads the Pod before deleting), not only at the
  caller that decided to call it. Pod state is *read* in one shared place,
  `src/nyxgpt/k8s_pod_state.py`, by both `self_heal.py` and
  `ops._classify_k8s_pod`, so the watchdog and the install report cannot
  disagree about whether a Pod is serving or why it is not. What each keeps
  is its own **policy** on that reading, which is not the same thing and is
  allowed to differ: a `CrashLoopBackOff` Pod fails an install (#3827's
  three-state vocabulary) and is healable by the watchdog. The distinction is
  the load-bearing part — the first cut of this change shipped the shared
  module and the claim while `ops.py` still parsed `PodScheduled` itself, so
  the two silently disagreed about `SchedulingGated`; two classifiers
  agreeing by convention is the defect, not the sharing of a vocabulary.
  Pinned by `test_ops_reads_pod_state_through_this_module_not_its_own_copy`
  and `test_ops_and_self_heal_never_disagree_about_whether_a_pod_is_serving`.
  Source: #3832; `docs/self-healing.md` §Pending Pods are reported, not deleted.

- **D-023** · 2026-08-18 · developer-agent — **A canary rollout gate reads
  the canary track's own Pods, never the serving process's counters.** The
  metrics behind `evaluate`/`promote`/`status` come from the Pods labelled
  `track=canary`, read through the API server's Pod proxy, with `/health`
  and `/metrics` requests excluded — kubelet probes alone would otherwise
  carry an idle canary past `min_requests_for_evaluation` within minutes.
  Two consequences that look like restrictions and are the point: `promote`
  refuses a canary track measurably at zero traffic (`--force` for an idle
  cluster), and `--component web` is reported as *not measurable* rather
  than being given a number belonging to something else, because Next.js
  Pods export no `/metrics`. Do not "restore" a process-wide metrics
  snapshot here: it is the defect, not a fallback.
  Source: #3829; `src/nyxgpt/canary.py`; `docs/kubernetes.md` §Metrics source.

- **D-024** · 2026-08-18 · owner (issue #3824) — **Model pulling is internal
  bootstrap machinery, not configuration.** Every run mode pulls the configured
  chat model (`[nyxgpt] default_model`) and the configured embedding model
  (`[rag] embedding_model`) as part of bringing the stack up, unconditionally:
  no flag skips it, because an install already needs network egress for the CLI,
  the service tarballs and Ollama's own installer, so a skip flag could only
  create a supported way for `ops install` to report success while chat is
  broken. Both models regardless of the RAG toggle — `rag_enabled` is
  per-session, so "RAG is off now" is never a reason to leave the embedding
  model unpulled. The knobs that used to gate and time this (`[rag]
  embedding_auto_pull`, `[rag] embedding_pull_timeout_seconds`) are retired;
  a config.ini that still sets them is ignored, never a startup error.
  What each run mode actually does about it is *not* recorded here: it is
  encoded in the guards that enforce it
  (`tests/unit/test_required_model_bootstrap.py`,
  `tests/unit/test_k8s_manifests.py`, `scripts/first-chat-smoke.py`,
  `scripts/compose-model-prepull-smoke.sh`), per the retirement above.
  (Filed as `D-021` under #3824; renumbered on successive merges of `v3.0.0`
  because concurrently-open branches allocated `D-021`, `D-022` and `D-023`
  first. `D-024` is what `python3 scripts/agents/lib/ledger_ids.py next D
  --base origin/v3.0.0` allocates against the current release branch -- run,
  not eyeballed, since a by-eye renumber against a stale base is what produced
  an earlier collision here. IDs are never reused.)
  Source: #3824.

- **D-025** · 2026-08-18 · developer agent (#3860) — **A green
  `macos-brew-smoke.yml` run from before this entry is not evidence that a
  macOS install works.** From the workflow's creation (#3753) until #3860, its
  two jobs installed a keg and never invoked the product: no `nyxgpt` by name
  from a shell, no `nyxgpt up`, no HTTP request to the API or the web UI, no
  uninstall — and `brew test` asserted only that the keg venv existed and
  `import nyxgpt.app` resolved, all of which is true of a keg carrying no
  reachable CLI. That is the keg that shipped: #3850, #3851, #3853, #3854,
  #3857 and #3859 all sit on the certified path and all passed. The Phase 6
  capstone #3516, whose acceptance criterion *is* the clean-machine scenario,
  closed as completed on that evidence. This is a correction, not a behavior
  fact: the current coverage is enforced by
  `tests/unit/test_macos_user_path_smoke.py` and by the job itself, and needs
  no ledger copy — what a future session cannot re-derive is that the *older*
  green runs certify nothing, so do not cite a pre-#3860 run of that workflow
  as executed evidence, and do not read #3516's closure as proof the scenario
  was ever run. The scenario rule that followed is
  `agents/runbooks/review-runbook.md` §1c ("Scenario criteria need scenario
  evidence") and §10.
  (Filed as `D-023` under #3860, renumbered to `D-024` when #3829's canary
  entry merged first, and to `D-025` here when #3824's model-bootstrap entry
  took `D-024` on `v3.0.0`. IDs are never reused; each merge keeps the number
  the mainline entry landed with and moves this one on.)
  Source: #3860; #3516; `scripts/macos-user-path-smoke.sh`.

- **D-026** · 2026-08-19 · huddle `change-approach` on PR #3925 (#3860) — **A
  CI gate may *record* an open, parked defect; it may not *fail* on one.**
  `macos-brew-smoke.yml`'s `stable-over-candidate` job covers both
  `conflicts_with` directions. The direction AC4 names (candidate onto an
  installed stable) works, so it stays a hard assertion. The reverse direction
  — stable onto an installed candidate, added beyond AC4 — reproduces **#3853**,
  which is open and whose fix direction is parked by **Q-002**. Hard-failing
  there made the check unlandable by any fix cycle: no change to #3860's branch
  could turn it green, and the only routes to green were softening the
  assertion or taking the packaging side of a parked owner decision. So the
  both-installed outcome is emitted as a `::warning::` naming #3853, while the
  assertions about the state the machine is *left in* (`nyxgpt` on PATH and
  running) stay hard. **The debt:** the PR that fixes #3853 flips that warning
  back to `::error::` + `exit 1` and updates
  `tests/unit/test_macos_user_path_smoke.py::test_the_reverse_direction_records_3853_instead_of_failing_on_it`
  with it; the note is on #3853 itself. Generalise the rule, not the special
  case: a gate that hard-fails on a defect the project has decided not to fix
  yet is not a gate, it is a permanent red that trains readers to ignore it —
  record it loudly and hold the debt where the fixer will find it.
  Source: PR #3925 huddle decision 2026-08-19; run 32202943938; run 32204454740.

- **D-027** · 2026-08-19 · developer agent (#3858) — **A subprocess reachable
  from an HTTP handler is bounded, and an expired bound is a result rather
  than an exception.** The vocabulary is one module,
  `src/nyxgpt/subprocess_bounds.py`, which also carries the enumeration of all
  18 `subprocess.run`/`Popen` call sites in `src/nyxgpt/` and which 10 of them
  a handler can reach — the list is the deliverable, because bounding two
  helpers while a third stays unbounded rebuilds the same trap. A timeout
  comes back as returncode 124 (`timed_out()`), so a status probe degrades
  ("… health check timed out after 5s") instead of 500ing. Two bounds are
  applied deliberately where the tool offers one: `kubectl --request-timeout`
  *and* Python's `timeout=` — **except from inside a Pod, where the kubectl
  flag is withheld**: it lands in client-go's config overrides, which makes
  kubectl skip its service-account fallback and dial `http://localhost:8080`.
  The Python bound is the half that carries the safety property and applies
  unconditionally; the reasoning and its cluster evidence live in
  `bounded_argv`'s docstring. **Handlers stay plain `def`** — the considered
  alternative, `async def` + `run_in_threadpool`, moves the same blocking call
  onto the same threadpool and buys nothing, while making a future forgotten
  `await` block the event loop instead of one worker; bounding the subprocess
  is what actually releases the worker. `canary.status()` also **skips** the
  per-track kubectl reads outside Kubernetes mode (the #3468 guard
  `ops.infra_status()` already had), which removes the calls on a native
  install rather than merely bounding them, and `canary.current_mode()` now
  answers **"unknown"** when its probe times out rather than asserting
  "native" about a substrate nothing could see.
  (Filed as `D-023`, renumbered to `D-025` and then to `D-027` as successive
  merges of `v3.0.0` landed #3829, #3824 and #3860 first. The number came from
  `python3 scripts/agents/lib/ledger_ids.py next D --base origin/v3.0.0` — run,
  not eyeballed. IDs are never reused.)
  Source: #3858; `src/nyxgpt/subprocess_bounds.py` (enumeration + rationale),
  `tests/unit/test_subprocess_bounds.py`.

- **D-028** · 2026-08-19 · owner (issue #3882) — **Assignment is the workflow
  lever; "process by comment" is retired.** A reviewer comments what they
  found and **assigns the issue back**; the developer, on picking it up, moves
  it to In Progress and works it. The comment carries findings, the assignment
  carries the instruction, and **the actor doing the work owns the status
  transition** — which is how people work, and what makes the state on the
  board mean something. Both control tokens are **deleted, not deprecated**:
  `READY_FOR_NEXT_ISSUE` became a `repository_dispatch` (#3917) and
  `RETRY_IMPLEMENTATION` became the assignment itself, so
  `developer_auto_implement.yml` subscribes to no comment event at all. Two
  lanes are claimable by assignment — `Backlog` (new work) and `In Review`
  (rework: REQUEST_CHANGES, huddle decision, conflict round, human override);
  the held lanes stay held (**D-001**/**D-008**) and an unpermitted assigner
  leaves the issue untouched. The stop-without-progress loop guard moved with
  the lever, onto the claim step, and the owner is never gated by it. Same
  move as **D-002** made for issue relationships: native mechanism, never body
  prose. Do not re-introduce a comment token that *starts, resumes or routes*
  work — that is the mechanism behind #3706 and #3790 (~500 runs in two
  hours), not the wording. Tokens that author content (`@improvement`,
  `@acceptance-failure`) or stop the loop (`PAUSE_SPRINT`,
  `CONFLICT_REQUIRES_OWNER_DECISION`) are deliberately kept.
  (Filed as `D-025` under #3882; renumbered to `D-028` when #3860, PR #3925's
  huddle entry and #3858 took `D-025`, `D-026` and `D-027` on `v3.0.0` first.
  The number came from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.)
  Source: #3882; `docs/agent-comment-tokens.md`;
  `tests/unit/test_dispatch_is_an_event.py`;
  `tests/unit/test_comment_token_triggers.py`.

- **D-029** · 2026-08-19 · developer session (#3911) — **The review huddle is one
  workflow run, and its venue is a Slack thread, not the PR thread.** The #3687
  protocol chained three comment-triggered workflows, so each leg's essay *was*
  its trigger: three long structured comments on every huddled PR, and two races
  that had to be guarded after the fact when a duplicated trigger produced two
  positions and two mediations (#3728/#3733, guarded in #3736). `huddle_session.yml`
  runs the whole huddle in one job — bounded rounds (`vars.HUDDLE_MAX_ROUNDS`,
  default 3) of a developer turn and a review turn, then the scrummaster's
  decision — with the conversation in a Slack thread under each agent's own
  identity (#3910) and only the decision plus a collapsed transcript on the PR.

  **Which #3736 guards went, and which did not — so a future session neither
  restores a dead one nor deletes a live one.** Two races were guarded in
  #3736 and they had different fates. The *mediation race* (two mediation runs
  for one huddle) is gone by construction: the legs are steps of one job, so
  there is no second leg to start. The *duplicate-trigger race* (#3728) is
  **not** gone — both of `review_agent_auto_review.yml`'s trigger paths can
  still post a HUDDLE_TRIGGERED comment minutes apart, and those upstream
  guards are unchanged. The concurrency group serializes that second run; it
  does not stop it, and left alone it would open a second Slack thread and
  spend a second huddle. So `huddle_session.yml` opens with a `gate` job that
  calls the surviving `is_primary_marker_comment`: only the round's first
  marker comment owns the round. It is a job, not a step condition, because a
  stand-down spread across ~20 steps is one forgotten `if:` away from running
  the whole huddle anyway. `huddle_decision_dispatch.yml` calls the same
  helper for the same reason on the decision comment.

  **The decision comment must be authored by the scrummaster.**
  `huddle_decision_dispatch.yml` fires only for `vars.SCRUM_AGENT` or the
  owner, and the session's job-level `GH_TOKEN` is the review agent's — so the
  decision turn and the step that posts it override `GH_TOKEN` to
  `SCRUMMASTER_AGENT_TOKEN`. Under the job default the comment lands, reads
  correctly, and dispatches nothing: the #3733 stall, silently. The old
  `scrummaster_huddle_mediation.yml` satisfied that gate by construction (the
  whole run was the scrummaster); consolidation turned a structural property
  into one env override, which is exactly the kind of thing a later edit
  drops, so `test_huddle_session.py::TestTheDecisionCanActuallyDispatch` pins
  both ends together.

  Preserved deliberately: each turn is its own `claude-code-action` invocation
  (one job is not one session — #3687's memorylessness is the reason a fresh
  agent re-reads the thread instead of trusting what it remembers);
  `huddle_decision_dispatch.yml` is untouched and still keys on
  `HUDDLE_DECISION:`; `escalate` runs the same escalation primitives. The
  transcript is assembled from the turn files rather than read back from Slack,
  so the record survives both an outage and Slack's retention setting.
  (That last clause is **amended by `D-040`**: the files are still the floor,
  but the thread is now read back *on top of* them. Kept here as written
  because the reasoning behind the floor is unchanged and still load-bearing.)

  Settling is **sticky**: round N inherits round N-1's answer. Round N gates on
  the previous round's settle output alone, so a huddle that settles in round 1
  skips round 2 — which means round 2's file is never written, which a
  self-only settle check reads as "not settled" and lets round 3 run: two paid
  invocations on a closed question. `huddle_session_probe.py` executes the
  session's real shell bodies on every change (#3775) and re-plants that
  pre-fix gating to prove it can still fail.
  (Filed as `D-027`, renumbered to `D-029` when the merge of `v3.0.0` landed
  #3858's `D-027` and #3882's `D-028` first. The number came from `python3
  scripts/agents/lib/ledger_ids.py next D --base origin/v3.0.0` — run, not
  eyeballed. IDs are never reused.)
  Source: #3911; `.github/workflows/huddle_session.yml`;
  `scripts/agents/lib/huddle_session_probe.py`; `tests/unit/test_huddle_session.py`.

- **D-030** · 2026-08-19 · developer agent (#3853) — **A service's name is read
  from what is installed, never asserted from a constant, and a conflict
  between two formulas is declared by both of them.** Two corrections in one
  fix, and both are the kind a future session would re-derive wrongly.

  (a) *`conflicts_with` is directional* — checked only when the formula that
  declares it is the one being installed. Only the rc formula declared one, so
  candidate-onto-stable was refused and stable-onto-candidate was checked by
  nothing: brew built the keg to completion and failed at `brew link`, which
  is **not a guard**, because the keg stays installed. This closes **Q-002**'s
  practical half — the stable formula now declares its own line's candidate
  (`build_homebrew_artifacts.render_stable_formula`), and both orders are hard
  assertions in `macos-brew-smoke.yml`'s `stable-over-candidate` job. That
  flip **pays the D-026 debt**; a candidate from a *different* release line is
  not nameable by a formula stamped before it existed, and is handled at
  runtime by the `superseded brew services` install step instead.

  (b) *The candidate channel's documented caveat was never only about
  `status`.* `ops install` printed "the service is named `nyxgpt-api@3.0.0rc`,
  so `ops status` reports this component as not running" and the project read
  that as an accepted trade. It was not: `nyxgpt up` gates on the **same**
  probe, so on every rc install it waited its full timeout and exited 2 on a
  healthy stack — and the candidate channel is the acceptance-testing path, so
  the one install flow used to accept a release was the one where `up`
  structurally could not succeed. **A documented caveat about a read-out is
  not a caveat about the commands that gate on it**; when writing one, name
  every consumer or fix the read-out. It was fixed rather than re-documented.

  Which service name each caller resolves, and the sweep that found Homebrew
  to be the only place a published artifact's name varies by channel, are not
  recorded here — they are in `src/nyxgpt/brew_services.py` and
  `tests/unit/test_brew_service_names.py`, per the verification retirement.
  (Number from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.)
  Source: #3853; #3860 run 32202943938 (Q-002's reproduction); D-026.

- **D-031** · 2026-08-19 · developer agent (#3862) — **A branch may be deleted
  only when its content is provably on the target branch, and an issue may be
  closed as `completed` only on the same proof.** "Provably" means blob-level:
  an ancestor, or every path the branch touches already identical there (or a
  subset of it, which is the shape a branch takes when the base has simply
  moved ahead on `agents/LEDGER.md`). Commit ancestry, commit count, `git
  branch --merged`, mergeability, branch age, "no PR exists" and "its issue is
  closed" are **all** unusable, each disproven against a real three-branch set
  on 2026-08-18: on that data the branch whose every byte had landed looked the
  *most* unmerged of the three, so acting on any of them keeps the redundant
  branch and destroys the two holding the only copy of 438 lines of tests.
  Anything not positively proven is reported, never deleted, and every gate
  fails closed — an unreachable check means "keep". This is the criterion
  D-013's event-driven cleanup acts on; it does not reopen the no-scheduled-
  sweep decision, and `.github/workflows/cleanup_stale_branches.yml` (a weekly
  sweep that deleted unmerged branches for being 14 days old) was removed under
  it. The behaviour itself is not recorded here — it is enforced by
  `tests/unit/test_branch_content.py`, `tests/test_branch_hygiene.sh` and
  `.github/workflows/branch-guard-smoke.yml`, per the retirement of the
  verification log.
  The same change adds the **one** exception to "PRs are created only via
  `developer_submit_for_review.sh`" — a draft rescue PR for a branch that
  reached `origin` without one — and that exception is written into `CLAUDE.md`
  § PR Rules, not left implicit here. A rescue draft is a waypoint: it carries a
  marker the developer workflow matches so a reassignment continues on that
  branch, and it is promoted (closing reference, out of draft) only when a run
  passes verification. Without that loop the rescue would trade an orphan branch
  for a stranded draft PR, which is worse — an open PR shields its head branch
  from every cleanup there is.
  **Amended 2026-08-22 (#3862, second round):** the rescue backstop identifies
  its target branches from the **remote and the workspace's refs**, never from
  the working tree. `claude-code-action` mints a fresh
  `claude/issue-<n>-<timestamp>` branch on every invocation, and
  `developer_auto_implement.yml` invokes it six times — including "Deep
  analysis with Claude (Phase 3)", which runs *after* a failed step, i.e. on
  the very path the backstop covers. So at the end of a failed run `git branch
  --show-current` names a decoy minted seconds earlier and the branch holding
  the work is checked out nowhere: run 32291977186 reported "never reached
  origin" and rescued nothing while #3956's only copy sat on
  `claude/issue-3956-20260819-1943` for two days, until the owner merged it by
  hand. Do not reintroduce a working-tree read as the branch source.
  (Filed as `D-030`, renumbered to `D-031` by `ledger_ids.py reallocate
  --write --base origin/v3.0.0` when `v3.0.0` landed #3853's `D-030` first —
  this entry's own machinery, run on the collision it was written for, and the
  first time that renumber was not done by hand. It rewrites the ledger only:
  the four cross-references this change had planted in
  `agents/runbooks/review-runbook.md`, `scripts/branch-guard-smoke.sh`,
  `scripts/closure-gate-smoke.sh` and
  `.github/workflows/delete_branch_on_pr_close.yml` were carried by hand,
  because a tree-wide rewrite cannot tell them from the base's *own*
  `D-030` reference in `macos-brew-smoke.yml`, which must not move. IDs are
  never reused.)
  Source: #3862; `scripts/agents/lib/branch_content.py`;
  `scripts/agents/developer_ensure_pr_exists.sh`.

- **D-032** · 2026-08-19 · developer agent (#3861) — **The native install
  marker records an identity, and reconciliation is a comparison of whole
  identities — never a list of transition pairs.** `mode` (`artifact`/`dev`)
  is now one field of an `InstallIdentity` alongside the service **manager**,
  the **concrete service name per component** (`nyxgpt-api@3.0.0rc`, not
  `nyxgpt-api`), the **version** and the **channel**. This retires the belief
  that a mode identifies an install: two artifact installs are indistinguishable
  by mode, so `_reconcile_install_mode`'s `previous.mode != target` gate saw
  `artifact` -> `artifact` and reconciled nothing while four install identities
  accumulated on the owner's Mac, two of them `keep_alive` services fighting
  over ports 8000/3000. That gate was not lax — it was the strongest check a
  two-value model can support, which is why the model changed and not the
  condition. Three consequences a future session must not undo: (a) the two
  hand-written cleanup halves are **no longer the reconcile path**, replaced by
  one subtraction (`_retire_previous_identity`: the recorded previous
  identity's services **union** what the service managers actually report,
  minus the target's own) — their existence as a *pair* is why there was no
  third case, and the union is why a fourth one nothing recorded is retired as
  well. `_stop_artifact_brew_services` is deleted outright;
  `_remove_dev_launchagents` survives with **one** caller, `uninstall`
  (#3859's teardown), which it acquired on `v3.0.0` while this change was in
  review — it is a teardown helper now, not a transition half, and reconcile
  must not start calling it again;
  (b) an **unknown** previous identity (pre-#3861 marker, malformed, or
  absent) is a possible mismatch reconciled defensively against what the
  service managers actually report, never "the same"; (c) reconcile **stops
  and de-registers, never uninstalls** — removal is a teardown decision
  (#3859); (d) **registration is a launchd fact, and neither of brew's two
  obvious signals reports it.** `brew services stop` exits 0 for a service
  that is registered but not *running* — the `error` state a crash-looping keg
  sits in, which is the state the owner's Mac was in — and reports nothing
  either way about whether the plist survived, so the exit code cannot say
  whether the stop took (trusting it printed `ok Stopped brew service:
  nyxgpt-api` over a service the step then read back as registered, run
  32222041921). **And `brew services list`'s Status column cannot say
  either**: it answers "is it running", never "will launchd start it again" —
  `error`, `stopped` and `scheduled` are all states of a *registered* service.
  On two runs a column-based read also reported a service registered while
  launchd said it was gone (32222041921, 32228088507 — in the latter the
  escalation found no plist to remove and no loaded job); the mechanism was
  left unestablished at the time and is now **measured**, run 32233162053,
  which printed brew's rows through `cat -v`: every state token is
  ANSI-wrapped (`ESC[39mnoneESC[0m`), so an unstripped `none` fails
  `!= "none"` and reads as registered. The competing "the column outlives the
  registration" explanation is **falsified** by that same capture — the
  just-retired service reads `none` with an empty File field within the
  second. Escapes are stripped at the parser now (see (f)). What launchd acts
  on at the next login is
  the **plist** in `~/Library/LaunchAgents`, plus whether the job is loaded, so
  that pair is the test — in `_brew_service_will_restart`, used both to verify
  a stop (escalate, then fail if it survives) and to decide what
  `_discover_native_services` reports. `started` is taken on brew's word and
  `none` with no plist is taken as unregistered; only the states between them
  need the file. A future session must not "simplify" this back to the column:
  it would make `doctor` name a service the last `nyxgpt up` retired and
  prescribe re-running the retire that already worked. Note also that since
  the check moved onto launchd, plain `brew services stop` has de-registered
  every time and the escalation has never fired (runs 32229751239,
  32233162053) — it is a guard, not an observed-necessary path;
  (e) the subtraction runs on **every**
  install, not only when the recorded identity differs. A matching marker
  records what the last install *targeted*, not what is registered now
  (a failed retire, a hand-started service, an install made outside `nyxgpt
  ops`), and gating it on `differences` made `doctor`'s own remedy — "re-run
  `nyxgpt up` … to retire the ones that are not this install's" — a no-op in
  every state `doctor` can fire in. What the comparison gates is the
  *reporting* of the change and the api-venv rebuild; (f) **every literal
  comparison against a brew state goes through
  `brew_services.parse_services_list`, which strips ANSI escapes first.**
  `brew services list` colourises the Status column and the escapes survive a
  pipe (`nyxgpt-api ESC[39mnoneESC[0m` and `nyxgpt-api@3.0.0rc ESC[31merror
  ESC[0m3` were captured verbatim through `cat -v` in run 32233162053, and
  `nyxgpt-api -> ESC[31merror` through `awk` in 32228088507), so a coloured
  token compares equal to nothing — `state == "started"`
  is False for a running service and `state != "none"` is True for one brew is
  not running. That inverts `self_heal`'s `healthy = state == "started"`,
  which `nyxgpt up`'s exit gate rides on, plus `LIVE_STATES`, `superseded`'s
  `registered_only` filter and the ollama/`native_running` reads. The guard is
  at the parser, not at each reader: readers receive a state they did not
  fetch and cannot know whether it was coloured, so a per-site guard would
  have to be re-added for every reader added later. Scope note: this is the
  runtime half of #3853, and it now sits **beside** that issue's packaging
  half rather than waiting on it — **D-030** landed on `v3.0.0` while this was
  in review, declaring `conflicts_with` in both directions and paying
  **D-026**'s debt. The two are defence in depth, not duplicates, and neither
  makes the other removable: packaging refuses the *second install* on a
  machine whose brew is up to date with both formulas, while this retires
  what is already registered — on every machine that reached the bad state
  before that declaration shipped, through a hand-installed keg, or through a
  local `file://` tap whose checked-in formulas that script never stamps.
  `_stop_superseded_brew_services` (D-030's install step) overlaps this
  subtraction by design since #3861 rather than covering a case reconcile
  cannot see; what it adds is a second stop attempt sited immediately before
  the api/web installs. The fact that keeps the two from collapsing into one
  was **measured** while merging them (run 32227410541): Homebrew checks
  `conflicts_with` against the **linked** keg, not the installed one — its own
  refusal says "Please `brew unlink nyxgpt-api@3.0.0rc` before continuing".
  So D-030's declaration stops a second *linked* install; two kegs on one
  machine stay reachable, and reconcile is what has to handle them. That is
  also how the evidence job now stages the two-keg state, which no edit to the
  tap could do safely — the declaration spans two lines, so a line-wise strip
  breaks the formula. Extends **D-009**, whose "installing
  either mode over the other stops the other's services" now reads as the
  identity comparison's special case.
  Source: #3861; `src/nyxgpt/install_mode.py` (`InstallIdentity`);
  `tests/unit/test_install_identity.py`;
  `.github/workflows/macos-brew-smoke.yml` (`stable-over-candidate`).

- **D-033** · 2026-08-19 · developer-agent (#3867) — `nyxgpt cloud deploy --os
  {auto,linux,macos}` is the single provisioning entry point for both target
  OSes: it renders the target's bootstrap and delivers it to the instance over
  the wrapped SSH path itself. `nyxgpt cloud user-data` stays as the renderer's
  own command for the first-boot `user_data` case a deploy cannot serve and for
  the CI jobs that execute a rendered bootstrap; it is no longer a user-facing
  provisioning instruction, and there is one renderer behind both.
  Source: #3867; `src/nyxgpt/cloud_deploy.py` (`resolve_os_family`,
  `render_provision_script`, `provision_remote_command`); `docs/cloud.md`
  §EC2 Mac targets; `.github/workflows/cloud-target-os-smoke.yml`.

- **D-034** · 2026-08-19 · owner acceptance (#3811), implemented by the
  developer agent — **nyxGPT files a support ticket itself; the GitHub
  compose page is a fallback, not the surface.** The owner failed the
  previous fix in acceptance because Support → File an Issue still handed the
  user to `github.com/.../issues/new`. The intake is now a form in the chat
  and `POST /api/v1/support/tickets`, which creates the labeled issue from
  the running install and answers with its number and URL; the UI shows the
  filer their own ticket. This answers **Q-006**'s credential question in the
  only way that leaves every filer able to report something: file with
  `[github] pat` when it is configured (the owner's install, any operator's),
  and offer the prefilled GitHub form when it is not — the one case the
  product genuinely cannot cover. A hosted intake that would remove even that
  case remains the owner's to decide and is not foreclosed. The `Support`
  label is **read back from the created issue** rather than assumed: GitHub
  drops `labels` silently for a token without push access, which is #3810's
  failure mode from the other side; `support_intake_guard.yml` remains the
  repair. Evidence is executed, not inspected — `tests/test_support_intake_live.sh`
  files through the real API against a stub GitHub and injects the
  dropped-label, no-credential and refusal cases.
  Source: #3811; `src/nyxgpt/support.py`; `src/nyxgpt/app.py`;
  `web/src/components/SupportTicketDialog.tsx`;
  `.github/workflows/support-intake-smoke.yml` (`files-a-ticket`).
  Filed as **D-033** on this branch; renumbered on the merge into `v3.0.0`,
  where #3867 had already allocated that number. IDs are never reused.

- **D-035** · 2026-08-19 · developer agent (#3950) — **"Dev mode on a cloud
  target" means shipping the working tree to the instance; it does not mean a
  cloud Terraform or Kubernetes deployment.** The two halves of this are what
  a future session would re-derive wrongly, because the flag names look like
  they compose and they do not.
  (a) *What was built.* `nyxgpt cloud deploy --dev` copies the operator's tree
  over the deploy's own SSH connection (git's file list — tracked plus
  new-not-ignored, so **uncommitted edits go**; never `.git`) into
  `~/.nyxGPT/src`, installs it editable there, and runs `ops install --dev` on
  the box, so `ops.dev_checkout_root()` on the *instance* answers the shipped
  directory. The refusal without a checkout is `ops.dev_checkout_root()` — the
  local paths' own predicate, reached through a new public forwarder rather
  than re-implemented, because two definitions of "is this a checkout" is how
  one path refuses a tree the other accepts. `--dev` is **not** carried
  forward by `resolve_plan` although every other recorded choice is: the
  others describe the instance's configuration, this one describes where a
  single run got its code, and inheriting it would re-ship whatever tree
  happened to be checked out under a command every operator reads as the
  artifact path. Not exposed on `POST /cloud/deploy`: the API host has no tree
  to ship (D-017).
  (b) *What "cloud mode" does and does not mean.* `--terraform` and
  `--kubernetes` are **local install-mode** flags of `ops install`; neither is
  a mode of `nyxgpt cloud deploy`, which deploys the native stack to one EC2
  box. So there was no "Terraform dev mode on cloud" to add — cloud uses
  Terraform for the *substrate* only — and no Kubernetes cloud target for
  `--dev` to modify. The latter is **unbuilt work, not a scope decision
  against it**: `product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md` (#3506)
  rejects a managed **EKS control plane** while explicitly calling for the
  existing `k8s/*.yaml` on a single-node k3s cluster on that instance. #3950's
  thread contains a retracted comment asserting the opposite from the Options
  section alone; the Decision section is what binds.
  (c) *`--dev` is Linux-only, and refuses rather than ignores.* The `--os
  macos` target (**D-033**, merged alongside this) renders the EC2 Mac
  bootstrap, which installs published Homebrew formulas and has no
  working-tree source. `resolve_plan` therefore rejects `--dev --os macos`
  before the substrate is applied: honouring the combination by rendering the
  Mac script anyway would install a published release to an operator who
  believes they are testing their tree, which is the exact defect this issue
  was filed about — reachable, without the refusal, by combining two flags
  that are each correct alone.
  The behaviour itself is not recorded here — it is enforced by
  `tests/unit/test_cloud_deploy_dev_mode.py` and, on a real machine, by
  `.github/workflows/cloud-dev-deploy-smoke.yml`, per the verification
  retirement. What that smoke found and inspection did not: `is_file()` drops
  every symlink-to-a-directory in `src/nyxgpt/resources/` (#3621), which
  silently shipped a checkout whose `ops install` could not find its own
  runtime data.
  (Allocated D-033 from `ledger_ids.py`; renumbered to D-034 when a merge from
  v3.0.0 showed #3867 had taken that number, then to D-035 when the next merge
  showed #3811 had taken *that* one. IDs are never reused and every entry
  stands.)
  Source: #3950; #3506; extends **D-009**; interacts with **D-033**.
- **D-036** · 2026-08-19 · owner (#3948) — **`--local` is the default locality
  for `ops install --terraform/--kubernetes`, not a requirement**, and it stays
  accepted as an explicit no-op so existing scripts and docs keep working. It
  had been mandatory while also being the only legal value, which made the CLI
  demand the one possible answer. `--cloud` is still refused by these flags,
  but the refusal (and the help) now says what it means: *this flag* has no
  cloud target — cloud deployment is `nyxgpt cloud infra apply` +
  `nyxgpt cloud deploy`. The old "not yet implemented" wording read as "nyxGPT
  cannot deploy to a cloud target at all", which was never true. One shared
  constant (`ops.CLOUD_DEPLOY_POINTER`) backs both the error and the help so
  they cannot drift, and `tests/unit/test_cli_locality_help.py` reads the
  requirement claims out of the generated help and compares them against what
  `_resolve_locality` enforces — a wording grep would not have caught this
  drift and does not catch the next one.
  Source: #3948; `src/nyxgpt/ops.py` (`_resolve_locality`);
  `src/nyxgpt/cli.py` (`_add_install_arguments`);
  `.github/workflows/cli-locality-smoke.yml`.
  Filed as **D-033** on this branch and renumbered three times on the way
  into `v3.0.0` — #3867 took `D-033`, #3811 then took `D-034`, and #3950 then
  took `D-035`, each while this PR was in review. IDs are never reused and
  every entry stands.
- **D-037** · 2026-08-19 · developer agent (#3956) — **A decision record is a
  requirement even where no issue transcribed it, and "the cloud target" is a
  *place the existing install mode runs*, not a second install mode.**
  `DECISION_AWS_COMPUTE_SUBSTRATE.md` (#3506, owner-approved 2026-08-04) chose
  EC2 single-box **with the `k8s/*.yaml` manifests optionally layered on a
  single-node k3s cluster for canary** — the question put to the owner was EC2
  vs EKS, i.e. *how* to host Kubernetes on the cloud target. #3513's
  acceptance criteria never mentioned k3s, Kubernetes or canary, so what
  shipped had no `--kubernetes` flag and canary rollout was unavailable on the
  cloud target entirely: the capability the substrate was being chosen *for*.
  The correction that generalises is the reading rule, not the flag — when a
  decision record and the issue implementing it disagree, the record is the
  higher authority and the gap is a spec-to-issue transcription failure, which
  is where the retrospective should count it rather than as a developer miss.

  Two things a future session would otherwise re-derive wrongly, both settled
  by measurement rather than by preference:

  (a) *The apiserver binds the node's **private** address, not loopback.* The
  obvious reading of "#3503 exposes nothing but TCP 22" is `--bind-address
  127.0.0.1`, and it is wrong: k3s builds the in-cluster `kubernetes` Service
  endpoint from the advertise address, so pinned to loopback every Pod that
  talks to the API server dials its own loopback. k3s's *default* is equally
  wrong in the other direction (0.0.0.0 is the instance's public NIC, refused
  by the security group but listening). The private address is the only
  correct answer, and `--tls-san` plus a kubeconfig rewrite are what make it
  usable — k3s writes `server: https://127.0.0.1:6443` regardless of
  `--bind-address`.

  (b) *`--disable=traefik --disable=servicelb`, but **never**
  `--disable=local-storage`.* The first two are the ingress controller and the
  `Service: LoadBalancer` implementation #3506's premise says the manifests
  need neither of. The third looks like more of the same trimming and is a
  trap: the Cassandra and Ollama StatefulSets declare `volumeClaimTemplates`
  with no `storageClassName`, so they bind through the cluster's default
  StorageClass, which on k3s is `local-path` — disabling it leaves both Pods
  Pending on unbound PVCs, which reads as a capacity problem and is not one.

  (c) *Switching substrates is a transition, and both directions fail
  silently if it is not.* A `--no-kubernetes` re-deploy that merely renders
  the native script leaves k3s and the `Restart=always` access bridge holding
  127.0.0.1:8000/3000, so the new native services never bind and *every*
  probe that would notice -- the install's health wait, the deploy's own
  check, the tunnel -- is answered by the cluster the operator just asked to
  leave, while `deploy.json` and the dashboard both say "native". Each
  provisioning script therefore retires the substrate it replaces before
  installing its own, guarded on existence so a first deploy runs neither.
  The reverse direction failed *loudly* instead, which was no better: its
  refusal prescribed `nyxgpt ops down` on the instance, a command no wrapped
  `nyxgpt cloud` surface can run, leaving `cloud destroy` as the only exit.

  What the change actually does is not recorded here — it is enforced by
  `tests/unit/test_cloud_deploy_kubernetes.py`,
  `tests/unit/test_k3s_image_import.py`,
  `tests/unit/test_k8s_access_bridge_doctor.py` and
  `.github/workflows/k3s-cloud-smoke.yml`, which executes the deploy's own
  bootstrap text on a real cluster, per the verification retirement. Related:
  **D-023** (canary reads the canary track's own Pods) is what makes the
  cloud canary surface meaningful; **D-017** is why `nyxgpt cloud canary` is a
  CLI command and the dashboard only *reports* which substrate is running.
  Source: #3956; `product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md`;
  #3513 (the issue that under-specified it); `docs/cloud.md`
  §Kubernetes on the instance.
  Filed as **D-033** on this branch, then **D-034**, then **D-035**, then
  **D-036** on the three earlier merges into `v3.0.0`; renumbered again here,
  #3867, #3811, #3950 and #3948 having taken those four in turn while this PR
  was in review. Number from `python3 scripts/agents/lib/ledger_ids.py next D
  --base origin/v3.0.0` — run, not eyeballed, on each pass. IDs are never
  reused.
  Supersedes the reading in **D-035**(b) that there is "no Kubernetes cloud
  target": that entry correctly called it unbuilt work rather than a scope
  decision, and this PR builds it. It also supersedes D-035(b)'s "no Kubernetes
  cloud target for `--dev` to modify" — the two flags **do** compose here:
  `--kubernetes` chooses the substrate and `--dev` chooses where the images
  come from, so `cloud deploy --dev --kubernetes` renders
  `ops install --kubernetes --local --dev` on the instance. D-035(a) still
  holds unchanged: `--dev` is not *carried forward* by `resolve_plan` on either
  substrate. **D-036** (#3948) landed while this was in review and removed the
  `--local` requirement these scripts were written against; the rendered
  command still passes `--local` explicitly, which is now the accepted no-op,
  and the `--cloud` refusal's pointer (`ops.CLOUD_DEPLOY_POINTER`) carries this
  PR's `--kubernetes` half so both the help and the error name the whole cloud
  path.

- **D-038** · 2026-08-19 · developer agent (#3814) — **A symptom repair that
  lets a broken interpreter keep going is worse than a refusal, and #3753 and
  #3788 were never two defects.** Both were one `python@3.12` keg whose
  `pyexpat` would not load: `plistlib` imports it, so `platform.mac_ver()`
  answered `('', ('', '', ''), '')` (#3753); pip's vendored distlib reaches it
  through `xmlrpc.client`, so pip's eager pre-import swallowed the dlopen
  error and its audit hook re-raised `No module named
  'pip._internal.operations.install.wheel'` (#3788) — a module that was
  present the whole time. Three release candidates were spent debugging pip.
  The **correction to retire**: the account written into
  `homebrew/nyxgpt-api.rb`, its tap template and `docs/homebrew.md` that "this
  pip installation cannot import its own wheel installer". It is wrong, it
  reads as a diagnosis, and it is what the next reader would have built on.
  Three things follow, and the third is the general one:
  (a) *nyxGPT refuses rather than repairs.* The api formulas preflight the
  resolved `python@3.12` before any work — inside `install`, because Homebrew
  calls that only after dependencies are resolved, so it is the interpreter
  the build will really use — and `odie` with the keg path, the loader's error
  verbatim, the measured macOS/SDK pair and the operator action. It fails
  **closed**: anything but the ok line refuses, including no output at all.
  (b) *the `sitecustomize` mac_ver shim was narrowed, not deleted.* It now
  declines to repair when `plistlib` will not import and says why; what is
  left for it is the case it was written for, a healthy interpreter and an
  unreadable SystemVersion.plist. Making pip *start* on a broken keg is what
  carried rc12 past the fault and into an opaque failure.
  (c) *the environment condition is describable, not exotic:* Homebrew tags
  bottles by macOS **major** version, so a machine behind the **minor**
  release its bottle was built against gets an extension linked to an expat
  the system does not export. Neither `brew reinstall python@3.12` (same
  bottle) nor `brew reinstall --build-from-source python@3.12` (pyexpat will
  not compile against the newer SDK) fixes it — updating macOS did, on the
  reporting machine. Do not add a third workaround for a symptom of this.
  The behaviour itself is not recorded here — it is enforced by
  `tests/unit/test_build_homebrew_artifacts.py` (the preflight section),
  `validate_interpreter_preflight` in `scripts/build_homebrew_artifacts.py`
  (which refuses to publish a formula that starts the brewed interpreter
  without one), and `macos-brew-smoke.yml`'s "Reproduce the broken pyexpat…"
  step, which injects an unloadable `pyexpat` at its realpath and proves both
  halves with the real `brew install` — per the verification retirement.
  Number from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.
  Source: #3814; #3753; #3788; **D-012** (the first principle this is the
  motivating incident for).

- **D-039** · 2026-08-20 · developer agent (#3971) — **A red or pending head is
  not reviewable, and deciding that is not a review invocation's job.** Two
  rules, one gate. *Red*: `developer_submit_for_review.sh` refuses to submit
  while a **required** check on the head has concluded failure (exit 3, before
  any GitHub write — a refusal that has already opened the PR and requested the
  reviewer is not a refusal), and a head that turns red after submission is
  handed back to the developer by assignment (**D-028**) with no invocation
  spent, no verdict posted and no REQUEST_CHANGES cycle counted. *Pending*: the
  review trigger **waits**; a PR that is merely mid-CI is never bounced back.
  Measured cause, window 2026-08-13..19: ~36% of 240 blocking findings (87, on
  39 of 65 rejected items) reported machine-observable check state the PR page
  already displays, at ~7.2M + ~10.7M tokens per reject/re-fix round-trip.

  Three things a future session would otherwise re-derive wrongly:

  (a) *The required set is a named list, and **absent is not pending**.*
  `.github/required-checks.txt` classifies every `pull_request` job as required
  or explicitly not; "every check on the head" would deadlock the gate against
  the review's own check run — the shape **Q-005** records for the merge path,
  which this change deliberately does not touch. A required check that is not
  *attached* to the head is neither waited for nor counted against it, because
  a path filter already decided it does not apply; that is what makes the list
  safe to extend. `tests/unit/test_required_checks.py` fails in both directions
  so a new smoke workflow cannot default to "not a gate" silently.

  (b) *It waits rather than subscribing to an event, on purpose.* The obvious
  wake-up — re-trigger on `check_suite: completed` — cannot be used: GitHub
  runs the **default branch's** copy of a workflow for events not attached to a
  pull request, and this project's default branch moves only at a release
  ceremony (**D-003**), so the trigger would not exist until the next release
  and would run a stale definition forever after (the trap review-runbook §5a
  records for `gh workflow run` without `--ref`). An idle ubuntu runner for the
  wait costs cents against ~18M tokens for the round-trip it replaces.

  (c) *The gate fails **open**.* An unreadable check list or a GitHub blip
  yields `unknown`, and every caller proceeds exactly as it did before the gate
  existed. Failing closed would jam every submission and every review in the
  pipeline to prevent a rare wasted review — the same trade
  `review_agent_auto_review.yml`'s merged-PR guard documents.

  (d) *A failing step's reason reaches the classifier through a **file**, not
  through the job log.* `developer_auto_implement.yml`'s Phase 1 harvests the
  text it classifies from GitHub's per-job logs API **mid-run**, which
  routinely returns nothing (the log is written on completion), and then falls
  back to the failed step's **name**. So `classify_error` gets
  `"Submit PR for review"`, matches no signature, and answers `unknown` —
  which the pipeline reads as *non-retriable* and escalates to the owner. This
  gate's own first refusal proved it: run 32419181728 refused correctly and the
  round still ended in a FATAL owner DM instead of the continuation AC1
  promises. The fix is `write_agent_error_detail` /
  `read_agent_error_detail` (`gh_project.sh`): a script that knows why it
  failed leaves the reason on the runner the classifier is standing on, and
  Phase 1 reads that **before** either guess. **Any future agent script with a
  known failure reason should do the same** — every signature in
  `classify_error` is otherwise reachable only by luck of log timing.

  The **override** is the one CI-adjacent thing still asked of the reviewer:
  `--ci-override "<reason>"` submits over a red check and writes the reason
  into the PR body under `<!-- nyxgpt-ci-override -->` as a *claim to verify*,
  never an accepted exception. Behaviour is not recorded here — it is enforced
  by `tests/test_reviewable_head_gate.sh` and
  `.github/workflows/reviewable-head-smoke.yml` (which injects the pre-fix
  condition: the same red head with the check unlisted, proving the refusal is
  the named list's doing), per the verification retirement.
  Number from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.
  Source: #3971; `docs/reviewable-head-gate.md`;
  `agents/runbooks/review-runbook.md` §3;
  `agents/runbooks/developer-runbook.md` §7a.

- **D-040** · 2026-08-21 · developer agent (#3911 reopened) — **A feature whose
  every execution has taken its degradation path has not been executed. The
  huddle archive now reads the thread back, and a dead session says so in the
  thread as well as on the PR.**

  #3911 closed on 2026-08-19 claiming executed evidence for a live huddle. No
  Slack user token existed until 2026-08-20, so `get_channel()` returned
  `NullChannel` on every run that had ever happened: no thread, no turns, no
  permalink. Every test and the `huddle_session_probe.py` smoke ran that same
  path, all green, and what they were green about was #3910's *degradation
  contract* — correct, and not the feature. The adapter had meanwhile shipped
  with `_call` sending JSON to every method, which Slack accepts on
  `chat.postMessage` and refuses on `conversations.replies` and
  `chat.getPermalink`: two of four operations dead for two months, invisible
  because a broken integration and a quiet one produce the same silence
  (fixed #3974, guarded #3975). **Generalised: when a component is specified to
  degrade quietly, "the tests are green" is evidence about the quiet path
  only. Something must execute the loud one, or the feature is unverified by
  construction.**

  Three changes follow from that, all on #3911's own criteria:

  (a) *The archive reads the thread back.* `read()` was the operation that had
  shipped broken, and `huddle_session.yml` — the workflow that supposedly
  depended on it — never called it. The turn files remain the **floor** (they
  are written on the runner, so the record survives a Slack outage, which is
  D-029's unchanged reasoning); the read-back is added on top, guarded by
  `|| true`, because the files hold only what the three agents wrote and the
  thread is the sole record of anyone else in the huddle — the owner weighing
  in, a human correcting a premise — which otherwise evaporates with Slack
  retention. Amends D-029's transcript clause.

  (b) *A failed session tells the thread, not only the PR.* The criterion is
  that a dead session leaves no "half-written Slack thread". A `HUDDLE_FAILED`
  marker on the PR makes it recoverable for someone reading the PR, while the
  thread just stops — indistinguishable from a huddle still thinking, and the
  thread is where a huddle is read.

  (c) *Read-back turns are named by role.* A user-token message returns an
  opaque `U…` id, so the archived thread would name its speakers
  `**U09ABCDEF:**` three times: the reason for posting under three identities,
  lost at the moment the record is meant to outlive Slack. `identities()`
  resolves them via one `auth.test` per configured token, at archive time
  only. It is deliberately **not** on the `Channel` interface — it exists
  because Slack labels its own messages badly, and a replacement transport
  would have to implement a method it has no use for, so callers ask with
  `getattr`. Matching is keyed on the account id, never the display name,
  which Slack attaches only sometimes.

  Evidence: `huddle_session_probe.py --live` runs the session's real `run:`
  bodies against the live channel with the three real user tokens, inverting
  the degradation contract the way `scripts/slack-huddle-smoke.py` does for
  the adapter — a warned no-op is a failure there. It runs as the `session`
  job of `slack-huddle-smoke.yml`, dispatch-only for the same reason that
  file's other job is (each run leaves a thread, and `chat:delete` is
  deliberately ungranted). Still not covered, and named rather than implied:
  the six model turns are canned prose, and `huddle_decision_dispatch.yml`
  firing needs a real decision comment on a real PR, so it stays pinned by
  `test_huddle_session.py::TestTheDecisionCanActuallyDispatch`.
  Number from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.
  Source: #3911; `.github/workflows/huddle_session.yml`;
  `.github/workflows/slack-huddle-smoke.yml`;
  `scripts/agents/lib/huddle_session_probe.py`;
  `scripts/agents/lib/huddle_channel.py`.

- **D-041** · 2026-08-21 · developer agent (#3911, owner scope addition) —
  **An escalation DM is signed by the agent that raised it, and the raiser is
  named from `AGENT_ROLE` alone — never inferred. The merge-conflict channel
  is a repo variable, and deliberately not the huddle's.**

  Owner direction, 2026-08-21: the two acceptance criteria left over from
  #3910 move to #3911 rather than staying with a closed issue.

  (a) *Attribution.* `notify_human_escalation` posted every DM with the single
  `SLACK_BOT_TOKEN`, so a self-heal FATAL and a review 3-cycle breaker arrived
  from the same sender and the owner had to read the body to learn which agent
  was stuck. It now posts with the raising agent's user token — the same three
  `SLACK_USER_TOKEN_{DEV,REVIEW,SCRUM}` secrets #3910 filed and D-040's huddle
  spends.

  (b) *The role is explicit or absent.* It comes from `AGENT_ROLE` and from
  nothing else. Inference was considered and rejected: this repo's workflow
  names are not uniform (`Notify Merge Conflicts` and `Claude Code Review`
  both escalate as the review agent and neither says so), and a wrong guess
  signs an escalation with the wrong agent's name — strictly worse than the
  unattributed bot DM it replaces. An unrecognised role attributes nothing.
  **The default lives in the role-owned script, not the workflow**, because
  the *report* has an author independent of the runner: `developer_pull_next_
  issue.yml` runs `scrummaster_dispatch_next.sh` under `DEVELOPER_AGENT_TOKEN`,
  and a dispatch-block report is the scrummaster's wherever it executes.

  (c) *Attribution never costs a notification.* An unset role, an
  unconfigured token, or a user token Slack refuses all fall back to
  `SLACK_BOT_TOKEN`, and only if that also fails does the DM degrade to
  comment-only. #3695's delivery guarantee outranks this entry's sender name;
  a nicer sender that loses the escalation is the wrong trade. The message's
  "Raised by" line and the `:envelope:` marker comment name the agent on
  either path, so attribution survives the fallback even when the sender does
  not.

  (d) *The merge-conflict channel is configuration.* It was the literal
  `C0AANK4KDM0` in `notify-merge-conflicts.yml`. It is now
  `vars.SLACK_CONFLICT_CHANNEL`, with that id kept only as the `||` fallback
  so setting the variable is what changes behaviour and not setting it changes
  nothing. It is **its own** variable, not `SLACK_HUDDLE_CHANNEL`
  (`C0ABH478QC8` / `#nyxagent-dev`): conflict notices and huddle deliberation
  are different audiences and must diverge without a code change. A later
  "tidy-up" collapsing them onto one variable would read as a simplification
  and would move every conflict notice into the huddle's reading channel —
  `TestTheConflictChannelIsConfiguration` exists to stop it.

  Evidence, applying D-040's own lesson: whether Slack will let an agent's
  user token DM the owner is a property of the workspace that no stub can
  answer, and the function swallows Slack failures by design, so a refused
  token would leave attribution dead with every test green. `scripts/slack-
  escalation-smoke.sh` asks the live API as the `escalation-identity` job of
  `slack-huddle-smoke.yml` — **with no bot token in the step's environment**,
  so a marker comment can only mean that agent's own token was accepted — and
  a `--prove-it-fails` half plants an invalid agent token and requires the bot
  fallback to carry the DM. Dispatch-only: each run DMs the owner for real.
  The decision itself (which token, which fallback, which record) is Test 17b
  of `tests/test_gh_project_lib.sh`, which `assignment-dispatch-smoke.yml`
  already runs on a real runner on every change to `gh_project.sh`; the wiring
  — a role declared whose token is never passed, which degrades silently to
  the old behaviour — is `tests/unit/test_escalation_attribution.py`.
  Number from `python3 scripts/agents/lib/ledger_ids.py next D --base
  origin/v3.0.0` — run, not eyeballed. IDs are never reused.
  Source: #3911 (owner comment 2026-08-21), #3910;
  `scripts/agents/lib/gh_project.sh`; `scripts/slack-escalation-smoke.sh`;
  `.github/workflows/notify-merge-conflicts.yml`;
  `.github/workflows/slack-huddle-smoke.yml`.

- **D-042** · 2026-08-22 · owner — **An acceptance failure attributable to one
  issue reopens that issue as the signal, and the rework is a separate issue
  that blocks it. The machinery does not implement this yet.**

  Owner statement of the current standard, 2026-08-22. Two cases:

  (a) *Attributable to one issue* (the common case). The original issue is
  **reopened and assigned to the scrummaster** — the reopen is the signal that
  *this* issue did not pass acceptance, and its last comment says why. A
  **new** issue is created from that comment and recorded as **blocking** the
  original. The new issue is what gets worked. The reopened original sits in
  `Acceptance Failed` until the derived issue reaches `For Release`, at which
  point the original goes to `For Release`, is assigned to the owner, and is
  closed.

  (b) *Spanning several issues, or fitting none.* A new issue is filed with
  **no** relationship — open, assigned to the scrummaster, parked in
  `Acceptance Failed`. It goes through the process exactly like a brand-new
  issue. The lane placement is only batching, so rc rounds stay legible.

  **Why both shapes exist, in the owner's words:** a separate issue can be
  counted as an acceptance failure rather than riding along as a feature with
  lots of additional work, and counting acceptance failures is useful — but
  logging the total effort a feature required to reach acceptance on one issue
  is *also* useful. The split above gets both: the derived issue carries the
  statistic, the reopened original accumulates the effort trail.

  **This is not what the code does today, and the divergence is the whole
  reason this entry exists.** `handle_acceptance_failure.yml` creates the
  derived issue and writes the native blocked-by edge (both halves of (a)
  already work), but it deliberately leaves the original **closed** — its own
  header says "The original is never reopened or relabeled". Meanwhile D-008
  reads an **open** issue in `Acceptance Failed` as *held rework* and the
  drain gate releases it to `Backlog` for a developer, and
  `promote_accepted_features.sh` skips open items there as "held rework, not a
  promotion candidate". So a reopened original is currently dispatched to a
  developer that has nothing to implement, and can never be promoted. Observed
  2026-08-22: the gate released #3835 (three open blockers) and #3829 (one)
  into `Backlog`.

  **The single point of failure is `handle_acceptance_failure.yml` itself.**
  If it fails, no derived issue is created and no blocking edge is written, so
  the reopened original sits forever with nothing pointing at it. That is not
  hypothetical: its run for the owner's 2026-08-22T02:48Z comment on #3829
  **failed** (run `32547268563`, exit 1) and the report survived only as
  comment text. Any implementation of this standard has to make that handler
  fail loudly rather than silently.

  **Revisit when:** the owner says so. This entry records a standard they have
  changed before and expect to weigh again — the trade between counting
  acceptance failures and logging whole-feature effort is genuinely two-sided,
  and "which shape gets the better fix out of the developer agent" is not yet
  answered. Do not treat it as settled forever; do not re-derive it from the
  code either, because the code currently disagrees with it.
  Source: owner statement 2026-08-22 (this session); `CLAUDE.md` §Issue
  Relationships; D-008; `.github/workflows/handle_acceptance_failure.yml`;
  `scripts/agents/lib/drain_gate.py`; `scripts/agents/promote_accepted_features.sh`.
- **D-043** · 2026-08-22 · owner (#3995) — **nyxGPT allocates the EC2 Mac
  Dedicated Host.** `nyxgpt cloud deploy --os macos` with no `--host` prices the
  host live from the AWS Pricing API, discloses the rate, the 24-hour minimum
  and the moment it becomes releasable, and allocates it after a typed
  confirmation (`--yes` skips the typing, never the disclosure). `nyxgpt cloud
  destroy` terminates the Mac immediately and defers **only** the host release
  — which AWS rejects inside the 24-hour window — to a one-shot EventBridge
  Scheduler schedule (`ActionAfterCompletion=DELETE`) driving a Step Functions
  state machine that calls `ReleaseHosts` and reports to Slack over an
  EventBridge Connection holding the existing bot token. No Lambda, no AWS
  Chatbot, no new Slack app.
  Two AWS behaviours make the naive version silently wrong and are handled:
  Slack answers `invalid_auth`/`channel_not_found` with **HTTP 200 and
  `"ok": false`**, and `ReleaseHosts` answers a still-scrubbing host with
  **HTTP 200 and the host in `Unsuccessful`** — so neither can be detected by
  status code or by a Step Functions `Retry`.
  Source: #3995; `src/nyxgpt/cloud_mac.py`; `terraform/aws/mac`,
  `terraform/aws/mac-release`; `docs/cloud.md` §EC2 Mac targets.

## Parked

- **P-001** · 2026-08-10 · owner — Intelligent test selection: scoping CI and
  the developer verification loop's test runs to a change's path/dependency
  impact (script/workflow-only diffs skip the full pytest + vitest suites, etc.).
  Reason: it is the largest recurring runner-spend multiplier in the pipeline —
  the dev workflow repeats both full suites on up to three fix attempts for every
  issue — but it is not v3.0.0 scope.
  Revisit when: ~~Sprint 9 (nyxAgent-focused) grooming~~ — **UNPARKED by the
  owner 2026-08-18**, ahead of Sprint 9, on the runner-spend evidence below.
  Filed as its own issue; this entry stays as the record of why it was parked.
  Evidence at unpark: 2,243 runner-minutes over 30 days, led by
  `security-scan.yml` (187 runs) and `ci-tests.yml` (173) — the full tree on
  every push, every review and every dev fix attempt.
  Source: `product_management/AGENTIC_SDLC_DESIGN.md` §9a; owner directive
  2026-08-18.

- **P-002** · 2026-08-09 · owner — A global hard budget circuit breaker (fixed
  caps on expensive invocations per unit time) is **rejected, not pending**. Do
  not re-propose it as a fix for spend incidents.
  Reason: a threshold constant is itself the kind of scripted guard §9 rules out;
  the intelligent watcher reading spend telemetry provides the same protection
  with judgment instead of a constant.
  Revisit when: the intelligent watcher is in place and demonstrably fails to
  catch a spend runaway.
  Source: `product_management/AGENTIC_SDLC_DESIGN.md` §9a.

- **P-004** · 2026-08-20 · owner — `GH_TOKEN_NYXAGENT` and `QA_AGENT_TOKEN`
  (Actions secrets, and `[github]` keys in the owner's `config.ini`) are
  **groundwork for the future nyxAgent product**, not nyxGPT configuration.
  Nothing in this repository reads either one, and that is correct: they are
  deliberately not declared in `example.config.ini`, not in
  `SECRETS_SYNC_MANIFEST`, and **not to be removed**.
  Reason: reconciling config.ini against `example.config.ini` on 2026-08-20
  found seven undeclared keys and asked, of each, "is this live or dead?" For
  these two the honest repo-wide answer is "no reader anywhere", and the
  conclusion that invites — delete the key, revoke the secret — is wrong.
  The same sweep found `[paths] compose_file`, where "no reader" *did* mean
  retired (#3621), so the distinction is not inferable from the code.
  `RELEASE_TRACKING_TOKEN` is a third unread secret whose disposition is not
  settled; it is not covered by this entry.
  Revisit when: nyxAgent has its own repository and configuration, at which
  point these move there rather than being deleted.
  Source: owner in session, 2026-08-20; `example.config.ini` §`[github]`.

## Open questions

- **Q-001** · 2026-08-14 · developer-agent (#3774) — Should the ledger be
  enforced beyond the structural test shipped with #3774 — e.g. CI warning on
  verifications whose `Re-verify when` condition names a file that has since
  changed, or on an entry count that has outgrown "cheap to read"?
  Needs: ~~owner decision on how much enforcement is wanted~~ — **ANSWERED
  2026-08-18**: the owner directed that ledger size be actively managed, not
  merely advised. The ledger is read in full on every agent run, so its growth
  is a per-run cost; it is to be split into a hot ledger (decisions binding on
  current work) and an on-demand archive, filed as its own issue. The
  The `Re-verify when` staleness half is **closed** by the 2026-08-18
  retirement: there are no verifications left to go stale.
  Blocks: nothing yet.

- **Q-002** · 2026-08-18 · owner acceptance (#3853) — Why did
  `conflicts_with` not prevent `nyxgpt-api@3.0.0rc` from installing alongside
  `nyxgpt-api` 2.1.0? The declaration is present — injected into candidate
  formulas by `scripts/build_homebrew_artifacts.py:377-380` — yet both kegs
  were installed simultaneously on the owner's Mac and only surfaced at
  teardown. `macos-brew-smoke.yml:546-550` asserts the conflict warning "is
  benign (brew warns and installs anyway)", written about the *absent
  counterpart* case; if that reading has been generalised to the
  *installed counterpart* case, the guard was never load-bearing.
  Needs: a runner reproduction — install the stable formula, attempt the
  candidate, capture what Homebrew actually does. Prior context in #3753,
  #3763, #3770.
  Blocks: #3853's fix direction (packaging-level guard vs `ops.py` reconcile).
  Answered 2026-08-19 (#3860), by the reproduction it asked for —
  `macos-brew-smoke.yml`'s `stable-over-candidate` job, run 32202943938 on a
  clean `macos-15` runner. **`conflicts_with` is directional, and only the rc
  formula declares it.**
  - Candidate onto an installed stable: the guard holds. Brew refuses with
    `Cannot install …@3.0.0rc because conflicting formulae are installed` and
    leaves `stable installed: 1 / candidate installed: 0`.
  - Stable onto an installed candidate — **the owner's direction** — nothing
    checks anything. Brew builds and installs the keg to completion
    (`/opt/homebrew/Cellar/nyxgpt-api/3.0.0: 6,140 files, 152MB`); only
    `brew link` then fails on the symlink collision (`Could not symlink
    bin/nyxgpt … is a symlink belonging to nyxgpt-api@3.0.0rc`). The keg stays
    installed, `/opt/homebrew/bin/nyxgpt` still points at the *candidate*, and
    brew prints a "shadowed by other commands" caveat. Final state:
    `stable installed: 1 / candidate installed: 1` — #3853's machine exactly.
  So the answer to "why did `conflicts_with` not prevent it" is that in that
  direction it was never asked. A `conflicts_with "nyxgpt-api@X.Y.Zrc"` on the
  stable formula is the packaging-level half; `brew link`'s failure is not a
  guard, because a failed link leaves the keg in place.
  Also learned there, separate from the answer: a `brew tap-new` tap is
  untrusted, and resolving `conflicts_with` *loads* the named formula, so brew
  refuses on trust grounds before it ever evaluates the conflict — #3770's
  shape, and why the job now trusts the whole tap.
  **Acted on 2026-08-19 (#3853), so this question no longer blocks anything:**
  the stable formula declares its own line's candidate, both directions are
  hard assertions in CI, and a cross-line candidate is stopped at install time
  rather than by packaging. See **D-030**.

- **Q-003** · 2026-08-18 · owner acceptance (#3857) — What stops the web UI's
  client JS from loading: the two builds racing for port 3000 (#3853), or a
  stale PWA service worker (`web/next.config.ts:62-65`)? Every endpoint was
  measured responsive while the UI showed permanent `next/dynamic` loading
  fallbacks, so the fault is client-side.
  Needs: DevTools → Application → Service Workers on a reproducing machine.
  The owner's machine was torn down before this was captured, so it must be
  reproduced from scratch.
  Blocks: nothing. #3857 shipped **both** branches unconditionally rather than
  waiting on the answer — a bounded chunk timeout with an error boundary, a
  build-change service-worker cache drop, and a document-inline hydration
  watchdog that surfaces the failure even when no client JS runs at all. The
  question stays open because the trigger is still unidentified; the in-product
  Details panel ("a service worker is controlling this page" / "no service
  worker is controlling this page") now answers it at the next occurrence
  without DevTools.

- **Q-004** · 2026-08-18 · owner acceptance (#3853) — What produced
  `Unknown system error -11` opening
  `/Users/.../Dropbox/repositories/nyxGPT/web/.next/dev/...` from a **brew**
  service? The dev-mode attribution was retired (see **S-005**) and nothing has
  replaced it. If a locally-generated tap can produce kegs whose runtime files
  resolve into a live checkout, that is a repo-less portability defect distinct
  from the known `file://`-tarball violation.
  Needs: inspection of a keg built from the local `nyxgpt-local` tap.
  Blocks: nothing yet; would warrant its own issue if confirmed.

- **Q-005** · 2026-08-18 · developer-agent (#3806) — How should the merge path
  gate on the head SHA's check status (**V-038**) without deadlocking on the
  review's *own* in-flight check runs? On `78c0e5cf` the checks include
  `claude-review` and `execute-review-decision`, which cannot be complete at
  the moment the reviewer merges, so a naive "no check may be queued or
  in_progress" rule blocks every merge forever; a failures-only rule would not
  have caught #3876, whose check had not failed *yet* at merge time. The
  discriminator (allowlist of quality gates, denylist of review-side runs, or
  a bounded wait) is a design decision on the pipeline's core merge path, and
  a wrong one jams every merge in the project.
  Needs: an owner-approved approach, then its own agent-process issue. Not
  taken inside #3806's fix branch: that branch exists to unpoison the release
  branch, and rebuilding the merge gate under it would put an unreviewed
  change to every merge behind an unrelated issue number.
  Blocks: nothing today — but every merge is exposed to **V-038** until it is
  answered.

- **Q-006** · 2026-08-18 · owner acceptance (#3811) — What credential files a
  support ticket for a filer who is not the owner? The owner's settled intent
  (#3811, 2026-08-16) is that the web UI captures the ticket and a background
  process creates the issue, so **the filer never leaves the nyxGPT chat**.
  Creating an issue server-side needs a GitHub credential: the owner's install
  has one (`[github] pat`); a stranger's does not, and a stranger is precisely
  the population support exists for. The owner named three options and left
  the choice open — a hosted intake the owner runs, backend creation when a
  token is configured with the GitHub handoff as fallback, or requiring a
  configured token. They are not variations on one design: the first needs
  hosting and abuse controls, the second is two permanent code paths, the
  third excludes the tokenless filer.
  Needs: ~~the owner's choice, then its own issue for the intake rework~~ —
  **ANSWERED 2026-08-19** by the owner's acceptance failure on this issue,
  which directed the rework rather than the decision: the second option, and
  the intake shipped in #3811 rather than in an issue of its own. See
  **D-034**. The residue is narrower than the original question: whether a
  hosted intake should also cover the tokenless filer, who today gets the
  prefilled GitHub form.
  Blocks: nothing on #3811 — the three criteria this once held back (no
  compose page, a confirmation in the chat, the product applying the label)
  are met by D-034's intake. A hosted intake, if the owner wants one, is new
  work and needs its own issue.

- **Q-007** · 2026-08-19 · developer agent (#3853) — After the release
  ceremony retires a shipped line's candidate formulas from the tap
  (`scripts/retire_rc_formulas.sh`), the stable formula's new
  `conflicts_with "<name>@<line>rc"` (**D-030**) names a formula the tap no
  longer carries. Two things follow and neither is established: (a) does the
  conflict still hold for a machine that still has that **keg** installed —
  i.e. can Homebrew's `Formulary` load the formula back from the keg's own
  `.brew/*.rb` — and (b) what does a user with no candidate installed see?
  An absent counterpart is documented as a benign warning (#3753), but on the
  *stable* formula that warning lands on the main install path, and
  Homebrew's wording for it advises removing the declaration, which is
  advice this project must not follow. Do not act on either by reading
  Homebrew's source: #3853 exists because a behavior everyone believed was
  never run.
  Needs: the `Measure: the same conflict after the candidate is retired from
  the tap` step in `macos-brew-smoke.yml`'s `stable-over-candidate` job,
  which reproduces exactly that state on a clean `macos-15` runner and prints
  the answer. It runs on every formula PR; read its `MEASURED:` lines.
  Blocks: nothing today. It cannot be reached before 3.0.0 ships and its
  candidates are retired, and until then both directions of the conflict are
  hard-asserted. If (b) turns out noisy, the candidate answer is retiring an
  rc formula by replacing it with a disabled stub rather than deleting it —
  which keeps the name resolvable and gives a better error than "No available
  formula" — not dropping the declaration.

## Superseded

Retired beliefs. Listed because this project asserted each of them, and a
session reconstructing state from older artifacts will find them and re-assert
them.

- **S-001** — ~~"master/main is human controlled: the owner runs the master
  fast-forward by hand."~~ Superseded 2026-08-12 by **D-003** — the ceremony runs
  unattended after the owner moves the tracking issue to `For Release`.

- **S-002** — ~~"Improvements never gate a feature's move to `For Release`"~~
  (owner decision, 2026-08-01). Superseded 2026-08-12 by **D-001**/**D-002**: an
  improvement filed against an issue blocks its acceptance exactly like a failure
  does. The `Improvement` label now separates the two only as *statistics* (spec
  gap vs implementation defect); the gating is identical.

- **S-004** — ~~"`Related feature: #N` in the issue body is how issues are
  linked."~~ Superseded 2026-08-12 by **D-002** — native relationships only. Still
  read as a fallback for issues filed before that decision; never written.

- **S-005** — ~~"The stale `nyxgpt-web` service on the owner's Mac was a
  **dev-mode** service pointing at a Dropbox-backed checkout."~~ Asserted in
  #3853's body and retired 2026-08-18 by the teardown evidence: dev mode runs
  api/web as `com.nyxgpt.api`/`com.nyxgpt.web` LaunchAgents
  (`install_mode.py:48-51`), and neither plist ever existed on that machine —
  `ls ~/Library/LaunchAgents` showed only the log/env agents and the two
  `homebrew.mxcl.*` plists. The service was listed under `Homebrew services:`
  throughout. The stale pair were `nyxgpt-api`/`nyxgpt-web` **2.1.0 kegs**, a
  prior release never uninstalled; `_remove_dev_launchagents` was never invoked
  on that machine at all, since `_reconcile_install_mode` gates on a mode
  change that never occurred. Replacement explanation is open — see **Q-004**.

- **S-006** — ~~"The developer queue is kicked by posting
  `READY_FOR_NEXT_ISSUE` as a comment on the release tracking issue."~~
  Superseded 2026-08-19 by **D-028** (#3882/#3917): the token was **deleted,
  not deprecated**. `developer_pull_next_issue.yml` subscribes to
  `repository_dispatch: [dispatch-next-issue]` and to nothing else, so the
  comment is inert — it posts, it reads like an action, and no run starts.
  **The kick is `dispatch_next_issue` (`scripts/agents/lib/gh_project.sh`),
  and the way to start work on a specific issue is to assign
  `myGPT-developer-agent` to it** (claimable from `Backlog`, `In Review` or
  `In Progress`; the held lanes stay held per **D-001**/**D-008**).

  Listed because the belief outlived the mechanism by a day and cost a real
  stall: on 2026-08-19 a shepherding session posted comment kicks across
  several hours, reported them as dispatches, and none of them fired. A
  session reading **D-020** alone would repeat it.
- **S-007** — ~~"Allocating the EC2 Dedicated Host an EC2 Mac requires is
  deliberately not built; `--os macos` refuses when there is no `--host`."~~
  (P-003, 2026-08-19, #3867.) Superseded 2026-08-22 by **D-043** (#3995). Both
  halves of the reason were retired rather than overridden: the configuration
  *can* tear the host down (the release is deferred, not skipped), and spending
  the operator's money without asking is a consent problem that disclosure plus
  a typed confirmation answers — as it does for every other irreversible spend
  in this CLI. P-003's parting advice, "boto3, not Terraform, whose destroy path
  is where the trap is", was **not** followed and the reasoning is worth
  keeping: the trap is not Terraform, it is `terraform destroy` calling
  `ReleaseHosts` inside the 24-hour window. Two isolated root modules plus a
  `terraform state rm` of the host before the destroy removes it, and keeps the
  idempotent reconcile that a boto3 allocate/release pair would have had to
  reinvent.

  ID from `ledger_ids.py next S` (S-003 is taken: it was relocated to the
  private annex, and IDs are never reused).
