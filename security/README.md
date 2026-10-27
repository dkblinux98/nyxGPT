# Security Scanning (#3501)

Three scanners run in CI on every push/PR: bandit (Python SAST), pip-audit
(Python dependency vulnerabilities), and `npm audit` via `audit-ci` (web
dependency vulnerabilities). See
[`docs/security-scanning-ci.md`](../docs/security-scanning-ci.md) for the
proposed workflow YAML (a hand-carry deliverable -- agent tokens cannot
write `.github/workflows/*`) and
[`agents/runbooks/developer-runbook.md`](../agents/runbooks/developer-runbook.md)
for the day-to-day developer workflow (running scans locally, adding
suppressions).

This directory holds the suppression/allowlist files each scanner's CI step
reads, so accepted findings are re-triaged in code review instead of buried
in a CI log:

| File | Scanner | Format |
|---|---|---|
| [`pip-audit-ignore.txt`](pip-audit-ignore.txt) | pip-audit | One vuln ID per line, `#`-commented justification above each entry |
| [`../web/audit-ci.jsonc`](../web/audit-ci.jsonc) | npm audit (via `audit-ci`) | JSONC `allowlist` array, **path-scoped entries only**: advisory ID, `\|`, then the `>`-separated dependency path as audit-ci prints it ready to paste (`<GHSA-id>\|<parent>child>…>vulnerable-package`), reasoning in a full-line `//` comment above each. A bare module name or advisory ID fails the build |
| [`codeql-adjudications.yml`](codeql-adjudications.yml) | CodeQL (GitHub default setup) | YAML `adjudications` list keyed to the **flow** (rule, sink function, source function) rather than the alert, so an edit that only moves the sink does not re-raise an accepted finding (#4222). Each entry carries a `headline` (lands on the alert, 280-char cap) and a `comment` (the full argument, stays here) |

The CodeQL row is not a fourth CI scanner: CodeQL runs as GitHub's default
setup, and `.github/workflows/codeql_adjudications.yml` applies these
adjudications on the `code_scanning_alert` event via
`scripts/agents/apply_codeql_adjudications.py`. **Never dismiss a code-scanning
alert by hand** — that state is keyed to the sink's line number, so the next
edit that moves the sink re-raises the identical accepted finding.

Bandit has no separate suppression file here: its official mechanism is an
inline `# nosec <RULE_ID> -- <reason>` comment at the flagged line (see
[bandit's docs](https://bandit.readthedocs.io/en/latest/config.html#exclusions)),
which keeps the justification next to the code it applies to. Project-wide
excludes live in `pyproject.toml`'s `[tool.bandit]` table.

## Gate thresholds (fails the build)

- **bandit**: `--severity-level high --confidence-level high` -- as of
  2026-08-03 this is 0 findings (`bandit -c pyproject.toml -r src/
  --severity-level high --confidence-level high`). A separate, non-blocking
  full-severity report step runs for visibility (currently 24 MEDIUM / 39
  LOW findings, mostly `subprocess` usage and dynamic CQL string-building in
  the Cassandra RAG store; none currently carry an inline suppression
  because none are blocking -- triage/suppress with `# nosec <RULE_ID> --
  <reason>` on a case-by-case basis if that changes).
- **pip-audit**: fails on any finding not listed in `pip-audit-ignore.txt`,
  **and on any entry in that file which matches no finding** (#4239 --
  `--ignore-vuln` tolerates an unmatched ID silently, so the gate classifies
  one unfiltered report instead of filtering the scan). Note that pip-audit
  audits the *environment it runs in*: on a machine with unrelated packages
  installed it will report their advisories too, which is not what CI
  measures.
- **npm audit / audit-ci**: `"high": true` in `web/audit-ci.jsonc` -- fails
  on high or critical severity findings not in the `allowlist`. Moderate/low
  findings are reported but non-blocking. **What is currently allowlisted,
  and why, is recorded in that file and nowhere else** -- read it rather
  than this bullet. The sentence that used to live here ("`npm audit`
  reports zero known vulnerabilities ... so the allowlist is empty", as of
  2026-08-06, #3644) was a dated world-state claim about a moving target,
  and it went false twice without a line of code changing: advisories get
  published against versions that are already pinned, so this gate rots in
  place (2026-09-30, cleared by `npm audit fix`; 2026-10-04 / #4150,
  GHSA-vfj7-8cjw-p6xm against `braces`, allowlisted per-path because it had
  no patched release -- and then **withdrawn** upstream on 2026-10-23, which
  is #4239 below).

  A red `security-scan` with no dependency change in the diff is that
  recurrence, not a defect in the branch under review -- it fails on the
  release branch and therefore on every open PR at once. Check `npm audit`
  before hunting for a culprit in the change.

## A suppression that suppresses nothing fails the build (#4239)

The gate rots in both directions. An advisory can appear against a pinned
version with nothing in the diff (above), and an *accepted* finding can
disappear without the condition the entry was watching ever being met.
GHSA-vfj7-8cjw-p6xm did the second: it was withdrawn ("not a practically
exploitable vulnerability") rather than fixed, so two path-scoped `braces`
entries stopped suppressing anything while `braces` stayed at 3.0.3 and both
dependency paths stayed exactly where they were. `audit-ci` reported it as a
*warning* on every green run for four days.

[`scripts/ci/stale_suppressions.py`](../scripts/ci/stale_suppressions.py) is
the one place that turns that into a failure, for both scanner registers:
`npm run audit:ci` routes through it, and so does the workflow's pip-audit
step. The other two registers already had the equivalent --
`test_codeql_adjudications.py::test_every_entry_names_a_function_that_exists`
for `codeql-adjudications.yml`, and
`test_npm_dependency_floors.py::test_no_deliberate_exact_pin_is_stale` for
the dependency floors.

When you delete a dead entry, record **why it went quiet**: upgraded,
re-scoped and withdrawn justify different next actions, and only the
advisory itself can tell you which. `curl -s
https://api.github.com/advisories/<GHSA-id>` needs no token and returns
`withdrawn_at`, `first_patched_version` and the current range.

## Running locally

```bash
# Python SAST
bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high

# Python dependency audit -- reads pip-audit-ignore.txt itself; same gate CI runs
python3 scripts/ci/stale_suppressions.py pip-audit

# Web dependency audit -- same gate CI runs
cd web && npm run audit:ci
```

## Adding a suppression

1. Confirm the finding is a false positive, not exploitable in nyxGPT's
   usage, or accepted with a tracked follow-up (link an issue if one exists
   yet -- filing one isn't a blocker to adding the suppression).
2. Add the entry with a justification comment and today's date, in the
   format used by the existing entries in the relevant file above.
3. Call out the suppression explicitly in the PR description so review can
   evaluate the justification, not just the code diff.
4. Write a revisit condition, but do not rely on it to retire the entry --
   see #4239 above. The gate is what notices when the entry dies.
