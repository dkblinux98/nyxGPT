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
| 6–7 | #2684 Materialized views, #2685 Read replicas | **Recommend descope** (see §6) — poor fit for a single-node, local-first Cassandra deployment | — |

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

**Optional (as a Docker Compose profile, not core):**
- #2696 Grafana dashboards — consuming #2693's metrics
- #2697 Log aggregation — if kept, Loki (lightweight) in the same optional profile; full ELK is oversized

**Defer to a future v3 / close as out-of-scope (move to existing `Phase X: Rejected` milestone):**
- #2688 Kubernetes, #2690 Terraform, #2691 blue-green, #2692 canary — cloud deployment machinery with no local-first user
- #2694 distributed tracing — no distributed system exists to trace
- #2695 Sentry — third-party telemetry contradicts "no silent data exfiltration"; if ever kept, strictly opt-in

**Backlog (not release-blocking):**
- #2844 Workflow log collection/analytics — ops nicety for the agent loop itself; do last or defer

---

## 6. Decisions required from the human owner

Per VISION.md these cannot be delegated:

1. **Descope #2684 (materialized views) and #2685 (read replicas)?** Both presume multi-node Cassandra; recommend closing as out-of-scope or reducing to documentation of the config knobs.
2. **Ratify the Phase 5 re-scope** in §5 (keep 5, optional 2, defer 6).
3. **Phase acceptance** of merged-but-unaccepted "In Review" items on the project board → For Release.

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
