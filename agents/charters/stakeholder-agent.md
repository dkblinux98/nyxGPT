# stakeholder-agent Charter (optional)

## Mission
Notify the human owner when stakeholder acceptance is required, and hold
the approval authority for changes scrummaster-agent may only propose.

## Authority
- May only notify; does not modify repo state directly.
- **Sprint reorganization approval (#3480):** scrummaster-agent's sprint
  reports may include a reorganization proposal (which Backlog issues to
  move out of the active Sprint) when the sprint is projected off-track.
  This role -- exercised by the human owner, since there is no separate
  automated stakeholder identity -- is the sole authority that can turn
  that proposal into an actual change:
  - Approve by commenting `APPROVE_SPRINT_REORG` on the release tracking
    issue. This triggers `scrummaster_sprint_reorg_apply.sh`, which applies
    the most recent unapplied proposal via the existing project field
    scripts and posts a summary of exactly what moved.
  - Decline by doing nothing, or by commenting anything else. An unapproved
    proposal changes nothing -- the sprint stays as-is.
  - Pause/resume the sprint autopilot loop at any time with `PAUSE_SPRINT`
    / `RESUME_SPRINT` comments on the release tracking issue, independent
    of any reorganization decision.

## Triggers (notification-only, pre-#3480)
- Active Phase completed (all issues complete/For Release).
