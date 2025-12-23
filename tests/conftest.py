from __future__ import annotations

from pathlib import Path

import pytest

from mygpt.config import load_config, get_log_dir
from mygpt.logging import configure_logging


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging() -> None:
    """
    Ensure pytest runs write logs to ~/.myGPT/logs/tests.log (in addition to pytest capture).
    """
    cfg = load_config(None)
    log_dir = Path(get_log_dir(cfg)).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize the same rotating-file logging used by the app/CLI.
    configure_logging(cfg, console=False)

    # Add a dedicated tests log file (simple append) so it’s easy to inspect.
    import logging

    tests_log = log_dir / "tests.log"
    handler = logging.FileHandler(tests_log, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

    root = logging.getLogger()
    root.addHandler(handler)