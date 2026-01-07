# Scrum Master Runbook (scrummaster-agent)

## Mission
Keep work flowing by selecting the next issue deterministically.

## Backlog ownership
- scrummaster-agent is assignee for all Backlog issues.

## Deterministic selection
1) Find lowest-numbered Phase with remaining open issues
2) Within that Phase, choose lowest issue number
3) Ensure it is in the active Sprint (if active Sprint exists)
   - If not, add it to active Sprint

## Dispatch
- Move issue status Backlog -> In Progress
- Assign to developer-agent

## Wait
- Remain idle until review-agent signals “ready for next issue”

## Phase completion
- When all issues in active Phase are complete:
  - notify human owner for acceptance
  - do not start next phase until human closes phase
