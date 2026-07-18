# nyxGPT v2.0.0 — Design & Codebase Audit

**Date:** 2026-07-15
**Scope:** All phases except Phase X (rejected multi-node/cloud items). Focus: feature gaps vs. plan, technical debt, security, and design weaknesses.
**Method:** Read-only static review of `src/nyxgpt/` (~17.6k LOC), `.github/workflows/`, config, and deploy assets against `VISION.md` / `ARCHITECTURE.md` / `CLAUDE.md`. This is a focused security + architecture + CI pass, **not** an exhaustive line-by-line review of all 17.6k LOC — a deeper pass (data-flow of RAG ingestion, session concurrency, the 4.6k-line `app.py` endpoint by endpoint) would surface more.

Findings are ranked by severity. Severity reflects the **deployed** posture (the capstone ships a k8s config that binds `0.0.0.0`), not just the default localhost workstation.

---

## Runtime defects (found during v2.0.0 acceptance testing)

### D1 — HIGH: Docker/stdout logs flooded with `KeyError: 'request_id'` tracebacks
`RequestIdFilter` is attached to the **root logger** (`src/nyxgpt/logging.py:209`,
`root.addFilter(...)`). In Python's logging, a filter on a *logger* only runs for records
logged directly to that logger — it is **not** applied to records that propagate up from
child loggers. The text formatter `DEFAULT_FMT` (`logging.py:11`) hard-requires
`%(request_id)s`. So every propagated record from `uvicorn.access`, `uvicorn.error`,
`httpx`, `fastapi` reaches the formatter with no `request_id` attribute → `KeyError` →
a `--- Logging error ---` traceback printed per line. Pollutes container logs and buries
real errors. (Confirmed statically; matches the runtime symptom observed in acceptance.)
- **Fix:** attach the filter to the **handlers** (`ch.addFilter(...)`, `fh.addFilter(...)`),
  which run for propagated records; and/or make the formatter resilient with
  `logging.Formatter(fmt=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT, defaults={"request_id": "N/A"})`
  (Python 3.10+). *Label: Acceptance Failure · Module: API · Effort: XS*

---

## Security

### S1 — HIGH: Arbitrary file read via `/api/v1/tools/{cat,ls,grep}`
`tools_fs.cat/ls/grep` resolve a caller-supplied path with `Path(req.path).expanduser().resolve()` and read it, with **no sandbox/root restriction** (`src/nyxgpt/app.py:2708-2732`, `src/nyxgpt/tools_fs.py`). Any caller can read any file the server process can — including `~/.nyxGPT/config.ini` (which holds the plaintext API key), SSH keys, `/etc/passwd`.

- **Reachability:** API auth is **off by default** (`_auth_cfg` → `enabled fallback=False`, `app.py:714`). The capstone's `k8s/configmap.yaml:26` sets `host = 0.0.0.0`, so a deployed instance is network-reachable with this open.
- **Notable contrast:** the log endpoints (`/api/v1/logs/view/{filename}`, `app.py:4470+`) **do** guard traversal (basename + resolved-parent check). The codebase knows the pattern; it just wasn't applied to `tools/*`. This is an inconsistency, not unfamiliarity.
- **Fix:** constrain `tools/*` to an allowlisted workspace root (reject paths whose `resolve()` escapes it), mirroring the logs-endpoint guard. Consider gating `tools/*` behind auth unconditionally.

### S2 — MEDIUM: API unauthenticated by default; only `/api/v1/*` is ever protected
Auth defaults to disabled, and even when enabled the middleware only protects `/api/v1` (`app.py:588-600`). Combined with the `0.0.0.0` deploy config, a deployed nyxGPT exposes its full API with no credential unless the operator explicitly turns auth on. The "deploy the whole app" capstone story makes this a realistic exposure, not a theoretical one.
- **Fix:** when `api.host` is not loopback, refuse to start (or log a loud warning + require auth). Document auth-on as the deploy default in the deployment checklist (#2633).

### S3 — MEDIUM: `config.ini` written world-readable; holds plaintext API key
Config is persisted with `cfg_path.open("w")` (`app.py:778, 837`) — default mode (0644 minus umask), no `chmod 0o600`. The file stores the API key in cleartext. On any shared host, other local users can read it.
- **Fix:** `os.chmod(cfg_path, 0o600)` after write; create parent dir `0o700`.

### S4 — MEDIUM: No security scanning in CI
No `bandit` (Python SAST), `pip-audit`/`safety` (dependency CVEs), `npm audit` (web deps), or CodeQL anywhere in `.github/workflows/`. Vulnerable transitive dependencies and injection-shaped code land unflagged.
- **Fix:** add a scan job (bandit + pip-audit + `npm audit --audit-level=high`) to a push/PR-triggered workflow.

**Positive:** subprocess use in `self_heal.py`/`deploy.py`/`canary.py` is list-form with no `shell=True`, and the self-heal service name is filtered against actually-running containers (`self_heal.py:258-268`) — no command injection there. Deploy target is allowlisted against `COLORS` (`app.py:1353`).

---

## CI / Quality Gates (technical debt)

### C1 — MEDIUM: No independent Python test gate on push/PR
The Python suite (`pytest`, 91 test files) runs **only inside the agent workflows** — `developer_auto_implement.yml` (implementation) and `claude-code-review.yml` (review) — and only over `tests/unit/`. There is **no** standalone push/PR-triggered workflow running the full Python suite. A direct human push to `v2.0.0`, or any PR not routed through the review agent, merges with **zero Python test signal**. (Web now has an independent vitest gate via `validate-web-routes.yml`; Python has no equivalent.)
- **Fix:** add a `pytest` job to a push/PR CI workflow (the whole `tests/` tree, not just `tests/unit/`), matching the web gate.

### C2 — MEDIUM: mypy is non-blocking and non-strict
`validate-web-routes.yml:51` runs `mypy src/ || echo "⚠️ not blocking"`, and `pyproject.toml` has `disallow_untyped_defs = false`. Type regressions can't fail CI, and untyped defs are allowed — so type coverage silently erodes. (This is the same class of latent-rot that #3148 was filed to fix for web tests.)
- **Fix:** make mypy blocking on a defined module set; ratchet `disallow_untyped_defs` on per-package.

### C3 — LOW: Coverage reported but not gated
`--cov` is passed but there's no `--cov-fail-under`. Coverage can decline unnoticed.

---

## Architecture / Design

### A1 — MEDIUM: `app.py` is a 4,616-line monolith (~90 endpoints)
A single module holds chat, RAG, sessions, admin, deploy, canary, self-heal, logs, and model endpoints on one `APIRouter`. This works against `VISION.md`'s "clear boundaries / UI clients do not own business logic" and makes the file hard to test and reason about. The `APIRouter` abstraction is already imported — the refactor is mechanical.
- **Fix:** split into per-domain routers (`routers/rag.py`, `routers/sessions.py`, `routers/ops.py`, …) and thin `app.py` to wiring.

### A2 — MEDIUM: Self-heal only covers Docker Compose, not k8s
`self_heal.py` monitors/restarts via `docker compose ps` / `restart` only. But the deploy story also ships k8s (blue/green/canary) and Terraform paths. A component deployed via k8s is **not** covered by self-heal, so the "self-heal the whole app" promise holds only for the Compose path.
- **Fix:** either document Compose as the single supported self-heal substrate (and say so in the capstone docs), or add a k8s probe/restart backend.

### A3 — LOW: `auto-check-tasklist` workflow has a read-modify-write race
Known recurring bug — it clobbered the #2759 tracker checkboxes three times during the completion campaign (manually repaired each time). Concurrent runs overwrite each other's checkbox state.
- **Fix:** serialize with a concurrency group, or re-fetch + merge before write.

---

## Documentation & Configuration

### DC1 — RESOLVED: `example.config.ini` completeness
All 23 config *sections* are present. Two keys the code reads were genuinely absent and
have been **added** (this pass): `[api] base_url` (`tui.py:818`) and `[logging] format`
(`logging.py:184`, values `text`|`json`). Three others my initial scan flagged
(`[pdf] tesseract_cmd`, `[rag] context_format`, `[rag] instruction_template`) turned out
to be present as **commented examples** already — adequately documented. The example file
is now complete with respect to code-read keys.

### DC2 — RESOLVED: `docs/configuration.md` now documents all 23 config sections
The 8 previously-undocumented sections (`[batch] [cache] [canary] [deploy] [pdf]
[rate_limit] [self_heal] [web]`) were **added** this pass.

### DC3 — RESOLVED: documentation index added
A grouped `docs/README.md` index (User / Ops / Developer / Agent-system) now covers all
24 docs so none are orphaned, and the root README's Documentation section links to it.

**Positive:** no stale `[cassandra]`-section references remain in docs (the earlier
`performance.md` fix held); `docs/` is otherwise substantial (24 files, ~11.3k lines).

---

## Feature Gaps vs. Plan

### F1 — MEDIUM/HIGH: The "single command deploys everything, OS-aware" story does not exist yet
Confirmed by inspection — the capstone (#3160) delivered self-heal end-to-end but the
unified deploy story is not met. Now the headline scope of **Phase 6 / v3.0.0** (see
`product_management/PHASE_6_PLAN.md`). Concretely today:
- **No unified command.** Docker Compose, k8s (`kubectl apply`), Terraform (`terraform
  apply`), and native install (`nyxgpt ops install`) are four separate hand-invoked paths.
  No `nyxgpt up` / `make deploy` orchestrates them.
- **Prometheus/Grafana are opt-in Compose profiles** (`docker-compose.yml:108`). A bare
  `docker compose up` does **not** start them — so "one command brings up prometheus +
  grafana" is false as-is.
- **OS detection is Mac-first and incomplete.** `ops.py`'s native install path is entirely
  macOS (`launchctl`, launchagents, `.plist`, Homebrew); there is **no** Linux/systemd
  branch and no `platform.system()` dispatch. The Compose path is OS-agnostic only because
  Docker abstracts it.
- **Self-heal covers only Compose**, not the k8s deploy path (see A2).
These are the prerequisites addressed by Phase 6 Sprint 6.1 before the cloud work.

### F2 — (verify in acceptance) One-command full-stack deploy → working chat/RAG in browser
Acceptance criterion of #3160 that can only be confirmed at runtime on your Mac. Flagged for the smoke-test pass.

---

## Suggested triage order

1. **S1** (arbitrary file read) — file as an Acceptance Failure now; smallest fix, highest impact.
2. **C1 + S4** (Python CI gate + security scan) — one new workflow covers both; prevents future rot.
3. **S2 / S3** (auth-on-deploy, config perms) — deploy-hardening, pairs with the deployment checklist.
4. **F1** (capstone dashboard control) — scope decision for you after acceptance testing.
5. **A1** (app.py split) — larger refactor; schedule deliberately, not urgent.

Items S1–S4, C1–C3, A2, A3 are all suitable as agent-loop issues labeled **Acceptance Failure**. A1 and F1 warrant your scoping call first.
