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
| [`../web/audit-ci.jsonc`](../web/audit-ci.jsonc) | npm audit (via `audit-ci`) | JSONC `allowlist` array (module name, advisory ID, or dependency path), inline `//` comments |

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
- **pip-audit**: fails on any finding not listed in `pip-audit-ignore.txt`.
  As of 2026-08-03, a clean `pip install -e .[dev]` reports zero known
  vulnerabilities in nyxGPT's own dependency closure, so the ignore file is
  empty.
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
  GHSA-vfj7-8cjw-p6xm against `braces`, which has **no** patched release to
  upgrade to and is allowlisted per-path).

  A red `security-scan` with no dependency change in the diff is that
  recurrence, not a defect in the branch under review -- it fails on the
  release branch and therefore on every open PR at once. Check `npm audit`
  before hunting for a culprit in the change.

## Running locally

```bash
# Python SAST
bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high

# Python dependency audit
pip-audit  # add --ignore-vuln <ID> per line in pip-audit-ignore.txt if needed

# Web dependency audit
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
