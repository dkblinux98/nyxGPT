from __future__ import annotations

import logging
import os
import socket
from configparser import ConfigParser
from pathlib import Path

import pytest
from log_guard import externally_held_log_files

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

[tracing]
# Tracing defaults to enabled in production (#3415), but the OTel SDK
# instrumentation `init_tracing` performs (Cassandra/urllib instrumentors, a
# global TracerProvider) is real, process-wide, and sticky across tests -- so
# the test fixture config keeps it off unless a test explicitly opts in.
enabled = false

[dev]
release_branch = v1.0.0
"""
        config_path.write_text(config_content)

    yield

    # Clean up created config after tests
    if created_config and config_path.exists():
        config_path.unlink()


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_log_dir(tmp_path_factory, _ensure_test_config):
    """Redirect the real config.ini's [logging] dir to a temp dir for the session.

    Root cause of #3443: `_configure_test_logging` (below), and every other
    code path that loads the real config (`app.py`, `cli.py`, `self_heal.py`,
    `admin_activity.py`, etc.), calls `load_config(None)` and then passes the
    *loaded* `ConfigParser` on to `configure_logging`/`get_log_dir` -- so an
    override keyed on "no cfg was passed" can't reach it. Rewriting
    `[logging] dir` in the actual `~/.nyxGPT/config.ini` file on disk (for
    the whole session, restored at teardown) is the one place that's
    upstream of every one of those call sites: `load_config` is mtime-cached,
    so this edit takes effect on the next call and needs no per-test
    cooperation (no caplog, no mocking get_log_dir).

    A second gap: some tests swap in their own bare/isolated config (e.g.
    `test_config_sections_endpoint.py` monkeypatches
    `nyxgpt.config.DEFAULT_CONFIG_PATH` to a tmp file with no `[logging]`
    section of its own) -- for those, `get_log_dir()` falls through to its
    *default*, which is `NYXGPT_LOG_DIR` when set (see nyxgpt/logging.py),
    so that env var is set here too, for the same reason.

    On a dev machine where promtail ships the real log dir to Loki, a plain
    `pytest tests/unit/` run's synthetic ERROR/WARNING records (e.g. the fake
    "Ollama HTTP 500" exception in test_chat_completeness.py) showed up in
    Grafana indistinguishable from a real chat failure and derailed an
    incident investigation -- this is what that redirected.

    Also asserts, at session teardown, that the real production log dir
    (resolved from the *original* config, before the rewrite) gained no
    files/bytes during the run -- so a future code path that bypasses both
    of the above and writes to the prod dir directly (e.g. a hardcoded
    ``~/.nyxGPT/logs`` path) fails the suite instead of silently shipping
    test noise to Loki again.

    Crash safety: before overwriting config.ini, the pre-redirect content is
    also written to a sibling ``config.ini.pytest-bak`` backup. If the
    process is killed mid-session (OOM, `kill -9`, CI eviction) the teardown
    below never runs and the real config.ini is left pointing at a tmp dir
    that will eventually be cleaned up -- silently misdirecting the
    developer's real logs away from Loki, which is exactly the failure mode
    this fixture exists to prevent. On the *next* session, an orphaned
    backup is detected and restored before anything else reads config.ini,
    so the corruption self-heals instead of persisting (or perpetuating,
    since a naive restore would otherwise re-snapshot the corrupted file as
    "original").
    """
    config_path = Path.home() / ".nyxGPT" / "config.ini"
    backup_path = config_path.with_name(config_path.name + ".pytest-bak")

    if backup_path.exists():
        # A prior session crashed before restoring config.ini -- recover the
        # real content now, before anything else (including this fixture)
        # reads or snapshots it.
        config_path.write_text(backup_path.read_text())
        backup_path.unlink()

    original_text = config_path.read_text()

    prod_log_dir = get_log_dir(load_config(None))

    def _snapshot() -> dict[str, tuple[int, float]]:
        if not prod_log_dir.exists():
            return {}
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime)
            for p in prod_log_dir.rglob("*")
            if p.is_file()
        }

    before = _snapshot()
    external_before = externally_held_log_files(prod_log_dir)

    tmp_log_dir = tmp_path_factory.mktemp("nyxgpt-test-logs")
    redirected_cfg = ConfigParser()
    redirected_cfg.read_string(original_text)
    if not redirected_cfg.has_section("logging"):
        redirected_cfg.add_section("logging")
    redirected_cfg.set("logging", "dir", str(tmp_log_dir))

    # Force tracing off for the session, for the same reason and by the same
    # mechanism. `_ensure_test_config` above already keeps it off -- but only
    # in the minimal config it writes when ~/.nyxGPT/config.ini is *absent*.
    # On a machine (or a CI runner) where an install has already produced a
    # real config, that file wins and carries `[tracing] enabled = true` (the
    # 2026-07-28 production default), so the OTel SDK initializes for real and
    # tests that assert the safe default invert: `X-Request-Id` becomes a
    # 32-char trace id instead of a 36-char UUID, and `/api/v1/tracing`
    # reports enabled. Rewriting the section here covers both cases, and the
    # opt-in tests are unaffected -- they enable tracing by monkeypatching
    # `tracing._enabled` or by passing their own ConfigParser, never through
    # this file.
    if not redirected_cfg.has_section("tracing"):
        redirected_cfg.add_section("tracing")
    redirected_cfg.set("tracing", "enabled", "false")

    # Persist the pre-redirect content as a recovery backup *before*
    # overwriting config.ini, so a hard-killed process leaves behind
    # something the next session can recover from.
    backup_path.write_text(original_text)
    with config_path.open("w", encoding="utf-8") as fh:
        redirected_cfg.write(fh)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("NYXGPT_LOG_DIR", str(tmp_log_dir))

    yield tmp_log_dir

    monkeypatch.undo()
    config_path.write_text(original_text)
    backup_path.unlink(missing_ok=True)

    after = _snapshot()

    # Union the holders seen at both ends of the session, so a service that was
    # started or stopped mid-run is still attributed to its supervisor.
    external = external_before | externally_held_log_files(prod_log_dir)

    changed = sorted(
        p for p in after if before.get(p) != after.get(p) and os.path.realpath(p) not in external
    )
    assert not changed, (
        f"pytest run wrote to the production log directory {prod_log_dir}: "
        f"{changed} -- tests must never write to the real log dir (see #3443)"
    )


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging(_isolate_test_log_dir):
    """
    Ensure pytest runs write logs to a session-scoped temp dir (in addition to
    pytest capture) -- never to the real production log directory. Depends on
    `_isolate_test_log_dir` (above), which redirects config.ini's [logging]
    dir to a temp dir before this fixture (or anything else) loads config.
    """
    cfg = load_config(None)
    log_dir = Path(get_log_dir(cfg)).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize the same rotating-file logging used by the app/CLI.
    # This adds the RequestIdFilter to the root logger.
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


@pytest.fixture(scope="session")
def embedding_backend_available():
    """Fixture for tests that ingest documents and so need real embeddings.

    Skips the test if the embedding backend cannot actually produce a vector.

    Reachability is not usability: `ollama serve` accepts connections on 11434
    as soon as it is up, but `/api/embed` answers `501 This server does not
    support embeddings` unless an embedding-capable model is loaded -- which is
    the normal state on a machine running the stack for chat. A test that
    treats "the port is open" as "embeddings work" fails there for a reason
    that has nothing to do with the code under test, so the probe issues the
    real call once per session and skips on anything short of a usable vector.

    The probe posts directly rather than going through `embed_text`, to avoid
    both the auto-pull of a multi-gigabyte model (`_pull_embedding_model`) and
    polluting the embedding cache as a side effect of an availability check.
    """
    from nyxgpt.rag.embeddings import EmbeddingError, _embedding_cfg, _post_json

    try:
        ecfg = _embedding_cfg()
        data = _post_json(
            f"{ecfg.base_url}/api/embed",
            {"model": ecfg.model, "input": ["probe"]},
            timeout=ecfg.timeout,
        )
    except EmbeddingError as exc:
        pytest.skip(f"Embedding backend unusable: {exc}")
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot embed"
        pytest.skip(f"Embedding backend unusable: {exc}")

    if not (data.get("embeddings") or data.get("embedding")):
        pytest.skip(f"Embedding backend returned no vector, got keys: {sorted(data)}")

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
