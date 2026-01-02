from __future__ import annotations

from pathlib import Path

import pytest
import logging

from mygpt.config import load_config
from mygpt.logging import configure_logging, get_log_dir


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging():
    """
    Ensure pytest runs write logs to ~/.myGPT/logs/tests.log (in addition to pytest capture).
    """
    cfg = load_config(None)
    log_dir = Path(get_log_dir(cfg)).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize the same rotating-file logging used by the app/CLI.
    configure_logging(cfg, console=False)

    # Add a dedicated tests log file (simple append) so it’s easy to inspect.
    tests_log = log_dir / "tests.log"

    # Truncate at start of test run, then append during execution.
    # (Using write_text is fine because the file is small.)
    tests_log.write_text("")

    handler = logging.FileHandler(tests_log, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

    root = logging.getLogger()
    # Make sure the root logger level does not filter out INFO/DEBUG during tests.
    if root.level in (logging.NOTSET, logging.WARNING, logging.ERROR, logging.CRITICAL):
        root.setLevel(logging.DEBUG)

    root.addHandler(handler)

    # Emit a startup line so the file is never mysteriously empty.
    logging.getLogger("mygpt.tests").info("pytest logging initialized")
    handler.flush()

    # Ensure handler is flushed and closed at end of test session
    yield

    try:
        handler.flush()
    finally:
        root.removeHandler(handler)
        handler.close()