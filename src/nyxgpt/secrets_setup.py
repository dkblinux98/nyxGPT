"""Guided setup for human-provided, write-once secrets (#3505).

Three `config.ini` values still require a human to go fetch them from an
external service: `[auth] api_key` (nyxGPT can generate its own), `[openai]
api_key`, and `[github] pat`. This module is the single source of truth for
that guided flow -- plain-language name, what it's for, exactly where to get
it, masked entry, and format validation.

**Entry is CLI-only (`nyxgpt secrets setup`), by owner decision (#3805).**
The `/admin/secrets` screen and `POST /api/v1/config/secrets` were removed: a
secret typed into a browser crosses an HTTP request and the page's process on
its way to disk, and by the time the web UI is running, reaching it already
required these secrets. `GET /api/v1/config/secrets` remains as the
read-only, never-cleartext status counterpart (`secret_status` below).

Deliberately separate from `config_wizard.py`'s schema-driven wizard: that
wizard excludes the `openai`/`github` sections entirely (they're
agent-system concerns, not nyxGPT user options, per #3388's
`EXCLUDED_SECTIONS`) and has no notion of per-field "where to obtain this"
guidance or a generate-for-me offer. Writes still go through
`config_wizard.apply_updates`, which is schema-agnostic -- it merges into
config.ini byte-preserving everything else and chmods 0600, exactly the
guarantee this module needs too.
"""

from __future__ import annotations

import getpass
import secrets as secrets_module
import sys
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt import config_wizard, restart_state
from nyxgpt.config import DEFAULT_CONFIG_PATH


def _read_config(cfg_path: Path) -> ConfigParser:
    """Read `cfg_path` into a plain `ConfigParser`, without `nyxgpt.config.load_config`'s
    global hot-reload cache or startup validation (both wrong for a setup flow that may
    run against a config.ini still being built up, or a non-default `--config` path).
    """
    parser = ConfigParser()
    if cfg_path.exists():
        parser.read(cfg_path, encoding="utf-8")
    return parser


class SecretValidationError(ValueError):
    """Raised by a `GuidedSecret.validator` when an entered value is malformed."""


def _validate_nonempty(value: str) -> str:
    """Require a non-empty, whitespace-free value (the common case)."""
    v = value.strip()
    if not v:
        raise SecretValidationError("must not be empty")
    if any(c.isspace() for c in v):
        raise SecretValidationError("must not contain whitespace")
    return v


def _validate_min_length(min_length: int) -> Callable[[str], str]:
    """Build a validator requiring `_validate_nonempty` plus a minimum length.

    Real tokens from these services are always well over this length; a
    short value is almost certainly a typo or a placeholder, so this catches
    it before it's written to disk rather than failing silently at first use.
    """

    def _validate(value: str) -> str:
        v = _validate_nonempty(value)
        if len(v) < min_length:
            raise SecretValidationError(f"must be at least {min_length} characters")
        return v

    return _validate


def _validate_github_pat(value: str) -> str:
    """Validate a GitHub PAT's shape: non-empty, no whitespace, a known prefix."""
    v = _validate_min_length(20)(value)
    known_prefixes = ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")
    if not v.startswith(known_prefixes):
        raise SecretValidationError(
            "doesn't look like a GitHub token (expected it to start with "
            f"one of {', '.join(known_prefixes)})"
        )
    return v


def _generate_auth_api_key() -> str:
    """Generate a strong `[auth] api_key`, mirroring `wizard.py`'s first-run generation."""
    return secrets_module.token_urlsafe(32)


@dataclass(frozen=True)
class GuidedSecret:
    """One human-provided, write-once secret the guided flow walks through.

    Attributes:
        section: `config.ini` section name.
        key: Option name within `section`.
        label: Plain-language name shown to the user.
        description: What this secret is for.
        obtain: Where to get it (a URL or a short instruction).
        validator: Parses/validates raw input, raising `SecretValidationError`.
        generate: Optional zero-argument generator offered instead of manual
            entry (only `[auth] api_key` has one -- nyxGPT can mint its own
            shared secret; OpenAI/GitHub tokens must come from those services).
    """

    section: str
    key: str
    label: str
    description: str
    obtain: str
    validator: Callable[[str], str]
    generate: Callable[[], str] | None = None

    @property
    def full_key(self) -> str:
        """Return the `section.key` identifier used in API payloads and messages."""
        return f"{self.section}.{self.key}"


GUIDED_SECRETS: tuple[GuidedSecret, ...] = (
    GuidedSecret(
        section="auth",
        key="api_key",
        label="API authentication key",
        description=(
            "Shared secret clients must send (in the `X-API-Key` header by default) to "
            "call the nyxGPT API when [auth] enabled = true."
        ),
        obtain="nyxGPT can generate a strong one for you -- no external service involved.",
        validator=_validate_nonempty,
        generate=_generate_auth_api_key,
    ),
    GuidedSecret(
        section="openai",
        key="api_key",
        label="OpenAI API key",
        description="Lets nyxGPT call OpenAI models alongside (or instead of) local Ollama models.",
        obtain="https://platform.openai.com/api-keys -- create a new secret key.",
        validator=_validate_min_length(20),
    ),
    GuidedSecret(
        section="github",
        key="pat",
        label="GitHub Personal Access Token",
        description=(
            "Authenticates the GitHub agent system's automation (issue/PR operations) and "
            "`nyxgpt ops secrets-sync`'s calls to the GitHub Actions secrets API."
        ),
        obtain="https://github.com/settings/tokens -- generate a token with `repo` scope.",
        validator=_validate_github_pat,
    ),
)

_BY_FULL_KEY: dict[str, GuidedSecret] = {s.full_key: s for s in GUIDED_SECRETS}


def find_guided_secret(section: str, key: str) -> GuidedSecret | None:
    """Look up a `GuidedSecret` by section/key, or None if it isn't one of `GUIDED_SECRETS`."""
    return _BY_FULL_KEY.get(f"{section}.{key}")


def mask_secret(value: str) -> str:
    """Mask a secret for display, keeping only a few edge characters."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def secret_status(cfg: ConfigParser) -> list[dict[str, Any]]:
    """Return `GUIDED_SECRETS` metadata plus each one's current set/masked state.

    Never returns cleartext -- shared by the CLI's status output and
    `GET /api/v1/config/secrets`, so a value can never round-trip to a client.
    """
    out = []
    for s in GUIDED_SECRETS:
        raw = cfg.get(s.section, s.key, fallback="").strip()
        out.append(
            {
                "section": s.section,
                "key": s.key,
                "full_key": s.full_key,
                "label": s.label,
                "description": s.description,
                "obtain": s.obtain,
                "can_generate": s.generate is not None,
                "set": bool(raw),
                "masked": mask_secret(raw) if raw else None,
            }
        )
    return out


def validate_secret_value(spec: GuidedSecret, value: str) -> str:
    """Validate `value` against `spec.validator`, raising `SecretValidationError` on failure."""
    return spec.validator(value)


def write_secret(cfg_path: Path, spec: GuidedSecret, value: str) -> str:
    """Validate and write `value` for `spec` into `cfg_path`.

    Delegates to `config_wizard.apply_updates` for the actual write, so a
    guided-secret save gets the same byte-preserving merge and 0600 chmod as
    every other config.ini write path. Returns the validated value written
    (never logged or echoed back -- callers must mask before it leaves the
    process boundary).

    Also records the pending restart the write implies (#3806). `[auth]
    api_key` is classified restart-required for `web`, so rotating it here
    raises exactly the same notice the Configuration Wizard would have
    raised -- one behavior, two surfaces. `restart_notice` renders it for
    this process's own output; the persisted state is what lets the running
    Admin Dashboard show it too.
    """
    validated = validate_secret_value(spec, value)
    previous = _read_config(cfg_path).get(spec.section, spec.key, fallback="")
    config_wizard.apply_updates(cfg_path, {spec.section: {spec.key: validated}})
    for component in config_wizard.field_restart_components(spec.section, spec.key):
        restart_state.mark_pending(component, {spec.full_key: previous})
        restart_state.reconcile_saved(component, {spec.full_key: validated})
    return validated


def restart_notice(spec: GuidedSecret) -> str | None:
    """Return the plain-language "not yet in effect" notice for `spec`, or None.

    None when `spec` is hot-reloadable (every consumer re-reads it), which is
    the case for the `[openai]`/`[github]` tokens -- they have no long-lived
    process holding a stale copy. Non-None text names the affected service(s)
    and the wrapped command that applies the value, never a raw
    `brew services`/`docker` command.
    """
    components = config_wizard.field_restart_components(spec.section, spec.key)
    if not components:
        return None
    services = ", ".join(components)
    return (
        f"Saved, but NOT YET IN EFFECT: {spec.full_key} is read once at start by: {services}.\n"
        f"  Until you restart, the saved value and the running value differ.\n"
        f"  Apply it with: {restart_state.restart_command(list(components))}\n"
        "  (You can defer -- the Admin Dashboard and Configuration Wizard keep showing this\n"
        "   notice until the restart happens.)"
    )


def _print_restart_notice(spec: GuidedSecret) -> None:
    """Print `restart_notice(spec)` right after a save, if the key is restart-required."""
    notice = restart_notice(spec)
    if notice:
        print(f"\n! {notice}\n")


def _prompt_masked(prompt: str) -> str:
    """Prompt for masked input via `getpass`, handling Ctrl-D/Ctrl-C like the rest of the CLI."""
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n\nSecrets setup cancelled.")
        sys.exit(0)


def run_secrets_setup(cfg_path: Path | None = None, reconfigure: bool = False) -> int:
    """Run the interactive guided secrets setup: `nyxgpt secrets setup`.

    Walks `GUIDED_SECRETS` in order. For each one already set in `config.ini`,
    skips it (prints its masked value) unless `reconfigure=True` -- this is
    what makes the command idempotent/safe to re-run. `[auth] api_key` offers
    to generate a value instead of pasting one in. Every value round-trips
    through `getpass` (never echoed) and format-validated before being
    written, with up to 3 retries on a validation failure before skipping
    that secret.

    Returns:
        0 on success (including an all-skipped run), 1 if the user cancelled.
    """
    if cfg_path is None:
        cfg_path = DEFAULT_CONFIG_PATH

    print("=" * 60)
    print("nyxGPT Guided Secrets Setup")
    print("=" * 60)
    print(f"\nSecrets are written to: {cfg_path}")
    print("Values are never echoed back to the screen.\n")

    cfg = _read_config(cfg_path)

    for spec in GUIDED_SECRETS:
        print("-" * 60)
        print(f"{spec.label}  [{spec.full_key}]")
        print(spec.description)
        print(f"Where to get it: {spec.obtain}")

        existing = cfg.get(spec.section, spec.key, fallback="").strip()
        if existing and not reconfigure:
            print(
                f"Already set ({mask_secret(existing)}) -- skipping. Use --reconfigure to change it."
            )
            continue
        if existing and reconfigure:
            print(f"Currently set ({mask_secret(existing)}).")

        if spec.generate is not None:
            choice = input("Generate a value automatically? (Y/n): ").strip().lower()
            if choice in ("", "y", "yes"):
                value = spec.generate()
                write_secret(cfg_path, spec, value)
                print(f"Generated and saved ({mask_secret(value)}).")
                _print_restart_notice(spec)
                cfg = _read_config(cfg_path)
                continue

        attempts = 0
        while attempts < 3:
            value = _prompt_masked(
                f"Enter {spec.label} (blank to skip): "
                if not existing
                else f"Enter new {spec.label} (blank to keep current): "
            )
            if not value.strip():
                print("Skipped.")
                break
            try:
                write_secret(cfg_path, spec, value)
            except SecretValidationError as e:
                attempts += 1
                print(f"Invalid value: {e}")
                continue
            print(f"Saved ({mask_secret(value.strip())}).")
            _print_restart_notice(spec)
            cfg = _read_config(cfg_path)
            break
        else:
            print(f"Too many invalid attempts -- skipping {spec.label}.")

    print("-" * 60)
    print("\nGuided secrets setup complete.")
    print(
        "Write-once tokens (OpenAI, GitHub) won't be shown again by the issuing service -- "
        "config.ini is now their canonical copy. Docker Compose/CI copies derived from it "
        "(`nyxgpt ops env-sync`, `nyxgpt ops secrets-sync`) must be re-run after any change here."
    )

    # The closing summary repeats the pending set from disk rather than from
    # what this run happened to write: a restart deferred during an earlier
    # run (or raised by the Configuration Wizard) is still owed, and this is
    # the last moment the CLI has the user's attention (#3806).
    pending = restart_state.snapshot()
    if pending:
        print("\n" + "!" * 60)
        print("RESTART REQUIRED -- saved values are not yet in effect:")
        for component in sorted(pending):
            keys = ", ".join(pending[component]["keys"])
            print(f"  {component}: {keys}")
        print(f"\nApply them with: {restart_state.restart_command(sorted(pending))}")
        print("You can defer -- this notice persists until the restart happens.")
        print("!" * 60)
    return 0


__all__ = [
    "GuidedSecret",
    "GUIDED_SECRETS",
    "SecretValidationError",
    "find_guided_secret",
    "mask_secret",
    "restart_notice",
    "secret_status",
    "validate_secret_value",
    "write_secret",
    "run_secrets_setup",
]
