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
  macOS. Do not read that as "macOS is owner-acceptance": the Homebrew keg
  **install** *and*, since #3860, the **user path after it** -- `nyxgpt
  --version`, `nyxgpt up`, `GET /health`, `GET /api/v1/sessions`, the web UI
  on :3000, `nyxgpt ops status`, `nyxgpt down`, `brew uninstall`/`untap` and
  the launchd/plist residue check -- are executed on a real `macos-15` runner
  by [`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml)
  (`published-tap` job, driving
  [`scripts/macos-user-path-smoke.sh`](../scripts/macos-user-path-smoke.sh)).
  What the install *tells* the operator to run is executed too, on every PR
  that touches a formula: the `keg-install` job captures the `brew install`
  transcript and
  [`scripts/homebrew-caveats-smoke.sh`](../scripts/homebrew-caveats-smoke.sh)
  requires it to name `nyxgpt up` and what `brew services start` leaves out
  (#3854), injecting the caveats-less formula to prove the check discriminates.
  A formula, service-lifecycle or install change is therefore not exempt from
  the executed-verification gate (`agents/runbooks/review-runbook.md` §1c) on
  the grounds that this entry exists; what remains uncovered on that runner is
  the Docker-backed set immediately below. The same workflow's
  `stable-over-candidate` job additionally executes what happens when two
  channels are installed at once: `brew services` really registers both keg's
  services and the real install-identity reconcile (#3861) runs against them,
  in both directions. The same boundary applies to the dev install mode's macOS
  LaunchAgents (`com.nyxgpt.api`/`com.nyxgpt.web`, see
  [`--dev`](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)):
  `launchctl bootstrap gui/<uid>` needs a real GUI session, which the hosted
  macOS runners do not have. Dev mode's *install mechanics* -- editable venv,
  dev-server wrapper, mode recording, mode switching -- are executed on a
  real runner by
  [`linux-native-smoke.yml`](../.github/workflows/linux-native-smoke.yml)'s
  `linux-native-dev-smoke` job, so only the launchd load itself is owner
  acceptance.
- **Docker-backed components on the hosted macOS runners** -- the `macos-15`
  images ship no Docker daemon (Docker Desktop is a licensed GUI application,
  and the Apple Silicon runners expose no nested virtualisation, so Colima
  cannot stand in for it). The macOS user path
  ([`scripts/macos-user-path-smoke.sh`](../scripts/macos-user-path-smoke.sh),
  run by `macos-brew-smoke.yml`'s `published-tap` job) therefore runs
  `nyxgpt up --skip-observability` and tolerates exactly the `nyxgpt ops
  install` steps that need that daemon:
  - `docker engine`
  - `cassandra container`
  - `cassandra log follower service`
  - `observability stack`
  - `glitchtip secrets dir`
  - `glitchtip auto-provisioning`
  - `slack webhook secret`

  That list is the script's
  `TOLERATED_STEPS` and this paragraph is its other half: **everything else in
  `nyxgpt up` is asserted** -- the config, the install-mode record, the native
  api/web/ollama services and the env sync are the user path, and a failure in
  any of them fails the job. Widening the tolerated set is how a gate goes
  hollow; a step that genuinely cannot run on a hosted runner belongs here,
  named, in the same commit that tolerates it. What stays owner acceptance:
  Cassandra-backed session storage (the CI run exercises the `file` backend
  the default config selects), and the observability profiles on macOS.
- **The setup wizard's prompts** -- `nyxgpt wizard` is interactive by design
  and `ops install` correctly refuses to run it without a TTY, so the CI user
  path seeds `~/.nyxGPT/config.ini` from the *installed package's* own
  `example.config.ini` instead. The wizard's questions and defaults are owner
  acceptance; that the packaged resource exists and is readable from a keg is
  asserted (it is #3759's defect class).
- **What the web UI renders** -- the user path asserts `GET /` on :3000
  answers 200 with an HTML document, which catches a web service that is
  installed but not serving (#3857's outer symptom). Whether a given panel
  shows data rather than a permanent placeholder is a browser question:
  `nyxgpt ops verify`'s Playwright screenshots cover it for the Compose
  stack, and for the brew path it stays owner acceptance.
- **Ollama model pulls and anything needing a GPU** -- the hosted runners have
  neither the disk budget nor the hardware. The `ollama` *service* is
  installed and started on the user path; pulling a model and generating from
  it is owner acceptance.
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
