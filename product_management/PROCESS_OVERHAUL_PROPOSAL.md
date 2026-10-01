# Process Overhaul — diagnosis and proposal

**Created:** 2026-08-20 · **Author:** executive-assistant session, at owner request
**Status:** Proposal for owner decision. Nothing here is filed or implemented.
**Trigger:** owner's read of the 2026-08-20 retrospective — "new hire agent
syndrome, tests not catching acceptance failures, insane token burn … it's
almost time to throw out the process and start over."

This document does three things: corrects what the retrospective actually
measures, re-roots the three problems in the data, and proposes what to keep,
what to delete, and what is missing entirely.

---

## 1. What the retrospective actually measures (four corrections)

The owner is right that the page does not reflect recent changes. Four of its
headline framings mislead, and the corrections change what to do about them.

**(a) The churn numbers are six days, not eight months.** `churn.json` holds
961 rounds, but only rounds from **2026-08-14 to 08-19** carry token data —
earlier runs' logs have expired. So "8,364,856,987 tokens" is a **six-day**
figure, and **81% of it lands on two days**: 3.41B on 08-18 and 3.36B on 08-19
(213 and 243 rounds respectively). The burn is not a slow eight-month
accumulation; it is the current daily rate.

**(b) The unattributed bucket is not huddles.** The page says 555 rounds "ran
on the release branch (huddles, standalone sessions)". They did not.
Grouping the unattributed rounds by workflow:

| Workflow / kind | Rounds | Tokens |
|---|---:|---:|
| `developer_auto_implement.yml` · implement | 143 | 3.40B |
| `developer_auto_implement.yml` · review-fix | 179 | 1.77B |
| `developer_auto_implement.yml` · acceptance-fix | 24 | 0.53B |
| `claude-code-review.yml` · review | 81 | 0.41B |
| `developer_auto_implement.yml` · self-heal | 96 | 0.17B |
| `huddle_session.yml` · huddle | 21 | 0.04B |

The 6.31B "unattributed" tokens are **ordinary developer rounds whose branch
name did not parse to an issue number** — an attribution defect in the dumper,
not a separate population of expensive sessions. Huddles are 0.5% of spend.
Fixing the parse is worth doing precisely because it moves three quarters of
the spend back onto the issues that caused it.

**(c) "99.1% context share" is not the useful number.** It comes from a
turn-ratio heuristic (`production_tokens` = 18.9M against `context_tokens` =
2.03B, with 424 rounds excluded for lacking turn markers). Read literally it
says the system produces nothing, which is false. The structural fact
underneath is cleaner and is not on the page:

> **`cache_read` is 7.87B of the 8.36B total — 94%.** Fresh input is 240M,
> cache creation 240M, output 13.3M.

Cost is therefore **≈ rounds × turns × context size**, because every turn
re-reads the whole prefix. Median round: **64 turns at ~137k tokens per turn**.

**(d) The bootstrap diet did not move the number.** D-021 (2026-08-18) cut the
bootstrap from ~180k tokens to a scoped list, on the reasoning that context was
97.3% of tokens. Median tokens/turn since: **08-15: 132k · 08-16: 120k ·
08-18: 135k · 08-19: 142k**. No improvement. The reason is arithmetic: by turn
30 of a 189-turn session the context is full of the session's *own* accumulated
tool output, not of bootstrap documents. **Trimming what agents read cannot fix
this. Only shorter sessions and fewer rounds can.**

---

## 2. The three problems, re-rooted

### 2.1 Token burn — the shape of the spend

Median cost per round, by kind, over the six measured days:

| Kind | Rounds | Median turns | Median tokens | Total |
|---|---:|---:|---:|---:|
| implement | 65 | **189** | 42.7M | 3.40B (41%) |
| review | 287 | 53 | 7.2M | 2.46B (29%) |
| review-fix | 115 | 81 | 10.7M | 1.77B (21%) |
| acceptance-fix | 22 | 126 | 20.4M | 0.53B (6%) |
| self-heal | 15 | 83 | 9.6M | 0.17B (2%) |
| huddle | 21 | 21 | 1.7M | 0.04B (0.5%) |

Three facts follow.

1. **The implement round is the single largest line item, and 189 median turns
   is a session that has lost the plot** — reading, re-reading, running full
   suites, self-healing, re-deriving.
2. **Review costs 29% of everything**: 287 review invocations for 127 PRs, 2.3
   per PR, ~7.2M tokens each.
3. **A rejection costs ~18M tokens** (one review + one review-fix) — and it
   buys a full re-onboarding on both sides.

Now what the rejections were *about*. Of the 240 blocking (Critical + Medium)
findings in the last-7-days window, a keyword pass over their titles gives:

- **~36% (87) report machine-observable check state** — "fails on this head",
  "CI red", "17 checks pending", "coverage gate fails", "smoke fails
  deterministically". **39 of the 65 rejected work items had at least one.**
- **~17% (41) are documentation or PR paperwork** — falsified doc claims,
  "PR body is still the unfilled template".
- **~8% (18) are a missing executed-evidence citation** (the #3775 rule).
- **~7% (16) are ledger ID collisions** — duplicate `V-0xx`/`D-0xx` entries
  from concurrent branches, i.e. friction generated by the memory fix itself.

So roughly **two thirds of blocking findings are assertions a gate could make
for free**, and the pipeline instead pays ~18M tokens per round-trip to have
one agent read a red check and tell another agent about it. The review agent is
not doing this badly — its Criticals are genuinely good ("Dev-mode web UI
returns 500 on every request", "`nyxgpt ops status` crashes on any machine
without `config.ini`"). It is being *used* badly.

### 2.2 Tests not catching acceptance failures — the pipeline has no user

The corpus has 223 escaped defects against 62 clean first-pass merges. The
instinct is "write better tests". The data says otherwise. Here are the 12 most
recent Acceptance Failure issues, unedited:

- brew install dies opaquely on a `python@3.12` keg with broken `pyexpat`
- wizard returns the Slack bot token in cleartext
- wizard save writes an unparseable `config.ini` and bricks the API
- `cloud deploy` has no `--kubernetes` — #3506's decision was never implemented
- `ops -h` misstates locality
- macOS target provisioning is unwrapped; `cloud deploy` lacks `--os`
- Support Docs viewer ships agent and CI process docs to end users
- uninstall leaves services running — orphaned launchd jobs and containers
- web UI renders permanent loading placeholders — client JS never loads, **while
  every endpoint is green**
- k8s local install oversubscribes the kind node — prometheus cannot schedule
- brew install exposes no `nyxgpt` command — the documented install flow cannot work
- `macos-brew-smoke` never exercises the user path, so a broken install certifies green

Not one is a logic error a unit test could reach. Every one is **install it
fresh, run it, click it, uninstall it**. The suite is not thin — 217 unit test
files and 160 web tests over 64.5k lines of product code — it is aimed
somewhere else. And the last item is the tell: the smoke workflow built to
prevent exactly this class certified a broken install green.

**There is no step in this pipeline where anyone installs the published
artifact and uses the product.** The owner is the integration test. That is why
45% of issues fail acceptance, and no amount of review intelligence will change
it — the reviewer reads a diff, and none of these defects are visible in one.

A second, cheaper miss: **#3825 was filed three times** (#3888, #3932, #3954)
and #3824 took 8 review rounds. When a fix fails re-test, nothing forces a
regression test that would have caught it. `grep -ri "regression test"` across
`CLAUDE.md`, `AGENTS.md`, every charter and every runbook returns **nothing**.
254 acceptance failures have produced 254 fixes and zero accumulated net.

### 2.3 New-hire agent syndrome — the ledger fixed the wrong half

D-005/#3774 put cross-session *decisions* in the repository, and that was
right. But the churn is not in decisions, it is in **intra-issue working
state**. A rejected PR spawns an 81-turn `review-fix` agent that re-derives
what the 189-turn `implement` agent already knew and did not write down.
Nothing carries: what was tried, what failed and why, which approach was
rejected, what the reviewer actually meant.

Meanwhile the fix is developing the disease. `agents/LEDGER.md` is **90KB /
13,059 words / 1,442 lines**, read in full by every agent run, still described
in its own header as "deliberately short enough to read in full". It grows
several entries a day, D-021 forbids reflowing it (cache), and its sequential
IDs now collide across concurrent branches often enough to be 7% of blocking
review findings.

---

## 3. Recommendation on "throw it out and start over"

**No — but do a deletion pass, and it should be the next sprint's whole scope.**

The artifact layer works and is the only reason any of this is measurable:
issues, native relationships, board lanes, the drain gate, the smoke family,
the executed-verification rule, the retro dumps. Rebuilding those reproduces
them.

What has accumulated is the *control* layer: **87 workflows, 31 agent scripts,
26 libs, and 24,099 words of charters, runbooks and prompts.** Nearly every one
is a scripted answer to a specific past failure — which is precisely the
pattern §9 of `AGENTIC_SDLC_DESIGN.md` says does not work ("you have to think
of every possible vector of failure and script for that — it's untenable").
The system has been treating its own principle as advice.

A rewrite that keeps the same shape rebuilds the same pile in four months. A
reduction does not. Target: **≤ 30 workflows, ~6 named invariants as guards,
each runbook under 1,500 words.**

---

## 4. Proposal

Six changes, in dependency order. Each names the measurement it should move.

### P1 — A red or pending head is not reviewable (removes ~36% of findings)

`developer_submit_for_review.sh` refuses to submit while any required check is
red or pending; the review workflow refuses to start on a head whose checks are
not green. The reviewer never spends a round to report CI state.

*Moves:* blocking findings −36%; ~39 of 65 rejected items lose their first
rejection cause; each avoided round-trip is ~18M tokens.

### P2 — Paperwork is a script, not an agent (removes ~24% more)

PR-body template completeness, executed-evidence citation presence, and
docs-claim checks become pre-submit checks. Ledger IDs become
content-addressed (`D-2026-08-20-huddle-gate`) instead of sequential, so
concurrent branches cannot collide.

*Moves:* another ~24% of blocking findings; frees the review agent for the
judgment its Criticals show it is good at.

### P3 — Bounded rounds with a written hand-off (attacks the 189-turn round)

Every round gets a turn budget. On reaching it the agent writes
`.agent/issue-<N>/state.md` — what is done, what is left, what was tried and
failed, what the reviewer asked and how it was answered — and stops. The next
round reads **that file plus the diff**, not the repository. This is the ledger
idea applied where the churn actually is: the 1.42B-token repeat-round
onboarding tax.

*Moves:* implement rounds from 189 median turns toward ~60. At constant
tokens/turn that is roughly −2.2B of the 3.4B implement bucket over a
six-day window.

### P4 — The first-run rehearsal (the missing gate)

**Before an issue reaches Acceptance Testing, an agent installs the published
artifact on a clean target and drives the user's actual path** — install,
`nyxgpt up`, open the page, do the thing the issue promised, `nyxgpt down`,
uninstall — and attaches the transcript. Not a unit test, not a function-level
smoke: the path the owner would take.

Every one of the twelve sampled acceptance failures dies here. This is the
single highest-value change in this document, and nothing currently in the
pipeline is a substitute for it.

**Paired rule: every acceptance failure must land a check that fails on the
pre-fix commit and passes after**, and its user path is appended to the
rehearsal script. That is how 254 failures become a growing net instead of 254
one-off fixes. The evidence standard already exists (#3775) — it is applied to
*changes*, and needs to be applied to *failures*.

### P5 — Convergence cap

Three review rounds on one PR, or a second acceptance failure filed for the
same defect, returns the issue to the owner or scrummaster for re-spec. It does
not get a fourth attempt. #3824 (8 rounds) and #3825 (filed three times) are
what the absence of this rule costs.

### P6 — Every round carries a price

Fix the branch-name attribution so the 442 misfiled developer rounds land on
their issues, and post each issue's running token total on its PR. Cost becomes
visible at the moment someone can act on it. This is also the precondition for
the §9 intelligent watcher — it cannot judge spend anomalies while 61% of runs
are unattributed. (P-002's rejection of hard caps stands; this is attribution,
not a cap.)

### Already unparked, still the right call

P-001 (intelligent test selection) — full pytest + vitest on every push, every
review and up to three fix attempts per issue. It is a runner-minutes lever
rather than a token lever, and it also shortens the implement round.

---

## 5. Expected effect, stated as an estimate

P1 + P2 remove roughly 60% of blocking findings, which should remove a
comparable share of reject/re-fix round-trips at ~18M tokens each. P3 attacks
the largest bucket directly. Together they plausibly halve the current daily
burn. **This is an estimate from six days of data, not a promise** — and the
right way to hold it is to re-run the churn dump one week after P1–P3 land and
compare medians, the way §1(d) compares them here.

P4 and P5 are not token optimizations. They are the quality changes, and P4 is
the one that decides whether the 45% acceptance-failure rate moves at all.

---

## 6. What I did not check

- Whether `developer_submit_for_review.sh` and `claude-code-review.yml` can
  read required-check state at the points P1 needs them to (mechanism only; the
  data is plainly available to Actions).
- Whether a clean-target install rehearsal (P4) fits inside available runner
  types for every portability target — macOS and Kubernetes are the doubtful
  ones, and `macos-brew-smoke.yml` already proves the macOS half is possible.
- Cost in dollars anywhere in this document. No price sheet was configured at
  dump time; every figure here is tokens.
