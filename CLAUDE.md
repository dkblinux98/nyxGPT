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

```
1. SELECT ISSUE (from GitHub)
   ↓
2. CREATE BRANCH (from current release branch: v1.0.0)
   git checkout v1.0.0
   git pull origin v1.0.0
   git checkout -b [type]/42-add-session-export (where type is Feature or Fix and assume Fix if there is no label for Feature) 
   ↓
3. IMPLEMENT CODE (one file at a time)
   ↓
4. WRITE/UPDATE TESTS
   pytest -m unit
   ↓
5. UPDATE DOCUMENTATION
   ↓
6. COMMIT
   Example:
   git commit -m "feat(api): add session export endpoint

   Implements /api/v1/sessions/{name}/export endpoint that
   exports sessions in markdown, JSON, or HTML format.

   Resolves #42"
   ↓
7. CREATE PULL REQUEST
   gh pr create --base v1.0.0
   ↓
8. MERGE & CLEANUP
```

See @AGENTS.md for complete workflow details, branch naming conventions, PR templates, and failure recovery procedures.

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

```
<type>(<scope>): <subject>

<body>

Resolves #<issue-number>
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
