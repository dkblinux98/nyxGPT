# Developer Runbook (developer-agent)

This is the procedural “how” for implementing issues. Authority is defined in the charter.

## 0) Preconditions
- Repo clean, on correct base (release branch or per project rule)
- Up to date with remote
- Services healthy (if applicable)
- Tests passing before starting

## 1) Pick up work
- Ensure issue is assigned to developer-agent and status is In Progress.
- Confirm Phase/Sprint fields are set.

## 2) Branching
- Create a short-lived branch named with issue reference, e.g.:
  - `feat/<issue-id>-<slug>` or `fix/<issue-id>-<slug>`
- Base off the current active release branch.

## 3) Implement
- Make smallest coherent change set that satisfies acceptance criteria.
- Add/extend tests (unit/integration as appropriate).
- Keep IO behind interfaces; maintain dependency flow.

## 4) Verification loop
- Run test suite locally until green.
- If flaky tests appear, isolate and fix; escalate if persistent.

## 5) Documentation
- Update docs for any user-facing change.
- Update architecture notes only if human-approved architecture change is required.

## 6) Commit discipline
- Small commits with clear messages.
- Reference issue in PR description (and/or commit) so GitHub links them.

## 7) Open PR
- Target: active release branch.
- Ensure CI runs.
- Update issue status -> In Review.
- Assign issue to review-agent.

## 8) Address review
- If review-agent creates Acceptance Failure sub-issues:
  - treat them as blocking
  - fix one-by-one
  - link fixes in PR(s)
