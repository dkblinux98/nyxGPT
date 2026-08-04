# Live Verification in CI (#3555 / P6-18)

Implements P6-18 (`product_management/PHASE_6_PLAN.md`): the review agent
runs `nyxgpt ops verify` -- the live smoke harness -- itself, in its own CI
job, on every PR that touches observability, metrics, or UI surfaces. It
reads the harness's Prometheus/Grafana assertion output, visually inspects
the Playwright dashboard screenshots (the review agent is multimodal), and
cites that live evidence in its APPROVE/REQUEST_CHANGES decision. This is
what closes the review deadlock from PR #3548/#3469: an acceptance criterion
demanding "verified live" no longer has to be deferred to owner acceptance
testing (`agents/runbooks/review-runbook.md` §2's interim exception,
2026-08-01) -- it's verifiable in the loop, before merge.

## What ships in this change

Everything that is **not** a `.github/workflows/*` file ships directly:

- `src/nyxgpt/verify.py` -- pure traffic-generation and assertion logic:
  a chat round-trip, one RAG ingest per source path (document/upload/repo),
  a RAG query; Prometheus instant-query counter-delta assertions; Grafana
  HTTP API panel-query assertions (re-executes each touched dashboard
  panel's own query, so a failure names the panel and the exact query);
  Playwright dashboard screenshots.
- `src/nyxgpt/ops.py` -- the `nyxgpt ops verify` CLI entrypoint (`verify()`):
  boots the Compose stack (core app + observability profiles) in an
  ephemeral environment unless `--skip-boot`, waits for health, generates
  the traffic, runs the assertions, captures screenshots, tears the stack
  back down unless `--keep-up`. This is the no-raw-`docker`-command wrapper
  the owner's "Operational Command Wrapping" requirement (`CLAUDE.md`)
  requires for any nyxGPT operation.
- `src/nyxgpt/cli.py` -- `ops verify` subcommand wiring (`--skip-boot`,
  `--keep-up`, `--skip-screenshots`, `--screenshot-dir`, `--dashboards`,
  `--api-url`, `--timeout`).
- `pyproject.toml` -- new `verify` optional-dependencies group
  (`playwright`), kept out of the base/dev sets since it pulls a separate
  browser-binary install step most commands never need.
- `tests/unit/test_verify.py`, `tests/unit/test_ops_verify.py` -- unit
  coverage for the harness's pure logic and its boot/teardown/gating
  orchestration (HTTP layer mocked via `httpx.MockTransport`, no live
  stack needed).
- `agents/runbooks/review-runbook.md`, `agents/prompts/review-agent.prompt.md`
  -- the review agent now runs the harness itself instead of deferring live
  checks to owner acceptance (see the diffs in those files for the exact
  narrowed rule).

Owner can run the exact same harness locally as a one-command pre-check
before acceptance testing:

```bash
pip install -e ".[verify]"
playwright install --with-deps chromium
nyxgpt ops verify           # boots, tests, tears down
nyxgpt ops verify --keep-up # leave the stack up afterward to look around
```

## What the owner needs to apply by hand

Agent tokens cannot write `.github/workflows/*` (same hand-carry pattern as
#3454/#3479/#3480/#3501). Apply these changes to
`.github/workflows/claude-code-review.yml`:

**1. Add an `observability_changed` output to the existing "Detect changed
languages" step** (right after the `python_changed` block, ~line 123):

```yaml
          if echo "$CHANGED_FILES" | grep -qE '^(src/nyxgpt/(metrics|app|ops|verify)\.py|docker/grafana/|docker/prometheus\.yml|docker-compose\.yml|web/src/app/admin/)'; then
            echo "observability_changed=true" >> "$GITHUB_OUTPUT"
          else
            echo "observability_changed=false" >> "$GITHUB_OUTPUT"
          fi
```

**2. Add a new step after "Install dependencies" (~line 134) that prepares
the live-verification environment, gated on that output** -- Docker is
preinstalled on `ubuntu-latest` GitHub-hosted runners, so this only needs
the Python `verify` extra, a Playwright browser, a seeded config, and
writable bind-mount dirs (same permissions fix `terraform-local-smoke.yml`
already carries for the same runner class):

```yaml
      - name: Prepare live-verification environment
        if: steps.changed.outputs.observability_changed == 'true'
        run: |
          pip install -e ".[verify]"
          playwright install --with-deps chromium

          mkdir -p "$HOME/.nyxGPT"
          cp example.config.ini "$HOME/.nyxGPT/config.ini"
          sed -i \
            -e '/^\[monitoring\]/,/^\[/ s/^enabled = false/enabled = true/' \
            -e '/^\[log_aggregation\]/,/^\[/ s/^enabled = false/enabled = true/' \
            -e '/^\[tracing\]/,/^\[/ s/^enabled = false/enabled = true/' \
            -e '/^\[error_tracking\]/,/^\[/ s/^enabled = false/enabled = true/' \
            "$HOME/.nyxGPT/config.ini"

          for d in prometheus grafana loki glitchtip-postgres glitchtip-uploads \
                   cassandra ollama nyxgpt-data; do
            mkdir -p "$HOME/.nyxGPT/volumes/$d"
            chmod 777 "$HOME/.nyxGPT/volumes/$d"
          done
```

**3. Tell the review agent to run the harness**, in the `prompt:` block
(~line 394, right after the existing "Perform comprehensive code review
checking:" section) -- this is what makes running `nyxgpt ops verify` the
review agent's own action (via its `Bash` tool), not a separate CI step it
merely reads artifacts from:

```
            ${{ steps.changed.outputs.observability_changed == 'true' && '
            ## Live verification (REQUIRED for this PR -- touches observability/metrics/UI)

            Run the live smoke harness yourself and cite its evidence in your
            review -- do NOT defer this to owner acceptance testing (the
            review-runbook §2 exception only covers what this harness cannot
            run in CI, e.g. Apple Silicon brew-services layout, real Slack
            delivery -- everything else is now verifiable in-loop):

            1. `nyxgpt ops verify` (the environment is already prepared --
               Compose stack boots ephemerally, generates known chat/RAG
               traffic, asserts it via Prometheus + Grafana, screenshots the
               touched dashboards, tears itself down).
            2. Read the full assertion output. A failing Prometheus counter
               delta or Grafana panel query names the exact query that
               failed -- treat any failure as a Critical or Medium finding
               per the severity model.
            3. Use your Read tool on every PNG under
               `~/.nyxGPT/verify-artifacts/` and visually inspect it -- does
               the dashboard render the touched panels with real data, or is
               it broken/empty/erroring?
            4. In your review body, add a "### Live Verification" section
               summarizing the harness run (pass/fail per check) and what
               the screenshots show. An APPROVE on an eligible PR with no
               "### Live Verification" section, or one that skipped running
               the harness, is a process violation.
            ' || '' }}
```

**4. Add the `~/.nyxGPT/verify-artifacts/` screenshots as a workflow
artifact** (optional but recommended, so a human can pull them up without
re-running the harness) -- add near the existing `claude-review` step:

```yaml
      - name: Upload verify screenshots
        if: always() && steps.changed.outputs.observability_changed == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: verify-dashboard-screenshots
          path: ~/.nyxGPT/verify-artifacts/
          if-no-files-found: ignore
```

### No new repo variables needed

This uses only `GITHUB_TOKEN`/checkout/setup-python, already present in the
workflow -- no new Settings -> Secrets and variables -> Actions entries.

## What CI cannot cover (owner acceptance only)

Per the acceptance criteria, this is the complete, documented list of what
stays an owner acceptance check -- everything else the harness's assertions
and screenshots make verifiable in the review loop:

- **Apple Silicon brew-services native layout** -- `nyxgpt ops verify` boots
  a pure-Compose stack (matches every other CI gate in this repo); it does
  not exercise the native launchd/brew path `nyxgpt ops install` uses on
  macOS.
- **Real Slack delivery** -- `nyxgpt ops alert-test` (separate command)
  posts through Grafana's contact-point test API; actually landing a
  message in a real Slack workspace still requires a real webhook secret,
  which CI does not have (see #3505/secrets-sync for wiring one in).
- **Anything gated behind a real (non-stubbed) LLM** -- CI runs the chat
  round-trip against whatever Ollama model is configured for the runner
  (small/stubbed per the acceptance criteria); response *quality* is not
  asserted, only that a reply came back and the request/response pipeline
  (chat -> RAG -> metrics -> dashboards) is intact end to end.

## Verifying the gate

Locally, from a clean checkout, with Docker running:

```bash
pip install -e ".[dev,verify]"
playwright install --with-deps chromium
nyxgpt wizard    # or seed ~/.nyxGPT/config.ini from example.config.ini
nyxgpt ops verify
```

Expected: `[OK]` lines for every chat/RAG traffic step, every Prometheus
counter-delta assertion, and every Grafana panel-query assertion, plus a
`[OK] Screenshot captured: ... -> ~/.nyxGPT/verify-artifacts/<uid>.png` line
per touched dashboard. A deliberately broken dashboard panel query (edit one
`expr` in `docker/grafana/dashboards/rag-performance.json` to reference a
nonexistent metric) reproduces the failure mode this harness exists to
catch: `nyxgpt ops verify` exits 2, and the failing check's message names
the panel and the exact (broken) query.
