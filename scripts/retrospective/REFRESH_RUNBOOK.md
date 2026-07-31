# MOVED: Retrospective Dashboard tooling now lives on the default branch

This feature branch is superseded (owner decision, 2026-07-31). The
retrospective dashboard pipeline — template, builder, data, and this runbook —
lives on the **repository default branch** (always the current release branch;
resolve it with `git ls-remote --symref origin HEAD`).

**If you are the daily-refresh Routine and your prompt pointed you here:**
check out the default branch instead and follow
`scripts/retrospective/REFRESH_RUNBOOK.md` there. Everything else in your
prompt (build, publish to the existing artifact URL, commit data) applies
unchanged, except commits go to the default branch, touching only files under
`scripts/retrospective/`.
