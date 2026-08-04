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

### Review Trigger (review-agent ownership)
The review workflow is triggered when review-agent is assigned as reviewer:
- **Automatic**: When `developer_submit_for_review.sh` assigns review-agent as reviewer
- **Manual re-trigger options**:
  - Push new commit to PR branch (triggers on synchronize)
  - Comment `@review` on the PR
  - Run via GitHub Actions UI: `gh workflow run claude-code-review.yml -f pr_number=<N>`

The review-agent OWNS the review process:
- Review workflow uses `REVIEW_AGENT_TOKEN` for all GitHub operations
- Review comments are posted by review-agent (claude[bot])
- Review-agent orchestrates the auto-fix loop (developer-agent executes fixes)

## 1) Review checklist

**IMPORTANT:**
- Run CI checks on ALL code in the repository (not just changed files)
- Review ALL changed files in the PR (not just new changes from current cycle)
- This ensures comprehensive quality coverage across the entire codebase
- **Reproduce gate failures with the EXACT gate commands, never approximations.**
  The web test gate is `npx vitest run --coverage` (100% line/branch/function/
  statement thresholds) — plain `npx vitest run` passes while the coverage
  gate fails, which has twice caused reviewers to mislabel a real coverage
  failure as unreproducible CI flakiness and burn escalation cycles
  (PR #3423 on 2026-07-29, PR #3486 on 2026-07-31). If a gate reports FAIL
  and your local run passes, diff your command against the workflow's
  (`.github/workflows/claude-code-review.yml`) before concluding flakiness;
  a coverage-threshold failure names the metric in the vitest output.

### Core Requirements (from project standards)
- Correctness vs issue acceptance criteria
- Tests added/updated and meaningful
- No architecture boundary violations
- No secrets committed
- Clear docs updates for user-facing changes
- Reasonable maintainability
- **End-to-end usability (Definition of Done, CLAUDE.md):** nyxGPT user features must be usable from the web interface; ops/SRE features must be operable from the SRE/admin dashboard. A backend-only implementation is a Medium (blocking) finding unless the issue explicitly scopes it backend-only with owner approval and a linked frontend follow-up issue.
- **Workflow actor gates (#3600, going-public hardening):** any new or edited `.github/workflows/*.yml` job triggered by `issues`, `issue_comment`, `pull_request`, or `pull_request_review*` that carries write permissions or a secret-backed `GH_TOKEN` MUST gate its `if:` on the actor's identity (`comment.user.login`/`review.user.login` against `vars.HUMAN_OWNER` or the relevant agent var) — a trigger phrase with no author check is a Medium (blocking) finding. See `agents/runbooks/developer-runbook.md` §3b for the pattern and the fork-PR guard requirement on merge/review paths.
- **Live verification (#3555/P6-18):** if the PR touches observability, metrics, or a UI surface, run `nyxgpt ops verify` yourself and cite its output/screenshots — see §2's "Live verification" entry below for the full rule.

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

### Live verification: run the harness, don't defer it (Owner decision
2026-08-01, narrowed 2026-08-04 by #3555/P6-18)

The original 2026-08-01 rule deferred every live-running-stack finding
(Grafana panels rendering, a running Compose stack, chat/RAG round-trips) to
owner acceptance testing, because neither agent had a running stack or eyes
on rendered output (PR #3548/#3469 deadlocked three review cycles demanding
evidence that couldn't structurally be produced). #3555/P6-18 closed that
gap: `nyxgpt ops verify` (see `docs/live-verification-ci.md`) boots the
Compose stack in CI, generates known chat/RAG traffic, asserts it landed via
Prometheus instant queries and Grafana's HTTP API re-executing each touched
dashboard panel's own query, and captures Playwright screenshots.

**On every PR that touches observability, metrics, or a UI surface, the
review agent runs this harness itself, in CI, before deciding:**

1. Run `nyxgpt ops verify` (the review workflow's environment is prepared
   for this — see `docs/live-verification-ci.md` for what CI installs).
2. Read the full assertion output. A failing Prometheus counter delta or
   Grafana panel-query check names the exact panel/query that failed —
   treat it as a Critical or Medium finding per the severity model below,
   the same as any other reproduced failure.
3. Use the Read tool on every screenshot under
   `~/.nyxGPT/verify-artifacts/` and *visually inspect* it (the review
   agent is multimodal) — a rendered-but-empty or visibly broken panel is a
   finding even if the underlying query technically returned data.
4. Include a "### Live Verification" section in the review body summarizing
   the harness run and what the screenshots show. **An APPROVE on an
   eligible PR that skipped running the harness, or that has no "### Live
   Verification" section, is a process violation** — not a style nit, treat
   it the same as approving with failing tests.

**What still defers to owner acceptance** — because CI genuinely cannot
exercise it, not because it's inconvenient — is the short, explicit list
`docs/live-verification-ci.md` documents: the Apple Silicon native
brew-services install path (CI only exercises the Compose path), real Slack
delivery (no real webhook secret in CI), and LLM response *quality* (CI's
model is stubbed/tiny — the pipeline being intact end to end is what's
asserted, not answer quality). List exactly which of these apply, explicitly,
in the APPROVE review so the owner knows precisely what to exercise during
acceptance. Do not REQUEST_CHANGES or burn escalation cycles demanding
evidence the harness already produced, or evidence that's on the
not-covered list above (still a structural impossibility for CI) — escalation
is reserved for unresolved findings the agents *could* fix but haven't.

## 3) CI failure handling
If CI fails during review (should not happen if developer phase worked correctly):
- Still review the code changes
- Capture all issues (CI failures + code review findings)
- Proceed with normal REQUEST_CHANGES flow
- Set issue status -> In Progress
- Assign issue -> developer-agent
- Comment with all findings (CI + code issues)

Note: Pre-commit hooks should prevent CI failures. If they occur, treat as REQUEST_CHANGES.

## 4) Review and recommendation
After completing the review:
- Post a structured review comment starting with "## Code Review - [APPROVE|REQUEST_CHANGES]"
- Include findings organized by severity (Critical/Medium/Minor)
- Provide clear recommendation with rationale

## 5) Automatic execution
The review decision is automatically executed based on the review comment:
- **APPROVE**: Workflow automatically merges the PR (no human confirmation required)
- **REQUEST_CHANGES**:
  - Issue returns to developer-agent with "In Progress" status
  - Developer reads review comment and implements fixes
  - Developer runs tests in 3-try loop (resets each assignment) BEFORE committing
  - Developer commits and re-submits for review (triggers re-review automatically)
  - Review cycle repeats (cumulative count tracked)
  - After 3 review cycles: Issue stays "In Review", escalates to human owner

Manual override (optional):
- `@approve-merge` - Human can manually trigger merge
- `@request-changes` - Human can manually trigger changes workflow (legacy)

## 6) Review cycle escalation
The review workflow tracks cumulative review cycles:
- Each REQUEST_CHANGES increments the cycle counter
- Developer 3-try loop (for test failures) resets each time issue is reassigned
- Review 3-cycle limit is cumulative across all reviews for this PR
- After 3rd REQUEST_CHANGES review:
  - Issue remains Status -> In Review
  - Issue reassigned to HUMAN_OWNER
  - Slack DM sent to human
  - Human intervenes to resolve

All fixes happen on the PR branch (no separate issues created).

## 7) Merge criteria
- All tests and linters passing
- Code review APPROVE decision (either from review agent or human override)

## 8) Post-merge
When PR is merged (automatically on APPROVE or via human `@approve-merge` override):
- Automation merges into active release branch (NEVER merge to master/main)
- Delete short-lived feature/fix branches created for the feature
- Close the issue (GitHub state)
- Set issue status -> Acceptance Testing (for human stakeholder acceptance)
- Assign issue -> human owner (dkblinux98)
- Notify scrummaster-agent that developer-agent is ready for next issue

### Important: Issue auto-close behavior
- PRs merged to the release branch (e.g., v1.0.0) do NOT auto-close linked issues
- GitHub only auto-closes issues when PRs merge to the default branch (master)
- Automation manually closes issues after merging to release branch
- Post-merge: issue should be CLOSED (GitHub state) + Acceptance Testing (project status) + assigned to human

## 9) Human stakeholder acceptance

After merge, each issue is assigned to the human owner with status "Acceptance Testing" for final acceptance -- the owner-created gate (2026-07-31) that marks work as merged and ready to acceptance-test, so unmerged In Review issues are never tested by mistake.

### If acceptance passes
Move the issue to "For Release" in the project board. No action needed in GitHub.
**Gate:** an issue with related acceptance-failure issues (see below) is NOT
moved to "For Release" by hand — the promotion sweep
(`promote_accepted_features.yml`, every 30 min) moves it automatically once
every related failure issue has itself been accepted (For Release).

### If acceptance reveals an improvement (not a defect)

When acceptance testing shows the feature works as specified but the spec was
incomplete or wrong, that is a **product management failure**, not an
acceptance failure — the metric is charged to requirements, not to
implementation (owner decision 2026-08-01). File it as a **new Backlog issue**
through the normal flow (it does not jump the queue the way a defect does):

- Label: **"Improvement"** (the existing label stands — no new label)
- Body includes a `Related feature: #N` line when a related feature exists.
  If no feature issue applies, that is NOT a blocker — file and work the
  Improvement without it (owner decision 2026-08-02).

Per feature, the two labels give two separate counts for metrics:
"Acceptance Failure"-labeled related issues = implementation failures;
"Improvement"-labeled related issues filed during acceptance = requirements
gaps. An improvement does NOT gate the feature's move to "For Release" —
only acceptance-failure issues do.

### If acceptance fails (bug found after merge)

1. **Go to the issue** (it is assigned to you and closed)
2. **Add a comment** describing what is broken — be specific:
   - What you expected
   - What actually happened
   - Steps to reproduce if relevant
3. **On the same or a separate comment, write:** `@acceptance-failure`

That's it. The system will automatically (related-issue model, owner decision
2026-08-02):
- Leave the original feature/doc/release/improvement issue **intact** — it
  stays closed, keeps its labels, and remains in "Acceptance Testing". The
  original never re-enters the dev/review cycle.
- Create a **new** issue labeled "Acceptance Failure", RELATED to the
  original via a `Related feature: #N` body line and marked as **blocking**
  the original (native issue-dependency relationship — the original's
  Relationships panel shows exactly what holds it back). NOT a sub-issue.
  Module/Priority/Effort/Milestone copy from the original; Sprint is the
  active sprint.
- Set the new issue to "In Progress" and assign the developer agent to create
  a `fix/N-...` branch and PR with `Closes #N`.

**If the failure issue's fix fails your re-test:** comment
`@acceptance-failure` on the FAILURE issue itself (the one carrying the
`Related feature: #N` body marker — that marker, not the "Acceptance
Failure" label, is what the automation keys on, since owner-filed defect
issues carry the label too while being parents) — it is **reopened** and
sent back through dev → review (no new issue; its own history is the trail
of that failure's resolution). A genuinely NEW, distinct failure of the
feature gets its own related issue via a comment on the feature.

**Promotion:** when every related acceptance-failure issue reaches
"For Release", the feature is promoted to "For Release" automatically by
the promotion sweep, with a comment recording which failures cleared it.
Unique-failure count per feature (usually 1) = its related
"Acceptance Failure" issues; rework rounds live inside each failure issue's
history.

> **Note:** `@acceptance-failure` is only accepted from the human owner account and only on
> issues (not PRs). It is entirely separate from the review-loop overrides
> (`@approve-merge`, `@request-changes`, `@send-to-developer`) which apply to PRs
> during the automated code review cycle.
>
> **Rollout:** `issue_comment` workflows run from the repository default
> branch — empirically the active release branch, so this model went live
> when it merged (first live run 2026-08-02, on the v2.0.0 tail failures).

## 10) Phase completion
When the human owner moves the last issue in the active Phase to "For Release" (human stakeholder acceptance):
- Notify human owner that phase is complete and ready for release

## 11) Configuration

### Required Secrets
The review workflow requires these secrets to be configured:
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token for Claude Code agent
- `REVIEW_AGENT_TOKEN` - GitHub token with repo/project permissions (used for review workflow)

**How to configure secrets:**
- Navigate to: Settings → Secrets and variables → Actions → Secrets
- Click "New repository secret"
- Add all secrets if not already present

### Branch Cleanup
Branch deletion happens in `review_accept_and_merge.sh` via `--delete-branch`, not in auto-fix workflow.
