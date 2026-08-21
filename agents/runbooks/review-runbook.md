# Review Runbook (review-agent)

## 0) Preconditions
- PR targets active release branch.
- The head's required checks are green — **and that is not yours to check**
  (#3971). A head with a failing required check is refused at submit and
  handed back to the developer; a head whose checks are still pending is
  waited on. You are only ever invoked on a head that passed that gate. See §3.
- The PR is linked to a valid issue (GitHub's closing-issue link, however it was created).

### Project hygiene
Every assignee is responsible for verifying project hygiene before reassigning:
- PRs must be linked to issues natively; `Closes #ISSUE` in the body is one way to create that link, not a required sentence (owner rule, 2026-08-19)
- Issues must have all required project fields populated
- Merged PRs without linked issues must be corrected
- Project fields must be accurate before state transitions

### Review Trigger (review-agent ownership)
The review workflow is triggered when review-agent is assigned as reviewer:
- **Automatic**: When `developer_submit_for_review.sh` assigns review-agent as reviewer
- **Manual re-trigger options**:
  - Push new commit to PR branch (triggers on synchronize)
  - Comment `@review` on the PR
  - Dispatch-mode recovery: `scripts/agents/manually_trigger_pr_review.sh <N>`
    (see §5a — always use the script, not a bare `gh workflow run`)

The review-agent OWNS the review process:
- Review workflow uses `REVIEW_AGENT_TOKEN` for all GitHub operations
- Review comments are posted by review-agent (claude[bot])
- Review-agent orchestrates the auto-fix loop (developer-agent executes fixes)

## 1) Review checklist

**IMPORTANT:**
- Do NOT run the CI gates, and do NOT read the check list. The gate in §3
  already answered it; the review agent stopped re-running the gates on
  2026-08-18 and stopped reading them in #3971
- Review ALL changed files in the PR (not just new changes from current cycle)
- This ensures comprehensive quality coverage across the entire codebase

### Core Requirements (from project standards)
- Correctness vs issue acceptance criteria
- Tests added/updated and meaningful
- No architecture boundary violations
- No secrets committed
- Clear docs updates for user-facing changes
- **Inverse-claims check (#3744):** the change must not leave *falsified*
  claims anywhere else in the tree, including files the diff never touches.
  See §1a — a falsified claim found and left unfixed is a Medium (blocking)
  finding.
- Reasonable maintainability
- **End-to-end usability (Definition of Done, CLAUDE.md):** nyxGPT user features must be usable from the web interface; ops/SRE *state* must be observable from the SRE/admin dashboard, while ops/SRE *lifecycle* is operated from the CLI and named on the dashboard as a text pointer (owner decision 2026-08-16, #3804). A backend-only implementation is a Medium (blocking) finding unless the issue explicitly scopes it backend-only with owner approval and a linked frontend follow-up issue — and so, in the other direction, is a **new acting control on a substrate the dashboard itself runs on**.
- **Executed-verification gate (#3775):** a change whose claim is about
  runtime, install or platform behavior must be demonstrated by *execution on
  the target platform*, not by inspection. See §1c — missing executed evidence
  on an in-scope change is a Medium (blocking) finding.
- **Workflow actor gates (#3600, going-public hardening):** any new or edited `.github/workflows/*.yml` job triggered by `issues`, `issue_comment`, `pull_request`, or `pull_request_review*` that carries write permissions or a secret-backed `GH_TOKEN` MUST gate its `if:` on the actor's identity (`comment.user.login`/`review.user.login` against `vars.HUMAN_OWNER` or the relevant agent var) — a trigger phrase with no author check is a Medium (blocking) finding. See `agents/runbooks/developer-runbook.md` §3b for the pattern and the fork-PR guard requirement on merge/review paths.
- **Code-scanning gate (#3837):** dispatch `code_scan_report.yml` and read the
  open-alert list before deciding. Report `TOTAL_OPEN` and any alert this PR's
  files appear in, in the review body. See §1f — an unread alert list, or an
  alert introduced by the diff, is a Medium (blocking) finding.
- **Live verification (#3555/P6-18):** if the PR touches observability, metrics, or a UI surface, run `nyxgpt ops verify` yourself and cite its output/screenshots — see §2's "Live verification" entry below for the full rule.

### Additional Quality Checks (comprehensive review)
- Code quality and best practices (use CLAUDE.md for guidance)
- Performance considerations and potential bottlenecks
- Security concerns beyond secret detection
- Potential bugs or edge cases not covered by tests
- API contract consistency and backward compatibility

## 1a) Inverse-claims check: what does this change make *untrue*? (#3744)

The documentation criterion above is **diff-scoped**: "is documentation for
this change updated?" It asks only about the change's own surfaces. The
inverse question is a separate check, and it is the one that has escaped both
agents:

> **Does this change falsify any claim already written somewhere else in the
> tree?**

**Motivating incident (2026-08-13, #3743).** #3727/#3735 shipped repo-less
PyPI publishing. `README.md`'s install section asserted that "this has not
shipped in a PyPI release yet… a repo checkout is still required" — a claim
the very capability under review made false. The developer and the reviewer
both passed the documentation criterion *honestly*: the README section was
outside the diff's surfaces, so "docs for this change updated?" was answered
yes while the tree now shipped a lie. #3743 fixed the instance; this section
closes the class.

**How to run the check.** For every PR, before deciding:

1. Name the capability/behavior/constraint the change adds, removes, or
   inverts (e.g. "installable from PyPI without a checkout", "`nyxgpt ops
   down` now preserves volumes", "Windows unsupported").
2. **Search the tree for existing assertions about it**, not just the files in
   the diff — `grep -ri` over `README.md`, `docs/`, `agents/`, `CLAUDE.md`,
   `product_management/`, help text and UI strings, using the *capability's*
   vocabulary (product name, command, flag, service, "not yet", "does not
   support", "requires", "currently"). The diff is the wrong place to look by
   construction; the whole point is that the falsified prose lives elsewhere.
3. Read each hit and decide: does the merged state still make this sentence
   true? A sentence that the PR turns false — or that describes a workaround
   the PR makes unnecessary — must be updated in this PR.
4. **A falsified claim found and left unfixed is a Medium (blocking)
   finding.** Cite it as `file:line`, quote the claim, and state what the
   change makes true instead. Report the search you ran (paths/terms) in the
   review's `### Documentation Status` section so an empty result is
   distinguishable from a check that was never run.

**Expiry-dated claims are a finding in themselves.** Prose that encodes
world-state with built-in expiry — "not yet published", "the current version
is X", "this will ship in Phase N", "a checkout is still required" — is
guaranteed to rot and is what made #3743 possible. New or edited docs must
point at living sources instead (the PyPI project page, `docs/portability-
matrix.md`, generated command/API docs, the release tracking issue). Flag a
newly introduced expiry-dated claim as a Medium finding and ask for the
pointer form; pre-existing ones are Minor unless *this* PR falsifies them, in
which case they are Medium under the rule above. See
`agents/runbooks/developer-runbook.md` §5 for the authoring side.

## 1b) The operating ledger (#3774)

Read `agents/LEDGER.md` in full before reviewing. It is the system of record
for cross-session memory, and it binds your findings harder than anything else
in this runbook.

**Never raise a finding whose premise is a project fact you recalled rather
than checked.** A confidently-wrong REQUEST_CHANGES costs a full developer
cycle. Before asserting that the PR contradicts how this project works: find
the ledger entry, or verify against the live system this session and cite what
you ran, or say in the review that you did not verify it.

**Check the Superseded section before flagging something as wrong.** A finding
that re-asserts a retired belief (S-001..S-004 — e.g. "agents can't write
workflow files", "improvements don't gate acceptance") is itself the defect,
not the code.

**Ledger entries in the PR are in scope by definition.** An entry riding along
in a fix PR needs no issue of its own and is never scope creep. Do not
REQUEST_CHANGES over entry wording, ID choice or placement. Two things *are*
normal findings: an entry that contradicts the change shipping with it, and a
`V-` entry with no usable `Method` (an unrepeatable method is an unverified
claim wearing a verification's clothes).

**Carve-out from §1a's expiry rule.** `agents/LEDGER.md` is the one file where
dated world-state facts are correct rather than rot: every `V-` entry carries
its date, its method and its `Re-verify when` condition, which is precisely the
pointer-to-living-source discipline §1a asks for, applied to facts no living
source states. Do not flag a dated ledger verification as an expiry-dated
claim. Do flag a ledger entry that hardcodes state a living source already
answers — the current release candidate, an issue's status, what is on PyPI
right now — since the entry should point at that source instead.

**Append** entries for what the review settles: a fact you verified while
reviewing (with method), a decision reached in a huddle, a question left open.

## 1c) Executed-verification gate: was the claim ever run? (#3775)

Review in this pipeline means inspection. Inspection reads a formula and
concludes the venv bootstraps; it cannot see `ensurepip` exit 1 on stock
Homebrew, `platform.mac_ver()` answering empty, `npm` absent from a cloud
image, or a path resolving relative to a repo the target does not have. Those
four defects (#3753 twice, #3761, #3759) all passed review and were all found
by the owner running the thing once.

> **A change whose claim is about runtime, install or platform behavior does
> not reach acceptance testing until that claim has been demonstrated by
> executing it on the target platform.**

**In scope — executed evidence required.** The claim is about what happens
when it runs, on a machine:

- installs and packaging (formulas, tarballs, wheels, venv bootstrap,
  dependency resolution, `pip install` of a published artifact);
- service lifecycle (`nyxgpt ops install/up/restart/down`, launchd, systemd,
  container start, health/readiness);
- provisioning and deployment (Terraform, cloud-init, EC2, Kubernetes,
  Compose);
- cross-platform or OS-specific behavior, and anything that depends on what
  exists on the target (interpreter version, `npm`, `brew`, `docker`,
  filesystem layout, repo-relative path resolution).

**Accepted evidence.** At least one of these, *cited in the PR* by run URL,
job name, or command transcript:

1. a CI job run on the target platform — the existing smoke workflows count:
   [`macos-brew-smoke.yml`](../../.github/workflows/macos-brew-smoke.yml),
   [`linux-native-smoke.yml`](../../.github/workflows/linux-native-smoke.yml),
   [`terraform-local-smoke.yml`](../../.github/workflows/terraform-local-smoke.yml);
2. a `workflow_dispatch` run of one of those, or of another workflow that
   exercises the changed path;
3. `nyxgpt ops verify` (§2's live-verification harness) where the change is in
   its coverage;
4. an equivalent demonstrated-by-running proof: the actual command, run on the
   target, with its output.

The evidence must exercise **the changed path on the target platform** — a
Linux job does not verify a macOS keg, and a unit test that mocks `systemctl`
does not verify a systemd unit.

**Fault injection where the runner is green by luck (#3753 round two).** A job
that only runs the install passes on every machine that fails to reproduce the
bug — which is exactly how the rc5 candidate passed CI and died on the owner's
Mac. When the fix targets a condition the runner does not naturally exhibit,
the evidence must *inject* that condition and show both halves: the failure
reproduces without the fix, and disappears with it.
`macos-brew-smoke.yml`'s "Reproduce the empty mac_ver() failure, then prove the
shim fixes it" step is the template.

**No covering job? The PR adds one.** "Nothing tests this path" is the finding,
not the excuse — the developer contract requires a smoke job for a changed path
that has none (`agents/runbooks/developer-runbook.md` §4a), using the
workflows above as templates.

**Scenario criteria need scenario evidence (#3860).** Component evidence does
not close an end-to-end acceptance criterion. Where an issue's criteria
describe a *user scenario* — a phase capstone, a "from a clean machine" or
"one command" criterion, or any criterion written as a sequence the user
performs — the review agent must **cite the CI job that executed that
scenario**, naming the job and the steps that ran the user's own commands. An
install that produced files, a `brew test` block, an import that resolved, or a
unit test of one component is evidence about a *component*, and a criterion
that says "install → one command → working app reachable at ..." is not
satisfied by any number of them.

Closing a capstone on component evidence is a **Medium (blocking)** finding.
This exists because it already happened: #3516 — the Phase 6 capstone, whose
criterion *is* the clean-machine scenario — closed as completed on evidence
from `macos-brew-smoke.yml`, a workflow that at the time installed a keg and
never invoked the product. Six defects sitting on that exact path (#3850 no
`nyxgpt` on `PATH`, #3851 a 500 from the session list, #3853 a health probe
blind to its own services, #3854 caveats naming the wrong command, #3857 a web
UI stuck on placeholders, #3859 a teardown that leaves services running) were
all discoverable by running the scenario once, and all reached the owner
instead. The scenario job for the macOS user path is now
`macos-brew-smoke.yml`'s `published-tap` job, which runs
`scripts/macos-user-path-smoke.sh`; cite it (or the equivalent for the
platform under review) rather than an install job.

**Explicitly exempt.**

- **Pure-logic changes fully covered by unit tests** — the gate targets
  behavior claims unit tests structurally cannot reach, not code they already
  cover.
- **Prose-only changes** (docs, runbooks, prompts, charters), including
  agent-process changes whose contract text is pinned by tests.
- **What CI genuinely cannot produce** — the short documented list in
  `docs/live-verification-ci.md` (Docker-backed components on the hosted macOS
  runners, the setup wizard's prompts, what the web UI *renders*, Ollama model
  pulls, real Slack delivery, LLM answer quality), plus EC2 Mac hardware, for
  which no hosted runner exists (`docs/portability-matrix.md`). Name which
  item applies and what the owner must exercise; do not defer to it when the
  condition could be injected instead. **The macOS brew path is not on this
  list — neither the install nor the operate half.** Since #3860,
  `macos-brew-smoke.yml`'s `published-tap` job runs `nyxgpt --version`,
  `nyxgpt up`, `GET /health`, `GET /api/v1/sessions`, the web UI on :3000,
  `nyxgpt ops status`, `nyxgpt down` and the uninstall/residue check on a real
  `macos-15` runner. "macOS can't be tested in CI" is not a valid excuse for a
  formula, install or service-lifecycle change.

**Severity and symmetry.** Missing executed evidence on an in-scope change is a
**Medium (blocking)** finding: cite the claim, name the platform it was never
run on, and say which job would have run it. The symmetry from §2 holds —
never REQUEST_CHANGES demanding evidence that is structurally impossible to
produce, or that a cited run already produced.

## 1d) Diagnosis gate: was the cause established before the change? (#3821)

The fourth of the agentic first principles — stated in full in `CLAUDE.md`
§ Agentic First Principles, and nowhere else — forbids taking change action
without first seeking to understand. This section is the review half of it.

> **Does this PR fix a cause that was actually established, or one that was
> guessed?**

**Motivating incident.** The broken-`pyexpat` chain: #3753 shipped a fix aimed
at pip, #3788 shipped another aimed at the same guess, and #3814 was still the
same defect — the real fault was a Homebrew bottle. Each round was reviewed and
approved on the strength of the patch, because no one asked what had
established the cause. Three cycles bought one diagnosis.

**How to run the check.** On any PR that claims to *fix* something:

1. **Find the stated cause.** The PR body or the issue thread must say what the
   defect's mechanism is — not what the symptom was, and not what the change
   does. "Install fails on macOS" is a symptom; "`brew`'s python bottle ships a
   `pyexpat` built against a missing libexpat" is a cause.
2. **Find what established it.** A log line, a reproduction, a bisect, a run
   that fails one way and succeeds the other. A cause asserted without any of
   those is a guess wearing a diagnosis's clothes.
3. **Check the fix connects to it.** Read the diff against the stated cause and
   ask how it makes that mechanism stop. A patch whose mechanism has nothing to
   do with the stated cause is the #3753 pattern even when the stated cause is
   correct.
4. **Check the thread does not contradict it.** Evidence posted on the issue —
   a log, a failing run, an owner's report — that is inconsistent with the
   stated cause outranks the stated cause.

**Blocking condition.** A fix whose cause was never established — no stated
cause, no evidence behind the stated cause, or a stated cause the thread's own
evidence contradicts — is a **Medium (blocking)** finding. Say which of the
four it is, and ask for the diagnosis rather than a different patch.

**Symmetry, so this does not become a tax.**

- **Only fixes are in scope.** Feature work has no defect and therefore no
  cause; do not demand a root cause from it.
- **A cited diagnosis is a satisfied gate.** If the cause is stated with its
  evidence, this section is done, whether or not you would have diagnosed it
  the same way. Disagreeing with an established cause is an ordinary finding on
  its merits, not a §1d finding.
- **An honest mitigation is not a guess.** A change that says plainly it works
  around an undiagnosed fault, names what is still unknown, and links the issue
  that will diagnose it, satisfies this gate — that is understanding the limits
  of what is understood. What blocks is a mitigation presented as a fix.

## 1e) Generality gate: is the patch as general as the defect? (#3821)

The second of the agentic first principles — again, stated in full only in
`CLAUDE.md` § Agentic First Principles — weighs harm to the agentic process
alongside harm to the application. Its commonest form is a general fault
patched at one instance, which leaves every other instance armed and the next
session to rediscover it.

> **Is the fault this PR fixes confined to the place it fixes, or is the same
> fault elsewhere in the tree?**

**Motivating incidents.** The project-hygiene clobber (#3500 → #3816) was fixed
by exempting a single author while every other author kept racing — the same
defect, one instance patched. Contrast #3801's escalation bug: the apostrophe
that broke one `github-script` step turned out to be **47 interpolations across
6 workflows**, and the sweep found them because someone asked the question
(ledger **V-027**).

**How to run the check.** On any PR that fixes a defect:

1. **Name the fault as a class**, not as a location: "a GitHub Actions
   expression interpolated into a `script:` body is JavaScript, not data", not
   "line 812 breaks on apostrophes".
2. **Search for the class.** `grep` the tree for the construct, not the
   symptom — the other call sites, the sibling workflows, the parallel branches
   of the same `if`, the other subclasses that share the base.
3. **Require one of three outcomes**: the class is fixed in this PR; the search
   is reported and shows the instance really is the only one; or the remaining
   instances are named with a filed issue and a stated reason for deferring.

**Blocking condition.** A narrow patch on a general defect is a **Medium
(blocking)** finding. Cite the other instances you found as `file:line` — a
generality finding must name at least one, or it is speculation, which §1b
forbids.

**Symmetry.** This asks for the sweep and its result, not for unbounded scope.
A reported search that found nothing satisfies the gate. Scaling the fix down
after the sweep is the owner's call (principle 3): the reviewer's job is to
make sure the class was looked for and its extent is on the record, never to
convert every bug fix into a refactor.

## 1f) Code-scanning gate: has anyone read the alerts? (#3837)

CodeQL runs on this repository and files alerts. Until now **no step of the
review process read them.** §1's checklist named no code-scanning check, and
`security-scan.yml` runs bandit / pip-audit / `npm audit` — none of which is
the CodeQL alert list. So alerts landed on the release branch and sat there.

**Motivating incident.** Eight alerts were open on `v3.0.0` at `2a64b53`,
accumulated 2026-08-05 → 2026-08-13, none caught by a review. One was
**critical** (#124): a step output substituted into a `run:` block that then
built a nested `bash -lc "..."` command string, in a job carrying
`REVIEW_AGENT_TOKEN` on an `issue_comment` trigger. It was also the #3820 fault
class one construct wider — see §1e and ledger **V-027**/**V-032**.

> **Read the open-alert list before deciding, and put what you found in the
> review body.**

**How to run the check.**

1. **Dispatch** [`code_scan_report.yml`](../../.github/workflows/code_scan_report.yml)
   (`workflow_dispatch`, optional `ref` input) and read its run log. This is
   the supported agent path — agent sessions cannot call the code-scanning API
   directly (`CLAUDE.md` § Tooling). The log prints recent analyses, the open
   alert list with `TOTAL_OPEN`, and every SARIF `codeFlow` per open alert.
2. **Report `TOTAL_OPEN`** in the review body, in a `### Code Scanning`
   section, so an empty result is distinguishable from a check never run.
3. **Cross-reference the diff.** For each open alert, check whether its
   `file:line` is in a file this PR touches.
4. **Read the alert before classifying it.** A dismissal is a claim about the
   whole flow to the sink, not about the line — trace it. Note the 280-character
   cap on dismissal rationales (ledger **V-029**) and that dismissal itself is
   a code-scanning *write* no agent token here holds: the reviewer names the
   verdict, the owner performs it.
5. **When a PR claims to *close* an alert, count the flows.** An alert is one
   sink with *N* source→sink paths, and it stays open while **any** of them
   lives — `code_scan_report.yml` dumps every `codeFlow` per result for exactly
   this reason. Read them all and check the diff kills each one. A PR that
   redacts the flow it happened to read is not a fix; it is a rescan away from
   the same alert with a new sink line.

**Blocking conditions.** All three are Medium:

- an alert whose `file:line` lies in the diff, unless the review states why it
  is a false positive and traces the flow that makes it one;
- a review that never read the list — the whole failure mode this gate exists
  to close;
- a PR claiming to close an alert without accounting for every `codeFlow` on it.

**Motivating incident for the flow count (#3837, acceptance failure).** PR
#3899 was merged claiming alert #123 fixed. It had three flows: the developer
traced the one reaching the `except` branch of `/ops/release-candidate`,
redacted it, and shipped. The other two ran through the **success** path — the
same caught exception reaching `pypi_lookup_error` and a `blockers[]` entry in
the `200` response — so the rescan reopened #123 at a new sink line and the
owner failed acceptance with three words: *"#123 is still open."* Nothing in
the review caught it, because the gate as first written asked only whether the
list had been read.

**Not blocking.** Pre-existing alerts in files the PR does not touch are
*reported*, not fixed here: converting every PR into an alert-backlog sweep is
the scope inflation §1e's symmetry rule warns against. File them, name them,
move on.

**Note on branch coverage.** Do not assume the list is stale. The general
statement that CodeQL default setup scans only the default branch plus PRs
(`CLAUDE.md` § Tooling) does **not** describe this repository's configuration:
dispatching `code_scan_report.yml` with `ref: refs/heads/v3.0.0` on 2026-08-19
returned four fresh analyses at the branch tip `b367cb5a`, minutes old, across
the python / javascript-typescript / actions / ruby categories. The release
branch **is** scanned, and its alert list is live.

This matters because the stale-list assumption is an excuse for every alert a
review would otherwise have to answer for. Check the `Recent analyses` block the
report prints first: it gives the commit and timestamp of the newest analysis
for the ref. If that commit is the branch tip, the list is current and binding.
Only if it genuinely lags should the review say so — and say which commit it is
reporting from.

## 2) Severity model
- Critical: correctness/security/data-loss/performance regression; must block merge
- Medium: significant bug risk, missing tests, broken contract, poor maintainability; must block merge
- Minor: style/nits, minor optimization opportunities; may proceed

### Live verification: run the harness, don't defer it (Owner decision
2026-08-01, narrowed 2026-08-04 by #3555/P6-18)

The original 2026-08-01 rule deferred every live-running-stack finding
(Grafana panels rendering, a running Compose stack, chat/RAG round-trips) to
owner acceptance testing, because neither agent had a running stack or eyes
on rendered output (PR #3548/#3469 deadlocked three review cycles demanding
evidence that couldn't structurally be produced). #3555/P6-18 closed that
gap: `nyxgpt ops verify` (see `docs/live-verification-ci.md`) boots the
Compose stack in CI, generates known chat/RAG traffic, asserts it landed via
Prometheus instant queries and Grafana's HTTP API re-executing each touched
dashboard panel's own query, and captures Playwright screenshots.

**On every PR that touches observability, metrics, or a UI surface, the
review agent runs this harness itself, in CI, before deciding:**

1. Run `nyxgpt ops verify` (the review workflow's environment is prepared
   for this — see `docs/live-verification-ci.md` for what CI installs).
2. Read the full assertion output. A failing Prometheus counter delta or
   Grafana panel-query check names the exact panel/query that failed —
   treat it as a Critical or Medium finding per the severity model below,
   the same as any other reproduced failure.
3. Use the Read tool on every screenshot under
   `~/.nyxGPT/verify-artifacts/` and *visually inspect* it (the review
   agent is multimodal) — a rendered-but-empty or visibly broken panel is a
   finding even if the underlying query technically returned data.
4. Include a "### Live Verification" section in the review body summarizing
   the harness run and what the screenshots show. **An APPROVE on an
   eligible PR that skipped running the harness, or that has no "### Live
   Verification" section, is a process violation** — not a style nit, treat
   it the same as approving with failing tests.

**What still defers to owner acceptance** — because CI genuinely cannot
exercise it, not because it's inconvenient — is the short, explicit list
`docs/live-verification-ci.md` documents: the Docker-backed components on the
hosted macOS runners (those images ship no Docker daemon), the setup wizard's
interactive prompts, what a web *panel* renders on the brew path, Ollama model
pulls, real Slack delivery (no real webhook secret in CI), and LLM response
*quality* (CI's model is stubbed/tiny — the pipeline being intact end to end
is what's asserted, not answer quality). The Apple Silicon brew-services
**operate** path is *not* on this list any more: this harness only exercises
the Compose path, but `macos-brew-smoke.yml`'s `published-tap` job executes
the brew keg install *and* the user path after it on a real `macos-15` runner
(#3860). List exactly which of these apply, explicitly,
in the APPROVE review so the owner knows precisely what to exercise during
acceptance. Do not REQUEST_CHANGES or burn escalation cycles demanding
evidence the harness already produced, or evidence that's on the
not-covered list above (still a structural impossibility for CI) — escalation
is reserved for unresolved findings the agents *could* fix but haven't.

## 3) CI state is gated before you (#3971)

**"CI is red on this head", "N checks are pending", "the web coverage gate
fails", "`k8s-artifact-smoke` fails deterministically" are NOT findings.** They
are machine-observable facts the PR page already displays, and reporting one
costs a reject round plus a re-fix round (~18M tokens) to say what a glance at
the PR says for free. Measured over 2026-08-13..19: **~36% of all blocking
(Critical + Medium) findings were this relay**, on 39 of the 65 rejected work
items. This section exists to end it, so that attention goes to judgment
instead.

What handles it instead:

- **Red head** — `developer_submit_for_review.sh` refuses to submit while any
  *required* check on the head has concluded failure (exit 3, before any
  GitHub write). If a check goes red *after* submission, the review workflow's
  `head-gate` job hands the head back to the developer by assignment, with no
  review invocation spent and **no REQUEST_CHANGES round recorded** — the
  cycle counter and the 3-strike escalation are untouched.
- **Pending head** — the gate *waits*. Nothing is posted and nothing is
  rejected; the review starts when the checks conclude. A PR that is merely
  mid-CI is never bounced back to the developer.
- **The required set is explicit**: the named list in
  `.github/required-checks.txt`, never "every check on the head" (which would
  deadlock the gate against the review's own check run). A check that is not
  attached to the head — the usual case for a path-filtered smoke — is not
  waited for and not counted against it.

**The one CI-adjacent thing that IS your job: verifying a developer override.**
When the failure reproduces on the base branch, the developer may submit with
`--ci-override "<reason>"`, which writes the reason into the PR body under
`<!-- nyxgpt-ci-override -->`. The review workflow surfaces it to you as a
**claim**. Check it — e.g. the same check on the base branch — and say what you
checked. An override whose stated reason does not hold is a Medium (blocking)
finding; an override accepted silently is a process violation.

Full mechanics: `docs/reviewable-head-gate.md`.

## 3a) Merge conflicts are developer work (owner rule 2026-08-15, #3801)

A conflicted PR is not an escalation. Owner rule: *"merge conflicts shouldn't
halt progress and shouldn't be escalated to me unless there's truly a decision
to be made only I can make."*

- **Do not assign the owner for a conflict, and do not ask them to resolve
  one.** `review_accept_and_merge.sh` hands a conflicted-but-approved PR to
  `scripts/agents/dispatch_conflict_resolution.sh`, which returns the issue to
  In Progress and reassigns the developer agent. That happens automatically;
  nothing here needs a human.
- The owner is reached only when the developer agent issues
  `CONFLICT_REQUIRES_OWNER_DECISION` with a specific question (two
  owner-accepted behaviors in semantic contradiction), or when the automated
  rounds stop converging (default 3).
- **A rebase is a finding.** Resolution in this repo is always *merge the
  release branch into the PR branch* — branches are never rebased
  (developer-runbook §2). A PR whose branch was rebased, or a diff/doc that
  instructs a rebase, is a Medium (blocking) finding.
- When reviewing a resolution round, check that **both** sides survived: the
  PR's feature content and the owner-accepted behavior merged into the release
  branch since. A conflict "resolved" by taking one side wholesale silently
  reverts merged work — Critical if it reverts release-branch behavior.

## 4) Review and recommendation
After completing the review:
- Post a structured review comment starting with "## Code Review - [APPROVE|REQUEST_CHANGES]"
- Include findings organized by severity (Critical/Medium/Minor)
- Include a `### Code Scanning` section reporting `TOTAL_OPEN` from the
  `code_scan_report.yml` run and any alert in a file this PR touches (§1f)
- Provide clear recommendation with rationale

## 5) Automatic execution
The review decision is automatically executed based on the review comment:
- **APPROVE**: Workflow automatically merges the PR (no human confirmation required)
- **REQUEST_CHANGES**:
  - Issue returns to developer-agent with "In Progress" status
  - Developer reads review comment and implements fixes
  - Developer runs tests in 3-try loop (resets each assignment) BEFORE committing
  - Developer commits and re-submits for review (triggers re-review automatically)
  - Review cycle repeats (cumulative count tracked)
  - After 3 review cycles: Issue stays "In Review", escalates to human owner

Manual override (optional):
- `@approve-merge` - Human can manually trigger merge
- `@request-changes` - Human can manually trigger changes workflow (legacy)

## 5a) Dispatch-mode recovery (`workflow_dispatch`, #3704)

**When to use it.** A PR is orphaned: it is open, the review agent is the
requested reviewer, and no review workflow run exists for it (the automatic
`review_requested`/`synchronize` trigger never fired, or its run died before
posting a verdict). This is the *only* supported way to start a review from
outside the PR event stream.

```bash
scripts/agents/manually_trigger_pr_review.sh <PR_NUMBER>
```

**Always use the script, never a bare `gh workflow run claude-code-review.yml`.**
Without `--ref`, `gh workflow run` dispatches the copy of the workflow on the
repo **default branch** (`master`), which under this project's branch rules
only moves on releases and is therefore arbitrarily far behind the active
release branch. Dispatched reviews were consequently executing a stale
workflow definition — including one whose `--json-schema` predated the #3687
`disagreement_type` field, so every dispatched REQUEST_CHANGES silently fell
back to type (a) and, on its second cycle, routed to a huddle instead of the
fix cycle the reviewer had actually called for. The script now pins
`--ref "$RELEASE_BRANCH"`.

**What dispatch mode guarantees.** Same outcome as an event-triggered review,
for both verdicts:

- **APPROVE** — merged by `review_agent_auto_review.yml` exactly as before.
- **REQUEST_CHANGES** — the handoff is guaranteed, not merely attempted.
  The primary path is still the event chain (`pull_request_review`, with the
  `nyxgpt-structured-review` comment as the `issue_comment` fallback). On top
  of that, the review run's own final step
  (`scripts/agents/review_ensure_handoff.sh`) waits ~4 minutes for that chain
  to leave a footprint on the PR and, if none appears, executes the same
  routing decision itself: resolve the PR's linked issue, then loop / huddle
  / escalate per §6b.

  Routing is not duplicated — the backstop calls the same
  `huddle_routing_decision` in `scripts/agents/lib/sprint_calc.py` the primary
  workflow does, so the two can never disagree. It is idempotent: every action
  it takes writes one of the handoff markers the footprint check looks for, so
  a re-run (or a late-firing event chain) is a no-op.

  The footprint check ignores the `nyxgpt-structured-review` comment itself.
  That comment is the event chain's *trigger*, never its footprint, and it
  embeds the review's free text — a summary that happens to say "review loop"
  or "spec ambiguity" (ordinary words when the PR under review is about the
  review machinery) would otherwise look like a handoff that never happened.
  The primary path excludes it the same way, by scanning only comments newer
  than it.

  Backstop-posted comments carry a "_Posted by the dispatch-mode review
  handoff backstop_" footer. **Seeing that footer means the event chain
  dropped a handoff** — worth a look at the review run's logs, even though
  the cycle itself recovered.

This closes the failure observed 2026-08-09/10, where dispatched
REQUEST_CHANGES verdicts on PRs #3684, #3683 and #3606 produced zero fix
activity for 9+ hours until a human re-dispatched the developer by hand.

## 6) Review cycle escalation
The review workflow tracks cumulative review cycles:
- Each REQUEST_CHANGES increments the cycle counter
- Developer 3-try loop (for test failures) resets each time issue is reassigned
- Review 3-cycle limit is cumulative across all reviews for this PR
- After 3rd REQUEST_CHANGES review:
  - Issue remains Status -> In Review
  - Issue reassigned to HUMAN_OWNER
  - Slack DM sent to human (`notify_human_escalation`,
    `scripts/agents/lib/gh_project.sh`, #3695 -- reuses the existing
    `SLACK_BOT_TOKEN` + `SLACK_USER_ID` secrets already configured for
    `notify-merge-conflicts.yml`; missing secrets or a failed Slack call
    degrade gracefully to the GitHub comment alone, which is always posted
    first and unconditionally; repeat escalations on the same PR within a
    60-minute window are deduped)
  - Human intervenes to resolve

All fixes happen on the PR branch (no separate issues created).

## 6b) Disagreement taxonomy & huddle protocol (owner-ratified 2026-08-09, #3687)

The 3-cycle gate above is a circuit breaker, not a decision-maker: it counts
failed cycles without ever changing the question. Every REQUEST_CHANGES
round must be classified as one of three disagreement types, posted in a
`### Disagreement Type` section of the structured review comment
(`.github/workflows/claude-code-review.yml`) as `**[a|b|c]**: [reason]`:

- **(a) verifiable defect** — a failing test/CI or a reproducible bug. Loops
  as in §5/§6, but **each round must state a *new* diagnosis**, not retry
  the old one. If your diagnosis hasn't changed since the last round even
  though the fix attempt did, that is itself a signal to reclassify as (b):
  the approach, not the diagnosis, is what's wrong.
- **(b) judgment call** — a design/approach disagreement, not a bug. This
  type **never loops** — it goes straight to a huddle instead of another fix
  cycle, regardless of what cycle count it's on.
- **(c) spec ambiguity** — the issue itself is unclear, or resolving it
  needs owner authority no agent conversation can supply. This **escalates
  to the owner immediately, cycle zero** — no agent conversation can resolve
  what only the PM knows.

Routing (`scripts/agents/lib/sprint_calc.py huddle-routing`, the
`huddle_routing_decision` function, called by
`review_agent_auto_review.yml`'s "Count review iterations and classify
disagreement" step): type (c) always escalates immediately; type
(b) always huddles immediately; type (a) follows the existing loop and
huddles on its **2nd** failed cycle instead of attempting a 3rd blind
retry, still hitting the unchanged 3-cycle outer breaker (§6) if the huddle
doesn't resolve it. A missing/malformed classification defaults to (a) —
degrades to pre-#3687 behavior rather than blocking the loop or
over-escalating.

**The huddle**, once triggered (`review_agent_auto_review.yml`'s "Trigger
huddle" step posts the `HUDDLE_TRIGGERED` marker):
1. The review agent's position is the code review comment already posted —
   nothing further to do here.
2. `huddle_session.yml` runs the whole conversation in **one job** (#3911):
   bounded rounds (`vars.HUDDLE_MAX_ROUNDS`, default 3) of a developer turn
   and a review turn, then the scrummaster's decision. The conversation
   happens in a Slack thread under each agent's own identity (#3910); the PR
   gets the decision comment and the full transcript in a collapsed block.
   Each turn is still its **own** invocation — one job is not one session,
   and the memorylessness #3687 relies on is preserved deliberately. A
   review turn may end `HUDDLE_SETTLED` to close the huddle early, and that
   sticks: every later round inherits it, so an early close is not re-opened
   by a round that never ran. Nobody posts a position comment on the PR any
   more — the workflow writes each turn to the thread.

   The collapsed block has **two** sources (D-040). The turn files are its
   floor — written on the runner, so the record survives a Slack outage —
   and the thread is then read back on top of them, because the files hold
   only what the three agents wrote and the thread is the sole record of
   anyone else in the huddle. **If you weigh in on a huddle in Slack, your
   message is archived to the PR**; if you weigh in after the session has
   closed, it is not. A session that dies mid-round says so in the thread as
   well as leaving the `HUDDLE_FAILED` marker on the PR.
3. The scrummaster's decision chooses one of: **proceed** / **change
   approach** (stated) / **descope** (e.g. drop a named test, split the
   issue) / **escalate to owner** (runs the same `assign_issue_verified` +
   `sprint_autopilot_kick` escalation primitives §6 uses), and lands on the
   PR carrying its rationale, a thread permalink, and the `HUDDLE_DECISION:`
   line.
4. `huddle_decision_dispatch.yml` executes the decision's "what happens
   next": **proceed** / **change-approach** / **descope** put the issue back
   to **In Progress** and hand it to the developer agent
   (`assign_and_trigger_developer`, the same primitive a REQUEST_CHANGES
   round uses), with a `HUDDLE_DECISION_DISPATCHED` marker so a duplicate
   decision cannot start two fix cycles. It acts only on a decision comment
   authored by the scrummaster, which is why the session posts that one
   comment (and runs the escalate shell-out) under `SCRUMMASTER_AGENT_TOKEN`
   rather than the review-agent token the rest of the job holds. **escalate**
   is terminal — the decision turn already performed it.
5. That fix cycle (`developer_auto_implement.yml`'s "Run Claude Code to
   fix review issues" step) reads the `HUDDLE_DECISION:` comment on the PR
   and executes the agreed plan rather than deciding independently.

**The huddle owns its round (#3736).** Two races used to make the protocol
lose to the machinery around it; both are now closed by state on the thread
rather than timing, in `scripts/agents/lib/huddle_state.py` (round scoping,
marker dedupe, decision parsing) and `plan_round` in
`scripts/agents/lib/review_handoff.py` (routing, shared with the §5
dispatch-mode backstop so the two can never disagree):

- **One huddle per round.** A *review round* starts at the newest
  `nyxgpt-structured-review` comment. While a round has a `HUDDLE_TRIGGERED`
  and no decision, nothing else fires — no second trigger, no return to the
  developer, no escalation. `review_agent_auto_review.yml` can execute one
  verdict twice (the `pull_request_review` event and the structured-comment
  fallback), and on PR #3728 both posted a trigger: two developer-position
  runs, two mediations, the second diagnosing the duplication itself. The
  fallback's footprint check now includes `HUDDLE_TRIGGERED`, and the trigger
  step re-reads the state immediately before posting. Since #3911 half of that
  race is gone by construction — the position and mediation legs are steps of
  one job, so a second trigger cannot start a second *leg* — but the duplicate
  trigger itself is not: those upstream guards are unchanged, and the
  concurrency group only serializes the second run. So `huddle_session.yml`
  opens with a **`gate` job** that calls the same `is_primary_marker_comment`
  and stands the whole run down unless its trigger comment is the round's
  first. `huddle_decision_dispatch.yml` calls it too, for the decision comment
  a human can re-post.
- **A decision re-arms the cycle counter.** proceed / change-approach /
  descope mean the disagreement was resolved, so only REQUEST_CHANGES
  reviews submitted *after* the decision count toward §6's 3-cycle breaker
  (`effective_request_changes_count`). On PR #3733 a "proceed" decision at
  19:04 was followed by the cycle-limit escalation at 19:10, which parked
  the issue on the owner and stalled the pipeline until a manual reset — the
  mediation had written that escalating "would spend an owner turn on a
  small mechanical change nobody disputes". Re-arming is not amnesty: three
  fresh failed rounds after a huddle still escalate.
- **One escalation per event.** The escalation step re-checks the thread
  before it acts, and the redundant "Post completion comment" restatement of
  the escalation is gone (it made every escalation arrive as a pair, and two
  pairs when the trigger paths raced).

## 6c) Unresolved-escalation dispatch pause backstop (#3687)

Escalations (§6's 3-cycle limit, or §6b's type-(c)/type-(b)-deadlock
escalate) must not silently accumulate: one escalated item is normal
traffic, but two or more open at once usually signals something systemic
(bad base commit, poisoned suite, review-prompt regression). "Unresolved
escalation" = an open issue currently assigned to `HUMAN_OWNER`, **excluding
the release tracking issue** (`RELEASE_ISSUE_NUMBER`), which is owner-assigned
by design for the whole life of a release and would otherwise inflate the count
by one forever, dropping the effective pause threshold from 2 to 1 (#3868 — the
same exemption the drain gate applies)
(`count_unresolved_escalations`/`escalation_pause_gate`,
`scripts/agents/lib/gh_project.sh`) — purely derived from live issue state,
no hidden counter. `scrummaster_dispatch_next.sh` checks this gate before
selecting a Backlog candidate: with 0 or 1 unresolved, dispatch proceeds
unconditionally; with 2+, new dispatch pauses and a loud report (listing
the escalated issues) is posted/updated on the release tracking issue.
Dispatch resumes automatically the next time it's checked once the count
drops below 2 — clearing the escalations is the only action needed, there
is no separate "resume" step. See `agents/runbooks/scrummaster-runbook.md`
for the dispatch-side detail.

## 7) Merge criteria
- All tests and linters passing
- Code review APPROVE decision (either from review agent or human override)

## 8) Post-merge
When PR is merged (automatically on APPROVE or via human `@approve-merge` override):
- Automation merges into active release branch (NEVER merge to master/main)
- Delete short-lived feature/fix branches created for the feature
- **Set the PR's own project item Status -> `Closed`** (owner decision,
  2026-08-12, reaffirmed 2026-08-13, #3742). A PR card is a review-lane
  artifact, not a work item: leaving it in `In Review` drowns the owner's
  acceptance queue (13 + 3 cards hand-swept on 2026-08-10, 10 on
  2026-08-13). This is agent-side on purpose — the board's built-in "Pull
  request merged" automation is GitHub-proprietary (the agent system must
  stay portable to non-GitHub deployments), is not retroactive, and was
  observed enabled while the debris accumulated.
- Close the issue (GitHub state)
- Check the issue's native blocked-by dependencies (`/issues/{n}/dependencies/blocked_by`):
  - **No open blockers (normal case):** set issue status -> Acceptance
    Testing, assign issue -> human owner (dkblinux98)
  - **Open blockers exist (parked case, owner process rule, 2026-08-04,
    #3631):** set issue status -> In Review instead, do NOT assign the human
    owner, comment on the issue naming the open blockers. See §9 "Parked
    issues" for how the set later moves to Acceptance Testing together.
- Notify scrummaster-agent that developer-agent is ready for next issue

### Important: Issue auto-close behavior
- PRs merged to the release branch (e.g., v1.0.0) do NOT auto-close linked issues
- GitHub only auto-closes issues when PRs merge to the default branch (master)
- Automation manually closes issues after merging to release branch
- Post-merge, non-parked case: issue should be CLOSED (GitHub state) +
  Acceptance Testing (project status) + assigned to human
- Post-merge, parked case: issue should be CLOSED (GitHub state) + In Review
  (project status) + NOT assigned to human (still shows the review agent /
  prior assignee) until the sweep promotes it

### PR lane invariant (#3742)

No merged or closed PR's project item may sit in `In Review` (or any other
active lane). Three paths enforce it, all agent-side and all idempotent:

| Path | Script | Covers |
| --- | --- | --- |
| Merge flow | `scripts/agents/review_accept_and_merge.sh` (via `close_pr_project_item`) | PRs the review agent merges |
| Close event | `.github/workflows/pr_project_status_on_close.yml` -> `scripts/agents/pr_close_project_status.sh` | Any `pull_request: closed` — rejected/abandoned PRs, owner and ceremony merges |
| Sweep backstop | `.github/workflows/sweep_pr_status.yml` -> `scripts/agents/reconcile_pr_lane.sh` | Cards predating the invariant, plus any stamp lost to a flaky API. Runs daily and applies; manual dispatch is dry-run by default and can narrow to one lane via `SOURCE_STATUS` |

The terminal lane is `STATUS_CLOSED` (config key, default `Closed`). Issues
are never touched by any of the three. Regression coverage:
`tests/test_pr_lane_hygiene.sh` / `tests/unit/test_pr_lane_hygiene.py`.

## 9) Human stakeholder acceptance

After merge, each issue is assigned to the human owner with status "Acceptance Testing" for final acceptance -- the owner-created gate (2026-07-31) that marks work as merged and ready to acceptance-test, so unmerged In Review issues are never tested by mistake.

### Parked issues (merged but blocked, owner process rule 2026-08-04, #3631)

A merged issue whose acceptance criteria depend on other, still-open issues
cannot be meaningfully accepted -- the owner would be testing against
unfinished work. `review_accept_and_merge.sh`'s post-merge step checks the
issue's native blocked-by dependencies; if any are still open, it parks the
issue at **In Review** instead of Acceptance Testing and does NOT assign the
human owner, commenting why and listing the open blockers. This never blocks
the merge itself or branch/PR cleanup -- only the acceptance handoff.

The parked issue moves to Acceptance Testing (and the owner is assigned)
only once **every** blocker is complete -- merged and itself in Acceptance
Testing or beyond (For Release). This is enforced by a sweep
(`sweep_parked_blocked_issues.sh`, run via
`.github/workflows/sweep_parked_blocked_issues.yml`, every 30 min +
`workflow_dispatch` with a `dry_run` input): it finds every parked issue and
promotes any whose blockers have all completed, posting a promotion comment
on each. A chain of parked issues (A blocked by B blocked by C) resolves
transitively within one sweep run -- once C completes, B promotes in the
same pass, which in turn makes A eligible and it promotes too, rather than
each hop waiting for a separate 30-minute cycle.

Motivating case: #3508 was parked back to In Review by the owner, blocked by
#3621/#3622 -- its repo-less-portability acceptance criteria are unmeetable
until those land.

### If acceptance passes
Move the issue to "For Release" in the project board. No action needed in GitHub.
**Gate:** an issue that anything blocks (see below) is NOT moved to
"For Release" by hand — the promotion sweep
(`promote_accepted_features.yml`, every 30 min) moves it automatically once
everything blocking it, transitively, has itself been accepted (For Release).

### If acceptance reveals an improvement (not a defect)

When acceptance testing shows the feature works as specified but the spec was
incomplete or wrong, that is a **product management failure**, not an
acceptance failure — the metric is charged to requirements, not to
implementation (owner decision 2026-08-01). File it by commenting
**`@improvement`** on the feature issue (with the description in the same
comment): `handle_improvement.yml` files it for you with the right label,
relation and fields. Filing it by hand through the normal flow is equally
fine — it does not jump the queue the way a defect does:

- Label: **"Improvement"** (the existing label stands — no new label)
- The link to the issue it was filed against is a **native blocked-by
  relationship** — visible in that issue's Relationships panel, written by
  the handler, stored nowhere else (owner decision 2026-08-12, #3731). No
  `Related feature: #N` body line; that convention is retired. If no feature
  issue applies, that is NOT a blocker — file and work the Improvement
  without a relationship.
- **Drain gate (owner decision 2026-08-12, #3730):** an improvement filed
  during a round lands in the **`Acceptance Failed`** lane with the failures
  and is worked once the round drains — see below.

Per feature, the two labels give two separate counts for metrics:
"Acceptance Failure"-labeled issues = implementation failures;
"Improvement"-labeled issues filed during acceptance = requirements gaps.

**Gating (owner decision 2026-08-12, #3731, superseding the 2026-08-01
"improvements never gate" rule):** an improvement blocks its issue's move to
"For Release" exactly like an acceptance failure does — both commands write
the same native blocked-by relationship. The label distinction is a
*statistic*, not a gate.

### If acceptance fails (bug found after merge)

1. **Go to the issue** (it is assigned to you and closed)
2. **Add a comment** describing what is broken — be specific:
   - What you expected
   - What actually happened
   - Steps to reproduce if relevant
3. **On the same or a separate comment, write:** `@acceptance-failure`

That's it. The system will automatically (related-issue model, owner decision
2026-08-02):
- Leave the original feature/doc/release/improvement issue **intact** — it
  stays closed, keeps its labels, and stays in whichever parking lane it is
  in: "Acceptance Testing", or "Acceptance Failed" if you moved it there
  (#3780). The original never re-enters the dev/review cycle.
- Create a **new** issue labeled "Acceptance Failure" marked as **blocking**
  the original through GitHub's native issue relationship — the original's
  Relationships panel shows exactly what holds it back, and that panel is the
  only place the link is stored (owner decision 2026-08-12, #3731: no body
  prose, no comment markers). NOT a sub-issue.
  Module/Priority/Effort/Milestone copy from the original; Sprint is the
  active sprint.
- Place the new issue in the **"Acceptance Failed"** lane and hold it there
  (drain gate, owner decision 2026-08-12, #3730) — it is not assigned or
  worked while you are still testing this round.

### The drain gate (owner decision 2026-08-12, #3730)

Failures and improvements filed during a round wait in **"Acceptance
Failed"**. The fix process starts when **"Acceptance Testing" drains** —
empty except the release tracking issue, which is exempt and stays there
until the whole release is accepted. On the drain, `acceptance_drain_gate.yml`
moves every held item to **Backlog** and kicks the scrummaster queue **once**.

So the rhythm is: **test the whole round → the agents drain the failures →
test the next candidate.** Nothing you file mid-round can flood the lane you
are still testing or burn an RC cycle.

Two exemptions, both encoded rather than incidental:

- the **release tracking issue** never counts as an item under test;
- **agent-process issues** bypass the gate entirely and are worked
  immediately — declare it in the issue body ("…bypasses the drain gate")
  or with the `<!-- drain-gate: bypass -->` marker.

**You may also park a failed feature in that lane** (owner decision
2026-08-14, #3780) — it is where you keep what you have tested and failed.
The machinery reads it: a **closed** item in `Acceptance Failed` is a parked
feature, promoted to "For Release" by the promotion sweep once its whole
blocked-by closure is accepted, and moved by nothing else. An **open** item
is this round's held rework, released to Backlog on the drain as above.

Once released to Backlog, the item follows the normal flow: the developer
agent takes it, creates a `fix/N-...` branch and a PR with `Closes #N`.

**If the failure issue's fix fails your re-test:** comment
`@acceptance-failure` on the FAILURE issue itself — it is **reopened** and
sent back through dev → review (no new issue; its own history is the trail
of that failure's resolution). A genuinely NEW, distinct failure of the
feature gets its own issue via a comment on the feature.

The automation tells those two cases apart by requiring **both** a handler
label ("Acceptance Failure" or "Improvement") **and** a native blocking edge
(#3731). Neither alone is enough: owner-filed defect issues carry the label
while being parents themselves, and a plain feature can block a sequenced
successor.

**Promotion:** when everything blocking an issue reaches "For Release" —
directly and **transitively**, so a failure filed against a failure counts —
it is promoted to "For Release" automatically by the promotion sweep, with a
comment recording which issues cleared it. Unique-failure count per feature
(usually 1) = the "Acceptance Failure"-labeled issues blocking it; rework
rounds live inside each failure issue's history.

> **Note:** `@acceptance-failure` is only accepted from the human owner account and only on
> issues (not PRs). It is entirely separate from the review-loop overrides
> (`@approve-merge`, `@request-changes`, `@send-to-developer`) which apply to PRs
> during the automated code review cycle.
>
> **Rollout:** `issue_comment` workflows run from the repository default
> branch — empirically the active release branch, so this model went live
> when it merged (first live run 2026-08-02, on the v2.0.0 tail failures).

## 10) Phase completion
When the human owner moves the last issue in the active Phase to "For Release" (human stakeholder acceptance):
- Notify human owner that phase is complete and ready for release

**A capstone is reviewed against the scenario, not the parts (#3860).** Before
approving or closing a phase capstone, apply §1c's scenario rule: read the
capstone's own acceptance criterion, and cite the CI job whose steps executed
that sequence end to end. If no job runs it, that is the finding — the capstone
does not close, and the covering job is the work.

## 11) Configuration

### Required Secrets
The review workflow requires these secrets to be configured:
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token for Claude Code agent
- `REVIEW_AGENT_TOKEN` - GitHub token with repo/project permissions (used for review workflow)

**How to configure secrets:**
- Navigate to: Settings → Secrets and variables → Actions → Secrets
- Click "New repository secret"
- Add all secrets if not already present

### Branch Cleanup
Branch deletion happens in `review_accept_and_merge.sh` via `--delete-branch`, not in auto-fix workflow.

Everywhere else, a branch is deleted **only when its content is provably on
the target branch** — an ancestor, or every path it touches already identical
there (`scripts/agents/lib/branch_content.py`; ledger D-031). Commit ancestry,
commit count, `git branch --merged`, mergeability, branch age, "no PR exists"
and "its issue is closed" are all unusable as signals; each was disproven
against a real branch set, where the branch whose every byte had landed looked
the *most* unmerged of three. Anything not positively proven is **reported,
not deleted**, and there is no scheduled sweep (D-013). `reconcile_dead_branches.sh`
reports on demand and needs `--delete` to act.

### The closure gate
`review_accept_and_merge.sh` will **not close an issue on a merge it cannot
verify**. `gh pr merge` exiting 0 is a report; the evidence is that every path
the PR head touched is readable on the base branch afterwards. If the check
cannot confirm it, the issue is left open, a comment says why, and the run
exits non-zero — #3789 and #3815 were both closed as `completed` with their
fixes stranded on branches that never landed (#3862).
