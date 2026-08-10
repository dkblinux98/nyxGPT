"""Schema, validation, and read/write helpers for the full-config web wizard (#3354, #3388).

The web admin Configuration Wizard (`/admin`) previously only exposed three
hot-configurable fields (default model, RAG on/off, log level), then #3354
added a hand-maintained schema covering 11 of `example.config.ini`'s 23
sections. That hand-maintained list silently drifted from the example file
(#3388) -- this module now *derives* `WIZARD_SCHEMA` from
`example.config.ini` itself at import time, so a new option added there
automatically appears in the wizard and a user's drifted `config.ini` can be
repaired through it, instead of the two ever silently diverging again.

`src/nyxgpt/app.py` wires `WIZARD_SCHEMA`/`read_sections`/`validate_updates`/
`apply_updates` into `GET|POST /api/v1/config/sections`, and uses
`restart_components`/`observability_changed` to decide whether to offer a
service restart or reconcile the observability Compose stack after a save.
`find_stale_keys`/`remove_keys` back the drift-reconciliation surface: a key
no longer declared in `example.config.ini` is reported, never removed
silently -- `apply_updates` itself never deletes anything.
"""

from __future__ import annotations

import importlib.resources
import os
import re
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# `example.config.ini` lives at the repo root; this module is
# `src/nyxgpt/config_wizard.py`, so two `.parent`s up is the repo root.
def _resolve_example_config_path() -> Path:
    """Locate example.config.ini, the wizard schema's source of truth (#3388).

    Resolution order:
      1. ``$NYXGPT_EXAMPLE_CONFIG`` -- explicit override. The Docker/Terraform/
         Kubernetes images install nyxgpt into site-packages, where the
         source-relative path below doesn't reach the repo-root file, so the
         image ships example.config.ini and points this env var at it (see
         Dockerfile).
      2. ``<package dir>/example.config.ini`` -- package-adjacent copy. When
         nyxgpt is installed into a venv (e.g. the self-contained Homebrew
         keg, #3406) the module lives at ``site-packages/nyxgpt/`` with no repo
         root above it, so the installer ships example.config.ini right next to
         this module. Found here, the app imports with no env var -- which the
         formula's ``test`` block and the always-on self-heal watchdog both
         rely on.
      3. ``<nyxgpt.resources>/example.config.ini`` -- packaged resource data
         (#3622). A bare `pip install nyxgpt` from PyPI (no Homebrew/systemd
         installer step to copy the package-adjacent case-2 file, no repo
         checkout for case 4 below) still needs `import nyxgpt.app` to work --
         example.config.ini is symlinked into `src/nyxgpt/resources/` the same
         way `.env.example` is (see `nyxgpt.resources` and #3621's
         importlib.resources treatment), so setuptools bundles a real copy
         into the wheel and this resolves with no repo checkout present.
      4. ``<repo root>/example.config.ini`` -- the source-checkout / local-first
         layout, where this module is at ``src/nyxgpt/config_wizard.py`` so
         ``parents[2]`` is the repo root.
    """
    override = os.environ.get("NYXGPT_EXAMPLE_CONFIG")
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent / "example.config.ini"
    if packaged.exists():
        return packaged
    resource = importlib.resources.files("nyxgpt.resources").joinpath("example.config.ini")
    if resource.is_file():
        return Path(str(resource))
    return Path(__file__).resolve().parents[2] / "example.config.ini"


_EXAMPLE_CONFIG_PATH = _resolve_example_config_path()

# Sections deliberately excluded from the wizard (owner decision, #3388):
# these are becoming *agent-level* concerns rather than nyxGPT options, not
# things a nyxGPT user configures. `openai` will resurface once external
# commercial LLM support is added -- excluded cleanly here rather than its
# support being deleted. `cloud` is excluded because it has its own guided
# flow instead (`aws_credentials_setup.py`, #3512): the actual AWS access
# key pair is never stored in this section (routed to ~/.aws/credentials or
# the OS keychain instead) and always travels together with that flow's
# destination choice, so editing `[cloud] profile`/`region` here without
# going through it could silently desync from where the key pair actually
# lives. Every other section is in scope: when in doubt the wizard covers a
# section rather than excluding it (owner decision, #3388).
EXCLUDED_SECTIONS = frozenset({"paths", "openai", "github", "cloud"})


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


def _validate_float(
    value: Any, *, min_value: float | None = None, max_value: float | None = None
) -> float:
    """Validate `value` is a number within the given inclusive bounds."""
    try:
        if isinstance(value, bool):
            raise ValueError
        v = float(value)
    except (TypeError, ValueError) as e:
        raise WizardValidationError("must be a number") from e
    if min_value is not None and v < min_value:
        raise WizardValidationError(f"must be >= {min_value}")
    if max_value is not None and v > max_value:
        raise WizardValidationError(f"must be <= {max_value}")
    return v


def _bounded_int(
    min_value: int | None = None, max_value: int | None = None
) -> Callable[[Any], int]:
    """Build a validator for an integer within `[min_value, max_value]`."""

    def _validate(value: Any) -> int:
        return _validate_int(value, min_value=min_value, max_value=max_value)

    return _validate


def _bounded_float(
    min_value: float | None = None, max_value: float | None = None
) -> Callable[[Any], float]:
    """Build a validator for a number within `[min_value, max_value]`."""

    def _validate(value: Any) -> float:
        return _validate_float(value, min_value=min_value, max_value=max_value)

    return _validate


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


def _enum_validator(*choices: str) -> Callable[[Any], str]:
    """Build a validator accepting only (case-insensitive) `choices`, lower-cased on return."""
    choice_set = {c.lower() for c in choices}

    def _validate(value: Any) -> str:
        v = _validate_str(value).lower()
        if v not in choice_set:
            raise WizardValidationError(f"must be one of {sorted(choice_set)}")
        return v

    return _validate


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
            hot-reloaded per-request (or has no known runtime consumer yet).
        observability: Whether changing this field should trigger a
            reconciliation of the observability Compose stack.
        default: The effective fallback value for this field -- normally
            the literal value declared in `example.config.ini`, since that
            file is authored to match each setting's actual code fallback.
            `None` for a field whose "unset" state is a genuinely
            empty/contextual value (e.g. `rag.embedding_model` falls back to
            `default_model` dynamically) rather than a fixed non-empty
            default worth labelling. Unused for `secret` fields -- those
            have no default concept here.
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


_UNSET = object()


@dataclass(frozen=True)
class _Override:
    """Metadata that can't be inferred from `example.config.ini`'s text alone.

    Any `(section, key)` not listed in `_FIELD_OVERRIDES` gets a validator
    inferred from its example value (bool/int/float/string) and is otherwise
    a plain hot-reloaded, non-secret field -- this table only needs entries
    for the exceptions: specific validation, secrets, and fields with a
    known restart/observability requirement.
    """

    validator: Callable[[Any], Any] | None = None
    secret: bool = False
    restart_component: str | None = None
    observability: bool = False
    default: Any = _UNSET


# Sections not listed here fall back to a humanized version of their name
# (e.g. `log_aggregation` -> "Log Aggregation").
_SECTION_LABELS: dict[str, str] = {
    "nyxgpt": "Core & model",
    "logging": "Logging",
    "cache": "Caching",
    "ollama": "Model backend",
    "api": "API server",
    "web": "Web UI server",
    "canary": "Canary rollout",
    "auth": "Authentication",
    "rate_limit": "Rate limiting",
    "batch": "Request batching",
    "tracing": "Tracing",
    "error_tracking": "Error tracking",
    "monitoring": "Monitoring",
    "log_aggregation": "Log aggregation",
    "self_heal": "Self-heal watchdog",
    "context": "Context window",
    "prompt": "Adaptive prompts",
    "rag": "RAG / retrieval",
    "pdf": "PDF ingestion (OCR)",
}

# Overrides for fields whose validation, secrecy, or restart/observability
# behaviour can't be inferred from `example.config.ini`'s text alone.
# `api`/`rag` cassandra/embedding entries mirror #3354's original hand-built
# schema so existing behaviour and tests are preserved exactly.
_FIELD_OVERRIDES: dict[tuple[str, str], _Override] = {
    ("nyxgpt", "system_prompt"): _Override(validator=_validate_optional_str),
    ("logging", "level"): _Override(validator=_validate_log_level),
    ("logging", "format"): _Override(validator=_enum_validator("text", "json")),
    ("cache", "embedding_cache_enabled"): _Override(
        validator=_validate_bool, restart_component="api"
    ),
    ("cache", "embedding_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_component="api"
    ),
    ("cache", "embedding_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_component="api"
    ),
    ("cache", "embedding_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_component="api"
    ),
    ("cache", "embedding_cache_dir"): _Override(restart_component="api"),
    ("cache", "response_cache_enabled"): _Override(
        validator=_validate_bool, restart_component="api"
    ),
    ("cache", "response_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_component="api"
    ),
    ("cache", "response_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_component="api"
    ),
    ("cache", "response_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_component="api"
    ),
    ("cache", "response_cache_dir"): _Override(restart_component="api"),
    ("cache", "query_cache_enabled"): _Override(validator=_validate_bool, restart_component="api"),
    ("cache", "query_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_component="api"
    ),
    ("cache", "query_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_component="api"
    ),
    ("cache", "query_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_component="api"
    ),
    ("cache", "query_cache_dir"): _Override(restart_component="api"),
    ("ollama", "base_url"): _Override(validator=_validate_url),
    ("api", "host"): _Override(validator=_validate_host, restart_component="api"),
    ("api", "port"): _Override(validator=_validate_port, restart_component="api"),
    ("api", "base_url"): _Override(validator=_validate_url),
    ("web", "host"): _Override(validator=_validate_host),
    ("web", "port"): _Override(validator=_validate_port),
    ("web", "api_base_url"): _Override(validator=_validate_optional_str),
    ("canary", "step_percent"): _Override(validator=_bounded_int(min_value=1, max_value=100)),
    ("canary", "total_replicas"): _Override(validator=_validate_positive_int),
    ("canary", "min_requests_for_evaluation"): _Override(validator=_validate_positive_int),
    ("canary", "error_rate_threshold_percent"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=100.0)
    ),
    ("canary", "latency_p95_threshold_ms"): _Override(validator=_bounded_float(min_value=0.0)),
    ("auth", "enabled"): _Override(validator=_validate_bool),
    ("auth", "api_key"): _Override(validator=_validate_optional_str, secret=True),
    ("rate_limit", "enabled"): _Override(validator=_validate_bool, restart_component="api"),
    ("rate_limit", "requests_per_second"): _Override(validator=_validate_positive_int),
    ("rate_limit", "burst_size"): _Override(validator=_validate_positive_int),
    ("batch", "enabled"): _Override(validator=_validate_bool, restart_component="api"),
    ("batch", "batch_size"): _Override(validator=_bounded_int(min_value=1, max_value=50)),
    ("batch", "wait_time_ms"): _Override(validator=_bounded_int(min_value=10, max_value=5000)),
    ("tracing", "enabled"): _Override(
        validator=_validate_bool, restart_component="api", observability=True
    ),
    ("tracing", "otlp_endpoint"): _Override(validator=_validate_url),
    ("tracing", "jaeger_ui_url"): _Override(validator=_validate_url),
    ("error_tracking", "enabled"): _Override(
        validator=_validate_bool, restart_component="api", observability=True
    ),
    ("error_tracking", "dsn"): _Override(validator=_validate_optional_str, secret=True),
    ("error_tracking", "release"): _Override(validator=_validate_optional_str),
    ("error_tracking", "traces_sample_rate"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=1.0)
    ),
    ("error_tracking", "glitchtip_ui_url"): _Override(validator=_validate_url),
    ("error_tracking", "admin_email"): _Override(validator=_validate_optional_str),
    ("error_tracking", "admin_password"): _Override(validator=_validate_optional_str, secret=True),
    ("monitoring", "enabled"): _Override(validator=_validate_bool, observability=True),
    ("monitoring", "grafana_ui_url"): _Override(validator=_validate_url),
    ("monitoring", "prometheus_ui_url"): _Override(validator=_validate_url),
    ("monitoring", "grafana_admin_password"): _Override(
        validator=_validate_optional_str, secret=True
    ),
    ("monitoring", "slack_webhook_url"): _Override(validator=_validate_optional_str, secret=True),
    ("log_aggregation", "enabled"): _Override(validator=_validate_bool, observability=True),
    ("log_aggregation", "grafana_explore_url"): _Override(validator=_validate_url),
    ("self_heal", "check_interval_seconds"): _Override(validator=_bounded_float(min_value=1.0)),
    ("self_heal", "max_consecutive_restarts"): _Override(validator=_validate_positive_int),
    ("self_heal", "backoff_seconds"): _Override(validator=_bounded_float(min_value=0.0)),
    ("context", "default_window_size"): _Override(
        validator=_bounded_int(min_value=100, max_value=1_000_000)
    ),
    ("context", "warning_threshold"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=1.0)
    ),
    ("prompt", "short_threshold"): _Override(validator=_validate_positive_int),
    ("prompt", "long_threshold"): _Override(validator=_validate_positive_int),
    ("rag", "cassandra_hosts"): _Override(validator=_validate_host_list, restart_component="api"),
    ("rag", "cassandra_port"): _Override(validator=_validate_port, restart_component="api"),
    ("rag", "cassandra_keyspace"): _Override(validator=_validate_str, restart_component="api"),
    ("rag", "cassandra_table"): _Override(validator=_validate_str, restart_component="api"),
    ("rag", "cassandra_pool_size"): _Override(
        validator=_bounded_int(min_value=1, max_value=16), restart_component="api"
    ),
    ("rag", "cassandra_health_check_interval"): _Override(
        validator=_bounded_float(min_value=5.0, max_value=300.0), restart_component="api"
    ),
    ("rag", "cassandra_reconnect_max_attempts"): _Override(
        validator=_bounded_int(min_value=1, max_value=10), restart_component="api"
    ),
    ("rag", "cassandra_batch_size"): _Override(validator=_bounded_int(min_value=1, max_value=100)),
    ("rag", "vector_similarity_function"): _Override(
        validator=_enum_validator("cosine", "dot_product", "euclidean")
    ),
    ("rag", "ann_oversample_factor"): _Override(
        validator=_bounded_float(min_value=1.0, max_value=5.0)
    ),
    ("rag", "cassandra_batch_query_concurrency"): _Override(
        validator=_bounded_int(min_value=1, max_value=32)
    ),
    # No fixed default: falls back to `default_model` dynamically (see
    # `embeddings.py`'s `_embedding_cfg`), so unset is a genuinely
    # context-dependent empty value, not a hidden default.
    ("rag", "embedding_model"): _Override(restart_component="api", default=None),
    ("rag", "embedding_dim"): _Override(validator=_validate_positive_int, restart_component="api"),
    ("rag", "chunk_size"): _Override(validator=_bounded_int(min_value=100, max_value=10_000)),
    ("rag", "chunk_overlap"): _Override(validator=_bounded_int(min_value=0, max_value=5_000)),
    ("rag", "overlap_strategy"): _Override(
        validator=_enum_validator("trailing", "sentence", "semantic")
    ),
    ("rag", "chat_top_k"): _Override(validator=_bounded_int(min_value=1, max_value=100)),
    ("rag", "min_score"): _Override(validator=_bounded_float(min_value=0.0, max_value=1.0)),
    ("rag", "good_score_threshold"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=1.0)
    ),
    ("rag", "medium_score_threshold"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=1.0)
    ),
    ("rag", "max_chunks"): _Override(validator=_bounded_int(min_value=1, max_value=100)),
    ("rag", "chat_context_max_chars"): _Override(
        validator=_bounded_int(min_value=100, max_value=100_000)
    ),
    ("rag", "bm25_b"): _Override(validator=_bounded_float(min_value=0.0, max_value=1.0)),
}


def _infer_validator(raw_value: str) -> Callable[[Any], Any]:
    """Infer a validator from `example.config.ini`'s literal value for a field with no override.

    Bool/int/float are inferred from the example's own value so a newly
    added option gets sane validation for free; anything else is treated as
    an (empty-allowed) string, since several example values are
    intentionally blank (e.g. optional URLs/paths).
    """
    v = raw_value.strip()
    if v.lower() in ("true", "false"):
        return _validate_bool
    if re.fullmatch(r"-?\d+", v):
        return _validate_int
    if re.fullmatch(r"-?\d+\.\d+", v):
        return _validate_float
    return _validate_optional_str


def _build_field_spec(section: str, key: str, raw_value: str) -> FieldSpec:
    """Build a `FieldSpec` for `section.key`, applying any `_FIELD_OVERRIDES` entry."""
    override = _FIELD_OVERRIDES.get((section, key))
    validator = (override.validator if override else None) or _infer_validator(raw_value)
    secret = override.secret if override else False
    default = (
        override.default
        if override is not None and override.default is not _UNSET
        else (None if secret else raw_value)
    )
    return FieldSpec(
        key=key,
        validator=validator,
        secret=secret,
        restart_component=override.restart_component if override else None,
        observability=override.observability if override else False,
        default=default,
    )


def _build_schema() -> tuple[SectionSpec, ...]:
    """Derive `WIZARD_SCHEMA` from `example.config.ini` (#3388).

    Every section in the example file is included except `EXCLUDED_SECTIONS`
    -- this is what keeps the wizard from ever silently truncating the
    options a user can see/repair, the bug this module was rewritten to fix.
    """
    if not _EXAMPLE_CONFIG_PATH.exists():
        raise FileNotFoundError(
            "Cannot derive the Configuration Wizard schema: missing "
            f"{_EXAMPLE_CONFIG_PATH} (example.config.ini is the schema's source of truth)"
        )
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")

    sections = []
    for section in parser.sections():
        if section in EXCLUDED_SECTIONS:
            continue
        fields = tuple(
            _build_field_spec(section, key, parser.get(section, key))
            for key in parser.options(section)
        )
        label = _SECTION_LABELS.get(section, section.replace("_", " ").title())
        sections.append(SectionSpec(section, label, fields))
    return tuple(sections)


WIZARD_SCHEMA: tuple[SectionSpec, ...] = _build_schema()

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


def find_stale_keys(cfg: ConfigParser) -> dict[str, list[str]]:
    """Return, per wizard-managed section present in `cfg`, options no longer declared.

    A key that exists in the user's file but isn't declared in the current
    `WIZARD_SCHEMA` for that section is either a retired option or something
    hand-added outside the wizard. Either way it's surfaced for the user to
    review via `GET /config/sections` -- never removed automatically (that's
    what `apply_updates` guarantees; only an explicit `remove_keys` call,
    made after the user confirms, can delete anything).
    """
    out: dict[str, list[str]] = {}
    for section_spec in WIZARD_SCHEMA:
        if not cfg.has_section(section_spec.section):
            continue
        known = {f.key for f in section_spec.fields}
        stale = sorted(k for k in cfg.options(section_spec.section) if k not in known)
        if stale:
            out[section_spec.section] = stale
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
    if isinstance(new_value, float):
        try:
            return cfg.getfloat(section, key, fallback=float("nan"))
        except ValueError:
            return float("nan")
    return cfg.get(section, key, fallback="")


def restart_required_detail(
    validated: dict[str, dict[str, Any]], cfg: ConfigParser
) -> dict[str, list[str]]:
    """Return, per `nyxgpt ops restart` target, the `section.key` fields that changed.

    Only fields whose value actually *changed* from what's on disk count --
    resubmitting the same host/port shouldn't claim a restart is needed. Used
    both to compute `restart_components`'s target list and, by the caller in
    `app.py`, to record *why* a restart is pending for the Admin Dashboard's
    restart-required button (#3407).
    """
    detail: dict[str, list[str]] = {}
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            if f.restart_component and _current_value(cfg, section, key, value) != value:
                detail.setdefault(f.restart_component, []).append(f"{section}.{key}")
    return detail


def restart_components(validated: dict[str, dict[str, Any]], cfg: ConfigParser) -> list[str]:
    """Return the sorted `nyxgpt ops restart` targets touched by `validated`.

    Only fields whose value actually *changed* from what's on disk count --
    resubmitting the same host/port shouldn't claim a restart is needed.
    """
    return sorted(restart_required_detail(validated, cfg))


def observability_changed(validated: dict[str, dict[str, Any]], cfg: ConfigParser) -> bool:
    """Return True if any observability-linked field's value actually changed."""
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            if f.observability and _current_value(cfg, section, key, value) != value:
                return True
    return False


def _ini_value_str(value: Any) -> str:
    """Render a validated Python value back to its `config.ini` text form."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


_SECTION_HEADER_RE = re.compile(r"^\[(.+)\]\s*$")


def _section_line_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each section name to its (start, end) body range within `lines`.

    `start` is the index right after the `[section]` header line; `end` is
    exclusive, right before the next section header (or `len(lines)`).
    """
    ranges: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start = 0
    for i, line in enumerate(lines):
        m = _SECTION_HEADER_RE.match(line.strip())
        if m:
            if current is not None:
                ranges[current] = (start, i)
            current = m.group(1).strip()
            start = i + 1
    if current is not None:
        ranges[current] = (start, len(lines))
    return ranges


def _find_key_line(lines: list[str], start: int, end: int, key: str) -> int | None:
    """Return the index of the last active `key = ...` line in `lines[start:end]`."""
    found = None
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        if "=" not in lines[i]:
            continue
        existing_key = lines[i].split("=", 1)[0].strip()
        if existing_key == key:
            found = i
    return found


def _last_nonblank(lines: list[str], start: int, end: int) -> int:
    """Return the index right after the last non-blank line in `lines[start:end]`."""
    for i in range(end - 1, start - 1, -1):
        if lines[i].strip():
            return i + 1
    return start


def _merge_ini_text(
    text: str,
    validated: dict[str, dict[str, Any]],
    remove: dict[str, list[str]] | None,
) -> tuple[str, dict[str, list[str]]]:
    """Merge `validated` values and `remove` deletions into `text`, byte-preserving everything else.

    This is a line-level merge, not a `ConfigParser.write()` regeneration:
    every comment, blank line, and key ordering the user's file already has
    survives untouched. Only the specific lines being updated, added, or
    removed are ever touched -- the non-destructive save this module exists
    to guarantee (#3388). Limitation: relies on one `key = value` per line
    (no multi-line continuation values), which matches every active key in
    `example.config.ini` today.
    """
    lines = text.splitlines()
    remove = remove or {}
    removed: dict[str, list[str]] = {}

    to_delete: list[int] = []
    for section, keys in remove.items():
        ranges = _section_line_ranges(lines)
        rng = ranges.get(section)
        if rng is None:
            continue
        for key in keys:
            idx = _find_key_line(lines, rng[0], rng[1], key)
            if idx is not None:
                to_delete.append(idx)
                removed.setdefault(section, []).append(key)
    for idx in sorted(set(to_delete), reverse=True):
        del lines[idx]

    for section, fields in validated.items():
        ranges = _section_line_ranges(lines)
        rng = ranges.get(section)
        if rng is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            for key, value in fields.items():
                lines.append(f"{key} = {_ini_value_str(value)}")
            continue

        start, end = rng
        for key, value in fields.items():
            idx = _find_key_line(lines, start, end, key)
            if idx is not None:
                prefix = lines[idx].split("=", 1)[0]
                lines[idx] = f"{prefix}= {_ini_value_str(value)}"
            else:
                insert_at = _last_nonblank(lines, start, end)
                lines.insert(insert_at, f"{key} = {_ini_value_str(value)}")
                ranges = _section_line_ranges(lines)
                start, end = ranges[section]

    text_out = "\n".join(lines)
    if lines:
        text_out += "\n"
    return text_out, removed


def apply_updates(
    cfg_path: Path, validated: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Write `validated` section/key values to `cfg_path`, preserving everything else.

    Merges at the line level (`_merge_ini_text`) rather than reparsing and
    rewriting the whole file, so comments, key order, and any section this
    payload doesn't touch (including the excluded `paths`/`openai`/`github`
    and anything hand-added) survive a save byte-for-byte (#3388). Never
    deletes a key -- that's `remove_keys`'s job, only invoked once a user
    confirms a key `find_stale_keys` reported. Returns the values written.

    Chmods to 0600 after every write (not just on creation): this is the
    general wizard-save endpoint (`POST /config/sections`) and `validated`
    can include `[auth] api_key`, so a config.ini created here must never be
    left at the default umask (typically world-readable).
    """
    cfg_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    new_text, _removed = _merge_ini_text(original_text, validated, None)
    cfg_path.write_text(new_text, encoding="utf-8")
    cfg_path.chmod(0o600)

    return {section: dict(fields) for section, fields in validated.items()}


def remove_keys(cfg_path: Path, keys_by_section: dict[str, list[str]]) -> dict[str, list[str]]:
    """Delete specific stale keys from `cfg_path`, preserving everything else.

    Only ever called after the user explicitly confirms a key reported by
    `find_stale_keys` (drift reconciliation, #3388) -- a save alone
    (`apply_updates`) never removes anything on its own. Returns the subset
    of `keys_by_section` that actually existed and was removed.
    """
    if not cfg_path.exists() or not keys_by_section:
        return {}
    original_text = cfg_path.read_text(encoding="utf-8")
    new_text, removed = _merge_ini_text(original_text, {}, keys_by_section)
    if removed:
        cfg_path.write_text(new_text, encoding="utf-8")
    return removed


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
