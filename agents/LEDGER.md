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
  the sanctioned comment triggers** (`READY_FOR_NEXT_ISSUE` on
  `notify_scrum_ready.yml`, `@review` on `claude-code-review.yml`, which also
  passes `allowed_bots: "claude"` to the review action). Every GitHub write
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

- **D-030** · 2026-08-19 · developer agent (#3861) — **The native install
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
  hand-written cleanup halves (`_remove_dev_launchagents` /
  `_stop_artifact_brew_services`) are **deleted**, replaced by one subtraction
  (`_retire_previous_identity`: the recorded previous identity's services
  **union** what the service managers actually report, minus the target's own)
  — their existence as a *pair* is why there was no third case, and the union
  is why a fourth one nothing recorded is retired as well;
  (b) an **unknown** previous identity (pre-#3861 marker, malformed, or
  absent) is a possible mismatch reconciled defensively against what the
  service managers actually report, never "the same"; (c) reconcile **stops
  and de-registers, never uninstalls** — removal is a teardown decision
  (#3859). Scope note: this is the `ops.py` half of #3853. The *packaging*
  half stays parked — nothing here adds `conflicts_with` to the stable
  formula, so **D-026**'s debt (flipping `macos-brew-smoke.yml`'s
  reverse-direction `::warning::` back to a hard failure) is untouched and
  still owed by the PR that fixes #3853. Extends **D-009**, whose "installing
  either mode over the other stops the other's services" now reads as the
  identity comparison's special case.
  Source: #3861; `src/nyxgpt/install_mode.py` (`InstallIdentity`);
  `tests/unit/test_install_identity.py`;
  `.github/workflows/macos-brew-smoke.yml` (`stable-over-candidate`).

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
  Needs: the owner's choice, then its own issue for the intake rework.
  Blocks: three acceptance criteria on #3811 that this PR does **not** meet
  and does not claim to — the filer not seeing GitHub's compose page, being
  returned to the chat with a confirmation after submitting, and the ticket
  type being applied by the product rather than answered on a form. Everything
  in #3811 that does not depend on the answer shipped instead: the `Support`
  label is guaranteed (**V-042**), the type is collected in nyxGPT and carried
  into the body, and hygiene survives a vanished project item (**V-043**).
  Not taken inside this PR: an intake path built on a guessed credential model
  is one that gets rebuilt, and the two paths it would have to hedge across
  differ in where the product is hosted, not in a detail.

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
