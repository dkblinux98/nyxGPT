# Agent Scripts (myGPT)

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
- `REPO_NAME`  (default: `myGPT`)
- `PROJECT_OWNER` (default: `dkblinux98`)  # user login that owns Project v2
- `PROJECT_NUMBER` (default: `2`)
- `RELEASE_BRANCH` (default: autodetected; falls back to `release/latest`)

Agent usernames:
- `DEV_AGENT` (default: `mygpt-developer-agent`)
- `REVIEW_AGENT` (default: `mygpt-review-agent`)
- `SCRUM_AGENT` (default: `mygpt-scrummaster-agent`)
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
- `scrummaster_next_issue.sh` — prints the next issue number to work (best effort ordering)
- `scrummaster_start_issue.sh <ISSUE_NUMBER>` — set issue -> In Progress, assign dev agent

### developer-agent
- `developer_create_branch.sh <ISSUE_NUMBER> [feat|fix] [slug]` — create branch from release branch
- `developer_submit_for_review.sh <ISSUE_NUMBER> "<PR_TITLE>" [PR_BODY_FILE]` — open PR + set issue -> In Review, assign review-agent

### review-agent
- `review_request_changes.sh <ISSUE_NUMBER> "<TITLE>" <BODY_FILE>` — create Acceptance Failure sub-issue and bounce parent -> In Progress
- `review_accept_and_merge.sh <PR_NUMBER_OR_URL> <ISSUE_NUMBER>` — set issue -> For Release, assign human owner, merge PR to release branch, delete branch

## Troubleshooting
- Run with `DEBUG=1` to print GraphQL responses and commands.
