# nyxAgent: adopting current Claude Code practice

**Drafted:** 2026-08-22, by the executive assistant, at the owner's request,
against *The Claude Code Guide for Startups*
(<https://claude.com/blog/claude-code-guide-for-startups>).

**Status:** proposal for owner markup. Nothing here is filed, scheduled, or
implemented. It follows the publication pattern the owner named in
`NYXAGENT_SEPARATION_PLAN.md` §4 — the agent drafts, the owner marks up, the
marked-up version becomes the charter.

**Companion documents:** `NYXAGENT_SEPARATION_PLAN.md` (2026-08-15, what
nyxAgent is and what moves), `AGENTIC_SDLC_DESIGN.md` (the process doctrine),
`PHASE_7_PLAN.md` (2026-07-15, the *older* extraction mechanism — §4 below
proposes superseding part of it).

---

## 0. How to read this

The owner's framing was that the nyxGPT agent work predates most of these
Claude features, so the workflow is behind. That is half right, and the half
that is wrong matters more, so it is stated first.

- **§1** is what nyxAgent already does *better* than the guide. Do not spend
  markup or sprint capacity re-deriving these.
- **§2** is the ranked gap list — the actual answer to "what do we adopt".
- **§3** is one structural idea that changes how the separation itself is done.
- **§4** is where the guide conflicts with a standing nyxAgent decision, and
  why the standing decision should win.
- **§5** is the knowledge-base question, answered separately.
- **§6** is what needs an owner decision before anything can be filed.

Every claim about current repository state in this document was checked in
session on 2026-08-22 and cites the file it was checked against. Claims about
the owner's local machine are marked as unverified where they are.

---

## 1. Where nyxAgent is already ahead of the guide

The guide's §3 ("Trust, But Verify") is its strongest section and is the one
nyxAgent has already overshot. Specifically:

| Guide practice | nyxAgent's existing, stronger version |
|---|---|
| "Invest deeply in testing infrastructure" | The **executed-verification gate** (D-006 / `CLAUDE.md` §Executed verification): a behavior claim is not done until it has been *run on the target platform*, with fault injection where the runner would pass by luck. The guide never gets past "have tests." |
| "Put what can't change in root `CLAUDE.md`" | Done, and then *bounded* — D-021 scoped context loading after measuring that **97.3% of all tokens were context, not production** (2.26B over 30 days). The guide has no cost model for its own advice. |
| Golden sets / eval discipline for the product | The review gate, the reviewable-head gate (D-039), 90 CI workflows including targeted fault-injection smokes. |
| "Fix the principle, not the example" | `AGENTIC_SDLC_DESIGN.md` §9/§9a — *judgment at decision points, scripts as floors never ceilings* — is a sharper statement of the same idea, arrived at from an incident record rather than from a slogan. |
| — (guide has no equivalent) | **The operating ledger** (`agents/LEDGER.md`, D-005). The guide has no concept of durable cross-session memory at all; §9b of the design doc is genuinely novel relative to it. |
| — (guide has no equivalent) | **Cost as a first principle** (D-012). Churn cost and artifact-lifetime cost are measured and reported. The guide's cost discussion is "it's cheaper than headcount." |

**Implication for markup:** the guide is not a maturity ladder nyxAgent is
below. It is a catalogue of *mechanisms*, and nyxAgent has independently
derived most of the *principles* while adopting few of the mechanisms. The
gaps below are all mechanism gaps.

---

## 2. The gap list, ranked

Ranked by (value delivered) ÷ (cost to adopt), highest first.

### 2.1 Skills — the operating instructions are forked, and neither fork is the one that runs

**Verified state, 2026-08-22:**

- `.claude/skills/` contains exactly two skills (`issue`, `workflow-status`),
  173 lines together, both for interactive owner sessions.
- **No workflow under `.github/workflows/` references a skill, subagent,
  plugin, or settings file.** (`grep -rln "skills\|--agents\|subagent\|plugin\|settings.json\|output-style" .github/workflows/*.yml` returns nothing.)
- What the CI agents actually run is inline YAML prose: six
  `claude-code-action` invocations in `developer_auto_implement.yml` carrying
  **502 lines** of `prompt:` between them, and a single **368-line** prompt in
  `claude-code-review.yml` (lines 462–830).
- Meanwhile `agents/prompts/*.md` — 350 lines of role prompts — is referenced
  by `tests/unit/test_operating_ledger.py`,
  `tests/unit/test_first_principles_contract.py`, `docs/how-this-project-is-run.md`
  and `docs/sprint-autopilot.md`, and **by no runtime whatsoever**.

So the project maintains two copies of each agent's standing instructions: a
markdown copy that tests assert against and docs point at, and a YAML copy that
is what the model actually receives. They drift by construction, and the tested
one is the one that does not run. `reviews_final.json` already contains a review
finding noting exactly this drift on the scrummaster prompt.

The guide's phrasing is the fix, almost verbatim: *"Standing instructions are in
markdown files as skills, committed in a GitHub repository."*

**Recommendation.** Make `agents/` the skill payload and delete the fork:

1. Convert each role's charter + runbook + prompt into a **skill** with
   progressive disclosure — a short `SKILL.md` that the model always sees, with
   the long procedural material in referenced files it opens only when the task
   needs them. Today a developer run reads an 890-line runbook wholesale; a
   review run reads 1,041 lines. That is D-021's problem in its purest form and
   skills are the mechanism D-021 was reaching for.
2. Reduce each workflow's `prompt:` to the *situation* (issue number, PR
   number, what happened) and let the skill carry the *procedure*. The 870 lines
   of inline YAML prose become roughly a dozen.
3. Point the existing tests at the skills, so the thing that is asserted and the
   thing that runs are the same file.

**Why this is first:** it is the only item that simultaneously cuts context cost
(D-021's live concern), removes a class of drift defect that has already been
observed, and produces the artifact nyxAgent needs to be *distributable* (§3).

**Risk:** skill discovery in `claude-code-action` needs to be confirmed on a
real run before the inline prompts are deleted — do one role, prove it, then do
the rest. Do not convert all three in one PR.

---

### 2.2 Subagent fan-out for review — the review agent is one context doing five jobs

**Verified state:** the review agent is a single `claude-code-action`
invocation that must, in one context window, check correctness, the Definition
of Done's frontend-surface rule, the executed-verification rule, the
`nyxgpt`-wrapper rule, and security — then emit one structured verdict. Its
`--json-schema` (`claude-code-review.yml:838`) already separates
`critical_issues` from `medium_issues`, so the *output* is structured; the
*reasoning* is not.

The guide's version: *"custom code reviewers that fan out across a change,
review it from multiple angles, and synthesize the results the way one of your
senior engineers would but faster"*, plus adversarial verification of another
agent's findings.

**Recommendation.** Fan the review out into per-dimension subagents, each with
its own context window, then synthesize:

- The dimensions are **not invented** — they are the blocking rules `CLAUDE.md`
  already names: frontend surface (DoD), executed evidence (D-006), operational
  command wrapping, repo-less portability, plus correctness and security.
- Add an **adversarial verify pass** over each candidate finding before it
  reaches the verdict. This is the direct attack on the 3-strike REQUEST_CHANGES
  loop: every finding that turns out not to be real costs a full developer fix
  round plus a re-review, and `AGENTS.md` already documents disagreement type
  (b) "judgment call" as a recognised failure mode of the current reviewer.
- The **huddle** (#3687, D-029) is already this pattern applied *between*
  agents — a scripted trigger handing a contested decision to an agent that
  reads the whole thread and rules. Fan-out is the same idea applied *within*
  the review.

**Cost note:** more invocations per review, but fewer review *rounds*, and each
subagent reads a fraction of the context. Whether that nets positive is
measurable from `spend.json` and the review-round data — measure it on one PR
class before rolling it out. Do not adopt this one on faith; first principle 1
applies.

---

### 2.3 Hooks as hard gates — three review rounds that could be keystrokes

**Verified state:** `.claude/settings.json` defines two hooks (a PostToolUse
ruff/black formatter, a PreToolUse confirm-on-workflow-file). Both are for the
owner's interactive sessions. **Neither runs in CI** — no agent workflow passes
a settings file to `claude-code-action`.

The guide's framing is the useful one: hooks are *hard gates that execute every
time regardless of what the model decides*. nyxAgent enforces several such
rules today through the review loop instead — which means the enforcement costs
a full review round plus a fix round each time it fires.

**Candidates, each of which is currently a post-hoc check:**

| Rule | Enforced today by | Could be a hook |
|---|---|---|
| No `git rebase`, no force-push, no history rewrite (`AGENTIC_SDLC_DESIGN.md` §6, hard rule) | W3's acceptance criterion, grep-verified after the fact | `PreToolUse` on Bash — block the command outright |
| No raw `docker compose` / `kubectl` in user-facing strings (`CLAUDE.md` §Operational Command Wrapping) | **Medium, blocking review finding** | `PreToolUse` on Edit/Write — refuse the write, cite the rule |
| Ledger append rides in the PR that produced the fact (D-005) | Reviewer's memory | `Stop` hook — check whether the run's diff warranted an entry |
| Ledger entries appended at end, never reflowed mid-file (D-021 — mid-file edits invalidate every cached token after them) | Nothing | `PreToolUse` on Edit against `LEDGER.md` |

That last one is worth noting: D-021 identifies mid-file ledger edits as a
direct, measurable cache cost, and there is currently nothing preventing one.

**Recommendation.** Adopt hooks for rules that are already absolute — the ones
phrased as "never" in `CLAUDE.md` and the design doc. Do **not** hook anything
requiring judgment; that is exactly §9a's "floors, never ceilings", and a hook
is a floor by definition.

---

### 2.4 Evals for the agents themselves — the largest genuine hole

The guide: *"Every startup should maintain multiple sets of evals for their key
use cases"*, with instructions versioned and back-tested against a golden set
before shipping.

nyxAgent applies exactly this rigour to **the product** (D-006 is stricter than
the guide) and **none of it to the agents**. There is no way to answer "did that
change to the review prompt make reviews better or worse?" — which is why §2.1
and §2.2 above both carry "measure it first" caveats that currently have nothing
to measure with.

**This is also the missing precondition under §9a.** The doctrine moves
decisions from conditionals to judgment. Judgment that is not measured is not
obviously better than the conditional it replaced; it is only less legible.

**The golden set already exists as committed data.** Verified in session:

- `scripts/retrospective/data/reviews_final.json` — **397 review records**,
  each `{issue, pr, module, date, critical[], medium[], minor[]}`.
- `scripts/retrospective/data/all_issues.json` — every issue with its label,
  including `Acceptance Failure`.
- `scripts/retrospective/data/relationships.json` — the native blocked-by
  edges linking a failure back to the feature it was filed against.

Joining those three yields the label the eval needs: *this PR was reviewed,
approved and merged, and then an acceptance failure was filed against its
issue* — i.e. **a review that missed something the owner later caught**. That is
a real, negative-labelled corpus of the project's own history, and it requires
no new API calls, which keeps `AGENTIC_SDLC_DESIGN.md` §10's non-goal intact.

**Recommendation.** Build a review-agent eval from that join; gate every future
change to review skills/prompts on it. Start with recall on known misses — the
question "would this reviewer have caught the thing that reached acceptance?" is
answerable today and is the one that matters.

---

### 2.5 Plan mode / `/goal` — cheap, small, uncontroversial

- **Plan gate before implementation.** The guide calls plan mode *"the cheapest
  place to catch a rebuild that's about to drift from your architecture."*
  nyxAgent grooms at the *sprint* level (the plan doc, §4 of the design) and then
  goes straight from issue assignment to implementation. For Effort ≥ M, having
  the developer publish a plan before writing code gives the review agent its
  cheapest possible reject — before there is a diff to argue about.
- **`/goal` for long-horizon runs.** The guide names it for tasks where the
  model terminates early, prefers its own findings on review, or drifts. The
  3-strike escalation detects those outcomes *after* three expensive rounds;
  `/goal` is an in-run mitigation. Low cost, worth trying on the developer
  implementation leg.

---

### 2.6 Claude Tag for the escalation channel and the SRE agent

`AGENTIC_SDLC_DESIGN.md` §9 design consequence 3 requires that **escalations
reach a human channel, never only a thread comment** — written after a FATAL
self-diagnosis with the complete correct remedy sat unread in an issue thread
for eight hours. Slack DM alerts (#3695) partially cover this.

`PHASE_7_PLAN.md` additionally specifies a fourth, proactive **SRE agent** to be
built from scratch.

The guide describes Claude Tag doing exactly that job — a service account with
monitoring-tool access, standing instructions as committed markdown skills,
first situation report within 15 minutes of an incident. Before nyxAgent builds
an SRE agent, it is worth establishing whether Claude Tag *is* the SRE agent,
with nyxAgent supplying the skills.

**This is an evaluation, not a recommendation** — I have not checked Claude Tag
against this project's requirements, and the identity/attribution constraint
(agents must be the actors, under their own identities) is the thing most likely
to decide it.

---

## 3. The structural idea: nyxAgent ships as a plugin, not a submodule

This is the item with the largest consequence for the extraction, and it needs
an owner decision because it partially supersedes a documented plan.

**`PHASE_7_PLAN.md` (2026-07-15)** chose *submodule + reusable workflows*, and
spends a section on its own hard constraint: **GitHub does not fire workflows
that live in a submodule**, so the plan needs either a sync-generator or a
reusable-workflow conversion. That constraint is real and unchanged.

**`NYXAGENT_SEPARATION_PLAN.md` (2026-08-15)** moved past submodules to a
separate repository with a tracker adapter — but §8 still leaves repository
topology and nyxAgent's own distribution as open questions.

The guide names the mechanism neither document had available: a **company
plugin marketplace**. A plugin's payload is precisely what nyxAgent is —
skills, subagents, hooks, slash commands, MCP server definitions, settings —
installed into a consuming repository without vendoring anything.

**What that resolves:**

- **The submodule workflow constraint mostly evaporates.** Once §2.1 has moved
  the procedure out of inline YAML and into skills, the workflows are thin
  triggers. Thin triggers are cheap to keep in the customer repo; the payload
  ships as a plugin and versions independently. The sync-generator disappears.
- **It satisfies nyxAgent's own portability doctrine.** `CLAUDE.md` §Repo-less
  Portability requires nyxGPT be installable without a checkout, and the
  separation plan's §8 asks whether nyxAgent should follow that rule from day
  one. A plugin marketplace is the answer that makes it true by construction —
  a customer project installs nyxAgent, never clones it.
- **It gives the tracker adapter a natural seam.** Per-project configuration
  (repo, board, release branch, lane names, cadence) is plugin configuration.
  nyxGPT's profile is the first one written, exactly as the separation plan
  describes.

**Recommendation for markup:** treat "nyxAgent distributes as a plugin" as the
default topology, and re-scope `PHASE_7_PLAN.md`'s submodule/reusable-workflow
sections as superseded — with the explicit caveat that the **Actions triggers
themselves** still live in the customer repository under any of these designs,
and that the plan's separate direction (move the pipeline off Actions entirely,
onto a `nyxagent` daemon) is orthogonal to this and unaffected.

---

## 4. Where the guide should lose

Two of the guide's practices conflict with decisions this project made
deliberately. Recording them here so a future session reading the guide does not
re-propose them as improvements.

**4.1 "Everyone ships" — largely not applicable, and its local translation
already exists.** The guide's first principle is about non-technical colleagues
prototyping. nyxGPT/nyxAgent has one human. The *useful* translation of that
principle is already the owner's own priority-one item:
`NYXAGENT_SEPARATION_PLAN.md` §4's human-like interaction model — voice huddles,
in-meeting note-taking, publication after the room empties. That is the same
goal (lower the tax on the human) aimed at the actual constraint. Nothing in the
guide's §1 improves on it.

**4.2 Git worktrees for parallel agents — do not overturn WIP-2 on a blog
post.** The guide recommends worktree isolation so many agents work in parallel.
`AGENTIC_SDLC_DESIGN.md` §6 deliberately chose the opposite: WIP limit 2, a
file-overlap check, and the explicit statement that *"scheduling is the entire
conflict-mitigation strategy; merge conflicts should be rare by construction
rather than resolved after the fact."* That is a considered decision backed by
this project's own conflict incidents (D-011).

The one place worktrees genuinely earn their keep is the guide's *other* use for
them: the "build it, build it again" rebuild pattern — running v2 beside v1 and
merging only when v2 wins on evals. **The nyxAgent extraction is itself exactly
that rebuild**, so worktrees are worth adopting for the extraction work
specifically, without touching the steady-state WIP rule.

---

## 5. The knowledge base for idea discussions and URLs

The second question — how to capture the ideas and links scattered across
separate Claude Code sessions — is the ledger's problem statement generalised.
The ledger (D-005) solved it for *decisions*. This is the same failure for
*inputs to decisions*: material the owner encountered, reacted to, and lost when
the session ended.

### 5.1 It must not go in the ledger

The ledger is read **in full on every agent run**, which makes every line a
recurring per-run cost, and Q-001 already has an open owner directive to *split*
it into a hot ledger and an on-demand archive because it has outgrown "cheap to
read." Adding an unbounded stream of exploratory links to it would be the
opposite of that directive. The knowledge base is a separate corpus that is
**never** on the bootstrap path.

### 5.2 Proposed shape

**Location:** `agents/knowledge/` today — inside `agents/`, so it moves to
nyxAgent automatically under `NYXAGENT_SEPARATION_PLAN.md` §5. This material is
about how to build agents; it is nyxAgent's memory, not nyxGPT's.

**Structure — reuse the pattern that already works here.** One file per
source or topic, plus a generated `INDEX.md` of one line each. This is exactly
`agents/CONTEXT_INDEX.md`: built by a script, guard-tested against drift
(`tests/unit/test_context_index.py`), never hand-edited, and explicitly designed
as *"the map — read it instead of the territory."* Do not invent a second
retrieval convention when the project already has one that is proven and
enforced.

**Capture must be one gesture.** A `/capture <url> [note]` skill that, in a
single command: fetches the source, writes a distilled note, files it,
regenerates the index, and commits. If capture costs the owner more than pasting
a link and hitting enter, it will not happen — and *the agents type* is the
standing principle (`NYXAGENT_SEPARATION_PLAN.md` §4).

**Store the distillation, not the page.** Each note carries: the URL, the date,
who brought it and why, **what it claims**, **what it would mean for this
project**, and an explicit status line (`unreviewed` / `parked` / `promoted to
D-0NN` / `rejected`). Never the full page text — archiving pages recreates the
97.3%-context problem D-021 exists to bound. If raw capture is wanted, keep it
under a `raw/` directory that nothing reads by default.

**The promotion path is the whole point.** A note is an *input*. When it drives
a decision, that decision becomes a ledger `D-` entry and the note is stamped
with its number. A note that has driven nothing after two grooming passes is
promoted, parked with a revisit condition, or deleted. Without that rule the
knowledge base becomes a second unread ledger within a quarter — which is the
predictable failure mode and the one thing that would make this proposal
harmful rather than neutral.

**Retrieval is grep plus the index, not RAG.** The guide makes this point
against its own industry's instinct: *"Avoid RAG complexity; keep approaches
simple using file-based methods observed in Claude Code."* nyxGPT ships a RAG
stack with a Cassandra store, which makes it a standing temptation to use it
here. It should not be used here: the corpus is small, the index is cheap, and
a retrieval layer would need its own operational surface, its own failure modes
and its own Definition-of-Done frontend under `CLAUDE.md`'s rules.

### 5.3 Recovering what is already scattered

The existing discussions are not lost. Claude Code writes a full JSONL
transcript per session under `~/.claude/projects/<slugified-cwd>/<session-id>.jsonl`
— verified in this container on 2026-08-22; **it needs confirming on the owner's
Mac before anything is built on it**, since that is where the sessions in
question ran.

A one-time backfill can extract every URL from those transcripts along with the
owner's surrounding prose, group by source, and stage the results as draft notes
for triage. Without it the knowledge base starts empty and the material the
owner is actually asking about stays where it is. This is a small script and it
should be part of the same piece of work, not a follow-up.

### 5.4 What this is not

It is not a wiki, and it should not accumulate summaries of things nobody
decided anything about. The measure of whether it is working is the count of
notes that became `D-` entries — not the count of notes.

---

## 6. What needs an owner decision before anything is filed

1. **§3 — does nyxAgent distribute as a plugin?** This changes the extraction
   mechanism and supersedes part of `PHASE_7_PLAN.md`. Everything else in this
   document is compatible with either answer, so this is the one that gates
   sequencing.
2. **§2.2 and §2.4 ordering.** Evals (§2.4) are the precondition for safely
   changing the reviewer (§2.2). Recommend §2.4 first even though §2.2 is the
   more visible win — the alternative is changing the reviewer and having no way
   to know whether it got better.
3. **§2.6 — is Claude Tag evaluated as the SRE agent**, or is the SRE agent
   built as `PHASE_7_PLAN.md` specifies? Identity/attribution is likely the
   deciding constraint.
4. **§5.2 — location.** `agents/knowledge/` assumes the knowledge base is
   nyxAgent's. If the owner intends it to span more than this project (personal
   research notes, product ideas for other work), it wants its own repository
   and the answer changes.
5. **Sequencing against Sprint 9.** Every item here is nyxAgent work and lands
   in the same bank as the separation plan's §7. None of it should displace the
   interaction model (§4 of that plan), which the owner sequenced explicitly as
   priority one.

---

## 7. What was deliberately not done

No code, workflow, skill, or configuration was changed in producing this
document. Per `CLAUDE.md` §Operating Mode, a change the owner wants made goes
through the agent process — issue, selection, implementation, review — and this
document is the input to that, not a substitute for it. The items above are
written to be groomable but are not filed: filing is the scrummaster's job once
the owner has marked this up.
