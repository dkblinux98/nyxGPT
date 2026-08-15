# nyxAgent: the Separation Plan

Drafted 2026-08-15 from the owner–assistant working sessions of 2026-08-13
through 2026-08-15. This is the post-meeting publication: the owner marks it
up, and the marked-up version becomes the Sprint 9 charter. Nothing in it is
scheduled until that markup happens.

---

## 1. Why now

Sprint 8 was chartered to make cloud deployments work. Its actual output
tells a different story: the majority of merged work this sprint was
agentic-process machinery — the drain gate, the operating ledger, the
executed-verification review gate, the comment-token gate, the promotion
sweep's dual-lane semantics, churn metrics, retry-loop containment — all
wearing nyxGPT issue numbers, all consuming nyxGPT sprint capacity, all
reviewed against nyxGPT's Definition of Done.

That work was necessary and most of it is good. But it is not nyxGPT. It is
a second product being built inside the first one's repository, billed to
the first one's roadmap, and invisible in the first one's metrics except as
noise. The costs of the entanglement are now measured, not felt:

- The sprint's process-vs-product issue ratio is the quantified form of
  "script growth indicates missing intelligence" — and this sprint it
  inverted hard toward process.
- Process defects (a retry loop, an escalation race) burn the product's
  runner budget and flood the product's issue threads.
- The release machinery cuts product candidates in response to process
  merges (rc10/rc11 carried no product change), because the pipeline cannot
  tell the two apart — nothing in the system can, structurally.
- Agent-process issues needed a standing exception to the product's own
  drain gate to be workable at all. A standing exception is a seam
  announcing itself.

The separation makes the second product real: **nyxAgent**, the agentic
SDLC team, with nyxGPT as its first customer.

## 2. What nyxAgent is

nyxAgent is the team, packaged: a scrummaster, developers, and reviewers
that run a software project's delivery loop — selection, implementation,
review, merge, acceptance flow, release — under a human owner who supplies
judgment, priorities, and acceptance, and who is treated the way a good
team treats its lead: briefed, consulted, never made to do the typing.

nyxGPT remains what it is: a local-first, private AI system. After the
separation it carries no agent machinery — it is operated *by* nyxAgent the
way any customer project would be.

## 3. First principles (carried in from the Sprint 8 doctrine)

These are design constraints, not aspirations. They come from the owner's
own practice and from this sprint's incident record, and they are already
codified in `product_management/AGENTIC_SDLC_DESIGN.md` §9/§9a:

1. **The agent is always in the room.** Deterministic triggers and scripts
   never act alone at a decision point; they invoke intelligence that reads
   the current situation first. The retry-loop incident — hundreds of
   cycles of machinery answering itself with the model never once invoked —
   is the permanent counterexample.

2. **Scripts are crystallized experience.** Automation is earned by doing
   the work with judgment until its failure modes are boring, then
   crystallized — and even then run under supervision before running
   unattended. Automating at experience level zero is the rookie inversion,
   and the growth rate of the script count is a health metric (falling is
   good).

3. **Floors, never ceilings.** Scripts define what may never happen;
   judgment picks what happens. The publish pipeline's guardrails
   (serialized cuts, tip-unchanged, ceremony gates) are the model.

4. **The system of record carries the memory.** Sessions and agents are
   stateless; ledgers, boards, and documents are not. A claim not recorded
   and not freshly verified is not asserted. (Public machine-facing ledger;
   owner-private annex; both proven this sprint.)

5. **Execution is the only evidence.** Nothing reaches the owner's
   acceptance that has not been demonstrated by running it on the target.
   Where CI is green by environmental luck, inject the failing condition
   and show both halves.

6. **The economics are visible.** Churn cost (per-session re-onboarding)
   and artifact-lifetime cost (the pound-foolish term: every guard, script,
   and issue breeds review rounds, maintenance, and interactions) are
   measured and reported, not laundered into "usage" and "rework."

## 4. Priority one after the separation: the human-like interaction model

Sequenced explicitly by the owner (2026-08-15): this is the first nyxAgent
capability after the split itself, because the current interaction model —
the owner typing prose at the system all day — is the largest tax on the
human in the loop.

The model is how good teams already work:

- **Live interaction is a meeting, and it is conversational.** The owner
  speaks — voice huddles — and agents participate the way colleagues do:
  listening, answering, asking. Nobody dictates polished prose at each
  other in a meeting. The existing huddle protocol (text mediation between
  agents) extends to include the owner by voice.
- **Someone takes notes.** During the meeting, an agent keeps the notes —
  decisions, action items, open questions — without the owner writing
  anything.
- **Publication happens after the room empties.** The polished artifact —
  plan, spec, decision record, this very document — is drafted offline from
  the notes, published for reading, and improved through a collaborative
  markup loop. The owner reads finished documents gladly; the owner does
  not produce documents live.
- **Briefings flow the same way.** Board state, release readiness, retro
  results arrive as things the owner can consume conversationally or read
  as short finished briefs — never as raw lanes, log tails, or comment
  threads to be excavated.

The agents type. The owner talks, listens, reads, and decides.

## 5. What moves, what stays

**Moves to nyxAgent** (today's locations in the nyxGPT repo):

- `agents/` — charters, prompts, runbooks, the public operating ledger's
  machinery (the ledger *content* splits per project: each customer project
  carries its own).
- `scripts/agents/` — the whole library: selection, review-merge, drain
  gate, promotion sweep, huddle, relationships, retro tooling.
- The agent workflow suite in `.github/workflows/` — implement, review,
  scrummaster, drain gate, promotion, ceremony, comment-token gate,
  admin/board tooling, gh_query, retro pipelines. (The product keeps its
  own CI: tests, smokes, CodeQL, release-artifact builds.)
- `scripts/retrospective/` and the retro data pipeline, extended with the
  artifact-lifetime metrics (§3.6).
- `product_management/AGENTIC_SDLC_DESIGN.md` — becomes nyxAgent's design
  document.
- The Sprint 9 grooming bank: the trigger-judgment gate; the judgment-based
  rc-cut decision (product-only clearance, process-blind, agent-invoked,
  no human hand-dispatch); intelligent test selection; module
  classification by scrummaster judgment instead of keyword grep; the
  curious-reader watcher; huddle/escalation semantics; the W-series
  SDLC items.

**Stays in nyxGPT**: everything a user installs or operates — source, web,
ops, formulas, k8s, terraform, docs, product CI and release pipelines — and
the product's own boards, plans, and vision under `product_management/`.

## 6. The seam

- **Tracker adapter** (owner decision, banked this sprint): nyxAgent talks
  to work tracking through one interface, with GitHub Projects as the first
  driver. Board names, lanes, labels, and relationship storage live behind
  it. Nothing in nyxAgent's logic names a GitHub-specific concept outside
  the driver.
- **Per-project configuration**: repository, board, release branch, tokens,
  lane names, owner cadence — the things today hard-wired as `vars.*` and
  conventions — become a customer-project profile. nyxGPT's profile is the
  first one written.
- **Identity and spend**: agent identities (the PATs, the bot accounts) and
  runner spend attach to nyxAgent, so the churn and artifact-lifetime
  accounting lands on the right product's books.
- **The exception disappears**: with two products, "agent-process issues
  bypass the product's drain gate" stops being an exception and becomes the
  ordinary fact that nyxAgent's work is tracked on nyxAgent's board.

## 7. Shape of the work (for grooming, not yet scheduled)

1. **Stand up the nyxAgent repository and board** (the board is already
   cloned from the nyxGPT template). Move the design doc and the grooming
   bank; open the product's ledger.
2. **Lift and re-point**: move `agents/`, `scripts/agents/`, and the agent
   workflows; parameterize the hard-wired nyxGPT references into the first
   project profile. nyxGPT keeps running under nyxAgent throughout — the
   customer must not notice the move.
3. **The interaction model** (§4) — first capability built *as* nyxAgent:
   voice huddle with the owner, in-meeting note-taking, post-meeting
   publication with markup loop, and conversational/brief-form reporting.
4. **The banked §9a implementations** follow — trigger-judgment gate and
   the rc-cut decision function — now as nyxAgent features serving any
   customer project.

Sequencing beyond item 3 is Sprint 9 grooming's job, held to the same
discipline the doctrine demands: judgment first, crystallize later.

## 8. Open questions for markup

- Repository topology: one `nyxagent` repo, or repo + separate tap/registry
  for its own distribution? Does nyxAgent itself follow repo-less
  portability from day one?
- The voice channel's mechanics: which surface carries the huddle (the
  Claude apps' voice mode, a nyxAgent-native interface, something else),
  and what is CI-coverable about it?
- Where nyxGPT's *product* ledger ends and nyxAgent's *operational* ledger
  begins — one file per project, or a ledger service the adapter fronts?
- Migration of open history: do in-flight agent-process issues (#3784-class)
  finish on nyxGPT's board or transfer?
- Licensing/visibility: is nyxAgent public from birth the way nyxGPT is?
