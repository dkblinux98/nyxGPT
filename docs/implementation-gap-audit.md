# Implementation Gap Audit

**Date:** 2026-03-07
**Scope:** GitHub Actions workflows, Python config options, recent merged PRs
**Trigger:** Post-#3143 review — structured output feature was emitted but not consumed downstream, raising the question of how many other "implemented" features have the same gap.

**Updated:** Post-implementation — initial audit findings were partially inaccurate. Corrections noted below.

---

## Summary

| # | Component | Gap | Severity | Status |
|---|-----------|-----|----------|--------|
| 1 | Workflow: `validate-web-routes.yml` | Targets `v1.0.0` branch, not `v2.0.0` — never runs | HIGH | ✅ Fixed |
| 2 | Config: `adaptive_mode_enabled` | ~~Getters defined, imported, never called~~ **FALSE — fully implemented** | ~~HIGH~~ | N/A |
| 3 | Config: `response_cache_enabled` | ~~Flag read, cache never initialized~~ **FALSE — fully implemented** | ~~MEDIUM~~ | N/A |
| 4 | Config: `embedding_cache` disk backend | ~~Disk backend not implemented~~ **FALSE — DiskCache fully implemented** | ~~MEDIUM~~ | N/A |
| 5 | Config: `embedding_gpu_enabled` | ~~No GPU logic follows~~ **FALSE — GPU detection and batch scaling implemented** | ~~MEDIUM~~ | N/A |
| 6 | Config: `embedding_adaptive_batching` | ~~Flag never read~~ **FALSE — `_get_optimal_batch_size()` reads and uses it** | ~~MEDIUM~~ | N/A |
| 7 | Workflow: `track_progress` | Set on all Claude action steps, never consumed downstream | LOW | ✅ Fixed |

---

## Findings

### GAP 1 — `validate-web-routes.yml` Targets Wrong Branch (HIGH) ✅ Fixed

**File:** `.github/workflows/validate-web-routes.yml` line 11

The workflow triggered on push to `v1.0.0` but the active release branch is `v2.0.0`. This validation never ran during development.

**Fix:** Changed `v1.0.0` → `v2.0.0`.

---

### GAPS 2–6 — Audit Findings Were Incorrect

Upon deeper code inspection, all five Python findings were false positives:

- **GAP 2 (adaptive prompt):** `_detect_prompt_mode()` at `chat.py:212` and `_get_prompt_template()` at `chat.py:233` are fully implemented and wired into the chat loop at `chat.py:458-466`.
- **GAP 3 (response cache):** `_get_response_cache()` at `chat.py:103` initializes the cache; lookup at `chat.py:702` and write at `chat.py:729` are both wired.
- **GAP 4 (embedding disk cache):** `DiskCache` is fully implemented in `cache.py:166-283` and selected at `rag/embeddings.py:164-168` when `embedding_cache_backend = disk`.
- **GAP 5 (GPU):** `_detect_gpu()` at `rag/embeddings.py:188-247` runs nvidia-smi; result feeds `_get_optimal_batch_size()` at `rag/embeddings.py:320-329` to scale batch sizes. Ollama handles actual GPU device selection internally — there is no client-side device assignment for HTTP-based embedding.
- **GAP 6 (adaptive batching):** `adaptive_batching` is read at `rag/embeddings.py:118` and the value drives the full adaptive sizing logic in `_get_optimal_batch_size()` at `rag/embeddings.py:285-332`.

The initial audit agent did not read the files deeply enough and reported these as gaps incorrectly.

---

### GAP 7 — `track_progress` Not Consumed Downstream (LOW) ✅ Fixed

**Files affected:**
- `.github/workflows/claude-code-review.yml`
- `.github/workflows/developer_auto_implement.yml`

`track_progress: true` causes the Claude action to post live checkbox progress comments to the PR/issue during execution. No downstream step was reading those checkboxes to verify Claude completed all planned tasks.

**Fix:** Added "Check Claude progress completion" steps after each Claude action step that:
1. Fetches the most recent progress comment (identified by checkbox markdown `- [ ]` / `- [x]`)
2. Counts checked vs total items
3. Posts a `::warning::` annotation if any items remain unchecked
4. Exposes `progress_complete`, `progress_done`, `progress_total` as step outputs
5. The "Post implementation complete comment" in the developer workflow now includes the progress count in its message

This makes `track_progress` programmatically meaningful — incomplete work surfaces as a workflow warning rather than being silently ignored.

---

## Confirmed Working (Never Gaps)

- **`--json-schema` in `claude-code-review.yml`** — present at line 404, producing structured output. Persist step correctly captures it. Developer workflow extraction (commit `66f4fc7`) is correctly wired.
- **All Python config features (GAPs 2–6)** — all fully implemented in `chat.py` and `rag/embeddings.py`.
