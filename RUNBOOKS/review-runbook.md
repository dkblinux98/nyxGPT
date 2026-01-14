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

## 5) Post-review workflow

### Automated Fix Loop (REVIEW_AUTO_FIX_ENABLED=true)

When automated fix loop is enabled, the system handles REQUEST_CHANGES automatically:

**Loop Detection:**
- Counts "## Code Review - " comments from github-actions bot on the PR
- Each review = 1 iteration
- Maximum 3 iterations before human escalation

**Fix Execution:**
1. On REQUEST_CHANGES comment:
   - Workflow automatically triggers (no human confirmation needed)
   - Checks out PR's feature branch
   - Parses Critical/Medium issues from review comment
   - Invokes developer-agent to fix all issues in a single commit
   - Pushes fixes back to PR branch
   - Re-review triggers automatically via `pull_request.synchronize`
2. Loops up to 3 times
3. After 3rd iteration with persistent issues:
   - Posts: "@HUMAN_OWNER claude bot has reviewed and fixed this PR 3 times. It now requires human intervention."
   - Assigns PR to HUMAN_OWNER
   - Stops automation
4. On APPROVE within 3 loops:
   - Proceeds to merge (section 7)

**Manual Override:**
- Human can post `@approve-merge` at any time to skip auto-fix and force merge
- Disable feature by setting REVIEW_AUTO_FIX_ENABLED=false

### Manual Workflow (REVIEW_AUTO_FIX_ENABLED=false)

When automated fix is disabled, human confirmation is required:

**Human Confirmation:**
The human owner must review the recommendation and post one of:
- `@approve-merge` - Confirms merge should proceed
- `@request-changes` - Confirms changes are needed

The GitHub workflow will then execute the approved action.

## 6) Acceptance Failure loop (manual workflow only)
**Note:** This section applies only when REVIEW_AUTO_FIX_ENABLED=false.

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

**When to enable:**
- After confirming the review workflow is stable
- When you want to reduce manual intervention in the review-fix cycle
- For repos with high PR velocity and responsive test suites

**When to disable:**
- During initial setup and testing
- If auto-fix loops are not converging (too many escalations)
- When manual review and sub-issue tracking is preferred

**How to change:**
```bash
# Enable auto-fix loop
gh variable set REVIEW_AUTO_FIX_ENABLED --body "true" --repo OWNER/REPO

# Disable auto-fix loop (revert to manual workflow)
gh variable set REVIEW_AUTO_FIX_ENABLED --body "false" --repo OWNER/REPO

# Check current value
gh variable list --repo OWNER/REPO | grep REVIEW_AUTO_FIX
```

### Branch Cleanup
**Note on branch deletion:** The auto-fix workflow does NOT delete branches. Branch deletion happens in `review_accept_and_merge.sh` via the `--delete-branch` flag when the PR is merged. If auto-fix escalates to human or fails, branches remain until the PR is manually closed or merged.
