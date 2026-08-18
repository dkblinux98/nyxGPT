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
here when they decide something, verify something, park something, or hit a
question they cannot close.

**The rule: a claim that is not in this ledger and not freshly verified is not
asserted as fact.** Say "I have not checked" and then check.

This also bounds churn cost. It converts an unbounded invisible expense
(re-derive everything every session, sometimes wrongly) into a bounded visible
one: read one dense file, then re-verify only what is stale *and* load-bearing
for the task in hand.

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
2. You just verified it this session, and you can name how; or
3. You say plainly that you have not verified it.

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
- Facts that were expensive to establish and that a session would otherwise
  re-derive — especially ones with a non-obvious answer.
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

**The test:** *could a future session get this wrong, confidently, and would
neither the live system nor a single doc lookup catch it?* If yes, it belongs
here. If no, it does not.

**Pruning:** never delete an entry. Move it to [Superseded](#superseded) with a
pointer to what replaced it. A deleted entry becomes re-derivable — and
re-derived wrong — which is the whole problem.

---

## Entry schema

Four kinds. Each has a stable ID (`D`/`V`/`P`/`Q` + zero-padded number).
**IDs are never reused**, including after supersession.

**Allocate with the helper, not by eye** (#3806):

```
git fetch origin <release branch>
python3 scripts/agents/lib/ledger_ids.py next V --base origin/<release branch>
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

**Verification** — a fact, plus how it was established and when.

```
- **V-000** · YYYY-MM-DD — <the fact>.
  Method: <the exact thing that was read, run or observed>.
  Re-verify when: <the condition that makes this stale>.
```

`Method` is mandatory and must be specific enough to repeat. "Known from
context" is not a method. If the check you ran is weaker than the claim (you
read a doc that describes a setting rather than reading the setting), say so in
the entry — an honest weaker fact beats a confident wrong one.

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

**Staleness.** A verification is stale when its `Re-verify when` condition has
fired, not merely when it is old. Re-verify a stale entry only if it is
load-bearing for the task at hand; otherwise leave it and say it is unverified.
When you do re-verify, update the date and `Method` in place — same ID, same
entry. Only a *changed fact* moves to Superseded.

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
not agent-editable except to add a `Re-verify` result or a supersession pointer.

**This public ledger is machine-facing.** Incident narratives, owner-sensitive
decisions and product-forward material live in the owner's private annex
(`product_management/private/LEDGER.md` — gitignored, never in the repository).
Public entries state mechanics; they do not narrate the owner. Some entry IDs
are absent here by design (relocated to the annex; IDs are never reused).

---

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

## Verifications

- **V-001** · 2026-08-14 — Releases in this repository are immutable: a
  published release can never gain or change an asset. An attempted asset upload
  returns `HTTP 422: Cannot upload assets to an immutable release`, so an
  incomplete release cannot be repaired — it must be superseded by the next
  candidate. PyPI versions are immutable for the same practical purpose: a burned
  version number is never reused.
  Method: read the guards and error text in `.github/workflows/release-artifacts.yml`
  and `.github/workflows/release-publish-pypi.yml`, the behaviour asserted in
  `tests/test_supersede_incomplete_rc_releases.sh`, and `docs/homebrew.md`.
  **Weaker than the claim it supports:** this verifies that the pipeline is built
  around immutability and has hit the 422 in practice — it does not read the
  GitHub repository setting itself, which is owner-readable only (no agent
  token path exists; verified 2026-08-14).
  Re-verify when: the owner changes the repository's release-immutability
  setting, or a release is observed gaining an asset after publish.

- **V-002** · 2026-08-14 — Installing any formula from the `dkblinux98/nyxgpt`
  tap requires a one-time **whole-tap** trust step, because Homebrew gates
  third-party taps: without it `brew install` stops instead of installing. It is
  per tap and per machine, not per formula or per version. Homebrew spells the
  subcommand `brew tap-trust <tap>` on some builds and `brew trust <tap>` on
  others — if one is rejected as unknown, run the other; an untried spelling is
  indistinguishable from a trusted tap. A grant scoped to a single formula is
  **not** sufficient: installing a candidate makes brew resolve
  `conflicts_with "nyxgpt-api"` and load the stable formula, which the narrow
  grant leaves untrusted, aborting the install.
  Method: read `docs/homebrew.md` §"Trusting the tap (one-time, required)" and
  the `commands.brew` contract in `docs/api.md`; incident #3770.
  Re-verify when: Homebrew changes its third-party tap gate, or the tap name
  changes.

- **V-006** · 2026-08-14 — The Homebrew keg **install** path is CI-coverable on
  a real macOS runner: `macos-brew-smoke.yml` runs `brew install` of the working
  tree's formulas and of the published tap candidate on `macos-15`, and injects
  the empty `mac_ver()` condition to prove the shim. "macOS cannot be tested in
  CI" is therefore not a valid deferral for a formula or install change — only
  the native launchd/brew-services *operate* half still defers to the owner.
  Method: read `.github/workflows/macos-brew-smoke.yml` — jobs at `runs-on:
  macos-15`, steps "Install nyxgpt-api from the local tap", "Tap and install the
  candidate", "Reproduce the empty mac_ver() failure, then prove the shim fixes
  it". The deferral lists in `review-runbook.md` §2, the review prompt and
  `docs/live-verification-ci.md` were corrected to match under #3775.
  Re-verify when: `macos-brew-smoke.yml` stops installing on a macOS runner, or
  GitHub retires hosted macOS runners.

- **V-008** · 2026-08-14 — The dual-lane rule of **D-008** is executed, not
  just written: a CLOSED issue parked in `Acceptance Failed` is promoted to
  `For Release` only when its whole transitive closure is accepted, is left
  untouched while any blocker is open, and is never released to `Backlog` by a
  gate opening; an OPEN issue in the same lane keeps its #3730 holding-pen
  behavior (released on the drain, never promoted).
  Method: `bash tests/test_promote_accepted_features.sh` and
  `bash tests/test_drain_gate_lib.sh` run 2026-08-14 on the #3780 branch —
  both run the real scripts against a stubbed `gh`/`graphql`, and both are now
  executed by `pytest tests/unit/` as well.
  Re-verify when: the lane names change, or either sweep's candidate rule is
  edited.

- **V-009** · 2026-08-15 — The pypi.org `/simple/` and `/pypi/*/json` endpoints,
  fetched via this remote session's egress proxy, can serve a **stale CDN cache**
  for hours: they showed rc9 as the newest candidate while rc10/rc11 were
  published and pip-resolvable, so stale reads produce confidently wrong
  currency claims. To learn which candidate is current,
  use `pip index versions nyxgpt --pre` (fresh in practice) or the publish
  workflow's run history — never the curled pypi.org JSON alone.
  Method: side-by-side check 2026-08-15 ~03:30Z — curled JSON showed …rc9 while
  `pip index versions` returned rc11/rc10 and the rc11 publish run's own
  "Verify PyPI serves the build" step had passed at 22:06Z.
  Re-verify when: the session egress proxy or PyPI CDN behaviour changes.

- **V-010** · 2026-08-15 — A distro's bare `python3` cannot be assumed to
  satisfy nyxGPT's `requires-python` (`>=3.11`): on Amazon Linux 2023 it is
  3.9, and a venv built from it is one pip refuses the nyxGPT artifact into
  ("requires a different Python: 3.9.x not in '>=3.11'"). Both the service
  venv (`ops._create_service_venv`) and the two cloud provisioning paths now
  select an interpreter by asking each candidate its own version.
  Method: `scripts/service-venv-python-smoke.py` run 2026-08-15 on Linux with
  a real CPython 3.9.25 and the real `nyxgpt-3.0.0` sdist — resolving bare
  `python3` produced a 3.9 venv pip refused; the selection logic picked a
  qualifying interpreter and the same `pip install` succeeded. Re-running it
  against a pre-fix `_create_service_venv` fails at that half, so the check
  is not green by luck. Wired as `linux-native-smoke.yml`'s
  `service-venv-python` job (#3782).
  Re-verify when: the `requires-python` floor moves, or the candidate list in
  `ops._SERVICE_PYTHON_NAMES` changes.

- **V-011** · 2026-08-15 — A comment token in this repo starts work **only
  where it opens a line**, for all four tokens (`RETRY_IMPLEMENTATION`,
  `READY_FOR_NEXT_ISSUE`, `@acceptance-failure`, `@improvement`). A GitHub
  Actions `if:` can only substring-match, which is why the same defect fired
  twice: #3706 (a park note naming the kick token dispatched work) and #3790
  (the developer agent's stop message named the retry token, so a stop
  produced a start — ~500 runs and ~500 comments across #3782/#3784 in under
  two hours). Each trigger now has a `comment_gate` job running
  `.github/actions/comment-token-gate`; agent prose that must name a token
  carries `<!-- nyxgpt-token-mention -->`, which makes the whole comment
  inert. Reference: `docs/agent-comment-tokens.md`.
  Method: `.github/workflows/comment-token-gate-smoke.yml` executes the gate
  action on a runner over the incident's real comment bodies and proves both
  halves — the pre-#3790 substring rule matches the looping message while
  the gate refuses it, a genuine command still proceeds; plus
  `tests/unit/test_comment_token_triggers.py`, which asserts the stop message
  is token-free and every trigger is gated.
  Re-verify when: a new comment token is added, or a trigger's `if:` is
  edited (both tests fail loudly if the gate is dropped).

- **V-012** · 2026-08-15 — The `--kubernetes --local` deployment is
  **self-contained and chattable**: `k8s/` now ships the data/LLM tier
  (single-replica Cassandra and Ollama StatefulSets on PVCs, `cassandra:9042`
  / `ollama:11434`), the api ConfigMap addresses both by Service name, the
  session backend is `cassandra` so all four api replicas share one session
  list, and `ops install --kubernetes --local` waits for both StatefulSets to
  be Ready before reporting health. Ollama's `postStart` pulls
  `[nyxgpt] default_model` and its readiness probe requires that model, so
  the Service gets no endpoints until a chat can be served. Before #3786 this
  path deployed api+web only, pointed Ollama at `host.docker.internal` and
  had no session store — Pods Running, no chat possible.
  Method: executed on a real `kind` cluster on a Linux runner (#3786 PR) —
  `kubectl apply -k k8s/`, both StatefulSets reached Ready, `ollama list`
  showed `qwen2.5:0.5b`, and a chat POSTed through the web Service's own
  proxy route (`/api/chat/stream`, the browser's path) streamed an answer
  back; `cqlsh -e "SELECT name FROM nyxgpt.chat_sessions"` showed the session
  row, and 12 consecutive session-list reads across the 4 replicas all
  returned it. `scripts/k8s-local-smoke.sh` /
  `.github/workflows/k8s-local-smoke.yml` re-run that end to end and then
  delete the tier to prove the same check fails without it.
  Re-verify when: `k8s/kustomization.yaml`, `k8s/configmap.yaml`, or the
  `ops._wait_for_k8s_data_tier` workload list changes (the smoke job and
  `tests/unit/test_k8s_manifests.py` fail loudly if the tier is dropped).

- **V-013** · 2026-08-15 — The rc11 keg install failure (#3788) is **not** an
  ordering problem in the pip bootstrap. Its cause is that the Homebrew
  `python@3.12` keg's pip cannot import
  `pip._internal.operations.install.wheel`; pip 26.2 pre-imports its lazy
  imports before writing anything, swallows that `ImportError` into
  `_MISSING_MODULES`, and its audit hook re-raises it from
  `_prevent_import_hook` when `req_install.py` needs the module for real.
  Consequence: **any** install routed through that pip dies, whatever is being
  installed and in whatever order — so the keg's pip is now allowed to
  `download` only, and the keg venv is bootstrapped by running pip out of the
  downloaded wheel (`python pip-X.whl/pip install pip-X.whl`).
  Method: created exactly that machine state on a stand-in keg (pip 26.2.1)
  on 2026-08-15 by moving `pip/_internal/operations/install/wheel.py` out of
  it, and reproduced the owner's traceback line-for-line, down to
  `req_install.py:779` → `install.py:97 in _prevent_import_hook`; the wheel
  bootstrap survives the same state (`pip download` succeeds, the venv
  python installs pip out of the wheel). Scope of that method, stated
  because it misled three rounds: the stand-in keg was a flat tree with no
  prefix→Cellar symlinks, so it could not show the injection missing the
  child, and off-workflow execution is evidence about the *recipe*, not
  about the step. `macos-brew-smoke.yml`'s "Reproduce the #3788 keg-pip
  failure" step is where both directions have to run on the real keg
  topology; as of this entry it has not yet been green there — cite the run
  here once it is.
  Corollary, verified 2026-08-15 across three red runs of that step: **a
  condition injected by emulation is tested in an environment the real
  install does not have, so the emulation becomes the thing under test.**
  Both earlier spellings emulated this one with a meta-path finder in a
  `sitecustomize` on `PYTHONPATH`, and both silently never fired: the first
  scoped the keg by `os.path.abspath`, which cannot match in the
  symlink-resolved `pip --python` child (`get_runnable_pip()` is
  `Path(pip_location).resolve().parent`); the second compared realpaths but
  shadowed the keg's own `sitecustomize` — python imports exactly one — and
  so moved pip resolution from the prefix copy to the Cellar copy, off the
  anchor. Chaining to the shadowed file fixed the resolution and the fault
  still did not fire, which is the point: each fix bought another round
  about the vehicle. Taking the file away needs no interpreter environment
  at all; the third round then moved it at the path *this* process imports
  it by, which on Homebrew is a link into the Cellar, so the self-check
  fired through the dangling link while `pip --python`'s child — which
  re-execs through `Path(pip.__file__).resolve()` — imported an untouched
  Cellar copy and installed successfully. Corollary of the corollary:
  **remove the file where every route resolves to (its realpath), and assert
  the condition by every route, not the convenient one.** A `trap` restores
  the keg on every exit path (executed: a mid-step failure leaves the keg
  intact). General rule, and the reason the self-check exists at all: **a
  fault that does not fire is indistinguishable in a log from a bug that is
  gone**, so the job asserts the condition exists before inferring anything
  from it.
  Re-verify when: pip changes `_EAGER_IMPORTS`/`_prevent_import_hook` (the
  deprecation there is marked `gone_in="26.3"`), or the recipe stops using
  `pip download`.

- **V-014** · 2026-08-15 — Next.js compiles `web/src/instrumentation.ts` for the
  **edge** server runtime as well as the Node.js one, so any node-only module it
  reaches must be imported *inside* a `process.env.NEXT_RUNTIME === "nodejs"`
  block, never at the top level. A top-level `import … from "./lib/logger"`
  (which reaches `node:fs`) failed the edge compile with
  `UnhandledSchemeError: Reading from "node:fs" is not handled by plugins`, and a
  failed instrumentation compile makes `next dev` answer **500 to every request**
  — while `next build`/`next start` are unaffected, so only the dev-server path
  (`nyxgpt up --dev`, **D-009**) broke and every artifact-path check stayed green.
  An early `return` is not sufficient: webpack drops an untaken `if` branch and
  the module graph under it, but statements after a `return` stay live to the
  bundler.
  Method: executed on this runner 2026-08-15 — reproduced `GET / 500` with the
  `⨯ node:fs` compile error, confirmed the failing compiler by logging the
  webpack context (`{isServer:true,nextRuntime:"edge"}`), then re-ran after the
  fix for `GET / 200` with zero `UnhandledSchemeError`; `npm run build` +
  `npm run start` also re-checked at 200 (#3789/#3791). Standing CI guard: the
  `web / expected 200` check in `linux-native-dev-smoke`.
  Re-verify when: Next.js changes which runtimes it compiles the instrumentation
  hook for, or `lib/logger.ts` stops using `node:fs`.

- **V-015** · 2026-08-15 — `ops.install()`'s unit tests patch its steps out **by
  enumeration**, so every step added to the list afterwards runs for real against
  the developer's own machine until someone adds it to each `with` block. The
  install-mode step (**D-009**) landed that way and was not inert: on a machine
  recording `dev`, running one install unit test deleted the real
  `~/.nyxGPT/opt/nyxgpt-api/venv`, rewrote the real marker back to `artifact`
  (and on macOS would `launchctl bootout` the live dev LaunchAgents), then failed
  — i.e. the suite destroyed the state of the machine `nyxgpt up --dev` had just
  produced, and passed in CI only because runners start in artifact mode.
  Method: executed on this runner 2026-08-15 — wrote `{"mode": "dev", …}` to the
  real `~/.nyxGPT/install-mode.json` plus a sentinel venv, ran
  `test_ops_install_returns_zero_when_all_ok` on the pre-fix tree (venv
  DESTROYED, marker rewritten, `assert 2 == 0`), then the same injection on the
  fixed tree (marker byte-identical, sentinel PRESENT, test passed) (#3789/#3791).
  Re-verify when: a new step is added to `ops.install()`'s step list — the
  standing guards are the autouse `_isolate_install_mode_marker` fixture in
  `tests/unit/conftest.py` and the paired
  `test_install_tests_patch_the_mode_step_…` / `test_an_unpatched_mode_step_…`
  fault-injection tests, which close the marker half but not the general pattern.

- **V-016** · 2026-08-15 — Observability on a **plain Linux docker engine**
  needs two engine-level fixes that Docker Desktop hides on macOS, and both
  are now reconciled inside `ops._reconcile_grafana_provisioning` — the one
  function every stack-start path goes through (`nyxgpt ops install`, the
  standalone `nyxgpt ops observability`, and the dashboard's
  `reconcile_observability` toggle). (1) dockerd creates a missing bind-mount
  source `root:root`, so Prometheus (uid 65534), Grafana (472) and Loki
  (10001) crash-loop unable to write their own data dirs; the #3632 guard for
  this was wired into `install()`'s step list **only**, so the other two paths
  brought the stack up broken (#3721). (2) `host.docker.internal:host-gateway`
  resolves to the bridge gateway, which a loopback-bound native API does not
  listen on — bridged by the `host-api-relay` Compose service (#3725). With
  both, `[api] host` stays `127.0.0.1` and no `0.0.0.0` listener is needed.
  Method: `scripts/linux-observability-smoke.py`, run on a real Linux docker
  engine (28.0.4) in CI as the `linux-observability` job of
  `linux-native-smoke.yml`. It fault-injects the pre-fix behaviour first —
  Prometheus must crash-loop on `open /prometheus/queries.active: permission
  denied` or the job fails as toothless — then asserts Prometheus runs, its
  `nyxgpt-api` target reports `up` (a real scrape through the relay), and
  nothing listens on `0.0.0.0:8000`.
  Re-verify when: the observability bind-mount set changes, an upstream image
  changes the uid it runs as, or a new entrypoint starts the stack without
  going through `_reconcile_grafana_provisioning`.

- **V-017** · 2026-08-15 — A smoke test that satisfies a prerequisite *itself*
  before invoking the code under test proves nothing about that prerequisite.
  `scripts/systemd-native-smoke.sh` ran the official Ollama installer before
  calling `nyxgpt ops install`, so CI never saw that the Linux install step
  simply stopped with "ollama not found on PATH — install it first: curl … |
  sh" on a real clean machine, while macOS's twin ran `brew install ollama`
  for the operator. Two structural lessons, both now encoded: a smoke script
  must not pre-satisfy what it verifies (the pre-install is gone; the script
  asserts ops installed it, and hard-fails under `CI` if Ollama is already
  present), and a smoke script must exercise **the commands the acceptance
  names** — this one drove `ops install`/`ops down` and never `nyxgpt up`,
  `nyxgpt down`, `ops status` or `ops doctor`, the four #3508's acceptance is
  written in terms of.
  Method: reproduced 2026-08-15 on Linux from the published **rc11 wheel with
  no repo checkout** (`pip install nyxgpt==3.0.0rc11` into a clean venv) —
  `nyxgpt up` reported `[FAIL] ollama not found on PATH`. After the fix, the
  same step ran the installer, `_takeover_system_ollama_service` disabled the
  system unit the installer had just enabled, `nyxgpt-ollama.service` came up
  active and served HTTP 200 on 11434. `scripts/ollama-bootstrap-smoke.py`
  injects the pre-fix behaviour and was executed to confirm it fails without
  the bootstrap, so the green run is not luck (#3508, #3775). Re-confirmed in
  CI rather than only off-CI: `linux-native-smoke` run 31907416812 on
  `49bbb94f` is green across all its jobs, with the injection step firing
  first — the standing guard for this entry.
  Re-verify when: Ollama changes its Linux distribution channel, or the
  install-step ordering in `_install_native_ollama_systemd` changes (the
  install must stay *before* the port takeover — the installer is what
  creates the conflicting system unit).

- **V-018** · 2026-08-15 — The pytest suite could not pass on a machine that
  was *running the stack it tests*, which is now the normal state of a
  developer machine on Linux as well as macOS (#3508). Two independent
  environmental couplings, both in `tests/`, neither in product code:
  the #3443 production-log-dir guard failed the session because the running
  `nyxgpt-api`/`nyxgpt-web`/`cassandra`/`ollama` supervisors append to
  `~/.nyxGPT/logs` throughout the run, and the RAG ingest tests treated
  "Cassandra's port is open" as "the stack is usable" and then hit a live
  `ollama serve` that answers `501 This server does not support embeddings`
  because the loaded model is chat-only. Ownership is the discriminator for
  the first (a file an *external* process holds open was not written by the
  code under test — `tests/log_guard.py`); probing the real `/api/embed` call
  is the discriminator for the second (reachability is not usability).
  Method: reproduced 2026-08-15 on a Linux runner with the native stack up —
  `pytest tests/unit/` gave 6 failed + 1 error; after the fix, 5073 passed,
  6 skipped, 0 failed. Both properties proven by execution rather than
  inspection: a fault-injected test that writes to `~/.nyxGPT/logs` still
  fails the guard, and `externally_held_log_files` was observed attributing
  all 9 live service logs to their supervisors. An A/B run of
  `tests/integration/test_rag_playground.py` and `test_request_id_streaming.py`
  with the change stashed and applied gave the same 5 pre-existing
  environmental failures (no `llama3.1:8b`, no embedding model) and dropped
  the guard error, confirming the fix is not masking product failures.
  Re-verify when: the guard's attribution moves off `psutil.open_files()`, or
  a supervisor starts writing service logs as a *different user* than the one
  running pytest (AccessDenied fails closed, so those files return to the
  guard's scope and the suite would fail again).

- **V-019** · 2026-08-15 — `nyxgpt up --skip-observability` could **never**
  return 0. The flag means "don't start the Grafana/Loki/Jaeger/GlitchTip
  Compose profiles"; it deliberately leaves their config.ini feature flags
  on, so self-heal keeps reporting those services `desired=True,
  state="absent"` — which is the correct answer to "what does the operator
  want running". `ops._wait_for_stack_healthy` knew nothing about the flag,
  so `up` waited on containers the same command had just chosen not to
  start, then exited 2 on a completely healthy stack. The wait now excludes
  `self_heal.observability_services()` when the flag is set, and the timeout
  message names what is still pending instead of only saying "not every
  component" (#3508).
  Two structural lessons, and the reason this sat undiscovered: (1) a flag
  that suppresses an *action* must also be honoured by anything that later
  asserts on that action's *effect*, or the two halves of one command
  disagree; (2) an alias is not covered by testing what it wraps —
  `systemd-native-smoke.sh` drove `nyxgpt ops install` for months and was
  green throughout, because `install()` has no health-wait. The defect
  existed the whole time and surfaced within one CI run of the smoke script
  being switched to `nyxgpt up` (**V-017**), i.e. to the command the
  acceptance is actually written in terms of.
  Method: observed on the #3798 `linux-native-smoke` run for `f6918b8d` —
  every systemd unit `active`, `ops status` clean, `jaeger`/`otel-collector`
  reported absent, and `up` still burned its full 300s timeout and exited 2.
  Confirmed pre-existing rather than merge-induced by reading
  `_wait_for_stack_healthy` at the merge base and on `v3.0.0`: identical in
  both. Standing guard: the `linux-native-smoke` job, which now fails if
  `nyxgpt up` cannot reach healthy.
  Re-verify when: a new flag suppresses part of the install (it will need
  the same treatment in the wait), or `--skip-observability` starts clearing
  the config.ini flags — at which point the exclusion becomes redundant
  rather than wrong.

- **V-020** · 2026-08-15 — The EC2 artifact install path on Amazon Linux 2023
  fails at the CLI venv: the AMI's system `python3` is **3.9.25** and every
  published nyxgpt distribution declares `requires-python >=3.11`, so
  `pip install nyxgpt` inside that venv resolves nothing (the #3782 class,
  observed independently on the artifact path — see V-010).
  Two further facts from the same execution: Ollama's official installer aborts
  the bootstrap on a bare AL2023 machine with "This version requires zstd for
  extraction" (fixed in the user-data template by #3784), and with a candidate
  interpreter fix applied the whole path is green — bare AL2023 -> artifact
  install -> api/web/ollama serving in 178s.
  Method: executed `nyxgpt cloud smoke --container` (#3784) three times on the
  agent runner (docker 28.0.4) on 2026-08-15 — unpatched tree failed in 47s and
  the harness classified it as the interpreter class; with the fix applied it
  passed in 178s against published 3.0.0rc11; `--inject old-python` exited 0.
  Re-verify when: #3782 lands (the unpatched half stops reproducing), or the
  AL2023 AMI's system interpreter changes.

- **V-021** · 2026-08-15 — An artifact install pulls **three** things off the
  network, not one: the `nyxgpt` CLI, then `nyxgpt-api-<version>.tar.gz` and
  `nyxgpt-web-<version>.tar.gz` from that version's GitHub Release
  (`ops._service_source_tarball`). So a smoke that swaps in a locally built
  wheel has only replaced the first — the wheel declares the checkout's
  version (`3.0.0`), which has no release until the ceremony cuts one, and
  `ops install` 404s at step 33 of 35. `nyxgpt cloud smoke --container
  --wheel` therefore now also builds those two tarballs with the release's own
  builder and points `ops` at them with `NYXGPT_ARTIFACT_DIR` (set but
  asset-missing raises, rather than falling back to the network). The general
  fact, and why this was worth an entry: **"install from an artifact" is not
  one artifact**, and a fix that covers only the entry point leaves the
  dependent assets resolving against a release that does not exist.
  Method: executed on the agent runner (docker 28.0.4) 2026-08-15 on the
  merged #3784 branch — `nyxgpt cloud smoke --container --wheel
  dist/nyxgpt-3.0.0-py3-none-any.whl` PASSED in 193s (bare AL2023 -> artifact
  install -> api/web/ollama serving), where the same command on the same tree
  before the staging fix failed with `Could not obtain the nyxgpt-api
  artifact` after 33/35 steps (CI run 31905652586). `--inject old-python`
  exited 0 on the same tree, so the fault still fires: it neutralizes the
  merged template's version loop, the bootstrap reaches its own
  "no Python >= 3.11 … requires-python is '>=3.11'" guard, and the harness
  still classifies it as the #3782 interpreter class.
  Re-verify when: `3.0.0` gains a real GitHub Release (the 404 half stops
  reproducing), or `_service_source_tarball` grows a fourth source.

- **V-022** · 2026-08-15 — A fault-injected smoke that treats *any* failure as
  its pass condition is still green by luck. `nyxgpt cloud smoke --container
  --inject <fault>` inverts the verdict, and inverted was implemented as
  `passed = bool(failure)` — so a docker outage, an image-pull flake, a build
  failure or a refused preflight all made the injection job exit 0 while
  proving nothing about whether the smoke can see the defect, which is the
  D-006 condition the job exists to close. The pass condition now needs three
  things: a failure, at or after the bootstrap the fault was injected into,
  whose classification is the class that fault reintroduces (`FAULTS[...]
  .expects` -> `injection_verdict`). Second fact from the same round: the
  failure classifier misdiagnosed a release-asset 404 as a broken
  `systemd --user` session (CI run 31905652586), because its systemd signature
  matched any output *mentioning* `systemctl --user` — which a successful
  install prints constantly — and the whole 35-step log was classified rather
  than its tail. The general form of both: **an assertion written against
  "something went wrong" is not an assertion about the thing under test.**
  Method: executed `pytest tests/unit/test_cloud_artifact_smoke.py` on
  2026-08-15 with each rejection path injected — a failing `docker build`
  under `--inject`, a bootstrap failing in another defect class, and an
  unclassifiable failure — each of which passed before this change and fails
  the run after it; the 404-vs-systemd misdiagnosis is pinned as a regression
  case from the real CI output. Standing guard: the `fault-injection` job in
  `cloud-artifact-smoke.yml`.
  Re-verify when: a fault is added to `FAULTS` (it needs an `expects` class a
  signature can actually produce — there is a test for that), or the phase
  list before `bootstrap` changes.

- **V-023** · 2026-08-16 — A repository ruleset on the release/default branch
  requires changes to arrive **through a pull request**: a direct
  `git push` at that ref from an Actions job is rejected with
  `GH013 ... - Changes must be made through a pull request`, and the job's
  own commit step reports success right up to the rejected push. This is why
  every retro dump had been silently discarded since it was written —
  `relationships.json` had never existed on the branch at all (`create mode
  100644` in a run that "succeeded"). Merging a pull request is **not**
  blocked, **but it is not free either**: the ruleset is `PR Rules`
  (id 20347138, active), it covers `~DEFAULT_BRANCH` and `refs/heads/master`,
  and its rules are `deletion`, `non_fast_forward`, and `pull_request` with
  **`required_approving_review_count: 1`**, `allowed_merge_methods: ["merge"]`
  and **no bypass actors** (`bypass_actors: null`). There is **no**
  required-status-check rule. So automation that opens its own pull request
  must also get it **approved by a second identity** — GitHub refuses
  self-approval — or the PR sits at `mergeable_state: blocked` forever. Every
  agent PR clears this only because the review agent approves it; that does
  not transfer to a PR nobody reviews. Generalisation for any future
  automation that writes to the release branch: **push to a side branch, land
  it through a PR, and have a second agent identity approve it**; and a green
  workflow run is not evidence its output landed — read the ref.
  Method: `gh api repos/dkblinux98/nyxGPT/rulesets/20347138` read in full with
  the developer agent token on 2026-08-16 (the earlier claim in this entry
  that the configuration is owner-readable only was wrong — it is readable by
  the agents, and reading it turned up the approval requirement that the
  behavioural evidence alone had missed). Run 31941019009 (`Retro Dashboard -
  Dump Relationships`) is the rejection with that exact GH013 text; both
  halves of the rule are reproduced on a runner by
  `tests/test_retro_data_pipeline.sh`, whose lab remote enforces
  no-direct-push in a `pre-receive` hook and requires an approving review from
  a second identity, proving the old push fails there and the new
  publish/approve/merge path lands the JSON (standing job:
  `.github/workflows/retro-data-pipeline-smoke.yml`). Executed end to end
  against the real ruleset on 2026-08-16: dispatched run 31959032954
  published to `claude/retro-data`, opened PR #3818 as
  `myGPT-scrummaster-agent`, approved it as `myGPT-review-agent`, merged it
  into `v3.0.0` and deleted the branch — after which
  `relationships.json` is present on `v3.0.0` (16447 bytes) for the first
  time ever, read back through the contents API. All six data files landed.
  Re-verify when: the owner changes the branch ruleset (re-read it — do not
  infer it from behaviour), or an automated PR merge into the release branch
  is refused.
  Source: #3815.

- **V-024** · 2026-08-16 — This repository's **default branch is the release
  branch `v3.0.0`, not `master`**, so every workflow triggered by an `issues`
  event (hygiene, drain gate, the comment-command handlers) runs the copy on
  `v3.0.0`. `master` lags the release line by hundreds of commits and its copy
  of those workflow files is inert. A session reading `master` to explain live
  agent-loop behavior will describe code that is not running — #3816 was filed
  against `master`'s pre-#3666 version of `ensure_project_hygiene.yml`.
  Method: `git ls-remote --symref origin HEAD` on 2026-08-16 →
  `ref: refs/heads/v3.0.0`; `git diff origin/master origin/v3.0.0 --
  .github/workflows/ensure_project_hygiene.yml` shows `master` still carrying
  the single-gate version, while the hygiene comment posted on #3816 at
  15:53Z used the "filled missing fields" wording that exists only on
  `v3.0.0` (introduced by 840225f4, #3666).
  Re-verify when: Phase 4 of a release ceremony repoints the default branch to
  the next release line, or `master` is fast-forwarded.

- **V-025** · 2026-08-16 — Fill-if-missing (**#3666**) is not by itself
  enough to stop hygiene clobbering a deliberate write: the defect is the
  *window* between a field's check and its write, not the absence of a check.
  Two runs minutes apart on 2026-08-16 took opposite outcomes on the same
  code — #3814's Status was overwritten with `Backlog`, #3813's survived. The
  window is now closed by re-reading each field inside
  `fill_project_field_if_empty` immediately before the mutation (plus a
  settle wait before any write, and a re-read before the Milestone edit,
  which is an issue attribute with no project-field guard available).
  Method: `bash tests/test_issue_hygiene.sh` on 2026-08-16 — the stub injects
  the concurrent write into that exact window; case R0 runs a guard-stripped
  copy of the script and the deliberate `Acceptance Failed` is overwritten
  with `Backlog` (the defect reproduces on demand), cases R1–R3 run the
  shipped script in the same scenario and Status, Priority and Milestone all
  survive, while case 2 shows a genuinely empty issue still fully populated.
  Standing guards: `project-hygiene-smoke.yml` and
  `tests/unit/test_issue_hygiene.py`.
  Re-verify when: the read sequence in `ensure_issue_hygiene.sh` changes (the
  injection thresholds are expressed in stub reads), or a field is added to
  the job.

- **V-026** · 2026-08-16 — The pytest suite was **not hermetic against an
  installed `~/.nyxGPT/config.ini`**. `_ensure_test_config` wrote its
  tracing-off config only when that file was *absent*, so on any machine (or
  CI job) where an install ran first, the suite inherited the production
  default `[tracing] enabled = true` (2026-07-28) and initialized the OTel SDK
  for real: `/api/v1/tracing` reported enabled and `X-Request-Id` became a
  32-char trace id instead of a 36-char UUID4. A second, independent leak sat
  behind it — OTel's global TracerProvider is set-once *and* its `ProxyTracer`
  caches the resolved tracer for the life of the process, so the SDK provider
  one tracing test installs can never be fully handed back, and any later test
  needing "no active trace" must pin `current_trace_id` itself rather than
  rely on ordering. This is why five failures unrelated to #3816's change
  appeared in its verification run.
  Method: on 2026-08-16, `pytest tests/unit/test_tracing.py
  tests/unit/test_request_id.py` on an **unmodified** checkout of `v3.0.0`
  reproduced the failures, while `pytest tests/unit/test_request_id.py` alone
  passed — isolating them to config leakage plus test order, not to any code
  change. Fixed by forcing the section off in `_isolate_test_log_dir`'s
  existing session-scoped config rewrite (which already has the crash-safe
  backup/restore).
  Standing guard: `test_session_config_keeps_tracing_disabled` fails at the
  cause instead of at the four downstream symptoms.
  Re-verify when: the tracing production default changes, or `conftest.py`'s
  config-rewrite fixtures are restructured.

- **V-027** · 2026-08-16 — A GitHub Actions expression interpolated into an
  `actions/github-script` `script:` body is **JavaScript source, not data**.
  Substitution happens before the script is parsed, so ordinary prose breaks
  it: the developer agent's fatal-error escalation step held
  `const phase3Diagnosis = '${{ steps.claude_result.outputs.diagnosis }}';`
  and an apostrophe in that free-form diagnosis terminated the literal, dying
  with `SyntaxError: Unexpected identifier 'issues'` (run 31959968196). The
  pipeline's own failure alarm was therefore silently disabled — two steps
  failed on #3815 and neither failure was reported anywhere. The same
  construct is an **injection surface**, not just a quoting bug: these steps
  carry `DEVELOPER_AGENT_TOKEN` / `SCRUMMASTER_AGENT_TOKEN` /
  `REVIEW_AGENT_TOKEN`, and whatever the substituted text parses as executes
  with that token. The audit found it was never confined to one step: **47
  interpolations across 6 workflows** — `developer_auto_implement.yml` (26),
  `handle_acceptance_failure.yml` (6), `notify_scrum_ready.yml` (6),
  `handle_improvement.yml` (4), `link_revert_pr_to_issue.yml` (3),
  `review_agent_auto_review.yml` (2) — including two other free-form-prose
  carriers — Phase 3's `recommendation`, and the scrummaster's multi-line
  `tried` list interpolated into a *template* literal, where a backtick or
  `${` in an issue title breaks out. Values now pass through `env:` and are
  read with `process.env.NAME`.
  Method: `scripts/agents/lib/escalation_script_probe.py` extracts the real
  `script:` body out of the workflow YAML and runs it under Node with
  `context`/`github`/`core` stubbed, so the JavaScript executed is the
  JavaScript Actions executes. Both halves, per **D-006**: the pre-fix form
  (env reads rewritten back into interpolated literals) dies with
  `SyntaxError: Unexpected identifier 's'`, and the current form escalates
  with an apostrophe, double quote, backtick, `${`, newline and backslash all
  intact in the posted body. Run 2026-08-16 on Linux and in
  `github-script-injection-smoke.yml`, which additionally proves the `env:`
  hand-off itself delivers hostile text to `process.env` uninterpreted, in a
  genuine github-script step on a runner.
  Standing guards: `scripts/agents/lib/workflow_script_guard.py` (fails on any
  `${{` in any `script:` body, tree-wide),
  `tests/unit/test_workflow_script_injection.py`, and the smoke workflow's
  planted-violation step — the guard must reject a seeded instance, so a
  scanner that silently stops scanning fails too.
  Re-verify when: a new `actions/github-script` step is added, or GitHub
  changes how `env:` values are delivered to the script sandbox.

- **V-028** · 2026-08-16 — **`anthropics/claude-code-action@v1` loads the
  repo-root `CLAUDE.md` into the agent's context as project instructions.**
  `CLAUDE.md` is therefore the runtime binding path for every agent in this
  repository — scrummaster, developer, review, huddle and `@claude` — and
  anything written there binds without being copied into a prompt. Two
  qualifications, both load-bearing:
  (a) it binds only where the job **checks the repo out** — the action reads
  project configuration from the working directory, so a `claude-code-action`
  step with no preceding `actions/checkout` in its job would be unbound (all
  10 invocations across 5 workflows currently have one);
  (b) on a **PR-context** run the action *restores* `CLAUDE.md` from the PR's
  **base branch** before starting Claude, parking the PR's version under
  `.claude-pr/`. A PR that edits `CLAUDE.md` therefore does not bind its own
  review — the change binds from the merge onward, never retroactively.
  Method: three independent lines, run/read 2026-08-16 on the runner —
  (1) **executed** — developer-agent run 31966671380 (`developer_auto_implement.yml`,
  `claude-code-action@v1`) opened with a system-reminder headed "Contents of
  /home/runner/work/nyxGPT/nyxGPT/CLAUDE.md (project instructions, checked into
  the codebase)" carrying the whole file, including text present in no prompt
  (the drain-gate rules, the `nyxgpt ops` inventory, the
  `web/src/app/admin/self-heal/page.tsx:366` violation note);
  (2) **source**, read at `/home/runner/work/_actions/anthropics/claude-code-action/v1`
  — `base-action/src/parse-sdk-options.ts:334-340` defaults `settingSources` to
  `["user", "project", "local"]` unless `--setting-sources` is passed, and
  `project` is the source that loads `CLAUDE.md`; `src/entrypoints/run.ts:261-272`
  calls `restoreConfigFromBase()` only when the context is a PR, with
  `src/github/operations/restore-config.ts:25-34` listing `CLAUDE.md` among the
  restored paths;
  (3) **configuration** — no workflow passes `--setting-sources`,
  `--system-prompt` or `--append-system-prompt` (tree-wide grep, 0 hits), so
  none opts out of the default.
  Repeatable: `.github/workflows/claude-md-binding-canary.yml`
  (`workflow_dispatch`) re-answers the question on demand. It injects a
  run-unique token into the checked-out `CLAUDE.md`, then proves both halves per
  **D-006** — the default run must return the token, and a control run pinned to
  `--setting-sources user` must not (a canary that cannot fail proves nothing,
  and a model that read the file instead of loading it would return the token in
  both).
  Re-verify when: the action is bumped past `v1` or its `settingSources` default
  changes, any workflow starts passing `--setting-sources`, or a
  `claude-code-action` step is added to a job with no `actions/checkout`.

- **V-029** · 2026-08-17 — A GitHub code-scanning **alert dismissal comment is
  capped at 280 characters**; a longer body is refused. Dismissal rationales
  must be written to that budget: name the sink, the reason the taint does not
  reach it, and the `file:line` that proves it — not the full argument. Six
  rationales drafted at 250–500 characters were all over the limit and had to
  be rewritten.
  Method: **owner-reported, not independently verified** (owner in session,
  2026-08-17, while dismissing alerts #115–#120). Weaker than the claim by
  necessity: dismissing an alert is a code-scanning *write*, and no agent token
  in this repo has that access — the supported agent path
  (`code_scan_report.yml`, `CLAUDE.md` § Tooling) is read-only, so the boundary
  cannot be tested from an agent session.
  Re-verify when: GitHub changes the dismissal form, or an agent gains
  code-scanning write access and can test the limit directly.
- **V-030** · 2026-08-17 — **Every agent script now stores an issue-to-issue
  link the way D-002 requires, and none of them writes the retired prose
  form.** `create_issue.sh --blocks N` calls `mark_issue_blocked_by` and does
  nothing else: it no longer reopens N, no longer applies a `blocks` label,
  and posts no `Blocks #N` / `Blocked by #N` comment. Its one consumer,
  `developer_submit_for_review.sh`, no longer scans comments for the retired
  marker, and no longer adds a second `Closes #N` for the blocked issue —
  which would have closed a feature that `promote_accepted_features.sh` is
  supposed to promote. **The gap this closes was between decision and code:**
  D-002 was taken 2026-08-12 (#3731) and recorded, while the script that
  contradicted it shipped unchanged for five days, documented in its own
  `--help` as the obvious way to link issues (#3836).
  Method: `tests/test_create_issue_blocks.sh`, run 2026-08-17 — the real
  scripts execute against a stub `gh` that records every call, asserting the
  POST to `issues/N/dependencies/blocked_by`, the absence of any reopen /
  label / comment call, and that the target issue's state is unchanged.
  Both directions per **D-006**: a copy of `create_issue.sh` carrying the
  pre-#3836 Step 4 (embedded in the test as a fixture) fails those assertions
  — it reopens the target and posts the prose markers — so the pass is not
  luck. `issue-relationships-smoke.yml` runs the suite and additionally greps
  every `scripts/agents/*.sh` executable line for the retired shapes; those
  greps were confirmed to match the pre-fix sources.
  Re-verify when: a new script or workflow links two issues, or `--blocks`
  grows a second write.

- **V-031** · 2026-08-18 — Native issue relationships (`blocked_by`) **are
  writable from a remote Claude Code session over REST**, even though GraphQL
  is restricted here to a pinned set of PR-review operations. The endpoint is
  `POST /repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by` with a
  `{"issue_id": <numeric id>}` body — the same call `mark_issue_blocked_by`
  makes (`scripts/agents/lib/gh_project.sh:2125-2135`). GraphQL being blocked
  is therefore **not** a reason to fall back to body prose for issue links.
  Method: executed 2026-08-18 from a remote session. `POST .../graphql`
  returned 403 with "only the pinned set of PR-review operations is served";
  six `blocked_by` edges were then written over REST (all 201) and confirmed by
  re-querying `GET .../dependencies/blocked_by` on each issue (#3853←#3861,
  #3854←#3850, #3857←#3853, #3859←#3850,#3854, #3861←#3860).
  Re-verify when: the session proxy's allowed-operation list changes, or GitHub
  moves issue dependencies off this REST route.

- **V-032** · 2026-08-18 — **`macos-brew-smoke.yml` does not exercise the user
  path.** Neither install job invokes `nyxgpt`, starts the stack, or issues an
  HTTP request. `keg-install` checks that `bin/nyxgpt-api` exists and runs
  `brew test`, whose `test do` block asserts only that the venv exists and
  `import nyxgpt.app` succeeds; `published-tap` verifies the keg version and
  wrapper file. A keg with no `nyxgpt` CLI passes both. **A green run on this
  workflow is not evidence that an install works** — it is evidence that a keg
  builds. This is how #3850 shipped and how the #3516 capstone closed green
  over an install path no user could complete.
  Method: read `.github/workflows/macos-brew-smoke.yml` (jobs at :70-506 and
  :508-586) and the `test do` blocks in `homebrew/nyxgpt-{api,web}.rb`,
  2026-08-18, against the owner's rc12 acceptance findings (#3850, #3853,
  #3854, #3857, #3859).
  Re-verify when: #3860 lands the end-to-end assertions, at which point this
  entry is superseded rather than re-verified.
- **V-033** · 2026-08-18 — **Only the Kubernetes mode wires the chat-session
  backend; every other install path runs the `file` default.** `k8s/configmap.yaml`
  sets `NYXGPT_SESSION_BACKEND=cassandra` (**V-012**), but nothing in
  `terraform/`, `docker/`, `src/nyxgpt/cloud_provision.py`,
  `src/nyxgpt/cloud_deploy.py` or `src/nyxgpt/ops.py` writes a session-backend
  setting, so a provisioned EC2 instance silently stores sessions as JSON files
  and the cross-mode shared-session guarantee (#3590) does not hold for the
  cloud mode — do not generalize **V-012** beyond k8s. Corollary from the same
  session: nothing except a RAG ingest (or a Cassandra-backend session write)
  creates the `nyxgpt` keyspace, and the RAG collections endpoints fail on its
  absence rather than degrade (`USE nyxgpt` inside `list_collections`; the
  create endpoint checks duplicates before `ensure_schema`), so a genuinely
  fresh Cassandra shows an unusable RAG Collections page until the first
  ingest.
  Method: tree-wide grep for `session_backend|NYXGPT_SESSION_BACKEND`
  (2026-08-18 — hits only in k8s/, docs/, and the config/session source), plus
  live reproduction on the owner's rc12 EC2 acceptance deploy: chats landed as
  files, both collections endpoints returned `Keyspace 'nyxgpt' does not
  exist`, and a single ingest recovered the page.
  Re-verify when: #3864 / #3865 land — each retires one half of this entry.
- **V-034** · 2026-08-18 — **The retrospective dumps' paginated-JSON defect was
  already fixed on `v3.0.0` before #3808 was worked; what remained was the
  failure being invisible.** `iter_json_objects` uses `idx = end` and lives in
  one place (`dump_spend.py`), imported by `dump_churn.py` and
  `dump_review_rounds.py`; `data/spend.json` and `data/churn.json` are present
  in the tree. Those fixes reached the release branch through the
  `claude/retro-data` publish path (commits `52b7255f`, `145d6040`, `d37e3a76`,
  `2659d64e`, `e5de6ff6`, `864abcc8`, `0f3688e8`, `d66bb9f0`, `6c2bf86f`,
  `8cda150e`), not through an issue PR, which is why the issue stayed open with
  its work apparently undone.
  Method: `git log --grep=3808 --all` and `git branch --contains` on each
  commit (all on `v3.0.0`), plus reading `scripts/retrospective/dump_spend.py`
  and `ls scripts/retrospective/data/`, 2026-08-18, while implementing #3808.
  Re-verify when: a retro dump fails again — check the run before assuming the
  helper regressed.
- **V-035** · 2026-08-18 — **A retrospective build with a missing input now
  fails loudly and says so on the page.** `build_dashboard.py` prints a
  `missing sources:` line naming each absent file and the dump that owes it and
  exits **2** (`--allow-missing-sources` to publish deliberately); the spend and
  churn panels render a "Section unavailable — this is missing data, not zero"
  notice linking that workflow's runs instead of being hidden. The old
  "the builder omits the section entirely rather than erroring" guidance in
  `REFRESH_RUNBOOK.md` steps 4/4b is retired.
  Method: ran `tests/test_retro_missing_sources.sh` (builds the real dashboard
  with `spend.json`/`churn.json` removed, then restored, and reads the rendered
  page back through `tests/retro_render_check.mjs`) — 27 assertions, 0 failures,
  2026-08-18; and confirmed the pre-fix template renders no notice at all under
  the same conditions.
  Re-verify when: the retro template's panel structure changes, or a new
  optional data source is added without a source stamp.

- **V-036** · 2026-08-18 — nyxGPT has **two config-reading tiers with different
  activation semantics**, and the difference is now data rather than folklore.
  The `api` tier re-reads `config.ini` per request through the hot-reload cache;
  the `web` tier is a Node process whose settings are read **once**, by the
  service wrapper (`_NATIVE_WEB_WRAPPER_TEMPLATE` in `ops.py`), and exported into
  its environment. Every key that wrapper reads is therefore frozen for that
  process's life: `[auth] api_key`, `[auth] enabled`, `[web] host`/`port`/
  `api_base_url`. Rotating `[auth] api_key` used to leave the web tier sending
  the old key into a silent 401 wall on every proxied call — including in the
  wizard session doing the rotating.
  Each config key now carries an **activation classification**
  (`FieldSpec.restart_components` in `config_wizard.py`): empty = hot-reloadable,
  otherwise the `nyxgpt ops restart` targets that stay stale. It drives the
  wizard's per-field hints, the persistent pending-restart notice on the wizard
  and Admin Dashboard, the `nyxgpt secrets setup` message, and
  `example.config.ini`'s `# Activation:` annotations — which are generated from
  it, with `tests/unit/test_restart_activation.py` failing on any drift.
  Pending state lives in `~/.nyxGPT/pending-restart.json`, not process memory,
  so the CLI writer and the API reader share one set and the notice survives an
  api restart. It retires on a real restart **or** on the value being reverted to
  what the service is still running.
  There are **three** writers of a restart-required key, not two — the
  Configuration Wizard (`POST /config/sections`), the Admin Dashboard's Access
  Management panel (`POST /admin/access` → `_apply_auth_config_updates`), and
  `nyxgpt secrets setup`. The dashboard one was missed on the first pass and
  rotated the key silently; all three now classify through
  `config_wizard.field_restart_components`/`restart_required_detail` and write
  the same `restart_state`, so a new writer that skips it is the failure mode to
  look for.
  Method: executed — `scripts/restart-activation-smoke.py` (run 2026-08-18, and
  wired into `.github/workflows/restart-activation-smoke.yml`) starts uvicorn and
  the web tier through the real generated wrapper, reproduces the 401 wall,
  asserts the notice/deferral/CLI-parity/dashboard-parity/restart/revert path,
  and includes the #3753 fault injection: with the classification stripped, no
  notice is raised.
  Re-verify when: the web wrapper stops reading a key from config.ini at start
  (e.g. if the proxy is ever made to resolve the key per request), or a third
  tier with its own activation semantics is added.

- **V-037** · 2026-08-18 — Two tests in
  `tests/unit/test_config_sections_endpoint.py` left a **live `threading.Timer`
  armed past their `patch` block**: they asserted the restart endpoints defer
  their work and then returned, so the timer fired seconds later, inside whatever
  test was running by then, calling the *real* `ops.restart` /
  `self_heal.heal_now`. On a developer machine that restarts actual services; in
  CI it silently corrupted an unrelated test's mock call counts
  (`test_ops_restart_all_ok` saw `_restart_launchagent` called twice). Fixed by a
  `captured_timers` fixture that records the scheduled timer instead of starting
  it.
  Method: executed — reproduced by running
  `test_config_sections_endpoint.py` before `test_ops.py` and observing the
  cross-file failure; the failure disappears with the fixture in place.
  Re-verify when: a new test asserts a `threading.Timer`-deferred endpoint —
  use `captured_timers`, never a bare "assert not called inline".
- **V-040** · 2026-08-18 — **An operator can now recover a cloud deployment's
  address and SSH target after the deploy's scrollback is gone, and read the
  instance's container state without a hand-rolled `ssh`.** `nyxgpt cloud
  status` is a first-class subcommand: human-readable by default (`--json` for
  the payload `nyxgpt cloud deploy --status` used to emit, which still works
  for anything scripted against it), and it prints `user@host` plus the
  identity file the deploy actually recorded — `tunnel_invocation()`, which had
  zero callers since it was written, now carries the raw `ssh` under a
  "diagnostics … run the wrapped command, not this" heading rather than as an
  instruction. `nyxgpt cloud ops {status,doctor,self-heal}` runs the instance's
  own read-only `nyxgpt` over the same wrapped SSH path `nyxgpt cloud
  credentials` uses. The SSH user/identity resolution for those read-only
  commands, and for `cloud tunnel`, is `resolve_access_target` — it fills from
  the deploy record what flags did not give, so a deployment made with a
  non-default key no longer needs `--identity-file` re-typed on every
  inspection.
  Method: executed — `scripts/cloud-status-smoke.sh` run 2026-08-18 against the
  installed console script and real files under `$HOME/.nyxGPT`, and wired into
  `.github/workflows/cloud-status-smoke.yml`. Three phases so a pass cannot be
  vacuous: no record → `UNKNOWN` and explicitly *not* "nothing is deployed"
  (the #3804 distinction); a record → the SSH target, identity file, URLs and
  the wrapped container-state command all printed, with no `docker compose`
  string anywhere in the output; an unroutable TEST-NET-3 host → `cloud ops`
  exits 1 naming `nyxgpt cloud allow-ip`, never a raw ssh/docker instruction.
  Re-verify when: the deploy record's field names change (`ssh_user`,
  `identity_file`, `host`), or a new `cloud ops` inspection is added — it must
  go in the `REMOTE_OPS_COMMANDS` read-only allowlist, not become a write path.

- **V-038** · 2026-08-18 — **A PR can be merged while the CI for its head
  commit is still running, and nothing re-examines the result.**
  `review_accept_and_merge.sh` validates state/mergeability/base-existence and
  then merges; it never reads the head SHA's check status, and no review
  workflow does either — "run CI checks on ALL code in the repository" is
  prose in `review-runbook.md`, evaluated by the reviewing model against
  whatever run it happened to see. Worked example: PR #3876 pushed `78c0e5cf` at 10:59:33Z,
  its `ci-tests` run was created at 10:59:37Z, and the PR merged at 10:59:38Z
  — one second later. That run finished **failing** at 11:05:49Z. The APPROVE
  had been computed against the previous push (`de50798d`), so the merge was
  green-by-staleness. This is what put a failing `pytest` on `v3.0.0`, and it
  is independent of *what* was failing.
  Method: read `scripts/agents/review_accept_and_merge.sh` end to end
  (2026-08-18) — no `statusCheckRollup`/check-runs call exists in it or in
  `review_agent_auto_review.yml`; timings from
  `gh api repos/.../actions/runs/32129497068` (created/updated) against
  `gh api repos/.../pulls/3876` (`merged_at`); failure text from that run's
  log. The check run `test` on `78c0e5cf` reads `completed / failure`.
  Re-verify when: a check-status gate is added to the merge path — this entry
  then describes history rather than the present. See **Q-005**.

- **V-042** · 2026-08-18 — **The `--kubernetes --local` stack is sized
  against the node it actually lands on — in BOTH memory and cpu — and the
  install measures that node before it applies anything.** The default
  deployment (app tier + data/LLM tier + the #3787 observability layer)
  reserves **6976Mi and 2075m**, down from 7872Mi and 2875m, against the
  **7936Mi / 4000m** allocatable a stock Docker Desktop VM reports; a canary
  rollout asks for a further 448Mi/150m and fits. **Memory was only the
  reported half:** with the memory right-sized, `nyxgpt-api-canary` still
  would not schedule on a 4-core node — `0/1 nodes are available: 1
  Insufficient cpu` — because four api replicas reserved 250m each. Fixing
  the named resource alone would have left the canary broken. Sizing is also
  not the whole fix, since an operator's VM is whatever they gave it, so
  `_preflight_k8s_capacity` (`src/nyxgpt/ops.py`) totals the rendered
  manifests per resource against allocatable minus other namespaces'
  requests and refuses *before* the first `kubectl apply`, warns when only
  the canary headroom is missing, and skips rather than blocks when it cannot
  measure (and warns rather than refuses on multi-node, where summed
  allocatable can disprove a placement but never prove one). Before #3825 the
  stack requested 8162Mi: every apply succeeded, the install reported
  success, prometheus was left `Pending / FailedScheduling: Insufficient
  memory`, and the later canary failure presented as "canary is broken".
  Method: executed on a real kind cluster on 2026-08-18, ballasted to 7936Mi
  allocatable (`scripts/k8s-node-ballast.sh` — a `pause` Pod reserving the
  surplus, since the runner has ~16GiB and would be green by luck; its 4 CPUs
  already match a Docker Desktop VM, so cpu needs no ballast). Observed, in
  order: the pre-#3825 memory sizing left `prometheus`, `loki` and
  `otel-collector` with no node and `Insufficient memory` events, and the
  preflight refused it ("requests 8256Mi but only 7646Mi is free … at least
  609Mi more"); the pre-fix cpu sizing with the memory fixed stranded
  `nyxgpt-api-canary` on `Insufficient cpu`; the shipped sizing scheduled all
  20 Pods and both canary Pods, and the preflight passed both resources
  (6976Mi/7646Mi free, 2075m/3050m free). `k8s-capacity-smoke.yml` runs all
  three phases; `k8s-local-smoke.yml` now runs the **default** install (no
  `--skip-observability`) on the same ballasted node. After the fact the state
  is observable: `infra_status()` reports `kubernetes.unschedulable` (Pods
  with an empty `.spec.nodeName`) and the Infrastructure page names them — the
  Pod list alone could not, since an unschedulable Pod and one pulling its
  image both read `Pending`.
  Re-verify when: a request/limit in `k8s/**` changes, or a workload is added
  to either kustomization — both gates and
  `tests/unit/test_k8s_capacity_preflight.py` fail loudly. Supersedes the
  measured footprint in **V-041**, which was taken on the runner's own
  16GB node before this right-sizing.

- **V-039** · 2026-08-18 — **A self-heal/infra probe reports "unknown" when it
  cannot run, and unknown is never counted as unhealthy.** `compose_probe()`
  answers availability by *running* `docker compose ps`, not by checking that
  `docker` and the compose file exist; a component whose state could not be
  determined carries `known=False`/`state="unknown"` plus the reason, is
  excluded from `unhealthy_count` and from the automatic heal pass, and is
  rendered as its own third state by the Self-Heal, Infrastructure and System
  Health pages — the last one because a zero `unhealthy_count` over an
  unqueryable probe is a green "all healthy" nothing established, which is the
  same defect pointing the other way.
  The pre-#3812 check (`_which("docker") is not None and COMPOSE_FILE.exists()`)
  is retired: it reported "available" for the condition it existed to catch.
  Method: ran `scripts/self-heal-probe-honesty-smoke.py` on a Linux docker
  engine, 2026-08-18 — it injects a root-owned mode-000 unix socket as
  `DOCKER_HOST`, asserts the pre-fix path really renders every desired service
  absent-and-unhealthy under that condition, then asserts the shipped path
  reports unknown-with-reason and heals nothing; the restored half starts a
  real prometheus container and asserts the same survey reports it running and
  the untouched services absent. Reverting the fix fails the injected half.
  Wired into `linux-native-smoke.yml` as the `self-heal-probe-honesty` job.
  Re-verify when: another probe (native/terraform/kubernetes/canary) starts
  reporting a definite state from an unqueryable source — the pattern, not just
  this call site, is what the entry stands for.

- **V-041** · 2026-08-18 — **The default `--kubernetes --local` stack (app +
  data/LLM + observability) fits a single 4-vCPU/16GB node**, and the reason
  the k8s smoke had been opting out of observability was not footprint but a
  missing wait. Measured on a kind cluster on the agent runner: with
  kube-system included the node carries **3825m of CPU requests (95% of
  allocatable)** and **8162Mi of memory requests (51%)**, every Pod scheduled,
  zero `FailedScheduling`. So CPU — not memory — is the binding dimension, and
  the margin is ~175m: a new workload requesting more than that leaves Pods
  Pending. The separate defect: `ops._k8s_stack_health` scores a Pod's *phase*,
  and observability Pods land in the same namespace still pulling images, so
  the default install reported failure on a healthy cluster until
  `_wait_for_k8s_observability` was added (#3826) — the same shape as **V-019**
  (an action's flag/effect halves disagreeing), and the reason a smoke that
  passes `--skip-observability` can be green while the real command is not.
  Method: executed on 2026-08-18 — `kind create cluster`, `kubectl apply -k
  k8s/` + `k8s/observability/`, then `kubectl describe node` (the numbers
  above), `--field-selector=status.phase=Pending` with `.spec.nodeName`
  populated on every Pod (scheduled, merely pulling), and no
  `FailedScheduling` event in any namespace. Standing guard:
  `scripts/k8s-local-smoke.sh` now runs the default install, asserts all ten
  observability workloads Ready, fails on any Pod the scheduler could not
  place, and prints the allocatable-vs-requests arithmetic every run.
  Re-verify when: any `k8s/**` manifest changes a `resources.requests`, a
  replica count, or adds a workload — the 175m CPU margin is what absorbs it.
  **Superseded in part by V-042** (#3825): the measured numbers above are the
  pre-right-sizing footprint, and they were taken on the agent runner's own
  ~16GB node, not on the 8GiB Docker Desktop VM an operator installs onto —
  where the same stack did *not* fit. The finding this entry stands for (the
  `_k8s_stack_health` phase/wait disagreement) is unaffected.

## Parked

- **P-001** · 2026-08-10 · owner — Intelligent test selection: scoping CI and
  the developer verification loop's test runs to a change's path/dependency
  impact (script/workflow-only diffs skip the full pytest + vitest suites, etc.).
  Reason: it is the largest recurring runner-spend multiplier in the pipeline —
  the dev workflow repeats both full suites on up to three fix attempts for every
  issue — but it is not v3.0.0 scope.
  Revisit when: Sprint 9 (nyxAgent-focused) grooming — file it there.
  Source: `product_management/AGENTIC_SDLC_DESIGN.md` §9a.

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
  Needs: owner decision on how much enforcement is wanted before it becomes
  ceremony.
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

- **Q-003** · 2026-08-18 · owner acceptance (#3857) — What stops the web UI's
  client JS from loading: the two builds racing for port 3000 (#3853), or a
  stale PWA service worker (`web/next.config.ts:62-65`)? Every endpoint was
  measured responsive while the UI showed permanent `next/dynamic` loading
  fallbacks, so the fault is client-side.
  Needs: DevTools → Application → Service Workers on a reproducing machine.
  The owner's machine was torn down before this was captured, so it must be
  reproduced from scratch.
  Blocks: #3857 — one branch of its fix is conditional on the answer.

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
