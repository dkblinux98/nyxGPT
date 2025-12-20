from __future__ import annotations

import argparse
from pathlib import Path

from mygpt.config import load_config


def cli() -> int:
    parser = argparse.ArgumentParser(prog="mygpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.myGPT/config.ini)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

    print("myGPT OK")
    print(f"Ollama base_url: {base_url}")
    print(f"Default model: {model}")

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
