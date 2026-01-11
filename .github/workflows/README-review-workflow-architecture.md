# Review Workflow Architecture

## Overview

The review process is split into two sequential workflows:

1. **`claude-code-review.yml`** - Performs code review, posts structured comment
2. **`review_agent_auto_review.yml`** - Reads comment, executes decision

## Architecture

```
PR opened/updated
    ↓
┌─────────────────────────────────────┐
│ claude-code-review.yml              │
│ - Reviews code thoroughly           │
│ - Posts structured comment with:    │
│   • APPROVE or REQUEST_CHANGES      │
│   • Detailed findings               │
│   • Critical/Medium issues          │
└─────────────────────────────────────┘
    ↓ (on workflow_run completion)
┌─────────────────────────────────────┐
│ review_agent_auto_review.yml        │
│ - Reads PR comment                  │
│ - Parses decision                   │
│                                     │
│ If APPROVE:                         │
│   → review_accept_and_merge.sh     │
│   → Merge PR                        │
│   → Assign to human owner          │
│                                     │
│ If REQUEST_CHANGES:                 │
│   → Parse Critical/Medium issues    │
│   → review_request_changes.sh      │
│     (for each issue)                │
│   → Create sub-issues               │
│   → Assign to developer-agent      │
│   → Triggers developer workflow    │
└─────────────────────────────────────┘
```

## Why This Design?

### Problem 1: No Visible Review Feedback
Previous architecture had Claude execute scripts directly without posting review comments. This meant:
- No audit trail of review decisions
- No way for humans to understand what was reviewed
- No transparency into why PRs were approved/rejected

### Problem 2: Merge-After-Failure Risk
When review and merge were in the same Claude Code execution context:
- If Claude crashed AFTER merge but BEFORE completing → PR merged but workflow failed
- Workflow status didn't match actual outcome
- False negatives in CI/CD pipeline

### Solution: Structured Comment as Data Interchange

The PR comment serves as both:
1. **Human-readable review feedback** - Clear findings, severity levels, specific issues
2. **Machine-parseable decision** - Structured format for automation

This gives us:
- ✅ Complete audit trail
- ✅ Both humans and automation can read decisions
- ✅ Safe failure mode (if parsing fails, workflow fails, no merge)
- ✅ Simple proven logic for sub-issue creation

## Comment Format

```markdown
## Code Review - [APPROVE|REQUEST_CHANGES]

### Summary
[Brief summary of review findings]

### Findings
[Detailed findings organized by severity]

### Critical Issues (if any)
- **[Title]**: [Description, file:line references, how to fix]

### Medium Issues (if any)
- **[Title]**: [Description, file:line references, how to fix]

### Minor Issues (if any)
- **[Title]**: [Description or note that these don't block merge]

### Decision
**[APPROVE|REQUEST_CHANGES]**
```

## Parsing Logic

`review_agent_auto_review.yml` parses the comment:

1. **Extract decision**: First line contains `APPROVE` or `REQUEST_CHANGES`
2. **Parse blocking issues**: Extract lines matching `- **Title**: Description` from Critical/Medium sections
3. **Create sub-issues**: Each blocking issue becomes an Acceptance Failure sub-issue
4. **Execute decision**: Call appropriate script based on decision

## Sub-Issue Creation

For each Critical/Medium issue:
- Title extracted from `**Title**:` format
- Description contains file/line references and how to fix
- Sub-issue created via `review_request_changes.sh`
- Parent issue set to In Progress
- Sub-issue assigned to developer-agent
- This triggers `developer_auto_implement.yml` to fix issues

## Benefits

1. **Transparency**: Every review decision is visible and auditable
2. **Safety**: Parser reads comment, executes scripts - if parsing fails, workflow fails before merge
3. **Proven logic**: Sub-issue creation uses tested `review_request_changes.sh` script
4. **Human-in-loop**: Humans can read review comments and override if needed
5. **Simple**: PR comment as data interchange is easier to debug than JSON files

## Workflow Dependencies

- `claude-code-review.yml` triggers on: `pull_request` events (opened, synchronize)
- `review_agent_auto_review.yml` triggers on: `workflow_run` completion of Claude Code Review
- GitHub automatically chains these workflows when Claude Code Review completes

## Testing

To test the full flow:

1. **APPROVE path**:
   - Create PR with clean code
   - Claude reviews, posts "APPROVE" comment
   - Workflow parses, executes merge
   - PR merged, assigned to human owner

2. **REQUEST_CHANGES path**:
   - Create PR with blocking issues
   - Claude reviews, posts "REQUEST_CHANGES" with Critical/Medium issues
   - Workflow parses issues, creates sub-issues
   - Parent assigned to developer-agent
   - Developer workflow triggered to fix

3. **Failure handling**:
   - If Claude crashes before posting comment → workflow fails, no merge
   - If parsing fails → workflow fails, no merge
   - If script execution fails → workflow fails, accurate status

## Related Files

- `claude-code-review.yml` - Review and comment posting
- `review_agent_auto_review.yml` - Decision parsing and execution
- `scripts/agents/review_accept_and_merge.sh` - Merge script
- `scripts/agents/review_request_changes.sh` - Sub-issue creation script
- `RUNBOOKS/review-runbook.md` - Review checklist and severity model
