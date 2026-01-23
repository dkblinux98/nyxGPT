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

## 1) Review checklist

### Core Requirements (from project standards)
- Correctness vs issue acceptance criteria
- Tests added/updated and meaningful
- No architecture boundary violations
- No secrets committed
- Clear docs updates for user-facing changes
- Reasonable maintainability

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

## 3) CI failure handling
If CI fails after PR is opened:
- Set parent issue status -> In Progress
- Assign parent issue -> developer-agent
- Comment on issue with CI failure details
- Switch role to developer-agent
- Fix the CI failures
- Update PR and ensure CI passes
- Re-submit for review

## 4) Review and recommendation
After completing the review:
- Post a structured review comment starting with "## Code Review: [APPROVE|REQUEST_CHANGES]"
- Include findings organized by severity (Critical/Medium/Minor)
- Provide clear recommendation with rationale
- **WAIT for human confirmation** - Do NOT proceed automatically

## 5) Human confirmation (required)
The human owner must review the recommendation and post one of:
- `@approve-merge` - Confirms merge should proceed
- `@request-changes` - Confirms changes are needed

The GitHub workflow will then execute the approved action.

## 6) Acceptance Failure loop (blocking findings)
When human posts `@request-changes`:
- Automation creates ONE sub-issue per Critical/Medium finding
- Sub-issues labeled `Acceptance Failure`
- Copy key context and reproduction details
- Inherit Phase/Sprint fields
- Assign to developer-agent and set status -> In Progress

## 7) Merge criteria
- No open Critical/Medium Acceptance Failure items
- CI green
- Human approval via `@approve-merge`

## 8) Post-merge
When human posts `@approve-merge`:
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

## 9) Phase completion
When the human owner moves the last issue in the active Phase to "For Release" (human stakeholder acceptance):
- Notify human owner that phase is complete and ready for release

## 10) Configuration

### REVIEW_AUTO_FIX_ENABLED
Controls whether the automated fix loop is active.

**Location:** GitHub Repository Settings
- Navigate to: Settings → Secrets and variables → Actions → Variables
- Variable name: `REVIEW_AUTO_FIX_ENABLED`
- Valid values: `true` (enabled) or `false` (disabled)
- Default: `false`

**Behavior when enabled (true):**
- On REQUEST_CHANGES review: Auto-fix workflow triggers automatically
- Developer-agent checks out PR branch and fixes all Critical/Medium issues
- Commits fixes and pushes to PR branch (triggers re-review via CI)
- Loops up to 3 times maximum
- After 3 loops with persistent issues: Escalates to @dkblinux98
- No sub-issues created during auto-fix loop
- Manual `@request-changes` trigger disabled (auto-fix takes over)

**Behavior when disabled (false):**
- Manual workflow active (wait for human `@approve-merge` or `@request-changes`)
- On `@request-changes`: Creates Acceptance Failure sub-issues as documented in section 6
- Developer-agent fixes sub-issues on separate branches
- Standard manual review-fix cycle

**How to change:**
```bash
# Enable auto-fix loop
gh variable set REVIEW_AUTO_FIX_ENABLED --body "true" --repo dkblinux98/nyxGPT

# Disable auto-fix loop (revert to manual workflow)
gh variable set REVIEW_AUTO_FIX_ENABLED --body "false" --repo dkblinux98/nyxGPT

# Check current value
gh variable list --repo dkblinux98/nyxGPT | grep REVIEW_AUTO_FIX_ENABLED
```

### Required Secrets
The auto-fix workflow requires these secrets to be configured:
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token for Claude Code agent
- `DEVELOPER_AGENT_TOKEN` - GitHub token with repo/project permissions

**How to configure secrets:**
- Navigate to: Settings → Secrets and variables → Actions → Secrets
- Click "New repository secret"
- Add both secrets if not already present

### Branch Cleanup
Branch deletion happens in `review_accept_and_merge.sh` via `--delete-branch`, not in auto-fix workflow.
