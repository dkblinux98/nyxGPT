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

### First Principles (All Agents, owner requirement 2026-08-16)

Prior to every role's permissions, and binding on all agentic work in this
repository regardless of which agent or session performs it:

1. **Consider cost.** Every run and every re-check spends real money. Prefer
   the cheaper path that answers the question.
2. **Consider future harm**, to the agentic process as much as to the
   application. A fix that leaves a trap for the next session is unfinished.
3. **Minimize both without compromising quality or completeness.** These
   constrain how the work is done, never how much of it gets delivered.
4. **Never take change action without first seeking to understand.** Diagnose
   before fixing; a fix aimed at a guess is how one defect gets patched three
   times.

Stated in full in `CLAUDE.md` § Agentic First Principles.

### The Operating Ledger (All Agents, #3774)

`agents/LEDGER.md` is the system of record for cross-session memory: decisions
made, items deliberately parked
(with reason and revisit condition), and questions left open.

- Read it in full at session start.
- **A claim that is not in the ledger and not freshly verified is not asserted
  as fact.** Consult it before stating project state; if it is silent and you
  have not checked, say so rather than reconstructing.
- Append an entry whenever you decide, verify, or park something — through the
  normal branch/PR path, riding in the PR that produced the fact.
- Do not overwrite state you did not create (a held lane, an owner's marker)
  without first checking the ledger for a parked entry explaining it.

Entry schema and granularity rules live in the ledger itself.

### Project Hygiene (All Agents)

Every agent is responsible for verifying project hygiene before reassigning issues/PRs:
- **PRs are linked to issues natively** — GitHub's own closing-issue link
  (the "Development" sidebar, and the `closingIssuesReferences` edge behind
  it), which is what actually closes the issue on merge. `Closes #ISSUE` in
  the body is one way to *create* that link and `developer_submit_for_review.sh`
  still writes it, but nothing requires the text: every consumer reads the
  link through `pr_linked_issue` (`lib/gh_project.sh`) and falls back to the
  body only for PRs that predate this. Requiring the sentence was prose
  standing in for a relationship the platform stores — the same mistake as
  driving workflows from comment tokens (owner rule, 2026-08-19).
- A PR that closes **no** issue is legitimate but rare; the issue-side
  automation stops loudly rather than guessing.
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
equivalent and are the main draw on the GraphQL pool. The other, added
2026-08-19, is the **PR/issue closing link** (`closingIssuesReferences`,
`closedByPullRequestsReferences`) -- GitHub exposes it nowhere in REST, and
the alternative was deriving it from `Closes #N` prose, which is what this
repo retired.

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

## Issue invariants (owner rules, 2026-08-19)

**Exactly one assignee.** An issue is assigned to exactly one identity at a
time — whoever owns the work right now. Handing work on means *replacing* the
assignee, never adding to it: the developer hands a PR to the review agent and
stops being the assignee; an escalation hands the issue to the owner and the
agent stops being the assignee. Use `assign_issue_verified` (or PATCH the
issue with the full list); GitHub's *add* verbs — `POST /issues/{n}/assignees`
and `issues.addAssignees` — append, and are refused by
`tests/unit/test_one_assignee_one_label.py`.

**Exactly one label.** An issue carries exactly one real label
(`Feature`, `Acceptance Failure`, `Improvement`, `Agent`, `Documentation`,
`Release Management`, `Production Defect`, …). Workflow-control labels such as
`usage-limit-retry` do not count — `real_label_names` is the one definition,
shared by project hygiene and `developer_submit_for_review.sh`, which fails
outright on a second label. Hygiene stamps `Feature` only on an issue with no
real label at all.

Both rules are old and both drifted anyway, which is why they are now checked
rather than written down: an issue showing two agents makes "who owns this?"
unanswerable from the board and miscounts every sweep that asks who an issue
is assigned to, and a second label deadlocks the issue at submit time.

---

## scrummaster-agent

Controls backlog intake and sequencing.

Allowed:
- Groom the sprint: scope, order, relationships, effort (taking developer
  feedback where an estimate is contested), and the per-issue expected-files
  list — written to `product_management/sprint_planning/sprint_<N>/PLAN.md`
- Set Sprint / Priority / Effort fields; maintain eligible work
- Push an issue directly when the owner asks for it

**Selection is retired here (#3883).** The rule was "lowest Phase, then
lowest issue number", and the scrummaster pushed the result at the developer.
Developers now *pull* — from the plan's order, filtered by relationships,
WIP and file overlap. Preparation is the scrummaster's job; choosing what to
work next is the developer's.

Scripts:
- groom_sprint.sh [--sprint TITLE] — writes the seed draft of the sprint plan
- scrummaster_start_issue.sh <ISSUE> — the claim mechanism (Status → In
  Progress, then assign), used by the pull and by an owner push
- scrummaster_dispatch_next.sh — the dispatch-pause backstops and the
  fall-through retry: one unclaimable candidate (stray assignee, human hold)
  excludes and retries the next candidate instead of blocking the whole
  queue (#3665)

Forbidden:
- Writing code
- Creating PRs
- Closing issues

---

## developer-agent

Implements features and fixes.

Allowed:
- **Pull the next issue** from the groomed sprint plan (#3883): plan order,
  filtered by relationships eligibility, a WIP limit read from the board and
  open PRs, and a file-overlap check against work already in flight. An
  overlapping candidate is deferred, never pulled in parallel — scheduling is
  how conflicts are avoided rather than resolved
- Claim it: Status → In Progress, then assign itself (the actor doing the
  work owns the transition)
- Create branches
- Write code and tests
- Open PRs
- Move issue → In Review
- Assign review-agent

Scripts:
- developer_pull_next.sh [--sprint-scoped] — the pull decision
- developer_create_branch.sh <ISSUE>
- developer_submit_for_review.sh <ISSUE> "<PR TITLE>"
- validate-web-routes.sh (validates web proxy routes match backend endpoints)

Validation Requirements:
- All pre-commit hooks MUST pass before commit succeeds (file hygiene,
  yamllint, secret detection -- black/ruff/mypy were removed from the hooks
  in 2026-08-18's de-duplication; they are CI gates now)
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
2. The `head-gate` job decides whether the head is reviewable at all (#3971);
   the review invocation starts only on a head whose required checks are green
3. Review ALL changed files in PR (not just new changes from current cycle)
4. Post review comment with recommendation (APPROVE or REQUEST_CHANGES)
5. Review workflow executes automatically:
   - Reviews all code changes in PR against acceptance criteria and quality standards
   - Posts structured review comment: "## Code Review - [APPROVE|REQUEST_CHANGES]"
   - Automation executes decision automatically (no human confirmation needed)

On a red or pending head (#3971) — the review agent is not involved, and
check state is **not** a review finding:
- **Red**: `developer_submit_for_review.sh` refuses to submit it at all, and a
  head that goes red after submission is handed back to developer-agent by
  assignment before any review invocation is spent. GitHub already displays
  the failure; relaying it cost a reject round plus a re-fix round.
- **Pending**: the trigger *waits*. A PR that is merely mid-CI is never
  bounced back to the developer.
- The required set is the named list in `.github/required-checks.txt` — never
  "every check on the head". See `docs/reviewable-head-gate.md`.

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
