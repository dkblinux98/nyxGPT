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

### Current Limitations

- Full component testing for Next.js components requires additional runtime mocking (see issue #2776)
- Current tests focus on infrastructure verification and utilities
- Future improvements will add comprehensive component test coverage

See `web/tests/README.md` for detailed web UI testing documentation.

---

## Notes

- Integration tests will be skipped automatically if required services are not reachable.
- Use `pytest -v` for verbose output during development.
