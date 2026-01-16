from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterable, List
from cassandra.cluster import Cluster
from cassandra.query import SimpleStatement

from mygpt.config import load_config


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
    keyspace = cfg.get("rag", "cassandra_keyspace", fallback="mygpt")
    table = cfg.get("rag", "cassandra_table", fallback="rag_chunks")

    return CassandraConfig(hosts=hosts, port=port, keyspace=keyspace, table=table)


class CassandraVectorStore:
    def __init__(self) -> None:
        self.cfg = _cassandra_cfg()
        self.cluster = Cluster(self.cfg.hosts, port=self.cfg.port)
        # Connect without a keyspace so we can create it if missing.
        self.session = self.cluster.connect()
        self._keyspace_ready = False

    def _ensure_keyspace_selected(self) -> None:
        if self._keyspace_ready:
            return
        # Selecting a non-existent keyspace will error; callers should either
        # create it first (via ensure_schema) or have created it externally.
        self.session.execute(f"USE {self.cfg.keyspace}")
        self._keyspace_ready = True

    def close(self) -> None:
        self.session.shutdown()
        self.cluster.shutdown()

    # ----------------------------
    # Schema helpers (optional)
    # ----------------------------

    def ensure_schema(self, embedding_dim: int) -> None:
        """Ensure keyspace, table, and SAI vector index exist.

        NOTE: embedding_dim must match the VECTOR<FLOAT, N> dimension.
        """
        ks = self.cfg.keyspace
        tbl = self.cfg.table

        self.session.execute(
            f"""
            CREATE KEYSPACE IF NOT EXISTS {ks}
            WITH REPLICATION = {{'class':'SimpleStrategy','replication_factor':1}};
            """
        )

        self.session.execute(f"USE {ks}")
        self._keyspace_ready = True

        self.session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
              doc_id text,
              chunk_id int,
              text text,
              metadata text,
              embedding VECTOR<FLOAT, {int(embedding_dim)}>,
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

    # ----------------------------
    # Upsert
    # ----------------------------

    def upsert_chunks(
        self,
        doc_id: str,
        texts: Iterable[str],
        embeddings: Iterable[List[float]],
        metadatas: Iterable[dict] | None = None,
    ) -> None:
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        texts_l = list(texts)
        embs_l = list(embeddings)
        metas_l = list(metadatas) if metadatas is not None else [{} for _ in texts_l]

        if not (len(texts_l) == len(embs_l) == len(metas_l)):
            raise VectorStoreError("texts, embeddings, and metadatas length mismatch")

        stmt = self.session.prepare(
            f"""
            INSERT INTO {self.cfg.table} (doc_id, chunk_id, text, metadata, embedding)
            VALUES (?, ?, ?, ?, ?)
            """
        )

        for idx, (text, emb, meta) in enumerate(zip(texts_l, embs_l, metas_l)):
            self.session.execute(
                stmt,
                (doc_id, idx, text, json.dumps(meta), emb),
            )

    # ----------------------------
    # Query
    # ----------------------------

    def query_by_embedding(
        self,
        embedding: List[float],
        k: int = 5,
        *,
        collect_metrics: bool = False
    ) -> list[dict] | tuple[list[dict], VectorSearchDebugMetrics]:
        """Query by embedding vector.

        Args:
            embedding: Query vector
            k: Number of results to return
            collect_metrics: If True, return tuple of (results, metrics)

        Returns:
            List of result dicts with doc_id, chunk_id, text, metadata, score.
            If collect_metrics=True, returns tuple of (results, VectorSearchDebugMetrics).
        """
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        start_time = time.perf_counter()

        stmt = SimpleStatement(
            f"""
            SELECT doc_id, chunk_id, text, metadata, similarity_cosine(embedding, %s) AS score
            FROM {self.cfg.table}
            ORDER BY embedding ANN OF %s
            LIMIT %s
            """,
            fetch_size=k,
        )

        rows = self.session.execute(stmt, (embedding, embedding, k))
        out: list[dict] = []
        scores: list[float] = []
        for r in rows:
            score = float(r.score) if hasattr(r, 'score') and r.score is not None else 0.0
            out.append(
                {
                    "doc_id": r.doc_id,
                    "chunk_id": r.chunk_id,
                    "text": r.text,
                    "metadata": json.loads(r.metadata) if r.metadata else {},
                    "score": score,
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
        """Return a list of documents currently stored: {doc_id, chunks}."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        stmt = SimpleStatement(
            f"""
            SELECT doc_id, count(*) as chunks
            FROM {self.cfg.table}
            GROUP BY doc_id
            """,
        )

        rows = self.session.execute(stmt)
        out: list[dict] = []
        for r in rows:
            out.append({"doc_id": r.doc_id, "chunks": int(r.chunks)})
        # Sort for stable output
        out.sort(key=lambda x: x["doc_id"])
        return out

    def delete_doc(self, doc_id: str) -> None:
        """Delete all chunks for the given doc_id."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        self.session.execute(
            SimpleStatement(
                f"DELETE FROM {self.cfg.table} WHERE doc_id = %s",
            ),
            (doc_id,),
        )

    def truncate(self) -> None:
        """Remove all rows from the vector table (development convenience)."""
        if not self._keyspace_ready:
            self._ensure_keyspace_selected()

        self.session.execute(SimpleStatement(f"TRUNCATE {self.cfg.table}"))