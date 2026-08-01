# Review Runbook (review-agent)

## 0) Preconditions
- PR targets active release branch.
- CI is green (required to merge unless human exception).
- PR body includes `Closes #ISSUE` linking to a valid issue.

### Project hygiene
Every assignee is responsible for verifying project hygiene before reassigning:
- PRs must be linked to issues via `Closes #ISSUE` in PR body
- Issues must have all required project fields populated
- Merged PRs without linked issues must be corrected
- Project fields must be accurate before state transitions

### Review Trigger (review-agent ownership)
The review workflow is triggered when review-agent is assigned as reviewer:
- **Automatic**: When `developer_submit_for_review.sh` assigns review-agent as reviewer
- **Manual re-trigger options**:
  - Push new commit to PR branch (triggers on synchronize)
  - Comment `@review` on the PR
  - Run via GitHub Actions UI: `gh workflow run claude-code-review.yml -f pr_number=<N>`

The review-agent OWNS the review process:
- Review workflow uses `REVIEW_AGENT_TOKEN` for all GitHub operations
- Review comments are posted by review-agent (claude[bot])
- Review-agent orchestrates the auto-fix loop (developer-agent executes fixes)

## 1) Review checklist

**IMPORTANT:**
- Run CI checks on ALL code in the repository (not just changed files)
- Review ALL changed files in the PR (not just new changes from current cycle)
- This ensures comprehensive quality coverage across the entire codebase

### Core Requirements (from project standards)
- Correctness vs issue acceptance criteria
- Tests added/updated and meaningful
- No architecture boundary violations
- No secrets committed
- Clear docs updates for user-facing changes
- Reasonable maintainability
- **End-to-end usability (Definition of Done, CLAUDE.md):** nyxGPT user features must be usable from the web interface; ops/SRE features must be operable from the SRE/admin dashboard. A backend-only implementation is a Medium (blocking) finding unless the issue explicitly scopes it backend-only with owner approval and a linked frontend follow-up issue.

### Additional Quality Checks (comprehensive review)
- Code quality and best practices (use CLAUDE.md for guidance)
- Performance considerations and potential bottlenecks
- Security concerns beyond secret detection
- Potential bugs or edge cases not covered by tests
- API contract consistency and backward compatibility

## 2) Severity model
- Critical: correctness/security/data-loss/performance regression; must block merge
- Medium: significant bug risk, missing tests, broken contract, poor maintainability; must block merge
- Minor: style/nits, minor optimization opportunities; may proceed

### Live-verification findings do NOT block (Owner decision, 2026-08-01)
When the only outstanding finding is that an acceptance criterion requires
exercising a **live running stack** (Grafana panels rendering, Slack delivery,
browser behavior, a running Compose stack) that the agent environment cannot
run, and everything the reviewer CAN verify — code correctness, tests and
their effectiveness, static/empirical checks — is satisfied: **APPROVE and
merge.** Owner acceptance testing after merge IS the live verification; that
is exactly what the Acceptance Testing stage exists for. Do not REQUEST_CHANGES
or burn escalation cycles demanding evidence the developer agent structurally
cannot produce (PR #3548/#3469 deadlocked three cycles this way). Instead,
list the deferred live checks explicitly in the APPROVE review so the owner
knows precisely what to exercise during acceptance. Escalation is reserved
for unresolved findings the agents *could* fix but haven't.

## 3) CI failure handling
If CI fails during review (should not happen if developer phase worked correctly):
- Still review the code changes
- Capture all issues (CI failures + code review findings)
- Proceed with normal REQUEST_CHANGES flow
- Set issue status -> In Progress
- Assign issue -> developer-agent
- Comment with all findings (CI + code issues)

Note: Pre-commit hooks should prevent CI failures. If they occur, treat as REQUEST_CHANGES.

## 4) Review and recommendation
After completing the review:
- Post a structured review comment starting with "## Code Review - [APPROVE|REQUEST_CHANGES]"
- Include findings organized by severity (Critical/Medium/Minor)
- Provide clear recommendation with rationale

## 5) Automatic execution
The review decision is automatically executed based on the review comment:
- **APPROVE**: Workflow automatically merges the PR (no human confirmation required)
- **REQUEST_CHANGES**:
  - Issue returns to developer-agent with "In Progress" status
  - Developer reads review comment and implements fixes
  - Developer runs tests in 3-try loop (resets each assignment) BEFORE committing
  - Developer commits and re-submits for review (triggers re-review automatically)
  - Review cycle repeats (cumulative count tracked)
  - After 3 review cycles: Issue stays "In Review", escalates to human owner

Manual override (optional):
- `@approve-merge` - Human can manually trigger merge
- `@request-changes` - Human can manually trigger changes workflow (legacy)

## 6) Review cycle escalation
The review workflow tracks cumulative review cycles:
- Each REQUEST_CHANGES increments the cycle counter
- Developer 3-try loop (for test failures) resets each time issue is reassigned
- Review 3-cycle limit is cumulative across all reviews for this PR
- After 3rd REQUEST_CHANGES review:
  - Issue remains Status -> In Review
  - Issue reassigned to HUMAN_OWNER
  - Slack DM sent to human
  - Human intervenes to resolve

All fixes happen on the PR branch (no separate issues created).

## 7) Merge criteria
- All tests and linters passing
- Code review APPROVE decision (either from review agent or human override)

## 8) Post-merge
When PR is merged (automatically on APPROVE or via human `@approve-merge` override):
- Automation merges into active release branch (NEVER merge to master/main)
- Delete short-lived feature/fix branches created for the feature
- Close the issue (GitHub state)
- Keep issue status -> In Review (for human stakeholder acceptance)
- Assign issue -> human owner (dkblinux98)
- Notify scrummaster-agent that developer-agent is ready for next issue

### Important: Issue auto-close behavior
- PRs merged to the release branch (e.g., v1.0.0) do NOT auto-close linked issues
- GitHub only auto-closes issues when PRs merge to the default branch (master)
- Automation manually closes issues after merging to release branch
- Post-merge: issue should be CLOSED (GitHub state) + In Review (project status) + assigned to human

## 9) Human stakeholder acceptance

After merge, each issue is assigned to the human owner with status "In Review" for final acceptance.

### If acceptance passes
Move the issue to "For Release" in the project board. No action needed in GitHub.

### If acceptance fails (bug found after merge)

1. **Go to the issue** (it is assigned to you and closed)
2. **Add a comment** describing what is broken — be specific:
   - What you expected
   - What actually happened
   - Steps to reproduce if relevant
3. **On the same or a separate comment, write:** `@acceptance-failure`

That's it. The system will automatically:
- Reopen the issue
- Add the "Acceptance Failure" label
- Set status back to "In Progress"
- Assign to the developer agent to create a `fix/N-...` branch and PR with correct `Closes #N` body

> **Note:** `@acceptance-failure` is only accepted from the human owner account and only on
> issues (not PRs). It is entirely separate from the review-loop overrides
> (`@approve-merge`, `@request-changes`, `@send-to-developer`) which apply to PRs
> during the automated code review cycle.

## 10) Phase completion
When the human owner moves the last issue in the active Phase to "For Release" (human stakeholder acceptance):
- Notify human owner that phase is complete and ready for release

## 11) Configuration

### Required Secrets
The review workflow requires these secrets to be configured:
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token for Claude Code agent
- `REVIEW_AGENT_TOKEN` - GitHub token with repo/project permissions (used for review workflow)

**How to configure secrets:**
- Navigate to: Settings → Secrets and variables → Actions → Secrets
- Click "New repository secret"
- Add all secrets if not already present

### Branch Cleanup
Branch deletion happens in `review_accept_and_merge.sh` via `--delete-branch`, not in auto-fix workflow.
