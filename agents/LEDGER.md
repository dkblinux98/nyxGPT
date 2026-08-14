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
before "cleaning it up". The 2026-08-14 incident that motivated this file was an
`Acceptance Failed` sweep that reclassified the owner's deliberately parked
failure markers as stale board state and destroyed them.

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
  GitHub repository setting itself (see Q-002).
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

- **V-003** · 2026-08-14 — `3.0.0rc7` is burned dead and will never be reused.
  Two rc dispatches 34 seconds apart on 2026-08-14 cut `3.0.0rc7` and `3.0.0rc8`
  from an identical tip: the tip-unchanged no-op guard reads the publish
  workflow's *finished* run history, so two runs overlapping before either had
  published both read "no candidate for this tip". `release-publish-pypi.yml` now
  serializes cuts with a non-cancelling concurrency group — a queued cut waits
  rather than racing, and must never be cancelled mid-upload.
  Method: read the "ONE CUT AT A TIME" comment block and `concurrency:` stanza in
  `.github/workflows/release-publish-pypi.yml` (#3771).
  **Which candidate is current is deliberately not recorded here** — that is
  live state; read the PyPI project page or the publish workflow's run history.
  Re-verify when: the publish workflow's concurrency control changes.

- **V-004** · 2026-08-07 — Agents **can** create and modify files under
  `.github/workflows/`. Every `claude-code-action` invocation in the agent
  workflows passes `github_token: DEVELOPER_AGENT_TOKEN`, a classic PAT carrying
  the `workflow` scope, so pushes to workflow files succeed. The refusals
  observed on #3642 came from the action's built-in App-mode capability text,
  which the implement prompts now explicitly override — not from a real
  permissions wall. Do not hand-carry a workflow-file issue on that basis; if a
  push is genuinely rejected, diagnose the actual error.
  Method: read the `github_token:` inputs across `.github/workflows/*` and the
  workflow-files exception paragraph in `developer_auto_implement.yml`.
  Re-verify when: the agent workflows change their token source, or
  `DEVELOPER_AGENT_TOKEN` is rotated to a token without `workflow` scope.

- **V-005** · 2026-08-14 — An agent session cannot call the code-scanning API
  directly. The supported path to code-scanning state is to dispatch
  `.github/workflows/code_scan_report.yml` (`workflow_dispatch`, optional `ref`)
  and read its run log, which prints recent analyses, the open-alert list with
  `TOTAL_OPEN`, and every SARIF codeFlow per open alert. CodeQL default setup
  scans only the default branch plus PRs, so a non-default branch's alert list is
  frozen until that branch becomes default and receives a push.
  Method: read `.github/workflows/code_scan_report.yml`; `CLAUDE.md` §Tooling.
  Re-verify when: the repository moves off CodeQL default setup, or agent tokens
  gain `security_events` scope.

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

- **Q-002** · 2026-08-14 · developer-agent (#3774) — What check can an agent
  session run, without owner help, to confirm the repository's release
  immutability setting directly rather than inferring it from the pipeline built
  around it (V-001)?
  Needs: owner, or a documented API/CLI path reachable with the agent token.
  Blocks: nothing; V-001 is honest about the gap in the meantime.

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

- **S-003** — ~~"The developer agent's GitHub App cannot write workflow
  files."~~ Superseded 2026-08-07 by **V-004** — the agents authenticate with a
  PAT carrying `workflow` scope. This belief caused workflow-file issues to be
  hand-carried unnecessarily.

- **S-004** — ~~"`Related feature: #N` in the issue body is how issues are
  linked."~~ Superseded 2026-08-12 by **D-002** — native relationships only. Still
  read as a fallback for issues filed before that decision; never written.
