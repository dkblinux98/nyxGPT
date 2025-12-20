

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".myGPT" / "config.ini"


def load_config(path: Path | None = None) -> ConfigParser:
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {config_path}\n"
            "Create it at ~/.myGPT/config.ini using example.config.ini as a template."
        )

    parser = ConfigParser()
    parser.read(config_path)
    return parser