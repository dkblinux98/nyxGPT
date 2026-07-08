from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from cassandra.cluster import Cluster, Session
from cassandra.concurrent import execute_concurrent
from cassandra.query import BatchStatement, BatchType, PreparedStatement, SimpleStatement

from nyxgpt.config import (
    get_ann_oversample_factor,
    get_cassandra_batch_query_concurrency,
    get_vector_similarity_function,
    load_config,
)

log = logging.getLogger(__name__)

# Maps the config-level similarity function name to the CQL function used to
# compute a result's score and the Cassandra SAI index build option. These
# must stay in sync: scoring a COSINE-built index with similarity_dot_product
# (or vice versa) produces meaningless scores.
_SIMILARITY_CQL_FUNCTION = {
    "cosine": "similarity_cosine",
    "dot_product": "similarity_dot_product",
    "euclidean": "similarity_euclidean",
}
_SIMILARITY_SAI_OPTION = {
    "cosine": "COSINE",
    "dot_product": "DOT_PRODUCT",
    "euclidean": "EUCLIDEAN",
}


@dataclass(frozen=True)
class CassandraConfig:
    hosts: list[str]
    port: int
    keyspace: str
    table: str
    pool_size: int = 2
    health_check_interval: float = 30.0
    reconnect_max_attempts: int = 3
    batch_size: int = 20
    similarity_function: str = "cosine"
    ann_oversample_factor: float = 1.0
    batch_query_concurrency: int = 4


@dataclass
class MetadataFilter:
    """Filter criteria for RAG queries based on document metadata.

    All filters are optional and combined with AND logic.

    Attributes:
        doc_ids: Filter by exact document IDs (OR logic within list)
        filename: Filter by exact or partial filename match (case-insensitive)
        tags: Filter by tags (doc must have ALL specified tags)
        date_from: Filter by ingestion date >= this datetime
        date_to: Filter by ingestion date <= this datetime
    """

    doc_ids: list[str] | None = None
    filename: str | None = None
    tags: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


@dataclass
class VectorSearchDebugMetrics:
    """Debug metrics for vector search operations."""

    raw_results_count: int
    score_min: float | None
    score_max: float | None
    score_mean: float | None
    vector_search_time_ms: float


class VectorStoreError(RuntimeError):
    pass


# Stay well under Cassandra's default batch_size_fail_threshold_in_kb (50KB) -
# a row's embedding vector alone can be several KB, so the configured
# cassandra_batch_size row count is not sufficient to bound payload size.
_MAX_BATCH_BYTES = 40 * 1024


def _estimate_row_bytes(text: str, embedding: list[float], metadata_json: str) -> int:
    """Rough estimate of a chunk row's serialized CQL payload size in bytes."""
    return len(text.encode("utf-8")) + len(embedding) * 4 + len(metadata_json.encode("utf-8")) + 128


def _candidate_fetch_multiplier(metadata_filter: MetadataFilter | None) -> int:
    """Determine how many extra ANN candidates to fetch per requested result.

    Each active metadata filter predicate can independently reject a
    candidate row, so the more filters are active the more candidates need
    to be pulled from the ANN search to still end up with ``k`` results
    after post-filtering. Capped to avoid pulling excessive rows when many
    filters are combined.
    """
    if metadata_filter is None:
        return 1

    active_filters = sum(
        1
        for value in (
            metadata_filter.doc_ids,
            metadata_filter.filename,
            metadata_filter.tags,
            metadata_filter.date_from,
            metadata_filter.date_to,
        )
        if value
    )
    if active_filters == 0:
        return 1

    return min(2 + active_filters, 6)


def _filter_and_score_rows(
    rows: Iterable,
    k: int,
    embedding_model: str | None,
    metadata_filter: MetadataFilter | None,
) -> tuple[list[dict], list[float]]:
    """Apply embedding-model/metadata filtering to raw ANN rows and score them.

    Shared between :meth:`CassandraVectorStore.query_by_embedding` and
    :meth:`CassandraVectorStore.query_by_embeddings_batch` so both the single-
    and batch-query paths post-filter identically.

    Returns:
        Tuple of (result dicts limited to ``k``, scores for those results).
    """
    out: list[dict] = []
    scores: list[float] = []
    for r in rows:
        if (
            embedding_model is not None
            and hasattr(r, "embedding_model")
            and r.embedding_model != embedding_model
        ):
            continue

        metadata = json.loads(r.metadata) if r.metadata else {}

        if metadata_filter:
            if metadata_filter.doc_ids and r.doc_id not in metadata_filter.doc_ids:
                continue

            if metadata_filter.filename:
                doc_filename = metadata.get("filename", "")
                if metadata_filter.filename.lower() not in doc_filename.lower():
                    continue

            if metadata_filter.tags:
                doc_tags = metadata.get("tags", [])
                if not isinstance(doc_tags, list):
                    continue
                if not all(tag in doc_tags for tag in metadata_filter.tags):
                    continue

            if metadata_filter.date_from or metadata_filter.date_to:
                if not hasattr(r, "ingested_at") or r.ingested_at is None:
                    continue
                if metadata_filter.date_from and r.ingested_at < metadata_filter.date_from:
                    continue
                if metadata_filter.date_to and r.ingested_at > metadata_filter.date_to:
                    continue

        score = float(r.score) if hasattr(r, "score") and r.score is not None else 0.0
        result = {
            "doc_id": r.doc_id,
            "chunk_id": r.chunk_id,
            "text": r.text,
            "metadata": metadata,
            "score": score,
            "embedding_model": r.embedding_model if hasattr(r, "embedding_model") else None,
            "embedding_dim": r.embedding_dim if hasattr(r, "embedding_dim") else None,
        }
        out.append(result)
        scores.append(score)

        if len(out) >= k:
            break

    return out, scores


def _cassandra_cfg() -> CassandraConfig:
    cfg = load_config(None)

    hosts_raw = cfg.get("rag", "cassandra_hosts", fallback="127.0.0.1")
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    port = cfg.getint("rag", "cassandra_port", fallback=9042)
    keyspace = cfg.get("rag", "cassandra_keyspace", fallback="nyxgpt")
    table = cfg.get("rag", "cassandra_table", fallback="rag_chunks")
    pool_size = cfg.getint("rag", "cassandra_pool_size", fallback=2)
    health_check_interval = cfg.getfloat("rag", "cassandra_health_check_interval", fallback=30.0)
    reconnect_max_attempts = cfg.getint("rag", "cassandra_reconnect_max_attempts", fallback=3)
    batch_size = cfg.getint("rag", "cassandra_batch_size", fallback=20)
    similarity_function = get_vector_similarity_function(cfg)
    ann_oversample_factor = get_ann_oversample_factor(cfg)
    batch_query_concurrency = get_cassandra_batch_query_concurrency(cfg)

    return CassandraConfig(
        hosts=hosts,
        port=port,
        keyspace=keyspace,
        table=table,
        pool_size=pool_size,
        health_check_interval=health_check_interval,
        reconnect_max_attempts=reconnect_max_attempts,
        batch_size=batch_size,
        similarity_function=similarity_function,
        ann_oversample_factor=ann_oversample_factor,
        batch_query_concurrency=batch_query_concurrency,
    )


class CassandraConnectionPool:
    """Shared connection pool for Cassandra.

    Maintains a single Cluster and per-keyspace Session objects that are
    reused across CassandraVectorStore instances. Provides health checking
    and graceful reconnection.

    Use :func:`get_connection_pool` to obtain the module-level singleton.
    Use :func:`reset_connection_pool` to clear the singleton (for testing).
    """

    def __init__(self, cfg: CassandraConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cluster: Cluster | None = None
        self._sessions: dict[str | None, Session] = {}
        self._last_health_check: float = 0.0
        self._connected = False

    def _make_cluster(self) -> Cluster:
        """Create a new Cluster with pooling and reconnection settings."""
        try:
            from cassandra.policies import ExponentialReconnectionPolicy

            reconnection_policy = ExponentialReconnectionPolicy(
                base_delay=1.0,
                max_delay=60.0,
            )
        except Exception:
            reconnection_policy = None

        kwargs: dict = {
            "port": self.cfg.port,
        }
        if reconnection_policy is not None:
            kwargs["reconnection_policy"] = reconnection_policy

        # Configure driver-level connection pool size
        try:
            from cassandra.cluster import PoolingOptions
            from cassandra.policies import HostDistance

            pooling_opts = PoolingOptions()
            pooling_opts.core_connections_per_host[HostDistance.LOCAL] = self.cfg.pool_size
            pooling_opts.max_connections_per_host[HostDistance.LOCAL] = max(
                self.cfg.pool_size * 2, 4
            )
            kwargs["pooling_options"] = pooling_opts
        except Exception:
            # PoolingOptions not available or misconfigured — proceed without it
            pass

        return Cluster(self.cfg.hosts, **kwargs)

    def _connect(self) -> Session:
        """Connect to Cassandra and return a session."""
        if self._cluster is None:
            self._cluster = self._make_cluster()
        session = self._cluster.connect()
        self._connected = True
        return session

    def get_session(self, keyspace: str | None = None) -> Session:
        """Return a cached session, creating one if needed.

        Sessions are cached per keyspace key.  A ``None`` keyspace means no
        keyspace has been selected on the session yet (the caller is
        responsible for issuing ``USE <keyspace>`` or using fully qualified
        table names).

        If enough time has elapsed since the last health check
        (:meth:`needs_health_check` returns ``True``) and a connection already
        exists, a lightweight health check query is executed before returning
        the cached session.  If the check fails a warning is logged but the
        session is still returned; call :meth:`reconnect` explicitly when
        stricter error handling is required.

        Args:
            keyspace: Optional keyspace to cache the session under.

        Returns:
            A connected Cassandra Session.
        """
        # Run the health check *outside* the lock so that the I/O does not
        # block other threads.  Only check when there is an established
        # connection – the very first call will create the session below.
        if self._connected and self.needs_health_check():
            self.health_check()

        with self._lock:
            if keyspace not in self._sessions:
                try:
                    self._sessions[keyspace] = self._connect()
                except Exception as exc:
                    raise VectorStoreError(
                        f"Failed to connect to Cassandra at "
                        f"{self.cfg.hosts}:{self.cfg.port}: {exc}"
                    ) from exc
            return self._sessions[keyspace]

    def health_check(self) -> bool:
        """Check whether the pool connection is alive.

        Reads a cached session directly from ``_sessions`` under the lock —
        it must never call :meth:`get_session` to avoid mutual recursion with
        the health-check trigger inside ``get_session``.

        Returns ``True`` if no session exists yet (nothing to check) or if the
        lightweight ``SELECT now() FROM system.local`` query succeeds.
        Returns ``False`` when the query raises, and logs a warning.
        Updates the internal last-check timestamp on success.
        """
        with self._lock:
            session = next(iter(self._sessions.values()), None)
        if session is None:
            return True  # no connection established yet; nothing to check
        try:
            session.execute("SELECT now() FROM system.local")
            self._last_health_check = time.monotonic()
            return True
        except Exception as exc:
            log.warning("Cassandra health check failed: %s", exc)
            return False

    def needs_health_check(self) -> bool:
        """Return True if enough time has elapsed since the last health check."""
        if self._last_health_check == 0.0:
            return True
        return (time.monotonic() - self._last_health_check) >= self.cfg.health_check_interval

    def reconnect(self) -> bool:
        """Attempt to reconnect to Cassandra, replacing all cached state.

        Tries up to ``cfg.reconnect_max_attempts`` times with exponential
        back-off (1 s, 2 s, 4 s, …).

        Returns:
            ``True`` if reconnection succeeded, ``False`` otherwise.
        """
        with self._lock:
            self._sessions.clear()
            old_cluster = self._cluster
            self._cluster = None
            self._connected = False

            # Shut down old cluster quietly
            if old_cluster is not None:
                with contextlib.suppress(Exception):
                    old_cluster.shutdown()

        delay = 1.0
        for attempt in range(1, self.cfg.reconnect_max_attempts + 1):
            try:
                log.info(
                    "Cassandra reconnect attempt %d/%d",
                    attempt,
                    self.cfg.reconnect_max_attempts,
                )
                self.get_session()
                log.info("Cassandra reconnected successfully")
                return True
            except Exception as exc:
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)
                if attempt < self.cfg.reconnect_max_attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)

        log.error(
            "Failed to reconnect to Cassandra after %d attempts",
            self.cfg.reconnect_max_attempts,
        )
        return False

    def shutdown(self) -> None:
        """Gracefully shut down all sessions and the cluster."""
        with self._lock:
            for session in self._sessions.values():
                with contextlib.suppress(Exception):
                    session.shutdown()
            self._sessions.clear()

            if self._cluster is not None:
                with contextlib.suppress(Exception):
                    self._cluster.shutdown()
                self._cluster = None
            self._connected = False


# ---------------------------------------------------------------------------
# Module-level singleton pool management
# ---------------------------------------------------------------------------

_pool_lock: threading.Lock = threading.Lock()
_pool_instance: CassandraConnectionPool | None = None


def get_connection_pool(cfg: CassandraConfig) -> CassandraConnectionPool:
    """Return the module-level singleton connection pool.

    Creates the pool on first call.  Subsequent calls with a *different*
    ``cfg`` will replace the existing pool (useful when config is reloaded).

    Args:
        cfg: Cassandra configuration.  When the config changes the old pool
            is shut down and a new one is created.

    Returns:
        The active :class:`CassandraConnectionPool` instance.
    """
    global _pool_instance

    with _pool_lock:
        if _pool_instance is None or _pool_instance.cfg != cfg:
            if _pool_instance is not None:
                with contextlib.suppress(Exception):
                    _pool_instance.shutdown()
            _pool_instance = CassandraConnectionPool(cfg)

    return _pool_instance


def reset_connection_pool() -> None:
    """Shut down and discard the module-level pool.

    Intended for use in tests and teardown scenarios.  After calling this
    function the next call to :func:`get_connection_pool` will create a
    fresh pool.
    """
    global _pool_instance

    with _pool_lock:
        if _pool_instance is not None:
            with contextlib.suppress(Exception):
                _pool_instance.shutdown()
            _pool_instance = None


class CassandraVectorStore:
    def __init__(self, *, collection: str = "default") -> None:
        """Initialize Cassandra vector store.

        Connections are reused from the module-level
        :class:`CassandraConnectionPool` singleton so that multiple store
        instances share the same underlying Cluster.

        Args:
            collection: Collection name for multi-model support (default: "default")
        """
        self.cfg = _cassandra_cfg()
        self.collection = collection
        self._pool = get_connection_pool(self.cfg)
        # Obtain a shared session from the pool (no keyspace selected yet).
        self.session = self._pool.get_session()
        self._keyspace_ready = False
        self._migration_checked = False
        # Prepared statements are cached per instance (table_name is fixed for
        # the instance's lifetime) so the read/write paths only pay CQL
        # parsing cost once per collection instead of on every call.
        self._insert_stmt: PreparedStatement | None = None
        self._ann_query_stmt: PreparedStatement | None = None

    @property
    def table_name(self) -> str:
        """Get the table name for the current collection.

        Sanitizes collection name to ensure valid CQL identifier:
        - Replaces hyphens with underscores
        - Collection name should already be validated to contain only alphanumeric, hyphens, underscores
        """
        if self.collection == "default":
            return self.cfg.table
        # Sanitize collection name for use in table name (replace hyphens with underscores)
        sanitized_collection = self.collection.replace("-", "_")
        return f"{self.cfg.table}_{sanitized_collection}"

    def _ensure_keyspace_selected(self) -> None:
        if self._keyspace_ready:
            return
        # Selecting a non-existent keyspace will error; callers should either
        # create it first (via ensure_schema) or have created it externally.
        self.session.execute(f"USE {self.cfg.keyspace}")
        self._keyspace_ready = True
        # After selecting keyspace, ensure migration has been checked
        self._ensure_schema_migrated()

    def _ensure_schema_migrated(self) -> None:
        """Ensure table schema includes multi-model support and version tracking columns.

        This migration adds embedding_model, embedding_dim, doc_hash, ingested_at,
        and updated_at columns to existing tables. It runs once per instance and is idempotent.
        """
        if self._migration_checked:
            return

        self._migration_checked = True
        ks = self.cfg.keyspace
        tbl = self.table_name

        try:
            # Check which columns exist
            result = self.session.execute(
                f"SELECT column_name FROM system_schema.columns "
                f"WHERE keyspace_name = '{ks}' AND table_name = '{tbl}'"
            )
            existing_columns = {row.column_name for row in result}

            # Add missing columns
            if "embedding_model" not in existing_columns:
                self.session.execute(f"ALTER TABLE {tbl} ADD embedding_model text")
            if "embedding_dim" not in existing_columns:
                self.session.execute(f"ALTER TABLE {tbl} ADD embedding_dim int")
            if "doc_hash" not in existing_columns:
                self.session.execute(f"ALTER TABLE {tbl} ADD doc_hash text")
            if "ingested_at" not in existing_columns:
                self.session.execute(f"ALTER TABLE {tbl} ADD ingested_at timestamp")
            if "updated_at" not in existing_columns:
                self.session.execute(f"ALTER TABLE {tbl} ADD updated_at timestamp")

            # Also create the index on embedding_model
            self.session.execute(
                f"CREATE INDEX IF NOT EXISTS {tbl}_model_idx ON {tbl}(embedding_model)"
            )
        except Exception:
            # Migration might fail if table doesn't exist yet (first run)
            # or if columns already exist (race condition in concurrent access)
            # This is fine - actual operations will reveal any real problems
            pass

    def close(self) -> None:
        """Release this store's reference to the shared pool session.

        The underlying Cluster and Session are *not* shut down here because
        they are shared across all CassandraVectorStore instances via the
        connection pool.  Call :func:`reset_connection_pool` or
        :meth:`CassandraConnectionPool.shutdown` when the process is exiting
        and a full teardown is required.
        """
        # The pool manages the actual connection lifetime.
        # No teardown needed here; call reset_connection_pool() for a full shutdown.

    def reconnect(self) -> bool:
        """Reconnect to Cassandra via the connection pool.

        Delegates to :meth:`CassandraConnectionPool.reconnect` and refreshes
        the local session reference on success.

        Returns:
            ``True`` if reconnection succeeded, ``False`` otherwise.
        """
        success = self._pool.reconnect()
        if success:
            self.session = self._pool.get_session()
            self._keyspace_ready = False
            self._migration_checked = False
            # Prepared statements are bound to the old session; drop the cache
            # so they get re-prepared against the new one on next use.
            self._insert_stmt = None
            self._ann_query_stmt = None
        return success

    # ----------------------------
    # Schema helpers (optional)
    # ----------------------------

    def ensure_schema(self, embedding_dim: int, *, collection: str = "default") -> None:
        """Ensure keyspace, table, and SAI vector index exist.

        Supports multiple embedding models per collection.

        NOTE: embedding_dim must match the VECTOR<FLOAT, N> dimension for the collection.

        Multi-model support:
        - Each collection can use a different embedding model and dimension
        - Creates a separate table per collection for optimal performance
        - Collection names: "default", "nomic768", "all-minilm-384", etc.

        Args:
            embedding_dim: Dimension of embedding vectors for this collection
            collection: Collection name (enables multi-model support)
        """
        ks = self.cfg.keyspace
        base_tbl = self.cfg.table
        # Each collection gets its own table with appropriate vector dimensions
        # Sanitize collection name for use in table name (replace hyphens with underscores)
        sanitized_collection = collection.replace("-", "_")
        tbl = f"{base_tbl}_{sanitized_collection}" if collection != "default" else base_tbl

        self.session.execute(f"""
            CREATE KEYSPACE IF NOT EXISTS {ks}
            WITH REPLICATION = {{'class':'SimpleStrategy','replication_factor':1}};
            """)

        self.session.execute(f"USE {ks}")
        self._keyspace_ready = True

        # Create table with collection-specific vector dimension
        self.session.execute(f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
              doc_id text,
              chunk_id int,
              text text,
              metadata text,
              embedding VECTOR<FLOAT, {int(embedding_dim)}>,
              embedding_model text,
              embedding_dim int,
              doc_hash text,
              ingested_at timestamp,
              updated_at timestamp,
              PRIMARY KEY (doc_id, chunk_id)
            );
            """)

        sai_similarity = _SIMILARITY_SAI_OPTION[self.cfg.similarity_function]
        self.session.execute(f"""
            CREATE INDEX IF NOT EXISTS {tbl}_embedding_sai
            ON {tbl}(embedding) USING 'sai'
            WITH OPTIONS = {{'similarity_function': '{sai_similarity}'}};
            """)

        # Add index on embedding_model for efficient model-based filtering
        self.session.execute(f"""
            CREATE INDEX IF NOT EXISTS {tbl}_model_idx
            ON {tbl}(embedding_model);
            """)

        # Ensure settings table exists
        self.ensure_settings_table()

    # ----------------------------
    # Upsert
    # ----------------------------

    def upsert_chunks(
        self,
        doc_id: str,
        texts: Iterable[str],
        embeddings: Iterable[list[float]],
        metadatas: Iterable[dict] | None = None,
        *,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        doc_hash: str | None = None,
        original_ingested_at: datetime | None = None,
    ) -> None:
        """Upsert document chunks with embeddings.

        Args:
            doc_id: Document identifier
            texts: Text chunks
            embeddings: Embedding vectors
            metadatas: Optional metadata dicts
            embedding_model: Embedding model name (for multi-model support)
            embedding_dim: Embedding dimension (for multi-model support)
            doc_hash: Document content hash (for update detection)
            original_ingested_at: Original ingestion timestamp (preserved on updates)
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        texts_l = list(texts)
        embs_l = list(embeddings)
        metas_l = list(metadatas) if metadatas is not None else [{} for _ in texts_l]

        if not (len(texts_l) == len(embs_l) == len(metas_l)):
            raise VectorStoreError("texts, embeddings, and metadatas length mismatch")

        # Auto-detect dimension from first embedding if not provided
        if embedding_dim is None and embs_l:
            embedding_dim = len(embs_l[0])

        # Current timestamp
        now = datetime.utcnow()
        ingested_at = original_ingested_at if original_ingested_at else now

        if self._insert_stmt is None:
            self._insert_stmt = self.session.prepare(f"""
                INSERT INTO {self.table_name} (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """)
        stmt = self._insert_stmt

        # Group inserts into batches to cut down on network round trips -
        # a single-chunk document skips batching overhead entirely. Batches
        # are flushed once either cassandra_batch_size rows or _MAX_BATCH_BYTES
        # of estimated payload is reached, whichever comes first, so large
        # embeddings/chunks can't build a batch that a real Cassandra cluster
        # rejects with "Batch too large".
        batch_size = max(1, self.cfg.batch_size)

        def _row_params(idx: int, text: str, emb: list[float], meta_json: str) -> tuple:
            return (
                doc_id,
                idx,
                text,
                meta_json,
                emb,
                embedding_model,
                embedding_dim,
                doc_hash,
                ingested_at,
                now,
            )

        pending: list[tuple] = []

        def _flush() -> None:
            if not pending:
                return
            if len(pending) == 1:
                self.session.execute(stmt, pending[0])
            else:
                batch = BatchStatement(batch_type=BatchType.UNLOGGED)
                for params in pending:
                    batch.add(stmt, params)
                self.session.execute(batch)
            pending.clear()

        pending_bytes = 0
        for idx, (text, emb, meta) in enumerate(zip(texts_l, embs_l, metas_l, strict=False)):
            meta_json = json.dumps(meta)
            row_bytes = _estimate_row_bytes(text, emb, meta_json)
            if pending and (
                len(pending) >= batch_size or pending_bytes + row_bytes > _MAX_BATCH_BYTES
            ):
                _flush()
                pending_bytes = 0
            pending.append(_row_params(idx, text, emb, meta_json))
            pending_bytes += row_bytes

        _flush()

    # ----------------------------
    # Query
    # ----------------------------

    def query_by_embedding(
        self,
        embedding: list[float],
        k: int = 5,
        *,
        collect_metrics: bool = False,
        embedding_model: str | None = None,
        metadata_filter: MetadataFilter | None = None,
    ) -> list[dict] | tuple[list[dict], VectorSearchDebugMetrics]:
        """Query by embedding vector with optional metadata filtering.

        Args:
            embedding: Query vector
            k: Number of results to return
            collect_metrics: If True, return tuple of (results, metrics)
            embedding_model: Filter results by embedding model (for multi-model support)
            metadata_filter: Optional metadata filter criteria

        Returns:
            List of result dicts with doc_id, chunk_id, text, metadata, score, embedding_model.
            If collect_metrics=True, returns tuple of (results, VectorSearchDebugMetrics).
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        start_time = time.perf_counter()
        fetch_n = self._ann_fetch_n(k, metadata_filter)
        bound = self._prepare_ann_stmt().bind((embedding, embedding, fetch_n))
        bound.fetch_size = fetch_n

        rows = self.session.execute(bound)
        out, scores = _filter_and_score_rows(rows, k, embedding_model, metadata_filter)

        if collect_metrics:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            metrics = VectorSearchDebugMetrics(
                raw_results_count=len(out),
                score_min=min(scores) if scores else None,
                score_max=max(scores) if scores else None,
                score_mean=sum(scores) / len(scores) if scores else None,
                vector_search_time_ms=elapsed_ms,
            )
            return out, metrics

        return out

    def query_by_embeddings_batch(
        self,
        embeddings: list[list[float]],
        k: int = 5,
        *,
        embedding_model: str | None = None,
        metadata_filter: MetadataFilter | None = None,
    ) -> list[list[dict]]:
        """Search multiple query embeddings in a single batched round trip.

        Uses the driver's :func:`~cassandra.concurrent.execute_concurrent` to
        run all ANN queries concurrently against the shared session instead
        of opening one blocking call per embedding, bounded by
        ``cassandra_batch_query_concurrency`` to cap peak in-flight requests
        (and therefore memory) regardless of how many embeddings are passed.

        Each bound statement has ``fetch_size`` capped to the same tuned
        ``fetch_n`` used by :meth:`query_by_embedding`, so batched searches
        get the same per-query memory bound instead of falling back to the
        driver's default page size (5000 rows).

        Args:
            embeddings: Query vectors to search for, one ANN search each
            k: Number of results to return per embedding
            embedding_model: Filter results by embedding model (for multi-model support)
            metadata_filter: Optional metadata filter criteria

        Returns:
            List of result lists, one per input embedding, in the same order.
            A search that fails (e.g. driver timeout) yields an empty list
            for that position rather than failing the whole batch.
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        if not embeddings:
            return []

        stmt = self._prepare_ann_stmt()
        fetch_n = self._ann_fetch_n(k, metadata_filter)
        statements_and_params = []
        for emb in embeddings:
            bound = stmt.bind((emb, emb, fetch_n))
            bound.fetch_size = fetch_n
            statements_and_params.append((bound, ()))

        responses = execute_concurrent(
            self.session,
            statements_and_params,
            concurrency=self.cfg.batch_query_concurrency,
            raise_on_first_error=False,
        )

        batch_results: list[list[dict]] = []
        for idx, (success, rows_or_exc) in enumerate(responses):
            if not success:
                log.warning("Batch ANN query %d failed: %s", idx, rows_or_exc)
                batch_results.append([])
                continue
            out, _scores = _filter_and_score_rows(rows_or_exc, k, embedding_model, metadata_filter)
            batch_results.append(out)

        return batch_results

    def _ann_fetch_n(self, k: int, metadata_filter: MetadataFilter | None) -> int:
        """Compute how many ANN candidates to fetch for a search of ``k`` results.

        Widens the candidate pool based on how many post-filter predicates are
        active - each active filter can independently reject candidates, so a
        flat multiplier under-fetches once more than one filter is set - and
        further scales by the configurable oversampling factor to trade
        recall for query cost.
        """
        multiplier = _candidate_fetch_multiplier(metadata_filter) * self.cfg.ann_oversample_factor
        return max(k, int(round(k * multiplier)))

    def _prepare_ann_stmt(self) -> PreparedStatement:
        """Prepare (once per instance) and return the ANN search statement.

        Reused across calls to avoid re-parsing the ANN query CQL on every
        search (the table name and similarity function are fixed per instance).
        """
        if self._ann_query_stmt is None:
            similarity_fn = _SIMILARITY_CQL_FUNCTION[self.cfg.similarity_function]
            self._ann_query_stmt = self.session.prepare(f"""
                SELECT doc_id, chunk_id, text, metadata, embedding_model, embedding_dim, ingested_at, {similarity_fn}(embedding, ?) AS score
                FROM {self.table_name}
                ORDER BY embedding ANN OF ?
                LIMIT ?
                """)
        return self._ann_query_stmt

    def list_docs(self) -> list[dict]:
        """Return a list of documents currently stored: {doc_id, chunks, embedding_model}."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Cassandra only supports GROUP BY on PRIMARY KEY columns.
        # We fetch all doc_id, embedding_model pairs and aggregate in Python.
        stmt = SimpleStatement(
            f"SELECT doc_id, embedding_model FROM {self.table_name}",
        )

        rows = self.session.execute(stmt)
        # Aggregate: count chunks per doc_id and capture embedding_model
        doc_info: dict[str, dict] = {}
        for r in rows:
            doc_id = r.doc_id
            if doc_id not in doc_info:
                doc_info[doc_id] = {
                    "doc_id": doc_id,
                    "chunks": 0,
                    "embedding_model": r.embedding_model if hasattr(r, "embedding_model") else None,
                }
            doc_info[doc_id]["chunks"] += 1

        out = list(doc_info.values())
        # Sort for stable output
        out.sort(key=lambda x: x["doc_id"])
        return out

    def delete_doc(self, doc_id: str) -> None:
        """Delete all chunks for the given doc_id."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        self.session.execute(
            SimpleStatement(
                f"DELETE FROM {self.table_name} WHERE doc_id = %s",
            ),
            (doc_id,),
        )

    def truncate(self) -> None:
        """Remove all rows from the vector table (development convenience)."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        self.session.execute(SimpleStatement(f"TRUNCATE {self.table_name}"))

    def drop_collection(self) -> None:
        """Drop the collection table entirely.

        WARNING: This permanently removes the table and all its data.
        Cannot be used on the default collection.
        """
        if self.collection == "default":
            raise ValueError("Cannot drop the default collection")

        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        self.session.execute(SimpleStatement(f"DROP TABLE IF EXISTS {self.table_name}"))

    def list_collections(self) -> list[str]:
        """List all available collections (tables)."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        stmt = SimpleStatement("""
            SELECT table_name
            FROM system_schema.tables
            WHERE keyspace_name = %s
            """)

        rows = self.session.execute(stmt, (self.cfg.keyspace,))
        base_tbl = self.cfg.table
        collections = []
        for r in rows:
            tbl_name = r.table_name
            if tbl_name == base_tbl:
                collections.append("default")
            elif tbl_name.startswith(f"{base_tbl}_"):
                collection_name = tbl_name[len(f"{base_tbl}_") :]
                collections.append(collection_name)
        collections.sort()
        return collections

    def get_document_hash(self, doc_id: str) -> str | None:
        """Get the content hash for a document.

        Args:
            doc_id: Document identifier

        Returns:
            Document hash if it exists, None otherwise
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        stmt = SimpleStatement(f"SELECT doc_hash FROM {self.table_name} WHERE doc_id = %s LIMIT 1")

        rows = self.session.execute(stmt, (doc_id,))
        row = rows.one()
        if row and hasattr(row, "doc_hash"):
            return str(row.doc_hash) if row.doc_hash is not None else None
        return None

    def get_document_info(self, doc_id: str) -> dict | None:
        """Get document version information.

        Args:
            doc_id: Document identifier

        Returns:
            Dict with doc_hash, ingested_at, updated_at, chunks count, or None if not found
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Cassandra only supports GROUP BY on PRIMARY KEY columns.
        # We fetch all rows for doc_id and aggregate in Python.
        stmt = SimpleStatement(f"""
            SELECT doc_id, doc_hash, ingested_at, updated_at, embedding_model
            FROM {self.table_name}
            WHERE doc_id = %s
            """)

        rows = list(self.session.execute(stmt, (doc_id,)))
        if not rows:
            return None

        # All chunks should have the same metadata, take from first row
        row = rows[0]
        return {
            "doc_id": row.doc_id,
            "doc_hash": row.doc_hash if hasattr(row, "doc_hash") else None,
            "ingested_at": (
                row.ingested_at.isoformat()
                if hasattr(row, "ingested_at") and row.ingested_at
                else None
            ),
            "updated_at": (
                row.updated_at.isoformat()
                if hasattr(row, "updated_at") and row.updated_at
                else None
            ),
            "chunks": len(rows),
            "embedding_model": row.embedding_model if hasattr(row, "embedding_model") else None,
        }

    def document_needs_update(self, doc_id: str, new_hash: str) -> bool:
        """Check if a document needs to be updated based on content hash.

        Args:
            doc_id: Document identifier
            new_hash: New content hash to compare

        Returns:
            True if document doesn't exist or hash differs, False otherwise
        """
        existing_hash = self.get_document_hash(doc_id)
        if existing_hash is None:
            return True  # Document doesn't exist, needs ingestion
        return existing_hash != new_hash

    def get_all_chunks(self) -> list[dict]:
        """Get all chunks from the collection.

        Returns all chunks with their text, embeddings, and metadata.
        Useful for re-indexing or bulk operations.

        Returns:
            List of dicts with keys: doc_id, chunk_id, text, metadata, embedding,
            embedding_model, embedding_dim, doc_hash, ingested_at, updated_at
        """
        self._ensure_keyspace_selected()
        tbl = self.table_name

        # Query all chunks from the collection
        query = f"SELECT * FROM {tbl}"
        stmt = SimpleStatement(query, fetch_size=1000)  # Use paging for large collections

        chunks = []
        rows = self.session.execute(stmt)

        for row in rows:
            chunks.append(
                {
                    "doc_id": row.doc_id,
                    "chunk_id": row.chunk_id,
                    "text": row.text,
                    "metadata": row.metadata,
                    "embedding": list(row.embedding) if row.embedding else None,
                    "embedding_model": row.embedding_model,
                    "embedding_dim": row.embedding_dim,
                    "doc_hash": row.doc_hash,
                    "ingested_at": row.ingested_at,
                    "updated_at": row.updated_at,
                }
            )

        return chunks

    # ----------------------------
    # Collection Settings
    # ----------------------------

    def ensure_settings_table(self) -> None:
        """Ensure the collection_settings table exists.

        This table stores per-collection configuration like:
        - Preferred embedding model
        - Default chunk size
        - Default chunk overlap

        Note: This assumes keyspace is already selected or uses fully qualified table name.
        """
        # Use fully qualified table name to work in any context
        self.session.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.cfg.keyspace}.collection_settings (
                collection_name text PRIMARY KEY,
                embedding_model text,
                chunk_size int,
                chunk_overlap int
            );
            """)

    def get_collection_settings(self) -> dict[str, str | int | None]:
        """Get settings for the current collection.

        Returns:
            Dict with keys: embedding_model, chunk_size, chunk_overlap
            All values will be None if no settings have been saved.
        """
        self.ensure_settings_table()

        query = f"""
            SELECT embedding_model, chunk_size, chunk_overlap
            FROM {self.cfg.keyspace}.collection_settings
            WHERE collection_name = %s
        """
        result = self.session.execute(query, [self.collection])
        row = result.one()

        if row is None:
            return {
                "embedding_model": None,
                "chunk_size": None,
                "chunk_overlap": None,
            }

        return {
            "embedding_model": row.embedding_model,
            "chunk_size": row.chunk_size,
            "chunk_overlap": row.chunk_overlap,
        }

    def update_collection_settings(
        self,
        *,
        embedding_model: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> None:
        """Update settings for the current collection.

        Args:
            embedding_model: Preferred embedding model (optional)
            chunk_size: Default chunk size for documents (optional)
            chunk_overlap: Chunk overlap in characters (optional)
        """
        self.ensure_settings_table()

        query = f"""
            INSERT INTO {self.cfg.keyspace}.collection_settings
            (collection_name, embedding_model, chunk_size, chunk_overlap)
            VALUES (%s, %s, %s, %s)
        """
        self.session.execute(query, [self.collection, embedding_model, chunk_size, chunk_overlap])
