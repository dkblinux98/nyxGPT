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

Four kinds. Each has a stable ID (`D`/`V`/`P`/`Q` + zero-padded number),
allocated by taking the next unused number in that class. **IDs are never
reused**, including after supersession.

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
  blocked: the review agent merges into the release branch with an agent PAT
  on every issue, so no ruleset bypass is needed to land automated data —
  only a PR. Generalisation for any future automation that writes to the
  release branch: **push to a side branch, land it through a PR**; and a
  green workflow run is not evidence its output landed — read the ref.
  Method: run 31941019009 (`Retro Dashboard - Dump Relationships`) rejected
  at the push with that exact GH013 text; the fix is reproduced on a runner
  by `tests/test_retro_data_pipeline.sh`, whose lab remote enforces the same
  rule in a `pre-receive` hook and proves the old push fails there and the
  new publish/merge path lands the JSON (standing job:
  `.github/workflows/retro-data-pipeline-smoke.yml`). The ruleset's own
  configuration is owner-readable only and was **not** read — the fact is
  established from the server's rejection, which is weaker than reading the
  rule but is the behaviour that matters.
  Re-verify when: the owner changes the branch ruleset, or an automated PR
  merge into the release branch is refused.
  Source: #3815.

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

