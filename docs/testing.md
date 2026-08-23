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

## What a pytest run may touch

**Nothing of yours.** The suite gives itself a private `$HOME` in a fresh temp
directory before any `nyxgpt` module is imported (`tests/home_sandbox.py`,
#4020), seeded with its own `config.ini`. Your real `~/.nyxGPT` — config,
secrets, sessions, install-mode markers, Terraform state, logs — is not
reachable from a test, so there is nothing to back up before running and
nothing to repair if a run is interrupted.

Until #4020 this was not true: the suite wrote its config into the real
`~/.nyxGPT/config.ini` and restored it at teardown, which two concurrent pytest
sessions could and did destroy. Do not reintroduce anything shaped like that.

`docs/unit-suite-expected-failures.md` records what `pytest tests/unit` is
expected to produce, and the one environment-dependent failure that is not
yours.

## Test logs

Each run's logs go to a session-scoped temp directory (`tests.log` inside it),
never to the production log directory — `_isolate_test_log_dir` in
`tests/conftest.py` redirects `[logging] dir` there and then *asserts* at
teardown that the real `~/.nyxGPT/logs` gained nothing (#3443: a local run's
synthetic ERROR records once reached Loki and derailed an incident
investigation). The path is printed by pytest as the session temp dir; the file
is truncated at the start of each run.

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

> **Status: shipped, and this section is history.**
> `.github/workflows/ci-tests.yml` **exists** — read that file, not the YAML
> quoted below, which is the original 2026-08-03 proposal and has since
> diverged (suite selection by diff, the web gates, and the `determinism` job
> added by #4020 are all in the real file and none of them are here).
> The claim below that **agent tokens cannot write `.github/workflows/*`** is
> also wrong and was retired on 2026-08-07: `DEVELOPER_AGENT_TOKEN` carries the
> `workflow` scope and agents push workflow files routinely (see `CLAUDE.md`,
> "Tooling"). The rationale in the rest of the section is still worth keeping;
> the instructions are not.

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
| [`linux-native-smoke.yml`](../.github/workflows/linux-native-smoke.yml) | `ubuntu-latest` | Questions about the Linux ops path, one job each: `nyxgpt ops install` on a real systemd userland (every unit active, api/web responding, diagnostics, `nyxgpt ops down`); a failed ops subprocess reporting its own output; a service venv built on a Python that satisfies `requires-python` even when `python3` on PATH is a real 3.9; the observability stack coming up on a plain Linux engine; the host relay bridging container -> host loopback; an unqueryable Compose probe reporting unknown rather than absent (#3812); and a crash-looping container failing the observability step with its own last log line rather than being reported up (#3993, `observability-settle`). **Scope boundary:** it installs *editable from the checkout* on a machine whose Python and Node were installed by setup actions, so it cannot see first-boot/artifact-install defects — that is `cloud-artifact-smoke.yml`'s job | PR/push (paths: `src/nyxgpt/ops.py`, `self_heal.py`, `ops/systemd/**`, the smoke scripts) |
| [`cloud-artifact-smoke.yml`](../.github/workflows/cloud-artifact-smoke.yml) | `ubuntu-latest`, inside a bare `amazonlinux:2023` container | The **artifact** install path on the real target distro: AL2023's Python 3.9, no node, no docker, no git, the real rendered EC2 user-data bootstrap, then api/web/ollama serving. Injects `old-python` in a second job so a green run is not green by luck ([cloud-artifact-smoke.md](cloud-artifact-smoke.md)) | PR/push (paths: `src/nyxgpt/ops.py`, `cloud_provision.py`, `cloud_artifact_smoke.py`, `scripts/cloud/**`), dispatch (pin an rc) |
| [`terraform-local-smoke.yml`](../.github/workflows/terraform-local-smoke.yml) | `ubuntu-latest` | The Terraform path applies locally end-to-end | PR/push (paths: `terraform/**`) |
| [`k8s-local-smoke.yml`](../.github/workflows/k8s-local-smoke.yml) | `ubuntu-latest` | That the **default** `nyxgpt ops install --kubernetes` produces a stack a user can chat with, on a node ballasted down to the 7936Mi a stock 8GiB Docker Desktop VM offers: every Pod scheduled and the observability layer present (#3825), Cassandra and Ollama Ready, a real chat round-trip through the web Service's own proxy routes, and the same chat failing once the data/LLM tier is deleted (#3786) | PR/push (paths: `k8s/**`, `src/nyxgpt/ops.py`, `Dockerfile`, the smoke script), dispatch |
| [`k8s-capacity-smoke.yml`](../.github/workflows/k8s-capacity-smoke.yml) | `ubuntu-latest` | That the stack's memory **and cpu** requests fit that same node -- the arithmetic no manifest review catches -- and that an operator whose node is too small is told so before it costs them anything. Four phases: the real `nyxgpt ops install --kubernetes`, run against a node deliberately ballasted below the shipped stack, must **refuse before it builds anything** — non-zero exit, `nyxgpt-api:local` absent from the daemon, no `nyxgpt` namespace (#3825 acceptance round; the preflight sits ahead of the two ~20-minute image builds, and this step fails on the previous order, which is its injected half); then the pre-#3825 memory sizing must leave Pods unschedulable and the install's capacity preflight must refuse it; the pre-fix cpu sizing with the memory fixed must strand the canary Pod on `Insufficient cpu` (right-sizing only the resource the issue named moved the wall rather than removing it); and the shipped sizing must schedule every Pod (prometheus included) with a whole rollout's borrowed pool — `[canary] total_replicas` Pods per track, not one — landing on top. Both sizing injections restore the pre-#3833 standing pool alongside the old requests (`scripts/k8s-inject-pre-fix-sizing.sh`, checked for having actually matched): against the elastic pool the repo ships now, the requests alone fit the node and the gate proves nothing. No image is built by a green run — the sizing phases decide scheduling from the Pod spec before any pull, and the refusal phase asserts the *real* install stopped short of its builds | PR/push (paths: `k8s/**`, `src/nyxgpt/ops.py`), dispatch |
| [`canary-rollout-smoke.yml`](../.github/workflows/canary-rollout-smoke.yml) | `ubuntu-latest` | That a canary rollout borrows replicas and gives them back, on a real kind cluster running the shipped manifests: stable rests at 1, `nyxgpt canary start --weight 25` grows the pool to 4 (read back out of the Service's own EndpointSlices, not out of our arithmetic), `promote` re-plans it down to the 2 replicas 50% needs, `rollback` deflates it, and an operator-scaled stable comes back to the count *they* set (#3833). A second job re-creates the pre-#3833 standing pool and requires the smoke to reject it; a final in-script phase applies the old `stable = 0` split and asserts the Service is left serving canary Pods only. Runs a 2.7MB stand-in image — replica arithmetic does not depend on the app image, and the full-stack install is `k8s-local-smoke.yml`'s job | PR/push (paths: `src/nyxgpt/canary.py`, the stable/canary manifests and their Services, the smoke script), dispatch |
| [`cloud-imds-smoke.yml`](../.github/workflows/cloud-imds-smoke.yml) | `ubuntu-latest` | That the AWS substrate panel reports the instance it is *running on* rather than a Terraform state file that lives elsewhere (#3804). Aliases `169.254.169.254` onto `lo` and serves a token-gated IMDSv2 stand-in, then asserts three phases in order: unknown before the alias exists, the real instance facts (id, type, region, IPs, VPC, subnet, security group, key pair) with IMDSv2 up, and unknown again when only IMDSv1 is offered — so neither half can pass by luck | PR/push (paths: `src/nyxgpt/cloud_imds.py`, `cloud_infra.py`, `cloud_deploy.py`, the smoke script), dispatch |
| [`cli-locality-smoke.yml`](../.github/workflows/cli-locality-smoke.yml) | `ubuntu-latest` | That the shipped `nyxgpt` — built as a wheel, installed into a venv with no checkout in reach — accepts `ops install --terraform/--kubernetes` with **no locality flag**, and that `ops install -h` describes that truthfully (#3948): no "requires --local" clause, and a pointer at `nyxgpt cloud infra apply` / `nyxgpt cloud deploy` so a reader cannot conclude the product has no cloud deployment. Each mode is given `--dev` from a package install, so it clears the locality gate and stops at the unrelated dev-mode-needs-a-checkout refusal — seconds, not a 20-minute image build. Two halves keep it honest: `--local` must still be accepted (a no-op, not "unrecognized arguments"), and `--cloud` must still be *refused* with the cloud pointer, which a wholesale-deleted gate would fail. The full deploy with no locality flag is `k8s-local-smoke.yml` / `terraform-local-smoke.yml` | PR/push (paths: `src/nyxgpt/cli.py`, `src/nyxgpt/ops.py`), dispatch |
| [`cloud-status-smoke.yml`](../.github/workflows/cloud-status-smoke.yml) | `ubuntu-latest` | That an operator who has lost the deploy's scrollback gets their instance back from `nyxgpt cloud status` — public IP, SSH `user@host`, identity file — when run as they run it: the built wheel installed into a clean venv, reading the real `~/.nyxGPT/cloud` files (#3813). Three phases so a pass is not vacuous: UNKNOWN with no deploy record, the connection target with one, and `nyxgpt cloud ops` reporting a real ssh failure (against an unroutable TEST-NET-3 address) with the wrapped fix rather than a raw `ssh`/`docker compose` instruction | PR/push (paths: `src/nyxgpt/cloud_deploy.py`, `cli.py`, the smoke script), dispatch |
| [`cloud-target-os-smoke.yml`](../.github/workflows/cloud-target-os-smoke.yml) | `ubuntu-latest` | That the installed `nyxgpt cloud deploy --os` **delivers** the target OS's bootstrap itself, rather than leaving a human to carry it to the instance — the defect #3867 reports, where the only route to an EC2 Mac was pasting `nyxgpt cloud user-data --os macos` into an AWS console launch. Runs a real sshd on the runner with an authorized-keys forced command that captures stdin and `$SSH_ORIGINAL_COMMAND`, so client, server, connection and delivery are all real while the bootstrap is inspected rather than executed. Four phases: `--os macos` with no host reaches the Dedicated Host allocation path (#3995) and stops on AWS rather than on the policy refusal it used to make, having recorded and billed nothing; `--os macos --host` lands the EC2 Mac bootstrap, elevated with `sudo -n`; a Linux plan lands the Linux one over the same path (so a deploy shipping one hard-coded script fails one of the two); and `cloud status` still reports the target OS afterwards | PR/push (paths: `src/nyxgpt/cloud_deploy.py`, `cloud_provision.py`, `cli.py`, `cloud_mac.py`, the macOS template, the smoke script), dispatch |
| [`k3s-cloud-smoke.yml`](../.github/workflows/k3s-cloud-smoke.yml) | `ubuntu-latest` | That `nyxgpt cloud deploy --kubernetes` produces a cluster the existing `k8s/*.yaml` run on with nothing on the public interface (#3956, implementing #3506). It executes the deploy's **own** bootstrap text — `cloud_deploy.render_k3s_bootstrap()`, not a copy — on a real Linux runner, since no hosted runner is an EC2 instance. Seven steps: the bootstrap; the access surface (apiserver on the node's private address and not `0.0.0.0`, no Traefik, no `servicelb`, `local-path` still the default StorageClass — asserted only *after* k3s's addon deployer has demonstrably run, or their absence is a head start rather than evidence); `k8s/` accepted by the real API server and byte-identical to the repository's copy; then two fault injections — a docker-built image must `ImagePullBackOff` **before** `ops._k3s_import_image` and run after it, and `127.0.0.1:8000` must go **dead** when the access bridge is stopped, so neither the import nor the bridge can pass by luck; and finally the `--no-kubernetes` teardown against that live cluster and bridge, plus an idempotence re-run (every first deploy runs it on a box that has neither). What it cannot cover, and says so: a real EC2 instance, a real security group, and IMDSv2 — see [live verification](live-verification-ci.md) | PR/push (paths: `src/nyxgpt/cloud_deploy.py`, `src/nyxgpt/ops.py`, `k8s/**`, the smoke script), dispatch |
| [`project-hygiene-smoke.yml`](../.github/workflows/project-hygiene-smoke.yml) | `ubuntu-latest` | A deliberate project-field write that lands *while* the hygiene job is running survives it, and a genuinely empty issue is still fully populated. The concurrent write is injected into the window between hygiene's check of a field and its write of it, and a guard-stripped copy of the script is run in the same scenario to show the clobber reproduces (#3816). The same suite also covers the non-completed **closure rule** (#3871) against a stateful `gh` stub — on a `not_planned` closure the issue ends up carrying the `Phase X: Rejected` **milestone** (written by number, because that milestone is closed and `gh issue edit --milestone` resolves open ones only), the owner as its **sole** assignee, and **every** project field cleared, Status included; untouched on a `completed` or reason-less one — plus a guard that no write on the *fill* path escapes the re-read protection | PR/push (paths: `scripts/agents/ensure_issue_hygiene.sh`, `lib/gh_project.sh`, the hygiene workflow, the suite), dispatch |
| [`github-script-injection-smoke.yml`](../.github/workflows/github-script-injection-smoke.yml) | `ubuntu-latest` | The developer agent's fatal-error escalation survives a diagnosis containing an apostrophe, double quote, backtick, `${`, newline and backslash, posting it intact. The real `script:` body is extracted from the workflow YAML and run under Node, and the pre-fix (interpolated) form is run alongside it and must still die with a `SyntaxError`, so the green half is not green by luck (#3820). A second job fails on any `${{` interpolation left in a `script:` body tree-wide, and requires the scanner to reject a planted one | PR/push (paths: `.github/workflows/**`, the guard/probe scripts, the suite), dispatch |
| [`canary-pod-reason-smoke.yml`](../.github/workflows/canary-pod-reason-smoke.yml) | `ubuntu-latest` | That the reason a canary Pod cannot run reaches the operator, parsed out of a live cluster rather than a fixture (#3831). A Deployment the scheduler really refuses must make `deployment_health`, `_wait_rollout` and the API's `409` detail all name `Unschedulable: … Insufficient memory`; the same Deployment made schedulable must report healthy with no reason appended, so the enrichment cannot pass by firing unconditionally. One kind node and two `pause` Pods — no image build | PR/push (paths: `src/nyxgpt/canary.py`, `app.py`, the smoke script), dispatch |
| [`self-heal-unschedulable-smoke.yml`](../.github/workflows/self-heal-unschedulable-smoke.yml) | `ubuntu-latest` (kind) | What the self-heal watchdog *does* to a Pod the scheduler refused. #3832 deleted one every ~15s forever — seven Pods in 4.5 minutes, each deletion resetting the Pod age the operator needed. Three halves on a real cluster: the old remedy reproduced (three deletions, three new names, still `Unschedulable` every time), real `heal_now()` passes through a recording `kubectl` shim asserting zero delete calls with the scheduler's message surfaced, and a Running-but-not-ready Pod still healed *and* still stopped at its budget despite being renamed by every heal | PR/push (paths: `src/nyxgpt/self_heal.py`, `k8s_pod_state.py`, `ops.py`, the smoke script), dispatch |
| [`claude-md-binding-canary.yml`](../.github/workflows/claude-md-binding-canary.yml) | `ubuntu-latest` | That `claude-code-action` loads the repo-root `CLAUDE.md` into the agent's context — the runtime binding path for every agent here, and the reason project doctrine is stated once and cited rather than copied into prompts (ledger **V-028**, #3821). A run-unique token is injected into the checked-out `CLAUDE.md`; the default run must return it and a control run pinned to `--setting-sources user` must not, so a model that read the file with a tool fails the same as a vacuous assertion | Dispatch only — each run spends model tokens, and the fact changes only when the action is upgraded or a workflow starts passing `--setting-sources` |
| [`config-wizard-save-smoke.yml`](../.github/workflows/config-wizard-save-smoke.yml) | `ubuntu-latest` | That a Configuration Wizard save cannot leave the running API unable to serve or boot (#3944). Against a real uvicorn reading a seeded `~/.nyxGPT/config.ini` whose `[monitoring] SLACK_BOT_TOKEN` is spelled uppercase — a spelling `ConfigParser` reads fine — it clicks Remove, then Save, and asserts the stale key really went, the file still parses, the option was rewritten *in place with its casing intact*, the API still answers, and a **fresh process still boots** from that file (the 502 half, which only a restart can see). The pre-fix matcher and unchecked write are injected first and must reproduce the brick, so a green run cannot be green by luck. The same job also reads the wizard's own load request off the wire and asserts `[monitoring] slack_bot_token` comes back as `{set, masked}` and its value appears nowhere in the response (#3947) — with the pre-fix `secret=False` classification injected first and required to leak it | PR/push (paths: `src/nyxgpt/config_wizard.py`, `config.py`, `app.py`, `example.config.ini`, the smoke script), dispatch |
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

The same workflow's "Reproduce the broken pyexpat…" step is that pattern
applied to a *cause* rather than a symptom (#3814): it makes the runner's
`python@3.12` unable to load `pyexpat`, installs the formula with its
preflight stripped out to show the recipe walking straight past the fault,
then installs the shipped formula and requires `brew install` to refuse
before the venv exists. Worth copying for the discipline as much as the
technique — the two symptoms that fault produced were each patched on their
own before anyone injected the thing underneath them.

`linux-native-smoke.yml`'s `service-venv-python` job is the same shape for an
*environment* the runner does not have: the defect (#3782) needed a system
`python3` below nyxGPT's `requires-python`, and every hosted runner's is
above it. The job installs a real Python 3.9, puts it on PATH as `python3`,
and proves both directions with the real sdist — a venv built by resolving
bare `python3` is one pip refuses the artifact into, and the selection logic
picks a qualifying interpreter over it and installs cleanly.

### What still cannot be executed in CI

The short list in [live verification in CI](live-verification-ci.md) — the
Docker-backed components on the hosted macOS runners (those images ship no
Docker daemon), the setup wizard's interactive prompts, what a web panel
*renders* on the brew path, Ollama model pulls, real Slack delivery, and LLM
answer *quality* — plus EC2 Mac hardware, which has no hosted runner (see the
[portability matrix](portability-matrix.md)). Those are named explicitly in
the PR and exercised by the owner during acceptance testing; everything else
gets run. Note what is *not* on the list: `macos-brew-smoke.yml` runs the
Homebrew keg install *and*, since #3860, the user path after it — `nyxgpt
--version`, `nyxgpt up`, `GET /health`, `GET /api/v1/sessions`, the web UI on
:3000, `nyxgpt ops status`, `nyxgpt down`, `nyxgpt ops uninstall`, `brew
uninstall`/`untap` and the launchd residue check — on a real `macos-15`
runner, so neither a formula change nor a macOS service-lifecycle change can
claim macOS is untestable. The teardown half is proven on **pull requests**
too, without waiting for a published candidate: `keg-install` injects the
`com.nyxgpt.*` agents, shows they survive `nyxgpt ops down`, and asserts they
do not survive `nyxgpt ops uninstall` (#3859).

## Notes

- Integration tests will be skipped automatically if required services are not reachable.
- Use `pytest -v` for verbose output during development.
