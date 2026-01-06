# myGPT Project Memory

This file provides quick reference information for Claude Code sessions. For comprehensive workflows and procedures, see @AGENTS.md.

## Project Overview

**myGPT** - Personal ChatGPT-like application powered by local Ollama models with optional RAG (Retrieval-Augmented Generation) capabilities.

- **Language**: Python 3.13+ (backend), TypeScript/React (web UI)
- **Framework**: FastAPI (API), Next.js (web UI)
- **Database**: Cassandra (vector storage for RAG)
- **LLM Backend**: Ollama (local models)
- **Current Release**: v1.0.0

## Working Style Preferences

**Autonomous Operation**: Work autonomously without constantly asking for approval. When given a task:
- Execute all steps without stopping for confirmation
- Don't use AskUserQuestion for routine decisions that follow established patterns
- Apply judgment based on documented guidelines (CLAUDE.md, AGENTS.md)
- Only ask for clarification when genuinely ambiguous or high-risk

**When to ask vs. proceed autonomously:**
- ✅ **Proceed autonomously**: Running tests, creating branches, committing code, creating PRs, applying documented patterns, fixing obvious bugs, following established workflows
- ❌ **Ask first**: Destructive actions (deleting branches, force pushing to protected branches), architectural decisions with multiple valid approaches, unclear requirements, breaking changes

**Default assumption**: If a choice would reasonably follow documented conventions and safety rules, proceed with the most sensible option. The user trusts you to make good decisions within established guidelines.

## Quick Commands

```bash
# Health check (run before starting work)
mygpt ops doctor

# Install/start services (Ollama, Cassandra, web UI)
mygpt ops install

# Testing
pytest -m unit              # Unit tests (fast, must pass before commits)
pytest -m integration       # Integration tests (requires services)
pytest --cov=src/mygpt      # Coverage report

# Linting
ruff check src/             # Python linter
mypy src/                   # Python type checker
cd web && npm run lint      # TypeScript/React linter

# CLI usage
mygpt chat "Hello"          # Single chat
mygpt tui                   # Terminal UI
mygpt sessions list         # List sessions

# Web UI
mygpt ops restart web      # Start dev server (http://localhost:3000)
```

## Code Style

### Python
- **Indentation**: 4 spaces
- **Line length**: 100 characters max
- **Type hints**: Required for all function signatures (except self, cls)
- **Docstrings**: Google style for all public functions/classes
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes

### TypeScript/React
- **Indentation**: 2 spaces
- **Line length**: 100 characters max
- **Components**: `PascalCase`, one per file
- **Functions/variables**: `camelCase`
- **Styling**: Tailwind classes (no inline styles)

## Important Locations

### Configuration
- **User config**: `~/.myGPT/config.ini` (runtime config, never commit)
- **Example config**: `example.config.ini` (template, version controlled)
- **Docs**: `docs/configuration.md`

### Runtime Data (Never Commit)
- **Sessions**: `~/.myGPT/sessions/`
- **Logs**: `~/.myGPT/logs/`
- **Vectorstore**: `cassandra database at localhost:9042 using databse mygpt and table rag_chunks`

### Source Code
- **Core Python**: `src/mygpt/`
- **Web UI**: `web/`
- **Tests**: `tests/unit/` (fast) and `tests/integration/` (requires services)
- **Docs**: `docs/`

## Critical Safety Rules

**NEVER:**
1. Force push to `master` or `v*.*.*` branches
2. Commit secrets, API keys, or runtime data (`~/.myGPT/`)
3. Merge PRs with failing tests
4. Delete protected resources (see @AGENTS.md)
5. Skip unit tests before commits

**ALWAYS:**
1. Run `pytest -m unit` before committing
2. Create feature branches: `{type}/{issue-number}-{description}`
3. Follow commit format: `<type>(<scope>): <subject>`
4. Update documentation when changing APIs or configs
5. Create tests for new code and bug fixes

## Development Workflow

### Complete Autonomous Issue Workflow

When working on an issue autonomously from start to finish:

```
1. SELECT ISSUE
   - Choose from current phase milestone
   - Prefer lower-numbered issues first
   ↓
2. UPDATE STATUS → "In Progress"
   - Update GitHub Project status field
   - Use GraphQL API to update project item
   ↓
3. CREATE ISSUE-LINKED BRANCH
   - Branch from current release (v1.0.0)
   - Use GitHub's issue branch creation (shows in issue UI)
   - Format: [type]/[issue-number]-[description]
   - Types: fix/ or feat/ (assume fix/ if no Feature label)
   - Example: fix/2597-increase-test-coverage
   ↓
4. IMPLEMENT CODE
   - Work one file at a time
   - Follow code style guidelines
   - Add type hints and docstrings
   ↓
5. WRITE/UPDATE TESTS
   - Add tests for new code
   - Update existing tests if needed
   - Ensure 80%+ coverage for new code
   - Run: pytest -m unit
   ↓
6. UPDATE DOCUMENTATION
   - Update relevant docs (README, API docs, etc.)
   - Add/update docstrings
   - Update example.config.ini if needed
   ↓
7. RUN ALL CHECKS
   - pytest -m unit (MUST pass)
   - ruff check src/
   - mypy src/
   - Coverage check: pytest --cov=src/mygpt
   ↓
8. COMMIT & PUSH
   - Use conventional commit format
   - MUST include "Resolves #[parent-issue-number]" in footer
   - If fixing sub-issues, use "Fixes #[sub-issue] / Contributes to #[parent]"
   - Push to remote branch
   ↓
9. UPDATE STATUS → "Review"
   - Update GitHub Project status
   ↓
10. SELF-REVIEW
    - Check ALL acceptance criteria met
    - Verify tests pass
    - Verify documentation updated
    - Check code quality
    ↓
11a. IF REVIEW FAILS (Critical/Medium Issues Found):
     - Create SEPARATE sub-issues for EACH critical/medium issue
     - Link each sub-issue to parent
     - Update parent status → "In Progress"
     - Update each sub-issue status → "In Progress"
     - Fix each issue (commits reference both sub-issue AND parent)
     - Re-review until NO critical/medium issues remain
     - Repeat from step 4
     ↓
11b. IF REVIEW PASSES (No Critical/Medium Issues):
     - Minor issues can be left unaddressed
     - Create PR to v1.0.0
     - Add PR to project "myGPT" with fields matching parent issue
     - Update PR status → "In Review"
     - Wait for CI checks to pass
     - Merge PR (squash and merge)
     - Update parent issue status → "For Release"
     - Update all sub-issue statuses → "For Release"
     - Update PR status → "For Release"
     - Delete feature branch
     - Verify issue auto-closed (via "Resolves #XXX")
     - GitHub Actions will auto-check in release tracking issue
```

### GitHub Project Status Flow

Issues move through these statuses:
- **Backlog** → Initial state for new issues
- **In Progress** → Actively being worked on
- **Review** → Code complete, undergoing review
- **For Release** → Passed review, ready to merge
- **Done** → Merged and closed (automatic when issue closes)

### Branch Creation (Issue-Linked)

To create a branch that shows in the issue's "Development" section:

```bash
# Option 1: Using gh CLI with issue link (preferred)
gh issue develop [issue-number] --checkout --base v1.0.0

# Option 2: Create branch then link via GitHub API
git checkout v1.0.0
git pull origin v1.0.0
git checkout -b fix/2597-description
git push -u origin fix/2597-description
# Link is created automatically when PR references the issue
```

### Review Criteria Checklist

Before marking review as passed, verify:
- [ ] All acceptance criteria from issue met
- [ ] Unit tests pass (`pytest -m unit`)
- [ ] Code coverage ≥80% for new code, ≥25% overall
- [ ] Linters pass (ruff, mypy)
- [ ] Documentation updated
- [ ] No security vulnerabilities introduced
- [ ] Code follows style guidelines
- [ ] Commit messages follow format
- [ ] No runtime data or secrets committed

### Sub-Issue Creation (Review Failures)

**CRITICAL**: When code review finds critical or medium issues, create a SEPARATE sub-issue for EACH issue (not one sub-issue for all).

**Example**: If review finds 2 critical issues, create 2 sub-issues.

**IMPORTANT - Label Usage**:
- Use **"Acceptance Failure"** label (NOT "bug") for issues found during review/acceptance testing
- "bug" label is ONLY for issues found in production/released code
- Issues found before code reaches production are acceptance failures, not bugs

```bash
# 1. Create sub-issue for EACH critical/medium review finding
gh issue create \
  --title "[Brief description of specific issue]" \
  --body "Parent issue: #[parent-number]

## Issue Description
[Specific review finding - e.g., 'Unused cleanup_interval_seconds config parameter']

## Required Fix
[What needs to be done]

## Acceptance Criteria
- [ ] [Specific fix completed]
- [ ] Tests pass
- [ ] No new issues introduced" \
  --milestone "[Same milestone as parent]" \
  --label "Acceptance Failure" \
  --assignee "@me"

# 2. Add sub-issue to project "myGPT" with IDENTICAL fields as parent
# Get parent issue's project fields first
PARENT_NUM=2624
SUB_ISSUE_NUM=2625  # From previous command output

# Get parent's project field values
gh api graphql -f query='
  query {
    repository(owner: "dkblinux98", name: "myGPT") {
      issue(number: '$PARENT_NUM') {
        projectItems(first: 5) {
          nodes {
            project { number title }
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field { ... on ProjectV2SingleSelectField { name } }
                  name
                }
              }
            }
          }
        }
      }
    }
  }
'

# Add sub-issue to project (copying ALL field values from parent)
gh project item-add 2 --owner dkblinux98 --url "https://github.com/dkblinux98/myGPT/issues/$SUB_ISSUE_NUM"

# Set Status to "In Progress" (same as parent when creating sub-issue)
# Set Phase, Priority, etc. to match parent exactly
# Use GraphQL mutation to update fields (see GitHub Bulk Operations section)

# 3. Link to parent by editing parent issue body
# Add each sub-issue to parent's task list:
# - [ ] #[sub-issue-number-1] - [Brief description]
# - [ ] #[sub-issue-number-2] - [Brief description]

# 4. When sub-issue is resolved (fixed or closed as not planned):
#    - Update sub-issue status to "For Release" (NOT "Closed")
#    - This tracks it through the release cycle
```

**Minor/nitpick issues**: Do NOT create sub-issues. These can be left unaddressed.

**Status for Acceptance Failure Issues**:
- When acceptance failure sub-issues are resolved (fixed or closed), set status to **"For Release"**
- Do NOT use "Closed" status for acceptance failures
- "For Release" status tracks them through the release cycle
- This applies even if the issue is closed as "not planned"

**Field inheritance from parent**:
- ✅ Milestone (same as parent)
- ✅ Labels (copy from parent, add "Acceptance Failure" - NOT "bug")
- ✅ Assignee (same as parent)
- ✅ Project (myGPT)
- ✅ Status (In Progress when created, "For Release" when resolved)
- ✅ Phase (same as parent)
- ✅ Priority (same as parent)
- ✅ Any other custom fields (match parent)

### Pull Request Creation

**CRITICAL**: PRs must also be added to project "myGPT" with fields matching the parent issue.

```bash
# 1. Create PR
gh pr create --base v1.0.0 \
  --title "[type]([scope]): [description] (#[parent-issue])" \
  --body "[PR description with issue reference]"

# PR is created with URL like: https://github.com/dkblinux98/myGPT/pull/2720

# 2. Add PR to project "myGPT"
gh project item-add 2 --owner dkblinux98 --url "https://github.com/dkblinux98/myGPT/pull/2720"

# 3. Set PR project fields to match parent issue
# Use same Status, Phase, Priority, etc. as the parent issue
# Get parent issue fields:
gh api graphql -f query='
  query {
    repository(owner: "dkblinux98", name: "myGPT") {
      issue(number: [parent-issue-num]) {
        projectItems(first: 5) {
          nodes {
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field { ... on ProjectV2SingleSelectField { name id } }
                  name
                  optionId
                }
              }
            }
          }
        }
      }
    }
  }
'

# Then update PR's project fields to match
# (See GitHub Bulk Operations section for GraphQL mutation examples)
```

**PR field inheritance from parent issue**:
- ✅ Project (myGPT)
- ✅ Status (In Review when PR created)
- ✅ Phase (same as parent issue)
- ✅ Priority (same as parent issue)
- ✅ Milestone (linked via issue reference)
- ✅ Any other custom fields (match parent issue)

See @AGENTS.md for complete workflow details, branch naming conventions, PR templates, and failure recovery procedures.

## GitHub Bulk Operations

When performing bulk operations on GitHub issues/projects (updating 50+ items):

**CRITICAL: Error Handling & Rate Limiting**

```bash
# ❌ BAD: Suppresses errors, no rate limiting
for issue in {1..100}; do
  gh project item-edit --id "$id" --field-id "$field" --option-id "$option" 2>/dev/null
done

# ✅ GOOD: Shows errors, handles rate limits, verifies success
count=0
for issue in {1..100}; do
  if gh project item-edit --id "$id" --field-id "$field" --option-id "$option"; then
    echo "✓ Updated issue #$issue"
  else
    echo "✗ Failed issue #$issue - check rate limits"
  fi

  # Pause every 10 items to avoid rate limiting
  ((count++))
  if [ $((count % 10)) -eq 0 ]; then
    sleep 2
  fi
done
```

**Best Practices:**

1. **Never suppress errors** - Remove `2>/dev/null` to see API failures
2. **Add delays** - Sleep 1-2 seconds every 10-20 items to avoid rate limits
3. **Wait for sync** - After `gh project item-add`, wait 3-5 seconds before editing
4. **Verify updates** - Check command exit codes and log failures
5. **Batch appropriately** - Update 20-30 items, verify, then continue
6. **Log everything** - Keep a record of what succeeded/failed for manual cleanup

**Common Failure Modes:**
- GitHub API rate limiting (429/403 errors)
- Project items not yet synced after adding
- Network timeouts on large batches
- GraphQL query complexity limits

**Recovery:**
- Check which items failed: `gh issue list --json number,title | jq '...'`
- Manually update failed items or rerun with smaller batches
- Use `gh api rate_limit` to check remaining API quota

## Current Context

**Branch**: v1.0.0
**Modified files** (uncommitted):
- `.gitignore`
- `docs/configuration.md`
- `example.config.ini`
- `src/mygpt/app.py`
- `src/mygpt/config.py`
- `src/mygpt/logging.py`
- `web/src/app/api/chat/stream/route.ts`

**Recent work**:
- Established `src/mygpt/logging.py` as single source of truth for logging configuration
- Fixed test failures in RAG and sessions tests
- Created comprehensive GitHub issue tracking for product roadmap
- Created milestone issues for historical work

## Testing Requirements

**Before any commit:**
- Unit tests MUST pass: `pytest -m unit`
- No decrease in test coverage
- Linters pass (ruff, mypy, eslint)

**Test organization:**
- `@pytest.mark.unit` - Fast tests, no external dependencies
- `@pytest.mark.integration` - Requires Ollama, Cassandra, or API

**Minimum coverage**:
- New code: 80%+
- Project overall: ≥25%

## Commit Message Format

**CRITICAL**: ALL commits related to an issue MUST reference the parent issue number.

### Initial Implementation Commit
```
<type>(<scope>): <subject>

<body>

Resolves #<parent-issue-number>
```

### Sub-Issue Fix Commits
When fixing issues found during code review:
```
<type>(<scope>): <subject>

<body describing the specific fix>

Fixes #<sub-issue-number>
Contributes to #<parent-issue-number>
```

**Example**:
```
fix(api): remove unused cleanup_interval_seconds config

Removed cleanup_interval_seconds from:
- src/mygpt/config.py (get_rate_limit_config)
- example.config.ini

Cleanup intervals are hardcoded in RateLimiter class.

Fixes #2625
Contributes to #2624
```

**Types**: `feat`, `fix`, `refactor`, `test`, `docs`, `style`, `perf`, `chore`
**Scopes**: `api`, `cli`, `web`, `tui`, `rag`, `chat`, `sessions`, `config`, `ops`

## Documentation Updates

Update when changing:
- **README.md** - CLI commands, installation, major features
- **docs/api.md** - API endpoints, request/response models
- **docs/configuration.md** - Config sections or keys
- **example.config.ini** - Config template
- **Inline docs** - Function docstrings, complex logic comments
- **Other docs in docs/** - As needed

## Key Architecture Patterns

### Logging
- **Single source of truth**: `src/mygpt/logging.py`
- **Config section**: `[logging]` in config.ini
- **Hot-reloadable**: Log level changes apply without restart
- All components use centralized logging

### Configuration
- **User config**: `~/.myGPT/config.ini` (not version controlled)
- **Template**: `example.config.ini` (version controlled)
- **Hot-reloadable settings**: `default_model`, `logging.level`, `rag.enabled`
- Path expansion: All paths support `~` expansion

### RAG System
- **Vector DB**: Cassandra with vector search
- **Embeddings**: Ollama (nomic-embed-text, 768 dimensions)
- **Retrieval**: Top-K similarity search with score filtering
- **Integration**: Auto-injects context into chat prompts when enabled

### Session Management
- **Storage**: JSON files in `~/.myGPT/sessions/`
- **Atomic writes**: UUID-based temp files to prevent race conditions
- **Structure**: Messages + metadata separate files

## Reference Documentation

- **Complete workflow**: @AGENTS.md
- **API documentation**: `docs/api.md`
- **Architecture**: `docs/architecture.md`
- **Configuration**: `docs/configuration.md`
- **Product roadmap**: `product_management/PRODUCT_ROADMAP.md`

## Notes

- This is a single-user local application (not designed for multi-user/production)
- All LLM inference happens via Ollama (local, private)
- Authentication is optional and disabled by default
- Session data never leaves the local machine unless explicitly exported
