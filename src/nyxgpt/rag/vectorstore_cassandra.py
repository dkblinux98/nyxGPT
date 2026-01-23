from __future__ import annotations

import json
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Iterable, List
from cassandra.cluster import Cluster
from cassandra.query import SimpleStatement

from nyxgpt.config import load_config


@dataclass(frozen=True)
class CassandraConfig:
    hosts: list[str]
    port: int
    keyspace: str
    table: str


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
        """Initialize Cassandra vector store.

        Args:
            collection: Collection name for multi-model support (default: "default")
        """
        self.cfg = _cassandra_cfg()
        self.collection = collection
        self.cluster = Cluster(self.cfg.hosts, port=self.cfg.port)
        # Connect without a keyspace so we can create it if missing.
        self.session = self.cluster.connect()
        self._keyspace_ready = False
        self._migration_checked = False

    @property
    def table_name(self) -> str:
        """Get the table name for the current collection."""
        if self.collection == "default":
            return self.cfg.table
        return f"{self.cfg.table}_{self.collection}"

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
        tbl = f"{base_tbl}_{collection}" if collection != "default" else base_tbl

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

        stmt = self.session.prepare(
            f"""
            INSERT INTO {self.table_name} (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )

        for idx, (text, emb, meta) in enumerate(zip(texts_l, embs_l, metas_l)):
            # For updates, preserve original ingested_at; for new docs, use current time
            ingested_at = original_ingested_at if original_ingested_at else now
            self.session.execute(
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
    ) -> list[dict] | tuple[list[dict], VectorSearchDebugMetrics]:
        """Query by embedding vector.

        Args:
            embedding: Query vector
            k: Number of results to return
            collect_metrics: If True, return tuple of (results, metrics)
            embedding_model: Filter results by embedding model (for multi-model support)

        Returns:
            List of result dicts with doc_id, chunk_id, text, metadata, score, embedding_model.
            If collect_metrics=True, returns tuple of (results, VectorSearchDebugMetrics).
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        start_time = time.perf_counter()

        # Build query with optional model filtering
        stmt = SimpleStatement(
            f"""
            SELECT doc_id, chunk_id, text, metadata, embedding_model, embedding_dim, similarity_cosine(embedding, %s) AS score
            FROM {self.table_name}
            ORDER BY embedding ANN OF %s
            LIMIT %s
            """,
            fetch_size=k,
        )

        rows = self.session.execute(stmt, (embedding, embedding, k))
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

            score = (
                float(r.score) if hasattr(r, "score") and r.score is not None else 0.0
            )
            out.append(
                {
                    "doc_id": r.doc_id,
                    "chunk_id": r.chunk_id,
                    "text": r.text,
                    "metadata": json.loads(r.metadata) if r.metadata else {},
                    "score": score,
                    "embedding_model": r.embedding_model
                    if hasattr(r, "embedding_model")
                    else None,
                    "embedding_dim": r.embedding_dim
                    if hasattr(r, "embedding_dim")
                    else None,
                }
            )
            scores.append(score)

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
        """Get the content hash for a document.

        Args:
            doc_id: Document identifier

        Returns:
            Document hash if it exists, None otherwise
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        stmt = SimpleStatement(
            f"SELECT doc_hash FROM {self.table_name} WHERE doc_id = %s LIMIT 1"
        )

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
        stmt = SimpleStatement(
            f"""
            SELECT doc_id, doc_hash, ingested_at, updated_at, embedding_model
            FROM {self.table_name}
            WHERE doc_id = %s
            """
        )

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
