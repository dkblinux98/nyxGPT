# Review Runbook (review-agent)

## 0) Preconditions
- PR targets active release branch.
- CI is green (required to merge unless human exception).

## 1) Review checklist
- Correctness vs issue acceptance criteria
- Tests added/updated and meaningful
- No architecture boundary violations
- No secrets committed
- Clear docs updates for user-facing changes
- Reasonable maintainability

## 2) Severity model
- Critical: correctness/security/data-loss; must block merge
- Medium: significant bug risk, missing tests, broken contract; must block merge
- Minor: style/nits; may proceed

## 3) Acceptance Failure loop (blocking findings)
For each Critical/Medium finding:
- Create ONE sub-issue labeled `Acceptance Failure`
- Copy key context and reproduction details
- Inherit Phase/Sprint fields
- Assign to developer-agent and set status -> In Progress

## 4) Merge criteria
- No open Critical/Medium Acceptance Failure items
- CI green
- PR approved

## 5) Post-merge
- Merge into active release branch
- Delete short-lived feature/fix branches created for the feature
- Move parent issue -> For Release
- Notify scrummaster-agent that developer-agent is ready for next issue

## 6) Phase completion
When the last issue in the active Phase reaches For Release (or equivalent complete state):
- Notify human owner for stakeholder acceptance
