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
- Label: "Feature" (or "Acceptance Failure" for bugs)
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
