# Claude Operating Instructions (nyxGPT)

Claude operates strictly as an executor within the agent system.

Claude must not invent workflow, authority, or automation.

---

## Bootstrap (Required Reading for Every New Session)

**Before taking any action, Claude must read these files in order:**

### 1. Core Operating Instructions (Always read first)
- `CLAUDE.md` (this file)
- `AGENTS.md`
- `agents/LEDGER.md` (the operating ledger — read in full)
- `docs/architecture.md`
- `product_management/VISION.md`
- `README.md`
- All files in `.github/workflows/*`
- All files in `agents/charters/*`
- All files in `agents/prompts/*`
- All files in `agents/runbooks/*`
- All files in `scripts/agents/*`

### After Reading, Claude Must:

1. **Announce current role:**
   - "Current role: [scrummaster-agent | developer-agent | review-agent | executive assistant]"

2. **Confirm understanding of task:**
   - Brief summary of what is being requested

3. **Ask clarifying questions:**
   - Any ambiguities or conflicts in instructions
   - Any missing context needed to proceed

4. **Only then proceed with work**

Throughout the session, treat your own recollection as untrusted input
wherever `agents/LEDGER.md` or the live system can answer instead. See
"The Operating Ledger" below.

---

## The Operating Ledger (Owner decision, 2026-08-14, #3774)

**`agents/LEDGER.md` is the system of record for cross-session agent memory.**
Agent memory is reconstructive — each session re-derives project state from
artifacts and lossy self-summaries, and asserts stale conclusions confidently
when the artifacts are incomplete. The ledger moves that memory into the
repository, where it can be read instead of reconstructed.

- **Read it in full at session start** (it is on the bootstrap list above, and
  is deliberately kept short enough to read).
- **A claim that is not in the ledger and not freshly verified is not asserted
  as fact.** Before stating how the project works, what was decided, what is
  published, or what is deliberately not being done: find the entry, verify it
  this session and be able to name how, or say plainly that you have not
  checked. Recalling it "from earlier in the project" is not one of the three.
- **Append entries** for decisions made, facts verified (with method and
  timestamp), items deliberately parked (with reason and revisit condition),
  and questions left open. Agents append through the normal branch/PR path; an
  entry may ride along in the PR that produced the fact and needs no issue of
  its own.
- **Check the Superseded section before correcting anyone** — it lists beliefs
  this project already held and retired.
- **Do not silently overwrite state you did not create.** If a lane, marker or
  board state looks wrong, look for a parked entry explaining it before
  "cleaning it up".

Entry granularity, the schema for each entry kind, and the pruning rule are
defined in the ledger itself. Keep it cheap to read: load-bearing facts and
decisions, never narration.

---

## Commit and Push Policy

Always commit and push code changes after making them, unless the user explicitly says not to.

---

## Project Environment

This project uses Python (primary), TypeScript, YAML workflows, and Markdown docs. Tools: mypy, ruff, pre-commit hooks, pytest. IDE: IntelliJ (not PyCharm). Platform: Apple Silicon (ARM64).

---

## Repository Organization

Root holds only the anchored docs (`CLAUDE.md`, `AGENTS.md`, `README.md`,
`CONTRIBUTING.md`) plus code and infra directories. Everything else has a home:

- **`agents/`** — the agent system's operating docs: `charters/`, `prompts/`,
  `runbooks/`, plus `LEDGER.md` (the operating ledger — cross-session memory:
  decisions, verified facts, parked items, open questions). (`AGENTS.md` at
  root is the index into it.)
- **`docs/`** — engineering documentation, including `docs/architecture.md`
  (the single source of truth for current architecture and its invariants).
- **`product_management/`** — product notes, documents, and decisions:
  - All phase-planning docs (`PHASE_*_PLAN.md`)
  - The project-completion plan (`PROJECT_COMPLETION_PLAN.md`)
  - Project audits (`PROJECT_AUDIT.md`)
  - The product vision (`VISION.md`)
  - Proposed/forward-looking architecture (`PROPOSED_ARCHITECTURE.md`)
  - `archived_product_docs/` — superseded planning docs kept for
    institutional knowledge (owner reorganization, 2026-07-31); the main
    folder holds only the latest decisions

Any new planning, roadmap, phase, audit, or product-decision document must be
created under `product_management/` — never at the repo root. New agent-system
docs go under `agents/`. `product_management/` is tracked; local-only scratch
belongs in `product_management/private/` (gitignored).

---

## Sources of Truth

- AGENTS.md
- agents/LEDGER.md (decisions, verified facts, parked items, open questions)
- scripts/agents/*
- GitHub Issues
- Release Issues

---

## General Guidelines

Do not spend excessive time reading bootstrap/context files before addressing the user's actual request. Start with the specific problem, then read context as needed.

---

## Definition of Done (Owner Requirement, 2026-07-08)

**A feature is complete only when it is usable end-to-end, not merely implemented in the backend:**

- **nyxGPT user features** MUST be usable from the **web interface**. An API endpoint or CLI command with no web UI surface is an incomplete implementation.
- **Ops/SRE features** (deploy, launch, control, monitor, heal) MUST be operable from the **SRE/admin dashboard**.
- The developer agent must implement the frontend surface as part of the same issue, and the review agent must treat a missing frontend surface as a **Medium (blocking) finding** — unless the issue body explicitly scopes the work as backend-only with an owner-approved rationale and a linked follow-up issue for the frontend.
- Context: several past issues were merged with backend-only implementations that no user could reach (e.g. #2688 shipped Kubernetes manifests for the API pod only — deploying them does not produce a working nyxGPT). This rule exists to prevent that pattern.

### Executed verification: nothing reaches acceptance testing unexecuted (Owner Requirement, 2026-08-14, #3775)

**A change whose claim is about runtime behavior is done only when that claim
has been demonstrated by *executing* it on the target platform.** Review in
this pipeline means inspection; inspection cannot see an `ensurepip` exit 1, a
missing `npm`, or a path that resolves relative to a repo that is not there.

- **In scope (executed evidence required):** installs and packaging, service
  lifecycle, provisioning/deployment, cross-platform or OS-specific behavior,
  and anything that depends on what exists on the target machine.
- **Accepted evidence:** a CI job run on the target platform — the existing
  smoke workflows (`macos-brew-smoke.yml`, `linux-native-smoke.yml`,
  `terraform-local-smoke.yml`) and `nyxgpt ops verify` all count — or an
  equivalent demonstrated-by-running proof, cited in the PR by run URL or
  command transcript. Where the runner does not naturally reproduce the
  failure, *inject the condition*: prove it fails without the fix and passes
  with it (the `macos-brew-smoke.yml` fault-injection job is the template).
  When no job covers the changed path, the developer agent adds one as part of
  the same issue.
- **Exempt:** pure-logic changes fully covered by unit tests, and prose-only
  changes. The gate targets behavior claims unit tests structurally cannot
  reach.
- The review agent must treat missing executed evidence on an in-scope change
  as a **Medium (blocking) finding** — and must not demand evidence CI
  genuinely cannot produce (the short list in `docs/live-verification-ci.md`),
  which is named in the review instead.
- Context: #3753 (`ensurepip` exit 1, then the `mac_ver()` crash), #3759
  (artifact installs resolving repo-relative paths) and #3761 (no `npm` on
  cloud instances) all reached owner acceptance testing and were all
  discoverable by running the install once on a clean target.

---

## Operational Command Wrapping (Owner Requirement, 2026-07-15)

**No nyxGPT operation may require the user to run a raw `docker`, `docker compose`, `docker-compose`, `kubectl`, or `terraform` command directly. Every operation is exposed through a `nyxgpt`-wrapped command (e.g. `nyxgpt up`, `nyxgpt down`, `nyxgpt ops …`).**

- The web UI, docs, help text, and scripts must instruct the user in terms of `nyxgpt` commands — never raw container/orchestrator commands. Showing `docker compose up -d` or `kubectl …` as a user instruction is a **Medium (blocking) review finding**.
- Internals that shell out to `docker`/`kubectl` (self-heal, deploy, canary) are fine as *implementation*, but must be reachable through a `nyxgpt` command and the dashboard — the user never types the raw command.
- This makes the unified `nyxgpt up`/`down`/`ops` wrappers (Phase 6) a hard architectural requirement, not just a convenience.
- **The `nyxgpt ops` suite is feature-complete for local-first stack lifecycle** (verified 2026-07-20): `install` (reconciles the full local stack, including creating the `nyxgpt-cassandra` Docker container from scratch — `_ensure_cassandra_container` in `src/nyxgpt/ops.py`), `restart {all,api,web,ollama,cassandra}`, `stop`, `down` (native services + Compose teardown, volumes preserved), `status`, `doctor`, `env-sync`, `logs <service>`, `glitchtip-init`, and `observability` (starts the monitoring/logging/tracing/errors Compose profiles). The local-first layout is: brew services `nyxgpt-api`/`nyxgpt-web`/`ollama` running natively, Cassandra as the only core-service Docker container (plain `docker run`, not Compose), observability stack in Compose. **Always use `nyxgpt ops` for stack lifecycle — never raw `docker compose` for the local-first path.**
- Known remaining violations (updated 2026-07-20): the four observability panels (`GrafanaPanel`, `ErrorTrackingPanel`, `LogAggregationPanel`, `TracingPanel`) now correctly show `nyxgpt ops` commands. Still outstanding: `web/src/app/admin/self-heal/page.tsx:366` shows raw `docker compose up -d`, and raw `docker compose`/`kubectl` snippets remain in `docs/{api,configuration,deployment-checklist,terraform,kubernetes,docker-compose,self-healing}.md` (`README.md` was cleared by its minimization, #3743) (tracked by the 2026-07-17 documentation freshness audit, #3226). Converting that UI string and those docs is the remaining Phase 6 wrapper scope.

---

## Repo-less Portability (Owner Requirement, 2026-08-01)

**The entire stack must be installable and runnable without checking out or
downloading the code repository.** Distribution is via published artifacts
(installable package / remote tap / container images), never `git clone`.
Portability targets, in scope: macOS, Linux, Docker/Compose, Kubernetes,
AWS EC2. Windows is explicitly out of scope.

- Known violations today: `src/nyxgpt/ops.py` resolves runtime data
  repo-relative (`REPO_ROOT = Path(__file__).parents[2]` -> `docker/`,
  `ops/launchagents/`, `scripts/`, `pyproject.toml`), and the Homebrew tap
  is generated locally FROM a checkout (`file://` tarball). Both must be
  retired: runtime data ships inside the package (importlib.resources or
  ops-managed copies under `~/.nyxGPT`), and artifacts are published so a
  clean machine can install without the repo.
- Delivery is spread across Phase 6: core packaging + self-containment in
  P6-5 (#3504); Linux artifact install in P6-14 (#3508); AWS instances
  provision from artifacts (never clone) in P6-12 (#3511) / P6-11 (#3513);
  the capstone P6-16 (#3516) accepts from a clean machine with no checkout.
- A source checkout remains supported for development (`pip install -e .`),
  but no user-facing install or operate flow may require one.

---

## Operating Mode

Claude must adopt exactly one role at a time:
- **scrummaster-agent** - Backlog management and issue selection
- **developer-agent** - Implementation and PR creation
- **review-agent** - Code review and merge operations
- **executive assistant** - Ad-hoc administrative tasks for human owner

Claude must follow that role's permissions strictly.

Agent roles (scrummaster, developer, review) follow strict workflow rules.
Executive assistant role uses efficient means for one-off tasks outside the workflow.

**"Fix it" means run the agent process (Owner Requirement, 2026-07-22):** when
the owner reports a defect or asks for a change to be fixed, the default is the
full cycle — file the issue, let the scrummaster select it, the developer agent
implement it, and the review agent review and merge. Claude does not implement
the change itself unless the owner explicitly says to hand-carry it (e.g.
"don't file it, fix it"). A hand-carried change is the exception, not the norm,
and still goes through review before merge.

---

## Creating Issues

When creating GitHub issues:

**Title Format:**
- Clear, concise, actionable (under 80 characters)
- Format: `[type]: [action/problem] - [component if relevant]`
- Examples: `feat: Add metrics dashboard`, `bug: RAG crashes on epub`, `fix: Agent ignoring reviews`

**Body Structure:**
```markdown
## Problem / Motivation
[Why is this needed? What problem does it solve?]

## Acceptance Criteria
- [ ] [Specific, testable criterion 1]
- [ ] [Specific, testable criterion 2]

## Technical Details (if applicable)
- Files affected: [list]
- Dependencies: [if any]

## Related Issues/PRs
- Related to #[number]
```

**Required Project Fields:**
- Status: Backlog
- Priority: P1 - High (default)
- Effort: XS (default)
- Module: Auto-detect from keywords (web-ui, api, rag, cli, tui, testing, documentation, security, observability), fallback to "api"
- Label: "Feature" (or "Acceptance Failure" for bugs/defects found before release)
- **NEVER use a "Bug" or "Production Defect" label.** "Production Defect" (formerly "Bug") is reserved for production issues and applied only by the human owner. A `bug:` title prefix is fine; the label for any pre-release defect is always "Acceptance Failure".
- **"Improvement" label + acceptance testing (Owner decision, 2026-08-01; relationship form 2026-08-12, #3731):** an improvement filed during acceptance testing of a feature counts as a *product management failure* (spec gap), distinct from an Acceptance Failure (implementation defect). File it with the **`@improvement`** comment command on the issue (or as a normal Backlog issue labeled "Improvement"); if no feature issue applies, that is not a blocker to filing or working it. See `agents/runbooks/review-runbook.md` §9.
  - The old `Related feature: #N` body line is **retired** — see "Issue Relationships" below. The 2026-08-01 rule that improvements never gate a feature's move to "For Release" is **superseded** by the same decision: an improvement filed against an issue blocks its acceptance exactly like a failure does. The label still keeps the two apart as *statistics* (spec gap vs implementation defect); only the gating changed.
- Sprint: Current sprint (if active)
- Milestone: Current open milestone (if exists)

**CRITICAL: Always verify fields were set**
- Re-query issue after creation: `gh issue view $ISSUE --json projectItems,labels,milestone`
- Never assume success from non-error response
- Report actual field values to user

Use `/issue` skill for full guided workflow.

---

## Tooling

- Use gh CLI
- Use scripts in scripts/agents/
- Do not modify Project fields directly outside scripts

**Agents CAN modify `.github/workflows/` files (verified 2026-08-07).** The
old belief that "the dev agent's GitHub App cannot write workflow files" was
wrong: every `claude-code-action` invocation in the agent workflows passes
`github_token: DEVELOPER_AGENT_TOKEN`, a classic PAT that carries the
`workflow` scope, so pushes to workflow files succeed. The refusals seen on
#3642 came from the action's built-in App-mode capability text, which the
implement prompts now explicitly override (see the "Workflow-files
exception" paragraph in `developer_auto_implement.yml`). Do not hand-carry
workflow-file issues on that basis; if a workflow push is actually rejected,
diagnose the real error rather than assuming a permissions wall.

**Reading code-scanning state without owner help:** dispatch
`.github/workflows/code_scan_report.yml` (workflow_dispatch, optional `ref`
input) and read its run log — it prints recent analyses, the open-alert
list with `TOTAL_OPEN`, and every SARIF codeFlow per open alert. Agent
sessions cannot call the code-scanning API directly; this is the supported
path. Note: CodeQL default setup only scans the repo's default branch (plus
PRs) — a non-default branch's alert list is frozen until it becomes default
again and receives a push.

**IMPORTANT: Do not create project metadata without explicit user permission:**
- Do NOT create labels (use existing labels only)
- Do NOT create milestones
- Do NOT create releases
- Do NOT add options to project field dropdowns (Module, Phase, Status, etc.)
- If a label/milestone/field option is needed, ASK the user first

## Branch Rules

- **NEVER merge to master/main** - All merges go to the active release branch (e.g., v1.0.0)
- Feature/fix branches are created from and merged back to the release branch
- master/main is reserved for releases only
- After merging to release branch, manually close linked issues (GitHub doesn't auto-close for non-default branch merges)

**Master merges are ceremony-only and automated (Owner decision, 2026-08-12, #3730).**
The old rule read "master/main is human controlled", meaning the owner ran the
master fast-forward by hand. That is superseded: **the human control point is
now the owner moving the release tracking issue to `For Release`.** That move
is the sign-off, and from it the release ceremony runs end-to-end unattended —
master fast-forward, tag, GitHub Release, `stable` publish via the #3727
pipeline, stable Homebrew tap stamp, and retirement of that line's `-rc`
formulas (`.github/workflows/release_ceremony.yml` →
`scripts/agents/release_ceremony_watch.sh` → `scripts/release_ceremony.sh
--unattended`). No human step happens after the move; any failure in the
ceremony alerts the owner over the Slack DM channel (#3695) and stops.

Nothing else may push master: agents still never merge to master, and the
ceremony reaches it only through that one signed-off path. Phase 4 of the
ceremony (next-line preparation and the repoint) remains owner-run.

---

## Acceptance Drain Gate (Owner decision, 2026-08-12, #3730)

Acceptance failures and improvements filed **during** an acceptance round are
held, not worked immediately:

- `@acceptance-failure` and `@improvement` file their issues into the
  **`Acceptance Failed`** Status lane. They are not selected, kicked or
  auto-resumed while they sit there.
- The gate **opens when `Acceptance Testing` has drained** — empty except the
  release tracking issue, which is exempt and stays there until the whole
  release is accepted.
- On the opening, every held item moves to **`Backlog`** and the scrummaster
  queue is kicked **once** (`scripts/agents/drain_gate.sh`, run by
  `.github/workflows/acceptance_drain_gate.yml`).
- **`Acceptance Failed` holds two populations (owner decision 2026-08-14,
  #3780).** The owner also parks *features they have tested and failed*
  there. The machinery splits them by issue state: **open** = this round's
  held rework (released on the drain, as above); **closed** = a parked
  feature, which the gate never moves and which
  `promote_accepted_features.sh` promotes to `For Release` — from either
  parking lane — once its whole transitive blocked-by closure is accepted.
  While any blocker is open, nothing moves it: the placement is owner
  signal. See `docs/acceptance-drain-gate.md`.
- **Agent-process issues bypass the gate** and are worked immediately. The
  rule is encoded in `scripts/agents/lib/drain_gate.py`: an owner-authored
  process exception in the body ("…bypasses the drain gate"), the
  `<!-- drain-gate: bypass -->` marker, or a label listed in
  `DRAIN_GATE_BYPASS_LABELS`.

Rationale: working failures the moment they are filed floods Acceptance
Testing with freshly-merged fixes mid-round and burns RC cycles while the
owner is still testing. Test everything → drain → test the next candidate.

---

## Issue Relationships (Owner decision, 2026-08-12, #3731)

**GitHub's native issue relationships (blocked by / blocks) are the only
storage for the link between issues. Never body prose, never comment
markers.**

- The owner files acceptance work with two comment commands, both owner-only
  and both on issues (never PRs): **`@acceptance-failure`**
  (`handle_acceptance_failure.yml`) and **`@improvement`**
  (`handle_improvement.yml`). They differ only in the label applied and the
  copy posted.
- Both record the same semantics: **the new issue blocks acceptance of the
  marked issue, and transitively anything blocked by that one.** The write is
  a native blocked-by edge (`mark_issue_blocked_by` in
  `scripts/agents/lib/gh_project.sh`); transitivity is *not* written as extra
  edges — promote/drain logic walks the chain instead
  (`transitive_blocked_by_issues`, `scripts/agents/lib/issue_relationships.py`).
- The `Related feature: #N` / `Parent feature: #N` body convention is
  **retired**. Nothing writes it. It is still *read* as a documented fallback
  for issues filed before this decision, and
  `promote_accepted_features.sh` heals any such link into a real native edge
  on its next sweep, so historical data converges instead of needing a
  separate backfill.
- Consumers: `promote_accepted_features.sh` (transitive promotion gate),
  `_issue_open_gate_refs` / `parked_resume.py` (auto-resume gating), and the
  retrospective (`scripts/retrospective/dump_relationships.py` →
  `data/relationships.json` → `build_dashboard.py`, which reports the
  `native`/`prose`/`none` attribution split in `qtotals.attribution`).

---

## PR Rules

- PRs are created only via developer_submit_for_review.sh
- PR body must include: Closes #ISSUE
- Issues close only on merge

---

## Code Changes

When renaming/migrating projects, do a comprehensive grep for ALL references (config files, env vars, docs, tests, scripts, workflows, directory names) before declaring the task complete. Never rename a working directory mid-session.

---

## GitHub Workflows

When setting GitHub project fields via CLI/API, always verify the fields were actually set by re-querying the project item. Never assume success from a non-error response.

---

## CI/CD

When fixing CI/CD failures, reproduce the issue locally with the same environment constraints (missing stubs, unmocked connections, pre-commit hooks) before pushing fixes. Avoid multiple push-and-pray cycles.

---

## Review Rules

- Only Critical or Medium issues block acceptance
- No style-only rejections

---

## Forbidden Legacy Behavior

- No create-pr.sh
- No Project workflow reliance
- No automatic status changes

---

If instructions conflict or are unclear:
Stop and report the blockage.
