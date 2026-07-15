# Phase 7 — Extract the agent system into nyxAGENT (submodule)

**Created:** 2026-07-15
**Owner decision (2026-07-15):** extract all the agent / GitHub-workflow / Claude-agent
machinery out of nyxGPT into a separate **`nyxAGENT`** repository, then consume it back in
nyxGPT as a **git submodule (sub-repo)**. Goal: the multi-agent scrummaster/developer/review
pipeline becomes a reusable, independently-versioned component rather than being welded into the
application repo.

## What moves to nyxAGENT

- **Agent orchestration workflows** (the pipeline, not the app CI): `notify_scrum_ready.yml`,
  `developer_auto_implement.yml`, `claude-code-review.yml`, `review_agent_auto_review.yml`,
  `assign_backlog.yml`, `ensure_project_hygiene.yml`, `handle_acceptance_failure.yml`,
  `usage_limit_retry.yml`, `admin_label_rename.yml`, `auto-check-tasklist.yml`,
  `add-to-release-issue-on-milestone.yml`, `link_revert_pr_to_issue.yml`,
  `notify-merge-conflicts.yml`, `manually_trigger_pr_review.yml`, `claude.yml`.
- **Agent definitions & docs:** `AGENT_CHARTERS/*`, `AGENT_PROMPTS/*`, `RUNBOOKS/*`, `AGENTS.md`,
  and the agent-generic portions of `CLAUDE.md`.
- **Agent tooling:** `scripts/agents/*` (create_issue.sh, developer_*.sh, review_*.sh,
  scrummaster_*.sh, `lib/gh_project.sh`, validate helpers that are agent-workflow-specific).
- **Claude config:** the agent-relevant parts of `.claude/*` (hooks, settings) — TBD which are
  generic vs nyxGPT-specific.

## What stays in nyxGPT

- All application code (`src/`, `web/`, `docs/`, `k8s/`, `docker/`, `terraform/`, `ops/`).
- App CI that is nyxGPT-specific: `validate-web-routes.yml` (type-check + vitest + route
  validation) stays in nyxGPT's own `.github/workflows/`.
- nyxGPT-specific operating instructions in `CLAUDE.md` (Definition of Done, ops-wrapper
  principle, deployment model, branch/PR rules) — the app-repo layer on top of the generic
  agent instructions pulled from nyxAGENT.

## Direction (owner, 2026-07-15): move the pipeline OFF GitHub Actions

Rather than keep the pipeline on GitHub Actions and fight the submodule constraint below, the
owner wants to **explore running the agents without GitHub Actions at all** — which also makes
nyxAGENT genuinely portable. Options under consideration:

1. **`nyxagent` orchestrator on the Claude Agent SDK, run via launchd (recommended).** A
   standalone package that polls GitHub (via `gh`) and runs scrummaster/developer/review as
   Claude Agent SDK invocations, committing/pushing/PR-ing via `gh`. Lives entirely in nyxAGENT
   (portable — no Actions, no submodule-workflow problem), runs on the workstation via launchd
   (matches native-first), driven by a `nyxagent` wrapped command (ops-wrapper principle).
   Bonuses: agents run under their own PATs so attribution is *fully* correct (the identity
   requirement), and tokens move from Actions secrets to local config/keychain (folds into the
   config single-source-of-truth, #3194). Cost: runs where the owner runs it (launchd while the
   Mac is on, or a small VPS for 24/7) instead of free GitHub-hosted runners.
2. **Local daemon + webhooks** — event-driven, but needs a public endpoint (tunnel/host).
3. **Scheduled poller** — launchd/cron (or Claude Code Routines); simplest, slight latency;
   effectively option 1 with a poll trigger.

**App CI stays on Actions** (`validate-web-routes.yml`); only the *agent orchestration* moves
off. Recommended: option 1. This supersedes the reusable-workflows idea below, which only
applies if the pipeline *stays* on Actions.

### Reference architecture: OpenClaw (and the "living agents" goal, owner 2026-07-15)

OpenClaw (open-source persistent personal-AI-agent daemon) is the reference design for options
1–3: a long-running daemon (local machine or VPS) that hosts the agent loop, connects to 12+
**messaging platforms**, and adds a **heartbeat scheduler**, **cross-channel session
management**, and **persistent memory** — and for coding it wraps Claude Code / Codex / OpenCode
via adapters with a git-**worktree** strategy. Two adoption paths:

- **Adopt OpenClaw as the runtime** and register the nyxGPT scrummaster/developer/review agents
  as OpenClaw *skills* (it already has a skills system + coding-agent skill) — fastest path;
  OpenClaw handles daemon/Slack/memory/scheduler plumbing.
- **Build `nyxagent` in its image** — purpose-built orchestrator, tighter to the pipeline.

**Owner goal — make the agents "alive" and interactive:**
- **Slack (first-class):** chat with the agents in Slack — give instructions, get status, drive
  the pipeline conversationally. Native to the OpenClaw-style daemon; not custom work.
- **Zoom (harder, separate integration):** real-time voice/video. Needs a meeting-bot layer
  (join call → speech-to-text → agent → text-to-speech) via the Zoom Meeting SDK + an STT/TTS
  pipeline — feasible but distinct from the text-messaging path. **Moved to its own Phase 9**
  (see `PHASE_9_PLAN.md`); it rides on this phase's daemon as another frontend.
- Running in the owner's own daemon under the agent PATs means the agents are genuinely the
  actors — the **identity/attribution requirement is fully satisfied** (unachievable on Actions
  or a proxied remote session).

## (Only if staying on Actions) Hard constraint — GitHub Actions do NOT run from a submodule

**GitHub only triggers workflows that live in the *consuming repo's* own `.github/workflows/`.**
Workflow YAML sitting inside a submodule directory is inert — GitHub will not fire it on
issue/PR events. So the extraction cannot simply move the workflow files into nyxAGENT and
submodule them back; the pipeline would stop running. Phase 7 must choose and implement a
sync/generation strategy, e.g.:

- **Generate/sync step:** nyxAGENT holds the canonical workflow templates; a `nyxagent sync`
  (or a scheduled workflow) renders them into nyxGPT's real `.github/workflows/` on update, so
  the checked-in workflows stay in sync with the submodule source of truth.
- **Reusable workflows:** convert the pipeline to GitHub *reusable workflows* published from
  nyxAGENT and referenced from thin caller-workflows committed in nyxGPT
  (`uses: dkblinux98/nyxAGENT/.github/workflows/xxx.yml@vX`). This keeps only small stubs in
  nyxGPT and the logic in nyxAGENT, and it actually runs (reusable workflows are supported).
  This is the recommended approach and should be an explicit architecture-decision issue.
- Scripts/prompts/charters/runbooks *can* be consumed directly from the submodule path (they're
  invoked by path), so those are straightforward; only the workflows need the special handling.

## Rough sprint sketch (to refine when the milestone opens)

1. **Decision issue:** submodule + reusable-workflows vs submodule + sync-generation (pick the
   mechanism; recommend reusable workflows for the pipeline, direct submodule for scripts/docs).
2. Create `nyxAGENT` repo; move agent tooling, charters, prompts, runbooks, agent-generic
   CLAUDE.md/AGENTS.md; tag an initial version.
3. Convert the pipeline workflows to reusable workflows in nyxAGENT (or the sync generator).
4. Add nyxAGENT as a submodule in nyxGPT; replace the moved files with thin caller-workflows /
   submodule references; update paths in any remaining nyxGPT scripts.
5. Verify the full loop still runs end-to-end (scrummaster → developer → review → merge) against
   the submodule-sourced pipeline; document the update/version-bump workflow.
6. Update token/identity docs (the agent PATs and `vars.*` still live in the nyxGPT repo/org
   settings; the submodule is code only).

## Notes

- Identity/attribution policy is unchanged: the agents remain the actors; nyxAGENT is where their
  code lives, not a new actor.
- Secrets/vars (`SCRUMMASTER_AGENT_TOKEN`, `DEV_AGENT`, `REVIEW_AGENT`, `AGENTS_ENABLED`, etc.)
  stay in the nyxGPT repo/org settings — a submodule cannot carry Actions secrets.
