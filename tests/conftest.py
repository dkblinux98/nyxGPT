from __future__ import annotations

import logging
import os
import socket
from configparser import ConfigParser
from pathlib import Path

import pytest

# MUST be imported before anything imports `nyxgpt`: importing it moves `$HOME`
# to a private per-process sandbox, and several `nyxgpt` modules resolve
# `Path.home()` at import time. See tests/home_sandbox.py for why the suite has
# a home of its own (#4020). isort keeps it ahead of the `nyxgpt` group, and
# `home_sandbox` itself fails loudly if the order is ever broken anyway.
from home_sandbox import REAL_HOME, SANDBOX_HOME
from log_guard import externally_held_log_files
from session_config import TEST_CONFIG_TEXT

from nyxgpt.config import load_config
from nyxgpt.logging import configure_logging, get_log_dir


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _real_prod_log_dir() -> Path:
    """Where the *operator's* logs go, resolved without touching their machine.

    `get_log_dir(load_config(None))` cannot answer this any more: since #4020
    `load_config(None)` reads the sandbox home's config, so it would report the
    suite's own temp dir and the guard below would be asserting about itself.
    This reads the real `~/.nyxGPT/config.ini` -- read-only, and tolerating its
    absence, which is the normal state on a CI runner -- and applies the same
    fallback `nyxgpt.logging.get_log_dir` does. `~` is expanded against
    `REAL_HOME` rather than by `expanduser()`, which now answers the sandbox.
    """
    default = REAL_HOME / ".nyxGPT" / "logs"
    real_config = REAL_HOME / ".nyxGPT" / "config.ini"
    parser = ConfigParser()
    try:
        parser.read(real_config, encoding="utf-8")
    except Exception:  # noqa: BLE001 - an unreadable operator config is not this suite's business
        return default
    configured = parser.get("logging", "dir", fallback=None)
    if not configured:
        return default
    if configured == "~":
        return REAL_HOME
    if configured.startswith("~/"):
        # Not `lstrip("~/")`, which strips *characters* and would eat the
        # leading dot of a path like `~/.nyxGPT/logs`'s successor if it ever
        # began with one of those two.
        return REAL_HOME / configured[2:]
    return Path(configured)


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_log_dir(tmp_path_factory):
    """Redirect the sandbox config.ini's [logging] dir to a temp dir for the session.

    Root cause of #3443: `_configure_test_logging` (below), and every other
    code path that loads the config (`app.py`, `cli.py`, `self_heal.py`,
    `admin_activity.py`, etc.), calls `load_config(None)` and then passes the
    *loaded* `ConfigParser` on to `configure_logging`/`get_log_dir` -- so an
    override keyed on "no cfg was passed" can't reach it. Rewriting
    `[logging] dir` in the `config.ini` file on disk, for the whole session, is
    the one place that's upstream of every one of those call sites:
    `load_config` is mtime-cached, so this edit takes effect on the next call
    and needs no per-test cooperation (no caplog, no mocking get_log_dir).

    The file it rewrites is the *sandbox* home's config (#4020), seeded with
    `TEST_CONFIG_TEXT` by `tests/home_sandbox.py` at import time. Nothing here
    reads, writes or backs up the operator's real `~/.nyxGPT/config.ini` any
    more -- see that module for the measurement showing why the previous
    swap-and-restore design could not be made concurrency-safe.

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

    Also asserts, at session teardown, that the *operator's* production log dir
    gained no files/bytes during the run -- so a future code path that bypasses
    both of the above and writes to it directly (a hardcoded absolute path, or
    one built from `home_sandbox.REAL_HOME`) fails the suite instead of
    silently shipping test noise to Loki again. Since #4020 the sandbox home
    makes an accidental `~/.nyxGPT/logs` write structurally impossible, so this
    is now a tripwire for the deliberate ones rather than the primary defence.

    No crash-safety backup is needed any more, and that is the point: the
    session's config lives in a per-process temp directory, so a hard-killed
    run leaves nothing of the operator's to recover. The old
    ``config.ini.pytest-bak`` was itself the hazard -- a fixed path two
    concurrent sessions both wrote and both deleted (#4020).
    """
    config_path = SANDBOX_HOME / ".nyxGPT" / "config.ini"

    prod_log_dir = _real_prod_log_dir()

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

    # Re-install the suite's own config for the session -- the whole file, not
    # a patch over the operator's (#3983). `home_sandbox` already seeded this
    # text; this rewrite adds the `[logging] dir` redirect, and is also what
    # keeps the guarantee honest if a test rewrote the file before this fixture
    # ran. On a machine (or CI runner) where an install has produced a real
    # config, that file used to win and its operator choices became the suite's
    # inputs: `[tracing] enabled = true` (the 2026-07-28 production default)
    # starts the OTel SDK for real, so `X-Request-Id` becomes a 32-char trace id
    # instead of a 36-char UUID and `/api/v1/tracing` reports enabled; `[nyxgpt]
    # session_backend = cassandra` and the other observability flags do the same
    # to 134 more tests (see `TEST_CONFIG_TEXT` for that count and its cause).
    # The single-key rewrites this replaces closed those reports one at a time
    # and left the class open. Opt-in tests are unaffected -- they enable a
    # feature by monkeypatching the module flag or by passing their own
    # ConfigParser, never through this file.
    redirected_cfg = ConfigParser()
    redirected_cfg.read_string(TEST_CONFIG_TEXT)
    if not redirected_cfg.has_section("logging"):
        redirected_cfg.add_section("logging")
    redirected_cfg.set("logging", "dir", str(tmp_log_dir))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        redirected_cfg.write(fh)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("NYXGPT_LOG_DIR", str(tmp_log_dir))

    yield tmp_log_dir

    monkeypatch.undo()

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


@pytest.fixture(autouse=True)
def _no_instance_metadata_reads(monkeypatch):
    """No test reaches the EC2 metadata address (#3804).

    `cloud_infra.infra_status` asks `cloud_imds` whether it is running on an
    instance, and every cloud status read goes through it. On a test runner
    that link-local address either black-holes (one timeout per uncached read)
    or -- on an actual EC2 runner -- answers, which would make the assertions
    depend on where CI happens to run. So the default for the whole suite is
    "not on EC2", stated once here.

    `tests/unit/test_cloud_imds.py` is the exception: it captures the real
    reader at import time, before this fixture replaces it, because exercising
    it is the point of that file.
    """
    from nyxgpt import cloud_imds

    monkeypatch.setattr(cloud_imds, "read_metadata", lambda timeout=None: None)
    cloud_imds.reset_cache()
    yield
    cloud_imds.reset_cache()


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
