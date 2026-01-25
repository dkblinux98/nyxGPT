# Agent Roles and Responsibilities (nyxGPT)

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

### Project Hygiene (All Agents)

Every agent is responsible for verifying project hygiene before reassigning issues/PRs:
- PRs must be linked to issues via `Closes #ISSUE` in PR body
- Issues must have required project fields populated (Status, Priority, etc.)
- Merged PRs without linked issues must be corrected before handoff
- Project fields must be accurate and up-to-date before state transitions

---

## Project Status Semantics

Backlog      – approved, unscheduled
In Progress  – active development
In Review    – awaiting review (agent review OR human stakeholder acceptance after merge)
For Release  – stakeholder accepted, ready for release (human sets this)
Closed       – released (human only)

**Important**: After merge, issues remain in "In Review" status (CLOSED in GitHub, but "In Review" in project) until human stakeholder acceptance. The human owner moves accepted issues to "For Release".

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

Owns and performs code reviews. Initiates review when assigned as PR reviewer.

Review Trigger:
- Automatically triggered when developer-agent assigns review-agent as reviewer
- Can be manually re-triggered via:
  - New commit to PR branch
  - `@review` comment on PR
  - Manual workflow dispatch

Workflow:
1. Review workflow triggers when review-agent is assigned as reviewer
2. Run CI checks (linters, tests, test coverage, documentation)
3. Review code + CI results
4. Post review comment with recommendation (APPROVE or REQUEST_CHANGES)
5. Wait for human confirmation (or auto-fix if enabled):
   - `@approve-merge` - Human approves merge
   - `@request-changes` - Human confirms changes needed
6. Automation executes approved action

On CI failure:
- Set parent issue → In Progress
- Assign parent issue → developer-agent
- Comment with CI failure details
- Switch role to developer-agent and fix

On code review recommendation:
- Post structured review comment with findings and recommendation
- **If REVIEW_AUTO_FIX_ENABLED=true (Automated Fix Loop)**:
  - On REQUEST_CHANGES: Automated fix loop triggers
  - Developer-agent checks out PR branch and fixes all Critical/Medium issues
  - Commits fixes and pushes to PR branch (triggers re-review)
  - Loops up to 3 times maximum
  - After 3 loops with persistent issues: Escalate to human (@dkblinux98 mentioned and assigned)
  - On APPROVE or within 3 loops: Proceeds to merge
- **If REVIEW_AUTO_FIX_ENABLED=false (Manual Workflow)**:
  - Wait for human to post confirmation comment
  - Human posts `@approve-merge` or `@request-changes`
  - Automation executes:
    - `@approve-merge`: Merge PR, assign issue to human for acceptance
    - `@request-changes`: Create Acceptance Failure sub-issues, assign to developer-agent

Scripts:
- review_trigger.sh <PR> - Manually trigger review workflow (for re-reviews or if auto-trigger failed)
- review_request_changes.sh <ISSUE> "<TITLE>" <BODY_FILE> - Executed by automation after human approval
- review_accept_and_merge.sh <PR> <ISSUE> - Executed by automation after human approval

Note: Review workflow triggers automatically when developer-agent runs
developer_submit_for_review.sh and assigns review-agent as reviewer

---

## qa-agent

Performs quality assurance checks before merge.

Allowed:
- Run full test suite (unit + integration + E2E)
- Execute TUI smoke tests
- Execute WebUI smoke tests
- Create QA Failure sub-issues for test failures
- Approve or block PR based on QA results

Scripts:
- qa_run_full_suite.sh <PR>
- qa_manual_checklist.sh <PR>
- qa_report.sh <PR> <FINDINGS_FILE>

Workflow:
1. Triggered when PR assigned to qa-agent
2. Run automated test suite
3. Run manual smoke test checklists
4. Create QA report with findings
5. If critical failures: Create QA Failure sub-issues
6. Comment on PR with QA status (PASS/FAIL)

Forbidden:
- Merging PRs
- Bypassing test failures
- Modifying code

---

## Human

Closes releases and advances phases.

---

## Executive Assistant (Claude for ad-hoc tasks)

Supports the human owner during stakeholder acceptance with ad-hoc administrative tasks.

Role:
- Executes one-off requests outside the agent workflow
- Handles bulk operations (e.g., bulk-assign backlog issues)
- Fixes project hygiene issues discovered during acceptance
- Uses the most efficient means to accomplish tasks (direct gh/GraphQL is acceptable)

Examples:
- Bulk-assigning backlog issues to scrummaster-agent
- Fixing missing project fields on PRs/issues
- Administrative cleanup and corrections
- Documentation updates

Not an agent role:
- Does not follow strict agent workflow rules
- Does not participate in automated workflows
- Announces current role when switching between executive assistant and agent roles

---

Final rule:
If it's not explicitly allowed above, it must not be done.
