# Development Guide

## GitHub Workflows & Automation

### Claude Code Automation

This repository uses GitHub Actions to enable AI-assisted development with Claude Code. There are two workflows that provide different levels of automation:

#### 1. Claude Code Workflow (`claude.yml`)

**Trigger**: Manual invocation via `@claude` mention

**Purpose**: On-demand AI assistance for issues and pull requests

**Runs when**:
- An issue is opened or assigned **and** contains `@claude` in the body or title
- Someone comments on an issue **and** the comment contains `@claude`
- Someone comments on a PR review **and** the comment contains `@claude`
- Someone submits a PR review **and** the review body contains `@claude`

**How to use**:
```markdown
@claude Please help implement this feature according to the specifications in CLAUDE.md
```

**Capabilities**:
- Read repository code and documentation
- Create/update issues and PRs
- Comment on issues and PRs
- Execute git operations
- Run tests and checks
- Access CI results

**Example use cases**:
- Ask Claude to implement a feature from an issue
- Request code review with specific focus areas
- Get help debugging a failing test
- Ask for documentation updates

#### 2. Automatic Code Review Workflow (`claude-code-review.yml`)

**Trigger**: Automatic on all PRs

**Purpose**: Automatic code review for every pull request

**Runs when**:
- A pull request is opened
- New commits are pushed to an open pull request

**Review focus**:
- Code quality and best practices
- Potential bugs or issues
- Performance considerations
- Security concerns
- Test coverage

**Note**: Uses the repository's `CLAUDE.md` for style and convention guidance.

---

## Development Workflow

### Standard Workflow (See CLAUDE.md and AGENTS.md)

1. Select an issue from the current milestone
2. Create an issue-linked feature branch
3. Implement changes following style guidelines
4. Write/update tests
5. Run quality checks (pytest, ruff, mypy)
6. Create PR with proper formatting
7. Wait for automatic code review
8. Merge when approved

### AI-Assisted Workflow

For complex tasks or when you need help:

1. **On an issue**: Comment with `@claude` + instructions
   ```markdown
   @claude Please implement this feature. Follow the workflow in AGENTS.md.
   ```

2. **On a PR**: Request specific review focus
   ```markdown
   @claude Please review the error handling in this PR for edge cases.
   ```

3. **New issue**: Include `@claude` in title or body to get immediate AI assistance
   ```markdown
   Title: Implement session export feature @claude
   Body: Need to add session export in JSON and Markdown formats.
   @claude Please implement according to acceptance criteria below.
   ```

---

## Development Environment Setup

### Prerequisites
- Python 3.11+
- Node.js 18+ (for web UI)
- Docker Desktop (for Cassandra)
- Ollama (for LLM backend)

### Local Setup
```bash
# Install in editable mode
pip install -e .

# Copy config template
mkdir -p ~/.nyxGPT
cp example.config.ini ~/.nyxGPT/config.ini

# Install services (Ollama, Cassandra, web UI)
nyxgpt ops install

# Verify health
nyxgpt ops doctor
```

### Running Tests
```bash
# Unit tests (fast, no dependencies)
pytest -m unit

# Integration tests (requires services)
pytest -m integration

# Coverage report
pytest --cov=src/nyxgpt --cov-report=term-missing
```

### Code Quality Checks
```bash
# Python linting
ruff check src/
mypy src/

# Docstring coverage (config in pyproject.toml, must stay at 100%)
interrogate src/nyxgpt
# Also enforced automatically by `pytest tests/unit/test_docstring_coverage.py`,
# which calls interrogate's Python API against the same [tool.interrogate]
# config, so a regression fails CI via the existing pytest gate.

# TypeScript linting (web UI)
cd web && npm run lint
```

---

## Automated Agent Workflows

Beyond the on-demand `@claude` workflow above, this repository runs a
continuous automated agent loop:

- **Scrummaster Agent** — selects and dispatches the next backlog issue
- **Developer Agent** — implements issues end-to-end with Claude Code
- **Review Agent** — reviews PRs and manages the merge workflow

See [AGENTS.md](../AGENTS.md) and `agents/runbooks/` for the full role
definitions, permissions, and state-transition rules.

**To trigger the workflow:**

```bash
./scripts/trigger_next_issue.sh <release_issue_number>
```

Or manually post a comment containing `READY_FOR_NEXT_ISSUE` in the
**Release tracking issue**.

The workflow will:
1. Select the next backlog issue (lowest Phase, lowest issue number)
2. Move it to In Progress and assign it to developer-agent
3. Auto-implement the issue with Claude Code
4. Create a PR and submit it for review

**Monitor agent activity in real-time:**

```bash
./scripts/watch_agents.sh
```

**Collect and analyze historical workflow logs:**

`watch_agents.sh` only shows live/recent runs (subject to GitHub's log
retention). To keep a durable, queryable history for post-mortem analysis
and trend detection, collect completed workflow runs into a local SQLite
store:

```bash
# Fetch recent completed runs and store them (safe to run repeatedly; re-run rows are upserted)
./scripts/collect_workflow_logs.sh collect --repo dkblinux98/nyxGPT

# Query stored runs
./scripts/collect_workflow_logs.sh query --workflow "Developer Agent" --conclusion failure

# Show aggregated analytics: success rate, avg duration, failure trends, top failing workflows
./scripts/collect_workflow_logs.sh stats --days 30

# Enforce the retention policy (default: 90 days)
./scripts/collect_workflow_logs.sh purge --retention-days 90
```

All commands accept `--db PATH` to override the default store location
(`~/.nyxGPT/logs/workflow_runs.sqlite3`) and `--json` (query/stats) for
machine-readable output. To run collection automatically, add a scheduled
GitHub Actions workflow that invokes `collect_workflow_logs.sh collect`
(this requires editing `.github/workflows/`, which Claude Code is not
permitted to do — a human maintainer needs to add it).

There is no admin-dashboard surface for this history: an owner scope decision
(2026-07-25, issue #3358) ruled the CI Workflow Analytics dashboard out of
scope for nyxGPT — it's an agent-level display, a candidate for a future
nyxAgent project rather than this app. Use the CLI (`query`/`stats` above) to
inspect collected history.

---

## Claude Code Local Automations

This repository includes Claude Code automations that activate automatically
when using `claude` in the project directory:

| Automation | Type | Description |
|---|---|---|
| GitHub MCP server | MCP | Native GitHub API access (issues, PRs, Actions, project board) — no `gh` CLI syntax needed |
| context7 MCP | MCP | Live, version-correct docs for FastAPI, Cassandra, React, Next.js, Anthropic SDK, etc. |
| Auto-lint on edit | Hook | Runs `ruff --fix` + `black` automatically after every Python file edit |
| Workflow guard | Hook | Prompts for confirmation before editing any `.github/workflows/` file |
| `test-gap-detector` | Subagent | Ask "check test coverage" — runs pytest with `--cov` and reports files under 80% |
| `/workflow-status` | Skill | Shows all open issues, PRs, and recent workflow run status in one table |

**Required:** The GitHub MCP server reads your PAT from `~/.nyxGPT/config.ini`:

```ini
[github]
pat = ghp_your_personal_access_token
```

The PAT needs `repo` and `project` scopes. All other automations (context7,
hooks, subagent, skill) work with no additional setup.

---

## Contributing

See [CLAUDE.md](../CLAUDE.md) for quick reference and [AGENTS.md](../AGENTS.md) for detailed workflows and procedures.

Key guidelines:
- Follow code style conventions
- Write tests for all new code
- Update documentation as needed
- Use conventional commit messages
- Keep PRs focused and reasonably sized

---

## Resources

- [CLAUDE.md](../CLAUDE.md) - Quick reference for Claude Code sessions
- [AGENTS.md](../AGENTS.md) - Comprehensive agent guidelines and workflows
- [API Documentation](api.md)
- [Configuration Guide](configuration.md)
- [Testing Guide](testing.md)
