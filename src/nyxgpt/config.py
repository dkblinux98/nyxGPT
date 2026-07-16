from __future__ import annotations

import os
import sys
from configparser import ConfigParser
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".nyxGPT" / "config.ini"

_CACHED_CFG: ConfigParser | None = None
_CACHED_PATH: Path | None = None
_CACHED_MTIME_NS: int | None = None


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    pass


def validate_config(cfg: ConfigParser) -> list[str]:
    """Validate configuration and return list of errors.

    This function checks that all required configuration sections and
    options exist and contain valid values. It's designed to fail fast
    on startup rather than silently using fallback values that might
    hide configuration errors.

    Args:
        cfg: The ConfigParser instance to validate

    Returns:
        List of error messages (empty if valid)
    """
    errors: list[str] = []

    # Check required sections
    required_sections = ["nyxgpt", "ollama"]
    for section in required_sections:
        if not cfg.has_section(section):
            errors.append(f"Missing required section: [{section}]")

    # Validate API port if specified
    if cfg.has_option("api", "port"):
        try:
            port = cfg.getint("api", "port")
            if not (1024 <= port <= 65535):
                errors.append(f"Invalid api.port: {port} (must be 1024-65535)")
        except ValueError as e:
            errors.append(f"Invalid api.port: must be an integer ({e})")

    # Validate API host if specified
    if cfg.has_option("api", "host"):
        host = cfg.get("api", "host")
        if not host.strip():
            errors.append("Invalid api.host: cannot be empty")

    # Validate Ollama base URL
    if cfg.has_option("ollama", "base_url"):
        url = cfg.get("ollama", "base_url")
        if not url.startswith(("http://", "https://")):
            errors.append(f"Invalid ollama.base_url: {url} (must start with http:// or https://)")

    # Validate RAG numeric settings
    rag_int_settings = {
        "chat_top_k": (1, 100),
        "max_chunks": (1, 100),
        "chat_context_max_chars": (100, 100000),
        "chunk_size": (100, 10000),
        "chunk_overlap": (0, 5000),
    }

    for setting, (min_val, max_val) in rag_int_settings.items():
        if cfg.has_option("rag", setting):
            try:
                val = cfg.getint("rag", setting)
                if not (min_val <= val <= max_val):
                    errors.append(f"Invalid rag.{setting}: {val} (must be {min_val}-{max_val})")
            except ValueError as e:
                errors.append(f"Invalid rag.{setting}: must be an integer ({e})")

    # Validate chunk_overlap < chunk_size
    if cfg.has_option("rag", "chunk_size") and cfg.has_option("rag", "chunk_overlap"):
        chunk_size = cfg.getint("rag", "chunk_size", fallback=800)
        chunk_overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
        if chunk_overlap >= chunk_size:
            errors.append(
                f"Invalid RAG config: chunk_overlap ({chunk_overlap}) "
                f"must be less than chunk_size ({chunk_size})"
            )

    # Validate context window settings
    if cfg.has_option("context", "default_window_size"):
        try:
            window_size = cfg.getint("context", "default_window_size")
            if window_size < 100:
                errors.append(
                    f"Invalid context.default_window_size: {window_size} " "(must be at least 100)"
                )
            elif window_size > 1000000:
                errors.append(
                    f"Invalid context.default_window_size: {window_size} "
                    "(must not exceed 1,000,000)"
                )
        except ValueError as e:
            errors.append(f"Invalid context.default_window_size: must be an integer ({e})")

    # Validate warning threshold
    if cfg.has_option("context", "warning_threshold"):
        try:
            threshold = cfg.getfloat("context", "warning_threshold")
            if not (0.0 <= threshold <= 1.0):
                errors.append(
                    f"Invalid context.warning_threshold: {threshold} "
                    "(must be between 0.0 and 1.0)"
                )
        except ValueError as e:
            errors.append(f"Invalid context.warning_threshold: must be a float ({e})")

    # Validate model-specific context window overrides
    if cfg.has_section("context"):
        for option in cfg.options("context"):
            if option.startswith("context_window_"):
                try:
                    window_size = cfg.getint("context", option)
                    if window_size < 100:
                        errors.append(
                            f"Invalid context.{option}: {window_size} " "(must be at least 100)"
                        )
                    elif window_size > 1000000:
                        errors.append(
                            f"Invalid context.{option}: {window_size} "
                            "(must not exceed 1,000,000)"
                        )
                except ValueError as e:
                    errors.append(f"Invalid context.{option}: must be an integer ({e})")

    return errors


def load_config(path: str | Path | None = None) -> ConfigParser:
    """Load config.ini from a path.

    - If `path` is None, uses DEFAULT_CONFIG_PATH.
    - If `path` is a string, expands `~` and environment variables.

    This function is intentionally *cached* and will reload automatically when
    the underlying file changes (mtime). This allows config.ini updates to take
    effect without restarting the API.

    Hot-reloadable settings include:
    - [nyxgpt] default_model
    - [rag] enable_chat_context
    """
    global _CACHED_CFG, _CACHED_PATH, _CACHED_MTIME_NS

    raw = path if path is not None else DEFAULT_CONFIG_PATH

    if isinstance(raw, Path):
        config_path = raw.expanduser()
    else:
        # Allow callers to pass strings (e.g., "~/.nyxGPT/config.ini").
        config_path = Path(os.path.expandvars(raw)).expanduser()

    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {config_path}\n"
            "Create it at ~/.nyxGPT/config.ini using example.config.ini as a template."
        )

    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except Exception:
        # If we can’t stat for some reason, fall back to always re-reading.
        mtime_ns = None

    if (
        _CACHED_CFG is not None
        and config_path == _CACHED_PATH
        and _CACHED_MTIME_NS is not None
        and mtime_ns is not None
        and mtime_ns == _CACHED_MTIME_NS
    ):
        return _CACHED_CFG

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    # Validate configuration on first load only (not on hot-reload)
    # This prevents noisy validation errors on every config change
    if _CACHED_CFG is None:
        validation_errors = validate_config(parser)
        if validation_errors:
            error_msg = "Configuration validation failed:\n  " + "\n  ".join(validation_errors)
            # Print to stderr for visibility even if logging isn't set up yet
            print(f"ERROR: {error_msg}", file=sys.stderr)
            # Don't raise, just warn - allows system to start with fallback values
            # But makes configuration errors visible

    _CACHED_CFG = parser
    _CACHED_PATH = config_path
    _CACHED_MTIME_NS = mtime_ns

    return parser


def get_default_model(cfg: ConfigParser) -> str:
    """Return the configured default chat model.

    Single source of truth:
    - [nyxgpt] default_model

    Falls back to a sane default if missing.

    This setting is hot-reloadable via config.ini changes.
    """
    return cfg.get("nyxgpt", "default_model", fallback="llama3.1:8b").strip()


def get_ollama_base_url(cfg: ConfigParser) -> str:
    return cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")


def get_chat_timeout_seconds(cfg: ConfigParser) -> int:
    """Return the configured per-request chat timeout (``[nyxgpt] chat_timeout_seconds``)."""
    try:
        return cfg.getint("nyxgpt", "chat_timeout_seconds", fallback=180)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid nyxgpt.chat_timeout_seconds in config, using 180: %s", e)
        return 180


def _expand_path(value: str) -> Path:
    return Path(value).expanduser()


def get_sessions_dir(cfg: ConfigParser) -> Path:
    val = cfg.get("nyxgpt", "sessions_dir", fallback=str(Path.home() / ".nyxGPT" / "sessions"))
    return _expand_path(val)


def get_vectorstore_dir(cfg: ConfigParser) -> Path:
    val = cfg.get(
        "nyxgpt", "vectorstore_dir", fallback=str(Path.home() / ".nyxGPT" / "vectorstore")
    )
    return _expand_path(val)


def get_api_host(cfg: ConfigParser) -> str:
    return cfg.get("api", "host", fallback="127.0.0.1")


def get_api_port(cfg: ConfigParser) -> int:
    try:
        return cfg.getint("api", "port", fallback=8000)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid api.port in config, using default 8000: %s", e)
        return 8000


def get_tools_root(cfg: ConfigParser) -> Path:
    """Root directory the /api/v1/tools/{ls,cat,grep} endpoints are confined to.

    Defense in depth for a network-reachable deployment: even though the API
    is loopback-only and unauthenticated by default (see docs/security.md),
    a caller that does reach it should not be able to read arbitrary files
    (SSH keys, /etc/passwd, ...) via these filesystem tools. Defaults to the
    user's home directory; override with `[api] tools_root` to widen or
    narrow it.
    """
    val = cfg.get("api", "tools_root", fallback=str(Path.home())).strip()
    return _expand_path(val or str(Path.home()))


def get_deploy_namespace(cfg: ConfigParser) -> str:
    return cfg.get("deploy", "namespace", fallback="nyxgpt")


def get_canary_namespace(cfg: ConfigParser) -> str:
    return cfg.get("canary", "namespace", fallback=get_deploy_namespace(cfg))


def get_canary_total_replicas(cfg: ConfigParser) -> int:
    try:
        return max(1, cfg.getint("canary", "total_replicas", fallback=4))
    except (ValueError, TypeError):
        return 4


def get_canary_step_percent(cfg: ConfigParser) -> int:
    try:
        return min(100, max(1, cfg.getint("canary", "step_percent", fallback=25)))
    except (ValueError, TypeError):
        return 25


def get_canary_error_rate_threshold(cfg: ConfigParser) -> float:
    try:
        return cfg.getfloat("canary", "error_rate_threshold_percent", fallback=5.0)
    except (ValueError, TypeError):
        return 5.0


def get_canary_latency_p95_threshold_ms(cfg: ConfigParser) -> float:
    try:
        return cfg.getfloat("canary", "latency_p95_threshold_ms", fallback=2000.0)
    except (ValueError, TypeError):
        return 2000.0


def get_canary_min_requests(cfg: ConfigParser) -> int:
    try:
        return max(1, cfg.getint("canary", "min_requests_for_evaluation", fallback=20))
    except (ValueError, TypeError):
        return 20


def get_rag_enabled(cfg: ConfigParser) -> bool:
    """Primary RAG on/off switch.

    Single source of truth, used by both the chat runtime (RAG context
    injection) and the admin health/overview/config endpoints, so they
    never disagree about whether RAG is active:
    - [rag] enable_chat_context

    Legacy alias (deprecated, only consulted when `enable_chat_context`
    is not explicitly set):
    - [rag] enabled
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        if cfg.has_option("rag", "enable_chat_context"):
            return cfg.getboolean("rag", "enable_chat_context")
    except (ValueError, TypeError) as e:
        log.warning("Invalid rag.enable_chat_context in config, checking legacy setting: %s", e)
        # Fall through to legacy key.
        pass

    # Legacy alias (kept for compatibility with earlier docs/tests).
    return _get_rag_enabled_legacy_alias(cfg)


def _get_rag_enabled_legacy_alias(cfg: ConfigParser) -> bool:
    """Legacy compatibility key.

    Some older configs/docs referred to this setting as:
    - [rag] enabled

    Prefer `get_rag_enabled()` / `[rag] enable_chat_context` going forward.
    """
    try:
        return cfg.getboolean("rag", "enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid rag.enabled in config, using False: %s", e)
        return False


def get_rag_chat_top_k(cfg: ConfigParser) -> int:
    try:
        return cfg.getint("rag", "chat_top_k", fallback=3)
    except Exception:
        return 3


def get_rag_min_score(cfg: ConfigParser) -> float:
    try:
        return cfg.getfloat("rag", "min_score", fallback=0.0)
    except Exception:
        return 0.0


def get_rag_max_chunks(cfg: ConfigParser) -> int:
    try:
        return cfg.getint("rag", "max_chunks", fallback=6)
    except Exception:
        return 6


def get_rag_chat_context_max_chars(cfg: ConfigParser) -> int:
    try:
        return cfg.getint("rag", "chat_context_max_chars", fallback=2400)
    except Exception:
        return 2400


def get_rag_dedupe(cfg: ConfigParser) -> bool:
    try:
        return cfg.getboolean("rag", "dedupe", fallback=True)
    except Exception:
        return True


def get_rag_include_scores(cfg: ConfigParser) -> bool:
    try:
        return cfg.getboolean("rag", "include_scores", fallback=False)
    except Exception:
        return False


def get_rag_include_headers(cfg: ConfigParser) -> bool:
    try:
        return cfg.getboolean("rag", "include_headers", fallback=True)
    except Exception:
        return True


def get_rag_debug_mode(cfg: ConfigParser) -> bool:
    """Get RAG debug mode flag.

    When enabled, RAG operations collect and return detailed debug information
    including timing metrics, query analysis, embedding details, and filtering stats.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if debug mode is enabled, False otherwise
    """
    try:
        return cfg.getboolean("rag", "debug_mode", fallback=False)
    except Exception:
        return False


def get_rag_good_score_threshold(cfg: ConfigParser) -> float:
    """Get the threshold for 'good' RAG similarity scores.

    Scores >= this threshold are considered high confidence and displayed
    with green visual indicators in the UI.

    Args:
        cfg: ConfigParser instance

    Returns:
        Good score threshold (default: 0.7)
    """
    try:
        return cfg.getfloat("rag", "good_score_threshold", fallback=0.7)
    except Exception:
        return 0.7


def get_rag_medium_score_threshold(cfg: ConfigParser) -> float:
    """Get the threshold for 'medium' RAG similarity scores.

    Scores >= this threshold but < good_score_threshold are considered
    medium confidence and displayed with yellow visual indicators in the UI.
    Scores < this threshold are considered low confidence (red).

    Args:
        cfg: ConfigParser instance

    Returns:
        Medium score threshold (default: 0.4)
    """
    try:
        return cfg.getfloat("rag", "medium_score_threshold", fallback=0.4)
    except Exception:
        return 0.4


def get_rate_limit_enabled(cfg: ConfigParser) -> bool:
    """Get rate limiting enabled flag.

    Returns whether rate limiting is enabled for the API.
    Disabled by default (localhost-only app).

    Args:
        cfg: ConfigParser instance

    Returns:
        True if rate limiting is enabled, False otherwise
    """
    try:
        return cfg.getboolean("rate_limit", "enabled", fallback=False)
    except Exception:
        return False


def get_rate_limit_config(cfg: ConfigParser) -> dict:
    """Get rate limiting configuration.

    Returns all rate limiting settings as a dictionary.
    Uses sensible defaults for localhost deployment.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: requests_per_second, burst_size
    """
    try:
        return {
            "requests_per_second": cfg.getint("rate_limit", "requests_per_second", fallback=10),
            "burst_size": cfg.getint("rate_limit", "burst_size", fallback=20),
        }
    except Exception:
        # Return defaults if any parsing errors
        return {
            "requests_per_second": 10,
            "burst_size": 20,
        }


def get_context_window_size(cfg: ConfigParser, model: str | None = None) -> int:
    """Get maximum context window size for a model.

    Checks model-specific override first, then falls back to default.
    Common context window sizes:
    - llama3.1:8b, llama3.1:70b: 128k tokens
    - llama2: 4k tokens
    - mistral: 8k tokens
    - qwen2.5: 32k tokens

    Args:
        cfg: ConfigParser instance
        model: Optional model name to look up specific limit

    Returns:
        Maximum context window size in tokens
    """
    # Try model-specific setting first
    if model:
        # Sanitize model name for config key (replace : and . with _)
        safe_model = model.replace(":", "_").replace(".", "_").replace("/", "_")
        option = f"context_window_{safe_model}"
        if cfg.has_option("context", option):
            try:
                return cfg.getint("context", option)
            except Exception:
                pass

    # Fall back to default
    try:
        return cfg.getint("context", "default_window_size", fallback=8192)
    except Exception:
        return 8192


def get_context_warning_threshold(cfg: ConfigParser) -> float:
    """Get threshold for warning when approaching context limit.

    Returns fraction of context window (0.0-1.0) at which to log warnings.

    Args:
        cfg: ConfigParser instance

    Returns:
        Warning threshold as fraction (default 0.8 = 80%)
    """
    try:
        threshold = cfg.getfloat("context", "warning_threshold", fallback=0.8)
        # Clamp to valid range
        return max(0.0, min(1.0, threshold))
    except Exception:
        return 0.8


def get_system_prompt_minimize(cfg: ConfigParser) -> bool:
    """Get system prompt minimization setting.

    When enabled, system prompts are optimized to reduce token usage by:
    - Removing redundant whitespace and formatting
    - Condensing verbose patterns
    - Preserving semantic meaning

    Args:
        cfg: ConfigParser instance

    Returns:
        True if minimization is enabled, False otherwise (default)
    """
    try:
        return cfg.getboolean("nyxgpt", "system_prompt_minimize", fallback=False)
    except Exception:
        return False


def get_rag_instruction_template(cfg: ConfigParser) -> str:
    """Get RAG instruction template for the LLM.

    This template tells the model how to use retrieved context.
    Supports template variables: {context}

    Args:
        cfg: ConfigParser instance

    Returns:
        Instruction template string
    """
    default_template = (
        "Use the retrieved context below when it is relevant and helpful. "
        "Do not mention that you were given retrieved context unless the user explicitly asks about sources. "
        "If the context is insufficient, say so and answer from general knowledge.\n\n"
        "{context}"
    )
    try:
        return cfg.get("rag", "instruction_template", fallback=default_template)
    except Exception:
        return default_template


def get_rag_context_format(cfg: ConfigParser) -> str:
    """Get RAG context format template.

    This template wraps the retrieved context chunks.
    Supports template variables: {context}

    Args:
        cfg: ConfigParser instance

    Returns:
        Context format template string
    """
    default_format = "--- BEGIN RETRIEVED CONTEXT ---\n{context}\n--- END RETRIEVED CONTEXT ---"
    try:
        return cfg.get("rag", "context_format", fallback=default_format)
    except Exception:
        return default_format


def get_prompt_mode_enabled(cfg: ConfigParser) -> bool:
    """Get whether adaptive prompt mode is enabled.

    When enabled, system prompts adapt based on conversation length
    (short/medium/long modes). Only applies when no custom system_prompt is set.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if adaptive mode is enabled, False otherwise
    """
    try:
        return cfg.getboolean("prompt", "adaptive_mode_enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.adaptive_mode_enabled in config, using False: %s", e)
        return False


def get_prompt_mode_short_threshold(cfg: ConfigParser) -> int:
    """Get message count threshold for short prompt mode.

    Conversations with fewer messages than this threshold use short mode.

    Args:
        cfg: ConfigParser instance

    Returns:
        Message count threshold for short mode (default: 3)
    """
    try:
        threshold = cfg.getint("prompt", "short_threshold", fallback=3)
        return max(1, threshold)  # Must be at least 1
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.short_threshold in config, using 3: %s", e)
        return 3


def get_prompt_mode_long_threshold(cfg: ConfigParser) -> int:
    """Get message count threshold for long prompt mode.

    Conversations with this many messages or more use long mode.
    Between short_threshold and long_threshold, medium mode is used.

    Args:
        cfg: ConfigParser instance

    Returns:
        Message count threshold for long mode (default: 10)
    """
    try:
        threshold = cfg.getint("prompt", "long_threshold", fallback=10)
        # Ensure long_threshold is greater than short_threshold
        short_threshold = get_prompt_mode_short_threshold(cfg)
        return max(short_threshold + 1, threshold)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.long_threshold in config, using 10: %s", e)
        return 10


def get_cassandra_pool_size(cfg: ConfigParser) -> int:
    """Get the Cassandra connection pool size (connections per host).

    Controls how many driver-level connections are maintained to each
    Cassandra host.  Higher values support more concurrent RAG queries.

    Args:
        cfg: ConfigParser instance

    Returns:
        Pool size (default: 2, range: 1-16)
    """
    try:
        size = cfg.getint("rag", "cassandra_pool_size", fallback=2)
        return max(1, min(16, size))
    except Exception:
        return 2


def get_cassandra_health_check_interval(cfg: ConfigParser) -> float:
    """Get the interval between Cassandra connection health checks (seconds).

    Args:
        cfg: ConfigParser instance

    Returns:
        Health check interval in seconds (default: 30.0)
    """
    try:
        interval = cfg.getfloat("rag", "cassandra_health_check_interval", fallback=30.0)
        return max(5.0, min(300.0, interval))
    except Exception:
        return 30.0


def get_cassandra_reconnect_max_attempts(cfg: ConfigParser) -> int:
    """Get the maximum number of Cassandra reconnection attempts.

    Args:
        cfg: ConfigParser instance

    Returns:
        Max reconnect attempts (default: 3, range: 1-10)
    """
    try:
        attempts = cfg.getint("rag", "cassandra_reconnect_max_attempts", fallback=3)
        return max(1, min(10, attempts))
    except Exception:
        return 3


def get_cassandra_batch_size(cfg: ConfigParser) -> int:
    """Get the number of chunk upserts grouped into a single Cassandra batch.

    Larger batches reduce network round trips during ingestion at the cost of
    larger individual requests; the driver enforces its own batch size limits,
    so this is clamped to a conservative range.

    Args:
        cfg: ConfigParser instance

    Returns:
        Batch size (default: 20, range: 1-100)
    """
    try:
        size = cfg.getint("rag", "cassandra_batch_size", fallback=20)
        return max(1, min(100, size))
    except Exception:
        return 20


def get_vector_similarity_function(cfg: ConfigParser) -> str:
    """Get the ANN similarity function used for vector search.

    Determines both how the Cassandra SAI vector index scores candidates
    (index build option) and which CQL similarity function is used to
    compute the returned score, so the two must stay in sync.

    Args:
        cfg: ConfigParser instance

    Returns:
        One of "cosine", "dot_product", "euclidean" (default: "cosine")
    """
    try:
        value = cfg.get("rag", "vector_similarity_function", fallback="cosine").strip().lower()
    except Exception:
        return "cosine"
    return value if value in ("cosine", "dot_product", "euclidean") else "cosine"


def get_ann_oversample_factor(cfg: ConfigParser) -> float:
    """Get the ANN oversampling factor for candidate retrieval.

    Multiplies the number of candidates fetched from the ANN index beyond
    what metadata filtering alone requires, trading additional query cost
    for improved recall on approximate search.

    Args:
        cfg: ConfigParser instance

    Returns:
        Oversampling factor (default: 1.0, range: 1.0-5.0)
    """
    try:
        factor = cfg.getfloat("rag", "ann_oversample_factor", fallback=1.0)
        return max(1.0, min(5.0, factor))
    except Exception:
        return 1.0


def get_cassandra_batch_query_concurrency(cfg: ConfigParser) -> int:
    """Get the concurrency cap for batched ANN vector searches.

    Bounds how many ANN queries run in flight at once when searching
    multiple query embeddings in a single batch call, limiting peak
    memory and connection usage.

    Args:
        cfg: ConfigParser instance

    Returns:
        Concurrency cap (default: 4, range: 1-32)
    """
    try:
        concurrency = cfg.getint("rag", "cassandra_batch_query_concurrency", fallback=4)
        return max(1, min(32, concurrency))
    except Exception:
        return 4


def get_batch_enabled(cfg: ConfigParser) -> bool:
    """Get whether request batching is enabled.

    When enabled, multiple chat/RAG requests can be batched together
    for improved throughput. Disabled by default.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if batching is enabled, False otherwise
    """
    try:
        return cfg.getboolean("batch", "enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.enabled in config, using False: %s", e)
        return False


def get_batch_size(cfg: ConfigParser) -> int:
    """Get maximum batch size for request batching.

    Maximum number of requests to batch together before processing.

    Args:
        cfg: ConfigParser instance

    Returns:
        Maximum batch size (default: 4, range: 1-50)
    """
    try:
        size = cfg.getint("batch", "batch_size", fallback=4)
        return max(1, min(50, size))  # Clamp to valid range
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.batch_size in config, using 4: %s", e)
        return 4


def get_batch_wait_time_ms(cfg: ConfigParser) -> int:
    """Get maximum wait time for batch to fill.

    Maximum time to wait for batch to reach batch_size before
    processing whatever is available (milliseconds).

    Args:
        cfg: ConfigParser instance

    Returns:
        Wait time in milliseconds (default: 100ms, range: 10-5000ms)
    """
    try:
        wait_ms = cfg.getint("batch", "wait_time_ms", fallback=100)
        return max(10, min(5000, wait_ms))  # Clamp to valid range
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.wait_time_ms in config, using 100: %s", e)
        return 100


def get_tracing_enabled(cfg: ConfigParser) -> bool:
    """Get whether distributed tracing is enabled.

    Disabled by default. When enabled, spans are exported via OTLP to a
    local collector (e.g. the `tracing` Compose profile's OTel collector +
    Jaeger all-in-one) -- traces never leave the machine.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if tracing is enabled, False otherwise
    """
    try:
        return cfg.getboolean("tracing", "enabled", fallback=False)
    except Exception:
        return False


def get_tracing_config(cfg: ConfigParser) -> dict:
    """Get distributed tracing configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, service_name, otlp_endpoint, jaeger_ui_url
    """
    return {
        "enabled": get_tracing_enabled(cfg),
        "service_name": cfg.get("tracing", "service_name", fallback="nyxgpt-api"),
        "otlp_endpoint": cfg.get(
            "tracing", "otlp_endpoint", fallback="http://localhost:4318/v1/traces"
        ),
        "jaeger_ui_url": cfg.get("tracing", "jaeger_ui_url", fallback="http://localhost:16686"),
    }


def get_error_tracking_enabled(cfg: ConfigParser) -> bool:
    """Get whether error tracking is enabled.

    Disabled by default. When enabled (and a DSN is configured), exceptions
    are reported via the Sentry SDK protocol to a self-hosted, local-only
    tracker (e.g. the `errors` Compose profile's GlitchTip instance) --
    error data never leaves the machine.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if error tracking is enabled, False otherwise
    """
    try:
        return cfg.getboolean("error_tracking", "enabled", fallback=False)
    except Exception:
        return False


def get_error_tracking_config(cfg: ConfigParser) -> dict:
    """Get error tracking configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, dsn, environment, release,
        traces_sample_rate, glitchtip_ui_url
    """
    try:
        traces_sample_rate = cfg.getfloat("error_tracking", "traces_sample_rate", fallback=0.0)
    except ValueError:
        traces_sample_rate = 0.0

    return {
        "enabled": get_error_tracking_enabled(cfg),
        "dsn": cfg.get("error_tracking", "dsn", fallback=""),
        "environment": cfg.get("error_tracking", "environment", fallback="development"),
        "release": cfg.get("error_tracking", "release", fallback=""),
        "traces_sample_rate": traces_sample_rate,
        "glitchtip_ui_url": cfg.get(
            "error_tracking", "glitchtip_ui_url", fallback="http://localhost:8080"
        ),
    }


def get_monitoring_enabled(cfg: ConfigParser) -> bool:
    """Get whether the Grafana/Prometheus monitoring stack is enabled.

    Disabled by default. This flag doesn't start anything by itself -- it
    only controls whether the SRE/admin dashboard treats the `monitoring`
    Compose profile as running and shows the Grafana link.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if monitoring is enabled, False otherwise
    """
    try:
        return cfg.getboolean("monitoring", "enabled", fallback=False)
    except Exception:
        return False


def get_monitoring_config(cfg: ConfigParser) -> dict:
    """Get Grafana/Prometheus monitoring configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, grafana_ui_url, prometheus_ui_url
    """
    return {
        "enabled": get_monitoring_enabled(cfg),
        "grafana_ui_url": cfg.get("monitoring", "grafana_ui_url", fallback="http://localhost:3001"),
        "prometheus_ui_url": cfg.get(
            "monitoring", "prometheus_ui_url", fallback="http://localhost:9090"
        ),
    }


def get_monitoring_grafana_admin_password(cfg: ConfigParser) -> str:
    """Get the Grafana admin password, config.ini's single source of truth for it.

    Deliberately kept out of `get_monitoring_config` -- that dict is returned
    verbatim by `GET /api/v1/monitoring`, and this value must never be
    exposed over the API. It exists only so local tooling (`nyxgpt ops
    env-sync`) can derive the Compose `.env`'s `GRAFANA_ADMIN_PASSWORD` from
    config.ini instead of the user maintaining both separately.

    Args:
        cfg: ConfigParser instance

    Returns:
        The configured password, or "" if unset
    """
    return cfg.get("monitoring", "grafana_admin_password", fallback="")


def get_log_aggregation_enabled(cfg: ConfigParser) -> bool:
    """Get whether the Loki/promtail log aggregation stack is enabled.

    Disabled by default. This flag doesn't start anything by itself -- it
    only controls whether the SRE/admin dashboard treats the `logging`
    Compose profile as running and shows the log search link.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if log aggregation is enabled, False otherwise
    """
    try:
        return cfg.getboolean("log_aggregation", "enabled", fallback=False)
    except Exception:
        return False


def get_log_aggregation_config(cfg: ConfigParser) -> dict:
    """Get Loki/promtail log aggregation configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, grafana_explore_url
    """
    return {
        "enabled": get_log_aggregation_enabled(cfg),
        "grafana_explore_url": cfg.get(
            "log_aggregation",
            "grafana_explore_url",
            fallback="http://localhost:3001/explore",
        ),
    }


def get_self_heal_default_enabled(cfg: ConfigParser) -> bool:
    """Get the self-heal watchdog's initial enabled state.

    Disabled by default. Only used to seed `~/.nyxGPT/self_heal_state.json`
    on first run -- after that, the SRE/admin dashboard's toggle (persisted
    in that state file) is the source of truth, not config.ini.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if the watchdog should start enabled on a fresh install
    """
    try:
        return cfg.getboolean("self_heal", "enabled", fallback=False)
    except Exception:
        return False


def get_self_heal_check_interval_seconds(cfg: ConfigParser) -> float:
    try:
        return max(1.0, cfg.getfloat("self_heal", "check_interval_seconds", fallback=15.0))
    except (ValueError, TypeError):
        return 15.0


def get_self_heal_max_consecutive_restarts(cfg: ConfigParser) -> int:
    try:
        return max(1, cfg.getint("self_heal", "max_consecutive_restarts", fallback=5))
    except (ValueError, TypeError):
        return 5


def get_self_heal_backoff_seconds(cfg: ConfigParser) -> float:
    try:
        return max(0.0, cfg.getfloat("self_heal", "backoff_seconds", fallback=30.0))
    except (ValueError, TypeError):
        return 30.0


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "load_config",
    "get_default_model",
    "get_ollama_base_url",
    "get_sessions_dir",
    "get_vectorstore_dir",
    "get_api_host",
    "get_api_port",
    "get_tools_root",
    "get_rag_enabled",
    "get_rag_chat_top_k",
    "get_rag_min_score",
    "get_rag_max_chunks",
    "get_rag_chat_context_max_chars",
    "get_rag_dedupe",
    "get_rag_include_scores",
    "get_rag_include_headers",
    "get_rag_instruction_template",
    "get_rag_context_format",
    "get_rate_limit_enabled",
    "get_rate_limit_config",
    "get_context_window_size",
    "get_context_warning_threshold",
    "get_system_prompt_minimize",
    "get_prompt_mode_enabled",
    "get_prompt_mode_short_threshold",
    "get_prompt_mode_long_threshold",
    "get_batch_enabled",
    "get_batch_size",
    "get_batch_wait_time_ms",
    "get_tracing_enabled",
    "get_tracing_config",
    "get_error_tracking_enabled",
    "get_error_tracking_config",
    "get_monitoring_enabled",
    "get_monitoring_config",
    "get_monitoring_grafana_admin_password",
    "get_log_aggregation_enabled",
    "get_log_aggregation_config",
    "get_self_heal_default_enabled",
    "get_self_heal_check_interval_seconds",
    "get_self_heal_max_consecutive_restarts",
    "get_self_heal_backoff_seconds",
    "get_cassandra_pool_size",
    "get_cassandra_health_check_interval",
    "get_cassandra_reconnect_max_attempts",
    "get_cassandra_batch_size",
    "get_vector_similarity_function",
    "get_ann_oversample_factor",
    "get_cassandra_batch_query_concurrency",
    "validate_config",
    "ConfigValidationError",
]
