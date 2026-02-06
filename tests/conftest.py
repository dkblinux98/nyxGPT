from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest

from nyxgpt.config import load_config
from nyxgpt.logging import configure_logging, get_log_dir


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_config():
    """Ensure a config file exists for tests (needed for CI environments).

    Creates a minimal config file if ~/.nyxGPT/config.ini doesn't exist.
    This allows tests to run in CI without requiring a pre-configured environment.
    """
    config_path = Path.home() / ".nyxGPT" / "config.ini"
    created_config = False

    if not config_path.exists():
        created_config = True
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Create minimal config for tests
        config_content = """[ollama]
base_url = http://localhost:11434

[nyxgpt]
default_model = qwen2.5-coder:latest

[rag]
cassandra_hosts = localhost
cassandra_port = 9042
cassandra_keyspace = nyxgpt
chat_top_k = 5
min_score = 0.0
max_chunks = 10
chunk_size = 500
chunk_overlap = 50
max_context_chars = 10000
enable_query_expansion = false
dedupe = true

[sessions]
dir = ~/.nyxGPT/sessions

[logs]
dir = ~/.nyxGPT/logs
level = INFO

[dev]
release_branch = v1.0.0
"""
        config_path.write_text(config_content)

    yield

    # Clean up created config after tests
    if created_config and config_path.exists():
        config_path.unlink()


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging():
    """
    Ensure pytest runs write logs to ~/.nyxGPT/logs/tests.log (in addition to pytest capture).
    """
    # In CI or when config doesn't exist, use default log directory
    try:
        cfg = load_config(None)
        log_dir = Path(get_log_dir(cfg)).expanduser()
    except FileNotFoundError:
        # Config doesn't exist (e.g., in CI), use default log directory
        log_dir = Path("~/.nyxGPT/logs").expanduser()
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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    # Make sure the root logger level does not filter out INFO/DEBUG during tests.
    if root.level in (logging.NOTSET, logging.WARNING, logging.ERROR, logging.CRITICAL):
        root.setLevel(logging.DEBUG)

    root.addHandler(handler)

    # Emit a startup line so the file is never mysteriously empty.
    logging.getLogger("nyxgpt.tests").info("pytest logging initialized")
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
    3. All nyxgpt loggers propagate properly
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

    # Ensure all nyxgpt.* loggers are set to DEBUG and propagate
    # This is needed for tests that create custom loggers like "nyxgpt.test"
    for logger_name in [
        "nyxgpt",
        "nyxgpt.test",
        "nyxgpt.chat",
        "nyxgpt.config",
        "nyxgpt.sessions",
        "nyxgpt.tui",
    ]:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = True

    yield


@pytest.fixture
def cassandra_test_setup():
    """Fixture for tests that require Cassandra connection.

    Skips the test if Cassandra is not available.
    """
    try:
        cfg = load_config(None)
        host = (
            cfg.get("rag", "cassandra_host", fallback=None)
            or cfg.get("rag", "cassandra_hosts", fallback="127.0.0.1").split(",")[0].strip()
            or "127.0.0.1"
        )
        port = int(cfg.get("rag", "cassandra_port", fallback="9042"))
    except Exception:
        host = "127.0.0.1"
        port = 9042

    if not _can_connect(host, port, timeout=2.0):
        pytest.skip(f"Cassandra not reachable at {host}:{port}")

    yield


# Test document prefixes that should always be cleaned up after tests
_TEST_DOC_PREFIXES = (
    "api-smoke",
    "disable-test-",
    "docx-only-table-",
    "docx-only-text-",
    "docx-upload-",
    "docx-with-image-",
    "empty-query-",
    "epub-metadata-",
    "epub-multi-chapter-",
    "epub-upload-",
    "hybrid-test-",
    "itest-",
    "keyword-test-",
    "md-upload-",
    "pdf-enhanced-",
    "pptx-notes-",
    "pptx-order-",
    "pptx-upload-",
    "test-auto-",
    "test-doc-",
    "test.",
    "test_",
    "tf-test-",
    "txt-upload-",
)


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_rag_documents():
    """Clean up test RAG documents after all tests complete.

    This fixture runs at the end of the test session and deletes any RAG documents
    that match test prefixes. This ensures test data doesn't accumulate in the
    RAG database across test runs.
    """
    yield  # Run all tests first

    # Clean up RAG documents created during tests
    print("\n[TESTS] Cleaning up test RAG documents...")

    try:
        from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore(collection="default")
        current_docs = store.list_docs()

        deleted_count = 0
        for doc in current_docs:
            doc_id = doc["doc_id"]

            # Delete ALL documents matching test prefixes
            if doc_id.startswith(_TEST_DOC_PREFIXES) or doc_id == "empty.epub":
                try:
                    store.delete_doc(doc_id)
                    deleted_count += 1
                except Exception as e:
                    print(f"[TESTS] Failed to delete RAG doc {doc_id}: {e}")

        store.close()
        print(f"[TESTS] RAG cleanup complete: {deleted_count} documents deleted")

    except Exception as e:
        print(f"[TESTS] RAG document cleanup failed: {e}")
