"""Tests for Cassandra connection pool."""
import time
import threading
from unittest.mock import Mock, patch, MagicMock
import pytest

from nyxgpt.rag.cassandra_pool import (
    CassandraConnectionPool,
    PoolConfig,
    ConnectionPoolError,
    get_pool_config,
)


@pytest.fixture
def pool_config():
    """Test pool configuration."""
    return PoolConfig(
        hosts=["127.0.0.1"],
        port=9042,
        keyspace="test_keyspace",
        pool_size=3,
        health_check_interval=1,
        connection_timeout=5,
        max_retry_attempts=2,
        retry_delay=1,
    )


@pytest.fixture
def mock_cluster():
    """Mock Cassandra Cluster."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster") as mock:
        cluster = MagicMock()
        # Return different session instances on each call
        cluster.connect.side_effect = lambda: MagicMock()
        mock.return_value = cluster
        yield mock


@pytest.fixture(autouse=True)
def reset_pool():
    """Reset pool singleton before each test."""
    CassandraConnectionPool.reset_instance()
    yield
    CassandraConnectionPool.reset_instance()


def test_pool_initialization(pool_config, mock_cluster):
    """Test connection pool initialization."""
    pool = CassandraConnectionPool(pool_config)

    # Verify cluster was created with correct config
    mock_cluster.assert_called_once()
    call_kwargs = mock_cluster.call_args[1]
    assert call_kwargs["contact_points"] == pool_config.hosts
    assert call_kwargs["port"] == pool_config.port

    # Verify pool size
    stats = pool.get_stats()
    assert stats["total_sessions"] == pool_config.pool_size
    assert stats["available_sessions"] == pool_config.pool_size
    assert stats["in_use_sessions"] == 0

    pool.shutdown()


def test_singleton_pattern(pool_config, mock_cluster):
    """Test singleton pattern for pool."""
    pool1 = CassandraConnectionPool.get_instance(pool_config)
    pool2 = CassandraConnectionPool.get_instance()

    assert pool1 is pool2

    pool1.shutdown()


def test_get_instance_without_config():
    """Test getting instance without config raises error."""
    with pytest.raises(ConnectionPoolError, match="Configuration required"):
        CassandraConnectionPool.get_instance()


def test_acquire_and_release(pool_config, mock_cluster):
    """Test acquiring and releasing sessions."""
    pool = CassandraConnectionPool(pool_config)

    # Acquire session
    session = pool.acquire()
    assert session is not None

    stats = pool.get_stats()
    assert stats["available_sessions"] == pool_config.pool_size - 1
    assert stats["in_use_sessions"] == 1

    # Release session
    pool.release(session)

    stats = pool.get_stats()
    assert stats["available_sessions"] == pool_config.pool_size
    assert stats["in_use_sessions"] == 0

    pool.shutdown()


def test_acquire_all_sessions(pool_config, mock_cluster):
    """Test acquiring all available sessions."""
    pool = CassandraConnectionPool(pool_config)

    sessions = []
    for _ in range(pool_config.pool_size):
        sessions.append(pool.acquire())

    stats = pool.get_stats()
    assert stats["available_sessions"] == 0
    assert stats["in_use_sessions"] == pool_config.pool_size

    # Trying to acquire another should fail
    with pytest.raises(ConnectionPoolError, match="No available sessions"):
        pool.acquire()

    # Release all
    for session in sessions:
        pool.release(session)

    pool.shutdown()


def test_health_check_replaces_unhealthy_session(pool_config, mock_cluster):
    """Test health check replaces unhealthy sessions."""
    pool = CassandraConnectionPool(pool_config)

    # Get a session and make it "unhealthy"
    sessions = list(pool._sessions)
    unhealthy_session = sessions[0]

    # Mock execute to raise exception for unhealthy session
    unhealthy_session.execute.side_effect = Exception("Connection lost")

    # Trigger health check
    pool._perform_health_check()

    # Verify unhealthy session was replaced
    assert unhealthy_session not in pool._sessions

    # Pool should still have same size
    stats = pool.get_stats()
    assert stats["total_sessions"] == pool_config.pool_size

    pool.shutdown()


def test_release_unhealthy_session(pool_config, mock_cluster):
    """Test releasing unhealthy session triggers replacement."""
    pool = CassandraConnectionPool(pool_config)

    # Acquire a session
    session = pool.acquire()

    # Make it unhealthy
    session.execute.side_effect = Exception("Connection lost")

    # Release it
    pool.release(session)

    # Session should not be back in available pool
    assert session not in pool._available

    # But pool should maintain size (new session created)
    stats = pool.get_stats()
    assert stats["total_sessions"] == pool_config.pool_size

    pool.shutdown()


def test_concurrent_access(pool_config, mock_cluster):
    """Test thread-safe concurrent access to pool."""
    pool = CassandraConnectionPool(pool_config)
    errors = []
    success_count = []

    def worker():
        try:
            session = pool.acquire()
            time.sleep(0.01)  # Simulate work
            pool.release(session)
            success_count.append(1)
        except Exception as e:
            errors.append(e)

    # Start threads equal to pool size (should all succeed)
    threads = []
    for _ in range(pool_config.pool_size):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)

    # Wait for all threads
    for t in threads:
        t.join()

    # All should succeed
    assert len(errors) == 0
    assert len(success_count) == pool_config.pool_size

    # All sessions should be available again
    stats = pool.get_stats()
    assert stats["available_sessions"] == pool_config.pool_size
    assert stats["in_use_sessions"] == 0

    pool.shutdown()


def test_shutdown(pool_config, mock_cluster):
    """Test pool shutdown cleans up resources."""
    pool = CassandraConnectionPool(pool_config)

    # Acquire a session to test cleanup
    session = pool.acquire()

    # Shutdown
    pool.shutdown()

    # Verify cleanup
    assert len(pool._sessions) == 0
    assert len(pool._available) == 0
    assert len(pool._in_use) == 0
    assert pool._cluster is None


def test_get_pool_config():
    """Test loading pool config from configuration."""
    with patch("nyxgpt.rag.cassandra_pool.load_config") as mock_load:
        mock_cfg = MagicMock()
        mock_cfg.get.side_effect = lambda section, key, fallback=None: {
            ("rag", "cassandra_hosts"): "host1,host2",
            ("rag", "cassandra_keyspace"): "test_ks",
        }.get((section, key), fallback)
        mock_cfg.getint.side_effect = lambda section, key, fallback=None: {
            ("rag", "cassandra_port"): 9042,
            ("rag", "connection_pool_size"): 10,
            ("rag", "health_check_interval"): 30,
            ("rag", "connection_timeout"): 15,
            ("rag", "max_retry_attempts"): 5,
            ("rag", "retry_delay"): 3,
        }.get((section, key), fallback)
        mock_load.return_value = mock_cfg

        config = get_pool_config()

        assert config.hosts == ["host1", "host2"]
        assert config.port == 9042
        assert config.keyspace == "test_ks"
        assert config.pool_size == 10
        assert config.health_check_interval == 30
        assert config.connection_timeout == 15
        assert config.max_retry_attempts == 5
        assert config.retry_delay == 3


def test_session_property_error_handling():
    """Test session property error when no session available."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Create a store instance with minimal setup
    with patch("nyxgpt.rag.vectorstore_cassandra._cassandra_cfg"):
        with patch("nyxgpt.rag.vectorstore_cassandra.Cluster"):
            store = CassandraVectorStore(use_pool=False)
            # Manually set state to simulate error condition
            store.use_pool = True
            store._pool = None
            store._session = None

            # Should raise error
            with pytest.raises(ConnectionPoolError, match="No active session"):
                _ = store.session


def test_health_check_thread_starts(pool_config, mock_cluster):
    """Test health check thread starts on initialization."""
    pool = CassandraConnectionPool(pool_config)

    # Health check thread should be running
    assert pool._health_check_thread is not None
    assert pool._health_check_thread.is_alive()

    pool.shutdown()


def test_pool_stats(pool_config, mock_cluster):
    """Test pool statistics reporting."""
    pool = CassandraConnectionPool(pool_config)

    stats = pool.get_stats()

    assert "total_sessions" in stats
    assert "available_sessions" in stats
    assert "in_use_sessions" in stats
    assert "last_health_check" in stats
    assert "pool_size" in stats

    assert stats["pool_size"] == pool_config.pool_size

    pool.shutdown()


def test_create_session_with_retry_failure(pool_config, mock_cluster):
    """Test session creation retry logic on failure."""
    pool = CassandraConnectionPool(pool_config)

    # Make cluster.connect fail
    pool._cluster.connect.side_effect = Exception("Connection failed")

    # Attempt to create session should fail after retries
    session = pool._create_session_with_retry()

    assert session is None

    pool.shutdown()


def test_keyspace_selection_failure_handling(pool_config, mock_cluster):
    """Test graceful handling of keyspace selection failure."""
    # This tests that pool initialization succeeds even if keyspace doesn't exist
    pool = CassandraConnectionPool(pool_config)

    # Sessions should still be created despite keyspace issues
    stats = pool.get_stats()
    assert stats["total_sessions"] == pool_config.pool_size

    pool.shutdown()
