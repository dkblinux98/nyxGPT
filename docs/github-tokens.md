# GitHub Tokens and Secrets

This document lists all required tokens and secrets for the agent workflows.

## Required Secrets

Configure these in: **Settings → Secrets and variables → Actions → Secrets**

### Agent Tokens (Classic PATs)

Each agent bot account needs a classic Personal Access Token with these scopes:
- `repo` (full control of private repositories)
- `project` (full control of projects)
- `workflow` (update GitHub Action workflows)

**Important:** Each bot account must be added as a collaborator to the project with admin access:
- Go to: https://github.com/users/dkblinux98/projects/2/settings/access
- Invite each bot account: `nyxGPT-scrummaster-agent`, `nyxGPT-developer-agent`, `nyxGPT-review-agent`

| Secret Name | Bot Account | Used By |
|------------|-------------|---------|
| `SCRUMMASTER_AGENT_TOKEN` | `nyxGPT-scrummaster-agent` | notify_scrum_ready.yml, assign_backlog.yml |
| `DEVELOPER_AGENT_TOKEN` | `nyxGPT-developer-agent` | developer_auto_implement.yml |
| `REVIEW_AGENT_TOKEN` | `nyxGPT-review-agent` | review_agent_auto_review.yml |

### Other Tokens

| Secret Name | Description | Used By |
|------------|-------------|---------|
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token for Claude Code action | developer_auto_implement.yml, review_agent_auto_review.yml, claude.yml, claude-code-review.yml |

## Required Variables

Configure these in: **Settings → Secrets and variables → Actions → Variables**

| Variable Name | Example Value | Description |
|--------------|---------------|-------------|
| `AGENTS_ENABLED` | `true` | Master switch for all agent workflows |
| `DEV_AUTO_IMPLEMENT_ENABLED` | `true` | Enable developer auto-implementation |
| `CLAUDE_REVIEW_ENABLED` | `true` | Enable automated Claude code reviews |
| `REPO_OWNER` | `dkblinux98` | GitHub repository owner |
| `REPO_NAME` | `nyxGPT` | GitHub repository name |
| `PROJECT_OWNER` | `dkblinux98` | GitHub project owner (user or org) |
| `PROJECT_NUMBER` | `2` | GitHub project number |
| `DEV_AGENT` | `nyxGPT-developer-agent` | Developer bot username |
| `REVIEW_AGENT` | `nyxGPT-review-agent` | Review bot username |
| `SCRUM_AGENT` | `nyxGPT-scrummaster-agent` | Scrummaster bot username |
| `HUMAN_OWNER` | `dkblinux98` | Human repository owner |
| `STATUS_FIELD` | `Status` | Project status field name |
| `STATUS_BACKLOG` | `Backlog` | Backlog status value |
| `STATUS_IN_PROGRESS` | `In Progress` | In Progress status value |
| `STATUS_IN_REVIEW` | `In Review` | In review status value |
| `STATUS_FOR_RELEASE` | `For Release` | For release status value |
| `RELEASE_BRANCH` | `v1.0.0` | Active release branch name |
| `RELEASE_ISSUE_NUMBER` | `2709` | Release tracking issue number |
| `SPRINT_AUTOPILOT` | `false` | Sprint autopilot kill switch (#3480) — `true` enables the self-continuing merge -> next-issue loop; see `docs/sprint-autopilot.md` |
| `SPRINT_FIELD` | `Sprint` | Project iteration field name for sprint scoping/reporting (#3480); optional, defaults to `Sprint` |
| `STATUS_ACCEPTANCE_TESTING` | `Acceptance Testing` | Post-merge acceptance lane; optional, defaults to the literal name |
| `STATUS_ACCEPTANCE_FAILED` | `Acceptance Failed` | Drain-gate holding lane (#3730); optional, defaults to the literal name — see `docs/acceptance-drain-gate.md` |
| `DRAIN_GATE_BYPASS_LABELS` | *(empty)* | Comma-separated labels that mark an issue as agent-process work, exempt from the drain gate (#3730); optional |

### Release ceremony secrets (#3730)

| Secret | Purpose |
|---|---|
| `RELEASE_CEREMONY_TOKEN` | Owner-level token for the automated release ceremony — Phase 1 pushes `master`, which only the owner may do (ruleset bypass). Without it the ceremony cannot run. |
| `HOMEBREW_TAP_TOKEN` | Push access to `HOMEBREW_TAP_REPO`, for the stable tap stamp and the retirement of that line's `-rc` formulas. Optional: an unconfigured tap is a warning, not a failure. |

## Token Scopes Required

All agent tokens must have these classic scopes:
- ✅ `repo` - Full control of private repositories
- ✅ `project` - Full control of projects
- ✅ `workflow` - Update GitHub Action workflows

## Verifying Tokens

Test a token locally:
```bash
export GH_TOKEN="<token-value>"
gh auth status
gh project view 2 --owner dkblinux98
```

The token should show the required scopes and be able to access the private project.

## Creating New Tokens

1. Go to bot account Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token
3. Select scopes: `repo`, `project`, `workflow`
4. No expiration (or set appropriate expiration)
5. Generate token
6. Copy token value immediately (shown only once)
7. Add to repository secrets

## Adding Bot as Project Collaborator

For each bot account:
1. Go to: https://github.com/users/dkblinux98/projects/2/settings/access
2. Click "Invite collaborators"
3. Enter bot username (e.g., `nyxGPT-scrummaster-agent`)
4. Grant admin or write access
5. Bot will receive invitation (may need to accept from bot account)

Without this step, the bot tokens cannot access your private project even with the `project` scope.
