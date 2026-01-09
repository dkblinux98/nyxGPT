# Agent Roles and Responsibilities (myGPT)

This document defines the only valid responsibilities and allowed actions
for each machine agent in this repository.

Agents must follow these instructions exactly.
Project automations are deliberately minimal.
Agents are the sole authority for state transitions.

---

## Global Rules

- Use only scripts in scripts/agents/
- Do not rely on GitHub Project automations
- Leave an auditable comment for every state change
- Do not merge to main/master
- Do not improvise workflow

---

## Project Status Semantics

Backlog      – approved, unscheduled  
In Progress  – active development  
In Review    – awaiting review  
For Release  – merged, pending release  
Closed       – released (human only)

---

## scrummaster-agent

Controls backlog intake and sequencing.

Allowed:
- Select next issue by lowest Phase then lowest issue number
- Set issue → In Progress
- Assign issue → developer-agent

Scripts:
- scrummaster_next_issue.sh
- scrummaster_start_issue.sh <ISSUE>

Forbidden:
- Writing code
- Creating PRs
- Closing issues

---

## developer-agent

Implements features and fixes.

Allowed:
- Create branches
- Write code and tests
- Open PRs
- Move issue → In Review
- Assign review-agent

Scripts:
- developer_create_branch.sh <ISSUE>
- developer_submit_for_review.sh <ISSUE> "<PR TITLE>"

Forbidden:
- Merging PRs
- Setting For Release
- Closing issues manually

---

## review-agent

Performs reviews and final integration.

Workflow:
1. Wait for CI checks: `gh pr checks <PR> --watch`
2. Review code + CI results
3. Decide: merge or create sub-issues

On failure:
- Create Acceptance Failure sub-issues (one per critical/medium issue)
- Set parent → In Progress
- Assign developer-agent

On success:
- Merge PR to release branch
- Delete feature branch
- Set issue → In Review
- Assign human owner

Scripts:
- review_request_changes.sh <ISSUE> "<TITLE>" <BODY_FILE>
- review_accept_and_merge.sh <PR> <ISSUE>

Note: Role transition happens automatically when developer-agent runs
developer_submit_for_review.sh

---

## Human

Closes releases and advances phases.

---

Final rule:
If it’s not explicitly allowed above, it must not be done.
