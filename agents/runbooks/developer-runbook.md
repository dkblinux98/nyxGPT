# Developer Runbook (developer-agent)

This is the procedural “how” for implementing issues. Authority is defined in the charter.

## 0) Preconditions
- Repo clean, on correct base (release branch or per project rule)
- Up to date with remote
- Services healthy (if applicable)
- Tests passing before starting
- **`agents/LEDGER.md` read in full** (see §0a)

## 0a) The operating ledger (#3774)

`agents/LEDGER.md` is the system of record for cross-session memory. Your own
recollection of "how this project works" is untrusted input wherever the ledger
or the live system can answer instead.

**Read it before implementing.** Then, for the whole session:

- **A claim that is not in the ledger and not freshly verified is not asserted
  as fact.** Before writing a project fact into code, a doc, a commit message,
  a PR body or a review reply: find its entry, or verify it this session and be
  able to name how, or say plainly that you have not checked.
- **Check the Superseded section before "correcting" anything.** Re-asserting a
  retired belief (S-001..S-004) is the exact failure this file exists to stop.
- **Do not overwrite state you did not create.** A lane, marker or field that
  looks stale to you may be deliberately parked — check for a `P-` entry first.

**Append in the same PR** when your work establishes any of:

| You did this | Entry kind |
| --- | --- |
| The owner settled something in the issue thread | Decision (`D-`) |
| You established a fact the hard way (a check, a run, a read) | Verification (`V-`) — record the **method** |
| You deliberately left something undone | Parked (`P-`) — record the **revisit condition** |
| You hit a question you could not close | Open question (`Q-`) |

Ledger entries ride in whatever PR produced the fact — no separate issue, and
in scope by definition (the review runbook §1b tells the reviewer the same).
Keep entries load-bearing: **facts and decisions, never narration**. What you
did is already recorded by the commits, the PR and the issue thread; what you
*learned* is not recorded anywhere else. Entry schema and granularity rules are
in the ledger itself.

## 1) Pick up work
- Ensure issue is assigned to developer-agent and status is In Progress.
- Confirm Phase/Sprint fields are set.

## 1a) Acceptance-criteria capability guardrail (#3647)

Applies whenever developer-agent authors or edits an issue's acceptance
criteria — filing a follow-up issue, splitting scope, or proposing an AC
during implementation. Every checkbox must be something the dev sandbox can
actually execute and verify itself; otherwise the loop stalls on a step no
one but a human/EA can perform, with no signal that it's stuck.

- **Known sandbox gaps** (non-exhaustive — verify capability, don't assume):
  live `workflow_dispatch`/Actions-API dispatch or run inspection, repo
  **Settings** changes (branch protection, secrets, variables, webhooks),
  anything requiring a `gh` CLI invocation (prohibited by this runbook's own
  implementation instructions — see the "CRITICAL PROHIBITIONS" block in
  the developer-implement prompt), and any step needing credentials/tokens
  the sandbox isn't issued.
- **If an AC needs one of these anyway**, don't file it as a plain
  checkbox. Either drop it from the AC list (implementation + tests are
  enough to close the issue) and file a **separate** owner/EA-assisted
  follow-up, or keep it in this issue but mark it explicitly, e.g.:
  `- [ ] (owner/EA-assisted) Dispatch a live workflow_dispatch run and
  attach the run link as evidence.` The marker tells the review agent not
  to block acceptance on a step the dev sandbox structurally cannot do.
- **Context:** #3614/PR #3645 required live `workflow_dispatch` dry-run
  evidence as an unmarked AC; the dev agent's sandbox had no Actions
  dispatch capability (documented in its own commit `ae4160f5`), so the
  executive assistant had to run it manually with no guardrail flagging the
  gap ahead of time.

## 2) Branching
- Create a short-lived branch named with issue reference, e.g.:
  - `feat/<issue-id>-<slug>` or `fix/<issue-id>-<slug>`
- Base off the current active release branch.

### Never rebase (owner rule 2026-08-08, into the runbook 2026-08-15, #3801)

**Branches in this repository are never rebased.** Not `git rebase`, not
`git pull --rebase`, not `git rebase -i` to tidy history, not "just a quick
rebase onto the release branch". When your branch is behind or conflicted,
you **merge the release branch into your branch**:

```
git fetch origin
git merge origin/<release-branch>     # e.g. origin/v3.0.0
```

Why: a rebase rewrites commits that have already been pushed, reviewed and
commented on — review threads detach, the PR's own history stops matching
what the reviewer read, and every conflict is re-resolved once per commit
instead of once. One merge commit is the correct shape, and the merge commit
is what the review agent re-reviews.

Also: no force-push and no history rewriting on shared branches, and no
branch *stacking* (stacking is only ergonomic with rebase). Dependent work is
sequenced, not stacked.

The owner settled this on 2026-08-08 ("merge, don't rebase") and it was
recorded only in `product_management/AGENTIC_SDLC_DESIGN.md` — a forward-looking
design doc, not the runbook an agent actually follows. So agents kept
proposing rebases and the owner kept correcting them by hand ("I've said over
and over again not to rebase", 2026-08-15, #3801). It is operating doctrine
here now: a rebase in a PR is a review finding (review-runbook §3a), and prose
anywhere in this repo instructing one is a bug.

## 3) Implement
- Make smallest coherent change set that satisfies acceptance criteria.
- Add/extend tests (unit/integration as appropriate).
- Keep IO behind interfaces; maintain dependency flow.

### Fixing a defect: state the cause, then size the patch to it (#3821)

The authoring side of review-runbook §1d and §1e. The principles behind both
are stated in full in `CLAUDE.md` § Agentic First Principles — which is loaded
into your context automatically (ledger **V-028**), so this section is the
procedure, not a restatement.

Two things belong in the PR body of any change that **fixes** something:

1. **The cause, and what established it.** Not the symptom, not what the patch
   does — the mechanism, plus the log line, reproduction, bisect or failing run
   that showed it. If you could not establish it, say so plainly, say what is
   still unknown, and link the issue that will diagnose it: an honest
   mitigation passes the gate, a guess dressed as a fix does not. Three cycles
   (#3753 → #3788 → #3814) went to patching the same defect against a guess.
2. **The sweep, and its result.** Name the fault as a class — not "line 812
   breaks on apostrophes" but "an Actions expression interpolated into a
   `script:` body is JavaScript, not data" — then grep the tree for the
   construct and fix every instance, or report the search and say why the
   remainder is deferred (with an issue). #3500 → #3816 was fixed for a single
   author while every other author kept racing; the same question asked on
   #3801 turned one broken step into 47 (**V-027**).

The reviewer runs both gates and blocks on either (Medium). Answering them in
the PR body costs one paragraph; not answering them costs a review cycle.

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
  retry path are the reference pattern (both now carry that gate on their
  `comment_gate` job — see §3g).
- **Match a command token at line start, never as a substring.** A
  `contains(comment.body, '<TOKEN>')` test also matches prose that merely
  names the token, so an agent's own guidance comment can start the very
  workflow that posted it (#3706, #3790). Add a `comment_gate` job using
  `.github/actions/comment-token-gate` and depend on its verdict; see §3g.
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
- **Never interpolate an expression into a `script:` body (#3820).** In an
  `actions/github-script` step, `${{ }}` is substituted *before* the script
  is parsed, so the value becomes JavaScript **source**. Pass it through
  `env:` and read `process.env.NAME`, where it is data:

  ```yaml
  env:
    PHASE3_DIAGNOSIS: ${{ steps.claude_result.outputs.diagnosis }}
  with:
    script: |
      const diagnosis = process.env.PHASE3_DIAGNOSIS || '';
  ```

  This is not a style preference. The escalation step that reports fatal
  errors held `const d = '${{ ...outputs.diagnosis }}';`; an apostrophe in
  that prose — the norm, not an edge case — closed the literal and the step
  died with `SyntaxError: Unexpected identifier`, so the run's real failure
  went unreported (run 31959968196). It is also an injection surface: these
  steps carry an agent token, and the substituted text executes as whatever
  it parses as. Multi-line and free-form values (a diagnosis, a
  recommendation, a list of issue titles) are the dangerous ones, and a
  template literal is no safer than a quoted one — a backtick or `${` breaks
  out of it just the same.

  `scripts/agents/lib/workflow_script_guard.py` fails on any remaining
  instance tree-wide (run by `tests/unit/test_workflow_script_injection.py`
  and by `github-script-injection-smoke.yml`), so a reintroduction is caught
  at verification rather than at the next fatal error.

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
| `handle_acceptance_failure.yml` | `issue_comment` | issues/PR write, `DEV_AGENT_TOKEN` | `comment.user == HUMAN_OWNER`, on the `comment_gate` job | Reference pattern; gate moved to `comment_gate` by #3790 |
| `developer_auto_implement.yml` | `issues`(assigned), `issue_comment` | `contents`/`issues`/PR write, `DEV_AGENT_TOKEN` | assignee==DEV_AGENT (issues) / `author_association==OWNER` or `user.login` ∈ `{DEV_AGENT, REVIEW_AGENT}` on the `comment_gate` job (retry path) | #3647: extended to `REVIEW_AGENT` so `assign_and_trigger_developer`'s redispatch-fallback comment (posted whenever it has to unassign-then-reassign an already-assigned dev agent) actually starts a run. #3790: the comment path's actor + token tests moved to `comment_gate`, whose verdict `implement` requires |
| `scrummaster_sprint_reorg_apply.yml` | `issue_comment` | project field writes | `author_association==OWNER` + release-issue check | Unchanged |
| `acceptance_plan.yml` | `issues`(edited) | issues write | `github.actor==HUMAN_OWNER` + plan marker in body | Unchanged |
| `add-to-release-issue-on-milestone.yml` | `issues`(milestoned) | issues write (`GITHUB_TOKEN`) | none, but `milestoned` can only be produced by a user with write access — no public-actor path exists | Unchanged, no gate needed |
| `assign_backlog.yml` | `issues`(opened,reopened) | issues write (`SCRUMMASTER_AGENT_TOKEN`), adds an assignee | none besides `AGENTS_ENABLED` | Unchanged — write is scoped to adding scrummaster-agent as assignee on the triggering issue itself; no cross-resource write, no code exec, no merge |
| `ensure_project_hygiene.yml` | `issues`(opened), `pull_request`(opened,reopened) | issues/PR write (`SCRUMMASTER_AGENT_TOKEN`) | none besides `event_name` checks | Unchanged — writes only project fields/labels/milestone on the same issue/PR that triggered it; no cross-resource write |
| `auto-check-tasklist.yml` | `issues`(closed), `repository_dispatch` | issues write | none besides `AGENTS_ENABLED` | Unchanged — only checks a box on a tracking issue that already contains an unchecked `- [ ] #<closed-issue-number>` line placed there by scrummaster automation beforehand; an attacker can close only issues they already have permission to close, and gains no reference in a tracking issue they don't already appear in |
| `link_revert_pr_to_issue.yml` | `pull_request`(opened) | pull-requests write (`github.token`) | gated on `body` `startsWith('Reverts')` (attacker-controlled string) | Unchanged — re-verified during this audit: every write (`gh pr edit`, the informational comment) targets `github.event.pull_request.number`, i.e. the PR the attacker themselves just opened. Crafting a "Reverts owner/repo#N" body lets an attacker rewrite the body of *their own* PR to include a `Closes #ISSUE` line (extracted read-only from a real PR's linked issue) — this writes no resource the attacker doesn't already control, and any downstream merge/close of that PR is independently gated elsewhere. No actor gate added. |
| `notify-merge-conflicts.yml` | `pull_request`(opened,synchronize,reopened), `push`(release branches), `workflow_dispatch` | `REVIEW_AGENT_TOKEN` on the `resolve` job: issue/PR comments, issue **Status** writes, developer-agent reassignment (which starts a developer run), owner assignment on the escalation path | none on the trigger; **comment-content gate**: only comments authored by `{DEV_AGENT, REVIEW_AGENT, HUMAN_OWNER}` steer the routing decision | **Rewritten by #3801** — the old row ("notification only, no merge/code-exec", "issues write (comment only)", gate "none") is false for every cell since #3801. Why no trigger actor gate is needed: `push` to a release branch and `workflow_dispatch` both require write access, and the `pull_request` path only *reads* the PR (the job checks out `RELEASE_BRANCH`, never the PR head, so no attacker-authored code executes; fork PRs receive no secrets; workflow-level `GITHUB_TOKEN` is read-only). The real public-actor surface is comment **content**, not the trigger — `dispatch_conflict_resolution.sh` polls the thread, so a stranger's comment could otherwise forge round exhaustion, force an owner escalation carrying their text, or hold a PR in permanent cooldown. That is closed by the author gate in `conflict_resolution.decide()`, which is fail-closed (the CLI refuses to route without a trusted set) |
| `conflict_owner_escalation.yml` | `issue_comment`(created) | issues write, `REVIEW_AGENT_TOKEN`; assigns the owner + Slack DM | commenter ∈ `{DEV_AGENT, REVIEW_AGENT, HUMAN_OWNER}` on the `comment_gate` job + the shared anchored comment-token gate (#3790/V-011) | Added by #3801 — the single route from a merge conflict to the owner; follows the `handle_acceptance_failure.yml` reference pattern (actor + token tests both on `comment_gate`, whose verdict the privileged job requires) |
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

## 3e) Self-heal retry budget, same-signature disproof, and reachability-aware FIXED (#3689)

`developer_auto_implement.yml`'s Phase 0-3 self-heal chain (usage-limit
detection, deterministic classification, scripted fix attempts, then Claude
reasoning) auto-retries a failed run when it judges the error transient.
The 2026-08-09 #3687 incident showed the original design could loop
indefinitely: the retry cap was keyed to a label each Phase 3 diagnosis
invented fresh, and its "manual intervention resets the count" check
matched this workflow's *own* comments (posted via `DEVELOPER_AGENT_TOKEN`
under the real login `myGPT-developer-agent`, not `github-actions[bot]`),
so the cap never actually bound anything. Three fixes:

- **Unforgeable retry cap, keyed to (issue, failed step).** Every
  auto-retry comment carries a machine-readable marker:
  `<!-- nyxgpt-retry: step=<slug> sig=<hash> n=<N> -->`. The "Compute retry
  budget" step (`scripts/agents/lib/retry_budget.py`, called from
  `developer_auto_implement.yml`) counts markers for the current failed
  step since the last comment from `author_association == "OWNER"` --
  the same signal the workflow's own `RETRY_IMPLEMENTATION` trigger gate
  already uses for "human intervention" (§3b). Nothing else resets the
  count: not a fresh Phase-3-invented error-type label, not a `STATUS`
  change, not this workflow's own bot comments. The cap is 3, hard-coded in
  `retry_budget.MAX_RETRIES`. Pure logic lives in `retry_budget.py`
  (unit-tested in `tests/unit/test_retry_budget.py`); the workflow only
  does the `gh api`/marker-rendering glue, mirroring the `sprint_calc.py`
  pattern (#3480).
- **Same-signature disproof.** The signature is a hash of (failed step,
  normalized error excerpt). If the immediately preceding auto-retry's
  marker carries the same signature as the current failure, that
  empirically disproves "transient" -- a retry already happened and
  reproduced the identical failure. The Phase 3 prompt is told this
  up front (its "Step 0") and instructed not to write `STATUS=TRANSIENT`
  again; the "Auto-retry on failure" step also enforces it as a hard
  backstop for Phase 3 verdicts and for `retriable:test_failure` (flaky-or-
  deterministic test failures deserve a human look on the 2nd identical
  failure, not a 3rd blind retry). Deterministic rate-limit/network/
  stale-ref backoffs are intentionally excluded -- their signature is
  expected to legitimately repeat across successive waits.
- **Reachability-aware `FIXED` vs `FIXED_REQUIRES_MERGE`.** Phase 3 must
  reason about *where its fix landed* vs. *where the failing step executes
  from*, not just whether the fix is correct. `issue_comment`-triggered
  runs of this workflow always execute the workflow definition and any
  scripts it shells out to from the repository's **default branch** (§3c)
  -- never from whatever work branch a fix commit landed on. Before writing
  `STATUS=FIXED` for a code/script/workflow-file fix, Phase 3 checks
  `git merge-base --is-ancestor <FIX_SHA> origin/<RELEASE_BRANCH>`. If the
  fix isn't reachable, it writes the new terminal status
  `STATUS=FIXED_REQUIRES_MERGE` with `COMMIT_SHA` and `RECOMMENDATION`
  instead -- this never auto-retries; a dedicated step escalates
  immediately to the owner with the commit SHA and a cherry-pick/merge
  recommendation.

All three exhaustion paths (retry cap exceeded, same-signature forced
escalation, `FIXED_REQUIRES_MERGE`, and the pre-existing `FATAL`
classification) route through the standard escalation comment and now also
call `sprint_autopilot_kick` (`scripts/agents/lib/gh_project.sh`, #3480) --
previously only the review-agent's escalation path did this, so a
developer-side escalation could silently park the sprint-autopilot queue
instead of freeing it to move to the next issue.

**Human-channel (Slack) notification (#3695).** The 2026-08-09 #3513
incident showed a correct `FATAL`/`FIXED_REQUIRES_MERGE` diagnosis can sit
unread in the issue thread for hours if the owner is not actively watching
GitHub -- the "Sprint autopilot kick (developer-side escalation)" step that
follows all three exhaustion paths above also calls
`notify_human_escalation` (`scripts/agents/lib/gh_project.sh`) with the
firing step's one-line diagnosis and recommended action (e.g. "merge
`<sha>` to v3.0.0" for `FIXED_REQUIRES_MERGE`). This sends a Slack DM to
the owner via the existing `SLACK_BOT_TOKEN` + `SLACK_USER_ID` Actions
secrets (already configured for `notify-merge-conflicts.yml` -- no new
secrets). Missing secrets or a failed Slack call degrade silently to the
comment-only behavior that already existed; the GitHub escalation comment
is always posted regardless. Repeated firings for the same (issue, state)
within a 60-minute window are suppressed via a dedup marker comment
(`_slack_notify_recent`) so a retry loop cannot spam the channel.

Separately, the "Verify issue is In Progress" and "Verify issue is assigned
to developer-agent" gates are policy stops, not infra failures: they now
set a `gate_stopped` step output that the self-heal entry points
(`usage_limit`, `classify_error`, `retry_budget`) and the generic
"Post verification failure comment" step check before running, so a manual
circuit-breaker (moving an issue out of In Progress) reliably stops the
run without triggering a spurious Phase 1-3 diagnosis or a misleading
"Failed after 3 attempts" comment.

## 3f) Cross-issue infrastructure-anomaly collapse (#3694)

The 2026-08-09 postmortem (`product_management/AGENTIC_SDLC_DESIGN.md` §9;
issue #3694's "Problem / Motivation" carries the same account): a
runner-image change made `gh api search/issues` fail deterministically in
the "Check if PR already exists" step. Five issues were in flight, so the
self-heal chain ran five independent Phase 1-3 diagnoses against the same
infrastructure fault -- ~45-50 Claude invocations to re-derive the same
one-line diagnosis five times. A fixed global spend cap was explicitly
rejected as the fix (owner direction); the same step failing on *different*
issues within a short window is one infrastructure event, not N coding
problems, and the pipeline needed a way to see across issues.

- **Detection.** A new "Check cross-issue infra anomaly" step runs right
  after "Compute retry budget" (same gate: any genuine failure Phase 1
  classified), before Phase 2/3 spend anything. It calls
  `cross_issue_anomaly_decision` (`scripts/agents/lib/gh_project.sh`),
  which asks `scripts/agents/lib/cross_issue_anomaly.py decide` whether
  another issue already opened a matching, unresolved tracking-record
  marker for this exact failed step within the last
  `CROSS_ISSUE_ANOMALY_WINDOW_MINUTES` (default 60) on the release tracking
  issue.
- **One diagnosis, not N.** If no matching record exists, this issue
  becomes the origin: `open_cross_issue_anomaly` posts the tracking-record
  marker (`<!-- nyxgpt-anomaly: step=<slug> issue=<origin> opened=<epoch>
  -->`) on the release issue immediately -- before Phase 2/3 run, so a
  near-simultaneous failure on another issue can see it -- and Phase 2/3
  proceed normally for this (origin) issue. If a match already exists from
  a *different* issue, Phase 2, Phase 3, the auto-retry step, and the
  fatal-escalation step are all skipped (each step's `if:` also checks
  `steps.cross_issue_anomaly.outputs.matched != 'true'`); a short comment
  links this issue to the origin and its retry loop stops until the
  anomaly resolves.
- **No hidden state.** The tracking record is a comment marker on
  `RELEASE_ISSUE_NUMBER`, re-derived fresh from the live comment thread on
  every check -- the same level-triggered shape as `escalation_pause_gate`
  (#3687, above). Detection deliberately does NOT use
  `gh api search/issues` (the endpoint that caused the incident) -- it uses
  plain issue-comment REST calls, so detection itself can't be taken out by
  the same class of fault. It self-expires after the window elapses, or
  closes early on an OWNER-authored `RESOLVE_ANOMALY` comment.
- **Dispatch pause.** `cross_issue_anomaly_pause_gate`
  (`scripts/agents/lib/gh_project.sh`) composes with the #3687
  `escalation_pause_gate` in `scrummaster_dispatch_next.sh`: new dispatch
  pauses while any step has an open tracking record, with its own loud
  report on the release tracking issue, and resumes automatically once the
  anomaly resolves or expires. See `agents/runbooks/scrummaster-runbook.md`
  for the dispatch-side detail.
- **Replay criterion.** The 2026-08-09 scenario (5 issues x the same failed
  step within the window) now yields one diagnosis (the origin issue's
  Phase 1-3) and a dispatch pause, not five independent loops.

## 3g) Comment tokens are commands, not substrings (#3790)

Full reference: `docs/agent-comment-tokens.md`. The short version, because
this defect has now fired twice (#3706 on the kick token, #3790 on the retry
token):

- **Resuming a stopped developer run.** Move the issue back to `In Progress`
  and post a comment whose **first line is** `RETRY_IMPLEMENTATION` (nothing
  else on that line). The stop comment the agent posts deliberately does not
  spell the token out — an automated comment naming it is exactly what
  produced ~500 runs across #3782/#3784 on 2026-08-15.
- **A token counts only where it opens a line.** Fenced code blocks and
  quoted (`>`) lines are stripped first, so quoting an earlier comment
  cannot replay its command.
- **Agent prose that must name a token carries
  `<!-- nyxgpt-token-mention -->`**, which makes the whole comment inert.
  Prefer not naming it at all — point at this runbook instead.
- **Every comment-token workflow has a `comment_gate` job** using
  `.github/actions/comment-token-gate`; the job that does the work
  `needs: [comment_gate]` and runs only on `outputs.proceed == 'true'`. When
  you add a token trigger, add the gate too — `tests/unit/
  test_comment_token_triggers.py` fails if you don't.
- **Loop guard.** Three "stopping — not In Progress" cycles on one issue
  within 30 minutes escalate once and then go silent
  (`scripts/agents/lib/stop_loop_guard.py`); automatic retries on that issue
  are ignored for the rest of the 30-minute window, or until the repo owner
  comments — whichever comes first. An owner comment clears the halt at once;
  otherwise it lapses as the cycles age out of the window. The guard bounds
  spend to ~3 cycles per window, it is not a lockout.

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

Run this loop locally exactly as described — do not rely on `claude-code-review.yml`'s
in-review checks alone, since that job only runs `pytest tests/unit/` (non-blocking
`mypy`) as part of an agent-driven review, not the full `tests/` suite (#3502). A
standalone push/PR gate proposed for this repo (`docs/testing.md`'s "Standalone CI
gate" section — hand-carry doc pending owner application per #3502, same pattern as
#3454/#3479/#3480) runs the full suite plus a blocking `mypy src/` independent of
agent involvement once applied; until then, this local loop is the only thing that
exercises `tests/integration/` for a change.

**CRITICAL**: Keep working until all checks pass (like a human developer would).
Pre-commit hooks MUST pass before commit succeeds.

If flaky tests appear, isolate and fix; escalate if persistent.

## 4a) Executed verification: run your claim on the target (#3775)

§4's loop proves the code is well-formed and its logic is unit-tested. It
proves nothing about what happens when the thing runs on a real machine.
`ensurepip` exit 1 on stock Homebrew (#3753), `platform.mac_ver()` answering
empty (#3753 again), `npm` missing from a cloud image (#3761), artifact
installs resolving paths relative to a repo that is not there (#3759) — every
one of those shipped through a green §4 loop and a passing review, and every
one was found by the owner running the install once.

> **If your change's claim is about runtime, install or platform behavior, you
> produce the evidence that it was executed on that platform — as part of this
> issue, not as a follow-up.**

**Does the gate apply?** Yes if the change touches installs or packaging
(formulas, tarballs, wheels, venv bootstrap, dependency resolution), service
lifecycle (`nyxgpt ops` install/up/restart/down, launchd, systemd, container
start, health), provisioning or deployment (Terraform, cloud-init, EC2,
Kubernetes, Compose), cross-platform or OS-specific behavior, or anything that
depends on what exists on the target machine (interpreter version, `npm`,
`brew`, `docker`, filesystem layout, repo-relative path resolution).

No if the change is pure logic fully covered by unit tests, or prose-only.
That exemption is the gate's point, not a loophole: it targets exactly the
behavior claims unit tests structurally cannot reach.

**Produce one of these, and cite it in the PR body** (run URL, workflow + job
name, or the command with its output):

1. a run of the smoke workflow that covers your path —
   [`macos-brew-smoke.yml`](../../.github/workflows/macos-brew-smoke.yml)
   (Homebrew keg install on a real macOS runner),
   [`linux-native-smoke.yml`](../../.github/workflows/linux-native-smoke.yml)
   (`nyxgpt ops install` on a real systemd userland),
   [`terraform-local-smoke.yml`](../../.github/workflows/terraform-local-smoke.yml)
   (local Terraform apply);
2. a `workflow_dispatch` run of one of those, or of another workflow that
   exercises the changed path;
3. `nyxgpt ops verify` (§2 of the review runbook) where your change is in its
   coverage;
4. the command itself, run on the target, with its output pasted in.

**If no job covers your path, add one.** That is part of the implementation,
not a follow-up issue. Copy the shape of the templates above: path-filtered
triggers so the cost lands only on PRs that can break it,
`permissions: contents: read` for a read-only smoke, and a header comment
saying which question the job answers.

**Inject the condition when the runner is green by luck.** A job that only
runs the install passes on every machine that fails to reproduce the bug —
which is how the rc5 candidate passed CI and died on the owner's Mac. When
your fix targets a condition the runner does not naturally exhibit, prove both
halves: the failure reproduces without the fix, and disappears with it. See
`macos-brew-smoke.yml`'s "Reproduce the empty mac_ver() failure, then prove
the shim fixes it" step.

**What genuinely cannot be executed in CI** is the short documented list in
`docs/live-verification-ci.md` (the native launchd/brew-services *operate*
path, real Slack delivery, LLM answer quality), plus EC2 Mac hardware, which
has no hosted runner (`docs/portability-matrix.md`). Name which item applies
and what the owner must exercise — and prefer injecting the condition over
deferring to that list. Note what is *not* on it: the Homebrew keg install
runs on a real `macos-15` runner in `macos-brew-smoke.yml`, so a formula
change cannot claim macOS is untestable.

The reviewer runs the same gate (`agents/runbooks/review-runbook.md` §1c) and
missing executed evidence is a Medium (blocking) finding.

## 5) Documentation
- Update docs for any user-facing change:
  - Modified `src/nyxgpt/api.py` or `src/nyxgpt/app.py` → Update `docs/api.md`
  - Modified `src/nyxgpt/cli.py` → Update `docs/cli.md` (and `docs/configuration.md` if config changed)
  - Added/changed config options → Update `example.config.ini` AND `docs/configuration.md`
  - Added new features → Update the owning `docs/*.md` and, if the feature is
    user-visible, the feature overview in `docs/README.md`. **Never add feature
    detail, install matrices, command listings, or world-state claims to
    `README.md`** — it is a pointer layer by owner decision (#3743): identity,
    a minimal install pointer, and the docs index only.
- Update architecture notes only if human-approved architecture change is required

### Inverse-claims sweep: fix what your change makes untrue (#3744)

Updating the docs for your change is only half the job. Also ask: **does this
change falsify something already written elsewhere in the tree?** Prose that
your change turns false is your change's bug, even when it lives in a file you
never opened.

Before you submit, for the capability/behavior/constraint you added, removed
or inverted, grep the *whole* tree for existing assertions about it —

```bash
grep -rin "<capability terms>" README.md docs/ agents/ CLAUDE.md \
  product_management/ src/ web/src/
```

— using the capability's own vocabulary plus expiry phrasing ("not yet",
"does not support", "currently", "still required", "will ship"). Fix every
sentence the merged state makes false, in this PR. The reviewer runs the same
check (`agents/runbooks/review-runbook.md` §1a) and an unfixed falsified claim
is a Medium (blocking) finding.

**Do not write world-state claims with built-in expiry.** "This has not
shipped in a PyPI release yet", "the currently published version is X", "a
repo checkout is still required", "planned for Phase N" are true only until
someone lands the work — and the PR that lands it will not be looking at your
sentence. Point at a living source instead: the PyPI project page,
`docs/portability-matrix.md`, generated command/API docs, the release tracking
issue. The reviewer flags a newly introduced expiry-dated claim as a Medium
finding.

**Motivating incident (#3743, 2026-08-13):** #3727/#3735 shipped repo-less
PyPI publishing while `README.md` still asserted that it had not shipped and a
checkout was still required. Both dev and reviewer passed the diff-scoped docs
criterion honestly — the falsified section simply was not in the diff.

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

## 8b) Review huddle protocol (owner-ratified 2026-08-09, #3687)

Not every REQUEST_CHANGES round is a "fix and resubmit": the review agent
classifies each round as (a) verifiable defect, (b) judgment call, or (c)
spec ambiguity (see `agents/runbooks/review-runbook.md` §6b for the full
taxonomy). Type (c) escalates immediately — nothing for you to do. Type (b),
or type (a) on its 2nd unresolved cycle, triggers a **huddle** instead of
another fix cycle:

1. `review_agent_auto_review.yml` posts a `HUDDLE_TRIGGERED` comment on the
   PR instead of reassigning the issue for another fix.
2. `developer_huddle_position.yml` runs you with a narrow job: read the PR
   thread and the linked issue, then post **one** `## Developer Position`
   comment covering what you believe the problem is, what was tried, and
   what you propose (proceed / a specific different approach / a specific
   descope / escalate). **Do not attempt a fix in this run** — post the
   position, then post a second comment containing exactly
   `HUDDLE_MEDIATION_REQUESTED`.
3. A fresh scrummaster invocation (`scrummaster_huddle_mediation.yml`) reads
   your position and the review's position (its review comment) and posts a
   `## Huddle Decision` comment: `HUDDLE_DECISION: proceed|change-approach|
   descope|escalate`.
4. `huddle_decision_dispatch.yml` starts the next fix cycle for you on a
   proceed / change-approach / descope decision: the issue goes back to **In
   Progress** and is reassigned to you, with a `HUDDLE_DECISION_DISPATCHED`
   comment on the PR (#3736). You do not wait for anything else, and the
   3-cycle counter is re-armed at that point — the rounds that led to the
   huddle no longer count toward escalation.
5. When that fix cycle runs (`developer_auto_implement.yml`'s "Run
   Claude Code to fix review issues" step), check for a `HUDDLE_DECISION:`
   comment on the PR first (Step 1.5 in that prompt) and **execute the
   agreed plan** — proceed with the original fix, follow the stated
   different approach, or perform the stated descope (e.g. delete a named
   flaky test, split off a follow-up issue) — rather than deciding
   independently. If the decision was `escalate`, this cycle should not
   normally run at all; if you see it anyway, do not push a speculative fix.

The existing 3-cycle outer breaker (§8, review-runbook §6) is unchanged —
the huddle changes what happens *between* cycles 2 and 3, not the limit
itself.

## 8c) Merge-conflict resolution rounds (owner rule 2026-08-15, #3801)

A PR that goes CONFLICTING because the mainline moved under it is **your**
work, not the owner's. Owner rule: *"merge conflicts shouldn't halt progress
and shouldn't be escalated to me unless there's truly a decision to be made
only I can make."* (Before this, the handler's only move was to assign the
owner — on 2026-08-15 nine merges landed on the release branch in one
afternoon, four In Review PRs went stale, and the owner was interrupted
twice in an hour for conflicts containing no owner judgment at all.)

**How a round reaches you.** Every conflict entry point — the PR events, a
push landing on the release branch, and the merge script finding a
conflicted-but-approved PR — routes through
`scripts/agents/dispatch_conflict_resolution.sh`. It returns the issue to
**In Progress**, reassigns you, and posts the resolution context on both the
PR and the issue. The Slack notification still fires: the owner hears about
the conflict, they are simply not assigned it.

**What you do:**

1. `git fetch origin` and **merge `origin/<release-branch>` into the PR
   branch. Never rebase** (§2).
2. Resolve with judgment, not mechanically. Read what each side was trying to
   do: keep this PR's feature content **and** the behavior the owner has
   already accepted on the release branch. Neither side is discarded
   wholesale.
3. **`agents/LEDGER.md` needs two specific things.** It is a *split* file:
   some entry IDs are absent by design (relocated to the owner's private
   annex), so never "restore" a missing ID or renumber to close a gap. And if
   your entry ID collides with one the mainline allocated while you were in
   review, **renumber yours** to the next unused number in that class and keep
   both entries — IDs are never reused.
4. Re-run the full verification suite (§4) and push the merge commit. That
   push re-triggers review. A conflict round can be the *whole* job of a run:
   if the review findings were already fixed and only the conflict remains,
   resolve, validate, push.

**Escalating (rare).** Only when resolution needs a decision only the owner
can make — two owner-accepted behaviors in genuine semantic contradiction,
where either choice silently reverses something they accepted. Then post one
PR comment whose **first line is exactly** `CONFLICT_REQUIRES_OWNER_DECISION`,
followed by the specific question: both behaviors, the file and lines, and
what each choice costs. `conflict_owner_escalation.yml` assigns the owner and
DMs them that question. Never issue the token to buy time on a fiddly merge;
"this is hard" is not an owner decision. The only other route to the owner is
automatic: `CONFLICT_MAX_ROUNDS` (default 3) rounds that fail to converge.
