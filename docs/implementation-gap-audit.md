# Implementation Gap Audit

**Date:** 2026-03-07
**Scope:** GitHub Actions workflows, Python config options, recent merged PRs
**Trigger:** Post-#3143 review — structured output feature was emitted but not consumed downstream, raising the question of how many other "implemented" features have the same gap.

---

## Summary

| # | Component | Gap | Severity |
|---|-----------|-----|----------|
| 1 | Workflow: `validate-web-routes.yml` | Targets `v1.0.0` branch, not `v2.0.0` — never runs | HIGH |
| 2 | Config: `adaptive_mode_enabled` | Getters defined, imported, never called in business logic | HIGH |
| 3 | Config: `response_cache_enabled` | Flag read, cache never initialized or used | MEDIUM |
| 4 | Config: `embedding_cache` disk backend | Config options exist, disk backend not implemented | MEDIUM |
| 5 | Config: `embedding_gpu_enabled` | Flag read, no GPU logic follows | MEDIUM |
| 6 | Config: `embedding_adaptive_batching` | Flag never read anywhere in codebase | MEDIUM |
| 7 | Workflow: `track_progress` | Set on all Claude action steps, never consumed downstream | LOW |

---

## Findings

### GAP 1 — `validate-web-routes.yml` Targets Wrong Branch (HIGH)

**File:** `.github/workflows/validate-web-routes.yml` lines 10-15

The workflow triggers on push to `v1.0.0` but the active release branch is `v2.0.0`. This validation never runs during development.

```yaml
push:
  branches:
    - v1.0.0  # should be v2.0.0
```

**Impact:** Web route validation is silently skipped on every push to the active branch.

---

### GAP 2 — Adaptive Prompt Mode Never Executes (HIGH)

**Files:**
- `src/nyxgpt/config.py` — getters `get_prompt_mode_adaptive_enabled`, `get_prompt_mode_short_threshold`, `get_prompt_mode_long_threshold` defined
- `src/nyxgpt/chat.py` line 17 — getters imported
- `~/.nyxGPT/config.ini` `[prompt]` section — `adaptive_mode_enabled`, `short_threshold`, `long_threshold` configurable

The getters are defined, exported, and imported into `chat.py` but are never called. No code path checks `adaptive_mode_enabled` or selects different prompting behaviour based on conversation length. The feature is entirely dead — config is parsed, nothing happens.

**Impact:** Users who enable `adaptive_mode_enabled = true` in config see no change in behaviour.

---

### GAP 3 — Response Cache Flag Read, Cache Never Initialized (MEDIUM)

**File:** `src/nyxgpt/chat.py` line 118

```python
cache_enabled = _get_bool(cfg, "cache", "response_cache_enabled", False)
```

The flag is read into `cache_enabled` but no subsequent code uses it. There is no `ResponseCache` initialization, no cache lookup before LLM calls, no cache write after responses. The variable is assigned and discarded.

**Impact:** `response_cache_enabled = true` in config has no effect.

---

### GAP 4 — Embedding Cache Disk Backend Not Implemented (MEDIUM)

**Files:**
- `src/nyxgpt/rag/embeddings.py` line 148 — `cache_enabled` flag is read
- `~/.nyxGPT/config.ini` `[cache]` section — `embedding_cache_backend`, `embedding_cache_dir`, `embedding_cache_ttl_seconds` etc. are configurable

`MemoryCache` exists and is partially wired. However, the `embedding_cache_backend = disk` option has no implementation — no code path selects a disk backend or uses `embedding_cache_dir`. Config options for disk caching describe a feature that doesn't exist.

**Impact:** Any disk caching config is silently ignored. Memory caching may be partially functional but the backend selection logic is incomplete.

---

### GAP 5 — GPU Flag Read, No GPU Logic Follows (MEDIUM)

**File:** `src/nyxgpt/rag/embeddings.py` line 117

```python
enable_gpu = cfg.getboolean("rag", "embedding_gpu_enabled", fallback=False)
```

`enable_gpu` is set but never referenced again in the file. No conditional batch size adjustment, no GPU device selection, no `nvidia-smi` detection. Setting `embedding_gpu_enabled = true` changes nothing.

**Impact:** GPU acceleration is entirely non-functional despite being a config option.

---

### GAP 6 — Adaptive Batching Flag Never Read (MEDIUM)

**File:** `~/.nyxGPT/config.ini` `[rag]` section — `embedding_adaptive_batching`

`grep -r "embedding_adaptive_batching" src/nyxgpt/` returns no results. The config key is defined in the example config but is never read anywhere in the codebase. Batch sizing is static.

**Impact:** Config option is entirely cosmetic.

---

### GAP 7 — `track_progress` Set But Not Consumed (LOW)

**Files:**
- `.github/workflows/claude-code-review.yml` line 399
- `.github/workflows/developer_auto_implement.yml` lines 319, 446, 533, 730, 831, 1182

`track_progress: true` is set on all Claude Code action steps. The action generates live progress checkbox updates on PRs/issues during execution — this is valuable for visibility. However, no downstream step reads or acts on the progress output. This is more of an omission than a gap — the feature works as intended (checkboxes appear on the PR), it just isn't programmatically consumed, which is fine.

**Impact:** Minimal. Progress is visible to humans in the PR; automation doesn't need to read it.

---

## Not a Gap (Confirmed Working)

- **`--json-schema` in `claude-code-review.yml`** — present at line 404 and producing structured output. The persist step at line 413 correctly captures `steps.claude-review.outputs.structured_output`. The developer workflow extraction added in commit `66f4fc7` is correctly wired.
- **`track_progress`** — works as intended for human visibility; low severity that it isn't programmatically consumed.

---

## Recommended Fixes

Priority order:

1. **GAP 1** — One-line fix: change `v1.0.0` → `v2.0.0` in `validate-web-routes.yml`
2. **GAP 2** — Either implement adaptive prompt selection in `chat.py` or remove the dead config/getters
3. **GAP 3** — Either implement `ResponseCache` and wire it in `chat.py` or remove the flag and config section
4. **GAPs 4, 5, 6** — Audit `rag/embeddings.py` for all dead config reads; implement or remove
