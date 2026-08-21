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

`find_stale_keys` reports only what the *wizard* can see, which is
`example.config.ini` minus `EXCLUDED_SECTIONS` -- and the excluded sections
are where the credentials are. `find_config_drift` (#3976) is the version
with no blind spot: it reconciles a live config.ini against
`example.config.ini` across every section, in both directions, and is what
`nyxgpt ops config-drift` reports.
"""

from __future__ import annotations

import configparser
import importlib.resources
import os
import re
import tempfile
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt.config import describe_config_parse_error

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
#
# `homebrew` and `pypi` join them for the same reason as `github` (owner
# decision, 2026-08-20): they hold release-engineering credentials for the
# tap push and a manual PyPI upload -- things CI and the owner's workstation
# use, never something a running nyxGPT instance reads.
#
# `homebrew` was declared in `example.config.ini` when the drift between it
# and a real config.ini was reconciled, and declaring it *without* excluding
# it would have created a wizard section whose `homebrew_tap_token` derives
# `secret=False` and so would come back in cleartext from
# `GET /api/v1/config/sections` -- #3947 again, and invisible to the guard
# that caught #3947 (that guard reads sensitivity from the sync manifest, the
# summary redactions and `GUIDED_SECRETS`, and this token is in none of
# them). The general guard for *that* gap is
# `test_no_credential_shaped_wizard_field_is_declared_secret_false`.
#
# `pypi` was listed here pre-emptively, before the section had any key at
# all; `[pypi] pypi_token` was then declared in `example.config.ini` (#3976)
# so that a config.ini carrying one reconciles clean instead of reporting as
# drift. This exclusion is what kept that declaration from reintroducing
# #3947 in the one commit where nothing else classifies the key yet.
EXCLUDED_SECTIONS = frozenset({"paths", "openai", "github", "cloud", "homebrew", "pypi"})


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
        restart_components: This field's **activation classification**
            (#3806): the `nyxgpt ops restart` targets that must be bounced
            before a changed value is actually in effect. Empty means
            hot-reloadable -- every consumer reads it per-request from the
            hot-reloading config cache, so a save applies immediately.
            Non-empty means the value on disk and the value in the named
            running service(s) diverge until that restart happens, which is
            what `restart_state.mark_pending` records and every surface
            (wizard notice, dashboard banner, CLI message) reports. A key can
            need more than one service: `[auth] api_key` is live on `api` but
            frozen at process start on `web`, so it is classified `("web",)`
            -- classification names the tiers that are *stale*, not the tiers
            that read the key.
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
    restart_components: tuple[str, ...] = ()
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
    restart_components: tuple[str, ...] = ()
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
        validator=_validate_bool, restart_components=("api",)
    ),
    ("cache", "embedding_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_components=("api",)
    ),
    ("cache", "embedding_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_components=("api",)
    ),
    ("cache", "embedding_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_components=("api",)
    ),
    ("cache", "embedding_cache_dir"): _Override(restart_components=("api",)),
    ("cache", "response_cache_enabled"): _Override(
        validator=_validate_bool, restart_components=("api",)
    ),
    ("cache", "response_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_components=("api",)
    ),
    ("cache", "response_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_components=("api",)
    ),
    ("cache", "response_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_components=("api",)
    ),
    ("cache", "response_cache_dir"): _Override(restart_components=("api",)),
    ("cache", "query_cache_enabled"): _Override(
        validator=_validate_bool, restart_components=("api",)
    ),
    ("cache", "query_cache_backend"): _Override(
        validator=_enum_validator("memory", "disk"), restart_components=("api",)
    ),
    ("cache", "query_cache_max_size"): _Override(
        validator=_validate_positive_int, restart_components=("api",)
    ),
    ("cache", "query_cache_ttl_seconds"): _Override(
        validator=_bounded_int(min_value=0), restart_components=("api",)
    ),
    ("cache", "query_cache_dir"): _Override(restart_components=("api",)),
    ("ollama", "base_url"): _Override(validator=_validate_url),
    ("api", "host"): _Override(validator=_validate_host, restart_components=("api",)),
    ("api", "port"): _Override(validator=_validate_port, restart_components=("api",)),
    ("api", "base_url"): _Override(validator=_validate_url),
    # The `web` tier is a Node process whose settings are read from config.ini
    # exactly once, by the service wrapper, and exported into its environment
    # (`_NATIVE_WEB_WRAPPER_TEMPLATE` in ops.py -> HOST/PORT/
    # NEXT_PUBLIC_API_BASE/NYXGPT_AUTH_API_KEY). Nothing in that process can
    # observe a later config.ini edit, so every key the wrapper reads is
    # restart-required for `web` by construction -- `test_restart_activation.py`
    # re-derives this list from the wrapper's own source and fails if a key is
    # added there without being classified here (#3806).
    ("web", "host"): _Override(validator=_validate_host, restart_components=("web",)),
    ("web", "port"): _Override(validator=_validate_port, restart_components=("web",)),
    ("web", "api_base_url"): _Override(
        validator=_validate_optional_str, restart_components=("web",)
    ),
    ("canary", "step_percent"): _Override(validator=_bounded_int(min_value=1, max_value=100)),
    ("canary", "total_replicas"): _Override(validator=_validate_positive_int),
    ("canary", "min_requests_for_evaluation"): _Override(validator=_validate_positive_int),
    ("canary", "error_rate_threshold_percent"): _Override(
        validator=_bounded_float(min_value=0.0, max_value=100.0)
    ),
    ("canary", "latency_p95_threshold_ms"): _Override(validator=_bounded_float(min_value=0.0)),
    # The worked example for #3806. `api` re-reads `[auth]` per request via
    # `require_api_key`, so the backend honours a rotation instantly; the web
    # tier's wrapper baked the key into NYXGPT_AUTH_API_KEY at process start
    # and `apiProxy.ts` sends that frozen value. Rotating without restarting
    # `web` therefore 401s every proxied call -- including the wizard session
    # doing the rotating. Classified for `web` so that divergence is
    # announced and a restart is offered instead of discovered as a blank
    # wall. `enabled` is here for the same reason: the wrapper only exports
    # the key at all when it reads `enabled = true`.
    ("auth", "enabled"): _Override(validator=_validate_bool, restart_components=("web",)),
    ("auth", "api_key"): _Override(
        validator=_validate_optional_str, secret=True, restart_components=("web",)
    ),
    ("rate_limit", "enabled"): _Override(validator=_validate_bool, restart_components=("api",)),
    ("rate_limit", "requests_per_second"): _Override(validator=_validate_positive_int),
    ("rate_limit", "burst_size"): _Override(validator=_validate_positive_int),
    ("batch", "enabled"): _Override(validator=_validate_bool, restart_components=("api",)),
    ("batch", "batch_size"): _Override(validator=_bounded_int(min_value=1, max_value=50)),
    ("batch", "wait_time_ms"): _Override(validator=_bounded_int(min_value=10, max_value=5000)),
    ("tracing", "enabled"): _Override(
        validator=_validate_bool, restart_components=("api",), observability=True
    ),
    ("tracing", "otlp_endpoint"): _Override(validator=_validate_url),
    ("tracing", "jaeger_ui_url"): _Override(validator=_validate_url),
    ("error_tracking", "enabled"): _Override(
        validator=_validate_bool, restart_components=("api",), observability=True
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
    # A Slack bot token, and every other reader of this key already treats it
    # as one: `config.SECRETS_SYNC_MANIFEST` pushes it to the *secret*
    # `SLACK_BOT_TOKEN` (not a variable), `get_effective_config_summary`
    # redacts it, and `example.config.ini` documents it as write-once (Slack
    # shows a bot token only at creation). Missing this override was #3947:
    # the field defaulted to `secret=False`, so `read_sections` took its
    # `elif` branch and `GET /api/v1/config/sections` handed the live token to
    # every browser that opened the wizard -- in cleartext, on every load,
    # including over a cloud access tunnel. `test_wizard_secret_classification`
    # is the general guard; this comment is why the entry exists.
    ("monitoring", "slack_bot_token"): _Override(validator=_validate_optional_str, secret=True),
    # The huddle's three per-agent Slack *user* tokens and the owner's DM
    # target (#3910/#3695), declared in `example.config.ini` by #3976 so
    # config.ini has a canonical home for them. Same rule as the bot token
    # above: `config.SECRETS_SYNC_MANIFEST` pushes all four to Actions
    # *secrets*, so the wizard must not hand any of them back in cleartext.
    # `slack_user_id` is a member id rather than a credential, but it is
    # carried as a secret, and sensitivity is one decision made once.
    ("monitoring", "slack_user_id"): _Override(validator=_validate_optional_str, secret=True),
    ("monitoring", "slack_user_token_dev"): _Override(
        validator=_validate_optional_str, secret=True
    ),
    ("monitoring", "slack_user_token_review"): _Override(
        validator=_validate_optional_str, secret=True
    ),
    ("monitoring", "slack_user_token_scrum"): _Override(
        validator=_validate_optional_str, secret=True
    ),
    # The huddle channel id is a *variable*, not a secret: it is
    # world-readable in GitHub's Actions settings by design, so masking it
    # here would claim a protection the destination does not provide.
    ("monitoring", "slack_huddle_channel"): _Override(validator=_validate_optional_str),
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
    ("rag", "cassandra_hosts"): _Override(
        validator=_validate_host_list, restart_components=("api",)
    ),
    ("rag", "cassandra_port"): _Override(validator=_validate_port, restart_components=("api",)),
    ("rag", "cassandra_keyspace"): _Override(validator=_validate_str, restart_components=("api",)),
    ("rag", "cassandra_table"): _Override(validator=_validate_str, restart_components=("api",)),
    ("rag", "cassandra_pool_size"): _Override(
        validator=_bounded_int(min_value=1, max_value=16), restart_components=("api",)
    ),
    ("rag", "cassandra_health_check_interval"): _Override(
        validator=_bounded_float(min_value=5.0, max_value=300.0), restart_components=("api",)
    ),
    ("rag", "cassandra_reconnect_max_attempts"): _Override(
        validator=_bounded_int(min_value=1, max_value=10), restart_components=("api",)
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
    ("rag", "embedding_model"): _Override(restart_components=("api",), default=None),
    ("rag", "embedding_dim"): _Override(
        validator=_validate_positive_int, restart_components=("api",)
    ),
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
        restart_components=override.restart_components if override else (),
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
                    # The activation classification (#3806): empty list means
                    # hot-reloadable, otherwise the services that stay stale
                    # until restarted. The UI renders this directly, so the
                    # notice can never disagree with the backend's own rule.
                    "restart_components": list(f.restart_components),
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


#: `section.key` names a real config.ini carries that `example.config.ini`
#: deliberately does not declare, and which `find_config_drift` must therefore
#: not report. Ledger **P-004**: both are groundwork for the future nyxAgent
#: product, nothing in this repository reads either one, and the conclusion an
#: unexplained "undeclared key" report invites -- delete the key, revoke the
#: secret -- is the wrong one. Removing them from this set is how they start
#: being reported again if that ledger entry is ever revisited.
UNDECLARED_BY_DESIGN: frozenset[str] = frozenset(
    {
        "github.qa_agent_token",
        "github.gh_token_nyxagent",
    }
)


def _example_option_names() -> set[str]:
    """Return every `section.key` declared in `example.config.ini`.

    The *key* half is lowercased because `config.load_config` builds a
    `ConfigParser` with the default `optionxform`, so a key the owner spelled
    `SLACK_BOT_TOKEN` on disk arrives here as `slack_bot_token` -- comparing
    raw spellings would report a live key as undeclared purely because of its
    case (#3947 is the same parser behavior seen from the other side).

    The *section* half is compared as spelled, because ConfigParser does not
    fold section names: `cfg.get("github", ...)` raises `NoSectionError`
    against a file that says `[GitHub]`, so every getter silently falls back
    to its default. Folding the section here would reconcile that file clean
    and report nothing, which is the one answer that is certainly wrong.
    Compared as spelled, `[GitHub] repo_owner` shows up as undeclared *and*
    `github.repo_owner` shows up as missing -- both true, and together they
    name the mistake.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")
    return {
        f"{section}.{key.lower()}"
        for section in parser.sections()
        for key in parser.options(section)
    }


def find_config_drift(cfg: ConfigParser) -> dict[str, list[str]]:
    """Reconcile a live `config.ini` against `example.config.ini`, both directions (#3976).

    `find_stale_keys` iterates `WIZARD_SCHEMA`, which is built from
    `example.config.ini` *minus* `EXCLUDED_SECTIONS` -- so it is structurally
    blind in `[github]`, `[paths]`, `[homebrew]`, `[pypi]`, `[openai]` and
    `[cloud]`, which is exactly where the credentials live. All eight keys
    found drifting on 2026-08-20 were in those sections and none of them could
    have been reported. This function has no such blind spot: it compares the
    two files key-for-key across *every* section.

    Returns `{"undeclared": [...], "missing": [...]}`, each a sorted list of
    `section.key` names:

    * `undeclared` -- in `cfg` but not in `example.config.ini`. Either a live
      setting nobody declared (declare it) or a retired one (remove it); the
      distinction is not inferable from the code, which is why neither this
      function nor its callers ever act on the answer.
    * `missing` -- declared in `example.config.ini` but absent from `cfg`.
      Every one of these is running on a fallback default.

    Names only, never values: a report about credentials must be safe to paste
    into an issue.

    Section names are compared as spelled and only the key is case-folded --
    see `_example_option_names` for why a mis-cased `[GitHub]` must not
    reconcile clean.
    """
    declared = _example_option_names()
    present = {
        f"{section}.{key.lower()}" for section in cfg.sections() for key in cfg.options(section)
    }
    return {
        "undeclared": sorted(present - declared - UNDECLARED_BY_DESIGN),
        "missing": sorted(declared - present),
    }


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
) -> dict[str, dict[str, str]]:
    """Return, per `nyxgpt ops restart` target, the changed fields and their *running* values.

    Shape: `{component: {"section.key": previous_value_as_ini_text}}`. The
    previous value is what is on disk right before this save -- which is the
    value the still-running service actually loaded -- so
    `restart_state.mark_pending` can later recognise a revert back to it and
    retire the pending notice without a restart (#3806).

    Only fields whose value actually *changed* from what's on disk count --
    resubmitting the same host/port shouldn't claim a restart is needed. A
    field classified for several services appears under each of them.
    """
    detail: dict[str, dict[str, str]] = {}
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            if not f.restart_components:
                continue
            previous = _current_value(cfg, section, key, value)
            if previous == value:
                continue
            # What the running service actually loaded. When the option is
            # absent from config.ini that is the *default* it fell back to --
            # not `_current_value`'s "-1"/"" absent-sentinel, which exists to
            # force the change comparison above to register. Recording the
            # sentinel would make "revert it back to the default" fail to
            # clear the notice, leaving a banner the user cannot dismiss.
            previous_text = (
                cfg.get(section, key)
                if cfg.has_option(section, key)
                else (f.default if f.default is not None else "")
            )
            for component in f.restart_components:
                detail.setdefault(component, {})[f"{section}.{key}"] = previous_text
    return detail


def restart_activation_saved(validated: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Return, per target, every restart-required field in `validated` and its *new* value.

    Unlike `restart_required_detail` this ignores whether the value changed:
    `restart_state.reconcile_saved` needs the newly saved value of every
    restart-required key in the payload so it can drop a pending entry whose
    value has been put back to what the running service is still using
    (#3806's "or the value is reverted").
    """
    saved: dict[str, dict[str, str]] = {}
    for section, fields in validated.items():
        field_specs = {f.key: f for f in _SCHEMA_BY_SECTION[section].fields}
        for key, value in fields.items():
            f = field_specs[key]
            for component in f.restart_components:
                saved.setdefault(component, {})[f"{section}.{key}"] = _ini_value_str(value)
    return saved


def field_restart_components(section: str, key: str) -> tuple[str, ...]:
    """Return `section.key`'s activation classification, or `()` if it's hot-reloadable.

    The lookup any non-wizard config writer uses to answer "does what I just
    wrote need a restart, and of what?" -- `secrets_setup.write_secret` calls
    it so the CLI reports exactly what the wizard reports. Unknown keys
    (sections the wizard excludes, e.g. `[openai]`/`[github]`) return `()`:
    they have no running consumer in the api/web tiers to go stale.
    """
    spec = _SCHEMA_BY_SECTION.get(section)
    if spec is None:
        return ()
    for f in spec.fields:
        if f.key == key:
            return f.restart_components
    return ()


def activation_classification() -> dict[str, tuple[str, ...]]:
    """Return the whole activation classification as `{"section.key": components}`.

    Every wizard-editable key appears, hot-reloadable ones with an empty
    tuple. This is the data form of what `example.config.ini` states in prose;
    `tests/unit/test_restart_activation.py` asserts the two agree, which is
    what keeps the annotations from drifting the way #3388's hand-maintained
    schema did.
    """
    return {f"{s.section}.{f.key}": f.restart_components for s in WIZARD_SCHEMA for f in s.fields}


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


#: The delimiters `ConfigParser` accepts between an option and its value, in
#: the order it looks for them: whichever appears *first* on the line wins
#: (its `OPTCRE` matches the option name non-greedily).
_DELIMITERS = ("=", ":")


def _split_option_line(line: str) -> tuple[str, str, str] | None:
    """Split an INI line into `(name, delimiter, raw_prefix)`, or None if it has neither.

    Matches `ConfigParser`'s own reading: the first `=` or `:` on the line
    ends the option name. `name` is stripped for comparison; `raw_prefix` is
    the untouched text before the delimiter (indentation, spelling, spacing),
    so a caller can rewrite the value and leave the rest of the line exactly
    as the user wrote it.
    """
    positions = [line.find(d) for d in _DELIMITERS]
    found = [p for p in positions if p != -1]
    if not found:
        return None
    pos = min(found)
    return line[:pos].strip(), line[pos], line[:pos]


def _option_name(name: str) -> str:
    """Normalise an option name the way `ConfigParser.optionxform` does.

    `nyxgpt.config.load_config` builds a plain `ConfigParser()`, whose default
    `optionxform` is `str.lower` -- so config.ini option names are
    **case-insensitive**, and `SLACK_BOT_TOKEN` is a valid spelling of the
    option the whole application reads as `slack_bot_token`. This matcher
    compared raw file text case-*sensitively*, so an uppercase key on disk
    was invisible to it: a save fell through to its insert branch and wrote a
    *second* line for the same option, which is a hard `DuplicateOptionError`
    on the next read -- the API-bricking defect in #3944. `remove_keys`
    shared the matcher and therefore silently removed nothing.

    Section names are deliberately *not* normalised here: `ConfigParser`
    treats those case-sensitively, so `[Monitoring]` and `[monitoring]` really
    are two different sections and `_section_line_ranges` matching them
    exactly is correct.
    """
    return name.lower()


def _find_key_line(lines: list[str], start: int, end: int, key: str) -> int | None:
    """Return the index of the last active `key = ...` line in `lines[start:end]`.

    Comparison is case-insensitive (`_option_name`) so the line is found
    whatever its spelling on disk; the caller rewrites it *in place*, keeping
    that spelling (#3944).
    """
    found = None
    wanted = _option_name(key)
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        parsed = _split_option_line(lines[i])
        if parsed is None:
            continue
        if _option_name(parsed[0]) == wanted:
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

    Key matching is case-insensitive and accepts either `=` or `:`, i.e. the
    same way `ConfigParser` reads the file. Any narrower match makes an
    existing option invisible to the update branch, which then *inserts* a
    duplicate -- an unparseable file, and (before `_write_ini_checked`) a
    bricked API (#3944). Callers do not, and must not, need to know how the
    user spelled the key.
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
                # Rewrite in place, keeping the line's own spelling and
                # delimiter. Normalising `SLACK_BOT_TOKEN` down to
                # `slack_bot_token` here would be a second, quieter defect:
                # config.ini is the single source of truth (#3194) and the
                # save is contractually byte-preserving outside the value
                # being changed (#3388, #3944).
                # The `or` fallback is unreachable -- `_find_key_line` only
                # ever matches lines `_split_option_line` could split.
                _name, delimiter, raw_prefix = _split_option_line(lines[idx]) or (key, "=", key)
                lines[idx] = f"{raw_prefix}{delimiter} {_ini_value_str(value)}"
            else:
                insert_at = _last_nonblank(lines, start, end)
                lines.insert(insert_at, f"{key} = {_ini_value_str(value)}")
                ranges = _section_line_ranges(lines)
                start, end = ranges[section]

    text_out = "\n".join(lines)
    if lines:
        text_out += "\n"
    return text_out, removed


class ConfigWriteError(RuntimeError):
    """A wizard write was refused because the merged text would not parse (#3944).

    Carries a `describe_config_parse_error` diagnosis. Raised *before* the
    real config.ini is touched, so the file on disk is unchanged when a
    caller sees this.

    The diagnosis is taken in its redacted form (the default -- see
    `describe_config_parse_error`): this message is rendered into an HTTP
    body as `config_write_refused`, and the offending line can be a
    credential either from the file or from the payload just posted. The
    line number and the option/section names are enough to act on, and
    `nyxgpt ops doctor` shows the text locally.
    """


def _write_ini_checked(cfg_path: Path, new_text: str, original_text: str) -> None:
    """Write `new_text` to `cfg_path` only if `ConfigParser` can read it back.

    Defence in depth for the #3944 brick, and the reason it is defence rather
    than the fix: `apply_updates` used to `write_text` the merged result with
    nothing between the merge and the disk. When the merge produced a
    duplicate option, the unparseable file landed, the endpoint's *next*
    statement (`load_config`) raised, and the owner was left with an API that
    could not serve a request or boot -- from a successful-looking UI click,
    with no backup to fall back to.

    The write is staged into a temp file in the same directory, parsed there,
    and only then `os.replace`d over the target. `os.replace` is atomic
    within a filesystem, so config.ini is never observed half-written either
    -- the API reads it on every request and a torn read is the same brick by
    another route. On a parse failure nothing is replaced and the original
    bytes stand untouched.

    A file that was *already* unparseable before this save is reported as
    such: the user needs to know the wizard did not cause it, and that no
    save can succeed until they repair the line.
    """
    if original_text.strip():
        try:
            ConfigParser().read_string(original_text, source=str(cfg_path))
        except configparser.Error as e:
            raise ConfigWriteError(
                "Refusing to save: "
                + describe_config_parse_error(cfg_path, e)
                + " The file was already in this state before this save; "
                "nothing has been written."
            ) from e

    fd, tmp_name = tempfile.mkstemp(
        dir=str(cfg_path.parent), prefix=f".{cfg_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            ConfigParser().read(tmp_path, encoding="utf-8")
        except configparser.Error as e:
            raise ConfigWriteError(
                "Refusing to save: the updated configuration would not be readable. "
                + describe_config_parse_error(cfg_path, e)
                + " config.ini is unchanged."
            ) from e
        # 0600 before the swap, not after: the general wizard-save endpoint
        # can carry `[auth] api_key`, and a window where the real path is
        # world-readable is a window an attacker can use.
        tmp_path.chmod(0o600)
        os.replace(tmp_path, cfg_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


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

    Chmods to 0600 on every write (not just on creation): this is the
    general wizard-save endpoint (`POST /config/sections`) and `validated`
    can include `[auth] api_key`, so a config.ini created here must never be
    left at the default umask (typically world-readable). The write itself
    goes through `_write_ini_checked`, which refuses to replace config.ini
    with text `ConfigParser` cannot read back (#3944), raising
    `ConfigWriteError` and leaving the original file untouched.
    """
    cfg_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    new_text, _removed = _merge_ini_text(original_text, validated, None)
    _write_ini_checked(cfg_path, new_text, original_text)

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
        # Same guard as `apply_updates`: this is the same `_merge_ini_text`
        # over the same file, so it has the same power to brick the API (#3944).
        _write_ini_checked(cfg_path, new_text, original_text)
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
