from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Iterable, List, Optional
from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import WhiteListRoundRobinPolicy, DowngradingConsistencyRetryPolicy
from cassandra.query import SimpleStatement, BatchStatement, BatchType, ConsistencyLevel

from nyxgpt.config import load_config


@dataclass(frozen=True)
class CassandraConfig:
    hosts: list[str]
    port: int
    keyspace: str
    table: str


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
    doc_ids: Optional[list[str]] = None
    filename: Optional[str] = None
    tags: Optional[list[str]] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


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


def _cassandra_cfg() -> CassandraConfig:
    cfg = load_config(None)

    hosts_raw = cfg.get("rag", "cassandra_hosts", fallback="127.0.0.1")
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    port = cfg.getint("rag", "cassandra_port", fallback=9042)
    keyspace = cfg.get("rag", "cassandra_keyspace", fallback="nyxgpt")
    table = cfg.get("rag", "cassandra_table", fallback="rag_chunks")

    return CassandraConfig(hosts=hosts, port=port, keyspace=keyspace, table=table)


class CassandraVectorStore:
    def __init__(self, *, collection: str = "default") -> None:
        """Initialize Cassandra vector store with optimized connection settings.

        Args:
            collection: Collection name for multi-model support (default: "default")
        """
        self.cfg = _cassandra_cfg()
        self.collection = collection

        # Configure execution profiles for different query types
        profile = ExecutionProfile(
            load_balancing_policy=WhiteListRoundRobinPolicy(self.cfg.hosts),
            retry_policy=DowngradingConsistencyRetryPolicy(),
            consistency_level=ConsistencyLevel.LOCAL_ONE,
            request_timeout=30.0,
        )

        self.cluster = Cluster(
            self.cfg.hosts,
            port=self.cfg.port,
            execution_profiles={EXEC_PROFILE_DEFAULT: profile},
            protocol_version=5,  # Use latest protocol for better performance
        )
        # Connect without a keyspace so we can create it if missing.
        self.session = self.cluster.connect()
        self._keyspace_ready = False
        self._migration_checked = False

        # Cache for prepared statements (lazy initialization)
        self._prepared_stmts: dict[str, any] = {}

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
        self.session.shutdown()
        self.cluster.shutdown()

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

        self.session.execute(
            f"""
            CREATE KEYSPACE IF NOT EXISTS {ks}
            WITH REPLICATION = {{'class':'SimpleStrategy','replication_factor':1}};
            """
        )

        self.session.execute(f"USE {ks}")
        self._keyspace_ready = True

        # Create table with collection-specific vector dimension
        self.session.execute(
            f"""
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
            """
        )

        self.session.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {tbl}_embedding_sai
            ON {tbl}(embedding) USING 'sai';
            """
        )

        # Add index on embedding_model for efficient model-based filtering
        self.session.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {tbl}_model_idx
            ON {tbl}(embedding_model);
            """
        )

        # Ensure settings table exists
        self.ensure_settings_table()

    # ----------------------------
    # Upsert
    # ----------------------------

    def upsert_chunks(
        self,
        doc_id: str,
        texts: Iterable[str],
        embeddings: Iterable[List[float]],
        metadatas: Iterable[dict] | None = None,
        *,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        doc_hash: str | None = None,
        original_ingested_at: datetime | None = None,
    ) -> None:
        """Upsert document chunks with embeddings using batch operations.

        Performance optimizations:
        - Uses prepared statements for query plan caching
        - Batches inserts in groups of 50 to reduce network round-trips
        - Batch size limited to avoid exceeding Cassandra batch size limits

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

        # Current timestamp (timezone-aware)
        now = datetime.now(timezone.utc)

        # Get or create prepared statement (cached)
        stmt_key = f"upsert_{self.table_name}"
        if stmt_key not in self._prepared_stmts:
            self._prepared_stmts[stmt_key] = self.session.prepare(
                f"""
                INSERT INTO {self.table_name} (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            )
        stmt = self._prepared_stmts[stmt_key]

        # Batch inserts in groups of 50 to balance performance and memory
        # Cassandra recommends batch sizes under 100 statements
        BATCH_SIZE = 50
        batch = BatchStatement(batch_type=BatchType.UNLOGGED)
        batch_count = 0

        for idx, (text, emb, meta) in enumerate(zip(texts_l, embs_l, metas_l)):
            # For updates, preserve original ingested_at; for new docs, use current time
            ingested_at = original_ingested_at if original_ingested_at else now

            batch.add(
                stmt,
                (
                    doc_id,
                    idx,
                    text,
                    json.dumps(meta),
                    emb,
                    embedding_model,
                    embedding_dim,
                    doc_hash,
                    ingested_at,
                    now,
                ),
            )
            batch_count += 1

            # Execute batch when it reaches the size limit
            if batch_count >= BATCH_SIZE:
                self.session.execute(batch)
                batch = BatchStatement(batch_type=BatchType.UNLOGGED)
                batch_count = 0

        # Execute any remaining statements in the final batch
        if batch_count > 0:
            self.session.execute(batch)

    # ----------------------------
    # Query
    # ----------------------------

    def query_by_embedding(
        self,
        embedding: List[float],
        k: int = 5,
        *,
        collect_metrics: bool = False,
        embedding_model: str | None = None,
        metadata_filter: Optional[MetadataFilter] = None,
    ) -> list[dict] | tuple[list[dict], VectorSearchDebugMetrics]:
        """Query by embedding vector with optional metadata filtering.

        Performance optimizations:
        - Uses prepared statements for ANN vector search
        - Dynamically adjusts fetch size based on filtering requirements
        - Smart limit calculation to account for post-query filtering

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

        # Calculate smart fetch size and limit based on filtering
        # If filtering is enabled, we need to fetch more results to account for filtering
        filter_multiplier = 3 if metadata_filter else 1
        fetch_limit = k * filter_multiplier
        fetch_size = min(fetch_limit, 1000)  # Cap fetch size to avoid memory issues

        # Use prepared statement for vector search (cached per table)
        stmt_key = f"query_embedding_{self.table_name}"
        if stmt_key not in self._prepared_stmts:
            self._prepared_stmts[stmt_key] = self.session.prepare(
                f"""
                SELECT doc_id, chunk_id, text, metadata, embedding_model, embedding_dim, ingested_at, similarity_cosine(embedding, ?) AS score
                FROM {self.table_name}
                ORDER BY embedding ANN OF ?
                LIMIT ?
                """
            )
        stmt = self._prepared_stmts[stmt_key]
        stmt.fetch_size = fetch_size

        rows = self.session.execute(stmt, (embedding, embedding, fetch_limit))
        out: list[dict] = []
        scores: list[float] = []
        for r in rows:
            # Filter by embedding_model if specified
            if (
                embedding_model is not None
                and hasattr(r, "embedding_model")
                and r.embedding_model != embedding_model
            ):
                continue

            # Parse metadata
            metadata = json.loads(r.metadata) if r.metadata else {}

            # Apply metadata filters
            if metadata_filter:
                # Filter by doc_ids (OR logic)
                if metadata_filter.doc_ids and r.doc_id not in metadata_filter.doc_ids:
                    continue

                # Filter by filename (case-insensitive partial match)
                if metadata_filter.filename:
                    doc_filename = metadata.get("filename", "")
                    if metadata_filter.filename.lower() not in doc_filename.lower():
                        continue

                # Filter by tags (doc must have ALL specified tags)
                if metadata_filter.tags:
                    doc_tags = metadata.get("tags", [])
                    if not isinstance(doc_tags, list):
                        continue
                    if not all(tag in doc_tags for tag in metadata_filter.tags):
                        continue

                # Filter by date range
                if metadata_filter.date_from or metadata_filter.date_to:
                    if not hasattr(r, "ingested_at") or r.ingested_at is None:
                        continue
                    if metadata_filter.date_from and r.ingested_at < metadata_filter.date_from:
                        continue
                    if metadata_filter.date_to and r.ingested_at > metadata_filter.date_to:
                        continue

            score = (
                float(r.score) if hasattr(r, "score") and r.score is not None else 0.0
            )
            result = {
                "doc_id": r.doc_id,
                "chunk_id": r.chunk_id,
                "text": r.text,
                "metadata": metadata,
                "score": score,
                "embedding_model": r.embedding_model
                if hasattr(r, "embedding_model")
                else None,
                "embedding_dim": r.embedding_dim
                if hasattr(r, "embedding_dim")
                else None,
            }
            out.append(result)
            scores.append(score)

            # Stop if we have enough results
            if len(out) >= k:
                break

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

    def list_docs(self) -> list[dict]:
        """Return a list of documents currently stored: {doc_id, chunks, embedding_model}.

        Performance optimization:
        - Uses paging with optimized fetch size for large collections
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Cassandra only supports GROUP BY on PRIMARY KEY columns.
        # We fetch all doc_id, embedding_model pairs and aggregate in Python.
        # Use paging for efficient memory usage with large collections
        stmt = SimpleStatement(
            f"SELECT doc_id, embedding_model FROM {self.table_name}",
            fetch_size=5000,  # Optimized fetch size for large collections
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
                    "embedding_model": r.embedding_model
                    if hasattr(r, "embedding_model")
                    else None,
                }
            doc_info[doc_id]["chunks"] += 1

        out = list(doc_info.values())
        # Sort for stable output
        out.sort(key=lambda x: x["doc_id"])
        return out

    def delete_doc(self, doc_id: str) -> None:
        """Delete all chunks for the given doc_id using prepared statement."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Use prepared statement for better performance
        stmt_key = f"delete_doc_{self.table_name}"
        if stmt_key not in self._prepared_stmts:
            self._prepared_stmts[stmt_key] = self.session.prepare(
                f"DELETE FROM {self.table_name} WHERE doc_id = ?"
            )
        stmt = self._prepared_stmts[stmt_key]

        self.session.execute(stmt, (doc_id,))

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

        stmt = SimpleStatement(
            """
            SELECT table_name
            FROM system_schema.tables
            WHERE keyspace_name = %s
            """
        )

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
        """Get the content hash for a document using prepared statement.

        Args:
            doc_id: Document identifier

        Returns:
            Document hash if it exists, None otherwise
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Use prepared statement for better performance
        stmt_key = f"get_doc_hash_{self.table_name}"
        if stmt_key not in self._prepared_stmts:
            self._prepared_stmts[stmt_key] = self.session.prepare(
                f"SELECT doc_hash FROM {self.table_name} WHERE doc_id = ? LIMIT 1"
            )
        stmt = self._prepared_stmts[stmt_key]

        rows = self.session.execute(stmt, (doc_id,))
        row = rows.one()
        if row and hasattr(row, "doc_hash"):
            return str(row.doc_hash) if row.doc_hash is not None else None
        return None

    def get_document_info(self, doc_id: str) -> dict | None:
        """Get document version information using prepared statement.

        Args:
            doc_id: Document identifier

        Returns:
            Dict with doc_hash, ingested_at, updated_at, chunks count, or None if not found
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        # Use prepared statement for better performance
        stmt_key = f"get_doc_info_{self.table_name}"
        if stmt_key not in self._prepared_stmts:
            self._prepared_stmts[stmt_key] = self.session.prepare(
                f"""
                SELECT doc_id, doc_hash, ingested_at, updated_at, embedding_model
                FROM {self.table_name}
                WHERE doc_id = ?
                """
            )
        stmt = self._prepared_stmts[stmt_key]

        rows = list(self.session.execute(stmt, (doc_id,)))
        if not rows:
            return None

        # All chunks should have the same metadata, take from first row
        row = rows[0]
        return {
            "doc_id": row.doc_id,
            "doc_hash": row.doc_hash if hasattr(row, "doc_hash") else None,
            "ingested_at": row.ingested_at.isoformat()
            if hasattr(row, "ingested_at") and row.ingested_at
            else None,
            "updated_at": row.updated_at.isoformat()
            if hasattr(row, "updated_at") and row.updated_at
            else None,
            "chunks": len(rows),
            "embedding_model": row.embedding_model
            if hasattr(row, "embedding_model")
            else None,
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
            chunks.append({
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
            })

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
        self.session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.cfg.keyspace}.collection_settings (
                collection_name text PRIMARY KEY,
                embedding_model text,
                chunk_size int,
                chunk_overlap int
            );
            """
        )

    def get_collection_settings(self) -> dict[str, Optional[str | int]]:
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
        embedding_model: Optional[str] = None,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
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
        self.session.execute(
            query,
            [self.collection, embedding_model, chunk_size, chunk_overlap]
        )
