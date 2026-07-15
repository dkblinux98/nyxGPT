# Phase 8 — Make nyxGPT actually useful (RESERVED)

**Created:** 2026-07-15
**Owner decision (2026-07-15):** reserve a milestone for the work that makes the **core product**
genuinely useful. Owner assessment: *"right now as a chatbot it's practically useless."* The
project has built a great deal of surrounding machinery — sessions, RAG, admin/SRE dashboards,
deployment, observability — but the actual chat/assistant experience a user gets is not yet
valuable. Phase 8 is where that gap gets closed.

## Status: reserved — scope to be defined

Scope is **intentionally not enumerated yet.** It will be defined from the **full
stakeholder-spec review** the owner conducts after the v2.0.0 acceptance failures (#3177–#3196)
are resolved and the release is acceptable. This file is a placeholder so the milestone exists
and the intent is recorded; do not treat the candidate list below as committed scope.

## Framing

The distinction this milestone is about: everything to date has largely been *plumbing and
operations* (can we run it, deploy it, monitor it, heal it). Phase 8 is *product* — is the thing
worth using? The bar is not "the chat endpoint returns a response," it is "a user reaches for
nyxGPT because it does something valuable for them."

## Candidate directions (illustrative, NOT commitments — the spec review decides)

- **Response quality:** default models, system prompts, and generation settings that produce
  genuinely useful answers rather than a bare small-model echo.
- **Model strategy:** sensible default model tiers with resource guidance (see #3192), easy
  switching, and models actually suited to real tasks.
- **RAG that helps:** retrieval quality, citation UX, document management that a user trusts and
  benefits from — not just a pipeline that runs.
- **Tools / agentic capability:** letting nyxGPT *do* things (the existing tools_fs and any new
  capabilities) in a way that is safe and useful.
- **Conversation UX:** the actual chat experience — editing, regeneration, context handling,
  attachments, streaming feel — polished to the point of daily-driver quality.
- **Opinionated defaults:** out-of-the-box configuration that is immediately useful to a solo
  developer, rather than requiring deep tuning to get value.

## Sequencing

After Phase 6 (deploy) and Phase 7 (agent extraction), and gated on the owner's stakeholder-spec
review that turns "practically useless" into a concrete, prioritized backlog of product work.
