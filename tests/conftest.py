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
    # In CI or when config doesn't exist, use default log directory
    try:
        cfg = load_config(None)
        log_dir = Path(get_log_dir(cfg)).expanduser()
    except FileNotFoundError:
        # Config doesn't exist (e.g., in CI), use default log directory
        log_dir = Path("~/.myGPT/logs").expanduser()
        cfg = None

    log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize the same rotating-file logging used by the app/CLI if config available
    # This adds the RequestIdFilter to the root logger
    if cfg is not None:
        configure_logging(cfg, console=False)

    # IMPORTANT: Ensure root logger level allows WARNING/INFO during tests
    # The configure_logging might set it too high
    root = logging.getLogger()
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    # CRITICAL: Replace formatters on root handlers to not use %(request_id)s
    # This is needed for tests that don't have request context
    # Also set all handlers to DEBUG level so tests can capture WARNING/INFO logs
    test_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in root.handlers:
        handler.setFormatter(test_formatter)
        handler.setLevel(logging.DEBUG)

    # Add a dedicated tests log file (simple append) so it's easy to inspect.
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


@pytest.fixture(autouse=True)
def _ensure_test_logging_works():
    """Ensure logging works properly in tests.

    This fixture runs before each test to ensure:
    1. All handlers use a simple formatter without %(request_id)s
    2. All handlers are set to DEBUG level
    3. All mygpt loggers propagate properly
    4. This avoids formatting errors and ensures log capture works
    """
    import logging

    root = logging.getLogger()

    # Simple formatter without request_id
    test_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Configure all root handlers
    for handler in root.handlers:
        handler.setFormatter(test_formatter)
        handler.setLevel(logging.DEBUG)

    # Ensure all mygpt.* loggers are set to DEBUG and propagate
    # This is needed for tests that create custom loggers like "mygpt.test"
    for logger_name in ["mygpt", "mygpt.test", "mygpt.chat", "mygpt.config", "mygpt.sessions", "mygpt.tui"]:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = True

    yield