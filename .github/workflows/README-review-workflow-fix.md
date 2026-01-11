# Review Workflow Fix - Separate Review from Merge Execution

## Problem

The review_agent_auto_review.yml workflow had a critical architectural flaw where Claude Code performed both code review AND merge execution in a single step. This created a dangerous failure mode:

1. Claude Code successfully reviewed and merged PR #2843
2. Claude Code then crashed with SDK error "only prompt commands are supported in streaming mode"
3. The workflow showed as "cancelled/failed" despite the PR being merged
4. **Result**: PRs can merge even when workflow fails, breaking CI/CD safety assumptions

This was a regression from the old architecture where claude-code-review.yml and review_agent_auto_review.yml were separate workflows. The old design was more reliable - even if it ran reviews twice, **it failed safely**.

## Root Cause

Combining review and merge into Claude Code's single execution context meant:
- If Claude crashes AFTER merging but BEFORE completing → PR merged but workflow failed
- No clear separation between "review decision" and "merge execution"
- Workflow status doesn't accurately reflect whether merge succeeded

## Solution

Restructured workflow to separate review from execution:

### Step 1: Claude Code Review Only
- Claude performs comprehensive code review
- Outputs decision to `/tmp/review-decision.json`
- Does NOT execute merge or create sub-issues
- If Claude crashes → workflow fails, no merge happens (safe failure)

Decision file format:
```json
{
  "decision": "APPROVE" | "REQUEST_CHANGES",
  "pr_number": 1234,
  "issue_number": 5678,
  "summary": "Brief summary of findings",
  "sub_issues": [
    {"title": "Issue title", "body_file": "/tmp/sub-issue-1.md"}
  ]
}
```

### Step 2: Execute Review Decision
- Workflow script reads `/tmp/review-decision.json`
- If `APPROVE`: executes `review_accept_and_merge.sh`
- If `REQUEST_CHANGES`: creates sub-issues via `review_request_changes.sh`
- If no decision file exists: workflow fails cleanly (manual intervention required)

### Step 3: Post Accurate Status Comment
- Comments on PR based on actual outcome
- Reflects whether merge succeeded, changes requested, or review failed
- Provides link to workflow run for debugging

## Benefits

1. **Safe failure**: If Claude crashes, merge doesn't happen
2. **Accurate status**: Workflow success/failure matches actual outcome
3. **Clear separation**: Review logic (Claude) separate from execution (workflow script)
4. **Debuggable**: Decision file provides audit trail of review outcome
5. **Idempotent**: Can re-run failed workflows without re-executing successful merges

## Migration Notes

- Old behavior: Claude executed `review_accept_and_merge.sh` directly
- New behavior: Claude writes decision, workflow executes merge
- If Claude crashes before writing decision file, workflow fails (no merge)
- If workflow step fails after reading decision, status accurately reflects failure

## Testing

To test:
1. Create a test PR with clean code → should merge successfully with green workflow
2. Create a test PR with blocking issues → should create sub-issues, no merge
3. Simulate Claude crash (timeout) → workflow should fail, no merge

## Related Issues

- PR #2843: Merged successfully but workflow showed as cancelled
- PR #2845: Had circular CI dependency (Claude checking for its own workflow)
- Root cause: Combining claude-code-review.yml into review_agent_auto_review.yml created this regression
