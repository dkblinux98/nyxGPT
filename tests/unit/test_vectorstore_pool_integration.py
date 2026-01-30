"""Integration tests for CassandraVectorStore with connection pooling."""
from unittest.mock import MagicMock, patch
import pytest

from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore
from nyxgpt.rag.cassandra_pool import CassandraConnectionPool


@pytest.fixture(autouse=True)
def reset_pool():
    """Reset pool singleton before each test."""
    CassandraConnectionPool.reset_instance()
    yield
    CassandraConnectionPool.reset_instance()


@pytest.fixture
def mock_pool():
    """Mock connection pool."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"):
        pool = MagicMock()
        session = MagicMock()
        pool.acquire.return_value = session
        pool.get_stats.return_value = {
            "total_sessions": 5,
            "available_sessions": 4,
            "in_use_sessions": 1,
        }
        yield pool


def test_vectorstore_uses_pool_by_default():
    """Test that vectorstore uses pool by default."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"), \
         patch("nyxgpt.rag.vectorstore_cassandra.Cluster"):
        store = CassandraVectorStore(use_pool=True)
        assert store.use_pool is True


def test_vectorstore_legacy_mode():
    """Test vectorstore can use legacy non-pooled mode."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_session = MagicMock()
        mock_cluster.return_value.connect.return_value = mock_session

        store = CassandraVectorStore(use_pool=False)

        assert store.use_pool is False
        assert hasattr(store, "cluster")
        mock_cluster.assert_called_once()


def test_vectorstore_session_property_with_pool():
    """Test session property acquires from pool."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"):
        with patch("nyxgpt.rag.cassandra_pool.CassandraConnectionPool.get_instance") as mock_get:
            mock_pool = MagicMock()
            mock_session = MagicMock()
            mock_pool.acquire.return_value = mock_session
            mock_get.return_value = mock_pool

            store = CassandraVectorStore(use_pool=True)
            session = store.session

            # Should acquire from pool
            mock_pool.acquire.assert_called_once()
            assert session == mock_session


def test_vectorstore_close_releases_to_pool():
    """Test close() releases session back to pool."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"):
        with patch("nyxgpt.rag.cassandra_pool.CassandraConnectionPool.get_instance") as mock_get:
            mock_pool = MagicMock()
            mock_session = MagicMock()
            mock_pool.acquire.return_value = mock_session
            mock_get.return_value = mock_pool

            store = CassandraVectorStore(use_pool=True)
            _ = store.session  # Acquire session

            # Close should release
            store.close()

            mock_pool.release.assert_called_once_with(mock_session)
            assert store._session is None


def test_vectorstore_close_legacy_mode():
    """Test close() in legacy mode shuts down cluster."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_session = MagicMock()
        cluster_instance = MagicMock()
        cluster_instance.connect.return_value = mock_session
        mock_cluster.return_value = cluster_instance

        store = CassandraVectorStore(use_pool=False)
        store.close()

        # Should shutdown session and cluster
        mock_session.shutdown.assert_called_once()
        cluster_instance.shutdown.assert_called_once()


def test_vectorstore_fallback_on_pool_error():
    """Test vectorstore falls back to direct connection on pool error."""
    with patch("nyxgpt.rag.cassandra_pool.get_pool_config") as mock_config, \
         patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        # Make pool config fail
        mock_config.side_effect = Exception("Pool config error")

        mock_session = MagicMock()
        cluster_instance = MagicMock()
        cluster_instance.connect.return_value = mock_session
        mock_cluster.return_value = cluster_instance

        # Should fallback to direct connection
        store = CassandraVectorStore(use_pool=True)

        # Verify it fell back
        assert store.use_pool is False
        assert hasattr(store, "cluster")


def test_vectorstore_multiple_instances_share_pool():
    """Test multiple vectorstore instances share the same pool."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"):
        with patch("nyxgpt.rag.cassandra_pool.CassandraConnectionPool.get_instance") as mock_get:
            mock_pool = MagicMock()
            mock_get.return_value = mock_pool

            store1 = CassandraVectorStore(use_pool=True, collection="coll1")
            store2 = CassandraVectorStore(use_pool=True, collection="coll2")

            # Both should get same pool instance
            assert mock_get.call_count == 2
            # Verify they can both access pool
            assert store1._pool is not None
            assert store2._pool is not None


def test_vectorstore_session_reuse():
    """Test session is reused within same vectorstore instance."""
    with patch("nyxgpt.rag.cassandra_pool.Cluster"):
        with patch("nyxgpt.rag.cassandra_pool.CassandraConnectionPool.get_instance") as mock_get:
            mock_pool = MagicMock()
            mock_session = MagicMock()
            mock_pool.acquire.return_value = mock_session
            mock_get.return_value = mock_pool

            store = CassandraVectorStore(use_pool=True)

            # Access session multiple times
            session1 = store.session
            session2 = store.session
            session3 = store.session

            # Should only acquire once
            mock_pool.acquire.assert_called_once()
            assert session1 == session2 == session3 == mock_session
