# Review Workflow Fix - Separate Review from Merge Execution

## Problems

### Problem 1: Review and Merge Not Separated

The review_agent_auto_review.yml workflow had a critical architectural flaw where Claude Code performed both code review AND merge execution in a single step. This created a dangerous failure mode:

1. Claude Code successfully reviewed and merged PR #2843
2. Claude Code then crashed with SDK error "only prompt commands are supported in streaming mode"
3. The workflow showed as "cancelled/failed" despite the PR being merged
4. **Result**: PRs can merge even when workflow fails, breaking CI/CD safety assumptions

This was a regression from the old architecture where claude-code-review.yml and review_agent_auto_review.yml were separate workflows. The old design was more reliable - even if it ran reviews twice, **it failed safely**.

### Problem 2: No Visible Review Comments

When the workflows were consolidated, we lost the explicit instruction to post review comments. The old `claude-code-review.yml` had:

```
Use `gh pr comment` with your Bash tool to leave your review as a comment on the PR.
```

Without this instruction:
- Claude Code merged PRs #2843 and #2845 **without posting any review comments**
- No visibility into what was reviewed or why merge was approved
- No audit trail of review decisions
- No way to learn from review feedback over time

## Root Cause

Combining review and merge into Claude Code's single execution context meant:
- If Claude crashes AFTER merging but BEFORE completing → PR merged but workflow failed
- No clear separation between "review decision" and "merge execution"
- Workflow status doesn't accurately reflect whether merge succeeded

## Solution

Restructured workflow to separate review from execution AND restore visible review comments:

### Step 1: Claude Code Review Only
- Claude performs comprehensive code review
- **Posts review findings as PR comment** using `gh pr comment` (restored from old workflow)
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
- Verifies that review comment was posted (warning if missing)
- If `APPROVE`: executes `review_accept_and_merge.sh`
- If `REQUEST_CHANGES`: creates sub-issues via `review_request_changes.sh`
- If no decision file exists: workflow fails cleanly (manual intervention required)

### Step 3: Post Accurate Status Comment
- Comments on PR based on actual outcome
- Reflects whether merge succeeded, changes requested, or review failed
- Provides link to workflow run for debugging

## Benefits

1. **Visible review feedback**: Every PR now gets a review comment explaining findings
2. **Audit trail**: Review comments provide permanent record of what was reviewed
3. **Safe failure**: If Claude crashes, merge doesn't happen
4. **Accurate status**: Workflow success/failure matches actual outcome
5. **Clear separation**: Review logic (Claude) separate from execution (workflow script)
6. **Debuggable**: Decision file + review comments provide complete audit trail
7. **Idempotent**: Can re-run failed workflows without re-executing successful merges

## Migration Notes

- Old behavior: Claude executed `review_accept_and_merge.sh` directly, no review comments
- New behavior:
  - Claude posts review comment (restored from old claude-code-review.yml)
  - Claude writes decision to file
  - Workflow executes merge based on decision
- If Claude crashes before writing decision file, workflow fails (no merge)
- If Claude crashes before posting review comment, workflow warns but proceeds (decision file is source of truth)
- If workflow step fails after reading decision, status accurately reflects failure

## Testing

To test:
1. Create a test PR with clean code → should merge successfully with green workflow
2. Create a test PR with blocking issues → should create sub-issues, no merge
3. Simulate Claude crash (timeout) → workflow should fail, no merge

## Related Issues

- PR #2843: Merged successfully but workflow showed as cancelled + no review comments
- PR #2845: Had circular CI dependency (Claude checking for its own workflow) + no review comments
- Root causes:
  1. Combining claude-code-review.yml into review_agent_auto_review.yml created merge-before-failure regression
  2. Lost the explicit instruction to post review comments from old claude-code-review.yml
- Both issues discovered when user noted "there was actually no claude code review performed"
