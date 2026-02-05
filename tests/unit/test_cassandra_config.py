"""Tests for Cassandra read replica configuration."""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock, patch
import pytest
from cassandra.query import ConsistencyLevel
from cassandra.policies import TokenAwarePolicy, RoundRobinPolicy, DCAwareRoundRobinPolicy


@pytest.mark.unit
def test_cassandra_config_default_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that default Cassandra configuration values are correctly parsed."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    result = _cassandra_cfg()
    assert result.hosts == ["127.0.0.1"]
    assert result.port == 9042
    assert result.keyspace == "nyxgpt"
    assert result.table == "rag_chunks"
    assert result.replication_strategy == "SimpleStrategy"
    assert result.replication_factor == 1
    assert result.read_consistency == ConsistencyLevel.LOCAL_ONE
    assert result.write_consistency == ConsistencyLevel.LOCAL_QUORUM
    assert result.load_balancing_policy == "TokenAwarePolicy"


@pytest.mark.unit
def test_cassandra_config_multiple_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that multiple Cassandra hosts are correctly parsed."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "192.168.1.10, 192.168.1.11, 192.168.1.12",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    result = _cassandra_cfg()
    assert result.hosts == ["192.168.1.10", "192.168.1.11", "192.168.1.12"]


@pytest.mark.unit
def test_cassandra_config_read_replica_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that read replica configuration settings are correctly parsed."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "node1,node2,node3",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "replication_strategy": "NetworkTopologyStrategy",
        "replication_factor": "3",
        "read_consistency": "LOCAL_QUORUM",
        "write_consistency": "QUORUM",
        "load_balancing_policy": "DCAwareRoundRobinPolicy",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    result = _cassandra_cfg()
    assert result.hosts == ["node1", "node2", "node3"]
    assert result.replication_strategy == "NetworkTopologyStrategy"
    assert result.replication_factor == 3
    assert result.read_consistency == ConsistencyLevel.LOCAL_QUORUM
    assert result.write_consistency == ConsistencyLevel.QUORUM
    assert result.load_balancing_policy == "DCAwareRoundRobinPolicy"


@pytest.mark.unit
def test_cassandra_config_consistency_level_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that consistency level strings are correctly mapped to enums."""
    test_cases = [
        ("ONE", ConsistencyLevel.ONE),
        ("LOCAL_ONE", ConsistencyLevel.LOCAL_ONE),
        ("QUORUM", ConsistencyLevel.QUORUM),
        ("LOCAL_QUORUM", ConsistencyLevel.LOCAL_QUORUM),
        ("ALL", ConsistencyLevel.ALL),
        ("ANY", ConsistencyLevel.ANY),
        ("TWO", ConsistencyLevel.TWO),
        ("THREE", ConsistencyLevel.THREE),
    ]

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    for level_str, expected_enum in test_cases:
        cfg = ConfigParser()
        cfg["rag"] = {
            "cassandra_hosts": "127.0.0.1",
            "cassandra_port": "9042",
            "cassandra_keyspace": "nyxgpt",
            "cassandra_table": "rag_chunks",
            "read_consistency": level_str,
            "write_consistency": level_str,
        }

        monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

        result = _cassandra_cfg()
        assert result.read_consistency == expected_enum
        assert result.write_consistency == expected_enum


@pytest.mark.unit
def test_cassandra_config_case_insensitive_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that consistency level strings are case-insensitive."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "read_consistency": "local_one",  # lowercase
        "write_consistency": "Local_Quorum",  # mixed case
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    result = _cassandra_cfg()
    assert result.read_consistency == ConsistencyLevel.LOCAL_ONE
    assert result.write_consistency == ConsistencyLevel.LOCAL_QUORUM


@pytest.mark.unit
def test_cassandra_config_invalid_consistency_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that invalid consistency levels fall back to defaults."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "read_consistency": "INVALID_LEVEL",
        "write_consistency": "ANOTHER_INVALID",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    result = _cassandra_cfg()
    # Should fall back to defaults
    assert result.read_consistency == ConsistencyLevel.LOCAL_ONE
    assert result.write_consistency == ConsistencyLevel.LOCAL_QUORUM


@pytest.mark.unit
def test_load_balancing_policy_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that load balancing policies are correctly created."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "load_balancing_policy": "TokenAwarePolicy",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock Cluster to avoid actual connection
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_cluster.return_value.connect.return_value = Mock()

        from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore()
        policy = store._create_load_balancing_policy()

        # TokenAwarePolicy should wrap RoundRobinPolicy
        assert isinstance(policy, TokenAwarePolicy)


@pytest.mark.unit
def test_load_balancing_policy_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test RoundRobinPolicy creation."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "load_balancing_policy": "RoundRobinPolicy",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_cluster.return_value.connect.return_value = Mock()

        from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore()
        policy = store._create_load_balancing_policy()

        assert isinstance(policy, RoundRobinPolicy)


@pytest.mark.unit
def test_load_balancing_policy_dc_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test DCAwareRoundRobinPolicy creation."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "load_balancing_policy": "DCAwareRoundRobinPolicy",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_cluster.return_value.connect.return_value = Mock()

        from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore()
        policy = store._create_load_balancing_policy()

        assert isinstance(policy, DCAwareRoundRobinPolicy)


@pytest.mark.unit
def test_load_balancing_policy_invalid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that invalid load balancing policy falls back to TokenAwarePolicy."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "load_balancing_policy": "InvalidPolicy",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster:
        mock_cluster.return_value.connect.return_value = Mock()

        from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

        store = CassandraVectorStore()
        policy = store._create_load_balancing_policy()

        # Should fall back to TokenAwarePolicy
        assert isinstance(policy, TokenAwarePolicy)
