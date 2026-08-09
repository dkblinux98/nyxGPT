# Agent Roles and Responsibilities (nyxGPT)

This document defines the only valid responsibilities and allowed actions
for each machine agent in this repository.

Agents must follow these instructions exactly.
Project automations are deliberately minimal.
Agents are the sole authority for state transitions.

---

## Global Rules

- Use only scripts in scripts/agents/
- Do not rely on GitHub Project automations
- Leave an auditable comment for every state change
- Do not merge to main/master
- Do not improvise workflow

### Project Hygiene (All Agents)

Every agent is responsible for verifying project hygiene before reassigning issues/PRs:
- PRs must be linked to issues via `Closes #ISSUE` in PR body
- Issues must have required project fields populated (Status, Priority, etc.)
- Merged PRs without linked issues must be corrected before handoff
- Project fields must be accurate and up-to-date before state transitions

---

## GitHub API Usage: REST vs GraphQL

Issue/PR reads and Projects v2 field operations draw from two separate
GitHub API rate pools: GraphQL (5,000 points/hour) and REST (5,000
requests/hour). Projects v2 field reads/writes (Status, Sprint, Priority,
Effort, Module, iteration lookups -- everything behind
`scripts/agents/lib/gh_project.sh`'s `graphql()` wrapper) have no REST
equivalent and are the only thing that should draw from the GraphQL pool.
Every plain issue/PR read (title, body, labels, state, assignees, comments,
reviews, files, mergeable status, PR/issue lists) is available via REST and
MUST use it (#3663) -- freeing the shared GraphQL budget for what only it
can do.

Rule for new scripts/workflows:
- Plain issue/PR data -> `gh api repos/<owner>/<repo>/issues|pulls/<n>`
  (REST). Never `gh issue view` / `gh pr view` for this -- they are
  GraphQL-backed regardless of which `--json` fields are requested.
- List-shaped reads -> REST list endpoints with `--paginate` and
  `per_page=100` (never rely on gh's default page size). `gh pr list`
  has no `--label` filter; use the `/issues` endpoint with `labels=` and
  filter for/against `select(has("pull_request"))` to get PRs vs. issues.
- Full-text search ("does a PR exist with 'Closes #N' in its body", "was
  this issue mentioned in a merged PR") -> the REST Search API
  (`gh api search/issues --method GET -f q=...`), not `gh issue/pr list
  --search`. The Search API has its own separate rate pool (distinct from
  both core REST and GraphQL), so it never contends with either.
  **`--method GET` is required**: `gh api` defaults to POST whenever `-f`/
  `-F` params are present unless `--method`/`-X` says otherwise, and
  `search/issues` only accepts GET -- omitting the flag makes every call
  404 deterministically (confirmed via `gh api ... --verbose`, not a
  rate-limit or flake; see issue #3687's diagnosis comment).
- Projects v2 field reads/writes (Status, Sprint, Priority, Effort, Module,
  iteration metadata) -> GraphQL, via `gh_project.sh`'s `graphql()` helper.
  This is the one case where GraphQL is correct: Projects v2 has no REST API.
- REST issue `state` is lowercase (`open`/`closed`); GraphQL's is uppercase
  (`OPEN`/`CLOSED`). When reading state, pipe through
  `jq '.state | ascii_upcase'` to match the uppercase comparisons used
  throughout the codebase. REST PR `mergeable`/`mergeable_state` is shaped
  differently than GraphQL's single `MERGEABLE`/`CONFLICTING`/`UNKNOWN`
  enum -- re-derive it with `if .mergeable == null then "UNKNOWN" elif
  .mergeable_state == "dirty" then "CONFLICTING" else "MERGEABLE" end`
  rather than comparing REST fields directly against the old GraphQL values.
- **`--paginate` + `--jq` never aggregates across pages.** `gh api ...
  --paginate --jq FILTER` runs `FILTER` once per fetched page, not once over
  the combined result set -- each page is a separate JSON document. Any
  `FILTER` that reduces across the whole list (`last`, `length`, `first`,
  `sort_by(...) | ...`) silently computes that reduction per page instead of
  over the true total once a list crosses the page boundary (default 30,
  or `per_page`'s value). This is invisible in manual testing against small
  result sets and only breaks once a PR/issue accumulates enough
  comments/reviews to span multiple pages. Verify: `gh api
  ".../issues/<N>/comments?per_page=10" --paginate --jq 'length'` on an
  issue with >10 comments prints one number per page, not the combined
  total. Fix: never combine an aggregate into the `--jq` passed to `gh api`
  when `--paginate` is used. Stream flat, already-filtered items instead
  (`--jq '.[] | select(COND)'`, no enclosing `[...]`), then pipe into a
  second `jq -s '...'` (slurp) call that does the aggregation over the full
  combined array, e.g.:
  `gh api ".../comments" --paginate --jq '.[] | select(COND)' | jq -s 'last | .body // empty'`.
  The same applies when `--paginate` is used *without* `--jq` and the raw
  output is piped to a separate `jq` afterward -- that `jq` still sees one
  top-level JSON array per page unless invoked with `-s`; slurp and flatten
  one level (`jq -s '[.[][]]'` or `'[.[][] | select(...)] | length'`) before
  aggregating.

---

## Project Status Semantics

Backlog      – approved, unscheduled
In Progress  – active development
In Review    – awaiting review (agent review OR human stakeholder acceptance after merge)
For Release  – stakeholder accepted, ready for release (human sets this)
Closed       – released (human only)

**Important**: After merge, issues remain in "In Review" status (CLOSED in GitHub, but "In Review" in project) until human stakeholder acceptance. The human owner moves accepted issues to "For Release".

**Note**: "In Progress" uses uppercase P for consistency across all systems.

---

## scrummaster-agent

Controls backlog intake and sequencing.

Allowed:
- Select next issue by lowest Phase then lowest issue number
- Set issue → In Progress
- Assign issue → developer-agent

Scripts:
- scrummaster_next_issue.sh
- scrummaster_start_issue.sh <ISSUE>
- scrummaster_dispatch_next.sh — select-and-start with fall-through: one
  unclaimable candidate (stray assignee, human hold) excludes and retries
  the next candidate instead of blocking the whole queue (#3665)

Forbidden:
- Writing code
- Creating PRs
- Closing issues

---

## developer-agent

Implements features and fixes.

Allowed:
- Create branches
- Write code and tests
- Open PRs
- Move issue → In Review
- Assign review-agent

Scripts:
- developer_create_branch.sh <ISSUE>
- developer_submit_for_review.sh <ISSUE> "<PR TITLE>"
- validate-web-routes.sh (validates web proxy routes match backend endpoints)

Validation Requirements:
- All pre-commit hooks MUST pass before commit succeeds
- Developer keeps working until all checks pass: black, ruff, mypy, pytest
- Only after all checks pass can commit/push/PR creation happen
- This ensures CI checks during review should not fail

Forbidden:
- Merging PRs
- Setting For Release
- Closing issues manually

---

## review-agent

Owns and performs code reviews. Initiates review when assigned as PR reviewer.

Review Trigger:
- Automatically triggered when developer-agent assigns review-agent as reviewer
- Can be manually re-triggered via:
  - New commit to PR branch
  - `@review` comment on PR
  - Manual workflow dispatch

Workflow:
1. Review workflow triggers when review-agent is assigned as reviewer
2. Run CI checks on ALL code in repository (not just changed files)
3. Review ALL changed files in PR (not just new changes from current cycle)
4. Post review comment with recommendation (APPROVE or REQUEST_CHANGES)
5. Review workflow executes automatically:
   - Review agent runs all tests and linters on entire codebase
   - Reviews all code changes in PR against acceptance criteria and quality standards
   - Posts structured review comment: "## Code Review - [APPROVE|REQUEST_CHANGES]"
   - Automation executes decision automatically (no human confirmation needed)

On CI failure (should not happen if developer phase worked correctly):
- Review-agent still reviews code
- Captures all issues (CI failures + code review findings)
- Proceeds with normal REQUEST_CHANGES flow
- Set issue → In Progress
- Assign issue → developer-agent
- Comment with all findings

On code review decision:
- **APPROVE**: Automation merges PR immediately via review_accept_and_merge.sh, assigns issue to human for acceptance
- **REQUEST_CHANGES**:
  - Issue returns to developer-agent with "In Progress" status
  - Developer reads review comment and fixes all Critical/Medium issues
  - Developer runs tests in 3-try loop (resets each time) BEFORE committing
  - Developer commits fixes and re-submits for review (triggers re-review)
  - Review cycle continues (cumulative count)
  - **After 3rd REQUEST_CHANGES cycle**:
    - Issue remains "In Review" status
    - Reassign issue to HUMAN_OWNER
    - Send Slack DM to human
    - Human intervenes to resolve

Scripts:
- manually_trigger_pr_review.sh <PR> - Manually trigger review workflow (for re-reviews or if auto-trigger failed)
- review_accept_and_merge.sh <PR> <ISSUE> - Executed by automation to merge approved PRs

Note: Review workflow triggers automatically when developer-agent runs
developer_submit_for_review.sh and assigns review-agent as reviewer

---

## Human

Closes releases and advances phases.

---

## Executive Assistant (Claude for ad-hoc tasks)

Supports the human owner during stakeholder acceptance with ad-hoc administrative tasks.

Role:
- Executes one-off requests outside the agent workflow
- Handles bulk operations (e.g., bulk-assign backlog issues)
- Fixes project hygiene issues discovered during acceptance
- Uses the most efficient means to accomplish tasks (direct gh/GraphQL is acceptable)

Examples:
- Bulk-assigning backlog issues to scrummaster-agent
- Fixing missing project fields on PRs/issues
- Administrative cleanup and corrections
- Documentation updates

Not an agent role:
- Does not follow strict agent workflow rules
- Does not participate in automated workflows
- Announces current role when switching between executive assistant and agent roles

---

Final rule:
If it's not explicitly allowed above, it must not be done.
