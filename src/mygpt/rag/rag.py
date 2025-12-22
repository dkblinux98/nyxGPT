

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from mygpt.rag.embeddings import embed_texts, embed_text
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore
from mygpt.config import load_config


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int
    overlap: int


@dataclass(frozen=True)
class RAGConfig:
    top_k: int


class RAGError(RuntimeError):
    pass


def _chunking_cfg() -> ChunkingConfig:
    cfg = load_config(None)
    size = cfg.getint("rag", "chunk_size", fallback=800)
    overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
    if overlap >= size:
        raise RAGError("chunk_overlap must be smaller than chunk_size")
    return ChunkingConfig(chunk_size=size, overlap=overlap)


def _rag_cfg() -> RAGConfig:
    cfg = load_config(None)
    top_k = cfg.getint("rag", "top_k", fallback=5)
    return RAGConfig(top_k=top_k)


# ----------------------------
# Chunking
# ----------------------------


def chunk_text(text: str) -> list[str]:
    """Deterministically chunk text by character count.

    This keeps behavior simple and reproducible. We can swap in
    token-based chunking later if needed.
    """
    cfg = _chunking_cfg()
    chunks: list[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + cfg.chunk_size, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start = end - cfg.overlap

    return chunks


# ----------------------------
# Ingest
# ----------------------------


def ingest_document(
    doc_id: str,
    text: str,
    metadata: dict | None = None,
    ensure_schema: bool = False,
) -> int:
    """Chunk, embed, and store a document.

    Returns the number of chunks ingested.
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks)

    metas = [metadata or {} for _ in chunks]

    store = CassandraVectorStore()
    try:
        if ensure_schema:
            # embedding dimension inferred from first vector
            store.ensure_schema(len(embeddings[0]))

        store.upsert_chunks(
            doc_id=doc_id,
            texts=chunks,
            embeddings=embeddings,
            metadatas=metas,
        )
    finally:
        store.close()

    return len(chunks)


# ----------------------------
# Retrieve
# ----------------------------


def retrieve_context(query: str) -> list[dict]:
    """Retrieve top-k similar chunks for a query string."""
    cfg = _rag_cfg()
    q_emb = embed_text(query)

    store = CassandraVectorStore()
    try:
        results = store.query_by_embedding(q_emb, k=cfg.top_k)
    finally:
        store.close()

    return results


# ----------------------------
# Compose
# ----------------------------


def compose_context(results: Iterable[dict]) -> str:
    """Compose retrieved chunks into a single context string."""
    parts: List[str] = []
    for r in results:
        text = r.get("text", "")
        if text:
            parts.append(text.strip())
    return "\n\n".join(parts)