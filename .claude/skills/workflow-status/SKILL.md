---
name: workflow-status
description: Show status of all in-flight issues, PRs, and recent workflow runs for the nyxGPT agent loop.
---

Run the following checks in parallel and output a concise summary table:

1. Open issues assigned to any agent:
   `gh issue list --state open --json number,title,assignees,labels --limit 20`

2. Open PRs with review status:
   `gh pr list --state open --json number,title,reviewRequests,reviews,headRefName`

3. Recent workflow runs (last 10):
   `gh run list --limit 10 --json name,status,conclusion,headBranch,createdAt`

4. Any PRs awaiting re-review (requested_reviewers non-empty):
   `gh pr list --state open --json number,title,reviewRequests --jq '.[] | select(.reviewRequests | length > 0)'`

Format output as markdown tables. Flag anything that looks stuck (open issue unassigned, PR with no reviews after 1+ day, failed workflow runs).
