from __future__ import annotations

import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from configparser import ConfigParser

DEFAULT_LOGGER_NAME = "mygpt"
DEFAULT_FMT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Context variable for request ID tracking
request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging.

    Outputs log records as JSON objects for easier parsing and analysis.
    """

    def format(self, record: logging.LogRecord) -> str:
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


def _coerce_cfg(cfg: Optional[ConfigParser]) -> ConfigParser:
    """Return a ConfigParser.

    If cfg is None, reload from the default config path.
    Uses local imports to avoid circular imports between config and logging.
    """
    if cfg is not None:
        return cfg
    from nyxgpt.config import load_config  # local import to avoid circular dependency

    return load_config(None)


def get_effective_log_level(cfg: Optional[ConfigParser]) -> int:
    cfg = _coerce_cfg(cfg)
    level_name = cfg.get("logging", "level", fallback="INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def get_log_dir(cfg: Optional[ConfigParser] = None) -> Path:
    """Return the configured log directory.

    This is the single source of truth for where logs are stored.
    Reads from [logging] dir, falls back to ~/.myGPT/logs.
    """
    cfg = _coerce_cfg(cfg)
    log_dir_str = cfg.get(
        "logging", "dir", fallback=str(Path.home() / ".myGPT" / "logs")
    )
    return Path(log_dir_str).expanduser()


def _ensure_rotating_file_handler(
    logger: logging.Logger,
    log_file: Path,
    formatter: logging.Formatter,
    level: int,
    *,
    max_bytes: int,
    backups: int,
) -> RotatingFileHandler:
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file:
            h.setLevel(level)
            h.setFormatter(formatter)
            return h

    fh = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backups,
    )
    fh.setFormatter(formatter)
    fh.setLevel(level)
    logger.addHandler(fh)
    return fh


def _ensure_console_handler(
    logger: logging.Logger,
    formatter: logging.Formatter,
    level: int,
) -> logging.Handler:
    for h in logger.handlers:
        # NOTE: RotatingFileHandler is also a StreamHandler; exclude it.
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, RotatingFileHandler
        ):
            h.setLevel(level)
            h.setFormatter(formatter)
            return h

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(level)
    logger.addHandler(ch)
    return ch


def configure_logging(
    cfg: Optional[ConfigParser] = None,
    *,
    logger_name: str = DEFAULT_LOGGER_NAME,
    console: bool = True,
    filename: str = "mygpt.log",
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
        formatter = logging.Formatter(fmt=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT)

    # Use get_log_dir for consistent log directory resolution
    log_dir = get_log_dir(cfg)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename

    # Root logger is the single sink for all logs.
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing RequestIdFilter to avoid duplicates (hot-reload safe)
    for f in root.filters[
        :
    ]:  # Iterate over copy to avoid modification during iteration
        if isinstance(f, RequestIdFilter):
            root.removeFilter(f)

    # Create and add request ID filter to root logger
    request_id_filter = RequestIdFilter()
    root.addFilter(request_id_filter)

    # Ensure our handlers exist on root (so third-party loggers propagate into our files).
    _ensure_rotating_file_handler(
        root,
        log_file,
        formatter,
        level,
        max_bytes=max_bytes,
        backups=backups,
    )
    if console:
        _ensure_console_handler(root, formatter, level)

    # Normalize existing root handlers to the effective level.
    for h in root.handlers:
        h.setLevel(level)

    # Make mygpt logger propagate into root; do not attach extra handlers to avoid duplicates.
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

    return logger


def refresh_logging(cfg: Optional[ConfigParser] = None) -> None:
    """Explicit hot-reload entrypoint.

    If cfg is None, this reloads config.ini and reapplies handlers/levels without restart.
    """
    configure_logging(cfg)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger (defaults to the app logger name)."""
    return logging.getLogger(name or DEFAULT_LOGGER_NAME)
