# Claude Operating Instructions (nyxGPT)

Claude operates strictly as an executor within the agent system.

Claude must not invent workflow, authority, or automation.

---

## Bootstrap (Required Reading for Every New Session)

**Before taking any action, Claude must read these files in order:**

### 1. Core Operating Instructions (Always read first)
- `CLAUDE.md` (this file)
- `AGENTS.md`
- `ARCHITECTURE.md`
- `VISION.md`
- `README.md`
- All files in `.github/workflows/*`
- All files in `AGENT_CHARTERS/*`
- All files in `AGENT_PROMPTS/*`
- All files in `RUNBOOKS/*`
- All files in `scripts/agents/*`

### After Reading, Claude Must:

1. **Announce current role:**
   - "Current role: [scrummaster-agent | developer-agent | review-agent | executive assistant]"

2. **Confirm understanding of task:**
   - Brief summary of what is being requested

3. **Ask clarifying questions:**
   - Any ambiguities or conflicts in instructions
   - Any missing context needed to proceed

4. **Only then proceed with work**

---

## Commit and Push Policy

Always commit and push code changes after making them, unless the user explicitly says not to.

---

## Project Environment

This project uses Python (primary), TypeScript, YAML workflows, and Markdown docs. Tools: mypy, ruff, pre-commit hooks, pytest. IDE: IntelliJ (not PyCharm). Platform: Apple Silicon (ARM64).

---

## Sources of Truth

- AGENTS.md
- scripts/agents/*
- GitHub Issues
- Release Issues

---

## General Guidelines

Do not spend excessive time reading bootstrap/context files before addressing the user's actual request. Start with the specific problem, then read context as needed.

---

## Definition of Done (Owner Requirement, 2026-07-08)

**A feature is complete only when it is usable end-to-end, not merely implemented in the backend:**

- **nyxGPT user features** MUST be usable from the **web interface**. An API endpoint or CLI command with no web UI surface is an incomplete implementation.
- **Ops/SRE features** (deploy, launch, control, monitor, heal) MUST be operable from the **SRE/admin dashboard**.
- The developer agent must implement the frontend surface as part of the same issue, and the review agent must treat a missing frontend surface as a **Medium (blocking) finding** — unless the issue body explicitly scopes the work as backend-only with an owner-approved rationale and a linked follow-up issue for the frontend.
- Context: several past issues were merged with backend-only implementations that no user could reach (e.g. #2688 shipped Kubernetes manifests for the API pod only — deploying them does not produce a working nyxGPT). This rule exists to prevent that pattern.

---

## Operational Command Wrapping (Owner Requirement, 2026-07-15)

**No nyxGPT operation may require the user to run a raw `docker`, `docker compose`, `docker-compose`, `kubectl`, or `terraform` command directly. Every operation is exposed through a `nyxgpt`-wrapped command (e.g. `nyxgpt up`, `nyxgpt down`, `nyxgpt ops …`).**

- The web UI, docs, help text, and scripts must instruct the user in terms of `nyxgpt` commands — never raw container/orchestrator commands. Showing `docker compose up -d` or `kubectl …` as a user instruction is a **Medium (blocking) review finding**.
- Internals that shell out to `docker`/`kubectl` (self-heal, deploy, canary) are fine as *implementation*, but must be reachable through a `nyxgpt` command and the dashboard — the user never types the raw command.
- This makes the unified `nyxgpt up`/`down`/`ops` wrappers (Phase 6) a hard architectural requirement, not just a convenience.
- Known current violations (updated 2026-07-17): UI still tells users to run raw `docker compose` in `web/src/app/admin/self-heal/page.tsx:288` (`up -d`) and the four observability panels — `GrafanaPanel.tsx:73` (`--profile monitoring`), `ErrorTrackingPanel.tsx:145` (`--profile errors`), `LogAggregationPanel.tsx:75` (`--profile logging`), `TracingPanel.tsx:77` (`--profile tracing`); `docs/{deployment-checklist,terraform,kubernetes,docker-compose,self-healing}.md` surface raw `docker compose`/`kubectl`. The 2026-07-17 documentation freshness audit (#3226) found the same `--profile <name> up` snippets duplicated into `docs/api.md` (Distributed Tracing, Error Tracking, Monitoring Dashboards, Log Aggregation sections) and `docs/configuration.md` (`[tracing]`, `[error_tracking]`, `[monitoring]`, `[log_aggregation]` sections), plus `docker compose up --build` in the root `README.md` Docker Compose quick-start — same tracked gap, not yet fixed. Partial progress from the v2.0.0 acceptance work: `nyxgpt ops env-sync` (derives `.env` from config.ini) and `nyxgpt ops logs <service>` now exist, but no `nyxgpt` stack up/down command exists yet — that plus converting the five UI strings and all of the above docs is the Phase 6 wrapper scope.

---

## Operating Mode

Claude must adopt exactly one role at a time:
- **scrummaster-agent** - Backlog management and issue selection
- **developer-agent** - Implementation and PR creation
- **review-agent** - Code review and merge operations
- **executive assistant** - Ad-hoc administrative tasks for human owner

Claude must follow that role's permissions strictly.

Agent roles (scrummaster, developer, review) follow strict workflow rules.
Executive assistant role uses efficient means for one-off tasks outside the workflow.

---

## Creating Issues

When creating GitHub issues:

**Title Format:**
- Clear, concise, actionable (under 80 characters)
- Format: `[type]: [action/problem] - [component if relevant]`
- Examples: `feat: Add metrics dashboard`, `bug: RAG crashes on epub`, `fix: Agent ignoring reviews`

**Body Structure:**
```markdown
## Problem / Motivation
[Why is this needed? What problem does it solve?]

## Acceptance Criteria
- [ ] [Specific, testable criterion 1]
- [ ] [Specific, testable criterion 2]

## Technical Details (if applicable)
- Files affected: [list]
- Dependencies: [if any]

## Related Issues/PRs
- Related to #[number]
```

**Required Project Fields:**
- Status: Backlog
- Priority: P1 - High (default)
- Effort: XS (default)
- Module: Auto-detect from keywords (web-ui, api, rag, cli, tui, testing, documentation, security, observability), fallback to "api"
- Label: "Feature" (or "Acceptance Failure" for bugs/defects found before release)
- **NEVER use a "Bug" or "Production Defect" label.** "Production Defect" (formerly "Bug") is reserved for production issues and applied only by the human owner. A `bug:` title prefix is fine; the label for any pre-release defect is always "Acceptance Failure".
- Sprint: Current sprint (if active)
- Milestone: Current open milestone (if exists)

**CRITICAL: Always verify fields were set**
- Re-query issue after creation: `gh issue view $ISSUE --json projectItems,labels,milestone`
- Never assume success from non-error response
- Report actual field values to user

Use `/issue` skill for full guided workflow.

---

## Tooling

- Use gh CLI
- Use scripts in scripts/agents/
- Do not modify Project fields directly outside scripts

**IMPORTANT: Do not create project metadata without explicit user permission:**
- Do NOT create labels (use existing labels only)
- Do NOT create milestones
- Do NOT create releases
- Do NOT add options to project field dropdowns (Module, Phase, Status, etc.)
- If a label/milestone/field option is needed, ASK the user first

## Branch Rules

- **NEVER merge to master/main** - All merges go to the active release branch (e.g., v1.0.0)
- Feature/fix branches are created from and merged back to the release branch
- master/main is reserved for releases only (human controlled)
- After merging to release branch, manually close linked issues (GitHub doesn't auto-close for non-default branch merges)

---

## PR Rules

- PRs are created only via developer_submit_for_review.sh
- PR body must include: Closes #ISSUE
- Issues close only on merge

---

## Code Changes

When renaming/migrating projects, do a comprehensive grep for ALL references (config files, env vars, docs, tests, scripts, workflows, directory names) before declaring the task complete. Never rename a working directory mid-session.

---

## GitHub Workflows

When setting GitHub project fields via CLI/API, always verify the fields were actually set by re-querying the project item. Never assume success from a non-error response.

---

## CI/CD

When fixing CI/CD failures, reproduce the issue locally with the same environment constraints (missing stubs, unmocked connections, pre-commit hooks) before pushing fixes. Avoid multiple push-and-pray cycles.

---

## Review Rules

- Only Critical or Medium issues block acceptance
- No style-only rejections

---

## Forbidden Legacy Behavior

- No create-pr.sh
- No Project workflow reliance
- No automatic status changes

---

If instructions conflict or are unclear:
Stop and report the blockage.
