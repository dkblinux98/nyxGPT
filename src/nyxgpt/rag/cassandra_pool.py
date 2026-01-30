"""Cassandra connection pool manager.

Provides connection pooling with health checking and graceful reconnection
for Cassandra database connections used by the RAG vector store.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional
from cassandra.cluster import Cluster, Session, NoHostAvailable
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy

from nyxgpt.config import load_config


@dataclass(frozen=True)
class PoolConfig:
    """Configuration for Cassandra connection pool."""

    hosts: list[str]
    port: int
    keyspace: str
    pool_size: int
    health_check_interval: int
    connection_timeout: int
    max_retry_attempts: int
    retry_delay: int


class ConnectionPoolError(RuntimeError):
    """Raised when connection pool encounters an error."""
    pass


class CassandraConnectionPool:
    """Thread-safe connection pool for Cassandra sessions.

    Manages a pool of Cassandra sessions with health checking and
    graceful reconnection. Uses singleton pattern to ensure only
    one pool exists per configuration.

    Features:
    - Configurable pool size
    - Connection reuse
    - Health checking with configurable interval
    - Graceful reconnection on failure
    - Thread-safe access
    """

    _instance: Optional[CassandraConnectionPool] = None
    _lock = threading.Lock()

    def __init__(self, config: PoolConfig) -> None:
        """Initialize connection pool.

        Args:
            config: Pool configuration
        """
        self.config = config
        self._cluster: Optional[Cluster] = None
        self._sessions: list[Session] = []
        self._available: list[Session] = []
        self._in_use: set[Session] = set()
        self._session_lock = threading.Lock()
        self._health_check_thread: Optional[threading.Thread] = None
        self._shutdown_flag = threading.Event()
        self._last_health_check = 0.0

        # Initialize cluster connection
        self._initialize_cluster()

    @classmethod
    def get_instance(cls, config: Optional[PoolConfig] = None) -> CassandraConnectionPool:
        """Get singleton instance of connection pool.

        Args:
            config: Pool configuration (required on first call, optional afterwards)

        Returns:
            Connection pool instance

        Raises:
            ConnectionPoolError: If config is None and instance doesn't exist
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    if config is None:
                        raise ConnectionPoolError(
                            "Configuration required for first initialization"
                        )
                    cls._instance = cls(config)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.shutdown()
                cls._instance = None

    def _initialize_cluster(self) -> None:
        """Initialize Cassandra cluster connection."""
        try:
            # Use load balancing policy for better performance
            load_balancing_policy = TokenAwarePolicy(
                DCAwareRoundRobinPolicy(local_dc=None)
            )

            self._cluster = Cluster(
                contact_points=self.config.hosts,
                port=self.config.port,
                load_balancing_policy=load_balancing_policy,
                connect_timeout=self.config.connection_timeout,
                control_connection_timeout=self.config.connection_timeout,
            )

            # Create pool of sessions
            for _ in range(self.config.pool_size):
                session = self._cluster.connect()
                # Select keyspace if configured
                if self.config.keyspace:
                    try:
                        session.execute(f"USE {self.config.keyspace}")
                    except Exception:
                        # Keyspace might not exist yet, caller will create it
                        pass
                self._sessions.append(session)
                self._available.append(session)

            # Start health check thread
            self._start_health_check()

        except NoHostAvailable as e:
            raise ConnectionPoolError(f"Failed to connect to Cassandra: {e}")
        except Exception as e:
            raise ConnectionPoolError(f"Failed to initialize connection pool: {e}")

    def _start_health_check(self) -> None:
        """Start background health check thread."""
        if self._health_check_thread is None or not self._health_check_thread.is_alive():
            self._shutdown_flag.clear()
            self._health_check_thread = threading.Thread(
                target=self._health_check_loop,
                daemon=True,
                name="cassandra-health-check"
            )
            self._health_check_thread.start()

    def _health_check_loop(self) -> None:
        """Background loop for periodic health checks."""
        while not self._shutdown_flag.is_set():
            # Wait for interval or shutdown signal
            if self._shutdown_flag.wait(timeout=self.config.health_check_interval):
                break

            self._perform_health_check()

    def _perform_health_check(self) -> None:
        """Check health of all sessions and reconnect if needed."""
        self._last_health_check = time.time()

        with self._session_lock:
            unhealthy_sessions: list[Session] = []

            # Check each session
            for session in self._sessions:
                if not self._is_session_healthy(session):
                    unhealthy_sessions.append(session)

            # Replace unhealthy sessions
            if unhealthy_sessions:
                self._replace_unhealthy_sessions(unhealthy_sessions)

    def _is_session_healthy(self, session: Session) -> bool:
        """Check if a session is healthy.

        Args:
            session: Session to check

        Returns:
            True if session is healthy, False otherwise
        """
        try:
            # Simple query to test connection
            session.execute("SELECT now() FROM system.local", timeout=5.0)
            return True
        except Exception:
            return False

    def _replace_unhealthy_sessions(self, unhealthy_sessions: list[Session]) -> None:
        """Replace unhealthy sessions with new ones.

        Args:
            unhealthy_sessions: List of sessions to replace
        """
        for session in unhealthy_sessions:
            try:
                # Remove from pools
                if session in self._available:
                    self._available.remove(session)
                if session in self._in_use:
                    self._in_use.remove(session)
                if session in self._sessions:
                    self._sessions.remove(session)

                # Close old session
                try:
                    session.shutdown()
                except Exception:
                    pass

                # Create new session with retry
                new_session = self._create_session_with_retry()
                if new_session:
                    self._sessions.append(new_session)
                    self._available.append(new_session)
            except Exception:
                # Log error but continue replacing other sessions
                pass

    def _create_session_with_retry(self) -> Optional[Session]:
        """Create a new session with retry logic.

        Returns:
            New session or None if all retries failed
        """
        for attempt in range(self.config.max_retry_attempts):
            try:
                if self._cluster is None:
                    raise ConnectionPoolError("Cluster not initialized")

                session = self._cluster.connect()

                # Select keyspace if configured
                if self.config.keyspace:
                    try:
                        session.execute(f"USE {self.config.keyspace}")
                    except Exception:
                        # Keyspace might not exist yet
                        pass

                return session
            except Exception:
                if attempt < self.config.max_retry_attempts - 1:
                    time.sleep(self.config.retry_delay)
                else:
                    return None
        return None

    def acquire(self) -> Session:
        """Acquire a session from the pool.

        Returns:
            Available session

        Raises:
            ConnectionPoolError: If no sessions available
        """
        with self._session_lock:
            if not self._available:
                raise ConnectionPoolError("No available sessions in pool")

            session = self._available.pop(0)
            self._in_use.add(session)
            return session

    def release(self, session: Session) -> None:
        """Release a session back to the pool.

        Args:
            session: Session to release
        """
        with self._session_lock:
            if session in self._in_use:
                self._in_use.remove(session)
                # Check health before returning to pool
                if self._is_session_healthy(session):
                    self._available.append(session)
                else:
                    # Replace unhealthy session
                    self._replace_unhealthy_sessions([session])

    def get_stats(self) -> dict:
        """Get pool statistics.

        Returns:
            Dict with pool stats
        """
        with self._session_lock:
            return {
                "total_sessions": len(self._sessions),
                "available_sessions": len(self._available),
                "in_use_sessions": len(self._in_use),
                "last_health_check": self._last_health_check,
                "pool_size": self.config.pool_size,
            }

    def shutdown(self) -> None:
        """Shutdown connection pool and cleanup resources."""
        # Signal health check thread to stop
        self._shutdown_flag.set()

        # Wait for health check thread
        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=5.0)

        # Close all sessions
        with self._session_lock:
            for session in self._sessions:
                try:
                    session.shutdown()
                except Exception:
                    pass
            self._sessions.clear()
            self._available.clear()
            self._in_use.clear()

        # Close cluster
        if self._cluster:
            try:
                self._cluster.shutdown()
            except Exception:
                pass
            self._cluster = None


def get_pool_config() -> PoolConfig:
    """Load pool configuration from config file.

    Returns:
        Pool configuration
    """
    cfg = load_config(None)

    hosts_raw = cfg.get("rag", "cassandra_hosts", fallback="127.0.0.1")
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    port = cfg.getint("rag", "cassandra_port", fallback=9042)
    keyspace = cfg.get("rag", "cassandra_keyspace", fallback="nyxgpt")

    # Pool configuration
    pool_size = cfg.getint("rag", "connection_pool_size", fallback=5)
    health_check_interval = cfg.getint("rag", "health_check_interval", fallback=60)
    connection_timeout = cfg.getint("rag", "connection_timeout", fallback=10)
    max_retry_attempts = cfg.getint("rag", "max_retry_attempts", fallback=3)
    retry_delay = cfg.getint("rag", "retry_delay", fallback=2)

    return PoolConfig(
        hosts=hosts,
        port=port,
        keyspace=keyspace,
        pool_size=pool_size,
        health_check_interval=health_check_interval,
        connection_timeout=connection_timeout,
        max_retry_attempts=max_retry_attempts,
        retry_delay=retry_delay,
    )
