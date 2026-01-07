# ARCHITECTURE.md

> This file is the **constraints/invariants** agents must not violate.
> Your existing `/docs/architecture.md` can describe the system more narratively.

## Core invariants
- CLI remains functional and is a first-class interface.
- FastAPI is the stable integration surface for UIs/automation.
- UIs (web/TUI) are clients; they must not become required for core operation.
- Model runtime is pluggable behind stable interfaces.
- Persistence is explicit and configurable.

## Dependency flow
UI -> API -> domain -> adapters (IO)
- IO (HTTP, filesystem, DB, LLM calls) must be isolated behind interfaces.
- No “god modules”; keep boundaries clear and testable.

## Branching invariant
- Long-lived branches: `master`/`main` + exactly one active `release/*`.
- All feature/fix branches are short-lived and must be deleted after merge.

## Config & secrets
- No secrets committed to the repo.
- New required external dependencies require human approval.

## Quality
- New features require appropriate tests (unit and/or integration).
- CI must be green prior to merge (unless human explicitly authorizes an exception).
