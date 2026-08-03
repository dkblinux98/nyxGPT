# Testing

nyxGPT uses **pytest** with explicit markers to separate fast unit tests from slower integration tests.

## Test categories

### Unit tests (`@pytest.mark.unit`)

- Fast, deterministic tests
- No external dependencies
- No network access
- Use mocks, monkeypatching, and temporary files
- Live under `tests/unit/`

### Integration tests (`@pytest.mark.integration`)

- Require running services:
  - Ollama
  - Cassandra (Docker)
  - FastAPI backend
- Exercise real HTTP and database interactions
- Live under `tests/integration/`

## Running tests

Run **all tests** (default):

```bash
pytest
```

Run **unit tests only**:

```bash
pytest -m unit
```

Run **integration tests only**:

```bash
pytest -m integration
```

## Test logs

All test runs (unit and integration) write logs to:

```text
~/.nyxGPT/logs/tests.log
```

The log file is **truncated at the start of each pytest run**, so it always reflects the most recent execution.

This logging setup mirrors the application and CLI logging configuration to ensure consistency when debugging test failures.

## Writing new tests

- Add new unit tests under `tests/unit/` and mark them with `@pytest.mark.unit` (or use a file-level marker).
- Add new integration tests under `tests/integration/` and mark them with `@pytest.mark.integration`.
- Do not mix unit and integration behavior in the same test file.

## Web UI Testing (Next.js)

The web UI (`web/`) has its own test infrastructure using **Vitest**, **Happy-DOM**, and **React Testing Library**.

### Running Web UI Tests

```bash
cd web

# Run all tests
npm test

# Run tests with UI
npm run test:ui

# Run tests with coverage
npm run test:coverage
```

### Test Infrastructure

- **Vitest** - Modern, fast test runner with ESM support
- **Happy-DOM** - Lightweight DOM implementation
- **React Testing Library** - Component testing utilities
- **MSW** - API request mocking

### Test Files

- `web/tests/` - Test files and configuration
- `web/tests/setup.ts` - Global test setup
- `web/tests/mocks/handlers.ts` - MSW API mock handlers
- `web/tests/README.md` - Detailed web UI testing documentation

### Coverage Gate

- `web/vitest.config.ts` enforces a 100% coverage gate (statements/branches/functions/lines) via `test.coverage.thresholds`; `npx vitest run --coverage` fails the build on any regression.
- Both `.github/workflows/claude-code-review.yml` and `.github/workflows/validate-web-routes.yml` run tests with `--coverage` so the gate applies in CI.
- Component tests (React Testing Library + Happy-DOM) are part of the standard suite alongside infrastructure/utility tests.

See `web/tests/README.md` for detailed web UI testing documentation.

---

## Standalone CI gate (#3502)

`claude-code-review.yml` runs `pytest tests/unit/` and a non-blocking `mypy`
only as part of an agent-driven review — there is no independent workflow
that runs on every push/PR regardless of agent involvement, and what does run
never covers `tests/integration/`.

**Agent tokens cannot write `.github/workflows/*`** (same hand-carry pattern
as #3454/#3479/#3480's `docs/sprint-autopilot.md`), so the workflow below is
proposed here for the owner to apply by hand as a new file,
`.github/workflows/ci-tests.yml`:

```yaml
name: CI - Tests & Type Check

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    # Same push/PR scoping as terraform-local-smoke.yml: run on every PR, but
    # only on pushes that land on the release branch (not every feature branch).
    if: github.event_name == 'pull_request' || github.ref == format('refs/heads/{0}', vars.RELEASE_BRANCH)
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install nyxgpt (editable, dev extras)
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"

      - name: mypy src/ (blocking)
        run: mypy src/

      - name: pytest - full tests/ suite
        run: pytest -v
```

A plain `pytest -v` already covers the full `tests/` suite (`testpaths =
["tests"]` in `pyproject.toml`, no `-m` filter) — unit tests always run for
real; integration tests run for real wherever a required service is
reachable and skip gracefully otherwise (see Notes below), satisfying "unit +
integration where environment-feasible" without provisioning a service
container in this lightweight gate.

**Why no Cassandra/Ollama service containers here:** standing up Cassandra as
a GitHub Actions service container was evaluated directly (a fresh
`cassandra:latest` container, port-mapped to `localhost:9042`) against the 7
tests in `tests/unit/test_rag_update_detection.py` gated on
`cassandra_test_setup`. They still fail — not skip — against a *truly* fresh
Cassandra: `ingest_document`'s auto-bootstrap (`rag.py`'s `not ensure_schema
and not store.schema_exists()` check, meant to create the keyspace on a
collection's first ingest) never gets a chance to run, because
`get_collection_settings()` is called unconditionally first (`rag.py:962`,
to look up a per-collection embedding-model override) and that method calls
`ensure_settings_table()` (`vectorstore_cassandra.py:1389`), which touches
the keyspace before it exists and raises `Keyspace 'nyxgpt' doesn't exist`.
In every environment these tests currently pass in, something upstream
(`nyxgpt ops install`, a prior test run, a dev's live stack) already created
the keyspace — a genuinely fresh Cassandra hits this ordering bug. Fixing
`ingest_document`'s bootstrap ordering is out of scope for this CI-gate issue
(a RAG core-module fix, not a testing-infrastructure one) and is left as a
follow-up if the owner wants this class of integration test to run for real
in CI rather than skip. `tests/integration/*` needing a live Ollama and/or a
running `nyxgpt` API server (the large majority of the marked `integration`
tests) are further out of reach for a lightweight gate regardless — that
heavier, full-stack path is what `terraform-local-smoke.yml` already covers
end-to-end (scoped to `terraform/**` changes, not every push/PR).

**Verified green on the release branch (2026-08-03):** `black --check .`,
`ruff check src/ tests/`, `mypy src/`, and `pytest -v` all pass on `v3.0.0`
plus this change: `tests/unit/` alone is 3026 passed, 7 skipped, 0 failed;
with no live API server/Cassandra/Ollama reachable (the lightweight CI
runner's actual environment), `tests/integration/` is 17 passed, 192 skipped,
0 failed — the 17 are tests that only need the in-process `client`
TestClient, not a live server. Two fixes were needed to get there:

1. `tests/integration/conftest.py`'s `api_base_url` fixture returned its URL
   unconditionally, so ~206 tests that call a live server directly over HTTP
   (as opposed to the in-process `client` TestClient fixture) hard-failed
   with a connection error instead of skipping, in any environment without
   the full stack running — inconsistent with the graceful-skip behavior
   `require_ollama`/`require_cassandra`/`require_grafana` already implement
   in the same file and that this doc's Notes section (and the
   `tests/integration/README.md`) already claims. The fixture now does the
   same reachability check and `pytest.skip`s if unreachable.
2. That first fix introduced a regression caught in review: three
   `autouse=True`, session-scoped cleanup fixtures
   (`cleanup_test_rag_documents`, `cleanup_test_collections`,
   `cleanup_test_sessions`) depended on `api_base_url` purely to read its
   URL. Because pytest caches a `Skipped` exception raised by a
   session-scoped fixture and re-raises it for every other requester in the
   session, an unreachable server made `api_base_url`'s skip cascade to
   *every* test in `tests/integration/` (208 skipped, 0 passed) rather than
   only the tests that actually need a live server. Fixed by having those
   three fixtures resolve the URL through a plain, non-skipping
   `_resolve_api_base_url()` helper instead of depending on the `api_base_url`
   fixture — they already tolerate an unreachable server via their own
   try/except blocks, so they don't need the skip gate. Covered by
   `tests/integration/test_conftest_fixtures.py`, which asserts none of the
   three depend on `api_base_url`.

**Known flake, not fixed here:** `test_gpu_detection_shell_false`
(`tests/unit/test_embedding_optimization.py`) failed once in ~4 full-suite
runs during this verification, always passing in isolation and on rerun —
consistent with the module-global `_gpu_info`/`_gpu_info_updated` cache in
`nyxgpt/rag/embeddings.py` racing with a leftover background thread from an
earlier async embedding test via the shared thread pool. Pre-existing and
unrelated to this change; flagged here per this issue's "explicitly
skip-with-comment anything red" allowance rather than fixed, since resolving
the race is a concurrency fix in `embeddings.py`, not CI-gate scope.

## Notes

- Integration tests will be skipped automatically if required services are not reachable.
- Use `pytest -v` for verbose output during development.
