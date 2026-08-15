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
   `tests/unit/test_integration_conftest_fixtures.py`, which asserts none of
   the three depend on `api_base_url`.
3. That regression test originally lived in `tests/integration/` and was
   therefore subject to the same package-level `autouse=True` fixtures it
   guards: with the bug reintroduced and no live server reachable, the skip
   cascade hit the regression test too, so it reported SKIPPED instead of
   FAILED and its assertions never ran — useless in exactly the no-live-server
   environment this CI gate targets. It now lives in `tests/unit/` and loads
   `tests/integration/conftest.py` by explicit file path via
   `importlib.util.spec_from_file_location`, so it is never collected as part
   of the integration package and cannot be skip-cascaded by the fixtures
   under test. **Any future regression test for integration-fixture wiring
   must follow the same pattern** — a test that can be skipped by the bug it
   is testing provides no coverage.

**Known flake, not fixed here:** `test_gpu_detection_shell_false`
(`tests/unit/test_embedding_optimization.py`) failed once in ~4 full-suite
runs during this verification, always passing in isolation and on rerun —
consistent with the module-global `_gpu_info`/`_gpu_info_updated` cache in
`nyxgpt/rag/embeddings.py` racing with a leftover background thread from an
earlier async embedding test via the shared thread pool. Pre-existing and
unrelated to this change; flagged here per this issue's "explicitly
skip-with-comment anything red" allowance rather than fixed, since resolving
the race is a concurrency fix in `embeddings.py`, not CI-gate scope.

## Executed verification: smoke jobs on real targets (#3775)

Unit tests prove logic; they cannot prove what happens when nyxGPT is
*installed and run* on a machine. A mocked `systemctl` says nothing about a
systemd unit, and reading a Homebrew formula says nothing about whether its
venv bootstraps on stock Homebrew. Every defect in the #3753 / #3759 / #3761
family was invisible to the test suite and obvious on first execution.

So, by owner requirement
([Definition of Done](../CLAUDE.md#definition-of-done-owner-requirement-2026-07-08)),
**a change whose claim is about runtime, install or platform behavior must be
demonstrated by executing it on the target platform**, with the run cited in
the PR. The review agent blocks on a missing run
([review runbook §1c](../agents/runbooks/review-runbook.md)); the developer
agent produces it
([developer runbook §4a](../agents/runbooks/developer-runbook.md)).

### The smoke jobs that count as evidence

| Workflow | Runs on | What it proves | Triggers |
| --- | --- | --- | --- |
| [`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml) | `macos-15` | The working tree's Homebrew formulas install into a real keg, and the published tap installs what the owner actually types | PR (paths: `homebrew/**`, artifact/tarball scripts), dispatch, called after an rc cut |
| [`linux-native-smoke.yml`](../.github/workflows/linux-native-smoke.yml) | `ubuntu-latest` | `nyxgpt ops install` on a real systemd userland: every unit active, api/web responding, diagnostics, `nyxgpt ops down`. **Scope boundary:** it installs *editable from the checkout* on a machine whose Python and Node were installed by setup actions, so it cannot see first-boot/artifact-install defects — that is `cloud-artifact-smoke.yml`'s job | PR/push (paths: `src/nyxgpt/ops.py`, `self_heal.py`, `ops/systemd/**`, the smoke script) |
| [`cloud-artifact-smoke.yml`](../.github/workflows/cloud-artifact-smoke.yml) | `ubuntu-latest`, inside a bare `amazonlinux:2023` container | The **artifact** install path on the real target distro: AL2023's Python 3.9, no node, no docker, no git, the real rendered EC2 user-data bootstrap, then api/web/ollama serving. Injects `old-python` in a second job so a green run is not green by luck ([cloud-artifact-smoke.md](cloud-artifact-smoke.md)) | PR/push (paths: `src/nyxgpt/ops.py`, `cloud_provision.py`, `cloud_artifact_smoke.py`, `scripts/cloud/**`), dispatch (pin an rc) |
| [`terraform-local-smoke.yml`](../.github/workflows/terraform-local-smoke.yml) | `ubuntu-latest` | The Terraform path applies locally end-to-end | PR/push (paths: `terraform/**`) |
| `nyxgpt ops verify` (see [live verification in CI](live-verification-ci.md)) | review job | The Compose stack boots, chat/RAG traffic lands in Prometheus, every touched Grafana panel's own query re-executes, screenshots captured | Run by the review agent on observability/metrics/UI PRs |

A run of the wrong platform is not evidence: a green Linux job says nothing
about a macOS keg. If no job covers the changed path, the PR adds one — copy
the shape of the table's entries (path-filtered triggers so cost lands only on
PRs that can break it, `permissions: contents: read` for a read-only smoke,
and a header comment stating which question the job answers).

### Injecting the failure, not hoping for it

A job that only runs the install is green on every machine that fails to
reproduce the bug. That is how the rc5 candidate passed CI and died on the
owner's Mac: the runner answered `platform.mac_ver()` and their machine did
not. `macos-brew-smoke.yml`'s "Reproduce the empty `mac_ver()` failure, then
prove the shim fixes it" step is the pattern to copy — assert the failure
happens **without** the fix, so a green result **with** it means something.

### What still cannot be executed in CI

The short list in [live verification in CI](live-verification-ci.md) — the
native launchd/brew-services *operate* path, real Slack delivery, and LLM
answer *quality* — plus EC2 Mac hardware, which has no hosted runner (see the
[portability matrix](portability-matrix.md)). Those are named explicitly in
the PR and exercised by the owner during acceptance testing; everything else
gets run. Note what is *not* on the list: the Homebrew keg install runs on a
real `macos-15` runner, so a formula change cannot claim macOS is untestable.

## Notes

- Integration tests will be skipped automatically if required services are not reachable.
- Use `pytest -v` for verbose output during development.
