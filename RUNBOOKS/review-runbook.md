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
