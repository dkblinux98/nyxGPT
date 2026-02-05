# Contributing to nyxGPT

Thank you for contributing to nyxGPT! This document provides guidelines for setting up your development environment and submitting changes.

---

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Git
- Docker (optional, for Cassandra RAG features)

### Initial Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/dkblinux98/nyxGPT.git
   cd nyxGPT
   ```

2. **Create a virtual environment:**
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install development dependencies:**
   ```bash
   pip install -e ".[dev,ui]"
   ```

4. **Install pre-commit hooks:**
   ```bash
   pre-commit install
   ```

   This will automatically run code quality checks before each commit.

5. **Verify the setup:**
   ```bash
   pre-commit run --all-files
   ```

---

## Pre-Commit Hooks

Pre-commit hooks ensure code quality and consistency before changes are committed. They run automatically on `git commit`.

### What Gets Checked

1. **YAML Files:**
   - Syntax validation (`check-yaml`)
   - Style and formatting (`yamllint`)

2. **Python Files:**
   - Code formatting (`black`)
   - Linting and style (`ruff`)
   - Type checking (`mypy`)

3. **General:**
   - Trailing whitespace removal
   - End-of-file fixing
   - Large file detection
   - Secret detection
   - Merge conflict markers

### Running Checks Manually

Run all checks on all files:
```bash
pre-commit run --all-files
```

Run specific checks:
```bash
# YAML only
pre-commit run yamllint --all-files

# Python formatting only
pre-commit run black --all-files

# Python linting only
pre-commit run ruff --all-files

# Type checking only
pre-commit run mypy --all-files
```

Run checks on specific files:
```bash
pre-commit run --files src/nyxgpt/cli.py
```

### Bypassing Hooks (Not Recommended)

Bypassing pre-commit hooks defeats their purpose and should be avoided:
```bash
git commit --no-verify -m "Your message"  # ❌ Skips ALL checks
```

**Why this is problematic:**
- Broken code enters the repository
- CI/CD pipelines fail, wasting resources
- Other developers may pull broken code
- Only delays feedback (CI will catch issues anyway)

See the [Commit Workflow](#commit-workflow-how-pre-commit-hooks-prevent-bad-commits) section for the proper way to handle blocked commits.

### Updating Hooks

Keep pre-commit hooks up to date:
```bash
pre-commit autoupdate
```

---

## Code Quality Standards

### Python

1. **Formatting (Black):**
   - Line length: 100 characters
   - Automatically formats code on commit

2. **Linting (Ruff):**
   - Enforces Python best practices
   - Automatically fixes many issues
   - Checks for common bugs and anti-patterns

3. **Type Checking (mypy):**
   - Type hints encouraged but not strictly enforced
   - Configuration in `pyproject.toml`

### YAML

1. **Syntax:**
   - Must be valid YAML
   - Checked with `check-yaml`

2. **Style (yamllint):**
   - 2-space indentation
   - Max line length: 120 characters
   - Truthy values: `true`/`false` or `yes`/`no`

### Secrets Detection

- All commits are scanned for secrets (API keys, tokens, passwords)
- Uses `.secrets.baseline` for known false positives
- Never commit real secrets to the repository

---

## Testing

### Running Tests

Run all tests:
```bash
pytest
```

Run specific test categories:
```bash
# Unit tests only (fast, no external dependencies)
pytest -m unit

# Integration tests (requires Docker services)
pytest -m integration
```

Run with coverage:
```bash
pytest --cov=nyxgpt --cov-report=html
open htmlcov/index.html  # View coverage report
```

### Writing Tests

- **Unit tests** (`tests/unit/`) - Mock all I/O, fast execution
- **Integration tests** (`tests/integration/`) - Test with real services (Cassandra, Ollama)

---

## Git Workflow

### Branch Naming

- **Feature branches:** `feat/<issue>-<short-description>`
- **Fix branches:** `fix/<issue>-<short-description>`
- **Docs:** `docs/<description>`

Examples:
- `feat/123-add-pdf-support`
- `fix/456-rag-search-bug`
- `docs/update-readme`

### Commit Messages

Follow conventional commits format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types:
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `test:` - Adding or updating tests
- `refactor:` - Code refactoring
- `chore:` - Maintenance tasks

Example:
```
feat(rag): add PDF document ingestion

- Add PDF text extraction with pdfplumber
- Implement chunking strategy for large documents
- Update RAG pipeline to handle PDF embeddings

Closes #123
```

### Commit Workflow (How Pre-Commit Hooks Prevent Bad Commits)

Pre-commit hooks automatically run quality checks **before** each commit is created. This prevents broken code from entering the repository and triggering failed CI/CD runs.

#### How It Works

When you run `git commit`, the following happens automatically:

1. **Git calls the pre-commit hook** (installed in `.git/hooks/pre-commit`)
2. **Hooks run on staged files only** (files you `git add`ed)
3. **All checks must pass** for the commit to succeed
4. **If any check fails**, the commit is **blocked** and changes are not committed

#### The Proper Commit Workflow

```bash
# 1. Make your changes
vim src/nyxgpt/api.py

# 2. Stage your changes
git add src/nyxgpt/api.py

# 3. Attempt to commit
git commit -m "feat(api): add new endpoint"

# Pre-commit hooks run automatically:
# ✓ trim trailing whitespace...........Passed
# ✓ fix end of files...................Passed
# ✓ Check YAML syntax..................Passed
# ✓ Format Python code with Black......Passed
# ✓ Lint Python code with Ruff.........Passed
# ✓ Type check Python code with mypy...Passed
# ✓ Detect secrets in code.............Passed
#
# ✅ Commit succeeds!
```

#### When Hooks Block a Commit (Example)

```bash
# You accidentally introduce a YAML syntax error
vim .github/workflows/test.yml

git add .github/workflows/test.yml
git commit -m "Update workflow"

# Pre-commit hooks run:
# ✓ trim trailing whitespace...........Passed
# ✓ fix end of files...................Passed
# ✗ Check YAML syntax..................Failed  <-- BLOCKS COMMIT
#
# ❌ Commit BLOCKED! No changes committed.
# Files remain staged, ready to fix and retry.
```

#### What Pre-Commit Hooks Prevent

**Without pre-commit hooks:**
1. Commit code with syntax errors ❌
2. Push to GitHub ❌
3. **10+ CI workflow runs fail** ❌
4. Manually fix and push again ❌
5. **Another 10+ CI workflow runs** ❌
6. Finally succeeds (after wasting time and resources)

**With pre-commit hooks:**
1. Attempt to commit code with errors ✓
2. **Pre-commit blocks immediately** ✓
3. Fix locally ✓
4. Commit succeeds ✓
5. Push to GitHub ✓
6. **All CI workflows pass on first try** ✓

#### Fixing Issues Caught by Hooks

When hooks block a commit, fix the issues and try again:

```bash
# Hooks found issues
git commit -m "Update code"
# ✗ Format Python code with Black......Failed
# ✗ Lint Python code with Ruff.........Failed

# Fix the issues (many auto-fix themselves)
# Black and Ruff often auto-format files

# Check what was changed
git diff

# Re-stage the auto-fixed files
git add src/nyxgpt/api.py

# Try committing again
git commit -m "Update code"
# ✅ All checks pass - commit succeeds!
```

#### NEVER Bypass Hooks (Unless Absolutely Necessary)

**Bad practice:**
```bash
git commit --no-verify -m "Quick fix"  # ❌ Skips ALL checks
```

**Why this is dangerous:**
- Broken code enters the repository
- CI/CD pipelines fail
- Other developers may pull broken code
- Wastes CI/CD resources
- Creates technical debt

**When bypassing might be acceptable:**
- Emergency hotfix (but still run checks manually afterward)
- You've manually verified all checks pass
- You understand the risks

**Better approach:**
```bash
# Run checks manually first
pre-commit run --all-files

# If checks pass, commit normally (hooks run again as confirmation)
git commit -m "Your message"
```

#### Checking Commit Status

After attempting a commit, verify it succeeded:

```bash
# Check if commit was created
git log -1

# Check if files are still staged (means commit failed)
git status

# If files are still staged, hooks blocked the commit
# Fix the issues and try again
```

### Pull Request Process

1. **Create a branch** from v2.0.0 or the active release branch
2. **Make changes** with clear, focused commits
3. **Run tests locally:** `pytest`
4. **Run pre-commit checks:** `pre-commit run --all-files`
5. **Push branch** to GitHub
6. **Create PR** targeting the release branch
7. **Wait for review** from maintainers
8. **Address feedback** if changes are requested
9. **Merge** once approved

---

## Documentation

Update documentation when:
- Adding new features
- Changing APIs or interfaces
- Modifying configuration options
- Adding new commands to the CLI

Documentation locations:
- **README.md** - Project overview and getting started
- **docs/** - Detailed guides and tutorials
- **Docstrings** - In-code documentation (follow Google style)

---

## Questions or Issues?

- **GitHub Issues:** [nyxGPT Issues](https://github.com/dkblinux98/nyxGPT/issues)
- **Documentation:** [README.md](README.md)

---

## Definition of Done

A contribution is complete when:
- ✅ Code is written and follows style guidelines
- ✅ Pre-commit hooks pass
- ✅ Tests are written and pass
- ✅ Documentation is updated
- ✅ CI/CD pipeline is green
- ✅ Code review is approved

---

Thank you for contributing to nyxGPT! 🚀
