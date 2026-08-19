# nyxGPT v2.0.0 Completion Plan

**Prepared:** 2026-07-06
**Scope:** Everything required to close release tracker #2759 (v2.0.0) and declare the project complete per VISION.md's definition of done.

---

## 1. Where the project stands

**Codebase:** Mature and feature-rich. Python core (CLI, FastAPI, TUI, RAG with Cassandra vector search, hybrid BM25+vector fusion, reranking, multi-collection embeddings, OCR ingestion, ops tooling), Next.js web UI with PWA support, code splitting, and virtualized lists. 1,036 test functions across unit and integration suites.

**Release status (tracker #2759):**

| Phase | Status |
|---|---|
| Phase 1–3 | Complete (4 leftover issues remain: #2629, #2630, #2633, #2635) |
| Phase 4 – Scale & Performance | 11 of 19 done; 7 open + 1 in-flight (#2679 / PR #3145) |
| Phase 5 – Enterprise Features | 0 of 13 started |
| Bugs | #3112 (SSE parse error, Acceptance Failure) open |

**Activity:** The agent pipeline last ran 2026-03-07. The last commit to `v2.0.0` was 2026-05-21 (manual Cassandra log-rotation fixes). The project has been idle ~6 weeks, and the automated loop has been stalled for ~4 months.

**Open items:** 28 open issues (including the tracker itself), 1 open PR.

---

## 1a. What the closed-issue history shows (199 closed issues examined)

**Delivery record.** v1.0.0 shipped (tracker #2709 closed 2026-01). Closed issues by milestone: Phase 2 UX 90, Phase 1 Quality/Security 40, Phase 3 Intelligence 29, Phase 4 21, Phase 0 Scaffolding 11, Phase 3.5 Post-1.0 Fixes 4, Phase X: Rejected 3. All but 3 closed as `completed` (2 duplicates, 1 not-planned).

**Velocity.** 177 issues closed in January 2026 (the v1.0.0 push with the agent loop at full throttle), 16 in February, 6 in March, **zero since March 7**. The loop is capable of very high throughput when healthy — the bottleneck is loop health, not implementation capacity.

**Rework rate.** 77 of 199 closed issues (39%) carry the **Acceptance Failure** label — follow-up fixes spawned by review/acceptance loops. The trend is strongly positive: Phase 2 was 52% rework (47/90), Phase 1 50% (20/40), but Phase 4 only 14% (3/21). Planning implication: budget roughly **1 acceptance-failure follow-up per 5–7 features** at current quality levels; the Sprint estimates in §8 include this buffer.

**Overlap with remaining work — three open Phase 4 issues are increments on shipped work, not greenfield:**
- #2687 (query result caching) — #2618 already shipped embedding + response caching; this extends `cache.py` to RAG query results.
- #2681 (service worker/offline) — #2682 already shipped the PWA (`@ducanh2912/next-pwa` integrated); only offline-fallback/cache-strategy gaps remain.
- #2680 (bundle size) — #2678 already shipped code splitting and vendor chunking; this is an audit/prune pass.

**#3112 is a same-day regression** from the SSE framing feature #2621 (closed 2026-02-13; bug filed 2026-02-13). The fix should include the fragmented-frame test that #2621 evidently lacked.

**The last work before the stall was loop-repair work** — #3132/#3143 (MCP servers, structured output, progress tracking in agent workflows), #3134 (acceptance-failure comment trigger to reopen + reassign), #3140 (jobs API + git identity fixes). The owner was already hardening the pipeline; the PR-branch-targeting bug in §2.1 is the piece that was missed.

**Precedents worth reusing:**
- The **`Phase X: Rejected` milestone already exists** as a parking spot for rejected/duplicate work — the Phase 5 descopes in §5 can be moved there (owner action; no new milestone needed).
- The **acceptance-failure reopen trigger (#3134)** and `handle_acceptance_failure.yml` are in place — once the loop is unwedged, #3112 can be driven through the normal automated path rather than by hand.

---

## 2. Why the pipeline is stalled — fix this first (Sprint 0)

### 2.1 PR #3145 is wedged in a review loop

The review agent rejected PR #3145 (image optimization, closes #2679) three times for ineffective tests. The developer agent produced correct fixes **twice**, but pushed them to new timestamped branches (`claude/issue-2679-20260307-2109`, `claude/issue-2679-20260307-2123`) instead of the PR's head branch (`claude/issue-2679-20260307-1826`). The PR itself never changed, so every re-review sees identical code and rejects again.

**Root cause:** The review-fix path in `developer_auto_implement.yml` relies on prompt instructions telling Claude to check out the PR head branch, but `claude-code-action` defaults to creating its own `claude/issue-N-<timestamp>` branch. The instruction is not reliably followed and nothing enforces it.

**Actions:**
1. **Land #3145 manually** — push the contents of `claude/issue-2679-20260307-2123` (verified correct fix: `imageConfig` export + tests that render ChatPane) onto the PR head branch, re-request review, merge. Closes #2679. Effort: ~30 min.
2. **Patch `developer_auto_implement.yml`** — in the review-fix job, pin the working branch by passing the PR head ref explicitly to the action (checkout ref + the action's branch input) so commits can only land on the PR branch. Without this fix, every future REQUEST_CHANGES cycle will wedge the same way. Effort: small, but test with a deliberate review-rejection round-trip before resuming the loop.

### 2.2 Open bug #3112 — SSE `Unexpected end of JSON input`

`ChatPane.tsx` calls `JSON.parse()` on SSE event data that can arrive fragmented across chunks. Introduced/exposed by the SSE framing work (#2621 / PR #3111). This is the only open **Acceptance Failure** and is user-facing.

**Action:** Buffer incoming SSE stream by complete `\n\n`-delimited frames before parsing; guard empty/partial `data:` payloads. Add a test streaming a JSON payload split mid-token across chunks. Effort: S.

### 2.3 Housekeeping

- Tracker #2759 checkboxes for #3138 and #3140 are stale (issues closed, boxes unchecked) — sync them (the `auto-check-tasklist` workflow evidently missed these; check why while in there).
- Stale branches from the wedged loop (`feat/2679-auto`, the two orphaned fix branches) — delete after #3145 merges.

**Sprint 0 total: ~1–2 days. Nothing else should proceed until 2.1 is done, because the entire automated loop depends on it.**

> **SPRINT 0 COMPLETED 2026-07-07.** PR #3145 merged (closes #2679) and PR #3146 merged (closes #3112), both APPROVEd by myGPT-review-agent and auto-merged by the pipeline. Fixes landed on v2.0.0 along the way: deterministic review-fix→PR-branch sync step (+ empty-branch guard) in `developer_auto_implement.yml`, and a pre-existing mypy baseline error in `app.py` that was REQUEST_CHANGES-blocking every PR at the review gate. Residual known issues for later: the `workflow_dispatch` path of `claude-code-review.yml` resolves the PR from the branch instead of the `pr_number` input (post-steps fail; the review itself posts), and the review-fix Claude step ran with restricted tools (no Edit/Write) in its last execution — watch the next organic review-fix cycle.

> **PHASE 4 COMPLETED 2026-07-08.** Sprint 1 ran fully autonomously overnight 07-07→07-08: #2680 (PR #3147), #2681 (PR #3150), #2683 (PR #3151), #2686 (PR #3152), #2687 (PR #3153) all implemented, reviewed, and merged by the agent loop. The review loop caught and self-healed three genuine defects (background-sync rollback bug, Cassandra batch-payload overflow, query-cache reindex invalidation gap) and self-corrected one policy violation (doc-nit-only REQUEST_CHANGES superseded by APPROVE). Phase 4 final: 17 delivered, 2 descoped. The review-fix path residual concern above is resolved — it converged correctly in four consecutive organic cycles. Remaining release scope: #3148, #3149 (hygiene), #2629, #2630, #2633 (Phase 1), #2635 (Phase 2), #2689/#2693/#2698/#2699/#2700 + optional #2696/#2697 (ratified Phase 5), #2844 (non-blocking backlog). Owner actions pending: Phase 4 stakeholder acceptance sweep → For Release; close the Phase 4 milestone.

---

## 3. Sprint 1 — Finish Phase 4 (7 open issues)

Recommended order, with notes from code inspection:

| Order | Issue | Notes | Effort |
|---|---|---|---|
| 1 | #2687 Query result caching | `cache.py` (344 lines) already exists — extend to RAG query results with query fingerprinting + TTL + hit-rate counters | M |
| 2 | #2686 Optimize vector search | Cassandra 5 ANN tuning, batch search in `vectorstore_cassandra.py` | M |
| 3 | #2683 Cassandra query optimization | Pairs naturally with #2686; do back-to-back | M |
| 4 | #2681 Service worker for offline | **Mostly done already** — `@ducanh2912/next-pwa` is integrated in `next.config.ts`. Verify offline fallback page + cache strategies, fill gaps, close | S |
| 5 | #2680 Bundle size reduction | Code splitting/vendor chunking already substantial; run bundle analyzer, prune deps | S |
| — | #2684 Materialized views, #2685 Read replicas | **Descoped 2026-07-06 by owner decision** — require multi-node Cassandra; closed as not planned, moved to Phase X: Rejected, tracker #2759 annotated | done |

---

## 4. Sprint 2 — Early-phase leftovers (quality & security)

These four directly serve the local-first vision, are low-risk, and complete the Phase 1/2 quality story:

| Issue | Notes | Effort |
|---|---|---|
| #2629 Security test suite | Auth bypass, session-name injection, API key validation, rate limiting, input sanitization — mostly exercising existing mechanisms | M |
| #2630 Security best practices guide | Docs only | S |
| #2633 Deployment checklist | Docs only | S |
| #2635 Mobile-responsive web UI | Largest UI item remaining; collapsible sidebar, touch targets | M |

---

## 5. Sprint 3 — Phase 5, re-scoped

Phase 5 as written (Kubernetes, Terraform, canary/blue-green deployment, ELK, Jaeger, Sentry) targets cloud/enterprise infrastructure. This **conflicts with VISION.md's non-negotiables** — local-first, no silent data exfiltration, practical on a single workstation. Per VISION.md, changing phase scope is a **human-only decision**, so this section is a recommendation for the owner to ratify:

**Keep (real local-first value):**
- #2689 Docker Compose for full stack — the single most valuable Phase 5 item; one-command bring-up of API + web + Cassandra + Ollama wiring
- #2693 Prometheus metrics endpoint — lightweight `/metrics`; `resource_monitor.py` already collects most of the data
- #2699 System health dashboard — builds on `ops doctor` + existing resource dashboard
- #2698 Admin dashboard improvements
- #2700 Usage analytics — local-only, privacy-safe stats

**Promoted to required by owner 2026-07-08 (packaged as Docker Compose profiles):**
- #2696 Grafana dashboards — consuming #2693's metrics
- #2697 Log aggregation — Loki + promtail (lightweight) in the same profile; full ELK remains out of scope

**Defer to a future v3 / close as out-of-scope (move to existing `Phase X: Rejected` milestone):**
- ~~#2688 Kubernetes~~ (**reversed by owner 2026-07-07**: "kubernetes can be implemented locally" — reopened, re-scoped to local-cluster kind/minikube/k3s deployment, implemented via PR #3158), #2690 Terraform, #2691 blue-green, #2692 canary — cloud deployment machinery with no local-first user
- #2694 distributed tracing — no distributed system exists to trace
- #2695 Sentry — third-party telemetry contradicts "no silent data exfiltration"; if ever kept, strictly opt-in

**Backlog (not release-blocking):**
- #2844 Workflow log collection/analytics — ops nicety for the agent loop itself; do last or defer

---

## 6. Decisions required from the human owner

Per VISION.md these cannot be delegated:

1. ~~**Descope #2684 (materialized views) and #2685 (read replicas)?**~~ **DECIDED 2026-07-06:** owner approved descoping the multi-node Cassandra issues. Both closed as not planned, moved to Phase X: Rejected, reassigned to the human owner per the closed-issue convention (agents/runbooks/review-runbook.md), tracker #2759 annotated. *Remaining manual step:* update their project-board Status (Projects v2 field — only settable via `scripts/agents/lib/gh_project.sh` with gh CLI, or manually on the board).
2. ~~**Ratify the Phase 5 re-scope** in §5 (keep 5, optional 2, defer 6).~~ **RATIFIED 2026-07-07 by owner.** Executed same day: #2688, #2690, #2691, #2692, #2694, #2695 closed as not planned → Phase X: Rejected with per-issue rationale; #2696/#2697 annotated with the optional Compose-profile scope (Loki-lite instead of ELK) and sequencing (#2689 → #2693 → #2696 → #2697); tracker #2759 annotated. Remaining Phase 5 scope: #2689, #2693, #2698, #2699, #2700, #2696, #2697 (all required as of 2026-07-08) + capstone #3160. **Amendment 2026-07-09:** the owner reinstated the remaining five descoped issues, each re-scoped local-first (reversal comments on each issue): #2690 Terraform→local IaC (docker/kind providers), #2691 blue-green→local zero-downtime switch, #2692 canary→local rollout with metrics-based promotion via #2693, #2694 tracing→local OTel+Jaeger Compose profile, #2695 Sentry→self-hosted GlitchTip Compose profile. The 'defer to v3' list is now empty; ALL Phase 5 issues are in scope with local-first requirements. **Amendment 2026-07-07/08:** the owner reversed the #2688 descope ("kubernetes can be implemented locally") — reopened and re-scoped to a local-cluster (kind/minikube/k3s) deployment of the FastAPI backend, consistent with VISION.md; implementation submitted as PR #3158.
3. **Phase acceptance** of merged-but-unaccepted "In Review" items on the project board → For Release.
4. ~~**Agent identity for interactive sessions** (§6a)~~ **DECIDED 2026-07-06:** reuse `myGPT-scrummaster-agent` for administrative actions; implemented via SessionStart hook. Remote-session agent identity is architecturally impossible (proxy credential injection) — remote writes route through Actions triggers instead.

---

## 6a. Identity & attribution policy (standing point of contention)

**Principle (owner requirement):** the defined agents must be the actors and authors of the activities they perform. The human owner's GitHub user should author only human-only decisions (VISION.md: scope changes, acceptance, releases).

**Infrastructure already in place:** bot accounts `myGPT-scrummaster-agent`, `myGPT-developer-agent`, `myGPT-review-agent` with `*_AGENT_TOKEN` secrets wired through the agent workflows; scripts in `scripts/agents/` export the role's token as `GH_TOKEN`; #3140 fixed `claude[bot]` commit attribution by adding `github_token` to the claude-code-action steps.

**Remaining attribution leaks, audited 2026-07-06:**

| Leak | Wrong actor today | Correct actor | Fix |
|---|---|---|---|
| Remote Claude Code sessions (web) | `dkblinux98` — the environment proxy injects the connected user's credentials into every `api.github.com` request and **overrides any Authorization header** (verified 2026-07-06) | role being performed | **Not fixable in-session** — agent PATs cannot take effect behind the proxy. Policy: remote sessions do GitHub *reads* only; writes route through the Actions triggers (both comment tokens named here were retired by #3882 — dispatch is now a `repository_dispatch` and rework is an assignment; the acceptance-failure trigger and `@claude` remain) or a local session. Enforced as a SessionStart warning (`.claude/hooks/session-start.sh`) |
| Local Claude Code sessions | whoever's PAT is in `~/.nyxGPT/config.ini` `[github] pat` / gh's own auth | scrummaster-agent (owner decision, see below) | SessionStart hook wires `GH_TOKEN` from `SCRUMMASTER_AGENT_TOKEN` in `~/.nyxGPT/config.ini`; role scripts still override with their own tokens |
| `auto-check-tasklist.yml` (tracker checkbox edits) | `github-actions[bot]` (default token) | scrummaster-agent | Use `SCRUMMASTER_AGENT_TOKEN` in the github-script step |
| `add-to-release-issue-on-milestone.yml` | `github-actions[bot]` | scrummaster-agent | Same |
| `notify-merge-conflicts.yml` (2 steps) | `github-actions[bot]` | scrummaster- or review-agent | Same pattern; owner to pick the role |

**DECIDED 2026-07-06 (owner):** ad-hoc administrative/executive-assistant work **reuses `myGPT-scrummaster-agent`** — no fourth bot account. Implemented via `.claude/hooks/session-start.sh` (registered in `.claude/settings.json`): local sessions export `GH_TOKEN` from `SCRUMMASTER_AGENT_TOKEN` so interactive `gh` commands are scrummaster-authored; remote sessions get an attribution warning injected into session context instead, because the proxy makes agent identity impossible there.

**Applied to Sprint 0:** the manual unwedging of PR #3145 must be executed under agent identities — the test-fix push as `myGPT-developer-agent` (`DEVELOPER_AGENT_TOKEN`), the re-review and merge as `myGPT-review-agent` (via `review_accept_and_merge.sh` / the review workflow) — not as the human owner. From a remote session this requires the agent PATs to be provisioned as environment secrets first, or the work routed through the existing `workflow_dispatch`/comment-trigger paths that already run under the correct tokens.

**Acknowledged violation (this session, 2026-07-06):** the descope of #2684/#2685 was executed under `dkblinux98` because that is the only identity available to remote sessions today. The *decision* being attributed to the owner is correct (descoping is human-only), but the mechanical edits (comments, reassignment, tracker annotation) would properly have been scrummaster-agent actions. GitHub does not allow reassigning authorship after the fact; the fix is forward-looking (rows above).

---

## 7. Release closure checklist

When Sprints 0–3 are merged to `v2.0.0`:

1. Full regression: `pytest` (unit + integration), web test suite, `nyxgpt ops doctor`, manual smoke of chat/RAG/TUI/web flows.
2. Docs pass: README + `docs/` updated for all user-facing changes; write release notes into #2759.
3. Human owner: stakeholder acceptance sweep, move accepted issues → For Release.
4. Human owner: merge `v2.0.0` → `master`, tag `v2.0.0`, close tracker #2759 and the Phase 4/5 milestones.
5. Delete merged feature branches; disable or re-point the agent loop.

---

## 8. Timeline summary

| Stage | Content | Duration |
|---|---|---|
| Sprint 0 | Unwedge pipeline, land #3145, fix #3112, workflow fix | 1–2 days |
| Sprint 1 | Phase 4 remainder (5 issues; 2 descoped) | 1–2 weeks |
| Sprint 2 | Quality/security leftovers (4 issues) | ~1 week |
| Sprint 3 | Phase 5 re-scoped (5–7 issues) | ~2 weeks |
| Closure | Regression, docs, acceptance, tag | 2–3 days |

**Total: roughly 5–6 calendar weeks** with the agent loop running, assuming the Sprint 0 workflow fix holds. The critical path is Sprint 0 — every subsequent stage depends on the develop→review→merge loop actually converging.

Two calibration points from the closed-issue history (§1a): the loop closed 177 issues in January when healthy, so the ~16 remaining feature issues are well within a few weeks of loop capacity; and the recent rework rate (~14% in Phase 4) means expect roughly 2–3 acceptance-failure follow-ups across Sprints 1–3, which the estimates absorb.
