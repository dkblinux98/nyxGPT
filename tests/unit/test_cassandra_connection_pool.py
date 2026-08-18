"""Unit tests for Cassandra connection pooling (#2675).

Tests cover:
- CassandraConfig pool fields
- CassandraConnectionPool creation, session reuse, health check, reconnect, shutdown
- Module-level get_connection_pool / reset_connection_pool helpers
- CassandraVectorStore uses pool (no per-instance Cluster creation)
- CassandraVectorStore.close() does not shut down shared pool
- CassandraVectorStore.reconnect() refreshes session reference
- Config helpers: get_cassandra_pool_size / health_check_interval / reconnect_max_attempts
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides):
    """Return a CassandraConfig with sensible test defaults."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    defaults = {
        "hosts": ["127.0.0.1"],
        "port": 9042,
        "keyspace": "test_ks",
        "table": "test_tbl",
        "pool_size": 2,
        "health_check_interval": 30.0,
        "reconnect_max_attempts": 3,
    }
    defaults.update(overrides)
    return CassandraConfig(**defaults)


# ---------------------------------------------------------------------------
# CassandraConfig – pool fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cassandra_config_pool_defaults():
    """CassandraConfig accepts pool fields with correct defaults."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    cfg = CassandraConfig(hosts=["localhost"], port=9042, keyspace="ks", table="tbl")
    assert cfg.pool_size == 2
    assert cfg.health_check_interval == 30.0
    assert cfg.reconnect_max_attempts == 3


@pytest.mark.unit
def test_cassandra_config_pool_custom():
    """CassandraConfig stores custom pool settings."""
    cfg = _make_cfg(pool_size=4, health_check_interval=15.0, reconnect_max_attempts=5)
    assert cfg.pool_size == 4
    assert cfg.health_check_interval == 15.0
    assert cfg.reconnect_max_attempts == 5


# ---------------------------------------------------------------------------
# CassandraConnectionPool – basic construction and session reuse
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_get_session_creates_cluster_and_session():
    """get_session() creates a Cluster and connects on first call."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        session = pool.get_session()

    assert session is mock_session
    mock_cluster.connect.assert_called_once()


@pytest.mark.unit
def test_pool_get_session_reuses_cached_session():
    """get_session() returns the same session on subsequent calls (connection reuse)."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        s1 = pool.get_session()
        s2 = pool.get_session()

    assert s1 is s2
    # Cluster.connect should only be called once
    mock_cluster.connect.assert_called_once()


@pytest.mark.unit
def test_pool_get_session_different_keyspaces_returns_same_connection():
    """Different keyspace keys result in different cached entries but same cluster."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        s_none = pool.get_session(keyspace=None)
        s_ks = pool.get_session(keyspace="test_ks")

    # Both are the same mock session object (from mock_cluster.connect)
    assert s_none is mock_session
    assert s_ks is mock_session
    # Cluster only created once; connect called twice (once per keyspace key)
    assert mock_cluster.connect.call_count == 2


# ---------------------------------------------------------------------------
# CassandraConnectionPool – shut-down cluster recovery (#3851)
# ---------------------------------------------------------------------------


def _live_cluster():
    """Return a (cluster, session) mock pair that reports itself as live."""
    session = Mock()
    session.is_shutdown = False
    cluster = Mock()
    cluster.is_shutdown = False
    cluster.connect.return_value = session
    return cluster, session


@pytest.mark.unit
def test_driver_object_is_shutdown_only_trusts_a_real_true():
    """Only a driver object whose is_shutdown is True counts as shut down.

    A missing attribute or a truthy non-bool (an auto-generated Mock
    attribute, a stand-in object) must read as *usable*: a false positive
    would rebuild a healthy cluster on every call.
    """
    from nyxgpt.rag.vectorstore_cassandra import driver_object_is_shutdown

    assert driver_object_is_shutdown(object()) is False
    assert driver_object_is_shutdown(Mock(is_shutdown=False)) is False
    assert driver_object_is_shutdown(Mock()) is False  # auto-generated attribute
    assert driver_object_is_shutdown(Mock(is_shutdown=True)) is True


@pytest.mark.unit
def test_pool_rebuilds_cluster_after_it_is_shut_down():
    """get_session() rebuilds a shut-down Cluster instead of reconnecting to it.

    Reproduces the acceptance failure: once the driver shuts the Cluster
    down, `Cluster.connect()` raises "Cluster is already shut down" forever,
    so restarting Cassandra could never recover the API (#3851).
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    cluster1, session1 = _live_cluster()
    cluster2, session2 = _live_cluster()

    pool = CassandraConnectionPool(_make_cfg())

    with patch(
        "nyxgpt.rag.vectorstore_cassandra.Cluster",
        side_effect=[cluster1, cluster2],
    ) as cluster_cls:
        assert pool.get_session() is session1

        # The driver shuts the cluster down (Cassandra went away, a restart,
        # a network event).  Both the cluster and its session are now dead.
        cluster1.is_shutdown = True
        session1.is_shutdown = True
        cluster1.connect.side_effect = RuntimeError("Cluster is already shut down")
        session1.execute.side_effect = RuntimeError("Cluster is already shut down")

        session = pool.get_session()

    assert session is session2, "a fresh session from a rebuilt cluster must be returned"
    assert cluster_cls.call_count == 2, "a new Cluster must be constructed"
    assert pool._cluster is cluster2


@pytest.mark.unit
def test_pool_discards_sessions_bound_to_shut_down_cluster():
    """Sessions cached against a dead cluster are dropped, never handed back."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    cluster1, session1 = _live_cluster()
    cluster2, session2 = _live_cluster()

    pool = CassandraConnectionPool(_make_cfg(health_check_interval=3600.0))

    with patch(
        "nyxgpt.rag.vectorstore_cassandra.Cluster",
        side_effect=[cluster1, cluster2],
    ):
        pool.get_session(keyspace=None)
        pool.get_session(keyspace="test_ks")
        assert set(pool._sessions) == {None, "test_ks"}

        cluster1.is_shutdown = True
        refreshed = pool.get_session(keyspace="test_ks")

    assert refreshed is session2
    assert session1 not in pool._sessions.values()
    assert set(pool._sessions) == {"test_ks"}, "stale keyspace entries must be cleared too"


@pytest.mark.unit
def test_pool_reopens_individually_shut_down_session_on_live_cluster():
    """A dead session on a live cluster is reopened without rebuilding the cluster."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    cluster, session1 = _live_cluster()
    session2 = Mock()
    session2.is_shutdown = False

    pool = CassandraConnectionPool(_make_cfg(health_check_interval=3600.0))

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=cluster) as cluster_cls:
        assert pool.get_session() is session1

        session1.is_shutdown = True
        cluster.connect.return_value = session2
        refreshed = pool.get_session()

    assert refreshed is session2
    assert cluster_cls.call_count == 1, "a live cluster must not be rebuilt"
    assert cluster.connect.call_count == 2


@pytest.mark.unit
def test_pool_keeps_live_cluster_and_session():
    """A live cluster/session pair is reused — no spurious rebuilds."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    cluster, session = _live_cluster()
    pool = CassandraConnectionPool(_make_cfg(health_check_interval=3600.0))

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=cluster) as cluster_cls:
        first = pool.get_session()
        second = pool.get_session()

    assert first is second is session
    assert cluster_cls.call_count == 1
    cluster.connect.assert_called_once()


# ---------------------------------------------------------------------------
# CassandraConnectionPool – health check
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_health_check_success():
    """health_check() returns True when the lightweight query succeeds."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        pool.get_session()  # populate _sessions so health_check has something to ping
        result = pool.health_check()

    assert result is True
    mock_session.execute.assert_called_once_with("SELECT now() FROM system.local")


@pytest.mark.unit
def test_pool_health_check_no_session_returns_true():
    """health_check() returns True (skip) when no session has been established yet."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    pool = CassandraConnectionPool(_make_cfg())
    # _sessions is empty — nothing to check
    assert pool.health_check() is True


@pytest.mark.unit
def test_pool_health_check_failure():
    """health_check() returns False when the query raises an exception."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_session.execute.side_effect = RuntimeError("connection lost")
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        pool.get_session()  # populate _sessions
        result = pool.health_check()

    assert result is False


@pytest.mark.unit
def test_pool_needs_health_check_initially_true():
    """needs_health_check() returns True before any check has been performed."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    pool = CassandraConnectionPool(_make_cfg())
    assert pool.needs_health_check() is True


@pytest.mark.unit
def test_pool_needs_health_check_false_after_recent_check():
    """needs_health_check() returns False immediately after a successful check."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    # Very long interval so the check is never due
    cfg = _make_cfg(health_check_interval=3600.0)
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        pool.get_session()  # populate _sessions
        pool.health_check()

    assert pool.needs_health_check() is False


@pytest.mark.unit
def test_pool_get_session_triggers_health_check_on_established_connection():
    """get_session() executes health-check query when called on an already-connected pool.

    This test exercises the path that previously caused mutual recursion:
      get_session() → health_check() → get_session() → …

    After the fix, health_check() reads _sessions directly, so the chain
    terminates correctly and session.execute is called exactly once.
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    # Short interval so needs_health_check() is always True
    cfg = _make_cfg(health_check_interval=0.0)
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        # First call: _connected is False, no health check triggered
        pool.get_session()
        mock_session.execute.assert_not_called()

        # Second call: _connected is True, needs_health_check() returns True
        # → health_check() should fire and execute the SELECT query
        pool.get_session()

    mock_session.execute.assert_called_once_with("SELECT now() FROM system.local")


# ---------------------------------------------------------------------------
# CassandraConnectionPool – reconnect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_reconnect_success():
    """reconnect() shuts down old cluster and creates a new connection."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session1 = Mock()
    mock_cluster1 = Mock()
    mock_cluster1.connect.return_value = mock_session1

    mock_session2 = Mock()
    mock_cluster2 = Mock()
    mock_cluster2.connect.return_value = mock_session2

    cfg = _make_cfg(reconnect_max_attempts=2)
    pool = CassandraConnectionPool(cfg)

    with patch(
        "nyxgpt.rag.vectorstore_cassandra.Cluster",
        side_effect=[mock_cluster1, mock_cluster2],
    ):
        pool.get_session()  # establishes first connection
        result = pool.reconnect()

    assert result is True
    # Old cluster should have been shut down
    mock_cluster1.shutdown.assert_called_once()


@pytest.mark.unit
def test_pool_reconnect_failure_exhausts_attempts():
    """reconnect() returns False when all attempts fail."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_cluster = Mock()
    mock_cluster.connect.side_effect = RuntimeError("cannot connect")

    cfg = _make_cfg(reconnect_max_attempts=2)
    pool = CassandraConnectionPool(cfg)

    with (
        patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster),
        patch("time.sleep"),
    ):
        result = pool.reconnect()

    assert result is False


# ---------------------------------------------------------------------------
# CassandraConnectionPool – shutdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_shutdown_closes_sessions_and_cluster():
    """shutdown() calls shutdown on all sessions and the cluster."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConnectionPool

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    cfg = _make_cfg()
    pool = CassandraConnectionPool(cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        pool.get_session()
        pool.shutdown()

    mock_session.shutdown.assert_called_once()
    mock_cluster.shutdown.assert_called_once()
    assert pool._sessions == {}
    assert pool._cluster is None


# ---------------------------------------------------------------------------
# Module-level get_connection_pool / reset_connection_pool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_connection_pool_returns_singleton():
    """get_connection_pool returns the same instance for identical config."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()
    cfg = _make_cfg()

    pool1 = vs.get_connection_pool(cfg)
    pool2 = vs.get_connection_pool(cfg)

    assert pool1 is pool2
    vs.reset_connection_pool()


@pytest.mark.unit
def test_get_connection_pool_replaces_on_config_change():
    """get_connection_pool creates a new pool when config changes."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()
    cfg1 = _make_cfg(pool_size=2)
    cfg2 = _make_cfg(pool_size=4)

    pool1 = vs.get_connection_pool(cfg1)
    pool2 = vs.get_connection_pool(cfg2)

    assert pool1 is not pool2
    vs.reset_connection_pool()


@pytest.mark.unit
def test_reset_connection_pool_clears_singleton():
    """reset_connection_pool discards the singleton so next call creates fresh pool."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    cfg = _make_cfg()
    pool1 = vs.get_connection_pool(cfg)
    vs.reset_connection_pool()
    pool2 = vs.get_connection_pool(cfg)

    assert pool1 is not pool2
    vs.reset_connection_pool()


# ---------------------------------------------------------------------------
# CassandraVectorStore – pool integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_vectorstore_uses_pool_not_direct_cluster():
    """CassandraVectorStore.__init__ obtains its session from the pool."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = vs.CassandraVectorStore()

    assert store.session is mock_session
    assert store._pool is not None

    vs.reset_connection_pool()


@pytest.mark.unit
def test_vectorstore_shares_pool_across_instances():
    """Multiple CassandraVectorStore instances share the same pool."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store1 = vs.CassandraVectorStore()
        store2 = vs.CassandraVectorStore(collection="other")

    assert store1._pool is store2._pool
    # Cluster should only be instantiated once despite two stores
    assert mock_cluster.connect.call_count == 1

    vs.reset_connection_pool()


@pytest.mark.unit
def test_vectorstore_close_does_not_shutdown_pool():
    """CassandraVectorStore.close() does not shut down the shared cluster."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = vs.CassandraVectorStore()
        store.close()

    mock_session.shutdown.assert_not_called()
    mock_cluster.shutdown.assert_not_called()

    vs.reset_connection_pool()


@pytest.mark.unit
def test_vectorstore_reconnect_refreshes_session():
    """CassandraVectorStore.reconnect() obtains a fresh session after reconnect."""
    import nyxgpt.rag.vectorstore_cassandra as vs

    vs.reset_connection_pool()

    mock_session1 = Mock()
    mock_cluster1 = Mock()
    mock_cluster1.connect.return_value = mock_session1

    mock_session2 = Mock()
    mock_cluster2 = Mock()
    mock_cluster2.connect.return_value = mock_session2

    with patch(
        "nyxgpt.rag.vectorstore_cassandra.Cluster",
        side_effect=[mock_cluster1, mock_cluster2],
    ):
        store = vs.CassandraVectorStore()
        old_session = store.session
        result = store.reconnect()

    assert result is True
    assert store.session is not old_session
    assert store._keyspace_ready is False
    assert store._migration_checked is False

    vs.reset_connection_pool()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_cassandra_pool_size_default():
    """get_cassandra_pool_size returns 2 when not configured."""
    from nyxgpt.config import get_cassandra_pool_size

    cfg = ConfigParser()
    assert get_cassandra_pool_size(cfg) == 2


@pytest.mark.unit
def test_get_cassandra_pool_size_custom():
    """get_cassandra_pool_size reads from [rag] cassandra_pool_size."""
    from nyxgpt.config import get_cassandra_pool_size

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "cassandra_pool_size", "5")
    assert get_cassandra_pool_size(cfg) == 5


@pytest.mark.unit
def test_get_cassandra_pool_size_clamps_to_range():
    """get_cassandra_pool_size clamps out-of-range values."""
    from nyxgpt.config import get_cassandra_pool_size

    cfg_low = ConfigParser()
    cfg_low.add_section("rag")
    cfg_low.set("rag", "cassandra_pool_size", "0")
    assert get_cassandra_pool_size(cfg_low) == 1

    cfg_high = ConfigParser()
    cfg_high.add_section("rag")
    cfg_high.set("rag", "cassandra_pool_size", "100")
    assert get_cassandra_pool_size(cfg_high) == 16


@pytest.mark.unit
def test_get_cassandra_health_check_interval_default():
    """get_cassandra_health_check_interval returns 30.0 when not configured."""
    from nyxgpt.config import get_cassandra_health_check_interval

    cfg = ConfigParser()
    assert get_cassandra_health_check_interval(cfg) == 30.0


@pytest.mark.unit
def test_get_cassandra_health_check_interval_custom():
    """get_cassandra_health_check_interval reads configured value."""
    from nyxgpt.config import get_cassandra_health_check_interval

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "cassandra_health_check_interval", "60.0")
    assert get_cassandra_health_check_interval(cfg) == 60.0


@pytest.mark.unit
def test_get_cassandra_reconnect_max_attempts_default():
    """get_cassandra_reconnect_max_attempts returns 3 when not configured."""
    from nyxgpt.config import get_cassandra_reconnect_max_attempts

    cfg = ConfigParser()
    assert get_cassandra_reconnect_max_attempts(cfg) == 3


@pytest.mark.unit
def test_get_cassandra_reconnect_max_attempts_custom():
    """get_cassandra_reconnect_max_attempts reads configured value."""
    from nyxgpt.config import get_cassandra_reconnect_max_attempts

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "cassandra_reconnect_max_attempts", "7")
    assert get_cassandra_reconnect_max_attempts(cfg) == 7
