# GitHub Tokens and Secrets

This document lists all required tokens and secrets for the agent workflows.

## Where these values come from

**`~/.nyxGPT/config.ini` is the canonical store for every name on this page,
and `nyxgpt ops config-sync` is the only supported way to get them into
GitHub** (#3505, #3976):

```bash
nyxgpt ops config-sync --dry-run   # names and destinations only, no network call
nyxgpt ops config-sync             # push every mapped secret and variable
nyxgpt ops config-drift            # reconcile config.ini against example.config.ini
```

Which `config.ini` key maps to which name is declared in
`SECRETS_SYNC_MANIFEST` and `VARIABLES_SYNC_MANIFEST` in
`src/nyxgpt/config.py`, and `tests/unit/test_sync_manifests.py` reconciles
both against the `vars.`/`secrets.` references the workflows actually make --
so a name added to a workflow without a config.ini key behind it fails the
build rather than becoming a value somebody has to remember typing in.

Typing a value into the settings UI instead still works, and is still the
wrong move: it produces a repository whose configuration cannot be
reconstructed from `config.ini` on a clean machine, which is the state
#3976 was filed to end. The token running the sync needs **admin** on the
repository -- managing Actions secrets and variables is a stronger
permission than repository write.

Values are never printed: sync results name the secret or variable and
whether it was set, never what it was set to.

## Required Secrets

Configure these with `nyxgpt ops config-sync`; they land in
**Settings → Secrets and variables → Actions → Secrets**

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
| `SCRUMMASTER_AGENT_TOKEN` | `nyxGPT-scrummaster-agent` | scrummaster_groom_sprint.yml, assign_backlog.yml, huddle_session.yml (the decision turn and the decision comment) |
| `DEVELOPER_AGENT_TOKEN` | `nyxGPT-developer-agent` | developer_auto_implement.yml, huddle_session.yml (dev turns) |
| `REVIEW_AGENT_TOKEN` | `nyxGPT-review-agent` | review_agent_auto_review.yml, huddle_decision_dispatch.yml, huddle_session.yml (job default + review turns) |

**The huddle's decision comment must be authored by the scrummaster.**
`huddle_decision_dispatch.yml` acts only on a `HUDDLE_DECISION:` comment
whose author is `vars.SCRUM_AGENT` or `vars.HUMAN_OWNER`, so
`huddle_session.yml` overrides `GH_TOKEN` to `SCRUMMASTER_AGENT_TOKEN` for
the decision turn and the step that posts it. Posting it under the job's
review-agent default is a silent failure: the comment lands, looks correct,
and starts no fix cycle (the #3733 stall).

### Other Tokens

| Secret Name | Description | Used By |
|------------|-------------|---------|
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token for Claude Code action | developer_auto_implement.yml, review_agent_auto_review.yml, claude.yml, claude-code-review.yml |

## Required Variables

Configure these with `nyxgpt ops config-sync` too; they land in
**Settings → Secrets and variables → Actions → Variables**. Unlike a secret,
a variable is readable by anyone with read access to the repository -- which
is why the two manifests are kept structurally disjoint and a credential can
never be pushed to this side.

| Variable Name | Example Value | Description |
|--------------|---------------|-------------|
| `AGENTS_ENABLED` | `true` | Master switch for all agent workflows |
| `DEV_AUTO_IMPLEMENT_ENABLED` | `true` | Enable developer auto-implementation. **No workflow reads this today** — it has no `config.ini` key and is not in `VARIABLES_SYNC_MANIFEST` for that reason; setting it changes nothing. |
| `CLAUDE_REVIEW_ENABLED` | `true` | Enable automated Claude code reviews. **No workflow reads this today** either — like the row above it has no `config.ini` key and is deliberately absent from `VARIABLES_SYNC_MANIFEST`; setting it changes nothing. |
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
| `STATUS_CLOSED` | `Closed` | Terminal lane for a merged/closed PR's own project card (#3742); optional, defaults to the literal name — the merge flow, the `pull_request: closed` handler and the daily sweep all stamp it |
| `DRAIN_GATE_BYPASS_LABELS` | *(empty)* | Comma-separated labels that mark an issue as agent-process work, exempt from the drain gate (#3730); optional |
| `AGENT_MODEL_DEV` | `claude-opus-5` | Model for the developer agent's implementation runs (`developer_auto_implement.yml`); optional, defaults to `claude-opus-5` |
| `AGENT_MODEL_REVIEW` | `claude-fable-5` | Model for the review agent, the `@claude` entry point and the developer's failure-analysis step; optional, defaults to `claude-fable-5` |
| `AGENT_MODEL_HUDDLE` | `claude-fable-5` | Model for every turn of a review huddle — the developer and review turns of each round and the scrummaster's decision, all run by `huddle_session.yml` (#3911); optional, defaults to `claude-fable-5` (ledger D-014) |
| `AGENT_MODEL_CANARY` | `claude-haiku-4-5-20251001` | Model for the CLAUDE.md binding canary (`claude-md-binding-canary.yml`); optional, defaults to `claude-haiku-4-5-20251001` |
| `HUDDLE_MAX_ROUNDS` | `3` | Round cap for `huddle_session.yml` (#3911); optional, defaults to `3`. **Configurable downward only** — the rounds are three explicit pairs of steps, so any value above 3 behaves as 3 |
| `REVIEW_CI_WAIT_MINUTES` | `60` | How long the review agent waits for a PR's CI to settle before giving up (`claude-code-review.yml`); optional, defaults to `60` and is capped by that job's own timeout |
| `HOMEBREW_TAP_REPO` | *(empty)* | `owner/repo` of the Homebrew tap the `-rc` and stable formulas are pushed to; optional — blank skips the tap push with a notice rather than failing the release |
| `CHURN_PRICE_SHEET_JSON` | *(empty)* | Per-model price sheet (JSON) the retrospective uses to attach dollars to token churn; optional — blank leaves the churn dump in tokens only |
| `SLACK_HUDDLE_CHANNEL` | *(empty)* | Channel id the huddle conversation is threaded in (#3910); optional — unset degrades the huddle to transcript-only, it never fails the run |

### Review huddle Slack identities (#3910)

| Secret | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_USER_ID` | The bot token the merge-conflict notifier posts with, and the owner's Slack member id the agent system DMs on an escalation (#3695). Both come from `[monitoring]` in `config.ini`. |
| `SLACK_USER_TOKEN_DEV` / `SLACK_USER_TOKEN_REVIEW` / `SLACK_USER_TOKEN_SCRUM` | User tokens with the `chat:write` user scope, one per agent, so each huddle turn posts under its own identity. A bot token would put every turn under one name, which defeats a thread you can read back. All three are **optional**: a missing token degrades that speaker (and a missing channel degrades the whole thread) to the PR transcript alone — `huddle_session.yml` deliberately does not validate them, because refusing to huddle over an unconfigured chat integration is a worse failure than the one it reports. |

### Switching agent models without a commit

The `AGENT_MODEL_*` variables exist so a model change is a setting, not a
code change — when a model is refused (see the ledger's D-010 for the
monthly-spend-limit signature), flip the variable and the next workflow run
picks it up:

Edit the key in `config.ini` and push it:

```ini
# ~/.nyxGPT/config.ini
[github]
agent_model_review = claude-opus-5
```

```bash
nyxgpt ops config-sync
```

Unset means the default in the table above, so the workflows keep the intended
dev/review/huddle/canary split with no variables configured at all.

### Release ceremony secrets (#3730)

| Secret | Purpose |
|---|---|
| `RELEASE_CEREMONY_TOKEN` | Owner-level token for the automated release ceremony — Phase 1 pushes `master`, which only the owner may do (ruleset bypass). Without it the ceremony refuses to start: the watcher fails fast before claiming the release issue, comments the reason and DMs the owner, so nothing is tagged or published half-way. |
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
