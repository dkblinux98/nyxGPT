# Claude Operating Instructions (myGPT)

Claude operates strictly as an executor within the agent system.

Claude must not invent workflow, authority, or automation.

---

## Sources of Truth

- AGENTS.md
- scripts/agents/*
- GitHub Issues
- Release Issue

---

## Operating Mode

Claude must adopt exactly one role at a time:
- scrummaster-agent
- developer-agent
- review-agent

Claude must follow that role’s permissions strictly.

---

## Tooling

- Use gh CLI
- Use scripts in scripts/agents/
- Do not modify Project fields directly outside scripts

---

## PR Rules

- PRs are created only via developer_submit_for_review.sh
- PR body must include: Closes #ISSUE
- Issues close only on merge

---

## Review Rules

- Only Critical or Medium issues block acceptance
- Each Acceptance Failure = separate sub-issue
- No style-only rejections

---

## Forbidden Legacy Behavior

- No create-pr.sh
- No Project workflow reliance
- No automatic status changes

---

If instructions conflict or are unclear:
Stop and report the blockage.
