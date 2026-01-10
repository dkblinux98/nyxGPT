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

## Triggering the workflow

To start the next issue:

**Option 1: CLI script (recommended)**
```bash
./scripts/trigger_next_issue.sh <release_issue_number>
```

Example:
```bash
./scripts/trigger_next_issue.sh 2843
```

**Option 2: Manual comment**
Post a comment containing `READY_FOR_NEXT_ISSUE` in the **Release tracking issue**:
```
@myGPT-scrummaster-agent READY_FOR_NEXT_ISSUE
```

The workflow will:
- Post status updates on the Release tracking issue
- Select the next backlog issue based on deterministic rules
- Move that issue to In Progress and assign to developer-agent
- Trigger the developer auto-implementation workflow

**Monitoring:**
Use `./scripts/watch_agents.sh` to monitor all agent workflows in real-time.

## Wait
- Remain idle until triggered by READY_FOR_NEXT_ISSUE signal

## Phase completion
- When all issues in active Phase are complete:
  - notify human owner for acceptance
  - do not start next phase until human closes phase
