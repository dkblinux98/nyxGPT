# Developer Runbook (developer-agent)

This is the procedural “how” for implementing issues. Authority is defined in the charter.

## 0) Preconditions
- Repo clean, on correct base (release branch or per project rule)
- Up to date with remote
- Services healthy (if applicable)
- Tests passing before starting

## 1) Pick up work
- Ensure issue is assigned to developer-agent and status is In Progress.
- Confirm Phase/Sprint fields are set.

## 2) Branching
- Create a short-lived branch named with issue reference, e.g.:
  - `feat/<issue-id>-<slug>` or `fix/<issue-id>-<slug>`
- Base off the current active release branch.

## 3) Implement
- Make smallest coherent change set that satisfies acceptance criteria.
- Add/extend tests (unit/integration as appropriate).
- Keep IO behind interfaces; maintain dependency flow.

## 3a) Instrumentation conventions

RCA, self-heal, and SRE work are only as good as the logging behind them
(#3415). Apply these conventions to any new code, not just the file you're
touching:

- **No silent excepts.** Every `except Exception:` (or narrower) that
  swallows an error and substitutes a default must log at WARNING (or ERROR
  if the caller can't recover) what failed and what default was used —
  `logger.warning("Invalid <key> in config, using default <value>: %s", e)`
  is the pattern used throughout `config.py`'s getters
  (`nyxgpt.config._log_fallback_once`, which also dedupes a given key's
  warning to once per process — reuse it for new config getters instead of
  a bare `except Exception: return default`).
- **Subprocess failures must log cmd + rc + stderr tail.** Any helper that
  wraps `subprocess.run` (see `ops.py`/`canary.py`/`self_heal.py`'s `_run`)
  must log at WARNING on non-zero exit, in addition to whatever the
  `CompletedProcess`/exception carries back to the caller — the caller's
  handling of the result must never be the only path to visibility.
- **New request paths get lifecycle records.** A request-shaped code path
  (chat turn, RAG query/ingest, an API endpoint doing real work) emits a
  start record and a completion-or-failure record at INFO, carrying
  `session`/`model` where applicable, an `outcome`, and a `duration_ms` —
  see `chat.py`'s `chat()`/`chat_stream()` and `rag.py`'s
  `retrieve_context()`/`ingest_document()` for the pattern.
- **Trace-context propagation for new outbound calls.** New code that calls
  Ollama, another internal service, or shells out should carry the current
  request/trace correlation id so cause and effect can be joined across
  logs (see `tracing.traced_span`/`traced` for the current span helpers;
  full W3C `traceparent` propagation across the web/API/Ollama boundary is
  tracked under #3415 gap 6).
- **Use the formatter's extras, not string interpolation, for structured
  fields.** Pass `component` (subsystem name, e.g. `"ops"`, `"rag"`,
  `"chat"`, `"ollama"`), and `session`/`model` where known, via
  `extra={...}` — `StructuredFormatter` (`logging.py`) serializes them as
  first-class JSON fields when `[logging] format = json`, instead of being
  buried in a formatted message string.
- **Exclude new polling endpoints from access-log noise.** A new
  `/health`-, `/metrics`-, or dashboard-polling-style endpoint hit on a
  fixed interval belongs in `logging.py`'s
  `_ACCESS_LOG_EXCLUDED_PATH_PREFIXES`, or it will fill the rotating file
  handler with near-zero-signal INFO lines on every poll.
- **Tests must never write to the production log dir.** A local
  `pytest tests/unit/` run once wrote synthetic ERROR records through the
  real rotating file handler into `~/.nyxGPT/logs`, which promtail shipped
  to Loki and which then derailed a real incident RCA because it looked
  indistinguishable from a genuine chat failure (#3443). Two structural
  fixes, both in `tests/conftest.py`'s session-scoped `_isolate_test_log_dir`
  fixture: it rewrites the real `~/.nyxGPT/config.ini`'s `[logging] dir` to a
  temp dir for the whole session (restored at teardown), covering every code
  path that loads the real config; and it sets a `NYXGPT_LOG_DIR` env var
  that `get_log_dir()` (`logging.py`) uses as the *fallback* when a cfg
  doesn't set `[logging] dir` at all, covering tests that swap in their own
  bare/isolated config. The fixture also asserts the real log dir is
  untouched at teardown. Don't bypass `get_log_dir()` with a hardcoded
  `~/.nyxGPT/logs` path in new code, and don't rely on per-test caplog
  discipline as the only safeguard.

## 3b) Workflow-authoring conventions

Any new or edited `.github/workflows/*.yml` job triggered by `issues`,
`issue_comment`, `pull_request`, or `pull_request_review*` and carrying write
permissions (`contents: write`, `issues: write`, `pull-requests: write`, or a
secret-backed `GH_TOKEN`) MUST carry an actor gate on its `if:` condition —
never rely on comment/body text alone (#3600, going-public hardening).

- **Gate on identity, not phrasing.** Check
  `github.event.comment.user.login` (or `github.event.review.user.login`)
  against `vars.HUMAN_OWNER` and/or the relevant agent identity var
  (`vars.SCRUM_AGENT`/`vars.DEV_AGENT`/`vars.REVIEW_AGENT`) — a trigger
  phrase like `contains(comment.body, '@approve-merge')` with no author
  check fires for any commenter on a public repo.
  `handle_acceptance_failure.yml` and `developer_auto_implement.yml`'s
  `RETRY_IMPLEMENTATION` path are the reference pattern.
- **Fork-PR guard on merge/review paths.** A job that reviews or merges a
  PR based on an `issue_comment` or `pull_request_review` event has no
  `pull_request.head.repo` in its event payload — add an explicit step
  that resolves it via `gh pr view <PR> --json headRepositoryOwner,
  headRepository` and fails the job if it doesn't equal
  `github.repository`, before any privileged action runs. See the "Verify
  PR head repo (fork guard)" step in `review_agent_auto_review.yml` and
  `claude-code-review.yml`.
- **Read-only automation is exempt.** Jobs that only read (e.g. posting a
  status comment gated on `vars.AGENTS_ENABLED`) don't need an actor gate;
  the requirement is scoped to jobs that write `contents`, `issues`, or
  `pull-requests`, or that hold a write-scoped secret token.

## 3c) Workflow actor-gate audit (#3600, 2026-08-03)

Point-in-time audit of every `.github/workflows/*.yml` job triggered by
`issues`, `issue_comment`, `pull_request`, or `pull_request_review*`, taken
when the three gates in §3b were added ahead of the repo going public.
Extend this table (don't replace it) the next time a workflow with one of
these triggers is added or edited — the review-runbook checklist entry for
§3b points back here.

| Workflow | Trigger(s) | Write scope | Actor gate | Notes |
|---|---|---|---|---|
| `review_agent_auto_review.yml` | `issue_comment`, `pull_request_review` | `contents`/`pull-requests`/`issues: write`, `REVIEW_AGENT_TOKEN` | `comment.user == HUMAN_OWNER` (manual overrides) / `REVIEW_AGENT` (auto+structured) + fork-PR guard | Fixed by #3600 |
| `notify_scrum_ready.yml` | `issue_comment` | `SCRUMMASTER_AGENT_TOKEN`, dispatches the scrummaster select-and-start loop | commenter ∈ `{HUMAN_OWNER, SCRUM_AGENT, DEV_AGENT, REVIEW_AGENT}` | Fixed by #3600 |
| `claude-code-review.yml` | `pull_request`(review_requested,synchronize), `issue_comment`, `workflow_dispatch` | Bash/Write/Edit + `CLAUDE_CODE_OAUTH_TOKEN` | `@review` path: commenter ∈ `{HUMAN_OWNER, REVIEW_AGENT, DEV_AGENT}` + fork-PR guard; other triggers already gated on `requested_reviewer`/`assignee==REVIEW_AGENT` | Fixed by #3600 |
| `handle_acceptance_failure.yml` | `issue_comment` | issues/PR write, `DEV_AGENT_TOKEN` | `comment.user == HUMAN_OWNER` | Reference pattern, unchanged |
| `developer_auto_implement.yml` | `issues`(assigned), `issue_comment` | `contents`/`issues`/PR write, `DEV_AGENT_TOKEN` | assignee==DEV_AGENT (issues) / `author_association==OWNER` or `user.login==DEV_AGENT` (`RETRY_IMPLEMENTATION`) | Reference pattern, unchanged |
| `scrummaster_sprint_reorg_apply.yml` | `issue_comment` | project field writes | `author_association==OWNER` + release-issue check | Unchanged |
| `acceptance_plan.yml` | `issues`(edited) | issues write | `github.actor==HUMAN_OWNER` + plan marker in body | Unchanged |
| `add-to-release-issue-on-milestone.yml` | `issues`(milestoned) | issues write (`GITHUB_TOKEN`) | none, but `milestoned` can only be produced by a user with write access — no public-actor path exists | Unchanged, no gate needed |
| `assign_backlog.yml` | `issues`(opened,reopened) | issues write (`SCRUMMASTER_AGENT_TOKEN`), adds an assignee | none besides `AGENTS_ENABLED` | Unchanged — write is scoped to adding scrummaster-agent as assignee on the triggering issue itself; no cross-resource write, no code exec, no merge |
| `ensure_project_hygiene.yml` | `issues`(opened,reopened), `pull_request`(opened,reopened) | issues/PR write (`SCRUMMASTER_AGENT_TOKEN`) | none besides `event_name` checks | Unchanged — writes only project fields/labels/milestone on the same issue/PR that triggered it; no cross-resource write |
| `auto-check-tasklist.yml` | `issues`(closed), `repository_dispatch` | issues write | none besides `AGENTS_ENABLED` | Unchanged — only checks a box on a tracking issue that already contains an unchecked `- [ ] #<closed-issue-number>` line placed there by scrummaster automation beforehand; an attacker can close only issues they already have permission to close, and gains no reference in a tracking issue they don't already appear in |
| `link_revert_pr_to_issue.yml` | `pull_request`(opened) | pull-requests write (`github.token`) | gated on `body` `startsWith('Reverts')` (attacker-controlled string) | Unchanged — re-verified during this audit: every write (`gh pr edit`, the informational comment) targets `github.event.pull_request.number`, i.e. the PR the attacker themselves just opened. Crafting a "Reverts owner/repo#N" body lets an attacker rewrite the body of *their own* PR to include a `Closes #ISSUE` line (extracted read-only from a real PR's linked issue) — this writes no resource the attacker doesn't already control, and any downstream merge/close of that PR is independently gated elsewhere. No actor gate added. |
| `notify-merge-conflicts.yml` | `pull_request`(opened,synchronize,reopened) | issues write (comment only) | none | Unchanged — notification only, no merge/code-exec |
| `delete_branch_on_pr_close.yml` | `pull_request`(closed) | contents write (branch delete) | none, but explicitly skips fork-head PRs + branch allow-pattern + deny-list | Unchanged — already scoped safely by construction |
| `claude.yml` | `issue_comment`, `pull_request_review_comment`, `issues`(opened,assigned), `pull_request_review` | Bash/Read/Write/Edit, `CLAUDE_CODE_OAUTH_TOKEN`; job-level `GITHUB_TOKEN` is read-only | **none** — any `@claude` mention triggers a full agentic session | **Known gap, out of #3600's scope.** The read-only job token can't push/merge directly, but on a public repo any user can trigger a costly agent session that posts comments under the bot's identity. Flagged for an owner decision (gate to `HUMAN_OWNER`/agent identities, or accept the risk for public Q&A). No fast-follow issue has been filed for this yet — file one before relying on this row as a tracked follow-up. |
| `admin_label_rename.yml`, `bulk_set_issue_status.yml`, `promote_accepted_features.yml`, `reconcile_closed_backlog_status.yml`, `scrummaster_sprint_report.yml`, `usage_limit_retry.yml`, `terraform-local-smoke.yml`, `validate-web-routes.yml`, `security-scan.yml` | `workflow_dispatch`/`schedule`/path-filtered CI | varies | N/A | No comment/issue-content-driven public-actor path. `security-scan.yml` (#3501, pending owner hand-carry per `docs/security-scanning-ci.md`) has no write permissions block and calls no `gh`/write APIs -- `pull_request`/`push` triggered but out of scope for this table's actor-gate requirement per §3b's "read-only automation is exempt." |

**Verification.** Each new `if:` condition was hand-traced against
representative actors:

- `review_agent_auto_review.yml`: `comment.user.login == vars.HUMAN_OWNER &&
  contains(body,'@approve-merge')` evaluates `true` only when HUMAN_OWNER
  comments `@approve-merge`; `false` for any other commenter regardless of
  body content, including a fork contributor.
- `notify_scrum_ready.yml`: the commenter-in-set check evaluates `true` for
  HUMAN_OWNER/SCRUM_AGENT/DEV_AGENT/REVIEW_AGENT comments containing
  `READY_FOR_NEXT_ISSUE`; `false` otherwise.
- `claude-code-review.yml`: the `@review` path evaluates `true` only for
  HUMAN_OWNER/REVIEW_AGENT/DEV_AGENT; the `review_requested`/`synchronize`/
  `workflow_dispatch` paths are unchanged and were not touched.
- Fork-PR guard: `gh pr view <PR> --json headRepositoryOwner,headRepository`
  compared against `github.repository` — traced against this repo's own PR
  #3603 (`headRepositoryOwner/headRepository` == `dkblinux98/nyxGPT`) to
  confirm the guard does not false-positive on same-repo PRs.

`issue_comment`-triggered workflows execute the workflow definition from the
repo's **default branch**, so these specific gates only take effect once
merged (v3.0.0 is also the default branch — see the issue's Technical
Details) and cannot be exercised live pre-merge. Two things stand in for a
live run: (1) the gates reuse the exact `comment.user.login ==
vars.HUMAN_OWNER`/`author_association` pattern already live in
`handle_acceptance_failure.yml` and `developer_auto_implement.yml` — the
boolean logic above was hand-traced against representative actors rather than
asserted from a specific run link, since individual workflow-run URLs are not
stable evidence (runs can be deleted or expire); run
[30856730941](https://github.com/dkblinux98/nyxGPT/actions/runs/30856730941)
is one concrete example of this pattern firing on issue #3600, triggered by
`myGPT-review-agent` re-invoking the developer-agent automation, and it
completed successfully; and (2) per the issue's own acceptance criteria, the
next `@approve-merge`, `READY_FOR_NEXT_ISSUE`, and `@review` invocations in
normal agent-loop operation after merge exercise the new gates for real, for
an allowed actor.

## 3d) Security scanning (#3501)

CI runs three scanners on every push/PR (proposed workflow staged at
`docs/security-scanning-ci.md` pending owner hand-carry into
`.github/workflows/` -- see §3b, agents can't write that path directly):
bandit (Python SAST), pip-audit (Python dependency vulnerabilities), and
`npm audit` via `audit-ci` (web dependency vulnerabilities). Full scanner
docs, gate thresholds, and the suppression-file format live in
[`security/README.md`](../../security/README.md) -- this section covers
when a developer needs to touch them.

- **Run locally before pushing** if your change touches `src/` (bandit +
  pip-audit) or `web/package.json`/`web/package-lock.json` (audit-ci):
  ```bash
  bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high
  pip-audit
  cd web && npm run audit:ci
  ```
- **A new HIGH-severity/HIGH-confidence bandit finding, an unignored
  pip-audit vulnerability, or a new high/critical npm advisory not already
  in the allowlist will fail CI.** Fix the underlying issue (upgrade the
  dependency, change the code pattern) rather than reaching for a
  suppression by default.
- **Only suppress after triage**, and only with a justification comment and
  today's date, per the format in `security/README.md`:
  - bandit: inline `# nosec <RULE_ID> -- <reason>` at the flagged line
    (bandit's own mechanism -- no separate baseline file).
  - pip-audit: add the vuln ID to `security/pip-audit-ignore.txt`.
  - npm/audit-ci: add the module name (or the more specific advisory-ID /
    dependency-path form) to the `allowlist` array in `web/audit-ci.jsonc`.
  Call out any new suppression explicitly in the PR description so review
  evaluates the justification, not just the diff.
- A dependency bump that resolves an existing allowlisted/ignored finding
  should remove that entry in the same PR -- don't let suppressions outlive
  the vulnerability they were accepting.

## 4) Verification loop (MANDATORY - ALL must pass before commit)
Run ALL of the following checks and fix issues until they pass:
- `black --check .` - If fails, run `black .` to auto-format, then re-check
- `ruff check src/ tests/` - Fix ALL linting errors (0 errors required)
- `mypy src/` - Fix ALL type errors (0 errors required)
- `pytest -v` - ALL tests MUST pass (0 failures, 0 errors)
- `./scripts/agents/validate-web-routes.sh` - If you modified web routes or API endpoints
- `bandit -c pyproject.toml -r src/ --severity-level high --confidence-level high` - If you modified `src/` (#3501)
- `pip-audit` - If you modified Python dependencies (#3501)
- `cd web && npm run audit:ci` - If you modified `web/package.json`/`web/package-lock.json` (#3501)

**CRITICAL**: Keep working until all checks pass (like a human developer would).
Pre-commit hooks MUST pass before commit succeeds.

If flaky tests appear, isolate and fix; escalate if persistent.

## 5) Documentation
- Update docs for any user-facing change:
  - Modified `src/nyxgpt/api.py` or `src/nyxgpt/app.py` → Update `docs/api.md`
  - Modified `src/nyxgpt/cli.py` → Update `docs/configuration.md` or `README.md` CLI section
  - Added/changed config options → Update `example.config.ini` AND `docs/configuration.md`
  - Added new features → Update `README.md` feature list
- Update architecture notes only if human-approved architecture change is required

## 6) Commit discipline
- Small commits with clear messages
- Reference issue in PR body: "Closes #ISSUE"
- Commit message format: `<type>: <description> (#ISSUE)`
- Valid types: feat, fix, test, docs, refactor, chore
- Only commit after ALL validation checks pass

## 7) Open PR
- Target: active release branch
- PR body MUST include: "Closes #ISSUE"
- Ensure CI runs (should pass since pre-commit hooks passed)
- Update issue status -> In Review
- Assign review-agent as PR reviewer (not just assignee)

## 8) Address REQUEST_CHANGES review
- If review-agent posts REQUEST_CHANGES review:
  - Issue automatically reassigned to you with Status -> In Progress
  - Read the full review comment
  - Fix ALL Critical and Medium issues listed
  - Run ALL validation checks again (full suite from step 4)
  - Commit and push fixes (triggers automatic re-review)
  - Repeat until APPROVE or 3rd REQUEST_CHANGES (escalates to human)
