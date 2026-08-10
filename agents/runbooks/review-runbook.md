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
  - Dispatch-mode recovery: `scripts/agents/manually_trigger_pr_review.sh <N>`
    (see §5a — always use the script, not a bare `gh workflow run`)

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

## 5a) Dispatch-mode recovery (`workflow_dispatch`, #3704)

**When to use it.** A PR is orphaned: it is open, the review agent is the
requested reviewer, and no review workflow run exists for it (the automatic
`review_requested`/`synchronize` trigger never fired, or its run died before
posting a verdict). This is the *only* supported way to start a review from
outside the PR event stream.

```bash
scripts/agents/manually_trigger_pr_review.sh <PR_NUMBER>
```

**Always use the script, never a bare `gh workflow run claude-code-review.yml`.**
Without `--ref`, `gh workflow run` dispatches the copy of the workflow on the
repo **default branch** (`master`), which under this project's branch rules
only moves on releases and is therefore arbitrarily far behind the active
release branch. Dispatched reviews were consequently executing a stale
workflow definition — including one whose `--json-schema` predated the #3687
`disagreement_type` field, so every dispatched REQUEST_CHANGES silently fell
back to type (a) and, on its second cycle, routed to a huddle instead of the
fix cycle the reviewer had actually called for. The script now pins
`--ref "$RELEASE_BRANCH"`.

**What dispatch mode guarantees.** Same outcome as an event-triggered review,
for both verdicts:

- **APPROVE** — merged by `review_agent_auto_review.yml` exactly as before.
- **REQUEST_CHANGES** — the handoff is guaranteed, not merely attempted.
  The primary path is still the event chain (`pull_request_review`, with the
  `nyxgpt-structured-review` comment as the `issue_comment` fallback). On top
  of that, the review run's own final step
  (`scripts/agents/review_ensure_handoff.sh`) waits ~4 minutes for that chain
  to leave a footprint on the PR and, if none appears, executes the same
  routing decision itself: resolve the linked issue from the PR body's
  `Closes #N`, then loop / huddle / escalate per §6b.

  Routing is not duplicated — the backstop calls the same
  `huddle_routing_decision` in `scripts/agents/lib/sprint_calc.py` the primary
  workflow does, so the two can never disagree. It is idempotent: every action
  it takes writes one of the handoff markers the footprint check looks for, so
  a re-run (or a late-firing event chain) is a no-op.

  Backstop-posted comments carry a "_Posted by the dispatch-mode review
  handoff backstop_" footer. **Seeing that footer means the event chain
  dropped a handoff** — worth a look at the review run's logs, even though
  the cycle itself recovered.

This closes the failure observed 2026-08-09/10, where dispatched
REQUEST_CHANGES verdicts on PRs #3684, #3683 and #3606 produced zero fix
activity for 9+ hours until a human posted `RETRY_IMPLEMENTATION` by hand.

## 6) Review cycle escalation
The review workflow tracks cumulative review cycles:
- Each REQUEST_CHANGES increments the cycle counter
- Developer 3-try loop (for test failures) resets each time issue is reassigned
- Review 3-cycle limit is cumulative across all reviews for this PR
- After 3rd REQUEST_CHANGES review:
  - Issue remains Status -> In Review
  - Issue reassigned to HUMAN_OWNER
  - Slack DM sent to human (`notify_human_escalation`,
    `scripts/agents/lib/gh_project.sh`, #3695 -- reuses the existing
    `SLACK_BOT_TOKEN` + `SLACK_USER_ID` secrets already configured for
    `notify-merge-conflicts.yml`; missing secrets or a failed Slack call
    degrade gracefully to the GitHub comment alone, which is always posted
    first and unconditionally; repeat escalations on the same PR within a
    60-minute window are deduped)
  - Human intervenes to resolve

All fixes happen on the PR branch (no separate issues created).

## 6b) Disagreement taxonomy & huddle protocol (owner-ratified 2026-08-09, #3687)

The 3-cycle gate above is a circuit breaker, not a decision-maker: it counts
failed cycles without ever changing the question. Every REQUEST_CHANGES
round must be classified as one of three disagreement types, posted in a
`### Disagreement Type` section of the structured review comment
(`.github/workflows/claude-code-review.yml`) as `**[a|b|c]**: [reason]`:

- **(a) verifiable defect** — a failing test/CI or a reproducible bug. Loops
  as in §5/§6, but **each round must state a *new* diagnosis**, not retry
  the old one. If your diagnosis hasn't changed since the last round even
  though the fix attempt did, that is itself a signal to reclassify as (b):
  the approach, not the diagnosis, is what's wrong.
- **(b) judgment call** — a design/approach disagreement, not a bug. This
  type **never loops** — it goes straight to a huddle instead of another fix
  cycle, regardless of what cycle count it's on.
- **(c) spec ambiguity** — the issue itself is unclear, or resolving it
  needs owner authority no agent conversation can supply. This **escalates
  to the owner immediately, cycle zero** — no agent conversation can resolve
  what only the PM knows.

Routing (`scripts/agents/lib/sprint_calc.py huddle-routing`, the
`huddle_routing_decision` function, called by
`review_agent_auto_review.yml`'s "Count review iterations and classify
disagreement" step): type (c) always escalates immediately; type
(b) always huddles immediately; type (a) follows the existing loop and
huddles on its **2nd** failed cycle instead of attempting a 3rd blind
retry, still hitting the unchanged 3-cycle outer breaker (§6) if the huddle
doesn't resolve it. A missing/malformed classification defaults to (a) —
degrades to pre-#3687 behavior rather than blocking the loop or
over-escalating.

**The huddle**, once triggered (`review_agent_auto_review.yml`'s "Trigger
huddle" step posts the `HUDDLE_TRIGGERED` marker):
1. The review agent's position is the code review comment already posted —
   nothing further to do here.
2. `developer_huddle_position.yml` runs the developer agent to post a
   written position (what it believes the problem is, what was tried, what
   it proposes) instead of attempting another fix, then posts
   `HUDDLE_MEDIATION_REQUESTED`.
3. `scrummaster_huddle_mediation.yml` runs a **fresh** scrummaster
   invocation — fresh context is structural, every invocation starts
   memoryless — that reads only the PR thread and posts a
   `## Huddle Decision` comment choosing one of: **proceed** / **change
   approach** (stated) / **descope** (e.g. drop a named test, split the
   issue) / **escalate to owner** (runs the same `assign_issue_verified` +
   `sprint_autopilot_kick` escalation primitives §6 uses).
4. The next fix cycle (`developer_auto_implement.yml`'s "Run Claude Code to
   fix review issues" step) reads any `HUDDLE_DECISION:` comment on the PR
   and executes the agreed plan rather than deciding independently.

## 6c) Unresolved-escalation dispatch pause backstop (#3687)

Escalations (§6's 3-cycle limit, or §6b's type-(c)/type-(b)-deadlock
escalate) must not silently accumulate: one escalated item is normal
traffic, but two or more open at once usually signals something systemic
(bad base commit, poisoned suite, review-prompt regression). "Unresolved
escalation" = an open issue currently assigned to `HUMAN_OWNER`
(`count_unresolved_escalations`/`escalation_pause_gate`,
`scripts/agents/lib/gh_project.sh`) — purely derived from live issue state,
no hidden counter. `scrummaster_dispatch_next.sh` checks this gate before
selecting a Backlog candidate: with 0 or 1 unresolved, dispatch proceeds
unconditionally; with 2+, new dispatch pauses and a loud report (listing
the escalated issues) is posted/updated on the release tracking issue.
Dispatch resumes automatically the next time it's checked once the count
drops below 2 — clearing the escalations is the only action needed, there
is no separate "resume" step. See `agents/runbooks/scrummaster-runbook.md`
for the dispatch-side detail.

## 7) Merge criteria
- All tests and linters passing
- Code review APPROVE decision (either from review agent or human override)

## 8) Post-merge
When PR is merged (automatically on APPROVE or via human `@approve-merge` override):
- Automation merges into active release branch (NEVER merge to master/main)
- Delete short-lived feature/fix branches created for the feature
- Close the issue (GitHub state)
- Check the issue's native blocked-by dependencies (`/issues/{n}/dependencies/blocked_by`):
  - **No open blockers (normal case):** set issue status -> Acceptance
    Testing, assign issue -> human owner (dkblinux98)
  - **Open blockers exist (parked case, owner process rule, 2026-08-04,
    #3631):** set issue status -> In Review instead, do NOT assign the human
    owner, comment on the issue naming the open blockers. See §9 "Parked
    issues" for how the set later moves to Acceptance Testing together.
- Notify scrummaster-agent that developer-agent is ready for next issue

### Important: Issue auto-close behavior
- PRs merged to the release branch (e.g., v1.0.0) do NOT auto-close linked issues
- GitHub only auto-closes issues when PRs merge to the default branch (master)
- Automation manually closes issues after merging to release branch
- Post-merge, non-parked case: issue should be CLOSED (GitHub state) +
  Acceptance Testing (project status) + assigned to human
- Post-merge, parked case: issue should be CLOSED (GitHub state) + In Review
  (project status) + NOT assigned to human (still shows the review agent /
  prior assignee) until the sweep promotes it

## 9) Human stakeholder acceptance

After merge, each issue is assigned to the human owner with status "Acceptance Testing" for final acceptance -- the owner-created gate (2026-07-31) that marks work as merged and ready to acceptance-test, so unmerged In Review issues are never tested by mistake.

### Parked issues (merged but blocked, owner process rule 2026-08-04, #3631)

A merged issue whose acceptance criteria depend on other, still-open issues
cannot be meaningfully accepted -- the owner would be testing against
unfinished work. `review_accept_and_merge.sh`'s post-merge step checks the
issue's native blocked-by dependencies; if any are still open, it parks the
issue at **In Review** instead of Acceptance Testing and does NOT assign the
human owner, commenting why and listing the open blockers. This never blocks
the merge itself or branch/PR cleanup -- only the acceptance handoff.

The parked issue moves to Acceptance Testing (and the owner is assigned)
only once **every** blocker is complete -- merged and itself in Acceptance
Testing or beyond (For Release). This is enforced by a sweep
(`sweep_parked_blocked_issues.sh`, run via
`.github/workflows/sweep_parked_blocked_issues.yml`, every 30 min +
`workflow_dispatch` with a `dry_run` input): it finds every parked issue and
promotes any whose blockers have all completed, posting a promotion comment
on each. A chain of parked issues (A blocked by B blocked by C) resolves
transitively within one sweep run -- once C completes, B promotes in the
same pass, which in turn makes A eligible and it promotes too, rather than
each hop waiting for a separate 30-minute cycle.

Motivating case: #3508 was parked back to In Review by the owner, blocked by
#3621/#3622 -- its repo-less-portability acceptance criteria are unmeetable
until those land.

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
