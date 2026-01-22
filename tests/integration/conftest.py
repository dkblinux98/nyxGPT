from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import pytest

from mygpt.config import load_config


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def ensure_api_server_current():
    """Restart API server before integration tests to ensure latest code is running.

    This fixture automatically restarts the mygpt-api service before integration tests
    run, ensuring tests always execute against the most recent code changes. This
    prevents the common issue where code changes aren't reflected in test results
    because an old server instance is still running.

    The restart is attempted but failures are logged rather than blocking tests,
    since the server might be managed differently in CI or development environments.
    """
    # Only restart if MYGPT_SKIP_RESTART is not set (allows CI/custom setups to opt out)
    if os.environ.get("MYGPT_SKIP_RESTART"):
        print(
            "\n[INTEGRATION TESTS] Skipping API server restart (MYGPT_SKIP_RESTART set)"
        )
        yield
        return

    print("\n[INTEGRATION TESTS] Restarting API server to ensure latest code...")
    try:
        result = subprocess.run(
            ["mygpt", "ops", "restart", "api"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            print("[INTEGRATION TESTS] API server restart successful")
        else:
            print(
                f"[INTEGRATION TESTS] API restart returned non-zero: {result.returncode}"
            )
            if result.stderr:
                print(f"[INTEGRATION TESTS] stderr: {result.stderr}")

        # Wait for server to be ready
        time.sleep(3)

        # Verify server is accessible
        if _can_connect("127.0.0.1", 8000, timeout=2.0):
            print("[INTEGRATION TESTS] API server is accessible")
        else:
            print(
                "[INTEGRATION TESTS] WARNING: API server not accessible after restart"
            )

    except subprocess.TimeoutExpired:
        print(
            "[INTEGRATION TESTS] WARNING: API restart timed out (server might not be managed by ops)"
        )
    except FileNotFoundError:
        print(
            "[INTEGRATION TESTS] WARNING: 'mygpt' command not found (tests may use stale code)"
        )
    except Exception as e:
        print(
            f"[INTEGRATION TESTS] WARNING: Could not restart API: {type(e).__name__}: {e}"
        )

    yield


@pytest.fixture(scope="session")
def cfg() -> Any:
    # Allow overriding config for CI / alt setups:
    #   export MYGPT_TEST_CONFIG=/path/to/config.ini
    path = os.environ.get("MYGPT_TEST_CONFIG")
    return load_config(path)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    return os.environ.get("MYGPT_TEST_API_BASE", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def require_ollama(cfg: Any) -> None:
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    u = urlparse(base_url)
    host = u.hostname or "127.0.0.1"
    port = int(u.port or 11434)

    if not _can_connect(host, port):
        pytest.skip(f"Ollama not reachable at {host}:{port}")


@pytest.fixture(scope="session")
def require_cassandra(cfg: Any) -> None:
    # Keep this tolerant: different keys possible over time.
    # Defaults assume local Docker Cassandra.
    host = (
        cfg.get("rag", "cassandra_host", fallback=None)
        or cfg.get("rag", "cassandra_contact_points", fallback="127.0.0.1")
        .split(",")[0]
        .strip()
        or "127.0.0.1"
    )
    port = int(cfg.get("rag", "cassandra_port", fallback="9042"))

    if not _can_connect(host, port, timeout=2.0):
        pytest.skip(f"Cassandra not reachable at {host}:{port}")


@pytest.fixture
def tmp_sessions_dir(tmp_path):
    """Provide a temporary directory for session files in integration tests."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    return sessions_dir


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_rag_documents(api_base_url):
    """Clean up test RAG documents after all integration tests complete.

    This fixture runs at the end of the test session and deletes any RAG documents
    that were created during the test run (identified by test prefixes or not existing
    before tests started).
    """
    # Test document prefixes that should always be cleaned up
    TEST_DOC_PREFIXES = (
        "disable-test-",
        "empty-query-",
        "hybrid-test-",
        "itest-",
        "keyword-test-",
        "md-upload-",
        "test-doc-",
        "tf-test-",
        "txt-upload-",
        "api-smoke",
        "test-auto-",
        "test_",
        "test.",
    )

    # Record RAG documents that exist before tests run
    try:
        from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore(collection="default")
        existing_docs = {doc["doc_id"] for doc in store.list_docs()}
        store.close()
    except Exception as e:
        print(f"\n[INTEGRATION TESTS] Could not list RAG docs before tests: {e}")
        existing_docs = set()

    print(f"\n[INTEGRATION TESTS] Found {len(existing_docs)} existing RAG documents before tests")

    yield  # Run all tests

    # Clean up RAG documents created during tests
    print("\n[INTEGRATION TESTS] Cleaning up test RAG documents...")

    try:
        from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore(collection="default")
        current_docs = store.list_docs()

        deleted_count = 0
        for doc in current_docs:
            doc_id = doc["doc_id"]

            # Skip documents that existed before tests
            if doc_id in existing_docs:
                continue

            # Delete new documents OR documents matching test prefixes
            if doc_id.startswith(TEST_DOC_PREFIXES):
                try:
                    store.delete_doc(doc_id)
                    deleted_count += 1
                except Exception as e:
                    print(f"[INTEGRATION TESTS] Failed to delete RAG doc {doc_id}: {e}")

        store.close()
        print(f"[INTEGRATION TESTS] RAG cleanup complete: {deleted_count} documents deleted")

    except Exception as e:
        print(f"[INTEGRATION TESTS] RAG document cleanup failed: {e}")


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sessions(api_base_url):
    """Clean up test sessions after all integration tests complete.

    This fixture runs at the end of the test session and deletes any sessions
    that were created during the test run (not existing before tests started).
    """
    import requests

    # Record sessions that exist before tests run
    try:
        resp = requests.get(f"{api_base_url}/api/v1/sessions", timeout=5)
        if resp.ok:
            existing_sessions = {s.get("name") for s in resp.json().get("sessions", [])}
        else:
            existing_sessions = set()
    except Exception:
        existing_sessions = set()

    print(f"\n[INTEGRATION TESTS] Found {len(existing_sessions)} existing sessions before tests")

    yield  # Run all tests

    # Clean up sessions created during tests
    print("\n[INTEGRATION TESTS] Cleaning up test sessions...")

    try:
        resp = requests.get(f"{api_base_url}/api/v1/sessions", timeout=5)
        if not resp.ok:
            print("[INTEGRATION TESTS] Could not fetch sessions for cleanup")
            return

        current_sessions = resp.json().get("sessions", [])

        # Protected session names that should never be deleted
        protected_sessions = {"default"}

        deleted_count = 0
        skipped_count = 0
        for session in current_sessions:
            name = session.get("name", "")

            # Skip protected sessions
            if name in protected_sessions:
                continue

            # Skip sessions that existed before tests
            if name in existing_sessions:
                skipped_count += 1
                continue

            # Delete any session created during tests
            try:
                del_resp = requests.delete(
                    f"{api_base_url}/api/v1/sessions/{name}",
                    timeout=5
                )
                if del_resp.ok:
                    deleted_count += 1
                    print(f"[INTEGRATION TESTS] Deleted: {name}")
                else:
                    print(f"[INTEGRATION TESTS] Failed to delete {name}: {del_resp.status_code}")
            except Exception as e:
                print(f"[INTEGRATION TESTS] Failed to delete session {name}: {e}")

        print(f"[INTEGRATION TESTS] Cleanup complete: {deleted_count} deleted, {skipped_count} preserved")

    except Exception as e:
        print(f"[INTEGRATION TESTS] Session cleanup failed: {e}")
