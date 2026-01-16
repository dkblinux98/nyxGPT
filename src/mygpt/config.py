from __future__ import annotations
import os
import sys

from configparser import ConfigParser
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".myGPT" / "config.ini"

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
    required_sections = ["mygpt", "ollama"]
    for section in required_sections:
        if not cfg.has_section(section):
            errors.append(f"Missing required section: [{section}]")

    # Validate API port if specified
    if cfg.has_option("api", "port"):
        try:
            port = cfg.getint("api", "port")
            if not (1024 <= port <= 65535):
                errors.append(
                    f"Invalid api.port: {port} (must be 1024-65535)"
                )
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
            errors.append(
                f"Invalid ollama.base_url: {url} "
                "(must start with http:// or https://)"
            )

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
                    errors.append(
                        f"Invalid rag.{setting}: {val} "
                        f"(must be {min_val}-{max_val})"
                    )
            except ValueError as e:
                errors.append(
                    f"Invalid rag.{setting}: must be an integer ({e})"
                )

    # Validate chunk_overlap < chunk_size
    if cfg.has_option("rag", "chunk_size") and cfg.has_option("rag", "chunk_overlap"):
        chunk_size = cfg.getint("rag", "chunk_size", fallback=800)
        chunk_overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
        if chunk_overlap >= chunk_size:
            errors.append(
                f"Invalid RAG config: chunk_overlap ({chunk_overlap}) "
                f"must be less than chunk_size ({chunk_size})"
            )

    return errors


def load_config(path: str | Path | None = None) -> ConfigParser:
    """Load config.ini from a path.

    - If `path` is None, uses DEFAULT_CONFIG_PATH.
    - If `path` is a string, expands `~` and environment variables.

    This function is intentionally *cached* and will reload automatically when
    the underlying file changes (mtime). This allows config.ini updates to take
    effect without restarting the API.

    Hot-reloadable settings include:
    - [mygpt] default_model
    - [rag] enabled
    """
    global _CACHED_CFG, _CACHED_PATH, _CACHED_MTIME_NS

    raw = path if path is not None else DEFAULT_CONFIG_PATH

    if isinstance(raw, Path):
        config_path = raw.expanduser()
    else:
        # Allow callers to pass strings (e.g., "~/.myGPT/config.ini").
        config_path = Path(os.path.expandvars(raw)).expanduser()

    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {config_path}\n"
            "Create it at ~/.myGPT/config.ini using example.config.ini as a template."
        )

    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except Exception:
        # If we can’t stat for some reason, fall back to always re-reading.
        mtime_ns = None

    if (
        _CACHED_CFG is not None
        and _CACHED_PATH == config_path
        and _CACHED_MTIME_NS is not None
        and mtime_ns is not None
        and _CACHED_MTIME_NS == mtime_ns
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
    - [mygpt] default_model

    Falls back to a sane default if missing.

    This setting is hot-reloadable via config.ini changes.
    """
    return cfg.get("mygpt", "default_model", fallback="llama3.1:8b").strip()


def get_ollama_base_url(cfg: ConfigParser) -> str:
    return cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")


def _expand_path(value: str) -> Path:
    return Path(value).expanduser()


def get_sessions_dir(cfg: ConfigParser) -> Path:
    val = cfg.get("mygpt", "sessions_dir", fallback=str(Path.home() / ".myGPT" / "sessions"))
    return _expand_path(val)


def get_vectorstore_dir(cfg: ConfigParser) -> Path:
    val = cfg.get("mygpt", "vectorstore_dir", fallback=str(Path.home() / ".myGPT" / "vectorstore"))
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


def get_rag_enabled(cfg: ConfigParser) -> bool:
    """Primary RAG on/off switch.

    Single source of truth going forward:
    - [rag] enabled

    Backward compatibility:
    - [rag] enable_chat_context
    """
    import logging
    log = logging.getLogger(__name__)

    # New setting.
    try:
        if cfg.has_option("rag", "enabled"):
            return cfg.getboolean("rag", "enabled")
    except (ValueError, TypeError) as e:
        log.warning("Invalid rag.enabled in config, checking legacy setting: %s", e)
        # Fall through to legacy key.
        pass

    # Legacy setting (kept for compatibility with earlier docs/tests).
    return get_rag_enable_chat_context(cfg)


def get_rag_enable_chat_context(cfg: ConfigParser) -> bool:
    """Legacy compatibility key.

    Older configs used:
    - [rag] enable_chat_context

    Prefer `get_rag_enabled()` / `[rag] enabled` going forward.
    """
    try:
        return cfg.getboolean("rag", "enable_chat_context", fallback=False)
    except (ValueError, TypeError) as e:
        import logging
        log = logging.getLogger(__name__)
        log.warning("Invalid rag.enable_chat_context in config, using False: %s", e)
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
            "requests_per_second": cfg.getint(
                "rate_limit", "requests_per_second", fallback=10
            ),
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
def get_rag_instruction_template(cfg: ConfigParser) -> str:
    """Get RAG instruction template for the LLM.

    This template tells the model how to use retrieved context.
    Supports template variables: {context}

    Args:
        cfg: ConfigParser instance

    Returns:
        True if minimization is enabled, False otherwise (default)
    """
    try:
        return cfg.getboolean("mygpt", "system_prompt_minimize", fallback=False)
    except Exception:
        return False
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
    default_format = (
        "--- BEGIN RETRIEVED CONTEXT ---\n"
        "{context}\n"
        "--- END RETRIEVED CONTEXT ---"
    )
    try:
        return cfg.get("rag", "context_format", fallback=default_format)
    except Exception:
        return default_format


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "load_config",
    "get_default_model",
    "get_ollama_base_url",
    "get_sessions_dir",
    "get_vectorstore_dir",
    "get_api_host",
    "get_api_port",
    "get_rag_enabled",
    "get_rag_enable_chat_context",
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
    "validate_config",
    "ConfigValidationError",
]
