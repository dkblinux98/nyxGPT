# Agent Scripts (nyxGPT)

These scripts implement the **explicit workflow plumbing** for:
- scrummaster-agent
- developer-agent
- review-agent

They update GitHub Projects v2 fields via GraphQL and perform common operations via `gh`.

## Prereqs
- GitHub CLI installed: `gh`
- Auth: the caller must be logged in with a token that has classic scopes: `repo`, `project`
  - Example: `gh auth login` (interactive) or `GH_TOKEN=...`

## Configuration
Scripts read defaults from environment variables (see `env.example`):

- `REPO_OWNER` (default: `dkblinux98`)
- `REPO_NAME`  (default: `nyxGPT`)
- `PROJECT_OWNER` (default: `dkblinux98`)  # user login that owns Project v2
- `PROJECT_NUMBER` (default: `2`)
- `RELEASE_BRANCH` (default: autodetected; falls back to `release/latest`)

Agent usernames:
- `DEV_AGENT` (default: `nyxgpt-developer-agent`)
- `REVIEW_AGENT` (default: `nyxgpt-review-agent`)
- `SCRUM_AGENT` (default: `nyxgpt-scrummaster-agent`)
- `HUMAN_OWNER` (default: `dkblinux98`)

Field names (must match your Project fields):
- `FIELD_STATUS` (default: `Status`)
- `FIELD_PRIORITY` (default: `Priority`)
- `FIELD_EFFORT` (default: `Effort`)
- `FIELD_MODULE` (default: `Module`)
- `FIELD_SPRINT` (default: `Sprint`)
Optional:
- `FIELD_PHASE` (default: `Phase`)  # if you have it; otherwise scripts fall back to issue number ordering

Status option names:
- `STATUS_BACKLOG` (default: `Backlog`)
- `STATUS_IN_PROGRESS` (default: `In Progress`)
- `STATUS_IN_REVIEW` (default: `In Review`)
- `STATUS_FOR_RELEASE` (default: `For Release`)

## Important behavioral choice: when issues are closed
These scripts assume the best-practice behavior:
- Developer **does not close** the issue when opening the PR.
- The PR body includes `Closes #<issue>` so the issue closes on merge.
This preserves your release-issue checklist signal: unchecked = planned, checked = completed.

## Commands
### scrummaster-agent
- `groom_sprint.sh [--sprint TITLE] [--out PATH]` — writes the seed draft of `product_management/sprint_planning/sprint_<N>/PLAN.md`: sprint members with their fields and native relationships, expected-files seeded from the issue bodies, and a seed order (dependencies, then priority, then effort). The scrummaster agent then grooms it — the order's justification is judgment, not a sort key (#3908). Re-running mid-sprint is a regroom: rationale, deferrals, the regroom log and curated expected-files survive.
- `scrummaster_start_issue.sh <ISSUE_NUMBER>` — classifies the issue's claim state and, if claimable, sets it -> In Progress and assigns the dev agent. Exit codes distinguish outcome: `0` started, `10` skipped quietly (in-flight duplicate, a deliberate human hold, or no longer open — not a block), `11` skipped loudly (unrecognized assignee — reported via a comment on the issue). See the start-guard decision matrix in `lib/gh_project.sh`'s `classify_backlog_claim_state` (#3665).
- `scrummaster_dispatch_next.sh [--sprint-scoped]` — runs the dispatch-pause backstops and the fall-through loop used by `developer_pull_next_issue.yml`: pulls a candidate (`developer_pull_next.sh`), attempts to start it, and on any skip excludes it and retries the next candidate, so a single bad-state issue can no longer block the whole queue (#3665, root cause of a ~5 day sprint-loop stall on #3593).

### developer-agent
- `developer_pull_next.sh [--sprint-scoped] [--explain FILE]` — the pull (#3883): prints the issue number to take next, chosen by sprint-plan order, relationships eligibility, the WIP limit (`WIP_LIMIT`, default 2, read from the board and open PRs) and a file-overlap check against in-flight work. Overlapping candidates are deferred rather than pulled in parallel. `--explain` writes the decision and everything it skipped as JSON, for the dispatch comment. `EXCLUDE_ISSUES` skips candidates, for fall-through retries within one dispatch (#3665).
- `developer_create_branch.sh <ISSUE_NUMBER> [feat|fix] [slug]` — create branch from release branch
- `developer_submit_for_review.sh <ISSUE_NUMBER> "<PR_TITLE>" [PR_BODY_FILE]` — open PR + set issue -> In Review, assign review-agent

### review-agent
- `manually_trigger_pr_review.sh <PR_NUMBER>` — manually trigger review workflow (for re-reviews or if auto-trigger failed). Dispatches against `RELEASE_BRANCH`, never the repo default branch — see `agents/runbooks/review-runbook.md` §5a
- `review_ensure_handoff.sh <PR_NUMBER>` — dispatch-mode backstop (#3704): verifies a REQUEST_CHANGES verdict actually handed off to the developer/huddle/owner, and performs the handoff itself if the event chain dropped it. Run automatically as the last step of a `workflow_dispatch` review; idempotent, so it is safe to re-run by hand
- `review_accept_and_merge.sh <PR_NUMBER_OR_URL> <ISSUE_NUMBER>` — merge PR to release branch, delete branch, set the merged PR's own project card -> `Closed` (#3742), close issue, set issue status -> Acceptance Testing, assign human owner for stakeholder acceptance

### PR lane hygiene (#3742)
The invariant: no merged or closed PR's project card sits in an active lane.
All three paths are agent-side (no reliance on the board's built-in "Pull
request merged" automation) and idempotent, and none of them touch issues.
- `pr_close_project_status.sh [--dry-run] <PR_NUMBER>` — stamps a merged/closed PR's card to `STATUS_CLOSED` (default `Closed`); no-op for an open PR. Run automatically by `pr_project_status_on_close.yml` on every `pull_request: closed`, covering rejected PRs and merges the review agent did not perform.
- `reconcile_pr_lane.sh` — backstop sweep for cards that predate the invariant or lost a stamp to a flaky API. `SOURCE_STATUS` narrows to one lane (blank = every active lane), `TARGET_STATUS` overrides the destination, `DRY_RUN=true` (default) lists only. Run daily in apply mode by `sweep_pr_status.yml`.

### branch hygiene
- `reconcile_dead_branches.sh [--dry-run] [base_branch]` — sweeps `claude/*`, `feat/*`, `fix/*`, `chore/*` branches and deletes ones that are merged/contained in `base_branch`, superseded (linked issue closed + equivalent commits already on `base_branch`), or the head of a PR closed without merging. `developer_create_branch.sh` also auto-deletes superseded prior-attempt branches for the same issue every time it creates/reuses a branch.

### validation
- `validate-web-routes.sh` — validates that web proxy routes exist for all backend API endpoints

## Troubleshooting
- Run with `DEBUG=1` to print GraphQL responses and commands.
