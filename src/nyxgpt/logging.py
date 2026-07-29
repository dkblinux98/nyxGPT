"""Centralized logging setup for nyxGPT.

Configures a single set of root-logger handlers (rotating file + optional
console, plain-text or JSON) shared by nyxgpt and third-party subsystems
(uvicorn, fastapi, httpx), with per-request request-id tagging via a
context variable. Settings (level, format, log directory) are read from
``config.ini`` and can be hot-reloaded via `refresh_logging`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from configparser import ConfigParser
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOGGER_NAME = "nyxgpt"
DEFAULT_FMT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Context variable for request ID tracking
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging.

    Outputs log records as JSON objects for easier parsing and analysis.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record as a single-line JSON string.

        Args:
            record: The log record to format.

        Returns:
            A JSON-encoded string with timestamp, level, logger, message,
            request_id/session/model (when present), and any other extra
            attributes attached to the record.
        """
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id
        if hasattr(record, "session"):
            log_data["session"] = record.session
        if hasattr(record, "model"):
            log_data["model"] = record.model

        # Add any other extra attributes
        for key, value in record.__dict__.items():
            if key not in [
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "thread",
                "threadName",
                "exc_info",
                "exc_text",
                "stack_info",
                "request_id",
                "session",
                "model",
            ]:
                log_data[key] = value

        return json.dumps(log_data)


class RequestIdFilter(logging.Filter):
    """Logging filter that adds request ID from context variable to all log records.

    This ensures all log entries during a request include the request ID for traceability,
    without requiring manual addition to every log call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request_id to log record if not already present."""
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get() or "N/A"
        return True


# Paths polled by the UI/monitoring and self/liveness checks: real coverage
# comes from the latency middleware + Prometheus, so logging every poll at
# INFO would just fill the rotating file handler with noise (see #3415).
_ACCESS_LOG_EXCLUDED_PATH_PREFIXES = ("/health", "/metrics")


class AccessLogNoiseFilter(logging.Filter):
    """Drops uvicorn access-log records for high-frequency polling endpoints.

    Attached directly to the ``uvicorn.access`` logger (not a handler), so it
    only ever inspects that logger's own records -- access log records carry
    ``(client_addr, method, path, http_version, status_code)`` as ``args``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False (drop the record) for excluded-path polling requests."""
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = str(args[2]).split("?", 1)[0]
        return not path.startswith(_ACCESS_LOG_EXCLUDED_PATH_PREFIXES)


def _coerce_cfg(cfg: ConfigParser | None) -> ConfigParser:
    """Return a ConfigParser.

    If cfg is None, reload from the default config path.
    Uses local imports to avoid circular imports between config and logging.
    """
    if cfg is not None:
        return cfg
    from nyxgpt.config import load_config  # local import to avoid circular dependency

    return load_config(None)


def get_effective_log_level(cfg: ConfigParser | None) -> int:
    """Return the effective numeric log level.

    Reads ``[logging] level`` (case-insensitive name, e.g. ``"DEBUG"``),
    falling back to ``INFO`` when unset or unrecognized. If `cfg` is
    None, config.ini is reloaded via `_coerce_cfg`.
    """
    cfg = _coerce_cfg(cfg)
    level_name = cfg.get("logging", "level", fallback="INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def get_log_dir(cfg: ConfigParser | None = None) -> Path:
    """Return the configured log directory.

    This is the single source of truth for where logs are stored.
    Reads from [logging] dir, falls back to ~/.nyxGPT/logs.
    """
    cfg = _coerce_cfg(cfg)
    log_dir_str = cfg.get("logging", "dir", fallback=str(Path.home() / ".nyxGPT" / "logs"))
    return Path(log_dir_str).expanduser()


def _set_request_id_filter(handler: logging.Handler, request_id_filter: RequestIdFilter) -> None:
    """Ensure exactly one RequestIdFilter is attached to the handler.

    Filters attached to a Logger only run for records logged directly at that
    logger, not for records propagated from child loggers (e.g. uvicorn,
    cassandra-driver). Handler-level filters run for every record the handler
    emits regardless of origin, so the filter must live here, not on the
    root Logger.
    """
    for f in handler.filters[:]:
        if isinstance(f, RequestIdFilter):
            handler.removeFilter(f)
    handler.addFilter(request_id_filter)


def _ensure_rotating_file_handler(
    logger: logging.Logger,
    log_file: Path,
    formatter: logging.Formatter,
    level: int,
    request_id_filter: RequestIdFilter,
    *,
    max_bytes: int,
    backups: int,
) -> RotatingFileHandler:
    """Attach (or update) a `RotatingFileHandler` pointed at `log_file`.

    If `logger` already has a rotating file handler for the same path, its
    level, formatter, and request-id filter are updated in place and it is
    reused; otherwise a new handler is created and attached. Idempotent, so
    it's safe to call on every `configure_logging` invocation.

    Args:
        logger: Logger to attach the handler to (typically the root logger).
        log_file: Path of the log file the handler should write to.
        formatter: Formatter to apply to the handler.
        level: Log level to set on the handler.
        request_id_filter: Filter ensuring records carry a request ID.
        max_bytes: Max size in bytes before the log file is rotated.
        backups: Number of rotated backup files to keep.

    Returns:
        The attached (or reused) `RotatingFileHandler`.
    """
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file:
            h.setLevel(level)
            h.setFormatter(formatter)
            _set_request_id_filter(h, request_id_filter)
            return h

    fh = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backups,
    )
    fh.setFormatter(formatter)
    fh.setLevel(level)
    _set_request_id_filter(fh, request_id_filter)
    logger.addHandler(fh)
    return fh


def _ensure_console_handler(
    logger: logging.Logger,
    formatter: logging.Formatter,
    level: int,
    request_id_filter: RequestIdFilter,
) -> logging.Handler:
    """Attach (or update) a console (`StreamHandler`) log handler.

    If `logger` already has a plain `StreamHandler` (excluding the
    `RotatingFileHandler`, which subclasses `StreamHandler`), its level,
    formatter, and request-id filter are updated in place and it is
    reused; otherwise a new handler is created and attached. Idempotent,
    so it's safe to call on every `configure_logging` invocation.

    Args:
        logger: Logger to attach the handler to (typically the root logger).
        formatter: Formatter to apply to the handler.
        level: Log level to set on the handler.
        request_id_filter: Filter ensuring records carry a request ID.

    Returns:
        The attached (or reused) console handler.
    """
    for h in logger.handlers:
        # NOTE: RotatingFileHandler is also a StreamHandler; exclude it.
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            h.setLevel(level)
            h.setFormatter(formatter)
            _set_request_id_filter(h, request_id_filter)
            return h

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(level)
    _set_request_id_filter(ch, request_id_filter)
    logger.addHandler(ch)
    return ch


def configure_logging(
    cfg: ConfigParser | None = None,
    *,
    logger_name: str = DEFAULT_LOGGER_NAME,
    console: bool = True,
    filename: str = "nyxgpt.log",
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
) -> logging.Logger:
    """Configure or refresh logging.

    - Safe to call repeatedly.
    - Log level + log dir are hot-reloaded from config.
    - Root logger owns handlers so uvicorn/fastapi/httpx logs also land in the same files.
    """

    cfg = _coerce_cfg(cfg)
    level = get_effective_log_level(cfg)

    # Determine log format (structured JSON or plain text)
    log_format = cfg.get("logging", "format", fallback="text").lower()
    use_json = log_format == "json"

    formatter: logging.Formatter
    if use_json:
        formatter = StructuredFormatter(datefmt=DEFAULT_DATEFMT)
    else:
        formatter = logging.Formatter(
            fmt=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT, defaults={"request_id": "-"}
        )
    # Timestamps are logged in UTC (not the host's local time) so promtail's
    # `timestamp` pipeline stage -- which parses them as UTC -- doesn't shift
    # every line hours into the past on a non-UTC host, pushing it outside the
    # curated Explore links' now-1h window (see #3349).
    formatter.converter = time.gmtime

    # Use get_log_dir for consistent log directory resolution
    log_dir = get_log_dir(cfg)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename

    # Root logger is the single sink for all logs.
    root = logging.getLogger()
    root.setLevel(level)

    # The filter must be attached to handlers, not the root Logger: Logger-level
    # filters only run for records logged directly at that logger, not for
    # records propagated from child loggers (uvicorn, cassandra-driver, etc.),
    # which is how they were bypassing request_id injection entirely.
    request_id_filter = RequestIdFilter()

    # Ensure our handlers exist on root (so third-party loggers propagate into our files).
    _ensure_rotating_file_handler(
        root,
        log_file,
        formatter,
        level,
        request_id_filter,
        max_bytes=max_bytes,
        backups=backups,
    )
    if console:
        _ensure_console_handler(root, formatter, level, request_id_filter)

    # Normalize existing root handlers to the effective level.
    for h in root.handlers:
        h.setLevel(level)

    # Make nyxgpt logger propagate into root; do not attach extra handlers to avoid duplicates.
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = True

    # Common subsystems: ensure they propagate and respect level.
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "httpx",
    ):
        subsystem_logger = logging.getLogger(name)
        subsystem_logger.setLevel(level)
        subsystem_logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogNoiseFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogNoiseFilter())

    return logger


def refresh_logging(cfg: ConfigParser | None = None) -> None:
    """Explicit hot-reload entrypoint.

    If cfg is None, this reloads config.ini and reapplies handlers/levels without restart.
    """
    configure_logging(cfg)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger (defaults to the app logger name)."""
    return logging.getLogger(name or DEFAULT_LOGGER_NAME)
