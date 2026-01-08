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
mkdir -p ~/.myGPT
cp example.config.ini ~/.myGPT/config.ini

# Install services (Ollama, Cassandra, web UI)
mygpt ops install

# Verify health
mygpt ops doctor
```

### Running Tests
```bash
# Unit tests (fast, no dependencies)
pytest -m unit

# Integration tests (requires services)
pytest -m integration

# Coverage report
pytest --cov=src/mygpt --cov-report=term-missing
```

### Code Quality Checks
```bash
# Python linting
ruff check src/
mypy src/

# TypeScript linting (web UI)
cd web && npm run lint
```

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
