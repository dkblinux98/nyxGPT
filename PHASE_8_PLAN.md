# Phase 8 — nyxGPT as a self-hosted agentic coding system

**Created:** 2026-07-15 · **Concretized:** 2026-07-15 (owner)
**Owner vision:** turn the **Mac mini** into a **self-hosted, LAN-accessible agentic coding
system** that **replaces commercial systems like Codex and Claude Code**. This is what "make the
app actually useful" means: not a nicer chatbot, but a coding agent you own end-to-end, running
on a local coding-geared LLM, that improves itself over time.

## Access posture evolution (localhost → LAN, never public)

- **Now / Phase 6:** localhost-only (workstation-only). Perfect for the default single-user
  install; codified in the private-to-workstation constraint (`PHASE_6_PLAN.md`).
- **Phase 8:** relax to **LAN-accessible** — the Mac mini serves the agentic coding system to
  the owner's own LAN (other machines the owner controls), **still never publicly exposed.**
  This is a deliberate, owner-gated change to the earlier localhost-only rule; auth becomes
  mandatory (ties to #3177/#3195), and binding moves from loopback to a LAN interface behind
  required auth. Private-to-LAN, not public.

## Components

1. **Local coding LLM (via Ollama).** Replace the tiny `qwen2.5:0.5b` default with a
   coding-geared model — e.g. Qwen3-Coder / Qwen2.5-Coder-32B, DeepSeek-Coder-V2, or Codestral —
   sized to what the Mac mini can serve. Includes resource guidance (#3192) and easy switching.
2. **Agentic coding harness.** Grow nyxGPT from chat + read-only tools (`tools_fs`: ls/cat/grep)
   into a real coding agent with parity to Claude Code/Codex: file **editing**, command/test
   **execution**, a multi-step **plan → act → verify** loop, repo/context awareness, and safe
   sandboxing. This is the large product build of the phase.
3. **LAN access + multi-client.** Reachable from the owner's other LAN machines, auth required,
   never public. Hosted by the Phase 7 daemon.
4. **Self-improvement loop (see below).**

## Self-improvement — grounded, bounded, coding-first

Truly *recursive* self-improvement is frontier research, and even demonstrated systems "gain on
a single loop, then decay, because they can generate faster than they can verify." **But coding
is the tractable exception: outcomes are verifiable (tests/CI pass or fail), and nyxGPT already
owns a verifier (its pytest/vitest suites + CI).** That makes a *bounded* self-improvement loop
realistic here in a way it isn't for soft-metric domains. Practical, current-technique paths:

- **RL from execution feedback (RLEF):** the coding agent's solutions that pass tests/CI become
  positive signal; failing ones negative. Verification is cheap and reliable in code.
- **Self-play / task generation (Agent0-style, ICLR 2026):** one agent proposes progressively
  harder coding tasks, another solves them; train on the verified successes. Demonstrated to
  lift Qwen3-8B-class *base* models with no human-curated data — directly relevant to a local
  Qwen-based coder.
- **Continuous LoRA fine-tuning** on accepted diffs / passing solutions (learn the owner's
  codebase and preferences).
- **Skill/memory accumulation** (OpenClaw-style skills): a growing library of validated patterns
  the agent reuses — improvement without weight updates.

**Honest constraints:** the Mac mini can serve *inference* on a mid-size coding model, but the
*training/fine-tune* step of any self-improvement loop is compute-heavy — expect periodic
batch/burst training (a beefier local box or a cloud-GPU step for the fine-tune only, results
pulled back local), not continuous on-device training. Near-term target is a *bounded*
verification-gated loop (measurably better on the owner's own repos), not unbounded RSI.

## Sequencing

After Phase 6 (native deploy) and Phase 7 (the daemon that hosts the agents). The Phase 7 daemon
is the natural runtime; the local coding model and harness build on top. Scope refined by the
owner's stakeholder-spec review. This is the most ambitious phase — the coding harness is a big
build and the self-improvement is a research bet with a tractable near-term and an aspirational
long-term.
