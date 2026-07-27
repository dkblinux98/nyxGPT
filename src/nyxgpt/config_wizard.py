"""Schema, validation, and read/write helpers for the full-config web wizard (#3354).

The web admin Configuration Wizard (`/admin`) previously only exposed three
hot-configurable fields (default model, RAG on/off, log level). This module
defines every wizard-editable `config.ini` section/field, validates a
`{section: {key: value}}` payload against it, and applies validated updates
to `config.ini` -- which remains the single source of truth (#3194); this
module only reads and writes it, it never holds config state itself.

`src/nyxgpt/app.py` wires `WIZARD_SCHEMA`/`read_sections`/`validate_updates`/
`apply_updates` into `GET|POST /api/v1/config/sections`, and uses
`restart_components`/`observability_changed` to decide whether to offer a
service restart or reconcile the observability Compose stack after a save.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HOME_NYXGPT = Path.home() / ".nyxGPT"

LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class WizardValidationError(ValueError):
    """Raised internally by a single field validator; callers see `validate_updates`'s error list."""


def _validate_str(value: Any, *, allow_empty: bool = False) -> str:
    """Validate `value` is a string, stripped of surrounding whitespace."""
    if not isinstance(value, str):
        raise WizardValidationError("must be a string")
    v = value.strip()
    if not allow_empty and not v:
        raise WizardValidationError("must not be empty")
    return v


def _validate_optional_str(value: Any) -> str:
    """Validate `value` is a string, allowing an empty one."""
    return _validate_str(value, allow_empty=True)


def _validate_bool(value: Any) -> bool:
    """Validate `value` is a bool, or the string `"true"`/`"false"`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise WizardValidationError("must be a boolean")


def _validate_int(value: Any, *, min_value: int | None = None, max_value: int | None = None) -> int:
    """Validate `value` is an integer within the given inclusive bounds."""
    try:
        # Reject bools (a bool is an int subclass) and non-numeric strings.
        if isinstance(value, bool):
            raise ValueError
        v = int(value)
    except (TypeError, ValueError) as e:
        raise WizardValidationError("must be an integer") from e
    if min_value is not None and v < min_value:
        raise WizardValidationError(f"must be >= {min_value}")
    if max_value is not None and v > max_value:
        raise WizardValidationError(f"must be <= {max_value}")
    return v


def _validate_port(value: Any) -> int:
    """Validate `value` is a TCP port number (1-65535)."""
    return _validate_int(value, min_value=1, max_value=65535)


def _validate_positive_int(value: Any) -> int:
    """Validate `value` is an integer >= 1."""
    return _validate_int(value, min_value=1)


def _validate_host(value: Any) -> str:
    """Validate `value` is a non-empty hostname with no embedded whitespace."""
    v = _validate_str(value)
    if any(c.isspace() for c in v):
        raise WizardValidationError("must not contain whitespace")
    return v


def _validate_host_list(value: Any) -> str:
    """Validate `value` is a comma-separated list of hostnames, normalizing spacing."""
    v = _validate_str(value)
    hosts = [h.strip() for h in v.split(",") if h.strip()]
    if not hosts:
        raise WizardValidationError("must contain at least one host")
    if any(" " in h for h in hosts):
        raise WizardValidationError("hosts must not contain whitespace")
    return ", ".join(hosts)


def _validate_url(value: Any) -> str:
    """Validate `value` is an absolute `http://` or `https://` URL."""
    v = _validate_str(value)
    if not _URL_RE.match(v):
        raise WizardValidationError("must start with http:// or https://")
    return v


def _validate_log_level(value: Any) -> str:
    """Validate `value` is one of `LOG_LEVELS`, case-insensitively."""
    v = _validate_str(value).upper()
    if v not in LOG_LEVELS:
        raise WizardValidationError(f"must be one of {sorted(LOG_LEVELS)}")
    return v


@dataclass(frozen=True)
class FieldSpec:
    """One wizard-editable `config.ini` field.

    Attributes:
        key: Option name within its section.
        validator: Parses/validates a raw payload value, raising
            `WizardValidationError` on failure.
        secret: Never round-tripped in cleartext (see `read_sections`); an
            empty string on save means "leave the existing value unchanged".
        restart_component: `nyxgpt ops restart` target that must be bounced
            for a changed value to take effect, or `None` if it's already
            hot-reloaded per-request.
        observability: Whether changing this field should trigger a
            reconciliation of the observability Compose stack.
        default: The effective fallback value the matching `config.py`
            getter uses when the key is absent from `config.ini` (mirrored
            here, not imported, since the getters read live config rather
            than exposing their fallback as a constant). `None` for a field
            whose "unset" state is a genuinely empty/contextual value (e.g.
            `rag.embedding_model` falls back to `default_model` dynamically)
            rather than a fixed non-empty default worth labelling. Unused
            for `secret` fields -- those have no default concept here.
    """

    key: str
    validator: Callable[[Any], Any]
    secret: bool = False
    restart_component: str | None = None
    observability: bool = False
    default: str | None = None


@dataclass(frozen=True)
class SectionSpec:
    """A `config.ini` section exposed by the wizard, grouping related `FieldSpec`s."""

    section: str
    label: str
    fields: tuple[FieldSpec, ...]


WIZARD_SCHEMA: tuple[SectionSpec, ...] = (
    SectionSpec(
        "nyxgpt",
        "Core & model",
        (
            FieldSpec("default_model", _validate_str, default="llama3.1:8b"),
            FieldSpec("chat_timeout_seconds", _validate_positive_int, default="180"),
            FieldSpec("sessions_dir", _validate_str, default=str(_HOME_NYXGPT / "sessions")),
            FieldSpec("vectorstore_dir", _validate_str, default=str(_HOME_NYXGPT / "vectorstore")),
        ),
    ),
    SectionSpec(
        "logging",
        "Logging",
        (
            FieldSpec("level", _validate_log_level, default="INFO"),
            FieldSpec("dir", _validate_str, default=str(_HOME_NYXGPT / "logs")),
        ),
    ),
    SectionSpec(
        "ollama",
        "Model backend",
        (FieldSpec("base_url", _validate_url, default="http://127.0.0.1:11434"),),
    ),
    SectionSpec(
        "api",
        "API server",
        (
            FieldSpec("host", _validate_host, restart_component="api", default="127.0.0.1"),
            FieldSpec("port", _validate_port, restart_component="api", default="8000"),
        ),
    ),
    SectionSpec(
        "auth",
        "Authentication",
        (
            FieldSpec("enabled", _validate_bool, default="false"),
            FieldSpec("header", _validate_str, default="X-API-Key"),
            FieldSpec("api_key", _validate_optional_str, secret=True),
        ),
    ),
    SectionSpec(
        "rate_limit",
        "Rate limiting",
        (FieldSpec("enabled", _validate_bool, restart_component="api", default="false"),),
    ),
    SectionSpec(
        "rag",
        "RAG / retrieval",
        (
            FieldSpec("enable_chat_context", _validate_bool, default="false"),
            FieldSpec(
                "cassandra_hosts",
                _validate_host_list,
                restart_component="api",
                default="127.0.0.1",
            ),
            FieldSpec("cassandra_port", _validate_port, restart_component="api", default="9042"),
            FieldSpec(
                "cassandra_keyspace", _validate_str, restart_component="api", default="nyxgpt"
            ),
            FieldSpec(
                "cassandra_table", _validate_str, restart_component="api", default="rag_chunks"
            ),
            # No fixed default: falls back to `default_model` dynamically
            # (see `embeddings.py`'s `_embedding_cfg`), so unset is a
            # genuinely context-dependent empty value, not a hidden default.
            FieldSpec("embedding_model", _validate_str, restart_component="api", default=None),
        ),
    ),
    SectionSpec(
        "tracing",
        "Tracing",
        (
            FieldSpec(
                "enabled",
                _validate_bool,
                restart_component="api",
                observability=True,
                default="false",
            ),
            FieldSpec("service_name", _validate_str, default="nyxgpt-api"),
            FieldSpec("otlp_endpoint", _validate_url, default="http://localhost:4318/v1/traces"),
        ),
    ),
    SectionSpec(
        "error_tracking",
        "Error tracking",
        (
            FieldSpec(
                "enabled",
                _validate_bool,
                restart_component="api",
                observability=True,
                default="false",
            ),
            FieldSpec("dsn", _validate_optional_str, secret=True),
            FieldSpec("environment", _validate_str, default="development"),
        ),
    ),
    SectionSpec(
        "monitoring",
        "Monitoring",
        (FieldSpec("enabled", _validate_bool, observability=True, default="false"),),
    ),
    SectionSpec(
        "log_aggregation",
        "Log aggregation",
        (FieldSpec("enabled", _validate_bool, observability=True, default="false"),),
    ),
)

_SCHEMA_BY_SECTION: dict[str, SectionSpec] = {s.section: s for s in WIZARD_SCHEMA}


def _mask(value: str) -> str:
    """Mask a secret for display, keeping only a few edge characters."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def schema_summary() -> list[dict[str, Any]]:
    """Return `WIZARD_SCHEMA` as JSON-serializable metadata for the wizard UI."""
    return [
        {
            "section": s.section,
            "label": s.label,
            "fields": [
                {
                    "key": f.key,
                    "secret": f.secret,
                    "restart_component": f.restart_component,
                    "observability": f.observability,
                }
                for f in s.fields
            ],
        }
        for s in WIZARD_SCHEMA
    ]


def read_sections(cfg: ConfigParser) -> dict[str, dict[str, Any]]:
    """Return the *effective* value of every wizard-editable field, grouped by section.

    "Effective" means fallback-applied: a field absent from `config.ini`
    renders as the same default value the matching `config.py` getter (e.g.
    `get_tracing_config`) would actually use at runtime, not a blank string.
    Use `field_defaults` alongside this to tell an inherited default apart
    from an explicit setting that merely matches it.

    Secret fields are never returned in cleartext -- only whether one is set
    plus a masked preview (mirrors `_mask_api_key`/`GET /admin/access`).
    """
    out: dict[str, dict[str, Any]] = {}
    for section_spec in WIZARD_SCHEMA:
        section_out: dict[str, Any] = {}
        for f in section_spec.fields:
            if f.secret:
                raw = cfg.get(section_spec.section, f.key, fallback="")
                section_out[f.key] = {
                    "set": bool(raw.strip()),
                    "masked": _mask(raw) if raw.strip() else None,
                }
            elif cfg.has_option(section_spec.section, f.key):
                section_out[f.key] = cfg.get(section_spec.section, f.key)
            else:
                section_out[f.key] = f.default if f.default is not None else ""
        out[section_spec.section] = section_out
    return out


def field_defaults(cfg: ConfigParser) -> dict[str, dict[str, bool]]:
    """Return, per non-secret field, whether it's currently an inherited default.

    `True` means the key is absent from `config.ini` and `read_sections` is
    showing `FieldSpec.default` rather than something the user configured.
    Secret fields are omitted -- `read_sections`'s `set`/`masked` pair already
    conveys "not set" for those.
    """
    out: dict[str, dict[str, bool]] = {}
    for section_spec in WIZARD_SCHEMA:
        out[section_spec.section] = {
            f.key: not cfg.has_option(section_spec.section, f.key)
            for f in section_spec.fields
            if not f.secret
        }
    return out


def validate_updates(payload: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate a `{section: {key: value}}` wizard payload against `WIZARD_SCHEMA`.

    Returns `(validated, errors)`. `validated` only contains fields that
    passed validation; `errors` is a list of `"section.key: reason"` strings
    for anything rejected or unknown. An empty-string secret value is treated
    as "leave unchanged" and silently dropped rather than erroring, since the
    wizard never echoes secrets back for the user to resubmit unmodified.
    """
    validated: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    if not isinstance(payload, dict):
        return {}, ["payload must be an object of {section: {key: value}}"]

    for section, fields in payload.items():
        spec = _SCHEMA_BY_SECTION.get(section)
        if spec is None:
            errors.append(f"{section}: unknown section")
            continue
        if not isinstance(fields, dict):
            errors.append(f"{section}: must be an object of {{key: value}}")
            continue
        field_specs = {f.key: f for f in spec.fields}
        for key, value in fields.items():
            f = field_specs.get(key)
            if f is None:
                errors.append(f"{section}.{key}: unknown field")
                continue
            if f.secret and isinstance(value, str) and value.strip() == "":
                continue
            try:
                validated.setdefault(section, {})[key] = f.validator(value)
            except WizardValidationError as e:
                errors.append(f"{section}.{key}: {e}")

    return validated, errors


def _current_value(cfg: ConfigParser, section: str, key: str, new_value: Any) -> Any:
    """Read `section.key` from `cfg`, coerced to `new_value`'s type for a fair comparison.

    Without this, e.g. comparing the on-disk string `"8000"` to a validated
    int `8000` always reports a change (they're never `==`), which would
    falsely claim every save needs a restart/reconciliation.
    """
    if isinstance(new_value, bool):
        return cfg.getboolean(section, key, fallback=False)
    if isinstance(new_value, int):
        try:
            return cfg.getint(section, key, fallback=-1)
        except ValueError:
            return -1
    return cfg.get(section, key, fallback="")


def restart_components(validated: dict[str, dict[str, Any]], cfg: ConfigParser) -> list[str]:
    """Return the sorted `nyxgpt ops restart` targets touched by `validated`.

    Only fields whose value actually *changed* from what's on disk count --
    resubmitting the same host/port shouldn't claim a restart is needed.
    """
    components: set[str] = set()
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            if f.restart_component and _current_value(cfg, section, key, value) != value:
                components.add(f.restart_component)
    return sorted(components)


def observability_changed(validated: dict[str, dict[str, Any]], cfg: ConfigParser) -> bool:
    """Return True if any observability-linked field's value actually changed."""
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            if f.observability and _current_value(cfg, section, key, value) != value:
                return True
    return False


def apply_updates(
    cfg_path: Path, validated: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Write `validated` section/key values to `cfg_path`, preserving everything else.

    Mirrors the read-modify-write pattern `_apply_hot_config_updates` in
    app.py already uses: one `ConfigParser`, existing file read first, only
    touched keys changed, whole file rewritten. Returns the values written.
    """
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    if cfg_path.exists():
        parser.read(cfg_path)

    applied: dict[str, dict[str, Any]] = {}
    for section, fields in validated.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in fields.items():
            str_value = "true" if value is True else "false" if value is False else str(value)
            parser.set(section, key, str_value)
            applied.setdefault(section, {})[key] = value

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)

    return applied


def mask_applied(applied: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return `applied` with secret field values replaced by a masked preview.

    `apply_updates` returns real values (e.g. for activity-log summaries that
    only ever reference field *names*, never values). Anything reflected
    back to the client -- the wizard save response -- must go through this
    first so a secret never round-trips in cleartext over the API.
    """
    out: dict[str, dict[str, Any]] = {}
    for section, fields in applied.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        out[section] = {
            key: ({"set": True, "masked": _mask(str(value))} if field_specs[key].secret else value)
            for key, value in fields.items()
        }
    return out
