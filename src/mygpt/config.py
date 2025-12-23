from __future__ import annotations
import os

from configparser import ConfigParser
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".myGPT" / "config.ini"


def load_config(path: str | Path | None = None) -> ConfigParser:
    """Load config.ini from a path.

    - If `path` is None, uses DEFAULT_CONFIG_PATH.
    - If `path` is a string, expands `~` and environment variables.
    """
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

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")
    return parser


def get_default_model(cfg: ConfigParser) -> str:
    return cfg.get("mygpt", "default_model", fallback="llama3.1:8b")


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


def get_log_dir(cfg: ConfigParser) -> Path:
    val = cfg.get("mygpt", "log_dir", fallback=str(Path.home() / ".myGPT" / "logs"))
    return _expand_path(val)


def get_api_host(cfg: ConfigParser) -> str:
    return cfg.get("api", "host", fallback="127.0.0.1")


def get_api_port(cfg: ConfigParser) -> int:
    try:
        return cfg.getint("api", "port", fallback=8000)
    except Exception:
        return 8000


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "load_config",
    "get_default_model",
    "get_ollama_base_url",
    "get_sessions_dir",
    "get_vectorstore_dir",
    "get_log_dir",
    "get_api_host",
    "get_api_port",
]