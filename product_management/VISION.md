# VISION.md

## Purpose
nyxGPT is a **local-first, developer-controlled** personal GPT system. It should be practical to run, modify, and extend on a single developer workstation while remaining operable and testable.

## Non‑negotiables
- Local-first by default; external services are opt-in and explicitly configured.
- Privacy-respecting: no silent data exfiltration.
- Predictable behavior: tests + CI are the source of truth.
- Clear boundaries: UI clients do not own business logic.
- Operability: logs, config, and failure modes are explicit.

## What “done” means
A milestone/phase is done when:
- All scoped issues are complete and merged to the active release branch.
- CI is green.
- Docs updated for user-facing changes.
- Stakeholder acceptance for the phase is completed by the human owner.

## Human-only decisions (never delegated)
- Changing phase definitions or ordering.
- Changing milestone scope.
- Approving architecture boundary changes.
- Security posture changes (secrets, permissions, external integrations).
- Final phase acceptance and closure.
