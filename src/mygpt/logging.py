from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mygpt.config import get_log_dir


_DEFAULT_LEVEL = "INFO"
_DEFAULT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _level_from_any(val: str) -> int:
    v = (val or "").strip().upper()
    if not v:
        v = _DEFAULT_LEVEL
    return getattr(logging, v, logging.INFO)


def configure_logging(
    cfg: Any | None = None,
    *,
    name: str = "mygpt",
    console: bool = True,
    filename: str = "mygpt.log",
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
) -> logging.Logger:
    """Configure application logging.

    - File logs go to `[mygpt].log_dir` (default: ~/.myGPT/logs)
    - Console logging is enabled by default for CLI usability
    - Safe to call multiple times (won't duplicate handlers)

    Log level precedence:
      1) env var `MYGPT_LOG_LEVEL`
      2) config key `[mygpt] log_level`
      3) INFO
    """

    logger = logging.getLogger(name)

    # Determine level
    env_level = os.environ.get("MYGPT_LOG_LEVEL")
    cfg_level = None
    if cfg is not None:
        try:
            cfg_level = cfg.get("mygpt", "log_level", fallback=None)
        except Exception:
            cfg_level = None

    level = _level_from_any(env_level or cfg_level or _DEFAULT_LEVEL)
    logger.setLevel(level)

    # Ensure we don't add handlers repeatedly
    existing = {type(h) for h in logger.handlers}

    # File handler
    if RotatingFileHandler not in existing:
        log_dir = get_log_dir(cfg) if cfg is not None else (Path.home() / ".myGPT" / "logs")
        log_dir = Path(log_dir).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / filename

        fh = RotatingFileHandler(str(log_path), maxBytes=max_bytes, backupCount=backups)
        fh.setLevel(level)
        fh.setFormatter(
            logging.Formatter(
                fmt=_DEFAULT_FMT,
                datefmt=_DEFAULT_DATEFMT,
            )
        )
        logger.addHandler(fh)

    # Console handler
    if console and logging.StreamHandler not in existing:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(
            logging.Formatter(
                fmt=_DEFAULT_FMT,
                datefmt=_DEFAULT_DATEFMT,
            )
        )
        logger.addHandler(ch)

    # Don't double-log via root
    logger.propagate = False

    # Ensure root logger handlers (e.g. uvicorn / launchd) also use timestamps
    root = logging.getLogger()
    for h in root.handlers:
        h.setFormatter(
            logging.Formatter(
                fmt=_DEFAULT_FMT,
                datefmt=_DEFAULT_DATEFMT,
            )
        )

    return logger