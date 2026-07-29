# Developer Runbook (developer-agent)

This is the procedural “how” for implementing issues. Authority is defined in the charter.

## 0) Preconditions
- Repo clean, on correct base (release branch or per project rule)
- Up to date with remote
- Services healthy (if applicable)
- Tests passing before starting

## 1) Pick up work
- Ensure issue is assigned to developer-agent and status is In Progress.
- Confirm Phase/Sprint fields are set.

## 2) Branching
- Create a short-lived branch named with issue reference, e.g.:
  - `feat/<issue-id>-<slug>` or `fix/<issue-id>-<slug>`
- Base off the current active release branch.

## 3) Implement
- Make smallest coherent change set that satisfies acceptance criteria.
- Add/extend tests (unit/integration as appropriate).
- Keep IO behind interfaces; maintain dependency flow.

## 3a) Instrumentation conventions

RCA, self-heal, and SRE work are only as good as the logging behind them
(#3415). Apply these conventions to any new code, not just the file you're
touching:

- **No silent excepts.** Every `except Exception:` (or narrower) that
  swallows an error and substitutes a default must log at WARNING (or ERROR
  if the caller can't recover) what failed and what default was used —
  `logger.warning("Invalid <key> in config, using default <value>: %s", e)`
  is the pattern used throughout `config.py`'s getters
  (`nyxgpt.config._log_fallback_once`, which also dedupes a given key's
  warning to once per process — reuse it for new config getters instead of
  a bare `except Exception: return default`).
- **Subprocess failures must log cmd + rc + stderr tail.** Any helper that
  wraps `subprocess.run` (see `ops.py`/`canary.py`/`self_heal.py`'s `_run`)
  must log at WARNING on non-zero exit, in addition to whatever the
  `CompletedProcess`/exception carries back to the caller — the caller's
  handling of the result must never be the only path to visibility.
- **New request paths get lifecycle records.** A request-shaped code path
  (chat turn, RAG query/ingest, an API endpoint doing real work) emits a
  start record and a completion-or-failure record at INFO, carrying
  `session`/`model` where applicable, an `outcome`, and a `duration_ms` —
  see `chat.py`'s `chat()`/`chat_stream()` and `rag.py`'s
  `retrieve_context()`/`ingest_document()` for the pattern.
- **Trace-context propagation for new outbound calls.** New code that calls
  Ollama, another internal service, or shells out should carry the current
  request/trace correlation id so cause and effect can be joined across
  logs (see `tracing.traced_span`/`traced` for the current span helpers;
  full W3C `traceparent` propagation across the web/API/Ollama boundary is
  tracked under #3415 gap 6).
- **Use the formatter's extras, not string interpolation, for structured
  fields.** Pass `component` (subsystem name, e.g. `"ops"`, `"rag"`,
  `"chat"`, `"ollama"`), and `session`/`model` where known, via
  `extra={...}` — `StructuredFormatter` (`logging.py`) serializes them as
  first-class JSON fields when `[logging] format = json`, instead of being
  buried in a formatted message string.
- **Exclude new polling endpoints from access-log noise.** A new
  `/health`-, `/metrics`-, or dashboard-polling-style endpoint hit on a
  fixed interval belongs in `logging.py`'s
  `_ACCESS_LOG_EXCLUDED_PATH_PREFIXES`, or it will fill the rotating file
  handler with near-zero-signal INFO lines on every poll.

## 4) Verification loop (MANDATORY - ALL must pass before commit)
Run ALL of the following checks and fix issues until they pass:
- `black --check .` - If fails, run `black .` to auto-format, then re-check
- `ruff check src/ tests/` - Fix ALL linting errors (0 errors required)
- `mypy src/` - Fix ALL type errors (0 errors required)
- `pytest -v` - ALL tests MUST pass (0 failures, 0 errors)
- `./scripts/agents/validate-web-routes.sh` - If you modified web routes or API endpoints

**CRITICAL**: Keep working until all checks pass (like a human developer would).
Pre-commit hooks MUST pass before commit succeeds.

If flaky tests appear, isolate and fix; escalate if persistent.

## 5) Documentation
- Update docs for any user-facing change:
  - Modified `src/nyxgpt/api.py` or `src/nyxgpt/app.py` → Update `docs/api.md`
  - Modified `src/nyxgpt/cli.py` → Update `docs/configuration.md` or `README.md` CLI section
  - Added/changed config options → Update `example.config.ini` AND `docs/configuration.md`
  - Added new features → Update `README.md` feature list
- Update architecture notes only if human-approved architecture change is required

## 6) Commit discipline
- Small commits with clear messages
- Reference issue in PR body: "Closes #ISSUE"
- Commit message format: `<type>: <description> (#ISSUE)`
- Valid types: feat, fix, test, docs, refactor, chore
- Only commit after ALL validation checks pass

## 7) Open PR
- Target: active release branch
- PR body MUST include: "Closes #ISSUE"
- Ensure CI runs (should pass since pre-commit hooks passed)
- Update issue status -> In Review
- Assign review-agent as PR reviewer (not just assignee)

## 8) Address REQUEST_CHANGES review
- If review-agent posts REQUEST_CHANGES review:
  - Issue automatically reassigned to you with Status -> In Progress
  - Read the full review comment
  - Fix ALL Critical and Medium issues listed
  - Run ALL validation checks again (full suite from step 4)
  - Commit and push fixes (triggers automatic re-review)
  - Repeat until APPROVE or 3rd REQUEST_CHANGES (escalates to human)
