# Phase 6 — Cloud Deployment & Unified Orchestration (v3.0.0)

**Created:** 2026-07-15
**Owner decision (2026-07-15):** add a Phase 6 milestone and a v3.0.0 release to deliver
cloud-native AWS deployment plus the unified one-command, OS-aware deploy story. Phase X is
unrelated to local-first and does not constrain this work; cloud-native provisioning is now
explicitly in scope for v3.

## Theme

One command, after a clean `git checkout`, brings up the **entire** nyxGPT stack —
API, web UI, Cassandra, host-wired Ollama, **and** the full monitoring stack
(Prometheus, Grafana, Loki, Jaeger) — with the deploy path **detecting macOS vs Linux**
and doing the right thing on each. That same capability extends to the cloud: one CLI
command provisions AWS infrastructure (Terraform) and deploys the full app onto a
Linux or macOS AWS instance, monitored and self-healing, operable from the SRE/admin
dashboard per the Definition of Done.

## Why a hardening sprint comes first

Two audit findings become **critical** the moment the API is exposed on a public cloud
endpoint rather than localhost:

- **S1** — `/api/v1/tools/{cat,ls,grep}` allow **arbitrary file read** (no path sandbox).
  On a public AWS endpoint this is remote arbitrary file disclosure of the host,
  including the API key in `~/.nyxGPT/config.ini`.
- **S2** — the API is **unauthenticated by default**, and the deploy config binds `0.0.0.0`.

These must land **before** any cloud exposure. Sprint 6.0 is therefore a gate, not optional.

---

## Sprint 6.0 — Deployment hardening (prerequisite gate)

Must merge before any public cloud exposure work in 6.2+.

1. **fix: Sandbox `/api/v1/tools/{cat,ls,grep}` to prevent arbitrary file read**
   Constrain resolved paths to an allowlisted workspace root, mirroring the existing
   guard on `/api/v1/logs/view/{filename}`. Reject any path whose `resolve()` escapes root.
   *Label: Acceptance Failure · Module: API · Effort: S*

2. **feat: Require auth when the API binds to a non-loopback host**
   If `api.host` is not loopback, refuse to start (or hard-warn) unless `[auth] enabled=true`.
   Document auth-on as the deploy default in the deployment checklist.
   *Label: Feature · Module: API · Effort: S*

3. **fix: Persist `config.ini` with 0600 permissions to protect the API key**
   `os.chmod(cfg_path, 0o600)` after every write; parent dir `0o700`.
   *Label: Acceptance Failure · Module: API · Effort: XS*

4. **feat: Add security scanning to CI (bandit, pip-audit, npm audit)**
   New push/PR-triggered job. Fail on high-severity dependency CVEs and Python SAST findings.
   *Label: Feature · Module: Agent System · Effort: S*

5. **feat: Add an independent push/PR Python test + mypy gate in CI**
   A standalone workflow that runs the full `tests/` suite and a blocking `mypy` on push/PR,
   independent of the agent pipeline (closes the C1/C2 gap: today pytest only runs inside
   the dev/review agent workflows, over `tests/unit/` only).
   *Label: Feature · Module: Agent System · Effort: S*

## Sprint 6.1 — Unified local deploy + OS detection

Closes the capstone (#3160) deploy-pillar gap: a real single command, monitoring included,
OS-aware.

6. **feat: `nyxgpt up` — one command brings up the full stack after checkout**
   Single entrypoint orchestrating the Compose stack (all core services), waiting for health,
   printing the web URL. Idempotent. Dashboard surface for status per DoD.
   *Label: Feature · Module: CLI · Effort: L*

7. **feat: Include Prometheus/Grafana/Loki/Jaeger in the default bring-up**
   Monitoring comes up as part of `nyxgpt up` (today they are opt-in Compose profiles that a
   bare `docker compose up` skips). `--no-monitoring` to opt out.
   *Label: Feature · Module: Observability · Effort: M*

8. **feat: macOS/Linux detection with platform-appropriate native install**
   Add a Linux path (systemd units) alongside the existing macOS-only launchd/launchctl path
   in `ops.py`; dispatch on `platform.system()`. Today the native install path is Mac-only.
   *Label: Feature · Module: CLI · Effort: L*

9. **feat: Single-command teardown + idempotent re-deploy for the unified stack**
   `nyxgpt down` / re-run `nyxgpt up` cleanly. Dashboard teardown control.
   *Label: Feature · Module: CLI · Effort: M*

## Sprint 6.2 — AWS IaC foundation

10. **feat: Architecture decision — AWS compute substrate (EC2 single-box vs EKS)**
    Decision issue with a recommendation and rationale; picks the target for the modules below.
    *Label: Feature · Module: Agent System · Effort: S*

11. **feat: Terraform AWS modules — VPC, subnets, security groups, compute**
    Cloud provider modules provisioning the chosen substrate (per #10). Least-privilege SGs.
    *Label: Feature · Module: CLI · Effort: XL*

12. **feat: Terraform remote state (S3 backend + DynamoDB lock)**
    *Label: Feature · Module: CLI · Effort: M*

13. **feat: Cloud secrets management (SSM Parameter Store / Secrets Manager) — no plaintext keys**
    API key and any credentials sourced from AWS secret storage, never baked into images/config.
    *Label: Feature · Module: API · Effort: M*

## Sprint 6.3 — One-command cloud deploy

14. **feat: `nyxgpt cloud deploy` — provision AWS + deploy the full app after checkout**
    Single CLI command: `terraform apply` the infra, then bring up the full stack on the
    provisioned Linux/macOS AWS instance, wired to the monitoring stack, returning the URL.
    *Label: Feature · Module: CLI · Effort: XL*

15. **feat: Target OS detection/provisioning for Linux vs macOS AWS instances**
    Provision and configure correctly whether the target AMI is Linux or macOS (EC2 mac).
    *Label: Feature · Module: CLI · Effort: L*

16. **feat: Cloud deploy status / teardown / rollback from the SRE dashboard**
    DoD surface: the whole cloud deploy lifecycle operable from `/admin`, not just the CLI.
    *Label: Feature · Module: Web UI · Effort: L*

17. **feat: Cloud smoke test — provision → verify chat/RAG over public endpoint → teardown**
    Mirrors `scripts/smoke-test.sh` but against a live AWS deployment.
    *Label: Feature · Module: Testing · Effort: M*

## Capstone (implement LAST)

18. **feat: Phase 6 capstone — deploy nyxGPT to AWS from a clean checkout in one command,
    monitored and self-healing**
    End-to-end acceptance: clean checkout → one command → provisioned, deployed, monitored,
    self-healing app reachable over the internet, entirely operable from the SRE dashboard;
    documented end-to-end smoke test; teardown. Depends on all of #6.0–#6.3.
    *Label: Feature · Module: CLI · Effort: XL · Sequencing: LAST*

---

## Sequencing rules

- Sprint 6.0 blocks 6.2/6.3 (no public exposure before the API is sandboxed + auth-gated).
- #18 (capstone) is selected LAST, only after #1–#17 are closed — same gate pattern as #3160.
- #10 (compute-substrate decision) blocks #11, #14, #15.

## Not in this milestone (recommend separate scheduling)

- **A1** — split the 4,616-line `app.py` monolith into per-domain routers. Pure refactor,
  no user-facing change; schedule as its own tech-debt issue, not gated on v3.
- **A3** — `auto-check-tasklist` read-modify-write race (workflow tooling hygiene).
- **C3** — coverage floor (`--cov-fail-under`).
