# Security Scanning in CI (#3501)

Implements P6-2 (`product_management/PHASE_6_PLAN.md`): a push/PR CI gate
running bandit (Python SAST), pip-audit (Python dependency vulnerabilities),
and `npm audit` (web dependency vulnerabilities, via `audit-ci` for its
allowlist support), failing on high-severity findings not already accepted
in a suppression file.

## What ships in this change

Everything that is **not** a `.github/workflows/*` file ships directly in
this PR:

- `pyproject.toml` -- `bandit[toml]` and `pip-audit` added to the `dev`
  optional-dependencies group; new `[tool.bandit]` table (excludes
  `tests/`, `.venv`, `venv`, `web` from the SAST scan).
- `security/README.md` -- scanner overview, gate thresholds, how to run
  locally, how to add a suppression.
- `security/pip-audit-ignore.txt` -- pip-audit suppression list (empty as
  of 2026-08-03; nyxGPT's own dependency closure has zero known
  vulnerabilities in a clean install).
- `web/audit-ci.jsonc` -- `audit-ci` config (fails on high/critical,
  allowlists 4 currently-accepted high findings with justification
  comments -- see below).
- `web/package.json` -- `audit-ci` added as a devDependency (pinned
  `7.1.0`) plus a `npm run audit:ci` script; `web/package-lock.json`
  updated via `npm audit fix` (non-forcing -- resolved 18 of the 36
  findings that existed before this change, all within already-declared
  semver ranges, no `package.json` version bumps).
- `agents/runbooks/developer-runbook.md` -- new "Security scanning" section
  (the issue's "documented in the developer runbook" acceptance criterion).

## What the owner needs to apply by hand

Agent tokens cannot write `.github/workflows/*` (same hand-carry pattern as
#3454/#3479/#3480). Add this new file:

**New file `.github/workflows/security-scan.yml`:**

```yaml
name: Security Scan

on:
  pull_request: {}
  push: {}

jobs:
  security-scan:
    runs-on: ubuntu-latest
    # For push events, only run on the configured release branch (vars.RELEASE_BRANCH).
    # pull_request events always run regardless of target branch.
    if: github.event_name == 'pull_request' || github.ref == format('refs/heads/{0}', vars.RELEASE_BRANCH)
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Python project + security tooling
        run: |
          python -m pip install --upgrade pip setuptools
          pip install -e ".[dev]"

      - name: Python SAST (bandit) -- blocking on high severity/confidence
        run: bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high

      - name: Python SAST (bandit) -- full report, informational only
        if: always()
        run: bandit -c pyproject.toml -r src/ -f txt || true

      - name: Python dependency audit (pip-audit)
        run: |
          set -euo pipefail
          IGNORE_ARGS=()
          while IFS= read -r line; do
            id="${line%%#*}"
            id="$(echo "$id" | xargs)"
            [ -z "$id" ] && continue
            IGNORE_ARGS+=(--ignore-vuln "$id")
          done < security/pip-audit-ignore.txt
          pip-audit "${IGNORE_ARGS[@]}"

      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install web dependencies
        run: cd web && npm ci

      - name: npm dependency audit (audit-ci) -- blocking on high/critical
        run: cd web && npm run audit:ci
```

This mirrors the existing `validate-web-routes.yml` workflow's shape (same
checkout/setup-python/setup-node steps, same `vars.RELEASE_BRANCH` push
gate) rather than inventing a new convention.

### No new repo variables needed

`vars.RELEASE_BRANCH` already exists and is used by `validate-web-routes.yml`
today -- no new Settings -> Secrets and variables -> Actions entries
required for this workflow.

## Verifying the gate

Locally, from a clean checkout:

```bash
pip install -e ".[dev]"
bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high   # exit 0, 0 findings
pip-audit                                                                        # exit 0, 0 findings
cd web && npm ci && npm run audit:ci                                             # exit 0, 4 findings allowlisted
```

To confirm the gate actually fails on a real finding (verified during
implementation, not shipped as a test fixture): running `npx audit-ci --high`
in `web/` *without* `--config audit-ci.jsonc` exits 1 against the same
`package-lock.json`, listing the same `next`/`postcss`/`sharp`/
`serialize-javascript` advisories that the allowlist suppresses -- proving
the allowlist, not an inert threshold, is what keeps the gate green.
